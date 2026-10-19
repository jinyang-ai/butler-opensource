import pytest

from butler import commands, devices, scheduler
from butler.config import settings


class FakePlug(devices.Device):
    def __init__(self, name, aliases, auto_off=None):
        super().__init__(name, aliases, auto_off_minutes=auto_off)
        self.on = False
        self.watts = None  # override power reading in tests

    def status(self):
        w = self.watts if self.watts is not None else (1500.0 if self.on else 0.0)
        return devices.PlugStatus(self.name, self.on, power_w=w if self.on else 0.0)

    def set(self, on):
        self.on = on
        return self.status()


@pytest.fixture(autouse=True)
def env(tmp_path, monkeypatch):
    monkeypatch.setattr(settings(), "data_dir", tmp_path)
    monkeypatch.setattr(devices, "_registry", {
        "geyser": FakePlug("geyser", ["water heater"], auto_off=30),
        "purifier": FakePlug("purifier", ["air purifier"]),
    })
    monkeypatch.setattr(scheduler, "_sched", None)
    monkeypatch.setattr(commands, "_recent", {"devices": [], "ts": 0.0})
    from butler import store
    monkeypatch.setattr(store, "_conn", None)  # fresh DB per test (data_dir is a tmp dir)
    scheduler.scheduler().start(paused=True)
    yield
    scheduler.scheduler().shutdown(wait=False)


def test_immediate():
    assert "is on" in commands.execute("geyser on")
    assert devices.get("geyser").on
    commands.execute("all off")
    assert not devices.get("geyser").on and not devices.get("purifier").on


def test_timer_schedules_inverse():
    out = commands.execute("purifier on for 20 min")
    assert devices.get("purifier").on and "turns off at" in out
    jobs = scheduler.list_jobs()
    assert len(jobs) == 1 and jobs[0]["action"] == "off"


def test_default_auto_off():
    out = commands.execute("geyser on")
    assert "turns off at" in out
    assert [j["action"] for j in scheduler.list_jobs()] == ["off"]
    commands.execute("cancel all")
    commands.execute("geyser on at 5am")  # scheduled ON also gets the default off
    assert sorted(j["action"] for j in scheduler.list_jobs()) == ["off", "on"]
    commands.execute("cancel all")
    commands.execute("geyser on 5-7am")
    jobs = sorted(scheduler.list_jobs(), key=lambda j: j["next_run"])
    assert [j["action"] for j in jobs] == ["on", "off"]
    assert jobs[1]["next_run"][11:16] == "07:00"
    commands.execute("cancel all")
    commands.execute("geyser on indefinitely")
    assert scheduler.list_jobs() == [] and "geyser" in scheduler._hold
    commands.execute("purifier on")  # no auto_off configured
    assert scheduler.list_jobs() == []


def test_poller_auto_off(monkeypatch):
    import time as _t
    g = devices.get("geyser")
    g.on = True
    scheduler._on_since["geyser"] = _t.time() - 31 * 60
    scheduler._hold.discard("geyser")
    monkeypatch.setattr(scheduler.store, "record", lambda st: None)
    scheduler.poll()
    assert not g.on
    g.on = True
    scheduler._on_since["geyser"] = _t.time() - 31 * 60
    scheduler.add("geyser", "off", in_minutes=60)  # explicit off time → poller leaves it alone
    scheduler.poll()
    assert g.on


def test_recurring_and_cancel():
    commands.execute("every weekday 6am geyser on for 25 min")
    jobs = scheduler.list_jobs()
    assert {j["action"] for j in jobs} == {"on", "off"}
    groups = commands.schedule_groups()
    assert len(groups) == 1 and "weekdays 6–6:25 am" in groups[0]["text"]
    assert commands.respond("schedules").views == {"schedules"}
    assert commands.execute("cancel geyser") == "🗑 Cancelled 1 schedule"
    assert commands.schedule_groups() == []


def test_cron_wraps_midnight():
    assert commands._shift_cron(23, 50, "fri", 20) == "10 0 * * sat"
    assert commands._shift_cron(23, 50, "mon-fri", 20) == "10 0 * * tue,wed,thu,fri,sat"


