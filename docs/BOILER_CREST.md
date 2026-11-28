# Lochinvar Crest FCB boiler simulator

The Lochinvar Crest FCB condensing boiler with SMART TOUCH control, behind the
boiler interface (`hvacsim/models/components/boilers/lochinvar_crest.py`).
The design is in [CREST_DESIGN.md](CREST_DESIGN.md).

**Source documents:**
- Lochinvar *Crest Installation & Operation Manual*, FCB1000-6000
  (100343224_2000600568 Rev U), section 10;
- Lochinvar *ModBus and BACnet Communication Instructions*
  (100161012_2000020084 Rev X), sections 4, 6, 7, 10, 12 and 13.

Values not in either manual are marked *(A)*.

## Choosing it

| Model (CSV `model` column) | Integration |
|---|---|
| `lochinvar_crest` (default) | BACnet, the object list in section 7 of the communication manual |
| `lochinvar_crest_modbus` | Modbus TCP (section 4 map, served as if behind an RS-485 gateway), with a `unit_id` column; the hot water plant's Modbus server, port 5022 |

**Ratings in `boilers.csv`:** FCB4000N, `input_mbh 3999`, `turndown 20`
(200-3999 MBH).

**Plant settings** (`hw_plant` template):

| Setting | Default | Meaning |
|---|---|---|
| `boiler_bms_mode` | `rate` | BMS type: `rate` (AV2 = firing rate %) or `setpoint` (AV2 = % of the 0-10 V span) |
| `boiler_hw_enable` | `false` | the boilers' hardwired Enable input is made (jumpered) |
| `boiler` | `{}` | display settings by name, e.g. `{bas_timer_s: 60, anti_cycle_s: 120, demand_config: enable_bms}` |

## Controlling it from the ALC (communication manual section 10)

**What the ALC writes:**

| Object | Value | Meaning |
|---|---|---|
| AV0 `Configuration` | **5** (bits 0 and 2) | use the BAS enable (bit 0) and the BAS rate / setpoint command (bit 2) |
| AV1 `Coils` | 1 / 0 | bit 0 = enable |
| AV2 `0-10 Volt Input / Rate Command / Setpoint Command` | 0-100 % | firing rate, or the setpoint as % of the 0-10 V span |

- **Configuration bits:** without bit 0 the boiler ignores AV1, and without
  bit 2 it ignores AV2. The manual says to "re-send the configuration bits
  prior to issuing a command".
- **Setpoint mode:** AV2 % → volts (÷ 10) → temperature, linear from 2 V =
  69.8 °F to 10 V = 179.6 °F. For example, 110 °F = 4.92 V → write 49. Below
  2 V there is no call for heat *(A)*.
- **Other writable objects:** BV0 `Boiler Enable` is the same coil as AV1
  bit 0. AV3-AV7 (tank setpoint and temperature, outdoor, system supply,
  system return, all °C) are accepted. With bit 4 set, AV6 is the control
  temperature.

**BAS timer:**
- The timer is 120 s (Setup > BAS, 5-120 s). Every BAS write restarts it.
- If it lapses, the boiler **reverts to its own controls**: its hardwired
  Enable and its local setpoint (180 °F). With `boiler_hw_enable: false` it
  stops.
- An ALC program that writes only on change will drop the boilers out. Keep
  writing, or set the network output's resend time below the timer.

**Demand configuration** (`demand_config`):

| Value | ENABLE / BMS | Writes needed |
|---|---|---|
| `enable_bms` (default) | ENABLE active, BMS active | AV0 = 5, AV1, AV2 |
| `enable` | ENABLE active, BMS inactive | AV0 = 1, AV1; runs to its local setpoint |
| `bms` | ENABLE inactive, BMS active | AV0 = 4, AV2; enabled by the signal itself |

## What it reports

**Temperatures:** all in **°C**, 0.1 °C resolution. The Modbus registers are
°C × 10.

