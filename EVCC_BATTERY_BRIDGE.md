# evcc battery mode bridge (legacy registers to Growatt VPP)

## Goal

Extend the curtailment daemon so evcc's battery control works on the Growatt SPH4000TL while VPP is enabled.

evcc uses its `growatt-hybrid` meter template with `usage: battery`. That template controls the battery by writing the legacy "battery first" and "AC charge" holding registers. With VPP enabled, the inverter ignores those registers. The daemon already controls the inverter through VPP for curtailment, so it takes on one more job: **read the legacy registers evcc writes, and translate them into VPP remote power control commands.**

evcc itself is not changed.

## Topology

```
evcc ──(own modbus connection)──────────────┐
                                            ├── one shared TCP connection ── Growatt 10.0.0.37:502
daemon ──► evcc modbus proxy (host:port) ───┘
```

- The daemon reaches the inverter through **evcc's modbus proxy** (server/modbus/proxy.go in evcc). The proxy and evcc's battery meter share one physical connection, and evcc serializes all requests on it. So there is exactly one Modbus master on the inverter. Direct parallel connections to 10.0.0.37 caused "Invalid data" and timeout errors in testing.
- The proxy forwards requests without caching, so reads return live inverter values.
- The proxy must run with `readonly: false`. `deny` rejects writes, and `true` silently drops them while reporting success.
- Use the daemon's **existing** Modbus client (the one used for curtailment) for the bridge as well. Do not open a second client. Serialize all bridge and curtailment access through it.
- Consequence: when evcc is down, the proxy is down too, and the daemon cannot write anything. The VPP timer (below) is the fail-safe for that case.

## What evcc writes (legacy, holding registers, 0-based)

| evcc battery mode | 1102 (battery first slot 1 enable) | 1092 (AC charge enable) |
|---|---|---|
| normal | 0 | 0 |
| hold | 1 | 0 |
| charge | 1 | 1 |

- evcc writes 1102 first, then 1092, as two separate FC16 writes of one register each.
- evcc writes **only when the mode changes**. It never refreshes them.
- The values persist in the inverter, including after evcc stops or crashes.

## VPP registers (holding, 0-based)

| reg | meaning |
|---|---|
| 30100 | Control authority, must be 1 (one-time setup, EEPROM). The daemon checks it on startup and logs an error if it is not 1. It does not write it. |
| 30407 | Remote power control enable (0/1) |
| 30408 | Remote power duration, minutes (0 = unlimited, never use 0) |
| 30409 | Remote power rate, INT16 % [-100,100], + = charge, - = discharge |
| 30410 | AC charge enable (0/1), EEPROM-backed |

Curtailment uses 30200 to 30202. These registers are independent of 30407 to 30409. Both features can be active at the same time.

## Verified inverter behaviour (manual tests, 2026-10-25)

- Rate **0%** with 30407=1 has **no effect**: the battery still discharges (about 3 kW under a 7 kW car load) and charges from PV. **Do not use 0% for hold.**
- Rate **+1%** with 30407=1 makes the battery **idle**: 0 W, no discharge under load, and no PV charging at 25% SoC. PV surplus gets curtailed by the export limit instead. This is accepted as "hold".
- The charge-from-grid sequence (30407=0, 30408, 30409=+N, 30407=1, with 30410=1) works. It was verified earlier with `growatt_export_limit.py --charge`.
- **Unverified:** what happens when the 30408 timer expires. In one test, 30407 still read 1 about 1 minute after a 2-minute timer elapsed, but at 0% rate that could not show whether the inverter reverted. See Open points.

## Mapping

| state | VPP writes (in order) |
|---|---|
| normal | 30407=0 |
| hold | 30408=T, 30409=+1, 30407=1 |
| charge | 30410=1, 30408=T, 30409=+charge_rate, 30407=1 |

- `T` = `vpp_timer_min`, default 2.
- `charge_rate` = config, default 100, valid range 1 to 100.
- Leave 30410 at 1 after charge. Do not toggle it per mode change: it is EEPROM-backed, and frequent writes wear it.
  - Before writing, read 30410 and write it only if it is not already 1.
- Do **not** write 30407=0 before a mode change from hold to charge or back. Write the new 30408/30409 values, then 30407=1. This avoids a brief window where the battery is uncontrolled.
  - Exception: if the inverter turns out to require the "disable first" order that `growatt_export_limit.py` uses, add it, and note it in a comment.

## Daemon logic

### Poll loop

The poll loop runs every `poll_interval_s`, default 5.

1. Read 1102 and 1092 (FC03). Derive the `seen` state from the table above.
2. If evcc is not alive (see Liveness), set `seen = normal`.
3. Debounce: act only when the same `seen` value has appeared on **two consecutive polls**. This filters out the intermediate state between evcc's two writes, for example normal → (1102=1, 1092=0) → charge, which would otherwise briefly look like hold.
4. If the stable state differs from `active`: apply its VPP writes, set `active` to it, record the refresh time, and log the transition at INFO.
5. If `active` is hold or charge, and `refresh_interval_s` (default 30) has passed since the last refresh: re-arm the timer by writing 30408=T.
   - If testing shows that writing 30408 alone does not restart the countdown, re-send the full hold/charge sequence instead, still without 30407=0 first.
