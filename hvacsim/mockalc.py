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

    def add_network_output(self, device: int, objid: str, value: Any, priority: int = 10, label: str = "",
                           every: int = 10) -> None:
        """A fixed network output to a simulator (demo): rewritten every `every` s (default 10)."""
        self.outputs.append({"device": device, "object": objid, "value": value, "priority": priority,
                             "label": label, "address": None, "status": "", "every": max(1, int(every))})

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

    async def _network_outputs(self, tick: int = 0) -> None:
        for o in self.outputs:
            if tick % o.get("every", 10):
                continue
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
            if self.outputs:
                await self._network_outputs(tick)
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


class PlantDemoLogic:
    """
    Demo-only stand-in for a trainee's chilled water plant program (in the first plant
    controller's mock, reading the other plant controllers' points in-process):
      * lead/lag chillers by load, each with "its" primary pump, condenser pump and tower cell
      * primary pump speed trims chiller flow to design, tower fan holds 75 °F CWS
      * secondary pumps on plant DP, setpoint trimmed by the riser remote DPs
      * tanks and tower outlet/equalizer valves open; on utility power loss everything
        but the secondary pumps and tank valves is commanded off, then restarted in steps
    """

    def __init__(self, mock: "MockController", plant, mocks: Dict[int, "MockController"]):
        self.mock, self.plant, self.mocks = mock, plant, mocks
        self.label = plant.name
        self.status = ""
        self.targets: Dict[str, tuple] = {}
        for f in plant.facets:
            for c in f.commands:
                self.targets[c.input] = (f.device_instance, objid_text(c.objid))
        m = plant.model
        self.ch = [c["pre"] for c in m.chillers]
        self.pp = [x["pre"] for x in m.pumps["primary_pumps"]]
        self.sp = [x["pre"] for x in m.pumps["secondary_pumps"]]
        self.cp = [x["pre"] for x in m.pumps["condenser_pumps"]]
        self.ct = [t["pre"] for t in m.towers]
        self.tk = [k["pre"] for k in m.tanks]
        self.br = [(b["pre"], int(b["controller_device"])) for b in m.branches]
        self.addr: Dict[int, str] = {}
        self.last: Dict[str, Any] = {}
        self.last_t: Dict[str, float] = {}
        self.n_ch, self.n_sec = 1, 2
        self.pspd = {p: 91.7 for p in self.pp}
        self.fspd = {t: 60.0 for t in self.ct}
        self.sspd = 80.0
        self.dp_sp = 20.0
        self.load = 0.0
        self.up = self.down = 0
        self.restart = 0
        self.ticks = 0

    def value(self, name: str, ctl: Optional[int] = None) -> Any:
        if ctl is not None and ctl in self.mocks:
            return self.mocks[ctl].value(name)
        for mk in self.mocks.values():
            v = mk.value(name)
            if v is not None:
                return v
        return None

    async def _write(self, name: str, val: Any) -> None:
        dev, obj = self.targets[name]
        loop = asyncio.get_running_loop()
        now = loop.time()
        prev = self.last.get(name)
        changed = prev is None or (val != prev if isinstance(val, bool) else abs(float(val) - float(prev)) > 0.4)
        if not changed and now - self.last_t.get(name, 0.0) < 20.0:
            return
        if dev not in self.addr:
            i_ams = await self.mock.app.who_is(dev, dev, timeout=3)
            if not i_ams:
                self.status = f"device {dev} not found"
                return
            self.addr[dev] = str(i_ams[0].pduSource)
        pv = (BinaryPV.active if val else BinaryPV.inactive) if isinstance(val, bool) else Real(float(val))
        try:
            await self.mock.app.write_property(Address(self.addr[dev]), obj, "present-value", pv, priority=10)
            self.last[name], self.last_t[name] = val, now
        except (Exception, ErrorRejectAbortNack) as err:
            self.status = f"write {name} failed: {describe_error(err)}"
            self.addr.pop(dev, None)

    async def tick(self) -> None:
        v = lambda n, d=0.0: (self.value(n) if self.value(n) is not None else d)
        self.ticks += 1
        outs: Dict[str, Any] = {}
        for k in self.tk:
            outs[f"{k}_top_vlv"] = outs[f"{k}_bot_vlv"] = True
        power = bool(self.plant.model.utility_ok)       # demo shortcut: is the plant powered (utility or generator)
        if not power:
            self.restart = 60
        elif self.restart > 0:
            self.restart -= 1
        # load estimate and staging (with hysteresis)
        flow, dt = v("schw_flow"), max(0.0, v("schwr_temp") - v("schws_temp"))
        self.load += (flow * dt / 24.0 - self.load) * 0.05
        cap = 1150.0
        if self.load > 0.92 * cap * self.n_ch and self.n_ch < len(self.ch):
            self.up += 1
        else:
            self.up = 0
        if self.n_ch > 1 and self.load < 0.65 * cap * (self.n_ch - 1):
            self.down += 1
        else:
            self.down = 0
        if self.up > 120:
            self.n_ch, self.up = self.n_ch + 1, 0
        if self.down > 300:
            self.n_ch, self.down = self.n_ch - 1, 0
        plant_on = power and self.restart == 0
        for i, c in enumerate(self.ch):
            on = plant_on and i < self.n_ch
            pp = self.pp[i % len(self.pp)] if self.pp else None
            cp = self.cp[i % len(self.cp)] if self.cp else None
            ct = self.ct[i % len(self.ct)] if self.ct else None
            outs[f"{c}_chw_vlv"] = outs[f"{c}_cw_vlv"] = on
            if pp:
                if on:
                    self.pspd[pp] = max(50.0, min(100.0, self.pspd[pp] + 0.004 * (2400.0 - v(f"{c}_chw_flow"))))
                outs[f"{pp}_ss"], outs[f"{pp}_vfd_spd"] = on, self.pspd[pp]
            if cp:
                outs[f"{cp}_ss"], outs[f"{cp}_vfd_spd"] = on, 91.7
            if ct:
                if on:
                    self.fspd[ct] = max(25.0, min(100.0, self.fspd[ct] + 1.0 * (v("cws_temp") - 75.0)))
                outs[f"{ct}_inlet_vlv"] = on
                outs[f"{ct}_fan_ss"], outs[f"{ct}_fan_vfd_spd"] = on and v("cws_temp") > 70.0, self.fspd[ct]
                outs[f"{ct}_bypass_vlv"] = False
            proven = on and v(f"{c}_chw_vlv_open") is True and v(f"{c}_cw_vlv_open") is True \
                and v(f"{c}_chw_flow") > 1500 and v(f"{c}_cw_flow") > 2000
            outs[f"{c}.rem_sp"] = 44.0
            outs[f"{c}.rem_run"] = bool(proven)
        for t in self.ct:
            outs[f"{t}_outlet_vlv"] = outs[f"{t}_eq_vlv"] = True
            outs[f"{t}_basin_htr"] = power and v(f"{t}_basin_temp", 60.0) < 40.0
        # secondary: plant DP with remote DP trim
        rdp = [self.value("chw_remote_dp", ctl) for _p, ctl in self.br]
        rdp = [x for x in rdp if x is not None]
        if rdp and self.ticks % 30 == 0:
            self.dp_sp = max(8.0, min(30.0, self.dp_sp + 0.5 * (8.0 - min(rdp))))
        self.sspd = max(30.0, min(100.0, self.sspd + 0.3 * (self.dp_sp - v("schw_dp"))))
        if flow > 2700:
            self.n_sec = 2
        elif flow < 1800:
            self.n_sec = 1
        for i, p in enumerate(self.sp):
            outs[f"{p}_ss"], outs[f"{p}_vfd_spd"] = i < self.n_sec, self.sspd
        for name, val in outs.items():
            if name in self.targets:
                await self._write(name, val)
        self.status = (f"load {self.load:.0f} t, {self.n_ch} chiller(s), plant DP {v('schw_dp'):.1f}/{self.dp_sp:.1f} psi"
                       f", remote {min(rdp) if rdp else 0:.1f} psi, {self.n_sec} sec pump(s)"
                       + ("" if power else " - UTILITY POWER FAILED") + (f" - restart in {self.restart}s" if power and self.restart else ""))


