"""Tiny HTTP API for Apple Shortcuts / Watch buttons. Runs inside the bot process on localhost;
Caddy terminates HTTPS in front of it (see deploy/https.sh).

All routes need the API token: `Authorization: Bearer <token>` or `?token=<token>`.
Responses are short plain text, made to be shown on a Watch.
"""

import asyncio
import logging
import secrets

from aiohttp import web

from . import commands, devices
from .config import settings

log = logging.getLogger(__name__)


def _plain(text: str) -> str:
    return text.replace("*", "").replace("`", "").replace("_", "")


@web.middleware
async def auth(request: web.Request, handler):
    if request.path == "/health":
        return await handler(request)
    want = settings().api_token
    got = request.headers.get("Authorization", "").removeprefix("Bearer ").strip() or request.query.get("token", "")
    if not want or not secrets.compare_digest(got, want):
        return web.Response(status=401, text="unauthorised")
    return await handler(request)


def make_app(on_action) -> web.Application:
    """`on_action(text)` is awaited after each command so the Telegram dashboard reflects it."""

    async def run(text: str, use_llm: bool) -> web.Response:
        r = await asyncio.to_thread(commands.respond, text, use_llm)
        reply = _plain(r.text) if r.text else (await asyncio.to_thread(commands.status_text))
        log.info("web: %r → %s", text, reply.replace("\n", " | ")[:120])
        try:
            await on_action(f"⌚ {reply}" if r.text else "")
        except Exception:
            log.exception("dashboard refresh after web action failed")
        return web.Response(text=reply)

    async def device_action(request: web.Request) -> web.Response:
        name, action = request.match_info["device"], request.match_info["action"]
        try:
            dev = devices.get(name)
        except devices.PlugError as e:
            return web.Response(status=404, text=str(e))
        if action == "toggle":
            st = await asyncio.to_thread(dev.state)
            action = "off" if st.on else "on"
        return await run(f"{dev.name} {action}", use_llm=False)

    async def status(request: web.Request) -> web.Response:
        return web.Response(text=_plain(await asyncio.to_thread(commands.status_text)))

    async def cmd(request: web.Request) -> web.Response:
        text = request.query.get("text") or (await request.post()).get("text") or ""
        if not str(text).strip():
            return web.Response(status=400, text="say something, e.g. geyser on for 20 min")
        return await run(str(text), use_llm=True)

    async def arrive(request: web.Request) -> web.Response:
        """Phone joined home Wi-Fi (iOS Shortcuts automation). Cancels a pending 'left home'; runs ARRIVE_COMMAND
        unless you were already home (within the cooldown); ends away mode."""
        import time as _time

        from . import scheduler

        p = scheduler.presence
        if await asyncio.to_thread(scheduler.cancel_leave):
            log.info("arrive: back within the leave delay — treated as a Wi-Fi blip")
            p["state"] = "home"
            return web.Response(text="🏠 Back on Wi-Fi — leave cancelled, nothing changed")
        now = _time.time()
        if p["state"] == "home" and now - p["arrived"] < settings().arrive_cooldown_minutes * 60:
            mins = round((now - p["arrived"]) / 60)
            return web.Response(text=f"🏠 Already home (arrived {mins} min ago) — nothing to do")
        p.update(state="home", arrived=now)
        notes = []
        if scheduler.away_state():
            n = await asyncio.to_thread(scheduler.end_away, True)
            notes.append(f"away mode off, {n} routine{'s' if n != 1 else ''} resumed")
        r = await asyncio.to_thread(commands.respond, settings().arrive_command, False)
        reply = "🏠 You're home — " + "; ".join(notes + [_plain(r.text).replace("✅ ", "")])
        log.info("arrive: %s", reply)
        try:
            await on_action(reply)
        except Exception:
            log.exception("dashboard refresh after arrive failed")
        return web.Response(text=reply)

    async def leave(request: web.Request) -> web.Response:
        """Phone left home Wi-Fi. Acts after LEAVE_DELAY_MINUTES unless the phone rejoins first."""
        from . import scheduler

        when = await asyncio.to_thread(scheduler.schedule_leave)
        log.info("leave: scheduled for %s", when.isoformat(timespec="minutes"))
        return web.Response(text=f"👋 Leaving — '{settings().leave_command}' at {commands.fmt_t(when)} "
                                 "unless you're back on Wi-Fi before then")

    async def health(request: web.Request) -> web.Response:
        return web.Response(text="ok")

    app = web.Application(middlewares=[auth])
    app.router.add_get("/health", health)
    app.router.add_get("/status", status)
    app.router.add_get("/arrive", arrive)
    app.router.add_get("/leave", leave)
    app.router.add_route("*", "/cmd", cmd)
    app.router.add_get(r"/{action:on|off|toggle}/{device}", device_action)
    return app


async def start(on_action) -> web.AppRunner | None:
    s = settings()
    if not s.api_token:
        log.info("web API disabled (no API_TOKEN)")
        return None
    runner = web.AppRunner(make_app(on_action), access_log=None)
    await runner.setup()
    await web.TCPSite(runner, s.web_host, s.web_port).start()  # no access log: URLs carry the API token
    log.info("web API on %s:%s", s.web_host, s.web_port)
    return runner
