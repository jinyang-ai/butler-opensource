"""Turn parsed commands into actions + plain-language replies (rendered by the Telegram bot)."""

import logging
import threading
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from pydantic import ValidationError

from . import devices, llm, nlp, scheduler, store
from .sun import describe as sun_describe, next_sun

log = logging.getLogger(__name__)

INVERSE = {"on": "off", "off": "on", "toggle": "toggle"}
_tl = threading.local()  # groups created while handling the current message
_recent: dict = {"devices": [], "ts": 0.0}  # last devices acted on → "turn it off" / "that was a mistake"
RECENT_SECONDS = 15 * 60


def recent_devices() -> list[str]:
    import time as _time
    if _time.time() - _recent["ts"] < RECENT_SECONDS:
        return [d for d in _recent["devices"] if d in devices.registry()]
    return []

HELP = """Just tell me what you want, for example:
• geyser on
• geyser on 6–7am tomorrow
• geyser daily at 6am
• purifier off at 11pm
• purifier off for 30 min
• what's on?
• I'm away till 25th · I'm back
• 🎙 or just send a voice note

The geyser turns itself off after 30 min unless you give an end time, and never runs more than 2 hours."""


@dataclass
class Reply:
    text: str
    groups: list[str] = field(default_factory=list)  # schedule groups created (undo = cancel them)
    views: set[str] = field(default_factory=set)  # "status" / "schedules" → bot shows that view
    switched: list[tuple[str, bool]] = field(default_factory=list)  # (device, new state) for immediate switches
    away: bool = False  # away mode was switched on (undo = end it)
    snapshot: dict | None = None  # schedule state before this command, if it changed anything


# ---------- formatting ----------

def fmt_t(dt: datetime) -> str:
    s = dt.strftime("%-I:%M %p").lower().replace(":00 ", " ")
    return "noon" if s == "12 pm" else s


def fmt_day(dt: datetime) -> str:
    today = datetime.now(dt.tzinfo).date()
    if dt.date() == today:
        return "today"
    if dt.date() == today + timedelta(days=1):
        return "tomorrow"
    return dt.strftime("%a %-d %b")


def fmt_when(dt: datetime) -> str:
    return f"{fmt_day(dt)} {fmt_t(dt)}"


def fmt_range(start: datetime, end: datetime) -> str:
    a, b = fmt_t(start), fmt_t(end)
    if a[-2:] == b[-2:] and a[-2:] in ("am", "pm"):  # "6–6:30 am" instead of "6 am–6:30 am"
        a = a[:-3]
    return f"{a}–{b}"


def fmt_span(start: datetime, end: datetime) -> str:
    if start.date() == end.date():
        return f"{fmt_day(start)} {fmt_range(start, end)}"
    return f"{fmt_when(start)} → {fmt_when(end)}"


DAY_NAMES = dict(zip(nlp.DAYS, ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]))


def fmt_days(dow: str) -> str:
    return {"*": "every day", "mon-fri": "weekdays", "sat,sun": "weekends"}.get(
        dow, "every " + ", ".join(DAY_NAMES.get(d, d) for d in dow.split(",")))


def _hm(h: int, m: int) -> datetime:
    return datetime.now(nlp.TZ).replace(hour=h, minute=m, second=0, microsecond=0)


def _jobs_for(name: str) -> list[dict]:
    return sorted((j for j in scheduler.list_jobs() if j["device"] == name), key=lambda j: j["next_run"] or "")


def _next(j: dict) -> datetime | None:
    return datetime.fromisoformat(j["next_run"]) if j["next_run"] else None


