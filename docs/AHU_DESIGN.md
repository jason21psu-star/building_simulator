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
| HUM | Steam humidifier: enable BO + 2-10 V valve | No hardwired high-limit humidistat on the drawing (software high limit only) |
| SA SMK / RA SMK | Duct smoke detectors | Reset when the alarm clears (fire alarm reset) |
| HI-SP | High static safety switch at the fan discharge | **Manual reset** |
| SP | Duct static pressure sensor, ¾ down the longest duct | |
| RF | Return fan on an ACH580-style VFD | Tracks supply fan speed (sequence) |

## 2. Equipment sizing / design conditions

| Item | Value |
|---|---|
| Design supply airflow | 24,000 cfm |
| Supply fans | 4 × 10 HP, 460 V, about 6,000 cfm each |
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
   **20.41 Start interlock 1 = DI4**.

When any device opens, all five drives stop at the drive level, whatever the
controller program does. The freezestat and high static switch latch until
someone presses their **manual reset**. In the simulator that's a button on
the web page, standing in for a technician at the unit.

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

- All modulating signals are **2-10 V**: 2 V = closed / minimum, 10 V = open.
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

- An adjustable **zone airflow demand**: fixed, scheduled, or following
  building load.
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

Weather profile options: fixed values, a daily cycle (min/max temperature and
RH), or a scripted scenario, all adjustable live from the web page.

## 8. Points list (revised for the 4-fan array)

| ALC point | Type | Simulator side |
|---|---|---|
| Mixed Air Temp | AI | written to ALC (by object name) |
| Prefilter Differential Pressure | AI | written to ALC |
| Return Air CO2 | AI | written to ALC |
| Return Air Humidity | AI | written to ALC |
| Return Air Temp | AI | written to ALC |
| Supply Air Humidity | AI | written to ALC |
| Supply Air Static Pressure | AI | written to ALC |
| Supply Air Temp | AI | written to ALC |
| Return Fan Amps | AI | written to ALC (CT) |
| Return Fan VFD Feedback | AI | written to ALC (drive AO1) |
| Supply Fan 1-4 Amps | 4 × AI | written to ALC (CT per fan) |
| Supply Fan 1-4 VFD Feedback | 4 × AI | written to ALC (drive AO1 per fan) |
| Cooling Valve | AO | simulator terminal object |
| Heating Valve | AO | simulator terminal object |
| Preheating Valve | AO | simulator terminal object |
| Humidifier | AO | simulator terminal object |
| Mixed Air Dampers | AO | simulator terminal object |
| Return Fan VFD Speed | AO | simulator terminal object → RF drive AI1 |
| Supply Fan 1-4 VFD Speed | 4 × AO | simulator terminal objects → each drive AI1 |
| Freezestat | BI | written to ALC |
| High Static Shutdown | BI | written to ALC |
| Return Air Smoke Detector | BI | written to ALC |
| Supply Air Smoke Detector | BI | written to ALC |
| Return Fan VFD Fault | BI | written to ALC (RF drive RO3) |
| Supply Fan 1-4 VFD Fault | 4 × BI | written to ALC (each drive RO3) |
| Humidifier Enable | BO | simulator terminal object |
| Return Fan Start/Stop | BO | simulator terminal object → RF drive DI1 |
| Supply Fan 1-4 Start/Stop | 4 × BO | simulator terminal objects → each drive DI1 |

Hardware totals: **18 AI, 10 AO, 9 BI, 6 BO = 43** (was 28). OA temp and RH
come from the weather station. Software points (setpoints, fan status BVs,
alarms) live in the ALC program, not the simulator.

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
| Safeties | Freezestat trip / failed, high static trip (e.g. VAV boxes or fire damper closed), smoke detector alarm |
| Fan array | Barometric damper stuck open/closed, plus each drive's faults (drive in Hand, coupling, bearings, …) |
| Humidifier | No steam, valve stuck open (wet duct, SA RH > 95 %) |
| Sensors | Offset, failed, drifting on any AI; MAT reading stratified air |

## 11. Open items (need an answer before or during build)

1. **Analog terminal convention.** The drive's AI1 terminal is written as **% of 10 V**
   (70 = 7 V). For the 2-10 V actuators that means the trainee's program writes
   the signal its physical AO would put out: 0 % logic = 2 V = **20**. Keep this
   convention, or write engineering % (0-100) and let the simulator apply 2-10 V?
2. **Drive speed signal.** Keep the ACH580 default AI1 = 0-10 V, or 2-10 V like the
   valves (12.17 AI1 min = 2 V)?
3. **CT ranges** for the amps AIs (e.g. 0-20 A supply fans, 0-30 A return fan).
4. **ALC object names** for the points written into the controller. A naming
   convention is fine, e.g. `ma_temp`, `sa_static`, `sf1_amps` … `sf4_vfd_flt`.
5. **Zone demand profile** default for the duct static model (fixed %, or an
   occupancy schedule).
6. **Weather default** (fixed, or a daily cycle for a chosen season).
7. **VFD changes** you're still testing. These affect all five AHU drives.
