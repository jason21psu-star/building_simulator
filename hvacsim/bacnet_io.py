"""
BACnet side of the simulators.

Network layout (one host = one IP address):

    LAN (BACnet/IP, network = host.ip_network)
     └── host router  device host.device_instance   e.g. 192.168.1.51:47808
          └── virtual network host.virtual_network   e.g. 5001
                ├── UH-1-SIM  device 4010001  MAC 1
                ├── UH-2-SIM  device 4010002  MAC 2
                └── ...

Each equipment instance is its own BACnet device:

1. SERVER - the ALC controller's network output microblocks write to the
   instance's commandable objects (e.g. BO-1 valve command).  Read-only
   "monitor" objects expose the simulator's internal state.

2. CLIENT - the instance writes sensor values into the ALC controller's
   physical inputs (Out_Of_Service).  Controller objects are found by
   BACnet object name (e.g. fan_amps_1) or given as a fixed object ID.

Everything is driven by the YAML config so points can be added or removed
without touching code (change order work).
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

from bacpypes3.apdu import AbortPDU, ConfirmedRequestPDU, ErrorPDU, ErrorRejectAbortNack, RejectPDU
from bacpypes3.app import Application
from bacpypes3.basetypes import BinaryPV, EngineeringUnits, ObjectType, PropertyIdentifier
from bacpypes3.errors import PropertyError
from bacpypes3.local.analog import AnalogInputObject, AnalogOutputObject, AnalogValueObject, AnalogValueObjectCmd
from bacpypes3.local.binary import BinaryInputObject, BinaryOutputObject, BinaryValueObject, BinaryValueObjectCmd
from bacpypes3.local.multistate import MultiStateValueObject
from bacpypes3.local.networkport import NetworkPortObject
from bacpypes3.pdu import Address
from bacpypes3.primitivedata import ObjectIdentifier, Real, Unsigned
from bacpypes3.vendor import get_vendor_info
from bacpypes3.vlan import VirtualNetwork

from .models.base import EquipmentModel

log = logging.getLogger("hvacsim.bacnet")

VENDOR_ID = 999  # BACpypes sample vendor id


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


class ReadOnlyAnalogInput(_ReadOnlyPV, AnalogInputObject):
    pass


class ReadOnlyBinaryInput(_ReadOnlyPV, BinaryInputObject):
    pass


class ReadOnlyAnalogOutput(_ReadOnlyPV, AnalogOutputObject):
    pass


class ReadOnlyBinaryOutput(_ReadOnlyPV, BinaryOutputObject):
    pass


class ReadOnlyMultiStateValue(_ReadOnlyPV, MultiStateValueObject):
    pass


class _Stamped:
    """Mix-in: remember when Present_Value was last written over the network (comm-loss supervision)."""

    _last_write: float = 0.0

    async def write_property(self, attr, value, index=None, priority=None):  # type: ignore[override]
        await super().write_property(attr, value, index, priority)
        if attr in ("presentValue", PropertyIdentifier.presentValue):
            self._last_write = time.monotonic()


class CmdBinaryOutput(_Stamped, BinaryOutputObject):
    pass


class CmdBinaryValue(_Stamped, BinaryValueObjectCmd):
    pass


class CmdAnalogOutput(_Stamped, AnalogOutputObject):
    pass


class CmdAnalogValue(_Stamped, AnalogValueObjectCmd):
    pass


class ParamAnalogValue(_Stamped, AnalogValueObject):
    """Writable, non-commandable AV mapped to a model parameter (e.g. ACCEL1 TIME)."""


COMMAND_CLASSES = {
    "binary-output": CmdBinaryOutput,
    "binary-value": CmdBinaryValue,
    "analog-output": CmdAnalogOutput,
    "analog-value": CmdAnalogValue,
}

MONITOR_CLASSES = {
    "analog-value": ReadOnlyAnalogValue,
    "binary-value": ReadOnlyBinaryValue,
    "analog-input": ReadOnlyAnalogInput,
    "binary-input": ReadOnlyBinaryInput,
    "analog-output": ReadOnlyAnalogOutput,
    "binary-output": ReadOnlyBinaryOutput,
    "multi-state-value": ReadOnlyMultiStateValue,
}

SHORT_TYPES = {
    "ai": "analog-input", "ao": "analog-output", "av": "analog-value",
    "bi": "binary-input", "bo": "binary-output", "bv": "binary-value",
    "msi": "multi-state-input", "mso": "multi-state-output", "msv": "multi-state-value",
}


def parse_objid(text: str) -> ObjectIdentifier:
    """Accept 'binary-output,1', 'BO:1', 'bo-1', 'AV:1' etc."""
    t = str(text).strip().replace(":", ",")
    if "," not in t and "-" in t:
        head, _, tail = t.rpartition("-")
        if tail.isdigit():
            t = f"{head},{tail}"
    typ, _, inst = t.partition(",")
    typ = SHORT_TYPES.get(typ.strip().lower(), typ.strip().lower())
    return ObjectIdentifier(f"{typ},{int(inst)}")


def objid_text(objid: Optional[ObjectIdentifier]) -> Optional[str]:
    if objid is None:
        return None
    return f"{objid[0]},{objid[1]}"


def _text(value: Any) -> str:
    """YAML turns YES/NO/ON/OFF into booleans; BACnet text properties need strings."""
    if isinstance(value, bool):
        return {True: "YES", False: "NO"}[value]
    return str(value)


def _is_binary(objid: ObjectIdentifier) -> bool:
    return str(objid[0]).startswith("binary")


def _to_bacnet(objid: ObjectIdentifier, value: Any):
    if str(objid[0]).startswith("multi-state"):
        return Unsigned(max(1, int(value) + 1))          # state code 0 = BACnet value 1
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


def _check(result: Any) -> Any:
    """bacpypes3 sometimes returns (rather than raises) errors."""
    if isinstance(result, ErrorRejectAbortNack):
        raise result
    if isinstance(result, str) and result.startswith("-"):
        raise RuntimeError(result)
    return result


# ---------------------------------------------------------------------------
# Network: router + virtual network + one device per instance
# ---------------------------------------------------------------------------


def make_virtual_network(name: str) -> VirtualNetwork:
    """Create a named in-process network, replacing a stale one of the same name."""
    VirtualNetwork._networks.pop(name, None)
    return VirtualNetwork(name)


def release_virtual_network(name: str) -> None:
    VirtualNetwork._networks.pop(name, None)


def virtual_port(instance: int, interface: str, network: int, mac: int) -> NetworkPortObject:
    return NetworkPortObject(
        objectIdentifier=("network-port", instance),
        objectName=f"NP-{interface}",
        networkType="virtual",
        protocolLevel="bacnet-application",
        networkNumber=int(network),
        networkNumberQuality="configured",
        macAddress=bytes([int(mac)]),
        networkInterfaceName=interface,
        statusFlags=[0, 0, 0, 0],
        reliability="no-fault-detected",
        outOfService=False,
        changesPending=False,
    )


def _device(instance: int, name: str, **props):
    cls = get_vendor_info(VENDOR_ID).get_object_class(ObjectType.device)
    dev = cls(objectIdentifier=("device", int(instance)), objectName=str(name))
    for k, v in props.items():
        if v is not None:
            setattr(dev, k, v)
    return dev


def build_router(host: Dict[str, Any], vlan_name: str, upstream_virtual: Optional[str] = None) -> Application:
    """
    The host's BACnet router: an IP port on host.ip_network and a virtual
    port (MAC 0) on host.virtual_network.  `upstream_virtual` replaces the
    IP port with an in-process network (demo mode and tests).
    """
    dev = _device(host["device_instance"], host["name"],
                  modelName="HVAC Simulator Router", description=host.get("description"),
                  location=host.get("location"))
    if upstream_virtual:
        up = virtual_port(1, upstream_virtual, host["ip_network"], 0xFE)
    else:
        up = NetworkPortObject(str(host["address"]), objectIdentifier=("network-port", 1), objectName="NP-IP")
        up.networkNumber = int(host["ip_network"])
        up.networkNumberQuality = "configured"
    down = virtual_port(2, vlan_name, host["virtual_network"], 0)
    return Application.from_object_list([dev, up, down])


class NodeApplication(Application):
    """An equipment instance's BACnet device; remembers when it last received a confirmed request."""

    last_request: float = 0.0
    offline: bool = False          # simulated "device offline": ignore every request

    async def indication(self, apdu) -> None:  # type: ignore[override]
        if self.offline:
            return
        if isinstance(apdu, ConfirmedRequestPDU):
            self.last_request = time.monotonic()
        await super().indication(apdu)