def device_card(name: str) -> tuple[str, bool | None]:
    """Card text for one device and whether it's on (None if unreachable)."""
    dev = devices.get(name)
    head = f"{dev.icon} *{dev.display}*"
    try:
        st = dev.state()
    except devices.PlugError as e:
        return f"{head} — ⚠️ can't reach it\n{e}", None
    jobs = _jobs_for(dev.name)
    lines = [f"{head} — {'ON' if st.on else 'off'}"]
    if st.on:
        bits = []
        if st.power_w is not None:
            bits.append(f"{st.power_w:g} W")
        if since := scheduler.on_since(dev.name):
            bits.append(f"on since {fmt_t(datetime.fromtimestamp(since, nlp.TZ))}")
        if bits:
            lines.append(" · ".join(bits))
        off = next((j for j in jobs if j["action"] in ("off", "toggle")), None)
        if off and _next(off):
            t = _next(off)
            lines.append(f"Turns off at {fmt_t(t)}" + ("" if fmt_day(t) == "today" else f" {fmt_day(t)}"))
        elif dev.name in scheduler._hold:
            lines.append("Stays on until you turn it off")
        elif dev.auto_off_minutes:
            lines.append(f"Auto-off after {dev.auto_off_minutes:g} min")
    else:
        on = next((j for j in jobs if j["action"] in ("on", "toggle")), None)
        if on and _next(on):
            lines.append(f"Next: on {fmt_when(_next(on))}" + (" (repeats)" if on["recurring"] else ""))
    return "\n".join(lines), st.on


def status_text() -> str:
    out = []
    for dev in devices.registry().values():
        try:
            st = dev.state()
            line = f"{dev.icon} {dev.display} — {'ON' if st.on else 'off'}"
            if st.on and st.power_w is not None:
                line += f" · {st.power_w:g} W"
        except devices.PlugError:
            line = f"{dev.icon} {dev.display} — ⚠️ unreachable"
        out.append(line)
    return "\n".join(out)


def schedule_groups() -> list[dict]:
    """Upcoming schedules, one entry per on→off pair."""
    groups: dict[str, list[dict]] = {}
    for j in scheduler.list_jobs():
        groups.setdefault(j["group"], []).append(j)
    out = []
    for gid, jobs in groups.items():
        jobs.sort(key=lambda j: j["next_run"] or "")
        first = jobs[0]
        dev = devices.get(first["device"])
        when = fmt_when(_next(first)) if _next(first) else "paused"
        if first["recurring"] or len(jobs) > 1:
            what = first["label"] or f"{first['action']} · {when}"
        else:
            what = f"{first['action']} · {when}" + (f" ({first['label']})" if first["label"] in ("auto-off", "timer") else "")
        out.append({"group": gid, "device": dev.name, "next_run": first["next_run"] or "",
                    "text": f"{dev.icon} {dev.display} · {what}"})
    return sorted(out, key=lambda g: g["next_run"])


# ---------- actions ----------

def _cron_str(h: int, m: int, dow: str) -> str:
    return f"{m} {h} * * {dow}"


