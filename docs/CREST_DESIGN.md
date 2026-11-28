# Lochinvar Crest from the manuals: design (for review)

Status: **built** (user doc: [BOILER_CREST.md](BOILER_CREST.md)). All ten
questions in section 11 were built with their proposed defaults, with one
change: a louver that never proves only blocks the start (the I&O sequence
says "a block that will clear when the louver proving switch closes");
lockout 134 applies when proof is lost while firing.

The Crest boiler was built without its
manuals, and everything Crest-specific was marked *(A)*: sequence timings,
limits, codes and the BACnet object list. This note replaces those
assumptions with the manuals.

**Source documents:**
- Lochinvar *Crest Installation & Operation Manual*, models FCB1000-6000
  (100343224_2000600568 Rev U):
  - section 10: operating information, protection features and the sequence
    of operation;
  - page 43: pump delays;
  - the model table.
- Lochinvar *ModBus and BACnet Communication Instructions*, Crest FB/OF751-6001
  and FCB1000-6000 (100161012_2000020084 Rev X):
  - section 4: Modbus memory map and configuration bits;
  - section 6: BACnet timing;
  - section 7: BACnet memory map;
  - section 10: unit operation with BMS;
  - section 12: rate and temperature conversion;
  - section 13: status, blocking and lockout codes.

Values still not found in either manual stay marked *(A)*.

## 1. What changes, in short

The manuals differ from the assumed Crest in ways a trainee would notice
straight away:

| Today *(A)* | From the manuals |
|---|---|
| Object list `BV1 BMS ENABLE`, `AV1 BMS FIRING RATE` | `AV0 Configuration` (bits), `AV1 Coils` (bit 0 = enable), `AV2 Rate Command / Setpoint Command`. The boiler ignores AV1 / AV2 unless the matching configuration bits are set in AV0 |
| Temperatures in °F | Every temperature object is in **°C** (0.1 °C resolution) |
| Status MSV with purge / ignition / running states | Status code AI13: what the boiler is doing *for its demand* (11 no demand, 22 SH demand from BMS, 33 SH pump delay, 40 lockout, …), not the burner sequence |
| Blocking / lockout codes 1-9 / 1-5 (made up) | The manual's codes: blocking AI14 (e.g. 11 high outlet, 12 anti-cycling, 15 high flue); lockout AI15 (e.g. 233 ignition failure, 170 LWCO, 246 internal MRHL) |
| Comm loss: hold the rate 60 s, then block | The BAS must refresh its commands within the BAS timer. When it lapses, the boiler **reverts to its own controls** (hardwired enable, local setpoint) |
| Limits 190 / 200 / 210 °F, rise 40 °F | Outlet 185 °F reduce, 195 °F auto-reset high limit, 200 °F auxiliary MRHL, 205 °F integral MRHL; flue 215 / 225 °F; ΔT 70 / 80 °F |
| Flow switch blocks | The Crest's standard flow protection is its ΔT limits; a flow switch is field-supplied |
| Model FBN4001, 4000 MBH, 25:1 | This I&O covers the FCB series: **FCB4000N**, 200-3999 MBH (20:1), with O2 trim, flame current over BACnet |

## 2. Model

**FCB4000N** *(question 1)*:
- input 3999 MBH max, 200 MBH min (5 %);
- gross output 3843 MBH;
- 201 gallons of water content.

The CSV rows change to `input_mbh 3999`, `turndown 20`, with the description
"Lochinvar Crest FCB4000N condensing boiler".

The FCB adds these BACnet objects to the FB list:
- AI35 Flame 1 (flame current);
- AI22 O2 Sensor Status and AI37 Corrected O2 Reading;
- BI17 Internal LWCO;
- BI18 Gas Pressure Switch 2.

## 3. BMS control (communication manual section 10)

**Demand configuration** (Main Menu > Setup > BMS) is a boiler setting with
three choices:

| Configuration | BAS writes | Boiler runs to |
|---|---|---|
| ENABLE active, BMS inactive | AV0 = 0x0001, AV1 bit 0 | its own setpoint (local) |
| **ENABLE active, BMS active** (default) | AV0 = 0x0005, AV1 bit 0, AV2 | AV2 as firing rate, or as setpoint |
| ENABLE inactive, BMS active | AV0 = 0x0004, AV2 | AV2 alone (enabled by the signal level) |

**BMS type** (rate or setpoint) is a boiler setting. The plant's
`boiler_bms_mode` sets it, and the Crest now supports both:
- **Rate:** AV2 is percent modulation.
- **Setpoint:** AV2 is percent of the 0-10 V span. It maps linearly from 2 V
  = 69.8 °F (21 °C) to 10 V = 179.6 °F (82 °C), the BMS setup defaults. The
  manual's example: 110 °F → 49 %.

**Configuration bits are enforced** *(question 3)*:
- Without bit 0, the boiler ignores AV1 and uses its hardwired Enable input.
- Without bit 2, it ignores AV2.
- This is the manual's "re-send the configuration bits" note, and a common
  real-world mistake worth training on.
- BV0 Boiler Enable is the same coil as AV1 bit 0 *(A)*.

