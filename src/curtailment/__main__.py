"""Entry point for `python -m curtailment`."""

from __future__ import annotations

import argparse
import logging
import sys


def main() -> None:
    parser = argparse.ArgumentParser(description="Solar Curtailment Daemon")
    parser.add_argument(
        "--config", "-c", required=True, help="Path to config YAML file"
    )
    parser.add_argument(
        "--dry-run", action="store_true", help="Simulate without hardware writes"
    )
    parser.add_argument(
        "--log-level", default=None, help="Override log level (DEBUG, INFO, WARNING, ERROR)"
    )
    parser.add_argument(
        "--one-shot",
        action="store_true",
        help="Run one cycle and exit (for cron-style debugging)",
    )
    args = parser.parse_args()

    # Load config
    from .config import Config, ConfigError

    try:
        cfg = Config.from_file(args.config)
    except ConfigError as e:
        print(f"Config error: {e}", file=sys.stderr)
        sys.exit(1)

    if args.log_level:
        cfg.logging.level = args.log_level

    # Setup logging
    from .util.logging import setup_logging

    setup_logging(cfg.logging.level, cfg.logging.format, cfg.logging.file)

    logger = logging.getLogger("curtailment.main")
    logger.info("Starting solar curtailment daemon (dry_run=%s)", args.dry_run)

    # Validate config
    try:
        cfg.validate()
    except ConfigError as e:
        logger.error("Config validation failed: %s", e)
        sys.exit(1)

    # Create IO clients
    from .io.growatt import GrowattClient
    from .io.apsystems import APSystemsClient
    from .io.evcc import EvccClient

    dry = args.dry_run
    growatt = GrowattClient(
        host=cfg.growatt.host, port=cfg.growatt.port, dry_run=dry
    )
    apsystems = APSystemsClient(
        host=cfg.apsystems.host,
        ecu_port=cfg.apsystems.ecu_port,
        dry_run=dry,
    )
    evcc = EvccClient(
        base_url=cfg.evcc.base_url,
        dry_run=dry,
        injection_tariff_path=cfg.evcc.injection_tariff_path,
        consumption_tariff_path=cfg.evcc.consumption_tariff_path,
        tariff_field=cfg.evcc.tariff_field,
    )

    # Create and run daemon
    from .daemon import Daemon

    daemon = Daemon(cfg, growatt, apsystems, evcc)

    if args.one_shot:
        daemon._connect_all()
        from .state import reconcile_startup

        world = daemon.read_world()
        state = reconcile_startup(world, cfg)
        from .state import decide_action

        action = decide_action(state, world, cfg, 0)
        print(f"State: {state.value}")
        print(f"World: injection_tariff={world.injection_tariff}, consumption_tariff={world.consumption_tariff}")
        print(f"  grid={world.grid_power}W (raw={world.grid_power_raw}W)")
        print(f"  pv={world.growatt_pv_power}W, battery={world.battery_power}W")
        print(f"  apsystems={world.current_apsystems_pct}%, ev_mode={world.current_ev_mode}")
        print(f"Action: {action.reason}, next_state={action.next_state.value}")
        if action.set_apsystems_pct is not None:
            print(f"  set_apsystems_pct={action.set_apsystems_pct}")
        if action.set_growatt_export_limit_pct is not None:
            print(f"  set_growatt_export_limit_pct={action.set_growatt_export_limit_pct}")
        if action.enable_apsystems_power_limit:
            print("  enable_apsystems_power_limit=True")
        if action.disable_growatt_export_limit:
            print("  disable_growatt_export_limit=True")
        if action.set_ev_mode is not None:
            print(f"  set_ev_mode={action.set_ev_mode}")
        daemon._disconnect_all()
    else:
        daemon.run()


if __name__ == "__main__":
    main()
