# AHU simulator: physical design (draft for review)

Source documents: `vav_ahu_econ` sequence of operation, points list and
flow diagram (ALC equipment builder), with the changes agreed in review.
**No simulator code has been written against this yet.** Mark up anything
that's wrong.

## 1. Air path

```
                                   ┌─ barometric dmpr ─ SF-1 ─┐
OA ─[OA dmpr NC]─┬─ PF ─ PH ─ MAT ─ FZ ─┤─ barometric dmpr ─ SF-2 ─├─ CHW ─ HW ─ HUM ─ SA RH ─ SAT ─ SA SMK ─ HI-SP ─ duct ··· SP sensor (¾ down) ─ VAV boxes ─ zones
                 │                      ├─ barometric dmpr ─ SF-3 ─┤
            [RA dmpr NO]                └─ barometric dmpr ─ SF-4 ─┘
                 │
EA ─[EA dmpr NC]─┴──────────── RF ─ RA SMK ─ CO2 ─ RA RH ─ RAT ─ RA ───────────────────────── zones (return)
```

| Tag | Component | Notes from the drawing |
|---|---|---|
| OA / RA / EA dmpr | Mixing box, one 2-10 V signal to all three actuators | OA and EA fail **closed**, RA fails **open** (spring return) |
| PF | Prefilter | Analog DP transmitter |
| PH | Preheat HW coil, **2-way valve, fails open** | Upstream of MAT |
| MAT | Averaging mixed air sensor | **Reads preheat leaving air**, not true mixed air |
| FZ | Freezestat, averaging capillary | **Manual reset** |
| SF-1…4 | Supply fan array, 4 direct-drive plenum fans, each on its own ACH580-style VFD | **Barometric backdraft damper on each fan** |
| CHW | Cooling coil, **2-way valve, fails closed** | Blow-through: fan heat is added before the coil |
| HW | Heating coil, **2-way valve, fails closed** | Reheat position, downstream of CHW |
| HUM | Steam humidifier: enable BO + 2-10 V valve, **valve fails closed** | Plus a **hardwired high-limit humidistat** (see section 3) |
| HL-RH | Supply air high-limit humidistat, **75 % RH**, downstream of the humidifier | In series with the steam valve actuator's **power**. When it trips, the valve spring-returns closed |
| SA SMK / RA SMK | Duct smoke detectors | Reset when the alarm clears (fire alarm reset) |
| HI-SP | High static safety switch at the fan discharge | **Manual reset** |
| SP | Duct static pressure sensor, ¾ down the longest duct | |
| RF | Return fan on an ACH580-style VFD | Tracks supply fan speed (sequence) |

## 2. Equipment sizing / design conditions

| Item | Value |
|---|---|
| Design supply airflow | 24,000 cfm |
| Supply fans | 4 × 10 HP, 460 V, **8,000 cfm each** at design static (N+1: any 3 fans meet the 24,000 cfm design; with all 4 running each fan delivers about 6,000 cfm at reduced speed) |
| Return fan | 1 × 15 HP, 460 V |
| Total static (supply) | 4.5 in. w.c. at design |
| Duct static range | 0-2.0 in. w.c. sensor span; sequence setpoint 1.3-1.8 in. w.c. |
| CHW | 44 °F supply / 56 °F return at design |
| HW (preheat + heating) | **140 °F** supply |
| Steam (humidifier) | Available or unavailable (plant parameter) |
| Minimum OA | 20 % damper position when occupied (sequence) |

HW/CHW supply temperatures, loop availability and steam availability are
adjustable parameters with "plant off" faults, until the boiler and chiller
simulators exist.

## 3. Safety circuit

Freezestat, high static switch, RA smoke and SA smoke are each:

1. monitored by the ALC controller on their own **BI** (aux contact), and
2. wired in **series** into **DI4** of every drive (SF-1…4 and RF), set as
   **20.41 Start interlock 1 = DI4** (the ACH580 default). An open start
   interlock prevents starting **and stops a running drive**.
   The drive **coasts** to a stop (20.45 *Not used* or *Coast*), so after a
   high-static trip the fan sheds duct pressure instead of being braked. It
   **restarts automatically** when the safety is reset and the interlock
   closes, as long as the start command is still on.
   default is a deliberate fault scenario for training.

When any device opens, all five drives stop at the drive level, whatever the
controller program does. The freezestat and high static switch latch until
someone presses their **manual reset**. In the simulator that's a button on
the web page, standing in for a technician at the unit.

**Humidifier high limit:** a separate hardwired circuit, not part of the
fan safety string. The 75 % RH supply air humidistat breaks **power** to the
steam valve actuator, so the valve spring-returns closed whatever the 2-10 V
signal and the humidifier enable say. It isn't on the points list, so the
controller only sees it indirectly, as supply humidity stops rising. It
**resets automatically** when supply RH falls below the setting minus its
differential (assumed 5 % RH).

