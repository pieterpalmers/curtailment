"""Tests for state machine module."""

import pytest

from curtailment.state import State, WorldState, decide_action, reconcile_startup
from curtailment.config import (
    Config, ControlConfig, TariffConfig, LoggingConfig,
    GrowattConfig, APSystemsConfig, EvccConfig,
)


def _cfg(**overrides):
    """Create a Config with defaults."""
    return Config(
        growatt=GrowattConfig(host="x"),
        apsystems=APSystemsConfig(host="y"),
        evcc=EvccConfig(base_url="http://z"),
        control=ControlConfig(**overrides.get("control", {})),
        tariff=TariffConfig(**overrides.get("tariff", {})),
        logging=LoggingConfig(),
    )


def _world(**overrides):
    """Create a WorldState with defaults."""
    kwargs = dict(
        injection_tariff=-0.01,
        consumption_tariff=0.05,
        grid_power=-200.0,
        grid_power_raw=-200.0,
        growatt_pv_power=5000.0,
        battery_power=0.0,
        growatt_export_limit_active=False,
        growatt_export_limit_pct=5,
        current_apsystems_pct=50,
        current_ev_mode="pv",
    )
    kwargs.update(overrides)
    return WorldState(**kwargs)


def test_reconcile_entering():
    cfg = _cfg(tariff={"enter_threshold": -0.005})
    world = _world(injection_tariff=-0.01, growatt_export_limit_active=False)
    assert reconcile_startup(world, cfg) == State.ENTERING_CURTAILMENT


def test_reconcile_regulating():
    cfg = _cfg(tariff={"enter_threshold": -0.005})
    world = _world(injection_tariff=-0.01, growatt_export_limit_active=True)
    assert reconcile_startup(world, cfg) == State.REGULATING


def test_reconcile_exiting():
    cfg = _cfg(tariff={"enter_threshold": -0.005})
    world = _world(injection_tariff=0.05, growatt_export_limit_active=True)
    assert reconcile_startup(world, cfg) == State.EXITING_CURTAILMENT


def test_reconcile_idle():
    cfg = _cfg(tariff={"enter_threshold": -0.005})
    world = _world(injection_tariff=0.05, growatt_export_limit_active=False)
    assert reconcile_startup(world, cfg) == State.IDLE


def test_idle_to_entering():
    cfg = _cfg(tariff={"enter_threshold": -0.005})
    world = _world(injection_tariff=-0.01, growatt_export_limit_active=False)
    action = decide_action(State.IDLE, world, cfg, 0)
    assert action.next_state == State.ENTERING_CURTAILMENT


def test_entering_to_regulating():
    cfg = _cfg(
        control={"growatt_mode_change_wait_seconds": 0},
        tariff={"enter_threshold": -0.005},
    )
    world = _world(injection_tariff=-0.01, growatt_export_limit_active=False)
    action = decide_action(State.ENTERING_CURTAILMENT, world, cfg, 10)
    assert action.next_state == State.REGULATING
    assert action.enable_apsystems_power_limit is True
    assert action.set_ev_mode == "minpv"


def test_entering_always_sets_minpv():
    """Entry should always set minpv regardless of default_ev_mode."""
    cfg = _cfg(
        control={"growatt_mode_change_wait_seconds": 0},
        evcc={"default_ev_mode": "minpv"},
    )
    world = _world(injection_tariff=-0.01, growatt_export_limit_active=False)
    action = decide_action(State.ENTERING_CURTAILMENT, world, cfg, 10)
    assert action.set_ev_mode == "minpv"


def test_regulating_to_exiting():
    cfg = _cfg(
        tariff={"enter_threshold": -0.005, "exit_threshold": 0.0, "min_dwell_seconds": 0},
    )
    world = _world(injection_tariff=0.05, grid_power=-220)
    action = decide_action(State.REGULATING, world, cfg, 10)
    assert action.next_state == State.EXITING_CURTAILMENT


def test_regulating_holds_until_dwell():
    """Injection tariff above exit threshold but dwell not elapsed → stay in REGULATING."""
    cfg = _cfg(
        tariff={"enter_threshold": -0.005, "exit_threshold": 0.0, "min_dwell_seconds": 600},
    )
    world = _world(injection_tariff=0.05, grid_power=-220)
    action = decide_action(State.REGULATING, world, cfg, 100)  # 100s < 600s
    assert action.next_state == State.REGULATING
    assert "dwell deferred" in action.reason


def test_regulating_exits_after_dwell():
    """Injection tariff above exit threshold and dwell elapsed → exit."""
    cfg = _cfg(
        tariff={"enter_threshold": -0.005, "exit_threshold": 0.0, "min_dwell_seconds": 600},
    )
    world = _world(injection_tariff=0.05, grid_power=-220)
    action = decide_action(State.REGULATING, world, cfg, 700)  # 700s >= 600s
    assert action.next_state == State.EXITING_CURTAILMENT
    assert "dwell elapsed" in action.reason


def test_exiting_to_idle():
    cfg = _cfg()
    world = _world(injection_tariff=0.05, growatt_export_limit_active=True, current_ev_mode="minpv")
    action = decide_action(State.EXITING_CURTAILMENT, world, cfg, 0)
    assert action.next_state == State.IDLE
    assert action.set_apsystems_pct == 100
    assert action.disable_growatt_export_limit is True
    assert action.set_ev_mode == "pv"  # default_ev_mode is "pv"


def test_exiting_preserves_user_mode():
    """If user set 'now' mid-curtailment, don't stomp it."""
    cfg = _cfg()
    world = _world(injection_tariff=0.05, growatt_export_limit_active=True, current_ev_mode="now")
    action = decide_action(State.EXITING_CURTAILMENT, world, cfg, 0)
    assert action.set_ev_mode is None  # don't change EV mode


def test_regulating_does_not_rewrite_growatt_when_active():
    """When export limit is already active, don't rewrite it."""
    cfg = _cfg()
    world = _world(injection_tariff=-0.01, growatt_export_limit_active=True)
    action = decide_action(State.REGULATING, world, cfg, 100)
    assert action.set_growatt_export_limit_pct is None
