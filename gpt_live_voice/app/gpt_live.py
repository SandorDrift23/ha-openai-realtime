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
import struct
import time
from typing import Awaitable, Callable, Optional

from app.echo_probe import EchoProbe
from app.live_delegation import AssistDelegator

logger = logging.getLogger(__name__)

LIVE_URL = "wss://api.openai.com/v1/live/sessions"
LIVE_MODEL = "gpt-live-1"
LIVE_SAMPLE_RATE = 24000
CLOSE_TIMEOUT_S = 15.0
# Half duplex: keep silence off the Live socket, then stream at real time.
# Dumping a whole utterance at once trips input_audio_rate_limit_exceeded.
# 0.9.1 tried streaming pauses forever; with echo_probe that became a
# listen→reply loop with no transcripts. Restore close-on-silence.
SPEECH_RMS = 300
MIN_SPEECH_MS = 120
END_SILENCE_MS = 900
# Live TTS can pause between fragments; keep replying a bit longer than 1.5 s.
DEFAULT_IDLE_DEBOUNCE_S = 3.0

STOP_INSTRUCTION = (
    "The user stopped you. Stop speaking now and wait silently for the next "
    "thing they say. Do not acknowledge the stop out loud."
)

LIVE_INSTRUCTION_SUFFIX = (
    " You are in a live voice session. You do not call tools yourself. A "
    "delegation brings Home Assistant results and web lookups back as "
    "commentary. Stay silent while that lookup runs, then speak only what "
    "the commentary says. Speak an action as done only after that commentary "
    "confirms it. If the device or action is unclear, ask one short question. "
    "Keep replies short enough to say aloud."
)

def _rms(pcm: bytes) -> float:
    count = len(pcm) // 2
    if count == 0:
        return 0.0
    samples = struct.unpack(f"<{count}h", pcm[: count * 2])
    total = 0
    for sample in samples:
        total += sample * sample
    return (total / count) ** 0.5


class SpeechGate:
    """Open the Live mic only after speech, then stream at real time.

    Silence before the first loud frames is dropped. Once speech is confirmed,
    every chunk is forwarded immediately so OpenAI never sees a burst dump.
    After a short trailing silence the gate closes again until the next word.
    """

    def __init__(
        self,
        sample_rate: int = LIVE_SAMPLE_RATE,
        speech_rms: float = SPEECH_RMS,
        min_speech_ms: int = MIN_SPEECH_MS,
        end_silence_ms: int = END_SILENCE_MS,
    ):
        self._speech_rms = speech_rms
        self._min_speech_bytes = int(sample_rate * 2 * min_speech_ms / 1000)
        self._end_silence_s = end_silence_ms / 1000
        self.reset()

    def reset(self) -> None:
        self._speech_bytes = 0
        self._silence_at: Optional[float] = None
        self.heard_speech = False
        self.open = False

    def push(self, pcm: bytes, now: float) -> Optional[bytes]:
        """Return PCM to send now, or None while the mic should stay closed."""
        if not pcm:
            return None
        loud = _rms(pcm) >= self._speech_rms
        if loud:
            self._speech_bytes += len(pcm)
            self._silence_at = None
            if not self.open and self._speech_bytes >= self._min_speech_bytes:
                self.open = True
                self.heard_speech = True
            if self.open:
                return pcm
            return None
        if not self.open:
            self._speech_bytes = 0
            return None
        if self._silence_at is None:
            self._silence_at = now
        if (now - self._silence_at) >= self._end_silence_s:
            self.reset()
            return None
        return pcm


SendFn = Callable[[dict], Awaitable[None]]
PhaseFn = Callable[[str], Awaitable[None]]
AudioFn = Callable[[bytes], Awaitable[None]]
SocketLostFn = Callable[[], None]


