# HVAC Equipment Training Simulators

Virtual HVAC equipment that behaves like the real thing on a BACnet/IP
network. Trainees program real Automated Logic (ALC) controllers. Each
controller commands its simulated equipment over BACnet, and the simulator
writes sensor values back into the controller's out-of-service inputs,
which it finds by **BACnet object name**.

## Network layout: many instances on one IP

Each simulator host (one machine, one IP address) is a **BACnet router** to
a virtual network. Every equipment instance is a separate BACnet device on
that network, the same way MS/TP devices sit behind a router:

```
 Lab LAN (BACnet/IP, network 1)
 ├── ALC controller 240110          fan_amps_1 (AI, OOS)  zone_temp_1
 │                                  fan_amps_2 (AI, OOS)  zone_temp_2
 └── SIM-HOST-1 192.168.1.51:47808  router, device 4010000
      └── virtual network 5001
           ├── WX-1      device 4010090  address 5001:90  OA TEMP (AI-1), OA RH (AI-2), …
           ├── UH-1-SIM  device 4010001  address 5001:1   BO-1 HWV_CMD …
           ├── UH-2-SIM  device 4010002  address 5001:2   BO-1 HWV_CMD …
           ├── AHU-1     device 4010020  address 5001:20  AO-1..10, BO-1..6 terminals; 27 values to ALC 240107
           ├── VAV-1…6   device 4010101-106  address 5001:101-106  AO-1 dmpr_1, AO-2 rh_vlv_1; to ALC 240901-240906
           ├── SF-1-VFD  device 4010011  address 5001:11  ACH580 object list (fieldbus)
           ├── SF-2-VFD  device 4010012  address 5001:12  DI1/AI1 terminals (hardwired)
           ├── SF-3-VFD  device 4010013  address 5001:13  Yaskawa Z1000, S1/A1 terminals (hardwired)
           ├── SF-4-VFD  device 4010014  address 5001:14  Yaskawa Z1000 BACnet objects (fieldbus)
           ├── CH-1…3    device 4010031-033  address 5001:31-33  YORK YK OptiView BAS objects (Mode B)
           ├── CHP-1 I/O device 4010041-044  address 5001:41-44  plant BO/AO terminals for ALC 241001-241004
           ├── B-1…3     device 4010051-053  address 5001:51-53  Lochinvar Crest BMS objects (Mode B)
           ├── HWP-1 I/O device 4010061-064  address 5001:61-64  plant BO/AO terminals for ALC 242001-242012
           └── GEN-PLANT-1 I/O  device 4010071  address 5001:71  ATS contacts / transfer inhibits for ALC 243001
  192.168.0.79:502  Modbus TCP: GEN-1…4 = unit IDs 1-4 (PowerCommand 3.x map), paralleling master = unit ID 5
```

- WebCTRL discovers the router and every instance with a normal Who-Is.
- The ALC network output writes `BO-1` of device 4010001 (or 4010002, …).
- Each instance writes its values to the controller objects named in its
  config, e.g. `fan_amps_1` and `zone_temp_1`.

### Network numbers (important)

| Setting | Rule |
|---|---|
| `host.ip_network` | The BACnet network number of the lab IP network. **It must match the number the ALC system uses** for that IP network, or routing breaks. |
| `host.virtual_network` | Must be **unique in the whole BACnet internetwork**: different on every simulator host, and not used by any ALC router or MS/TP network. |

## Equipment available

