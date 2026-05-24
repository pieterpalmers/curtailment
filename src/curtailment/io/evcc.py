"""evcc REST API client.

Communicates with evcc via its REST API for reading tariff information
and controlling loadpoint modes.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    import httpx

logger = logging.getLogger(__name__)


class EvccClient:
    """Client for evcc REST API.

    When dry_run=True, reads return sensible defaults and writes log only.
    """

    def __init__(
        self,
        base_url: str,
        timeout: float = 5.0,
        dry_run: bool = False,
        injection_tariff_path: str = "/api/tariff/feedin",
        consumption_tariff_path: str = "/api/tariff/grid",
        tariff_field: str = "rate",
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self.dry_run = dry_run
        self._injection_tariff_path = injection_tariff_path
        self._consumption_tariff_path = consumption_tariff_path
        self._tariff_field = tariff_field
        self._client: httpx.Client | None = None
        self._last_mode: str | None = None

    def connect(self) -> None:
        """Initialize the HTTP client."""
        if self.dry_run:
            logger.debug("dry_run: would connect to evcc at %s", self.base_url)
            return
        import httpx

        self._client = httpx.Client(base_url=self.base_url, timeout=self.timeout)

    def close(self) -> None:
        if self._client:
            self._client.close()
            self._client = None

    def get_injection_tariff(self) -> float:
        """€/kWh, can be negative. Reads from injection tariff endpoint."""
        if self.dry_run:
            return getattr(self, "_injection_tariff_override", -0.10)
        resp = self._client.get(self._injection_tariff_path)  # type: ignore
        resp.raise_for_status()
        data = resp.json()
        return float(data["result"][self._tariff_field])

    def get_consumption_tariff(self) -> float:
        """€/kWh, can be negative. Reads from consumption tariff endpoint."""
        if self.dry_run:
            return getattr(self, "_consumption_tariff_override", 0.05)
        resp = self._client.get(self._consumption_tariff_path)  # type: ignore
        resp.raise_for_status()
        data = resp.json()
        return float(data["result"][self._tariff_field])

    def get_loadpoint_mode(self, loadpoint_id: int) -> str:
        """Returns 'off', 'now', 'minpv', 'pv'. Reads from /api/state."""
        if self.dry_run:
            return self._last_mode or "pv"
        resp = self._client.get("/api/state")  # type: ignore
        resp.raise_for_status()
        data = resp.json()
        loadpoints = data["result"]["loadpoints"]
        return str(loadpoints[loadpoint_id - 1]["mode"])

    def set_loadpoint_mode(self, loadpoint_id: int, mode: str) -> None:
        """Set the loadpoint mode. Uses POST /api/loadpoints/{id}/mode/{mode}."""
        if self.dry_run:
            logger.info("dry_run: would POST evcc loadpoint %s mode %s", loadpoint_id, mode)
            self._last_mode = mode
            return
        resp = self._client.post(  # type: ignore
            f"/api/loadpoints/{loadpoint_id}/mode/{mode}"
        )
        resp.raise_for_status()
        logger.info("evcc loadpoint %s mode set to %s", loadpoint_id, mode)
