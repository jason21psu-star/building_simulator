# Chilled water plant simulator: physical design (for review)

Status: **built** as equipment type `chw_plant`. The model is
`hvacsim/models/chw_plant.py`, the template `configs/templates/chw_plant.yaml`,
and the schedules are in `configs/chw_plant/*.csv`. This records the 22
design questions answered in review. Section 14 describes how each part is
simulated. Source documents:

- YORK *YK OptiView BAS points list*, Rev K_04d, tab "YK EM or SSS" (BACnet objects and status code tables)
- YORK *Model YK Style G Operations and Maintenance*, form 160.75-O1.EN.CE (start/stop sequence, capacity control)

Open items are listed in section 13.

## 1. Plant arrangement

```
                     ┌────────────── CT-1..3 cells (separate basins + equalizer line) ──────────────┐
                     │  CW return header ─┬─ inlet iso ─► fill ─┐                                    │
                     │                    └─ bypass (2-pos) ────┴► BASIN ─ outlet iso ─► CW supply hdr│
                     │                                  equalizer ◄─ eq iso ─┘                        │
                     │                                                                               ▼
   CW return hdr ◄── condenser of CH-1..3 (CW iso valve each) ◄─── CWP-1..3 (VFD, headered) ◄── CW supply hdr

   PCHWP-1..3 (VFD, headered) ──► evaporator of CH-1..3 (CHW iso valve each) ──► primary supply hdr (FM total)
           ▲                                                                          │
           │                                                    TES cold hdr ◄────────┤
           │                     TK-1..3 in parallel (closed, stratified,             │
           │                     motorized iso top + bottom, decoupler FM ±)          ▼
   primary return hdr ◄──────── TES warm hdr ◄───────────────────────────── SCHWP-1..3 (VFD, on UPS)
                                        ▲                                             │ plant DP (hardwired)
                                        │                              ┌──────────────┴──────────────┐
                                        └──── building return ◄──── East riser                West riser
                                                                    (AHU-1 + aggregate)        (aggregate)
                                                                    remote DP (241011)         remote DP (241012)
```

- **Decoupler:** the tanks are the primary/secondary decoupler. Decoupler flow = primary − secondary.
  - Positive flow charges the tanks: cold water enters at the bottom and warm water leaves at the top.
  - Negative flow discharges them.
- **Headered pumps:** any primary pump can serve any chiller whose CHW isolation valve is open, and the same goes for the condenser pumps.

## 2. Chillers (CH-1..3): YORK YK centrifugal, VSD, OptiView, Mode B

| Item | Default (per chiller, `chillers.csv`) |
|---|---|
| Capacity | 1,200 tons |
| Chilled water | 44 °F leaving / 56 °F entering, 2,400 gpm |
| Condenser water | 85 °F entering / 95 °F leaving, 3,600 gpm |
| Compressor | Variable speed drive plus pre-rotation vanes. Speed reduces first, then the vanes close at minimum speed for the current lift |
| Efficiency | about 0.57 kW/ton at full load. Strongly better at part load and with low condenser water temperature |
| Minimum load | 10 %. Hot gas bypass: none |
| Evaporator / condenser ΔP | about 20 ft at design, rising with the square of flow |
| Flow switches | The chiller's own. Open below about 50 % of design flow |

**BACnet interface.** One simulated device per chiller, with the object list taken from the YORK BAS points list:

- **Written by ALC:**
  - `REM RUN STOP CMD` BV1
  - `REM LEAVING CH LIQ TEMP SP` AV1
  - `REM MOTOR CURRENT SP` AV2
- **Read:**
  - **Temperatures:** AV5/AV6 leaving/entering chilled liquid; AV11/AV10 leaving/entering condenser.
  - **Pressures and saturation temperatures:** AV7/AV8 evaporator/condenser pressure; AV21/AV22 saturation temperatures.
  - **Motor and setpoints:** AV12 % FLA; AV19/AV20 active setpoints; AV35 anti-recycle time remaining.
  - **Status bits:** BV5 motor run, BV7 chilled liquid pump contact (not wired), BV9/BV10 flow switches.
  - **Section 2:** AV112 PRV position, AV114 refrigerant level setpoint.
  - **Totals and other readings:** run hours, starts, oil pressures and temperatures, refrigerant level.