def _is_socket_lost(exc: BaseException) -> bool:
    """True when a send hit a WebSocket that is already gone.

    websockets raises ConnectionClosedError('no close frame received or sent')
    for an abnormal close (code 1006). The pipeline must not see that once per
    microphone frame.
    """
    if type(exc).__name__ in {"ConnectionClosed", "ConnectionClosedError", "ConnectionClosedOK"}:
        return True
    text = str(exc).lower()
    return (
        "no close frame" in text
        or "connection is closed" in text
        or "socket is closed" in text
    )


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
        idle_debounce_s: float = DEFAULT_IDLE_DEBOUNCE_S,
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
        self._on_socket_lost: Optional[SocketLostFn] = None
        self._n = 0
        self._odd = b""
        self._ready = asyncio.Event()
        self._closed = asyncio.Event()
        self._phase = "idle"
        self._hold: Optional[str] = None
        self._reply_mute = False
        self._input_muted = True
        self._drop_output = False
        self._armed = False
        self._hold_output = False
        self._busy = False
        self._close_after_busy = False
        self._gate = SpeechGate()
        self._now = time.monotonic
        self._started = False
        self._finished = False
        self._idle_task: Optional[asyncio.Task] = None
        self.transcript = TranscriptLog()
        self.output_audio = bytearray()

    def bind_send(self, send: SendFn) -> None:
        self._send = send

    def bind_socket_lost(self, callback: SocketLostFn) -> None:
        """Called once when an audio send discovers the Live socket is dead."""
        self._on_socket_lost = callback

    @property
    def ready(self) -> bool:
        return self._ready.is_set()

    @property
    def phase(self) -> str:
        return self._phase

    def mark_socket_lost(self) -> None:
        """Drop session readiness after an abnormal socket close.

        session.closed never arrives on a 1006 drop, so ready would otherwise
        stay set and the next wake would keep the dead socket.
        """
        self._ready.clear()
        self._started = False
        self._closed.set()

    def _eid(self) -> str:
        self._n += 1
        return f"evt-{self._n}"

    async def _emit(self, event: dict) -> None:
        if self._send is None:
            raise RuntimeError("GPT-Live session has no socket")
        try:
            await self._send(event)
        except Exception as exc:
            if not _is_socket_lost(exc):
                raise
            kind = str(event.get("type") or "")
            logger.warning("GPT-Live send failed because the socket is closed: %s", exc)
            self.mark_socket_lost()
            if kind.startswith("session.input_audio."):
                if self._on_socket_lost is not None:
                    self._on_socket_lost()
                return
            raise

    async def resume_after_reconnect(self) -> None:
        """Unmute after a reconnect if this turn was still accepting speech."""
        if not self.ready or self._input_muted or self._reply_mute or self._hold == "interrupt":
            return
        await self._control_input(muted=False)

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
        self._armed = False
        self._hold_output = False
        self._close_after_busy = False
        self._input_muted = True
        self._gate.reset()
        await self._control_input(muted=True)

    async def on_uplink(self, pcm: bytes) -> None:
        """Stream speech in real time; drop silence until the user talks."""
        if self._echo is not None and pcm:
            self._echo.record("uplink", pcm)
        if self._hold == "interrupt" or self._reply_mute or self._phase == "replying":
            return
        chunk = self._gate.push(pcm, self._now())
        if self._gate.heard_speech and self._phase != "listening":
            await self._set_phase("listening")
        if not chunk:
            if not self._gate.open and self._armed and self._phase == "listening":
                # Trailing silence closed the gate — mute Live until the next word.
                self._input_muted = True
                await self._control_input(muted=True)
            return
        if not self._armed or self._input_muted:
            self._armed = True
            self._input_muted = False
            await self._control_input(muted=False)
        await self._append_audio(chunk)

    async def on_flush(self) -> None:
        self._odd = b""
        self._gate.reset()
        self._input_muted = True
        await self._control_input(muted=True)

    async def on_interrupt(self) -> None:
        self._drop_output = True
        self._hold = "interrupt"
        self._reply_mute = False
        self._armed = False
        self._hold_output = False
        self._close_after_busy = False
        self._busy = False
        self._input_muted = True
        self._cancel_idle()
        self._odd = b""
        self._gate.reset()
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
        if self._busy or self._hold_output:
            # Follow-up window ended while a planner/tool injection is still
            # running. Closing now causes context_injection_incomplete.
            self._close_after_busy = True
            logger.info("GPT-Live deferring session close until delegation finishes")
            return
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
            # Transcript can continue across audio gaps — keep idle from firing.
            if self._phase == "replying":
                self._arm_idle()
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
        if not pcm or self._hold_output:
            return
        # After a finished turn we ignore orphan TTS until the user speaks again.
        # While still in (or resuming) a reply, never drop fragments just because
        # an aggressive idle cleared _armed — that caused chopped answers.
        if self._phase == "idle" and not self._armed:
            return
        if self._echo is not None:
            self._echo.record("downlink", pcm)
        if self._phase != "replying":
            self._reply_mute = True
            self._input_muted = True
            self._armed = True
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
        self._busy = True
        self._hold_output = True
        if self._phase != "replying":
            await self._set_phase("thinking")
        try:
            try:
                calls, question = await self._delegator.plan(self.transcript.text())
            except Exception as exc:
                logger.warning("GPT-Live delegation planner failed: %s", exc)
                await self._append_context(
                    "session.commentary.append",
                    delegation_id,
                    "That could not be confirmed. Say that briefly.",
                )
                return
            if not calls:
                if question:
                    await self._append_context(
                        "session.commentary.append", delegation_id, question
                    )
                return
            looking_up = any(name == "web_search" for name, _ in calls)
            await self._append_context(
                "session.thinking.append",
                delegation_id,
                (
                    "Looking that up. Stay silent until commentary arrives."
                    if looking_up
                    else "Checking Home Assistant. Stay silent until commentary arrives."
                ),
            )
            try:
                commentary = await self._delegator.execute(calls)
            except Exception as exc:
                logger.warning("GPT-Live delegation tool failed: %s", exc)
                commentary = (
                    "That could not be confirmed. Say that briefly."
                )
            await self._append_context(
                "session.commentary.append", delegation_id, commentary
            )
        finally:
            self._hold_output = False
            self._busy = False
            if self._close_after_busy:
                self._close_after_busy = False
                await self.close()

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
        if self._drop_output or self._busy or self._hold_output:
            return
        if self._phase != "replying":
            return
        self._reply_mute = False
        self._armed = False
        self._input_muted = True
        self._gate.reset()
        await self._control_input(muted=True)
        await self._set_phase("idle")


