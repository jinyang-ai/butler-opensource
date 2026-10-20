"""Natural-language → structured commands via Gemini (free tier). Falls back to nlp.py when unavailable."""

import logging
import re
from datetime import datetime
from functools import lru_cache
from typing import Literal, Optional

from pydantic import BaseModel, Field

from . import nlp
from .config import settings

log = logging.getLogger(__name__)


class Cron(BaseModel):
    hour: int = Field(description="0-23, local time")
    minute: int = Field(description="0-59")
    days: str = Field(description='"*" for every day, "mon-fri", "sat,sun", or comma list like "mon,wed"')


class Sun(BaseModel):
    event: Literal["sunset", "sunrise"]
    offset_minutes: float = Field(0, description="negative = before, positive = after")
    days: Optional[str] = Field(None, description='repeat days like cron.days ("*", "mon-fri", ...); null = just the next one')


class Step(BaseModel):
    intent: Literal["power", "status", "list", "cancel", "energy", "help", "away", "back"]
    devices: list[str] = Field(description="canonical device names; empty means all/unspecified")
    action: Optional[Literal["on", "off", "toggle"]] = None
    at: Optional[str] = Field(None, description='one-off local start time "YYYY-MM-DDTHH:MM"')
    in_minutes: Optional[float] = Field(None, description="start after this many minutes")
    duration_minutes: Optional[float] = Field(None, description="revert to the opposite state after this long")
    cron: Optional[Cron] = Field(None, description="recurring start time")
    sun: Optional[Sun] = Field(None, description="start at sunrise/sunset instead of a clock time")
    hours: Optional[float] = Field(None, description="energy lookback window in hours")
    cancel_target: Optional[str] = Field(None, description='"all", a device name, or a schedule id prefix')
    cancel_action: Optional[Literal["on", "off"]] = Field(None, description='only cancel that device\'s ON or OFF routines')
    away_from: Optional[str] = Field(None, description='away mode start day "YYYY-MM-DD" (null = starting now)')
    away_until: Optional[str] = Field(None, description='away mode return day "YYYY-MM-DD" (routines resume at its start)')
    no_auto_off: bool = Field(False, description='true only if the user explicitly wants it left on with no end ("indefinitely", "until I say")')


class Parsed(BaseModel):
    steps: list[Step]
    clarification: Optional[str] = Field(None, description="set ONLY if the request is ambiguous or not about the plugs")


SYSTEM = """You turn messages into smart-plug commands. Output JSON only, matching the schema.

Devices (canonical name: aliases):
{devices}

Rules:
- One step per distinct action. "geyser on and purifier off" → two steps. "both/all off" → one step listing every device.
- power: switch now unless a time is given. Use `at` for a clock time/date, `in_minutes` for "in/after X",
  `cron` for repeating ("every day", "weekdays", "mondays"). "for X" → duration_minutes (switch back after).
  Ranges/end times: "5-7am" → start 05:00 with duration_minutes 120; "on until 7" → duration from start to 7.
  Leave duration_minutes null when no end is given (a default auto-off applies automatically).
- Sunset/sunrise/dusk/dawn → `sun` (not `at`/`cron`). "daily/every day at sunset" → sun.days "*";
  "30 min before sunset" → offset_minutes -30; "at sunset" with no repeat words → days null (one-off).
- Messages may be Hindi/Hinglish. Number words: ek=1, do=2, teen=3, char/chaar=4, paanch=5, chhe=6, saat=7,
  aath=8, nau=9, das=10, pandrah=15, bees=20, tees=30, aadha ghanta=30 min, dedh ghanta=90 min, dhai ghante=150 min.
  "on kar do/de" = on, "band/off kar do" = off, "... pe band kar" / "... baad band" = off after that long.
- Times without am/pm: pick the next sensible occurrence after now ("at 6" in the evening → 18:00 if 6am passed).
  "morning" = am, "evening/tonight/night" = pm.
- status: questions like "is the geyser on?", "what's running". list: "schedules", "what's planned".
- cancel: "cancel/stop the 6am schedule", "clear all timers".
- CHANGING an existing routine ("change/move the lamp off time to 11:30") = cancel ONLY that kind: cancel step with
  cancel_target=<device> and cancel_action="off" (or "on"), then a power step with the new time. Never cancel the
  device's other routines. energy: usage/kWh/consumption questions;
  "today" = hours since midnight, "this week" = 168.
- away: user is travelling / away / wants all routines paused for a while. Set away_until to the RETURN day
  ("away for 5 days" → today + 5; "back on 25th" → that date; "till Sunday" → that Sunday). away_from only if a
  future start is given ("from Friday"). back: "I'm back", "resume routines", "cancel away mode".
- "it"/"that"/"this one" refers to the device in the [Device the user just used: …] note, if present.
- If you cannot map the message to these intents, return no steps and a short clarification.

Now: {now} ({weekday})."""


class FormatError(ValueError):
    """Gemini produced a malformed field — caller should fall back to the offline parser."""


def _norm_days(days: str | None) -> str | None:
    """Gemini sometimes escapes '*' (→ 'u002a') or says 'daily'."""
    if days is None:
        return None
    d = days.strip().lower().replace("\\u002a", "*").replace("u002a", "*")
    return "*" if d in ("*", "daily", "every day", "everyday", "all", "") else d


