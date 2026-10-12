<p align="center">
  <img src="openai_realtime_voice_agent/icon.png" alt="OpenAI Realtime 2 Voice Agent — a stylised Voice PE" width="160"/>
</p>

# OpenAI Realtime 2 Voice Agent (Home Assistant Voice PE)

> [!IMPORTANT]
> **This is 1 of 2 repos — you need both halves.** This repo is the **backend add-on**
> (the voice "brain"). It needs the custom Voice PE **firmware** to connect to it — the
> stock Home Assistant voice pipeline won't talk to this add-on. You must set up both:
> - 🧠 **Backend add-on** (this repo) — runs inside Home Assistant
> - 🔌 **Device firmware** → **[SandorDrift23/home-assistant-voice-pe](https://github.com/SandorDrift23/home-assistant-voice-pe/tree/feature/gpt-live-voice-pe)**, branch `feature/gpt-live-voice-pe` (flashed onto the Voice PE)
>
> 📖 New here? Start with that firmware branch. Leave `barge_in` false.

A Home Assistant **add-on** that turns a [Voice PE](https://www.home-assistant.io/voice-pe/)
device into a low-latency voice assistant. The default session is **OpenAI
`gpt-realtime-2`**. Set **Voice provider** to `gpt_live` to use **`gpt-live-1`**
instead. The device streams microphone audio to this add-on over a plain
WebSocket; the add-on runs the speech-to-speech session and controls Home
Assistant through the official
**[Home Assistant MCP Server](https://www.home-assistant.io/integrations/mcp_server/)**
integration. STT, TTS and the LLM all run in that session — there is no Home
Assistant `voice_assistant` pipeline on the audio path.

`gpt-live-1` can listen while it speaks. The Voice PE does not do that yet.
With `barge_in` false the microphone closes during playback, and a reply is
interrupted with "stop" or the center button. An echo recording, off by
default, is how we decide whether the microphone can stay open. Do not turn
`barge_in` on before that recording shows the uplink is intelligible. Play the
test through a powered speaker on the Voice PE 3.5 mm jack, away from the
microphones.

> Fork of **[fjfricke/ha-openai-realtime](https://github.com/fjfricke/ha-openai-realtime)**,
> with `gpt-realtime-2` kept as the default and `gpt-live-1` added as a separate
> provider. Home control uses the official HA MCP Server. The Voice PE firmware
> is the branch linked above.

## Repository layout

- **[`openai_realtime_voice_agent/`](openai_realtime_voice_agent/)** — the Home
  Assistant add-on (Python / [Pipecat](https://github.com/pipecat-ai/pipecat)).
  This is the only thing you install.
  - [`DOCS.md`](openai_realtime_voice_agent/DOCS.md) — full setup: OpenAI key, the
    Home Assistant MCP connection, recommended settings, web search, all options.
  - [`CHANGELOG.md`](openai_realtime_voice_agent/CHANGELOG.md) — what changed per version.

The **device firmware** lives in its own repository —
**[SandorDrift23/home-assistant-voice-pe](https://github.com/SandorDrift23/home-assistant-voice-pe/tree/feature/gpt-live-voice-pe)**,
branch `feature/gpt-live-voice-pe`
(a custom `va_client` ESPHome component, specific to the Voice PE hardware).

## Install

1. In Home Assistant, open **Settings → Add-ons → Add-on store → ⋮ → Repositories**
   and add `https://github.com/SandorDrift23/ha-openai-realtime`.
2. Install **OpenAI Realtime 2 Voice Agent**. It ships with no prebuilt `image:`,
   so Home Assistant builds it locally on first install (a few minutes on a Pi).
3. Configure the add-on and flash the companion firmware — see
   [`openai_realtime_voice_agent/DOCS.md`](openai_realtime_voice_agent/DOCS.md).

(An optional GitHub Actions workflow can publish container images to ghcr.io; it
isn't needed for a normal local-build install.)

## How it works

```
Voice PE (ESP32-S3)  ──WS, 16 kHz PCM up──▶   this add-on    ──▶  gpt-realtime-2
  va_client firmware  ◀──── 24 kHz PCM down──                     or gpt-live-1
                                                   │ tools
                                                   ▼
                                         Home Assistant MCP Server
```

The device does wake-word detection and XMOS audio cleanup locally and is a thin
client. It does not hold an API key. Interrupt a reply with the **"stop"** word
or the center button. `gpt-realtime-2` goes through Pipecat. `gpt-live-1` uses
OpenAI's Live WebSocket. In GPT-Live mode, Home Assistant actions are spoken
only after the MCP server returns the result.

## Known limitations

- **No voice timers or alarms yet** — every other Assist action (lights, switches,
  scenes, climate) and online questions work.
- **A brief reconnect about once an hour** (OpenAI's 60-minute session cap; the
  add-on refreshes proactively during a quiet moment, so it rarely interrupts).
- **Rarely, the assistant may stop itself** on a word in its own reply that sounds
  like "stop" — just ask again.

## Credits

- Forked from **[fjfricke/ha-openai-realtime](https://github.com/fjfricke/ha-openai-realtime)**.
- Built on **[Pipecat](https://github.com/pipecat-ai/pipecat)**.
- Firmware thin-client design based on **[maxmaxme/home-assistant-voice-pe](https://github.com/maxmaxme/home-assistant-voice-pe)** (a fork of **[esphome/home-assistant-voice-pe](https://github.com/esphome/home-assistant-voice-pe)**, Nabu Casa / ESPHome).
- Inspiration from **[marcinnowak79/home-assistant-voice-pe](https://github.com/marcinnowak79/home-assistant-voice-pe)** (gemini-live-proxy).

## License

MIT — see [LICENSE](LICENSE).
