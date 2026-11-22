# ACH580-style VFD simulator

Equipment type `vfd_fan`: an ABB ACH580-style HVAC drive running a
direct-drive plenum fan. The drive model, `hvacsim/models/components/vfd.py`,
is a reusable component. The AHU will put four of them in its supply fan
array.

> **Verify against your drives.** Parameter numbers, the BACnet object list
> and fault codes follow ABB's ACH580 documentation as I know it. Check them
> against the firmware manual for the drives in your lab. The object list
> lives in the templates, so you can fix it without code changes.

## Two ways to control it

Like the real drive, the **drive's own parameters** decide how it's
controlled. Trainees have to set them correctly.

| | Hardwired (`templates/vfd_ach580_hardwired.yaml`) | Fieldbus (`templates/vfd_ach580_bacnet.yaml`) |
|---|---|---|
| 20.01 Ext1 commands | `in1_start` (DI1) | `fieldbus` |
| 28.11 Ext1 frequency ref1 | `ai1_scaled` | `efb_ref1` |
| Start | ALC BO → **DI1 TERMINAL** (sim BV-1001) | ALC writes **RUN/STOP CMD** (BV-10) |
| Speed | ALC AO 0-10 V → **AI1 TERMINAL** (sim AV-1001, % of 10 V) | ALC writes **INPUT REF 1** (AV-16, % of 46.02) |
| Status | RO2 (running) → ALC BI `sf{n}_status` | ALC reads **RUN/STOP ACT** (BV-0) |
| Fault | RO3 (fault −1, opens on fault) → ALC BI `sf{n}_fault` | ALC reads **FAULT ACT** (BV-2), **LAST FLT** (AV-18) |
| Speed feedback | AO1 → ALC AI `sf{n}_speed_fb` (% of 10 V) | ALC reads **OUTPUT FREQ** (AV-1) |
| Safety circuit | Freezestat/smoke on **DI4**, 20.40 Run permissive = DI4 | Same wiring can be used |

Trainees have to scale the feedback themselves. AO1 is written as % of
10 V, the same as a 0-10 V input at the controller, so the ALC AI decides
what 0-10 V means (0-60 Hz, 0-100 %, …).

## Simulated parameters

| No. | Name | Default |
|---|---|---|
| 99.06-99.10 | Motor nameplate | 14 A, 460 V, 60 Hz, 1770 rpm, 7.5 kW (10 HP) |
| 20.01 | Ext1 commands | per template |
| 20.12 | Run enable 1 source | selected |
| 20.40 | Run permissive | not used / DI4 (hardwired template) |
| 20.41 | Start interlock 1 | not used |
| 21.03 | Stop mode | coast |
| 12.17-12.20 | AI1 min/max, scaled min/max | 0-10 V = 0-60 Hz |
| 28.11 | Ext1 frequency ref1 | per template |
| 28.41 | Frequency ref safe | 30 Hz |
| 28.72 / 28.73 | Accel / decel time 1 | 30 s / 30 s |
| 30.13 / 30.14 | Min / max frequency | 12 / 60 Hz (templates) |
| 30.17 | Maximum current | 19.6 A |
| 30.30 | Overvoltage control | enabled |
| 31.01 | External event 1 source | not used |
| 31.27 / 31.28 | Stall frequency / time | 15 Hz / 20 s |
| 10.24 / 10.27 / 10.30 | RO1 / RO2 / RO3 source | ready run / running / fault (−1) |
| 13.12 | AO1 source | output frequency |
| 46.02 | Frequency scaling | 60 Hz |
| 58.14 / 58.15 / 58.16 | Comm loss action / mode / time | no action (fault in the fieldbus template) / any message / 30 s |
| 70.02 / 70.03 / 70.05 | Override enable / source / frequency | off / DI6 / 60 Hz |

All of these can be changed on the web page (*Parameters* panel) or in YAML.
ACCEL1 TIME (AV-23) and DECEL1 TIME (AV-24) are also writable over BACnet.

## Keypad

The web page has an ACH580-style panel with **Off / Hand / Auto / Reset** and
a Hand reference. A drive left in Hand or Off is a classic service call:
the controller commands it, and nothing happens.

## Behaviour

- **Ramps and current limit:** acceleration slows down if the current limit is reached.
- **Fan load:** torque follows the fan law (~speed²), and current includes magnetizing current.
- **Coast stop and flying start:** with stop mode coast, the fan spins down on its own. If the drive restarts while the fan is still turning, it catches the fan at its current speed.
- **Ramp stop with a short decel time:** the fan returns energy to the drive. With 30.30 Overvoltage control on, the drive extends the decel and shows a warning. With it off, the drive trips **3210 DC link overvoltage**.
- **Permissive, interlock and run enable:** losing any of them stops the drive with a warning. It restarts by itself when the signal returns.
- **Fieldbus comm loss (58.14):** triggers when nothing has talked to the drive for 58.16 seconds. In *any message* mode, WebCTRL polling counts as activity. In *cw_ref* mode, only writes to RUN/STOP CMD and INPUT REF 1 count.
- **Override (fire mode):** runs at the override frequency and ignores permissives, interlocks and faults.

## Fault codes and fault injection

| Code | Text | Caused by |
|---|---|---|
| 2310 | Overcurrent | Fault injection: overcurrent trip |
| 2330 | Earth leakage | Fault injection: motor insulation failure |
| 3130 | Input phase loss | Fault injection, under load |
| 3210 | DC link overvoltage | Decel too short with 30.30 off |
| 3220 | DC link undervoltage | Fault injection: low supply voltage |
| 4310 | Excess temperature | Fault injection: drive cooling fan failed (takes a while) |
| 5091 | Safe torque off | STO circuit opened while running |
| 6681 | EFB comm loss | Fieldbus quiet longer than 58.16 with 58.14 = fault |
| 7121 | Motor stall | Fan wheel seized |
| 9081 | External fault 1 | 31.01 input opened |

Field and mechanical faults that the drive doesn't flag itself:
- **AI1 wire broken:** the reference drops to minimum frequency.
- **Coupling failed:** the motor runs with low amps and moves no air.
- **Worn bearings:** high load and amps.
- **Discharge damper closed:** low amps and low airflow.
- **Safety circuit open:** opens the DI set by `safety_di`.

## Using it in the AHU

The AHU model will own four `Vfd` components, one per array fan, each with
its own fan load. Each drive can be exposed as its own BACnet device
(fieldbus) or through terminal objects (hardwired), using the same object
lists as these templates.