def test_run_action_executes():
    scheduler.run_action("geyser", "on")
    assert devices.get("geyser").on


def test_errors():
    assert "Which one?" in commands.execute("on")
    assert commands.execute("blah").startswith("didn't get that")


def test_llm_step_conversion():
    from butler import llm, nlp
    from datetime import datetime, timedelta

    al = commands.aliases()
    now = datetime.now(nlp.TZ)
    later = (now + timedelta(hours=2)).strftime("%Y-%m-%dT%H:%M")
    c = llm._to_command(llm.Step(intent="power", devices=["Water Heater"], action="on", at=later,
                                 duration_minutes=20), al, now)
    assert c.devices == ["geyser"] and c.at > now and c.duration_minutes == 20
    c = llm._to_command(llm.Step(intent="power", devices=["geyser"], action="on",
                                 cron=llm.Cron(hour=6, minute=0, days="mon-fri")), al, now)
    assert c.cron == (6, 0, "mon-fri")
    with pytest.raises(ValueError):
        llm._to_command(llm.Step(intent="power", devices=["fridge"], action="on"), al, now)


def test_llm_failure_falls_back(monkeypatch):
    from butler import llm

    monkeypatch.setattr(llm, "enabled", lambda: True)
    monkeypatch.setattr(llm, "parse", lambda *a: (_ for _ in ()).throw(RuntimeError("429")))
    assert "is on" in commands.execute("geyser on")


def test_dashboard_render_and_undo():
    from butler import bot

    bot.STATE = bot.State()
    st = bot.STATE.get(1)
    text, markup = bot.render_home(st)
    labels = [b.text for row in markup.inline_keyboard for b in row]
    assert "updated" in text and "🔌 Turn on Geyser" in labels and "📅" in labels
    assert "⏱ Geyser 1 hr" in labels  # geyser has auto-off → timer button

    r = commands.respond("geyser on")
    bot._set_last(1, r)
    text, markup = bot.render_home(bot.STATE.get(1))
    labels = [b.text for row in markup.inline_keyboard for b in row]
    assert "is on" in text and "↩️ Undo" in labels and "🔌 Turn off Geyser" in labels and "⏱ +15 min Geyser" in labels
    assert "off at" in text

    u = bot.STATE.get(1)["undo"]
    commands.undo(u["groups"], u["switched"])
    assert not devices.get("geyser").on and scheduler.list_jobs() == []

    r = commands.respond("geyser on at 5am")
    text, markup = bot.render_schedules({})
    assert "5–5:30 am" in text and markup.inline_keyboard[0][0].callback_data.startswith("xg:")
    assert all(len(b.callback_data) <= 64 for row in markup.inline_keyboard for b in row)
    commands.undo(r.groups, r.switched)
    assert scheduler.list_jobs() == []


def test_device_card_and_extend():
    commands.execute("geyser on")
    text, on = commands.device_card("geyser")
    assert on and "Turns off at" in text and "on since" in text
    before = commands._next(commands._jobs_for("geyser")[0])
    after = commands.extend("geyser", 15)
    assert (after - before).total_seconds() == 15 * 60
    commands.keep_on("geyser")
    assert "Stays on" in commands.device_card("geyser")[0]
    commands.execute("geyser off")
    assert not commands.device_card("geyser")[1]


def test_tuya_trial_alert(monkeypatch):
    sent = []
    monkeypatch.setattr(scheduler.notify, "send", sent.append)
    monkeypatch.setattr(scheduler, "_last_tuya_alert", 0.0)
    scheduler._maybe_alert_tuya("geyser: Tuya cloud error: IoT Core service is not subscribed.")
    scheduler._maybe_alert_tuya("geyser: Tuya cloud error: IoT Core service is not subscribed.")  # rate-limited
    scheduler._maybe_alert_tuya("geyser: timeout")  # unrelated
    assert len(sent) == 1 and "Extend Trial" in sent[0]


