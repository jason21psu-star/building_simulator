"""
A plant instance on the simulator host: one physics model, several BACnet devices.

  * one device per chiller (Mode B): the YORK OptiView BAS object list
  * one I/O device per ALC plant controller (Mode A): the BO/AO objects that
    controller's network outputs write, i.e. the field wiring
  * one controller link per ALC controller: sensor values written into its
    out-of-service inputs by object name (riser remote-DP controllers have
    only a link, sent from the first I/O device)
"""

from __future__ import annotations

import logging
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from . import bacnet_io
from .models.chw_plant import ChwPlant
from .models.components.york_yk import YK_OBJECTS, state_texts

log = logging.getLogger("hvacsim")


@dataclass
class Facet:
    """One BACnet device of the plant."""
    name: str
    kind: str                      # "chiller" or "io"
    device_instance: int
    mac: int
    address: str
    app: Any
    commands: List[bacnet_io.CommandPoint] = field(default_factory=list)
    monitors: List[bacnet_io.MonitorPoint] = field(default_factory=list)
    controller: Optional[int] = None
    chiller: Optional[Dict[str, Any]] = None


class PlantSim:
    def __init__(self, inst: Dict[str, Any], host):
        self.cfg = inst
        self.name = inst["name"]
        eq = inst["equipment"]
        self.model = ChwPlant(eq.get("params") or {}, layout=inst.get("plant_layout") or {})
        plant_cfg = inst.get("plant") or {}
        self.weather = None
        self.served_by = None
        self.weather_params: Dict[str, str] = {}
        self.events: deque = deque(maxlen=300)
        self._watch: Dict[str, Any] = {}
        self.facets: List[Facet] = []
        self.links: Dict[int, bacnet_io.ControllerLink] = {}
        net = host.virtual_network
        m = self.model

        # chillers: YORK OptiView BAS object list
        for c in m.chillers:
            pre = c["pre"]
            node = {"name": c["tag"], "device_instance": int(c["sim_device"]), "mac": int(c["mac"]),
                    "model_name": "YORK YK OptiView (simulated)",
                    "description": c.get("description", "YORK YK VSD centrifugal chiller")}
            app = bacnet_io.build_node(node, host.vlan_name, net)
            cmds, mons = [], []
            for kind, n, oname, key, units, role in YK_OBJECTS:
                obj = f"{kind},{n}"
                if role == "cmd":
                    d = {"object": obj, "name": oname, "input": f"{pre}.{key}", "description": oname}
                    if kind.startswith("binary"):
                        d.update(active_text="RUN", inactive_text="STOP")
                    else:
                        d["units"] = units
                    cmds.append(d)
                elif isinstance(role, tuple):
                    _tag, table, count = role
                    mons.append({"object": obj, "name": oname, "signal": f"{pre}.{key}", "states": state_texts(table, count)})
                else:
                    d = {"object": obj, "name": oname, "signal": f"{pre}.{key}"}
                    if units:
                        d["units"] = units
                    mons.append(d)
            commands, _params = bacnet_io.add_command_points(app, m, cmds)
            monitors = bacnet_io.add_monitor_points(app, m, mons)
            self.facets.append(Facet(c["tag"], "chiller", node["device_instance"], node["mac"],
                                     f"{net}:{node['mac']}", app, commands, monitors, chiller=c))

        # I/O devices: one per ALC controller that has outputs
        io_cfg = {int(k): v for k, v in (plant_cfg.get("io_devices") or {}).items()}
        writes_cfg = plant_cfg.get("writes") or {}
        addresses = {int(k): v for k, v in (plant_cfg.get("controller_addresses") or {}).items()}
        sender = None
        for ctl, io in sorted(m.io.items()):
            if not (io["bo"] or io["ao"]):
                continue
            if ctl not in io_cfg:
                raise SystemExit(f"{self.name}: controller {ctl} has outputs but no plant.io_devices entry "
                                 f"(device_instance / mac of its simulator I/O device)")
            d = io_cfg[ctl]
            node = {"name": f"{self.name}-IO-{ctl}", "device_instance": int(d["device_instance"]), "mac": int(d["mac"]),
                    "model_name": "Plant I/O (simulated field wiring)",
                    "description": f"{self.name} hardwired I/O for ALC controller {ctl}"}
            app = bacnet_io.build_node(node, host.vlan_name, net)
            cmds = []
            for i, name in enumerate(io["bo"], start=1):
                cmds.append({"object": f"binary-output,{i}", "name": name, "input": name,
                             "active_text": "ON", "inactive_text": "OFF"})
            for i, name in enumerate(io["ao"], start=1):
                cmds.append({"object": f"analog-output,{i}", "name": name, "input": name, "units": "percent"})
            commands, _params = bacnet_io.add_command_points(app, m, cmds)
            self.facets.append(Facet(node["name"], "io", node["device_instance"], node["mac"],
                                     f"{net}:{node['mac']}", app, commands, [], controller=ctl))
            if sender is None:
                sender = app
        if sender is None:
            raise SystemExit(f"{self.name}: plant.io_devices is empty")

        # controller links (simulator -> ALC inputs)
        by_ctl = {f.controller: f.app for f in self.facets if f.kind == "io"}
        for ctl, io in sorted(m.io.items()):
            if not io["writes"]:
                continue
            writes = []
            for i, (signal, oname) in enumerate(io["writes"], start=1):
                binary = m.signal_def(signal).kind == "binary"
                opts = writes_cfg.get("binary" if binary else "analog") or {}
                w = {"signal": signal, "object_name": oname, "name": oname,
                     "heartbeat": float(opts.get("heartbeat", 30.0)),
                     "demo_object": f"{'binary' if binary else 'analog'}-input,{i}"}
                if not binary:
                    w["deadband"] = float(opts.get("deadband", 0.1))
                writes.append(w)
            cfg = {"device_instance": ctl, "address": addresses.get(ctl), "writes": writes,
                   "min_interval": float(writes_cfg.get("min_interval", 1.0))}
            self.links[ctl] = bacnet_io.ControllerLink.from_config(
                by_ctl.get(ctl, sender), m, cfg, host.directory, host.ip_network)
        self.link = next(iter(self.links.values()))       # (single-link API compatibility)
        self.address = ", ".join(f.address for f in self.facets)

    # ------------------------------------------------------------------

    def all_links(self) -> List[bacnet_io.ControllerLink]:
        return list(self.links.values())

    def close(self) -> None:
        for f in self.facets:
            f.app.close()

    def event(self, text: str) -> None:
        self.events.appendleft({"t": time.strftime("%H:%M:%S"), "sim": round(self.model.sim_time), "text": text})
        log.info("%s: %s", self.name, text)

    def step(self, dt: float) -> None:
        m = self.model
        if self.weather is not None:
            for param, signal in self.weather_params.items():
                m.p[param] = self.weather.model.signals[signal]
        for f in self.facets:
            if f.kind == "chiller":
                pre = f.chiller["pre"]
                f.app.offline = (not m.utility_ok) or m.fault(f"{pre}_gateway_offline")
            bacnet_io.read_commands(m, f.commands)
        if dt > 0:
            m.step(dt)
        for f in self.facets:
            if f.monitors:
                bacnet_io.update_monitors(m, f.monitors)
        self._log_changes()

    def _log_changes(self) -> None:
        m = self.model
        watched: Dict[str, Any] = {}
        for f in self.facets:
            for cp in f.commands:
                if m.signal_def(cp.input).kind == "binary":
                    watched[f"input {cp.input}"] = m.inputs[cp.input]
        for c in m.chillers:
            watched[f"{c['tag']}"] = m.signals[f"{c['pre']}.status"].split(" · ")[0]
        for k in m.drive_keys:
            st = m.signals[f"{k}.status"].split("|")
            watched[f"{k.upper()} drive"] = st[1] if len(st) > 1 else ""
        watched["utility power"] = "NORMAL" if m.utility_ok else "FAILED"
        for k, v in watched.items():
            if k in self._watch and self._watch[k] != v:
                shown = v if isinstance(v, str) else ("ON" if v else "OFF")
                self.event(f"{k} -> {shown}")
            self._watch[k] = v

    # ------------------------------------------------------------------

    def summary(self) -> Dict[str, Any]:
        points = [p for l in self.links.values() for p in l.snapshot()["points"]]
        return {
            "name": self.name,
            "equipment": self.model.TYPE,
            "device_instance": ", ".join(str(f.device_instance) for f in self.facets),
            "address": self.address,
            "controller": ", ".join(str(c) for c in self.links),
            "writes_ok": all(p["ok"] for p in points) if points else None,
            "active_faults": sum(1 for f in self.model.faults.values() if f.active),
            "uses_weather": self.weather is not None,
        }

    def state(self) -> Dict[str, Any]:
        m = self.model
        links = {ctl: l.snapshot() for ctl, l in self.links.items()}
        merged_points = []
        for ctl, snap in links.items():
            for p in snap["points"]:
                merged_points.append({**p, "name": f"{ctl} · {p['name']}"})
        return {
            "unit": {**self.summary(), "description": m.DESCRIPTION, "sim_time": round(m.sim_time, 1)},
            "signals": m.signals,
            "inputs": m.inputs,
            "params": {k: (v if not isinstance(v, dict) else "") for k, v in m.p.items()},
            "param_info": [
                {"name": k, "id": i.id, "label": i.label, "group": i.group, "units": i.units,
                 "choices": i.choices, "minimum": i.minimum, "maximum": i.maximum, "value": v}
                for k, i, v in m.param_items()
            ],
            "conditions": m.CONDITIONS,
            "weather_driven": list(self.weather_params),
            "weather_source": self.weather.name if self.weather else None,
            "served_by": None,
            "actions": m.ACTIONS,
            "faults": [
                {"name": f.name, "label": f.label, "group": f.group, "kind": f.kind,
                 "units": f.units, "description": f.description,
                 "active": m.faults[f.name].active, "value": m.faults[f.name].value}
                for f in m.FAULTS
            ],
            "commands": [
                {"name": f"{f.name} · {c.name}", "object": bacnet_io.objid_text(c.objid), "input": c.input,
                 **bacnet_io.priority_summary(c.obj)}
                for f in self.facets for c in f.commands
            ],
            "param_objects": [],
            "monitor": [],
            "controller": {"device_instance": None, "multi": True, "address": None,
                           "status": " · ".join(f"{ctl}: {s['status']}" for ctl, s in links.items()),
                           "enabled": True, "points": merged_points},
            "devices": [{"name": f.name, "kind": f.kind, "device_instance": f.device_instance,
                         "address": f.address, "controller": f.controller, "offline": bool(getattr(f.app, "offline", False)),
                         "objects": len(f.commands) + len(f.monitors)} for f in self.facets],
            "controllers": [{"device_instance": ctl, "status": s["status"],
                             "ok": sum(1 for p in s["points"] if p["ok"]), "points": len(s["points"])}
                            for ctl, s in links.items()],
            "events": list(self.events)[:60],
        }
