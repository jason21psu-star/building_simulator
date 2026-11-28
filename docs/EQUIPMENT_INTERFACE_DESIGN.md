# Chiller, boiler and generator interfaces: design (for review)

Status: **for review, nothing built.** This is step 2 of the modularization
plan:
1. Drive interface: built ([DRIVE_INTERFACE_DESIGN.md](DRIVE_INTERFACE_DESIGN.md)).
2. **Vendor interfaces for chillers, boilers and generators: this document.**
3. AHU built from sections, with a general connection system (including
   outdoor air from a DOAS).

It follows the drive interface: one interface per equipment family, with
today's vendors moved behind it unchanged and protected by golden traces. A
second vendor is then one new file that passes the family's conformance tests.

Values not taken from a manual are marked *(A)*. Proposed defaults for the
review questions are in section 10.

## 1. Goal

**Swap the make and model by configuration.** Each chiller, boiler and
generator can change make and model with one CSV column, and no plant code
changes.

**What stays the same with a swap:**
- the plant's physics: piping, pumps, decoupler, towers, loads, bus and ATSs;
- every hardwired point the trainees program (Mode A):
  - isolation valves and end switches;
  - flow meters;
  - pump and fan drives;
  - the boiler room louver;
  - the ATSs and utility sensing;
- the plant's web page layout and instructor controls;
- the equipment's ratings, taken from the same CSV columns.

**What changes, as it would on a real job:**
- the equipment's controller: its sequence, timers, limits and safeties;
- its panel or display keys, and its status texts;
- its fault, alarm, lockout and diagnostic codes;
- its integration (Mode B): the object list or register map, the protocol
  (BACnet or Modbus TCP), and how the BAS commands it (enable plus setpoint,
  or a direct firing rate, …).

A swapped vendor therefore gives the trainees a new integration to map in the
ALC, just as a real project would, while the rest of the plant program still
works.

**Today the three plants are tied to one vendor each:**

| Plant | Vendor | Where the vendor is hard-coded |
|---|---|---|
| `chw_plant` | YORK YK with OptiView (BACnet) | `ch["yk"]` and YK attributes (`lchwt`, `lcwt`, `q`, `kw`, `local_sp`) are used directly in `chw_plant.py`; object list `YK_OBJECTS`; view heading "YORK YK" |
| `hw_plant` | Lochinvar Crest (BMS, BACnet) | `b["crest"]` with `rate`, `flame`, `q`, `t_hx`, `lockout` used directly; `CREST_OBJECTS`; view heading |
| `gen_plant` | Cummins PowerCommand 3.3 (Modbus TCP) | paralleling code reaches into `Genset` internals (`_raise(1444)`, `active`, `cb_closed`, `master_call`); register numbers in `GenUnit.write`; view heading |

The demo ALC logic (`mockalc.py`) also writes vendor names: `rem_run` and
`rem_sp` for the York, `bms_enable` and `bms_rate` for the Crest, and
registers 40010, 40300 and 40301 for the Cummins.

## 2. How it is used (configuration)

| Plant | CSV | New column | Blank means |
|---|---|---|---|
| `chw_plant` | `chillers.csv` | `model` | `york_yk` |
| `hw_plant` | `boilers.csv` | `model` | `lochinvar_crest` |
| `gen_plant` | `generators.csv` | `model` | `cummins_pcc33` |

**Mixing vendors:**
- One plant can mix vendors, one row at a time (a York and a Trane chiller in
  the same plant, as on many real sites).
- For generators, mixing is allowed but the page warns: paralleled gensets of
  different makes are rare.

**Settings:**
- The ratings already in the CSVs are vendor-neutral and keep their names:
  - chillers: `capacity_tons`, `chw_design_gpm`, `cw_design_gpm`,
    `chws_design_temp`, `kw_per_ton`, `min_load_pct`;
  - boilers: `input_mbh`, `design_gpm`, `min_flow_gpm`, `turndown`;
  - generators: `kw_rating`, `volts`.
- Vendor-specific settings (York `anti_recycle_min`, Crest timers, …) go in
  the template under the plant's settings with the vendor's own names, as the
  drive overrides do (`chiller: { york_yk: { anti_recycle_min: 15 } }`).
- A setting that doesn't belong to the selected vendor is a configuration
  error that names the vendor and the setting.

