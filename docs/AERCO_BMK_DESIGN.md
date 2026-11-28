# AERCO Benchmark 4000 with Edge controller: design (for review)

Status: **for review, nothing built.** This is the second boiler behind the
boiler interface ([EQUIPMENT_INTERFACE_DESIGN.md](EQUIPMENT_INTERFACE_DESIGN.md)),
next to the Lochinvar Crest. It is one new file,
`components/boilers/aerco_bmk.py`, registered as `aerco_bmk` (BACnet) and
`aerco_bmk_modbus`. Both must pass the boiler conformance tests.

**Source documents:**
- AERCO *Edge Controller Communications Manual*, OMM-0140:
  - section 2: BAS menus, BAS Comm Timeout, BAS Temp Format;
  - section 4: BACnet objects, stand-alone (tables 4-1, 4-2);
  - section 5: Modbus point list (tables 5-1, 5-2);
  - section 6: fault / status codes (table 6).
- AERCO *Benchmark 750-6000 with Edge [i] Operation-Maintenance Manual*,
  OMM-0145 GF-218:
  - section 2: menus (Communication Failsafe, Temperature Control, Fire
    Control, Interlocks);
  - section 3: start sequence, start/stop levels, BMK4000 air/fuel valve
    table 3-7a;
  - section 6: Constant, Remote Setpoint and Direct Drive modes;
  - the input/output range table.

Values not in either manual are marked *(A)*.

## 1. What a trainee meets that differs from the Crest

| | Lochinvar Crest | AERCO Benchmark (Edge) |
|---|---|---|
| Object types | AI / AV / BI / BV | **AI (read) and AV (read/write) only**; status and faults are AI codes |
| Enable | AV1 Coils bit 0, with AV0 configuration bits | **no enable point.** In Direct Drive the unit starts when the commanded valve position reaches the **Start Level (27 %)** and stops at the **Stop Level (23 %)**. AV80 `Remote Disable Code` (85 = disable, 0 = enable) can disable it |
| Rate command | AV2 % of modulation | AV1 `Net Direct Drive`: **air/fuel valve position %**, not input. The BMK4000 valve table is not linear: 50 % valve = 30 % input |
| Setpoint command | AV2 % of a 2-10 V span | AV0 `Net Remote Setpt` in °F, directly |
| Mode selection | demand configuration (display) | AV12 `SH Operating Mode` (0 Constant, 1 Remote Setpt, 2 Direct Drive …) and AV13 `Remote Signal` (3 = BAS) are writable over BACnet |
| Temperatures | °C | °F (BAS Temp Format; °C selectable) |
| Comm loss | reverts to own controls after the BAS timer | **BAS Comm Timeout**, then the **Unit Failsafe Mode**: Shutdown or Constant Setpoint (60-160 °F). It shows fault 39 / 40 (Direct Drive / Remote Setpt Signal Fault); warning 105 "No BAS Communication" after 5 minutes |
| Codes | status / blocking / lockout | one **Message Code** (AI0, table 6) plus **Unit Status** (AI1: 0 Disabled, 1 Standby, 2 Manual, 3 Remote, 4 Auto, 5 Fault) |
| Reset | display Reset | **CLEAR** on the Edge controller ("Fault, Reset" codes); plain "Fault" codes clear by themselves |
| Interlocks | louver proving input | **Remote Interlock** (flow, auto restart, code 12) and **Delayed Interlock** (louver / valve end switch after the Auxiliary Delay, code 13, needs Clear) |

## 2. Model and ratings

**BMK4000, natural gas** (input range table):
- input 267-4,000 MBH;
- output 232-3,800 MBH.

The CSV row is `input_mbh 4000`, with `turndown` 15 (4000 / 267).

**Air/fuel valve to input** (table 3-7a), interpolated:

| Valve % | 23 | 30 | 40 | 50 | 60 | 70 | 80 | 90 | 100 |
|---|---|---|---|---|---|---|---|---|---|
| Input % | 5.7 | 11.4 | 20.6 | 30.1 | 42.1 | 59.7 | 77.7 | 89.6 | 100 |

The shared boiler core takes the input fraction. The Benchmark is
condensing, with the same efficiency curve as the Crest.

## 3. BAS control

**Modes** (OMM section 6), set by AV12 / AV13 or by the plant's
`boiler_bms_mode`:

| Plant mode | Edge setting | BAS writes |
|---|---|---|
| `rate` (default) | SH Operating Mode = Direct Drive, Remote Signal = BAS | AV1 `Net Direct Drive` = valve % |
| `setpoint` | SH Operating Mode = Remote Setpt, Remote Signal = BAS | AV0 `Net Remote Setpt` = °F (clamped to Setpoint Low / High Limit) |

**Start and stop in Direct Drive** (section 3.3):
- The unit starts when the valve command ≥ **Start Level 27 %**.
- It stops when the command ≤ **Stop Level 23 %**.
- An ALC that writes 20 % to "idle" a boiler turns it off. One that writes
  25 % to a stopped boiler doesn't start it.

