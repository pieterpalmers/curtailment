"""Simulate the daemon against synthetic CSV inputs.

CSV format: tariff,tariff,grid_power,pv_power
Runs the controller logic and prints decisions without touching hardware.
"""

from __future__ import annotations

import argparse
import csv
import sys
from dataclasses import dataclass


@dataclass
class CsvRow:
    tariff: float
    grid_power: float
    pv_power: float


def main() -> None:
    parser = argparse.ArgumentParser(description="Dry run against CSV data")
    parser.add_argument("csv_file", help="Path to CSV file with synthetic data")
    parser.add_argument("--apsystems-pct", type=int, default=50, help="Starting APsystems %%")
    args = parser.parse_args()

    from curtailment.config import ControlConfig
    from curtailment.controller import ControlInput, compute_decision

    cfg = ControlConfig()
    current_pct = args.apsystems_pct

    rows = _load_csv(args.csv_file)

    print(f"{'tariff':>10} {'grid':>8} {'pv':>8} {'aps_before':>12} {'decision':>15} {'aps_after':>11}")
    print("-" * 70)

    for row in rows:
        inp = ControlInput(
            grid_power=row.grid_power,
            growatt_pv_power=row.pv_power,
            current_apsystems_pct=current_pct,
            growatt_export_limit_watts=200.0,
        )
        decision = compute_decision(inp, cfg)
        current_pct = decision.new_apsystems_pct
        print(
            f"{row.tariff:>10.4f} "
            f"{row.grid_power:>8.0f} "
            f"{row.pv_power:>8.0f} "
            f"{inp.current_apsystems_pct:>12} "
            f"{decision.reason:>15} "
            f"{current_pct:>11}"
        )


def _load_csv(path: str) -> list[CsvRow]:
    rows = []
    with open(path) as f:
        reader = csv.DictReader(f)
        for r in reader:
            rows.append(
                CsvRow(
                    tariff=float(r.get("tariff", 0)),
                    grid_power=float(r.get("grid_power", 0)),
                    pv_power=float(r.get("pv_power", 0)),
                )
            )
    return rows


if __name__ == "__main__":
    main()