## 3. What every family shares

Each family is a package with a base class, a registry and one file per
vendor, like `components/drives/`:

```
hvacsim/models/components/chillers/  base.py  york_yk.py
hvacsim/models/components/boilers/   base.py  lochinvar_crest.py
hvacsim/models/components/gensets/   base.py  cummins_pcc33.py
```

Today's `york_yk.py`, `crest.py` and `pcc.py` move into these packages. The
old module names stay as imports, so nothing else breaks.

### Identity and metadata

| Item | Example |
|---|---|
| `MODEL`, `MAKE`, `DISPLAY` | `york_yk`, `YORK`, `YORK YK (OptiView)` |
| `PROTOCOL` | `bacnet` or `modbus_tcp` |
| `OBJECTS` | the integration list in one format for all vendors (below) |
| `ROLES` | BAS role → the vendor's object key (section 3, "BAS roles") |
| `PANEL` | panel or display keys for the web page (York: Local / Remote / Run / Stop / Reset; Cummins: Off / Auto / Manual / Reset) |
| `SETTINGS` | the vendor's own settings, with defaults, ranges and units |
| `FAULTS` | instructor faults: generic names mapped to the vendor's codes, plus any vendor extras |
| `CODE_TABLES` | the vendor's status and fault code texts (for multi-state objects and the page) |

**One integration list format** for both protocols. Today's YK and Crest
lists already use it; the Cummins register map moves into it:

```
(kind, address, name, key, units, role)
  kind     analog-value / binary-value / multi-state-value   (BACnet)
           holding-register / input-register                  (Modbus)
  address  BACnet instance, or Modbus register (with scale, word count, signed)
  role     "cmd" (BAS writes), None (read only), ("msv", table, count)
```

The plant builds its BACnet devices or Modbus units from this list, whatever
the vendor. A plant can then serve both at once: a Modbus chiller in the
BACnet chilled water plant gets a Modbus TCP server, as the generator plant
has now.

### BAS roles

Some code must command or read the equipment without knowing its vendor:
- the demo ALC logic;
- the conformance tests;
- the plant's own page.

They use **roles**, and each vendor maps a role to one of its objects. This is
the same idea as the drives' wiring roles. The trainee never sees roles: they
map the vendor's real objects in the ALC.

| Family | Roles the BAS writes | Roles the BAS reads |
|---|---|---|
| Chiller | `run_cmd`, `chws_sp`, `demand_limit` | `running`, `alarm`, `lchwt`, `echwt`, `lcwt`, `ecwt`, `pct_load`, `kw`, `ready`, `local` |
| Boiler | `enable`, `rate_cmd` and/or `sp_cmd` (section 5) | `firing`, `flame`, `supply_t`, `return_t`, `alarm`, `pump_request`, `flow_switch`, `local` |
| Generator | `remote_start`, `fault_reset` | `running`, `at_rated`, `kw`, `hz`, `volts`, `shutdown`, `warning`, `in_auto`, `fault_code` |

A vendor that has no object for a read role gives `None` (the demo logic then
uses a plant sensor instead). Every write role must exist.

### Common behaviour

