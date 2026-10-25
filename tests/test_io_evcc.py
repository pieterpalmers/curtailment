"""Tests for evcc IO client."""

from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock, patch

from curtailment.io.evcc import EvccClient


def _rates(value, field="rate"):
    """evcc tariff response with one slot covering now."""
    now = datetime.now(tz=timezone.utc)
    return {"rates": [{
        "start": (now - timedelta(minutes=15)).isoformat(),
        "end": (now + timedelta(minutes=15)).isoformat(),
        field: value,
    }]}


class TestEvccClient:
    def _make_client(self, dry_run=False, **kwargs):
        with patch("httpx.Client"):
            client = EvccClient("http://10.0.0.150:7070", dry_run=dry_run, **kwargs)
            client._client = MagicMock()
            return client

    def test_get_injection_tariff(self):
        client = self._make_client()
        mock_resp = MagicMock()
        mock_resp.raise_for_status = MagicMock()
        mock_resp.json.return_value = _rates(-0.08)
        client._client.get.return_value = mock_resp
        tariff = client.get_injection_tariff()
        assert tariff == -0.08
        # Check it hits the right path
        call_args = client._client.get.call_args
        assert "feedin" in call_args[0][0] or "tariff" in call_args[0][0]

    def test_get_injection_tariff_failure_raises(self):
        client = self._make_client()
        client._client.get.side_effect = Exception("connection refused")
        try:
            tariff = client.get_injection_tariff()
            assert False, "Should have raised"
        except Exception:
            pass  # Expected

    def test_get_consumption_tariff(self):
        client = self._make_client()
        mock_resp = MagicMock()
        mock_resp.raise_for_status = MagicMock()
        mock_resp.json.return_value = _rates(0.05)
        client._client.get.return_value = mock_resp
        tariff = client.get_consumption_tariff()
        assert tariff == 0.05
        # Check it hits the right path
        call_args = client._client.get.call_args
        assert "grid" in call_args[0][0] or "tariff" in call_args[0][0]

    def test_get_consumption_tariff_failure_raises(self):
        client = self._make_client()
        client._client.get.side_effect = Exception("connection refused")
        try:
            tariff = client.get_consumption_tariff()
            assert False, "Should have raised"
        except Exception:
            pass  # Expected

    def test_get_loadpoint_mode(self):
        client = self._make_client()
        mock_resp = MagicMock()
        mock_resp.raise_for_status = MagicMock()
        mock_resp.json.return_value = {"result": {"loadpoints": [{"mode": "minpv"}]}}
        client._client.get.return_value = mock_resp
        mode = client.get_loadpoint_mode(1)
        assert mode == "minpv"
        # Check it uses /api/state
        call_args = client._client.get.call_args
        assert call_args[0][0] == "/api/state"

    def test_loadpoint_index_off_by_one_handled(self):
        """Loadpoint ID 1 should index position 0 in the array."""
        client = self._make_client()
        mock_resp = MagicMock()
        mock_resp.raise_for_status = MagicMock()
        # Two loadpoints, ID 1 = index 0, ID 2 = index 1
        mock_resp.json.return_value = {
            "result": {"loadpoints": [{"mode": "pv"}, {"mode": "now"}]}
        }
        client._client.get.return_value = mock_resp
        assert client.get_loadpoint_mode(1) == "pv"
        assert client.get_loadpoint_mode(2) == "now"

    def test_set_loadpoint_mode(self):
        client = self._make_client()
        mock_resp = MagicMock()
        mock_resp.raise_for_status = MagicMock()
        client._client.post.return_value = mock_resp
        client.set_loadpoint_mode(1, "minpv")
        # Should use POST /api/loadpoints/{id}/mode/{mode}
        call_args = client._client.post.call_args
        assert "loadpoints/1/mode/minpv" in call_args[0][0]

    def test_set_loadpoint_mode_failure_raises(self):
        client = self._make_client()
        client._client.post.side_effect = Exception("connection refused")
        try:
            client.set_loadpoint_mode(1, "minpv")
            assert False, "Should have raised"
        except Exception:
            pass  # Expected

    def test_uses_configured_tariff_paths(self):
        client = self._make_client(
            injection_tariff_path="/api/tariff/feedin",
            consumption_tariff_path="/api/tariff/grid",
            tariff_field="rate",
        )
        mock_resp = MagicMock()
        mock_resp.raise_for_status = MagicMock()
        mock_resp.json.return_value = _rates(-0.05)
        client._client.get.return_value = mock_resp

        # Test injection tariff uses configured path
        client.get_injection_tariff()
        call_args = client._client.get.call_args
        assert "feedin" in call_args[0][0]

        # Test consumption tariff uses configured path
        client.get_consumption_tariff()
        call_args = client._client.get.call_args
        assert "grid" in call_args[0][0]

    def test_dry_run_no_requests(self):
        client = EvccClient("http://10.0.0.150:7070", dry_run=True)
        client._client = None
        tariff = client.get_injection_tariff()
        assert tariff == -0.10  # default
        mode = client.get_loadpoint_mode(1)
        assert mode == "pv"
        client.set_loadpoint_mode(1, "minpv")  # should not raise
