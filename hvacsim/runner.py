"""
Run a simulator host:  python -m hvacsim configs/station-1.yaml

One host = one IP address.  The host is a BACnet router to a virtual network
and every equipment instance in the config is its own BACnet device there.

Command-line overrides:
  --address 192.168.1.51/24     host IP address (BACnet/IP, port 47808 default)
  --controller UH-1-SIM=240115  point an instance at a different ALC controller
  --web-port 8081               web page port
  --speed 10                    run the physics 10x faster than real time
  --demo                        no network: in-process mock ALC controllers
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import time
from collections import deque
from typing import Any, Dict, List, Optional

from . import bacnet_io
from .config import load_host_config
from .models import MODELS

log = logging.getLogger("hvacsim")

DEMO_LAN = "demo-lan"


class UnitSim:
    """One equipment instance: physics model + its own BACnet device."""

    def __init__(self, inst: Dict[str, Any], host: "Host"):
        self.cfg = inst
        self.name = inst["name"]
        eq = inst["equipment"]
        model_cls = MODELS.get(eq["type"])
        if model_cls is None:
            raise SystemExit(f"{self.name}: unknown equipment type {eq['type']!r}; known: {list(MODELS)}")
        self.model = model_cls(eq.get("params") or {})
        self.app = bacnet_io.build_node(inst, host.vlan_name, host.virtual_network)
        self.commands, self.params = bacnet_io.add_command_points(self.app, self.model, inst.get("commands"))
        self.monitors = bacnet_io.add_monitor_points(self.app, self.model, inst.get("monitor"))
        self.link = bacnet_io.ControllerLink.from_config(
            self.app, self.model, inst.get("controller") or {}, host.directory, host.ip_network)
        self.started_at = time.monotonic()
        self.weather: Optional["UnitSim"] = None          # host weather station feeding this unit
        self.served_by: Optional["UnitSim"] = None        # AHU whose duct this unit sits in
        self.weather_params: Dict[str, str] = {}
        self.address = f"{host.virtual_network}:{inst['mac']}"
        self.events: deque = deque(maxlen=200)
        self._watch: Dict[str, Any] = {}

    def event(self, text: str) -> None:
        self.events.appendleft({"t": time.strftime("%H:%M:%S"), "sim": round(self.model.sim_time), "text": text})
        log.info("%s: %s", self.name, text)

    def all_links(self) -> List[bacnet_io.ControllerLink]:
        return [self.link]

    def close(self) -> None:
        self.app.close()

    def step(self, dt: float) -> None:
        if self.weather is not None:
            for param, signal in self.weather_params.items():
                self.model.p[param] = self.weather.model.signals[signal]
        if "device_offline" in self.model.faults:
            self.app.offline = self.model.faults["device_offline"].active
        bacnet_io.read_commands(self.model, self.commands, self.params)
        if "efb_idle_s" in self.model.inputs:
            mode = self.model.p.get("comm_loss_mode", "any_message")
            last = bacnet_io.last_fieldbus_activity(self.app, self.commands, self.params, mode)
            self.model.set_input("efb_idle_s", time.monotonic() - (last or self.started_at))
        if dt > 0:
            self.model.step(dt)
        bacnet_io.update_monitors(self.model, self.monitors)
        self._log_changes()

    def _log_changes(self) -> None:
        m = self.model
        watched = {f"input {d.name}": m.inputs[d.name] for d in m.INPUTS if d.kind == "binary"}
        for d in m.SIGNALS:
            if d.kind == "binary" or (d.kind == "text" and d.name != "warnings"):
                watched[d.name] = m.signals[d.name]
        for k, v in watched.items():
            if k in self._watch and self._watch[k] != v:
                extra = ""
                if k.startswith("input "):
                    for cp in self.commands:
                        if cp.input == k[6:]:
                            pr = bacnet_io.priority_summary(cp.obj)["active_priority"]
                            extra = f" ({cp.name} @ priority {pr})" if pr else f" ({cp.name} relinquished)"
                shown = v if isinstance(v, str) else ("ON" if v else "OFF")
                self.event(f"{k} -> {shown}{extra}")
            self._watch[k] = v

    def summary(self) -> Dict[str, Any]:
        link = self.link.snapshot()
        return {
            "name": self.name,
            "equipment": self.model.TYPE,
            "device_instance": int(self.cfg["device_instance"]),
            "address": self.address,
            "controller": link["device_instance"],
            "writes_ok": all(p["ok"] for p in link["points"]) if link["points"] else None,
            "active_faults": sum(1 for f in self.model.faults.values() if f.active),
            "uses_weather": self.weather is not None,
        }

    def state(self) -> Dict[str, Any]:
        m = self.model
        return {
            "unit": {**self.summary(), "description": m.DESCRIPTION, "sim_time": round(m.sim_time, 1)},
            "signals": m.signals,
            "inputs": m.inputs,
            "params": m.p,
            "param_info": [
                {"name": k, "id": i.id, "label": i.label, "group": i.group, "units": i.units,
                 "choices": i.choices, "minimum": i.minimum, "maximum": i.maximum, "value": v}
                for k, i, v in m.param_items()
            ],
            "conditions": m.CONDITIONS,
            "weather_driven": list(self.weather_params),
            "weather_source": self.weather.name if self.weather else None,
            "served_by": self.served_by.name if self.served_by else None,
            "actions": m.ACTIONS,
            "faults": [
                {"name": f.name, "label": f.label, "group": f.group, "kind": f.kind,
                 "units": f.units, "description": f.description,
                 "active": m.faults[f.name].active, "value": m.faults[f.name].value}
                for f in m.FAULTS
            ],
            "commands": [
                {"name": c.name, "object": bacnet_io.objid_text(c.objid), "input": c.input,
                 **bacnet_io.priority_summary(c.obj)}
                for c in self.commands
            ],
            "param_objects": [
                {"name": pp.name, "object": bacnet_io.objid_text(pp.objid), "param": pp.param,
                 "value": m.p[pp.param]}
                for pp in self.params
            ],
            "monitor": [
                {"name": p.name, "object": bacnet_io.objid_text(p.objid), "signal": p.signal,
                 "value": m.signals[p.signal]}
                for p in self.monitors
            ],
            "controller": self.link.snapshot(),
            "events": list(self.events)[:50],
        }


class Host:
    def __init__(self, cfg: Dict[str, Any], demo: bool = False, demo_controllers: bool = True):
        self.cfg = cfg
        self.demo = demo
        h = cfg["host"]
        self.name = h["name"]
        self.ip_network = int(h["ip_network"])
        self.virtual_network = int(h["virtual_network"])
        self.speed = float(h.get("speed", 1.0))
        self.step_s = float(h.get("step", 0.1))
        self.paused = False
        self.directory = bacnet_io.NameDirectory()
        self.sim_time = 0.0
        self.utility_ok = True              # utility power (host wide; the plant's UPS keeps some loads)
        self.restore_at: Optional[float] = None

        self.vlan_name = f"vlan{self.virtual_network}"
        bacnet_io.make_virtual_network(self.vlan_name)
        if demo:
            bacnet_io.make_virtual_network(DEMO_LAN)
        self.router = bacnet_io.build_router(h, self.vlan_name, DEMO_LAN if demo else None)
        self.units: List[Any] = [self._make_unit(inst) for inst in cfg["instances"]]
        self.weather = self._link_weather(h.get("weather"))
        self._link_air()
        self._link_chw()
        self.mocks = self._build_demo_controllers() if (demo and demo_controllers) else []

    def _make_unit(self, inst: Dict[str, Any]):
        if inst["equipment"].get("type") == "chw_plant":
            from .plant_runner import PlantSim
            return PlantSim(inst, self)
        return UnitSim(inst, self)

    def _link_chw(self) -> None:
        """Feed AHU cooling coils with `chw_from: <plant>` / `chw_branch: <riser tag>` from the plant."""
        for u in self.units:
            name = u.cfg.get("chw_from")
            if not name:
                continue
            try:
                plant = self.unit(name)
            except KeyError:
                log.warning("%s: chw_from %r is not on this host - using its fixed CHW supply parameters", u.name, name)
                continue
            if plant.model.TYPE != "chw_plant" or not hasattr(u.model, "cc"):
                raise SystemExit(f"{u.name}: chw_from must name a chw_plant and the unit needs a cooling coil")
            tag = str(u.cfg.get("chw_branch") or plant.model.branches[0]["tag"])
            branch = next((b for b in plant.model.branches if b["tag"].lower() == tag.lower()), None)
            if branch is None:
                raise SystemExit(f"{u.name}: chw_branch {tag!r} is not a riser of {name} "
                                 f"({[b['tag'] for b in plant.model.branches]})")
            plant.model.ahus.append({"model": u.model, "branch": branch, "name": u.name})
            u.chw_plant = plant

    def power(self, ok: bool, minutes: Optional[float] = None) -> None:
        """Utility power fail / restore for the whole host (optional automatic restore)."""
        self.utility_ok = bool(ok)
        self.restore_at = self.sim_time + minutes * 60.0 if (not ok and minutes) else None
        for u in self.units:
            if hasattr(u.model, "set_utility"):
                u.model.set_utility(self.utility_ok)
                u.event("UTILITY POWER " + ("RESTORED" if ok else "FAILED")
                        + (f" (restores in {minutes:g} min)" if (not ok and minutes) else ""))

    def unit(self, name: Optional[str]) -> UnitSim:
        if not name:
            return self.units[0]
        for u in self.units:
            if u.name == name:
                return u
        raise KeyError(f"no unit named {name!r}")

    def _link_weather(self, name: Optional[str]) -> Optional[UnitSim]:
        """Share one weather station's true outdoor conditions with every unit that uses them."""
        stations = [u for u in self.units if u.model.TYPE == "weather_station"]
        if name:
            stations = [u for u in stations if u.name == name]
            if not stations:
                raise SystemExit(f"host.weather: no weather_station instance named {name!r}")
        if not stations:
            return None
        wx = stations[0]
        for u in self.units:
            if u is not wx and u.model.WEATHER_PARAMS and u.cfg.get("use_weather", True):
                u.weather = wx
                u.weather_params = dict(u.model.WEATHER_PARAMS)
        # the station steps first so everyone sees this step's weather
        self.units.remove(wx)
        self.units.insert(0, wx)
        return wx

    def _link_air(self) -> None:
        """Connect units with `served_by: <AHU>` to that AHU's duct (VAV boxes)."""
        for u in self.units:
            name = u.cfg.get("served_by")
            if not name:
                continue
            try:
                ahu = self.unit(name)
            except KeyError:
                raise SystemExit(f"{u.name}: served_by {name!r} is not an instance on this host")
            if not hasattr(ahu.model, "boxes"):
                ahu.model.boxes = []
            u.model.supply = ahu.model
            ahu.model.boxes.append(u.model)
            u.served_by = ahu
        # step order: weather station, then air handlers, then what they serve
        rank = {"weather_station": 0, "ahu_vav_econ": 1, "chw_plant": 3}
        self.units.sort(key=lambda u: rank.get(u.model.TYPE, 2))

    def _build_demo_controllers(self):
        from .mockalc import AhuDemoLogic, MockController, Thermostat, VavDemoLogic

        mocks: Dict[int, MockController] = {}

        def mock_for(dev: int) -> MockController:
            if dev not in mocks:
                port = bacnet_io.virtual_port(1, DEMO_LAN, self.ip_network, 0x10 + len(mocks))
                mocks[dev] = MockController(dev, port, name=f"ALC-DEMO-{dev}")
            return mocks[dev]

        for unit in self.units:
            if unit.model.TYPE == "chw_plant":
                from .mockalc import PlantDemoLogic
                for link in unit.all_links():
                    mock = mock_for(link.device_instance)
                    for pt in link.points:
                        mock.add_point(pt.object_name, pt.demo_object, unit.model.signal_def(pt.signal).units)
                if (unit.cfg.get("plant") or {}).get("demo_logic", True) and unit.links:
                    first = mock_for(next(iter(unit.links)))
                    first.logics.append(PlantDemoLogic(first, unit, mocks))
                continue
            demo_cfg = unit.cfg.get("demo") or {}
            if not unit.link.points and not demo_cfg.get("writes") and not demo_cfg.get("logic"):
                continue   # nothing for a demo controller to do (e.g. the weather station)
            dev = unit.link.device_instance or 240000 + int(unit.cfg["mac"])
            mock = mock_for(dev)
            zone_name = None
            for pt in unit.link.points:
                name = pt.object_name or f"{unit.name}_{pt.signal}"
                objid = pt.demo_object or bacnet_io.objid_text(pt.fixed_objid)
                units = unit.model.signal_def(pt.signal).units
                mock.add_point(name, objid, units)
                if pt.signal == "space_temp":
                    zone_name = name
            if demo_cfg.get("logic") == "ahu":
                mock.logics.append(AhuDemoLogic(mock, int(unit.cfg["device_instance"]), label=unit.name))
            elif demo_cfg.get("logic") == "vav":
                mock.logics.append(VavDemoLogic(mock, int(unit.cfg["device_instance"]),
                                                float(unit.model.p["box_max_cfm"]), label=unit.name))
            for w in demo_cfg.get("writes", []) or []:
                mock.add_network_output(int(unit.cfg["device_instance"]), w["object"], w["value"],
                                        int(w.get("priority", 10)), label=unit.name)
            valve = next((c for c in unit.commands if c.input == "valve_cmd"), None)
            if zone_name and valve:
                mock.add_thermostat(Thermostat(
                    label=unit.name.lower().replace("-", "_"), zone_object=zone_name,
                    target_device=int(unit.cfg["device_instance"]),
                    target_object=bacnet_io.objid_text(valve.objid)))
        return list(mocks.values())

    async def announce(self) -> None:
        try:
            await self.router.nse.i_am_router_to_network()
        except Exception as err:  # pragma: no cover - informational only
            log.warning("router announcement failed: %s", err)

    async def run(self) -> None:
        await asyncio.sleep(0.2)
        await self.announce()
        tasks = [asyncio.create_task(link.run()) for u in self.units for link in u.all_links()]
        tasks += [asyncio.create_task(m.run()) for m in self.mocks]
        last = time.monotonic()
        try:
            while True:
                await asyncio.sleep(self.step_s)
                now = time.monotonic()
                dt = 0.0 if self.paused else (now - last) * self.speed
                last = now
                self.step(dt)
        finally:
            for t in tasks:
                t.cancel()

    def step(self, dt: float) -> None:
        self.sim_time += dt
        if self.restore_at is not None and self.sim_time >= self.restore_at:
            self.power(True)
        for u in self.units:
            u.step(dt)

    def close(self) -> None:
        for u in self.units:
            u.close()
        for m in self.mocks:
            m.app.close()
        self.router.close()
        bacnet_io.release_virtual_network(self.vlan_name)
        if self.demo:
            bacnet_io.release_virtual_network(DEMO_LAN)

    def host_state(self) -> Dict[str, Any]:
        h = self.cfg["host"]
        return {
            "name": self.name,
            "device_instance": int(h["device_instance"]),
            "address": "in-process demo network" if self.demo else h["address"],
            "ip_network": self.ip_network,
            "virtual_network": self.virtual_network,
            "speed": self.speed,
            "paused": self.paused,
            "demo": self.demo,
            "weather": self.weather.name if self.weather else None,
            "utility_ok": self.utility_ok,
            "restore_in": round(self.restore_at - self.sim_time) if self.restore_at is not None else None,
            "units": [u.summary() for u in self.units],
            "demo_controllers": [m.snapshot() for m in self.mocks],
        }


