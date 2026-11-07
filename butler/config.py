from functools import lru_cache
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict

ROOT = Path(__file__).resolve().parent.parent


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=ROOT / ".env", extra="ignore")

    devices_file: Path = ROOT / "plugs.json"
    plugs_json: str = ""  # full plugs config as JSON (handy on cloud hosts instead of a file)
    tuya_transport: str = "local"  # "local" (LAN) or "cloud" (Tuya Cloud API)
    tuya_api_key: str = ""  # Access ID — only for cloud transport
    tuya_api_secret: str = ""
    tuya_api_region: str = "in"

    data_dir: Path = Path("~/.butler")
    energy_poll_seconds: int = 60
    latitude: float = 12.9716  # for sunrise/sunset (default: Bengaluru)
    longitude: float = 77.5946
    # "revert": Butler is the only control centre — on/off made elsewhere (app schedules, voice assistants,
    # wall button) is switched straight back. "allow": accept them (away mode still reverts).
    external_changes: str = "allow"
    notify_state_changes: bool = True  # phone notification for changes you didn't make from the chat
    tuya_push: bool = True  # real-time updates via Tuya Message Service (needs it enabled on the project)

    api_token: str = ""  # enables the HTTPS API for Apple Shortcuts / Watch (see deploy/https.sh)
    web_host: str = "127.0.0.1"
    web_port: int = 8787
    public_url: str = ""  # e.g. https://1-2-3-4.sslip.io (your server IP with dashes) — used when generating Shortcuts

    arrive_command: str = "purifier on"  # what /arrive does when your phone joins home Wi-Fi
    arrive_cooldown_minutes: int = 120  # ignore repeat arrivals (Wi-Fi blips) within this window
    leave_command: str = "purifier off"  # what /leave does when your phone drops off home Wi-Fi
    leave_delay_minutes: int = 10  # wait this long; rejoining Wi-Fi meanwhile cancels it (blip)

    telegram_bot_token: str = ""
    telegram_allowed_chat_ids: str = ""

    gemini_api_key: str = ""
    gemini_model: str = "gemini-flash-lite-latest"

    @property
    def data_path(self) -> Path:
        p = self.data_dir.expanduser()
        p.mkdir(parents=True, exist_ok=True)
        return p

    @property
    def allowed_chat_ids(self) -> set[int]:
        return {int(x) for x in self.telegram_allowed_chat_ids.split(",") if x.strip()}


@lru_cache
def settings() -> Settings:
    return Settings()