def build_node(inst: Dict[str, Any], vlan_name: str, network: int) -> Application:
    dev = _device(inst["device_instance"], inst["name"],
                  modelName=inst.get("model_name", "HVAC Training Simulator"),
                  description=inst.get("description"), location=inst.get("location"))
    port = virtual_port(1, vlan_name, network, inst["mac"])
    return NodeApplication.from_object_list([dev, port])


# ---------------------------------------------------------------------------
# Server side objects
# ---------------------------------------------------------------------------


@dataclass
class CommandPoint:
    name: str
    objid: ObjectIdentifier
    input: str
    obj: Any


@dataclass
class ParamPoint:
    name: str
    objid: ObjectIdentifier
    param: str
    obj: Any
    synced: Any = None


@dataclass
class MonitorPoint:
    name: str
    objid: ObjectIdentifier
    signal: str
    obj: Any


def add_command_points(app: Application, model: EquipmentModel, cfg: List[Dict[str, Any]]) -> Tuple[List[CommandPoint], List[ParamPoint]]:
    """Objects the controller writes: commandable objects -> model inputs, writable AVs -> model parameters."""
    points: List[CommandPoint] = []
    params: List[ParamPoint] = []
    for c in cfg or []:
        objid = parse_objid(c["object"])
        if "param" in c:
            if str(objid[0]) != "analog-value":
                raise ValueError(f"{c['name']}: parameter objects must be analog-value")
            name = c["param"]
            if name not in model.p:
                raise KeyError(f"{c['name']}: model has no parameter {name!r}")
            info = model.PARAM_INFO.get(name)
            obj = ParamAnalogValue(objectIdentifier=objid, objectName=_text(c["name"]),
                                   description=c.get("description", info.label if info else name),
                                   presentValue=Real(float(model.p[name])),
                                   units=EngineeringUnits(c.get("units", "noUnits")),
                                   outOfService=False, statusFlags=[0, 0, 0, 0])
            app.add_object(obj)
            params.append(ParamPoint(c["name"], objid, name, obj, float(model.p[name])))
            continue
        cls = COMMAND_CLASSES.get(str(objid[0]))
        if cls is None:
            raise ValueError(f"command object type {objid[0]} not supported (use {list(COMMAND_CLASSES)})")
        sdef = model.signal_def(c["input"])
        relinquish = c.get("relinquish_default", sdef.default)
        kwargs = dict(
            objectIdentifier=objid,
            objectName=_text(c["name"]),
            description=_text(c.get("description", sdef.description)),
            presentValue=_to_bacnet(objid, relinquish),
            relinquishDefault=_to_bacnet(objid, relinquish),
            outOfService=False,
            statusFlags=[0, 0, 0, 0],
        )
        if _is_binary(objid):
            kwargs.update(activeText=_text(c.get("active_text", "On")), inactiveText=_text(c.get("inactive_text", "Off")))
            if str(objid[0]) == "binary-output":
                kwargs["polarity"] = "normal"
        else:
            kwargs["units"] = EngineeringUnits(c.get("units", sdef.units))
        obj = cls(**kwargs)
        obj._watchdog = bool(c.get("comm_watchdog", False))
        app.add_object(obj)
        points.append(CommandPoint(c["name"], objid, c["input"], obj))
    return points, params


