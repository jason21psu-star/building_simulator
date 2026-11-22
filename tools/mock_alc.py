"""
Stand-in for a trainee's ALC controller on the real BACnet/IP network, for
testing a simulator host before the real controllers are connected.

Defaults match configs/station-1.yaml (UH-1 and UH-2 on controller 240110):

  python tools/mock_alc.py --address 192.168.1.20/24

  --point NAME=OBJECT     object to create, e.g. fan_amps_1=analog-input,1
                          (analog/binary inputs start Out_Of_Service)
  --thermostat ZONE=DEV   heating logic: read object ZONE, write BO-1 of
                          simulator device DEV (found with Who-Is)

For a quick look without any network, use `python -m hvacsim <config> --demo`.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from bacpypes3.local.networkport import NetworkPortObject  # noqa: E402

from hvacsim.mockalc import MockController, Thermostat  # noqa: E402

DEFAULT_POINTS = ["fan_amps_1=analog-input,1", "zone_temp_1=analog-value,1",
                  "fan_amps_2=analog-input,2", "zone_temp_2=analog-value,2"]
DEFAULT_TSTATS = ["zone_temp_1=4010001", "zone_temp_2=4010002"]


async def amain(a) -> None:
    port = NetworkPortObject(a.address, objectIdentifier=("network-port", 1), objectName="NP-IP")
    mock = MockController(a.instance, port, name="ALC-MOCK")
    for item in a.point or DEFAULT_POINTS:
        name, _, obj = item.partition("=")
        units = "amperes" if "amp" in name else "degreesFahrenheit" if "temp" in name else "noUnits"
        mock.add_point(name, obj, units)
    for item in a.thermostat or DEFAULT_TSTATS:
        zone, _, dev = item.partition("=")
        mock.add_thermostat(Thermostat(label=zone.replace("zone_temp", "tstat"), zone_object=zone,
                                       target_device=int(dev), setpoint=a.setpoint, priority=a.priority))
    print(f"mock ALC device {a.instance} on {a.address}")
    task = asyncio.create_task(mock.run())
    while True:
        await asyncio.sleep(5)
        snap = mock.snapshot()
        vals = "  ".join(f"{p['name']}={p['value']}" for p in snap["points"] if not p["name"].endswith("_htg_sp"))
        ts = "  ".join(f"{t['label']}:{'CALL' if t['call'] else 'off'} ({t['status']})" for t in snap["thermostats"])
        print(f"{vals} | {ts}", flush=True)
        if task.done():
            task.result()


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--instance", type=int, default=240110)
    ap.add_argument("--address", required=True, help="this mock controller's BACnet/IP address, e.g. 192.168.1.20/24")
    ap.add_argument("--point", action="append", metavar="NAME=OBJECT")
    ap.add_argument("--thermostat", action="append", metavar="ZONE_OBJECT=SIM_DEVICE")
    ap.add_argument("--priority", type=int, default=10)
    ap.add_argument("--setpoint", type=float, default=68.0)
    asyncio.run(amain(ap.parse_args()))


if __name__ == "__main__":
    main()