| Object | Meaning |
|---|---|
| AI8 / AI9 / AI10 | outlet, inlet, flue temperature |
| AI7 / AI3 | outlet setpoint, system setpoint |
| AI11 | firing rate % |
| AI13 `Boiler Status Code` | 11 no demand, 21 SH demand from the hardwired Enable, **22 SH demand from the BMS**, 30 freeze protection, 33 SH pump delay, 2 / 3 / 4 blocked on outlet / flue / ΔT, 10 switched off at the display, **40 lockout** |
| AI14 `Boiler Blocking Code` | 0 none, 8 switched off, 10 high ΔT, 11 high outlet, 12 anti-cycling, 15 high flue |
| AI15 `Boiler Lockout Code` | the last lockout code; it **stays after a reset**. Only status 40 means locked out (manual notice) |
| AI0-AI2, AI25 | the binary inputs packed 16 to a word |
| BI0-BI50 | the FCB binary inputs: MRHL, flow switch, gas pressure, louver proving, flame, LWCO, run time contacts, alarm contacts, boiler pump, louver relay, gas valve, fan, spark … |
| AI35 / AI37 | flame current (µA), corrected O2 |

## Sequence and protection (I&O section 10)

**Sequence:**
1. A call for heat closes the **louver relay**. The boiler blocks until the
   louver proving switch closes.
2. Pre-purge, then trial for ignition. After 3 failed tries: lockout 233
   *(A: tries)*.
3. Flame stabilization at 40 % for 30 s *(A)*, then running at 5-100 %.
4. Demand ends: post-purge 30 s *(A)*, anti-cycling 60 s *(A, adjustable)*,
   boiler pump delay **30 s**.

**Protection:**

| Condition | Action |
|---|---|
| Outlet > 185 °F | reduce firing |
| Outlet > 195 °F | auto-reset high limit (blocking 11) until 10 °F lower and the minimum off time |
| Outlet > 200 °F | auxiliary MRHL dial: lockout 240 |
| Outlet > 205 °F | internal MRHL: lockout 246 |
| Flue > 215 °F | reduce firing |
| Flue > 225 °F | shutdown (blocking 15) until 10 °F lower |
| ΔT > 70 °F | reduce firing |
| ΔT > 80 °F | shutdown (blocking 10) until below 50 °F |
| Inlet < 45 °F | boiler pump on |
| Inlet < 37 °F | fires (status 30) |

**Flow switch** (field supplied, BI1): when it's open the boiler doesn't
fire. The manual gives no code for this. The display shows "Flow switch open"
*(A)*.

**Louver:**
- Not proven at a call for heat: the boiler waits (blocked).
- Proof lost while firing: lockout 134 "Louvers Not Open" *(A: when 134
  applies)*.

## Display (web page)

- **Keys:** **Power** (off = status 10, blocking 8) and **Reset** (clears a
  lockout once its cause has gone).
- **Settings:**
  - local setpoint;
  - BAS timer;
  - anti-cycling time;
  - auto-reset high limit;
  - aux MRHL dial;
  - BMS temperatures at 2 V and 10 V.

## Instructor faults

| Fault | Result |
|---|---|
| `ignition_fail` | lockout 233 Ignition Failure |
| `flame_unstable` | flame dropouts, then lockout 232 Flame Failure 1 |
| `high_limit_trip` | lockout 240 External MRHL |
| `lwco_trip` | lockout 170 Low Water Cut-Off Open |
| `gas_valve` | lockout 244 Large Gas Valve Relay Failure |
| `gas_pressure` | lockout 169 Gas Pressure Switch Open |
| `flue_blocked` | lockout 167 after 2 min firing *(A: code)* |
| `louver_fail` | louver proving open (see above) |
| `supply_sensor_open` / `_short` | lockout 193 / 192 (outlet sensor S1) |
| `return_sensor_open` / `_short` | lockout 180 / 179 (inlet sensor S2) |
| `flow_sw_open` / `flow_sw_stuck` | won't fire / fires without flow (then the high limits act) |
| `hx_scaled` | high flue temperature, lower efficiency; can reach the flue limits |
| `gateway_offline` (`comm_loss`) | the interface is offline; after the BAS timer the boiler is on its own controls |

## Not simulated

- domestic hot water (tank thermostat / tank setpoint are accepted but unused);
- cascade;
- outdoor reset, boost, night setback, ramp delay;
- O2 trim (reported only).
