# Solar Curtailment Daemon

Automatically curtails solar production during negative electricity tariff periods to maximize self-consumption value.

## Concept

When grid injection tariffs turn negative (you pay to export), the daemon:

1. **Enables Growatt export limit** — caps inverter export at ~5% to maintain grid presence
2. **Throttles APsystems microinverters** — reduces production via Modbus power limit
3. **Sets EV to minpv mode** — directs available solar to the EV battery

The control loop regulates grid export to ~-200W (configurable) by adjusting APsystems output up/down in steps.

## States

| State | Description |
|-------|-------------|
| `idle` | Normal operation, tariff >= exit threshold |
| `entering` | Setting up export limit + initial curtailment |
| `regulating` | Active control loop adjusting APsystems % |
| `exiting` | Restoring 100% production, disabling limits |

## Install

```bash
cd /workspaces/curtailment
pip install -e ".[dev]"
```

## Configure

```bash
cp config.example.yaml /etc/curtailment/config.yaml
# Edit to match your installation
```

Key settings in `config.yaml`:
- `growatt.host` / `apsystems.host` — device IPs
- `control.export_setpoint_watts` — target export (negative = exporting)
- `tariff.enter_threshold` / `exit_threshold` — €/kWh triggers

## Run

```bash
# Dry run (no hardware writes)
python -m curtailment --config /etc/curtailment/config.yaml --dry-run

# Production
python -m curtailment --config /etc/curtailment/config.yaml

# One-shot (see what daemon would do)
python -m curtailment --config /etc/curtailment/config.yaml --one-shot
```

## Deploy

```bash
sudo cp deploy/curtailment.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now curtailment
sudo journalctl -u curtailment -f
```

## Troubleshooting

- **APSystems reads 30% (300 permille):** ECU lost state — daemon auto-corrects to 100%
- **Growatt connection fails:** Check host/port, verify Modbus TCP is enabled
- **evcc unreachable:** Daemon logs warning and continues — no control action taken
- **First APsystems call fails:** Normal — warm-up handles this automatically