- **Status codes:** MV1–MV8 (operation, safety, cycling, warning, control source, modified run, start inhibit, internal). The BACnet value = code + 1, and the state text is copied from the code tables.
- **VSD points:** output frequency, input kW and others are on the VSD tab of the YORK spreadsheet, which hasn't been provided (section 13).

**Sequence** (O&M 160.75-O1, plus review answers):

1. **Remote control:** BV1 = Run is obeyed only when the panel is in Remote (ISN). MV5 shows the control source. "Panel left in Local" is a fault you can inject.
2. **Start conditions:** the chiller starts only when both flow switches are made, the anti-recycle timer has expired, and no oil coastdown is in progress. If those hold, it pre-lubes for 50 s, then the motor starts.
   - Afterwards, the refrigerant level setpoint ramps up and the chiller loads to its setpoint, limited by the current-limit setpoint.
3. **Low load:** after the vanes are fully closed at minimum speed, if the water keeps falling the chiller cycles off on code 6, "Leaving Chilled Liquid – Low Temperature". It restarts when the water returns to setpoint.
4. **Stopping:** after any stop, the oil pump runs for 150 s of coastdown and the chiller can't restart during that time.
5. **Cycling shutdowns** (flow switch open, power failure, low temperature) clear automatically. **Safety shutdowns** (evaporator low pressure, condenser high pressure, motor or VSD faults, excess surge) lock out until someone resets the panel from the web page. BV1 does not clear them.
6. **Power failure** does **not** latch: cycling code 5, "Control Panel – Power Failure", then normal restart timers.
7. **Surge:** low load with high lift gives warning 30, "Excess Surge Detected". If it persists, safety 53, "Surge Protection – Excess Surge".
8. **Overflow and starvation:**
   - **Low evaporator flow at load:** high chiller ΔT and low evaporator pressure, giving warning 4 and then safety 1.
   - **Low condenser flow:** high condenser pressure, giving warning 5 and then safety 5.
   - **Evaporator overflow:** low ΔT and high evaporator ΔP.
9. **Hardwired chiller points:** none. A gateway failure means ALC loses control of the chiller.

**Faults:**
- Flow switch failed open
- Low refrigerant charge
- Fouled condenser tubes (high approach and lift)
- Fouled evaporator tubes
- Panel in Local
- VSD fault
- Gateway offline

## 3. Pumps (all on ACH580 VFDs, hardwired, headered)

| Set | Each | Motor | CT range | Notes |
|---|---|---|---|---|
| PCHWP-1..3 | 2,400 gpm @ 60 ft | 50 hp | 0–100 A | Variable primary flow down to 80 % of design |
| SCHWP-1..3 | 2,400 gpm @ 100 ft | 75 hp | 0–150 A | 2 + 1. **On UPS** |
| CWP-1..3 | 3,600 gpm @ 80 ft | 100 hp | 0–200 A | Normally a fixed speed set by the balancer. Reduced flow is optional |

- **Sizing margin:** pumps are sized with the usual margin, so design flow comes at about 54–55 Hz.
- **Flow:** set by pump curves (affinity laws) against the circuit's resistance, not prescribed.
- **Overflow staging:** two primary pumps running into one open chiller give about 110 % of design flow (about 2,650 gpm). This allows the stage-up sequence "start the next pump, overflow the running chiller, then open the next chiller's valve".
- **Drive I/O:** each drive is wired like SF-2-VFD:

  | Drive terminal | Signal |
  |---|---|
  | DI1 | Start |
  | AI1 | 0–10 V speed reference |
  | AO1 | Speed feedback |
  | RO3 | Fault |

  Status comes from a CT.
- **Drive faults and hand control:** all ACH580 faults, warnings, parameters and Hand-Off-Auto, as on the AHU drives.

