"""Generate signed Apple Shortcuts (iPhone + Apple Watch) for the butler HTTPS API.

    uv run python deploy/make_shortcuts.py geyser:🔥 purifier:🌬 lamp:💡

Reads API_TOKEN and PUBLIC_URL from .env. Writes signed files to shortcuts-out/ (gitignored: they
contain the token). Open them on an iPhone (AirDrop, Files, or Telegram) → "Add Shortcut".
Each shows the bot's reply, e.g. "✅ 🔥 Geyser is on · turns off at 6:32 pm".
"""

import plistlib
import subprocess
import sys
import uuid
from pathlib import Path
from urllib.parse import quote

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from butler.config import settings  # noqa: E402

OUT = Path(__file__).resolve().parent.parent / "shortcuts-out"
ORANGE, BLUE, GREY, PURPLE = 4282601983, 463140863, 255, 2071128575


def _token_string(template: str, attachments: dict[int, dict]) -> dict:
    """Text with action-output variables; `\\ufffc` placeholders at the given character offsets."""
    return {"Value": {"string": template,
                      "attachmentsByRange": {f"{{{pos}, 1}}": att for pos, att in attachments.items()}},
            "WFSerializationType": "WFTextTokenString"}


def _output(uuid_: str, name: str) -> dict:
    return {"OutputUUID": uuid_, "Type": "ActionOutput", "OutputName": name}


def _workflow(actions: list, color: int) -> dict:
    return {
        "WFWorkflowActions": actions,
        "WFWorkflowClientVersion": "2605.0.5",
        "WFWorkflowMinimumClientVersion": 900,
        "WFWorkflowMinimumClientVersionString": "900",
        "WFWorkflowIcon": {"WFWorkflowIconStartColor": color, "WFWorkflowIconGlyphNumber": 59511},
        "WFWorkflowTypes": ["Watch", "NCWidget"],
        "WFWorkflowInputContentItemClasses": [],
        "WFWorkflowImportQuestions": [],
        "WFQuickActionSurfaces": [],
        "WFWorkflowHasShortcutInputVariables": False,
    }


def fetch_and_show(url_token: dict) -> list:
    get = str(uuid.uuid4()).upper()
    return [
        {"WFWorkflowActionIdentifier": "is.workflow.actions.downloadurl",
         "WFWorkflowActionParameters": {"UUID": get, "WFURL": url_token, "WFHTTPMethod": "GET"}},
        {"WFWorkflowActionIdentifier": "is.workflow.actions.showresult",
         "WFWorkflowActionParameters": {"Text": _token_string("￼", {0: _output(get, "Contents of URL")})}},
    ]


def simple(url: str, color: int) -> dict:
    return _workflow(fetch_and_show({"Value": {"string": url, "attachmentsByRange": {}},
                                     "WFSerializationType": "WFTextTokenString"}), color)


def ask(base: str, token: str) -> dict:
    """Dictate a command → /cmd. Works for anything the Telegram bot understands."""
    dictate, enc = str(uuid.uuid4()).upper(), str(uuid.uuid4()).upper()
    prefix = f"{base}/cmd?token={quote(token)}&text="
    actions = [
        {"WFWorkflowActionIdentifier": "is.workflow.actions.dictatetext",
         "WFWorkflowActionParameters": {"UUID": dictate}},
        {"WFWorkflowActionIdentifier": "is.workflow.actions.urlencode",
         "WFWorkflowActionParameters": {"UUID": enc, "WFInput": _token_string(
             "￼", {0: _output(dictate, "Dictated Text")})}},
    ] + fetch_and_show(_token_string(prefix + "￼", {len(prefix): _output(enc, "URL Encoded Text")}))
    return _workflow(actions, PURPLE)


def write(name: str, wf: dict) -> Path:
    OUT.mkdir(exist_ok=True)
    raw, signed = OUT / f"{name}.unsigned.shortcut", OUT / f"{name}.shortcut"
    raw.write_bytes(plistlib.dumps(wf, fmt=plistlib.FMT_BINARY))
    subprocess.run(["shortcuts", "sign", "--mode", "anyone", "--input", str(raw), "--output", str(signed)],
                   check=True, capture_output=True)
    raw.unlink()
    return signed


def main(specs: list[str]) -> None:
    s = settings()
    if not (s.api_token and s.public_url):
        sys.exit("API_TOKEN and PUBLIC_URL must be set in .env")
    base, tok = s.public_url.rstrip("/"), quote(s.api_token)
    made = []
    for spec in specs:
        name, _, icon = spec.partition(":")
        label = name.replace("-", " ").title()
        made.append(write(f"{icon} {label}".strip(), simple(f"{base}/toggle/{name}?token={tok}", ORANGE)))
    made.append(write("🏠 Home status", simple(f"{base}/status?token={tok}", BLUE)))
    made.append(write("⏻ All off", simple(f"{base}/cmd?token={tok}&text={quote('turn everything off')}", GREY)))
    made.append(write("🎙 Ask Butler", ask(base, s.api_token)))
    for p in made:
        print(p.name)


if __name__ == "__main__":
    main(sys.argv[1:] or ["geyser:🔥", "purifier:🌬", "lamp:💡"])
