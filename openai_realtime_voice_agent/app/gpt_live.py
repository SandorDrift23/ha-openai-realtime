"""GPT-Live session beside the existing gpt-realtime-2 path.

gpt-live-1 uses wss://api.openai.com/v1/live/sessions and session.start.
It is not selected by changing the Realtime model name. One PCM format covers
both directions; this client uses 24 kHz and expects the pipeline resampler
to have already lifted the Voice PE's 16 kHz uplink.

While the device keeps barge-in off, uplink that arrives during a reply is
not appended. The model is muted for that interval. An optional echo probe
may record those bytes locally.
"""

import asyncio
import base64
import json
import logging
from typing import Awaitable, Callable, Optional

from app.echo_probe import EchoProbe
from app.live_delegation import AssistDelegator

logger = logging.getLogger(__name__)

LIVE_URL = "wss://api.openai.com/v1/live/sessions"
LIVE_MODEL = "gpt-live-1"
LIVE_SAMPLE_RATE = 24000
CLOSE_TIMEOUT_S = 15.0

STOP_INSTRUCTION = (
    "The user stopped you. Stop speaking now and wait silently for the next "
    "thing they say. Do not acknowledge the stop out loud."
)

LIVE_INSTRUCTION_SUFFIX = (
    " You are in a live voice session. You do not call home-control tools "
    "yourself. A delegation brings Home Assistant results back as commentary. "
    "Speak an action as done only after that commentary confirms it. If the "
    "device or action is unclear, ask one short question. Keep replies short "
    "enough to say aloud."
)

SendFn = Callable[[dict], Awaitable[None]]
PhaseFn = Callable[[str], Awaitable[None]]
AudioFn = Callable[[bytes], Awaitable[None]]


class TranscriptLog:
    def __init__(self, limit: int = 4000):
        self.user = ""
        self.assistant = ""
        self._limit = limit

    def add_user(self, delta: str) -> None:
        self.user = (self.user + delta)[-self._limit :]

    def add_assistant(self, delta: str) -> None:
        self.assistant = (self.assistant + delta)[-self._limit :]

    def text(self) -> str:
        return f"User:\n{self.user}\nAssistant:\n{self.assistant}".strip()


