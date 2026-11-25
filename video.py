"""
Camera and screen sharing for Campfire.

Frames are captured with Qt (camera via Qt Multimedia, screen via QScreen.grabWindow),
scaled down, compressed with VP8 and sent through the chat connection as base64. Encoding
and decoding happen on background threads so the window stays responsive.

Needs: numpy and av (already required for voice).
"""

from __future__ import annotations

import base64
import fractions
import queue
import threading
import time

from PyQt6.QtCore import QObject, Qt, QTimer, pyqtSignal
from PyQt6.QtGui import QGuiApplication, QImage

try:
    import numpy as np
    import av
    HAVE_CODEC = True
except ImportError:                              # pragma: no cover
    np = av = None
    HAVE_CODEC = False

try:
    from PyQt6.QtMultimedia import QCamera, QMediaCaptureSession, QMediaDevices, QVideoSink
    HAVE_CAMERA_API = True
except ImportError:                              # pragma: no cover
    HAVE_CAMERA_API = False

CODEC = "libvpx"                                 # VP8

CAMERA = {"size": (640, 360), "fps": 15, "bitrate": 600_000}
SCREEN_SIZES = {"720p": (1280, 720), "1080p": (1920, 1080)}
SCREEN_FPS = (5, 15, 30)
KEYFRAME_EVERY = 4                               # seconds between automatic keyframes


def screen_bitrate(size: str, fps: int) -> int:
    base = {"720p": 1_500_000, "1080p": 3_000_000}.get(size, 1_500_000)
    return int(base * max(fps, 5) / 15)


def list_cameras():
    """[(id, description)] of cameras Qt can see."""
    if not HAVE_CAMERA_API:
        return []
    try:
        return [(bytes(d.id()).decode(errors="replace"), d.description()) for d in QMediaDevices.videoInputs()]
    except Exception:
        return []


def list_screens():
    return [(i, f"{s.name()}  ({s.size().width()}x{s.size().height()})")
            for i, s in enumerate(QGuiApplication.screens())]


# ---------------------------------------------------------------- image helpers
def qimage_to_rgb(img: QImage, max_w: int, max_h: int):
    """Scale to fit max_w x max_h (never up), make the size even, return an RGB numpy array."""
    if img.width() > max_w or img.height() > max_h:
        img = img.scaled(max_w, max_h, Qt.AspectRatioMode.KeepAspectRatio,
                         Qt.TransformationMode.SmoothTransformation)
    w, h = img.width() - img.width() % 2, img.height() - img.height() % 2
    if (w, h) != (img.width(), img.height()):
        img = img.copy(0, 0, w, h)
    img = img.convertToFormat(QImage.Format.Format_RGB888)
    ptr = img.constBits()
    ptr.setsize(img.sizeInBytes())
    rows = np.frombuffer(ptr, np.uint8).reshape(h, img.bytesPerLine())
    return rows[:, : w * 3].reshape(h, w, 3).copy()


def rgb_to_qimage(arr) -> QImage:
    arr = np.ascontiguousarray(arr)
    h, w, _ = arr.shape
    return QImage(arr.data, w, h, 3 * w, QImage.Format.Format_RGB888).copy()


# ---------------------------------------------------------------- codec
class VideoEncoder:
    def __init__(self, width, height, fps, bitrate):
        self.size = (width, height)
        self.ctx = av.CodecContext.create(CODEC, "w")
        self.ctx.width, self.ctx.height = width, height
        self.ctx.pix_fmt = "yuv420p"
        self.ctx.bit_rate = bitrate
        self.ctx.time_base = fractions.Fraction(1, fps)
        self.ctx.framerate = fractions.Fraction(fps, 1)
        self.ctx.gop_size = fps * KEYFRAME_EVERY
        self.ctx.options = {"deadline": "realtime", "cpu-used": "8", "lag-in-frames": "0",
                            "error-resilient": "1"}
        self.pts = 0

    def encode(self, rgb, force_key=False) -> list[tuple[bytes, bool]]:
        frame = av.VideoFrame.from_ndarray(rgb, format="rgb24").reformat(format="yuv420p")
        frame.pts = self.pts
        self.pts += 1
        if force_key:
            frame.pict_type = av.video.frame.PictureType.I
        return [(bytes(p), bool(p.is_keyframe)) for p in self.ctx.encode(frame)]


class VideoDecoder:
    def __init__(self):
        self.ctx = av.CodecContext.create(CODEC, "r")

    def decode(self, data: bytes):
        return [f.to_ndarray(format="rgb24") for f in self.ctx.decode(av.Packet(data))]


# ---------------------------------------------------------------- sending
class Publisher:
    """Takes QImages from a capture source and sends compressed frames on a background thread.
    send(kind, base64_data, is_keyframe) is called from that thread."""

    def __init__(self, kind, send, max_size, fps, bitrate):
        self.kind, self.send = kind, send
        self.max_size, self.fps, self.bitrate = max_size, fps, bitrate
        self.q: queue.Queue = queue.Queue(maxsize=2)
        self.want_key = True
        self.encoder: VideoEncoder | None = None
        self.running = True
        self.frames_sent = 0
        self.thread = threading.Thread(target=self._run, daemon=True)
        self.thread.start()

    def push(self, img: QImage):
        try:
            self.q.put_nowait(img)
        except queue.Full:                       # encoder is behind: replace the oldest frame
            try:
                self.q.get_nowait()
            except queue.Empty:
                pass
            try:
                self.q.put_nowait(img)
            except queue.Full:
                pass

    def request_keyframe(self):
        self.want_key = True

    def stop(self):
        self.running = False
        try:
            self.q.put_nowait(None)
        except queue.Full:
            pass

    def _run(self):
        while self.running:
            img = self.q.get()
            if img is None or not self.running:
                break
            try:
                rgb = qimage_to_rgb(img, *self.max_size)
                h, w, _ = rgb.shape
                if self.encoder is None or self.encoder.size != (w, h):
                    self.encoder = VideoEncoder(w, h, self.fps, self.bitrate)
                    self.want_key = True
                key, self.want_key = self.want_key, False
                for data, is_key in self.encoder.encode(rgb, force_key=key):
                    self.send(self.kind, base64.b64encode(data).decode(), is_key)
                    self.frames_sent += 1
            except Exception:
                self.encoder = None              # rebuild on the next frame


