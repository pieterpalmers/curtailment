# Solar Curtailment Daemon — Specification

## 1. Purpose

A Python daemon that curtails PV production during periods of negative grid injection tariffs. It coordinates two PV sources (a Growatt SPH4000 hybrid inverter and an APsystems DS3 microinverter array) and signals an evcc instance to absorb surplus into the EV charger when possible.

The economic goal: avoid paying to export PV during negative-tariff windows, while continuing to extract value through self-consumption (loads + battery storage) wherever possible.

## 2. System under control

**Generation:**
- **Growatt SPH4000** — 4 kW hybrid inverter with attached battery storage. Has an internal export-limit controller that regulates against a grid CT clamp. Responds within seconds to setpoint changes, but **mode changes** (enabling/disabling the export limit) take up to 2 minutes.
- **APsystems DS3 array** — 7 microinverters, 6 kW total. Controlled via Modbus through the ECU. Power-limit changes take roughly 60 seconds to take effect. No grid awareness — purely open-loop from the daemon's perspective.

**Loads & coordination:**
- **evcc** — orchestrates the EV charger and other dispatchable loads. Also manages the Growatt battery charge/discharge strategy. Exposes a REST API including loadpoint mode control.
- **EV charger** — 1-phase, 1.4 kW minimum charging power.

**Important constraint:** the daemon does not control individual APsystems microinverters; it controls the entire array as a unit. The daemon does not directly control loads or the battery — that's evcc's responsibility.

## 3. Control philosophy

The daemon is the **slow outer loop** of a cascaded control system. The Growatt's internal export-limit regulator is the **fast inner loop** that handles transients (cloud cover, load changes). The daemon's only job is to keep the APsystems array operating in a range where the Growatt's inner loop has the headroom to do its work.

The two loops have well-separated timescales (seconds vs. minutes), so they don't fight each other.

Regulation is performed on the grid power measurement directly — not on a computed production target. The feedback loop handles whatever loads and battery activity exist without needing to know about them.

## 4. Tariff regimes

evcc exposes both the injection tariff (`feedin`) and the consumption tariff (`grid`) explicitly, so the daemon reads them as independent signals rather than deriving one from the other. The consumption tariff is typically higher than the injection tariff (local taxes and distribution costs), but the daemon does not assume any fixed offset.

| Regime | Condition | Daemon action | EV mode |
|---|---|---|---|
| Normal | injection ≥ 0 | No curtailment. APsystems 100%, Growatt export limit off. | Not touched |
| Mild negative | injection < 0 and consumption ≥ 0 | Curtail generation to target ~200 W export. | `minpv` (Min+Solar) |
| Deep negative | consumption < 0 | (v2 — deferred) | (v2) |

**v1 implements only Normal and Mild negative.** Deep negative regime — defined as the situation where the consumption price itself is negative, so importing energy is also profitable — is deferred. However for now this should be considered identical to Mild negative.

**Hysteresis on tariff transitions:** enter curtailment when injection tariff < 0 €/kWh; exit when injection tariff ≥ 0.005 €/kWh. Minimum dwell time in curtailment: 10 minutes. This prevents oscillation around the zero crossing.

## 5. Control law

### 5.1 What is regulated

