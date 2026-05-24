"""Modbus reader for Growatt registers.

Thin wrapper that reads grid_power and pv_power from Growatt via Modbus.
Used by the daemon's _read_world() to populate WorldState.

This is a convenience that bundles the two Growatt reads into one place.
For more control, use GrowattClient directly.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .growatt import GrowattClient

logger = logging.getLogger(__name__)


class ModbusReader:
    """Reads grid power and PV power from Growatt inverter."""

    def __init__(
        self,
        host: str,
        port: int = 502,
        timeout: int = 5,
        dry_run: bool = False,
    ) -> None:
        self._client = GrowattClient(host, port, timeout, dry_run)

    def connect(self) -> None:
        self._client.connect()

    def close(self) -> None:
        self._client.close()

    def read_grid_power(self) -> float:
        """Register 31112 Net Grid Power. Positive=import, negative=export. Watts."""
        return self._client.read_grid_power()

    def read_pv_power(self) -> float:
        """Register 31058 PV Power. Watts."""
        return self._client.read_pv_power()

    def read_all(self) -> tuple[float, float]:
        """Return (grid_power, pv_power) in watts."""
        return self.read_grid_power(), self.read_pv_power()

    @property
    def client(self) -> GrowattClient:
        return self._client
