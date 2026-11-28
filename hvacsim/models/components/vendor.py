"""
What every vendor's equipment shares (docs/EQUIPMENT_INTERFACE_DESIGN.md, section 3).

Chillers, boilers and generator sets are each a family (components/chillers,
components/boilers, components/gensets) with a base class, a registry and one
file per vendor.  The plants use a vendor's equipment only through this
surface and its family's, so the make and model is a CSV column.

    cls = family_class("chiller", "york_yk")
    unit = cls(tag, cfg, faults, rng)        # cfg: the CSV row + plant settings
    unit.powered, unit.comm_ok = ...
    unit.bas_write(key, value)               # the BAS writes one of the vendor's command objects
    unit.step(h, ...)                        # family-specific physics arguments
    unit.points()                            # every object in OBJECTS, by key
    unit.role("lchwt")                       # a value by BAS role (None if the vendor has none)
    unit.status()                            # {"text", "state", "codes"} for the web page
    unit.generic_state()                     # off / starting / running / stopping / alarm / lockout / local / no_power
    unit.panel(key)                          # panel / display key from the web page
"""

from __future__ import annotations

import struct
import time
from typing import Any, Dict, List, Optional, Tuple, Type

from ..base import ParamInfo, SignalDef, coerce

# generic web page states, whatever the vendor's own status texts are
STATES = ("off", "starting", "running", "stopping", "alarm", "lockout", "local", "no_power")


