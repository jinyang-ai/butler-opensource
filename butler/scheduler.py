"""Persistent schedules (APScheduler + SQLite) and the background energy/safety poller."""

import json
import logging
import time
from datetime import datetime, timedelta

from apscheduler.jobstores.memory import MemoryJobStore
from apscheduler.jobstores.sqlalchemy import SQLAlchemyJobStore
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.date import DateTrigger
from tzlocal import get_localzone

from . import devices, notify, store
from .sun import SunTrigger
from .config import settings

log = logging.getLogger(__name__)
TZ = get_localzone()

_sched: BackgroundScheduler | None = None
_on_since: dict[str, float] = {}
_last_on: dict[str, bool] = {}  # state seen at the previous poll
_expected: dict[str, tuple[bool, float]] = {}  # changes the bot itself made recently (not "from the app")


def expect(device: str, on: bool) -> None:
    _expected[device] = (on, time.time())
    _last_on[device] = on
    store.record_state(device, on)
    if not on:
        stop_heat_watch(device)
_hold: set[str] = set()  # devices the user asked to keep on (skip default auto-off until next off)


def hold(device: str, keep_on: bool) -> None:
    (_hold.add if keep_on else _hold.discard)(device)


def _off_pending(device: str) -> bool:
    return any(j["device"] == device and j["action"] in ("off", "toggle") for j in list_jobs())


def scheduler() -> BackgroundScheduler:
    global _sched
    if _sched is None:
        url = f"sqlite:///{settings().data_path / 'schedules.db'}"
        _sched = BackgroundScheduler(
            jobstores={"default": SQLAlchemyJobStore(url=url), "memory": MemoryJobStore()},
            job_defaults={"misfire_grace_time": 300, "coalesce": True},
            timezone=TZ,
        )
    return _sched


# Job targets must be module-level so the SQLite jobstore can pickle a reference.
def run_action(device: str, action: str, label: str = "", group: str = "") -> None:
    try:
        dev = devices.get(device)
        if action == "off" and _last_on.get(dev.name) is False:
            log.info("%s already off (%s) — nothing to do", dev.name, label or "schedule")
            return
        st = dev.apply(action)
        expect(dev.name, st.on)
        if st.on:
            mark_on(dev.name)
        msg = f"⏰ {dev.display} turned {'on' if st.on else 'off'}" + (f" · {label}" if label else "")
        log.info(msg)
        notify.event(msg)
    except Exception as e:
        msg = f"⚠️ Couldn't turn {device} {action} ({label or 'schedule'}): {e}"
        log.exception(msg)
        notify.send(msg)


_last_tuya_alert = 0.0
TUYA_SUBSCRIPTION_HINTS = ("not subscribed", "expired", "trial", "service is suspended", "28841")


def _maybe_alert_tuya(err: str) -> None:
    """Tell the user (at most every 12h) when Tuya's IoT Core trial lapses — the bot can't renew it itself."""
    global _last_tuya_alert
    if not any(h in err.lower() for h in TUYA_SUBSCRIPTION_HINTS) or time.time() - _last_tuya_alert < 12 * 3600:
        return
    _last_tuya_alert = time.time()
    notify.send("⚠️ Tuya's free IoT Core trial seems to have lapsed — I can't control the plugs until it's renewed.\n"
                "Renew (free): iot.tuya.com → Cloud → Cloud Services → IoT Core → Extend Trial Period.\n"
                f"Error: {err[:200]}")


