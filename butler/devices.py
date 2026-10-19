"""Tuya plug control: local LAN (tinytuya, needs local_key + same network) or Tuya Cloud API (works from anywhere)."""

import json
import re
import threading
from dataclasses import asdict, dataclass, field

import tinytuya

from .config import settings


class PlugError(RuntimeError):
    pass


@dataclass
class PlugStatus:
    name: str
    on: bool
    power_w: float | None = None
    voltage_v: float | None = None
    current_a: float | None = None
    today_kwh: float | None = None

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class Device:
    name: str
    aliases: list[str] = field(default_factory=list)
    auto_off_minutes: float | None = None  # default run time after any ON without an explicit off time
    max_on_minutes: float | None = None  # hard safety cap, applies even to explicit ranges
    off_when_heated: bool = False  # heater on a thermostat: switch off once power drops (water's hot)
    icon: str = "🔌"
    label: str | None = None  # display name; defaults to the name, title-cased

    def __post_init__(self):
        self.cached: PlugStatus | None = None  # last known state (from pushes/commands/reads) — no API cost

    @property
    def display(self) -> str:
        return self.label or self.name.replace("-", " ").title()

    def remember(self, st: PlugStatus) -> PlugStatus:
        self.cached = st
        return st

    def state(self) -> PlugStatus:
        """Last known state; only reads the plug if we know nothing yet."""
        return self.cached or self.status()

    def note(self, on: bool | None = None, power_w: float | None = None) -> None:
        """Update the cached state from a push."""
        st = self.cached or PlugStatus(self.name, bool(on))
        if on is not None:
            st.on = on
            if not on:
                st.power_w = 0.0 if st.power_w is not None else None
        if power_w is not None:
            st.power_w = power_w
        self.cached = st

    def status(self) -> PlugStatus:
        raise NotImplementedError

    def set(self, on: bool) -> PlugStatus:
        raise NotImplementedError

    def apply(self, action: str) -> PlugStatus:
        if action == "on":
            return self.set(True)
        if action == "off":
            return self.set(False)
        if action == "toggle":
            return self.set(not self.state().on)
        raise PlugError(f"unknown action {action!r}")


class TuyaPlug(Device):
    DEFAULT_DPS = {"switch": 1, "energy_add": 17, "current_ma": 18, "power_dw": 19, "voltage_dv": 20}

    def __init__(self, name, device_id, ip, local_key, version=3.3, dps=None, **kw):
        super().__init__(name, **kw)
        self.device_id = device_id
        self.dps = {**self.DEFAULT_DPS, **(dps or {})}
        self._lock = threading.Lock()
        self._metering: bool | None = None  # learned on first status
        self._dev = tinytuya.OutletDevice(device_id, ip, local_key)
        self._dev.set_version(float(version))
        self._dev.set_socketTimeout(4)
        self._dev.set_socketRetryLimit(2)

    def _check(self, resp):
        if isinstance(resp, dict) and "Error" in resp:
            raise PlugError(f"{self.name}: {resp.get('Error')} (code {resp.get('Err')})")
        return resp

    def status(self) -> PlugStatus:
        d = self.dps
        with self._lock:
            if self._metering is not False:  # ask the plug to refresh metering DPs (skip on plugs without a meter)
                try:
                    self._dev.updatedps([d["current_ma"], d["power_dw"], d["voltage_dv"]], nowait=True)
                except Exception:
                    pass
            dps = (self._check(self._dev.status()) or {}).get("dps", {})
            if self._metering is None:
                self._metering = str(d["power_dw"]) in dps
        sw = str(d["switch"])
        if sw not in dps:
            raise PlugError(f"{self.name}: switch DP {sw} missing from status {dps}")

        def scaled(key, div):
            v = dps.get(str(d[key]))
            return round(v / div, 3) if isinstance(v, (int, float)) else None

        return self.remember(PlugStatus(
            name=self.name,
            on=bool(dps[sw]),
            power_w=scaled("power_dw", 10),
            voltage_v=scaled("voltage_dv", 10),
            current_a=scaled("current_ma", 1000),
        ))

    def set(self, on: bool) -> PlugStatus:
        with self._lock:
            self._check(self._dev.set_status(on, self.dps["switch"]))
        return self.status()


def tuya_creds() -> tuple[str, str, str] | None:
    """(access_id, secret, region) from env, else the wizard's tinytuya.json."""
    s = settings()
    if s.tuya_api_key:
        return s.tuya_api_key, s.tuya_api_secret, s.tuya_api_region
    try:
        c = json.loads((settings().devices_file.parent / "tinytuya.json").read_text())
        return c["apiKey"], c["apiSecret"], c.get("apiRegion", "in")
    except Exception:
        return None


