"""Daemon main loop — ties IO, state machine, and controller together."""

from __future__ import annotations

import logging
import signal
import sys
import threading
import time
from dataclasses import asdict
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .config import Config
    from .io.apsystems import APSystemsClient
    from .io.evcc import EvccClient
    from .io.growatt import GrowattClient
    from .state import Action, State, WorldState

logger = logging.getLogger("curtailment")

# Per-source failure counters for persistent-IO-error detection
FAIL_SOURCES = ["growatt", "apsystems", "evcc"]
FAIL_THRESHOLD = 3

READ_RETRY_COUNT = 3
READ_RETRY_DELAY_S = 5


class Daemon:
    """Main daemon that runs the curtailment control loop."""

    def __init__(
        self,
        cfg: Config,
        growatt: GrowattClient,
        apsystems: APSystemsClient,
        evcc: EvccClient,
    ) -> None:
        self.cfg = cfg
        self.growatt = growatt
        self.apsystems = apsystems
        self.evcc = evcc
        self._shutdown = False
        self._state: State | None = None
        self._time_in_state: float = 0.0
        self._state_start: float = 0.0
        self._fail_counts: dict[str, int] = {src: 0 for src in FAIL_SOURCES}
        self._fail_logged: dict[str, bool] = {src: False for src in FAIL_SOURCES}
        self._grid_power_samples: list[float] = []
        self._status_lock = threading.Lock()
        self._started_at = time.time()
        self._last_cycle_at: float | None = None
        self._last_world: dict | None = None
        self._last_action: dict | None = None
        self._last_error: str | None = None
        self._web = None

    def run(self) -> None:
        """Main loop. Runs until SIGTERM/SIGINT."""
        self._install_signal_handlers()
        self._start_web()

        try:
            self._connect_all()
            self._state = self._reconcile_startup()
            self._state_start = time.monotonic()

            while not self._shutdown:
                self._run_cycle()
        except Exception:
            logger.exception("Fatal error in daemon")
            raise
        finally:
            self._cleanup_on_shutdown()
            self._disconnect_all()
            if self._web is not None:
                self._web.stop()
            logger.info("Daemon stopped")

    def _start_web(self) -> None:
        web_cfg = getattr(self.cfg, "web", None)
        if web_cfg is None or not web_cfg.enabled:
            return
        from .web import StatusServer

        try:
            self._web = StatusServer(web_cfg.host, web_cfg.port, self.get_status)
            self._web.start()
        except OSError as e:
            logger.warning("Could not start status web server: %s", e)
            self._web = None

    def get_status(self) -> dict:
        """Thread-safe snapshot of daemon state for the status web server."""
        now = time.time()
        with self._status_lock:
            last = self._last_cycle_at
            daemon = {
                "state": self._state.value if self._state else "none",
                "time_in_state_s": round(time.monotonic() - self._state_start) if self._state_start else 0,
                "uptime_s": round(now - self._started_at),
                "last_cycle": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(last)) if last else "never",
                "last_cycle_age_s": round(now - last) if last else None,
                "last_error": self._last_error,
                "fail_counts": dict(self._fail_counts),
            }
            return {
                "daemon": daemon,
                "world": dict(self._last_world or {}),
                "last_action": dict(self._last_action or {}),
            }

    def _run_cycle(self) -> None:
        """Execute one control cycle."""
        from .state import Action, WorldState, decide_action
        logger.info("Running control cycle...")

        cycle_start = time.monotonic()
        time_in_state = cycle_start - self._state_start

        for attempt in range(READ_RETRY_COUNT):
            try:
                world = self._read_world()
                break
            except Exception as e:
                if attempt < READ_RETRY_COUNT - 1:
                    logger.warning(
                        "Failed to read world state: %s — retry %d/%d in %ds",
                        e, attempt + 1, READ_RETRY_COUNT - 1, READ_RETRY_DELAY_S,
                    )
                    time.sleep(READ_RETRY_DELAY_S)
                else:
                    logger.warning(
                        "Failed to read world state after %d attempts: %s — skipping cycle",
                        READ_RETRY_COUNT, e,
                    )
                    with self._status_lock:
                        self._last_error = f"read world failed: {e}"
                    time.sleep(self.cfg.control.period_seconds)
                    return

        state_value: State = self._state  # type: ignore
        action: Action = decide_action(state_value, world, self.cfg, time_in_state)

        exec_error: str | None = None
        try:
            self._execute_action(action)
        except Exception as e:
            logger.warning("Failed to execute action: %s — continuing", e)
            exec_error = f"execute action failed: {e}"

        # Emit per-cycle structured log (spec §13)
        logger.info(
            "cycle",
            extra={
                "state": self._state.value if self._state else "none",  # type: ignore
                "injection_tariff": world.injection_tariff,
                "consumption_tariff": world.consumption_tariff,
                "grid_power_avg": world.grid_power,
                "grid_power_raw": world.grid_power_raw,
                "growatt_pv_power": world.growatt_pv_power,
                "battery_power": world.battery_power,
                "growatt_export_limit_active": world.growatt_export_limit_active,
                "growatt_export_limit_pct": world.growatt_export_limit_pct,
                "apsystems_pct": world.current_apsystems_pct,
                "decision_reason": action.reason,
            },
        )

        # State transition
        if action.next_state != self._state:
            logger.info(
                "State transition: %s → %s (%s)",
                self._state.value if self._state else "none",  # type: ignore
                action.next_state.value,
                action.reason,
            )
            self._state = action.next_state
            self._state_start = time.monotonic()

        with self._status_lock:
            self._last_cycle_at = time.time()
            self._last_world = asdict(world)
            self._last_action = {
                k: (v.value if hasattr(v, "value") else v) for k, v in asdict(action).items()
            }
            self._last_error = exec_error

        # Wait — sample grid power throughout the idle period
        elapsed = time.monotonic() - cycle_start
        wait = max(0.0, action.wait_seconds - elapsed)
        self._sample_grid_power_during_wait(wait)

    def _sample_grid_power_during_wait(self, duration_seconds: float) -> None:
        """Sample grid power at regular intervals while waiting for the next cycle."""
        if duration_seconds <= 0 or self._shutdown:
            return

        interval = self.cfg.control.grid_sample_interval_seconds
        deadline = time.monotonic() + duration_seconds

        while not self._shutdown:
            try:
                sample = self.growatt.read_grid_power()
                self._grid_power_samples.append(sample)
            except Exception as e:
                logger.debug("Grid power sample skipped during wait: %s", e)

            now = time.monotonic()
            next_sample_at = now + interval
            if next_sample_at >= deadline:
                remaining = deadline - now
                if remaining > 0 and not self._shutdown:
                    time.sleep(remaining)
                break
            time.sleep(interval)

    def read_world(self) -> WorldState:
        """Public method to read world state. Used by scripts."""
        return self._read_world()

    def _read_world(self) -> WorldState:
        """Read all external state needed for a decision."""
        from .state import WorldState
        from .util.smoothing import sample_median

        ctrl_cfg = self.cfg.control

        # Read tariffs (single reads, no smoothing needed)
        injection_tariff = self._read_evcc_injection_tariff()
        consumption_tariff = self._read_evcc_consumption_tariff()
        ev_mode = self._read_evcc_loadpoint_mode()

        # Grid power: consume samples accumulated during the previous wait period.
        # On the first cycle the buffer is empty — fall back to a direct read.
        if self._grid_power_samples:
            grid_power_raw = self._grid_power_samples[-1]
            grid_power = sum(self._grid_power_samples) / len(self._grid_power_samples)
            self._grid_power_samples.clear()
        else:
            val = self.growatt.read_grid_power()
            grid_power_raw = val
            grid_power = val

        pv_power = sample_median(
            self.growatt.read_pv_power,
            count=ctrl_cfg.sample_count,
            interval_seconds=ctrl_cfg.sample_interval_seconds,
        )
        battery_power = self._read_growatt_battery_power()

        limit_status = self._read_growatt_export_limit_status()

        # Reconnect APSystems right before reading — ECU drops idle connections
        # and Growatt sampling above takes ~30s, which exceeds the ECU idle timeout.
        self._read_apsystems_connection()
        apsystems_pct = self._read_apsystems_power_limit()

        return WorldState(
            injection_tariff=injection_tariff,
            consumption_tariff=consumption_tariff,
            grid_power=grid_power,
            grid_power_raw=grid_power_raw,
            growatt_pv_power=pv_power,
            battery_power=battery_power,
            growatt_export_limit_active=limit_status.enabled,
            growatt_export_limit_pct=limit_status.limit_pct,
            current_apsystems_pct=apsystems_pct,
            current_ev_mode=ev_mode,
        )

    def _read_growatt_world(self) -> dict:
        """Read Growatt-specific world state. Returns dict for partial updates."""
        return {}

    def _read_apsystems_connection(self) -> None:
        """Ensure APSystems connection is alive."""
        try:
            self.apsystems.ensure_connected()
            self._reset_fail("apsystems")
        except Exception as e:
            self._track_fail("apsystems", e, "APSystems connection check failed")

    def _read_growatt_battery_power(self) -> float:
        """Read battery power with failure tracking."""
        try:
            result = self.growatt.read_battery_power()
            self._reset_fail("growatt")
            return result
        except Exception as e:
            self._track_fail("growatt", e, "Growatt battery power read failed")
            raise

    def _read_growatt_export_limit_status(self):
        """Read Growatt export limit status with failure tracking."""
        try:
            result = self.growatt.read_export_limit_status()
            self._reset_fail("growatt")
            return result
        except Exception as e:
            self._track_fail("growatt", e, "Growatt export limit status read failed")
            raise

    def _read_apsystems_power_limit(self) -> int:
        """Read APSystems power limit with failure tracking."""
        try:
            result = self.apsystems.read_power_limit()
            self._reset_fail("apsystems")
            return result
        except Exception as e:
            self._track_fail("apsystems", e, "APSystems power limit read failed")
            raise

    def _read_evcc_injection_tariff(self) -> float:
        """Read injection tariff with failure tracking."""
        try:
            result = self.evcc.get_injection_tariff()
            self._reset_fail("evcc")
            return result
        except Exception as e:
            self._track_fail("evcc", e, "evcc injection tariff read failed")
            raise

    def _read_evcc_consumption_tariff(self) -> float:
        """Read consumption tariff with failure tracking."""
        try:
            result = self.evcc.get_consumption_tariff()
            self._reset_fail("evcc")
            return result
        except Exception as e:
            self._track_fail("evcc", e, "evcc consumption tariff read failed")
            raise

    def _read_evcc_loadpoint_mode(self) -> str:
        """Read EV loadpoint mode with failure tracking."""
        try:
            result = self.evcc.get_loadpoint_mode(self.cfg.evcc.ev_loadpoint_id)
            self._reset_fail("evcc")
            return result
        except Exception as e:
            self._track_fail("evcc", e, "evcc loadpoint mode read failed")
            raise

    def _track_fail(self, source: str, exc: Exception, msg: str) -> None:
        """Track a failure for a source and log if threshold crossed."""
        self._fail_counts[source] += 1
        count = self._fail_counts[source]
        if count >= FAIL_THRESHOLD and not self._fail_logged[source]:
            logger.error(
                "Persistent IO failure on %s: %s consecutive failures — %s: %s",
                source, count, msg, exc,
            )
            self._fail_logged[source] = True
        # Re-raise so the caller knows
        raise

    def _reset_fail(self, source: str) -> None:
        """Reset failure counter for a source on success."""
        if self._fail_counts[source] > 0:
            self._fail_counts[source] = 0
            self._fail_logged[source] = False

    def _execute_action(self, action: Action) -> None:
        """Apply each non-null field of Action to the right IO client."""
        if action.set_growatt_export_limit_pct is not None:
            self.growatt.set_export_limit(action.set_growatt_export_limit_pct)

        if action.disable_growatt_export_limit:
            self.growatt.disable_export_limit()

        if action.enable_apsystems_power_limit:
            self.apsystems.enable_power_limit()

        if action.set_apsystems_pct is not None:
            self.apsystems.set_power_limit(action.set_apsystems_pct)

        if action.disable_apsystems_power_limit:
            self.apsystems.disable_power_limit()

        if action.set_ev_mode is not None:
            self.evcc.set_loadpoint_mode(
                self.cfg.evcc.ev_loadpoint_id, action.set_ev_mode
            )

    def _cleanup_on_shutdown(self) -> None:
        """Restore uncurtailed defaults if stopping while curtailment may be active."""
        from .state import Action, State

        if self._state is None or self._state == State.IDLE:
            return

        logger.info("Shutdown in state %s — restoring uncurtailed defaults", self._state.value)
        restore_ev = None
        try:
            if self._read_evcc_loadpoint_mode() == "minpv":
                restore_ev = self.cfg.evcc.default_ev_mode
        except Exception as e:
            logger.warning("Could not read EV mode during shutdown cleanup: %s", e)

        # Run each step independently so one failing device doesn't block the others
        steps = [
            ("growatt export limit", lambda: self._execute_action(
                Action(disable_growatt_export_limit=True))),
            ("apsystems power limit", lambda: self._execute_action(
                Action(set_apsystems_pct=100, disable_apsystems_power_limit=True))),
        ]
        if restore_ev is not None:
            steps.append(("ev mode", lambda: self._execute_action(
                Action(set_ev_mode=restore_ev))))
        for name, step in steps:
            try:
                step()
            except Exception as e:
                logger.error("Shutdown cleanup failed for %s: %s", name, e)
        self._state = State.IDLE

    def _reconcile_startup(self) -> State:
        """Determine initial state from external system state. Retries on failure."""
        from .state import WorldState, reconcile_startup

        max_retries = 3
        backoff = 5  # seconds

        for attempt in range(max_retries):
            try:
                world = self._read_world()
                return reconcile_startup(world, self.cfg)
            except Exception as e:
                if attempt < max_retries - 1:
                    logger.warning(
                        "Startup reconciliation failed (attempt %s/%s): %s — retrying in %ss",
                        attempt + 1, max_retries, e, backoff,
                    )
                    time.sleep(backoff)
                else:
                    logger.error(
                        "Startup reconciliation failed after %s attempts, defaulting to IDLE: %s",
                        max_retries, e,
                    )
                    return __import__("curtailment.state", fromlist=["State"]).State.IDLE

    def _connect_all(self) -> None:
        logger.info("Connecting to all services...")
        self.growatt.connect()
        self.apsystems.connect()
        self.apsystems.warm_up()
        self.evcc.connect()
        logger.info("All connections established")
        self._log_startup_status()

    def _log_startup_status(self) -> None:
        """Log current device state at startup for operator awareness."""
        try:
            grid = self.growatt.read_grid_power()
            pv = self.growatt.read_pv_power()
            battery = self.growatt.read_battery_power()
            export_limit = self.growatt.read_export_limit_status()
            logger.info(
                "Growatt startup status",
                extra={
                    "grid_power_w": grid,
                    "pv_power_w": pv,
                    "battery_power_w": battery,
                    "export_limit_enabled": export_limit.enabled,
                    "export_limit_pct": export_limit.limit_pct,
                },
            )
        except Exception as e:
            logger.warning("Could not read Growatt startup status: %s", e)

        try:
            self.apsystems.ensure_connected()
            aps_pct = self.apsystems.read_power_limit()
            logger.info(
                "APSystems startup status",
                extra={"power_limit_pct": aps_pct},
            )
        except Exception as e:
            logger.warning("Could not read APSystems startup status: %s", e)

    def _disconnect_all(self) -> None:
        logger.info("Disconnecting from all services...")
        for client in [self.growatt, self.apsystems, self.evcc]:
            try:
                client.close()
            except Exception:
                pass

    def _install_signal_handlers(self) -> None:
        def handle_signal(signum, frame):  # type: ignore
            logger.info("Received signal %s, shutting down...", signum)
            self._shutdown = True

        signal.signal(signal.SIGTERM, handle_signal)
        signal.signal(signal.SIGINT, handle_signal)
