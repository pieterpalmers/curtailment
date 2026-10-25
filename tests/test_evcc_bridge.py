"""Tests for the evcc battery mode bridge."""

import signal
import threading
from unittest.mock import MagicMock

import pytest

from curtailment.config import (
    APSystemsConfig,
    Config,
    ConfigError,
    ControlConfig,
    EvccBridgeConfig,
    EvccConfig,
    GrowattConfig,
    LoggingConfig,
    TariffConfig,
)
from curtailment.evcc_bridge import BatteryMode, EvccBatteryBridge, mode_from_registers


class FakeGrowatt:
    """Holding-register store that records writes."""

    def __init__(self, regs=None):
        self.regs = {1102: 0, 1092: 0, 30100: 1, 30410: 0}
        self.regs.update(regs or {})
        self.writes = []
        self.lock = threading.RLock()
        self.fail_reads = False

    def read_holding(self, address):
        if self.fail_reads:
            raise ConnectionError("boom")
        return self.regs.get(address, 0)

    def write_holding(self, address, value):
        self.writes.append((address, value))
        self.regs[address] = value

    def set_mode(self, battery_first, ac_charge):
        self.regs[1102] = battery_first
        self.regs[1092] = ac_charge


class Clock:
    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t


def make_bridge(regs=None, alive=True, **cfg_kw):
    g = FakeGrowatt(regs)
    clock = Clock()
    alive_check = MagicMock(return_value=alive)
    b = EvccBatteryBridge(EvccBridgeConfig(enabled=True, **cfg_kw), g, alive_check, clock=clock)
    return b, g, clock, alive_check


def poll_twice(b, clock, dt=5):
    b.poll_once()
    clock.t += dt
    b.poll_once()


@pytest.mark.parametrize(
    "bf,ac,mode",
    [
        (0, 0, BatteryMode.NORMAL),
        (1, 0, BatteryMode.HOLD),
        (1, 1, BatteryMode.CHARGE),
        (0, 1, BatteryMode.NORMAL),
    ],
)
def test_mode_mapping(bf, ac, mode):
    assert mode_from_registers(bf, ac) == mode


def test_first_stable_state_is_applied_on_startup():
    b, g, clock, _ = make_bridge()
    poll_twice(b, clock)
    assert b.active == BatteryMode.NORMAL
    assert g.writes == [(30407, 0)]


def test_single_reading_does_not_transition():
    b, g, clock, _ = make_bridge()
    poll_twice(b, clock)
    g.writes.clear()

    # Intermediate state between evcc's two writes: looks like hold for one poll
    g.set_mode(1, 0)
    clock.t += 5
    b.poll_once()
    g.set_mode(1, 1)
    clock.t += 5
    b.poll_once()
    assert g.writes == []
    assert b.active == BatteryMode.NORMAL

    clock.t += 5
    b.poll_once()
    assert b.active == BatteryMode.CHARGE


def test_hold_sequence():
    b, g, clock, _ = make_bridge({1102: 1})
    poll_twice(b, clock)
    assert g.writes == [(30408, 2), (30409, 1), (30407, 1)]


def test_charge_sequence_writes_30410_when_not_set():
    b, g, clock, _ = make_bridge({1102: 1, 1092: 1, 30410: 0}, charge_rate_pct=80)
    poll_twice(b, clock)
    assert g.writes == [(30410, 1), (30408, 2), (30409, 80), (30407, 1)]


def test_charge_sequence_skips_30410_when_already_set():
    b, g, clock, _ = make_bridge({1102: 1, 1092: 1, 30410: 1})
    poll_twice(b, clock)
    assert g.writes == [(30408, 2), (30409, 100), (30407, 1)]


def test_hold_to_charge_does_not_disable_first():
    b, g, clock, _ = make_bridge({1102: 1, 30410: 1})
    poll_twice(b, clock)
    g.writes.clear()
    g.set_mode(1, 1)
    poll_twice(b, clock)
    assert (30407, 0) not in g.writes
    assert g.writes == [(30408, 2), (30409, 100), (30407, 1)]


def test_refresh_only_in_hold_or_charge_after_interval():
    b, g, clock, _ = make_bridge({1102: 1}, refresh_interval_s=30)
    poll_twice(b, clock)  # applied at t=1005
    g.writes.clear()

    clock.t += 20
    b.poll_once()
    assert g.writes == []

    clock.t += 10  # 30s since apply
    b.poll_once()
    assert g.writes == [(30408, 2)]