def test_away_mode_pauses_and_resumes(monkeypatch):
    from datetime import datetime, timedelta
    from butler import nlp

    monkeypatch.setattr(scheduler.notify, "event", lambda *_: None)
    commands.execute("every day 6am geyser on")
    commands.execute("purifier on")
    assert devices.get("purifier").on

    r = commands.respond("I'm away for 3 days")
    assert r.away and "1 routine paused" in r.text
    assert not devices.get("purifier").on  # everything switched off
    assert all(j["next_run"] is None for j in scheduler.list_jobs() if j["recurring"])
    assert "Away" in commands.away_banner()
    cal = commands.week_days()
    assert "✈️ away" in cal[1] and "6–6:30 am" in cal[4]  # back on day 3 → routines show again

    commands.execute("weekdays 7am purifier on")  # added while away → also paused
    assert all(j["next_run"] is None for j in scheduler.list_jobs() if j["recurring"])

    assert "2 routines resumed" in commands.execute("I'm back")
    assert all(j["next_run"] for j in scheduler.list_jobs() if j["recurring"])
    assert commands.away_banner() is None

    r = commands.respond("away till " + (datetime.now(nlp.TZ) + timedelta(days=4)).strftime("%-d %b"))
    commands.undo(r.groups, r.switched, r.away)
    assert commands.away_banner() is None


def test_week_calendar_spans():
    commands.execute("every day 8pm purifier on for 12 hours")
    cal = commands.week_days()
    assert len(cal) == 7 and all("8 pm–8 am" in line for line in cal[2:])  # today/tomorrow depend on the clock


def test_poll_detects_app_changes(monkeypatch):
    events = []
    monkeypatch.setattr(scheduler.notify, "event", events.append)
    monkeypatch.setattr(scheduler.store, "record", lambda st: None)
    scheduler._last_on.clear(); scheduler._expected.clear()
    scheduler.poll()  # baseline
    devices.get("purifier").on = True  # switched in the Smart Life app
    scheduler.poll()
    assert events and "Purifier turned on in the app" in events[-1]
    events.clear()
    commands.execute("purifier off")  # bot's own change → no "from the app" event
    scheduler.poll()
    assert events == []


def test_push_events(monkeypatch):
    events = []
    monkeypatch.setattr(scheduler.notify, "event", events.append)
    scheduler._last_on.clear(); scheduler._expected.clear()
    g = devices.get("geyser"); g.device_id = "dev-g"
    scheduler.observed("geyser", False)  # baseline
    msg = {"bizCode": "devicePropertyMessage",
           "bizData": {"devId": "dev-g", "properties": [{"code": "switch_1", "value": True}]}}
    scheduler.on_push(msg)
    assert "Geyser turned on in the app · off at" in events[-1]
    assert [j["action"] for j in scheduler.list_jobs()] == ["off"]  # auto-off scheduled immediately
    msg["bizData"]["properties"][0]["value"] = False
    scheduler.on_push(msg)
    assert "turned off in the app" in events[-1] and scheduler.list_jobs() == []
    events.clear()
    commands.execute("geyser on")  # our own change, then its push echo → no "in the app" event
    msg["bizData"]["properties"][0]["value"] = True
    scheduler.on_push(msg)
    assert events == []


def test_geyser_blanket_auto_off(monkeypatch):
    monkeypatch.setattr(scheduler.notify, "event", lambda *_: None)
    out = commands.execute("toggle geyser")  # toggle → on → auto-off
    assert devices.get("geyser").on and "turns off at" in out
    commands.execute("cancel all"); commands.execute("geyser off")
    # bot restarts while the geyser is on (e.g. switched on in the app during downtime)
    scheduler._last_on.clear(); scheduler._expected.clear(); scheduler._hold.clear()
    devices.get("geyser").on = True
    scheduler.observed("geyser", True)
    assert [j["label"] for j in scheduler.list_jobs()] == ["auto-off"]
    # purifier has no auto-off rule → untouched
    devices.get("purifier").on = True
    scheduler.observed("purifier", True)
    assert all(j["device"] == "geyser" for j in scheduler.list_jobs())