**Pump faults:**
- Worn impeller (low head)
- Bearing or seal failure (overload)
- Plus the drive faults

## 4. Isolation valves

The plant has 18 motorized valves. Each has a BO command, an open end switch and a closed end switch, and a 60 s stroke.

| Valves | Count | Size / type | Fail position |
|---|---|---|---|
| Chiller CHW and CW isolation | 6 | Butterfly, 14" / 16", two-position | In place |
| Tower inlet, outlet, equalizer | 9 | Butterfly, two-position | In place |
| Tower basin bypass (one per cell) | 3 | Two-position | **Spring-return closed** (water over the fill) |
| Tank top and bottom isolation | 6 | Butterfly, two-position (actuators on UPS) | In place |

- **Flow during stroke:** follows a butterfly characteristic, so most of the change happens early in travel.

**Valve faults:**
- Actuator failed
- Stuck part-open
- End switch out of adjustment (reports open at 80 %)
- Seat leak (5 % when closed)

## 5. Cooling towers (CT-1..3)

| Item | Default (per cell, `cooling_towers.csv`) |
|---|---|
| Rating | 3,600 gpm, 95 → 85 °F at 78 °F wet bulb (7 °F approach), induced-draft counterflow |
| Fan | 60 hp on ACH580, hardwired. **Vibration switch on DI4 start interlock** (coasts, manual reset) |
| Distribution | Flow splits among cells with inlet valves open. Below about 50 % flow, fill wetting is poor and performance drops; above about 120 % the basin overflows |
| Basins | Separate basins tied by an equalizer line, with a motorized equalizer isolation valve at each cell |
| Bypass | Each cell has its own 2-way bypass straight into its basin. An active cell's water goes either over the fill or into the basin |
| Basin | Temperature sensor, heater (BO) with low-level lockout, mechanical makeup float, low- and high-level switches |

- **Performance:** set by the WX-1 wet bulb and an effectiveness model that depends on air/water flow.
- **Basin levels:** each basin's level is modelled. With its equalizer valve closed and its outlet open, a basin can draw down, which causes a low-level alarm and then pump cavitation.

**Winter behaviour** (no waterside economizer in this build):
- Basin freezing when a heater fails or is locked out
- Fill icing at low fan speed with low flow
- Low entering condenser water at start-up

**Tower faults:**
- Fan vibration trip
- Clogged nozzles
- Fouled fill
- Makeup valve stuck
- Heater failed
- Basin temperature sensor failed
- Valve faults as in section 4

## 6. Thermal storage / decoupler (TK-1..3)

| Item | Default (per tank, `tanks.csv`) |
|---|---|
| Volume | 120,000 gal, about 40 ft tall × 23 ft diameter, closed / pressurized |
| Capacity | About 900 usable ton-hours each, about 2,700 total (12 °F ΔT, 90 % stratification efficiency). Peak 2,400 tons ≈ 1.1 h |
| Piping | Parallel between a cold header (bottom diffusers) and a warm header (top diffusers). Motorized isolation valves top and bottom |
| Sensors | 10 temperatures evenly up the height (`tkN_temp_01` top … `_10` bottom) |

- **Stratification:** modelled with a vertical node model of each tank, so the thermocline moves with charge and discharge flow and blurs over time.
- **Thermocline breakthrough:** when the thermocline reaches the outlet, the supply water warms. That's what ends a long outage.

## 7. Sensors

| Location | Sensors |
|---|---|
| Primary | Supply and return header temperatures; **total primary flow meter on the supply header**; evaporator flow meter on each chiller |
| Secondary | Supply and return temperatures; flow meter; plant DP across the secondary headers (hardwired, controls pump speed) |
| Decoupler / TES | Bidirectional decoupler flow meter (+ = charging); cold and warm header temperatures |
| Condenser | Supply and return header temperatures; condenser flow meter on each chiller |
| Building | One remote DP at the far end of each riser, wired to a building controller, used to reset the plant DP setpoint over BACnet |
| Power | `utility_power` BI (phase monitor / ATS normal contact, True = normal) |

