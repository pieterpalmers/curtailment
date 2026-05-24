"""State machine — pure functions, no IO."""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from enum import Enum
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .config import Config


class State(Enum):
    IDLE = "idle"
    ENTERING_CURTAILMENT = "entering"
    REGULATING = "regulating"
    EXITING_CURTAILMENT = "exiting"


@dataclass
class WorldState:
    """Snapshot read each cycle."""

    injection_tariff: float  # €/kWh, can be negative — drives v1 state transitions
    consumption_tariff: float  # €/kWh — logged, not branched on in v1
    grid_power: float  # smoothed, watts. positive=import, negative=export
    grid_power_raw: float  # last instantaneous sample, for logging
    growatt_pv_power: float  # smoothed, watts
    battery_power: float  # watts. positive=charging — logged, not used in v1
    growatt_export_limit_active: bool
    current_apsystems_pct: int  # 0-100
    current_ev_mode: str


@dataclass
class Action:
    """What the daemon should do this cycle."""

    set_growatt_export_limit_pct: float | None = None
    disable_growatt_export_limit: bool = False
    set_apsystems_pct: int | None = None
    enable_apsystems_power_limit: bool = False
    set_ev_mode: str | None = None
    next_state: State = State.IDLE
    wait_seconds: int = 30
    reason: str = ""


def reconcile_startup(world: WorldState, cfg: Config) -> State:
    """Determine state from external system state. No history needed."""
    tariff_cfg = cfg.tariff

    if world.injection_tariff < tariff_cfg.enter_threshold:
        if world.growatt_export_limit_active:
            return State.REGULATING  # resume regulation
        else:
            return State.ENTERING_CURTAILMENT  # need to enter
    else:
        if world.growatt_export_limit_active:
            return State.EXITING_CURTAILMENT  # cleanup needed
        else:
            return State.IDLE


def decide_action(
    state: State,
    world: WorldState,
    cfg: Config,
    time_in_state: float,
) -> Action:
    """Pure function: given current state + world snapshot, return the action."""
    tariff_cfg = cfg.tariff
    ctrl_cfg = cfg.control
    evcc_cfg = cfg.evcc

    if state == State.IDLE:
        return _idle(world, cfg, tariff_cfg)

    if state == State.ENTERING_CURTAILMENT:
        return _entering(world, cfg, ctrl_cfg, evcc_cfg, time_in_state)

    if state == State.REGULATING:
        return _regulating(world, cfg, ctrl_cfg, evcc_cfg, time_in_state)

    if state == State.EXITING_CURTAILMENT:
        return _exiting(world, cfg, ctrl_cfg, evcc_cfg)

    return Action(next_state=State.IDLE, reason="unknown state fallback")


def _idle(
    world: WorldState,
    cfg: Config,
    tariff_cfg: TariffConfig,
) -> Action:
    if world.injection_tariff < tariff_cfg.enter_threshold:
        return Action(
            next_state=State.ENTERING_CURTAILMENT,
            reason="injection tariff below enter threshold",
        )
    return Action(
        next_state=State.IDLE,
        wait_seconds=cfg.control.period_seconds,
        reason="injection tariff above enter threshold",
    )


def _entering(
    world: WorldState,
    cfg: Config,
    ctrl_cfg: ControlConfig,
    evcc_cfg: EvccConfig,
    time_in_state: float,
) -> Action:
    wait = ctrl_cfg.growatt_mode_change_wait_seconds
    if time_in_state < wait:
        # Still waiting for Growatt mode change to take effect
        return Action(
            set_growatt_export_limit_pct=ctrl_cfg.growatt_export_limit_pct,
            set_apsystems_pct=ctrl_cfg.start_apsystems_pct,
            enable_apsystems_power_limit=True,
            set_ev_mode="minpv",
            next_state=State.ENTERING_CURTAILMENT,
            wait_seconds=min(10, wait - int(time_in_state)),
            reason="waiting for Growatt mode change",
        )

    return Action(
        set_growatt_export_limit_pct=ctrl_cfg.growatt_export_limit_pct,
        set_apsystems_pct=ctrl_cfg.start_apsystems_pct,
        enable_apsystems_power_limit=True,
        set_ev_mode="minpv",
        next_state=State.REGULATING,
        wait_seconds=ctrl_cfg.period_seconds,
        reason="entered curtailment, transitioning to regulating",
    )


def _regulating(
    world: WorldState,
    cfg: Config,
    ctrl_cfg: ControlConfig,
    evcc_cfg: EvccConfig,
    time_in_state: float,
) -> Action:
    from .controller import ControlInput, compute_decision

    # Run the controller to get the APsystems decision
    ctrl_input = ControlInput(
        grid_power=world.grid_power,
        growatt_pv_power=world.growatt_pv_power,
        current_apsystems_pct=world.current_apsystems_pct,
    )
    decision = compute_decision(ctrl_input, ctrl_cfg)

    # Check tariff exit condition
    tariff_cfg = cfg.tariff
    next_state = State.REGULATING
    reason = decision.reason

    if world.injection_tariff >= tariff_cfg.exit_threshold:
        if time_in_state >= tariff_cfg.min_dwell_seconds:
            next_state = State.EXITING_CURTAILMENT
            reason = "injection tariff above exit threshold, dwell elapsed"
        else:
            reason = "injection tariff above exit threshold, dwell deferred"

    # Only rewrite Growatt export limit if not currently active
    needs_limit_write = not world.growatt_export_limit_active

    return Action(
        set_apsystems_pct=decision.new_apsystems_pct,
        set_growatt_export_limit_pct=(
            ctrl_cfg.growatt_export_limit_pct if needs_limit_write else None
        ),
        set_ev_mode=None,  # no EV mode change during regulation for v1
        next_state=next_state,
        wait_seconds=ctrl_cfg.period_seconds,
        reason=reason,
    )


def _exiting(
    world: WorldState,
    cfg: Config,
    ctrl_cfg: ControlConfig,
    evcc_cfg: EvccConfig,
) -> Action:
    restore_ev = evcc_cfg.default_ev_mode if world.current_ev_mode == "minpv" else None
    return Action(
        disable_growatt_export_limit=True,
        set_apsystems_pct=100,
        set_ev_mode=restore_ev,
        next_state=State.IDLE,
        wait_seconds=ctrl_cfg.period_seconds,
        reason="exited curtailment, restoring defaults",
    )


# Local imports for type checking
from .config import ControlConfig, EvccConfig, TariffConfig  # noqa: E402
