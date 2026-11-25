"""
File uploads and downloads for Campfire.

Each transfer opens its own connection to the server (logging in with transfer=True), so a
big file never holds up chat, voice or video. If the connection drops, the transfer reconnects
and carries on from where it got to.
"""

from __future__ import annotations

import json
import os
import socket
import ssl
import threading
import time

from PyQt6.QtCore import QObject, pyqtSignal

MAX_FILE = 5 * 1024 ** 3          # must match the server
CHUNK = 1024 * 1024
RETRIES = 5
IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp"}
PREVIEW_MAX = 10 * 1024 * 1024    # images up to this size get an inline preview


class TransferError(Exception):
    """The server refused (no point retrying)."""


def human_size(n: float) -> str:
    for unit in ("bytes", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return f"{n:.0f} {unit}" if unit == "bytes" else f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} GB"


def human_time(sec: float) -> str:
    sec = int(sec)
    if sec < 60:
        return f"{sec}s"
    if sec < 3600:
        return f"{sec // 60}m {sec % 60:02d}s"
    return f"{sec // 3600}h {sec % 3600 // 60:02d}m"


class TransferJob(QObject):
    """kind "upload": path, conv, text.   kind "download": fid, dest."""

    progress = pyqtSignal(object, object)          # bytes done, total
    finished = pyqtSignal(bool, str, object)       # ok, message, result (file id or saved path)

    def __init__(self, server: tuple, user: str, password: str, kind: str, **params):
        super().__init__()
        self.host, self.port, self.use_tls, self.ca_file = server
        self.user, self.password = user, password
        self.kind = kind
        self.params = params
        self.cancelled = False
        self.sock = None
        self.done_bytes = 0
        self.total = params.get("size", 0)
        self.upload_id = os.urandom(12).hex()
        self.name = (os.path.basename(params["path"]) if kind == "upload" else params.get("name", "file"))
        self._last_emit = 0.0

    # -- control
    def start(self):
        threading.Thread(target=self._run, daemon=True).start()

    def cancel(self):
        self.cancelled = True
        if self.sock is not None:
            try:
                self.sock.close()
            except OSError:
                pass

    def _emit(self, name, *args):
        try:
            getattr(self, name).emit(*args)
        except RuntimeError:                       # window closed
            pass

    def _tick(self, force=False):
        now = time.monotonic()
        if force or now - self._last_emit > 0.15:
            self._last_emit = now
            self._emit("progress", self.done_bytes, self.total)

    # -- the work
    def _run(self):
        attempt = 0
        while True:
            try:
                result = self._attempt()
                self._tick(force=True)
                self._emit("finished", True, "", result)
                return
            except TransferError as e:
                self._emit("finished", False, str(e), None)
                return
            except Exception as e:                 # connection trouble: reconnect and resume
                if self.cancelled:
                    if self.kind == "download":
                        try:
                            os.remove(self.params["dest"] + ".part")
                        except OSError:
                            pass
                    self._emit("finished", False, "Cancelled", None)
                    return
                attempt += 1
                if attempt > RETRIES:
                    self._emit("finished", False, f"Connection lost ({e})", None)
                    return
                time.sleep(min(2 ** attempt, 20))
            finally:
                if self.sock is not None:
                    try:
                        self.sock.close()
                    except OSError:
                        pass
                    self.sock = None

    def _connect(self):
        sock = socket.create_connection((self.host, self.port), timeout=20)
        if self.use_tls:
            ctx = ssl.create_default_context(cafile=self.ca_file or None)
            sock = ctx.wrap_socket(sock, server_hostname=self.host)
        sock.settimeout(120)
        self.sock = sock
        self.rfile = sock.makefile("rb")
        hello = self._line()
        if hello.get("t") == "kicked":
            raise TransferError(hello.get("reason", "Refused by server"))
        self._send(t="login", user=self.user, transfer=True, **{"pass": self.password})
        resp = self._line()
        if resp.get("t") != "login_ok":
            raise TransferError(resp.get("reason", "Couldn't log in for the transfer"))

    def _send(self, **obj):
        self.sock.sendall((json.dumps(obj) + "\n").encode())

    def _line(self):
        line = self.rfile.readline(1 << 20)
        if not line:
            raise ConnectionError("server closed the connection")
        msg = json.loads(line)
        if msg.get("t") == "transfer_error":
            raise TransferError(msg.get("msg", "The server refused the transfer"))
        return msg

    def _attempt(self):
        if self.cancelled:
            raise ConnectionError("cancelled")
        self._connect()
        return self._upload() if self.kind == "upload" else self._download()

    def _upload(self):
        path = self.params["path"]
        size = os.path.getsize(path)
        self.total = size
        self._send(t="upload", id=self.upload_id, conv=self.params["conv"], name=self.name, size=size,
                   text=self.params.get("text", ""))
        ready = self._line()
        offset = ready.get("offset", 0)
        self.done_bytes = offset
        with open(path, "rb") as f:
            f.seek(offset)
            while self.done_bytes < size:
                if self.cancelled:
                    raise ConnectionError("cancelled")
                chunk = f.read(min(CHUNK, size - self.done_bytes))
                if not chunk:
                    raise TransferError("The file got shorter while uploading")
                self.sock.sendall(chunk)
                self.done_bytes += len(chunk)
                self._tick()
        done = self._line()
        return done.get("fid")

    def _download(self):
        dest = self.params["dest"]
        part = dest + ".part"
        offset = os.path.getsize(part) if os.path.exists(part) else 0
        self._send(t="download", fid=self.params["fid"], offset=offset)
        ready = self._line()
        size, offset = ready["size"], ready["offset"]
        self.total, self.done_bytes = size, offset
        with open(part, "ab" if offset else "wb") as f:
            if offset:
                f.truncate(offset)
            while self.done_bytes < size:
                if self.cancelled:
                    raise ConnectionError("cancelled")
                chunk = self.rfile.read1(min(CHUNK, size - self.done_bytes))
                if not chunk:
                    raise ConnectionError("download interrupted")
                f.write(chunk)
                self.done_bytes += len(chunk)
                self._tick()
        os.replace(part, dest)
        return dest