class VendorEquipment:
    """Interface + shared helpers.  Each family subclasses it; each vendor subclasses its family."""

    FAMILY = ""                                  # "chiller", "boiler", "genset"
    MODEL = ""                                   # registry name, e.g. "york_yk"
    MAKE = ""
    DISPLAY = ""                                 # e.g. "YORK YK (OptiView)"
    SHORT = ""                                   # e.g. "YORK YK" - table headings
    PROTOCOL = "bacnet"                          # "bacnet" or "modbus_tcp"
    INTEGRATION = ""                             # e.g. "OptiView BAS gateway" - device table text
    # integration list: (kind, address, name, key, units, role)
    #   role "cmd" = written by the BAS, None = read only, ("msv", table, count) = multi-state with texts
    OBJECTS: List[Tuple] = []
    ROLES: Dict[str, Optional[str]] = {}         # BAS role -> key in OBJECTS / points() (None = not offered)
    PANEL: List[str] = []                        # panel keys the web page shows as buttons
    PANEL_LABELS: Dict[str, str] = {}
    PANEL_PARAMS: Dict[str, ParamInfo] = {}      # panel settings (attribute name -> info), e.g. local_sp
    SETTINGS: Dict[str, Any] = {}                # vendor-specific settings and their defaults
    GENERIC_FAULTS: Dict[str, str] = {}          # set by the family: every vendor must support these
    FAULTS: Dict[str, str] = {}                  # the vendor's instructor faults: key -> text
    FAULT_ALIASES: Dict[str, str] = {}           # generic name -> the vendor's key, where they differ
    CODE_TABLES: Dict[str, Dict[int, str]] = {}  # the vendor's status / fault code texts
    BINARY_CMD_TEXT: Tuple[str, str] = ("ON", "OFF")   # active / inactive text of binary command objects
    DEVICE_MODEL_NAME = ""                       # BACnet device model name
    DEFAULT_DESCRIPTION = ""                     # device description when the CSV row has none

    @classmethod
    def cmd_default(cls, key: str, cfg: Dict[str, Any]) -> Any:
        """Power-up value of a command object (before the BAS writes it)."""
        return 0.0

    # ---------------------------------------------------------------- construction helpers
    @classmethod
    def settings_for(cls, cfg: Dict[str, Any], overrides: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        """
        Vendor settings: defaults, then the CSV row (columns with the setting's name), then the
        plant's overrides - flat, or per model ({"york_yk": {...}, "trane_cvhf": {...}}).
        """
        out = dict(cls.SETTINGS)
        for k in cls.SETTINGS:
            if cfg.get(k) not in (None, ""):
                out[k] = cfg[k]
        ov = dict(overrides or {})
        if cls.MODEL in ov and isinstance(ov[cls.MODEL], dict):
            ov = ov[cls.MODEL]
        elif any(isinstance(v, dict) for v in ov.values()):
            ov = {}                                  # per-model overrides, none for this model
        for k, v in ov.items():
            if k not in cls.SETTINGS:
                raise ValueError(f"{cls.DISPLAY}: no setting {k!r} (settings: {sorted(cls.SETTINGS)})")
            out[k] = v
        return {k: coerce(v, cls.SETTINGS[k]) for k, v in out.items()}

    @classmethod
    def fault_key(cls, generic: str) -> str:
        """The vendor's own fault key for a generic fault name."""
        return cls.FAULT_ALIASES.get(generic, generic)

    @classmethod
    def fault_aliases(cls) -> Dict[str, str]:
        return {g: k for g, k in cls.FAULT_ALIASES.items() if g != k}

    @classmethod
    def cmd_keys(cls) -> List[str]:
        return [o[3] for o in cls.OBJECTS if o[5] == "cmd"]

    @classmethod
    def check(cls) -> None:
        """Registration checks: the family's roles and generic faults are all there."""
        keys = {o[3] for o in cls.OBJECTS}
        for role in cls.WRITE_ROLES:
            k = cls.ROLES.get(role)
            if k is None or k not in cls.cmd_keys():
                raise TypeError(f"{cls.DISPLAY}: write role {role!r} must map to a command object")
        for role in cls.READ_ROLES:
            if role not in cls.ROLES:
                raise TypeError(f"{cls.DISPLAY}: read role {role!r} missing (map it to None if not offered)")
        for g in cls.GENERIC_FAULTS:
            if cls.fault_key(g) not in cls.FAULTS:
                raise TypeError(f"{cls.DISPLAY}: generic fault {g!r} has no vendor fault")
        if len(keys) != len(cls.OBJECTS):
            raise TypeError(f"{cls.DISPLAY}: duplicate keys in OBJECTS")

    WRITE_ROLES: Tuple[str, ...] = ()
    READ_ROLES: Tuple[str, ...] = ()

    # ---------------------------------------------------------------- runtime surface
    powered = True
    comm_ok = True

    bas_idle_s = 0.0                             # real seconds since the BAS last wrote (set by the runner)

    def bas_write(self, key: str, value: Any, seen: bool = True) -> None:
        """
        A command object's value.  seen=True: the BAS just wrote it (restarts a vendor's BAS
        timer); the plants re-apply their inputs every step with seen=False.
        """
        if key not in self.cmd_keys():
            raise KeyError(f"{self.DISPLAY}: {key!r} is not a command object")
        setattr(self, key, value)
        if seen:
            self.bas_idle_s = 0.0

    @classmethod
    def bas_setup_writes(cls, mode: str = "") -> Dict[str, Any]:
        """Object values a BAS must write besides its commands (e.g. the Crest's configuration bits)."""
        return {}

    @classmethod
    def encode_role(cls, role: str, value: Any, settings: Optional[Dict[str, Any]] = None) -> Any:
        """An engineering value for a write role -> what the BAS writes to that object."""
        return value

    def points(self) -> Dict[str, Any]:
        raise NotImplementedError

    def role(self, role: str, points: Optional[Dict[str, Any]] = None) -> Any:
        key = self.ROLES.get(role)
        if key is None:
            return None
        return (points if points is not None else self.points()).get(key)

    def status_text(self) -> str:
        raise NotImplementedError

    def generic_state(self) -> str:
        """One of STATES, whatever the vendor's own state names are."""
        raise NotImplementedError

    def codes(self) -> List[str]:
        """Active codes with their texts, the vendor's own numbering."""
        return []

    def status(self) -> Dict[str, Any]:
        return {"text": self.status_text(), "state": self.generic_state(), "codes": self.codes()}

    def panel(self, key: str) -> None:
        raise KeyError(key)

    def panel_param(self, name: str) -> Any:
        if name not in self.PANEL_PARAMS:
            raise KeyError(name)
        return getattr(self, name)

    def set_panel_param(self, name: str, value: Any) -> None:
        info = self.PANEL_PARAMS.get(name)
        if info is None:
            raise KeyError(name)
        v = float(value)
        if info.minimum is not None:
            v = max(float(info.minimum), v)
        if info.maximum is not None:
            v = min(float(info.maximum), v)
        setattr(self, name, v)

    # ---------------------------------------------------------------- Modbus (PROTOCOL "modbus_tcp")
    # A Modbus vendor lists its points in OBJECTS with kind coil (0xxxx), discrete-input (1xxxx),
    # input-register (3xxxx) or holding-register (4xxxx), numbered as its manual numbers them,
    # and an optional 7th element: the scale (register = value x scale, default 1) or "float32"
    # (IEEE 754 in two registers, high word first).  16-bit values are two's complement.
    # A vendor with other encodings overrides modbus_registers() / modbus_write() (the Cummins does).
    UNLISTED_REGISTER = 0                        # what a read of an unlisted register in a block returns
    MODBUS_KINDS = ("coil", "discrete-input", "input-register", "holding-register")

    @classmethod
    def modbus_objects(cls) -> List[Tuple]:
        return [o for o in cls.OBJECTS if o[0] in cls.MODBUS_KINDS]

    def modbus_registers(self, pf: float = 1.0) -> Dict[int, int]:
        """Register / bit image by Modbus number (1-9999 coils, 1xxxx inputs, 3xxxx, 4xxxx registers)."""
        pts = self.points()
        out: Dict[int, int] = {}
        tables: Dict[int, List[int]] = {}
        for o in self.modbus_objects():
            reg, enc = int(o[1]), (o[6] if len(o) > 6 else 1.0)
            v = pts.get(o[3], 0)
            if o[0] in ("coil", "discrete-input"):
                out[reg] = 1 if v else 0
            elif enc == "float32":
                hi, lo = struct.unpack(">HH", struct.pack(">f", float(v)))
                out[reg], out[reg + 1] = hi, lo
                tables.setdefault(reg // 10000, []).append(reg + 1)
            else:
                out[reg] = int(round(float(v) * float(enc))) & 0xFFFF
            tables.setdefault(reg // 10000, []).append(reg)
        for regs in tables.values():                 # holes inside each table read UNLISTED_REGISTER
            for reg in range(min(regs), max(regs) + 1):
                out.setdefault(reg, self.UNLISTED_REGISTER)
        return out

    @classmethod
    def modbus_command(cls, reg: int) -> Optional[Tuple[str, Any]]:
        """(key, scale or "float32") of the writable object at a coil / holding register number, or None."""
        for o in cls.modbus_objects():
            if int(o[1]) == int(reg) and o[5] == "cmd":
                return o[3], (o[6] if len(o) > 6 else 1.0)
        return None

    def view_fields(self) -> List[Tuple[str, str]]:
        """Vendor columns for the plant page: (label, text)."""
        return []

    def view(self) -> Dict[str, Any]:
        """Everything the plant page shows for this unit, vendor-neutral."""
        return {"model": self.MODEL, "short": self.SHORT, "display": self.DISPLAY, "protocol": self.PROTOCOL,
                "integration": self.INTEGRATION, **self.status(),
                "fields": [list(f) for f in self.view_fields()],
                "panel": [[k, self.PANEL_LABELS.get(k, k.title())] for k in self.PANEL]}


# ----------------------------------------------------------------------------- registry

_REGISTRY: Dict[str, Dict[str, Type[VendorEquipment]]] = {"chiller": {}, "boiler": {}, "genset": {}}
DEFAULT_MODEL = {"chiller": "york_yk", "boiler": "lochinvar_crest", "genset": "cummins_pcc33"}


def register(cls: Type[VendorEquipment]) -> Type[VendorEquipment]:
    cls.check()
    _REGISTRY[cls.FAMILY][cls.MODEL] = cls
    return cls


def family_class(family: str, model: Optional[str]) -> Type[VendorEquipment]:
    name = str(model or DEFAULT_MODEL[family]).strip().lower() or DEFAULT_MODEL[family]
    try:
        return _REGISTRY[family][name]
    except KeyError:
        raise ValueError(f"unknown {family} model {name!r}; known: {sorted(_REGISTRY[family])}") from None


def family_models(family: str) -> List[str]:
    return sorted(_REGISTRY[family])


def objects_table(cls: Type[VendorEquipment]) -> List[Dict[str, Any]]:
    """The integration list as rows (docs, tests, device table)."""
    return [{"kind": o[0], "address": o[1], "name": o[2], "key": o[3], "units": o[4],
             "role": "cmd" if o[5] == "cmd" else ("msv" if isinstance(o[5], tuple) else "read")}
            for o in cls.OBJECTS]


def state_texts(table: Dict[int, str], count: Optional[int] = None) -> List[str]:
    """MSV state text list (index 0 = code 0 = BACnet present value 1)."""
    n = count if count is not None else max(table) + 1
    return [table.get(i, f"Code {i}") for i in range(n)]


def _msv_states(role: Tuple) -> List[str]:
    return state_texts(role[1], role[2] if len(role) > 2 else None)


# ----------------------------------------------------------------------------- integration builder
# (section 3: the plant builds its BACnet devices or Modbus units from the vendor's list)

def signal_defs(cls: Type[VendorEquipment], pre: str, cfg: Dict[str, Any]) -> Tuple[List[SignalDef], List[SignalDef]]:
    """Model inputs (the BAS's command objects) and signals (everything it reads), named pre.key."""
    inputs, signals = [], []
    for kind, _a, oname, key, units, role in (o[:6] for o in cls.OBJECTS):
        name = f"{pre}.{key}"
        if role == "cmd":
            if kind.startswith("binary"):
                inputs.append(SignalDef(name, "binary", description=oname, default=bool(cls.cmd_default(key, cfg))))
            else:
                inputs.append(SignalDef(name, "analog", units or "noUnits", oname, float(cls.cmd_default(key, cfg))))
        elif kind.startswith("binary"):
            signals.append(SignalDef(name, "binary", description=oname, default=False))
        elif isinstance(role, tuple):
            signals.append(SignalDef(name, "analog", "noUnits", oname, 0))
        else:
            signals.append(SignalDef(name, "analog", units or "noUnits", oname, 0.0))
    return inputs, signals


def bacnet_device(cls: Type[VendorEquipment], row: Dict[str, Any], kind: str) -> Dict[str, Any]:
    """One BACnet device (plant_runner format) for a unit whose vendor speaks BACnet."""
    pre = row["pre"]
    cmds, mons = [], []
    for okind, n, oname, key, units, role in (o[:6] for o in cls.OBJECTS):
        obj = f"{okind},{n}"
        if role == "cmd":
            d = {"object": obj, "name": oname, "input": f"{pre}.{key}", "description": oname}
            if okind.startswith("binary"):
                d.update(active_text=cls.BINARY_CMD_TEXT[0], inactive_text=cls.BINARY_CMD_TEXT[1])
            else:
                d["units"] = units
            cmds.append(d)
        elif isinstance(role, tuple):
            mons.append({"object": obj, "name": oname, "signal": f"{pre}.{key}", "states": _msv_states(role)})
        else:
            d = {"object": obj, "name": oname, "signal": f"{pre}.{key}"}
            if units:
                d["units"] = units
            mons.append(d)
    return {"tag": row["tag"], "pre": pre, "sim_device": int(row["sim_device"]), "mac": int(row["mac"]),
            "model_name": cls.DEVICE_MODEL_NAME or f"{cls.DISPLAY} (simulated)",
            "description": row.get("description") or cls.DEFAULT_DESCRIPTION or cls.DISPLAY,
            "kind": kind, "vendor": cls.MODEL, "commands": cmds, "monitors": mons}


class VendorModbusUnit:
    """
    Modbus TCP unit (gateway unit ID) for one plant row whose vendor speaks Modbus.
    Reads come from the vendor's register image; writes to a command register set the
    plant's input pre.key, which the plant hands to the vendor (bas_write) each step,
    the same path a BACnet write takes.
    """

    def __init__(self, plant, row: Dict[str, Any]):
        self.plant, self.row = plant, row
        self._t = None
        self._regs: Dict[int, int] = {}
        self._pending: Dict[int, int] = {}

    @property
    def unit(self) -> VendorEquipment:
        return self.row["unit"]

    def online(self) -> bool:
        key = type(self.unit).fault_key("comm_loss")
        return not self.plant.device_offline(self.row["pre"]) and not self.plant.fault(f"{self.row['pre']}_{key}")

    def read(self, reg: int) -> Optional[int]:
        if self._t != self.plant.sim_time:
            self._regs, self._t = self.unit.modbus_registers(), self.plant.sim_time
        return self._regs.get(reg)

    def write(self, reg: int, value: int) -> bool:
        cls = type(self.unit)
        cmd = cls.modbus_command(reg)
        if cmd is None:
            prev = cls.modbus_command(reg - 1)        # low word of a float32 command
            if prev is None or prev[1] != "float32":
                return False
            hi = self._pending.pop(reg - 1, self.read(reg - 1) or 0)
            v = struct.unpack(">f", struct.pack(">HH", hi & 0xFFFF, value & 0xFFFF))[0]
            return self._set(prev[0], v)
        key, enc = cmd
        if enc == "float32":
            self._pending[reg] = value                # wait for the low word
            return True
        if reg < 10000:                               # coil
            return self._set(key, bool(value))
        v = value - 65536 if value >= 32768 else value
        return self._set(key, v / float(enc))

    def _set(self, key: str, value: Any) -> bool:
        self.plant.set_input(f"{self.row['pre']}.{key}", value)
        self.unit.bas_last_write = time.monotonic()
        self._t = None
        return True


def modbus_units(plant, rows: List[Dict[str, Any]]) -> Dict[int, VendorModbusUnit]:
    """Modbus units for the plant rows whose vendor speaks Modbus (CSV column unit_id)."""
    out: Dict[int, VendorModbusUnit] = {}
    for r in rows:
        if r["unit"].PROTOCOL != "modbus_tcp":
            continue
        if r.get("unit_id") in (None, ""):
            raise ValueError(f"{r['tag']}: a Modbus {r['unit'].FAMILY} needs a unit_id column")
        uid = int(r["unit_id"])
        if uid in out:
            raise ValueError(f"{r['tag']}: Modbus unit ID {uid} is used twice")
        out[uid] = VendorModbusUnit(plant, r)
    return out
