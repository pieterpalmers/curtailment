"""Tests for controller module."""

from curtailment.controller import ControlInput, ControlDecision, compute_decision
from curtailment.config import ControlConfig


class TestComputeDecision:
    def _cfg(self, **overrides):
        return ControlConfig(**overrides)

    def test_under_exporting_raises(self):
        """Grid less negative than setpoint + deadband → raise APsystems %."""
        cfg = self._cfg(export_setpoint_watts=-200, deadband_watts=50, step_up_pct=5)
        inp = ControlInput(
            grid_power=-100,  # above setpoint (-200) + deadband (50) = -150
            growatt_pv_power=5000,
            current_apsystems_pct=50,
            growatt_export_limit_watts=200,
        )
        decision = compute_decision(inp, cfg)
        assert decision.reason == "under_export"
        assert decision.new_apsystems_pct == 55  # 50 + 5

    def test_over_exporting_lowers(self):
        """Grid more negative than setpoint - deadband → lower APsystems %."""
        cfg = self._cfg(export_setpoint_watts=-200, deadband_watts=50, step_down_pct=10)
        inp = ControlInput(
            grid_power=-300,  # below setpoint (-200) - deadband (50) = -250
            growatt_pv_power=5000,
            current_apsystems_pct=50,
            growatt_export_limit_watts=200,
        )
        decision = compute_decision(inp, cfg)
        assert decision.reason == "over_export"
        assert decision.new_apsystems_pct == 40  # 50 - 10

    def test_on_target_growatt_healthy_hold(self):
        """On target, Growatt healthy → hold."""
        cfg = self._cfg(
            export_setpoint_watts=-200, deadband_watts=50,
            growatt_pinned_threshold_watts=100,
        )
        inp = ControlInput(
            grid_power=-220,  # within deadband of -200
            growatt_pv_power=5000,  # well above pinned threshold
            current_apsystems_pct=50,
            growatt_export_limit_watts=200,
        )
        decision = compute_decision(inp, cfg)
        assert decision.reason == "hold"
        assert decision.new_apsystems_pct == 50

    def test_on_target_growatt_pinned_lowers(self):
        """On target but Growatt pinned → lower to unpin."""
        cfg = self._cfg(
            export_setpoint_watts=-200, deadband_watts=50,
            growatt_pinned_threshold_watts=100, step_down_pct=10,
        )
        inp = ControlInput(
            grid_power=-220,  # within deadband
            growatt_pv_power=50,  # below pinned threshold = pinned!
            current_apsystems_pct=50,
            growatt_export_limit_watts=200,
        )
        decision = compute_decision(inp, cfg)
        assert decision.reason == "pinned_low"
        assert decision.new_apsystems_pct == 40  # 50 - 10

    def test_saturation_at_zero(self):
        """Don't go below 0%."""
        cfg = self._cfg(export_setpoint_watts=-200, deadband_watts=50, step_down_pct=10)
        inp = ControlInput(
            grid_power=-300,
            growatt_pv_power=5000,
            current_apsystems_pct=5,  # can't go below 0
            growatt_export_limit_watts=200,
        )
        decision = compute_decision(inp, cfg)
        assert decision.new_apsystems_pct >= 0

    def test_saturation_at_100(self):
        """Don't go above 100%."""
        cfg = self._cfg(export_setpoint_watts=-200, deadband_watts=50, step_up_pct=10)
        inp = ControlInput(
            grid_power=-100,
            growatt_pv_power=5000,
            current_apsystems_pct=98,
            growatt_export_limit_watts=200,
        )
        decision = compute_decision(inp, cfg)
        assert decision.new_apsystems_pct <= 100

    def test_boundary_deadband_lower(self):
        """Grid exactly at setpoint - deadband → not over_exporting."""
        cfg = self._cfg(export_setpoint_watts=-200, deadband_watts=50)
        inp = ControlInput(
            grid_power=-250,  # exactly at boundary
            growatt_pv_power=5000,
            current_apsystems_pct=50,
            growatt_export_limit_watts=200,
        )
        decision = compute_decision(inp, cfg)
        assert decision.reason == "hold"  # not over_exporting

    def test_boundary_deadband_upper(self):
        """Grid exactly at setpoint + deadband → not under_exporting."""
        cfg = self._cfg(export_setpoint_watts=-200, deadband_watts=50)
        inp = ControlInput(
            grid_power=-150,  # exactly at boundary
            growatt_pv_power=5000,
            current_apsystems_pct=50,
            growatt_export_limit_watts=200,
        )
        decision = compute_decision(inp, cfg)
        assert decision.reason == "hold"  # not under_exporting
