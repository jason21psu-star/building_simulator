# Natural gas generator plant: physical design (for review)

Status: **design for review, not built yet.** Equipment type will be `gen_plant`
(model `hvacsim/models/gen_plant.py`, Modbus TCP server `hvacsim/modbus_tcp.py`,
template `configs/templates/gen_plant.yaml`, schedules `configs/gen_plant/*.csv`).
This records the 7 design questions answered in review.

**Source documents:**
- Cummins *Modbus Register Mapping*, A029X159 Issue 26 (6-2020):
  - section 13: PowerCommand 2.x/3.x parametrics;
  - section 14: fault status bitmaps;
  - section 3: NFPA 110 bitmaps;
  - section 7: DMC 1500.
- Cummins *PowerCommand 3.3* spec sheet S-1570.

Register numbers, scaling and fault codes below come from these. Values marked
*(A)* are assumptions (timings, ratings not in the documents).

## 1. Arrangement

```
 UTILITY ──────────────────────────────┬──────────┬──────────┬──────────┐
                                       │ N        │ N        │ N        │ N
 GEN-1 ─CB─┐                        ┌──┴──┐    ┌──┴──┐    ┌──┴──┐    ┌──┴──┐
 GEN-2 ─CB─┤  paralleling bus ──────┤ATS-1├────┤ATS-2├────┤ATS-3├────┤ATS-4│  (E side)
 GEN-3 ─CB─┤  (DMC 1500 master)     └──┬──┘    └──┬──┘    └──┬──┘    └──┬──┘
 GEN-4 ─CB─┘                           │          │          │          │
                                   UPS/critical  CHP-1     HWP-1    AHUs / building
```

- **Generators:** 4 × 1,000 kW natural gas generator sets (Cummins C1000N6C *(A)*, 480 V, 60 Hz, PowerCommand 3.3), paralleled N+1.
  - Three sets carry the full plant (about 2.9 MW); the fourth is redundant.
- **Transfer to generators:** open transition.
- **Transfer back to utility:** closed transition (make-before-break, ≤ 100 ms overlap).
  - If sync fails within 60 s, it falls back to open transition.
- **ALC transfer inhibit:** each ATS has a hardwired input that holds it on its present source.

## 2. Interface: Modbus TCP (Mode B) + hardwired ATS points (Mode A)

**Modbus TCP server:** one on the simulator host's IP, port 502 (configurable).
It behaves like a real Modbus TCP/RTU gateway in front of the PCC 3.3 RS-485 ports:

| Unit ID | Device | Register map |
|---|---|---|
| 1-4 | GEN-1..4 (PowerCommand 3.3) | A029X159 section 13 / 14 / 3 |
| 5 | Paralleling master (DMC 1500 style) | A029X159 section 7.1 (Communication Server 1) |

**Functions supported:**
- 03 read holding registers;
- 04 read input registers (same table);
- 06 write single register;
- 16 write multiple registers.

**Limits and exceptions:**
- Reads and writes are 1–40 contiguous registers, as in the manual.
- Unimplemented addresses return exception 02 (illegal data address).
- An unknown unit ID returns exception 0B (gateway target failed to respond).
- An offline generator (fault "Modbus communication loss") returns 0B.

**Addressing:** register 4xxxx is PDU address xxxx − 1 (40010 → 9).

**Scaling:** as in the manual.
- 32-bit values are a high/low register pair, high register first.
- "Not available" is 65535 (unsigned) / 32767 (signed).
- An open sensor is 65534 / 32766.

## 3. Generator registers implemented (PowerCommand 3.x)

| Register | Parameter | Scaling / values |
|---|---|---|
| 40010 | Control switch position | 0 Off, 1 Auto, 2 Manual |
| 40011 | Genset run sequence state | 0 Stop, 1 Time delay to start, 2 Warmup at idle, 3 Rated freq and voltage, 4 Cooldown / stop delay, 5 Cooldown at idle, 6 Rated to idle transition |
| 40012 | Most recent fault or warning | fault code |
| 40013 | Fault type | 0 None, 1 Warning, 2 Derate, 3 Shutdown with cooldown, 4 Shutdown |
| 40014 | Genset % standby total kW | ×0.01 %, signed |
| 40016 / 40017 | NFPA 110 / extended annunciation bitmaps | section 3 tables 3 and 4 |
| 40018-40021 | L-N volts L1, L2, L3, average | V |
| 40022-40025 | L-L volts L1L2, L2L3, L3L1, average | V |
| 40026-40029 | Current L1, L2, L3, average | A |
| 40031-40034 | kW L1, L2, L3, total | kW, signed |
| 40035-40038 | kVAR L1, L2, L3, total | kVAR, signed |
| 40039 | Total power factor | ×0.01 |
| 40040-40043 | kVA L1, L2, L3, total | kVA |
| 40044 | Frequency | ×0.01 Hz |
| 40061 | Battery voltage | ×0.1 V |
| 40062 | Oil pressure | kPa |
| 40064 | Coolant temperature | ×0.1 °C, signed |
| 40068 | Average engine speed | rpm |
| 40069 | Total start attempts | count |
| 40070-40071 | Engine running time | ×0.1 s, 32-bit |
| 40074-40075 | Total number of runs | 32-bit |
| 40300 | Remote start switch (Modbus) | **read/write**: 0 inactive, 1 active |
| 40301 | Fault reset (Modbus) | **read/write**: 0 inactive, 1 active |
| 40400+ | Fault status bitmaps | section 14: bits for the fault codes in section 5 |

