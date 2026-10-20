"""Telegram bot — one self-updating dashboard message.

The chat holds a single message: device states, the last action (with Undo), and buttons.
Taps edit it in place. Typed commands are handled, then deleted, and the dashboard re-posts at the bottom.
Scheduled on/offs update it silently; only problems arrive as separate messages.
"""

import asyncio
import html
import json
import logging
import sys
from datetime import datetime

from telegram import (BotCommand, InlineKeyboardButton as Btn, InlineKeyboardMarkup, ReplyKeyboardRemove, Update)
from telegram.constants import ParseMode
from telegram.error import BadRequest
from telegram.ext import Application, CallbackQueryHandler, CommandHandler, ContextTypes, MessageHandler, filters

from . import commands, devices, llm, nlp, notify, scheduler
from .config import settings

log = logging.getLogger(__name__)
LAYOUT_VERSION = "5"  # bump to wipe the chat (last 48h) and re-post a fresh dashboard on next start


# ---------- per-chat dashboard state (survives restarts) ----------

class State:
    def __init__(self):
        self.path = settings().data_path / "dashboard.json"
        try:
            self.data = json.loads(self.path.read_text())
        except Exception:
            self.data = {}

    def get(self, chat_id: int) -> dict:
        return self.data.setdefault(str(chat_id), {"msg_id": None, "last": "", "undo": None, "view": "home"})

    def save(self):
        self.path.write_text(json.dumps(self.data))


STATE: State | None = None


# ---------- rendering ----------

def render_home(st: dict, live: bool = False) -> tuple[str, InlineKeyboardMarkup]:
    now = datetime.now(nlp.TZ)
    lines = []
    if st.get("last"):  # latest happening first — it's also what the notification preview shows
        lines += [st["last"], ""]
    rows, extra = [], []
    if banner := commands.away_banner():
        lines += [banner, ""]
    for name, dev in devices.registry().items():
        line, on = commands.device_line(name, live=live)
        lines.append(line)
        if on is None:
            continue
        rows.append([Btn(f"{dev.icon} Turn {'off' if on else 'on'} {dev.display}",
                         callback_data=f"t:{name}:{'off' if on else 'on'}")])
        if dev.auto_off_minutes:  # timed devices get a quick timer / extend button
            extra.append(Btn(f"⏱ +15 min {dev.display}", callback_data=f"ext:{name}") if on
                         else Btn(f"⏱ {dev.display} 1 hr", callback_data=f"hour:{name}"))
    lines += ["", f"_updated {commands.fmt_t(now)}_"]
    if st.get("undo"):
        rows.append([Btn("↩️ Undo", callback_data="undo")])
    rows += [extra[i:i + 2] for i in range(0, len(extra), 2)]
    if banner:
        rows.append([Btn("🏠 I'm back — resume routines", callback_data="back")])
    rows.append([Btn("📅", callback_data="view:schedules"), Btn("⚡", callback_data="view:energy"),
                 Btn("⚙️", callback_data="view:devices"), Btn("🔄", callback_data="refresh")])
    return "\n".join(lines), InlineKeyboardMarkup(rows)


def render_schedules(st: dict) -> tuple[str, InlineKeyboardMarkup]:
    groups = commands.schedule_groups()
    lines = ["📅 *Next 7 days*", ""] + commands.week_days()
    if banner := commands.away_banner():
        lines += ["", banner]
    if groups:
        lines += ["", "*Routines & timers* — tap ❌ to cancel"] + [g["text"] for g in groups]
    else:
        lines += ["", "Nothing scheduled. Type e.g. “geyser daily 6–6:30am”."]
    rows = [[Btn(f"❌ {g['text']}"[:64], callback_data=f"xg:{g['group']}")] for g in groups]
    if st.get("undo"):
        rows.append([Btn("↩️ Undo last change", callback_data="undo")])
    rows.append([Btn("← Back", callback_data="view:home")])
    return "\n".join(lines), InlineKeyboardMarkup(rows)