def observed(name: str, on: bool, watts: float | None = None) -> None:
    """A state seen from outside (poll or real-time push). Changes the bot didn't make → refresh the dashboard;
    an app/button ON on a timed device gets its auto-off scheduled right away."""
    prev = _last_on.get(name)
    _last_on[name] = on
    since = store.last_on_since(name) if prev is None else None  # read before logging, to survive restarts
    store.record_state(name, on)
    dev = devices.get(name)
    if prev is None:  # first look after a (re)start: a timed device found ON with no off planned gets one now
        if on and since:
            _on_since.setdefault(name, since)
        if on and dev.off_when_heated and name not in _heat:  # resume the water's-hot watch
            start_heat_watch(name)
            if since:
                _heat[name]["start"] = since  # heat-up time counts from the real switch-on, not the restart
            if watts is not None and watts >= HEAT_HIGH_W:
                _heat[name]["seen_high"] = True
        limit = dev.auto_off_minutes or dev.max_on_minutes
        if on and limit and not _off_pending(name) and name not in _hold:
            mark_on(name)
            add(name, "off", at=datetime.now(TZ) + timedelta(minutes=limit), label="auto-off")
        return
    exp = _expected.get(name)
    if prev == on or (exp and exp[0] == on and time.time() - exp[1] < 15 * 60):
        return
    note = ""
    if on:
        mark_on(name)
        limit = dev.auto_off_minutes or dev.max_on_minutes
        if limit and not _off_pending(name) and name not in _hold:
            when = datetime.now(TZ) + timedelta(minutes=limit)
            add(name, "off", at=when, label="auto-off")
            note = f" · off at {when.strftime('%-I:%M %p').lower()}"
    else:
        _on_since.pop(name, None)
        _hold.discard(name)
        stop_heat_watch(name)
        for j in list_jobs():  # its pending auto-off is moot now
            if j["device"] == name and j["action"] == "off" and not j["recurring"]:
                cancel(j["id"])
    notify.event(f"{dev.icon} {dev.display} turned {'on' if on else 'off'} in the app{note}")


def on_push(msg: dict) -> None:
    """Tuya Message Service event → observed()."""
    biz = msg.get("bizData") or {}
    dev_id = biz.get("devId")
    dev = next((d for d in devices.registry().values() if getattr(d, "device_id", None) == dev_id), None)
    if not dev:
        return
    if msg.get("bizCode") == "devicePropertyMessage":
        log.info("push %s: %s", dev.name, {p.get("code"): p.get("value") for p in biz.get("properties", [])})
        code = getattr(dev, "switch_code", "switch_1")
        for p in biz.get("properties", []):
            if p.get("code") == code:
                dev.note(on=bool(p.get("value")))
                observed(dev.name, bool(p.get("value")))
            elif p.get("code") == "cur_power" and isinstance(p.get("value"), (int, float)):
                dev.note(power_w=p["value"] / 10)  # 0.1 W units
                heat_reading(dev.name, p["value"] / 10)
            elif p.get("code") == "add_ele" and isinstance(p.get("value"), (int, float)) and p["value"] > 0:
                store.record_energy(dev.name, p["value"] / 1000)  # increment since the plug's last report, 0.001 kWh units
    elif msg.get("bizCode") == "deviceOffline":
        notify.event(f"⚠️ {dev.display} went offline")
    elif msg.get("bizCode") == "deviceOnline":
        notify.event(f"{dev.icon} {dev.display} is back online")


HEAT_HIGH_W, HEAT_LOW_W = 1000, 100
_heat: dict[str, dict] = {}  # per heater: {"start", "seen_high", "lows"}


def mark_on(device: str) -> None:
    _on_since[device] = time.time()
    try:
        if devices.get(device).off_when_heated:
            start_heat_watch(device)
    except Exception:
        log.exception("heat watch start failed")


push_connected = lambda: False  # set by the bot when the Tuya push stream is up


def _heat_job(name: str, seconds: float, kind: str) -> None:
    s = scheduler()
    if not s.running:
        return
    jid = f"_heat_{name}"
    if kind == "poll":
        s.add_job(heat_check, "interval", seconds=seconds, args=[name], id=jid, jobstore="memory", replace_existing=True)
    else:
        s.add_job(heat_check, DateTrigger(run_date=datetime.now(TZ) + timedelta(seconds=seconds)), args=[name],
                  id=jid, jobstore="memory", replace_existing=True)


def start_heat_watch(name: str) -> None:
    """Thermostat heater turned on. With live pushes: no polling, just one check at 3 min (for 'already hot').
    Without pushes: poll every 2 min."""
    _heat[name] = {"start": time.time(), "seen_high": False, "lows": 0}
    if push_connected():
        _heat_job(name, 180, "once")
    else:
        _heat_job(name, 120, "poll")