class GptLiveSession:
    """Protocol state for one Voice PE conversation. The socket is injected."""

    def __init__(
        self,
        *,
        voice: str,
        instructions: str,
        delegator: AssistDelegator,
        on_phase: PhaseFn,
        on_output_audio: AudioFn,
        echo_probe: Optional[EchoProbe] = None,
        idle_debounce_s: float = 1.5,
        on_transcript: Optional[Callable[[str, str], None]] = None,
    ):
        self.voice = voice or "marin"
        self.instructions = (instructions or "").strip() + LIVE_INSTRUCTION_SUFFIX
        self._delegator = delegator
        self._on_phase = on_phase
        self._on_output_audio = on_output_audio
        self._echo = echo_probe
        self._idle_debounce_s = max(0.0, idle_debounce_s)
        self._on_transcript = on_transcript
        self._send: Optional[SendFn] = None
        self._n = 0
        self._odd = b""
        self._ready = asyncio.Event()
        self._closed = asyncio.Event()
        self._phase = "idle"
        self._hold: Optional[str] = None
        self._reply_mute = False
        self._input_muted = True
        self._drop_output = False
        self._started = False
        self._finished = False
        self._idle_task: Optional[asyncio.Task] = None
        self.transcript = TranscriptLog()
        self.output_audio = bytearray()

    def bind_send(self, send: SendFn) -> None:
        self._send = send

    @property
    def ready(self) -> bool:
        return self._ready.is_set()

    @property
    def phase(self) -> str:
        return self._phase

    def _eid(self) -> str:
        self._n += 1
        return f"evt-{self._n}"

    async def _emit(self, event: dict) -> None:
        if self._send is None:
            raise RuntimeError("GPT-Live session has no socket")
        await self._send(event)

    async def start(self) -> None:
        """Send session.start. Caller waits for session.started before audio."""
        self._finished = False
        self._closed.clear()
        self._ready.clear()
        self._started = True
        await self._emit({
            "type": "session.start",
            "event_id": self._eid(),
            "session": {
                "model": LIVE_MODEL,
                "instructions": self.instructions,
                "audio": {
                    "format": {"type": "audio/pcm", "rate": LIVE_SAMPLE_RATE},
                    "output": {"voice": self.voice},
                },
                "delegation": {"type": "client"},
            },
        })

    async def on_wake(self) -> None:
        self._drop_output = False
        self._reply_mute = False
        self._hold = None
        self._input_muted = False
        await self._control_input(muted=False)

    async def on_uplink(self, pcm: bytes) -> None:
        """Forward 24 kHz PCM, or only record it while a reply is playing."""
        if self._echo is not None and pcm:
            self._echo.record("uplink", pcm)
        if self._hold == "interrupt" or self._reply_mute or self._phase == "replying":
            return
        if self._input_muted:
            self._input_muted = False
            await self._control_input(muted=False)
        if self._phase != "listening":
            await self._set_phase("listening")
        await self._append_audio(pcm)

    async def on_flush(self) -> None:
        self._odd = b""
        self._input_muted = True
        await self._control_input(muted=True)

    async def on_interrupt(self) -> None:
        self._drop_output = True
        self._hold = "interrupt"
        self._reply_mute = False
        self._input_muted = True
        self._cancel_idle()
        self._odd = b""
        await self._control_input(muted=True)
        if self.ready:
            await self._emit({
                "type": "session.instructions.append",
                "event_id": self._eid(),
                "delegation_id": None,
                "content": STOP_INSTRUCTION,
            })
        await self._set_phase("idle")

    async def on_conversation_end(self) -> None:
        await self.close()

    async def close(self) -> None:
        if self._finished:
            return
        self._cancel_idle()
        if not self._started or not self.ready:
            self._finished = True
            self._closed.set()
            self._ready.clear()
            return
        await self._emit({"type": "session.close", "event_id": self._eid()})
        try:
            await asyncio.wait_for(self._closed.wait(), CLOSE_TIMEOUT_S)
        except TimeoutError:
            logger.warning("GPT-Live session.close did not receive session.closed")
            self._finished = True
            self._ready.clear()

    async def handle_server_event(self, event: dict) -> None:
        kind = event.get("type")
        if kind == "session.started":
            self._ready.set()
            logger.info("GPT-Live session started")
        elif kind == "session.output_audio.delta":
            await self._on_output_delta(event.get("delta") or "")
        elif kind == "session.input_transcript.delta":
            delta = event.get("delta") or ""
            self.transcript.add_user(delta)
            self._note_transcript("user", delta)
        elif kind == "session.output_transcript.delta":
            delta = event.get("delta") or ""
            self.transcript.add_assistant(delta)
            self._note_transcript("assistant", delta)
        elif kind == "session.delegation.created":
            await self._on_delegation(event)
        elif kind == "session.closed":
            logger.info("GPT-Live session closed")
            self._finished = True
            self._ready.clear()
            self._started = False
            self._closed.set()
            await self._set_phase("idle")
        elif kind == "error":
            logger.warning("GPT-Live error: %s", event.get("error"))

    async def _on_output_delta(self, delta_b64: str) -> None:
        if self._drop_output or not delta_b64:
            return
        try:
            pcm = base64.b64decode(delta_b64)
        except (ValueError, TypeError):
            logger.warning("GPT-Live output audio was not valid base64")
            return
        if not pcm:
            return
        if self._echo is not None:
            self._echo.record("downlink", pcm)
        if self._phase != "replying":
            self._reply_mute = True
            self._input_muted = True
            await self._control_input(muted=True)
            await self._set_phase("replying")
        self.output_audio.extend(pcm)
        await self._on_output_audio(pcm)
        self._arm_idle()

    async def _on_delegation(self, event: dict) -> None:
        delegation = event.get("delegation") or {}
        if delegation.get("target") not in (None, "client"):
            return
        delegation_id = delegation.get("id") or ""
        if not self._delegator.begin(delegation_id):
            logger.info("GPT-Live duplicate delegation %s ignored", delegation_id)
            return
        if self._phase != "replying":
            await self._set_phase("thinking")
        try:
            calls, question = await self._delegator.plan(self.transcript.text())
        except Exception as exc:
            logger.warning("GPT-Live delegation planner failed: %s", exc)
            await self._append_context(
                "session.commentary.append",
                delegation_id,
                "Home Assistant did not confirm that action. Say that it could not be confirmed.",
            )
            return
        if not calls:
            if question:
                await self._append_context("session.commentary.append", delegation_id, question)
            return
        await self._append_context(
            "session.thinking.append",
            delegation_id,
            "Checking Home Assistant. No action has been taken yet.",
        )
        try:
            commentary = await self._delegator.execute(calls)
        except Exception as exc:
            logger.warning("GPT-Live delegation tool failed: %s", exc)
            commentary = (
                "Home Assistant did not confirm that action. "
                "Say that it could not be confirmed."
            )
        await self._append_context("session.commentary.append", delegation_id, commentary)

    async def _append_context(self, event_type: str, delegation_id: str, content: str) -> None:
        if not self.ready or not content:
            return
        await self._emit({
            "type": event_type,
            "event_id": self._eid(),
            "delegation_id": delegation_id,
            "content": content,
        })

    async def _append_audio(self, pcm: bytes) -> None:
        if not pcm or not self.ready or self._hold == "interrupt":
            return
        pending = self._odd + pcm
        even = len(pending) - (len(pending) % 2)
        self._odd = pending[even:]
        if even == 0:
            return
        await self._emit({
            "type": "session.input_audio.append",
            "event_id": self._eid(),
            "audio": base64.b64encode(pending[:even]).decode("ascii"),
        })

    async def _control_input(self, *, muted: bool) -> None:
        if not self.ready:
            return
        await self._emit({
            "type": "session.input_audio.mute" if muted else "session.input_audio.unmute",
            "event_id": self._eid(),
        })

    async def _set_phase(self, value: str) -> None:
        if value == self._phase and value != "listening":
            return
        self._phase = value
        try:
            await self._on_phase(value)
        except Exception as exc:
            logger.warning("phase %s failed: %s", value, exc)

    def _note_transcript(self, role: str, delta: str) -> None:
        if self._on_transcript is not None and delta:
            self._on_transcript(role, delta)

    def _arm_idle(self) -> None:
        self._cancel_idle()
        self._idle_task = asyncio.create_task(self._go_idle())

    def _cancel_idle(self) -> None:
        if self._idle_task is not None and not self._idle_task.done():
            self._idle_task.cancel()
        self._idle_task = None

    async def _go_idle(self) -> None:
        try:
            await asyncio.sleep(self._idle_debounce_s)
        except asyncio.CancelledError:
            return
        if self._drop_output:
            return
        self._reply_mute = False
        self._input_muted = True
        await self._set_phase("idle")