**BAS timer** (Main Menu > Setup > BAS, 5-120 s) *(question 4)*:
- Every BAS write restarts it.
- If no write arrives in time, the boiler reverts to its own controls: its
  **hardwired Enable input** and its **local setpoint**.
- The manual also says BMS commands must be refreshed at least every 4
  minutes. The simulator uses the BAS timer setting, default **120 s** *(A,
  inside the manual's range)*.
- An ALC network output that writes only on change will drop the boiler back
  to local control. That's realistic, and it's in the troubleshooting notes.

**Hardwired Enable** is a new plant setting `boiler_hw_enable`:
- **Default: open.** Nothing is wired, so a boiler that loses the BAS stops,
  with status 11 "No heat demand requested".
- With it closed, which models a jumper, the boiler keeps heating on its
  local setpoint after the BAS goes quiet.

## 4. Temperatures and units

- **BACnet:** present values are in **°C**, at the resolution the manual
  gives: 0.1 °C for outlet, inlet, flue, tank and outdoor; 0.5 °C for the
  setpoints *(question 5)*.
- **Modbus:** values are scaled ×10 (0.1 °C) or ×2 (0.5 °C), per section 12.
- The ALC trainee converts to °F, as on a real job.
- The web page still shows °F.

## 5. Sequence (I&O section 10, sequence of operation)

1. A call for heat closes the **louver relay** (BI36). The boiler blocks
   until the louver proving switch closes (BI3).
2. Pre-purge, then trial for ignition: spark, gas valve, flame check.
   - No flame: a recycle, then lockout **233 Ignition Failure**.
3. Flame stabilization, then running, modulating 5-100 % to the BMS rate or
   to its setpoint.
4. Demand ends: gas valve off, post-purge, louver relay off at the end of
   post-purge.
5. Pump delay **30 s** (manual, factory setting): the boiler pump contact
   (BI34) stays made.

**Timings the manuals don't give** stay *(A)*:
- pre-purge 10 s;
- trial for ignition 4 s;
- flame stabilization 30 s at 40 %;
- post-purge 30 s;
- anti-cycling 60 s (installer adjustable);
- 3 ignition tries before lockout.

**Freeze protection** (manual, new):
- inlet below 45 °F: boiler pump request on;
- inlet below 37 °F: the burner fires (status 30);
- off again above 45 °F.

## 6. Protection (I&O section 10)

| Condition | Action | Code |
|---|---|---|
| Outlet > 185 °F | reduce fan speed (rate cut-back) | — |
| Outlet > 195 °F | auto-reset high limit: off until the minimum off time has passed and the outlet has dropped 10 °F | blocking 11, status 2 |
| Outlet > 200 °F | auxiliary manual reset high limit (dial) | lockout 240 External MRHL, BI0 |
| Outlet > 205 °F | integral manual reset high limit | lockout 246 Internal MRHL |
| Flue > 215 °F | reduce maximum fan speed | — |
| Flue > 225 °F | off until 10 °F lower and the minimum off time | blocking 15, status 3 |
| Outlet − inlet > 70 °F | reduce fan speed | — |
| Outlet − inlet > 80 °F | off until below 50 °F and the minimum off time | blocking 10, status 4 |
| Anti-cycling | after a space heating demand ends | blocking 12 |
| Display power button off | — | blocking 8, status 10 |

**Low flow:**
- The I&O says the Crest's own flow protection is temperature based: it
  modulates down, then shuts off on the outlet and ΔT limits above.
- The plant keeps the field-supplied **flow switch** on BI1 *(question 7)*.
- With the switch open, the boiler won't start and stops if running. The
  manual publishes no code for this, so the status is 11 and the display
  text says "Flow switch open" *(A)*.

## 7. Instructor faults → manual codes

Fault names stay as they are (generic names unchanged):

| Fault | Crest behaviour | Code |
|---|---|---|
| `ignition_fail` | 3 tries, then lockout | 233 Ignition Failure |
| `flame_unstable` | flame drops out while running, recycles, then locks out | 232 Flame Failure 1 |
| `high_limit_trip` | auxiliary MRHL open | 240 External MRHL |
| `lwco_trip` | LWCO open (BI17 on FCB) | 170 Low Water Cut-Off Open |
| `flow_sw_open` / `flow_sw_stuck` | flow switch (section 6) | — |
| `flue_blocked` | blocked flue switch (BI10), then lockout | 167 Blocked Drain Switch Open *(A: closest listed)* |
| `gas_valve` | gas valve relay failure at ignition | 244 Large Gas Valve Relay Failure |
| `hx_scaled` | high flue, low efficiency; can reach the 215 / 225 °F flue limits | blocking 15 if it does |
| `supply_sensor_open` / `_short` | outlet sensor S1 | 193 / 192 |
| `return_sensor_open` / `_short` | inlet sensor S2 | 180 / 179 |
| `comm_loss` (`gateway_offline`) | the BAS timer lapses; reverts to its own controls (section 3) | — |
| **new** `gas_pressure` | gas pressure switch open | 169 Gas Pressure Switch Open |
| **new** `louver_fail` | louver proving never closes: the boiler waits, then locks out | 134 Louvers Not Open |

The sensor faults become **lockouts** as on the real control. They are
blocking today.

**Lockout code register:**
- The manual says AI15 is only meaningful while the status code AI13 is 40.
- The simulator leaves the last lockout code in AI15 after a reset, so a
  program that alarms on AI15 alone, instead of on status 40, raises false
  alarms *(question 9)*.

## 8. Display (web page panel)

- **Keys:**
  - **Power** (on/off): off gives blocking 8, status 10.
  - **Reset**: clears a lockout once its cause is gone.
- **Local / BMS buttons go away.** On the real Crest, local operation is
  either the demand configuration (a setting) or the BAS timer lapsing, not a
  key.
- **Display settings** (web page parameters):
  - SH setpoint (local, 32-185 °F, default 180 °F);
  - demand configuration;
  - BMS type;
  - BMS temperature at 2 V / 10 V;
  - BAS timer;
  - anti-cycling time;
  - auto-reset high limit (default 195 °F);
  - aux MRHL dial (default 200 °F).

## 9. Integration list

**BACnet (default), communication manual section 7:**
- **Writable:** BV0, BV4, AV0-AV7.
- **Read only:**
  - BI0-BI52, the FB plus FCB set listed in the manual;
  - AI0-AI2 packed binary inputs;
  - AI3 system / cascade setpoint, AI7 outlet setpoint;
  - AI8 outlet, AI9 inlet, AI10 flue (°C);
  - AI11 firing rate, AI12 boiler pump speed;
  - AI13 / AI14 / AI15 status / blocking / lockout;
  - AI22, AI35, AI37 (FCB).
- **Names, types, instances and units are the manual's exactly.** The status
  codes are AIs in the manual, not multi-state objects, so the trainee
  decodes them.

**Modbus (optional second model, question 2):**
- Registered as `lochinvar_crest_modbus`.
- Uses the section 4 map:
  - coils 00001 / 00005;
  - discrete inputs 10001-10053;
  - input registers 30001-30038;
  - holding registers 40001-40008.
- Served by the hot water plant's Modbus TCP server (port 5022), standing in
  for an RS-485 gateway.
- Needs Modbus function codes **1, 2, 5 and 15** (coils and discrete inputs)
  added to the server.

## 10. Plant, demo logic and tests

**Hot water plant:**
- Boiler CSV rows: model FCB4000N ratings.
- The ALC's plant louver BO stays. It is the common combustion air louver for
  the boiler room, and its end switch is wired to every boiler's louver
  proving input.
- Each Crest's own louver relay (BI36) shows its call for combustion air.

**Demo logic (by roles):**
- Writes AV0 = 5, AV1 = 1 and AV2 with the rate.
- Rewrites every 10 s, inside the BAS timer.
- New roles: `config` (AV0), and `outlet_c` for reading in °C.

**Golden traces:**
- `boiler_vendor` and `hw_plant` will change: different limits, codes,
  object names and BMS handling.
- I'll record them again and report the differences, as an intended change.
- The drive, AHU, chiller and generator traces must not change.

**Tests:**
- The boiler conformance tests must pass.
- New Crest tests:
  - configuration bits ignored / honoured;
  - BAS timer revert with hardwired Enable open and closed;
  - setpoint-mode scaling (110 °F → 49 %);
  - each limit and its code;
  - lockout code persistence;
  - °C objects;
  - the Modbus map, if built.

**Docs:**
- New user doc `docs/BOILER_CREST.md`, like `VFD_Z1000.md`.
- Updates to `HW_PLANT_DESIGN.md` and the README's hot water plant section.
- The *(A)* list shrinks to what is in neither manual.

## 11. Questions for review (proposed defaults)

1. **Model.** *Default:* FCB4000N (3999 MBH, 20:1), since the I&O manual is
   for the FCB series.
2. **Modbus model.** *Default:* add `lochinvar_crest_modbus` as a second
   registered model (with server function codes 1/2/5/15). The plant stays on
   BACnet; a CSV row can switch.
3. **Configuration bits enforced.** *Default:* yes. AV1 / AV2 are ignored
   unless AV0 has bits 0 / 2 set.
4. **BAS timer and hardwired Enable.** *Default:* timer 120 s; on timeout,
   revert to own controls; hardwired Enable open, so the boiler stops.
5. **Units.** *Default:* °C on BACnet (0.1 / 0.5 °C), scaled on Modbus, as the
   manual specifies.
6. **Timings not in the manuals.** *Default:* keep today's *(A)* values
   (section 5); pump delay 30 s from the manual.
7. **Flow switch.** *Default:* keep the field flow switch on BI1, with no
   code (status 11, display text). The ΔT and outlet limits provide the
   Crest's own low-flow protection.
8. **New faults.** *Default:* add `gas_pressure` (169) and `louver_fail`
   (134).
9. **Lockout code persistence.** *Default:* AI15 keeps the last lockout code
   after a reset; only status 40 means locked out.
10. **Display.** *Default:* Power and Reset keys; BMS settings as page
    parameters; Local / BMS buttons removed.
