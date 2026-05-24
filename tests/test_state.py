"""Tests for state machine module."""

from curtailment.state import (
    State, WorldState, Action,
    decide_action, reconcile_startup,
)
from curtailment.config import (
    Config, ControlConfig, TariffConfig, LoggingConfig,
    GrowattConfig, APsystemsConfig, EvccConfig,
)


def _make_cfg(**overrides):
    kwargs = dict(
        growatt=GrowattConfig(host="x"),
        apsystems=APSystemsConfig(host="y"),
        evcc=EvccConfig(base_url="http://z"),
        control=ControlConfig(),
        tariff=TariffConfig(),
        logging=LoggingConfig(),
    )
    if "tariff" in overrides:
        kwargs["tariff"] = overrides["tariff"]
    return Config(**kwargs)


def _make_world(**overrides):
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


class TestReconcileStartup:
    def test_tariff_low_limit_inactive_entering(self):
        cfg = _make_cfg(tariff=TariffConfig(enter_threshold=-0.005))
        world = _make_world(tariff=-0.01, growatt_export_limit_active=False)
        state = reconcile_startup(world, cfg)
        assert state == State.ENTERING_CURTAILMENT

    def test_tariff_low_limit_active_regulating(self):
        cfg = _make_cfg(tariff=TariffConfig(enter_threshold=-0.005))
        world = _make_world(tariff=-0.01, growatt_export_limit_active=True)
        state = reconcile_startup(world, cfg)
        assert state == State.REGULATING

    def test_tariff_high_limit_active_exiting(self):
        cfg = _make_cfg(tariff=TariffConfig(enter_threshold=-0.005))
        world = _make_world(tariff=0.05, growatt_export_limit_active=True)
        state = reconcile_startup(world, cfg)
        assert state == State.EXITING_CURTAILMENT

    def test_tariff_high_limit_inactive_idle(self):
        cfg = _make_cfg(tariff=TariffConfig(enter_threshold=-0.005))
        world = _make_world(tariff=0.05, growatt_export_limit_active=False)
        state = reconcile_startup(world, cfg)
        assert state == State.IDLE


class TestDecideAction:
    def test_idle_stays_idle_when_tariff_high(self):
        cfg = _make_cfg(tariff=TariffConfig(enter_threshold=-0.005))
        world = _make_world(tariff=0.0, growatt_export_limit_active=False)
        action = decide_action(State.IDLE, world, cfg, 0)
        assert action.next_state == State.IDLE

    def test_idle_transitions_to_entering_when_tariff_low(self):
        cfg = _make_cfg(tariff=TariffConfig(enter_threshold=-0.005))
        world = _make_world(tariff=-0.01, growatt_export_limit_active=False)
        action = decide_action(State.IDLE, world, cfg, 0)
        assert action.next_state == State.ENTERING_CURTAILMENT

    def test_entering_grows_into_regulating(self):
        cfg = _make_cfg(
            control=ControlConfig(
                growatt_mode_change_wait_seconds=0,  # instant for test
                growatt_export_limit_pct=5,
                start_apsystems_pct=50,
            ),
            tariff=TariffConfig(enter_threshold=-0.005),
            evcc=EvccConfig(base_url="http://z", default_ev_mode="pv"),
        )
        world = _make_world(
            tariff=-0.01, growatt_export_limit_active=False,
            current_apsystems_pct=100,
        )
        action = decide_action(State.ENTERING_CURTAILMENT, world, cfg, 10)
        assert action.next_state == State.REGULATING
        assert action.set_growatt_export_limit_pct == 5
        assert action.set_apsystems_pct == 50

    def test_regulating_sets_ev_mode_to_minpv(self):
        cfg = _make_cfg(
            control=ControlConfig(
                export_setpoint_watts=-200, deadband_watts=50,
                growatt_export_limit_pct=5,
            ),
            tariff=TariffConfig(enter_threshold=-0.005, exit_threshold=0.0),
            evcc=EvccConfig(base_url="http://z", default_ev_mode="pv"),
        )
        world = _make_world(
            tariff=-0.01,
            grid_power=-220,  # on target
            growatt_pv_power=5000,
            current_apsystems_pct=50,
        )
        action = decide_action(State.REGULATING, world, cfg, 10)
        # Should stay in regulating, run controller
        assert action.next_state == State.REGULATING

    def test_regulating_exits_when_tariff_high(self):
        cfg = _make_cfg(
            control=ControlConfig(export_setpoint_watts=-200),
            tariff=TariffConfig(enter_threshold=-0.005, exit_threshold=0.0),
        )
        world = _make_world(
            tariff=0.05,  # above exit threshold
            grid_power=-220,
            growatt_pv_power=5000,
            current_apsystems_pct=50,
        )
        action = decide_action(State.REGULATING, world, cfg, 10)
        assert action.next_state == State.EXITING_CURTAILMENT

    def test_exiting_restores_defaults(self):
        cfg = _make_cfg(
            control=ControlConfig(growatt_export_limit_pct=5),
            evcc=EvccConfig(base_url="http://z", default_ev_mode="pv"),
        )
        world = _make_world(
            tariff=0.05,
            growatt_export_limit_active=True,
            current_apsystems_pct=30,
        )
        action = decide_action(State.EXITING_CURTAILMENT, world, cfg, 0)
        assert action.next_state == State.IDLE
        assert action.disable_growatt_export_limit is True
        assert action.set_apsystems_pct == 100
        assert action.set_ev_mode == "pv"