def _device_lines() -> str:
    from . import devices

    return "\n".join(f"- {d.name}: {', '.join(d.aliases) or '-'}" for d in devices.registry().values())


@lru_cache
def _client():
    from google import genai

    return genai.Client(api_key=settings().gemini_api_key)


def enabled() -> bool:
    return bool(settings().gemini_api_key)


def _resolve_devices(names: list[str], aliases: dict[str, str]) -> list[str]:
    out = []
    for n in names:
        canon = aliases.get(n.strip().lower())
        if not canon:
            raise ValueError(f"unknown device {n!r}")
        if canon not in out:
            out.append(canon)
    return out


def _to_command(step: Step, aliases: dict[str, str], now: datetime) -> nlp.Command:
    cmd = nlp.Command(step.intent, devices=_resolve_devices(step.devices, aliases))
    if step.intent == "power":
        if not step.action:
            raise ValueError("no on/off action")
        cmd.action = step.action
        cmd.in_minutes = step.in_minutes
        cmd.duration_minutes = step.duration_minutes
        cmd.no_auto_off = step.no_auto_off
        if step.at:
            at = datetime.fromisoformat(step.at)
            at = at.replace(tzinfo=nlp.TZ) if at.tzinfo is None else at.astimezone(nlp.TZ)
            if at <= now:
                raise ValueError(f"{at:%H:%M %d %b} is in the past")
            cmd.at = at
        if step.sun:
            sd = _norm_days(step.sun.days) if step.sun.days else None
            if sd and not re.fullmatch(r"\*|(?:mon|tue|wed|thu|fri|sat|sun)(?:[-,](?:mon|tue|wed|thu|fri|sat|sun))*", sd):
                raise FormatError(f"bad days {sd!r}")
            if sd:
                cmd.sun = (step.sun.event, step.sun.offset_minutes, sd)
            else:
                from .sun import next_sun
                cmd.at = next_sun(step.sun.event, step.sun.offset_minutes, now)
        if step.cron and not step.sun:
            c = step.cron
            days = _norm_days(c.days)
            if not (0 <= c.hour <= 23 and 0 <= c.minute <= 59) or not re.fullmatch(
                r"\*|(?:mon|tue|wed|thu|fri|sat|sun)(?:[-,](?:mon|tue|wed|thu|fri|sat|sun))*", days):
                raise FormatError(f"bad recurring schedule {c}")
            cmd.cron = (c.hour, c.minute, days)
    elif step.intent == "energy":
        cmd.hours = max(step.hours or 24, 0.1)
    elif step.intent == "away":
        def day(v):
            d = datetime.fromisoformat(v[:10])
            return d.replace(tzinfo=nlp.TZ, hour=0, minute=0, second=0, microsecond=0)
        if not step.away_until:
            raise ValueError("Until when? e.g. “away for 5 days” or “away till 25th”")
        cmd.away_until = day(step.away_until)
        cmd.away_from = day(step.away_from) if step.away_from else None
        if cmd.away_from and cmd.away_from <= now:
            cmd.away_from = None
    elif step.intent == "cancel":
        t = (step.cancel_target or "").strip().lower()
        if not t:
            raise ValueError("cancel what?")
        cmd.target = "all" if t == "all" else t
        if t in aliases:
            cmd.devices = [aliases[t]]
        cmd.action = step.cancel_action
    return cmd


def parse(text: str, aliases: dict[str, str], recent: list[str] | None = None) -> list[nlp.Command]:
    """Raises on any API/validation failure so the caller can fall back."""
    from google.genai import types

    now = datetime.now(nlp.TZ)
    if recent:  # resolves "it", "that", "undo that" etc.
        text = f"[Device the user just used: {', '.join(recent)}]\n{text}"
    resp = _client().models.generate_content(
        model=settings().gemini_model,
        contents=text,
        config=types.GenerateContentConfig(
            system_instruction=SYSTEM.format(devices=_device_lines(), now=now.strftime("%Y-%m-%d %H:%M"),
                                             weekday=now.strftime("%A")),
            response_mime_type="application/json",
            response_schema=Parsed,
            temperature=0,
        ),
    )
    parsed = resp.parsed if isinstance(resp.parsed, Parsed) else Parsed.model_validate_json(resp.text)
    log.info("gemini: %r → %s", text, parsed.model_dump_json(exclude_none=True))
    if not parsed.steps:
        return [nlp.Command("unknown", error=parsed.clarification or "didn't get that — send /help")]
    return [_to_command(s, aliases, now) for s in parsed.steps]


def _devices():
    from . import devices
    return devices.registry()


def transcribe(audio: bytes, mime_type: str = "audio/ogg") -> str:
    """Voice note → text (Gemini understands audio natively; English/Hindi/Hinglish all fine)."""
    from google.genai import types

    resp = _client().models.generate_content(
        model=settings().gemini_model,
        contents=[types.Part.from_bytes(data=audio, mime_type=mime_type),
                  "Transcribe this voice note exactly, in English letters (Hinglish stays Hinglish). It's a smart-home "
                  f"command for these devices: {', '.join(d.display for d in _devices().values())}. "
                  "Write numbers as digits (char → 4, das → 10). Output only the transcript, nothing else."],
        config=types.GenerateContentConfig(temperature=0),
    )
    return (resp.text or "").strip().strip('"')