def test_energy_report(monkeypatch):
    from butler import store

    monkeypatch.setattr(scheduler.notify, "event", lambda *_: None)
    g = devices.get("geyser"); g.device_id = "dev-g"
    scheduler.on_push({"bizCode": "devicePropertyMessage", "bizData": {"devId": "dev-g", "properties": [
        {"code": "add_ele", "value": 120}, {"code": "add_ele", "value": 80}]}})
    commands.execute("geyser on")
    rep = "\n".join(commands.energy_report())
    assert "0.20 kWh" in rep and "Today" in rep and "This week" in rep and "Total" in rep
    assert "Purifier" in rep and "kWh" not in rep.split("Purifier")[1]  # no meter → on-time only
    assert commands.respond("energy this week").views == {"energy"}


def test_geyser_two_hour_cap(monkeypatch):
    monkeypatch.setattr(scheduler.notify, "event", lambda *_: None)
    g = devices.get("geyser"); g.max_on_minutes = 120
    out = commands.execute("geyser on for 3 hours")
    assert "capped at 2 h max" in out
    off = commands._next(commands._jobs_for("geyser")[0])
    assert 119 <= (off - commands.datetime.now(commands.nlp.TZ)).total_seconds() / 60 <= 120
    commands.execute("geyser off")
    out = commands.execute("geyser on indefinitely")
    assert "capped" in out and commands._jobs_for("geyser")  # no endless runs
    commands.execute("geyser off")
    out = commands.execute("every day 5am geyser on until 9am")
    assert "5–7 am" in out and "capped" in out
    commands.execute("cancel all")
    commands.execute("geyser on")  # 30-min default stays
    when = commands.extend("geyser", 200)  # +200 min → clamped to on-since + 2 h
    assert when <= commands.cap_time("geyser")
    assert commands.keep_on("geyser") == commands.cap_time("geyser")


def test_latest_off_wins_and_dedupe():
    commands.execute("purifier on for 40 min")
    jobs = commands._jobs_for("purifier")
    assert len(jobs) == 1 and jobs[0]["label"] == "timer"
    commands.execute("purifier off in 3 min")
    jobs = commands._jobs_for("purifier")
    assert len(jobs) == 1 and (commands._next(jobs[0]) - commands.datetime.now(commands.nlp.TZ)).total_seconds() < 200
    commands.execute("geyser on at 4pm")
    out = commands.execute("geyser on at 4pm")
    assert "already scheduled" in out
    assert len([j for j in commands._jobs_for("geyser") if j["action"] == "on"]) == 1


def test_read_logs(tmp_path):
    from butler import bot
    from butler.config import settings

    (settings().data_path / "butler.log.1").write_text("old line\n")
    (settings().data_path / "butler.log").write_text("new line 1\nnew line 2\n")
    assert bot.read_logs() == ["old line", "new line 1", "new line 2"]
    assert bot.read_logs(2) == ["new line 1", "new line 2"]


def test_it_refers_to_recent_device():
    commands._recent.update(devices=[], ts=0)
    commands.execute("purifier on")
    assert "Purifier is off" in commands.execute("turn it off")


def test_sun_routine_schedules_and_shows():
    out = commands.execute("every day at sunset lamp on", use_llm=False) if "lamp" in devices.registry() else \
        commands.execute("every day at sunset purifier on")
    assert out.startswith("🌇") and "at sunset" in out and "next" in out
    jobs = scheduler.raw_jobs()
    assert len(jobs) == 1 and type(jobs[0].trigger).__name__ == "SunTrigger"
    cal = commands.week_days()
    assert all(" on 5:" in line or " on 6:" in line for line in cal[1:])  # a real time every day (today may be past)
    assert commands.schedule_groups()[0]["text"].endswith("every day at sunset")