6. On a Modbus error: log it, keep `active` unchanged, retry on the next poll. Never treat a failed read as a state change. The VPP timer covers long outages.

### Startup

- Set `active = None` so the first stable state always gets applied. This re-syncs after a daemon restart.
- Check 30100 == 1.

### Shutdown

- On SIGTERM or SIGINT, write 30407=0 (best effort), then exit.

### Liveness

- Make an HTTP GET to `evcc_url + "/api/state"`, default `http://<evcc host>:7070/api/state`. It needs no auth.
  - Use a timeout of about 5 s.
  - Count any 2xx response as alive. Don't parse the body: it is large, and a status check is enough.
- Check at most every 30 s, and cache the result in between.
- Declare evcc dead after `evcc_dead_after_s` (default 120) without a successful response. Then force normal, as in step 2 of the poll loop.
- In practice, if evcc is dead the proxy is usually dead too, so the write also fails. Liveness matters mainly when evcc is hung or restarting while the proxy still answers. Keep it anyway, because it's cheap.

### Fail-safe hierarchy

1. evcc sets normal on a clean shutdown (believed, **not verified**, see Open points).
2. The daemon's liveness check forces normal if evcc stops answering but the proxy still works.
3. The daemon's SIGTERM handler writes 30407=0.
4. The **inverter's VPP timer** (30408 = T minutes) reverts the inverter if nothing re-arms it. This is the only fail-safe when the proxy is gone. That makes it critical, and it is unverified (see Open points).

## Configuration (new keys)

Add these keys to the existing daemon config (see `config.example.yaml`). Match the existing style.

```yaml
evcc_bridge:
  enabled: true
  evcc_url: http://127.0.0.1:7070
  poll_interval_s: 5
  refresh_interval_s: 30
  vpp_timer_min: 2
  charge_rate_pct: 100     # 1..100, % for 30409 in charge mode
  hold_rate_pct: 1         # must be >= 1, 0 does not block discharge
  evcc_dead_after_s: 120
```

- Validate on startup: `charge_rate_pct` must be in 1 to 100, `hold_rate_pct` in 1 to 100, and `vpp_timer_min` in 1 to 10.
  - `refresh_interval_s` must be below `vpp_timer_min * 60 / 2`, so a single missed refresh does not let the timer expire.

## evcc side (for reference, not the daemon's job)

- The battery meter uses `template: growatt-hybrid`, `usage: battery`, pointed at 10.0.0.37:502.
- The one-time 1100/1101 slot-time setup from that template's docs is not needed, because the daemon ignores the slot times.
- The modbus proxy config points at the same inverter URI, with `readonly: false`.

## Tests

### Unit tests (no hardware)

Mock the Modbus client.

- State mapping: covers all 4 combinations of 1102/1092. The unused one, 1102=0 with 1092=1, maps to normal.
- Debounce: a single intermediate reading does not trigger a transition. Two identical readings in a row do.
- Transitions write the exact register sequences from the Mapping table.
- 30410 is written only when it is not already 1.
- Refresh fires only in hold or charge, and only after `refresh_interval_s`.
- A Modbus read error leaves `active` unchanged and writes nothing.
- Liveness: after `evcc_dead_after_s` of failed checks, the daemon goes to normal.
- SIGTERM writes 30407=0.
- Config validation rejects `hold_rate_pct: 0`.

### Hardware tests

Run these against the proxy, not directly against 10.0.0.37.

1. Put evcc in charge mode, for example by setting a battery grid charge limit above the current price. Within about 10 s, 30407=1, 30409=charge_rate, and the battery charges from the grid.
2. Put evcc in hold mode while the car charges. Battery about 0 W, grid covers the load.
3. Switch evcc back to normal. 30407=0, and the battery resumes normal behaviour.
4. Stop evcc cleanly while in charge mode (`systemctl stop evcc`). Read 1102 and 1092 afterwards. Does evcc reset them to normal on shutdown?
5. Stop the daemon with `kill -9` while in charge mode, with evcc still running. **After T minutes:** does the battery stop charging from the grid? Does 30407 drop to 0? This settles the open timer question.
6. Run for 1 h with curtailment active. No Modbus errors beyond occasional retries. Curtailment and battery control don't interfere.

## Open points (resolve during hardware testing, then update this doc)

- [ ] Does the inverter revert when the 30408 timer expires (hardware test 5)? If not, the fail-safe design must change. One option would be an external watchdog on the inverter connection. Report back before shipping.
- [ ] Does writing 30408 alone restart the countdown, or is the full sequence needed?
- [ ] Is the "30407=0 first" write order from `growatt_export_limit.py` actually required?
- [ ] Does evcc reset the legacy registers to normal on a clean shutdown (hardware test 4)?
- [ ] What does 100% charge rate mean in watts? Measure once and note it here.
- [ ] Hold at +1% blocks PV charging. Accepted for now. Revisit if PV yield during hold matters.
