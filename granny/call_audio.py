"""Listen-only playback of Twilio's two audio tracks; never opens a microphone."""
import base64
import threading
import time

import numpy as np


def decode_mulaw(payload):
    raw = base64.b64decode(payload, validate=True)
    if not raw or len(raw) > 8000:
        raise ValueError("Invalid call audio packet")
    values = np.frombuffer(raw, dtype=np.uint8).astype(np.int32) ^ 0xff
    magnitude = (((values & 15) << 3) + 132) << ((values & 112) >> 4)
    return np.where(values & 128, 132 - magnitude, magnitude - 132).astype(np.int16)


class CallAudioPlayer:
    """Small jitter buffer mixes both timestamped tracks into the Mac speaker."""
    def __init__(self):
        self.lock = threading.Lock()
        self.frames = {}
        self.cursor = None
        self.ready_at = 0
        self.last_chunk = {}
        self.stream = None
        self.closed = False

    def start(self):
        import sounddevice as sd
        self.stream = sd.RawOutputStream(samplerate=8000, blocksize=160, channels=1, dtype="int16",
                                         callback=self._output)
        self.stream.start()

    def add(self, track, timestamp, chunk, payload):
        if track not in {"inbound", "outbound"}:
            raise ValueError("Unknown audio track")
        timestamp, chunk = int(timestamp), int(chunk)
        if not 0 <= timestamp <= 300_000 or chunk < 0:
            raise ValueError("Invalid audio timing")
        samples = decode_mulaw(payload).astype(np.int32)
        with self.lock:
            if self.closed or chunk <= self.last_chunk.get(track, -1):
                return
            self.last_chunk[track] = chunk
            start = timestamp * 8
            if self.cursor is None:
                self.cursor = start // 160
                self.ready_at = time.monotonic() + .12
            for offset in range(0, len(samples), 160):
                # Twilio sends 20ms frames; handle split/nonaligned packets as well.
                absolute = start + offset
                part = samples[offset:offset + 160]
                while len(part):
                    frame, position = divmod(absolute, 160)
                    count = min(160 - position, len(part))
                    if frame >= self.cursor:
                        self.frames.setdefault(frame, np.zeros(160, dtype=np.int32))[position:position + count] += part[:count]
                    absolute += count
                    part = part[count:]
            if len(self.frames) > 400:
                self.frames.clear()
                self.cursor = None

    def _output(self, output, frames, _timing, _status):
        result = np.zeros(frames, dtype=np.int16)
        with self.lock:
            if not self.closed and self.cursor is not None and time.monotonic() >= self.ready_at:
                for offset in range(0, frames, 160):
                    samples = self.frames.pop(self.cursor, None)
                    if samples is not None:
                        count = min(160, frames - offset)
                        result[offset:offset + count] = np.clip(samples[:count], -32768, 32767).astype(np.int16)
                    self.cursor += 1
        output[:] = result.tobytes()

    def close(self):
        with self.lock:
            if self.closed:
                return
            self.closed = True
            self.frames.clear()
        if self.stream is not None:
            self.stream.stop()
            self.stream.close()
