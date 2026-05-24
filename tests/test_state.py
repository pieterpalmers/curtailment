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
        tariff=-0.01,
        grid_power=-200.0,
        growatt_pv_power=5000.0,
        growatt_export_limit_active=False,
        growatt_export_limit_watts=0.0,
        current_apsystems_pct=50,
        current_ev_mode="pv",
    )
    kwargs.update(overrides)
    return WorldState(**kwargs)


def test_reconcile_entering():
    cfg = _cfg(tariff={"enter_threshold": -0.005})
    world = _world(tariff=-0.01, growatt_export_limit_active=False)
    assert reconcile_startup(world, cfg) == State.ENTERING_CURTAILMENT


def test_reconcile_regulating():
    cfg = _cfg(tariff={"enter_threshold": -0.005})
    world = _world(tariff=-0.01, growatt_export_limit_active=True)
    assert reconcile_startup(world, cfg) == State.REGULATING


def test_reconcile_exiting():
    cfg = _cfg(tariff={"enter_threshold": -0.005})
    world = _world(tariff=0.05, growatt_export_limit_active=True)
    assert reconcile_startup(world, cfg) == State.EXITING_CURTAILMENT


def test_reconcile_idle():
    cfg = _cfg(tariff={"enter_threshold": -0.005})
    world = _world(tariff=0.05, growatt_export_limit_active=False)
    assert reconcile_startup(world, cfg) == State.IDLE


def test_idle_to_entering():
    cfg = _cfg(tariff={"enter_threshold": -0.005})
    world = _world(tariff=-0.01, growatt_export_limit_active=False)
    action = decide_action(State.IDLE, world, cfg, 0)
    assert action.next_state == State.ENTERING_CURTAILMENT


def test_entering_to_regulating():
    cfg = _cfg(
        control={"growatt_mode_change_wait_seconds": 0},
        tariff={"enter_threshold": -0.005},
    )
    world = _world(tariff=-0.01, growatt_export_limit_active=False)
    action = decide_action(State.ENTERING_CURTAILMENT, world, cfg, 10)
    assert action.next_state == State.REGULATING


def test_regulating_to_exiting():
    cfg = _cfg(
        tariff={"enter_threshold": -0.005, "exit_threshold": 0.0},
    )
    world = _world(tariff=0.05, grid_power=-220)
    action = decide_action(State.REGULATING, world, cfg, 0)
    assert action.next_state == State.EXITING_CURTAILMENT


def test_exiting_to_idle():
    cfg = _cfg()
    world = _world(tariff=0.05, growatt_export_limit_active=True)
    action = decide_action(State.EXITING_CURTAILMENT, world, cfg, 0)
    assert action.next_state == State.IDLE
    assert action.set_apsystems_pct == 100
    assert action.disable_growatt_export_limit is True
