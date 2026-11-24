# VAV box simulator (vav_rh)

Single-duct VAV box with hot water reheat and no fan, per the ALC `vav_rh`
sequence. Equipment type `vav_rh` (`hvacsim/models/vav.py`), template
`configs/templates/vav_rh.yaml`. Boxes are listed in a CSV schedule.

```
AHU-1 duct ─► flow cross ─► damper + integrated actuator ─► HW reheat (2-way) ─► DAT ─► diffusers ─► zone
                                                                                   zone sensor: temp, setpoint adjust, override, RH
```

## Points (identical names in every VAV controller)

| ALC point | Type | Name | Simulator side |
|---|---|---|---|
| Zone Temp | AI | `zone_temp_1` | written to the controller |
| Zone Humidity | AV | `zone_humid_1` | written to the controller |
| Discharge Air Temp | AI | `da_temp_1` | written to the controller |
| Airflow | AI | `airflow_1` | written to the controller, **cfm** |
| Zone Setpoint Adjust | AI | `stpt_adj_1` | written to the controller, **± °F offset** |
| Zone Override | BI | `override_1` | written to the controller, **momentary** (closed about 3 s per push) |
| Zone Damper | AO | `dmpr_1` | simulator AO-1, **damper position 0-100 %** |
| Reheating Valve | AO | `rh_vlv_1` | simulator AO-2, **% of 10 V** (2-10 V, fails closed) |

The `_1` suffix is the same in every controller; it doesn't change from box
to box. Software points (setpoints, airflow setpoint, heating mode,
environmental index, alarms, schedule) live in the ALC program.

## Physical behaviour

| Part | Model |
|---|---|
| Damper | Integrated actuator, **154 s** for full stroke, no spring return: it fails in place. Round single-blade damper with 1 % leakage. Fully open, the box passes 1.6 × its rated cfm at 1.0 in. inlet static; 0.25 in. at rated flow downstream (coil, duct, diffusers) |
| Airflow | Comes from damper position and inlet static, so **pressure independence is the trainee's airflow loop**. Written as measured cfm (about 1 % noise) |
| Reheat | 2-way equal-percentage valve, 2-10 V, spring closed, 90 s stroke, 2 gpm design, 140 °F water from the AHU's hot water. Design point: 40 % of box max from 55 → 95 °F. A small coil with < 1 gal of water |
| Zone | Lights 1.0 W/ft² and equipment 1.5 W/ft² when occupied (0.2 / 0.5 unoccupied), 250 Btu/h sensible + latent and CO2 per person. Perimeter zones (north/south/east/west) add envelope loss to the weather station's OAT and solar gain by orientation and time of day. The conference room fills only during meetings (9-11, 13-15:30). Interior zones have internal loads only |
| Occupancy | People follow the building schedule: the AHU's `occ_days` / `occ_start` / `occ_end` when served by an AHU |
| Setpoint adjust | The occupant moves a ± slider. The range is selectable **1-5 °F** (`stpt_adj_range`, per box in the CSV) |
| Override button | Each push adds **30 min**, up to **120 min**. The contact closes momentarily so the ALC program sees each push. The simulator keeps the same timer so people are actually in the zone during an override |

## Tied to the AHU

`served_by: AHU-1` puts the boxes in AHU-1's duct:

- **Duct static:** the AHU's duct static solve includes every box's damper,
  so closing boxes raises duct static.
- **Inlet static:** depends on `duct_position` (0 = at the AHU, 0.75 = at the
  static sensor, 1 = end of the duct). Boxes upstream of the sensor see more
  static, boxes beyond it less.
- **Supply air:** boxes receive the AHU's supply air temperature, humidity
  and CO2.
- **Return air:** the AHU's return air temperature, humidity and CO2 are the
  airflow-weighted mix of the simulated zones and the remaining aggregate.
- **Aggregate VAV:** the AHU still models the rest of its 24,000 cfm design
  as aggregate VAV (design minus the sum of the boxes' `box_max_cfm`).

## CSV VAV schedule

The default 6-box configuration is `configs/vav_schedule.csv`. A custom
configuration is another CSV in the same format, named in the host config:

```yaml
  - schedule: vav_schedule.csv        # path relative to the host config
    template: templates/vav_rh.yaml
    served_by: AHU-1
```

| Column | Meaning |
|---|---|
| `tag` | Instance name (required). Rows whose tag starts with `#` are skipped |
| `sim_device` | Simulator BACnet device instance |
| `mac` | Address on the host's virtual network (1-254) |
| `controller_device` | **ALC VAV controller device ID** the box writes into |
| `controller_address` | Optional: controller IP, to skip Who-Is |
| `description` | Free text |
| any other column | An equipment parameter: `zone_type` (interior/north/south/east/west/conference), `area_sqft`, `box_max_cfm`, `people`, `reheat_gpm`, `duct_position`, `stpt_adj_range`, `stroke_s` |

Blank cells keep the template value.

Default schedule:

| Tag | Zone | Box max cfm | Controller |
|---|---|---|---|
| VAV-1 | North, 1,000 ft² | 1,200 | 240901 |
| VAV-2 | South, 1,000 ft² | 1,200 | 240902 |
| VAV-3 | East, 1,000 ft² | 1,200 | 240903 |
| VAV-4 | West, 1,000 ft² | 1,200 | 240904 |
| VAV-5 | Interior, 1,500 ft² | 1,500 | 240905 |
| VAV-6 | Conference, 600 ft² | 1,000 | 240906 |

## Faults

| Area | Faults |
|---|---|
| Damper | Stuck at a position, actuator failed (doesn't move), linkage slipped (half travel) |
| Airflow sensor | Tube pinched (reads 30 %), tubes reversed (reads 0), dirty probe (reads -20 %) |
| Reheat | Valve stuck, actuator failed (springs closed), valve leaking through, coil air-bound (no heat with the valve open) |
| Zone | Extra load (door open, equipment; + or − Btu/h) |
| Sensors | Zone sensor in the sun, out of calibration, failed; humidity drift; DAT offset |
| Communication | Box offline (no BACnet response) |

Upstream problems show up on their own: low duct static from the AHU starves
the box (full open, not enough airflow), and warm supply air from the AHU
can't cool the zone.

## Demo mode

`--demo` gives each box a mock controller (240901-240906) running a minimal
pressure-independent sequence: 75/70 °F plus setpoint adjust, airflow 30-100 %
of box max on the cooling loop, damper PI on airflow, and reheat with a 95 °F
DAT limit. It's only there to show the simulator working; trainees write the
real program.