def stop_heat_watch(name: str) -> None:
    _heat.pop(name, None)
    s = scheduler()
    if s.running and s.get_job(f"_heat_{name}", jobstore="memory"):
        s.remove_job(f"_heat_{name}", jobstore="memory")


def heat_check(name: str) -> None:
    """One API status read (confirmation or fallback poll)."""
    try:
        st = devices.get(name).status()
    except Exception as e:
        log.warning("heat check %s: %s", name, e)
        return
    if not st.on:
        return stop_heat_watch(name)
    heat_reading(name, st.power_w or 0.0, confirm=True)


def heat_reading(name: str, watts: float, confirm: bool = False) -> None:
    """Feed one power reading (push, or an API check). Two lows in a row after heating = thermostat cut out.
    A first low from a push schedules a single confirmation check a minute later."""
    h = _heat.get(name)
    if h is None:
        return
    log.info("heat %s: %.0f W (%s)", name, watts, "check" if confirm else "push")
    if watts >= HEAT_HIGH_W:
        h["seen_high"], h["lows"] = True, 0
        if push_connected():  # heating confirmed; nothing to poll for until power changes
            s = scheduler()
            if s.running and s.get_job(f"_heat_{name}", jobstore="memory"):
                s.remove_job(f"_heat_{name}", jobstore="memory")
        return
    if watts >= HEAT_LOW_W:
        h["lows"] = 0
        return
    h["lows"] += 1
    mins = (time.time() - h["start"]) / 60
    if h["seen_high"] and h["lows"] >= 2:
        msg = f"heated in {mins:.0f} min"
    elif not h["seen_high"] and h["lows"] >= 2 and mins >= 3:
        msg = "water was already hot"
    elif not h["seen_high"] and mins >= 3 and confirm:  # the 3-min check found no heating at all
        h["lows"] = 1
        _heat_job(name, 60, "once")
        return
    elif h["seen_high"] and not confirm:
        msg = f"heated in {mins:.0f} min"  # live push: power fell after heating = thermostat cut out → act now
    else:
        if push_connected():
            _heat_job(name, 60, "once")  # confirm with one API read
        return
    dev = devices.get(name)
    stop_heat_watch(name)
    try:
        dev.set(False)
        expect(name, False)
        for j in list_jobs():  # pending one-off offs are moot now
            if j["device"] == name and j["action"] == "off" and not j["recurring"]:
                cancel(j["id"])
        store.record_heat(name, mins if "heated" in msg else 0)
        notify.event(f"{dev.icon} Water's hot — {msg}, {dev.display.lower()} off" if "heated" in msg
                     else f"{dev.icon} {msg.capitalize()} — {dev.display.lower()} off")
    except Exception as e:
        notify.send(f"⚠️ {dev.display}: water's hot but turning it off failed: {e}")


def on_since(device: str) -> float | None:
    return _on_since.get(device)


_last_full_poll = 0.0
SANITY_POLL_SECONDS = 6 * 3600


def poll() -> None:
    """Read every plug (safety cap, baseline, missed changes). While live pushes are flowing this is only a
    6-hourly sanity check — pushes already keep state current — so it costs almost no API calls."""
    global _last_full_poll
    if push_connected() and time.time() - _last_full_poll < SANITY_POLL_SECONDS:
        return
    _last_full_poll = time.time()
    for name, dev in devices.registry().items():
        try:
            st = dev.status()
        except Exception as e:
            log.warning("poll %s: %s", name, e)
            _maybe_alert_tuya(str(e))
            continue
        store.record(st)
        observed(name, st.on, st.power_w)
        if st.on:
            on_min = (time.time() - _on_since.setdefault(name, time.time())) / 60
            reason = None
            if dev.max_on_minutes and on_min > dev.max_on_minutes:
                reason = f"safety limit of {dev.max_on_minutes:g} min reached"
            # Catches ONs from the ZEB app / physical button too; respects explicit off times and "indefinitely".
            elif (dev.auto_off_minutes and on_min > dev.auto_off_minutes
                  and name not in _hold and not _off_pending(name)):
                reason = f"auto-off after {dev.auto_off_minutes:g} min"
            if reason:
                try:
                    dev.set(False)
                    expect(name, False)
                    notify.event(f"⏰ {dev.display} turned off · {reason}")
                except Exception as e:
                    notify.send(f"⚠️ {dev.display}: {reason}, but turning it off failed: {e}")
        else:
            _on_since.pop(name, None)
            _hold.discard(name)