def _shift_cron(h: int, m: int, dow: str, minutes: float) -> str:
    total = h * 60 + m + round(minutes)
    days, rem = divmod(total, 24 * 60)
    if days and dow != "*":
        expanded = []
        for part in dow.split(","):
            if "-" in part:
                a, b = (nlp.DAYS.index(x) for x in part.split("-"))
                expanded += nlp.DAYS[a:b + 1]
            else:
                expanded.append(part)
        dow = ",".join(nlp.DAYS[(nlp.DAYS.index(d) + days) % 7] for d in expanded)
    return _cron_str(rem // 60, rem % 60, dow)


def _new_group() -> str:
    gid = uuid.uuid4().hex[:8]
    if not hasattr(_tl, "groups"):
        _tl.groups = []
    _tl.groups.append(gid)
    return gid


def _drop_pending_offs(name: str) -> None:
    """A fresh manual on/off makes earlier one-off offs moot."""
    for j in _jobs_for(name):
        if j["action"] == "off" and not j["recurring"]:
            scheduler.cancel(j["id"])


def _cap_minutes(dev) -> float | None:
    return dev.max_on_minutes or None


def cap_time(name: str) -> datetime | None:
    """Latest allowed OFF for a device that's on now (on-since + max_on_minutes)."""
    dev = devices.get(name)
    cap = _cap_minutes(dev)
    if not cap:
        return None
    since = scheduler.on_since(name)
    start = datetime.fromtimestamp(since, nlp.TZ) if since else datetime.now(nlp.TZ)
    return start + timedelta(minutes=cap)


def fmt_mins(m: float) -> str:
    return f"{m / 60:g} h" if m >= 60 and m % 30 == 0 else f"{m:g} min"


def extend(name: str, minutes: float) -> datetime:
    """Push the device's next one-off OFF back by `minutes` (or create one)."""
    scheduler.hold(name, False)
    cap = cap_time(name)
    for j in _jobs_for(name):
        if j["action"] == "off" and not j["recurring"]:
            when = max(_next(j), datetime.now(nlp.TZ)) + timedelta(minutes=minutes)
            when = min(when, cap) if cap else when
            scheduler.reschedule(j["id"], when)
            return when
    when = datetime.now(nlp.TZ) + timedelta(minutes=minutes)
    when = min(when, cap) if cap else when
    scheduler.add(name, "off", at=when, label="auto-off", group=_new_group())
    return when


def keep_on(name: str) -> datetime | None:
    """Drop the short auto-off; devices with a max run time still stop at the cap. Returns the off time, if any."""
    _drop_pending_offs(name)
    cap = cap_time(name)
    if cap:
        scheduler.add(name, "off", at=max(cap, datetime.now(nlp.TZ) + timedelta(minutes=1)), label="max run time",
                      group=_new_group())
        return cap
    scheduler.hold(name, True)
    return None


def _power(cmd: nlp.Command) -> str:
    names = cmd.devices or recent_devices()  # "turn it off" → whatever was just used
    if not names:
        reg = devices.registry()
        if len(reg) != 1:
            return f"Which one? ({', '.join(d.display for d in reg.values())})"
        names = list(reg)
    import time as _time
    _recent.update(devices=list(names), ts=_time.time())

    lines = []
    for name in names:
        dev = devices.get(name)
        who = f"{dev.icon} {dev.display}"
        act, inv, dur = cmd.action, INVERSE[cmd.action], cmd.duration_minutes
        if act == "on" and dur is None and dev.auto_off_minutes and not cmd.no_auto_off:
            dur = dev.auto_off_minutes
        cap, capped = _cap_minutes(dev), ""
        if act == "on" and cap and (dur is None or dur > cap):
            if cmd.duration_minutes or cmd.no_auto_off:
                capped = f" (capped at {fmt_mins(cap)} max)"
            dur = cap

        if cmd.sun:
            event, off, dow = cmd.sun
            what = f"{fmt_days(dow)} {sun_describe(event, off)}"
            if dur:
                what += f" for {fmt_mins(dur)}"
            what = what if act == "on" else f"{act} · {what}"
            nxt = next_sun(event, off, datetime.now(nlp.TZ), dow)
            gid = _new_group()
            scheduler.add(name, act, sun=(event, off, dow), label=what, group=gid)
            if dur:
                scheduler.add(name, inv, sun=(event, off + dur, dow), label=what, group=gid)
            lines.append(f"🌇 {who} · {what}{capped}" + (f" — next {fmt_when(nxt)}" if nxt else ""))

        elif cmd.cron:
            h, m, dow = cmd.cron
            start = _hm(h, m)
            if dur:
                what = f"{fmt_days(dow)} {fmt_range(start, start + timedelta(minutes=dur))}"
            else:
                what = f"{fmt_days(dow)} at {fmt_t(start)}"
            what = what if act == "on" else f"{act} · {what}"
            gid = _new_group()
            scheduler.add(name, act, cron=_cron_str(h, m, dow), label=what, group=gid)
            if dur:
                scheduler.add(name, inv, cron=_shift_cron(h, m, dow, dur), label=what, group=gid)
            lines.append(f"🔁 {who} · {what}{capped}")

        elif cmd.at or cmd.in_minutes is not None:
            start = cmd.at or datetime.now(nlp.TZ) + timedelta(minutes=cmd.in_minutes)
            same = [j for j in _jobs_for(name) if not j["recurring"] and j["action"] == act and _next(j)
                    and abs((_next(j) - start).total_seconds()) < 90]
            if same:
                lines.append(f"👍 {who} {act} at {fmt_when(start)} is already scheduled")
                continue
            if act == "off" and not dur:  # newest off time replaces earlier one-off offs for this device
                _drop_pending_offs(name)
            gid = _new_group()
            if act == "on" and dur:
                what = fmt_span(start, start + timedelta(minutes=dur))
            elif dur:
                what = f"{act} · {fmt_when(start)}, back {inv} at {fmt_t(start + timedelta(minutes=dur))}"
            else:
                what = f"{act} · {fmt_when(start)}"
            scheduler.add(name, act, at=start, label=what, group=gid)
            if dur:
                scheduler.add(name, inv, at=start + timedelta(minutes=dur), label=what, group=gid)
            lines.append(f"⏰ {who} · {what}{capped}")

        else:
            if act in ("on", "off"):
                _drop_pending_offs(name)
            st = dev.apply(act)
            scheduler.expect(name, st.on)
            _tl.switched.append((name, st.on))
            if st.on:
                scheduler.mark_on(name)
            scheduler.hold(name, st.on and cmd.no_auto_off)
            line = f"✅ {who} is {'on' if st.on else 'off'}"
            if act == "toggle":  # judge by the resulting state: a toggle that turns it on gets the auto-off too
                act, inv = ("on", "off") if st.on else ("off", "on")
                dur = cmd.duration_minutes or (dev.auto_off_minutes if st.on and not cmd.no_auto_off else None)
                if st.on and cap and (dur is None or dur > cap):
                    dur = cap
            if dur:
                end = datetime.now(nlp.TZ) + timedelta(minutes=dur)
                label = ("timer" if cmd.duration_minutes else "auto-off") if inv == "off" else "back on"
                scheduler.add(name, inv, at=end, label=label, group=_new_group())
                line += f" · {'turns off' if inv == 'off' else 'back on'} at {fmt_t(end)}{capped}"
            elif st.on and cmd.no_auto_off:
                line += " · stays on until you turn it off"
            lines.append(line)
    return "\n".join(lines)


def _cancel(cmd: nlp.Command) -> str:
    jobs = scheduler.list_jobs()
    t = cmd.target
    if t == "all":
        hit = jobs
    elif cmd.devices:
        hit = [j for j in jobs if j["device"] in cmd.devices]
        if cmd.action in ("on", "off"):  # only routines that switch the device that way (whole on→off pairs)
            groups = {j["group"] for j in hit if j["action"] == cmd.action and j["label"] and
                      (" · " not in j["label"] or j["label"].startswith(cmd.action))}
            groups |= {j["group"] for j in hit if j["action"] == cmd.action and not any(
                k["group"] == j["group"] and k["action"] != cmd.action for k in hit)}
            hit = [j for j in hit if j["group"] in groups]
    else:
        hit = [j for j in jobs if j["id"].startswith(t) or j["group"].startswith(t)]
    for j in hit:
        scheduler.cancel(j["id"])
    n = len({j["group"] for j in hit})
    return f"🗑 Cancelled {n} schedule{'s' if n != 1 else ''}" if hit else "Nothing to cancel"


def remove_device(name: str) -> None:
    for j in _jobs_for(name):
        scheduler.cancel(j["id"])
    devices.remove_device(name)


def cancel_group(gid: str) -> int:
    hit = [j for j in scheduler.list_jobs() if j["group"] == gid or j["id"].startswith(gid)]
    for j in hit:
        scheduler.cancel(j["id"])
    return len(hit)


def away_banner() -> str | None:
    st = scheduler.away_state()
    if not st:
        return None
    until = datetime.fromisoformat(st["until"])
    if st.get("active"):
        return f"✈️ *Away* — routines paused, back {fmt_day(until)}"
    start = datetime.fromisoformat(st["from"])
    return f"✈️ Away from {fmt_day(start)} until {fmt_day(until)} — routines pause then"


def _away(cmd: nlp.Command) -> str:
    until, start = cmd.away_until, cmd.away_from
    n = len({j["group"] for j in scheduler.list_jobs() if j["recurring"]})
    scheduler.set_away(until, start)
    _tl.away = True
    routines = f"{n} routine{'s' if n != 1 else ''}"
    if start:
        return f"✈️ Away from {fmt_day(start)}: {routines} pause and everything switches off then. Back {fmt_day(until)}."
    return f"✈️ Away until {fmt_day(until)}: {routines} paused, everything switched off. Routines resume {fmt_day(until)}."


def week_days(days: int = 7) -> list[str]:
    """Calendar lines for the next `days` days: what runs when, grouped as on→off spans."""
    now = datetime.now(nlp.TZ)
    day0 = now.replace(hour=0, minute=0, second=0, microsecond=0)
    end = day0 + timedelta(days=days)
    away = scheduler.away_state()
    a_from = datetime.fromisoformat(away["from"]) if away else None
    a_until = datetime.fromisoformat(away["until"]) if away else None

    fires: dict[str, list[tuple[datetime, str]]] = {}
    for job in scheduler.raw_jobs():
        kw = job.kwargs
        t = job.trigger.get_next_fire_time(None, now)
        while t and t < end + timedelta(days=1):
            fires.setdefault(kw.get("group") or job.id, []).append((t, kw.get("action"), kw.get("device")))
            t = job.trigger.get_next_fire_time(t, t + timedelta(seconds=1))

    per_day: dict = {}
    for evs in fires.values():
        evs.sort()
        used = set()
        for i, (t, act, devname) in enumerate(evs):
            if i in used or t >= end or (a_from and a_from <= t < a_until):
                continue
            dev = devices.get(devname)
            if act == "on":
                j = next((k for k in range(i + 1, len(evs)) if evs[k][1] == "off" and k not in used
                          and evs[k][0] - t <= timedelta(hours=24)), None)
                if j is not None:
                    used.add(j)
                    txt = f"{dev.icon} {fmt_range(t, evs[j][0])}"
                else:
                    txt = f"{dev.icon} on {fmt_t(t)}"
            else:
                txt = f"{dev.icon} {act} {fmt_t(t)}"
            per_day.setdefault(t.date(), []).append((t, txt))

    lines = []
    for d in range(days):
        day = day0 + timedelta(days=d)
        label = "Today" if d == 0 else "Tmrw" if d == 1 else day.strftime("%a %-d")
        if a_from and a_from <= day + timedelta(hours=23, minutes=59) and day < a_until:
            lines.append(f"`{label:<6}` ✈️ away")
            continue
        items = " · ".join(txt for _, txt in sorted(per_day.get(day.date(), [])))
        lines.append(f"`{label:<6}` {items or '—'}")
    return lines


def energy_report() -> list[str]:
    """Per device: today, this week (since Monday), total, and a 7-day daily breakdown."""
    now = datetime.now(nlp.TZ)
    day0 = now.replace(hour=0, minute=0, second=0, microsecond=0)
    monday = day0 - timedelta(days=day0.weekday())
    ts = lambda d: d.timestamp()
    out = []
    for dev in devices.registry().values():
        since = store.tracking_since(dev.name)
        today = store.period(dev.name, ts(day0), ts(now) + 1)
        week = store.period(dev.name, ts(monday), ts(now) + 1)
        total = store.period(dev.name, 0, ts(now) + 1)
        days = [(day0 - timedelta(days=i)) for i in range(6, -1, -1)]
        daily = [store.period(dev.name, ts(d), ts(d + timedelta(days=1))) for d in days]
        has_kwh = total["kwh"] > 0

        def fmt(p):
            hrs = f"{p['on_min'] // 60}h {p['on_min'] % 60}m" if p["on_min"] >= 60 else f"{p['on_min']}m"
            return f"{p['kwh']:.2f} kWh · on {hrs}" if has_kwh else f"on {hrs}"

        heat = store.avg_heat_minutes(dev.name) if dev.off_when_heated else None
        lines = [f"{dev.icon} *{dev.display}*" + (f" · heats up in ~{heat:.0f} min" if heat else ""),
                 f"`Today    ` {fmt(today)}",
                 f"`This week` {fmt(week)}",
                 f"`Total    ` {fmt(total)}" + (f" (since {datetime.fromtimestamp(since, nlp.TZ):%-d %b})" if since else "")]
        vals = [d["kwh"] if has_kwh else d["on_min"] for d in daily]
        top = max(vals) or 1
        for d, v in zip(days, vals):
            label = "Today" if d == day0 else d.strftime("%a %-d")
            bar = "█" * max(1, round(v / top * 8)) if v else "·"
            val = f"{v:.2f}" if has_kwh else f"{int(v)}m"
            lines.append(f"`{label:<6}` {bar} {val if v else ''}")
        out += lines + [""]
    if not any(store.tracking_since(d.name) for d in devices.registry().values()):
        out.append("Tracking just started — numbers fill in as devices run.")
    return out


def export_history() -> dict:
    """All historic data per device, LLM-friendly: runs (on→off with kWh), daily totals and raw rows."""
    iso = lambda t: datetime.fromtimestamp(t, nlp.TZ).isoformat(timespec="seconds")
    now = datetime.now(nlp.TZ).timestamp()
    out = {
        "exported_at": iso(now),
        "timezone": str(nlp.TZ),
        "notes": [
            "energy_increments are kWh reported by metering plugs (Tuya add_ele, 0.001 kWh steps); they arrive "
            "every ~10 min while drawing power and once more shortly after switch-off.",
            "run.energy_kwh sums increments from switch-on until 15 min after switch-off (late reports included).",
            "readings are occasional polled snapshots; power_w/voltage_v/current_a are null on plugs without a meter.",
            "heat_events: minutes the geyser took until its thermostat cut out (0 = water was already hot).",
            "History starts when the bot began recording; earlier usage is not available (Tuya statistics API is paid).",
        ],
        "devices": [],
    }
    for name, dev in devices.registry().items():
        raw = store.dump(name)
        runs, start = [], None
        for ts, on in raw["states"]:
            if on and start is None:
                start = ts
            elif not on and start is not None:
                runs.append((start, ts))
                start = None
        if start is not None:
            runs.append((start, None))
        run_rows = []
        for a, b in runs:
            end = b or now
            kwh = sum(k for t, k in raw["energy"] if a <= t <= end + 15 * 60)
            heat = next((m for t, m in raw["heat"] if a <= t <= end + 60), None)
            run_rows.append({"start": iso(a), "end": iso(b) if b else None, "duration_min": round((end - a) / 60, 1),
                             "energy_kwh": round(kwh, 3) if raw["energy"] else None,
                             "heat_up_min": round(heat, 1) if heat is not None else None, "ongoing": b is None})
        daily: dict[str, dict] = {}
        for t, k in raw["energy"]:
            d = daily.setdefault(datetime.fromtimestamp(t, nlp.TZ).date().isoformat(), {"kwh": 0.0, "on_min": 0.0})
            d["kwh"] += k
        for r in run_rows:  # on-minutes per day (split at midnight)
            a = datetime.fromisoformat(r["start"])
            b = datetime.fromisoformat(r["end"]) if r["end"] else datetime.fromtimestamp(now, nlp.TZ)
            while a < b:
                midnight = (a + timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)
                seg_end = min(b, midnight)
                d = daily.setdefault(a.date().isoformat(), {"kwh": 0.0, "on_min": 0.0})
                d["on_min"] += (seg_end - a).total_seconds() / 60
                a = seg_end
        out["devices"].append({
            "name": name, "label": dev.display, "device_id": getattr(dev, "device_id", None),
            "metered": bool(raw["energy"]) or any(r[2] for r in raw["readings"]),
            "rules": {"auto_off_minutes": dev.auto_off_minutes, "max_on_minutes": dev.max_on_minutes,
                      "off_when_heated": dev.off_when_heated},
            "routines": [g["text"] for g in schedule_groups() if g["device"] == name],
            "totals": {"runs": len(run_rows), "on_minutes": round(sum(r["duration_min"] for r in run_rows), 1),
                       "energy_kwh": round(sum(k for _, k in raw["energy"]), 3)},
            "daily": [{"date": d, "kwh": round(v["kwh"], 3), "on_min": round(v["on_min"], 1)}
                      for d, v in sorted(daily.items())],
            "runs": run_rows,
            "heat_events": [{"at": iso(t), "minutes": round(m, 1)} for t, m in raw["heat"]],
            "state_changes": [{"at": iso(t), "on": bool(o)} for t, o in raw["states"]],
            "energy_increments": [{"at": iso(t), "kwh": k} for t, k in raw["energy"]],
            "readings": [{"at": iso(t), "on": bool(o), "power_w": p, "voltage_v": v, "current_a": c}
                         for t, o, p, v, c in raw["readings"]],
        })
    return out


def run(cmd: nlp.Command) -> str:
    if cmd.error:
        return cmd.error
    if cmd.intent == "help":
        return HELP
    if cmd.intent == "away":
        return _away(cmd)
    if cmd.intent == "back":
        if not scheduler.away_state():
            return "You're not in away mode."
        n = scheduler.end_away(silent=True)
        return f"🏠 Welcome back — {n} routine{'s' if n != 1 else ''} resumed"
    if cmd.intent == "status":
        if cmd.devices and len(cmd.devices) == 1:
            return device_card(cmd.devices[0])[0]
        _tl.views.add("status")
        return status_text()
    if cmd.intent == "list":
        _tl.views.add("schedules")
        return ""
    if cmd.intent == "cancel":
        return _cancel(cmd)
    if cmd.intent == "energy":
        _tl.views.add("energy")
        return ""
    if cmd.intent == "power":
        return _power(cmd)
    return HELP


def aliases() -> dict[str, str]:
    out = {}
    for dev in devices.registry().values():
        out[dev.name.lower()] = dev.name
        if dev.label:
            out[dev.label.lower()] = dev.name
        for a in dev.aliases:
            out[a.lower()] = dev.name
    return out


def parse(text: str, use_llm: bool = True) -> list[nlp.Command]:
    """Gemini first (if configured), offline parser as fallback."""
    al = aliases()
    if use_llm and llm.enabled():
        try:
            return llm.parse(text, al, recent=[devices.get(n).display for n in recent_devices()])
        except (ValidationError, llm.FormatError):
            log.warning("gemini returned a malformed command; falling back to offline parser", exc_info=True)
        except ValueError as e:  # model understood but asked for something invalid (past time, unknown device)
            return [nlp.Command("unknown", error=str(e))]
        except Exception:
            log.warning("gemini unavailable; falling back to offline parser", exc_info=True)
    return [nlp.parse(text, al)]


def respond(text: str, use_llm: bool = True) -> Reply:
    _tl.groups, _tl.views, _tl.switched, _tl.away = [], set(), [], False
    before = scheduler.snapshot()
    try:
        body = "\n".join(filter(None, (run(cmd) for cmd in parse(text, use_llm))))
    except (devices.PlugError, ValueError) as e:
        body = f"⚠️ {e}"
    changed = scheduler.snapshot() != before
    return Reply(body, list(_tl.groups), set(_tl.views), list(_tl.switched), _tl.away, before if changed else None)


def undo(groups: list[str], switched: list[tuple[str, bool]], away: bool = False, snapshot: dict | None = None) -> str:
    """Go back to how things were before the last change: restore the schedule snapshot (routines, timers,
    away mode — including anything that change cancelled) and flip back plugs it switched."""
    if snapshot is not None:
        scheduler.restore(snapshot)
    else:  # older undo records without a snapshot
        if away:
            scheduler.end_away(silent=True)
        for g in groups:
            cancel_group(g)
    for name, now_on in switched:
        dev = devices.get(name)
        dev.set(not now_on)
        scheduler.expect(name, not now_on)
        scheduler.hold(name, False)
        if not now_on:  # it was on before → give it the usual auto-off again
            scheduler.mark_on(name)
            if dev.auto_off_minutes and snapshot is None:
                scheduler.add(name, "off", at=datetime.now(nlp.TZ) + timedelta(minutes=dev.auto_off_minutes),
                              label="auto-off", group=uuid.uuid4().hex[:8])
    return "↩️ Undone"


def device_line(name: str, live: bool = False) -> tuple[str, bool | None]:
    """One dashboard line per device, and whether it's on (None = unreachable). Uses the cached state
    (kept current by pushes) unless `live`."""
    dev = devices.get(name)
    try:
        st = dev.status() if live else dev.state()
    except devices.PlugError:
        return f"{dev.icon} {dev.display} — ⚠️ unreachable", None
    jobs = _jobs_for(dev.name)
    if st.on:
        line = f"{dev.icon} {dev.display} — *ON*"
        off = next((j for j in jobs if j["action"] in ("off", "toggle")), None)
        if off and _next(off):
            t = _next(off)
            line += f" · off at {fmt_t(t)}" + ("" if fmt_day(t) == "today" else f" {fmt_day(t)}")
        elif dev.name in scheduler._hold:
            line += " · stays on"
        if st.power_w:
            line += f" · {st.power_w:g} W"
    else:
        line = f"{dev.icon} {dev.display} — off"
        on = next((j for j in jobs if j["action"] in ("on", "toggle")), None)
        if on and _next(on):
            t = _next(on)
            line += f" · next on {fmt_t(t)}" + ("" if fmt_day(t) == "today" else f" {fmt_day(t)}")
    return line, st.on


def execute(text: str, use_llm: bool = True) -> str:
    return respond(text, use_llm).text
