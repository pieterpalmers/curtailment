"""evcc battery mode bridge: legacy Growatt registers -> VPP remote power control.

evcc's growatt-hybrid battery template writes the legacy "battery first" (1102)
and "AC charge" (1092) holding registers. With VPP enabled the inverter ignores
those, so this bridge polls them and translates the mode into VPP remote power
commands (30407-30410). See EVCC_BATTERY_BRIDGE.md.

All register addresses are 0-based (pymodbus wire addresses), same as the
curtailment registers in io/growatt.py.
"""

from __future__ import annotations

import logging
import threading
import time
from enum import Enum
from typing import TYPE_CHECKING, Callable

if TYPE_CHECKING:
    from .config import EvccBridgeConfig
    from .io.growatt import GrowattClient

logger = logging.getLogger("curtailment.evcc_bridge")

# Legacy registers written by evcc
REG_BATTERY_FIRST = 1102
REG_AC_CHARGE = 1092

# VPP registers
REG_CONTROL_AUTHORITY = 30100
REG_REMOTE_ENABLE = 30407
REG_REMOTE_DURATION_MIN = 30408
REG_REMOTE_RATE_PCT = 30409
REG_AC_CHARGE_ENABLE = 30410

LIVENESS_CHECK_INTERVAL_S = 30
LIVENESS_TIMEOUT_S = 5


class BatteryMode(Enum):
    NORMAL = "normal"
    HOLD = "hold"
    CHARGE = "charge"


def mode_from_registers(battery_first: int, ac_charge: int) -> BatteryMode:
    """Map evcc's legacy register pair to a battery mode.

    1102=0/1092=1 is not produced by evcc and maps to normal.
    """
    if battery_first == 1 and ac_charge == 1:
        return BatteryMode.CHARGE
    if battery_first == 1:
        return BatteryMode.HOLD
    return BatteryMode.NORMAL


def http_alive_check(url: str) -> Callable[[], bool]:
    """Return a callable that GETs `url` and reports whether it answered 2xx."""
    import httpx

    def check() -> bool:
        try:
            resp = httpx.get(url, timeout=LIVENESS_TIMEOUT_S)
            return 200 <= resp.status_code < 300
        except Exception as e:
            logger.debug("evcc liveness check failed: %s", e)
            return False

    return check