def apply_overrides(cfg: Dict[str, Any], args: argparse.Namespace) -> Dict[str, Any]:
    h = cfg["host"]
    if args.address:
        h["address"] = args.address
    if args.speed is not None:
        h["speed"] = args.speed
    if args.web_port is not None:
        cfg.setdefault("web", {})["port"] = args.web_port
    for item in args.controller or []:
        name, _, dev = item.partition("=")
        matches = [i for i in cfg["instances"] if i["name"] == name and "plant" not in i]
        if not matches or not dev.isdigit():
            raise SystemExit(f"--controller expects NAME=DEVICE_ID with a known instance name, got {item!r}")
        matches[0]["controller"]["device_instance"] = int(dev)
    return cfg


async def amain(args: argparse.Namespace) -> None:
    from .web import start_web

    cfg = apply_overrides(load_host_config(args.config), args)
    host = Host(cfg, demo=args.demo)
    web_cfg = cfg.get("web", {}) or {}
    port = int(web_cfg.get("port", 8080))
    await start_web(host, web_cfg.get("host", "0.0.0.0"), port)
    log.info("%s: router device %s on %s, IP network %s -> virtual network %s; %d instance(s); web :%d",
             host.name, cfg["host"]["device_instance"],
             "demo network" if args.demo else cfg["host"]["address"],
             host.ip_network, host.virtual_network, len(host.units), port)
    for u in host.units:
        if hasattr(u, "facets"):
            u.event(f"started as devices {u.summary()['device_instance']}; writing to controllers "
                    + ", ".join(str(c) for c in u.links))
        else:
            u.event(f"started as device {u.cfg['device_instance']} at {u.address}; "
                    f"writing to controller {u.link.device_instance}")
    try:
        await host.run()
    finally:
        host.close()


def main() -> None:
    ap = argparse.ArgumentParser(description="HVAC equipment training simulator host")
    ap.add_argument("config", help="host YAML file")
    ap.add_argument("--address", help="host BACnet/IP address, e.g. 192.168.1.51/24")
    ap.add_argument("--controller", action="append", metavar="NAME=ID",
                    help="ALC controller device ID for an instance (repeatable)")
    ap.add_argument("--web-port", type=int)
    ap.add_argument("--speed", type=float, help="time acceleration factor")
    ap.add_argument("--demo", action="store_true",
                    help="no network: run mock ALC controllers on an in-process network")
    ap.add_argument("-v", "--verbose", action="store_true")
    args = ap.parse_args()
    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO,
                        format="%(asctime)s %(name)s %(levelname)s %(message)s")
    try:
        asyncio.run(amain(args))
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
