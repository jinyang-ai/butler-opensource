"""Offline, rule-based command parser. No LLM, no network.

Understands things like:
  geyser on                     purifier off in 2h
  geyser on at 6:30am for 20m   turn on the air purifier for an hour
  every weekday 6am geyser on for 25 min
  purifier off for 30 min       (pause, then back on)
  status / is the geyser on     schedules / cancel all / cancel geyser / cancel 3fa2c1
  energy today / usage week / geyser kwh 24h
"""

import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from tzlocal import get_localzone

TZ = get_localzone()

DAYS = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]
DAY_RE = r"(?:mon|tue|tues|wed|thu|thur|thurs|fri|sat|sun)(?:day|nesday|urday|sday|s)?"
UNIT_MIN = {"h": 60, "hr": 60, "hrs": 60, "hour": 60, "hours": 60,
            "m": 1, "min": 1, "mins": 1, "minute": 1, "minutes": 1,
            "s": 1 / 60, "sec": 1 / 60, "secs": 1 / 60, "second": 1 / 60, "seconds": 1 / 60}
DUR_PART = r"\d+(?:\.\d+)?\s*(?:hours|hour|hrs|hr|h|minutes|minute|mins|min|m|seconds|second|secs|sec|s)\b"
DUR = rf"(?:{DUR_PART}\s*(?:and\s*)?)+"
TIME = r"(?:noon|midnight|\d{1,2}(?:[:.]\d{2})?\s*(?:am|pm)?)"


@dataclass
class Command:
    intent: str  # power | status | list | cancel | energy | help | unknown
    devices: list[str] = field(default_factory=list)
    action: str | None = None
    at: datetime | None = None
    in_minutes: float | None = None
    duration_minutes: float | None = None
    cron: tuple[int, int, str] | None = None  # (hour, minute, day_of_week)
    sun: tuple[str, float, str | None] | None = None  # (sunrise|sunset, offset min, day_of_week or None = one-off)
    hours: float = 24
    target: str | None = None  # cancel target
    no_auto_off: bool = False  # user asked to keep it on with no default auto-off
    away_from: datetime | None = None  # away mode: start (None = now)
    away_until: datetime | None = None  # away mode: return day (routines resume at its start)
    error: str | None = None


def _minutes(dur: str) -> float:
    return sum(float(n) * UNIT_MIN[u] for n, u in re.findall(r"(\d+(?:\.\d+)?)\s*([a-z]+)", dur))


def _day_index(word: str) -> int:
    return DAYS.index(word[:3])


def _parse_dow(spec: str) -> str:
    spec = spec.strip()
    if re.fullmatch(r"every\s*day|everyday|daily|every\s*(?:morning|night|evening)", spec):
        return "*"
    if "weekday" in spec:
        return "mon-fri"
    if "weekend" in spec:
        return "sat,sun"
    return ",".join(DAYS[_day_index(d)] for d in re.findall(DAY_RE, spec))


def _hm(tok: str) -> tuple[int, int, bool]:
    """Return (hour, minute, explicit_meridiem)."""
    tok = tok.strip()
    if tok == "noon":
        return 12, 0, True
    if tok == "midnight":
        return 0, 0, True
    m = re.fullmatch(r"(\d{1,2})(?:[:.](\d{2}))?\s*(am|pm)?", tok)
    h, mi, ap = int(m[1]), int(m[2] or 0), m[3]
    if ap == "pm" and h < 12:
        h += 12
    if ap == "am" and h == 12:
        h = 0
    if h > 23 or mi > 59:
        raise ValueError(f"bad time {tok!r}")
    return h, mi, bool(ap) or h > 12 or h == 0


