"""Pipeline processor that feeds 24 kHz PCM into a GPT-Live session.

Sits after InputResampler. Microphone frames are consumed here so they are
not played back out the speaker. Model audio is pushed downstream as
OutputAudioRawFrame at 24 kHz.
"""

import logging

from pipecat.frames.frames import Frame, InputAudioRawFrame, OutputAudioRawFrame
from pipecat.processors.frame_processor import FrameDirection, FrameProcessor

from app.gpt_live import GptLiveSession, LIVE_SAMPLE_RATE

logger = logging.getLogger(__name__)


class GptLiveProcessor(FrameProcessor):
    def __init__(self, session: GptLiveSession, **kwargs):
        super().__init__(**kwargs)
        self._session = session

    async def process_frame(self, frame: Frame, direction: FrameDirection):
        await super().process_frame(frame, direction)
        if isinstance(frame, InputAudioRawFrame):
            if frame.audio:
                if frame.sample_rate != LIVE_SAMPLE_RATE:
                    logger.warning(
                        "GPT-Live uplink arrived at %s Hz; expected %s Hz after resampling",
                        frame.sample_rate,
                        LIVE_SAMPLE_RATE,
                    )
                await self._session.on_uplink(frame.audio)
            return
        await self.push_frame(frame, direction)

    async def play(self, pcm: bytes) -> None:
        if not pcm:
            return
        await self.push_frame(
            OutputAudioRawFrame(audio=pcm, sample_rate=LIVE_SAMPLE_RATE, num_channels=1)
        )
