# Trane CenTraVac CVHE with Symbio 800: design (for review)

Status: **for review, nothing built.** This is the second chiller behind the
chiller interface ([EQUIPMENT_INTERFACE_DESIGN.md](EQUIPMENT_INTERFACE_DESIGN.md)),
next to the YORK YK. It is one new file, `components/chillers/trane_cvhe.py`,
registered as `trane_cvhe`. It must pass the chiller conformance tests.

**Source documents:**
- Trane *Symbio 800 Integration Points List, BACnet, CenTraVac*,
  BAS-PTS009C-EN (12/2025):
  - object naming conventions;
  - Simplex tables: AI, AV, BI, BV, MI, MV and the Binary Input Diagnostics;
  - Simplex diagnostic codes;
  - the Modbus register for every object.
- Trane *CenTraVac CVHE, CVHF, CVHG with Symbio Controls, IOM*,
  CVHE-SVX005E-EN (04/2026):
  - operating principles;
  - start-up and shutdown, sequence of operation figures 50-56;
  - field control wiring, table 22;
  - CVHF operating limits, table 1.

Values not in either document are marked *(A)*.

## 1. What a trainee meets that differs from the York

| | YORK YK (OptiView) | Trane CVHE (Symbio 800) |
|---|---|---|
| Run command | `REM RUN STOP CMD` BV1 | **`Chiller Auto Stop Command BAS` BV-10104** (0 = Stop, 1 = Auto). Auto lets the chiller start when it needs to cool |
| Setpoint | `REM LEAVING CH LIQ TEMP SP` AV1 | `Chilled Water Setpoint` AV-10100 (BAS arbitration); `Active Chilled Water Setpoint` AI-10100 shows what is used |
| Limit | `REM MOTOR CURRENT SP` (% FLA) | `Demand Limit Setpoint` AV-10101 (% RLA); `Active Demand Limit Setpoint` AI-10209 |
| Start decision | starts when commanded, if flow and anti-recycle allow | **Differential to start / stop**: in Auto it starts only if leaving water is above setpoint + differential to start, and stops when it is below setpoint − differential to stop (`BAS Cooling Differential to Start/Stop` AV-10104 / 10105) |
| Pumps | the plant's own (ALC) | The chiller has **evaporator and condenser water pump relays**: `Evaporator Water Pump Request` BI-10114, `Condenser Water Pump Request` BI-10116, plus BAS overrides BV-10102 / 10103 |
| Flow | flow switches, cycling shutdown | **Flow must be proven within 4 min 15 s** of the pump relay (6 s filter), or diagnostic `Evaporator / Condenser Water Flow Overdue` |
| Capacity | VSD speed, then pre-rotation vanes | **Inlet guide vanes** (`Inlet Guide Vane 1 Percent Open` AI-10134), constant speed, wye-delta starter *(question 2)* |
| Status | operation code MSV (160 states) | `Running Mode` MI-10100 (Off / Start / Run / Pre-Shutdown / Service), `Operating Mode` MI-10101, `Chiller Setpoint Source` MI-10102 |
| Faults | safety / cycling / warning code MSVs | **one binary object per diagnostic**: `Diagnostic Present`, `Diagnostic Shutdown Present`, `Diagnostic: Manual Reset Required` (BI-10118-10125), and BI-11xxx per diagnostic; `Unit Source ID (Last Diagnostic Code)` AI-10118 gives the code (e.g. 31002) |
| Reset | panel | `Reset Diagnostic` BV-10101 from the BAS, or the display |
| Refrigerant | R-134a, positive pressure | **R-514A low pressure**: the evaporator runs in a vacuum; a purge removes air |

## 2. Model and ratings

**CVHE, 1200 tons, R-514A, inlet guide vanes, wye-delta starter, constant
speed** *(question 2: or with an adaptive frequency drive)*.
- The same plant CSV ratings as the York: capacity, flows, design leaving
  temperature, kW/ton.