| Type | Mode | Description |
|---|---|---|
| `hw_unit_heater` | A (virtual I/O) | Hot water unit heater, 2-position valve, aquastat fan interlock (90 °F on / 80 °F off), 0-10 A CT on the fan motor |
| `weather_station` | B (ALC reads it) | Shared outdoor air temp/RH for the ALC system and every simulator on the host. Derived dew point, enthalpy and wet bulb, sensor faults, "device offline" |
| `ahu_vav_econ` | A (virtual I/O) | VAV AHU with economizer, 4-fan supply array with barometric dampers, return fan: 5 ACH580-style drives, preheat / CHW / HW coils with 2-way valves, steam humidifier with high limit, freezestat / high-static / smoke safety chain to drive DI4, aggregate VAV + building model. 43 hardware points. See [docs/AHU_DESIGN.md](docs/AHU_DESIGN.md) |
| `vav_rh` | A (virtual I/O) | Single-duct VAV with HW reheat, no fan: integrated 154 s damper actuator, airflow in cfm, 2-10 V reheat valve, zone with loads / humidity / CO2, zone sensor setpoint adjust (±1-5 °F) and override button (+30 min, max 120). Sits in an AHU's duct (`served_by`). Boxes come from a CSV schedule. See [docs/VAV_DESIGN.md](docs/VAV_DESIGN.md) |
| `chw_plant` | A + B | Chilled water plant: YORK YK VSD chillers (Mode B, OptiView BAS object list and status codes), headered primary / condenser pumps and UPS-backed secondary pumps on ACH580 drives, motorized isolation valves with end switches, cooling tower cells with basins / bypass / equalizer, stratified thermal storage tanks as the decoupler, risers with remote DP, building load, utility power failure. Equipment from one CSV per type. See [docs/CHILLER_PLANT_DESIGN.md](docs/CHILLER_PLANT_DESIGN.md) |
| `hw_plant` | A + B | Hot water plant: Lochinvar Crest condensing boilers (Mode B, BMS enable + firing rate, ignition sequence, blocking / lockout codes, efficiency by return temperature), dedicated primary pumps and headered secondary pumps on ACH580 drives, common-pipe decoupler with a bidirectional flow meter, branches with remote DP and end-of-branch 3-way valves, combustion air louver, gas pressure, emergency stop, expansion tank / loop pressure, OA-driven heating load with morning warm-up, utility power failure. Feeds AHU-1, the VAV reheat coils and UH-1. See [docs/HW_PLANT_DESIGN.md](docs/HW_PLANT_DESIGN.md) |
| `gen_plant` | A + B (Modbus TCP) | Natural gas generator plant: 4 × 1,000 kW Cummins gensets paralleled N+1, each a Modbus TCP unit serving the PowerCommand 3.x register map (run states, metering, NFPA 110 bitmaps, real fault codes, remote start / fault reset), a DMC 1500-style paralleling master (unit 5), ATSs with open transition to the generators and closed transition back, ALC transfer inhibits, load demand, frequency dips on load steps, overload shedding and underfrequency trips. Every other instance gets its power from an ATS (`ats: ATS-n`). See [docs/GENERATOR_DESIGN.md](docs/GENERATOR_DESIGN.md) |
| `vfd_fan` | A or B | A VFD driving a plenum fan, **ABB ACH580 or Yaskawa Z1000** (`drive_model`): keypad Hand/Off/Auto, the drive's own parameters, terminals and fault codes, hardwired or BACnet fieldbus control. See [docs/VFD_ACH580.md](docs/VFD_ACH580.md), [docs/VFD_Z1000.md](docs/VFD_Z1000.md) |

## Quick start

```bash
pip install -r requirements.txt
python -m hvacsim configs/station-1.yaml
```

Open `http://<host>:8080`. One page covers every instance on the host, with a
tab per unit showing:
- the equipment animation
- controller write status (object name → resolved object ID)
- the priority array
- fault injection
- the event log

**Demo mode** needs no network or controllers. The simulator runs mock ALC
controllers in-process, with the named objects and a 68 °F thermostat per
unit:

```bash
python -m hvacsim configs/station-1.yaml --demo --speed 20
```

Useful overrides:

```bash
python -m hvacsim configs/station-1.yaml --address 192.168.1.51/24 --controller UH-1-SIM=240115 --speed 10
```

## Config files

