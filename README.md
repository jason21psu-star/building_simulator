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
           ├── CH-1…3    device 4010031-033  address 5001:31-33  YORK YK OptiView BAS objects (Mode B)
           └── CHP-1 I/O device 4010041-044  address 5001:41-44  plant BO/AO terminals for ALC 241001-241004
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
| `vfd_fan` | A or B | ABB ACH580-style VFD driving a plenum fan: keypad Hand/Off/Auto, ACH580 parameters, fault codes, hardwired (DI1/AI1/RO/AO) or BACnet fieldbus control. See [docs/VFD_ACH580.md](docs/VFD_ACH580.md) |

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
configs/templates/hw_unit_heater.yaml        unit heater points list
configs/templates/vfd_ach580_bacnet.yaml     ACH580-style drive, BACnet fieldbus control
configs/templates/vfd_ach580_hardwired.yaml  ACH580-style drive, hardwired control
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

## Code layout

```
hvacsim/
  models/base.py         EquipmentModel: inputs, signals, faults, params (no BACnet)
  models/unit_heater.py  HW unit heater physics and sequence
  models/components/vfd.py  ACH580-style drive component (reused by the AHU fan array)
  models/vfd_fan.py      Standalone drive + motor + fan
  models/weather_station.py  Weather station
  models/ahu.py          VAV AHU (fan array, coils, safeties, duct with VAV boxes)
  models/vav.py          VAV box with reheat and its zone
  models/chw_plant.py    Chilled water plant (hydraulics, TES, towers, building risers)
  models/components/york_yk.py   YORK YK chiller + OptiView BAS object list / status codes
  models/components/hydronic.py  Pumps, isolation valves, pump/system solves, stratified tank, R-134a
  models/components/tower.py     Cooling tower cell with basin
  plant_runner.py        A plant = several BACnet devices (chillers + I/O devices) on one model
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
