"""
Voice chat for Campfire.

Audio is captured from the microphone in 20 ms frames (48 kHz mono), compressed with
Opus, and sent to the server as base64 inside the normal chat connection. Audio from
other people is decoded, held in a small per-person jitter buffer and mixed together
for the speakers.

Needs:  pip install numpy av sounddevice
"""

from __future__ import annotations

import base64
import collections
import math
import queue
import threading
import time

try:
    import numpy as np
    import av
    HAVE_CODEC = True
except ImportError:                      # pragma: no cover - depends on the machine
    np = av = None
    HAVE_CODEC = False

try:
    import sounddevice as sd
    HAVE_AUDIO = True
except (ImportError, OSError):           # OSError: PortAudio missing (Linux without libportaudio2)
    sd = None
    HAVE_AUDIO = False

RATE = 48000
FRAME = 960                  # 20 ms at 48 kHz
BITRATE = 32000
PREFILL = 2                  # frames to buffer before starting someone's audio (smooths jitter)
MAX_QUEUE = 10               # frames buffered per person before we drop old ones (200 ms)
SPEAK_DB = -50               # louder than this counts as "speaking" for the indicator
HANGOVER = 0.35              # keep sending this long after you stop talking (voice activity)
DEVICE_LATENCY = 0.08         # seconds of sound-card buffering: lower = snappier, too low = crackles


def missing_packages() -> str | None:
    """What's missing for voice to work, as a pip command, or None if everything's there."""
    need = []
    if np is None:
        need.append("numpy")
    if av is None:
        need.append("av")
    if not HAVE_AUDIO:
        need.append("sounddevice")
    return f"pip install {' '.join(need)}" if need else None


def level_db(pcm) -> float:
    """Loudness of a frame in dBFS (0 = maximum, -90 = silence)."""
    if pcm is None or len(pcm) == 0:
        return -90.0
    rms = math.sqrt(float(np.mean(pcm.astype(np.float32) ** 2)))
    return 20 * math.log10(max(rms, 1.0) / 32768.0)


def list_devices():
    """([(index, name)] inputs, [(index, name)] outputs). Empty if audio isn't available."""
    if not HAVE_AUDIO:
        return [], []
    ins, outs = [], []
    try:
        apis = sd.query_hostapis()
        for i, d in enumerate(sd.query_devices()):
            api = apis[d["hostapi"]]["name"]
            # MME duplicates every device under several drivers on Windows; show one copy
            label = f"{d['name']}" if api in ("MME", "ALSA", "Core Audio") else f"{d['name']} ({api})"
            if d["max_input_channels"] > 0:
                ins.append((i, label))
            if d["max_output_channels"] > 0:
                outs.append((i, label))
    except Exception:
        pass
    return ins, outs


# ---------------------------------------------------------------- codec
class Encoder:
    def __init__(self):
        self.ctx = av.CodecContext.create("libopus", "w")
        self.ctx.sample_rate = RATE
        self.ctx.layout = "mono"
        self.ctx.format = "s16"
        self.ctx.bit_rate = BITRATE
        self.pts = 0

    def encode(self, pcm) -> list[bytes]:
        frame = av.AudioFrame.from_ndarray(pcm.reshape(1, -1), format="s16", layout="mono")
        frame.sample_rate = RATE
        frame.pts = self.pts
        self.pts += len(pcm)
        return [bytes(p) for p in self.ctx.encode(frame)]


class Decoder:
    def __init__(self):
        self.ctx = av.CodecContext.create("libopus", "r")
        self.ctx.sample_rate = RATE
        self.ctx.layout = "mono"

    def decode(self, data: bytes):
        out = []
        for f in self.ctx.decode(av.Packet(data)):
            a = f.to_ndarray()
            if a.dtype != np.int16:                     # float output on some builds
                a = np.clip(a * 32767, -32768, 32767).astype(np.int16)
            out.append(a.reshape(-1))
        return out


# ---------------------------------------------------------------- mixer
class Mixer:
    """Per-person jitter buffers, mixed into one output stream. Thread-safe."""

    def __init__(self):
        self.lock = threading.Lock()
        self.queues: dict[str, collections.deque] = {}
        self.started: set[str] = set()
        self.volume: dict[str, float] = {}
        self.last_loud: dict[str, float] = {}
        self.leftover = np.zeros(0, dtype=np.int16)

    def push(self, user, pcm):
        with self.lock:
            q = self.queues.setdefault(user, collections.deque())
            q.append(pcm)
            while len(q) > MAX_QUEUE:             # we've fallen behind: catch up instead of lagging
                q.popleft()

    def remove(self, user):
        with self.lock:
            self.queues.pop(user, None)
            self.started.discard(user)
            self.last_loud.pop(user, None)

    def clear(self):
        with self.lock:
            self.queues.clear()
            self.started.clear()
            self.last_loud.clear()
            self.leftover = np.zeros(0, dtype=np.int16)

    def _mix_frame(self):
        total = np.zeros(FRAME, dtype=np.int32)
        now = time.monotonic()
        for user, q in self.queues.items():
            if user not in self.started:
                if len(q) < PREFILL:
                    continue
                self.started.add(user)
            if not q:
                self.started.discard(user)       # ran dry: re-buffer before playing again
                continue
            pcm = q.popleft()
            if level_db(pcm) > SPEAK_DB:
                self.last_loud[user] = now
            vol = self.volume.get(user, 1.0)
            total[: len(pcm)] += (pcm.astype(np.float32) * vol).astype(np.int32)[:FRAME]
        return np.clip(total, -32768, 32767).astype(np.int16)

    def mix(self, n: int):
        """Return exactly n samples of mixed audio."""
        with self.lock:
            buf = self.leftover
            while len(buf) < n:
                buf = np.concatenate([buf, self._mix_frame()])
            self.leftover = buf[n:]
            return buf[:n]

    def speaking(self, window=0.3):
        now = time.monotonic()
        with self.lock:
            return {u for u, t in self.last_loud.items() if now - t < window}