ICONS = ["🔌", "💡", "🔥", "🌬", "🌀", "❄️", "📺", "☕", "🛁", "🪴"]
AUTO_OFF = [(None, "No auto-off"), (30, "30 min"), (60, "1 hr"), (120, "2 hr")]


def _ao_text(m) -> str:
    return "no auto-off" if not m else f"auto-off {m:g} min"


def render_devices(st: dict) -> tuple[str, InlineKeyboardMarkup]:
    regs = devices.registry()
    lines = ["⚙️ *Devices*", ""] + [f"{d.icon} {d.display} · {_ao_text(d.auto_off_minutes)}" for d in regs.values()]
    if st.get("last"):
        lines += ["", st["last"]]
    rows = [[Btn(f"✏️ {d.icon} {d.display}", callback_data=f"dev:{n}")] for n, d in regs.items()]
    rows.append([Btn("➕ Add a device", callback_data="find")])
    rows.append([Btn("← Back", callback_data="view:home")])
    return "\n".join(lines), InlineKeyboardMarkup(rows)


def render_device(st: dict, name: str) -> tuple[str, InlineKeyboardMarkup]:
    d = devices.get(name)
    text = f"{d.icon} *{d.display}*\n{_ao_text(d.auto_off_minutes)}"
    if st.get("last"):
        text += f"\n\n{st['last']}"
    ao = [Btn(("✓ " if (d.auto_off_minutes or None) == m else "") + label, callback_data=f"setao:{name}:{m or 0}")
          for m, label in AUTO_OFF]
    rows = [[Btn("✏️ Rename", callback_data=f"ren:{name}"), Btn("🎨 Icon", callback_data=f"seticon:{name}")],
            ao[:2], ao[2:],
            [Btn("🗑 Remove", callback_data=f"rm?:{name}")],
            [Btn("← Devices", callback_data="view:devices")]]
    return text, InlineKeyboardMarkup(rows)


def render_flow(st: dict) -> tuple[str, InlineKeyboardMarkup]:
    """Multi-step prompts (adding / renaming a device) shown inside the dashboard."""
    f = st["flow"]
    step = f["step"]
    cancel = [Btn("✖️ Cancel", callback_data="flow:cancel")]
    if step == "found":
        if not f["found"]:
            return ("➕ *Add a device*\n\nNo new devices found.\n\n1. Pair the plug in the *Smart Life* app (same account as the others)\n"
                    "2. Tap *Search again*"), InlineKeyboardMarkup([[Btn("🔍 Search again", callback_data="find")], cancel])
        rows = [[Btn(f"➕ {d['app_name']} ({d['kind']})"[:64], callback_data=f"pick:{i}")] for i, d in enumerate(f["found"])]
        return "➕ *Add a device*\n\nFound in your Smart Life account — tap one:", InlineKeyboardMarkup(rows + [cancel])
    if step == "name":
        d = f["device"]
        return (f"➕ *{d['app_name']}*\n{d['product']}\n\nWhat should I call it? Type a name (e.g. “Lamp”).",
                InlineKeyboardMarkup([[Btn(f"Use “{d['app_name']}”"[:64], callback_data="flow:usename")], cancel]))
    if step in ("icon", "reicon"):
        rows = [[Btn(i, callback_data=f"icon:{i}") for i in ICONS[:5]], [Btn(i, callback_data=f"icon:{i}") for i in ICONS[5:]]]
        who = f.get("label") or devices.get(f["name"]).display
        return f"Pick an icon for *{who}*:", InlineKeyboardMarkup(rows + [cancel])
    if step == "autooff":
        rows = [[Btn(label, callback_data=f"ao:{m or 0}") for m, label in AUTO_OFF[:2]],
                [Btn(label, callback_data=f"ao:{m or 0}") for m, label in AUTO_OFF[2:]]]
        return (f"{f['icon']} *{f['label']}* — turn it off automatically after a while when switched on without an end time?",
                InlineKeyboardMarkup(rows + [cancel]))
    if step == "rename":
        return f"Type a new name for *{devices.get(f['name']).display}*:", InlineKeyboardMarkup([cancel])
    if step == "remove":
        d = devices.get(f["name"])
        return (f"Remove {d.icon} *{d.display}*? Its schedules are cancelled too. (The plug itself stays in Smart Life.)",
                InlineKeyboardMarkup([[Btn("🗑 Yes, remove", callback_data=f"rm!:{f['name']}")], cancel]))
    return "…", InlineKeyboardMarkup([cancel])