class GptLiveConnection:
    """Primary Live WebSocket. Reopens after conversation_end on the next wake."""

    def __init__(self, session: GptLiveSession, api_key: str, url: str = LIVE_URL):
        self.session = session
        self._api_key = api_key
        self._url = url
        self._ws = None
        self._reader: Optional[asyncio.Task] = None
        self._opening: Optional[asyncio.Lock] = None

    async def ensure_open(self) -> None:
        if self._opening is None:
            self._opening = asyncio.Lock()
        async with self._opening:
            if self.session.ready and self._ws is not None:
                return
            await self._open()

    async def _open(self) -> None:
        import websockets

        await self._shutdown_socket()
        self._ws = await websockets.connect(
            self._url,
            additional_headers={"Authorization": f"Bearer {self._api_key}"},
            max_size=8_000_000,
        )

        async def send(event: dict) -> None:
            await self._ws.send(json.dumps(event))

        self.session.bind_send(send)
        await self.session.start()
        self._reader = asyncio.create_task(self._read())
        await asyncio.wait_for(self.session._ready.wait(), CLOSE_TIMEOUT_S)

    async def _read(self) -> None:
        try:
            async for raw in self._ws:
                if isinstance(raw, bytes):
                    raw = raw.decode("utf-8")
                await self.session.handle_server_event(json.loads(raw))
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.warning("GPT-Live socket ended: %s", exc)
        finally:
            if not self.session._finished:
                await self.session._set_phase("idle")

    async def close(self) -> None:
        await self.session.close()
        await self._shutdown_socket()

    async def _shutdown_socket(self) -> None:
        if self._reader is not None:
            self._reader.cancel()
            try:
                await self._reader
            except (asyncio.CancelledError, Exception):
                pass
            self._reader = None
        if self._ws is not None:
            try:
                await self._ws.close()
            except Exception:
                pass
            self._ws = None