# ---------------------------------------------------------------- engine
class VoiceEngine:
    """Owns the mic, the speakers, the encoder and one decoder per person.

    send is called (from a background thread) with each base64 packet to transmit.
    """

    def __init__(self, send):
        self.send = send
        self.mixer = Mixer()
        self.decoders: dict[str, Decoder] = {}
        self.encoder = Encoder()
        self.outq: queue.Queue = queue.Queue(maxsize=8)
        self.in_stream = self.out_stream = None
        self.running = False
        self.sender = None

        # settings (the UI changes these directly)
        self.muted = False
        self.deaf = False
        self.mode = "vad"            # "vad" (voice activity) or "ptt" (push to talk)
        self.threshold_db = -45.0
        self.ptt_down = False

        # state the UI reads
        self.mic_db = -90.0
        self.last_sent = 0.0
        self.has_mic = False
        self._pending = np.zeros(0, dtype=np.int16)

    # -- lifecycle
    def start(self, input_device=None, output_device=None) -> list[str]:
        """Open the devices. Returns a list of problems (empty if all good). Raises if
        there's no way to play audio at all."""
        problems = []
        self.running = True
        self.sender = threading.Thread(target=self._send_loop, daemon=True)
        self.sender.start()
        if not HAVE_AUDIO:
            return ["Audio devices unavailable"]
        self.out_stream = sd.OutputStream(samplerate=RATE, channels=1, dtype="int16",
                                          device=output_device, callback=self._out_cb, latency=DEVICE_LATENCY)
        self.out_stream.start()
        try:
            self.in_stream = sd.InputStream(samplerate=RATE, channels=1, dtype="int16", blocksize=FRAME,
                                            device=input_device, callback=self._in_cb, latency=DEVICE_LATENCY)
            self.in_stream.start()
            self.has_mic = True
        except Exception as e:
            self.in_stream = None
            problems.append(f"No microphone ({e}) - you can listen but not talk")
        return problems

    def stop(self):
        self.running = False
        for st in (self.in_stream, self.out_stream):
            if st is not None:
                try:
                    st.stop()
                    st.close()
                except Exception:
                    pass
        self.in_stream = self.out_stream = None
        self.mixer.clear()
        self.decoders.clear()
        try:
            self.outq.put_nowait(None)
        except queue.Full:
            pass

    # -- receiving
    def receive(self, user: str, b64: str):
        if self.deaf:
            return
        try:
            data = base64.b64decode(b64)
        except ValueError:
            return
        dec = self.decoders.get(user)
        if dec is None:
            dec = self.decoders[user] = Decoder()
        try:
            for pcm in dec.decode(data):
                self.mixer.push(user, pcm)
        except Exception:
            self.decoders.pop(user, None)        # corrupt stream: start fresh next packet

    def forget(self, user: str):
        self.decoders.pop(user, None)
        self.mixer.remove(user)

    def set_volume(self, user: str, volume: float):
        self.mixer.volume[user] = volume

    def speaking(self) -> set[str]:
        return self.mixer.speaking()

    def transmitting(self) -> bool:
        return time.monotonic() - self.last_sent < 0.3

    # -- microphone
    def process_mic(self, pcm):
        """Handle one chunk of microphone audio (any length). Public so tests can feed it."""
        self._pending = np.concatenate([self._pending, pcm.astype(np.int16).reshape(-1)])
        while len(self._pending) >= FRAME:
            frame, self._pending = self._pending[:FRAME], self._pending[FRAME:]
            self._process_frame(frame)

    def _process_frame(self, frame):
        db = level_db(frame)
        self.mic_db = db
        now = time.monotonic()
        if self.muted or self.deaf:
            return
        if self.mode == "ptt":
            if not self.ptt_down:
                return
        else:
            if db >= self.threshold_db:
                self._open_until = now + HANGOVER
            if now > getattr(self, "_open_until", 0):
                return
        try:
            self.outq.put_nowait(frame.copy())
        except queue.Full:                       # network can't keep up: drop the oldest frame
            try:
                self.outq.get_nowait()
                self.outq.put_nowait(frame.copy())
            except (queue.Empty, queue.Full):
                pass

    def _send_loop(self):
        while self.running:
            frame = self.outq.get()
            if frame is None:
                break
            try:
                for pkt in self.encoder.encode(frame):
                    self.send(base64.b64encode(pkt).decode())
                    self.last_sent = time.monotonic()
            except Exception:
                pass

    # -- device callbacks (run on PortAudio's thread)
    def _in_cb(self, indata, frames, t, status):
        self.process_mic(indata[:, 0])

    def _out_cb(self, outdata, frames, t, status):
        if self.deaf:
            outdata[:, 0] = 0
            self.mixer.mix(frames)               # keep draining so we don't replay stale audio
            return
        outdata[:, 0] = self.mixer.mix(frames)
