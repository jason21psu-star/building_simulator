# HVAC Equipment Training Simulators

Virtual HVAC equipment that behaves like the real thing on a BACnet/IP
network. Trainees program real Automated Logic (ALC) controllers. The
controller commands the simulator over BACnet, and the simulator writes its
sensor values back into the controller's out-of-service inputs.

```
 ┌─────────────────────────┐   WriteProperty BO-1 (network output)   ┌──────────────────────┐
 │  ALC controller 240110  │ ──────────────────────────────────────▶ │  UH-1-SIM  4010001   │
 │  (trainee's logic)      │                                         │  physics + safeties  │
 │                         │ ◀────────────────────────────────────── │                      │
 │  AI:1 fan amps (OOS)    │   WriteProperty AI:1, AV:1 present-value│  web UI :8080        │
 │  AV:1 space temp        │                                         │  fault injection     │
 └─────────────────────────┘                                         └──────────────────────┘
```

## Equipment available

| Type | Mode | Description |
|---|---|---|
| `hw_unit_heater` | A (virtual I/O) | Hot water unit heater, 2-position valve, aquastat fan interlock (90 °F on / 80 °F off), 0-10 A CT on the fan motor |

## Quick start

```bash
pip install -r requirements.txt
python -m hvacsim configs/uh-1.yaml
```

Open `http://<host>:8080` to see the equipment animation, BACnet point status
and priority array, event log, and fault injection.

Useful overrides (no YAML edit needed):

```bash
python -m hvacsim configs/uh-1.yaml --controller-id 240110 --address 192.168.1.51/24 --speed 10
```

## Unit heater sequence (what the simulator does physically)

1. ALC logic writes **BO-1 HWV_CMD = Open**. The actuator strokes in about 20 s.
2. Hot water purges the cold branch piping and heats the coil and the supply
   pipe. The aquastat on the HW supply closes at **90 °F**, which energizes the
   fan motor. The controller has no fan command.
3. The motor draws inrush current, then settles at about **2.0 A**. The CT
   reading (0-10 A) is written to **ALC AI:1**.
4. The space warms up. Space temp is written to **ALC AV:1**.
5. When the valve closes, the coil and pipe cool down. The fan **keeps running
   until the aquastat opens at 80 °F**, which takes several minutes.

## Points

| Direction | Object | Name | Notes |
|---|---|---|---|
| ALC → sim | sim `BO-1` | HWV_CMD | Commandable, 16-level priority array, relinquish default = Closed |
| sim → ALC | ALC `AI:1` | Fan amps | 0-10 A scaled value. AI must be Out_Of_Service |
| sim → ALC | ALC `AV:1` | Space temp | °F |
| read-only | sim `AV-101…107`, `BV-101…102` | Monitor points | True space temp, pipe temp, coil temp, valve %, motor amps, heat output, OAT, aquastat, fan running |

Writes to the controller happen **on change** (deadband 0.05 A / 0.1 °F) and on
a **30 s heartbeat**, with at least 1 s between writes of the same point. This
keeps traffic low when many simulators share the lab network.

## Changing the controller's device ID

Part of the training is designing the ALC system, so the target controller
can be changed three ways. The input object IDs (AI:1, AV:1) stay fixed.

1. **Web UI:** in the *Controller link* panel, enter the device ID and click Apply. This takes effect immediately and is not saved.
2. **Command line:** `--controller-id 240115`
3. **YAML:** `controller.device_instance` in the config file. This is the permanent change.

If `controller.address` is blank, the simulator finds the controller with
**Who-Is/I-Am**. Enter an IP address only if broadcasts can't reach it,
for example across subnets without a BBMD.

## Setting up the ALC side

- **Valve command:** in the trainee's logic, the heat call drives both the
  physical BO and a **BACnet binary network output microblock** that writes
  `present-value` of **device 4010001, binary-output 1**. Use whatever
  priority your standard calls for. The simulator's web page shows which
  priority slot is active, which helps when a point "won't release."
- **Fan amps:** AI:1 is configured for a 0-5 V transducer scaled 0-10 A.
  Set AI:1 **Out_Of_Service = True** so the simulator can write
  Present_Value. If a download or reboot puts it back in service, the
  simulator page shows `write-access-denied - is the input still Out_Of_Service?`.
- **Space temp:** AV:1. If the AV is commandable in your program, set
  `priority:` on that write in the YAML.

## Faults the instructor can inject

| Group | Faults |
|---|---|
| Valve | Stuck closed / actuator failed, stuck open, passing (leak-by) |
| Hot water | No hot water from plant, low supply temp (85 °F default, just under the aquastat setpoint) |
| Aquastat | Failed open, failed closed, setpoint drifted |
| Motor | Disconnect off, open winding, locked rotor (trips the protector and retries), overloaded (cycles on the protector), worn bearings |
| CT | Reads 0 A, reads full scale |
| Space sensor | Offset, open circuit |

## Change orders (adding or removing points)

Points are defined entirely in the YAML. Code changes are not needed. See
[docs/CHANGE_ORDERS.md](docs/CHANGE_ORDERS.md) for worked examples:
- adding a HW supply temp sensor
- adding a fan start/stop relay
- monitoring the aquastat as a BI

## Lab deployment

- **One IP per simulator, port 47808.** That's how real equipment looks to
  WebCTRL, and it lets Who-Is work. On a Linux host, add secondary addresses
  to the lab NIC (`ip addr add 192.168.1.51/24 dev eth0`, …) and run one
  process per config. On Windows, add addresses under the adapter's
  Advanced TCP/IP settings.
- **Device instance plan:** keep simulator instances out of the ALC range,
  e.g. `40SSUUU` (SS = station, UUU = unit), so UH-1 at station 1 is `4010001`.
- **Web port:** give each simulator its own web port if they share a host.

## Testing without a controller

`tools/mock_alc.py` stands in for the ALC controller: AI:1 out of service,
AV:1, a 68 °F setpoint and a heating logic block that writes the
simulator's BO-1.

Run the simulator and the mock controller in two terminals:

```bash
python -m hvacsim configs/uh-1.yaml --address 192.168.1.51/24 --controller-address 192.168.1.20
```

```bash
python tools/mock_alc.py --address 192.168.1.20/24 --sim-address 192.168.1.51 --priority 10
```

Automated tests (model physics + a full BACnet loop on localhost):

```bash
python -m pytest -q
```

YABE (free) is useful for browsing the simulator's objects and writing BO-1
by hand at different priorities.

## Code layout

```
hvacsim/
  models/base.py         EquipmentModel: inputs, signals, faults, params (no BACnet)
  models/unit_heater.py  HW unit heater physics and sequence
  bacnet_io.py           BACnet server objects + client writes into the controller
  runner.py              Loads YAML, runs the simulation loop
  web.py, static/        Instructor/trainee web page
configs/uh-1.yaml        Unit heater points list and parameters
tools/mock_alc.py        Fake ALC controller for testing
tests/                   pytest suite
```

To add new equipment, subclass `EquipmentModel`, declare its INPUTS, SIGNALS,
FAULTS and DEFAULTS, register it in `hvacsim/models/__init__.py`, and write a
YAML points list.