- **Setpoint:** `grid_power = -200 W` (200 W flowing out to grid; the 200 W slack covers measurement noise and provides a positive signal so evcc doesn't shut things off).
- **Process variable:** smoothed `grid_power` (median of 6 samples taken at 5 s intervals across a 30 s control period).
- **Manipulated variable:** APsystems power-limit percentage (integer, 0–100).
- **Secondary observation:** `growatt_pv_power`, used to detect when the Growatt is "pinned" (self-curtailing to zero because APsystems is producing too much).

### 5.2 Decision logic per cycle

```
error = grid_power - setpoint   # positive = under-exporting / importing

if error > deadband:
    # under-exporting → raise APsystems
    new_pct = clamp(current_pct + step_up, 0, 100)
    reason = "under_export"
elif error < -deadband:
    # over-exporting → lower APsystems
    new_pct = clamp(current_pct + step_down, 0, 100)
    reason = "over_export"
else:
    # within deadband
    if growatt_pv_power < pinned_threshold:
        # Growatt is self-curtailing because APsystems is too high
        new_pct = clamp(current_pct + step_down, 0, 100)
        reason = "pinned_low"
    else:
        new_pct = current_pct
        reason = "hold"
```

### 5.3 Asymmetric step sizes

Over-exporting during negative tariff costs money on every kWh; under-exporting is just sub-optimal. Therefore: step down faster than step up.

| Parameter | Starting value |
|---|---|
| `step_up` | +5 percentage points |
| `step_down` | -10 percentage points |
| `deadband` | ±100 W |
| `pinned_threshold` | 100 W |
| `setpoint` | -200 W |

These are tunable in config and may need adjustment based on observed behavior.

### 5.4 Growatt export limit setting

The Growatt accepts a percentage-of-max-power as its export limit. For a 4 kW inverter to target 200 W export, this is approximately **5%**. The exact value needs empirical calibration once running against real hardware.

## 6. State machine

```
IDLE ──tariff drops──> ENTERING_CURTAILMENT ──after 120s wait──> REGULATING
  ▲                                                                   │
  │                                                                   │
  └──── EXITING_CURTAILMENT <───────────tariff rises──────────────────┘
```

### 6.1 States

**IDLE**
- Tariff ≥ exit threshold.
- APsystems at 100%, Growatt export limit disabled.
- EV mode untouched.
- Per cycle: read tariff. If tariff drops below enter threshold with hysteresis confirmed → ENTERING_CURTAILMENT.

**ENTERING_CURTAILMENT**
- One-shot setup:
  1. Set Growatt export limit (e.g., 5% → ~200 W).
  2. Set APsystems to initial conservative % (50%).
  3. Set EV loadpoint mode to `minpv`.
- Wait 120 s for Growatt mode change to complete.
- Transition → REGULATING.

**REGULATING**
- Per cycle: sample world state (smoothed grid_power, growatt_pv_power), run controller, write new APsystems % if changed.
- Check tariff exit condition each cycle; if exit confirmed (and min dwell elapsed) → EXITING_CURTAILMENT.

**EXITING_CURTAILMENT**
- One-shot teardown:
  1. Set APsystems to 100%.
  2. Disable Growatt export limit.
  3. If EV loadpoint mode is currently `minpv`, restore to configured default (e.g., `pv`). If user has manually changed it, leave alone.
- Transition → IDLE.

### 6.2 Startup reconciliation

The daemon must determine its state from external system state alone — no persistent state files. On startup:

```
read tariff, growatt_export_limit_active

if tariff < enter_threshold:
    if growatt_export_limit_active:
        → REGULATING (resume)
    else:
        → ENTERING_CURTAILMENT
else:
    if growatt_export_limit_active:
        → EXITING_CURTAILMENT (cleanup)
    else:
        → IDLE
```

This gives crash-restart recovery for free.

## 7. evcc coordination

### 7.1 Why Min+Solar

evcc's normal `pv` mode requires sustained measurable surplus on the grid before starting the EV. Since the daemon curtails to ~200 W export, that signal never appears. `minpv` (Min+Solar) mode sidesteps this: the EV charges continuously at minimum power (1.4 kW), and ramps up if surplus appears. The EV becomes a known baseline load that absorbs PV that would otherwise be curtailed.

### 7.2 When to use it

In v1: switch EV to `minpv` on entering curtailment, restore on exiting. No mid-curtailment mode switching based on PV availability — that's deferred.

This means during mild-negative tariff with low PV, the EV may draw from grid/battery at minimum power. Given the 0.15 €/kWh consumption-vs-injection spread, this is roughly break-even (you'd have charged the EV at some point anyway). Acceptable for v1; refine later if real data shows it's an issue.

### 7.3 Mode restoration is conditional

On exit, only restore to the default mode if the current mode is still `minpv`. If the user manually changed it during curtailment, respect that choice. This keeps the daemon idempotent and respectful of user intent.

### 7.4 What the daemon does NOT do

- Does not start, stop, or modulate loads directly.
- Does not control the battery charging/discharging strategy.
- Does not coordinate multiple loadpoints.
- Does not know about non-EV dispatchable loads.

All load and battery logic stays in evcc.

## 8. Idempotence

**Hard requirement:** the daemon can be killed and restarted at any time without changing system behavior.

Achieved by:
- No persistent state files. Daemon's internal state is reconstructed from external readings at startup.
- All write operations are idempotent (writing the current value to a setpoint is a no-op).
- The "are we curtailing?" question is answered by reading the Growatt's export-limit register, not from daemon memory.
- The "should we restore EV mode?" question is answered by reading current EV mode and only restoring if it's `minpv`.

The only ephemeral state held in memory: recent samples for smoothing (rebuilds in 30 s), and time-in-current-state for the entering-curtailment wait timer (worst case: another 2-minute wait on restart, which is acceptable).

## 9. IO interfaces

### 9.1 Growatt client

Wraps `growatt_export_limit.py` (verified working reference script). Operations needed:

- `read_grid_power() -> float` — register 31112, watts. Positive = import, negative = export.
- `read_pv_power() -> float` — register 31058, watts.
- `read_battery_power() -> float` — register TBD. **Logged only in v1, not used in control logic.**
- `read_export_limit_status() -> {enabled, limit_pct}` — uses the script's `--status` operation.
- `set_export_limit(pct: float)` — uses the script's `--limit PCT` operation.
- `disable_export_limit()` — uses the script's `--disable` operation.

### 9.2 APsystems client

Wraps `write_MO.py` (verified working reference script). Operations needed:

- `warm_up()` — dummy read to work around the "first call fails" quirk.
- `set_power_limit(pct: int)` — port 502, unit 1, register `power_limit`.
- `enable_power_limit()` / `disable_power_limit()` — port 1503, register `power_limit_ena`.

**Note:** the reference notes show two IPs (10.0.0.150 for ECU, 10.0.0.32 for inverter). Verify the configuration for your setup.

### 9.3 evcc client

Wraps the evcc REST API.

- `get_injection_tariff() -> float` — €/kWh, can be negative.
- `get_loadpoint_mode(id: int) -> str` — returns `off`, `now`, `minpv`, `pv`.
- `set_loadpoint_mode(id: int, mode: str)` — `POST /api/loadpoints/{id}/mode/{mode}` (see https://docs.evcc.io/integrations/rest-api/operations/setloadpointmode/).

### 9.4 Dry-run mode

Every IO client supports a `dry_run: bool` constructor flag. When true, reads return last-set values (or sensible defaults), writes log the intent without executing. Essential for safe testing.

## 10. Smoothing & sampling

To avoid jittery decisions driven by short noise spikes:

- 30 s control period.
- Within each period: take 6 samples of `grid_power` and `growatt_pv_power` at 5 s intervals.
- Use the **median** (not mean) — more robust to outliers.
- No cross-period EMA: each control decision uses fresh samples only.

## 11. Error handling

**Transient IO errors** (Modbus timeout, evcc 5xx): log, skip this cycle, retry next cycle.

**Persistent IO errors** (3+ consecutive failures on same source): log loudly, continue running. Do **not** exit curtailment because evcc is unreachable — leave the system in its current state. Next successful read resumes regulation.

**Config errors at startup:** fail fast.

**Shutdown (SIGTERM/SIGINT):** graceful exit. No emergency cleanup — the system is idempotent and recoverable from any state.

## 12. Configuration

Single YAML file. Key sections:

- `growatt`: host, port
- `apsystems`: host, ecu_port, inv_port (and inv_host if different)
- `evcc`: base_url, ev_loadpoint_id, default_ev_mode
- `control`: period_seconds, sample_count, sample_interval_seconds, export_setpoint_watts, deadband_watts, step_up_pct, step_down_pct, growatt_pinned_threshold_watts, growatt_export_limit_pct, start_apsystems_pct, growatt_mode_change_wait_seconds
- `tariff`: enter_threshold, exit_threshold, deep_negative_threshold (unused in v1), min_dwell_seconds
- `logging`: level, format, file

Validated on load: required fields, sensible ranges, types.

## 13. Observability

Structured JSON logs, one INFO line per control cycle with consistent fields:
- `timestamp`, `state`, `tariff`, `grid_power` (raw + smoothed), `growatt_pv_power`, `battery_power` (logged but not used), `apsystems_pct`, `decision_reason`.

DEBUG logs include full world state and intermediate calculations.

State transitions logged at INFO with the reason.

Prometheus metrics endpoint and MQTT integration deferred to later iterations.

## 14. Deferred for v2+

Items consciously left out of v1:

- **Deep negative tariff regime** (`now` mode for EV when import is profitable).
- **Mid-curtailment EV mode switching** based on real-time PV availability.
- **Battery-power-aware control law** (faster ramp on under-export when battery discharging; inhibit step-down when battery charging). Battery power is logged but not consumed in v1 decisions.
- **Multi-loadpoint coordination** if more dispatchable loads are added.
- **Manual override / emergency stop** interfaces beyond systemctl stop.
- **Battery SoC threshold logic** for load enablement (evcc's job anyway).
- **Adaptive parameter tuning** based on observed behavior.

## 15. Open calibration items

These need empirical adjustment after initial deployment:

1. **`growatt_export_limit_pct`** — starts at 5%, adjust to achieve ~200 W actual export.
2. **`deadband_watts`** — may need widening if grid measurement is noisier than expected.
3. **`step_up_pct` / `step_down_pct`** — tune based on observed convergence speed and overshoot.
4. **`start_apsystems_pct`** — may benefit from being remembered across sessions (in-memory only, lost on restart) once basic operation is verified.
5. **Battery power register** — identify the correct Modbus register and verify sign convention.

## 16. Success criteria

The daemon is successful when:

- During positive-tariff periods, it does nothing (verified by no log activity beyond cycle heartbeats).
- During negative-tariff periods, grid export stays close to 200 W (±200 W say, over 5-minute windows).
- The EV charges at minimum power (or higher if surplus is available) whenever tariff is mildly negative.
- The daemon survives restarts at any point in any state without misbehavior.
- Over a week of operation, manual intervention is not required.