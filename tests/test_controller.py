"""Tests for controller module."""

from curtailment.controller import ControlInput, compute_decision
from curtailment.config import ControlConfig


def test_under_exporting():
    cfg = ControlConfig(export_setpoint_watts=-200, deadband_watts=50, step_up_pct=5)
    inp = ControlInput(
        grid_power=-100, growatt_pv_power=5000,
        current_apsystems_pct=50,
    )
    d = compute_decision(inp, cfg)
    assert d.reason == "under_export"


def test_over_exporting_growatt_low_pv():
    cfg = ControlConfig(
        export_setpoint_watts=-200, deadband_watts=50, step_down_pct=10,
        growatt_pinned_threshold_watts_low=100,
    )
    inp = ControlInput(
        grid_power=-300, growatt_pv_power=50,
        current_apsystems_pct=50,
    )
    d = compute_decision(inp, cfg)
    assert d.reason == "over_export"
    assert d.new_apsystems_pct == 40


def test_over_exporting_growatt_can_still_regulate_holds():
    cfg = ControlConfig(
        export_setpoint_watts=-200, deadband_watts=50,
        growatt_pinned_threshold_watts_low=100,
    )
    inp = ControlInput(
        grid_power=-300, growatt_pv_power=5000,
        current_apsystems_pct=50,
    )
    d = compute_decision(inp, cfg)
    assert d.reason == "hold"
    assert d.new_apsystems_pct == 50


def test_on_target_growatt_low_pv_holds():
    cfg = ControlConfig(
        export_setpoint_watts=-200, deadband_watts=50,
        growatt_pinned_threshold_watts_low=100,
    )
    inp = ControlInput(
        grid_power=-220, growatt_pv_power=50,
        current_apsystems_pct=50,
    )
    d = compute_decision(inp, cfg)
    assert d.reason == "hold"
    assert d.new_apsystems_pct == 50


def test_on_target_growatt_high_pv_raises():
    cfg = ControlConfig(
        export_setpoint_watts=-200, deadband_watts=50,
        growatt_pinned_threshold_watts_low=100, step_up_pct=5,
    )
    inp = ControlInput(
        grid_power=-220, growatt_pv_power=5000,
        current_apsystems_pct=50,
    )
    d = compute_decision(inp, cfg)
    assert d.reason == "growatt_high_pv"
    assert d.new_apsystems_pct == 55