def render_energy(st: dict) -> tuple[str, InlineKeyboardMarkup]:
    lines = ["⚡ *Energy*", ""] + commands.energy_report()
    return "\n".join(lines).rstrip(), InlineKeyboardMarkup([[Btn("📋", callback_data="export"),
                                                              Btn("🔄", callback_data="view:energy"),
                                                              Btn("← Back", callback_data="view:home")]])


def render(st: dict) -> tuple[str, InlineKeyboardMarkup]:
    if st.get("flow"):
        return render_flow(st)
    v = st.get("view") or "home"
    if v == "schedules":
        return render_schedules(st)
    if v == "devices":
        return render_devices(st)
    if v == "energy":
        return render_energy(st)
    if v.startswith("dev:") and v[4:] in devices.registry():
        return render_device(st, v[4:])
    return render_home(st)


# ---------- dashboard I/O ----------

async def _call(fn, text: str, **kw):
    """Send/edit with Markdown; fall back to plain text if the content has stray markdown characters."""
    try:
        return await fn(text=text, parse_mode=ParseMode.MARKDOWN, **kw)
    except BadRequest as e:
        if "not modified" in str(e).lower():
            return None
        if "parse entities" in str(e).lower():
            return await fn(text=text.replace("*", ""), **kw)
        raise


_locks: dict[int, asyncio.Lock] = {}


async def _retire(bot, chat_id: int, mid: int):
    """Remove an old dashboard; if Telegram refuses (older than 48h), blank it so it can't be mistaken for live."""
    try:
        await bot.delete_message(chat_id, mid)
    except Exception:
        try:
            await bot.edit_message_text("·", chat_id=chat_id, message_id=mid, reply_markup=None)
        except Exception:
            pass


async def show(bot, chat_id: int, repost: bool = False, notify_user: bool = False):
    """Edit the dashboard in place, or (re)post it at the bottom of the chat (with a phone notification if asked).
    Serialised per chat so simultaneous updates can't leave two dashboards behind."""
    lock = _locks.setdefault(chat_id, asyncio.Lock())
    async with lock:
        st = STATE.get(chat_id)
        text, markup = await asyncio.to_thread(render, st)
        if st["msg_id"] and not repost:
            try:
                await _call(bot.edit_message_text, text, chat_id=chat_id, message_id=st["msg_id"], reply_markup=markup)
                return
            except Exception:
                log.info("dashboard edit failed; re-posting", exc_info=True)
        old = [m for m in [st.get("msg_id"), *st.get("old_ids", [])] if m]
        msg = await _call(bot.send_message, text, chat_id=chat_id, reply_markup=markup,
                          disable_notification=not notify_user)
        st["msg_id"] = msg.message_id
        st["old_ids"] = []
        STATE.save()
        for mid in old:
            if mid != msg.message_id:
                await _retire(bot, chat_id, mid)
        if notify_user:
            log.info("notified chat %s: %s", chat_id, st.get("last", "")[:80])


async def on_event(text: str):
    """Scheduler/poller happenings → silent dashboard update for every allowed chat."""
    for cid in settings().allowed_chat_ids:
        st = STATE.get(cid)
        st.update(last=text, undo=None, view="home", flow=None)
        STATE.save()
        try:
            await show(notify._bot, cid, repost=settings().notify_state_changes,
                       notify_user=settings().notify_state_changes)
        except Exception:
            log.exception("dashboard refresh failed")