**Remote Setpoint:**
- The Edge runs its own PID on the outlet temperature to the setpoint.
- It starts when the PID asks for the Start Level, with an outlet below
  setpoint − Demand Offset *(A: 0)*.
- It stops above setpoint + Shutoff Delay Temp *(A: 10 °F)*.

**`Remote Disable Code`** (AV80):
- 85 disables the unit, 0 enables it. This needs Allow System Disable = Yes
  *(A: Yes on this site)*.
- The generic `enable` role writes 0 / 85.

**BAS Comm Timeout *(question 4)*:**
- If the BAS stops writing for the timeout *(A: default 60 s; the range is
  not given)*, the unit raises fault 39 (Direct Drive) or 40 (Remote Setpt)
  and goes to its **Unit Failsafe Mode**:
  - Shutdown (*default*);
  - or Constant Setpoint at the Unit Failsafe Setpoint (60-160 °F,
    *A: 140 °F*).
- After 5 minutes it also shows warning 105 "No BAS Communication".
- Writes resume: fault 39 / 40 clears by itself ("Fault" type).

## 4. Start sequence (OMM section 3.2)

1. **Pre-purge safety switches checked.** The READY light shows when they're
   made and the Enable switch is on.
2. **Demand.** The **Auxiliary Delay** runs, then the **Delayed Interlock**
   must be closed (louver end switch). Auxiliary Delay is 0-240 s, *A:
   30 s*.
3. **Purge.**
   - The blower runs and the air/fuel valve goes to 100 %.
   - The Blower Proof and Blocked Inlet switches close.
   - Purge Timer 5-60 s, *A: 7 s*.
4. **Ignition.**
   - The valve goes to the ignition position (BMK4000 **45 %**).
   - **Spark cleaning 7 s** (manual default).
   - The SSOV opens (proof of closure); up to **4 s** for flame.
5. **Flame proven** after 2 s.
6. **Low Fire Timer** *(A: 2 s, range 2-600)*.
7. **Modulation** to the command, at most the Max Valve Position (100 %).
8. **Stop.**
   - Post Purge Timer, *A: 0 s, range 0-60*.
   - Then the On Delay before it may restart, *A: 30 s, range 0-600*.
   - Pump Off Delay, *A: 2 min*.

**Message codes in sequence:** 2 Standby, 4 Purging, 70 Cleaning Igniter,
69 Pre Ignition, 5 Ignition Trial, 6 Flame Proven, 7 Warmup (2 minutes,
first start only), 49 / 51 ignition retry, 1 Disabled.

## 5. Protection and faults (table 6)

| Condition | Code | Type |
|---|---|---|
| Outlet ≥ Temperature High Limit (AV20, 40-210 °F, *A: 200 °F*) | 44 High Water Temp | Fault (auto) |
| Over-temp alarm switch, 210 °F default (OMM) | 8 High Water Temp Switch Open | Fault, Reset |
| Remote Interlock open (flow switch) | 12 Interlock Open | Fault (auto) |
| Delayed Interlock not closed after the Auxiliary Delay (louver) | 13 Delayed Interlock Open | Fault, Reset |
| No flame in 4 s, after 1 retry *(A)* | 22 Flame Loss During Ignition | Fault, Reset |
| Flame lost running | 23 Flame Loss During Run | Fault, Reset |
| Low / high gas pressure switch | 10 / 11 | Fault, Reset |
| Low water level | 9 Low Water Level | Fault, Reset |
| SSOV not proving at ignition | 20 SSOV Fault During Ignition | Fault, Reset |
| Exhaust high (warning, then fault) | 54 / 55 | Warning / Fault, Reset |
| Outlet sensor out of range | 42 Outlet Temp Sensor Fault | Fault (auto) |
| Inlet sensor short / open | 56 / 57 | Warning (keeps running) |
| BAS signal lost | 39 / 40 | Fault (auto), then failsafe |
| Power loss | 25 Loss of Power | Fault (auto), shown after power returns |
| Too many cycles in 24 h | 71 | Fault *(A: 200 cycles)* |

**Instructor faults** (the generic names map as follows):

| Fault | Code |
|---|---|
| `ignition_fail` | 22 |
| `flame_unstable` | 23 |
| `high_limit_trip` | 8 |
| `lwco_trip` | 9 |
| `flow_sw_open` | 12 (Remote Interlock = flow) |
| `flow_sw_stuck` | fires without flow, then 44 / 8 |
| `flue_blocked` | Blocked Inlet / exhaust: 55 Exhaust Temp High *(A: via exhaust temperature)* |
| `gas_valve` | 20 |
| `hx_scaled` | exhaust rises: 54, then 55 |
| `supply_sensor_open` / `_short` | 42 |
| `return_sensor_open` / `_short` | 57 / 56 (warnings) |
| `gateway_offline` (`comm_loss`) | the BAS timeout path |
| `gas_pressure` | 10 |
| `louver_fail` | 13 |

