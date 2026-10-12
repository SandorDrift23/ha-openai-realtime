"""Device handshake shared by the realtime and GPT-Live paths.

The Voice PE matches hello and phase JSON by substring, so callers must send
these objects with compact separators (no space after ':' or ',').
"""

from typing import Any


def hello_message(
    *,
    follow_up_ms: int,
    follow_up_open_delay_ms: int,
    wake_open_delay_ms: int,
    playback_prebuffer_ms: int,
    echo_probe: bool = False,
    echo_probe_max_volume_millis: int = 300,
) -> dict[str, Any]:
    """Build the server hello. Echo-probe keys are omitted unless the probe is on.

    ``barge_in`` is intentionally absent. Open-mic playback stays a firmware
    decision, and this handshake must not turn it on.
    """
    message: dict[str, Any] = {
        "type": "hello",
        "audio_out": "pcm",
        "follow_up_ms": int(follow_up_ms),
        "follow_up_open_delay_ms": int(follow_up_open_delay_ms),
        "wake_open_delay_ms": int(wake_open_delay_ms),
        "playback_prebuffer_ms": int(playback_prebuffer_ms),
    }
    if echo_probe:
        # Firmware clamps this to 0.4. 300 ms-units == 0.30 playback gain.
        capped = max(0, min(400, int(echo_probe_max_volume_millis)))
        message["echo_probe"] = True
        message["echo_probe_max_volume_millis"] = capped
    return message