```
configs/station-1.yaml                       the host: router, network numbers, instances
configs/templates/weather_station.yaml       weather station objects
configs/templates/ahu_vav_econ.yaml          AHU points list (43 hardware points)
configs/templates/vav_rh.yaml                VAV box points list
configs/vav_schedule.csv                     VAV schedule: one row per box (controller device, size, zone)
configs/templates/chw_plant.yaml             chilled water plant (header sensor allocation, write settings)
configs/chw_plant/*.csv                      plant schedules: chillers, primary / secondary / condenser
                                             pumps, cooling towers, tanks, chw_branches (risers)
configs/templates/hw_plant.yaml              hot water plant (sensor allocation, write settings)
configs/hw_plant/*.csv                       plant schedules: boilers, primary pumps (one per boiler),
                                             secondary pumps, hw_branches
configs/templates/gen_plant.yaml             generator plant (Modbus TCP port, building load, write settings)
configs/gen_plant/*.csv                      generators (tag, Modbus unit ID, kW) and ats (priority, shed order)
configs/templates/hw_unit_heater.yaml        unit heater points list
configs/templates/vfd_ach580_bacnet.yaml     ACH580-style drive, BACnet fieldbus control
configs/templates/vfd_ach580_hardwired.yaml  ACH580-style drive, hardwired control
configs/templates/vfd_z1000_bacnet.yaml      Yaskawa Z1000, BACnet control (manual object list)
configs/templates/vfd_z1000_hardwired.yaml   Yaskawa Z1000, hardwired control (HVAC Initialization)
```

An instance references a template and fills in its placeholders:

```yaml
instances:
  - name: UH-1-SIM
    template: templates/hw_unit_heater.yaml
    device_instance: 4010001
    mac: 1                      # address 5001:1 on the virtual network
    vars: { n: 1 }              # template "fan_amps_{n}" -> fan_amps_1
    controller:
      device_instance: 240110
```

Anything an instance sets is merged over the template, such as `equipment.params`
and `controller`. A `commands` or `monitor` list on an instance replaces the
template's list.

## Chilled water plant

`CHP-1` in `configs/station-1.yaml` is the plant. Its equipment comes from
one CSV per type in `configs/chw_plant/`. To change the plant, edit or add
rows, for example a fourth chiller or 800-ton machines. Blank cells keep the
default, and `#` rows are skipped. Point names come from each row's tag:
`PCHWP-2` gives `pchwp2_ss`, `pchwp2_vfd_spd`, `pchwp2_amps`, `pchwp2_vfd_fb`
and `pchwp2_vfd_flt`. Each point belongs to the ALC controller in that row's
`controller_device` column.

| ALC controller | Simulator I/O device | Serves |
|---|---|---|
| 241001 | 4010041 | chiller integration (BACnet), primary pumps, chiller isolation valves, primary / CW header sensors and flow meters, `utility_power` |
| 241002 | 4010042 | secondary pumps, `schw_dp`, secondary temperatures and flow |
| 241003 | 4010043 | condenser pumps, cooling tower cells |
| 241004 | 4010044 | tanks (`tkN_temp_01` top … `_10` bottom), `dcplr_flow` (+ charging), TES header temperatures |
| 241011 / 241012 | — | `chw_remote_dp` for the East / West riser |

**Chillers (Mode B).** Each chiller is its own device (`CH-1` 4010031 …)
serving the YORK YK OptiView BAS object list.
- **Commands:** `REM RUN STOP CMD` (BV1), `REM LEAVING CH LIQ TEMP SP` (AV1)
  and `REM MOTOR CURRENT SP` (AV2).
- **Status codes:** multi-state values MV1-MV8. The BACnet value is the
  code + 1.
- **Panel buttons on the web page:** Remote/Local, the compressor switch
  (Run / Stop-Reset), and Reset for safety shutdowns.

**Utility power.** The plant page fails and restores utility power, with
an optional duration. During an outage:
- The chillers' gateways go offline, and the chillers, primary and
  condenser pumps, tower fans, tower and chiller valves and basin heaters
  lose power.
- The secondary pumps, tank valves, controllers and sensors stay on the UPS,
  so the building runs on the tanks.

When power returns:
- The drives restart if their start command is still on.
- The chillers show cycling code 5 and restart after their timers. A power
  failure does not latch a chiller.

**Coupling to AHUs.** Give an AHU `chw_from: CHP-1` and
`chw_branch: EAST`. Its cooling coil then gets the plant's secondary supply
temperature and the riser's differential pressure. The coil's flow and
return water go back to the plant, and the riser's aggregate load shrinks
by the coil's design tons.

## Weather station

