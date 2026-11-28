# Trane CenTraVac CVHE chiller simulator

The Trane CenTraVac CVHE with Symbio 800 controls, behind the chiller
interface (`hvacsim/models/components/chillers/trane_cvhe.py`). The design
is in [TRANE_CVHE_DESIGN.md](TRANE_CVHE_DESIGN.md).

**Source documents:**
- Trane *Symbio 800 Integration Points List, BACnet, CenTraVac*,
  BAS-PTS009C-EN (12/2025);
- Trane *CenTraVac CVHE/CVHF/CVHG with Symbio Controls IOM*, CVHE-SVX005E-EN
  (04/2026).

Values not in either document are marked *(A)*.

## Choosing it

| Model (CSV `model` column) | Integration |
|---|---|
| `trane_cvhe` | BACnet, the Simplex object list |
| `trane_cvhe_modbus` | Modbus TCP, the same points list's registers: analog values as 32-bit floats (high word first), binary and multi-state as 16-bit, multi-state counted from 1. Needs a `unit_id` column; served on the chilled water plant's Modbus port 5021 |

**Station 1:** **CH-3 is a Trane CVHE** (device 4010033, MAC 33), next to
the two YORK YKs. Vendor settings go under the plant's `chiller:`, e.g.
`chiller: { trane_cvhe: { start_to_start_min: 15 } }`:

| Setting | Default | Meaning |
|---|---|---|
| `start_to_start_min` | 20 *(A)* | restart inhibit, start to start (5-60 min) |
| `stop_to_start_s` | 30 | stop to start (5-200 s, IOM default 30) |
| `evap_pump_off_delay_min` | 1 *(A)* | evaporator pump off delay (0-30 min) |
| `diff_to_start_f` / `diff_to_stop_f` | 2 / 2 *(A)* | cooling differentials (the BAS can write them) |

## Controlling it from the ALC

| Object | Meaning |
|---|---|
| BV-10104 `Chiller Auto Stop Command BAS` | 1 = Auto, 0 = Stop. **Relinquish default is Auto** (the front panel), so a Trane runs unless the BAS commands Stop |
| AV-10100 `Chilled Water Setpoint` | °F; relinquish = front panel 44 °F. `Chiller Setpoint Source` MI-10102 says BAS or Front Panel; `Active Chilled Water Setpoint` AI-10100 shows the one in use |
| AV-10101 `Demand Limit Setpoint` | % RLA, 40-100 |
| AV-10104 / AV-10105 | BAS cooling differential to start / stop |
| BV-10101 `Reset Diagnostic` | writing 1 resets diagnostics whose cause has gone |
| BV-10102 / BV-10103 | evaporator / condenser water pump request overrides |
| MV-10100 `Chiller Mode Command BAS` | Cool (the CVHE here cools only) |

**In Auto the chiller decides when to run:**
- It energizes its **evaporator water pump relay** (BI-10114 `Evaporator
  Water Pump Request`). Flow must be proven within **4 min 15 s**, or
  diagnostic **31001 Evaporator Water Flow Overdue**.
- It starts when leaving water is above setpoint + differential to start,
  once the restart inhibit has expired.
- It stops when leaving water is below setpoint − differential to stop.

**The pumps stay under ALC control on this plant.** Read the chiller's pump
requests (BI-10114 / BI-10116) and run the pumps to suit them.

**Hardwired contacts** (on the I/O device of the chiller's controller, as
BOs):
- `ch3_ext_auto_stop`: External Auto Stop, closed = normal;
- `ch3_estop`: Emergency Stop, closed = normal.

Both are closed unless the ALC opens them.

## Sequence (IOM figures 50-56)

1. Power up 15 s.
2. In Auto: evaporator pump relay, flow proven (6 s filter).
3. Need to cool and restart inhibit expired: condenser pump relay, condenser
   flow proven (else 41001 Overdue).
4. Oil pump: 12 psid within 3 min (else 171008), held 60 s (prelube).
5. IGVs overdriven closed, wye-delta start (about 8 s *(A)*).
6. Running:
   - IGVs modulate;
   - soft load 10 %/min for 15 min *(A)*;
   - limit modes: current (demand) limit, condenser pressure limit,
     evaporator refrigerant temperature limit.
7. Satisfied or Stop:
   - IGVs close (up to 50 s);
   - compressor off;
   - 3 min post-lube;
   - condenser pump relay off.

   After a Stop, the evaporator pump relay drops after its off delay.
8. A start inside the start-to-start time waits. The informational
   diagnostic *Restart Inhibit* is shown, and AI-10200 gives the seconds
   left.

**Running Mode** MI-10100: 1 Off, 2 Start, 3 Run, 4 Pre-Shutdown.

**Refrigerant:** R-514A, so the evaporator gauge pressure is negative (a
vacuum). AI-10124 / 10125 are absolute and AI-10237 / 10234 are gauge.

## Diagnostics

Each diagnostic has its own BI (`Diagnostic: …`), and the summary BIs show
Present / Shutdown / Manual Reset Required. `Unit Source ID (Last Diagnostic
Code)` AI-10118 holds the last code.

| Code | Diagnostic | Type | Cause in the simulator |
|---|---|---|---|
| 31001 / 31002 | Evaporator Water Flow Overdue / Lost | MAR | no chilled water flow / `evap_fs_failed` |
| 41001 / 41002 | Condenser Water Flow Overdue / Lost | MAR | no condenser flow / `cond_fs_failed` |
| 71001 | Emergency Stop | MMR | E-stop contact open |
| 161002 | Low Evap Leaving Water Temp: Unit On | MAR | leaving water < 36 °F *(A)* |
| 171008 / 171001 | Differential Oil Pressure Overdue / Low Differential Oil Pressure | MMR | `low_oil_pressure` |
| 201001 | Low Evaporator Refrigerant Temperature | MMR | `low_charge`, `evap_fouled` (below 32 °F sat *(A)*) |
| — | High Condenser Pressure (BI-11228; code not in the table) | MMR | `cond_fouled` (above 115 °F sat *(A)*) |
| 211005 | Extended Compressor Surge | MMR | `surge` |
| 211081 | Starter Fault Type I | MMR | `starter_fault` |
| 211160 | High Motor Winding Temperature 1 | MMR | `motor_winding_hot` |
| 211091 | EM Momentary Power Loss | MAR | utility outage while running |
| 211186 | Restart Inhibit | info | start held by the restart timers |

MAR diagnostics clear when the cause goes. MMR diagnostics need **Reset**
(display or BV-10101) after the cause has gone.

**Comm loss (`gateway_offline`):** the device goes offline and the chiller
keeps the last values the BAS wrote *(A)*.

## Display (web page)

- **Keys:** Auto, Stop, Reset.
- **Vendor fields:** Running Mode, BAS / panel Auto-Stop, IGV, % RLA,
  evaporator gauge pressure, restart inhibit, pump requests.

## Not simulated

- heat recovery, hot water control, ice building, free cooling, base
  loading;
- purge operation (no points);
- adaptive frequency drive;
- energy meter phase data.
