"""Sunrise/sunset schedules (computed offline with astral for the configured location)."""

from datetime import datetime, timedelta

from apscheduler.triggers.base import BaseTrigger
from astral import Observer
from astral.sun import sun

from .config import settings

DAYS = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]


def _expand(dow: str) -> set[int]:
    if dow in ("*", ""):
        return set(range(7))
    out = set()
    for part in dow.split(","):
        if "-" in part:
            a, b = (DAYS.index(x) for x in part.split("-"))
            out |= set(range(a, b + 1))
        else:
            out.add(DAYS.index(part))
    return out


def sun_time(event: str, day, tz) -> datetime:
    s = settings()
    return sun(Observer(latitude=s.latitude, longitude=s.longitude), date=day, tzinfo=tz)[event]


def next_sun(event: str, offset_minutes: float, after: datetime, dow: str = "*") -> datetime | None:
    days = _expand(dow)
    for i in range(0, 9):
        day = (after + timedelta(days=i)).date()
        if day.weekday() not in days:
            continue
        t = sun_time(event, day, after.tzinfo) + timedelta(minutes=offset_minutes)
        if t > after:
            return t
    return None


class SunTrigger(BaseTrigger):
    """Fires at sunrise/sunset (+/- offset) on the given days. Picklable for the SQLite job store."""

    def __init__(self, event: str, offset_minutes: float = 0, dow: str = "*", timezone=None):
        if event not in ("sunrise", "sunset"):
            raise ValueError(f"bad sun event {event!r}")
        self.event, self.offset_minutes, self.dow, self.timezone = event, offset_minutes, dow, timezone

    def get_next_fire_time(self, previous_fire_time, now):
        after = max(now, previous_fire_time + timedelta(seconds=1)) if previous_fire_time else now
        if self.timezone:
            after = after.astimezone(self.timezone)
        return next_sun(self.event, self.offset_minutes, after, self.dow)

    def __str__(self):
        return f"sun[{describe(self.event, self.offset_minutes)}, {self.dow}]"


def describe(event: str, offset_minutes: float) -> str:
    if not offset_minutes:
        return f"at {event}"
    m = abs(offset_minutes)
    amount = f"{m / 60:g} h" if m >= 60 and m % 30 == 0 else f"{m:g} min"
    return f"{amount} {'before' if offset_minutes < 0 else 'after'} {event}"