def add_monitor_points(app: Application, model: EquipmentModel, cfg: List[Dict[str, Any]]) -> List[MonitorPoint]:
    points = []
    for c in cfg or []:
        objid = parse_objid(c["object"])
        sdef = model.signal_def(c["signal"])
        kind = str(objid[0])
        common = dict(
            objectIdentifier=objid,
            objectName=_text(c["name"]),
            description=_text(c.get("description", sdef.description)),
            outOfService=False,
            statusFlags=[0, 0, 0, 0],
        )
        cls = MONITOR_CLASSES.get(kind)
        if cls is None:
            raise ValueError(f"monitor object type {kind} not supported (use {list(MONITOR_CLASSES)})")
        if sdef.kind == "text":
            raise ValueError(f"{c['name']}: signal {c['signal']} is text and cannot be a BACnet object")
        if kind.startswith("multi-state"):
            states = [_text(x)[:60] for x in c.get("states") or ["State 0"]]
            extra = dict(presentValue=Unsigned(1), numberOfStates=len(states), stateText=states)
        elif kind.startswith("analog"):
            units = c.get("units", sdef.units if sdef.kind == "analog" else "noUnits")
            extra = dict(presentValue=Real(0.0), units=EngineeringUnits(units))
            if kind == "analog-output":
                extra["relinquishDefault"] = Real(0.0)
        else:
            extra = dict(presentValue=BinaryPV.inactive, activeText=_text(c.get("active_text", "On")),
                         inactiveText=_text(c.get("inactive_text", "Off")))
            if kind in ("binary-input", "binary-output"):
                extra["polarity"] = "normal"
            if kind == "binary-output":
                extra["relinquishDefault"] = BinaryPV.inactive
        obj = cls(**extra, **common)
        app.add_object(obj)
        points.append(MonitorPoint(c["name"], objid, c["signal"], obj))
    return points


