"""
End-to-end BACnet/IP test: simulator <-> mock ALC controller on the loopback
host, using two UDP ports and static addresses.
"""

import asyncio
import socket

import pytest
from bacpypes3.app import Application
from bacpypes3.argparse import SimpleArgumentParser
from bacpypes3.basetypes import BinaryPV, EngineeringUnits
from bacpypes3.local.analog import AnalogInputObject, AnalogValueObject
from bacpypes3.local.oos import OutOfService
from bacpypes3.primitivedata import Null, Real

from hvacsim.runner import Simulation


class OOSAnalogInput(OutOfService, AnalogInputObject):
    pass


def host_ip() -> str:
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("192.0.2.123", 9))
        return s.getsockname()[0]
    except OSError:
        return "127.0.0.1"
    finally:
        s.close()


def config(ip, sim_port, ctl_port):
    return {
        "simulator": {"name": "UH-TEST", "device_instance": 4019999,
                      "address": f"{ip}/24:{sim_port}", "speed": 50.0, "step": 0.05},
        "equipment": {"type": "hw_unit_heater", "params": {}},
        "commands": [{"object": "binary-output,1", "name": "HWV_CMD", "input": "valve_cmd"}],
        "controller": {
            "device_instance": 240110, "address": f"{ip}:{ctl_port}", "min_interval": 0.2,
            "writes": [
                {"signal": "fan_amps", "object": "analog-input,1", "deadband": 0.05, "heartbeat": 5},
                {"signal": "space_temp", "object": "analog-value,1", "deadband": 0.1, "heartbeat": 5},
            ],
        },
        "monitor": [{"object": "binary-value,101", "name": "AQUASTAT", "signal": "aquastat_closed"}],
    }


def test_full_loop():
    asyncio.run(_full_loop())


async def _full_loop():
    ip = host_ip()
    sim_port, ctl_port = 47870, 47871
    sim = Simulation(config(ip, sim_port, ctl_port), "test")

    args = SimpleArgumentParser().parse_args(
        ["--name", "ALC", "--instance", "240110", "--address", f"{ip}/24:{ctl_port}"])
    alc = Application.from_args(args)
    ai1 = OOSAnalogInput(objectIdentifier=("analog-input", 1), objectName="FAN_AMPS",
                         presentValue=Real(0.0), units=EngineeringUnits.amperes,
                         outOfService=True, statusFlags=[0, 0, 0, 0])
    av1 = AnalogValueObject(objectIdentifier=("analog-value", 1), objectName="SPACE_TEMP",
                            presentValue=Real(0.0), units=EngineeringUnits.degreesFahrenheit,
                            outOfService=False, statusFlags=[0, 0, 0, 0])
    alc.add_object(ai1)
    alc.add_object(av1)

    tasks = [asyncio.create_task(sim.run()), asyncio.create_task(sim.link.run())]
    try:
        # controller (network output) commands the valve open at priority 10
        await alc.write_property(f"{ip}:{sim_port}", "binary-output,1", "present-value",
                                 BinaryPV.active, priority=10)
        for _ in range(100):
            await asyncio.sleep(0.2)
            if float(ai1.presentValue) > 1.5:
                break
        assert sim.model.inputs["valve_cmd"] is True
        assert sim.commands[0].obj.currentCommandPriority.unsigned == 10
        assert float(ai1.presentValue) > 1.5, "fan amps never reached the controller"
        assert 55 < float(av1.presentValue) < 80, "space temp never reached the controller"

        # monitor point is readable but not writable
        aq = await alc.read_property(f"{ip}:{sim_port}", "binary-value,101", "present-value")
        assert aq == BinaryPV.active

        # put AI:1 back in service: simulator must report the rejected write
        ai1.outOfService = False
        sim.link.points[0].last_value = None
        for _ in range(50):
            await asyncio.sleep(0.2)
            if sim.link.points[0].ok is False:
                break
        assert sim.link.points[0].ok is False
        assert "Out_Of_Service" in sim.link.points[0].last_error

        # relinquish: valve goes back to relinquish default (closed)
        await alc.write_property(f"{ip}:{sim_port}", "binary-output,1", "present-value",
                                 Null(()), priority=10)
        await asyncio.sleep(0.5)
        assert sim.model.inputs["valve_cmd"] is False
    finally:
        for t in tasks:
            t.cancel()
        sim.app.close()
        alc.close()
