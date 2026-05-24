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
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self.dry_run = dry_run
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
        """€/kWh, can be negative.

        Falls back to 0.0 if the API endpoint is not available.
        """
        if self.dry_run:
            return getattr(self, "_tariff_override", -0.10)
        try:
            resp = self._client.get("/api/tariff")  # type: ignore
            resp.raise_for_status()
            data = resp.json()
            # The tariff API returns the injection rate
            return float(data.get("injection", 0.0))
        except Exception as e:
            logger.warning("Failed to read tariff from evcc: %s", e)
            return 0.0

    def get_loadpoint_mode(self, loadpoint_id: int) -> str:
        """Returns 'off', 'now', 'minpv', 'pv'."""
        if self.dry_run:
            return self._last_mode or "pv"
        try:
            resp = self._client.get(f"/api/loadpoints/{loadpoint_id}")  # type: ignore
            resp.raise_for_status()
            data = resp.json()
            return str(data.get("mode", "pv"))
        except Exception as e:
            logger.warning("Failed to read loadpoint mode from evcc: %s", e)
            return "pv"

    def set_loadpoint_mode(self, loadpoint_id: int, mode: str) -> None:
        """Set the loadpoint mode."""
        if self.dry_run:
            logger.info("dry_run: would set evcc loadpoint %s mode to %s", loadpoint_id, mode)
            self._last_mode = mode
            return
        try:
            resp = self._client.put(  # type: ignore
                f"/api/loadpoints/{loadpoint_id}/mode",
                json={"mode": mode},
            )
            resp.raise_for_status()
            logger.info("evcc loadpoint %s mode set to %s", loadpoint_id, mode)
        except Exception as e:
            logger.warning("Failed to set loadpoint mode on evcc: %s", e)
