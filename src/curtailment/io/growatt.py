"""Growatt Modbus TCP client.

Wraps the Growatt inverter Modbus protocol for reading grid power, PV power,
and controlling the export limit. Based on the verified growatt_export_limit.py script.

Register addresses use the same 1-based convention as the reference script:
- 31112 Net Grid Power INT32 (0.1W unit)
- 31058 PV Power INT32 (0.1W unit)
- 30200 Export Limit Enable (0/1)
- 30201 Export Limit Power % [-100, 100]
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from pymodbus.client import ModbusTcpClient

logger = logging.getLogger(__name__)

SLAVE_ID = 1
DEFAULT_TIMEOUT = 5
MAX_RETRIES = 2


class GrowattClient:
    """Client for Growatt inverter via Modbus TCP.

    When dry_run=True, reads return sensible defaults and writes log only.
    """

    def __init__(
        self,
        host: str,
        port: int = 502,
        timeout: int = DEFAULT_TIMEOUT,
        dry_run: bool = False,
    ) -> None:
        self.host = host
        self.port = port
        self.timeout = timeout
        self.dry_run = dry_run
        self._client: ModbusTcpClient | None = None

    def connect(self) -> None:
        """Connect to the Growatt inverter."""
        if self.dry_run:
            logger.debug("dry_run: would connect to %s:%s", self.host, self.port)
            return
        from pymodbus.client import ModbusTcpClient

        self._client = ModbusTcpClient(self.host, port=self.port, timeout=self.timeout)
        if not self._client.connect():
            raise ConnectionError(f"Failed to connect to Growatt at {self.host}:{self.port}")

    def close(self) -> None:
        if self._client:
            self._client.close()
            self._client = None

    def read_grid_power(self) -> float:
        """Register 31112 Net Grid Power. Positive=import, negative=export. Watts."""
        if self.dry_run:
            return getattr(self, "_last_grid_power", 0.0)
        raw = self._read_input_int32(31112)
        watts = raw / 10.0
        self._last_grid_power = watts
        return watts

    def read_pv_power(self) -> float:
        """Register 31058 PV Power. Watts."""
        if self.dry_run:
            return getattr(self, "_last_pv_power", 3000.0)
        raw = self._read_input_int32(31058)
        watts = raw / 10.0
        self._last_pv_power = watts
        return watts

    REG_BATTERY_POWER = 31200  # INT32 high|low, 0.1 W, + = charging

    def read_battery_power(self) -> float:
        """Register 31200 Battery Power. Watts. Positive=charging."""
        if self.dry_run:
            return getattr(self, "_last_battery_power", 0.0)
        raw = self._read_input_int32(self.REG_BATTERY_POWER)
        watts = raw / 10.0
        self._last_battery_power = watts
        return watts

    def read_export_limit_status(self) -> ExportLimitStatus:
        """Returns enabled status and limit percentage."""
        if self.dry_run:
            return ExportLimitStatus(
                enabled=getattr(self, "_limit_ena", False),
                limit_pct=getattr(self, "_limit_pct", None),
            )
        enabled = self._read_holding(30200) == 1
        pct = float(self._read_holding(30201))
        return ExportLimitStatus(enabled=enabled, limit_pct=pct if enabled else None)

    def set_export_limit(self, pct: float) -> None:
        """pct in [-100, 100]. Enables limit if not enabled."""
        if self.dry_run:
            logger.info("dry_run: would set Growatt export limit to %s%%", pct)
            self._limit_ena = True
            self._limit_pct = pct
            return
        # Order from reference: disable, set power, enable
        self._write_holding(30200, 0)  # disable first
        self._write_holding(30201, int(pct))
        self._write_holding(30200, 1)  # enable
        logger.info("Growatt export limit set to %s%%", pct)

    def disable_export_limit(self) -> None:
        if self.dry_run:
            logger.info("dry_run: would disable Growatt export limit")
            self._limit_ena = False
            self._limit_pct = None
            return
        self._write_holding(30200, 0)
        logger.info("Growatt export limit disabled")

    def _read_input_int32(self, address: int) -> int:
        """Read a 32-bit signed integer from input registers (high|low)."""
        result = self._retry_read(self._client.read_input_registers, address, 2)
        return _int32_from_registers(result.registers[0], result.registers[1])

    def _read_holding(self, address: int) -> int:
        """Read a single holding register (1-based address)."""
        result = self._retry_read(self._client.read_holding_registers, address, 1)
        return result.registers[0]

    def _write_holding(self, address: int, value: int) -> None:
        """Write a single holding register (1-based address)."""
        if self.dry_run:
            return
        result = self._retry_write(self._client.write_registers, address, [value])
        if result.isError():
            raise ConnectionError(f"Modbus write error at {address}: {result}")

    def _retry_read(self, read_fn, address, count, max_retries=MAX_RETRIES):
        """Retry logic for reads, matching the reference script behavior."""
        last_error = None
        for attempt in range(max_retries + 1):
            if attempt > 0:
                time.sleep(0.5 * attempt)
            try:
                result = read_fn(address, count, device_id=SLAVE_ID)
                if result.isError():
                    raise ConnectionError(f"Modbus read error at {address}: {result}")
                return result
            except Exception as e:
                last_error = e
                if attempt == max_retries:
                    raise
        raise last_error  # type: ignore

    def _retry_write(self, write_fn, address, values, max_retries=MAX_RETRIES):
        """Retry logic for writes."""
        last_error = None
        for attempt in range(max_retries + 1):
            if attempt > 0:
                time.sleep(0.5 * attempt)
            try:
                result = write_fn(address, values, device_id=SLAVE_ID)
                if result.isError():
                    raise ConnectionError(f"Modbus write error at {address}: {result}")
                return result
            except Exception as e:
                last_error = e
                if attempt == max_retries:
                    raise
        raise last_error  # type: ignore


@dataclass
class ExportLimitStatus:
    enabled: bool
    limit_pct: float | None


def _int32_from_registers(high: int, low: int) -> int:
    """Combine two 16-bit registers into a signed 32-bit integer."""
    value = (high << 16) | low
    if value >= 0x80000000:
        value -= 0x100000000
    return value