class GptLiveConnection:
    """Primary Live WebSocket.

    A normal conversation_end closes the session and the next wake opens a
    new one. An abnormal drop (no close frame) also clears readiness so the
    next wake, or an in-flight turn, opens a new session instead of sending
    on the dead socket.
    """

    def __init__(self, session: GptLiveSession, api_key: str, url: str = LIVE_URL):
        self.session = session
        self._api_key = api_key
        self._url = url
        self._ws = None
        self._reader: Optional[asyncio.Task] = None
        self._opening: Optional[asyncio.Lock] = None
        self._reopen_task: Optional[asyncio.Task] = None
        self._accepting = True
        self.session.bind_socket_lost(self._schedule_reopen)

    def _transport_open(self) -> bool:
        ws = self._ws
        if ws is None:
            return False
        state = getattr(ws, "state", None)
        if state is None:
            return getattr(ws, "close_code", None) is None
        return getattr(state, "name", "") == "OPEN"

    async def ensure_open(self) -> None:
        if self._opening is None:
            self._opening = asyncio.Lock()
        async with self._opening:
            if self.session.ready and self._transport_open():
                return
            await self._open()

    async def _open(self) -> None:
        import websockets

        self._accepting = True
        await self._shutdown_socket()
        self._ws = await websockets.connect(
            self._url,
            additional_headers={"Authorization": f"Bearer {self._api_key}"},
            max_size=8_000_000,
        )

        async def send(event: dict) -> None:
            ws = self._ws
            if ws is None:
                raise ConnectionError("GPT-Live socket is closed")
            await ws.send(json.dumps(event))

        self.session.bind_send(send)
        await self.session.start()
        self._reader = asyncio.create_task(self._read())
        await asyncio.wait_for(self.session._ready.wait(), CLOSE_TIMEOUT_S)

    async def _read(self) -> None:
        ws = self._ws
        cancelled = False
        try:
            async for raw in ws:
                if isinstance(raw, bytes):
                    raw = raw.decode("utf-8")
                await self.session.handle_server_event(json.loads(raw))
        except asyncio.CancelledError:
            # Shutdown cancels this task. Cleanup below must stay synchronous:
            # awaiting here can be interrupted by the same cancellation.
            cancelled = True
            raise
        except Exception as exc:
            logger.warning("GPT-Live socket ended: %s", exc)
        finally:
            self._note_reader_ended(ws, reopen=not cancelled)

    def _note_reader_ended(self, ws, *, reopen: bool) -> None:
        """Forget a socket that died without session.closed.

        Only the socket this reader owned is cleared, so a reconnect that
        already replaced it is left alone. A cancelled reader is shut down
        on purpose (close, or the start of a new connect) and must not reopen.
        """
        if self._ws is not ws or self.session._finished:
            return
        self.session.mark_socket_lost()
        self._ws = None
        if reopen:
            self._schedule_reopen()

    def _schedule_reopen(self) -> None:
        if not self._accepting:
            return
        if self._reopen_task is not None and not self._reopen_task.done():
            return
        self._reopen_task = asyncio.create_task(self._reopen())

    async def _reopen(self) -> None:
        try:
            await self.session._set_phase("idle")
            await self.ensure_open()
            await self.session.resume_after_reconnect()
        except Exception as exc:
            logger.warning("GPT-Live reopen failed: %s", exc)

    async def close(self) -> None:
        self._accepting = False
        try:
            await self.session.close()
        finally:
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
