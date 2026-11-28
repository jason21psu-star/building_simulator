# Hot water plant simulator: physical design (for review)

Status: **built** as equipment type `hw_plant` (model
`hvacsim/models/hw_plant.py`, boiler `hvacsim/models/components/boilers/lochinvar_crest.py` (behind the boiler interface, [EQUIPMENT_INTERFACE_DESIGN.md](EQUIPMENT_INTERFACE_DESIGN.md)),
template `configs/templates/hw_plant.yaml`, schedules `configs/hw_plant/*.csv`).
This records the 11 design questions answered in review; section 14
describes how each part is simulated.

**Source documents:** the Crest manuals now drive the boiler: see
[BOILER_CREST.md](BOILER_CREST.md). Plant-side values not from a manual are
still marked *(A)*.

## 1. Plant arrangement

Primary/secondary with a common-pipe decoupler.

```
   B-1 ◄── PHWP-1 (VFD) ◄──┐                          ┌──► SHWP-1..3 (VFD, headered) ──► secondary supply hdr ──► EAST / WEST branches
   B-2 ◄── PHWP-2 (VFD) ◄──┤ primary return hdr       │                                                              │
   B-3 ◄── PHWP-3 (VFD) ◄──┤                          │                                       3-way valve at the end of each branch
    │                      │                          │                                                              │
    └──► primary supply hdr ─┬────────────────────────┘                                                              │
                             │  DECOUPLER (common pipe, bidirectional flow meter)                                    │
         primary return hdr ◄┴──────────────────────────────── secondary return hdr ◄────────────────────────────────┘
```

- **Primary pumps** are dedicated: PHWP-N serves B-N only, with a check valve and no boiler isolation valve.
- **Decoupler flow:**
  - **Primary > secondary:** supply water short-circuits to the boiler return. The return gets hotter and condensing efficiency drops.
  - **Secondary > primary:** return water mixes into the building supply, so the supply temperature drops.
  - Sign convention: **+ = supply → return** (primary excess).

## 2. Boilers (B-1..3): Lochinvar Crest FCB4000N, Mode B

The boilers now follow the Crest manuals. Their sequence, limits, codes,
BACnet object list, BAS timer and the configuration bits the ALC must write
are in **[BOILER_CREST.md](BOILER_CREST.md)** (design:
[CREST_DESIGN.md](CREST_DESIGN.md)). The *(A)* values this section used to
carry are replaced there.

- **Rating:** 3,999 MBH input each, condensing, 20:1 turndown (200-3,999 MBH).
- **Design flow:** about 400 gpm at a 20 °F ΔT.
- **Control:** BMS firing rate by default (`boiler_bms_mode: rate`), or BMS
  setpoint. There is no Lochinvar cascade. The trainees' loop holds supply
  temperature, reset, staging and rotation.

### Efficiency
- Combustion efficiency depends on return water temperature: about 95–96 % at 80 °F return, falling to about 87 % above 140 °F (dew point about 130 °F).
- Low firing rates improve it slightly. Heat exchanger scaling lowers it and raises the flue temperature.
- Condensate flow is calculated (about 0 above dew point).

## 3. Pumps (all on ACH580 VFDs, hardwired)

| Pumps | Arrangement | Rating | Default operation |
|---|---|---|---|
| PHWP-1..3 | Dedicated, one per boiler | 400 gpm, 40 ft, 7.5 hp | Constant speed at the balancer's speed (boiler design flow). The ALC may reduce it down to the boiler's minimum flow |
| SHWP-1..3 | Headered, 2 duty + 1 standby | 600 gpm, 80 ft, 20 hp | Speed on plant DP, with remote DP trimming the setpoint |

- **Primary pump below minimum flow:** below the boiler's minimum, the flow switch opens and the boiler blocks (non-latching).
- **Primary speed vs secondary flow:** matching primary speed to secondary flow is how trainees avoid decoupler short-circuiting.
- **Boiler pump request:** the boiler's BACnet output is available for the ALC to use for pump post-purge.

## 4. Branches and end-of-branch valves

- **Branches:** EAST (55 %) and WEST (45 %).
- **At the far end of each branch:**
  - a remote DP sensor, used to trim the plant DP setpoint;
  - an end-of-branch supply temperature sensor;
  - a modulating 3-way valve (ALC analog output). Fully open it bypasses about 5 % of the branch design flow, to keep the pipes at temperature.
- There is no secondary minimum-flow bypass at the plant.

## 5. Boiler room

- **Combustion air louver:** one for the room.
  - Opened by the ALC when any boiler is enabled. It has an end switch.
  - The boilers will not fire until it is proven (interlock wired to each boiler).
- **Building gas pressure switch:** low/high, to an ALC input.
- **Emergency shutoff switch:** kills boiler power and is monitored by the ALC. It can be triggered from the web page.
- **Room temperature:** a boiler room temperature sensor.
- **Per boiler, hardwired:** an alarm relay to an ALC digital input. The flow switch and LWCO are read by the boiler and also reported over BACnet.

