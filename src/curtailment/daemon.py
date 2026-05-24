"""Daemon main loop — ties IO, state machine, and controller together."""

from __future__ import annotations

import logging
import signal
import sys
import time
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .config import Config
    from .io.apsystems import APSystemsClient
    from .io.evcc import EvccClient
    from .io.growatt import GrowattClient
    from .state import Action, State, WorldState

logger = logging.getLogger("curtailment")


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

    def run(self) -> None:
        """Main loop. Runs until SIGTERM/SIGINT."""
        self._install_signal_handlers()

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
            self._disconnect_all()
            logger.info("Daemon stopped")

    def _run_cycle(self) -> None:
        """Execute one control cycle."""
        from .state import Action, WorldState, decide_action

        cycle_start = time.monotonic()
        time_in_state = cycle_start - self._state_start

        try:
            world = self._read_world()
        except Exception as e:
            logger.warning("Failed to read world state: %s — retrying next cycle", e)
            time.sleep(self.cfg.control.period_seconds)
            return

        state_value: State = self._state  # type: ignore
        action: Action = decide_action(state_value, world, self.cfg, time_in_state)

        try:
            self._execute_action(action)
        except Exception as e:
            logger.warning("Failed to execute action: %s — continuing", e)

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

        # Wait
        elapsed = time.monotonic() - cycle_start
        wait = max(1, action.wait_seconds - int(elapsed))
        if not self._shutdown and wait > 0:
            time.sleep(wait)

    def _read_world(self) -> WorldState:
        """Read all external state needed for a decision."""
        from .state import WorldState

        # Ensure APSystems connection is alive (ECU drops idle connections)
        self.apsystems.ensure_connected()

        # Read tariff
        tariff = self.evcc.get_injection_tariff()

        # Read Growatt
        grid_power = self.growatt.read_grid_power()
        pv_power = self.growatt.read_pv_power()
        limit_status = self.growatt.read_export_limit_status()

        # Read APsystems
        apsystems_pct = self.apsystems.read_power_limit()

        # Read EV mode
        ev_mode = self.evcc.get_loadpoint_mode(self.cfg.evcc.ev_loadpoint_id)

        # Compute effective export limit watts
        if limit_status.enabled and limit_status.limit_pct is not None:
            # Approximate: limit_pct % of a nominal max (e.g. 4000W)
            # In practice, we track what we set
            growatt_limit_watts = (
                abs(self.cfg.control.growatt_export_limit_pct) / 100.0 * 4000.0
            )
        else:
            growatt_limit_watts = 0.0

        return WorldState(
            tariff=tariff,
            grid_power=grid_power,
            growatt_pv_power=pv_power,
            growatt_export_limit_active=limit_status.enabled,
            growatt_export_limit_watts=growatt_limit_watts,
            current_apsystems_pct=apsystems_pct,
            current_ev_mode=ev_mode,
        )

    def _execute_action(self, action: Action) -> None:
        """Apply each non-null field of Action to the right IO client."""
        if action.set_growatt_export_limit_pct is not None:
            self.growatt.set_export_limit(action.set_growatt_export_limit_pct)

        if action.disable_growatt_export_limit:
            self.growatt.disable_export_limit()

        if action.set_apsystems_pct is not None:
            self.apsystems.set_power_limit(action.set_apsystems_pct)

        if action.set_ev_mode is not None:
            self.evcc.set_loadpoint_mode(
                self.cfg.evcc.ev_loadpoint_id, action.set_ev_mode
            )

    def _reconcile_startup(self) -> State:
        """Determine initial state from external system state."""
        from .state import WorldState, reconcile_startup

        try:
            world = self._read_world()
        except Exception:
            logger.warning("Startup reconciliation failed, defaulting to IDLE")
            return __import__("curtailment.state", fromlist=["State"]).State.IDLE

        return reconcile_startup(world, self.cfg)

    def _connect_all(self) -> None:
        logger.info("Connecting to all services...")
        self.growatt.connect()
        self.apsystems.connect()
        self.apsystems.warm_up()
        self.evcc.connect()
        logger.info("All connections established")

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
