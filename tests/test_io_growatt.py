"""Tests for Growatt IO client."""

from unittest.mock import MagicMock, patch

from curtailment.io.growatt import GrowattClient


def test_read_grid_power():
    with patch("pymodbus.client.ModbusTcpClient"):
        client = GrowattClient("10.0.0.150")
        client._client = MagicMock()
        # -2000 as INT32: high=0xFFFF, low=0xF830
        result = MagicMock()
        result.isError.return_value = False
        result.registers = [0xFFFF, 0xF830]
        client._client.read_input_registers.return_value = result
        assert client.read_grid_power() == -200.0


def test_dry_run():
    client = GrowattClient("10.0.0.150", dry_run=True)
    assert client.read_grid_power() == 0.0
    client.set_export_limit(5.0)
    status = client.read_export_limit_status()
    assert status.enabled is True