def _next_at(h: int, mi: int, explicit: bool, day: str | None, now: datetime) -> datetime:
    base = now.replace(second=0, microsecond=0)
    hours = [h] if explicit or h >= 12 else [h, h + 12]  # "at 6" → whichever of 6am/6pm comes first
    if day == "tonight" and not explicit and h < 12:
        hours = [h + 12]
    if day is None:
        offsets = [0, 1]
    elif day in ("today", "tonight"):
        offsets = [0]
    elif day == "tomorrow":
        offsets = [1]
    else:
        first = (_day_index(day) - now.weekday()) % 7
        offsets = [first, first + 7]
    future = [t for d in offsets for hh in hours if (t := (base + timedelta(days=d)).replace(hour=hh, minute=mi)) > now]
    if not future:
        raise ValueError("that time has already passed today")
    return min(future)


def _range_minutes(start: str, end: str) -> tuple[int, int, bool, int]:
    """'5'-'7am' → (5, 0, True, 120). Start borrows the end's am/pm when it has none."""
    hs, ms, es = _hm(start)
    he, me, ee = _hm(end)
    if not es and ee and hs < 12:
        if he >= 12 and (hs + 12) * 60 + ms <= he * 60 + me:
            hs += 12
        es = True
    dur = ((he * 60 + me) - (hs * 60 + ms)) % 1440 or 1440
    return hs, ms, es, dur


MONTHS = ["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"]
MON_RE = r"(?:jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]*"


def parse_day(text: str, now: datetime) -> datetime | None:
    """'tomorrow', 'monday', '25th', '25 oct', 'oct 25' → that day at 00:00 (always in the future)."""
    midnight = now.replace(hour=0, minute=0, second=0, microsecond=0)
    t = text.lower()
    if re.search(r"\btomorrow\b", t):
        return midnight + timedelta(days=1)
    if m := re.search(rf"\b(\d{{1,2}})(?:st|nd|rd|th)?(?:\s+(?:of\s+)?({MON_RE}))?\b|\b({MON_RE})\s+(\d{{1,2}})(?:st|nd|rd|th)?\b", t):
        day = int(m[1] or m[4])
        mon = m[2] or m[3]
        if 1 <= day <= 31:
            month = MONTHS.index(mon[:3]) + 1 if mon else now.month
            year = now.year
            for _ in range(13):
                try:
                    cand = midnight.replace(year=year, month=month, day=day)
                except ValueError:
                    cand = None
                if cand and cand > now:
                    return cand
                month, year = (1, year + 1) if month == 12 else (month + 1, year)
                if mon:  # explicit month → only roll the year
                    month = MONTHS.index(mon[:3]) + 1
    if m := re.search(rf"\b({DAY_RE})\b", t):
        delta = (_day_index(m[1]) - now.weekday()) % 7 or 7
        return midnight + timedelta(days=delta)
    return None


AWAY_RE = r"\b(?:away|travell?ing|vacation|holiday|out of town|trip|pause (?:all|everything|routines|schedules))\b"
BACK_RE = r"\b(?:i'?m back|i am back|back home|we'?re back|resume (?:all|everything|routines|schedules)|end away|cancel away|stop away)\b"


def _parse_away(s: str, now: datetime) -> Command:
    cmd = Command("away")
    midnight = now.replace(hour=0, minute=0, second=0, microsecond=0)
    if m := re.search(r"\bfrom\s+(.+?)(?=\s+(?:till|until|to|back|returning|for)\b|$)", s):
        cmd.away_from = parse_day(m[1], now)
    if m := re.search(r"\bfor\s+(\d+|a|one|two|three)\s*(day|days|week|weeks)\b", s):
        n = {"a": 1, "one": 1, "two": 2, "three": 3}.get(m[1]) or int(m[1])
        base = cmd.away_from or midnight
        cmd.away_until = base + timedelta(days=n * (7 if m[2].startswith("week") else 1))
    elif m := re.search(r"\b(?:till|until|to|back on|back|returning on|returning|return on|return)\s+(.+)$", s):
        cmd.away_until = parse_day(m[1], cmd.away_from or now)  # "from friday back on monday" → the Monday after
    if not cmd.away_until:
        return Command("away", error="Until when? e.g. “away for 5 days” or “away till 25th”")
    return cmd