def start() -> None:
    s = scheduler()
    s.add_job(poll, "interval", seconds=settings().energy_poll_seconds, id="_poll", jobstore="memory",
              next_run_time=datetime.now(TZ))
    s.start()


def add(device: str, action: str, *, at: datetime | None = None, in_minutes: float | None = None,
        cron: str | None = None, sun: tuple | None = None, label: str = "", group: str = "") -> dict:
    """Schedule `action` on `device`. Exactly one of at / in_minutes / cron / sun=(event, offset_min, dow)."""
    dev = devices.get(device)  # validates the name
    if sum(x is not None for x in (at, in_minutes, cron, sun)) != 1:
        raise ValueError("give exactly one of at, in_minutes, cron, sun")
    if action not in ("on", "off", "toggle"):
        raise ValueError(f"bad action {action!r}")
    if cron:
        trigger = CronTrigger.from_crontab(cron, timezone=TZ)
    elif sun:
        trigger = SunTrigger(sun[0], sun[1], sun[2] or "*", timezone=TZ)
    else:
        when = at if at else datetime.now(TZ) + timedelta(minutes=in_minutes)
        if when.tzinfo is None:
            when = when.replace(tzinfo=TZ)
        if when <= datetime.now(TZ):
            raise ValueError(f"{when.isoformat()} is in the past")
        trigger = DateTrigger(run_date=when)
    job = scheduler().add_job(run_action, trigger, kwargs={"device": dev.name, "action": action, "label": label, "group": group})
    if (cron or sun) and away_state().get("active"):  # new routines added while away wait too
        job.pause()
        _save_away({**away_state(), "paused": away_state().get("paused", []) + [job.id]})
        job = scheduler().get_job(job.id)
    return describe(job)


def describe(job) -> dict:
    kw = job.kwargs
    return {
        "id": job.id,
        "device": kw.get("device"),
        "action": kw.get("action"),
        "label": kw.get("label", ""),
        "group": kw.get("group") or job.id,
        "recurring": isinstance(job.trigger, (CronTrigger, SunTrigger)),
        "next_run": job.next_run_time.isoformat() if getattr(job, "next_run_time", None) else None,
        "trigger": str(job.trigger),
    }


def list_jobs() -> list[dict]:
    return [describe(j) for j in scheduler().get_jobs() if not j.id.startswith("_")]


def cancel(job_id: str) -> None:
    scheduler().remove_job(job_id)


def reschedule(job_id: str, when: datetime) -> None:
    scheduler().reschedule_job(job_id, trigger=DateTrigger(run_date=when))


def raw_jobs() -> list:
    return [j for j in scheduler().get_jobs() if not j.id.startswith("_")]


# ---------- away mode: pause every routine until the return day ----------

def _away_file():
    return settings().data_path / "away.json"


def away_state() -> dict:
    try:
        return json.loads(_away_file().read_text())
    except Exception:
        return {}


def _save_away(state: dict) -> None:
    if state:
        _away_file().write_text(json.dumps(state))
    else:
        _away_file().unlink(missing_ok=True)


def set_away(until: datetime, start: datetime | None = None) -> dict:
    """Pause routines from `start` (default now) until `until`; resume automatically then."""
    now = datetime.now(TZ)
    if until <= now:
        raise ValueError("the return date is in the past")
    if start and start >= until:
        raise ValueError("away start must be before the return date")
    end_away(silent=True)
    s = scheduler()
    s.add_job(away_end, DateTrigger(run_date=until), id="_away_end", replace_existing=True)
    state = {"active": False, "from": (start or now).isoformat(), "until": until.isoformat(), "paused": []}
    _save_away(state)
    if start and start > now:
        s.add_job(away_start, DateTrigger(run_date=start), id="_away_start", replace_existing=True)
    else:
        state = away_start(silent=True)
    return state