class EvccBatteryBridge:
    """Polls evcc's legacy battery registers and drives VPP remote power control."""

    def __init__(
        self,
        cfg: EvccBridgeConfig,
        growatt: GrowattClient,
        alive_check: Callable[[], bool],
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.cfg = cfg
        self.growatt = growatt
        self._alive_check = alive_check
        self._clock = clock

        self.active: BatteryMode | None = None  # None -> first stable state always applied
        self._last_seen: BatteryMode | None = None
        self._last_refresh: float = 0.0
        self._last_alive_check: float | None = None
        self._last_alive_ok: float = clock()  # grace period from startup
        self.evcc_alive = True
        self.last_error: str | None = None

        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    # ── lifecycle ────────────────────────────────────────────────────────

    def start(self) -> None:
        self.check_control_authority()
        self._thread = threading.Thread(target=self._run, name="evcc-bridge", daemon=True)
        self._thread.start()
        logger.info("evcc battery bridge started (poll every %ss)", self.cfg.poll_interval_s)

    def stop(self) -> None:
        """Stop polling and release VPP remote control (best effort)."""
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=self.cfg.poll_interval_s + 15)
        try:
            self.growatt.write_holding(REG_REMOTE_ENABLE, 0)
            logger.info("evcc bridge: VPP remote power control disabled on shutdown")
        except Exception as e:
            logger.error("evcc bridge: failed to disable VPP remote control on shutdown: %s", e)
        self.active = None

    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                self.poll_once()
            except Exception:
                logger.exception("evcc bridge: unexpected error in poll")
            self._stop.wait(self.cfg.poll_interval_s)

    def check_control_authority(self) -> None:
        try:
            value = self.growatt.read_holding(REG_CONTROL_AUTHORITY)
        except Exception as e:
            logger.warning("evcc bridge: could not read control authority (%d): %s",
                           REG_CONTROL_AUTHORITY, e)
            return
        if value != 1:
            logger.error(
                "evcc bridge: VPP control authority register %d is %d, expected 1 "
                "— battery commands will be ignored by the inverter",
                REG_CONTROL_AUTHORITY, value,
            )

    # ── poll logic ───────────────────────────────────────────────────────

    def poll_once(self) -> None:
        now = self._clock()

        try:
            with self.growatt.lock:
                battery_first = self.growatt.read_holding(REG_BATTERY_FIRST)
                ac_charge = self.growatt.read_holding(REG_AC_CHARGE)
        except Exception as e:
            # Never treat a failed read as a state change; the VPP timer covers outages.
            logger.warning("evcc bridge: register read failed: %s", e)
            self.last_error = f"read failed: {e}"
            return

        seen = mode_from_registers(battery_first, ac_charge)
        if not self._is_evcc_alive(now):
            seen = BatteryMode.NORMAL

        # Debounce: act only on two consecutive identical readings. Filters the
        # intermediate state between evcc's separate 1102 and 1092 writes.
        stable = seen if seen == self._last_seen else None
        self._last_seen = seen

        if stable is not None and stable != self.active:
            try:
                self._apply(stable)
            except Exception as e:
                logger.warning("evcc bridge: applying %s failed: %s", stable.value, e)
                self.last_error = f"apply {stable.value} failed: {e}"
                return
            logger.info(
                "evcc bridge: battery mode %s → %s",
                self.active.value if self.active else "none", stable.value,
            )
            self.active = stable
            self._last_refresh = now
            self.last_error = None
            return

        if (
            self.active in (BatteryMode.HOLD, BatteryMode.CHARGE)
            and now - self._last_refresh >= self.cfg.refresh_interval_s
        ):
            try:
                # Re-arm the VPP fail-safe timer. If the inverter turns out not to
                # restart the countdown on a 30408-only write, re-send the full
                # hold/charge sequence here instead (without 30407=0 first).
                self.growatt.write_holding(REG_REMOTE_DURATION_MIN, self.cfg.vpp_timer_min)
                self._last_refresh = now
                self.last_error = None
            except Exception as e:
                logger.warning("evcc bridge: timer refresh failed: %s", e)
                self.last_error = f"refresh failed: {e}"

    def _apply(self, mode: BatteryMode) -> None:
        """Write the VPP sequence for `mode`.

        Hold/charge switches do not write 30407=0 first, to avoid a window where
        the battery is uncontrolled.
        """
        g = self.growatt
        t = self.cfg.vpp_timer_min
        with g.lock:
            if mode == BatteryMode.NORMAL:
                g.write_holding(REG_REMOTE_ENABLE, 0)
            elif mode == BatteryMode.HOLD:
                g.write_holding(REG_REMOTE_DURATION_MIN, t)
                g.write_holding(REG_REMOTE_RATE_PCT, self.cfg.hold_rate_pct)
                g.write_holding(REG_REMOTE_ENABLE, 1)
            elif mode == BatteryMode.CHARGE:
                # 30410 is EEPROM-backed: write only when needed, never reset it.
                if g.read_holding(REG_AC_CHARGE_ENABLE) != 1:
                    g.write_holding(REG_AC_CHARGE_ENABLE, 1)
                g.write_holding(REG_REMOTE_DURATION_MIN, t)
                g.write_holding(REG_REMOTE_RATE_PCT, self.cfg.charge_rate_pct)
                g.write_holding(REG_REMOTE_ENABLE, 1)

    def _is_evcc_alive(self, now: float) -> bool:
        if self._last_alive_check is None or now - self._last_alive_check >= LIVENESS_CHECK_INTERVAL_S:
            self._last_alive_check = now
            if self._alive_check():
                self._last_alive_ok = now
        alive = now - self._last_alive_ok < self.cfg.evcc_dead_after_s
        if alive != self.evcc_alive:
            if alive:
                logger.info("evcc bridge: evcc is reachable again")
            else:
                logger.warning(
                    "evcc bridge: evcc unreachable for %ss — forcing battery mode normal",
                    self.cfg.evcc_dead_after_s,
                )
        self.evcc_alive = alive
        return alive

    def status(self) -> dict:
        return {
            "active": self.active.value if self.active else "none",
            "seen": self._last_seen.value if self._last_seen else "none",
            "evcc_alive": self.evcc_alive,
            "last_error": self.last_error,
        }