- **Consistency check:** total primary flow = secondary flow + decoupler flow, so a drifting meter shows up.

**Sensor faults:**
- Drift
- Failure
- Flow meter reading zero
- Decoupler meter sign reversed

## 8. Building load

- **Peak:** 2,400 tons, split East riser 55 % and West riser 45 % (`chw_branches.csv`).
- **AHU-1:** its cooling coil (about 100 tons) moves from a fixed 44 °F supply to this plant, on the East riser.
- **Aggregate load:**
  - Each riser's share of the peak, minus the design coil loads of the AHUs connected to that riser.
  - It shrinks automatically as AHUs are added, the same idea as the aggregate VAV.
- **Coil behaviour:** the aggregate behaves like coils with 2-way valves holding discharge air temperature.
  - It follows the occupancy schedule and WX-1 (outdoor air temperature, humidity, solar).
  - Warm supply water draws more flow at a lower ΔT.
  - Too little DP at a riser leaves its valves wide open and its load unmet.
- **Piping:** plant DP = remote DP + main and riser friction. The friction is about 15 psi in the main at design flow and rises with the square of flow. The remote sensors see about 8 psi at design.

**Load faults:**
- Low-ΔT syndrome
- Riser bypass stuck open
- Load spike

## 9. Power and UPS

- **Power model:** utility, UPS and (future) generator supplies, so generators can be added later and power all equipment. The UPS bridges the building to generator power.
- **On UPS:**
  - Secondary pump VFDs
  - Plant ALC controllers and sensors
  - Tank valve actuators

  Runtime is unlimited.
- **Not on UPS:**
  - Chillers, including their gateways
  - Primary and condenser pumps
  - Tower fans
  - Chiller and tower valves
  - Basin heaters
- **Air side:** the AHU and VAV simulators stay energized.
- **Trigger:** Utility Fail and Restore buttons on the web page, with an optional duration. It applies to the whole host.
- **When power fails:**
  - **Chillers:** the compressors coast and the chillers' BACnet devices go offline.
  - **Drives not on UPS:** these lose power, so status drops to 0 A while ALC's commands stay on.
  - **Secondary flow:** comes entirely from the tanks.
- **When power returns:**
  - **Drives:** ACH580s restart if their start command is still on (level start). A controlled restart depends on the trainees' logic using `utility_power`.
  - **Chillers:** return with cycling code 5, then the 150 s coastdown, anti-recycle and 50 s pre-lube.

## 10. ALC controllers and simulated devices

| ALC controller | Device | Serves | Simulator I/O device (MAC) |
|---|---|---|---|
| CHW plant | 241001 | Chiller BACnet integration (all three), PCHWPs, chiller isolation valves, primary/condenser header sensors and flow meters, `utility_power` | 4010041 (41) |
| Secondary | 241002 | SCHWPs, plant DP, secondary temperatures and flow | 4010042 (42) |
| Condenser / towers | 241003 | CWPs, CT-1..3 | 4010043 (43) |
| Thermal storage | 241004 | TK-1..3, decoupler flow meter, cold/warm header temperatures | 4010044 (44) |
| East riser | 241011 | `chw_remote_dp` | written by the plant simulator |
| West riser | 241012 | `chw_remote_dp` | written by the plant simulator |

- **Chiller devices:** CH-1..3 are simulated devices 4010031–4010033 (MACs 31–33), each serving the YORK object list.
- **I/O devices:** each I/O device acts as the field wiring of one ALC controller. ALC network outputs write to it.
- **Moving equipment:** each CSV row's `controller_device` decides which controller, and therefore which I/O device, its points belong to.

## 11. Point names

The prefix and number come from the CSV tag (PCHWP-2 → `pchwp2_…`), in the AHU style.

- **Pumps** (`pchwpN`, `schwpN`, `cwpN`):
  - Outputs: `_ss`, `_vfd_spd`
  - Inputs: `_amps`, `_vfd_fb`, `_vfd_flt`