- The shared chiller core supplies tons, kW and approaches.
- The Trane file sets two things the core takes as vendor settings:
  - **IGV part-load kW:** constant speed with IGVs gives a flatter part-load
    curve than a VSD. A new core setting, `part_load_curve: "igv"`, raises kW
    below 60 % load by up to 15 % *(A, typical published IPLV difference)*.
    The York keeps the current curve, so its golden traces are unchanged.
  - **Refrigerant property** for the pressures it reports: R-514A saturation
    pressure, using a fitted Antoine curve *(A: R-514A fit to published
    saturation data, ±1 %)*.

## 3. Sequence (IOM figures 50-56)

**Power up:**
- Controller boot 15 s.
- Power-up start delay 0-30 min *(default 0, A)*.
- The last chiller mode (Auto / Stop) is remembered.

**Auto, waiting to start:**
1. The **evaporator water pump relay** energizes. Flow must be proven within
   4 min 15 s (6 s filter), or diagnostic **31001 Evaporator Water Flow
   Overdue** (auto reset).
2. Start when leaving water > setpoint + differential to start, the
   **start-to-start timer** (5-60 min, default 20 min *(A)*) and the
   **stop-to-start timer** (5-200 s, default 30 s) have expired, and oil
   temperature is above sat. evap + 30 °F and 100 °F.
3. The **condenser water pump relay** energizes. Flow must be proven within
   4 min 15 s, or **41001 Condenser Water Flow Overdue**.
4. The **oil pump** energizes. Oil pressure must reach 12 psid within 3 min
   (else 171008 Differential Oil Pressure Overdue), then hold 12 psid for a
   60 s **prelube**.
5. The IGVs are overdriven closed. The motor starts wye and transitions to
   delta when current falls below 85 % RLA for 1.5 s *(A: about 8 s after
   start)*.

**Running:**
- The IGVs modulate to leaving water temperature.
- Soft load at start *(A: 10 % / min pulldown limit)*.
- Limit modes (`Running – Limit`): current (demand) limit, condenser
  pressure limit and evaporator refrigerant temperature limit unload the
  chiller before any diagnostic trips *(A: the limit thresholds)*.

**Satisfied setpoint** (leaving water < setpoint − differential to stop):
- IGVs close (up to 50 s);
- compressor off;
- 3 min post-lube;
- condenser pump relay off;
- the evaporator pump relay stays on in Auto.

**Stop** (BAS Stop, display Stop, External Auto Stop open, or a latching
diagnostic):
- Same as satisfied setpoint.
- The evaporator pump relay then opens after the **evaporator pump off delay**
  (0-30 min, default 1 min *(A)*).
- The status is Stopped or Run Inhibit.

**Fast restart:** a stop that is removed during post-lube restarts straight
away, through condenser flow proving. The restart inhibit is enforced.

**Default differentials:** to start 2 °F, to stop 2 °F *(A, not in these
documents)*. The BAS can write them (AV-10104 / 10105).

## 4. Pumps: who commands them *(question 3)*

On a real CenTraVac job, the chiller's pump relays often start the pumps. In
this plant, the **ALC controls the pumps** (they're on drives with ALC
start/stop).

*Default:*
- Keep the ALC in control of the pumps.
- The chiller's relays are read-only points (BI-10114 / BI-10116) the ALC can
  use.
- If the ALC doesn't establish flow within 4 min 15 s of the chiller's
  request, the chiller raises Flow Overdue.
- The trainee has to sequence the pumps with the chiller's requests, which is
  real CenTraVac integration work.

## 5. Diagnostics

**Every diagnostic has a type** (IOM):
- **MMR**: latching, manual reset;
- **MAR**: non-latching, auto reset;
- **informational**: warning only.

**Reset:** BV-10101 `Reset Diagnostic` (write 1) or the display Reset key.
Manual-reset diagnostics block a start until reset (IOM page 86).

**BACnet:**
- each diagnostic sets its own BI;
- `Diagnostic Present` / `Shutdown Present` / `Manual Reset Required` /
  `Critical` / `Service Required` summarize them;
