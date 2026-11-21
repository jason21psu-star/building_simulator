"""Small web server for the equipment view, fault injection and controller retargeting."""

from __future__ import annotations

from pathlib import Path

from aiohttp import web

STATIC = Path(__file__).parent / "static"


async def start_web(sim, host: str, port: int) -> web.AppRunner:
    routes = web.RouteTableDef()

    @routes.get("/")
    async def index(_req):
        return web.FileResponse(STATIC / "index.html")

    @routes.get("/api/state")
    async def state(_req):
        return web.json_response(sim.state())

    @routes.post("/api/fault")
    async def fault(req):
        body = await req.json()
        name = body["name"]
        active = bool(body.get("active", True))
        value = body.get("value")
        sim.model.set_fault(name, active, None if value in (None, "") else float(value))
        f = sim.model.faults[name]
        sim.event(f"FAULT {name} {'ACTIVE' if active else 'cleared'}" + (f" ({f.value})" if f.value is not None and active else ""))
        return web.json_response({"ok": True})

    @routes.post("/api/faults/clear")
    async def clear(_req):
        sim.model.clear_faults()
        sim.event("all faults cleared")
        return web.json_response({"ok": True})

    @routes.post("/api/param")
    async def param(req):
        body = await req.json()
        name, value = body["name"], body["value"]
        if name == "speed":
            sim.speed = max(0.0, float(value))
        elif name == "paused":
            sim.paused = bool(value)
        else:
            sim.model.set_param(name, value)
        sim.event(f"parameter {name} = {value}")
        return web.json_response({"ok": True})

    @routes.post("/api/controller")
    async def controller(req):
        body = await req.json()
        if "enabled" in body:
            sim.link.enabled = bool(body["enabled"])
        if "device_instance" in body:
            sim.link.retarget(int(body["device_instance"]), body.get("address") or None)
            sim.event(f"controller target set to device {sim.link.device_instance}")
        return web.json_response({"ok": True})

    @routes.post("/api/reset")
    async def reset(_req):
        sim.model.reset()
        sim.model.step(0.0)
        sim.event("equipment state reset")
        return web.json_response({"ok": True})

    app = web.Application()
    app.add_routes(routes)
    runner = web.AppRunner(app, access_log=None)
    await runner.setup()
    await web.TCPSite(runner, host, port).start()
    return runner