- **Chiller valves and flow:**
  - Outputs: `chN_chw_vlv`, `chN_cw_vlv`
  - Inputs: each valve's `_open` and `_closed` switches; `chN_chw_flow`, `chN_cw_flow`
- **Tower cells:**
  - Outputs: `ctN_fan_ss`, `ctN_fan_vfd_spd`, `ctN_inlet_vlv`, `ctN_outlet_vlv`, `ctN_bypass_vlv`, `ctN_eq_vlv`, `ctN_basin_htr`
  - Inputs: `ctN_fan_amps`, `ctN_fan_vfd_fb`, `ctN_fan_vfd_flt`, `ctN_basin_temp`, `ctN_level_lo`, `ctN_level_hi`, and each valve's `_open` and `_closed`
- **Tanks:**
  - Outputs: `tkN_top_vlv`, `tkN_bot_vlv`
  - Inputs: each valve's `_open` and `_closed`; `tkN_temp_01` … `tkN_temp_10`
- **Plant sensors:**
  - 241001: `pchws_temp`, `pchwr_temp`, `pchw_flow`, `cws_temp`, `cwr_temp`, `utility_power`
  - 241002: `schws_temp`, `schwr_temp`, `schw_flow`, `schw_dp`
  - 241004: `dcplr_flow`, `tes_cold_temp`, `tes_warm_temp`
  - 241011 / 241012: `chw_remote_dp`. The name is the same on both, because each riser has its own controller.

## 12. CSV files

Every file has `tag` and `controller_device`. Blank cells keep the default, `#` rows are skipped, and adding a row adds that piece of equipment.

| File | Further columns |
|---|---|
| `chillers.csv` | `sim_device`, `mac`, `capacity_tons`, `chw_design_gpm`, `cw_design_gpm`, `chws_design_temp`, `kw_per_ton`, `min_load_pct`, `evap_dp_ft`, `cond_dp_ft` |
| `primary_pumps.csv`, `secondary_pumps.csv`, `condenser_pumps.csv` | `design_gpm`, `design_head_ft`, `motor_hp`, `ct_range_amps` |
| `cooling_towers.csv` | `design_gpm`, `range_f`, `approach_f`, `design_wb`, `fan_hp`, `ct_range_amps`, `basin_gal` |
| `tanks.csv` | `volume_gal`, `height_ft`, `sensor_count` |
| `chw_branches.csv` | `load_share_pct`, `design_remote_dp_psi`. Here `controller_device` is the remote DP controller |

**Plant-wide settings (plant config):**
- Building peak tons
- Design CHWS temperature
- Main piping loss
- Which controller gets each header sensor

## 13. Open items

1. **YORK VSD tab** of the BAS points list: needed for the VSD-specific objects. Until then those objects are marked unconfirmed.
2. **OptiView operating manual 160.54-O1:** needed to confirm these assumed values:
   - Anti-recycle timer: 30 min start-to-start.
   - Low-temperature cycling: shuts down at 4 °F below setpoint, restarts at setpoint.
   - Remote-setpoint behaviour.
3. Nothing has been tested against a real OptiView / E-Link gateway or ALC integration yet.

## 14. How it is simulated

**Hydraulics.** The hydraulic network is solved every half second of
simulated time.
- **Pumps:** pump curves are H = 1.25·H_rated·s² − 0.25·H_rated·(Q/Q_rated)².
  Each pump branch adds 10 ft of piping loss (suction diffuser, check valve,
  triple-duty valve), and a check valve stops reverse flow.
- **Balancer speed:** the system resistances are calibrated so that one pump
  at 55 Hz gives design flow through its circuit.
- **Primary and secondary loops:** each is a parallel pump set against a
  quadratic system curve. They are coupled through the tanks: the flow
  through the decoupler is solved so that primary = secondary + tank flow.
  With every tank valved out, primary and secondary run in series, and the
  building valves limit primary flow.
- **Overflow:** two primary pumps at 55 Hz into one open chiller give about
  120 % of design flow, and the evaporator ΔP rises with the square of flow.
  Review question 5 estimated about 110 %. The difference comes from how the
  piping losses are split between the pump branches and the chiller branch.
