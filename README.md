# 🏠 Butler: a Telegram smart-home bot for Tuya / Smart Life plugs

Butler controls your Tuya-based smart plugs (Smart Life, Tuya Smart, and the many brands built on them) from **one tidy Telegram chat**. It handles taps, plain English or Hinglish, voice notes, and Apple Watch buttons. It runs 24/7 on a **free** Oracle Cloud VM and uses **no paid APIs**.

> **Fastest setup:** clone this repo and tell your AI coding agent (Claude Code, Codex, Cursor, …):
> *"Set up Butler for me by following AGENTS.md."*
> It walks you through everything. You only do the logins and phone taps.
> Prefer doing it yourself? Follow [AGENTS.md](AGENTS.md) step by step; it's written for humans too.

## What you get
| | |
|---|---|
| **One-message dashboard** | Each device's state with on/off buttons, the last action with ↩️ Undo, plus 📅 calendar · ⚡ energy · ⚙️ devices · 🔄. The chat stays clean: commands you type are handled and deleted. |
| **Natural language and voice** | "geyser on 6–7am tomorrow", "purifier char minute ke liye on kar do", "turn it off", or a voice note. Uses Gemini's free tier, with an offline parser as fallback. |
| **Schedules** | One-offs, timers, ranges, weekday routines, and **sunrise/sunset** ("lamp on at sunset every day"), recalculated daily for your city. |
| **Away mode** | "away for 5 days" or "back on 25th" pauses routines, switches everything off, and resumes on your return day. |
| **Real-time sync** | Changes from the Smart Life app, Google/Alexa, or a plug's button appear within about a second, with a phone notification. |
| **Safety rules** | Per device: default auto-off (e.g. geyser 30 min), a hard cap (e.g. 2 h), applied even when switched on from the app or the wall. |
| **"Water's hot" detection** | For metered heater plugs: when the thermostat cuts out, Butler switches off within a second and tells you the heat-up time. |
| **Energy** | Today, this week, totals and 7-day bars (kWh on metered plugs, on-time on all). 📋 exports the full history as JSON for your own AI analysis. |
| **Add devices from chat** | Pair a plug in Smart Life, then ⚙️ → ➕ in Telegram to name it, pick an icon, and set auto-off. |
| **Apple Watch / Siri** | Optional signed Shortcuts: one-tap toggles, status, all off, and "Ask Butler" by dictation. |
| **Ops** | `/logs` (copyable plus a file), `/export`, `/clean`, alerts if the Tuya trial lapses, about 1–2 API calls per heater cycle. |

## How it works
```
 Telegram ──► Butler (Oracle Always Free VM, systemd)
                │  python-telegram-bot · APScheduler (SQLite) · Gemini (optional)
                ├─► Tuya Cloud API ──────────── switch / read plugs
                └─◄ Tuya Message Service (push) ─ live on/off, watts, kWh
 Apple Watch ─► HTTPS (Caddy + Let's Encrypt) ─► Butler      (optional)
```
- **Cloud mode** (on the VM) controls plugs through Tuya's cloud. **Local mode** (a machine at home) talks to plugs directly over Wi-Fi.
- State is cached from pushes, so the dashboard costs no API calls. Tuya's free trial credit covers typical use many times over.

## Costs
| Piece | Cost |
|---|---|
| Oracle Cloud Always Free VM (Pay As You Go upgrade recommended) | ₹0 / $0 |
| Tuya IoT Core trial (renew free when it expires) | $0 |
| Telegram bot, Let's Encrypt, sslip.io | $0 |
| Gemini (optional, free tier; prompts may be used by Google) | $0 |

## Code map
| Path | What |
|---|---|
| `butler/bot.py` | Telegram UI: dashboard, views, flows, voice, /commands |
| `butler/commands.py` | executes commands, device rules, replies, calendar, energy, export |
| `butler/llm.py` · `butler/nlp.py` | Gemini structured parsing · offline fallback parser |
| `butler/scheduler.py` | jobs, auto-off and cap, water's-hot, away mode, change detection |
| `butler/devices.py` | registry, local/cloud transports, state cache, discovery |
| `butler/realtime.py` | Tuya Message Service consumer (AES-GCM) |
| `butler/sun.py` · `butler/store.py` · `butler/web.py` | sunrise/sunset · SQLite history · Watch API |
| `deploy/` | VM provisioning, deploy, HTTPS, Shortcuts generator, macOS launchd |

## Develop
```bash
uv sync && uv run pytest -q
```

## License
MIT. Butler isn't affiliated with Tuya, Smart Life, Telegram, Oracle, Google or Apple.