`WX-1` is the host's outdoor conditions, set with `host.weather` in the host config.
- **The ALC system** reads its objects: `OA TEMP`, `OA RH`, and the optional
  `OA DEWPOINT`, `OA ENTHALPY` and `OA WETBULB`.
- **Every other simulator** takes the *true* conditions from it. For example,
  the unit heaters' `oat` follows WX-1 and is no longer set per unit.
  An instance can opt out with `use_weather: false`.
- **Sensor faults** change only what the station reports, not the physics.
  For example, a damaged radiation shield makes the temperature read high
  and the RH read low, so a controller calculating enthalpy gets it wrong.
- **Weather is fixed by default.** Set OA temp and RH on the WX-1 tab, with
  an optional change rate.

Psychrometric calculations (ASHRAE, I-P) are in `hvacsim/psychro.py`. The AHU
will reuse them.

## Unit heater sequence (what the simulator does physically)

1. ALC logic writes **BO-1 HWV_CMD = Open**. The actuator strokes in about 20 s.
2. Hot water purges the cold branch piping and heats the coil and the supply
   pipe. The aquastat on the HW supply closes at **90 °F**, which energizes the
   fan motor. The controller has no fan command.
3. The motor draws inrush current, then settles at about **2.0 A**. The CT
   reading (0-10 A) is written to **`fan_amps_{n}`**.
4. The space warms up. Space temp is written to **`zone_temp_{n}`**.
5. When the valve closes, the coil and pipe cool down. The fan **keeps running
   until the aquastat opens at 80 °F**, which takes several minutes.

## Points (per unit heater instance)

| Direction | Object | Name | Notes |
|---|---|---|---|
| ALC → sim | sim `BO-1` | HWV_CMD | Commandable, 16-level priority array, relinquish default = Closed |
| sim → ALC | object named `fan_amps_{n}` | Fan amps | 0-10 A scaled value. The input must be Out_Of_Service |
| sim → ALC | object named `zone_temp_{n}` | Zone temp | °F |
| read-only | sim `AV-101…107`, `BV-101…102` | Monitor points | True space temp, pipe temp, coil temp, valve %, motor amps, heat output, OAT, aquastat, fan running |

Writes happen **on change** (deadband 0.05 A / 0.1 °F) and on a **30 s
heartbeat**, with at least 1 s between writes of the same point.

### How object names are found

1. The simulator reads the controller's **object list and object names**
   once. This scan is shared by every instance on the host that uses that
   controller, and the result is cached.
2. If the object list can't be read, it falls back to **Who-Has** by object name.
3. If a write returns `unknown-object` (the trainee deleted or renumbered the
   object), the name is looked up again.
4. A name that doesn't exist yet shows on the web page as
   `object name 'zone_temp_3' not found in device 240110`. It's retried
   every 10 s, so it starts working as soon as the trainee creates the object.

A fixed object ID still works: use `object: analog-input,1` instead of
`object_name:` in a write.

## Changing a controller's device ID

Part of the training is designing the ALC system:

1. **Web UI:** on a unit's tab, use the *Controller link* panel. This takes effect immediately and is not saved.
2. **Command line:** `--controller UH-1-SIM=240115` (repeatable)
3. **YAML:** `controller.device_instance` on the instance. This is the permanent change.

If `controller.address` is blank, the controller is found with Who-Is
through the router. Enter a plain IP (e.g. `192.168.1.20`) to skip
discovery. It's automatically qualified with `ip_network`.

## Setting up the ALC side

- **Valve command:** the heat call drives the physical BO and a **BACnet
  binary network output** that writes `present-value` of **device 4010001,
  binary-output 1** at the priority your standard calls for.
- **Fan amps:** the 0-5 V CT input, scaled 0-10 A and named **`fan_amps_1`**,
  set **Out_Of_Service = True**. If a download puts it back in service, the
  page shows `write-access-denied - is the input still Out_Of_Service?`.
- **Zone temp:** an object named **`zone_temp_1`**. If it's commandable, set
  `priority:` on that write.
- **Network:** make sure the WebCTRL network number for the lab IP network
  equals `host.ip_network`.

## Faults the instructor can inject