## 4. Fan array

- Each supply fan has its own drive (hardwired control, same as the
  `vfd_ach580_hardwired` template): **BO start → DI1, AO speed → AI1,
  RO3 fault → BI, AO1 → feedback AI, CT on the motor leads → amps AI.**
- The fans discharge into a common plenum. Total airflow is where the
  combined fan curves (at each fan's speed) meet the duct system curve.
- **Barometric dampers:** a stopped fan's damper closes under plenum pressure,
  so air doesn't short-circuit backward through it. Simulated faults:
  - **Stuck open:** air recirculates through the idle fan, reducing supply
    airflow. The fan windmills backward, so its drive restarts into a
    reverse-spinning fan.
  - **Stuck closed:** the fan runs with no airflow. Amps are low and
    plenum static is high.
- Unequal fan speeds (a drive in Hand, a different reference) make the fans
  share load unevenly, and that shows in the amps.

## 5. Valves and dampers

- **Signal convention (decided):** analog terminal objects are written as
  **% of 10 V**, the voltage the ALC's physical AO would put on the wire.
  Valves and dampers are 2-10 V, so closed = 2 V = **20** and open =
  10 V = **100**. The drives keep the ACH580 default **AI1 = 0-10 V**
  (0 = 0 Hz … 100 = 60 Hz).
- All valve and damper signals are **2-10 V**: 2 V = closed / minimum, 10 V = open.
  Below 2 V (including a broken wire = 0 V) the actuator drives to the
  2 V position. Only loss of **power** sends it to its spring-return
  position.
  - Training point: a broken signal wire on the fail-open preheat valve
    makes it go **closed**, not open.
- 2-way valves give **variable coil flow** (equal-percentage valve, with
  authority). Loop differential pressure is a plant parameter.
- Actuators have stroke times (valves about 90 s, dampers about 150 s).

## 6. Duct and zones (until the VAV simulators exist)

The VAV boxes will report damper positions to the ALC program themselves, so
the AHU simulator does **not** provide any reset data. For duct static
pressure only, it models the VAV boxes as one aggregate:

- **Zone airflow demand follows an occupancy schedule** (decided). For
  example: occupied weekdays 06:00-18:00 at high demand with a midday peak,
  unoccupied at minimum demand. The schedule and levels are adjustable, and
  the demand can be overridden live from the web page.
- Pressure-independent boxes: they throttle to hold their demand when static
  is high. When static is too low they open fully and can't get enough air
  ("starved" zones).
- Duct static at the sensor comes from fan airflow, duct friction up to the
  sensor, and the aggregate box resistance.
- Return air temp, RH and CO2 come from a simple aggregate building model
  (load, occupancy, supply air conditions). It will be replaced by the VAV and
  zone simulators later.

## 7. Weather station (new simulated device)

A separate equipment type, `weather_station`, is its own BACnet device on
the virtual network. The ALC system reads it (no writes into the controller),
and every simulator on the host uses the same outdoor conditions:
- the AHU: OA temp and enthalpy for mixing, coil loads, economizer behaviour
- later the chiller, boiler and zones

Objects: OA temperature and OA RH (AI). Dew point and enthalpy (AV) are
optional and can be turned off in the template, so trainees calculate
enthalpy in the ALC program.

Weather profile: **fixed values by default** (decided), adjustable live from
the web page. A daily cycle or scripted scenario can be added later.

## 8. Points list (revised for the 4-fan array)

Names follow the confirmed convention.
- **Inputs:** the simulator writes them into the ALC controller's objects
  with these names, found by object name.
- **Outputs:** the ALC network outputs write simulator terminal objects; the
  names listed are the simulator objects' names.

| ALC point | Type | Name | Simulator side |
|---|---|---|---|
| Mixed Air Temp | AI | `ma_temp` | written to ALC |
| Prefilter Differential Pressure | AI | `pf_dp` | written to ALC |
| Return Air CO2 | AI | `ra_co2` | written to ALC |
| Return Air Humidity | AI | `ra_rh` | written to ALC |
| Return Air Temp | AI | `ra_temp` | written to ALC |
| Supply Air Humidity | AI | `sa_rh` | written to ALC |
| Supply Air Static Pressure | AI | `sa_static` | written to ALC |
| Supply Air Temp | AI | `sa_temp` | written to ALC |
| Return Fan Amps | AI | `rf_amps` | written to ALC (CT, 0-30 A) |
| Return Fan VFD Feedback | AI | `rf_vfd_fb` | written to ALC (drive AO1) |
| Supply Fan 1-4 Amps | 4 × AI | `sf1_amps` … `sf4_amps` | written to ALC (CT per fan, 0-30 A) |
| Supply Fan 1-4 VFD Feedback | 4 × AI | `sf1_vfd_fb` … `sf4_vfd_fb` | written to ALC (drive AO1 per fan) |
| Freezestat | BI | `frz_stat` | written to ALC |
| High Static Shutdown | BI | `hi_static` | written to ALC |
| Return Air Smoke Detector | BI | `ra_smoke` | written to ALC |
| Supply Air Smoke Detector | BI | `sa_smoke` | written to ALC |
| Return Fan VFD Fault | BI | `rf_vfd_flt` | written to ALC (RF drive RO3) |
| Supply Fan 1-4 VFD Fault | 4 × BI | `sf1_vfd_flt` … `sf4_vfd_flt` | written to ALC (each drive RO3) |
| Cooling Valve | AO | `clg_vlv` | simulator terminal, % of 10 V (2-10 V) |
| Heating Valve | AO | `htg_vlv` | simulator terminal, % of 10 V (2-10 V) |
| Preheating Valve | AO | `ph_vlv` | simulator terminal, % of 10 V (2-10 V) |
| Humidifier | AO | `hum_vlv` | simulator terminal, % of 10 V (2-10 V) |
| Mixed Air Dampers | AO | `ma_dmpr` | simulator terminal, % of 10 V (2-10 V) |
| Return Fan VFD Speed | AO | `rf_vfd_spd` | simulator terminal → RF drive AI1 (0-10 V) |
| Supply Fan 1-4 VFD Speed | 4 × AO | `sf1_vfd_spd` … `sf4_vfd_spd` | simulator terminals → each drive AI1 (0-10 V) |
| Humidifier Enable | BO | `hum_enable` | simulator terminal |
| Return Fan Start/Stop | BO | `rf_ss` | simulator terminal → RF drive DI1 |
| Supply Fan 1-4 Start/Stop | 4 × BO | `sf1_ss` … `sf4_ss` | simulator terminals → each drive DI1 |

Hardware totals: **18 AI, 10 AO, 9 BI, 6 BO = 43** (was 28).
- OA temp and RH come from the weather station.
- The humidifier high-limit humidistat is hardwired only and isn't a point.
- Software points (setpoints, fan status BVs, alarms) live in the ALC
  program, not the simulator.

## 9. BACnet layout

On the host's virtual network:

| Device | Contents |
|---|---|
| AHU-1 | Terminal objects for every BO/AO above, plus read-only monitor points (true temperatures, airflows, damper/valve positions, safety device states) |
| AHU-1 SF-1…SF-4, RF | Optional: each drive's ACH580-style object list as its own device, for troubleshooting or a future fieldbus option. Control stays hardwired, per the drawing |
| WX-1 | Weather station |

## 10. Faults planned (in addition to the drive faults)

| Area | Faults |
|---|---|
| Dampers | Actuator failed (spring return), linkage slipped, stuck damper, OA damper leakage |
| Filter | Loading over time, blown filter |
| Coils / valves | Valve stuck, actuator failed, broken 2-10 V wire, plant off, low CHW/HW temp, **preheat coil freeze** (water in the tubes below freezing at low flow; it stays damaged until reset) |
| Safeties | Freezestat trip / failed, high static trip (e.g. VAV boxes or fire damper closed), smoke detector alarm, a drive with 20.41 not set to DI4 (doesn't stop on a safety trip) |
| Fan array | Barometric damper stuck open/closed, plus each drive's faults (drive in Hand, coupling, bearings, …) |
| Humidifier | No steam, valve stuck open: the high-limit humidistat trips at 75 % RH and closes the valve. Also high-limit humidistat failed (doesn't trip: wet duct, SA RH > 95 %), high-limit setpoint drifted |
| Sensors | Offset, failed, drifting on any AI; MAT reading stratified air |

## 11. Decisions and open items

Decided in review:

| Item | Decision |
|---|---|
| Analog terminal convention | % of 10 V (closed 2-10 V valve = 20) |
| Drive speed signal | ACH580 default AI1 = 0-10 V |
| ALC object names | Convention in section 8 (`ma_temp`, `sa_static`, `sf1_amps` …) |
| Zone demand for duct static | Occupancy schedule |
| Weather default | Fixed values |
| Supply fan size | 8,000 cfm each (N+1) |
| Humidifier high limit | Hardwired 75 % RH supply humidistat breaking steam valve actuator power |
| CT ranges | 0-30 A on all fans (SF-1…4 and RF) |
| Humidistat reset | Automatic, 5 % RH differential |

Still open:

1. **VFD changes** you're still testing. These affect all five AHU drives.