# ---------------------------------------------------------------- capture
class CameraSource(QObject):
    frame = pyqtSignal(QImage)
    error = pyqtSignal(str)

    def __init__(self, fps=CAMERA["fps"]):
        super().__init__()
        self.fps = fps
        self.last = 0.0
        self.camera = self.session = self.sink = None

    def start(self, device_id: str | None = None) -> bool:
        if not HAVE_CAMERA_API:
            self.error.emit("Camera support isn't available in this PyQt6 install")
            return False
        devices = QMediaDevices.videoInputs()
        if not devices:
            self.error.emit("No camera found")
            return False
        dev = next((d for d in devices if bytes(d.id()).decode(errors="replace") == device_id),
                   QMediaDevices.defaultVideoInput())
        self.camera = QCamera(dev)
        fmt = self.pick_format(dev)
        if fmt is not None:
            self.camera.setCameraFormat(fmt)
        self.session = QMediaCaptureSession()
        self.session.setCamera(self.camera)
        self.sink = QVideoSink()
        self.session.setVideoSink(self.sink)
        self.sink.videoFrameChanged.connect(self._on_frame)
        self.camera.errorOccurred.connect(lambda _e, text: self.error.emit(f"Camera error: {text}"))
        self.camera.start()
        return True

    @staticmethod
    def pick_format(dev):
        """The camera mode closest to 720p that can do our frame rate."""
        best, best_score = None, None
        for f in dev.videoFormats():
            res = f.resolution()
            if f.maxFrameRate() < 10 or res.width() < 320:
                continue
            score = abs(res.width() * res.height() - 1280 * 720)
            if best is None or score < best_score:
                best, best_score = f, score
        return best

    def _on_frame(self, vf):
        now = time.monotonic()
        if now - self.last < 1 / self.fps:
            return
        self.last = now
        img = vf.toImage()
        if not img.isNull():
            self.frame.emit(img)

    def stop(self):
        if self.camera is not None:
            self.camera.stop()
        self.camera = self.session = self.sink = None


class ScreenSource(QObject):
    frame = pyqtSignal(QImage)
    error = pyqtSignal(str)

    def __init__(self, screen_index=0, fps=15):
        super().__init__()
        self.screen_index, self.fps = screen_index, fps
        self.timer = QTimer(self)
        self.timer.timeout.connect(self._grab)

    def start(self) -> bool:
        screens = QGuiApplication.screens()
        if not screens:
            self.error.emit("No screen to share")
            return False
        self.screen = screens[min(self.screen_index, len(screens) - 1)]
        self.timer.start(int(1000 / self.fps))
        self._grab()
        return True

    def _grab(self):
        img = self.screen.grabWindow(0).toImage()
        if not img.isNull():
            self.frame.emit(img)

    def stop(self):
        self.timer.stop()


# ---------------------------------------------------------------- receiving
class Receiver(QObject):
    """Decodes incoming streams on a background thread; emits (user, kind, QImage)."""

    frame = pyqtSignal(str, str, QImage)

    def __init__(self):
        super().__init__()
        self.q: queue.Queue = queue.Queue(maxsize=90)
        self.decoders: dict[tuple, VideoDecoder] = {}
        self.need_key: set[tuple] = set()
        self.running = True
        self.lock = threading.Lock()
        self.thread = threading.Thread(target=self._run, daemon=True)
        self.thread.start()

    def push(self, user, kind, b64, key):
        try:
            self.q.put_nowait((user, kind, b64, key))
        except queue.Full:                       # can't keep up: this stream waits for a keyframe
            with self.lock:
                self.need_key.add((user, kind))

    def reset(self, user, kind):
        with self.lock:
            self.decoders.pop((user, kind), None)
            self.need_key.discard((user, kind))

    def stop(self):
        self.running = False
        try:
            self.q.put_nowait(None)
        except queue.Full:
            pass

    def _run(self):
        while self.running:
            item = self.q.get()
            if item is None or not self.running:
                break
            user, kind, b64, key = item
            stream = (user, kind)
            with self.lock:
                if stream in self.need_key and not key:
                    continue
                self.need_key.discard(stream)
                dec = self.decoders.get(stream)
                if dec is None or key:
                    dec = self.decoders[stream] = dec or VideoDecoder()
            try:
                frames = dec.decode(base64.b64decode(b64))
            except Exception:
                with self.lock:
                    self.decoders.pop(stream, None)
                    self.need_key.add(stream)
                continue
            if frames:
                try:
                    self.frame.emit(user, kind, rgb_to_qimage(frames[-1]))
                except RuntimeError:             # window closed
                    return