def away_start(silent: bool = False) -> dict:
    state = away_state()
    paused = []
    for j in raw_jobs():
        if getattr(j, "next_run_time", None):
            j.pause()
            paused.append(j.id)
    state.update(active=True, paused=paused)
    _save_away(state)
    for dev in devices.registry().values():
        try:
            dev.set(False)
            expect(dev.name, False)
        except Exception as e:
            notify.send(f"⚠️ Away mode: couldn't switch off {dev.display}: {e}")
    if not silent:
        until = datetime.fromisoformat(state["until"])
        notify.event(f"✈️ Away mode on — routines paused until {until:%a %-d %b}")
    return state


def away_end(silent: bool = False) -> int:
    state = away_state()
    s = scheduler()
    for jid in ("_away_start", "_away_end"):
        if s.get_job(jid):
            s.remove_job(jid)
    groups = set()
    for jid in state.get("paused", []):
        if job := s.get_job(jid):
            groups.add(job.kwargs.get("group") or jid)
            s.resume_job(jid)  # one-offs that fell inside the trip are dropped by APScheduler here
    n = len(groups)
    _save_away({})
    if state and not silent:
        notify.event(f"🏠 Welcome back — {n} routine{'s' if n != 1 else ''} resumed")
    return n


def end_away(silent: bool = False) -> int:
    return away_end(silent=silent)


# ---------- snapshots (for Undo: restore the exact schedule state before a change) ----------

def _trigger_spec(trigger) -> dict:
    if isinstance(trigger, DateTrigger):
        return {"type": "date", "at": trigger.run_date.isoformat()}
    if isinstance(trigger, SunTrigger):
        return {"type": "sun", "event": trigger.event, "offset": trigger.offset_minutes, "dow": trigger.dow}
    if isinstance(trigger, CronTrigger):
        return {"type": "cron", "fields": {f.name: str(f) for f in trigger.fields if not f.is_default}}
    raise ValueError(f"unsupported trigger {trigger!r}")


def _trigger_from(spec: dict):
    if spec["type"] == "date":
        return DateTrigger(run_date=datetime.fromisoformat(spec["at"]))
    if spec["type"] == "sun":
        return SunTrigger(spec["event"], spec["offset"], spec["dow"], timezone=TZ)
    return CronTrigger(timezone=TZ, **spec["fields"])


_FUNCS = {"run_action": lambda: run_action, "away_start": lambda: away_start, "away_end": lambda: away_end}


def snapshot() -> dict:
    """JSON-serialisable copy of every persisted job (routines, timers, away jobs) + away state."""
    jobs = []
    for j in scheduler().get_jobs(jobstore="default"):
        name = getattr(j.func, "__name__", "")
        if name not in _FUNCS:
            continue
        jobs.append({"id": j.id, "func": name, "kwargs": dict(j.kwargs), "trigger": _trigger_spec(j.trigger),
                     "paused": getattr(j, "next_run_time", None) is None})
    return {"jobs": sorted(jobs, key=lambda x: x["id"]), "away": away_state()}


def restore(snap: dict) -> None:
    s = scheduler()
    for j in s.get_jobs(jobstore="default"):
        if getattr(j.func, "__name__", "") in _FUNCS:
            s.remove_job(j.id)
    now = datetime.now(TZ)
    for spec in snap.get("jobs", []):
        trig = _trigger_from(spec["trigger"])
        if spec["trigger"]["type"] == "date" and trig.run_date <= now:
            continue  # a one-off whose time has passed meanwhile
        job = s.add_job(_FUNCS[spec["func"]](), trig, id=spec["id"], kwargs=spec["kwargs"], replace_existing=True)
        if spec.get("paused"):
            job.pause()
    _save_away(snap.get("away") or {})