## 6. Integration list (communication manual sections 4 and 5)

**BACnet (`aerco_bmk`):**
- **AI** (read only): 0 Message Code, 1 Unit Status, 2 Outlet, 3 Lower
  Inlet, 4 Air Inlet, 5 Outside Temp, 6 Exhaust, 7 Feed Forward, 8 Current
  Valve Position, 9 O2, 11 Run Cycles, 13 Run Hours, 15 Flame Strength, 16
  Active Setpoint Temp, 17 PID Output, 18 SET Valve Position, 19
  Communication Address, 64 Manifold Gas Pressure, 147 Software Version, 151
  Fuel Type, 152 Unit Mode.
- **AV** (read/write): 0 Net Remote Setpt, 1 Net Direct Drive, 4 SH
  Setpoint, 12 SH Operating Mode, 13 Remote Signal, 18 / 19 Setpoint Low /
  High Limit, 20 Temperature High Limit, 21 Max Valve Position, 22 Pump Off
  Delay, 23 Auxiliary Delay, 24 Unit Failsafe Mode, 25 Low Fire Timer, 44
  Purge Timer, 45 Post Purge Timer, 46 Ignition Position, 47 / 48 Stop /
  Start Valve Position, 49 On Delay, 80 Remote Disable Code.
- **Names, instances and ranges** are the manual's (the trailing colons are
  kept, e.g. `Outlet:`).

**Writable settings:**
- Some AVs are display settings: limits, timers, modes. The BAS writing them
  changes the boiler's behaviour, as on the real Edge.
- Out-of-range writes are refused (BACnet `value-out-of-range`).

**Modbus (`aerco_bmk_modbus`):**
- Input registers 30001 + AI number; holding registers 40001 + AV number
  (table 5-1 / 5-2).
- Run Cycles and Run Hours are 32-bit (two registers).
- The hot water plant's Modbus server (port 5022) serves it.

## 7. Plant, page, demo, tests

**Station 1 *(question 1)*:**
- *Default:* **B-3 becomes an AERCO BMK4000**, so the plant is mixed (B-1 /
  B-2 Crest, B-3 AERCO), as was done for CH-3.
- Its device stays 4010053 / MAC 53. Rows can be switched back in the CSV.

**Plant wiring:**
- The plant's flow and louver end switch map to the AERCO's Remote Interlock
  (flow) and Delayed Interlock (louver).
- The ALC louver BO stays as it is.

**Display keys:** **Enable / Disable** (the Edge's switch) and **Clear**.

**Demo logic (roles):**
- Rate mode writes the valve position. Because the valve-to-input curve isn't
  linear, it writes 30 % rather than 27 % as the lowest firing command.
- It writes Remote Disable Code 0 / 85 for enable, and refreshes inside the
  timeout.
- New: an encoding hook maps the plant's "firing %" to the valve position,
  using the inverse of table 3-7a. The demo's PI then holds for any vendor.

**Tests:**
- boiler conformance;
- AERCO tests:
  - start / stop levels;
  - valve-to-input table;
  - Remote Setpoint PID;
  - Remote Disable Code;
  - BAS timeout to Shutdown and to Constant Setpoint;
  - auxiliary delay / delayed interlock fault 13;
  - interlock 12 auto restart;
  - CLEAR rules;
  - message codes through the sequence;
  - object instances against tables 4-1 / 4-2;
  - Modbus 32-bit run hours.

**Golden traces:**
- `hw_plant` and `boiler_vendor` use the default layout (all Crest) and must
  not change.
- All other traces must not change either.

**Docs:**
- new `docs/BOILER_AERCO_BMK.md`;
- README vendor table;
- `HW_PLANT_DESIGN.md` pointer.

## 8. Questions for review (proposed defaults)

1. **Where it's used.** *Default:* B-3 on station 1 becomes `aerco_bmk`
   (mixed plant); the template default stays Crest.
2. **Size.** *Default:* BMK4000 (4,000 MBH, closest to the Crest's 3,999).
3. **BAS mode.** *Default:* Direct Drive (`rate`) with start / stop levels
   27 / 23 %; Remote Setpoint available as `setpoint`.
4. **Comm loss.** *Default (A):*
   - BAS Comm Timeout 60 s;
   - Unit Failsafe Mode = Shutdown;
   - Constant Setpoint (140 °F) selectable.
5. **Temperature format.** *Default:* °F (selectable °C).
6. **Timers not in the manuals.** *Default (A):*
   - purge 7 s;
   - low fire 2 s;
   - post purge 0 s;
   - On Delay 30 s;
   - Auxiliary Delay 30 s;
   - Pump Off Delay 2 min;
   - Temperature High Limit 200 °F;
   - 1 ignition retry.
7. **Writable settings over BACnet.** *Default:* yes, as listed in section 6
   (range-checked).
8. **Remote Disable Code as the enable role.** *Default:* yes, with Allow
   System Disable = Yes.