| Item | Meaning |
|---|---|
| `step(h, …)` | one integration step against the plant (family-specific arguments, section 4-6) |
| `powered` | utility or generator power present (set by the plant) |
| `comm_ok` | the integration is online (cleared by the generic `comm_loss` fault) |
| `panel(key)` | a panel or display key from the web page |
| `points()` | current value of every object in `OBJECTS`, by key |
| `status()` | a dict for the web page: `text` (the panel's own status line), `state` (generic: off / starting / running / stopping / alarm / lockout / local), `codes` (active codes with texts) |
| `reset_state()` | back to power-up state |

### Instructor faults

Each family defines **generic faults** that every vendor must support, mapped
to its own codes. Vendors can add their own.

**Existing fault keys stay as they are.** `ch1_vsd_fault`, `b1_flue_blocked`,
`gen1_low_oil` and the others keep their names, so saved scenarios and the
tests keep working. The generic names below are the existing York, Crest and
Cummins keys, with three exceptions:
- `vsd_fault` becomes the York's name for the generic `starter_fault`;
- `gateway_offline` (York, Crest) and `comm_loss` (Cummins) become the
  generic `comm_loss`;
- each existing key stays as an alias.

## 4. Chiller interface

### Plant ↔ chiller (physics)

| Direction | Items |
|---|---|
| Plant gives | `evap_gpm`, `cond_gpm`, entering chilled water `ewt`, entering condenser water `ecwt`, room temperature, `powered` |
| Chiller gives | `lchwt`, `lcwt`, heat removed `q` (Btu/h), input `kw`, `evap_fs` / `cond_fs`, `running`, `pump_request` (chilled and condenser water pump contacts, when the vendor has them) |

**Shared thermal core** (`chillers/core.py`, taken out of `YorkYK`, numbers
unchanged):
- capacity against lift and flow;
- the kW curve;
- the evaporator and condenser heat balance;
- refrigerant saturation temperatures.

Every chiller uses the core, so two vendors with the same ratings give the
same tons and kW on the same plant. They differ in their controls, codes and
integration, as real machines do. A vendor can adjust the core (for example
inlet guide vanes with a constant speed motor instead of a VSD) through its
settings.

### What the vendor owns

- start sequence: prelube, flow proving, anti-recycle, soft load;
- capacity control to its active setpoint and current limit;
- safeties and cycling shutdowns with its own code numbers;
- local / remote control source and the panel keys;
- its integration list.

### Generic chiller faults

| Generic | YORK YK (today) |
|---|---|
| `evap_fs_failed` | `evap_fs_failed`, safety code on loss of flow |
| `cond_fs_failed` | `cond_fs_failed` |
| `low_charge` | `low_charge` |
| `cond_fouled` | `cond_fouled` |
| `evap_fouled` | `evap_fouled` |
| `starter_fault` | `vsd_fault` (VSD control fault) |
| `comm_loss` | `gateway_offline` |

## 5. Boiler interface

### Plant ↔ boiler (physics)

| Direction | Items |
|---|---|
| Plant gives | `gpm` through the heat exchanger, entering water temperature, room temperature, `powered`, permissives `louver_ok` and `gas_ok` |
| Boiler gives | leaving water temperature (return value of `step`, as today), heat to water `q`, gas input, `flame`, `pump_request`, `flow_switch`, `alarm` |

**Shared heat exchanger core** (`boilers/core.py`, taken out of
`CrestBoiler`, numbers unchanged):
- the heat exchanger water mass;
- efficiency against return water temperature and firing rate (condensing or
  non-condensing, set by the vendor);
- flue temperature;
- condensate;
- the no-flow hot spot at the high limit.

### What the vendor owns

- burner sequence: pre-purge, ignition trials, low-fire hold, post-purge,
  anti-cycle;
- modulation limits (turndown, slew rate);
- cut-back, operating and high limits;
- blocking and lockout codes;
- its BMS control modes and integration list.

### BMS control mode

Boilers differ in how the BAS commands them:
- **Direct firing rate:** the BAS writes `enable` and `rate_cmd`. The Crest
  is commanded this way today.
- **Remote setpoint:** the BAS writes `enable` and `sp_cmd`, and the boiler's
  own controller modulates to it.

Each vendor declares the modes it supports. A plant setting
`boiler_bms_mode` (default `rate`, today's behaviour) picks one, and a vendor
that doesn't support the chosen mode is a configuration error.

### Generic boiler faults

These are today's Crest fault keys: `ignition_fail`, `flame_unstable`,
`high_limit_trip`, `lwco_trip`, `flow_sw_open`, `flow_sw_stuck`,
`flue_blocked`, `gas_valve`, `hx_scaled`, `supply_sensor_open` / `_short`,
`return_sensor_open` / `_short`, and `comm_loss` (Crest `gateway_offline`).

## 6. Generator interface

### Plant ↔ genset (physics and paralleling)

The generator plant runs the paralleling, the bus and the ATSs. Today it
reaches into the Cummins `Genset` object. Under the interface, it uses only
these:

| Direction | Items |
|---|---|
| Plant gives | `step(h, t_amb, bus_f)`, `master_call` (load demand), `kw` and `kvar` share, `cb_closed` (the plant closes and opens the breaker) |
| Plant raises | generic **plant events**: `overload`, `underfrequency`, `cb_fail_to_close`, `fail_to_sync`. The genset maps each to its own code (Cummins: 1444, 1448, 1452, 1457) and fault type. Today the plant calls `gs._raise(1444)` directly. |
| Genset gives | `at_rated`, `running`, `capacity()` (kW, after any derate), `shutdown`, `in_auto`, `start_request`, `volts_rated`, `load_demand_stopped` |

**Shared engine core** (`gensets/core.py`, taken out of `Genset`, numbers
unchanged):
- cranking and firing;
- speed ramp;
- coolant, oil pressure and battery models;
- fuel use;
- run hours and starts.

### What the vendor owns

- the control's sequence: start delay, crank and rest cycles, warm-up,
  cooldown;
- control switch positions;
- fault codes and types;
- the NFPA 110 annunciator bits;
- its register map, including scaling, word order and the value of unlisted
  registers (65535 on the PCC 3.3).

### Paralleling master

The DMC 1500 master register map (unit 5) is **plant equipment, not part of
the genset**: real paralleling switchgear is often from another maker than
the engines. It stays in `gen_plant.py` and reads the gensets only through
the interface.

Choosing a switchgear vendor (ASCO, Russelectric, …) is out of scope for this
step. Section 10 asks about it.

### Generic genset faults

These are today's Cummins fault keys: `fail_to_crank`, `fail_to_start`,
`high_coolant`, `low_oil`, `overspeed`, `high_ac_volts`, `low_ac_volts`,
`high_current`, `low_battery`, `weak_battery`, `low_gas`, `fail_to_sync`,
`cb_fail_close`, `charger_fail`, `comm_loss`.

## 7. Web pages

The three plant pages stop naming a vendor:
- **Table headings** come from the rows' vendors: "Chillers (YORK YK,
  BACnet)", or "Chillers (YORK YK, Trane CenTraVac)" when mixed.
- **Each row shows** the vendor's `status().text`, its active codes, and its
  `PANEL` keys as buttons. This is the same idea as the drives' generic
  keypad.
- **Common columns** come from roles and physics, the same for every vendor:
  - chillers: LCHWT/ECHWT, setpoint, tons, kW;
  - boilers: firing, supply/return, MBH;
  - generators: kW, Hz, volts, state.
- **Vendor columns** (York %FLA and lift, Crest flame µA, Cummins switch
  position) come from a short `view_fields()` list on the vendor, so a new
  vendor shows its own without editing the plant's JavaScript.
- **The devices table** lists each device's protocol and object count from
  the vendor.

## 8. Quality safeguards

**Golden traces, before any refactor:**
- `chw_plant` and `hw_plant` already have them (recorded for the drive
  interface). I'll extend them so they also trip, and reset, at least one
  vendor fault each: York `evap_fs_failed` and Crest `ignition_fail`.
- **New `gen_plant` trace:**
  - utility outage, start and synchronize;
  - open transition out;
  - a load step with the frequency dip;
  - shedding on overload;
  - a genset fault;
  - closed transition back.

  It records the register values the ALC reads.

The refactored plants must reproduce all the traces, and all 177 existing
tests must still pass.

**Conformance tests per family.** Every registered vendor must pass the same
checks:
- **Chiller:**
  - starts only with `run_cmd`, flow and power;
  - holds `chws_sp` within a band at steady load;
  - obeys `demand_limit`;
  - stops on loss of evaporator flow with an alarm, and resets;
  - honours its anti-recycle;
  - after an outage it is dark, then restarts per its own rules;
  - every generic fault trips;
  - its integration list builds, and every write role is writable.
- **Boiler:**
  - fires only with `enable`, flow, louver and gas;
  - follows `rate_cmd` (or `sp_cmd`);
  - goes through its purge and ignition sequence;
  - limits and locks out on the generic faults, and resets;
  - stops firing on loss of comm, per its own rule;
  - builds its integration list.
- **Genset:**
  - starts on `remote_start` or a master call;
  - reaches rated speed and closes;
  - shares load;
  - each plant event raises the vendor's own code;
  - each generic fault shuts down or warns per its type;
  - fault reset obeys its switch rules;
  - the register map reads in the gateway's block sizes with no gaps that
    raise exceptions.

**Demo ALC logic** moves to roles (`run_cmd`, `enable`, `remote_start`, …).
The plants keep running in demo mode whatever the vendor.

## 9. Second vendors

Building the interfaces only proves anything once a second vendor sits behind
each one. As with the Z1000, I'd build each second vendor from its manuals.
Common choices for a building like this:

| Family | Candidate | Integration | Manuals needed |
|---|---|---|---|
| Chiller | **Trane CenTraVac** (CVHF/CVHH, Tracer AdaptiView UC800) | BACnet | Tracer AdaptiView BACnet points list, diagnostics list, operating sequence (IOM) |
| Chiller | Carrier AquaEdge 19DV (PIC 6) | BACnet | PIC 6 controls and BACnet points manual |
| Boiler | **AERCO Benchmark** (BMK Edge) | BACnet or Modbus | Edge controller manual (Modbus/BACnet points, fault messages, sequence) |
| Boiler | Cleaver-Brooks ClearFire-CE (Falcon control) | Modbus | Falcon Modbus map, ClearFire manual |
| Generator | **Caterpillar G3512 / EMCP 4.4** | Modbus (RTU, behind a gateway shown as TCP) | EMCP 4.4 Modbus data link manual, event code list |
| Generator | Kohler KG series / Decision-Maker 6000 | Modbus | DEC 6000 Modbus register list |

**Lochinvar Crest:** its manuals are still needed. Its sequence values and
object names are *(A)* today, and the interface work doesn't change them.

## 10. Questions for review (proposed defaults)

1. **Scope of this step.**
   *Default:*
   - build the three interfaces;
   - move YORK YK, Lochinvar Crest and Cummins PCC 3.3 behind them unchanged,
     with golden traces;
   - add conformance tests and the generic page pieces;
   - then add one second vendor per family, in the order chiller, boiler,
     generator, each when you've supplied its manuals.
2. **Second vendors.** *Default:* Trane CenTraVac, AERCO Benchmark and
   Caterpillar EMCP 4.4.
3. **Shared physics cores.** *Default:*
   - take a core out of each existing vendor now (numbers unchanged, checked
     by the golden traces);
   - every vendor uses it, so swaps compare like for like;
   - the alternative is to let each vendor carry its own physics.
4. **Boiler BMS mode.** *Default:* the vendor declares the modes it supports,
   a plant setting `boiler_bms_mode` picks one, and the default is `rate`
   (today's behaviour).
5. **Mixed protocols in one plant.** *Default:* allowed. A Modbus vendor in
   the chilled or hot water plant gets a Modbus TCP server on its own port,
   from `plant.modbus.port`.
6. **Paralleling switchgear vendor.** *Default:* out of scope. The DMC 1500
   map stays as plant equipment, engine-agnostic.
7. **Fault keys.** *Default:* existing keys keep their names; the generic
   names `starter_fault` and `comm_loss` are aliases.
8. **CSV column name.** *Default:* `model`, blank = today's vendor.

## 11. Build steps

1. Extend the golden traces (vendor faults) and add the `gen_plant` trace, on
   the current code.
2. Families and registries. Move York, Crest and Cummins behind them, with
   the shared cores taken out, numbers unchanged. Switch all three plants and
   the demo logic to the interface and roles. Golden traces and all tests
   must pass.
3. Generic integration builder: BACnet or Modbus, from `OBJECTS`, with
   `PROTOCOL` per row.
4. Conformance tests per family. The three existing vendors pass.
5. Generic plant page pieces: headings, status, panel keys, `view_fields()`.
6. Docs: README (choosing a vendor), and a pointer in each plant design doc.
   Then commit and push.
7. Later, one at a time with their manuals: the second vendor in each family.
   Each gets its own short design note, its templates and a user doc
   (`docs/CHILLER_TRANE_CVHF.md`, …).

## 12. Open items / assumptions

- **Crest manuals:** still not available. The Crest stays *(A)*.
- **Generator register numbering:** to be checked against the ALC Modbus
  driver's addressing, 4xxxx or zero-based. This applies to every Modbus
  vendor, so the integration list keeps the manual's numbering and the
  gateway applies any offset in one place.
- **Chiller pump contacts:** the York sequence today assumes the plant
  provides flow. Vendors that drive their own pump contacts (`pump_request`)
  will show it on the page, but the plant's pumps stay under ALC control.
