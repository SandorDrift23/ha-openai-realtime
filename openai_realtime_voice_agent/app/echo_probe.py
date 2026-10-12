"""Timestamped uplink/downlink recording while the assistant is speaking.

Default off. The recording does not enable barge-in. The Voice PE firmware
already measured about a 10x speaker-to-microphone leak through XMOS, both
I2S channels are the same AEC output, and an open mic during playback has
squealed. A capped barge-in test is not justified until a probe recording
shows the uplink is intelligible and not dominated by the reply.
"""

import json
import os
import struct
import time
from datetime import datetime


# Kept false on purpose. Do not flip this because a config flag exists.
BARGE_IN_TEST_JUSTIFIED = False


class EchoProbe:
    """Write 24 kHz PCM16 WAVs plus a JSONL timeline of each chunk."""

    def __init__(self, output_dir: str = "recordings/echo-probe"):
        self.output_dir = output_dir
        os.makedirs(output_dir, exist_ok=True)
        stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        self._t0 = time.monotonic()
        self._uplink = bytearray()
        self._downlink = bytearray()
        self._events: list[dict] = []
        self._prefix = os.path.join(output_dir, f"echo_{stamp}")
        self._closed = False

    def record(self, direction: str, pcm: bytes) -> None:
        if self._closed or not pcm or direction not in ("uplink", "downlink"):
            return
        t_ms = int((time.monotonic() - self._t0) * 1000)
        self._events.append({"t_ms": t_ms, "dir": direction, "nbytes": len(pcm)})
        if direction == "uplink":
            self._uplink.extend(pcm)
        else:
            self._downlink.extend(pcm)

    def close(self) -> str:
        """Flush the timeline and both WAVs. Returns the JSONL path."""
        if self._closed:
            return self._prefix + ".jsonl"
        self._closed = True
        timeline = self._prefix + ".jsonl"
        with open(timeline, "w", encoding="utf-8") as handle:
            handle.write(json.dumps({
                "barge_in_test_justified": BARGE_IN_TEST_JUSTIFIED,
                "note": (
                    "Uplink during downlink is the echo probe. Do not enable "
                    "open-microphone playback until this recording shows the "
                    "uplink is intelligible and not dominated by the reply."
                ),
            }) + "\n")
            for event in self._events:
                handle.write(json.dumps(event) + "\n")
        _write_wav(self._prefix + "_uplink.wav", bytes(self._uplink))
        _write_wav(self._prefix + "_downlink.wav", bytes(self._downlink))
        return timeline


def _write_wav(path: str, pcm: bytes, sample_rate: int = 24000) -> None:
    if len(pcm) % 2:
        pcm += b"\x00"
    with open(path, "wb") as handle:
        handle.write(b"RIFF")
        handle.write(struct.pack("<I", 36 + len(pcm)))
        handle.write(b"WAVEfmt ")
        handle.write(struct.pack("<IHHIIHH", 16, 1, 1, sample_rate, sample_rate * 2, 2, 16))
        handle.write(b"data")
        handle.write(struct.pack("<I", len(pcm)))
        handle.write(pcm)