def read_commands(model: EquipmentModel, points: List[CommandPoint], params: Optional[List[ParamPoint]] = None) -> None:
    for pt in points:
        model.set_input(pt.input, _from_bacnet(pt.obj.presentValue))
    for pp in params or []:
        value = float(pp.obj.presentValue)
        if value != pp.synced:
            # written over BACnet: apply to the model (rejects out-of-range values)
            try:
                model.set_param(pp.param, value)
            except (ValueError, TypeError) as err:
                log.warning("%s: rejected %s = %s (%s)", pp.name, pp.param, value, err)
        current = float(model.p[pp.param])
        if float(pp.obj.presentValue) != current:
            pp.obj.presentValue = Real(current)
        pp.synced = float(pp.obj.presentValue)


def last_fieldbus_activity(app: Application, points: List[CommandPoint], params: List[ParamPoint],
                           mode: str = "any_message") -> float:
    """Monotonic time the device last heard from the fieldbus (0 = never)."""
    if mode == "any_message":
        return getattr(app, "last_request", 0.0)
    stamps = [getattr(p.obj, "_last_write", 0.0) for p in points if getattr(p.obj, "_watchdog", False)]
    return max(stamps) if stamps else 0.0


def update_monitors(model: EquipmentModel, points: List[MonitorPoint]) -> None:
    for pt in points:
        new = _to_bacnet(pt.objid, model.signals[pt.signal])
        if isinstance(new, Unsigned) and int(new) > int(pt.obj.numberOfStates):
            new = Unsigned(int(pt.obj.numberOfStates))
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
# Finding controller objects by name
# ---------------------------------------------------------------------------


