"""
Stand-in for the trainee's ALC controller, for testing the simulator without
hardware.  It mimics what the real controller will have:

  AI:1  Fan amps      physical input, Out_Of_Service = True (simulator writes it)
  AV:1  Space temp    written by the simulator
  AV:2  Heating setpoint
  BV:1  Heat call     logic result
  BI-like status: fan proven when AI:1 > 0.5 A

and a simple heating logic block that writes the simulator's BO-1 through a
"network output" at a configurable priority.

  python tools/mock_alc.py --address 192.168.1.20/24 --sim-address 192.168.1.51
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from bacpypes3.app import Application  # noqa: E402
from bacpypes3.argparse import SimpleArgumentParser  # noqa: E402
from bacpypes3.basetypes import BinaryPV, EngineeringUnits  # noqa: E402
from bacpypes3.local.analog import AnalogInputObject, AnalogValueObject  # noqa: E402
from bacpypes3.local.binary import BinaryValueObject  # noqa: E402
from bacpypes3.local.oos import OutOfService  # noqa: E402
from bacpypes3.primitivedata import Real  # noqa: E402


class OOSAnalogInput(OutOfService, AnalogInputObject):
    """Present_Value is only writable while Out_Of_Service is True (like a real controller)."""


async def amain(a) -> None:
    args = SimpleArgumentParser().parse_args(
        ["--name", "ALC-MOCK", "--instance", str(a.instance), "--address", a.address,
         "--vendoridentifier", "999"])
    app = Application.from_args(args)

    fan_amps = OOSAnalogInput(objectIdentifier=("analog-input", 1), objectName="UH1_FAN_AMPS",
                              presentValue=Real(0.0), units=EngineeringUnits.amperes,
                              outOfService=not a.in_service, statusFlags=[0, 0, 0, 0])
    space = AnalogValueObject(objectIdentifier=("analog-value", 1), objectName="UH1_SPACE_TEMP",
                              presentValue=Real(0.0), units=EngineeringUnits.degreesFahrenheit,
                              statusFlags=[0, 0, 0, 0], outOfService=False)
    setpoint = AnalogValueObject(objectIdentifier=("analog-value", 2), objectName="UH1_HTG_SP",
                                 presentValue=Real(a.setpoint), units=EngineeringUnits.degreesFahrenheit,
                                 statusFlags=[0, 0, 0, 0], outOfService=False)
    heat_call = BinaryValueObject(objectIdentifier=("binary-value", 1), objectName="UH1_HEAT_CALL",
                                  presentValue=BinaryPV.inactive, statusFlags=[0, 0, 0, 0], outOfService=False)
    for obj in (fan_amps, space, setpoint, heat_call):
        app.add_object(obj)

    call = False
    last_sent = None
    last_send_t = 0.0
    loop = asyncio.get_running_loop()
    print(f"mock ALC device {a.instance} on {a.address}; commanding sim BO-1 at {a.sim_address} priority {a.priority}")
    while True:
        await asyncio.sleep(1.0)
        t = float(space.presentValue)
        sp = float(setpoint.presentValue)
        if t > 0:  # no reading yet = 0
            if not call and t < sp - a.diff / 2:
                call = True
            elif call and t > sp + a.diff / 2:
                call = False
        heat_call.presentValue = BinaryPV.active if call else BinaryPV.inactive

        now = loop.time()
        if call != last_sent or now - last_send_t > 60:
            try:
                await app.write_property(a.sim_address, "binary-output,1", "present-value",
                                         BinaryPV.active if call else BinaryPV.inactive,
                                         priority=a.priority)
                last_sent, last_send_t = call, now
            except BaseException as err:  # bacpypes3 BACnet errors derive from BaseException
                if isinstance(err, (KeyboardInterrupt, asyncio.CancelledError)):
                    raise
                print("network output write failed:", err.__class__.__name__, getattr(err, 'apduAbortRejectReason', '') or getattr(err, 'errorCode', ''))
        proven = float(fan_amps.presentValue) > 0.5
        print(f"space={t:6.2f}F sp={sp:.1f} heat_call={'ON ' if call else 'OFF'} "
              f"fan_amps={float(fan_amps.presentValue):4.2f}A fan_status={'PROVEN' if proven else 'off'}",
              flush=True)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--instance", type=int, default=240110)
    ap.add_argument("--address", required=True, help="this mock controller's BACnet/IP address")
    ap.add_argument("--sim-address", required=True, help="simulator address, e.g. 192.168.1.51")
    ap.add_argument("--priority", type=int, default=16)
    ap.add_argument("--setpoint", type=float, default=68.0)
    ap.add_argument("--diff", type=float, default=1.0)
    ap.add_argument("--in-service", action="store_true", help="leave AI:1 in service (to test the error path)")
    asyncio.run(amain(ap.parse_args()))


if __name__ == "__main__":
    main()
