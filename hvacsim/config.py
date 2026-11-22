"""
Load a simulator host config.

A host is one machine / one IP address.  It runs a BACnet router to a virtual
network, and every equipment instance is its own BACnet device on that
virtual network.

    host:       router device, IP address, network numbers, speed
    web:        web page port
    instances:  list of equipment instances; each normally references a
                template (shared points list) and overrides what differs

Template strings may contain {placeholders} filled from the instance's
`vars` plus {name}, {mac} and {device_instance}.  For example
`object_name: "fan_amps_{n}"` with `vars: {n: 2}` becomes `fan_amps_2`.
"""

from __future__ import annotations

import copy
import string
from pathlib import Path
from typing import Any, Dict, List

import yaml

LIST_KEYS = ("commands", "monitor")


class ConfigError(ValueError):
    pass


def _deep_merge(base: Dict[str, Any], over: Dict[str, Any]) -> Dict[str, Any]:
    out = copy.deepcopy(base)
    for k, v in (over or {}).items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _deep_merge(out[k], v)
        else:
            out[k] = copy.deepcopy(v)
    return out


class _Vars(dict):
    def __missing__(self, key):
        raise ConfigError(f"template placeholder {{{key}}} has no value; add it to the instance's vars")


def _substitute(obj: Any, values: Dict[str, Any]) -> Any:
    if isinstance(obj, str):
        if "{" not in obj:
            return obj
        result = string.Formatter().vformat(obj, (), _Vars(values))
        # keep numbers numeric when the whole string was a placeholder
        if obj.strip().startswith("{") and obj.strip().endswith("}") and result.lstrip("-").isdigit():
            return int(result)
        return result
    if isinstance(obj, list):
        return [_substitute(x, values) for x in obj]
    if isinstance(obj, dict):
        return {k: _substitute(v, values) for k, v in obj.items()}
    return obj


def load_host_config(path: str) -> Dict[str, Any]:
    path_obj = Path(path)
    cfg = yaml.safe_load(path_obj.read_text()) or {}
    if "host" not in cfg or "instances" not in cfg:
        raise ConfigError(f"{path}: needs 'host' and 'instances' sections")
    cfg["instances"] = [resolve_instance(inst, path_obj.parent, i) for i, inst in enumerate(cfg["instances"])]
    validate(cfg)
    return cfg


def resolve_instance(inst: Dict[str, Any], base_dir: Path, index: int) -> Dict[str, Any]:
    inst = dict(inst)
    merged: Dict[str, Any] = {}
    if "template" in inst:
        tpath = (base_dir / inst.pop("template")).resolve()
        merged = yaml.safe_load(tpath.read_text()) or {}
        merged["_template"] = str(tpath)
    # list sections given on the instance replace the template's list
    for key in LIST_KEYS:
        if key in inst:
            merged[key] = inst.pop(key)
    merged = _deep_merge(merged, inst)

    merged.setdefault("mac", index + 1)
    if "name" not in merged:
        raise ConfigError(f"instance #{index + 1} needs a name")
    if "device_instance" not in merged:
        raise ConfigError(f"instance {merged['name']} needs a device_instance")
    values = {
        "name": merged["name"],
        "mac": merged["mac"],
        "device_instance": merged["device_instance"],
        **(merged.get("vars") or {}),
    }
    return _substitute(merged, values)


def validate(cfg: Dict[str, Any]) -> None:
    host = cfg["host"]
    for key in ("name", "device_instance", "address", "ip_network", "virtual_network"):
        if key not in host:
            raise ConfigError(f"host.{key} is required")
    if int(host["ip_network"]) == int(host["virtual_network"]):
        raise ConfigError("host.ip_network and host.virtual_network must be different network numbers")
    for key in ("ip_network", "virtual_network"):
        if not 1 <= int(host[key]) <= 65534:
            raise ConfigError(f"host.{key} must be 1-65534")

    seen_names, seen_dev, seen_mac = set(), {int(host["device_instance"])}, set()
    insts: List[Dict[str, Any]] = cfg["instances"]
    if not insts:
        raise ConfigError("at least one instance is required")
    for inst in insts:
        name, dev, mac = inst["name"], int(inst["device_instance"]), int(inst["mac"])
        if name in seen_names:
            raise ConfigError(f"duplicate instance name {name}")
        if dev in seen_dev:
            raise ConfigError(f"{name}: device instance {dev} is already used on this host")
        if not 1 <= mac <= 254:
            raise ConfigError(f"{name}: mac must be 1-254")
        if mac in seen_mac:
            raise ConfigError(f"{name}: mac {mac} is already used on this host")
        seen_names.add(name)
        seen_dev.add(dev)
        seen_mac.add(mac)
        if "equipment" not in inst or "type" not in inst["equipment"]:
            raise ConfigError(f"{name}: equipment.type is required (usually from the template)")
        ctl = inst.get("controller") or {}
        if "device_instance" not in ctl:
            raise ConfigError(f"{name}: controller.device_instance is required")
        for w in ctl.get("writes", []) or []:
            if not (w.get("object_name") or w.get("object")):
                raise ConfigError(f"{name}: controller write for {w.get('signal')} needs object_name or object")
