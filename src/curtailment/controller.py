"""Pure control logic — no IO, fully unit-testable."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass
class ControlInput:
    grid_power: float  # smoothed, watts. positive=import, negative=export
    growatt_pv_power: float  # smoothed, watts
    current_apsystems_pct: int  # 0-100
    growatt_export_limit_watts: float  # the effective watt limit Growatt is enforcing


@dataclass
class ControlDecision:
    new_apsystems_pct: int  # 0-100
    reason: str  # for logging


def compute_decision(inp: ControlInput, cfg: ControlConfig) -> ControlDecision:
    """Pure function implementing the regulation logic.

    Cases:
    - Over-exporting (grid more negative than setpoint - deadband) → lower APsystems
    - Under-exporting (grid less negative than setpoint + deadband) → raise APsystems
    - On target, Growatt not pinned → hold
    - On target, Growatt pinned at limit → lower APsystems
    """
    setpoint = cfg.export_setpoint_watts
    deadband = cfg.deadband_watts

    # Determine which side of the setpoint we're on
    over_exporting = inp.grid_power < setpoint - deadband
    under_exporting = inp.grid_power > setpoint + deadband
    on_target = not over_exporting and not under_exporting

    # Check if Growatt is pinned: PV power is near the export limit,
    # meaning the Growatt inverter is maxed out at its export limit
    # and can't export more even if APsystems allows it
    growatt_pinned = (
        inp.growatt_pv_power < cfg.growatt_pinned_threshold_watts
    )

    if over_exporting:
        # Exporting too much (grid power too negative) → reduce APsystems output
        new_pct = max(0, inp.current_apsystems_pct - int(cfg.step_down_pct))
        return ControlDecision(
            new_apsystems_pct=new_pct,
            reason="over_export",
        )

    if under_exporting:
        # Not exporting enough → increase APsystems output
        new_pct = min(100, inp.current_apsystems_pct + int(cfg.step_up_pct))
        return ControlDecision(
            new_apsystems_pct=new_pct,
            reason="under_export",
        )

    if on_target and growatt_pinned:
        # On target but Growatt is pinned at its limit → lower to unpin
        new_pct = max(0, inp.current_apsystems_pct - int(cfg.step_down_pct))
        return ControlDecision(
            new_apsystems_pct=new_pct,
            reason="pinned_low",
        )

    # On target, Growatt healthy → hold
    return ControlDecision(
        new_apsystems_pct=inp.current_apsystems_pct,
        reason="hold",
    )


# Import locally to avoid circular; use string annotation above
from .config import ControlConfig  # noqa: E402