**NFPA 110 bitmap** (40016):
- 15 common alarm
- 14 supplying load
- 13 running
- 12 not in auto
- 11 / 10 high / low battery voltage
- 9 charger AC failure
- 8 fail to start
- 7 low coolant temperature
- 6 / 5 pre-high / high engine temperature
- 4 / 3 pre-low / low oil pressure
- 2 overspeed
- 1 low coolant level
- 0 low fuel level (used for low gas pressure)

**Extended bitmap** (40017):
- 15 check genset
- 13 / 12 high / low AC voltage
- 11 underfrequency
- 10 overload
- 9 overcurrent
- 5 fail to sync
- 4 fail to close
- 3 load demand (stopped by load demand)
- 2 genset CB tripped
- 0 emergency stop

## 4. Paralleling master registers (DMC 1500, Communication Server 1)

| Register | Parameter |
|---|---|
| 40001-40003 | Warning bitmaps: genset bus under/over voltage, frequency, failed to come online, over load limit, fail to synchronize, … |
| 40006 | Status bitmap: auto open / hard closed transition, genset bus available / connected, utility bus available / connected, synchronized, genset bus load |
| 40007 | Load demand enable / disable |
| 40009-40057 | Genset bus data: L-N / L-L volts, amps, kW, kVAR, PF (×0.01), kVA, frequency (×0.1 Hz), kWh (32-bit), % values (×0.1) |
| 40059-40070 | Utility bus data: volts, amps |
| 40257 + 60·(n−1) | ATS n: device type, mode, state, fault code / type, % amps (×0.5), total kW, NFPA 110, frequency / kW / kVA / PF / volts / amps of load, source 1 and source 2 |

**ATS state:**
- 0 neutral
- 1 source 1 (normal) connected
- 2 source 2 (emergency) connected
- 3 both connected (the closed-transition overlap)

**ATS mode:** 1 utility / genset.

The master also carries the **load shed level** (0–4) and the **sets online** count *(A)*. The DMC section of the manual has no register for these, so they use a documented spare block at 40900-40909.

## 5. Generator behavior *(A timings)*

**Start sequence:**
1. Remote start: 40300 = 1, or the master calling a set.
2. Time delay to start: 1 s.
3. Crank: up to 3 attempts of 15 s with 15 s rests. Battery voltage dips while cranking.
4. Fires, then warmup at idle (0 s on an emergency start).
5. Rated frequency and voltage at about 10 s.
6. The master closes the set to the bus:
   - the first set closes to a dead bus;
   - the others synchronize (2–8 s), then close.

**Stop sequence:** start removed, then cooldown at rated with no load for 5 min, then stop.

**Control switch** (Off / Auto / Manual on the web page):
- Off: the set will not start, and "not in auto" is set in the NFPA 110 bitmap.
- Manual: the set runs unloaded until the switch is moved.

**Running values:**
- coolant warms toward 185 °F;
- oil pressure follows speed;
- run hours, starts and runs count up.

**Shutdown faults:**
- They latch until a fault reset (40301, or the web page), and reset only in Off or Auto.
- A shutdown opens the set's breaker. The master starts the reserve set.

**Faults trainees can inject (Cummins codes):**

| Code | Fault | Response |
|---|---|---|
| 1438 | Fail to crank | shutdown |
| 359 | Fail to start (cranks, will not fire) | shutdown |
| 151 | High coolant temperature (pre-high warning first) | shutdown |
| 415 | Low oil pressure | shutdown |
| 234 | Overspeed | shutdown |
| 1448 | Underfrequency (overload) | shutdown |
| 1446 / 1447 | High / low AC voltage | shutdown |
| 1472 | High current | shutdown |
| — | Low battery voltage, weak battery (slow crank) | warning |
| — | Low gas supply pressure: derate, then fail to start | derate |
| — | Fail to synchronize (runs, cannot parallel) | warning |
| — | Breaker fails to close | warning |
| — | Battery charger AC failure | warning |
| — | Modbus communication loss (gateway gets no answer) | — |