- **Condenser circuit:** this is an open circuit. Each cell's inlet path lifts
  water 12 ft to the distribution basin. Each bypass path drops it into the
  basin at 2 ft. The pumps take suction from basins whose outlet valves are
  open.
- **Basin levels:** each basin's level is integrated over time. Inflows are
  fill, bypass, makeup and equalizer flow. Outflows are the outlet and
  evaporation. A basin below 4 in. starves the pumps.

**Valves.** Two-position butterfly valves with a 60 s stroke. Flow fraction
is 1 − (1 − position)³. A closed valve leaks 0.2 %, or 5 % with the seat
leak fault. The end switches trip at 98 % open and 2 % closed. With the
end switch fault, "open" trips at 80 %.

**Chiller (`components/chillers/york_yk.py`, behind the chiller interface: [EQUIPMENT_INTERFACE_DESIGN.md](EQUIPMENT_INTERFACE_DESIGN.md)).**
- **State machine:** pre-lube 50 s, VSD ramp 30 s, run, soft stop,
  coastdown 150 s.
- **Capacity:** follows the leaving chilled liquid error at up to +0.4 %/s
  loading and −1 %/s unloading, with a minimum of 10 %.
- **Limits:** the current limit, evaporator low-pressure limit (33.5 °F
  saturation) and condenser high-pressure limit (165 psig) hold or unload
  the chiller.
- **Trips:** evaporator low pressure (31.5 °F saturation for 5 s, safety 1),
  condenser high pressure (180 psig, safety 5) and VSD fault (safety 77)
  latch until a panel reset. Surge occurs when lift exceeds
  lift_d·(0.75 + 0.45·load). It gives warning 30, then safety 53 after
  5 minutes.
- **Approaches:** evaporator and condenser approaches grow with load and
  with low water flow.
- **Power:** kW = design kW × load × part-load factor × (lift / design lift)^1.15.
- **Refrigerant:** R-134a saturation pressures. The YK O&M manual also lists
  R-513A and R-1234ze; the simulator uses R-134a.

**Towers.** Effectiveness on the wet bulb is ε = 1 − e^(−NTU). NTU depends
on the air/water flow ratio and is calibrated to 95 → 85 °F at 78 °F wet
bulb.
- **Water distribution:** wetting is poor below 50 % flow. The distribution
  basin overflows above 120 %, or above 100 % with clogged nozzles.
- **Natural draft:** with the fan off, natural draft gives 12 % airflow.
- **Basin:** fully mixed, with a heater and a makeup float.
- **Freezing:** the basin freezes at 32 °F with no flow. Fill icing builds
  below 32 °F outdoor air at low water loading and melts above 34 °F.

**Tanks.** Each tank is 40 nodes stacked vertically. Water moves between
nodes by upwind advection, with a little mixing between nodes and
overturning if a colder node sits above a warmer one. This gives a moving,
blurring thermocline. The 10 sensors interpolate the node temperatures.

**Building.**
- **Coil banks:** each riser's coil bank is a counterflow ε-NTU coil sized
  to its share of the peak load: entering air 78 °F, leaving 55 °F, water
  44 → 56 °F. UA scales with airflow^0.6 and water flow^0.8, because the
  tube-side coefficient falls at low flow.
- **Valves:** the aggregate valve moves, with a 30 s time constant, to the
  opening that meets the load at the riser's present DP.
- **Return water:** passes through 30,000 gal of loop water, so the return
  temperature responds over minutes.
- **Load:** the load fraction is internal (0.45 occupied / 0.10 unoccupied)
  + envelope (outdoor temperature) + ventilation (outdoor humidity) + solar.

**Demo mode.** In `--demo` a simple plant sequence runs in the mock 241001
controller:
- lead/lag chillers, each with its own primary pump, condenser pump and tower cell
- primary pump speed trims chiller flow to design
- tower fans hold 75 °F condenser water supply
- the secondary pumps hold a plant DP setpoint that the remote DPs trim
- restart after a utility outage

It is only a stand-in for the trainees' programs.
