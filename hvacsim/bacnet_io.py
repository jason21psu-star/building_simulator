"""
BACnet/IP side of a simulator.

Two roles:

1. SERVER - the simulator is its own BACnet device.  The ALC controller's
   network output microblocks write to the simulator's commandable objects
   (e.g. BO-1 valve command).  Read-only "monitor" objects expose the
   simulator's internal state for troubleshooting and the instructor.

2. CLIENT - the simulator writes sensor values into the ALC controller's
   physical inputs, which are placed Out_Of_Service so their Present_Value
   can be written.

Everything here is driven by the YAML config so points can be added or
removed without touching code (change order work).
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from bacpypes3.apdu import AbortPDU, ErrorPDU, ErrorRejectAbortNack, RejectPDU
from bacpypes3.app import Application
from bacpypes3.argparse import SimpleArgumentParser
from bacpypes3.basetypes import BinaryPV, EngineeringUnits, PropertyIdentifier
from bacpypes3.errors import PropertyError
from bacpypes3.local.analog import AnalogOutputObject, AnalogValueObject, AnalogValueObjectCmd
from bacpypes3.local.binary import BinaryOutputObject, BinaryValueObject, BinaryValueObjectCmd
from bacpypes3.pdu import Address
from bacpypes3.primitivedata import ObjectIdentifier, Real

from .models.base import EquipmentModel

log = logging.getLogger("hvacsim.bacnet")


# ---------------------------------------------------------------------------
# Object helpers
# ---------------------------------------------------------------------------


class _ReadOnlyPV:
    """Mix-in: Present_Value cannot be written over the network."""

    async def write_property(self, attr, value, index=None, priority=None):  # type: ignore[override]
        if attr in ("presentValue", PropertyIdentifier.presentValue):
            raise PropertyError("writeAccessDenied")
        await super().write_property(attr, value, index, priority)


class ReadOnlyAnalogValue(_ReadOnlyPV, AnalogValueObject):
    pass


class ReadOnlyBinaryValue(_ReadOnlyPV, BinaryValueObject):
    pass


COMMAND_CLASSES = {
    "binary-output": BinaryOutputObject,
    "binary-value": BinaryValueObjectCmd,
    "analog-output": AnalogOutputObject,
    "analog-value": AnalogValueObjectCmd,
}


def parse_objid(text: str) -> ObjectIdentifier:
    """Accept 'binary-output,1', 'BO:1', 'bo-1', 'AV:1' etc."""
    short = {
        "ai": "analog-input", "ao": "analog-output", "av": "analog-value",
        "bi": "binary-input", "bo": "binary-output", "bv": "binary-value",
        "msi": "multi-state-input", "mso": "multi-state-output", "msv": "multi-state-value",
    }
    t = str(text).strip().replace(":", ",")
    if "," not in t and "-" in t:
        head, _, tail = t.rpartition("-")
        if tail.isdigit():
            t = f"{head},{tail}"
    typ, _, inst = t.partition(",")
    typ = short.get(typ.strip().lower(), typ.strip().lower())
    return ObjectIdentifier(f"{typ},{int(inst)}")


def _is_binary(objid: ObjectIdentifier) -> bool:
    return str(objid[0]).startswith("binary")


def _to_bacnet(objid: ObjectIdentifier, value: Any):
    if _is_binary(objid):
        return BinaryPV.active if bool(value) else BinaryPV.inactive
    return Real(float(value))


def _from_bacnet(value: Any) -> Any:
    if isinstance(value, BinaryPV):
        return value == BinaryPV.active
    try:
        return float(value)
    except (TypeError, ValueError):
        return value


def describe_error(err: BaseException) -> str:
    if isinstance(err, ErrorPDU):
        return f"{err.errorClass}: {err.errorCode}"
    if isinstance(err, (AbortPDU, RejectPDU)):
        return f"{err.__class__.__name__[:-3].lower()}: {err.apduAbortRejectReason}"
    if isinstance(err, asyncio.TimeoutError):
        return "timeout (no response)"
    return str(err) or err.__class__.__name__


# ---------------------------------------------------------------------------
# Server side
# ---------------------------------------------------------------------------


@dataclass
class CommandPoint:
    name: str
    objid: ObjectIdentifier
    input: str
    obj: Any


@dataclass
class MonitorPoint:
    name: str
    objid: ObjectIdentifier
    signal: str
    obj: Any


def build_application(sim_cfg: Dict[str, Any]) -> Application:
    argv = [
        "--name", str(sim_cfg["name"]),
        "--instance", str(int(sim_cfg["device_instance"])),
        "--address", str(sim_cfg["address"]),
    ]
    if sim_cfg.get("vendor_identifier") is not None:
        argv += ["--vendoridentifier", str(int(sim_cfg["vendor_identifier"]))]
    args = SimpleArgumentParser().parse_args(argv)
    app = Application.from_args(args)
    dev = app.device_object
    dev.modelName = sim_cfg.get("model_name", "HVAC Training Simulator")
    dev.description = sim_cfg.get("description", "")
    if "location" in sim_cfg:
        dev.location = sim_cfg["location"]
    return app


def add_command_points(app: Application, model: EquipmentModel, cfg: List[Dict[str, Any]]) -> List[CommandPoint]:
    points = []
    for c in cfg or []:
        objid = parse_objid(c["object"])
        cls = COMMAND_CLASSES.get(str(objid[0]))
        if cls is None:
            raise ValueError(f"command object type {objid[0]} not supported (use {list(COMMAND_CLASSES)})")
        sdef = model.signal_def(c["input"])
        relinquish = c.get("relinquish_default", sdef.default)
        kwargs = dict(
            objectIdentifier=objid,
            objectName=c["name"],
            description=c.get("description", sdef.description),
            presentValue=_to_bacnet(objid, relinquish),
            relinquishDefault=_to_bacnet(objid, relinquish),
            outOfService=False,
            statusFlags=[0, 0, 0, 0],
        )
        if _is_binary(objid):
            kwargs.update(activeText=c.get("active_text", "On"), inactiveText=c.get("inactive_text", "Off"))
            if str(objid[0]) == "binary-output":
                kwargs["polarity"] = "normal"
        else:
            kwargs["units"] = EngineeringUnits(c.get("units", sdef.units))
        obj = cls(**kwargs)
        app.add_object(obj)
        points.append(CommandPoint(c["name"], objid, c["input"], obj))
    return points


def add_monitor_points(app: Application, model: EquipmentModel, cfg: List[Dict[str, Any]]) -> List[MonitorPoint]:
    points = []
    for c in cfg or []:
        objid = parse_objid(c["object"])
        sdef = model.signal_def(c["signal"])
        kind = str(objid[0])
        common = dict(
            objectIdentifier=objid,
            objectName=c["name"],
            description=c.get("description", sdef.description),
            outOfService=False,
            statusFlags=[0, 0, 0, 0],
        )
        if kind == "analog-value":
            obj = ReadOnlyAnalogValue(presentValue=Real(0.0), units=EngineeringUnits(c.get("units", sdef.units)), **common)
        elif kind == "binary-value":
            obj = ReadOnlyBinaryValue(presentValue=BinaryPV.inactive,
                                      activeText=c.get("active_text", "On"),
                                      inactiveText=c.get("inactive_text", "Off"), **common)
        else:
            raise ValueError(f"monitor objects must be analog-value or binary-value, got {kind}")
        app.add_object(obj)
        points.append(MonitorPoint(c["name"], objid, c["signal"], obj))
    return points


def read_commands(model: EquipmentModel, points: List[CommandPoint]) -> None:
    for pt in points:
        model.set_input(pt.input, _from_bacnet(pt.obj.presentValue))


def update_monitors(model: EquipmentModel, points: List[MonitorPoint]) -> None:
    for pt in points:
        value = model.signals[pt.signal]
        new = _to_bacnet(pt.objid, value)
        if pt.obj.presentValue != new:
            pt.obj.presentValue = new


def priority_summary(obj) -> Dict[str, Any]:
    slots = []
    for i, pv in enumerate(obj.priorityArray):
        if pv.null is None:
            raw = _from_bacnet(getattr(pv, pv._choice))
            if _is_binary(obj.objectIdentifier):
                raw = bool(raw)
            slots.append({"priority": i + 1, "value": raw})
    cur = obj.currentCommandPriority
    return {
        "present_value": _from_bacnet(obj.presentValue),
        "relinquish_default": _from_bacnet(obj.relinquishDefault),
        "active_priority": cur.unsigned if cur is not None and cur.unsigned is not None else None,
        "slots": slots,
    }


# ---------------------------------------------------------------------------
# Client side: writing into the ALC controller
# ---------------------------------------------------------------------------


@dataclass
class ControllerWrite:
    signal: str
    objid: ObjectIdentifier
    deadband: float = 0.1
    heartbeat: float = 60.0
    priority: Optional[int] = None
    name: str = ""
    # runtime status
    last_value: Any = None
    last_write: float = 0.0
    writes: int = 0
    errors: int = 0
    last_error: str = ""
    ok: Optional[bool] = None


@dataclass
class ControllerLink:
    """Pushes model signals into the controller's out-of-service inputs."""

    app: Application
    model: EquipmentModel
    device_instance: int
    address: Optional[str] = None  # static address, else Who-Is
    min_interval: float = 1.0      # s between writes of the same point
    points: List[ControllerWrite] = field(default_factory=list)
    enabled: bool = True
    # runtime
    resolved_address: Optional[Address] = None
    status: str = "starting"
    _last_resolve: float = 0.0

    @classmethod
    def from_config(cls, app, model, cfg: Dict[str, Any]) -> "ControllerLink":
        points = []
        for w in cfg.get("writes", []) or []:
            model.signal_def(w["signal"])  # validate
            points.append(ControllerWrite(
                signal=w["signal"],
                objid=parse_objid(w["object"]),
                deadband=float(w.get("deadband", 0.1)),
                heartbeat=float(w.get("heartbeat", 60.0)),
                priority=w.get("priority"),
                name=w.get("name", w["signal"]),
            ))
        return cls(
            app=app, model=model,
            device_instance=int(cfg["device_instance"]),
            address=cfg.get("address") or None,
            min_interval=float(cfg.get("min_interval", 1.0)),
            points=points,
            enabled=bool(cfg.get("enabled", True)),
        )

    def retarget(self, device_instance: int, address: Optional[str] = None) -> None:
        """Point the simulator at a different controller (e.g. trainee re-addressed it)."""
        self.device_instance = int(device_instance)
        self.address = address or None
        self.resolved_address = None
        self._last_resolve = 0.0
        for pt in self.points:
            pt.last_value = None
            pt.ok = None
            pt.last_error = ""
        log.info("controller retargeted to device %s address %s", self.device_instance, self.address)

    async def _resolve(self) -> bool:
        if self.resolved_address is not None:
            return True
        if self.address:
            self.resolved_address = Address(self.address)
            self.status = f"device {self.device_instance} at {self.address} (static)"
            return True
        now = time.monotonic()
        if now - self._last_resolve < 10.0:
            return False
        self._last_resolve = now
        self.status = f"searching for device {self.device_instance} (Who-Is)"
        try:
            i_ams = await self.app.who_is(self.device_instance, self.device_instance, timeout=3)
        except (Exception, ErrorRejectAbortNack) as err:  # pragma: no cover - network dependent
            self.status = f"Who-Is failed: {describe_error(err)}"
            return False
        if not i_ams:
            self.status = f"device {self.device_instance} not found (no I-Am)"
            return False
        self.resolved_address = i_ams[0].pduSource
        self.status = f"device {self.device_instance} at {self.resolved_address}"
        log.info(self.status)
        return True

    def _due(self, pt: ControllerWrite, value: Any, now: float) -> bool:
        if pt.last_value is None or pt.ok is False:
            return now - pt.last_write >= max(self.min_interval, 5.0 if pt.ok is False else 0.0)
        if now - pt.last_write < self.min_interval:
            return False
        if now - pt.last_write >= pt.heartbeat:
            return True
        if isinstance(value, bool) or isinstance(pt.last_value, bool):
            return bool(value) != bool(pt.last_value)
        return abs(float(value) - float(pt.last_value)) >= pt.deadband

    async def run(self) -> None:
        while True:
            try:
                await self._tick()
            except asyncio.CancelledError:
                raise
            except Exception as err:  # keep the simulator alive no matter what
                log.exception("controller link error")
                self.status = f"error: {err}"
            await asyncio.sleep(0.25)

    async def _tick(self) -> None:
        if not self.enabled:
            self.status = "controller writes disabled"
            return
        if not await self._resolve():
            return
        now = time.monotonic()
        failures = 0
        for pt in self.points:
            value = self.model.signals[pt.signal]
            if not self._due(pt, value, now):
                continue
            pt.last_write = now
            try:
                result = await asyncio.wait_for(
                    self.app.write_property(
                        self.resolved_address, pt.objid, "present-value",
                        _to_bacnet(pt.objid, value), priority=pt.priority,
                    ),
                    timeout=5.0,
                )
                if isinstance(result, str):  # bacpypes3 returns a string on local problems
                    raise RuntimeError(result)
                pt.last_value = value
                pt.writes += 1
                pt.ok = True
                pt.last_error = ""
            except (Exception, ErrorRejectAbortNack) as err:
                pt.errors += 1
                pt.ok = False
                text = describe_error(err)
                if "write-access-denied" in text or "writeAccessDenied" in text:
                    text += " - is the input still Out_Of_Service?"
                pt.last_error = text
                failures += 1
                log.warning("write %s %s failed: %s", pt.objid, pt.signal, text)
        if failures and failures == len(self.points) and not self.address:
            # maybe the controller moved; rediscover next time
            self.resolved_address = None

    def snapshot(self) -> Dict[str, Any]:
        return {
            "device_instance": self.device_instance,
            "address": self.address,
            "resolved_address": str(self.resolved_address) if self.resolved_address else None,
            "status": self.status,
            "enabled": self.enabled,
            "points": [
                {
                    "name": p.name, "signal": p.signal, "object": f"{p.objid[0]},{p.objid[1]}",
                    "priority": p.priority, "last_value": p.last_value, "ok": p.ok,
                    "writes": p.writes, "errors": p.errors, "last_error": p.last_error,
                    "age": round(time.monotonic() - p.last_write, 1) if p.last_write else None,
                }
                for p in self.points
            ],
        }
