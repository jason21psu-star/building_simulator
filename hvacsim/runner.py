"""
Run one equipment simulator:  python -m hvacsim configs/uh-1.yaml

Command-line overrides (handy in the lab, no YAML editing needed):
  --controller-id 240110     ALC controller device instance to write into
  --controller-address IP    skip Who-Is and use this address
  --address 192.168.1.51/24  simulator's own BACnet/IP address
  --device-id 4010001        simulator's own device instance
  --web-port 8081            instructor/trainee web page
  --speed 10                 run the physics 10x faster than real time
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import time
from collections import deque
from pathlib import Path
from typing import Any, Dict

import yaml

from . import bacnet_io
from .models import MODELS

log = logging.getLogger("hvacsim")


class Simulation:
    def __init__(self, cfg: Dict[str, Any], config_path: str):
        self.cfg = cfg
        self.config_path = config_path
        sim_cfg = cfg["simulator"]
        eq = cfg["equipment"]
        model_cls = MODELS.get(eq["type"])
        if model_cls is None:
            raise SystemExit(f"unknown equipment type {eq['type']!r}; known: {list(MODELS)}")
        self.model = model_cls(eq.get("params") or {})
        self.speed = float(sim_cfg.get("speed", 1.0))
        self.step_s = float(sim_cfg.get("step", 0.1))
        self.paused = False

        self.app = bacnet_io.build_application(sim_cfg)
        self.commands = bacnet_io.add_command_points(self.app, self.model, cfg.get("commands"))
        self.monitors = bacnet_io.add_monitor_points(self.app, self.model, cfg.get("monitor"))
        self.link = bacnet_io.ControllerLink.from_config(self.app, self.model, cfg["controller"])

        self.events: deque = deque(maxlen=200)
        self._watch = {}  # last values of binary signals/inputs for the event log

    # ------------------------------------------------------------------

    def event(self, text: str) -> None:
        self.events.appendleft({"t": time.strftime("%H:%M:%S"), "sim": round(self.model.sim_time), "text": text})
        log.info(text)

    def _log_changes(self) -> None:
        m = self.model
        watched = {f"input {k}": v for k, v in m.inputs.items()}
        for d in m.SIGNALS:
            if d.kind == "binary":
                watched[d.name] = m.signals[d.name]
        for k, v in watched.items():
            if k in self._watch and self._watch[k] != v:
                extra = ""
                if k.startswith("input "):
                    for cp in self.commands:
                        if cp.input == k[6:]:
                            pr = bacnet_io.priority_summary(cp.obj)["active_priority"]
                            extra = f" ({cp.name} @ priority {pr})" if pr else f" ({cp.name} relinquished)"
                self.event(f"{k} -> {'ON' if v else 'OFF'}{extra}")
            self._watch[k] = v

    async def run(self) -> None:
        last = time.monotonic()
        while True:
            await asyncio.sleep(self.step_s)
            now = time.monotonic()
            real_dt, last = now - last, now
            bacnet_io.read_commands(self.model, self.commands)
            if not self.paused:
                self.model.step(real_dt * self.speed)
            bacnet_io.update_monitors(self.model, self.monitors)
            self._log_changes()

    # ------------------------------------------------------------------

    def state(self) -> Dict[str, Any]:
        m = self.model
        sim_cfg = self.cfg["simulator"]
        return {
            "simulator": {
                "name": sim_cfg["name"],
                "device_instance": int(sim_cfg["device_instance"]),
                "address": sim_cfg["address"],
                "equipment": m.TYPE,
                "description": m.DESCRIPTION,
                "sim_time": round(m.sim_time, 1),
                "speed": self.speed,
                "paused": self.paused,
            },
            "signals": m.signals,
            "inputs": m.inputs,
            "params": m.p,
            "faults": [
                {"name": f.name, "label": f.label, "group": f.group, "kind": f.kind,
                 "units": f.units, "description": f.description,
                 "active": m.faults[f.name].active, "value": m.faults[f.name].value}
                for f in m.FAULTS
            ],
            "commands": [
                {"name": c.name, "object": f"{c.objid[0]},{c.objid[1]}", "input": c.input,
                 **bacnet_io.priority_summary(c.obj)}
                for c in self.commands
            ],
            "monitor": [
                {"name": p.name, "object": f"{p.objid[0]},{p.objid[1]}", "signal": p.signal,
                 "value": m.signals[p.signal]}
                for p in self.monitors
            ],
            "controller": self.link.snapshot(),
            "events": list(self.events)[:50],
        }


def load_config(path: str, args: argparse.Namespace) -> Dict[str, Any]:
    cfg = yaml.safe_load(Path(path).read_text())
    sim, ctl = cfg["simulator"], cfg["controller"]
    if args.controller_id is not None:
        ctl["device_instance"] = args.controller_id
    if args.controller_address is not None:
        ctl["address"] = args.controller_address
    if args.address is not None:
        sim["address"] = args.address
    if args.device_id is not None:
        sim["device_instance"] = args.device_id
    if args.speed is not None:
        sim["speed"] = args.speed
    if args.web_port is not None:
        cfg.setdefault("web", {})["port"] = args.web_port
    return cfg


async def amain(args: argparse.Namespace) -> None:
    from .web import start_web

    cfg = load_config(args.config, args)
    sim = Simulation(cfg, args.config)
    web_cfg = cfg.get("web", {}) or {}
    await start_web(sim, web_cfg.get("host", "0.0.0.0"), int(web_cfg.get("port", 8080)))
    sim.event(
        f"{cfg['simulator']['name']} started as device {cfg['simulator']['device_instance']} "
        f"on {cfg['simulator']['address']}; writing to controller {sim.link.device_instance}"
    )
    await asyncio.gather(sim.run(), sim.link.run())


def main() -> None:
    ap = argparse.ArgumentParser(description="HVAC equipment training simulator")
    ap.add_argument("config", help="simulator YAML file")
    ap.add_argument("--controller-id", type=int, help="ALC controller device instance")
    ap.add_argument("--controller-address", help="controller address (skip Who-Is), e.g. 192.168.1.20")
    ap.add_argument("--address", help="simulator BACnet/IP address, e.g. 192.168.1.51/24")
    ap.add_argument("--device-id", type=int, help="simulator device instance")
    ap.add_argument("--web-port", type=int)
    ap.add_argument("--speed", type=float, help="time acceleration factor")
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
