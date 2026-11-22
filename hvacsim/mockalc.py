"""
A stand-in for the trainee's ALC controller.

It has the objects the real controller program will have (inputs placed
Out_Of_Service, found by object name) and a simple thermostat block per unit
that writes the simulator's valve command, like an Eikon network output.

Used by `python -m hvacsim <config> --demo` (in-process, no network needed)
and by tools/mock_alc.py (on the real BACnet/IP network).
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Any, Dict, List, Optional

from bacpypes3.apdu import ErrorRejectAbortNack
from bacpypes3.app import Application
from bacpypes3.basetypes import BinaryPV, EngineeringUnits
from bacpypes3.local.analog import AnalogInputObject, AnalogValueObject
from bacpypes3.local.binary import BinaryInputObject, BinaryValueObject
from bacpypes3.local.oos import OutOfService
from bacpypes3.pdu import Address
from bacpypes3.primitivedata import ObjectIdentifier, Real

from .bacnet_io import _device, describe_error, objid_text, parse_objid


class OOSAnalogInput(OutOfService, AnalogInputObject):
    """Present_Value writable only while Out_Of_Service (like a real controller input)."""


class OOSBinaryInput(OutOfService, BinaryInputObject):
    pass


@dataclass
class Thermostat:
    label: str
    zone_object: str            # object name holding the zone temperature
    target_device: int          # simulator device to command
    target_object: str = "binary-output,1"
    target_address: Optional[str] = None
    setpoint: float = 68.0
    diff: float = 1.0
    priority: int = 10
    call: bool = False
    last_sent: Optional[bool] = None
    last_send_t: float = 0.0
    status: str = ""


class MockController:
    def __init__(self, device_instance: int, port, name: str = "ALC-MOCK"):
        self.device_instance = int(device_instance)
        self.app = Application.from_object_list([_device(device_instance, name, modelName="Mock ALC controller"), port])
        self.thermostats: List[Thermostat] = []
        self.outputs: List[Dict[str, Any]] = []
        self._next_av = 100

    # ----- objects ------------------------------------------------------

    def add_point(self, object_name: str, objid: Optional[str], units: str = "noUnits") -> None:
        """Create an object; a missing or already-used object id gets the next free instance."""
        if object_name in self.app.objectName:
            return
        oid = parse_objid(objid or "analog-value,1000")
        kind = str(oid[0])
        inst = oid[1]
        while (oid[0], inst) in self.app.objectIdentifier or ObjectIdentifier(f"{kind},{inst}") in self.app.objectIdentifier:
            inst += 1
        oid = ObjectIdentifier(f"{kind},{inst}")
        common = dict(objectIdentifier=oid, objectName=object_name, statusFlags=[0, 0, 0, 0])
        if kind == "analog-input":
            obj = OOSAnalogInput(presentValue=Real(0.0), units=EngineeringUnits(units), outOfService=True, **common)
        elif kind == "analog-value":
            obj = AnalogValueObject(presentValue=Real(0.0), units=EngineeringUnits(units), outOfService=False, **common)
        elif kind == "binary-input":
            obj = OOSBinaryInput(presentValue=BinaryPV.inactive, outOfService=True, polarity="normal", **common)
        elif kind == "binary-value":
            obj = BinaryValueObject(presentValue=BinaryPV.inactive, outOfService=False, **common)
        else:
            raise ValueError(f"mock controller cannot create {kind}")
        self.app.add_object(obj)

    def add_thermostat(self, t: Thermostat) -> None:
        self.add_point(f"{t.label}_htg_sp", f"analog-value,{self._next_av}", "degreesFahrenheit")
        self.app.objectName[f"{t.label}_htg_sp"].presentValue = Real(t.setpoint)
        self._next_av += 1
        self.thermostats.append(t)

    def add_network_output(self, device: int, objid: str, value: Any, priority: int = 10, label: str = "") -> None:
        """A fixed network output to a simulator (demo): rewritten every 10 s."""
        self.outputs.append({"device": device, "object": objid, "value": value, "priority": priority,
                             "label": label, "address": None, "status": ""})

    def value(self, object_name: str) -> Any:
        obj = self.app.objectName.get(object_name)
        if obj is None:
            return None
        pv = obj.presentValue
        return (pv == BinaryPV.active) if isinstance(pv, BinaryPV) else round(float(pv), 2)

    # ----- logic --------------------------------------------------------

    async def _find(self, t: Thermostat) -> bool:
        if t.target_address:
            return True
        i_ams = await self.app.who_is(t.target_device, t.target_device, timeout=3)
        if i_ams:
            t.target_address = str(i_ams[0].pduSource)
            return True
        t.status = f"simulator device {t.target_device} not found"
        return False

    async def _network_outputs(self) -> None:
        for o in self.outputs:
            try:
                if o["address"] is None:
                    i_ams = await self.app.who_is(o["device"], o["device"], timeout=3)
                    if not i_ams:
                        o["status"] = f"device {o['device']} not found"
                        continue
                    o["address"] = str(i_ams[0].pduSource)
                oid = parse_objid(o["object"])
                v = o["value"]
                pv = (BinaryPV.active if v else BinaryPV.inactive) if str(oid[0]).startswith("binary") else Real(float(v))
                await self.app.write_property(Address(o["address"]), oid, "present-value", pv, priority=o["priority"])
                o["status"] = f"wrote {v} @ {o['priority']}"
            except (Exception, ErrorRejectAbortNack) as err:
                o["status"] = f"failed: {describe_error(err)}"
                o["address"] = None

    async def run(self) -> None:
        loop = asyncio.get_running_loop()
        tick = 0
        while True:
            await asyncio.sleep(1.0)
            if self.outputs and tick % 10 == 0:
                await self._network_outputs()
            tick += 1
            for t in self.thermostats:
                temp = self.value(t.zone_object) or 0.0
                sp_obj = self.app.objectName.get(f"{t.label}_htg_sp")
                sp = float(sp_obj.presentValue) if sp_obj is not None else t.setpoint
                if temp > 0:
                    if not t.call and temp < sp - t.diff / 2:
                        t.call = True
                    elif t.call and temp > sp + t.diff / 2:
                        t.call = False
                now = loop.time()
                if t.call == t.last_sent and now - t.last_send_t < 60:
                    continue
                try:
                    if not await self._find(t):
                        continue
                    await self.app.write_property(Address(t.target_address), t.target_object, "present-value",
                                                  BinaryPV.active if t.call else BinaryPV.inactive,
                                                  priority=t.priority)
                    t.last_sent, t.last_send_t = t.call, now
                    t.status = f"wrote {'ON' if t.call else 'OFF'} @ {t.priority}"
                except (Exception, ErrorRejectAbortNack) as err:
                    t.status = f"network output failed: {describe_error(err)}"
                    t.target_address = None

    def snapshot(self) -> Dict[str, Any]:
        points = []
        for name, obj in self.app.objectName.items():
            if str(obj.objectIdentifier[0]) in ("device", "network-port"):
                continue
            points.append({"name": name, "object": objid_text(obj.objectIdentifier), "value": self.value(name),
                           "out_of_service": bool(getattr(obj, "outOfService", False))})
        return {
            "device_instance": self.device_instance,
            "points": points,
            "thermostats": [{"label": t.label, "call": t.call, "status": t.status} for t in self.thermostats],
            "outputs": [{"label": o["label"], "object": o["object"], "value": o["value"], "status": o["status"]}
                        for o in self.outputs],
        }