class HwPlantDemoLogic(PlantDemoLogic):
    """
    Demo-only stand-in for a trainee's hot water plant program (docs/HW_PLANT_DESIGN.md 13.2):
      * OA reset 160 °F at 0 °F OA -> 120 °F at 60 °F OA
      * one PI firing rate for the running boilers; lead/lag staging (add above 80 % for
        10 min, drop below 30 % for 10 min), weekly lead rotation, alarmed boilers skipped
      * each primary pump starts before its boiler is enabled and runs 2 min after it stops
      * secondary pumps on plant DP, setpoint trimmed by the branch remote DPs
      * end-of-branch 3-way valves hold 10 °F below the supply setpoint
      * louver open with any boiler enabled; staggered restart after a power outage
    """

    def __init__(self, mock: "MockController", plant, mocks: Dict[int, "MockController"]):
        self.mock, self.plant, self.mocks = mock, plant, mocks
        self.label = plant.name
        self.status = ""
        self.targets: Dict[str, tuple] = {}
        for f in plant.facets:
            for c in f.commands:
                self.targets[c.input] = (f.device_instance, objid_text(c.objid))
        m = plant.model
        self.bl = [b["pre"] for b in m.boilers]
        self.pump_of = {b["pre"]: b["pumpx"]["pre"] for b in m.boilers}
        self.sp = [x["pre"] for x in m.pumps["secondary_pumps"]]
        self.br = [(b["pre"], int(b["controller_device"])) for b in m.branches]
        self.addr: Dict[int, str] = {}
        self.last: Dict[str, Any] = {}
        self.last_t: Dict[str, float] = {}
        self.n_bl = 1
        self.rate = 30.0
        self.up = self.down = 0
        self.sspd = 70.0
        self.dp_sp = 15.0
        self.n_sec = 1
        self.eob = {pre: 0.0 for pre, _c in self.br}
        self.pump_run: Dict[str, float] = {b: 0.0 for b in self.bl}     # s the pump has run
        self.pump_post: Dict[str, float] = {b: 0.0 for b in self.bl}    # s of post-run left
        self.restart = 0.0
        self.ticks = 0
        self._t_prev: Optional[float] = None

    async def tick(self) -> None:
        v = lambda n, d=0.0, ctl=None: (self.value(n, ctl) if self.value(n, ctl) is not None else d)
        self.ticks += 1
        m = self.plant.model
        now = m.sim_time                        # timers run on simulated time (the host may run faster)
        dt = 1.0 if self._t_prev is None else max(0.0, min(120.0, now - self._t_prev))
        self._t_prev = now
        outs: Dict[str, Any] = {}
        power = bool(self.plant.model.utility_ok)       # demo shortcut: is the plant powered (utility or generator)
        if not power:
            self.restart = 90.0
            self.n_bl = 1
        elif self.restart > 0:
            self.restart = max(0.0, self.restart - dt)
        oat = float(m.p["oat"])
        sp = max(120.0, min(160.0, 160.0 - (oat - 0.0) * 40.0 / 60.0))
        t_sup = v("shws_temp", sp)
        # lead rotation (weekly) and boilers available (not alarmed)
        week = m.clock().isocalendar()[1]
        order = self.bl[week % len(self.bl):] + self.bl[:week % len(self.bl)] if self.bl else []
        ok = [b for b in order if v(f"{b}_alarm", False) is not True]
        on_set = ok[:self.n_bl] if power and self.restart == 0 else []
        firing = [b for b in on_set if self.pump_run[b] >= 30.0]
        if firing:
            self.rate = max(5.0, min(100.0, self.rate + 0.03 * (sp - t_sup) * dt))
        else:
            self.rate = 30.0
        if self.rate > 80.0 and self.n_bl < len(ok):
            self.up += dt
        else:
            self.up = 0
        if self.rate < 30.0 and self.n_bl > 1:
            self.down += dt
        else:
            self.down = 0
        if self.up > 600:
            self.n_bl, self.up, self.rate = self.n_bl + 1, 0, self.rate * 0.6
        if self.down > 600:
            self.n_bl, self.down = self.n_bl - 1, 0
        for b in self.bl:
            pump = self.pump_of[b]
            want = b in on_set
            if want:
                self.pump_run[b] += dt if v(f"{pump}_amps") > 0.5 else 0.0
                self.pump_post[b] = 120.0
            else:
                self.pump_run[b] = 0.0
                self.pump_post[b] = max(0.0, self.pump_post[b] - dt) if power else 0.0
            outs[f"{pump}_ss"] = want or self.pump_post[b] > 0
            outs[f"{pump}_vfd_spd"] = 91.7
            enable = want and self.pump_run[b] >= 30.0
            outs[f"{b}.bms_enable"] = enable
            outs[f"{b}.bms_rate"] = round(self.rate, 1) if enable else 0.0
        outs["louver"] = bool(on_set)
        # secondary pumps: plant DP, setpoint trimmed by the remote DPs
        rdp = [self.value("hw_remote_dp", ctl) for _p, ctl in self.br]
        rdp = [x for x in rdp if x is not None]
        if rdp and self.ticks % 30 == 0:
            self.dp_sp = max(5.0, min(25.0, self.dp_sp + 0.5 * (6.0 - min(rdp))))
        self.sspd = max(30.0, min(100.0, self.sspd + 0.3 * (self.dp_sp - v("hw_dp"))))
        flow = v("shw_flow")
        if flow > 700:
            self.n_sec = 2
        elif flow < 450:
            self.n_sec = 1
        sec_on = power and self.restart < 60.0
        for i, p in enumerate(self.sp):
            outs[f"{p}_ss"], outs[f"{p}_vfd_spd"] = sec_on and i < self.n_sec, self.sspd
        # end-of-branch 3-way valves
        for pre, ctl in self.br:
            t = self.value("hw_eob_temp", ctl)
            if t is not None:
                self.eob[pre] = max(0.0, min(100.0, self.eob[pre] + 0.05 * (sp - 10.0 - t) * dt))
            outs[f"{pre}_hw_eob_vlv"] = round(self.eob[pre], 1)
        for name, val in outs.items():
            if name in self.targets:
                await self._write(name, val)
        self.status = (f"reset SP {sp:.0f}°F (OA {oat:.0f}°F), supply {t_sup:.1f}°F, {len(firing)}/{self.n_bl} boiler(s) "
                       f"at {self.rate:.0f} %, plant DP {v('hw_dp'):.1f}/{self.dp_sp:.1f} psi, {self.n_sec} sec pump(s)"
                       + ("" if power else " - UTILITY POWER FAILED") + (f" - restart in {self.restart:.0f}s" if power and self.restart else ""))