| Group | Faults |
|---|---|
| Valve | Stuck closed / actuator failed, stuck open, passing (leak-by) |
| Hot water | No hot water from plant, low supply temp (85 °F default, just under the aquastat setpoint) |
| Aquastat | Failed open, failed closed, setpoint drifted |
| Motor | Disconnect off, open winding, locked rotor (trips the protector and retries), overloaded (cycles on the protector), worn bearings |
| CT | Reads 0 A, reads full scale |
| Space sensor | Offset, open circuit |

## Change orders

Points are defined entirely in YAML. See
[docs/CHANGE_ORDERS.md](docs/CHANGE_ORDERS.md).

## Lab deployment

- **One process per host, one IP per host, port 47808.** Add instances to the
  host config instead of running more processes.
- **Device instance plan:** keep simulator instances out of the ALC range,
  e.g. `40SSUUU` (SS = station, UUU = unit, 000 = router).
- **Virtual network plan:** e.g. 5000 + station number.

## Testing

```bash
python -m pytest -q
```

The tests run the router, instances and mock controllers on an in-process
network. They check:
- discovery through the router
- writes to individual units at a priority
- object-name lookup by object list and by Who-Has
- missing names
- out-of-service rejection
- retargeting a unit to another controller
- relinquish

**Testing on a real network:** `tools/mock_alc.py` puts a fake controller
240110 on the network, with `fan_amps_1/2`, `zone_temp_1/2` and thermostats
writing to devices 4010001/2:

```bash
python tools/mock_alc.py --address 192.168.1.20/24
```

Run it on a **different machine** from the simulator host. Several BACnet
programs on one Linux machine don't reliably share broadcasts on port 47808.

## Hot water plant

`HWP-1` in `configs/station-1.yaml` is the plant, built the same way as the
chilled water plant from one CSV per type in `configs/hw_plant/`. Each boiler
needs exactly one primary pump row naming it in the `boiler` column; the
simulator refuses to start and names the missing row otherwise.

| ALC controller | Simulator I/O device | Serves |
|---|---|---|
| 242001 | 4010061 | boiler integration (BACnet), primary pumps, `bN_alarm`, `louver` / `louver_open`, `gas_press_ok`, `boiler_estop`, `boiler_rm_temp`, `utility_power` |
| 242002 | 4010062 | secondary pumps, `phws_temp`, `phwr_temp`, `shws_temp`, `shwr_temp`, `shw_flow`, `hw_dcplr_flow` (+ = supply → return), `hw_dp`, `hw_loop_press` |
| 242011 / 242012 | 4010063 / 4010064 | East / West branch: `hw_remote_dp`, `hw_eob_temp`, `hw_eob_vlv` (3-way valve) |

**Boilers (Mode B).** Each boiler is its own device (`B-1` 4010051 …).
The ALC writes `BMS ENABLE` (BV1) and `BMS FIRING RATE` (AV1, %). The boiler
runs its own pre-purge, ignition, low-fire hold, modulation and post-purge,
and reports `STATUS`, `LOCKOUT CODE` and `BLOCKING CODE` as multi-state
values (BACnet value = code + 1). **The object names and codes are
assumptions** until the Crest BMS points list is available (see the design
doc, section 13). The web page has the boiler display buttons: BMS / Local
and Reset for lockouts.

**Power and emergency stop.** A utility outage drops the boilers, all pumps
and the louver; nothing latches. The boiler room E-stop on the plant page
removes boiler power only.

**Coupling.** Give an AHU, a VAV schedule entry or a unit heater
`hw_from: HWP-1` and `hw_branch: EAST`. Its coils then get the plant's
secondary supply temperature and the branch DP, their flow and return water
go back to the plant, and the branch's aggregate load shrinks by their
design flow (10 MBH per gpm).

## Generator plant

`GEN-PLANT-1` is four 1,000 kW natural gas generator sets paralleled N+1, a
paralleling master and four ATSs (`configs/gen_plant/*.csv`).

