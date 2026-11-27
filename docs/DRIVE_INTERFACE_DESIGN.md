# Drive interface and the Yaskawa Z1000: design (for review)

Status: **design for review, not built yet.** This is step 1 of the
modularization plan: one interface for every variable frequency drive in the
simulator, with the ABB ACH580 (today's drive) and the Yaskawa Z1000 as its
first two implementations.

**Source documents:**
- Yaskawa *Z1000 Programming Manual* SIEP YAIZ1U 03C:
  - chapter 1: parameter details;
  - appendix B.7: BACnet objects supported.
- Yaskawa *Z1000 User Manual* TOEP C710616 45H:
  - section 3.9: control circuit terminals;
  - section 5.3: faults and alarms.
- The ACH580 firmware manual already used for `components/vfd.py`.

Values not taken from these documents are marked *(A)*.

## 1. Goal

- **Swap drives by configuration.** Any drive in the simulator can change make
  and model with one setting, and no model code changes. This covers the
  standalone drive, the AHU fan array, the plant pumps and the tower fans.
- **ALC points don't change.** Swapping a drive leaves the trainees' ALC points
  as they are (`pchwp1_ss`, `pchwp1_vfd_spd`, `pchwp1_amps`, `pchwp1_vfd_fb`,
  `pchwp1_vfd_flt`, …). These belong to the equipment's wiring, not to the drive.
- **What does change with a swap is what a technician would see:**
  - the drive's terminals, parameter numbers, defaults and keypad behaviour;
  - fault and alarm codes;
  - its BACnet object list, when it is on fieldbus.
- **Adding a third drive** (Danfoss FC 102, Siemens G120P, …) is one new file
  plus its templates, and must pass the same conformance tests.

## 2. How it is used (configuration)

| Where | Setting | Default |
|---|---|---|
| Standalone drive (`vfd_fan`) | template `templates/vfd_z1000_hardwired.yaml` or `vfd_z1000_bacnet.yaml` (new), or `equipment.params.drive_model: z1000` | `ach580` |
| AHU fan array | `equipment.params.fan_drive_model: z1000` | `ach580` |
| Plant pumps / tower fans | CSV column `drive_model` on each row (`primary_pumps.csv`, `secondary_pumps.csv`, `condenser_pumps.csv`, `cooling_towers.csv`) | blank = `ach580` |

**Drive parameter overrides:**
- They are written with the drive's own parameter names, under the drive
  settings already in the templates (`pchwp_drive: {...}`, …).
- An override that doesn't belong to the selected drive is a configuration
  error that names the drive and the parameter.

A few common settings are set in drive-neutral terms by the equipment, and
each drive translates them into its own parameters (section 5).

## 3. The drive interface

Every drive is a class in `hvacsim/models/components/drives/` registered by
name (`ach580`, `z1000`). Equipment models only use this surface.

### Identity and metadata

| Item | Meaning |
|---|---|
| `MODEL`, `MAKE`, `DISPLAY` | `z1000`, `Yaskawa`, `Yaskawa Z1000` |
| `DEFAULTS`, `PARAM_INFO` | the drive's own parameters, numbers, ranges and choices, as in its manual |
| `TERMINALS` | names of its digital inputs, analog inputs, relay outputs and analog outputs |
| `KEYPAD` | keypad keys the web page shows (Hand / Off / Auto / Reset, reference entry) |
| `FIELDBUS_INPUTS`, `FIELDBUS_SIGNALS` | values its fieldbus object list reads and writes |
| `INJECTABLE` | the instructor faults it supports |

**Instructor faults:** generic names (`overcurrent_trip`, `earth_leakage`,
`input_phase_loss`, `mains_undervoltage`, `cooling_fan_failed`) mapped to the
drive's own codes, plus any drive-specific faults.

### Inputs (set by the equipment each step)

| Item | Meaning |
|---|---|
| `terminals.di[name]` | digital input states, by the drive's terminal names (`di1`… / `S1`…) |
| `terminals.ai[name]` | analog input signals, % of span (0-10 V or 4-20 mA as the drive is set) |
| `fieldbus` | fieldbus inputs (run, reference, fault reset, idle time), by the drive's own names |
| `powered` | mains present (ATS / utility) |

### Behaviour

| Item | Meaning |
|---|---|
| `step(h, load_torque)` | one integration step against the mechanical load (unchanged from today) |
| `keypad(key, value)` | web page keypad |

### Outputs (generic)

These are what equipment models and plant physics read:
- `speed_pu`, `freq`, `current`, `power_kw`, `torque_pu`;
- `running`, `fault_active`, `warning_active`;
- `fault_code` (the drive's own text, e.g. `2310` or `oC`), `warnings`;
- `mode` (hand / off / auto);
- `relay(terminal)`, `analog_out(terminal)` (% of span);
- `status()`: a dict for the web page (replaces today's `|`-joined status string).

### Wiring roles

Equipment models stop naming terminals. They wire **roles**, and each drive
maps the roles to its terminals:

| Role | Equipment meaning | ACH580 terminal | Z1000 terminal |
|---|---|---|---|
| `start` | BAS start/stop (ALC BO) | DI1 (20.01 In1 Start) | S1 (H1-01 = 40 Forward run, 2-wire) |
| `safeties` | safety chain (AHU freezestat / smoke / high static; pumps: closed) | DI4 (20.41 Start interlock 1) | S3 (H1-03 = B1 Customer Safeties) |
| `damper_interlock` | damper end switch, when the equipment has one | — (not wired) | S4 (H1-04 = B2 BAS Interlock) |
| `speed_ref` | ALC AO 0-10 V | AI1 (12.17-12.20) | A1 (H3-01 = 0: 0-10 V) |
| `running_out` | status to ALC BI | RO2 (10.27 Running) | M1-M2 (H2-01 = 0 During Run) |
| `fault_out` | fault to ALC BI, **closed = healthy** | RO3 (10.30 Fault (-1)) | MB-MC (fault relay N.C. contact, opens on fault) |
| `speed_fb` | speed feedback to ALC AI, % of 10 V | AO1 (13.12 Output frequency) | FM (H4-01 = 102 output frequency) |
| `damper_out` | damper open command, when used | RO1 (10.24 Damper control) | M5-M6 (H2-03 = B2 BAS Interlock Relay Contact) |

The equipment's ALC points keep their names and meanings. Only the drive
behind them changes.

## 4. Quality safeguards

**Golden traces, before any refactor.** Record today's ACH580 behaviour over
fixed scenarios:
- standalone drive: hardwired and fieldbus start / stop / comm loss / keypad;
- AHU fan array: start, high-static trip and safety chain;
- CHW pump start and decel;
- utility outage and level restart.

The refactored ACH580 must reproduce them (same signals within tight
tolerances), and all 143 existing tests must still pass.

**Conformance tests.** Every registered drive must pass the same checks:
- starts from the `start` role and follows `speed_ref` with its ramp;
- stops by its stop method;
- `fault_out` opens on a fault and closes after reset;
- `speed_fb` follows speed;
- current and power are sensible for the load;
- no power: dark (relays dropped), then a level restart per its own parameters;
- every generic fault trips with a code from its table;
- keypad Hand / Off / Auto works;
- its fieldbus template loads, and every object maps to a real input or signal.

A new drive isn't accepted until it passes all of these.

## 5. Common settings (equipment → drive)

Equipment states these in drive-neutral terms. Each drive maps them:

| Common setting | ACH580 | Z1000 |
|---|---|---|
| motor rated current / power / voltage | 99.06 / 99.10 / 99.07 | E2-01 / E2-11 / E1-05 |
| max frequency | 30.14 | E1-04 |
| min frequency (Hz) | 30.13 | d2-02 (% of E1-04) |
| accel / decel time | 28.72 / 28.73 (0 → 46.02 Hz) | C1-01 / C1-02 (0 → E1-04) |
| reference at 10 V | 12.20 AI1 scaled max | H3-03 A1 gain (% of E1-04) |
| stop mode | 21.03 | b1-03 |

## 6. Yaskawa Z1000 model

### Terminals (User Manual 3.9)

| Group | Terminals |
|---|---|
| Digital inputs | S1-S7 |
| Analog inputs | A1, A2 |
| Fault relay | MA (N.O.) / MB (N.C.) / MC (common) |
| Relay outputs | M1-M2, M3-M4, M5-M6 |
| Analog outputs | FM, AM |
| Serial | R+ R- S+ S- for BACnet MS/TP |

**Simulator default layout: HVAC Initialization (A1-03 = 3410).** Programming
manual 1.1 lists these terminal settings for it:
- S1 Forward run (H1-01 = 40)
- S2 Reverse run (41)
- S3 Customer Safeties (H1-03 = B1)
- S4 BAS Interlock (H1-04 = B2)
- S5 Emergency Override Forward Run (H1-05 = AF)
- S6 Multi-step speed 2 (4)
- S7 Jog (6)
- M1-M2 During run (H2-01 = 0)
- M3-M4 Zero speed (H2-02 = 1)
- M5-M6 BAS Interlock Relay Contact (H2-03 = B2)
- FM output frequency (H4-01 = 102)
- AM output current (H4-04 = 103)
- A1 0-10 V (H3-01 = 0)
- A2 4-20 mA (H3-09 = 2)

### Run and reference sources (AUTO)

| Parameter | Setting |
|---|---|
| b1-01 Frequency reference (AUTO) | 0 HOA keypad, **1 terminals (default)**, 2 serial (BACnet), 3 option card |
| b1-02 Run command (AUTO) | **1 terminals (default)**, 2 serial (BACnet), 3 option card |
| b1-03 Stopping method | 0 ramp, **1 coast (default)** |
| C1-01 / C1-02 Accel / decel | **30.0 s** |
| d2-01 / d2-02 Reference upper / lower limit | 100 % / 0 % |
| E1-04 Max output frequency | 60 Hz |
| E2-01 Motor rated current | from the motor |

### HOA keypad (HAND / OFF / AUTO)

| Parameter | Setting |
|---|---|
| S5-01 HAND reference | **0 keypad (default)**, 1 terminals, 3 S5-05 |
| S5-02 HAND/AUTO during run | enabled (default) |
| S5-04 HAND mode behaviour | 1 Normal: **powers up in AUTO** |
| S5-05 HAND frequency reference | 0.00 Hz |

### Interlocks

- **B1 Customer Safeties** (S3) open:
  - the drive coasts (stop method forced to coast);
  - alarm `SAFE`;
  - it restarts when closed and the run command is still on.
- **B2 BAS Interlock** (S4) open with a run command:
  - the output is baseblocked;
  - alarm `inTLK`;
  - M5-M6 (H2-03 = B2) closes on the run command to open the damper, and the
    damper end switch closes S4. This is the classic Z1000 damper interlock,
    and trainees can see it work.
- **6A Drive Enabled / 70 Drive Enable 2:** alarm `dnE` when open, if a
  template assigns it.
- **External fault (H1 = 20-2F):** fault `EFn` with the terminal number.

### Emergency override

- AF on S5 runs the drive forward at S6-01 Emergency Override Speed
  (S6-02 = 0: use S6-01).
- It ignores BAS interlock and run commands.

### Fault reset

- Done from the HOA RESET key or an input set to 14.
- The manual says resets are **ignored while a run command is present**, and
  the simulator follows that.

### Power loss

| Parameter | Setting |
|---|---|
| L2-01 Momentary power loss | 2: recover as long as the CPU has power (default). Short dips ride through with speed search; a real outage drops the control |
| b1-17 Run command at power up | 1 allowed (table default). With a level run command present, the drive starts when power returns (manual note: some units ship with 0) *(A)* |
| L5-01 Auto restart attempts | 0 |

### Serial communication (BACnet)

| Parameter | Setting |
|---|---|
| H5-04 Stopping method after CE | **3 alarm only (default)**, 0 ramp, 1 coast, 2 fast stop, 4 run at d1-04 |
| H5-05 CE detection | enabled |
| H5-09 CE detection time | **2.0 s** |

The ALC must keep writing, or `CE` appears. The default response is an alarm,
not a fault. This differs from the ACH580's 58.14 default (fault), which is a
useful training contrast.

### Faults (User Manual table 5.5)

| Code | Fault | Trigger in the simulator |
|---|---|---|
| oC | Overcurrent | injected; locked rotor |
| GF | Ground fault | injected |
| ov | Overvoltage | fast decel of a high-inertia fan |
| Uv1 | DC bus undervoltage | injected; outage when L2-01 = 0 |
| PF | Input phase loss | injected |
| LF | Output phase loss | injected |
| oH1 | Heatsink overheat | cooling fan failed + high load |
| oL1 | Motor overload | sustained overload |
| oL2 | Drive overload | sustained overload |
| FAn | Internal fan fault | injected |
| EF1-EF7 | External fault | terminal set to 20-2F |
| CE | BACnet / Modbus comm error | only when H5-04 ≠ 3 |

### Alarms (User Manual table 5.6)

| Code | Alarm |
|---|---|
| oH | Heatsink pre-alarm |
| CE | Comm error (H5-04 = 3) |
| inTLK | Interlock open |
| SAFE | Customer safety open |
| dnE | Drive disabled |
| Uv | Undervoltage (riding through) |
| bb | Baseblock |
| EF | Run command input error |
| ov | Overvoltage |

**Not modelled:** the Z1000 PI controllers (b5, S3), sequence timers (S2),
bypass (Z1000 Bypass is a separate product), multi-step speeds, auto-tuning,
and Modbus RTU / N2 / P1.

### BACnet object list (Programming Manual B.7, tables B.5-B.10)

Simulated as a BACnet/IP device on the virtual network, like the other
fieldbus drives (on a real site it is BACnet MS/TP on R+/R-/S+/S-).

**Commandable (C) / writable (W):**

| Object | Name | Notes |
|---|---|---|
| BV1 | RUN FWD | run command when b1-02 = 2 |
| BV4 | FAULT RESET | |
| BV3 | EXT FAULT | |
| BV5 / BV6 | COM NET / COM CNTRL | shown; the run and reference source follow b1-01 / b1-02 *(A)* |
| BV7-BV11 | MF Input 3-7 Cmd | virtual S3-S7 |
| AV2 | Frequency Command | **Hz** |
| AV27 / AV28 | Accel / Decel Time | s |
| AO1 / AO2 | Analog Output 1 / 2 Level | when the monitor is set to through mode |
| BO1-BO3 | MF outputs | M1-M2, M3-M4, MA-MC, when set to through mode |

**Read only:**

| Object | Name | Units |
|---|---|---|
| AV9 | Frequency Reference | Hz |
| AV10 | Output Frequency | Hz |
| AV11 | Output Voltage | V |
| AV12 | Output Current | A |
| AV13 | Output Power | **W** |
| AV14 | Torque Reference | % |
| AV18 | DC Bus Voltage | V |
| AI1 / AI2 | Analog Input 1 / 2 Level | % |
| AI9 | Rated Current (n9-01) | A |
| BI1-BI7 | Input Terminal 1-7 | |
| BI8 / BI9 | Multi-Function Out 1 / 2 | |
| BV13 | RUN-STOP | |
| BV14 | REV-FWD | |
| BV15 | READY | |
| BV16 | FAULT | |
| BV33 | Operating | |
| BV35 | Frequency Agree | |
| BV41 | Base Block | |
| BV42 / BV43 | Frequency Reference / Run Command Mode | COM / LOCAL |

**Fault and status bits (read only):**

| Object | Name |
|---|---|
| BV18 | Overcurrent – Ground Fault |
| BV19 | Main Circuit Overvoltage |
| BV20 | Drive Overload |
| BV21 | Drive Overheat |
| BV24 | External Fault |
| BV26 | Mtr Ovrld-OvrTorque |
| BV28 | Main CKT Undervoltage |
| BV30 | Output Phase Loss |
| BV31 | Communication Error |
| BV45 | Frequency Refer Lost |
| BV57 | Drive Comm Error |

AV29 / AV30 / BV55 / BV56 (parameter access by number) are a later option.

## 7. What the trainees get

- **New templates:** `vfd_z1000_hardwired.yaml` and `vfd_z1000_bacnet.yaml`,
  and an optional new instance on station 1 (SF-3-VFD, Yaskawa Z1000,
  hardwired) to compare with SF-2-VFD side by side.
- **Web page:** a Z1000 keypad with HAND / OFF / AUTO / RESET, its parameter
  groups (b, C, d, E, H, L, S) with Yaskawa numbering, and its fault and alarm
  codes.
- **Pumps and fans:** any pump or fan in the plants can be switched to Z1000
  with the CSV column.

## 8. Build steps

1. Record the golden traces (current code, no changes).
2. Create the interface and move the ACH580 into it unchanged. Switch every
   user (`vfd_fan`, `ahu`, `chw_plant`, `hw_plant`) to roles and the factory.
   Golden traces and all tests must still pass.
3. Add the Z1000, its templates and the conformance tests. Every drive passes.
4. Generic web keypad driven by the drive's metadata. Docs:
   `docs/VFD_Z1000.md`, plus updates to README and `VFD_ACH580.md`.

## 9. Open items / assumptions

1. **b1-17 default:** the parameter table says 1 (allowed), but a note says
   the drive ships with 0. The simulator uses 1 (level start after an outage,
   matching the ACH580) and it is settable.
2. **BV5 / BV6 COM NET / COM CNTRL:** these are shown only. The run and
   reference sources follow b1-01 / b1-02, as the parameter chapter describes.
3. **Manual voltage classes:** the programming manual cover says 600 V class;
   the parameters and BACnet objects match the 200 / 400 V user manual. The
   simulator uses 480 V motors.
4. **Fieldbus transport:** BACnet MS/TP is simulated as BACnet/IP on the
   virtual network, as for the York and Crest gateways.
