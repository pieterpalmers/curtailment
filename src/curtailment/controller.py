"""Pure control logic — no IO, fully unit-testable."""

from __future__ import annotations

import logging
from dataclasses import dataclass

from .config import ControlConfig

logger = logging.getLogger("curtailment")


@dataclass
class ControlInput:
    grid_power: float  # smoothed, watts. positive=import, negative=export
    growatt_pv_power: float  # smoothed, watts
    current_apsystems_pct: int  # 0-100


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

    logger.info(
        "controller: grid=%.0fW setpoint=%.0fW±%.0fW apsystems=%d%% "
        "growatt_pv=%.0fW (pinned_threshold=%.0fW) | "
        "over=%s under=%s on_target=%s pinned=%s",
        inp.grid_power, setpoint, deadband, inp.current_apsystems_pct,
        inp.growatt_pv_power, cfg.growatt_pinned_threshold_watts,
        over_exporting, under_exporting, on_target, growatt_pinned,
    )

    if over_exporting:
        # Exporting too much (grid power too negative) → reduce APsystems output
        new_pct = max(0, inp.current_apsystems_pct - int(cfg.step_down_pct))
        logger.info(
            "controller decision: over-export → lower apsystems %d%% → %d%% (step_down=%.0f%%)",
            inp.current_apsystems_pct, new_pct, cfg.step_down_pct,
        )
        return ControlDecision(
            new_apsystems_pct=new_pct,
            reason="over_export",
        )

    if under_exporting:
        # Not exporting enough → increase APsystems output
        new_pct = min(100, inp.current_apsystems_pct + int(cfg.step_up_pct))
        logger.info(
            "controller decision: under-export → raise apsystems %d%% → %d%% (step_up=%.0f%%)",
            inp.current_apsystems_pct, new_pct, cfg.step_up_pct,
        )
        return ControlDecision(
            new_apsystems_pct=new_pct,
            reason="under_export",
        )

    if on_target and growatt_pinned:
        # On target but Growatt is pinned at its limit → lower to unpin
        new_pct = max(0, inp.current_apsystems_pct - int(cfg.step_down_pct))
        logger.info(
            "controller decision: on-target but growatt pinned (pv=%.0fW < %.0fW) → lower apsystems %d%% → %d%%",
            inp.growatt_pv_power, cfg.growatt_pinned_threshold_watts,
            inp.current_apsystems_pct, new_pct,
        )
        return ControlDecision(
            new_apsystems_pct=new_pct,
            reason="pinned_low",
        )

    # On target, Growatt healthy → hold
    logger.info(
        "controller decision: on-target and growatt healthy → hold apsystems at %d%%",
        inp.current_apsystems_pct,
    )
    return ControlDecision(
        new_apsystems_pct=inp.current_apsystems_pct,
        reason="hold",
    )

