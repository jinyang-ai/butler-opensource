# Butler: setup playbook for AI coding agents

You're helping someone set up **Butler**, a Telegram bot that controls their Tuya / Smart Life smart plugs. It has schedules, natural language, voice notes, energy tracking, real-time sync, safety auto-offs, and optional Apple Watch buttons. It runs 24/7 on a free Oracle Cloud VM.

This playbook is the **fastest proven path**: every gotcha below was hit in a real setup and has a fix here. Follow it in order. Steps marked **👤** need the human: logins, payments, phone actions. Ask them to do it, wait, then verify it yourself.

## Ground rules (must follow)
- **Never ask for, type or store passwords or card details.** The human signs in and pays in their own browser. If a login page appears in a browser you control, hand it to them.
- **Ask before creating any key or secret,** and keep secrets out of chat. Have the human paste them into `.env`. Verify a secret's format without printing it, e.g. `grep -c '^TELEGRAM_BOT_TOKEN=[0-9]\+:' .env`.
- **Never commit** `.env`, `plugs.json`, `tinytuya.json`, `devices.json` or `snapshot.json`. They're gitignored, so keep it that way.
- **Run commands from the repo root** (`uv run …` outside it fails with "No module named …").
- **Only one running copy per Telegram bot token.** Two copies fight over updates and Tuya pushes.
- Prefer the free options everywhere. Tell the human about any cost before it happens.

## 0. Ask the human first (one message)
1. **Which plugs/devices** do they have, and what's each one for (geyser/water heater, purifier, lamp, …)? Any must be in the **Smart Life** or **Tuya Smart** app. Brand apps like ZEB Home, Wipro or HomeMate are usually Tuya underneath, but **re-pair the devices in Smart Life**, because linking brand apps to a Tuya developer project is unreliable.
2. **Their Smart Life region** (Me → Settings → Account and Security → Region). The Tuya data center must match: India → `in`, US West → `us`, Central Europe → `eu`, China → `cn`, and so on.
3. **City** (for sunrise/sunset) and **timezone**.
4. **Hosting:** the free Oracle Cloud VM (recommended, works from anywhere), or a computer that's always on at home (Mac, Pi or Linux box on the same Wi-Fi).
5. Optional: **natural language and voice** (a free Gemini key), and **Apple Watch / Siri buttons**.

## 1. Local workspace
```bash
git clone <this repo> butler && cd butler
uv sync                     # install uv first if needed: https://docs.astral.sh/uv/
cp .env.example .env
uv run pytest -q            # all green
```

## 2. Tuya developer project 👤 (free, ~10 min)
Guide the human through it. You can drive their browser if you have one, but they must log in themselves.
1. Sign up at **https://platform.tuya.com**, then **Cloud → Development → Create Cloud Project**.
   - Industry: Smart Home. Development method: **Smart Home**. Data center: **the one matching their app region** (step 0.2).
   - The free trial allows **only one data center per project**, so choose correctly.
   - If asked to authorize API services, keep the defaults (IoT Core, Authorization).
2. **Devices → Link App Account → Add App Account → "Tuya App Account Authorization"** shows a QR code.
   👤 In Smart Life: **Me → scan icon (top right) → scan → tap Confirm/Authorize**.
   - The link can take ~1 minute to appear under *Devices → All Devices*. If it still shows 0 accounts, they probably didn't tap Confirm, or the QR expired: generate a new one.