def test_no_refresh_in_normal():
    b, g, clock, _ = make_bridge()
    poll_twice(b, clock)
    g.writes.clear()
    clock.t += 100
    b.poll_once()
    assert g.writes == []


def test_read_error_keeps_active_and_writes_nothing():
    b, g, clock, _ = make_bridge({1102: 1})
    poll_twice(b, clock)
    g.writes.clear()
    g.fail_reads = True
    clock.t += 5
    b.poll_once()
    assert b.active == BatteryMode.HOLD
    assert g.writes == []
    assert b.last_error is not None


def test_dead_evcc_forces_normal():
    b, g, clock, alive_check = make_bridge({1102: 1, 1092: 1, 30410: 1}, evcc_dead_after_s=120)
    poll_twice(b, clock)
    assert b.active == BatteryMode.CHARGE

    alive_check.return_value = False
    # Still within dead_after window: stays in charge
    clock.t += 60
    b.poll_once()
    assert b.active == BatteryMode.CHARGE

    # Past dead_after since last successful check, two polls to debounce
    clock.t += 90
    b.poll_once()
    clock.t += 5
    b.poll_once()
    assert b.active == BatteryMode.NORMAL
    assert g.writes[-1] == (30407, 0)
    assert b.evcc_alive is False


def test_liveness_check_is_cached():
    b, g, clock, alive_check = make_bridge()
    for _ in range(5):
        b.poll_once()
        clock.t += 5
    assert alive_check.call_count == 1


def test_stop_disables_remote_control():
    b, g, clock, _ = make_bridge({1102: 1})
    poll_twice(b, clock)
    b.stop()
    assert g.writes[-1] == (30407, 0)


def test_sigterm_writes_30407_zero(monkeypatch):
    """SIGTERM -> daemon loop exits -> bridge.stop() writes 30407=0."""
    from curtailment.daemon import Daemon

    cfg = Config(
        growatt=GrowattConfig(host="x"),
        apsystems=APSystemsConfig(host="x"),
        evcc=EvccConfig(base_url="http://x:7070"),
        control=ControlConfig(),
        tariff=TariffConfig(),
        logging=LoggingConfig(),
        evcc_bridge=EvccBridgeConfig(enabled=True),
    )
    g = FakeGrowatt({1102: 1})
    daemon = Daemon(cfg, g, MagicMock(), MagicMock())
    monkeypatch.setattr(daemon, "_connect_all", lambda: None)
    monkeypatch.setattr(daemon, "_disconnect_all", lambda: None)
    monkeypatch.setattr(daemon, "_reconcile_startup", lambda: None)
    monkeypatch.setattr("curtailment.evcc_bridge.http_alive_check", lambda url: lambda: True)

    def cycle():
        signal.raise_signal(signal.SIGTERM)

    monkeypatch.setattr(daemon, "_run_cycle", cycle)
    old = signal.getsignal(signal.SIGTERM), signal.getsignal(signal.SIGINT)
    try:
        daemon.run()
    finally:
        signal.signal(signal.SIGTERM, old[0])
        signal.signal(signal.SIGINT, old[1])
    assert g.writes[-1] == (30407, 0)


def _cfg_with_bridge(**kw):
    return Config(
        growatt=GrowattConfig(host="x"),
        apsystems=APSystemsConfig(host="x"),
        evcc=EvccConfig(base_url="http://x:7070"),
        control=ControlConfig(sample_count=2, sample_interval_seconds=1),
        tariff=TariffConfig(),
        logging=LoggingConfig(),
        evcc_bridge=EvccBridgeConfig(enabled=True, **kw),
    )


def test_config_defaults_valid():
    _cfg_with_bridge().validate()


@pytest.mark.parametrize(
    "kw",
    [
        {"hold_rate_pct": 0},
        {"charge_rate_pct": 0},
        {"charge_rate_pct": 101},
        {"vpp_timer_min": 0},
        {"vpp_timer_min": 11},
        {"vpp_timer_min": 1, "refresh_interval_s": 30},
    ],
)
def test_config_rejects_invalid(kw):
    with pytest.raises(ConfigError):
        _cfg_with_bridge(**kw).validate()
