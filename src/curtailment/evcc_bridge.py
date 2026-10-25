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
from collections import deque
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
HISTORY_LEN = 50

# Read back after each apply/refresh so the status page shows what the
# inverter actually holds, not just what we wrote.
READBACK_REGS = (REG_REMOTE_ENABLE, REG_REMOTE_DURATION_MIN, REG_REMOTE_RATE_PCT, REG_AC_CHARGE_ENABLE)


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

        # Introspection for the status page
        self.control_authority: int | None = None
        self.decision = "not polled yet"
        self._raw: tuple[int, int] | None = None
        self._last_poll: float | None = None
        self._readback: dict[int, int] = {}
        self._readback_at: float | None = None
        self._readback_error: str | None = None
        self._history: deque[dict] = deque(maxlen=HISTORY_LEN)
        self._history_lock = threading.Lock()

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
        self.control_authority = value
        if value != 1:
            self._event("control authority", f"{REG_CONTROL_AUTHORITY}={value}, expected 1")
            logger.error(
                "evcc bridge: VPP control authority register %d is %d, expected 1 "
                "— battery commands will be ignored by the inverter",
                REG_CONTROL_AUTHORITY, value,
            )

    # ── poll logic ───────────────────────────────────────────────────────

    def poll_once(self) -> None:
        now = self._clock()
        self._last_poll = now
        kept = self.active.value if self.active else "none"

        try:
            with self.growatt.lock:
                battery_first = self.growatt.read_holding(REG_BATTERY_FIRST)
                ac_charge = self.growatt.read_holding(REG_AC_CHARGE)
        except Exception as e:
            # Never treat a failed read as a state change; the VPP timer covers outages.
            logger.warning("evcc bridge: register read failed: %s", e)
            self.last_error = f"read failed: {e}"
            self.decision = f"register read failed, keeping {kept}"
            self._event("read failed", str(e))
            return

        self._raw = (battery_first, ac_charge)
        requested = mode_from_registers(battery_first, ac_charge)
        regs = f"evcc requests {requested.value} ({REG_BATTERY_FIRST}={battery_first}, {REG_AC_CHARGE}={ac_charge})"
        seen = requested
        if not self._is_evcc_alive(now):
            seen = BatteryMode.NORMAL
            regs += f"; evcc unreachable for {now - self._last_alive_ok:.0f}s, forcing normal"

        # Debounce: act only on two consecutive identical readings. Filters the
        # intermediate state between evcc's separate 1102 and 1092 writes.
        stable = seen if seen == self._last_seen else None
        self._last_seen = seen

        if stable is None:
            self.decision = f"{regs}; first reading of {seen.value}, waiting for a second one (debounce)"
            return

        if stable != self.active:
            try:
                writes = self._apply(stable)
            except Exception as e:
                logger.warning("evcc bridge: applying %s failed: %s", stable.value, e)
                self.last_error = f"apply {stable.value} failed: {e}"
                self.decision = f"{regs}; applying {stable.value} failed, keeping {kept}"
                self._event("apply failed", f"{stable.value}: {e}")
                return
            logger.info(
                "evcc bridge: battery mode %s → %s",
                self.active.value if self.active else "none", stable.value,
            )
            self._event(f"{kept} → {stable.value}", f"{regs}; wrote {_fmt_writes(writes)}")
            self.active = stable
            self._last_refresh = now
            self.last_error = None
            self.decision = f"{regs}; applied {stable.value}: {_fmt_writes(writes)}"
            self._read_back(now)
            return

        if self.active == BatteryMode.NORMAL:
            self.decision = f"{regs}; normal active, VPP remote control off, inverter decides"
            if self._readback_at is None or now - self._readback_at >= self.cfg.refresh_interval_s:
                self._read_back(now)
            return

        since = now - self._last_refresh
        if since >= self.cfg.refresh_interval_s:
            try:
                # Re-arm the VPP fail-safe timer. If the inverter turns out not to
                # restart the countdown on a 30408-only write, re-send the full
                # hold/charge sequence here instead (without 30407=0 first).
                self.growatt.write_holding(REG_REMOTE_DURATION_MIN, self.cfg.vpp_timer_min)
                self._last_refresh = now
                self.last_error = None
                self.decision = f"{regs}; {self.active.value} active, re-armed VPP timer ({REG_REMOTE_DURATION_MIN}={self.cfg.vpp_timer_min})"
            except Exception as e:
                logger.warning("evcc bridge: timer refresh failed: %s", e)
                self.last_error = f"refresh failed: {e}"
                self.decision = f"{regs}; {self.active.value} active, timer refresh failed"
                self._event("refresh failed", str(e))
            self._read_back(now)
            return

        self.decision = (
            f"{regs}; {self.active.value} active, next timer re-arm in "
            f"{self.cfg.refresh_interval_s - since:.0f}s"
        )

    def _apply(self, mode: BatteryMode) -> list[tuple[int, int]]:
        """Write the VPP sequence for `mode` and return the writes made.

        Hold/charge switches do not write 30407=0 first, to avoid a window where
        the battery is uncontrolled.
        """
        g = _RecordingWriter(self.growatt)
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
        return g.writes

    def _read_back(self, now: float) -> None:
        try:
            with self.growatt.lock:
                values = {reg: self.growatt.read_holding(reg) for reg in READBACK_REGS}
        except Exception as e:
            self._readback_error = str(e)
            return
        self._readback = values
        self._readback_at = now
        self._readback_error = None

    def _expected_vpp(self) -> dict[int, int]:
        if self.active == BatteryMode.NORMAL:
            return {REG_REMOTE_ENABLE: 0}
        if self.active == BatteryMode.HOLD:
            return {REG_REMOTE_ENABLE: 1, REG_REMOTE_RATE_PCT: self.cfg.hold_rate_pct}
        if self.active == BatteryMode.CHARGE:
            return {REG_REMOTE_ENABLE: 1, REG_REMOTE_RATE_PCT: self.cfg.charge_rate_pct,
                    REG_AC_CHARGE_ENABLE: 1}
        return {}

    def _event(self, event: str, detail: str) -> None:
        entry = {"time": time.strftime("%Y-%m-%d %H:%M:%S"), "event": event, "detail": detail}
        with self._history_lock:
            self._history.appendleft(entry)

    def _is_evcc_alive(self, now: float) -> bool:
        if self._last_alive_check is None or now - self._last_alive_check >= LIVENESS_CHECK_INTERVAL_S:
            self._last_alive_check = now
            if self._alive_check():
                self._last_alive_ok = now
        alive = now - self._last_alive_ok < self.cfg.evcc_dead_after_s
        if alive != self.evcc_alive:
            if alive:
                logger.info("evcc bridge: evcc is reachable again")
                self._event("evcc alive", "evcc is reachable again")
            else:
                self._event("evcc dead", f"no response for {self.cfg.evcc_dead_after_s}s, forcing normal")
                logger.warning(
                    "evcc bridge: evcc unreachable for %ss — forcing battery mode normal",
                    self.cfg.evcc_dead_after_s,
                )
        self.evcc_alive = alive
        return alive

    def status(self) -> dict:
        now = self._clock()

        def age(t: float | None) -> int | None:
            return None if t is None else round(now - t)

        readback = {f"{reg} {_REG_NAMES[reg]}": _signed16(v) if reg == REG_REMOTE_RATE_PCT else v
                    for reg, v in self._readback.items()}
        mismatch = [
            f"{reg} is {_signed16(self._readback[reg])}, expected {want}"
            for reg, want in self._expected_vpp().items()
            if reg in self._readback and _signed16(self._readback[reg]) != want
        ]
        in_vpp = self.active in (BatteryMode.HOLD, BatteryMode.CHARGE)
        with self._history_lock:
            history = list(self._history)
        return {
            "active": self.active.value if self.active else "none",
            "decision": self.decision,
            "evcc_requests": mode_from_registers(*self._raw).value if self._raw else "unknown",
            "legacy_registers": (
                f"{REG_BATTERY_FIRST}={self._raw[0]}, {REG_AC_CHARGE}={self._raw[1]}" if self._raw else "unknown"
            ),
            "seen": self._last_seen.value if self._last_seen else "none",
            "last_poll_age_s": age(self._last_poll),
            "evcc_alive": self.evcc_alive,
            "evcc_last_ok_age_s": age(self._last_alive_ok),
            "timer_rearm_age_s": age(self._last_refresh) if in_vpp else None,
            "vpp_readback": readback or "not read yet",
            "vpp_readback_age_s": age(self._readback_at),
            "vpp_readback_error": self._readback_error,
            "vpp_mismatch": "; ".join(mismatch) or None,
            "control_authority_30100": self.control_authority,
            "config": (
                f"poll {self.cfg.poll_interval_s}s, re-arm {self.cfg.refresh_interval_s}s, "
                f"timer {self.cfg.vpp_timer_min}min, hold +{self.cfg.hold_rate_pct}%, "
                f"charge +{self.cfg.charge_rate_pct}%, evcc dead after {self.cfg.evcc_dead_after_s}s"
            ),
            "last_error": self.last_error,
            "history": history,
        }


_REG_NAMES = {
    REG_REMOTE_ENABLE: "remote enable",
    REG_REMOTE_DURATION_MIN: "duration min",
    REG_REMOTE_RATE_PCT: "rate %",
    REG_AC_CHARGE_ENABLE: "AC charge",
}


def _signed16(v: int) -> int:
    return v - 0x10000 if v >= 0x8000 else v


def _fmt_writes(writes: list[tuple[int, int]]) -> str:
    return ", ".join(f"{reg}={val}" for reg, val in writes) or "nothing"


class _RecordingWriter:
    """Wraps a GrowattClient and records holding-register writes."""

    def __init__(self, growatt: GrowattClient) -> None:
        self._g = growatt
        self.lock = growatt.lock
        self.writes: list[tuple[int, int]] = []

    def read_holding(self, address: int) -> int:
        return self._g.read_holding(address)

    def write_holding(self, address: int, value: int) -> None:
        self._g.write_holding(address, value)
        self.writes.append((address, value))
