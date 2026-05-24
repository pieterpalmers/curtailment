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


def test_validate_growatt_export_limit_pct():
    """growatt_export_limit_pct must be in [0, 100]."""
    from curtailment.config import (
        GrowattConfig, APSystemsConfig, EvccConfig,
        ControlConfig, TariffConfig, LoggingConfig,
    )
    cfg = Config(
        growatt=GrowattConfig(host="x"),
        apsystems=APSystemsConfig(host="y"),
        evcc=EvccConfig(base_url="http://z"),
        control=ControlConfig(growatt_export_limit_pct=150),
        tariff=TariffConfig(),
        logging=LoggingConfig(),
    )
    with pytest.raises(ConfigError):
        cfg.validate()


def test_validate_apsystems_pct():
    """start_apsystems_pct must be in [0, 100]."""
    from curtailment.config import (
        GrowattConfig, APSystemsConfig, EvccConfig,
        ControlConfig, TariffConfig, LoggingConfig,
    )
    cfg = Config(
        growatt=GrowattConfig(host="x"),
        apsystems=APSystemsConfig(host="y"),
        evcc=EvccConfig(base_url="http://z"),
        control=ControlConfig(start_apsystems_pct=150),
        tariff=TariffConfig(),
        logging=LoggingConfig(),
    )
    with pytest.raises(ConfigError):
        cfg.validate()


def test_validate_step_sizes():
    """Step sizes must be positive."""
    from curtailment.config import (
        GrowattConfig, APSystemsConfig, EvccConfig,
        ControlConfig, TariffConfig, LoggingConfig,
    )
    cfg = Config(
        growatt=GrowattConfig(host="x"),
        apsystems=APSystemsConfig(host="y"),
        evcc=EvccConfig(base_url="http://z"),
        control=ControlConfig(step_up_pct=0),
        tariff=TariffConfig(),
        logging=LoggingConfig(),
    )
    with pytest.raises(ConfigError):
        cfg.validate()


def test_validate_tariff_thresholds():
    """enter_threshold must be < exit_threshold (hysteresis)."""
    from curtailment.config import (
        GrowattConfig, APSystemsConfig, EvccConfig,
        ControlConfig, TariffConfig, LoggingConfig,
    )
    # enter >= exit should fail
    cfg = Config(
        growatt=GrowattConfig(host="x"),
        apsystems=APSystemsConfig(host="y"),
        evcc=EvccConfig(base_url="http://z"),
        control=ControlConfig(),
        tariff=TariffConfig(enter_threshold=0.0, exit_threshold=0.0),
        logging=LoggingConfig(),
    )
    with pytest.raises(ConfigError):
        cfg.validate()


def test_validate_enter_threshold_negative():
    """enter_threshold should be <= 0 (spec: enter when injection < 0)."""
    from curtailment.config import (
        GrowattConfig, APSystemsConfig, EvccConfig,
        ControlConfig, TariffConfig, LoggingConfig,
    )
    cfg = Config(
        growatt=GrowattConfig(host="x"),
        apsystems=APSystemsConfig(host="y"),
        evcc=EvccConfig(base_url="http://z"),
        control=ControlConfig(),
        tariff=TariffConfig(enter_threshold=0.01),  # positive = bad
        logging=LoggingConfig(),
    )
    with pytest.raises(ConfigError):
        cfg.validate()


def test_validate_exit_threshold_positive():
    """exit_threshold should be > 0 (spec: exit when injection >= 0.005)."""
    from curtailment.config import (
        GrowattConfig, APSystemsConfig, EvccConfig,
        ControlConfig, TariffConfig, LoggingConfig,
    )
    cfg = Config(
        growatt=GrowattConfig(host="x"),
        apsystems=APSystemsConfig(host="y"),
        evcc=EvccConfig(base_url="http://z"),
        control=ControlConfig(),
        tariff=TariffConfig(exit_threshold=-0.01),  # negative = bad
        logging=LoggingConfig(),
    )
    with pytest.raises(ConfigError):
        cfg.validate()


def test_validate_hysteresis_direction():
    """exit_threshold must be > enter_threshold."""
    from curtailment.config import (
        GrowattConfig, APSystemsConfig, EvccConfig,
        ControlConfig, TariffConfig, LoggingConfig,
    )
    # exit <= enter should fail
    cfg = Config(
        growatt=GrowattConfig(host="x"),
        apsystems=APSystemsConfig(host="y"),
        evcc=EvccConfig(base_url="http://z"),
        control=ControlConfig(),
        tariff=TariffConfig(enter_threshold=-0.005, exit_threshold=-0.001),
        logging=LoggingConfig(),
    )
    with pytest.raises(ConfigError):
        cfg.validate()


def test_validate_min_dwell_seconds():
    """min_dwell_seconds must be >= 0."""
    from curtailment.config import (
        GrowattConfig, APSystemsConfig, EvccConfig,
        ControlConfig, TariffConfig, LoggingConfig,
    )
    cfg = Config(
        growatt=GrowattConfig(host="x"),
        apsystems=APSystemsConfig(host="y"),
        evcc=EvccConfig(base_url="http://z"),
        control=ControlConfig(),
        tariff=TariffConfig(min_dwell_seconds=-10),
        logging=LoggingConfig(),
    )
    with pytest.raises(ConfigError):
        cfg.validate()


def test_validate_sample_timing():
    """sample_count * sample_interval_seconds < period_seconds."""
    from curtailment.config import (
        GrowattConfig, APSystemsConfig, EvccConfig,
        ControlConfig, TariffConfig, LoggingConfig,
    )
    # (6-1) * 5 = 25, period=30 → 25 < 30, should pass
    cfg = Config(
        growatt=GrowattConfig(host="x"),
        apsystems=APSystemsConfig(host="y"),
        evcc=EvccConfig(base_url="http://z"),
        control=ControlConfig(
            sample_count=6,
            sample_interval_seconds=5,
            period_seconds=30,
        ),
        tariff=TariffConfig(),
        logging=LoggingConfig(),
    )
    cfg.validate()  # should not raise

    # Now test failure case: (3-1) * 5 = 10, period=5 → 10 >= 5, should fail
    cfg2 = Config(
        growatt=GrowattConfig(host="x"),
        apsystems=APSystemsConfig(host="y"),
        evcc=EvccConfig(base_url="http://z"),
        control=ControlConfig(
            sample_count=3,
            sample_interval_seconds=5,
            period_seconds=5,
        ),
        tariff=TariffConfig(),
        logging=LoggingConfig(),
    )
    with pytest.raises(ConfigError):
        cfg2.validate()


def test_validate_ev_mode():
    """default_ev_mode must be in {off, now, minpv, pv}."""
    from curtailment.config import (
        GrowattConfig, APSystemsConfig, EvccConfig,
        ControlConfig, TariffConfig, LoggingConfig,
    )
    cfg = Config(
        growatt=GrowattConfig(host="x"),
        apsystems=APSystemsConfig(host="y"),
        evcc=EvccConfig(base_url="http://z", default_ev_mode="invalid"),
        control=ControlConfig(),
        tariff=TariffConfig(),
        logging=LoggingConfig(),
    )
    with pytest.raises(ConfigError):
        cfg.validate()
