"""APSystems ECU/MI Modbus TCP client.

Wraps the APSystems microinverter control protocol.
Based on the verified write_MO.py and solar_power_regulator.py scripts.

Key quirks (from reference scripts):
- Register 40189 power_limit value is multiplied by 10 (250 = 25%)
- ECU returns 300 (30.0%) when it loses state (after power cycle, night, etc.)
- Writes to any slave ID affect ALL microinverters globally
- ECU drops TCP connection if idle >5-10 seconds
- First call after connection may fail — warm_up() works around this
- All registers (power_limit and ECU queries) go through the single ECU port
"""

from __future__ import annotations

import logging
import time
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from pymodbus.client import ModbusTcpClient

logger = logging.getLogger(__name__)

SLAVE_ID = 1
BUGGY_LIMIT_PERMILLE = 300  # 30.0% — returned when ECU loses state
MODBUS_POWER_LIMIT_REGISTER = 40189  # relative to max MI power, value*10
MODBUS_POWER_LIMIT_ENA_REGISTER = 40193  # 0=disable, 1=enable
MODBUS_CONNECTED_REGISTER = 40188  # 0=stop production, 1=resume


class APSystemsClient:
    """Client for APSystems ECU via Modbus TCP.

    When dry_run=True, reads return the last-set value (or 100 default)
    and writes log only.
    """

    def __init__(
        self,
        host: str,
        ecu_port: int = 1503,
        timeout: int = 10,
        dry_run: bool = False,
    ) -> None:
        self.host = host
        self.ecu_port = ecu_port
        self.timeout = timeout
        self.dry_run = dry_run
        self._ecu_client: ModbusTcpClient | None = None
        self._last_written_pct: int = 100
        self._first_connect_done = False

    def connect(self) -> None:
        """Connect to the APSystems ECU."""
        if self.dry_run:
            logger.debug("dry_run: would connect to %s:%s", self.host, self.ecu_port)
            return
        from pymodbus.client import ModbusTcpClient

        self._ecu_client = ModbusTcpClient(
            self.host, port=self.ecu_port, timeout=self.timeout
        )
        if not self._ecu_client.connect():
            raise ConnectionError(f"Failed to connect to APSystems ECU at {self.host}:{self.ecu_port}")

    def warm_up(self) -> None:
        """Dummy read to work around 'first call fails' quirk.

        The ECU may drop connections after ~10s idle. This warms up the connection.
        Also handles the BUGGY 300 (30%) state by forcing 100% on first connect.
        """
        if self.dry_run:
            logger.debug("dry_run: would warm up APSystems connection")
            return
        try:
            result = self._ecu_client.read_holding_registers(  # type: ignore
                MODBUS_POWER_LIMIT_REGISTER, count=1, device_id=SLAVE_ID
            )
            if not result.isError() and hasattr(result, "registers"):
                val = result.registers[0]
                if val == BUGGY_LIMIT_PERMILLE:
                    # ECU lost state — force to 100%
                    logger.warning("ECU returned buggy 30%% value, forcing 100%%")
                    self.set_power_limit(100)
                else:
                    self._last_written_pct = val // 10
        except Exception:
            logger.debug("warm_up read failed (expected on first call)")
        self._first_connect_done = True

    def close(self) -> None:
        if self._ecu_client:
            self._ecu_client.close()
            self._ecu_client = None

    def set_power_limit(self, pct: int) -> None:
        """pct in [0, 100]. Sets via port 502, unit 1.

        Value written is pct * 10 (register uses permille).
        """
        pct = max(0, min(100, pct))
        if self.dry_run:
            logger.info("dry_run: would set APsystems power limit to %s%%", pct)
            self._last_written_pct = pct
            return

        # Avoid writing the buggy 300 value (30%)
        value = pct * 10
        if value == BUGGY_LIMIT_PERMILLE:
            value += 1  # 301 instead of 300

        result = self._ecu_client.write_registers(  # type: ignore
            MODBUS_POWER_LIMIT_REGISTER, [value], device_id=SLAVE_ID
        )
        if result.isError():
            raise ConnectionError(f"Modbus write error at power_limit: {result}")
        self._last_written_pct = pct

        # disable the inverters if the ask is 0pct
        result = self._ecu_client.write_registers(  # type: ignore
            MODBUS_CONNECTED_REGISTER, [0 if value <= 0.01 else 1], device_id=SLAVE_ID
        )
        if result.isError():
            raise ConnectionError(f"Modbus write error at conn_enable: {result}")

        logger.info("APSystems power limit set to %s%% (register value %s)", pct, value)

    def enable_power_limit(self) -> None:
        """Ensures power_limit_ena = 1 via port 1503."""
        if self.dry_run:
            logger.info("dry_run: would enable APsystems power limit")
            return
        result = self._ecu_client.write_registers(  # type: ignore
            MODBUS_POWER_LIMIT_ENA_REGISTER, [1], device_id=SLAVE_ID
        )
        if result.isError():
            raise ConnectionError(f"Modbus write error at power_limit_ena: {result}")
        logger.info("APSystems power limit enabled")

    def disable_power_limit(self) -> None:
        """power_limit_ena = 0, effectively 100% production."""
        if self.dry_run:
            logger.info("dry_run: would disable APsystems power limit")
            return
        result = self._ecu_client.write_registers(  # type: ignore
            MODBUS_POWER_LIMIT_ENA_REGISTER, [0], device_id=SLAVE_ID
        )
        if result.isError():
            raise ConnectionError(f"Modbus write error at power_limit_ena: {result}")
        logger.info("APSystems power limit disabled")

    def read_power_limit(self) -> int:
        """Read current power limit in percent (0-100).

        Returns cached value if dry_run, otherwise reads from register.
        Handles BUGGY_LIMIT_PERMILLE (300) by returning cached value.
        """
        if self.dry_run:
            return self._last_written_pct
        result = self._ecu_client.read_holding_registers(  # type: ignore
            MODBUS_POWER_LIMIT_REGISTER, count=1, device_id=SLAVE_ID
        )
        if result.isError():
            raise ConnectionError(f"Modbus read error at power_limit: {result}")
        val = result.registers[0]
        if val == BUGGY_LIMIT_PERMILLE:
            logger.warning("ECU returned buggy 30%% value, using cached %s%%", self._last_written_pct)
            return self._last_written_pct
        pct = val // 10
        self._last_written_pct = pct
        return pct

    def ensure_connected(self) -> None:
        """Reconnect to the ECU unconditionally.

        The ECU drops idle TCP connections after ~5-10 seconds, and
        is_socket_open() cannot detect a remote-side close, so we always
        close and reconnect before any use.
        """
        if self.dry_run:
            return
        if self._ecu_client is None:
            raise ConnectionError("APSystems client not initialised — call connect() first")
        self._ecu_client.close()
        self._ecu_client.connect()
        self.warm_up()
