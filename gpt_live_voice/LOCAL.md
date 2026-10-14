# Local Windows development (slim voice)

Run GPT-Live Voice on this PC. Voice PE talks to your LAN IP. Home Assistant
tools come later — step 1 is a stable, low-latency conversation only.

## 1. App on Windows

```powershell
cd gpt_live_voice
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -e .
copy .env.example .env
# edit .env → set OPENAI_API_KEY and keep SLIM_VOICE=true
.\run_local.ps1
```

Allow TCP **8080** in Windows Firewall. Note your PC IPv4 (e.g. `192.168.178.50`).

## 2. Voice PE → fixed IP + Windows app

Use stub [`esphome-builder.local-windows.yaml`](https://github.com/SandorDrift23/home-assistant-voice-pe/blob/feature/gpt-live-voice-pe/esphome-builder.local-windows.yaml) in the firmware repo:

1. Set `va_url` to `ws://YOUR_PC_IP:8080/`
2. Put Wi‑Fi + `static_ip` / gateway / subnet / dns in ESPHome secrets
3. Clean Build Files → Install
4. Confirm log: `WS connected` and hello from the app

Secrets example:

```yaml
wifi_ssid: "…"
wifi_password: "…"
ota_password: "…"
api_key: "…"
static_ip: "192.168.178.52"
gateway: "192.168.178.1"
subnet: "255.255.255.0"
dns1: "192.168.178.1"
dns2: "0.0.0.0"
```

## 3. Test

1. Stop the HA add-on (only one listener on the PE’s `va_url`).
2. Start `run_local.ps1`.
3. Wake the PE and talk. Expect user/assistant transcripts in the console.
4. Echo probe **off**. No weather/tools until step 2.

## Slim mode (`SLIM_VOICE=true`)

Forces / defaults:

| Setting | Value | Why |
|---------|--------|-----|
| gpt_live only | yes | Target model |
| web search | off | Avoids tool/idle races |
| MCP / HA tools | off (no token) | Voice-only |
| follow-up window | 0 s | Turn-based, simpler |
| playback prebuffer | 80 ms | Lower latency |
| echo probe | off | No open-mic loops |

## Step 2 later

Add `LONGLIVED_TOKEN` + `HA_MCP_URL`, set `ENABLE_WEB_SEARCH=true` if needed,
set `SLIM_VOICE=false` or override individual flags, point `va_url` back at HA
when you leave local mode.
