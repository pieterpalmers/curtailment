"""Tests for APSystems IO client."""

from unittest.mock import MagicMock, patch

from curtailment.io.apsystems import APSystemsClient, BUGGY_LIMIT_PERMILLE


class TestAPSystemsClient:
    def _make_client(self, dry_run=False):
        with patch("pymodbus.client.ModbusTcpClient"):
            client = APSystemsClient("10.0.0.150", dry_run=dry_run)
            client._ecu_client = MagicMock()
            client._inv_client = MagicMock()
            return client

    def test_set_power_limit(self):
        client = self._make_client()
        mock_result = MagicMock()
        mock_result.isError.return_value = False
        client._inv_client.write_registers.return_value = mock_result
        client.set_power_limit(25)  # 25%
        # Should write value 250 (25 * 10) to register 40189
        call_args = client._inv_client.write_registers.call_args
        assert call_args[0][0] == 40189  # register
        assert call_args[0][1] == [250]  # value*10

    def test_set_power_limit_avoid_buggy_300(self):
        client = self._make_client()
        mock_result = MagicMock()
        mock_result.isError.return_value = False
        client._inv_client.write_registers.return_value = mock_result
        # 30% → 300 permille, which is the buggy value
        client.set_power_limit(30)
        call_args = client._inv_client.write_registers.call_args
        # Should write 301 instead of 300
        assert call_args[0][1] == [301]

    def test_read_power_limit_buggy_300(self):
        client = self._make_client()
        # Simulate reading 300 (buggy value)
        mock_result = MagicMock()
        mock_result.isError.return_value = False
        mock_result.registers = [BUGGY_LIMIT_PERMILLE]
        client._inv_client.read_holding_registers.return_value = mock_result
        # Set a cached value first
        client._last_written_pct = 50
        pct = client.read_power_limit()
        # Should return cached value, not 30
        assert pct == 50

    def test_read_power_limit_normal(self):
        client = self._make_client()
        mock_result = MagicMock()
        mock_result.isError.return_value = False
        mock_result.registers = [250]  # 25%
        client._inv_client.read_holding_registers.return_value = mock_result
        pct = client.read_power_limit()
        assert pct == 25

    def test_dry_run_no_writes(self):
        client = APSystemsClient("10.0.0.150", dry_run=True)
        client._inv_client = None
        client._ecu_client = None
        # These should not raise
        client.set_power_limit(50)
        assert client.read_power_limit() == 50
        client.enable_power_limit()
        client.disable_power_limit()

    def test_enable_power_limit(self):
        client = self._make_client()
        mock_result = MagicMock()
        mock_result.isError.return_value = False
        client._ecu_client.write_registers.return_value = mock_result
        client.enable_power_limit()
        call_args = client._ecu_client.write_registers.call_args
        assert call_args[0][0] == 40193  # register
        assert call_args[0][1] == [1]  # enable

    def test_warm_up_handles_buggy_300(self):
        client = self._make_client()
        # Simulate reading 300 on warm-up
        mock_result = MagicMock()
        mock_result.isError.return_value = False
        mock_result.registers = [BUGGY_LIMIT_PERMILLE]
        client._inv_client.read_holding_registers.return_value = mock_result
        # Also mock the set_power_limit to avoid recursive issues
        client.set_power_limit = MagicMock()
        client.warm_up()
        # Should have forced 100%
        client.set_power_limit.assert_called_once_with(100)
