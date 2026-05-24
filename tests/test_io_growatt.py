"""Tests for GrowAtt IO client."""

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


def test_read_grid_power_uses_register_31112():
    """Verify read_grid_power calls read_input_registers with 31112, not 31111."""
    with patch("pymodbus.client.ModbusTcpClient"):
        client = GrowattClient("10.0.0.150")
        client._client = MagicMock()
        result = MagicMock()
        result.isError.return_value = False
        result.registers = [0xFFFF, 0xF830]
        client._client.read_input_registers.return_value = result
        client.read_grid_power()
        # Check that read_input_registers was called with 31112 (not 31111)
        call_args = client._client.read_input_registers.call_args
        assert call_args[0][0] == 31112  # address should be 31112


def test_read_pv_power_uses_register_31058():
    """Verify read_pv_power calls read_input_registers with 31058."""
    with patch("pymodbus.client.ModbusTcpClient"):
        client = GrowattClient("10.0.0.150")
        client._client = MagicMock()
        result = MagicMock()
        result.isError.return_value = False
        # 30000 as INT32: high=0x0000, low=0x7530
        result.registers = [0x0000, 0x7530]
        client._client.read_input_registers.return_value = result
        assert client.read_pv_power() == 30000.0 / 10.0  # 3000.0


def test_read_battery_power():
    with patch("pymodbus.client.ModbusTcpClient"):
        client = GrowattClient("10.0.0.150")
        client._client = MagicMock()
        # 500W charging as INT32: high=0x0000, low=0x1388 (5000 / 0.1 = 5000)
        result = MagicMock()
        result.isError.return_value = False
        result.registers = [0x0000, 0x1388]
        client._client.read_input_registers.return_value = result
        assert client.read_battery_power() == 500.0  # 5000 / 10


def test_set_export_limit_writes_in_order():
    """Test that set_export_limit writes disable → set → enable."""
    with patch("pymodbus.client.ModbusTcpClient"):
        client = GrowattClient("10.0.0.150")
        client._client = MagicMock()
        mock_result = MagicMock()
        mock_result.isError.return_value = False
        client._client.write_registers.return_value = mock_result
        client.set_export_limit(5.0)
        # Check write order: disable(30200,0) → set(30201,pct) → enable(30200,1)
        calls = client._client.write_registers.call_args_list
        assert len(calls) == 3
        # First call: disable (30200, [0])
        assert calls[0][0][0] == 30200
        assert calls[0][0][1] == [0]
        # Second call: set (30201, [5])
        assert calls[1][0][0] == 30201
        assert calls[1][0][1] == [5]
        # Third call: enable (30200, [1])
        assert calls[2][0][0] == 30200
        assert calls[2][0][1] == [1]


def test_read_export_limit_status_enabled():
    with patch("pymodbus.client.ModbusTcpClient"):
        client = GrowattClient("10.0.0.150")
        client._client = MagicMock()

        # Mock enabled=1, pct=5
        def mock_read_holding(address, count, device_id=None):
            result = MagicMock()
            result.isError.return_value = False
            if address == 30200:
                result.registers = [1]
            elif address == 30201:
                result.registers = [5.0]
            return result

        client._client.read_holding_registers.side_effect = mock_read_holding
        status = client.read_export_limit_status()
        assert status.enabled is True
        assert status.limit_pct == 5.0


def test_read_export_limit_status_disabled():
    with patch("pymodbus.client.ModbusTcpClient"):
        client = GrowattClient("10.0.0.150")
        client._client = MagicMock()

        def mock_read_holding(address, count, device_id=None):
            result = MagicMock()
            result.isError.return_value = False
            if address == 30200:
                result.registers = [0]
            elif address == 30201:
                result.registers = [0.0]
            return result

        client._client.read_holding_registers.side_effect = mock_read_holding
        status = client.read_export_limit_status()
        assert status.enabled is False
        assert status.limit_pct is None


def test_disable_export_limit_writes_zero_to_30200():
    with patch("pymodbus.client.ModbusTcpClient"):
        client = GrowattClient("10.0.0.150")
        client._client = MagicMock()
        mock_result = MagicMock()
        mock_result.isError.return_value = False
        client._client.write_registers.return_value = mock_result
        client.disable_export_limit()
        call_args = client._client.write_registers.call_args
        assert call_args[0][0] == 30200
        assert call_args[0][1] == [0]


def test_dry_run():
    client = GrowattClient("10.0.0.150", dry_run=True)
    assert client.read_grid_power() == 0.0
    client.set_export_limit(5.0)
    status = client.read_export_limit_status()
    assert status.enabled is True
