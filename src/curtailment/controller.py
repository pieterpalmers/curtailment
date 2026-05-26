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
    - On target, Growatt high PV (at export cap) → raise APsystems
    - On target, Growatt low PV (barely contributing) → lower APsystems
    - On target, Growatt mid-range → hold
    """
    setpoint = cfg.export_setpoint_watts
    deadband = cfg.deadband_watts

    # Determine which side of the setpoint we're on
    over_exporting = inp.grid_power < setpoint - deadband
    under_exporting = inp.grid_power > setpoint + deadband
    on_target = not over_exporting and not under_exporting

    # Classify effective Growatt PV contribution (battery correction applied upstream)
    growatt_high_pv = inp.growatt_pv_power >= cfg.growatt_pinned_threshold_watts_high
    growatt_low_pv = inp.growatt_pv_power < cfg.growatt_pinned_threshold_watts_low

    logger.info(
        "controller: grid=%.0fW setpoint=%.0fW±%.0fW apsystems=%d%% "
        "growatt_pv=%.0fW (low_thresh=%.0fW high_thresh=%.0fW) | "
        "over=%s under=%s on_target=%s high_pv=%s low_pv=%s",
        inp.grid_power, setpoint, deadband, inp.current_apsystems_pct,
        inp.growatt_pv_power,
        cfg.growatt_pinned_threshold_watts_low, cfg.growatt_pinned_threshold_watts_high,
        over_exporting, under_exporting, on_target, growatt_high_pv, growatt_low_pv,
    )

    if over_exporting and growatt_low_pv:
        # Exporting too much (grid power too negative) and Growatt can't regulate anymore
        # → reduce APsystems output

        new_pct = max(0, inp.current_apsystems_pct - int(cfg.step_down_pct))
        logger.info(
            "controller decision: over-export → lower apsystems %d%% → %d%% (step_down=%.0f%%)",
            inp.current_apsystems_pct, new_pct, cfg.step_down_pct,
        )
        return ControlDecision(
            new_apsystems_pct=new_pct,
            reason="over_export",
        )

    if under_exporting and growatt_high_pv:
        # Not exporting enough and Growatt at max → increase APsystems output
        new_pct = min(100, inp.current_apsystems_pct + int(cfg.step_up_pct))
        logger.info(
            "controller decision: under-export → raise apsystems %d%% → %d%% (step_up=%.0f%%)",
            inp.current_apsystems_pct, new_pct, cfg.step_up_pct,
        )
        return ControlDecision(
            new_apsystems_pct=new_pct,
            reason="under_export",
        )

    if on_target and growatt_high_pv:
        # Growatt is well above its export cap — reliable fixed contributor.
        # Keep pushing APsystems up; over-export protection will catch any overshoot.
        new_pct = min(100, inp.current_apsystems_pct + int(cfg.step_up_pct))
        logger.info(
            "controller decision: on-target, growatt high pv (%.0fW >= %.0fW) → raise apsystems %d%% → %d%%",
            inp.growatt_pv_power, cfg.growatt_pinned_threshold_watts_high,
            inp.current_apsystems_pct, new_pct,
        )
        return ControlDecision(
            new_apsystems_pct=new_pct,
            reason="growatt_high_pv",
        )

    if on_target and growatt_low_pv:
        # Growatt effective PV near zero — lower APsystems to avoid over-export if PV recovers
        new_pct = max(0, inp.current_apsystems_pct - int(cfg.step_down_pct))
        logger.info(
            "controller decision: on-target, growatt low pv (%.0fW < %.0fW) → lower apsystems %d%% → %d%%",
            inp.growatt_pv_power, cfg.growatt_pinned_threshold_watts_low,
            inp.current_apsystems_pct, new_pct,
        )
        return ControlDecision(
            new_apsystems_pct=new_pct,
            reason="pinned_low",
        )

    if not on_target:
        # On target, Growatt mid-range → hold
        logger.warning(
            "controller decision: fallthrough but not on_target? → hold apsystems at %d%%",
            inp.current_apsystems_pct,
        )
    else:
        # On target, Growatt mid-range → hold
        logger.info(
            "controller decision: on-target and growatt mid-range → hold apsystems at %d%%",
            inp.current_apsystems_pct,
        )
    return ControlDecision(
        new_apsystems_pct=inp.current_apsystems_pct,
        reason="hold",
    )