## 6. Sensors

| Location | Sensors |
|---|---|
| Primary | Primary supply and return header temperatures |
| Secondary | Secondary supply and return temperatures; secondary flow meter (building load in MBH); plant DP across the secondary headers (hardwired, pump speed control) |
| Decoupler | Bidirectional decoupler flow meter (4–20 mA, + = supply → return) |
| Loop | Loop pressure at the expansion tank connection |
| Branches | Remote DP and end-of-branch supply temperature at the end of each branch |
| Room / power | Boiler room temperature, gas pressure switch, emergency stop status, `utility_power` (True = normal) |
| OA | Outdoor air from WX-1, already on the ALC system |

- **Consistency check:** primary flow = secondary flow + decoupler flow. Decoupler temperatures follow from the mixing.

## 7. Building load

- **Design:** 8,000 MBH at 0 °F OA, so two boilers carry design and the third is standby.
- **Load vs OA:** linear from 0 at 65 °F OA to design at 0 °F, with a **reheat floor of 10 % of design** all year (VAV reheat).
- **Morning warm-up:** about 1.3× load for 1–2 h after night setback, on the building occupancy schedule.
- **Coils:** ε-NTU coil banks with valves, the same as the chilled water risers.
  - A low supply temperature (over-reset, or decoupler mixing) opens the valves wider and raises flow and return temperature.
  - Too little supply temperature or DP leaves load unmet.