class NameDirectory:
    """
    Maps object names to object identifiers in each controller.

    Shared by every instance on the host, so ten unit heaters wired to one
    controller cause one object-list scan, not ten.  Lookup order:
      1. cached result of reading the controller's object-list + object names
      2. Who-Has (object name) as a fallback
    """

    RESCAN_AFTER = 30.0  # s before a missing name triggers another scan

    def __init__(self) -> None:
        self.maps: Dict[int, Dict[str, ObjectIdentifier]] = {}
        self.scanned_at: Dict[int, float] = {}
        self.locks: Dict[int, asyncio.Lock] = {}

    def invalidate(self, device: int) -> None:
        self.maps.pop(device, None)
        self.scanned_at.pop(device, None)

    async def lookup(self, app: Application, address: Address, device: int, name: str) -> Tuple[ObjectIdentifier, str]:
        lock = self.locks.setdefault(device, asyncio.Lock())
        async with lock:
            names = self.maps.get(device)
            stale = time.monotonic() - self.scanned_at.get(device, 0.0) > self.RESCAN_AFTER
            if names is None or (name not in names and stale):
                try:
                    names = await self._scan(app, address, device)
                    self.maps[device] = names
                    self.scanned_at[device] = time.monotonic()
                    log.info("read %d object names from device %s", len(names), device)
                except (Exception, ErrorRejectAbortNack) as err:
                    log.warning("object-list scan of device %s failed: %s", device, describe_error(err))
                    names = None
            if names and name in names:
                return names[name], "object-list"

        # fallback: Who-Has
        i_haves = await app.who_has(device, device, object_name=name, timeout=3)
        for ih in i_haves or []:
            if ih.deviceIdentifier[1] == device:
                return ih.objectIdentifier, "who-has"
        raise LookupError(f"object name '{name}' not found in device {device}")

    async def _scan(self, app: Application, address: Address, device: int) -> Dict[str, ObjectIdentifier]:
        dev_id = ObjectIdentifier(f"device,{device}")
        try:
            object_list = _check(await asyncio.wait_for(
                app.read_property(address, dev_id, "object-list"), 10))
        except (Exception, ErrorRejectAbortNack):
            # device may not support segmentation: read element by element
            count = int(_check(await asyncio.wait_for(app.read_property(address, dev_id, "object-list", 0), 5)))
            object_list = []
            for i in range(1, count + 1):
                object_list.append(_check(await asyncio.wait_for(
                    app.read_property(address, dev_id, "object-list", i), 5)))

        names: Dict[str, ObjectIdentifier] = {}
        object_list = [ObjectIdentifier(o) for o in object_list]
        chunk = 20
        for start in range(0, len(object_list), chunk):
            part = object_list[start:start + chunk]
            try:
                result = await asyncio.wait_for(app.read_property_multiple(
                    address, [(oid, ["object-name"]) for oid in part]), 10)
                for oid, _prop, _idx, value in _check(result):
                    if not isinstance(value, ErrorRejectAbortNack) and value is not None:
                        names[str(value)] = ObjectIdentifier(oid)
            except (Exception, ErrorRejectAbortNack):
                for oid in part:
                    try:
                        value = _check(await asyncio.wait_for(app.read_property(address, oid, "object-name"), 5))
                        names[str(value)] = oid
                    except (Exception, ErrorRejectAbortNack):
                        continue
        return names


# ---------------------------------------------------------------------------
# Client side: writing into the ALC controller
# ---------------------------------------------------------------------------


@dataclass
class ControllerWrite:
    signal: str
    object_name: Optional[str] = None       # find the object by name (preferred)
    fixed_objid: Optional[ObjectIdentifier] = None  # or use a fixed object id
    deadband: float = 0.1
    heartbeat: float = 60.0
    priority: Optional[int] = None
    name: str = ""
    demo_object: Optional[str] = None       # object type/id the demo controller creates
    # runtime status
    objid: Optional[ObjectIdentifier] = None
    found_by: str = ""
    last_value: Any = None
    last_write: float = 0.0
    last_lookup: float = 0.0
    writes: int = 0
    errors: int = 0
    last_error: str = ""
    ok: Optional[bool] = None

    def reset(self) -> None:
        self.objid = self.fixed_objid
        self.found_by = "fixed" if self.fixed_objid else ""
        self.last_value = None
        self.last_write = 0.0
        self.last_lookup = 0.0
        self.ok = None
        self.last_error = ""


def controller_address(address: Optional[str], ip_network: int) -> Optional[str]:
    """A plain IP address of a controller on the LAN becomes 'ipnet:ip' as seen from the virtual network."""
    if not address:
        return None
    text = str(address).strip()
    head = text.split(":", 1)[0]
    if ":" in text and head.isdigit():
        return text  # already network-qualified, e.g. 1:192.168.1.20 or 5001:3
    return f"{ip_network}:{text}"


