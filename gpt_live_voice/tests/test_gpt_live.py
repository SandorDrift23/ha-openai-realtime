"""Protocol tests for the GPT-Live path. No cloud calls and no Voice PE required.

The physical proof still uses a powered speaker on the Voice PE 3.5 mm jack.
These tests check the software contract that speaker has to meet: 24 kHz
playback, transcripts, follow-up audio, session close, and barge-in left off.
"""

import asyncio
import base64
import json
import struct
import unittest
from pathlib import Path

from app.device_protocol import hello_message
from app.echo_probe import BARGE_IN_TEST_JUSTIFIED, EchoProbe
from app.gpt_live import LIVE_MODEL, LIVE_SAMPLE_RATE, GptLiveConnection, GptLiveSession
from app.live_delegation import AssistDelegator, function_tools_from_schema


class _Item:
    def __init__(self, kind, name="", arguments="{}", text=""):
        self.type = kind
        self.name = name
        self.arguments = arguments
        self.text = text
        self.content = [self] if kind == "message" else []


class _Response:
    def __init__(self, output):
        self.output = output


class _SchemaTool:
    def __init__(self, name, description, properties, required):
        self.name = name
        self.description = description
        self.properties = properties
        self.required = required


class _Schema:
    def __init__(self, tools):
        self.standard_tools = tools


def _pcm() -> bytes:
    return b"\x01\x00\x02\x00"


def _tone(ms: int, amplitude: int = 8000) -> bytes:
    count = max(1, int(LIVE_SAMPLE_RATE * ms / 1000))
    return struct.pack(f"<{count}h", *([amplitude] * count))


async def _say(session) -> None:
    """Feed a short loud utterance and the pause that commits it."""
    clock = {"t": 0.0}
    session._now = lambda: clock["t"]
    await session.on_uplink(_tone(250))
    await session.on_uplink(_tone(20, 0))
    clock["t"] = 0.7
    await session.on_uplink(_tone(20, 0))


class SessionHarness:
    def __init__(self, delegator, echo=None, idle_debounce_s=0.0):
        self.sent = []
        self.phases = []
        self.played = []
        self.transcripts = []
        self.session = GptLiveSession(
            voice="marin",
            instructions="Be brief.",
            delegator=delegator,
            on_phase=self._phase,
            on_output_audio=self._play,
            echo_probe=echo,
            idle_debounce_s=idle_debounce_s,
            on_transcript=self._transcript,
        )
        self.session.bind_send(self._send)

    async def _send(self, event):
        self.sent.append(event)

    async def _phase(self, value):
        self.phases.append(value)

    async def _play(self, pcm):
        self.played.append(pcm)

    def _transcript(self, role, delta):
        self.transcripts.append((role, delta))

    def types(self):
        return [event["type"] for event in self.sent]


async def _quiet_delegator():
    async def complete(transcript, tools):
        return _Response([])

    async def call_tool(name, arguments):
        raise AssertionError("no tool should run")

    return AssistDelegator(complete, call_tool, [])


class _ConnectionClosedError(Exception):
    """Stand-in for websockets' abnormal-close error, matched by type name."""


_ConnectionClosedError.__name__ = "ConnectionClosedError"


class _Socket:
    def __init__(self, state_name):
        self.state = type("State", (), {"name": state_name})()


class GptLiveTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.harness = SessionHarness(await _quiet_delegator())
        self.session = self.harness.session

    async def test_session_start_is_gpt_live_not_realtime(self):
        await self.session.start()
        start = self.harness.sent[0]
        self.assertEqual(start["type"], "session.start")
        self.assertEqual(start["session"]["model"], LIVE_MODEL)
        self.assertEqual(start["session"]["audio"]["format"], {"type": "audio/pcm", "rate": 24000})
        self.assertEqual(start["session"]["delegation"], {"type": "client"})
        self.assertNotIn("gpt-realtime", json.dumps(start))
        self.assertEqual(LIVE_SAMPLE_RATE, 24000)

    async def test_half_duplex_playback_follow_up_and_close(self):
        await self.session.start()
        await self.session.handle_server_event({"type": "session.started", "session": {"id": "sess_test"}})
        await self.session.on_wake()
        await _say(self.session)
        self.assertIn("session.input_audio.append", self.harness.types())
        appended = [e for e in self.harness.sent if e["type"] == "session.input_audio.append"]
        self.assertGreater(len(base64.b64decode(appended[-1]["audio"])), 1000)

        await self.session.handle_server_event({"type": "session.input_transcript.delta", "delta": "lamp"})
        reply = b"\x03\x00\x04\x00"
        await self.session.handle_server_event({
            "type": "session.output_audio.delta",
            "delta": base64.b64encode(reply).decode("ascii"),
        })
        await self.session.handle_server_event({
            "type": "session.output_transcript.delta",
            "delta": "on",
        })
        self.assertEqual(self.harness.played, [reply])
        self.assertIn(("user", "lamp"), self.harness.transcripts)
        self.assertIn(("assistant", "on"), self.harness.transcripts)
        self.assertEqual(self.harness.phases[-1], "replying")

        before = len(appended)
        await self.session.on_uplink(b"\x05\x00\x06\x00")
        appended = [e for e in self.harness.sent if e["type"] == "session.input_audio.append"]
        self.assertEqual(len(appended), before)

        await self.session._idle_task
        self.assertEqual(self.harness.phases[-1], "idle")

        before_follow = len(appended)
        await self.session.on_uplink(b"\x07\x00\x08\x00")
        appended = [e for e in self.harness.sent if e["type"] == "session.input_audio.append"]
        self.assertEqual(len(appended), before_follow)
        played = len(self.harness.played)
        await self.session.handle_server_event({
            "type": "session.output_audio.delta",
            "delta": base64.b64encode(b"\x09\x00").decode("ascii"),
        })
        self.assertEqual(len(self.harness.played), played)
        await _say(self.session)
        appended = [e for e in self.harness.sent if e["type"] == "session.input_audio.append"]
        self.assertGreater(len(appended), before_follow)
        self.assertEqual(self.harness.phases[-1], "listening")

        closing = asyncio.create_task(self.session.on_conversation_end())
        await asyncio.sleep(0)
        self.assertIn("session.close", self.harness.types())
        await self.session.handle_server_event({"type": "session.closed"})
        await closing
        self.assertFalse(self.session.ready)

    async def test_dead_socket_does_not_raise_on_every_mic_frame(self):
        lost = {"n": 0}

        async def send(event):
            if str(event["type"]).startswith("session.input_audio."):
                raise _ConnectionClosedError("no close frame received or sent")
            self.harness.sent.append(event)

        self.session.bind_send(send)
        self.session.bind_socket_lost(lambda: lost.__setitem__("n", lost["n"] + 1))
        await self.session.start()
        await self.session.handle_server_event({"type": "session.started"})
        await self.session.on_wake()
        await _say(self.session)
        await _say(self.session)
        self.assertFalse(self.session.ready)
        self.assertEqual(lost["n"], 1)
        self.assertFalse(any(event["type"] == "session.input_audio.append" for event in self.harness.sent))

    async def test_session_start_still_fails_when_the_socket_is_closed(self):
        async def send(event):
            raise _ConnectionClosedError("no close frame received or sent")

        self.session.bind_send(send)
        with self.assertRaises(_ConnectionClosedError):
            await self.session.start()
        self.assertFalse(self.session.ready)

    async def test_ensure_open_replaces_a_closed_socket(self):
        self.session._ready.set()
        self.session._started = True
        connection = GptLiveConnection(self.session, "test-key")
        connection._ws = _Socket("CLOSED")
        opened = {"n": 0}

        async def open_socket():
            opened["n"] += 1
            self.session._ready.set()
            connection._ws = _Socket("OPEN")

        connection._open = open_socket
        await connection.ensure_open()
        await connection.ensure_open()
        self.assertEqual(opened["n"], 1)

    async def test_reader_drop_forgets_only_its_own_socket(self):
        connection = GptLiveConnection(self.session, "test-key")
        dead = _Socket("CLOSED")
        replacement = _Socket("OPEN")
        connection._ws = replacement
        self.session._ready.set()
        self.session._started = True

        connection._note_reader_ended(dead, reopen=True)
        self.assertTrue(self.session.ready)
        self.assertIs(connection._ws, replacement)
        self.assertIsNone(connection._reopen_task)

        connection._ws = dead
        scheduled = {"n": 0}
        connection._schedule_reopen = lambda: scheduled.__setitem__("n", scheduled["n"] + 1)
        connection._note_reader_ended(dead, reopen=True)
        self.assertFalse(self.session.ready)
        self.assertIsNone(connection._ws)
        self.assertEqual(scheduled["n"], 1)

        connection._ws = dead
        self.session._ready.set()
        self.session._started = True
        connection._note_reader_ended(dead, reopen=False)
        self.assertEqual(scheduled["n"], 1)

    async def test_interrupt_drops_later_audio_until_wake(self):
        await self.session.start()
        await self.session.handle_server_event({"type": "session.started"})
        await self.session.on_wake()
        await _say(self.session)
        await self.session.handle_server_event({
            "type": "session.output_audio.delta",
            "delta": base64.b64encode(b"\x01\x00").decode("ascii"),
        })
        await self.session.on_interrupt()
        self.assertTrue(any(e["type"] == "session.instructions.append" for e in self.harness.sent))
        await self.session.handle_server_event({
            "type": "session.output_audio.delta",
            "delta": base64.b64encode(b"\x09\x00").decode("ascii"),
        })
        self.assertEqual(self.harness.played, [b"\x01\x00"])
        await self.session.on_uplink(b"\x02\x00")
        appended = [e for e in self.harness.sent if e["type"] == "session.input_audio.append"]
        self.assertFalse(any(base64.b64decode(e["audio"]) == b"\x02\x00" for e in appended))

    async def test_delegation_speaks_only_after_home_assistant_returns(self):
        seen = {"tool": False}

        async def complete(transcript, tools):
            return _Response([_Item("function_call", "HassTurnOn", '{"name":"light.kitchen"}')])

        async def call_tool(name, arguments):
            self.assertEqual(name, "HassTurnOn")
            self.assertFalse(any(e["type"] == "session.commentary.append" for e in self.harness.sent))
            self.assertTrue(any(e["type"] == "session.thinking.append" for e in self.harness.sent))
            seen["tool"] = True
            return "light.kitchen is on"

        self.session._delegator = AssistDelegator(complete, call_tool, [])
        await self.session.start()
        await self.session.handle_server_event({"type": "session.started"})
        await self.session.handle_server_event({"type": "session.input_transcript.delta", "delta": "kitchen light"})
        await self.session.handle_server_event({
            "type": "session.delegation.created",
            "delegation": {"id": "item_1", "target": "client"},
        })
        commentary = [e for e in self.harness.sent if e["type"] == "session.commentary.append"]
        self.assertTrue(seen["tool"])
        self.assertEqual(len(commentary), 1)
        self.assertIn("light.kitchen is on", commentary[0]["content"])
        self.assertIn("item_1", commentary[0]["delegation_id"])

        await self.session.handle_server_event({
            "type": "session.delegation.created",
            "delegation": {"id": "item_1", "target": "client"},
        })
        commentary = [e for e in self.harness.sent if e["type"] == "session.commentary.append"]
        self.assertEqual(len(commentary), 1)

    def test_hello_leaves_barge_in_off_and_probe_defaults_off(self):
        plain = hello_message(
            follow_up_ms=8000,
            follow_up_open_delay_ms=700,
            wake_open_delay_ms=700,
            playback_prebuffer_ms=150,
        )
        self.assertNotIn("barge_in", plain)
        self.assertNotIn("echo_probe", plain)
        self.assertEqual(plain["audio_out"], "pcm")

        probed = hello_message(
            follow_up_ms=8000,
            follow_up_open_delay_ms=700,
            wake_open_delay_ms=700,
            playback_prebuffer_ms=150,
            echo_probe=True,
            echo_probe_max_volume_millis=900,
        )
        self.assertTrue(probed["echo_probe"])
        self.assertLessEqual(probed["echo_probe_max_volume_millis"], 400)
        self.assertNotIn("barge_in", probed)
        self.assertFalse(BARGE_IN_TEST_JUSTIFIED)

    def test_echo_probe_records_uplink_during_playback(self):
        async def scenario(directory):
            probe = EchoProbe(output_dir=directory)
            harness = SessionHarness(await _quiet_delegator(), echo=probe)
            await harness.session.start()
            await harness.session.handle_server_event({"type": "session.started"})
            await harness.session.on_wake()
            await _say(harness.session)
            await harness.session.handle_server_event({
                "type": "session.output_audio.delta",
                "delta": base64.b64encode(b"\x01\x00").decode("ascii"),
            })
            leak = b"\xaa\x00\xbb\x00"
            await harness.session.on_uplink(leak)
            self.assertIn(leak, bytes(probe._uplink))
            self.assertTrue(any(event["dir"] == "uplink" for event in probe._events))
            self.assertTrue(any(event["dir"] == "downlink" for event in probe._events))
            appended = [e for e in harness.sent if e["type"] == "session.input_audio.append"]
            self.assertFalse(any(base64.b64decode(e["audio"]) == leak for e in appended))
            probe.close()

        import tempfile
        with tempfile.TemporaryDirectory() as directory:
            asyncio.run(scenario(directory))

    def test_planner_tools_follow_the_allowlist(self):
        schema = _Schema([
            _SchemaTool("HassTurnOn", "Turn on", {"name": {"type": "string"}}, ["name"]),
            _SchemaTool("HassTurnOff", "Turn off", {}, []),
        ])
        tools = function_tools_from_schema(schema, ["HassTurnOn"])
        self.assertEqual([tool["name"] for tool in tools], ["HassTurnOn"])

    def test_firmware_keeps_barge_in_false(self):
        firmware = Path(__file__).resolve().parents[3] / "home-assistant-voice-pe-qwen"
        yaml_path = firmware / "home-assistant-voice.realtime.yaml"
        source = (firmware / "esphome" / "components" / "va_client" / "va_client.cpp").read_text(encoding="utf-8")
        yaml = yaml_path.read_text(encoding="utf-8")
        self.assertIn("barge_in: false", yaml)
        self.assertIn("echo_probe_", source)
        self.assertIn("!this->echo_probe_", source)


if __name__ == "__main__":
    unittest.main()
