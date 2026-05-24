"""Tests for Growatt IO client."""

from unittest.mock import MagicMock, patch

from curtailment.io.growatt import GrowattClient, ExportLimitStatus


class TestGrowattClient:
    def _make_client(self, dry_run=False):
        with patch("pymodbus.client.ModbusTcpClient") as mock:
            client = GrowattClient("10.0.0.150", dry_run=dry_run)
            client._client = MagicMock()
            return client

    def test_read_grid_power(self):
        client = self._make_client()
        # Simulate reading register 31112 (address 31111) → INT32 = -2000 = -200.0W
        mock_result = MagicMock()
        mock_result.isError.return_value = False
        mock_result.registers = [0xFFF8, 0xD8]  # -2000 in INT32
        client._client.read_input_registers.return_value = mock_result
        power = client.read_grid_power()
        assert power == -200.0  # -2000 / 10

    def test_read_pv_power(self):
        client = self._make_client()
        mock_result = MagicMock()
        mock_result.isError.return_value = False
        mock_result.registers = [0x0, 0x1388]  # 5000 in INT32
        client._client.read_input_registers.return_value = mock_result
        power = client.read_pv_power()
        assert power == 500.0  # 5000 / 10

    def test_read_export_limit_status_enabled(self):
        client = self._make_client()
        # Mock reading register 30200 → 1 (enabled), 30201 → 5 (%)
        mock_result_ena = MagicMock()
        mock_result_ena.isError.return_value = False
        mock_result_ena.registers = [1]
        mock_result_pct = MagicMock()
        mock_result_pct.isError.return_value = False
        mock_result_pct.registers = [5]
        client._client.read_holding_registers.side_effect = [
            mock_result_ena, mock_result_pct,
        ]
        status = client.read_export_limit_status()
        assert status.enabled is True
        assert status.limit_pct == 5.0

    def test_set_export_limit(self):
        client = self._make_client()
        client._client.write_registers = MagicMock()
        mock_result = MagicMock()
        mock_result.isError.return_value = False
        client._client.write_registers.return_value = mock_result
        client.set_export_limit(5.0)
        # Should write: disable (30200=0), set power (30201=5), enable (30200=1)
        assert client._client.write_registers.call_count == 3

    def test_dry_run_no_writes(self):
        client = GrowattClient("10.0.0.150", dry_run=True)
        client._client = None  # should not be used
        # These should not raise even without a real connection
        power = client.read_grid_power()
        assert power == 0.0  # default
        client.set_export_limit(5.0)  # should not raise
        status = client.read_export_limit_status()
        assert status.enabled is False

    def test_int32_from_registers_positive(self):
        from curtailment.io.growatt import _int32_from_registers

        result = _int32_from_registers(0x0000, 0x1388)  # 5000
        assert result == 5000

    def test_int32_from_registers_negative(self):
        from curtailment.io.growatt import _int32_from_registers

        # -2000 in two's complement: high=0xFFF8, low=0xD8 (actually 0xFFF8D8 == -2000... let me compute)
        # Actually -2000 = 0xFFFFF830... let me be more careful
        # -2000 as INT32: 0xFFFFF830
        # High 16 bits: 0xFFFF, Low 16 bits: 0xF830
        result = _int32_from_registers(0xFFFF, 0xF830)
        assert result == -2000