3. **Cloud Services → IoT Core** must say **In service**. Error `28841001 IoT Core service is not subscribed` means they need to subscribe to the free trial there.
4. **Message Service** (needed for real-time sync, water's-hot detection and app-change notifications):
   - **Message Service** tab → **Enable**. Type: Message Queue. Encryption: **AES-GCM**. The dialog mentions fees beyond quota, but the trial has no payment method, so it can't charge.
   - **Messaging Rules → Production Environment → Create Messaging Rules → Add Message Filtering Rule**: BizCode **In** `devicePropertyMessage`, `deviceOnline`, `deviceOffline` → **Release Rule → OK**.
   - Then switch on the **"rules for the production environment"** toggle and confirm. **Without a released and enabled rule, nothing is pushed.**
5. 👤 Note the **Access ID** and **Access Secret** (project **Overview → Authorization Key**). They go into the wizard next, typed by the human.

## 3. Device IDs and keys
```bash
uv run python -m tinytuya wizard     # must run in the repo root
```
👤 The human enters: the Access ID, the Secret, any one device ID (from Tuya → Devices → All Devices) and the region code, then **Y** to every question. This writes `tinytuya.json` (cloud credentials), `devices.json` and `snapshot.json`.

**You** then build `plugs.json` from `snapshot.json` and `devices.json`, without printing the `key` fields. One entry per device, using `plugs.example.json` as the shape:
- `name`: a short slug. `label`, `icon` and `aliases`: what the human will call it.
- `device_id` = `id`, `ip`, `local_key` = `key`, `version` = `ver` (used for LAN control).
- **Rules:**
  - Heaters and geysers: `auto_off_minutes: 30`, `max_on_minutes: 120`, and `off_when_heated: true` if the plug has a power meter.
  - Everything else: no rules unless the human asks.
- **Metered plugs:** `devices.json` → `mapping` contains `cur_power` / `add_ele`. Only those can show watts and kWh, and use water's-hot detection.

Verify:
```bash
uv run butler check                         # LAN (must be on the same Wi-Fi)
TUYA_TRANSPORT=cloud uv run butler check     # via Tuya cloud
```
Both should print `{"name": …, "on": …}` for every device. Local errors 901/914 mean a wrong key or version: try `version` 3.4 or 3.5.

Optional cloud write test (switches a plug, so ask first): use `tinytuya.Cloud().sendcommand(id, {"commands":[{"code":"switch_1","value":True}]})`.

## 4. Telegram bot 👤
1. In Telegram: **@BotFather → /newbot** → pick a name. 👤 The human pastes the token into `.env` as `TELEGRAM_BOT_TOKEN=`.
   On macOS, `open -e .env` opens TextEdit. Tell them to use Format → Make Plain Text if quotes get curly.
2. Start the bot temporarily (`uv run butler serve`) and have them message it. It replies **"Not authorised. Your chat ID is N"**. Put N in `.env` as `TELEGRAM_ALLOWED_CHAT_IDS=`, then stop the bot.
3. Optional **Gemini** (free): 👤 create a key at https://aistudio.google.com/apikey in a project **without billing** → `GEMINI_API_KEY=`. Without it, an offline parser handles commands and voice notes are off.
4. Set `LATITUDE`/`LONGITUDE` for their city. Set `TUYA_API_REGION` to the data center code. `TUYA_API_KEY` and `TUYA_API_SECRET` can stay empty: `tinytuya.json` is used.

## 5a. Hosting: Oracle Cloud Always Free (recommended)
**Create the VM 👤:**
1. Sign up at **https://signup.cloud.oracle.com**. The home region is permanent: pick one near them. Card verification happens in their own browser.
2. Create an SSH key for them:
   ```bash
   ssh-keygen -t ed25519 -N "" -f ~/.ssh/butler_vm -C butler-vm
   pbcopy < ~/.ssh/butler_vm.pub     # macOS
   ```
3. 👤 **Compute → Instances → Create instance:**
   - Image: **Canonical Ubuntu 24.04**.
   - Shape: **Change shape → Specialty and previous generation → VM.Standard.E2.1.Micro** (Always Free). If that's out of capacity: Ampere A1 Flex, 1 OCPU, 6 GB.
   - Security step: change nothing. Storage: defaults.
   - **Networking on a new account:** choose **Create new virtual cloud network** and **Create new public subnet**. The "assign public IPv4" toggle stays **greyed out**. That's expected, so continue.
   - SSH keys: **Paste public key**, then **Create**.
4. 👤 When it's Running, add a public IP: instance → **Networking → Attached VNICs → (VNIC) → IP administration / IPv4 addresses → ⋮ Edit → Ephemeral public IP**. Public IPs are free on Oracle.
5. 👤 **Optional but recommended:** upgrade the tenancy to **Pay As You Go** (Billing → Upgrade and Manage Payment). Always Free resources stay $0, but Oracle otherwise reclaims "idle" free VMs (under 20% CPU for 7 days), and this bot counts as idle. Then add a **$1/month budget alert** (Billing → Budgets).

**Provision and deploy** (you):
```bash
export BUTLER_HOST=ubuntu@<public-ip>
./deploy/provision-oracle.sh     # timezone (BUTLER_TZ, default Asia/Kolkata), swap, uv, systemd unit "butler"
./deploy/oracle.sh               # syncs code + .env + plugs.json + tinytuya.json, starts the service
./deploy/oracle.sh logs          # expect: "tuya push connected", "bot running"
```
The server runs with `TUYA_TRANSPORT=cloud` and `DATA_DIR=~/butler-data` (both set in the systemd unit).

## 5b. Hosting: a computer at home (alternative)
```bash
uv run butler serve                  # LAN control (TUYA_TRANSPORT=local is the default)
./deploy/install-launchd.sh          # macOS: run in the background at login. The Mac must stay awake.
```
On Linux, copy the systemd unit from `deploy/provision-oracle.sh`.

## 6. Verify end to end
👤 In Telegram, send `/home`. **One dashboard message** should appear with a button per device.
- Tap a device's **Turn on**, then **Turn off**. The dashboard updates in place.
- 👤 Toggle a plug in the **Smart Life app**. Within about a second the dashboard shows "… turned on in the app" and their phone gets a notification.
  - If that doesn't happen, recheck step 2.4 and look for `tuya push connected` in the logs.
  - No notification on iPhone: the chat is muted, or iOS Settings → Telegram → Notifications has Lock Screen / Banners unticked.
- Type: `geyser on 6-7am tomorrow` → the reply comes with ↩️ Undo, and 📅 shows it in the calendar.
- `/logs` returns a copyable log. `/clean` wipes the chat down to the dashboard.

## 7. Optional: Apple Watch / Siri buttons
1. 👤 Oracle: **Networking → Virtual cloud networks → (VCN) → Security Lists → Default → Add Ingress Rules**:
   - CIDR `0.0.0.0/0`, TCP, destination port **80**
   - **+ Another Ingress Rule**, the same with destination port **443**
2. Then run:
   ```bash
   ./deploy/https.sh            # Caddy + Let's Encrypt at https://<ip-with-dashes>.sslip.io, opens VM iptables
   ```
3. Ask before generating the token, then:
   ```bash
   python3 -c 'import secrets;print(secrets.token_urlsafe(24))'   # → .env API_TOKEN=… (don't echo it in chat)
   # .env: PUBLIC_URL=https://<ip-with-dashes>.sslip.io
   ./deploy/oracle.sh
   curl -s https://<ip-with-dashes>.sslip.io/health            # → ok
   uv run python deploy/make_shortcuts.py geyser:🔥 purifier:🌬   # signed .shortcut files in shortcuts-out/ (macOS only)
   ```
4. 👤 Send the files to their iPhone and open each → **Add Shortcut**. The first run asks to allow the domain: **Always Allow**. On the Watch they appear in the Shortcuts app, and can be added to a watch face as complications.

## 8. Hand over
Tell the human:
- **Day-to-day:** use the dashboard buttons, or type or speak commands. The examples are in `/help`.
- **Renew the Tuya IoT Core trial** before it expires: platform.tuya.com → Cloud → Cloud Services → IoT Core → View Details → **Extend Trial Period**. The bot also warns on Telegram if it lapses.
- **Quota:** Tuya's free trial gives about $0.20/month of credit. Typical use is about 5–10%.
- **New plug:** pair it in Smart Life, then in Telegram go to ⚙️ → ➕ Add a device.
- **Back up** the four secret files (`.env`, `tinytuya.json`, `plugs.json`, `~/.ssh/butler_vm`) somewhere safe, such as a password manager.

## Reference
| Setting (.env) | Meaning |
|---|---|
| `TELEGRAM_BOT_TOKEN`, `TELEGRAM_ALLOWED_CHAT_IDS` | required |
| `GEMINI_API_KEY`, `GEMINI_MODEL` | natural language and voice (optional, free tier) |
| `TUYA_TRANSPORT` | `local` (LAN) or `cloud` (the server sets `cloud`) |
| `TUYA_API_KEY/SECRET/REGION` | cloud credentials. If empty, `tinytuya.json` is used |
| `TUYA_PUSH` | real-time Message Service stream (default on) |
| `NOTIFY_STATE_CHANGES` | phone notification for changes you didn't make |
| `ENERGY_POLL_SECONDS` | fallback poll. While pushes flow, real reads happen only every 6 h |
| `LATITUDE`, `LONGITUDE` | for sunrise/sunset schedules |
| `API_TOKEN`, `PUBLIC_URL` | Watch/Shortcuts API (optional) |
| `DATA_DIR` | schedules, energy history, logs, Telegram-added devices |

| plugs.json field | Meaning |
|---|---|
| `auto_off_minutes` | turn off this long after any ON without an end time (app and button included) |
| `max_on_minutes` | hard cap, even for explicit durations |
| `off_when_heated` | metered heaters: switch off when the thermostat cuts out (power drops after heating) |
| `transport` | per-device override: `local` / `cloud` |
| `switch_code` | cloud switch datapoint (default `switch_1`; bulbs often use `switch_led`) |

Known limits:
- Plugs without a meter show on-time only.
- Proactive alerts are Telegram-only.
- If Tuya's cloud is down, cloud mode is down; local mode keeps working on the LAN.
