# ACH580-style VFD simulator

Equipment type `vfd_fan` with `drive_model: ach580` (the default): an ABB
ACH580-style HVAC drive running a direct-drive plenum fan. The drive engine is
`hvacsim/models/components/vfd.py`, behind the drive interface
(`components/drives/ach580.py`, see [DRIVE_INTERFACE_DESIGN.md](DRIVE_INTERFACE_DESIGN.md)).
The same drive is used by the AHU fan array and the plant pumps, and any of
them can be swapped for a Yaskawa Z1000 ([VFD_Z1000.md](VFD_Z1000.md)).

Parameter numbers, names, choices, defaults, and fault and warning codes
follow the **ACH580 HVAC control program firmware manual
(3AXD50000027537 Rev C)**. Values that aren't from the manual are labelled
*simulator*.

## Two ways to control it

Like the real drive, the **drive's own parameters** decide how it's
controlled.

| | Hardwired (`templates/vfd_ach580_hardwired.yaml`) | Fieldbus (`templates/vfd_ach580_bacnet.yaml`) |
|---|---|---|
| 20.01 Ext1 commands | `in1_start` (DI1, manual default) | `fieldbus` |
| 28.11 Ext1 frequency ref1 | `ai1_scaled` (manual default) | `efb_ref1` |
| Start | ALC BO → **DI1 TERMINAL** (sim BV-1001) | ALC writes **RUN/STOP CMD** (BV-10) |
| Speed | ALC AO 0-10 V → **AI1 TERMINAL** (sim AV-1001, % of 10 V) | ALC writes **INPUT REF 1** (AV-16, % of 46.02) |
| Status | RO2 (running) → ALC BI `sf{n}_status` | ALC reads **RUN/STOP ACT** (BV-0) |
| Fault | RO3 (Fault (-1), opens on fault) → ALC BI `sf{n}_fault` | ALC reads **FAULT ACT** (BV-2), **LAST FLT** (AV-18) |
| Speed feedback | AO1 → ALC AI `sf{n}_speed_fb` (% of 10 V) | ALC reads **OUTPUT FREQ** (AV-1) |
| Safety circuit | DI4 = **20.41 Start interlock 1** (manual default): stops the drive when open | 20.41 = Selected (no DI wiring) |

> The BACnet object list for fieldbus control is in the template. It still
> needs checking against the drive's BACnet documentation, because the
> firmware manual doesn't list BACnet objects.

## 28.76 Freq ramp in zero source

The source forces the frequency reference to zero: **0 = force the reference
to zero, 1 = normal operation**.

| Choice | Effect |
|---|---|
| `inactive` (default) | Constant 1: normal operation |
| `active` | Constant 0: reference always forced to zero |
| `di1` … `di6` | DI **open** = hold zero speed, DI closed = normal |

What happens in the simulator:

- The reference into the ramp becomes 0 Hz. The drive **decelerates on the
  active decel ramp** (28.73) and **stays running** at 0 Hz with the motor
  magnetized. RO "Running" stays closed, and current is about magnetizing
  current (≈ 35 % of 99.06).
- It holds 0 Hz even when 30.13 Minimum frequency is above zero. This follows
  the control chain diagram, where the forced zero is applied at the ramp input
  (28.01).
- When the source returns to 1, the drive **accelerates back to the reference
  without a new start command**.
- It applies in remote control (Auto / Ext1) only. The keypad Hand reference
  isn't affected, and neither is Override.
