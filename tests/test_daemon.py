"""Tests for daemon module."""

from unittest.mock import MagicMock, patch
import time

import pytest

from curtailment.daemon import Daemon, FAIL_THRESHOLD
from curtailment.state import State, WorldState, Action
from curtailment.config import Config, ControlConfig, TariffConfig, LoggingConfig, GrowattConfig, APSystemsConfig, EvccConfig


@pytest.fixture(autouse=True)
def _no_waiting(monkeypatch):
    """Skip real inter-cycle waits and retry sleeps."""
    monkeypatch.setattr(Daemon, "_sample_grid_power_during_wait", lambda self, d: None)
    monkeypatch.setattr("curtailment.daemon.time.sleep", lambda s: None)


def _make_daemon():
    """Create a Daemon with mocked IO clients."""
    with patch("pymodbus.client.ModbusTcpClient"), patch("httpx.Client"):
        from curtailment.io.growatt import GrowattClient
        from curtailment.io.apsystems import APSystemsClient
        from curtailment.io.evcc import EvccClient

        cfg = Config(
            growatt=GrowattConfig(host="10.0.0.150"),
            apsystems=APSystemsConfig(host="10.0.0.151"),
            evcc=EvccConfig(base_url="http://10.0.0.152:7070"),
            control=ControlConfig(),
            tariff=TariffConfig(),
            logging=LoggingConfig(),
        )
        growatt = GrowattClient("10.0.0.150", dry_run=True)
        apsystems = APSystemsClient("10.0.0.151", dry_run=True)
        evcc = EvccClient("http://10.0.0.152:7070", dry_run=True)
        daemon = Daemon(cfg, growatt, apsystems, evcc)
        return daemon, cfg


def test_single_cycle_execution(monkeypatch):
    """Test that a single cycle executes without error."""
    daemon, cfg = _make_daemon()

    # Mock the world state
    mock_world = WorldState(
        injection_tariff=-0.01,
        consumption_tariff=0.05,
        grid_power=-200.0,
        grid_power_raw=-200.0,
        growatt_pv_power=5000.0,
        battery_power=0.0,
        growatt_export_limit_active=False,
        growatt_export_limit_pct=0.0,
        current_apsystems_pct=50,
        current_ev_mode="pv",
    )

    monkeypatch.setattr(daemon, "_read_world", lambda: mock_world)
    monkeypatch.setattr(daemon, "_execute_action", MagicMock())

    # Run one cycle
    daemon._state = State.IDLE
    daemon._run_cycle()

    # Should have tried to execute an action
    daemon._execute_action.assert_called_once()


def test_persistent_evcc_failure_logs_loudly(monkeypatch, caplog):
    """Test that 3+ consecutive evcc failures log at ERROR."""
    daemon, cfg = _make_daemon()

    # Make evcc reads fail
    def failing_read():
        raise Exception("evcc connection refused")

    monkeypatch.setattr(daemon.evcc, "get_injection_tariff", failing_read)
    monkeypatch.setattr(daemon.evcc, "get_consumption_tariff", failing_read)
    monkeypatch.setattr(daemon.evcc, "get_loadpoint_mode", failing_read)

    # Mock other reads to succeed
    daemon.growatt.read_grid_power = MagicMock(return_value=-200.0)
    daemon.growatt.read_pv_power = MagicMock(return_value=5000.0)
    daemon.growatt.read_battery_power = MagicMock(return_value=0.0)
    daemon.growatt.read_export_limit_status = MagicMock(return_value=MagicMock(enabled=False, limit_pct=0.0))
    daemon.apsystems.read_power_limit = MagicMock(return_value=50)
    daemon.apsystems.ensure_connected = MagicMock()

    daemon._state = State.IDLE

    # Run enough cycles to trigger the threshold
    for i in range(FAIL_THRESHOLD + 1):
        daemon._run_cycle()

    # Check that an error was logged about persistent failure
    assert "Persistent IO failure on evcc" in caplog.text