- **Connected equipment comes off East and shrinks the aggregate:**
  - AHU-1 preheat and heating coils;
  - the VAV reheat coils;
  - UH-1 (moves from a fixed 180 °F to the plant's reset temperature).

  Each reads the plant's supply temperature and branch DP; the plant sees their flow and return temperature.
- **Loop volume:** 15,000 gal, for return temperature lag.

## 8. Expansion tank and loop pressure

- **Fill pressure:** 12 psig static fill at the tank connection. It rises about 3 psi from cold fill to 180 °F.
- **Waterlogged-tank fault:** pressure swings widely with temperature, and the relief valve lifts at 30 psig.

## 9. Power

- **Not on UPS:** boilers (including their BACnet interfaces), primary and secondary pumps, louver.
- **Utility outage:** the same host-wide Fail and Restore buttons as the chiller plant.
  - The boilers block on power loss (never latch) and their BACnet devices go offline.
  - The drives go dark and the building coasts on the loop's stored heat.
- **When power returns:**
  - drives restart on level start if their command is still on;
  - boilers re-run the full purge and ignition sequence once re-enabled.

  A staggered restart is the trainees' logic, using `utility_power`.
- **Emergency stop:** removes boiler power only. Pumps keep running.

## 10. Faults trainees can inject

| Group | Faults |
|---|---|
| Each boiler | Flame failure (intermittent ignition fail / lockout after 3 tries); unstable flame / dirty flame sensor (blocking dropouts and retries); high-limit trip; LWCO trip; flow switch failed open / stuck closed (fires without flow); blocked flue / condensate drain (cut-back, then lockout); gas valve fault; scaled heat exchanger (higher flue temperature, lower efficiency); supply / return sensor open or shorted; BACnet interface offline |
| Each pump | All existing ACH580 drive faults, plus pump wear, cavitation, seized motor |
| Plant | Building gas pressure low / high; louver stuck closed; decoupler flow meter failed; plant DP tube leak; header / decoupler sensor drift or failure; 3-way valve actuator failed; air in loop (noisy DP, poorer heat transfer); expansion tank waterlogged |

## 11. ALC controllers and simulated devices

| ALC controller | Device | Serves | Simulator I/O device (MAC) |
|---|---|---|---|
| HW plant | 242001 | Boiler BACnet integration, PHWP-1..3, boiler alarm inputs, louver, gas pressure, emergency stop, boiler room temperature, `utility_power` | 4010061 (61) |
| HW secondary | 242002 | SHWP-1..3, primary / secondary header temperatures, secondary flow, decoupler flow, plant DP, loop pressure | 4010062 (62) |
| East branch | 242011 | Remote DP, end-of-branch temperature, 3-way valve | 4010063 (63) |
| West branch | 242012 | Remote DP, end-of-branch temperature, 3-way valve | 4010064 (64) |

- **Instance:** HWP-1.
- **Boiler devices:** B-1..3 are simulated devices 4010051–4010053 (MACs 51–53).
- **I/O devices:** act as each controller's field wiring. ALC network outputs write to them.

## 12. Point names and CSV files

**Point names:** the prefix comes from the CSV tag (PHWP-2 → `phwp2_…`).
- **Pumps:**
  - outputs `_ss`, `_vfd_spd`;
  - inputs `_amps`, `_vfd_fb`, `_vfd_flt`.
- **Boilers (hardwired):** `bN_alarm`.
- **Room:**
  - output `louver`;
  - inputs `louver_open`, `gas_press_ok`, `boiler_estop`, `boiler_rm_temp`, `utility_power`.
- **Header and loop:** `phws_temp`, `phwr_temp`, `shws_temp`, `shwr_temp`, `shw_flow`, `hw_dcplr_flow`, `hw_dp`, `hw_loop_press`.
- **Branches:**
  - inputs `hw_remote_dp`, `hw_eob_temp`;
  - output `hw_eob_vlv`.

  The names are the same on both branches, since each has its own controller.

**CSV files** (`configs/hw_plant/`, same rules as the chiller plant: blank cell = default, `#` rows skipped):

| File | Columns |
|---|---|
| `boilers.csv` | `tag`, `sim_device`, `mac`, `controller_device`, `input_mbh`, `design_gpm`, `min_flow_gpm`, `turndown`, `hx_dp_ft`, `description` |
| `primary_pumps.csv` | `tag`, `boiler`, `controller_device`, `design_gpm`, `design_head_ft`, `motor_hp`, `ct_range_amps`, `description` |
| `secondary_pumps.csv` | `tag`, `controller_device`, `design_gpm`, `design_head_ft`, `motor_hp`, `ct_range_amps`, `description` |
| `hw_branches.csv` | `tag`, `controller_device`, `load_share_pct`, `design_remote_dp_psi`, `eob_bypass_pct`, `description` |

- **Dedicated pumps:** every boiler needs exactly one primary pump (its `boiler` column). The plant refuses to start and says which row is missing.
- **Plant-wide settings:** design load, design OA, reheat floor, loop volume, fill pressure, and which controller gets each sensor.

## 13. Open items

0. **Boiler minimum flow** is assumed to be 120 gpm (30 % of design) *(A)*.
   The pump drives' 15 Hz minimum frequency gives about 110 gpm, so turning a
   primary pump all the way down opens the flow switch.

1. **Lochinvar Crest manuals:** received and applied (I&O FCB Rev U, ModBus /
   BACnet Rev X); see [BOILER_CREST.md](BOILER_CREST.md) for what is still *(A)*.
2. **Demo logic** (stand-in, demo mode only):
   - OA reset 160 °F at 0 °F → 120 °F at 60 °F;
   - lead/lag staging: add above 80 % for 10 min, drop when below 30 % with one fewer boiler, weekly rotation;
   - primary pump starts before its boiler and runs 2 min after;
   - PI on supply temperature writes the firing rate, with AV0 = 5 and AV1 written every 20 s at most (inside the 120 s BAS timer);
   - secondary pumps on DP with remote trim;
   - EOB valves hold 10 °F below setpoint;
   - louver open with any boiler enabled;
   - staggered restart.
3. **Web page:** a wide plant page like the chilled water one:
   - outage and emergency stop controls;
   - boiler table with display buttons and sequence status;
   - pump keypads;
   - decoupler flow and direction;
   - branch table;
   - device list.
4. Nothing has been tested against a real Crest BMS interface or ALC integration yet.

## 14. How it is simulated

- **Hydraulics** (every 0.5 s substep): each primary pump with its boiler is
  one circuit, balanced so the balancer speed (55 Hz) gives the boiler's design
  flow. The decoupler is a low-resistance common pipe; the solver finds the
  decoupler flow at which primary flow − secondary flow = decoupler flow. The
  secondary pumps work against the main plus the parallel branches. Each branch
  end is the aggregate coil valves, the connected equipment's own flow, and the
  3-way bypass in parallel.
- **Temperatures:** the boilers heat their own flow (heat exchanger plus water,
  about 3,000 Btu/°F each). The primary return mixes secondary return with any
  supply short-circuiting through the decoupler. The secondary supply mixes
  primary supply with any return water coming through the decoupler the other
  way. The return lags by the loop volume (15,000 gal).
- **Boiler** (`components/boilers/lochinvar_crest.py`): the sequence, limits and codes of
  section 2. A no-flow hot spot at the high limit well makes the manual-reset
  high limit trip when a boiler fires without flow (stuck flow switch).
  Efficiency follows return temperature; flue temperature and condensate
  follow from it.
- **Building:** heating coil banks (ε-NTU, EAT 55 °F) per branch. Their valves
  move toward the opening that meets the load (τ = 30 s). The load comes from
  OA temperature, the reheat floor, night setback and morning warm-up, minus
  the connected equipment. The end-of-branch pipe temperature is purged by the
  flow that reaches the far end (3-way bypass plus the last coils) and cools
  toward the ceiling space otherwise.
- **Loop pressure:** fill pressure plus thermal expansion of the mean loop
  temperature. A waterlogged tank multiplies the swing by six and lifts the
  relief valve at 30 psig.
- **Boiler room:** temperature from boiler casing losses and the louver
  bringing in outdoor air.
- **Demo logic** (`mockalc.HwPlantDemoLogic`, demo mode only): section 13,
  item 2, running on simulated time.

