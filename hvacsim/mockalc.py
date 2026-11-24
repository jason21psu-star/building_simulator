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


class AhuDemoLogic:
    """
    A deliberately simple stand-in for a trainee's AHU program (demo mode only):
    all fans on, duct static PI on the four supply fan speeds, return fan tracking
    at 80 %, SAT 55 °F on cooling / heating, preheat to MAT 45 °F, OA dampers at minimum.
    Signals are written as % of 10 V (valves/dampers 2-10 V, drive speeds 0-10 V).
    """

    def __init__(self, mock: "MockController", device: int, label: str = "AHU"):
        self.mock, self.device, self.label = mock, device, label
        self.address: Optional[str] = None
        self.speed, self.clg, self.htg, self.ph = 50.0, 0.0, 0.0, 0.0
        self.last: Dict[str, Any] = {}
        self.last_t: Dict[str, float] = {}
        self.status = ""

    async def tick(self) -> None:
        m = self.mock
        if self.address is None:
            i_ams = await m.app.who_is(self.device, self.device, timeout=3)
            if not i_ams:
                self.status = f"AHU device {self.device} not found"
                return
            self.address = str(i_ams[0].pduSource)
        v = lambda n: m.value(n) or 0.0
        safeties = all(m.value(n) is not False for n in ("frz_stat", "hi_static", "ra_smoke", "sa_smoke"))
        self.speed = max(30.0, min(100.0, self.speed + 1.5 * (1.5 - v("sa_static"))))
        sat = v("sa_temp")
        self.clg = max(0.0, min(100.0, self.clg + 0.6 * (sat - 55.0)))
        self.htg = max(0.0, min(100.0, self.htg + 0.6 * (52.0 - sat))) if self.clg < 1 else 0.0
        self.ph = max(0.0, min(100.0, self.ph + 0.8 * (45.0 - v("ma_temp"))))
        if not safeties:
            self.clg, self.ph = 50.0, 100.0          # freeze protection per the sequence
        two_ten = lambda pct: 20.0 + 0.8 * pct
        outs = {
            "binary-output,2": True, **{f"binary-output,{i}": True for i in range(3, 7)},   # RF + SF1-4 start
            "analog-output,1": two_ten(20.0 if safeties else 0.0),                            # min OA 20 %
            "analog-output,2": two_ten(self.ph),
            "analog-output,3": two_ten(self.clg),
            "analog-output,4": two_ten(self.htg),
            "analog-output,6": max(20.0, 0.8 * self.speed),                                   # RF tracks 80 %
            **{f"analog-output,{i}": self.speed for i in range(7, 11)},                       # SF1-4 speed
        }
        loop = asyncio.get_running_loop()
        now = loop.time()
        for obj, val in outs.items():
            prev = self.last.get(obj)
            changed = prev is None or (abs(float(val) - float(prev)) > 0.5 if not isinstance(val, bool) else val != prev)
            if not changed and now - self.last_t.get(obj, 0.0) < 20.0:
                continue
            pv = (BinaryPV.active if val else BinaryPV.inactive) if isinstance(val, bool) else Real(float(val))
            try:
                await m.app.write_property(Address(self.address), obj, "present-value", pv, priority=10)
                self.last[obj], self.last_t[obj] = val, now
            except (Exception, ErrorRejectAbortNack) as err:
                self.status = f"write {obj} failed: {describe_error(err)}"
                self.address = None
                return
        self.status = f"static {v('sa_static'):.2f} -> fans {self.speed:.0f} %, SAT {sat:.1f} -> clg {self.clg:.0f} % htg {self.htg:.0f} %"


class VavDemoLogic:
    """
    Demo-only stand-in for a trainee's VAV program: occupied 75/70 °F (+ setpoint adjust),
    cooling airflow between 30 % and 100 % of box max, damper PI on airflow (pressure
    independent), reheat to the heating setpoint with a 95 °F discharge limit.
    """

    def __init__(self, mock: "MockController", device: int, box_max: float, label: str = "VAV"):
        self.mock, self.device, self.box_max, self.label = mock, device, box_max, label
        self.address: Optional[str] = None
        self.dmpr, self.rh = 50.0, 0.0
        self.last: Dict[str, Any] = {}
        self.last_t: Dict[str, float] = {}
        self.status = ""

    async def tick(self) -> None:
        m = self.mock
        if self.address is None:
            i_ams = await m.app.who_is(self.device, self.device, timeout=3)
            if not i_ams:
                self.status = f"VAV device {self.device} not found"
                return
            self.address = str(i_ams[0].pduSource)
        v = lambda n: m.value(n) or 0.0
        t, adj, flow = v("zone_temp_1"), v("stpt_adj_1"), v("airflow_1")
        clg, htg = 75.0 + adj, 70.0 + adj
        mn, mx = 0.3 * self.box_max, self.box_max
        if t > clg:
            sp = mn + (mx - mn) * max(0.0, min(1.0, (t - clg) / 2.0))
        else:
            sp = mn
        self.dmpr = max(0.0, min(100.0, self.dmpr + 0.02 * (sp - flow)))
        if t < htg and v("da_temp_1") < 95.0:
            self.rh = min(100.0, self.rh + 1.0 * (htg - t) + 0.2)
        else:
            self.rh = max(0.0, self.rh - 1.0)
        outs = {"analog-output,1": self.dmpr, "analog-output,2": 20.0 + 0.8 * self.rh}
        loop = asyncio.get_running_loop()
        now = loop.time()
        for obj, val in outs.items():
            prev = self.last.get(obj)
            if prev is not None and abs(val - prev) <= 0.5 and now - self.last_t.get(obj, 0.0) < 20.0:
                continue
            try:
                await m.app.write_property(Address(self.address), obj, "present-value", Real(float(val)), priority=10)
                self.last[obj], self.last_t[obj] = val, now
            except (Exception, ErrorRejectAbortNack) as err:
                self.status = f"write {obj} failed: {describe_error(err)}"
                self.address = None
                return
        self.status = f"zone {t:.1f} (clg {clg:.1f}/htg {htg:.1f}) flow {flow:.0f}/{sp:.0f} cfm, damper {self.dmpr:.0f} %, reheat {self.rh:.0f} %"


class MockController:
    def __init__(self, device_instance: int, port, name: str = "ALC-MOCK"):
        self.device_instance = int(device_instance)
        self.app = Application.from_object_list([_device(device_instance, name, modelName="Mock ALC controller"), port])
        self.thermostats: List[Thermostat] = []
        self.outputs: List[Dict[str, Any]] = []
        self.logics: List[Any] = []
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
            for logic in self.logics:
                await logic.tick()
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
            "logics": [{"label": lg.label, "status": lg.status} for lg in self.logics],
        }
