"""Read current world state and print what the daemon would do. Safe to run anytime."""

from __future__ import annotations

import argparse
import sys


def main() -> None:
    parser = argparse.ArgumentParser(description="One-shot: read state and decide")
    parser.add_argument("--config", "-c", required=True, help="Path to config YAML")
    parser.add_argument("--dry-run", action="store_true", help="Simulate writes")
    args = parser.parse_args()

    from curtailment.config import Config, ConfigError
    from curtailment.daemon import Daemon

    try:
        cfg = Config.from_file(args.config)
        cfg.validate()
    except ConfigError as e:
        print(f"Config error: {e}", file=sys.stderr)
        sys.exit(1)

    from curtailment.io.growatt import GrowattClient
    from curtailment.io.apsystems import APSystemsClient
    from curtailment.io.evcc import EvccClient

    dry = args.dry_run
    growatt = GrowattClient(cfg.growatt.host, cfg.growatt.port, dry_run=dry)
    apsystems = APSystemsClient(cfg.apsystems.host, cfg.apsystems.ecu_port, dry_run=dry)
    evcc = EvccClient(
        cfg.evcc.base_url, dry_run=dry,
        injection_tariff_path=cfg.evcc.injection_tariff_path,
        consumption_tariff_path=cfg.evcc.consumption_tariff_path,
        tariff_field=cfg.evcc.tariff_field,
    )

    daemon = Daemon(cfg, growatt, apsystems, evcc)

    try:
        daemon._connect_all()
        world = daemon.read_world()
        from curtailment.state import reconcile_startup, decide_action

        state = reconcile_startup(world, cfg)
        action = decide_action(state, world, cfg, 0)

        print("World state:")
        print(f"  injection_tariff:      {world.injection_tariff:.4f} €/kWh")
        print(f"  consumption_tariff:    {world.consumption_tariff:.4f} €/kWh")
        print(f"  grid_power:            {world.grid_power:.0f} W (raw: {world.grid_power_raw:.0f} W)")
        print(f"  pv_power:              {world.growatt_pv_power:.0f} W")
        print(f"  battery_power:         {world.battery_power:.0f} W")
        print(f"  apsystems:             {world.current_apsystems_pct}%")
        print(f"  ev_mode:               {world.current_ev_mode}")
        print(f"  growatt_limit_active:  {world.growatt_export_limit_active}")
        print(f"")
        print(f"Reconciled state: {state.value}")
        print(f"Action: {action.reason}")
        print(f"  next_state:  {action.next_state.value}")
        if action.set_apsystems_pct is not None:
            print(f"  set_apsystems_pct: {action.set_apsystems_pct}%")
        if action.set_growatt_export_limit_pct is not None:
            print(f"  set_growatt_export_limit_pct: {action.set_growatt_export_limit_pct}%")
        if action.enable_apsystems_power_limit:
            print(f"  enable_apsystems_power_limit: True")
        if action.disable_growatt_export_limit:
            print(f"  disable_growatt_export_limit: True")
        if action.set_ev_mode is not None:
            print(f"  set_ev_mode: {action.set_ev_mode}")
    finally:
        daemon._disconnect_all()


if __name__ == "__main__":
    main()