class TuyaCloudPlug(Device):
    """Same plug via Tuya's cloud API — for running the bot off your home network (e.g. a cloud VM)."""

    _client = None
    _client_lock = threading.Lock()

    def __init__(self, name, device_id, switch_code="switch_1", ip=None, local_key=None, version=None, dps=None, **kw):
        super().__init__(name, **kw)
        self.device_id = device_id
        self.switch_code = switch_code

    @classmethod
    def client(cls):
        with cls._client_lock:
            if cls._client is None:
                s = settings()
                if s.tuya_api_key:
                    any_id = next((c.get("device_id") for c in _load_config() if c.get("device_id")), "")
                    cls._client = tinytuya.Cloud(apiRegion=s.tuya_api_region, apiKey=s.tuya_api_key,
                                                 apiSecret=s.tuya_api_secret, apiDeviceID=any_id)
                else:  # fall back to tinytuya.json written by the wizard
                    cls._client = tinytuya.Cloud(configFile=str(settings().devices_file.parent / "tinytuya.json"))
            return cls._client

    def _check(self, resp):
        if not isinstance(resp, dict) or not resp.get("success"):
            msg = resp.get("msg") or resp.get("Error") if isinstance(resp, dict) else resp
            raise PlugError(f"{self.name}: Tuya cloud error: {msg}")
        return resp

    def status(self) -> PlugStatus:
        codes = {x["code"]: x["value"] for x in self._check(self.client().getstatus(self.device_id))["result"]}
        if self.switch_code not in codes:
            raise PlugError(f"{self.name}: {self.switch_code} missing from cloud status {codes}")

        def scaled(code, div):
            v = codes.get(code)
            return round(v / div, 3) if isinstance(v, (int, float)) else None

        return self.remember(PlugStatus(
            name=self.name,
            on=bool(codes[self.switch_code]),
            power_w=scaled("cur_power", 10),
            voltage_v=scaled("cur_voltage", 10),
            current_a=scaled("cur_current", 1000),
        ))

    def set(self, on: bool) -> PlugStatus:
        self._check(self.client().sendcommand(self.device_id, {"commands": [{"code": self.switch_code, "value": on}]}))
        self.note(on=on)  # one API call; reading it back would only echo the cloud's cache
        return self.cached


TYPES = {"local": TuyaPlug, "cloud": TuyaCloudPlug}

_registry: dict[str, Device] | None = None


def _base_config() -> list[dict]:
    s = settings()
    if s.plugs_json:
        return json.loads(s.plugs_json)
    if not s.devices_file.exists():
        raise PlugError(f"{s.devices_file} not found — copy plugs.example.json and fill it in")
    return json.loads(s.devices_file.read_text())


# Devices added/edited from Telegram live in the data dir, so deploys (which overwrite plugs.json) keep them.
def _changes_file():
    return settings().data_path / "plugs_changes.json"


def _changes() -> dict:
    try:
        return json.loads(_changes_file().read_text())
    except Exception:
        return {"added": [], "overrides": {}, "removed": []}


def _save_changes(ch: dict) -> None:
    _changes_file().write_text(json.dumps(ch, indent=2, ensure_ascii=False))
    reload()


def _load_config() -> list[dict]:
    ch = _changes()
    out = []
    for cfg in _base_config() + ch.get("added", []):
        if cfg["name"] in ch.get("removed", []):
            continue
        out.append({**cfg, **ch.get("overrides", {}).get(cfg["name"], {})})
    return out


def registry() -> dict[str, Device]:
    global _registry
    if _registry is None:
        reg = {}
        for cfg in _load_config():
            cfg = dict(cfg)
            cfg.pop("type", None)
            cls = TYPES[cfg.pop("transport", settings().tuya_transport)]
            reg[cfg["name"]] = cls(**cfg)
        _registry = reg
    return _registry


def reload() -> None:
    global _registry
    _registry = None


def slug(label: str) -> str:
    base = re.sub(r"[^a-z0-9]+", "-", label.lower()).strip("-") or "device"
    name, i = base, 2
    while name in {c["name"] for c in _base_config() + _changes().get("added", [])}:
        name, i = f"{base}-{i}", i + 1
    return name


SWITCH_CATEGORIES = {"cz": "plug", "pc": "power strip", "kg": "switch", "dj": "light", "tdq": "breaker", "fs": "fan"}
SWITCH_CODES = ("switch_1", "switch", "switch_led", "switch_on")


def discover_new() -> list[dict]:
    """Devices in the linked Tuya app account that aren't configured yet and have an on/off switch."""
    client = TuyaCloudPlug.client()
    known = {c.get("device_id") for c in _load_config()}
    out = []
    for d in client.getdevices(verbose=False) or []:
        if not isinstance(d, dict) or d.get("id") in known or d.get("category") not in SWITCH_CATEGORIES:
            continue
        try:
            codes = [x["code"] for x in client.getstatus(d["id"]).get("result", [])]
        except Exception:
            codes = []
        code = next((c for c in SWITCH_CODES if c in codes), None)
        if code:
            out.append({"id": d["id"], "app_name": d.get("name") or "New device", "product": d.get("product_name") or "",
                        "kind": SWITCH_CATEGORIES[d["category"]], "switch_code": code})
    return out


def add_device(device_id: str, app_name: str, switch_code: str, label: str, icon: str,
               auto_off_minutes: float | None) -> Device:
    ch = _changes()
    name = slug(label)
    cfg = {"name": name, "label": label, "icon": icon, "aliases": sorted({label.lower(), app_name.lower()}),
           "device_id": device_id, "switch_code": switch_code, "transport": "cloud"}
    if auto_off_minutes:
        cfg["auto_off_minutes"] = auto_off_minutes
    ch.setdefault("added", []).append(cfg)
    _save_changes(ch)
    return registry()[name]


def update_device(name: str, **fields) -> None:
    ch = _changes()
    if "label" in fields:
        aliases = set(get(name).aliases) | {fields["label"].lower()}
        fields["aliases"] = sorted(aliases)
    ch.setdefault("overrides", {}).setdefault(name, {}).update(fields)
    _save_changes(ch)


def remove_device(name: str) -> None:
    ch = _changes()
    before = len(ch.get("added", []))
    ch["added"] = [c for c in ch.get("added", []) if c["name"] != name]
    if len(ch["added"]) == before:  # came from plugs.json → hide it
        ch.setdefault("removed", []).append(name)
    ch.get("overrides", {}).pop(name, None)
    _save_changes(ch)


def get(name: str) -> Device:
    """Resolve a device by name or alias (case-insensitive)."""
    key = name.strip().lower()
    for dev in registry().values():
        if key == dev.name.lower() or key in (a.lower() for a in dev.aliases):
            return dev
    raise PlugError(f"unknown device {name!r}; known: {', '.join(registry())}")
