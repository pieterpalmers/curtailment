"""Tests for config module."""

from curtailment.config import Config, ConfigError
from curtailment.config import (
    ControlConfig, TariffConfig, LoggingConfig,
    GrowattConfig, APsystemsConfig, EvccConfig,
)
import yaml


def _make_minimal_yaml(**overrides):
    """Generate a minimal valid config YAML with optional overrides."""
    cfg = {
        "growatt": {"host": "10.0.0.150"},
        "apsystems": {"host": "10.0.0.150"},
        "evcc": {"base_url": "http://10.0.0.150:7070"},
        "control": {"export_setpoint_watts": -200},
        "tariff": {"enter_threshold": -0.005},
        "logging": {"level": "INFO"},
    }
    cfg.update(overrides)
    return yaml.dump(cfg)


class TestConfigLoading:
    def test_from_file_valid(self, tmp_path):
        cfg_file = tmp_path / "config.yaml"
        cfg_file.write_text(_make_minimal_yaml())
        cfg = Config.from_file(str(cfg_file))
        assert cfg.growatt.host == "10.0.0.150"
        assert cfg.growatt.port == 502  # default
        assert cfg.control.export_setpoint_watts == -200

    def test_from_file_missing(self):
        try:
            Config.from_file("/nonexistent/config.yaml")
            assert False, "Should have raised"
        except ConfigError:
            pass

    def test_missing_required_growatt_host(self):
        try:
            Config.from_file_validate({"apsystems": {"host": "x"}})
        except ConfigError:
            pass


class TestConfigValidation:
    def test_validate_export_setpoint_positive(self):
        cfg = Config(
            growatt=GrowattConfig(host="x"),
            apsystems=APSystemsConfig(host="y"),
            evcc=EvccConfig(base_url="http://z"),
            control=ControlConfig(export_setpoint_watts=100),
            tariff=TariffConfig(),
            logging=LoggingConfig(),
        )
        try:
            cfg.validate()
            assert False, "Should have raised"
        except ConfigError as e:
            assert "negative" in str(e)

    def test_validate_step_sizes_zero(self):
        cfg = Config(
            growatt=GrowattConfig(host="x"),
            apsystems=APSystemsConfig(host="y"),
            evcc=EvccConfig(base_url="http://z"),
            control=ControlConfig(step_up_pct=0),
            tariff=TariffConfig(),
            logging=LoggingConfig(),
        )
        try:
            cfg.validate()
            assert False, "Should have raised"
        except ConfigError:
            pass

    def test_validate_tariff_thresholds(self):
        cfg = Config(
            growatt=GrowattConfig(host="x"),
            apsystems=APSystemsConfig(host="y"),
            evcc=EvccConfig(base_url="http://z"),
            control=ControlConfig(),
            tariff=TariffConfig(enter_threshold=0.0, exit_threshold=-0.01),
            logging=LoggingConfig(),
        )
        try:
            cfg.validate()
            assert False, "Should have raised"
        except ConfigError:
            pass

    def test_validate_valid(self):
        cfg = Config(
            growatt=GrowattConfig(host="x"),
            apsystems=APSystemsConfig(host="y"),
            evcc=EvccConfig(base_url="http://z"),
            control=ControlConfig(),
            tariff=TariffConfig(),
            logging=LoggingConfig(),
        )
        cfg.validate()  # should not raise