class GenPlantDemoLogic(PlantDemoLogic):
    """
    Demo-only stand-in for a trainee's generator / ATS program (docs/GENERATOR_DESIGN.md 10):
      * polls the generators and the paralleling master over Modbus TCP (the real server)
      * on an outage, holds transfer inhibit on the CHW and HW plant ATSs until the
        critical and building ATSs have been on the generator bus for 30 s, then
        releases them 30 s apart (staged restart); releases every inhibit on normal power
    """

    STAGED = ("ATS-2", "ATS-3")

    def __init__(self, mock: "MockController", plant, mocks: Dict[int, "MockController"]):
        self.mock, self.plant, self.mocks = mock, plant, mocks
        self.label = plant.name
        self.status = "waiting for Modbus"
        self.targets: Dict[str, tuple] = {}
        for f in plant.facets:
            for c in f.commands:
                self.targets[c.input] = (f.device_instance, objid_text(c.objid))
        self.addr: Dict[int, str] = {}
        self.last: Dict[str, Any] = {}
        self.last_t: Dict[str, float] = {}
        self.client = None
        self.on_gen_s = 0.0
        self._t_prev: Optional[float] = None
        self.data: Dict[str, Any] = {}

    async def _poll(self) -> bool:
        from .modbus_tcp import ModbusTcpClient
        server = self.plant.modbus
        if server is None or server.server is None:
            return False
        if self.client is None:
            self.client = ModbusTcpClient("127.0.0.1", server.port, timeout=1.0)
        m = self.plant.model
        try:
            master = await self.client.read(5, 40900, 5)
            status = (await self.client.read(5, 40006, 1))[0]
            ats = []
            for n in range(len(m.ats)):
                ats.append((await self.client.read(5, 40257 + 60 * n + 2, 1))[0])   # ATS state
            gens = []
            for g in m.gens:
                try:
                    gens.append(await self.client.read(int(g["unit_id"]), 40010, 5))
                except Exception:
                    gens.append(None)
        except Exception as err:
            self.status = f"Modbus poll failed: {err}"
            if self.client is not None:
                self.client.close()
                self.client = None
            return False
        self.data = {"shed": master[0], "online": master[1], "cap": master[2], "load": master[3],
                     "utility": bool(status & (1 << 7)), "ats": ats, "gens": gens}
        return True

    async def tick(self) -> None:
        m = self.plant.model
        now = m.sim_time
        dt = 1.0 if self._t_prev is None else max(0.0, min(120.0, now - self._t_prev))
        self._t_prev = now
        if not await self._poll():
            return
        d = self.data
        tags = [a["tag"] for a in m.ats]
        state = dict(zip(tags, d["ats"]))
        on_gen = [t for t in tags if t not in self.STAGED and state.get(t) == 2]
        emergency = not d["utility"]
        if emergency and on_gen and len(on_gen) == len([t for t in tags if t not in self.STAGED]):
            self.on_gen_s += dt
        elif not emergency:
            self.on_gen_s = 0.0
        outs: Dict[str, Any] = {}
        for i, a in enumerate(m.ats):
            name = f"{a['pre']}_xfer_inhibit"
            if a["tag"] in self.STAGED and emergency:
                release_at = 30.0 * (1 + self.STAGED.index(a["tag"]))
                outs[name] = self.on_gen_s < release_at and state.get(a["tag"]) != 2
            else:
                outs[name] = False
        for name, val in outs.items():
            if name in self.targets:
                await self._write(name, val)
        running = sum(1 for g in d["gens"] if g and g[1] == 3)
        held = [n.split("_")[0].upper() for n, v in outs.items() if v]
        self.status = (f"utility {'NORMAL' if d['utility'] else 'FAILED'}, {d['online']} set(s) online, "
                       f"bus {d['load']}/{d['cap']} kW, {running} rated, shed level {d['shed']}"
                       + (f", inhibiting {', '.join(held)}" if held else ""))