**Modbus TCP.** The simulator listens on the host address, port 502
(`plant.modbus.port`), like a Modbus TCP gateway in front of the
PowerCommand 3.3 RS-485 ports:
- **Unit IDs 1–4:** GEN-1…4, PowerCommand 3.x register map from the Cummins
  manual A029X159.
  - 40010 switch position and 40011 run state
  - 40012 / 40013 fault code and type
  - 40016 / 40017 NFPA 110 bitmaps
  - 40018-40044 metering
  - 40061-40075 engine data
  - **40300 Remote Start and 40301 Fault Reset are writable**
  - 40400+ fault bitmaps
- **Unit ID 5:** the paralleling master, DMC 1500 map: bus and utility
  metering, ATS 1-n blocks from 40257.
- **Not in the DMC map:** load shed level, sets online, capacity and bus
  load are at 40900-40904.
- **Requests:** 1-40 registers per request, functions 03 / 04 / 06 / 16.
- **Exceptions:** 02 for an address that isn't implemented, 0B for a unit
  that does not answer.
- **If port 502 can't be opened** (Linux without privileges), the
  simulator logs the free port it used instead.

**ATSs (hardwired to ALC 243001).** Each ATS has these points:
- `atsN_norm_pos` / `atsN_emer_pos` (position contacts)
- `atsN_xfer_inhibit` (ALC output)

The controller also gets `utility_avail`, `gen_running` and `load_shed`.

| ATS | Feeds | Shed order |
|---|---|---|
| ATS-1 | UPS / critical: CHP-1 secondary pumps, controllers | never |
| ATS-2 | CHP-1 | last |
| ATS-3 | HWP-1 | second |
| ATS-4 | AHU-1, VAVs, unit heaters, VFDs, building load (600 / 200 kW) | first |

An instance takes its power from an ATS with `ats: ATS-n`.

**What happens on an outage:**
1. All sets in Auto start. The first to reach rated voltage closes to the
   dead bus, and the others synchronize and close.
2. The ATSs transfer in priority order (open transition), unless the ALC
   holds their transfer inhibit.
3. Load steps dip the bus frequency (about 3.5 Hz for a 25 % step).
4. On overload, the master sheds by shed order. Frequency held below 55 Hz
   trips the sets on 1448.
5. Load demand stops surplus sets but keeps N+1.

**When utility returns:** after 30 min (per ATS), each ATS retransfers with
a closed transition (make before break). The sets then cool down for 5 min
and stop.

## Drives: choosing the make and model

Every drive in the simulator sits behind one interface
([docs/DRIVE_INTERFACE_DESIGN.md](docs/DRIVE_INTERFACE_DESIGN.md)). That
covers the standalone drives, the AHU fan array, the plant pumps and the tower
fans.
- **Equipment wires roles, not terminals.** The roles are start, safeties,
  damper interlock, speed reference, running, fault, speed feedback and damper
  output.
- **Each drive maps the roles to its own terminals.** Swapping the drive keeps
  the trainees' ALC points.
- **Registered drives:** `ach580` (ABB, default) and `z1000` (Yaskawa).

| Where | Setting |
|---|---|
| Standalone drive | template `vfd_ach580_*` or `vfd_z1000_*`, or `equipment.params.drive_model` |
| AHU fan array | `equipment.params.fan_drive_model: z1000` |
| Plant pumps / tower fans | CSV column `drive_model` |

Parameter overrides use the drive's own parameter names (`accel_time_1` on an
ACH580, `c1_01` on a Z1000).

**Adding a drive:** one file in `models/components/drives/` plus its templates.
It must pass the conformance tests in `tests/test_drives.py`, which run against
every registered drive. `tests/test_golden.py` replays recorded traces, so
refactoring can't silently change drive behaviour.

## Chillers, boilers and generators: choosing the make and model

Each chiller, boiler and generator set sits behind its family's interface
([docs/EQUIPMENT_INTERFACE_DESIGN.md](docs/EQUIPMENT_INTERFACE_DESIGN.md)).
The `model` column in the plant CSV picks the vendor, row by row:

| Plant | CSV | Registered models (default first) |
|---|---|---|
| Chilled water | `chillers.csv` | `york_yk` |
| Hot water | `boilers.csv` | `lochinvar_crest` |
| Generator | `generators.csv` | `cummins_pcc33` |

**What a swap changes:**
- the equipment's controller: its sequence, panel keys, status texts and
  fault codes;