- AI-10118 holds the last diagnostic's code.

**Instructor faults → Symbio diagnostics:**

| Fault (existing generic name) | Diagnostic (code, BI) | Type |
|---|---|---|
| `evap_fs_failed` | 31002 Evaporator Water Flow Lost (BI-11210); 31001 Overdue on a start (BI-11211) | MAR |
| `cond_fs_failed` | 41002 Condenser Water Flow Lost (BI-11195); 41001 Overdue (BI-11196) | MAR |
| `low_charge` | Evaporator Refrigerant Temperature limit, then 201001 Low Evaporator Refrigerant Temperature (BI-11262) | MMR |
| `evap_fouled` | Evaporator limit; 201001 if severe | MMR |
| `cond_fouled` | Condenser pressure limit, then High Condenser Pressure (BI-11228, code not in the table *(A)*) | MMR |
| `starter_fault` | 211081 Starter Fault Type I (BI-11314) | MMR |
| `comm_loss` | the BACnet interface is offline; the chiller keeps its last BAS values *(A, section 6)* | — |
| **new** `surge` | 211005 Extended Compressor Surge (BI-11213) | MMR |
| **new** `low_oil_pressure` | 171001 Low Differential Oil Pressure (BI-11259) | MMR |
| **new** `motor_winding_hot` | 211160 High Motor Winding Temperature 1 (BI-11235) | MMR |
| **new** `emergency_stop` | 71001 Emergency Stop (BI-11202), the hardwired E-stop input open | MMR |

**From the sequence, without injection:**
- utility outage: 211091 EM Momentary Power Loss (BI-11271), MAR;
- low leaving water: 161002 Low Evap Leaving Water Temp: Unit On;
- a reset while the motor is still hot: 211186 Restart Inhibit
  (informational).

## 6. BAS arbitration and comm loss

**Arbitration (points list, naming conventions):**
- "BAS" and "Setpoint" AV/BV/MV objects feed the arbitration logic.
- "Active" / "Status" AI objects show the value in use.
- The source is chosen in the order BAS, then External (hardwired), then
  Front Panel. `Chiller Setpoint Source` MI-10102 reports which one.
- The simulator keeps a priority array on the writable objects. A write of
  NULL (relinquish) hands control back to the front panel setpoint.

**Comm loss *(question 5)*:**
- The points list sends the BAS timeout rules to BAS-SVP083 (not provided).
- *Default (A):* the chiller keeps the last BAS-written values, as a BACnet
  priority array does.
- The ALC sees the device go offline.
- It is the opposite of the Crest, which reverts to local control after its
  timer. Comparing the two is a good training point.

## 7. Hardwired points (IOM table 22)

These stay with the plant, as the York's do:
- chilled and condenser **flow proving inputs** (from the plant's flow
  physics);
- **External Auto Stop** input: closed for normal, read back on BI-10128;
- **Emergency Stop** input: closed for normal, BI-10101.

The plant gets two new binary inputs per Trane chiller, both closed by
default: `chN_ext_auto_stop` and `chN_estop`. They can be wired from an ALC
BO, or left made *(question 4)*.

The relay outputs appear as BACnet points:
- alarm MAR / MMR;
- compressor running;
- maximum capacity;
- head relief request.

## 8. Integration list

**Default: BACnet** (BACnet/IP on the virtual network; on site, MS/TP or
BACnet/IP through the Symbio).

| Object types | Objects |
|---|---|
| Writable | AV-10100, AV-10101, AV-10104, AV-10105; BV-10101-10104; MV-10100 (Chiller Mode Command BAS: Cool only, other modes refused *(A)*) |
| AI | 10100, 10101, 10106-10110, 10118, 10124-10126, 10130-10134, 10145, 10146, 10154, 10155, 10158, 10159, 10163, 10200, 10201, 10206, 10207, 10209, 10228 |
| BI | 10101, 10104-10108, 10113-10128 |
| BI diagnostics | the subset in section 5 |
| MI | 10100-10105 |