def parse(text: str, device_aliases: dict[str, str], now: datetime | None = None) -> Command:
    """`device_aliases` maps lowercase alias/name → canonical device name."""
    now = now or datetime.now(TZ)
    s = " " + text.lower().strip() + " "
    s = s.replace("half an hour", "30 min").replace("an hour", "1 hour").replace("a minute", "1 min")
    s = re.sub(r"\b(?:a\.m\.|a\.m)", "am", s)
    s = re.sub(r"\b(?:p\.m\.|p\.m)", "pm", s)
    s = re.sub(r"[!,]", " ", s)

    # Devices
    found: list[str] = []
    if re.search(r"\b(?:all|both|everything)\b", s):
        found = sorted(set(device_aliases.values()))
        s = re.sub(r"\b(?:all|both|everything)\b", " ", s)
    for alias in sorted(device_aliases, key=len, reverse=True):
        pat = rf"\b{re.escape(alias)}\b"
        if re.search(pat, s):
            name = device_aliases[alias]
            if name not in found:
                found.append(name)
            s = re.sub(pat, " ", s)

    if re.search(r"^\s*(?:/?help|/start|\?)\s*$", s):
        return Command("help")

    if re.search(BACK_RE, s):
        return Command("back")
    if re.search(AWAY_RE, s) and not found:
        return _parse_away(s, now)

    m = re.search(r"\b(?:cancel|delete|remove|clear)\b\s*(.*)", s)
    if m:
        target = m[1].strip() or None
        target = re.sub(r"\b(?:the|schedules?|jobs?|timers?)\b", " ", target or "").strip() or None
        if re.search(r"\b(?:all|everything)\b", text.lower()):
            target = "all"
        elif found:
            target = found[0]
        if not target:
            return Command("cancel", error="cancel what? use `cancel all`, `cancel geyser`, or `cancel <id>`")
        return Command("cancel", devices=found, target=target)

    if re.search(r"\b(?:schedules?|scheduled|jobs|timers|list|upcoming)\b", s):
        return Command("list", devices=found)

    if re.search(r"\b(?:energy|usage|consumption|kwh|units|bill)\b", s):
        hours = 24.0
        if "today" in s:
            hours = (now - now.replace(hour=0, minute=0, second=0, microsecond=0)).total_seconds() / 3600
        elif "week" in s:
            hours = 168
        elif "month" in s:
            hours = 720
        elif m := re.search(rf"({DUR})", s):
            hours = _minutes(m[1]) / 60
        return Command("energy", devices=found, hours=max(hours, 0.1))

    cmd = Command("power", devices=found)
    try:
        if m := re.search(rf"\bfor\s+({DUR})", s):
            cmd.duration_minutes = _minutes(m[1])
            s = s.replace(m[0], " ")
        if m := re.search(rf"\b(?:in|after)\s+({DUR})", s):
            cmd.in_minutes = _minutes(m[1])
            s = s.replace(m[0], " ")

        recur = re.search(
            rf"\b(every\s*day|everyday|daily|every\s*(?:morning|night|evening)|(?:every\s+)?weekdays?|"
            rf"(?:every\s+)?weekends?|every\s+{DAY_RE}(?:\s*(?:and\s+)?{DAY_RE})*|{DAY_RE}s"
            rf"(?:\s*(?:and\s+)?{DAY_RE}s)*)\b", s)
        dow = None
        if recur:
            dow = _parse_dow(recur[1])
            s = s.replace(recur[0], " ")

        if m := re.search(r"\b(?:(\d+(?:\.\d+)?)\s*(h|hr|hrs|hours?|m|min|mins|minutes?)\s*(before|after)\s+)?(?:at\s+)?(sunset|sunrise|dusk|dawn)\b", s):
            event = {"dusk": "sunset", "dawn": "sunrise"}.get(m[4], m[4])
            off = (float(m[1]) * (60 if m[2].startswith("h") else 1) * (-1 if m[3] == "before" else 1)) if m[1] else 0
            cmd.sun = (event, off, dow)
            s = s.replace(m[0], " ")

        day = None
        if m := re.search(rf"\b(today|tonight|tomorrow|{DAY_RE})\b", s):
            day = m[1] if m[1] in ("today", "tonight", "tomorrow") else DAYS[_day_index(m[1])]
            s = s.replace(m[0], " ")

        if re.search(r"\b(?:indefinitely|forever|permanently|no auto[- ]?off|without auto[- ]?off|until i (?:say|turn it off))\b", s):
            cmd.no_auto_off = True

        rng = re.search(rf"\b(?:from\s+|between\s+)?({TIME})\s*(?:-|–|to|till|until|and)\s*({TIME})(?![\d:])", s)
        if rng and re.search(r"\d", rng[1]) and re.search(r"\d", rng[2]) or (rng and "from" in rng[0]):
            h, mi, explicit, cmd.duration_minutes = _range_minutes(rng[1], rng[2])
            s = s.replace(rng[0], " ")
            if dow is not None:
                cmd.cron = (h, mi, dow)
            else:
                cmd.at = _next_at(h, mi, explicit, day, now)
            rng = True
        until = None if rng else re.search(rf"\b(?:until|till|til)\s+({TIME})(?![\d:])", s)
        if until:
            s = s.replace(until[0], " ")

        tm = None if rng else (re.search(rf"\b(?:at|@|by)\s*({TIME})(?![\d:])", s)
              or re.search(r"\b(\d{1,2}(?:[:.]\d{2})?\s*(?:am|pm))\b", s)
              or re.search(r"\b(\d{1,2}:\d{2})\b", s)
              or re.search(r"\b(noon|midnight)\b", s))
        if cmd.sun:
            tm = None
            if cmd.sun[2] is None:  # one-off: next occurrence (from the given day, if any)
                from .sun import next_sun
                base = now if not day else _next_at(0, 0, True, day, now) - timedelta(seconds=1)
                cmd.at, cmd.sun = next_sun(cmd.sun[0], cmd.sun[1], base), None
        if tm:
            h, mi, explicit = _hm(tm[1])
            s = s.replace(tm[0], " ")
            if dow is not None:
                if not explicit and h < 12 and re.search(r"night|evening", text.lower()):
                    h += 12
                cmd.cron = (h, mi, dow)
            else:
                cmd.at = _next_at(h, mi, explicit, day, now)
        elif rng or cmd.sun or cmd.at:
            pass
        elif dow is not None:
            return Command("power", error="recurring schedules need a time, e.g. `every day at 6:30am geyser on`")
        elif day:
            return Command("power", error=f"what time {day}?")

        if until:
            he, me, ee = _hm(until[1])
            if cmd.cron:
                h, mi, _ = cmd.cron
                cmd.duration_minutes = ((he * 60 + me) - (h * 60 + mi)) % 1440 or 1440
            else:
                start = cmd.at or now + timedelta(minutes=cmd.in_minutes or 0)
                end = _next_at(he, me, ee, None, start)
                cmd.duration_minutes = round((end - start).total_seconds() / 60)
    except ValueError as e:
        return Command("power", error=str(e))

    am = re.search(r"\b(on|off|toggle|start|stop|kill|enable|disable)\b", s)
    question = "?" in text or re.match(r"\s*(?:is|are|what'?s|check|how)\b", s)
    if am and not (question and not (cmd.at or cmd.cron or cmd.sun or cmd.in_minutes or cmd.duration_minutes)):
        cmd.action = {"start": "on", "enable": "on", "stop": "off", "kill": "off", "disable": "off"}.get(am[1], am[1])
    elif re.search(r"\b(?:status|state|running|is|are|what'?s|check|how)\b", s) or (found and not s.strip(" ?")):
        return Command("status", devices=found)
    else:
        return Command("unknown", error="didn't get that — try `geyser on for 20 min` or send /help")

    if cmd.at and cmd.in_minutes is not None:
        return Command("power", error="use either `at <time>` or `in <duration>`, not both")
    return cmd