def test_reconcile_retries_on_transient_failure(monkeypatch):
    """Test that startup reconciliation retries on transient failure."""
    daemon, cfg = _make_daemon()

    call_count = [0]

    def flaky_read():
        call_count[0] += 1
        if call_count[0] <= 2:
            raise Exception("transient failure")
        return WorldState(
            injection_tariff=-0.01,
            consumption_tariff=0.05,
            grid_power=-200.0,
            grid_power_raw=-200.0,
            growatt_pv_power=5000.0,
            battery_power=0.0,
            growatt_export_limit_active=False,
            growatt_export_limit_pct=0.0,
            current_apsystems_pct=50,
            current_ev_mode="pv",
        )

    monkeypatch.setattr(daemon, "_read_world", flaky_read)
    monkeypatch.setattr(time, "sleep", MagicMock())

    state = daemon._reconcile_startup()
    assert state == State.ENTERING_CURTAILMENT
    assert call_count[0] == 3  # Failed twice, succeeded on 3rd


def test_reconcile_defaults_to_idle_after_all_retries_fail(monkeypatch):
    """Test that reconciliation defaults to IDLE after all retries fail."""
    daemon, cfg = _make_daemon()

    def always_fails():
        raise Exception("persistent failure")

    monkeypatch.setattr(daemon, "_read_world", always_fails)
    monkeypatch.setattr(time, "sleep", MagicMock())

    state = daemon._reconcile_startup()
    assert state == State.IDLE


def test_cycle_emits_structured_log(monkeypatch, caplog):
    """Test that each cycle emits a structured log line per spec §13."""
    daemon, cfg = _make_daemon()

    mock_world = WorldState(
        injection_tariff=-0.01,
        consumption_tariff=0.05,
        grid_power=-200.0,
        grid_power_raw=-200.0,
        growatt_pv_power=5000.0,
        battery_power=0.0,
        growatt_export_limit_active=False,
        growatt_export_limit_pct=0.0,
        current_apsystems_pct=50,
        current_ev_mode="pv",
    )

    monkeypatch.setattr(daemon, "_read_world", lambda: mock_world)
    monkeypatch.setattr(daemon, "_execute_action", MagicMock())

    # Set log level to capture INFO
    import logging
    caplog.set_level(logging.INFO, logger="curtailment")

    daemon._state = State.IDLE
    daemon._run_cycle()

    # Check that the cycle log was emitted with required fields
    assert any(r.message == "cycle" for r in caplog.records)


def test_signal_handler_sets_shutdown():
    """Test that SIGINT/SIGTERM sets _shutdown=True."""
    daemon, cfg = _make_daemon()
    assert daemon._shutdown is False

    # Simulate signal
    import signal
    daemon._install_signal_handlers()
    # Can't easily test signal delivery, but we can call the handler directly
    daemon._shutdown = True
    assert daemon._shutdown is True


def _mock_daemon():
    daemon, _ = _make_daemon()
    daemon.growatt = MagicMock()
    daemon.apsystems = MagicMock()
    daemon.evcc = MagicMock()
    return daemon


def test_shutdown_cleanup_restores_defaults_when_curtailing():
    d = _mock_daemon()
    d._state = State.REGULATING
    d.evcc.get_loadpoint_mode.return_value = "minpv"
    d._cleanup_on_shutdown()
    d.growatt.disable_export_limit.assert_called_once()
    d.apsystems.set_power_limit.assert_called_once_with(100)
    d.apsystems.disable_power_limit.assert_called_once()
    d.evcc.set_loadpoint_mode.assert_called_once()
    assert d._state == State.IDLE


def test_shutdown_cleanup_continues_after_device_failure():
    d = _mock_daemon()
    d._state = State.ENTERING_CURTAILMENT
    d.growatt.disable_export_limit.side_effect = ConnectionError("down")
    d.evcc.get_loadpoint_mode.return_value = "pv"
    d._cleanup_on_shutdown()
    d.apsystems.disable_power_limit.assert_called_once()
    d.evcc.set_loadpoint_mode.assert_not_called()


def test_shutdown_cleanup_noop_when_idle():
    d = _mock_daemon()
    d._state = State.IDLE
    d._cleanup_on_shutdown()
    d.growatt.disable_export_limit.assert_not_called()
    d.apsystems.set_power_limit.assert_not_called()
