"""Tests for evcc IO client."""

from unittest.mock import MagicMock, patch

from curtailment.io.evcc import EvccClient


class TestEvccClient:
    def _make_client(self, dry_run=False):
        with patch("httpx.Client"):
            client = EvccClient("http://10.0.0.150:7070", dry_run=dry_run)
            client._client = MagicMock()
            return client

    def test_get_injection_tariff(self):
        client = self._make_client()
        mock_resp = MagicMock()
        mock_resp.raise_for_status = MagicMock()
        mock_resp.json.return_value = {"injection": -0.08}
        client._client.get.return_value = mock_resp
        tariff = client.get_injection_tariff()
        assert tariff == -0.08

    def test_get_injection_tariff_failure_returns_zero(self):
        client = self._make_client()
        client._client.get.side_effect = Exception("connection refused")
        tariff = client.get_injection_tariff()
        assert tariff == 0.0

    def test_get_loadpoint_mode(self):
        client = self._make_client()
        mock_resp = MagicMock()
        mock_resp.raise_for_status = MagicMock()
        mock_resp.json.return_value = {"mode": "minpv"}
        client._client.get.return_value = mock_resp
        mode = client.get_loadpoint_mode(1)
        assert mode == "minpv"

    def test_set_loadpoint_mode(self):
        client = self._make_client()
        mock_resp = MagicMock()
        mock_resp.raise_for_status = MagicMock()
        client._client.put.return_value = mock_resp
        client.set_loadpoint_mode(1, "minpv")
        call_args = client._client.put.call_args
        assert "mode" in call_args[1]["json"] or "mode" in call_args[0][1]

    def test_dry_run_no_requests(self):
        client = EvccClient("http://10.0.0.150:7070", dry_run=True)
        client._client = None
        tariff = client.get_injection_tariff()
        assert tariff == -0.10  # default
        mode = client.get_loadpoint_mode(1)
        assert mode == "pv"
        client.set_loadpoint_mode(1, "minpv")  # should not raise