@dataclass
class ControllerLink:
    """Pushes model signals into the controller's out-of-service inputs."""

    app: Application
    model: EquipmentModel
    directory: NameDirectory
    device_instance: int
    ip_network: int
    address: Optional[str] = None  # static address, else Who-Is
    min_interval: float = 1.0
    points: List[ControllerWrite] = field(default_factory=list)
    enabled: bool = True
    # runtime
    resolved_address: Optional[Address] = None
    status: str = "starting"
    _last_resolve: float = 0.0

    @classmethod
    def from_config(cls, app, model, cfg: Dict[str, Any], directory: NameDirectory, ip_network: int) -> "ControllerLink":
        points = []
        for w in cfg.get("writes", []) or []:
            model.signal_def(w["signal"])  # validate
            fixed = parse_objid(w["object"]) if w.get("object") else None
            pt = ControllerWrite(
                signal=w["signal"],
                object_name=w.get("object_name") or None,
                fixed_objid=None if w.get("object_name") else fixed,
                deadband=float(w.get("deadband", 0.1)),
                heartbeat=float(w.get("heartbeat", 60.0)),
                priority=w.get("priority"),
                name=w.get("name", w["signal"]),
                demo_object=w.get("demo_object") or w.get("object"),
            )
            pt.reset()
            points.append(pt)
        return cls(
            app=app, model=model, directory=directory,
            device_instance=int(cfg.get("device_instance") or 0),
            ip_network=int(ip_network),
            address=controller_address(cfg.get("address"), ip_network),
            min_interval=float(cfg.get("min_interval", 1.0)),
            points=points,
            enabled=bool(cfg.get("enabled", True)),
        )

    def retarget(self, device_instance: int, address: Optional[str] = None) -> None:
        """Point this instance at a different controller (e.g. trainee re-addressed it)."""
        self.directory.invalidate(self.device_instance)
        self.device_instance = int(device_instance)
        self.address = controller_address(address, self.ip_network)
        self.resolved_address = None
        self._last_resolve = 0.0
        self.directory.invalidate(self.device_instance)
        for pt in self.points:
            pt.reset()
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

    async def _lookup(self, pt: ControllerWrite, now: float) -> bool:
        if pt.objid is not None:
            return True
        if now - pt.last_lookup < 10.0:
            return False
        pt.last_lookup = now
        try:
            pt.objid, pt.found_by = await self.directory.lookup(
                self.app, self.resolved_address, self.device_instance, pt.object_name)
            pt.last_error = ""
            log.info("%s -> %s (%s)", pt.object_name, objid_text(pt.objid), pt.found_by)
            return True
        except (Exception, ErrorRejectAbortNack) as err:
            pt.ok = False
            pt.errors += 1
            pt.last_error = describe_error(err) if not isinstance(err, LookupError) else str(err)
            return False

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
        if not self.points:
            self.status = "no controller writes configured"
            return
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
            if not await self._lookup(pt, now):
                failures += 1
                continue
            pt.last_write = now
            try:
                _check(await asyncio.wait_for(
                    self.app.write_property(
                        self.resolved_address, pt.objid, "present-value",
                        _to_bacnet(pt.objid, value), priority=pt.priority,
                    ),
                    timeout=5.0,
                ))
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
                if "unknown-object" in text and pt.object_name:
                    # object deleted or renumbered: look the name up again
                    self.directory.invalidate(self.device_instance)
                    pt.objid = None
                    pt.last_lookup = 0.0
                pt.last_error = text
                failures += 1
                log.warning("write %s %s failed: %s", objid_text(pt.objid), pt.signal, text)
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
                    "name": p.name, "signal": p.signal, "object_name": p.object_name,
                    "object": objid_text(p.objid), "found_by": p.found_by,
                    "priority": p.priority, "last_value": p.last_value, "ok": p.ok,
                    "writes": p.writes, "errors": p.errors, "last_error": p.last_error,
                    "age": round(time.monotonic() - p.last_write, 1) if p.last_write else None,
                }
                for p in self.points
            ],
        }
