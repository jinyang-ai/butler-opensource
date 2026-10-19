"""SQLite energy log."""

import sqlite3
import threading
import time

from .config import settings

_lock = threading.Lock()
_conn: sqlite3.Connection | None = None


def conn() -> sqlite3.Connection:
    global _conn
    if _conn is None:
        _conn = sqlite3.connect(settings().data_path / "energy.db", check_same_thread=False)
        _conn.execute(
            "CREATE TABLE IF NOT EXISTS readings (ts REAL, device TEXT, on_ INTEGER, power_w REAL, voltage_v REAL, current_a REAL)"
        )
        _conn.execute("CREATE INDEX IF NOT EXISTS readings_dev_ts ON readings(device, ts)")
        _conn.execute("CREATE TABLE IF NOT EXISTS energy (ts REAL, device TEXT, kwh REAL)")  # pushed add_ele increments
        _conn.execute("CREATE TABLE IF NOT EXISTS states (ts REAL, device TEXT, on_ INTEGER)")  # on/off transitions
        _conn.execute("CREATE TABLE IF NOT EXISTS heat (ts REAL, device TEXT, minutes REAL)")  # heat-up times
    return _conn


def record(status) -> None:
    with _lock:
        c = conn()
        c.execute(
            "INSERT INTO readings VALUES (?,?,?,?,?,?)",
            (time.time(), status.name, int(status.on), status.power_w, status.voltage_v, status.current_a),
        )
        c.commit()


def summary(device: str, hours: float) -> dict:
    """On-time and energy (trapezoid over samples) for the last `hours`."""
    since = time.time() - hours * 3600
    with _lock:
        rows = conn().execute(
            "SELECT ts, on_, power_w FROM readings WHERE device=? AND ts>=? ORDER BY ts", (device, since)
        ).fetchall()
    on_s = wh = 0.0
    peak = None
    for (t0, on0, p0), (t1, _, p1) in zip(rows, rows[1:]):
        dt = t1 - t0
        if dt > 600:  # gap (daemon down); don't extrapolate
            continue
        if on0:
            on_s += dt
        if p0 is not None and p1 is not None:
            wh += (p0 + p1) / 2 * dt / 3600
    powers = [r[2] for r in rows if r[2] is not None]
    if powers:
        peak = max(powers)
    return {
        "device": device,
        "hours": hours,
        "samples": len(rows),
        "on_minutes": round(on_s / 60, 1),
        "energy_kwh": round(wh / 1000, 3) if powers else None,
        "peak_w": peak,
    }


def record_energy(device: str, kwh: float) -> None:
    with _lock:
        c = conn()
        c.execute("INSERT INTO energy VALUES (?,?,?)", (time.time(), device, kwh))
        c.commit()


def record_state(device: str, on: bool) -> None:
    with _lock:
        c = conn()
        last = c.execute("SELECT on_ FROM states WHERE device=? ORDER BY ts DESC LIMIT 1", (device,)).fetchone()
        if last is None or bool(last[0]) != on:
            c.execute("INSERT INTO states VALUES (?,?,?)", (time.time(), device, int(on)))
            c.commit()


def _on_seconds(device: str, start: float, end: float) -> float:
    with _lock:
        before = conn().execute("SELECT on_ FROM states WHERE device=? AND ts<? ORDER BY ts DESC LIMIT 1",
                                (device, start)).fetchone()
        rows = conn().execute("SELECT ts, on_ FROM states WHERE device=? AND ts>=? AND ts<? ORDER BY ts",
                              (device, start, end)).fetchall()
    on, t, total = bool(before[0]) if before else False, start, 0.0
    for ts, st in rows:
        if on:
            total += ts - t
        on, t = bool(st), ts
    if on:
        total += min(end, time.time()) - t
    return max(total, 0.0)


def _kwh(device: str, start: float, end: float) -> float:
    with _lock:
        r = conn().execute("SELECT SUM(kwh) FROM energy WHERE device=? AND ts>=? AND ts<?", (device, start, end)).fetchone()
    return r[0] or 0.0


def tracking_since(device: str) -> float | None:
    with _lock:
        r = conn().execute("SELECT MIN(ts) FROM (SELECT ts FROM energy WHERE device=? UNION ALL "
                           "SELECT ts FROM states WHERE device=?)", (device, device)).fetchone()
    return r[0]


def period(device: str, start: float, end: float) -> dict:
    return {"kwh": round(_kwh(device, start, end), 3), "on_min": round(_on_seconds(device, start, end) / 60)}


def record_heat(device: str, minutes: float) -> None:
    with _lock:
        c = conn()
        c.execute("INSERT INTO heat VALUES (?,?,?)", (time.time(), device, minutes))
        c.commit()


def avg_heat_minutes(device: str, last: int = 10) -> float | None:
    with _lock:
        rows = conn().execute("SELECT minutes FROM heat WHERE device=? AND minutes>0 ORDER BY ts DESC LIMIT ?",
                              (device, last)).fetchall()
    return sum(r[0] for r in rows) / len(rows) if rows else None


def last_on_since(device: str) -> float | None:
    """When the device last switched on, if it's currently on according to the log."""
    with _lock:
        r = conn().execute("SELECT ts, on_ FROM states WHERE device=? ORDER BY ts DESC LIMIT 1", (device,)).fetchone()
    return r[0] if r and r[1] else None


def dump(device: str) -> dict:
    """Every stored row for a device (raw, epoch seconds)."""
    with _lock:
        c = conn()
        q = lambda sql: c.execute(sql, (device,)).fetchall()
        return {
            "states": q("SELECT ts, on_ FROM states WHERE device=? ORDER BY ts"),
            "energy": q("SELECT ts, kwh FROM energy WHERE device=? ORDER BY ts"),
            "readings": q("SELECT ts, on_, power_w, voltage_v, current_a FROM readings WHERE device=? ORDER BY ts"),
            "heat": q("SELECT ts, minutes FROM heat WHERE device=? ORDER BY ts"),
        }