**Object identifiers and names:**
- Object types and instances are the manual's (AI-10107 = analog-input
  10107).
- Object names are the manual's, e.g. `Evaporator Leaving Water Temperature`.
- Configuration-dependent points the CVHE doesn't have (heat recovery, ice
  building, free cooling, CVHS lube) are left out.

**Units:**
- *Default (A):* IP units (°F, psi, tons, kW). The Symbio's BACnet units
  follow its display unit setting.

**Modbus *(question 6)*:**
- The same list is on Modbus.
- Analog registers are 32-bit floats, high word first; binary and
  multi-state are 16-bit.
- *Default:* also register `trane_cvhe_modbus`, served by the chilled water
  plant's Modbus server. This uses the generic Modbus encoder with a float32
  encoding added.

## 9. Plant, page, demo, tests

**Station 1, *question 1*:**
- *Default:* **CH-3 becomes a Trane CVHE**, so the plant is mixed: CH-1 and
  CH-2 YORK, CH-3 Trane. This is common on real sites and shows the
  interface working.
- Its BACnet device stays 4010033 / MAC 33.

**Web page:**
- Display keys: **Auto**, **Stop**, **Reset**.
- Trane vendor fields: Running Mode, IGV %, % RLA, restart inhibit time
  remaining, purge state, active diagnostics with type.

**Demo logic (roles):**
- Writes Auto / Stop (BV-10104), the chilled water setpoint and the demand
  limit.
- Starts the pumps when the chiller's pump request BIs ask, which the York
  doesn't have. That's a new optional read role, `pump_request`.

**Tests:**
- The chiller conformance tests: the conformance flow-loss and outage cases
  allow the Trane's timers.
- Trane tests:
  - differential to start / stop;
  - Flow Overdue at 4 min 15 s;
  - prelube and 12 psid oil;
  - post-lube and fast restart;
  - start-to-start and stop-to-start timers;
  - MMR vs MAR reset;
  - Reset Diagnostic over BACnet;
  - setpoint arbitration and relinquish;
  - the integration list against the manual's instances.

**Golden traces:**
- York, boiler and generator traces must not change.
- `chw_plant` and `chiller_vendor` use the default layout (all York) and must
  not change either.
- The station-1 change (CH-3 Trane) only affects the host config.

**Docs:**
- `docs/CHILLER_TRANE_CVHE.md` (user doc);
- README vendor table;
- `CHILLER_PLANT_DESIGN.md` pointer.

## 10. Questions for review (proposed defaults)

1. **Where it's used.** *Default:* CH-3 on station 1 becomes `trane_cvhe`,
   making a mixed plant; the template default stays York.
2. **Drive.** *Default:* constant speed with IGVs and a wye-delta starter
   (classic CVHE), with an IGV part-load curve. Alternative: AFD option.
3. **Pumps.** *Default:* the ALC keeps control of the pumps; the chiller's
   pump requests are readable, and Flow Overdue enforces the 4 min 15 s.
4. **Hardwired External Auto Stop / E-stop.** *Default:* two plant inputs per
   Trane chiller, closed (made) unless the ALC opens them.
5. **BAS comm loss.** *Default (A):* last written values held; no timeout.
6. **Modbus.** *Default:* also register a Modbus variant (32-bit floats).
7. **Timers not in the documents.** *Default (A):*
   - start-to-start 20 min;
   - evaporator pump off delay 1 min;
   - differentials to start / stop 2 °F;
   - soft load 10 %/min.
8. **New faults.** *Default:* surge, low oil pressure, motor winding hot,
   emergency stop.

**Build order with the Crest design** ([CREST_DESIGN.md](CREST_DESIGN.md),
also awaiting review): the Crest first, then the Trane. They share one new
piece of work, the extra Modbus encodings (coils and discrete inputs for the
Crest, float32 for the Trane).
