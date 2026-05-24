"""Simulate the state machine against synthetic CSV inputs.

CSV format: tariff,grid_power,pv_power[,seconds_since_start]
Drives decide_action through state transitions, advancing synthetic time_in_state.
"""

from __future__ import annotations

import argparse
import csv
import sys
from dataclasses import dataclass

from curtailment.config import ControlConfig, TariffConfig
from curtailment.state import State, WorldState, reconcile_startup, decide_action


@dataclass
class CsvRow:
    injection_tariff: float
    grid_power: float
    pv_power: float
    seconds_since_start: float = 0.0


def main() -> None:
    parser = argparse.ArgumentParser(description="Dry run against CSV data")
    parser.add_argument("csv_file", help="Path to CSV file with synthetic data")
    parser.add_argument("--apsystems-pct", type=int, default=50, help="Starting APsystems %%")
    args = parser.parse_args()

    cfg = ControlConfig()
    tariff_cfg = TariffConfig()
    current_pct = args.apsystems_pct
    time_in_state = 0.0
    state = State.IDLE

    rows = _load_csv(args.csv_file)

    print(f"{'tariff':>10} {'grid':>8} {'pv':>8} {'state':>12} {'aps_before':>12} {'decision':>15} {'aps_after':>11}")
    print("-" * 80)

    for i, row in enumerate(rows):
        # Advance time for state machine
        if i > 0:
            time_in_state += row.seconds_since_start if row.seconds_since_start > 0 else cfg.period_seconds

        world = WorldState(
            injection_tariff=row.injection_tariff,
            consumption_tariff=0.05,  # not used in v1 decisions
            grid_power=row.grid_power,
            grid_power_raw=row.grid_power,
            growatt_pv_power=row.pv_power,
            battery_power=0.0,
            growatt_export_limit_active=(state == State.REGULATING or state == State.ENTERING_CURTAILMENT),
            current_apsystems_pct=current_pct,
            current_ev_mode="pv",
        )

        action = decide_action(state, world, _make_cfg(), time_in_state)

        print(
            f"{row.injection_tariff:>10.4f} "
            f"{row.grid_power:>8.0f} "
            f"{row.pv_power:>8.0f} "
            f"{state.value:>12} "
            f"{current_pct:>12} "
            f"{action.reason:>15} "
            f"{action.set_apsystems_pct if action.set_apsystems_pct is not None else current_pct:>11}"
        )

        # Update state and pct for next cycle
        if action.next_state != state:
            time_in_state = 0.0  # reset on transition
            state = action.next_state
        if action.set_apsystems_pct is not None:
            current_pct = action.set_apsystems_pct


def _make_cfg():
    """Create a simple config for dry run."""
    from curtailment.config import (
        Config, GrowattConfig, APSystemsConfig, EvccConfig,
        LoggingConfig,
    )
    return Config(
        growatt=GrowattConfig(host="x"),
        apsystems=APSystemsConfig(host="y"),
        evcc=EvccConfig(base_url="http://z"),
        control=ControlConfig(),
        tariff=TariffConfig(),
        logging=LoggingConfig(),
    )


def _load_csv(path: str) -> list[CsvRow]:
    rows = []
    with open(path) as f:
        reader = csv.DictReader(f)
        for r in reader:
            rows.append(
                CsvRow(
                    injection_tariff=float(r.get("tariff", 0)),
                    grid_power=float(r.get("grid_power", 0)),
                    pv_power=float(r.get("pv_power", 0)),
                    seconds_since_start=float(r.get("seconds_since_start", 0)),
                )
            )
    return rows


if __name__ == "__main__":
    main()
