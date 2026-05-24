"""Tests for config module."""

import pytest
import yaml

from curtailment.config import Config, ConfigError


def test_from_file_valid(tmp_path):
    cfg_data = {
        "growatt": {"host": "10.0.0.150"},
        "apsystems": {"host": "10.0.0.151"},
        "evcc": {"base_url": "http://10.0.0.152:7070"},
    }
    path = tmp_path / "config.yaml"
    path.write_text(yaml.dump(cfg_data))
    cfg = Config.from_file(str(path))
    assert cfg.growatt.host == "10.0.0.150"
    assert cfg.apsystems.host == "10.0.0.151"


def test_from_file_missing():
    with pytest.raises(ConfigError):
        Config.from_file("/nonexistent.yaml")


def test_validate_bad_setpoint():
    from curtailment.config import (
        GrowattConfig, APSystemsConfig, EvccConfig,
        ControlConfig, TariffConfig, LoggingConfig,
    )
    cfg = Config(
        growatt=GrowattConfig(host="x"),
        apsystems=APSystemsConfig(host="y"),
        evcc=EvccConfig(base_url="http://z"),
        control=ControlConfig(export_setpoint_watts=100),
        tariff=TariffConfig(),
        logging=LoggingConfig(),
    )
    with pytest.raises(ConfigError):
        cfg.validate()


def test_validate_good():
    from curtailment.config import (
        GrowattConfig, APSystemsConfig, EvccConfig,
        ControlConfig, TariffConfig, LoggingConfig,
    )
    cfg = Config(
        growatt=GrowattConfig(host="x"),
        apsystems=APSystemsConfig(host="y"),
        evcc=EvccConfig(base_url="http://z"),
        control=ControlConfig(),
        tariff=TariffConfig(),
        logging=LoggingConfig(),
    )
    cfg.validate()  # should not raise
