# Change orders

Every point between the simulators and the controllers lives in YAML. A
change order is a YAML edit followed by a restart of the simulator host.

- **Change every unit of a type:** edit the template, `configs/templates/hw_unit_heater.yaml`.
- **Change one unit:** add the change to that instance in the host config, `configs/station-1.yaml`.

Available signals and inputs are listed at the bottom of each template.

---

## CO-1: Add a HW supply temperature sensor (all unit heaters)

*Owner wants to trend the HW supply pipe temperature at every unit heater.*

Field work for the trainee: create an input named `hw_supply_temp_1`
(`_2`, …) and set it Out_Of_Service.

Template change, under `controller.writes`:

```yaml
    - name: HW supply temp
      signal: pipe_temp
      object_name: "hw_supply_temp_{n}"
      demo_object: "analog-input,{n}0"   # only used by --demo
      deadband: 0.5
      heartbeat: 30
```

---

## CO-2: Add a fan start/stop relay on UH-2 only

*Owner wants the controller to be able to stop UH-2's fan (unoccupied
lockout), with the aquastat still in series as a safety.*

Field work for the trainee: add a network output in the logic that writes
the simulator's new **BO-2** on device 4010002.

Instance change in the host config. A `commands` list on an instance
replaces the template's list, so repeat BO-1:

```yaml
  - name: UH-2-SIM
    template: templates/hw_unit_heater.yaml
    device_instance: 4010002
    mac: 2
    vars: { n: 2 }
    controller:
      device_instance: 240110
    equipment:
      params:
        fan_control: controller_and_aquastat   # motor needs BOTH the BO and the aquastat
    commands:
      - { object: "binary-output,1", name: HWV_CMD, input: valve_cmd, active_text: Open, inactive_text: Closed }
      - { object: "binary-output,2", name: FAN_CMD, input: fan_cmd,   active_text: Start, inactive_text: Stop }
```

Training point: the controller can now command the fan on, but it still
won't run until the aquastat makes.

---

## CO-3: Monitor the aquastat contact

*Owner wants an alarm when the aquastat is open during a call for heat.*

Field work for the trainee: create a BI named `aquastat_1` (Out_Of_Service).

```yaml
    - name: Aquastat status
      signal: aquastat_closed
      object_name: "aquastat_{n}"
      demo_object: "binary-input,{n}"
      heartbeat: 30
```

Binary signals are written as active/inactive. The deadband is ignored.

---

## CO-4: Add another unit heater (UH-3)

Add an instance to the host config with a new device instance, MAC and `n`:

```yaml
  - name: UH-3-SIM
    template: templates/hw_unit_heater.yaml
    device_instance: 4010003
    mac: 3
    vars: { n: 3 }                    # fan_amps_3, zone_temp_3
    controller:
      device_instance: 240110
```

It appears in WebCTRL as device 4010003 at address 5001:3 behind the
simulator router.

---

## CO-5: Rename or remove a point

- **Rename:** if the trainee's program uses a different object name, change
  `object_name`. Names can also differ per instance by overriding
  `controller.writes` on that instance.
- **Remove:** delete the entry or comment it out.

---

## Re-addressing a controller

The trainee picks each controller's device ID. Change
`controller.device_instance` on the instance, pass
`--controller UH-1-SIM=240115`, or use the web page's *Controller link*
panel. Object names stay the same, and the simulator scans the new
controller's object list.