## 6. Paralleling, load demand and load shedding

- **Utility fails:** the master starts all 4 sets.
  - The first to reach rated voltage closes to the dead bus.
  - The rest synchronize and close.
- **Transfer:** each ATS transfers to emergency (open transition) once the bus is available.
  - ATS-1 goes first, the others follow 5 s apart.
  - A held transfer inhibit stops an ATS where it is.
- **Load demand:** with light load, the master stops surplus sets after a delay (10 min) but always keeps one set more than the load needs.
- **Bus response to load steps:**
  - Frequency dips about 3–4 Hz for a 25 % step of online capacity, recovering in a few seconds.
  - Gas engines dip more than diesels.
  - Voltage dips with it.
- **Overload:**
  - Above about 105 % of online capacity, frequency sags.
  - The master sheds the lowest-priority ATS: ATS-4 building, then ATS-3 HW, then ATS-2 CHW. It opens that ATS to neutral and raises the load shed signal to the ALC.
  - If frequency stays below about 55 Hz for more than 10 s, sets trip on 1448 underfrequency.
- **Utility returns:**
  - After 30 min stable (adjustable per ATS), each ATS does a closed-transition retransfer, unless inhibited.
  - The sets then cool down for 5 min and stop.

## 7. Electrical load model

Each ATS carries the live kW of the equipment behind it:

| Load | Model |
|---|---|
| Chillers | kW from the chiller model (VSD ramp on start, no inrush) |
| ACH580 drives | pumps, tower fans, AHU fans: drive output power, soft start at the accel ramp; PF 0.95 |
| Boilers | blower about 1.5 kW each while firing |
| Unit heater fans | across the line: about 6× running current for the first second |
| Building (non-HVAC) | 600 kW occupied / 200 kW unoccupied on ATS-4, same occupancy schedule; shrinks as more equipment is modelled |

- **Power factor:** about 0.85 for motor loads.
- **Power loss:** today the outage drops the whole host at once. With the generator plant, each instance gets `ats: ATS-n`.
  - An instance loses power while its ATS has no live source: during the open-transition gap, or when shed.
  - CHP-1's UPS loads (secondary pumps, controllers) stay on ATS-1 behind the UPS, as they do now.

## 8. ALC controller and devices

| ALC controller | Interface | Points |
|---|---|---|
| 243001 | Modbus TCP to the host IP, port 502, unit IDs 1-5 | generator and master registers |
| 243001 | Hardwired, simulator I/O device 4010071 (MAC 71) | `atsN_norm_pos`, `atsN_emer_pos` (BI), `atsN_xfer_inhibit` (BO), `utility_avail`, `gen_running`, `load_shed` (BI) |

- **Instance:** GEN-PLANT-1, equipment type `gen_plant`.
- **Tags:** GEN-1..4 and ATS-1..4.

## 9. CSV files (`configs/gen_plant/`)

| File | Columns |
|---|---|
| `generators.csv` | `tag`, `unit_id`, `kw_rating`, `volts`, `start_priority`, `description` |
| `ats.csv` | `tag`, `controller_device`, `priority`, `shed_order`, `retransfer_delay_min`, `description` |

Adding a generator or ATS is a row. Which instances an ATS feeds is set on each instance with `ats: ATS-n`.

## 10. Demo logic and web page

**Demo logic** (demo mode only). The mock ALC controller polls the generators and master over Modbus TCP, which exercises the real server. It:
- holds transfer inhibit on ATS-2 and ATS-3 until the bus has carried ATS-1 and ATS-4 for 30 s;
- then releases them 30 s apart;
- drops non-critical loads on load shed.

**Web page** (wide):
- utility fail / restore (host-wide);
- a one-line view showing utility, the generator bus, and each ATS's position, inhibit and kW;
- a generator table: state, kW, %, Hz, V, A, coolant, oil, battery, hours, starts, active fault code, Off / Auto / Manual and Fault Reset;
- bus Hz, V and kW against online capacity, load shed level, and a short frequency trend;
- Modbus server status and the last requests (unit ID, function, registers).

## 11. Open items

1. The generator model (C1000N6C) and its timings are assumptions. The register map and fault codes come from the Cummins documents.
2. The DMC 1500 has no load-shed-level or sets-online registers in the manual. They use a spare block (40900-40909) until the real switchgear points list is available.
3. Nothing has been tested against a real ALC Modbus TCP integration yet. The register layout should be checked once with the ALC Modbus driver (register numbering 4xxxx vs 0-based).