- its integration, i.e. the object list or register map the trainees map in
  the ALC, and the protocol. A Modbus vendor in the chilled or hot water plant
  gets that plant's own Modbus TCP server (port 5021 / 5022 unless
  `plant.modbus.port`), with a `unit_id` column on its row.

**What a swap doesn't change:**
- the plant's physics, because every vendor in a family shares the same
  thermal or engine core;
- every hardwired ALC point (valves, flow meters, drives, louver, ATSs).

**Vendor settings:**
- Set them with the vendor's own names under `chiller:`, `boiler:` or
  `genset:` in the plant template's settings, flat or per model, e.g.
  `chiller: { york_yk: { anti_recycle_min: 15 } }`. A same-named CSV column
  works too.
- The boiler plant's `boiler_bms_mode` is `rate` (enable + firing rate) or
  `setpoint`, if the vendor supports it.

**Instructor faults:**
- Each vendor keeps its own fault names (`ch1_vsd_fault`,
  `b1_gateway_offline`).
- Every family also has generic names that work on any vendor (`ch1_starter_fault`,
  `b1_comm_loss`).

**Adding a vendor:**
- One file in `models/components/chillers/`, `boilers/` or `gensets/`.
- It must pass `tests/test_vendors.py`, which runs against every registered
  vendor.
- The golden traces (`tests/test_golden.py`) protect the existing vendors.

## Code layout

```
hvacsim/
  models/base.py         EquipmentModel: inputs, signals, faults, params (no BACnet)
  models/unit_heater.py  HW unit heater physics and sequence
  models/components/drives/  Drive interface + registered drives (ach580.py, z1000.py): every
                         drive in the simulator is built here and wired by role
  models/components/vfd.py  ACH580 drive engine (behind drives/ach580.py)
  models/vfd_fan.py      Standalone drive + motor + fan
  models/weather_station.py  Weather station
  models/ahu.py          VAV AHU (fan array, coils, safeties, duct with VAV boxes)
  models/vav.py          VAV box with reheat and its zone
  models/chw_plant.py    Chilled water plant (hydraulics, TES, towers, building risers)
  models/components/vendor.py    Shared vendor interface, registry, BACnet / Modbus integration builder
  models/components/chillers/    Chiller interface, shared thermal core, york_yk.py (YORK YK + OptiView objects)
  models/components/hydronic.py  Pumps, isolation valves, pump/system solves, stratified tank, R-134a
  models/components/tower.py     Cooling tower cell with basin
  models/hw_plant.py     Hot water plant (boilers, primary/secondary, decoupler, branches)
  models/components/boilers/     Boiler interface, shared HX core, lochinvar_crest.py (Crest + BMS objects, assumed)
  models/gen_plant.py    Generator plant: paralleling master, ATSs, bus, load shedding, power to instances
  models/components/gensets/     Genset interface, shared engine core, cummins_pcc33.py (PowerCommand 3.3 registers)
  modbus_tcp.py          Modbus TCP server (gateway with unit IDs) and client
  plant_runner.py        A plant = several BACnet devices (chillers / boilers + I/O devices) on one model
  models/components/airside.py  Actuators, valves, dampers, water coils, steam humidifier
  psychro.py             Moist-air psychrometrics (I-P)
  config.py              Host config, templates, {placeholders}, validation
  bacnet_io.py           Router, per-instance devices, name lookup, controller writes
  runner.py              Host + instances, simulation loop, demo mode
  mockalc.py             Mock ALC controller (demo mode, tests, tools/mock_alc.py)
  web.py, static/        Web page; static/views/<type>.js draws each equipment type
configs/                 Host configs and equipment templates
tools/mock_alc.py        Fake ALC controller on a real network
tests/                   pytest suite
```

To add new equipment:
1. Subclass `EquipmentModel`.
2. Register it in `hvacsim/models/__init__.py`.
3. Write a template.
4. Add a view in `hvacsim/static/views/<type>.js`.

Template commands can target model inputs (`input:`) or parameters
(`param:`, for writable objects such as the drive's ACCEL1 TIME). A
`demo.writes` block tells `--demo` what the mock controller should write.
