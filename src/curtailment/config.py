"""Config loading and validation."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml


class ConfigError(ValueError):
    """Raised when config validation fails."""


@dataclass
class GrowattConfig:
    host: str
    port: int = 502


@dataclass
class APSystemsConfig:
    host: str
    ecu_port: int = 1503


@dataclass
class EvccConfig:
    base_url: str
    ev_loadpoint_id: int = 1
    default_ev_mode: str = "pv"
    injection_tariff_path: str = "/api/tariff/feedin"
    consumption_tariff_path: str = "/api/tariff/grid"
    tariff_field: str = "rate"


@dataclass
class ControlConfig:
    period_seconds: int = 30
    sample_count: int = 6
    sample_interval_seconds: int = 5

    export_setpoint_watts: float = -200
    deadband_watts: float = 100
    step_up_pct: float = 5
    step_down_pct: float = 10

    growatt_pinned_threshold_watts: float = 100
    growatt_export_limit_pct: float = 5
    start_apsystems_pct: int = 50
    growatt_mode_change_wait_seconds: int = 120


@dataclass
class TariffConfig:
    enter_threshold: float = -0.005
    exit_threshold: float = 0.005
    deep_negative_threshold: float = -0.15
    min_dwell_seconds: int = 600


@dataclass
class LoggingConfig:
    level: str = "INFO"
    format: str = "json"
    file: str = "/var/log/curtailment.log"


@dataclass
class Config:
    growatt: GrowattConfig
    apsystems: APSystemsConfig
    evcc: EvccConfig
    control: ControlConfig
    tariff: TariffConfig
    logging: LoggingConfig

    @classmethod
    def from_file(cls, path: str | Path) -> Config:
        """Load and validate configuration from a YAML file."""
        path = Path(path)
        if not path.exists():
            raise ConfigError(f"Config file not found: {path}")

        raw = yaml.safe_load(path.read_text()) or {}

        try:
            return cls(
                growatt=_parse_growatt(raw.get("growatt", {})),
                apsystems=_parse_apsystems(raw.get("apsystems", {})),
                evcc=_parse_evcc(raw.get("evcc", {})),
                control=_parse_control(raw.get("control", {})),
                tariff=_parse_tariff(raw.get("tariff", {})),
                logging=_parse_logging(raw.get("logging", {})),
            )
        except ConfigError:
            raise
        except Exception as e:
            raise ConfigError(f"Config parsing failed: {e}") from e

    def validate(self) -> None:
        """Run cross-field validations."""
        if self.control.export_setpoint_watts >= 0:
            raise ConfigError("export_setpoint_watts must be negative (exporting)")
        if not 0 <= self.control.growatt_export_limit_pct <= 100:
            raise ConfigError("growatt_export_limit_pct must be in [0, 100]")
        if not 0 <= self.control.start_apsystems_pct <= 100:
            raise ConfigError("start_apsystems_pct must be in [0, 100]")
        if self.control.step_up_pct <= 0 or self.control.step_down_pct <= 0:
            raise ConfigError("step sizes must be positive")
        if self.tariff.enter_threshold >= self.tariff.exit_threshold:
            raise ConfigError("enter_threshold must be < exit_threshold")
        # New validators from Fix 5.1
        # Sampling takes (count-1) * interval seconds; must fit within period
        if (self.control.sample_count - 1) * self.control.sample_interval_seconds >= self.control.period_seconds:
            raise ConfigError("sample_count * sample_interval_seconds must be < period_seconds")
        if self.evcc.default_ev_mode not in {"off", "now", "minpv", "pv"}:
            raise ConfigError("default_ev_mode must be one of: off, now, minpv, pv")
        if self.tariff.enter_threshold > 0:
            raise ConfigError("enter_threshold must be <= 0 (spec: enter when injection < 0)")
        if self.tariff.exit_threshold <= 0:
            raise ConfigError("exit_threshold must be > 0 (spec: exit when injection >= 0.005)")
        if self.tariff.min_dwell_seconds < 0:
            raise ConfigError("min_dwell_seconds must be >= 0")


def _parse_growatt(raw: dict[str, Any]) -> GrowattConfig:
    _require(raw, "host", "growatt.host")
    return GrowattConfig(
        host=raw["host"],
        port=raw.get("port", 502),
    )


def _parse_apsystems(raw: dict[str, Any]) -> APSystemsConfig:
    _require(raw, "host", "apsystems.host")
    return APSystemsConfig(
        host=raw["host"],
        ecu_port=raw.get("ecu_port", 1503),
    )


def _parse_evcc(raw: dict[str, Any]) -> EvccConfig:
    _require(raw, "base_url", "evcc.base_url")
    return EvccConfig(
        base_url=raw["base_url"],
        ev_loadpoint_id=raw.get("ev_loadpoint_id", 1),
        default_ev_mode=raw.get("default_ev_mode", "pv"),
    )


def _parse_control(raw: dict[str, Any]) -> ControlConfig:
    return ControlConfig(
        period_seconds=raw.get("period_seconds", 30),
        sample_count=raw.get("sample_count", 6),
        sample_interval_seconds=raw.get("sample_interval_seconds", 5),
        export_setpoint_watts=raw.get("export_setpoint_watts", -200),
        deadband_watts=raw.get("deadband_watts", 100),
        step_up_pct=raw.get("step_up_pct", 5),
        step_down_pct=raw.get("step_down_pct", 10),
        growatt_pinned_threshold_watts=raw.get("growatt_pinned_threshold_watts", 100),
        growatt_export_limit_pct=raw.get("growatt_export_limit_pct", 5),
        start_apsystems_pct=raw.get("start_apsystems_pct", 50),
        growatt_mode_change_wait_seconds=raw.get("growatt_mode_change_wait_seconds", 120),
    )


def _parse_tariff(raw: dict[str, Any]) -> TariffConfig:
    return TariffConfig(
        enter_threshold=raw.get("enter_threshold", -0.005),
        exit_threshold=raw.get("exit_threshold", 0.0),
        deep_negative_threshold=raw.get("deep_negative_threshold", -0.15),
        min_dwell_seconds=raw.get("min_dwell_seconds", 600),
    )


def _parse_logging(raw: dict[str, Any]) -> LoggingConfig:
    return LoggingConfig(
        level=raw.get("level", "INFO"),
        format=raw.get("format", "json"),
        file=raw.get("file", "/var/log/curtailment.log"),
    )


def _require(raw: dict, key: str, path: str) -> None:
    if key not in raw:
        raise ConfigError(f"Missing required config key: {path}")
