# Change orders

Every point between the simulator and the controller lives in the
simulator's YAML file. A change order is a YAML edit followed by a restart
of that simulator. The physics doesn't change, only what is wired to the
controller.

The signals and inputs each equipment model offers are listed at the bottom
of its config file.

---

## CO-1: Add a HW supply temperature sensor

*Owner wants to trend the HW supply pipe temperature at UH-1.*

Field work for the trainee: wire a sensor to a spare input, for example
**AI:2**, then set it Out_Of_Service.

Simulator change: add a write under `controller.writes`.

```yaml
    - name: HW supply temp
      signal: pipe_temp
      object: analog-input,2
      deadband: 0.5
      heartbeat: 30
```

---

## CO-2: Add a fan start/stop relay (fan command + aquastat)

*Owner wants the controller to be able to stop the fan (e.g. unoccupied
lockout), with the aquastat still in series as a safety.*

Field work for the trainee: add a BO and a network output in the logic that
writes the simulator's new **BO-2**.

Simulator changes:

```yaml
equipment:
  params:
    fan_control: controller_and_aquastat   # motor needs BOTH the BO and the aquastat

commands:
  - object: binary-output,2
    name: FAN_CMD
    input: fan_cmd
    description: Fan start/stop relay
    active_text: Start
    inactive_text: Stop
    relinquish_default: false
```

Training point: the controller can now command the fan on, but it still
won't run until the aquastat makes. Fan status (amps) shows the difference
between commanded and proven.

---

## CO-3: Monitor the aquastat contact

*Owner wants an alarm when the aquastat is open during a call for heat.*

Field work for the trainee: wire an auxiliary contact to **BI:1** on the
controller (Out_Of_Service).

```yaml
    - name: Aquastat status
      signal: aquastat_closed
      object: binary-input,1
      heartbeat: 30
```

Binary signals are written as active/inactive. The deadband is ignored.

---

## CO-4: Remove a point (value engineering)

Delete or comment out the entry. For example, to remove space temp from the
simulator because a room sensor is being installed:

```yaml
#    - name: Space temp
#      signal: space_temp
#      object: analog-value,1
```

---

## Re-addressing the controller

Controller object IDs stay fixed, but the trainee picks the controller's
device ID. Change `controller.device_instance`, pass `--controller-id`, or
use the web page's *Controller link* panel. See the README.