def _set_last(chat_id: int, r: commands.Reply | None, text: str | None = None, view: str = "home"):
    st = STATE.get(chat_id)
    st["view"] = view
    st["last"] = text if text is not None else (r.text if r else "")
    st["undo"] = ({"groups": r.groups, "switched": r.switched, "away": r.away, "snapshot": r.snapshot}
                  if r and (r.snapshot is not None or r.switched) else None)
    STATE.save()


# ---------- handlers ----------

def _allowed(update: Update) -> bool:
    return update.effective_chat.id in settings().allowed_chat_ids


async def _deny(update: Update):
    await update.effective_message.reply_text(
        f"Not authorised. Your chat ID is {update.effective_chat.id} — add it to TELEGRAM_ALLOWED_CHAT_IDS and restart."
    )


async def _delete_incoming(update: Update):
    try:
        await update.effective_message.delete()
    except Exception:
        pass


def read_logs(max_lines: int = 3000) -> list[str]:
    """Newest last; includes rotated files so restarts don't lose history."""
    base = settings().data_path / "zebplug.log"
    lines: list[str] = []
    for f in [base.with_name(f"zebplug.log.{i}") for i in (3, 2, 1)] + [base]:
        if f.exists():
            lines += f.read_text(encoding="utf-8", errors="replace").splitlines()
    return lines[-max_lines:]


async def send_logs(bot, chat_id: int):
    lines = await asyncio.to_thread(read_logs)
    if not lines:
        return await bot.send_message(chat_id, "No logs yet.")
    stamp = datetime.now(nlp.TZ).strftime("%Y%m%d-%H%M")
    doc = await bot.send_document(chat_id, document=("\n".join(lines) + "\n").encode(),
                                  filename=f"zebplug-logs-{stamp}.txt", disable_notification=True,
                                  caption=f"Full log · last {len(lines)} lines")
    recent, size = [], 0
    for line in reversed(lines):  # as many recent lines as fit in one message
        short = line[11:]  # drop the date, keep time
        if size + len(short) > 3500:
            break
        recent.insert(0, short)
        size += len(short) + 1
    await bot.send_message(chat_id, f"<pre>{html.escape(chr(10).join(recent))}</pre>", parse_mode=ParseMode.HTML,
                           disable_notification=True,
                           reply_markup=InlineKeyboardMarkup([[Btn("🗑 Delete logs", callback_data=f"dl:{doc.message_id}")]]))


async def send_export(bot, chat_id: int):
    data = await asyncio.to_thread(commands.export_history)
    stamp = datetime.now(nlp.TZ).strftime("%Y%m%d-%H%M")
    body = json.dumps(data, indent=1, ensure_ascii=False).encode()
    caption = ("📋 Full power & usage history, every device. Open → Share to your AI app, or save.\n"
               + " · ".join(f"{d['label']}: {d['totals']['runs']} runs, {d['totals']['energy_kwh']} kWh"
                            for d in data["devices"]))
    await bot.send_document(chat_id, document=body, filename=f"zebplug-history-{stamp}.json", caption=caption[:1000],
                            disable_notification=True,
                            reply_markup=InlineKeyboardMarkup([[Btn("🗑 Delete", callback_data="dl:0")]]))


