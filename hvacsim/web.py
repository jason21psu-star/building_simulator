"""Web server: equipment views, fault injection and controller retargeting for every instance on the host."""

from __future__ import annotations

from pathlib import Path

from aiohttp import web

STATIC = Path(__file__).parent / "static"


async def start_web(host, bind: str, port: int) -> web.AppRunner:
    routes = web.RouteTableDef()

    def unit_of(req_or_body):
        name = req_or_body.get("unit") if isinstance(req_or_body, dict) else req_or_body.query.get("unit")
        try:
            return host.unit(name)
        except KeyError as err:
            raise web.HTTPNotFound(text=str(err))

    @routes.get("/")
    async def index(_req):
        return web.FileResponse(STATIC / "index.html")

    @routes.get("/api/host")
    async def host_state(_req):
        return web.json_response(host.host_state())

    @routes.get("/api/state")
    async def state(req):
        return web.json_response({"host": host.host_state(), **unit_of(req).state()})

    @routes.post("/api/fault")
    async def fault(req):
        body = await req.json()
        unit = unit_of(body)
        name = body["name"]
        active = bool(body.get("active", True))
        value = body.get("value")
        unit.model.set_fault(name, active, None if value in (None, "") else float(value))
        f = unit.model.faults[name]
        unit.event(f"FAULT {name} {'ACTIVE' if active else 'cleared'}"
                   + (f" ({f.value})" if f.value is not None and active else ""))
        return web.json_response({"ok": True})

    @routes.post("/api/faults/clear")
    async def clear(req):
        unit = unit_of(await req.json())
        unit.model.clear_faults()
        unit.event("all faults cleared")
        return web.json_response({"ok": True})

    @routes.post("/api/param")
    async def param(req):
        body = await req.json()
        name, value = body["name"], body["value"]
        if name == "speed":
            host.speed = max(0.0, float(value))
        elif name == "paused":
            host.paused = bool(value)
        else:
            unit = unit_of(body)
            unit.model.set_param(name, value)
            unit.event(f"parameter {name} = {value}")
        return web.json_response({"ok": True})

    @routes.post("/api/controller")
    async def controller(req):
        body = await req.json()
        unit = unit_of(body)
        if "enabled" in body:
            unit.link.enabled = bool(body["enabled"])
        if "device_instance" in body:
            unit.link.retarget(int(body["device_instance"]), body.get("address") or None)
            unit.event(f"controller target set to device {unit.link.device_instance}")
        return web.json_response({"ok": True})

    @routes.post("/api/reset")
    async def reset(req):
        unit = unit_of(await req.json())
        unit.model.reset()
        unit.model.step(0.0)
        unit.event("equipment state reset")
        return web.json_response({"ok": True})

    app = web.Application()
    app.add_routes(routes)
    runner = web.AppRunner(app, access_log=None)
    await runner.setup()
    await web.TCPSite(runner, bind, port).start()
    return runner