def test_water_hot_detection(monkeypatch):
    import time as _t
    events = []
    monkeypatch.setattr(scheduler.notify, "event", events.append)
    g = devices.get("geyser"); g.off_when_heated = True
    commands.execute("geyser on")
    assert "geyser" in scheduler._heat
    for w in (2050, 2048, 3, 2):  # polling fallback: heating… then two low checks
        scheduler.heat_reading("geyser", w, confirm=True)
    assert not g.on and "Water's hot — heated in" in events[-1]
    assert commands._jobs_for("geyser") == []  # auto-off no longer needed
    assert "geyser" not in scheduler._heat

    events.clear()
    commands.execute("geyser on")
    scheduler._heat["geyser"]["start"] -= 4 * 60  # 4 min in, never drew power
    scheduler.heat_reading("geyser", 1, confirm=True); scheduler.heat_reading("geyser", 0, confirm=True)
    assert not g.on and "already hot" in events[-1]

    events.clear()  # a later scheduled off for an already-off geyser stays quiet
    scheduler.run_action("geyser", "off", label="every day 6–6:30 am")
    assert events == []

    commands.execute("geyser on")  # polling: a brief dip without two lows in a row doesn't trip it
    for w in (2050, 40, 2050, 2040):
        scheduler.heat_reading("geyser", w, confirm=True)
    assert g.on


def test_heat_push_mode_uses_few_api_calls(monkeypatch):
    events = []
    monkeypatch.setattr(scheduler.notify, "event", events.append)
    monkeypatch.setattr(scheduler, "push_connected", lambda: True)
    g = devices.get("geyser"); g.off_when_heated = True
    commands.execute("geyser on")
    job = scheduler.scheduler().get_job("_heat_geyser", jobstore="memory")
    assert job and type(job.trigger).__name__ == "DateTrigger"  # one check at 3 min, no polling
    scheduler.heat_reading("geyser", 1983)  # push: heating
    assert scheduler.scheduler().get_job("_heat_geyser", jobstore="memory") is None
    scheduler.heat_reading("geyser", 2)  # push: thermostat cut out → off that second, no API call
    assert not g.on and "Water's hot" in events[-1]
    # restart while heating: the watch resumes and the next low push still acts
    commands.execute("geyser on")
    from butler import store
    with store._lock:  # pretend it was switched on 20 min ago
        store.conn().execute("UPDATE states SET ts = ts - 1200 WHERE device='geyser'")
    scheduler._heat.clear(); scheduler._last_on.clear()
    scheduler.observed("geyser", True, 1983)
    scheduler.heat_reading("geyser", 0)
    assert not g.on and "heated in 20 min" in events[-1]




def test_web_api(monkeypatch):
    import asyncio
    from aiohttp.test_utils import TestClient, TestServer
    from butler import web
    from butler.config import settings

    monkeypatch.setattr(settings(), "api_token", "s3cret")
    seen = []

    async def on_action(text):
        seen.append(text)

    async def go():
        async with TestClient(TestServer(web.make_app(on_action))) as c:
            assert (await c.get("/status")).status == 401
            r = await c.get("/on/geyser", headers={"Authorization": "Bearer s3cret"})
            assert r.status == 200 and "is on" in await r.text()
            r = await c.get("/toggle/geyser?token=s3cret")
            assert "is off" in await r.text()
            r = await c.get("/status?token=s3cret")
            assert "Geyser" in await r.text()
            r = await c.get("/cmd?token=s3cret&text=purifier%20on%20for%2010%20min")
            assert "turns off at" in await r.text()
            assert (await c.get("/on/fridge?token=s3cret")).status == 404
            assert (await c.get("/health")).status == 200

    asyncio.run(go())
    assert seen and seen[0].startswith("⌚")


def test_export_history():
    import json
    g = devices.get("geyser"); g.device_id = "dev-g"
    commands.execute("geyser on")
    scheduler.on_push({"bizCode": "devicePropertyMessage", "bizData": {"devId": "dev-g", "properties": [
        {"code": "add_ele", "value": 250}]}})
    commands.execute("geyser off")
    data = commands.export_history()
    json.dumps(data)  # serialisable
    gd = next(d for d in data["devices"] if d["name"] == "geyser")
    assert gd["totals"]["runs"] == 1 and gd["runs"][0]["energy_kwh"] == 0.25 and gd["metered"]
    assert gd["daily"][0]["kwh"] == 0.25 and gd["state_changes"][0]["on"] is True
    assert any(d["name"] == "purifier" for d in data["devices"])