async def on_command(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not _allowed(update):
        return await _deny(update)
    cmd = update.effective_message.text.lstrip("/").split("@")[0].split()[0]
    cid = update.effective_chat.id
    if cmd == "export":
        await _delete_incoming(update)
        return await send_export(ctx.bot, cid)
    if cmd == "logs":
        await _delete_incoming(update)
        return await send_logs(ctx.bot, cid)
    if cmd == "clean":
        await purge_chat(ctx.bot, cid)
        STATE.get(cid)["msg_id"] = None
        _set_last(cid, None, text="🧹 Chat cleaned")
        return await show(ctx.bot, cid, repost=True)
    if cmd == "schedules":
        _set_last(cid, None, view="schedules")
    elif cmd in ("help", "start"):
        _set_last(cid, None, text=commands.HELP)
    else:
        _set_last(cid, None)
    await _delete_incoming(update)
    await show(ctx.bot, cid, repost=True)


async def on_text(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not _allowed(update):
        return await _deny(update)
    cid = update.effective_chat.id
    st = STATE.get(cid)
    if (f := st.get("flow")) and f["step"] in ("name", "rename"):
        label = update.effective_message.text.strip()[:30]
        if f["step"] == "name":
            f.update(label=label, step="icon")
        else:
            await asyncio.to_thread(devices.update_device, f["name"], label=label)
            st.update(flow=None, last=f"✏️ Renamed to {label}", view=f"dev:{f['name']}")
        STATE.save()
        await _delete_incoming(update)
        return await show(ctx.bot, cid, repost=True)
    st["flow"] = None  # typing anything else abandons a half-done flow
    await ctx.bot.send_chat_action(cid, "typing")
    r = await asyncio.to_thread(commands.respond, update.effective_message.text)
    if "schedules" in r.views:
        _set_last(cid, None, view="schedules")
    elif "energy" in r.views:
        _set_last(cid, None, view="energy")
    elif "status" in r.views:
        _set_last(cid, None)  # the dashboard *is* the status
    else:
        _set_last(cid, r)
    await _delete_incoming(update)
    await show(ctx.bot, cid, repost=True)


async def on_voice(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not _allowed(update):
        return await _deny(update)
    cid = update.effective_chat.id
    msg = update.effective_message
    media = msg.voice or msg.audio
    STATE.get(cid)["flow"] = None
    if not llm.enabled():
        _set_last(cid, None, text="🎙 Voice notes need GEMINI_API_KEY — type the command instead.")
    else:
        await ctx.bot.send_chat_action(cid, "typing")
        try:
            audio = bytes(await (await media.get_file()).download_as_bytearray())
            heard = await asyncio.to_thread(llm.transcribe, audio, media.mime_type or "audio/ogg")
        except Exception as e:
            log.exception("transcription failed")
            heard = ""
            _set_last(cid, None, text=f"🎙 Couldn't understand the voice note ({type(e).__name__}) — try again or type it.")
        if heard:
            r = await asyncio.to_thread(commands.respond, heard)
            if "schedules" in r.views or "energy" in r.views or "status" in r.views:
                view = "schedules" if "schedules" in r.views else "energy" if "energy" in r.views else "home"
                _set_last(cid, None, text=f"🎙 “{heard}”", view=view)
            else:
                _set_last(cid, r)
                st = STATE.get(cid)
                st["last"] = f"🎙 “{heard}”\n{st['last']}"
                STATE.save()
    await _delete_incoming(update)
    await show(ctx.bot, cid, repost=True)


async def on_button(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    if not _allowed(update):
        return await q.answer("Not authorised")
    cid = update.effective_chat.id
    st = STATE.get(cid)
    kind, _, arg = q.data.partition(":")
    if kind == "export":
        await q.answer("Preparing…")
        return await send_export(ctx.bot, cid)
    if kind == "dl":  # delete the log file + this message; dashboard untouched
        for mid in {int(arg), q.message.message_id} - {0}:
            try:
                await ctx.bot.delete_message(cid, mid)
            except Exception:
                pass
        return await q.answer("Deleted")
    if q.message and st["msg_id"] != q.message.message_id:  # an older dashboard was tapped: make it live, retire the other
        if st["msg_id"]:
            await _retire(ctx.bot, cid, st["msg_id"])
        st["msg_id"] = q.message.message_id

    if kind == "t":
        name, target = arg.rsplit(":", 1)
        r = await asyncio.to_thread(commands.respond, f"{name} {target}", False)
        _set_last(cid, r)
        await q.answer()
    elif kind == "hour":
        r = await asyncio.to_thread(commands.respond, f"{arg} on for 1 hour", False)
        _set_last(cid, r)
        await q.answer()
    elif kind == "ext":
        snap = await asyncio.to_thread(scheduler.snapshot)
        when = await asyncio.to_thread(commands.extend, arg, 15)
        cap = commands.cap_time(arg)
        note = " (2 h max reached)" if cap and when >= cap else ""
        _set_last(cid, None, text=f"⏱ {devices.get(arg).display} now turns off at {commands.fmt_t(when)}{note}")
        st["undo"] = {"groups": [], "switched": [], "away": False, "snapshot": snap}
        STATE.save()
        await q.answer()
    elif kind == "undo":
        u = st.get("undo")
        if u:
            msg = await asyncio.to_thread(commands.undo, u["groups"], [tuple(x) for x in u["switched"]],
                                          u.get("away", False), u.get("snapshot"))
            _set_last(cid, None, text=msg)
        await q.answer("Undone" if u else "Nothing to undo")
    elif kind == "find":
        await q.answer("Searching…")
        try:
            found = await asyncio.to_thread(devices.discover_new)
            st["flow"] = {"step": "found", "found": found}
        except Exception as e:
            st.update(flow=None, view="devices", last=f"⚠️ Couldn't search Tuya: {e}")
        STATE.save()
    elif kind == "pick":
        f = st.get("flow") or {}
        if f.get("step") == "found" and int(arg) < len(f["found"]):
            st["flow"] = {"step": "name", "device": f["found"][int(arg)]}
            STATE.save()
        await q.answer()
    elif kind == "flow" and arg == "usename":
        f = st["flow"]
        f.update(label=f["device"]["app_name"], step="icon")
        STATE.save()
        await q.answer()
    elif kind == "flow" and arg == "cancel":
        f = st.get("flow") or {}
        st.update(flow=None, last="", view=f"dev:{f['name']}" if f.get("name") else "devices")
        STATE.save()
        await q.answer()
    elif kind == "icon":
        f = st.get("flow") or {}
        if f.get("step") == "icon":
            f.update(icon=arg, step="autooff")
        elif f.get("step") == "reicon":
            await asyncio.to_thread(devices.update_device, f["name"], icon=arg)
            st.update(flow=None, last="🎨 Icon updated", view=f"dev:{f['name']}")
        STATE.save()
        await q.answer()
    elif kind == "ao":
        f = st.get("flow") or {}
        if f.get("step") == "autooff":
            d = f["device"]
            dev = await asyncio.to_thread(devices.add_device, d["id"], d["app_name"], d["switch_code"], f["label"],
                                          f["icon"], float(arg) or None)
            st.update(flow=None, view="home", last=f"✅ Added {dev.icon} {dev.display} — try “{dev.display.lower()} on”")
            STATE.save()
        await q.answer()
    elif kind == "dev":
        st.update(view=f"dev:{arg}", last="")
        STATE.save()
        await q.answer()
    elif kind == "ren":
        st["flow"] = {"step": "rename", "name": arg}
        STATE.save()
        await q.answer()
    elif kind == "seticon":
        st["flow"] = {"step": "reicon", "name": arg}
        STATE.save()
        await q.answer()
    elif kind == "setao":
        name, m = arg.rsplit(":", 1)
        await asyncio.to_thread(devices.update_device, name, auto_off_minutes=float(m) or None)
        st["last"] = "⏱ Saved"
        STATE.save()
        await q.answer("Saved")
    elif kind == "rm?":
        st["flow"] = {"step": "remove", "name": arg}
        STATE.save()
        await q.answer()
    elif kind == "rm!":
        disp = devices.get(arg).display
        await asyncio.to_thread(commands.remove_device, arg)
        st.update(flow=None, view="devices", last=f"🗑 Removed {disp}")
        STATE.save()
        await q.answer()
    elif kind == "refresh":  # the only button that reads the plugs live (API calls)
        st.update(view="home", flow=None)
        STATE.save()
        await asyncio.to_thread(lambda: [commands.device_line(n, live=True) for n in devices.registry()])
        await q.answer("Refreshed")
    elif kind == "back":
        r = await asyncio.to_thread(commands.respond, "I'm back", False)
        _set_last(cid, None, text=r.text)
        await q.answer()
    elif kind == "xg":
        snap = await asyncio.to_thread(scheduler.snapshot)
        n = await asyncio.to_thread(commands.cancel_group, arg)
        st["view"] = "schedules"
        if n:
            st["last"] = "🗑 Schedule cancelled"
            st["undo"] = {"groups": [], "switched": [], "away": False, "snapshot": snap}
        STATE.save()
        await q.answer("Cancelled" if n else "Already gone")
    elif kind == "view":
        st.update(view=arg, flow=None)
        if arg != "home":
            st["last"] = st["last"] if arg == "schedules" else ""
        STATE.save()
        await q.answer()
    else:
        await q.answer()
    await show(ctx.bot, cid)


async def purge_chat(bot, chat_id: int) -> int:
    """Delete everything Telegram still lets a bot delete (both sides, last 48h) and drop the old keyboard."""
    probe = await bot.send_message(chat_id, "🧹", reply_markup=ReplyKeyboardRemove(), disable_notification=True)
    ids = list(range(max(1, probe.message_id - 5000), probe.message_id + 1))
    deleted = 0
    for i in range(0, len(ids), 100):
        try:
            if await bot.delete_messages(chat_id, ids[i:i + 100]):
                deleted += len(ids[i:i + 100])
        except Exception:
            pass  # batches with nothing deletable raise; that's fine
    log.info("purged chat %s up to message %s", chat_id, probe.message_id)
    return deleted


# ---------- lifecycle ----------

async def _post_init(app: Application):
    global STATE
    STATE = State()
    devices.registry()  # fail fast on bad config
    scheduler.start()
    notify.bind(app.bot, asyncio.get_running_loop(), settings().allowed_chat_ids, on_event=on_event)
    from . import web

    async def after_web_action(text: str):  # Watch/Shortcut actions show on the dashboard (silently)
        for cid in settings().allowed_chat_ids:
            st = STATE.get(cid)
            if text:
                st.update(last=text, undo=None, view="home", flow=None)
                STATE.save()
            await show(app.bot, cid)

    app.bot_data["web"] = await web.start(after_web_action)
    if settings().tuya_push and (creds := devices.tuya_creds()):
        from .realtime import TuyaPush
        push = TuyaPush(*creds, on_message=scheduler.on_push)
        push.start()
        scheduler.push_connected = lambda: push.connected
    await app.bot.set_my_commands([
        BotCommand("home", "Show the dashboard"),
        BotCommand("schedules", "Upcoming schedules"),
        BotCommand("help", "What I understand"),
        BotCommand("logs", "Activity log (copyable + file)"),
        BotCommand("export", "All power/usage history as JSON"),
        BotCommand("clean", "Wipe the chat, keep just the dashboard"),
    ])
    marker = settings().data_path / "layout_version"
    if not marker.exists() or marker.read_text() != LAYOUT_VERSION:
        for cid in settings().allowed_chat_ids:
            try:
                await purge_chat(app.bot, cid)
                STATE.get(cid).update(msg_id=None, last="✨ Cleaned up — one dashboard again.",
                                      undo=None, view="home")
                await show(app.bot, cid, repost=True)
            except Exception:
                log.exception("layout migration failed for %s", cid)
        marker.write_text(LAYOUT_VERSION)
    log.info("bot running")


async def _post_shutdown(app: Application):
    scheduler.scheduler().shutdown(wait=False)


def build() -> Application:
    token = settings().telegram_bot_token
    if not token:
        sys.exit("TELEGRAM_BOT_TOKEN is not set (create a bot with @BotFather)")
    app = Application.builder().token(token).post_init(_post_init).post_shutdown(_post_shutdown).build()
    app.add_handler(CommandHandler(["start", "help", "home", "menu", "status", "schedules", "clean", "logs", "export"], on_command))
    app.add_handler(CallbackQueryHandler(on_button))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_text))
    app.add_handler(MessageHandler(filters.VOICE | filters.AUDIO, on_voice))
    return app


def run() -> None:
    build().run_polling(drop_pending_updates=True)
