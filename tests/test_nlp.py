from datetime import datetime

import pytest

from butler import nlp

ALIASES = {"geyser": "geyser", "water heater": "geyser", "heater": "geyser",
           "purifier": "purifier", "air purifier": "purifier", "ap": "purifier"}
NOW = datetime(2026, 10, 18, 9, 0, tzinfo=nlp.TZ)  # Sunday 9am


def p(text):
    return nlp.parse(text, ALIASES, now=NOW)


def test_simple_power():
    c = p("geyser on")
    assert (c.intent, c.devices, c.action, c.at) == ("power", ["geyser"], "on", None)
    assert p("turn off the air purifier").devices == ["purifier"]
    assert p("Switch OFF purifier!").action == "off"


def test_timer():
    c = p("geyser on for 20 min")
    assert c.action == "on" and c.duration_minutes == 20 and c.at is None
    assert p("purifier on for an hour").duration_minutes == 60
    assert p("purifier off for 1h 30m").duration_minutes == 90


def test_in():
    c = p("purifier off in 2h")
    assert c.in_minutes == 120 and c.action == "off"


def test_at_times():
    assert p("geyser on at 6:30am").at == NOW.replace(hour=6, minute=30) .replace(day=19)
    assert p("geyser on at 6pm").at == NOW.replace(hour=18)
    assert p("geyser on at 6").at == NOW.replace(hour=18)  # 6am passed → 6pm
    assert p("geyser on at 10").at == NOW.replace(hour=10)
    assert p("geyser off tomorrow 7am").at == NOW.replace(day=19, hour=7)
    assert p("purifier on tonight at 10").at == NOW.replace(hour=22)
    assert p("geyser on at 18:45").at == NOW.replace(hour=18, minute=45)
    c = p("geyser on at 6am for 25 min")
    assert c.at == NOW.replace(day=19, hour=6) and c.duration_minutes == 25


def test_recurring():
    c = p("every weekday 6am geyser on for 25 min")
    assert c.cron == (6, 0, "mon-fri") and c.duration_minutes == 25 and c.action == "on"
    assert p("daily 11pm purifier off").cron == (23, 0, "*")
    assert p("every monday and wednesday at 7:15 geyser on").cron == (7, 15, "mon,wed")
    assert p("weekends at 9am geyser on").cron == (9, 0, "sat,sun")
    assert p("every day geyser on").error


def test_multi_device():
    assert sorted(p("all off").devices) == ["geyser", "purifier"]
    assert sorted(p("geyser and purifier on").devices) == ["geyser", "purifier"]
    assert p("geyser off on monday at 7").at == NOW.replace(day=19, hour=7)
    assert p("geyser on mondays at 7am").cron == (7, 0, "mon")


def test_other_intents():
    assert p("status").intent == "status"
    c = p("is the geyser on?")
    assert c.intent == "status" and c.devices == ["geyser"]
    assert p("geyser").intent == "status"
    assert p("schedules").intent == "list"
    assert p("cancel all").target == "all"
    assert p("cancel geyser").target == "geyser"
    assert p("cancel 3fa2c1").target == "3fa2c1"
    e = p("energy today")
    assert e.intent == "energy" and e.hours == pytest.approx(9)
    assert p("geyser usage week").hours == 168
    assert p("/help").intent == "help"
    assert p("blah").intent == "unknown"


def test_ranges_and_until():
    c = p("keep the geyser on from 5-7am")
    assert c.at == NOW.replace(day=19, hour=5) and c.duration_minutes == 120 and c.action == "on"
    c = p("geyser on 11am to 1pm")
    assert c.at == NOW.replace(hour=11) and c.duration_minutes == 120
    c = p("geyser on between 5 and 7am")
    assert c.at == NOW.replace(day=19, hour=5) and c.duration_minutes == 120
    c = p("every weekday geyser on 5-6:30am")
    assert c.cron == (5, 0, "mon-fri") and c.duration_minutes == 90
    c = p("geyser on until 11am")
    assert c.at is None and c.duration_minutes == 120
    c = p("geyser on at 5am until 7am")
    assert c.at == NOW.replace(day=19, hour=5) and c.duration_minutes == 120
    assert p("geyser on indefinitely").no_auto_off


def test_away_and_back():
    c = p("I'm away for 5 days")
    assert c.intent == "away" and c.away_until == NOW.replace(day=23, hour=0)
    c = p("going on a trip till 25th")
    assert c.away_until == NOW.replace(day=25, hour=0)
    c = p("away until 3 nov")
    assert c.away_until == NOW.replace(month=11, day=3, hour=0)
    c = p("vacation from friday back on monday")
    assert c.away_from == NOW.replace(day=23, hour=0) and c.away_until == NOW.replace(day=26, hour=0)
    c = p("travelling back on 10th")
    assert c.away_until == NOW.replace(month=11, day=10, hour=0)  # 10th already passed this month
    assert p("away").error
    assert p("I'm back").intent == "back"
    assert p("resume routines").intent == "back"


def test_sun_phrases():
    c = p("lamp on at sunset every day")
    assert c.sun == ("sunset", 0, "*") and c.action == "on"
    c = p("every day 30 min before sunset lamp on")
    assert c.sun == ("sunset", -30, "*")
    c = p("geyser on 1 hour after sunrise on weekdays")
    assert c.sun == ("sunrise", 60, "mon-fri")
    c = p("lamp on at sunset")  # one-off → concrete time today (Bengaluru sunset ~5:58 pm in Oct)
    assert c.sun is None and c.at and c.at.date() == NOW.date() and 17 <= c.at.hour <= 18