- Holding zero speed doesn't count as a stall. The status shows `RUNNING · RAMP
  IN ZERO`, and **06.19 bit 0 Zero speed** comes on once the shaft is below
  21.06 Zero speed limit for 21.07 Zero speed delay. That's available as relay
  source `zero_speed`.

**Not simulated:** the control word bit 6 "Ramp in zero" (06.01), which the
manual ORs with 28.76.

> **Verify on a real drive:** whether 30.13 Minimum frequency still applies
> while ramp in zero is active, and whether it affects Hand.

## Simulated parameters

| No. | Name | Default (manual) | Notes |
|---|---|---|---|
| 99.06-99.10 | Motor nominal current / voltage / frequency / speed / power | 0 (set at start-up) | Simulator: 14 A, 460 V, 60 Hz, 1770 rpm, 7.5 kW (10 HP) |
| 20.01 | Ext1 commands | In1 Start | `not_selected`, `in1_start`, `in1_start_in2_dir`, `fieldbus` |
| 20.40 | Run permissive | Selected | Not selected / Selected / DI1-6 / -DI1-6 / Embedded fieldbus (BV-12 RUN ENA CMD). Losing it stops the drive (coast) and shows warning AFED. It restarts by itself when the signal returns |
| 20.41 | Start interlock 1 | **DI4** | Not selected / Selected / DI1-6 / Embedded fieldbus. Open = **prevents starting and stops a running drive**, warning AFEE. The drive restarts by itself when the interlock closes and the start command is still on |
| 20.45 | Start interlock stop mode | Not used | How the drive stops when the interlock opens. Not used / Coast = **coast stop regardless of 21.03**, so the fan sheds duct pressure after a high-static trip. Ramp = ramped stop |
| 21.03 | Stop mode | Coast | Coast / Ramp |
| 21.06 / 21.07 | Zero speed limit / delay | 30 rpm / 0 ms | A ramped stop ends at the limit, then the motor coasts. Also sets the zero speed status |
| 12.17-12.20 | AI1 min / max / scaled min / scaled max | 0 V / 10 V / 0 / 60 Hz | |
| 28.11 | Ext1 frequency ref1 | AI1 scaled | `zero`, `ai1_scaled`, `efb_ref1`, `panel` |
| 28.41 | Frequency ref safe | 0.00 Hz | Used by 58.14 = Speed ref safe |
| 28.72 / 28.73 | Freq acceleration / deceleration time 1 | 20 s / 20 s | 0 → 46.02 and 46.02 → 0 |
| **28.76** | **Freq ramp in zero source** | **Inactive** | See above |
| 30.13 / 30.14 | Minimum / maximum frequency | 0 / 60 Hz | |
| 30.17 | Maximum current | drive dependent | Simulator: 19.6 A |
| 30.30 | Overvoltage control | Enable | Off with a short decel time → 3210 |
| 31.01 / 31.02 | External event 1 source / type | Inactive (true) / Fault | Source 0 = event. Type Fault → 9081, Warning → A981 |
| 31.27 / 31.28 | Stall frequency limit / time | 15 Hz / 20 s | |
| 10.24 / 10.27 / 10.30 | RO1 / RO2 / RO3 source | **Damper control** / Running / Fault (-1) | Also Ready run, Enabled, Started, Magnetized, Ready ref, At setpoint, Zero speed, Warning, Fault, Fault/Warning |
| 13.12 | AO1 source | Output frequency | |
| 46.02 | Frequency scaling | 60 Hz | |
| 58.14 | Communication loss action | **Fault** | No action / Fault / Last speed / Speed ref safe / Fault always / Warning. Warnings use code A7CE |
| 58.15 | Communication loss mode | **Cw / Ref1 / Ref2** | Any message / Cw-Ref (only writes to RUN/STOP CMD and INPUT REF 1 count) |
| 58.16 | Communication loss time | 30 s | |
| 70.02 / 70.03 / 70.06 | Override enable / activation source / override frequency | Off / Not used / 0 Hz | |

**Damper control (RO1 default)** energizes when the drive is started. The
damper actuator opens, and its end switch wired to DI4 (Start interlock 1)
lets the drive start.

## Keypad

The web page has an ACH580-style panel with **Off / Hand / Auto / Reset** and
a Hand reference.

## Behaviour

- **Ramps and current limit:** acceleration slows down if the current limit is reached.
- **Fan load:** torque follows the fan law (~speed²), and current includes magnetizing current.
- **Coast stop and flying start:** with stop mode coast, the fan spins down on its own. If the drive restarts while the fan is still turning, it catches the fan at its current speed.
- **Ramp stop with a short decel time:** with 30.30 on, the drive extends the decel. With it off, it trips **3210**.
- **Fieldbus comm loss:** follows 58.14-58.16. *Fault always* and *Warning* supervise even when control isn't from the fieldbus.
- **Override (fire mode):** runs at 70.06 and ignores permissives, interlocks and faults.

## Fault and warning codes (from the manual)

| Code | Text | Caused by |
|---|---|---|
| 2310 | Overcurrent | Fault injection |
| 2330 | Earth leakage | Fault injection: motor insulation failure |
| 3130 | Input phase loss | Fault injection, under load |
| 3210 | DC link overvoltage | Decel too short with 30.30 off |
| 3220 | DC link undervoltage | Fault injection: low supply voltage |
| 4310 | Excess temperature | Drive cooling fan failed (takes a while) |
| 5091 | Safe torque off | STO circuit opened while running |
| 6681 | EFB comm loss | 58.14 = Fault / Fault always |
| 7121 | Motor stall | Fan wheel seized |
| 9081 | External fault 1 | 31.01 source = 0 with 31.02 = Fault |
| A4A9 | Cooling | Drive temperature high |
| A5A0 | Safe torque off | STO open |
| A7CE | EFB comm loss | 58.14 = Last speed / Speed ref safe / Warning |
| A981 | External warning 1 | 31.01 source = 0 with 31.02 = Warning |
| AFED | Run permissive | 20.40 source = 0 |
| AFEE | Start interlock 1 | 20.41 source = 0 |

Field and mechanical faults the drive doesn't flag itself:
- **AI1 wire broken:** the reference drops to minimum frequency.
- **Coupling failed:** the motor runs with low amps and moves no air.
- **Worn bearings:** high load and amps.
- **Discharge damper closed:** low amps and low airflow.
- **Safety circuit open:** opens the DI set by `safety_di`.

## Using it in the AHU

The AHU owns five `Vfd` components (SF-1…4, RF) with hardwired control. The
safety chain goes to DI4 (20.41 Start interlock 1), which stops the fans when
any safety opens. See `docs/AHU_DESIGN.md`.
