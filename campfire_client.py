"""
Campfire chat client (PyQt6).

Run:  python campfire_client.py
Needs the Campfire server (server.py) running somewhere you can reach.
"""

from __future__ import annotations

import base64
import hashlib
import html
import json
import math
import os
import re
import socket
import ssl
import sys
import threading
import time

from PyQt6.QtCore import QBuffer, QByteArray, QEvent, QIODevice, QObject, QRect, QSettings, Qt, QTimer, pyqtSignal
from PyQt6.QtGui import QAction, QColor, QFont, QIcon, QImage, QPainter, QPixmap, QPen
from PyQt6.QtWidgets import (
    QApplication, QCheckBox, QComboBox, QDialog, QDialogButtonBox, QFileDialog, QFormLayout, QHBoxLayout,
    QInputDialog, QLabel, QLineEdit, QListWidget, QListWidgetItem, QMainWindow, QMenu,
    QGridLayout, QMessageBox, QProgressBar, QPushButton, QSlider, QSplitter, QStackedWidget, QTextBrowser,
    QTreeWidget, QTreeWidgetItem,
    QVBoxLayout, QWidget,
)

import video
import voice

APP_DIR = os.path.dirname(os.path.abspath(__file__))
# ---- The one server this client talks to. Change these if your server moves.
SERVER_HOST = "74.132.179.199"
SERVER_PORT = 55555
USE_TLS = False              # set True once the server runs with --certfile/--keyfile
TLS_CERT = "cert.pem"        # the server's certificate, shipped next to this file
LOGIN_NOTE = ""              # extra line on the login screen (campfire_admin.py sets this)
HELLO_TIMEOUT = 10           # seconds to wait for the server to greet us
LOCAL_HOSTS = {"localhost", "127.0.0.1", "::1"}
ADMIN_COMMANDS = {"kick", "ban", "unban", "ipban", "unipban", "mute", "unmute", "announce",
                  "clearhistory", "delroom", "resetpass", "bans", "voicekick", "delvoice"}
MAX_LINE = 1024 * 1024
URL_RE = re.compile(r"(https?://[^\s<]+)")
ADMIN_COLOR = "#f0b232"
ADMIN_BADGE = "\u2605"          # ★
GROUP_MAX = 50              # must match the server
PTT_KEYS = {
    "` (backtick)": Qt.Key.Key_QuoteLeft, "Caps Lock": Qt.Key.Key_CapsLock, "Ctrl": Qt.Key.Key_Control,
    "Alt": Qt.Key.Key_Alt, "Shift": Qt.Key.Key_Shift,
    **{f"F{i}": getattr(Qt.Key, f"Key_F{i}") for i in range(1, 13)},
}
SPEAKING_COLOR = "#23a55a"
MUTE_CHOICES = [("10 minutes", 10), ("1 hour", 60), ("1 day", 1440), ("Until unmuted", 0)]

settings = QSettings("Campfire", "Campfire")


def asset(name: str) -> str:
    return os.path.join(APP_DIR, name)


# ====================================================================== network
class Connection(QObject):
    """One TCP (optionally TLS) connection. Reads on a background thread and
    hands every message to the GUI thread through Qt signals."""

    connected = pyqtSignal()
    failed = pyqtSignal(str)
    received = pyqtSignal(dict)
    disconnected = pyqtSignal(str)

    def __init__(self, host: str, port: int, use_tls=False, ca_file: str | None = None):
        super().__init__()
        self.host, self.port = host, port
        self.use_tls, self.ca_file = use_tls, ca_file
        self.sock: socket.socket | None = None
        self.lock = threading.Lock()
        self.closing = False
        self.seq = 0

    def start(self):
        threading.Thread(target=self._run, daemon=True).start()

    def _emit(self, signal_name, *args):
        try:
            getattr(self, signal_name).emit(*args)
        except RuntimeError:            # the window went away while we were reading
            pass

    def _run(self):
        try:
            sock = socket.create_connection((self.host, self.port), timeout=8)
            if self.use_tls:
                ctx = ssl.create_default_context(cafile=self.ca_file or None)
                sock = ctx.wrap_socket(sock, server_hostname=self.host)
            sock.settimeout(None)
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_KEEPALIVE, 1)
            self.sock = sock
        except Exception as e:
            self._emit("failed", friendly_error(e))
            return
        self._emit("connected")

        reason = "Connection closed by server"
        try:
            stream = sock.makefile("rb")
            while True:
                line = stream.readline(MAX_LINE)
                if not line:
                    break
                try:
                    msg = json.loads(line)
                except ValueError:
                    continue
                if isinstance(msg, dict):
                    self.seq += 1
                    msg["_seq"] = self.seq          # lets a new window skip messages it already saw
                    self._emit("received", msg)
        except Exception as e:
            reason = friendly_error(e)
        if not self.closing:
            self._emit("disconnected", reason)

    def send(self, **msg):
        data = (json.dumps(msg, separators=(",", ":")) + "\n").encode()
        try:
            with self.lock:
                self.sock.sendall(data)
            return True
        except Exception:
            return False

    def close(self):
        self.closing = True
        if self.sock:
            try:
                self.sock.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            self.sock.close()


def friendly_error(e: Exception) -> str:
    if isinstance(e, socket.timeout):
        return "Server didn't answer (timed out)"
    if isinstance(e, ConnectionRefusedError):
        return "Server is not running at that address"
    if isinstance(e, socket.gaierror):
        return "Couldn't find that server address"
    if isinstance(e, ssl.SSLError):
        return f"TLS error: {e.reason or e}"
    return str(e) or e.__class__.__name__


# ====================================================================== helpers
def name_color(name: str) -> str:
    palette = ["#ff8a65", "#ffd54f", "#81c784", "#4fc3f7", "#ba68c8", "#f06292", "#4db6ac", "#aed581"]
    return palette[int(hashlib.md5(name.lower().encode()).hexdigest(), 16) % len(palette)]


def default_avatar(name: str, size=64) -> QPixmap:
    pm = QPixmap(size, size)
    pm.fill(Qt.GlobalColor.transparent)
    p = QPainter(pm)
    p.setRenderHint(QPainter.RenderHint.Antialiasing)
    p.setBrush(QColor(name_color(name)))
    p.setPen(Qt.PenStyle.NoPen)
    p.drawEllipse(0, 0, size, size)
    p.setPen(QColor("#1e1f22"))
    f = QFont()
    f.setBold(True)
    f.setPixelSize(size // 2)
    p.setFont(f)
    p.drawText(pm.rect(), Qt.AlignmentFlag.AlignCenter, name[:1].upper())
    p.end()
    return pm


def avatar_icon(name: str, data: str) -> QIcon:
    if data:
        pm = QPixmap()
        try:
            if pm.loadFromData(base64.b64decode(data)):
                return QIcon(pm)
        except ValueError:
            pass
    return QIcon(default_avatar(name))


def encode_avatar(path: str) -> str | None:
    img = QImage(path)
    if img.isNull():
        return None
    side = min(img.width(), img.height())
    img = img.copy((img.width() - side) // 2, (img.height() - side) // 2, side, side)
    img = img.scaled(64, 64, Qt.AspectRatioMode.IgnoreAspectRatio, Qt.TransformationMode.SmoothTransformation)
    for fmt in ("PNG", "JPG"):
        buf = QByteArray()
        dev = QBuffer(buf)
        dev.open(QIODevice.OpenModeFlag.WriteOnly)
        img.save(dev, fmt, 85)
        if buf.size() < 40 * 1024:
            return base64.b64encode(bytes(buf)).decode()
    return None


def render_text(text: str) -> str:
    out = html.escape(text)
    out = URL_RE.sub(r'<a href="\1">\1</a>', out)
    return out.replace("\n", "<br>")


STYLE = """
QWidget { background: #2b2d31; color: #dbdee1; font-size: 10pt; }
QMainWindow, QDialog { background: #2b2d31; }
QListWidget { background: #232428; border: none; border-radius: 6px; padding: 4px; }
QListWidget::item { padding: 5px; border-radius: 4px; }
QListWidget::item:selected { background: #404249; color: white; }
QListWidget::item:hover { background: #35373c; }
QTreeWidget { background: #232428; border: none; border-radius: 6px; padding: 4px; }
QTreeWidget::item { padding: 3px; border-radius: 4px; }
QTreeWidget::item:selected { background: #404249; color: white; }
QTreeWidget::item:hover { background: #35373c; }
QWidget#voicebar { background: #232428; border-radius: 6px; }
QWidget#voicebar QLabel { background: transparent; }
QWidget#callbar { background: #232428; border-radius: 6px; }
QWidget#callbar QLabel { background: transparent; }
QPushButton#call { background: #248046; }
QPushButton#call:hover { background: #1a6334; }
QWidget#voicebar QPushButton { padding: 6px 4px; }
QPushButton:checked { background: #da373c; }
QPushButton#danger { background: #da373c; }
QPushButton#danger:hover { background: #a12828; }
QProgressBar { background: #1e1f22; border: none; border-radius: 3px; height: 8px; text-align: center; }
QProgressBar::chunk { background: #23a55a; border-radius: 3px; }
QComboBox { background: #383a40; border: 1px solid #1e1f22; border-radius: 6px; padding: 5px; }
QTextBrowser { background: #313338; border: none; border-radius: 6px; padding: 8px; font-size: 10.5pt; }
QLineEdit { background: #383a40; border: 1px solid #1e1f22; border-radius: 6px; padding: 8px; }
QLineEdit:focus { border: 1px solid #5865f2; }
QPushButton { background: #5865f2; color: white; border: none; border-radius: 6px; padding: 7px 14px; }
QPushButton:hover { background: #4752c4; }
QPushButton:disabled { background: #4e5058; color: #a0a0a0; }
QPushButton[flat="true"] { background: #3a3c42; }
QPushButton[flat="true"]:hover { background: #4a4c52; }
QLabel#heading { color: #949ba4; font-weight: bold; font-size: 8.5pt; padding: 6px 2px 2px 2px; }
QLabel#title { font-size: 13pt; font-weight: bold; padding: 4px; }
QLabel#status { color: #f23f42; }
QMenu { background: #111214; border: 1px solid #1e1f22; }
QMenu::item:selected { background: #5865f2; }
QSplitter::handle { background: #1e1f22; }
QStatusBar { color: #949ba4; }
"""


def plus_button(tip, slot):
    btn = QPushButton("+")
    btn.setProperty("flat", True)
    btn.setFixedSize(28, 24)
    btn.setStyleSheet("padding: 0; font-size: 12pt; font-weight: bold;")
    btn.setToolTip(tip)
    btn.clicked.connect(slot)
    return btn


def group_icon(name: str, size=64) -> QIcon:
    pm = QPixmap(size, size)
    pm.fill(Qt.GlobalColor.transparent)
    p = QPainter(pm)
    p.setRenderHint(QPainter.RenderHint.Antialiasing)
    p.setBrush(QColor(name_color(name)))
    p.setPen(Qt.PenStyle.NoPen)
    p.drawRoundedRect(0, 0, size, size, size * 0.28, size * 0.28)
    p.setBrush(QColor("#1e1f22"))
    r = size // 7                       # two little "people" heads and shoulders
    for cx in (size * 0.36, size * 0.64):
        p.drawEllipse(int(cx - r), int(size * 0.28), 2 * r, 2 * r)
        p.drawRoundedRect(int(cx - r * 1.7), int(size * 0.52), int(r * 3.4), int(r * 2.2), r, r)
    p.end()
    return QIcon(pm)


def heading(text):
    lbl = QLabel(text.upper())
    lbl.setObjectName("heading")
    return lbl


def voice_settings() -> dict:
    return {
        "input": settings.value("voice/input", None),
        "output": settings.value("voice/output", None),
        "mode": settings.value("voice/mode", "vad"),
        "threshold": float(settings.value("voice/threshold", -45)),
        "ptt_key": settings.value("voice/ptt_key", "` (backtick)"),
    }


def video_settings() -> dict:
    return {
        "camera": settings.value("video/camera", None),
        "screen_size": settings.value("video/screen_size", "720p"),
        "screen_fps": int(settings.value("video/screen_fps", 15)),
    }


def device_index(value):
    try:
        return None if value in (None, "", "default") else int(value)
    except (TypeError, ValueError):
        return None


# ====================================================================== login
class LoginWindow(QWidget):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("Campfire")
        self.setFixedWidth(340)
        self.conn: Connection | None = None
        self.chat: ChatWindow | None = None

        layout = QVBoxLayout(self)
        layout.setContentsMargins(24, 24, 24, 24)
        layout.setSpacing(10)

        if os.path.exists(asset("icon.png")):
            logo = QLabel()
            logo.setPixmap(QPixmap(asset("icon.png")).scaled(
                72, 72, Qt.AspectRatioMode.KeepAspectRatio, Qt.TransformationMode.SmoothTransformation))
            logo.setAlignment(Qt.AlignmentFlag.AlignCenter)
            layout.addWidget(logo)

        title = QLabel("Welcome to Campfire")
        title.setObjectName("title")
        title.setAlignment(Qt.AlignmentFlag.AlignCenter)
        layout.addWidget(title)
        if LOGIN_NOTE:
            note = QLabel(LOGIN_NOTE)
            note.setAlignment(Qt.AlignmentFlag.AlignCenter)
            note.setWordWrap(True)
            note.setStyleSheet("color:#f0b232; font-size:8.5pt")
            layout.addWidget(note)

        self.user_input = QLineEdit(settings.value("username", ""))
        self.user_input.setPlaceholderText("Username")
        self.pass_input = QLineEdit()
        self.pass_input.setPlaceholderText("Password")
        self.pass_input.setEchoMode(QLineEdit.EchoMode.Password)

        for w in (self.user_input, self.pass_input):
            w.returnPressed.connect(self.login)
            layout.addWidget(w)

        self.btn = QPushButton("Log in / Register")
        self.btn.clicked.connect(self.login)
        layout.addWidget(self.btn)

        hint = QLabel("New username? An account is created automatically.")
        hint.setStyleSheet("color:#949ba4; font-size:8.5pt")
        hint.setWordWrap(True)
        layout.addWidget(hint)

        self.status = QLabel("")
        self.status.setObjectName("status")
        self.status.setWordWrap(True)
        layout.addWidget(self.status)

        (self.pass_input if self.user_input.text() else self.user_input).setFocus()

    def set_busy(self, busy: bool, text=""):
        self.btn.setEnabled(not busy)
        self.btn.setText("Connecting..." if busy else "Log in / Register")
        self.show_status(text)

    def show_status(self, text):
        self.status.setText(text)
        self.status.setMinimumHeight(self.status.heightForWidth(self.width() - 48) if text else 0)
        self.adjustSize()

    def login(self):
        user, pw = self.user_input.text().strip(), self.pass_input.text()
        if not user or not pw:
            self.status.setText("Enter a username and password")
            return
        self.set_busy(True)

        if self.conn:                       # already connected (e.g. retry after wrong password)
            self.conn.send(t="login", user=user, **{"pass": pw})
            return

        conn = Connection(SERVER_HOST, SERVER_PORT, USE_TLS, asset(TLS_CERT) if USE_TLS else None)
        conn.failed.connect(self.on_failed)
        conn.connected.connect(lambda: None)
        conn.received.connect(self.on_message)
        conn.disconnected.connect(self.on_disconnected)
        self.conn = conn
        self.got_hello = False
        conn.start()
        QTimer.singleShot(HELLO_TIMEOUT * 1000, lambda: self.check_hello(conn))

    def check_hello(self, conn):
        """Connected but the server never said hello: wrong or old server, or a firewall eating it."""
        if self.conn is not conn or self.got_hello:
            return
        conn.closing = True
        conn.close()
        self.conn = None
        self.set_busy(False, "The server isn't answering. Make sure the NEW server.py is running "
                             "(the old server can't talk to this client), then try again.")

    def on_failed(self, reason):
        self.conn = None
        text = f"Couldn't connect to {SERVER_HOST}:{SERVER_PORT} - {reason}."
        if SERVER_HOST not in LOCAL_HOSTS:
            text += ("\nIs server.py running? If it's running on THIS computer, open "
                     "campfire_admin.py instead - your own public IP often doesn't work from home.")
        self.set_busy(False, text)

    def on_disconnected(self, reason):
        self.conn = None
        self.set_busy(False, f"Disconnected: {reason}")

    def stop_forwarding(self):
        conn, self.handoff = getattr(self, "handoff", None), None
        if conn is not None:
            try:
                conn.received.disconnect(self.on_message)
            except (TypeError, RuntimeError):
                pass

    def on_message(self, msg):
        if getattr(self, "handoff", None) is not None and self.sender() is self.handoff:
            if self.chat is not None:
                self.chat.handle(msg)
            return
        kind = msg.get("t")
        if kind == "kicked":                      # e.g. this address is banned
            self.conn.closing = True
            self.conn.close()
            self.conn = None
            self.set_busy(False, msg.get("reason", "Refused by server"))
        elif kind == "hello":
            self.got_hello = True
            self.conn.send(t="login", user=self.user_input.text().strip(), **{"pass": self.pass_input.text()})
        elif kind == "login_fail":
            self.set_busy(False, msg.get("reason", "Login failed"))
            self.pass_input.selectAll()
            self.pass_input.setFocus()
        elif kind == "login_ok":
            settings.setValue("username", msg["user"])
            conn, self.conn = self.conn, None
            for sig in (conn.failed, conn.disconnected):
                sig.disconnect()
            # Messages right after login_ok (friends, groups, voice...) may already be queued
            # for this window. Keep forwarding them until the chat window receives its own.
            self.handoff = conn
            self.chat = ChatWindow(conn, self.pass_input.text(), msg, handoff=self.stop_forwarding)
            self.chat.logged_out.connect(self.back_to_login)
            self.chat.show()
            self.pass_input.clear()
            self.hide()
            self.set_busy(False)

    def back_to_login(self, reason: str):
        self.chat = None
        self.show_status(reason)
        self.show()
        self.pass_input.setFocus()


# ====================================================================== settings
class SettingsDialog(QDialog):
    def __init__(self, parent: "ChatWindow"):
        super().__init__(parent)
        self.setWindowTitle("Settings")
        self.win = parent
        self.avatar_data: str | None = None

        form = QFormLayout(self)
        self.name_input = QLineEdit(parent.me)
        form.addRow("Username", self.name_input)

        row = QHBoxLayout()
        self.preview = QLabel()
        self.preview.setPixmap(parent.icon_for(parent.me).pixmap(48, 48))
        pick = QPushButton("Choose image...")
        pick.setProperty("flat", True)
        pick.clicked.connect(self.pick_avatar)
        clear = QPushButton("Remove")
        clear.setProperty("flat", True)
        clear.clicked.connect(self.clear_avatar)
        row.addWidget(self.preview)
        row.addWidget(pick)
        row.addWidget(clear)
        row.addStretch()
        form.addRow("Avatar", row)

        # ---- voice
        vs = voice_settings()
        form.addRow(heading("Voice"))
        ins, outs = voice.list_devices()
        self.in_combo, self.out_combo = QComboBox(), QComboBox()
        for combo, devices, current in ((self.in_combo, ins, vs["input"]), (self.out_combo, outs, vs["output"])):
            combo.addItem("System default", "default")
            for idx, label in devices:
                combo.addItem(label, str(idx))
            i = combo.findData(str(current)) if current not in (None, "default") else 0
            combo.setCurrentIndex(max(i, 0))
        form.addRow("Microphone", self.in_combo)
        form.addRow("Speakers", self.out_combo)

        self.mode_combo = QComboBox()
        self.mode_combo.addItem("Voice activity (talk to transmit)", "vad")
        self.mode_combo.addItem("Push to talk", "ptt")
        self.mode_combo.setCurrentIndex(max(self.mode_combo.findData(vs["mode"]), 0))
        form.addRow("Input mode", self.mode_combo)

        self.key_combo = QComboBox()
        self.key_combo.addItems(list(PTT_KEYS))
        self.key_combo.setCurrentText(vs["ptt_key"])
        self.key_label = QLabel("Push-to-talk key")
        form.addRow(self.key_label, self.key_combo)

        self.sens = QSlider(Qt.Orientation.Horizontal)
        self.sens.setRange(-70, -10)
        self.sens.setValue(int(vs["threshold"]))
        self.meter = QProgressBar()
        self.meter.setRange(-70, -10)            # same scale as the slider, so they line up
        self.meter.setTextVisible(False)
        self.meter.setFixedHeight(8)
        self.sens_label = QLabel("Sensitivity")
        sens_box = QVBoxLayout()
        sens_box.addWidget(self.sens)
        sens_box.addWidget(self.meter)
        self.meter_hint = QLabel("Join a voice channel to see your mic level. Talk: the bar should pass the slider.")
        self.meter_hint.setWordWrap(True)
        self.meter_hint.setStyleSheet("color:#949ba4; font-size:8.5pt")
        sens_box.addWidget(self.meter_hint)
        form.addRow(self.sens_label, sens_box)

        # ---- video
        vid = video_settings()
        form.addRow(heading("Camera & screen sharing"))
        self.cam_combo = QComboBox()
        self.cam_combo.addItem("System default", "default")
        for cid, label in video.list_cameras():
            self.cam_combo.addItem(label, cid)
        i = self.cam_combo.findData(vid["camera"]) if vid["camera"] else 0
        self.cam_combo.setCurrentIndex(max(i, 0))
        form.addRow("Camera", self.cam_combo)
        self.size_combo = QComboBox()
        self.size_combo.addItems(list(video.SCREEN_SIZES))
        self.size_combo.setCurrentText(vid["screen_size"])
        self.fps_combo = QComboBox()
        for f in video.SCREEN_FPS:
            self.fps_combo.addItem(f"{f} fps", f)
        self.fps_combo.setCurrentIndex(max(self.fps_combo.findData(vid["screen_fps"]), 0))
        qrow = QHBoxLayout()
        qrow.addWidget(self.size_combo)
        qrow.addWidget(self.fps_combo)
        form.addRow("Screen share", qrow)
        tip = QLabel("Higher quality uses more of your upload. 720p at 15 fps is about 1.5 Mbps per viewer.")
        tip.setWordWrap(True)
        tip.setStyleSheet("color:#949ba4; font-size:8.5pt")
        form.addRow(tip)
        self.mode_combo.currentIndexChanged.connect(self.mode_changed)
        self.mode_changed()
        if voice.missing_packages():
            warn = QLabel(f"Voice needs extra packages: {voice.missing_packages()}")
            warn.setStyleSheet("color:#f23f42")
            form.addRow(warn)
        self.meter_timer = QTimer(self, interval=60, timeout=self.update_meter)
        self.meter_timer.start()

        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Save | QDialogButtonBox.StandardButton.Cancel)
        buttons.accepted.connect(self.save)
        buttons.rejected.connect(self.reject)
        form.addRow(buttons)

    def mode_changed(self, *_):
        ptt = self.mode_combo.currentData() == "ptt"
        for w in (self.key_label, self.key_combo):
            w.setVisible(ptt)
        for w in (self.sens_label, self.sens, self.meter, self.meter_hint):
            w.setVisible(not ptt)

    def update_meter(self):
        eng = self.win.engine
        if eng is not None and eng.has_mic:
            self.meter.setValue(int(max(-70, min(-10, eng.mic_db))))
            self.meter_hint.setText("Talk normally: the green bar should go past the slider, "
                                    "and stay behind it when you're quiet.")
            eng.threshold_db = float(self.sens.value())      # live preview while dragging

    def pick_avatar(self):
        path, _ = QFileDialog.getOpenFileName(self, "Choose avatar", "", "Images (*.png *.jpg *.jpeg *.gif *.bmp *.webp)")
        if not path:
            return
        data = encode_avatar(path)
        if data is None:
            QMessageBox.warning(self, "Avatar", "Couldn't use that image.")
            return
        self.avatar_data = data
        self.preview.setPixmap(avatar_icon(self.win.me, data).pixmap(48, 48))

    def clear_avatar(self):
        self.avatar_data = ""
        self.preview.setPixmap(QIcon(default_avatar(self.win.me)).pixmap(48, 48))

    def save(self):
        name = self.name_input.text().strip()
        if name and name != self.win.me:
            self.win.conn.send(t="rename", name=name)
        if self.avatar_data is not None:
            self.win.conn.send(t="avatar", data=self.avatar_data)
        old = voice_settings()
        settings.setValue("voice/input", self.in_combo.currentData())
        settings.setValue("voice/output", self.out_combo.currentData())
        settings.setValue("voice/mode", self.mode_combo.currentData())
        settings.setValue("voice/ptt_key", self.key_combo.currentText())
        settings.setValue("voice/threshold", self.sens.value())
        settings.setValue("video/camera", self.cam_combo.currentData())
        settings.setValue("video/screen_size", self.size_combo.currentText())
        settings.setValue("video/screen_fps", self.fps_combo.currentData())
        new = voice_settings()
        self.win.apply_voice_settings(restart=(old["input"], old["output"]) != (new["input"], new["output"]))
        self.accept()


# ====================================================================== group picker
class GroupDialog(QDialog):
    """Pick people for a new group DM, or to add to an existing one."""

    def __init__(self, parent: "ChatWindow", title: str, candidates: list[str], room_left: int,
                 ask_name: bool):
        super().__init__(parent)
        self.setWindowTitle(title)
        self.resize(340, 460)
        self.room_left = room_left
        v = QVBoxLayout(self)

        self.name_input = QLineEdit()
        self.name_input.setPlaceholderText("Group name (optional)")
        self.name_input.setMaxLength(40)
        if ask_name:
            v.addWidget(self.name_input)

        v.addWidget(heading("Friends and people online"))
        self.list = QListWidget()
        for n in candidates:
            self.add_person(n, checked=False)
        self.list.itemChanged.connect(self.update_count)
        v.addWidget(self.list, 1)

        row = QHBoxLayout()
        self.extra = QLineEdit()
        self.extra.setPlaceholderText("Add someone by username")
        self.extra.returnPressed.connect(self.add_typed)
        add = QPushButton("Add")
        add.setProperty("flat", True)
        add.clicked.connect(self.add_typed)
        row.addWidget(self.extra, 1)
        row.addWidget(add)
        v.addLayout(row)

        self.count = QLabel()
        v.addWidget(self.count)
        self.buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        self.buttons.accepted.connect(self.accept)
        self.buttons.rejected.connect(self.reject)
        v.addWidget(self.buttons)
        self.update_count()

    def add_person(self, name, checked):
        for i in range(self.list.count()):
            it = self.list.item(i)
            if it.text().lower() == name.lower():
                it.setCheckState(Qt.CheckState.Checked)
                return
        it = QListWidgetItem(name)
        it.setFlags(it.flags() | Qt.ItemFlag.ItemIsUserCheckable)
        it.setCheckState(Qt.CheckState.Checked if checked else Qt.CheckState.Unchecked)
        self.list.addItem(it)

    def add_typed(self):
        name = self.extra.text().strip()
        if name:
            self.add_person(name, checked=True)
            self.extra.clear()
            self.update_count()

    def picked(self) -> list[str]:
        return [self.list.item(i).text() for i in range(self.list.count())
                if self.list.item(i).checkState() == Qt.CheckState.Checked]

    def update_count(self, *_):
        n = len(self.picked())
        over = n > self.room_left
        self.count.setText(f"{n} selected - room for {self.room_left}" + ("  (too many!)" if over else ""))
        self.count.setStyleSheet("color:#f23f42" if over else "color:#949ba4")
        self.buttons.button(QDialogButtonBox.StandardButton.Ok).setEnabled(0 < n <= self.room_left)


# ====================================================================== incoming call
class RingDialog(QDialog):
    """'alice is calling Weekend plans' with Join / Ignore. Closes itself after 30 seconds."""

    def __init__(self, parent: "ChatWindow", gid: int, caller: str, group_name: str):
        super().__init__(parent)
        self.setWindowTitle("Incoming call")
        self.gid = gid
        v = QVBoxLayout(self)
        v.setContentsMargins(20, 16, 20, 16)
        icon = QLabel()
        icon.setPixmap(group_icon(group_name).pixmap(56, 56))
        icon.setAlignment(Qt.AlignmentFlag.AlignCenter)
        v.addWidget(icon)
        title = QLabel(f"<b>{html.escape(caller)}</b> is calling<br><b>{html.escape(group_name)}</b>")
        title.setAlignment(Qt.AlignmentFlag.AlignCenter)
        title.setStyleSheet("font-size: 11pt")
        v.addWidget(title)
        row = QHBoxLayout()
        join = QPushButton("Join")
        join.setObjectName("call")
        join.clicked.connect(self.accept)
        ignore = QPushButton("Ignore")
        ignore.setProperty("flat", True)
        ignore.clicked.connect(self.reject)
        row.addWidget(join)
        row.addWidget(ignore)
        v.addLayout(row)
        QTimer.singleShot(30_000, self.reject)


# ====================================================================== video tiles
class VideoTile(QWidget):
    """One camera or screen stream. Paints the latest frame scaled to fit, with the name on it."""

    def __init__(self, win: "ChatWindow", user: str, kind: str, own: bool):
        super().__init__()
        self.win, self.user, self.kind, self.own = win, user, kind, own
        self.image: QImage | None = None
        self.watching = own
        self.setMinimumSize(240, 135)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.customContextMenuRequested.connect(self.menu)

    @property
    def key(self):
        return (self.user, self.kind)

    def set_image(self, img: QImage):
        self.image = img
        self.update()

    def label(self):
        who = "You" if self.own else self.user
        return f"\U0001f5a5  {who}'s screen" if self.kind == "screen" else f"\U0001f4f7  {who}"

    def paintEvent(self, ev):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform)
        p.fillRect(self.rect(), QColor("#111214"))
        if self.image is not None and self.watching:
            img = self.image.scaled(self.size(), Qt.AspectRatioMode.KeepAspectRatio,
                                    Qt.TransformationMode.SmoothTransformation)
            p.drawImage((self.width() - img.width()) // 2, (self.height() - img.height()) // 2, img)
        else:
            p.setPen(QColor("#b5bac1"))
            f = p.font(); f.setPointSize(11); p.setFont(f)
            text = ("Watch stream" if not self.watching
                    else "Waiting for video..." if not self.own else "Starting...")
            if not self.watching:
                r = QRect(0, 0, 160, 40)
                r.moveCenter(self.rect().center())
                p.setBrush(QColor("#5865f2")); p.setPen(Qt.PenStyle.NoPen)
                p.drawRoundedRect(r, 6, 6)
                p.setPen(QColor("white"))
                p.drawText(r, Qt.AlignmentFlag.AlignCenter, text)
            else:
                p.drawText(self.rect(), Qt.AlignmentFlag.AlignCenter, text)
        # name badge
        f = p.font(); f.setPointSize(9); f.setBold(True); p.setFont(f)
        tw = p.fontMetrics().horizontalAdvance(self.label()) + 16
        badge = QRect(8, self.height() - 30, tw, 22)
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(QColor(0, 0, 0, 160))
        p.drawRoundedRect(badge, 4, 4)
        p.setPen(QColor("white"))
        p.drawText(badge, Qt.AlignmentFlag.AlignCenter, self.label())
        if self.win.stage_focus == self.key:
            p.setPen(QPen(QColor("#5865f2"), 3))
            p.setBrush(Qt.BrushStyle.NoBrush)
            p.drawRect(self.rect().adjusted(1, 1, -2, -2))
        p.end()

    def mousePressEvent(self, ev):
        if ev.button() == Qt.MouseButton.LeftButton and not self.watching:
            self.win.watch_stream(self.user, self.kind)

    def mouseDoubleClickEvent(self, ev):
        if self.watching:
            self.win.toggle_focus(self.key)

    def menu(self, pos):
        m = QMenu(self)
        if self.watching:
            m.addAction("Focus" if self.win.stage_focus != self.key else "Show everyone",
                        lambda: self.win.toggle_focus(self.key))
            m.addAction("Pop out", lambda: self.win.pop_out(self.key))
            if not self.own and self.kind == "screen":
                m.addAction("Stop watching", lambda: self.win.unwatch_stream(self.user, self.kind))
        else:
            m.addAction("Watch stream", lambda: self.win.watch_stream(self.user, self.kind))
        m.exec(self.mapToGlobal(pos))


class VideoWindow(QWidget):
    """A stream in its own window. Double-click or F11 for full screen, Esc to leave it."""

    closed = pyqtSignal(tuple)

    def __init__(self, key, title):
        super().__init__()
        self.key = key
        self.image: QImage | None = None
        self.setWindowTitle(title)
        self.resize(1280, 720)
        self.setStyleSheet("background:#000")

    def set_image(self, img):
        self.image = img
        self.update()

    def paintEvent(self, ev):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform)
        p.fillRect(self.rect(), QColor("#000"))
        if self.image is not None:
            img = self.image.scaled(self.size(), Qt.AspectRatioMode.KeepAspectRatio,
                                    Qt.TransformationMode.SmoothTransformation)
            p.drawImage((self.width() - img.width()) // 2, (self.height() - img.height()) // 2, img)
        p.end()

    def toggle_full(self):
        self.showNormal() if self.isFullScreen() else self.showFullScreen()

    def mouseDoubleClickEvent(self, ev):
        self.toggle_full()

    def keyPressEvent(self, ev):
        if ev.key() == Qt.Key.Key_F11:
            self.toggle_full()
        elif ev.key() == Qt.Key.Key_Escape and self.isFullScreen():
            self.showNormal()
        else:
            super().keyPressEvent(ev)

    def closeEvent(self, ev):
        self.closed.emit(self.key)
        super().closeEvent(ev)


# ====================================================================== chat
class ChatWindow(QMainWindow):
    logged_out = pyqtSignal(str)

    def __init__(self, conn: Connection, password: str, login: dict, handoff=None):
        super().__init__()
        self.handoff = handoff
        self.last_seq = login.get("_seq", 0)
        self.conn = conn
        self.password = password
        self.me = login["user"]
        self.rooms: list[str] = []
        self.users: dict[str, dict] = {}            # online name -> {"avatar", "admin"}
        self.is_admin = False
        self.admin_menu: QMenu | None = None
        self.icons: dict[str, QIcon] = {}
        self.friends: list[dict] = []
        self.convs: dict[str, list[dict]] = {}       # "#room" or "@user" -> messages
        self.loaded: set[str] = set()
        self.unread: dict[str, int] = {}
        self.current = "#general"
        self.reconnect_delay = 1
        self.leaving = False
        self.pending_room: str | None = None
        self.groups: dict[int, dict] = {}            # gid -> {"name", "owner", "members"}
        self.voice_channels: list[dict] = []
        self.my_voice: str | None = None             # voice channel we're in
        self.engine: voice.VoiceEngine | None = None
        self.voice_items: dict[str, QTreeWidgetItem] = {}
        self.self_muted = False
        self.self_deaf = False
        # video
        self.sources: dict[str, QObject] = {}        # kind -> CameraSource / ScreenSource
        self.publishers: dict[str, video.Publisher] = {}
        self.receiver: video.Receiver | None = None
        self.tiles: dict[tuple, VideoTile] = {}      # (user, kind) -> tile
        self.popouts: dict[tuple, VideoWindow] = {}
        self.want_watch: set[tuple] = set()          # screen shares you clicked "Watch" on
        self.watching_sent: set[tuple] = set()       # what the server thinks we're watching
        self.stage_focus: tuple | None = None
        self.calls: dict[int, list] = {}             # gid -> users in that group's call
        self.ring: RingDialog | None = None

        self.update_title()
        self.resize(1150, 700)
        if os.path.exists(asset("icon.png")):
            self.setWindowIcon(QIcon(asset("icon.png")))
        self.build_ui()
        self.build_menu()
        self.attach(conn)
        self.apply_login(login)

        self.ping_timer = QTimer(self, interval=30_000, timeout=lambda: self.conn.send(t="ping"))
        self.ping_timer.start()
        self.speak_timer = QTimer(self, interval=120, timeout=self.update_speaking)
        QApplication.instance().installEventFilter(self)

    # ---------------------------------------------------------- ui
    def build_ui(self):
        split = QSplitter()
        self.setCentralWidget(split)

        # left: rooms + DMs
        left = QWidget()
        lv = QVBoxLayout(left)
        lv.setContentsMargins(8, 8, 4, 8)
        top = QHBoxLayout()
        top.addWidget(heading("Channels"))
        top.addWidget(plus_button("Create a channel", self.create_room))
        lv.addLayout(top)
        self.room_list = QListWidget()
        self.room_list.itemClicked.connect(lambda it: self.open_conv(it.data(Qt.ItemDataRole.UserRole)))
        self.room_list.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.room_list.customContextMenuRequested.connect(self.room_menu)
        lv.addWidget(self.room_list, 2)
        top = QHBoxLayout()
        top.addWidget(heading("Direct messages"))
        top.addWidget(plus_button(f"New group DM (up to {GROUP_MAX} people)", self.new_group))
        lv.addLayout(top)
        self.dm_list = QListWidget()
        self.dm_list.itemClicked.connect(lambda it: self.open_conv(it.data(Qt.ItemDataRole.UserRole)))
        self.dm_list.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.dm_list.customContextMenuRequested.connect(self.dm_menu)
        lv.addWidget(self.dm_list, 3)

        top = QHBoxLayout()
        top.addWidget(heading("Voice channels"))
        top.addWidget(plus_button("Create a voice channel", self.create_voice))
        lv.addLayout(top)
        self.voice_tree = QTreeWidget()
        self.voice_tree.setHeaderHidden(True)
        self.voice_tree.setRootIsDecorated(False)
        self.voice_tree.setIndentation(16)
        self.voice_tree.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.voice_tree.itemClicked.connect(self.voice_clicked)
        self.voice_tree.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.voice_tree.customContextMenuRequested.connect(self.voice_menu)
        lv.addWidget(self.voice_tree, 2)

        self.voice_bar = QWidget()
        self.voice_bar.setObjectName("voicebar")
        vb = QVBoxLayout(self.voice_bar)
        vb.setContentsMargins(8, 6, 8, 8)
        self.voice_label = QLabel()
        self.voice_label.setStyleSheet(f"color:{SPEAKING_COLOR}; font-weight:bold")
        vb.addWidget(self.voice_label)
        row = QHBoxLayout()
        self.mute_btn = QPushButton("Mute")
        self.mute_btn.setCheckable(True)
        self.mute_btn.setProperty("flat", True)
        self.mute_btn.toggled.connect(self.toggle_mute)
        self.deaf_btn = QPushButton("Deafen")
        self.deaf_btn.setCheckable(True)
        self.deaf_btn.setProperty("flat", True)
        self.deaf_btn.toggled.connect(self.toggle_deaf)
        leave = QPushButton("Leave")
        leave.setObjectName("danger")
        leave.clicked.connect(self.leave_voice)
        for b in (self.mute_btn, self.deaf_btn, leave):
            row.addWidget(b)
        vb.addLayout(row)
        self.ptt_btn = QPushButton("Hold to talk")
        self.ptt_btn.pressed.connect(lambda: self.set_ptt(True))
        self.ptt_btn.released.connect(lambda: self.set_ptt(False))
        vb.addWidget(self.ptt_btn)
        row = QHBoxLayout()
        self.cam_btn = QPushButton("Camera")
        self.cam_btn.setToolTip("Turn your camera on or off")
        self.cam_btn.setCheckable(True)
        self.cam_btn.setProperty("flat", True)
        self.cam_btn.toggled.connect(self.toggle_camera)
        self.screen_btn = QPushButton("Screen")
        self.screen_btn.setToolTip("Share your screen")
        self.screen_btn.setCheckable(True)
        self.screen_btn.setProperty("flat", True)
        self.screen_btn.toggled.connect(self.toggle_screen)
        self.stage_btn = QPushButton("Video")
        self.stage_btn.setToolTip("Show cameras and screen shares in this channel")
        self.stage_btn.clicked.connect(self.show_stage)
        for b in (self.cam_btn, self.screen_btn, self.stage_btn):
            row.addWidget(b)
        vb.addLayout(row)
        self.voice_bar.hide()
        lv.addWidget(self.voice_bar)

        # center: chat
        center = QWidget()
        cv = QVBoxLayout(center)
        cv.setContentsMargins(4, 8, 4, 8)
        self.title = QLabel()
        self.title.setObjectName("title")
        cv.addWidget(self.title)
        self.call_bar = QWidget()
        self.call_bar.setObjectName("callbar")
        cb = QHBoxLayout(self.call_bar)
        cb.setContentsMargins(10, 6, 6, 6)
        self.call_label = QLabel()
        self.call_label.setWordWrap(True)
        cb.addWidget(self.call_label, 1)
        self.call_video_btn = QPushButton("Video")
        self.call_video_btn.setProperty("flat", True)
        self.call_video_btn.clicked.connect(self.show_stage)
        cb.addWidget(self.call_video_btn)
        self.call_btn = QPushButton("Start call")
        self.call_btn.setObjectName("call")
        self.call_btn.clicked.connect(self.call_button)
        cb.addWidget(self.call_btn)
        self.call_bar.hide()
        cv.addWidget(self.call_bar)
        self.view = QTextBrowser()
        self.view.setOpenExternalLinks(True)
        self.view.document().setDefaultStyleSheet("a { color: #00a8fc; }")
        cv.addWidget(self.view, 1)
        row = QHBoxLayout()
        self.input = QLineEdit()
        self.input.setPlaceholderText("Message  (type /help for commands)")
        self.input.returnPressed.connect(self.send_input)
        send = QPushButton("Send")
        send.clicked.connect(self.send_input)
        row.addWidget(self.input, 1)
        row.addWidget(send)
        cv.addLayout(row)

        # right: people
        right = QWidget()
        rv = QVBoxLayout(right)
        rv.setContentsMargins(4, 8, 8, 8)
        self.members_box = QWidget()
        mv = QVBoxLayout(self.members_box)
        mv.setContentsMargins(0, 0, 0, 0)
        top = QHBoxLayout()
        self.members_heading = heading("Members")
        top.addWidget(self.members_heading)
        top.addWidget(plus_button("Add people to this group", self.add_to_group))
        mv.addLayout(top)
        self.member_list = QListWidget()
        self.member_list.itemDoubleClicked.connect(lambda it: self.open_dm(it.data(Qt.ItemDataRole.UserRole)))
        self.member_list.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.member_list.customContextMenuRequested.connect(lambda p: self.person_menu(self.member_list, p))
        mv.addWidget(self.member_list)
        self.members_box.hide()
        rv.addWidget(self.members_box, 3)
        self.online_heading = heading("Online")
        rv.addWidget(self.online_heading)
        self.user_list = QListWidget()
        self.user_list.itemDoubleClicked.connect(lambda it: self.open_dm(it.data(Qt.ItemDataRole.UserRole)))
        self.user_list.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.user_list.customContextMenuRequested.connect(lambda p: self.person_menu(self.user_list, p))
        rv.addWidget(self.user_list, 3)
        rv.addWidget(heading("Friends"))
        self.friend_list = QListWidget()
        self.friend_list.itemDoubleClicked.connect(lambda it: self.open_dm(it.data(Qt.ItemDataRole.UserRole)))
        self.friend_list.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.friend_list.customContextMenuRequested.connect(lambda p: self.person_menu(self.friend_list, p))
        rv.addWidget(self.friend_list, 2)
        self.req_heading = heading("Friend requests")
        rv.addWidget(self.req_heading)
        self.req_list = QListWidget()
        rv.addWidget(self.req_list, 1)
        reqrow = QHBoxLayout()
        self.accept_btn = QPushButton("Accept")
        self.decline_btn = QPushButton("Decline")
        self.decline_btn.setProperty("flat", True)
        self.accept_btn.clicked.connect(lambda: self.respond_request(True))
        self.decline_btn.clicked.connect(lambda: self.respond_request(False))
        reqrow.addWidget(self.accept_btn)
        reqrow.addWidget(self.decline_btn)
        rv.addLayout(reqrow)

        for w in (left, right):
            w.setMinimumWidth(180)
        split.addWidget(left)
        self.center_stack = QStackedWidget()
        self.center_stack.addWidget(center)
        self.center_stack.addWidget(self.build_stage())
        split.addWidget(self.center_stack)
        split.addWidget(right)
        split.setSizes([220, 700, 230])
        split.setStretchFactor(1, 1)
        self.input.setFocus()

    def build_stage(self):
        stage = QWidget()
        sv = QVBoxLayout(stage)
        sv.setContentsMargins(4, 8, 4, 8)
        top = QHBoxLayout()
        self.stage_title = QLabel()
        self.stage_title.setObjectName("title")
        top.addWidget(self.stage_title, 1)
        back = QPushButton("Back to chat")
        back.setProperty("flat", True)
        back.clicked.connect(self.show_chat)
        top.addWidget(back)
        sv.addLayout(top)
        self.stage_empty = QLabel("Nobody has their camera on or is sharing their screen yet.\n"
                                  "Use Camera or Screen in the voice bar to start.")
        self.stage_empty.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.stage_empty.setStyleSheet("color:#949ba4")
        sv.addWidget(self.stage_empty, 1)
        self.grid_host = QWidget()
        self.grid = QGridLayout(self.grid_host)
        self.grid.setSpacing(8)
        self.grid.setContentsMargins(0, 0, 0, 0)
        sv.addWidget(self.grid_host, 1)
        return stage

    def build_menu(self):
        m = self.menuBar().addMenu("&Campfire")
        for text, slot, key in (
            ("Settings...", self.open_settings, "Ctrl+,"),
            ("Add friend...", self.add_friend_prompt, None),
            ("New channel...", self.create_room, None),
            ("Log out", self.logout, None),
            ("Quit", QApplication.quit, "Ctrl+Q"),
        ):
            act = QAction(text, self)
            act.triggered.connect(slot)
            if key:
                act.setShortcut(key)
            m.addAction(act)

    def update_title(self):
        self.setWindowTitle(f"Campfire - {self.me}" + ("  (admin)" if self.is_admin else ""))

    def build_admin_menu(self):
        m = self.menuBar().addMenu("&Admin")
        for text, slot in (
            ("Send announcement...", self.announce_prompt),
            ("Show bans", lambda: self.admin("bans")),
            ("Unban user...", lambda: self.ask_then("Unban user", "Username:", lambda n: self.admin("unban", user=n))),
            ("Unban IP address...", lambda: self.ask_then("Unban IP", "IP address or username:",
                                                           lambda n: self.admin("unipban", user=n))),
            ("Clear this channel's history", lambda: self.clear_room(self.current)),
        ):
            act = QAction(text, self)
            act.triggered.connect(slot)
            m.addAction(act)
        self.admin_menu = m

    # ---------------------------------------------------------- connection
    def attach(self, conn: Connection):
        old = getattr(self, "conn", None)
        if old is not None and old is not conn:       # reconnected: new numbering
            self.last_seq = 0
        self.conn = conn
        conn.received.connect(self.on_message)
        conn.disconnected.connect(self.on_disconnected)
        conn.failed.connect(self.on_disconnected)

    def apply_login(self, login: dict):
        self.me = login["user"]
        self.reconnect_delay = 1
        self.statusBar().showMessage("Connected", 3000)
        self.users = {u["name"]: u for u in login["users"]}
        self.is_admin = bool(login.get("admin"))
        self.update_title()
        if self.is_admin and self.admin_menu is None:
            self.build_admin_menu()
        if self.admin_menu is not None:
            self.admin_menu.menuAction().setVisible(self.is_admin)
        self.icons.clear()
        self.set_rooms(login["rooms"])
        self.refresh_users()
        # anything we had cached may be stale after a reconnect
        self.loaded.clear()
        if self.current not in self.convs and self.current.startswith("#") and self.current not in self.rooms:
            self.current = "#general"
        self.open_conv(self.current)
        if self.my_voice:                       # reconnected: get back into our voice channel
            self.conn.send(t="voice_join", channel=self.my_voice, muted=self.self_muted, deaf=self.self_deaf)
        if login.get("registered"):
            self.notice("Welcome! Your account was just created. Double-click someone to DM them.")

    def on_disconnected(self, reason: str):
        if self.leaving or self.sender() is not self.conn:
            return
        self.statusBar().showMessage(f"Disconnected ({reason}) - reconnecting in {self.reconnect_delay}s...")
        QTimer.singleShot(self.reconnect_delay * 1000, self.reconnect)
        self.reconnect_delay = min(self.reconnect_delay * 2, 30)

    def reconnect(self):
        if self.leaving:
            return
        old = self.conn
        for sig in (old.received, old.disconnected, old.failed):
            try:
                sig.disconnect()
            except TypeError:
                pass
        conn = Connection(old.host, old.port, old.use_tls, old.ca_file)
        self.attach(conn)
        conn.start()

    # ---------------------------------------------------------- incoming
    def on_message(self, msg: dict):
        if self.sender() is not self.conn:          # a stale connection
            return
        if self.handoff is not None:                # our own messages are flowing now
            self.handoff()
            self.handoff = None
        self.handle(msg)

    def handle(self, msg: dict):
        seq = msg.get("_seq", 0)
        if seq and seq <= self.last_seq:            # already handled (handed over from login)
            return
        self.last_seq = seq
        kind = msg.get("t")
        if kind == "hello":
            self.conn.send(t="login", user=self.me, **{"pass": self.password})
        elif kind == "login_ok":
            self.apply_login(msg)
        elif kind == "login_fail":
            self.leave(f"Couldn't log back in: {msg.get('reason')}")
        elif kind == "kicked":
            self.leave(msg.get("reason", "Disconnected by server"))
        elif kind == "rooms":
            self.set_rooms(msg["rooms"])
        elif kind == "history":
            self.load_history(msg["room"], msg["messages"])
        elif kind == "dm_history":
            self.load_history("@" + msg["with"], msg["messages"])
        elif kind == "msg":
            self.add_message(msg["room"], msg)
        elif kind == "dm":
            other = msg["to"] if msg["from"] == self.me else msg["from"]
            self.ensure_dm(other)
            self.add_message("@" + other, {"user": msg["from"], "text": msg["text"], "ts": msg["ts"],
                                           "admin": msg.get("admin", False)})
            if msg["from"] != self.me and not self.isActiveWindow():
                QApplication.alert(self)
        elif kind == "user_join":
            self.users[msg["name"]] = msg
            self.refresh_users()
        elif kind == "user_leave":
            self.users.pop(msg["name"], None)
            self.refresh_users()
        elif kind == "user_update":
            self.user_updated(msg)
        elif kind == "renamed":
            self.me = msg["name"]
            self.update_title()
            settings.setValue("username", self.me)
            self.notice(f"You are now {self.me}")
        elif kind == "friends":
            self.set_friends(msg["friends"], msg["requests"])
        elif kind == "groups":
            self.set_groups(msg["groups"])
        elif kind == "group_created":
            self.open_conv(f"g:{msg['gid']}")
            self.input.setFocus()
        elif kind == "group_msg":
            key = f"g:{msg['gid']}"
            self.add_message(key, msg)
            if msg["user"] != self.me and not self.isActiveWindow():
                QApplication.alert(self)
        elif kind == "group_history":
            self.load_history(f"g:{msg['gid']}", msg["messages"])
        elif kind == "group_notice":
            key = f"g:{msg['gid']}"
            if key in self.convs:
                self.add_message(key, {"user": "", "text": msg["text"], "ts": msg["ts"], "kind": "event"},
                                 count_unread=False)
            else:
                self.notice(msg["text"])
        elif kind == "voice_state":
            self.set_voice_state(msg["channels"])
        elif kind == "voice_joined":
            self.voice_joined(msg["channel"])
        elif kind == "voice_left":
            self.stop_voice()
            if msg.get("reason"):
                self.notice(msg["reason"], error=True)
        elif kind == "admin_changed":
            self.is_admin = bool(msg["admin"])
            self.update_title()
            if self.is_admin and self.admin_menu is None:
                self.build_admin_menu()
            if self.admin_menu is not None:
                self.admin_menu.menuAction().setVisible(self.is_admin)
            self.notice("You're now an admin - the Admin menu is at the top" if self.is_admin
                        else "You're no longer an admin")
        elif kind == "call_state":
            self.set_call_state(msg["gid"], msg["users"])
        elif kind == "call_ring":
            self.incoming_call(msg["gid"], msg["from"], msg["name"])
        elif kind == "video_data":
            if self.receiver is not None and (msg["u"], msg["kind"]) in self.watching_sent:
                self.receiver.push(msg["u"], msg["kind"], msg["d"], msg.get("k", False))
        elif kind == "video_keyframe":
            pub = self.publishers.get(msg.get("kind"))
            if pub:
                pub.request_keyframe()
        elif kind == "video_stopped":
            self.stop_video_all()
            self.notice(f"Your video was stopped: {msg.get('reason', '')}", error=True)
        elif kind == "voice_data":
            if self.engine is not None:
                self.engine.receive(msg["u"], msg["d"])
        elif kind == "announce":
            ann = {"user": msg["from"], "text": msg["text"], "ts": msg["ts"], "kind": "announce"}
            for key in self.rooms:
                self.add_message(key, ann, count_unread=False)
            if not self.current.startswith("#"):
                self.add_message(self.current, ann, count_unread=False)
            self.statusBar().showMessage(f"Announcement from {msg['from']}: {msg['text']}", 15000)
            QApplication.alert(self)
        elif kind == "cleared":
            room = msg["room"]
            self.convs[room] = []
            if room == self.current:
                self.redraw()
                self.notice(f"{msg.get('by', 'An admin')} cleared this channel's history")
        elif kind == "muted":
            until = msg.get("until", 0)
            span = ("until an admin unmutes you" if until > time.time() + 10 * 365 * 86400
                    else "until " + time.strftime("%H:%M", time.localtime(until)))
            by = f" by {msg['by']}" if msg.get("by") else ""
            reason = f" - {msg['reason']}" if msg.get("reason") else ""
            self.notice(f"You have been muted{by} {span}{reason}", error=True)
        elif kind == "notice":
            self.notice(msg["msg"])
        elif kind == "error":
            self.notice(msg["msg"], error=True)

    def user_updated(self, msg):
        old, new = msg["old"], msg["name"]
        self.users.pop(old, None)
        self.users[new] = msg
        self.icons.pop(old, None)
        self.icons.pop(new, None)
        if old != new and "@" + old in self.convs:
            self.convs["@" + new] = self.convs.pop("@" + old)
            if self.current == "@" + old:
                self.current = "@" + new
            self.refresh_dms()
        self.refresh_users()
        self.refresh_friends()
        self.refresh_voice()

    # ---------------------------------------------------------- conversations
    def set_rooms(self, rooms):
        self.rooms = rooms
        for r in rooms:
            self.convs.setdefault(r, [])
        if self.current.startswith("#") and self.current not in rooms:
            self.convs.pop(self.current, None)
            self.open_conv("#general")
        self.refresh_rooms()
        if self.pending_room in rooms:
            room, self.pending_room = self.pending_room, None
            self.open_conv(room)

    def ensure_dm(self, name):
        if "@" + name not in self.convs:
            self.convs["@" + name] = []
            self.refresh_dms()

    def open_dm(self, name):
        if not name or name == self.me:
            return
        self.ensure_dm(name)
        self.open_conv("@" + name)
        self.input.setFocus()

    def open_conv(self, key):
        self.center_stack.setCurrentIndex(0)
        self.current = key
        self.unread.pop(key, None)
        self.convs.setdefault(key, [])
        if key not in self.loaded:
            if key.startswith("#"):
                self.conn.send(t="history", room=key)
            elif key.startswith("g:"):
                self.conn.send(t="group_history", gid=int(key[2:]))
            else:
                self.conn.send(t="dm_history", **{"with": key[1:]})
        self.update_conv_title()
        self.refresh_members()
        self.refresh_call_bar()
        self.refresh_rooms()
        self.refresh_dms()
        self.redraw()

    def update_conv_title(self):
        key = self.current
        if key.startswith("g:"):
            g = self.groups.get(int(key[2:]), {})
            n = len(g.get("members", []))
            self.title.setText(f"{g.get('name', 'Group')}   \u00b7  {n} member{'s' if n != 1 else ''}")
        else:
            self.title.setText(key if key.startswith("#") else f"@ {key[1:]}")

    # ---------------------------------------------------------- group DMs
    def set_groups(self, groups):
        self.groups = {g["id"]: g for g in groups}
        for gid in self.groups:
            self.convs.setdefault(f"g:{gid}", [])
        for gid in [g for g in self.calls if g not in self.groups]:
            self.calls.pop(gid)
        for key in [k for k in self.convs if k.startswith("g:") and int(k[2:]) not in self.groups]:
            self.convs.pop(key)                    # we left or were removed
            self.unread.pop(key, None)
            self.loaded.discard(key)
            if key == self.current:
                self.open_conv("#general")
        self.refresh_dms()
        if self.current.startswith("g:"):
            self.update_conv_title()
            self.refresh_members()

    def current_group(self):
        return self.groups.get(int(self.current[2:])) if self.current.startswith("g:") else None

    def refresh_members(self):
        g = self.current_group()
        self.members_box.setVisible(g is not None)
        if g is None:
            return
        self.members_heading.setText(f"MEMBERS - {len(g['members'])}/{GROUP_MAX}")
        self.member_list.clear()
        for name in g["members"]:
            tags = []
            if name == g["owner"]:
                tags.append("owner")
            if name in self.users:
                tags.append("online")
            it = QListWidgetItem(self.icon_for(name), name + ("  \u265b" if name == g["owner"] else ""))
            it.setData(Qt.ItemDataRole.UserRole, name)
            it.setToolTip(", ".join(tags))
            if name not in self.users:
                it.setForeground(QColor("#80848e"))
            self.member_list.addItem(it)

    def people_i_know(self, exclude=()):
        skip = {n.lower() for n in exclude} | {self.me.lower()}
        names = {f["name"] for f in self.friends} | set(self.users)
        return sorted((n for n in names if n.lower() not in skip), key=str.lower)

    def new_group(self, preselect=None):
        dlg = GroupDialog(self, "New group DM", self.people_i_know(), GROUP_MAX - 1, ask_name=True)
        for n in preselect or []:
            dlg.add_person(n, checked=True)
        dlg.update_count()
        if dlg.exec():
            self.conn.send(t="group_create", name=dlg.name_input.text().strip(), members=dlg.picked())

    def add_to_group(self, gid=None):
        gid = gid if isinstance(gid, int) else (int(self.current[2:]) if self.current.startswith("g:") else None)
        g = self.groups.get(gid)
        if not g:
            return
        room = GROUP_MAX - len(g["members"])
        if room <= 0:
            QMessageBox.information(self, "Group full", f"This group already has {GROUP_MAX} people.")
            return
        dlg = GroupDialog(self, f"Add people to {g['name']}", self.people_i_know(g["members"]), room, ask_name=False)
        if dlg.exec():
            self.conn.send(t="group_add", gid=gid, users=dlg.picked())

    def rename_group(self, gid):
        g = self.groups.get(gid)
        if g:
            name, ok = QInputDialog.getText(self, "Rename group", "Group name:", text=g["name"])
            if ok and name.strip():
                self.conn.send(t="group_rename", gid=gid, name=name.strip())

    def leave_group(self, gid):
        g = self.groups.get(gid)
        if g and QMessageBox.question(self, "Leave group", f"Leave {g['name']}?") == QMessageBox.StandardButton.Yes:
            self.conn.send(t="group_leave", gid=gid)

    def dm_menu(self, pos):
        it = self.dm_list.itemAt(pos)
        if not it:
            return
        key = it.data(Qt.ItemDataRole.UserRole)
        menu = QMenu(self)
        if key.startswith("g:"):
            gid = int(key[2:])
            g = self.groups[gid]
            menu.addAction("Add people...", lambda: self.add_to_group(gid))
            menu.addAction("Rename...", lambda: self.rename_group(gid))
            if g["owner"] == self.me and len(g["members"]) > 1:
                rm = menu.addMenu("Remove member")
                for n in g["members"]:
                    if n != self.me:
                        rm.addAction(n, lambda _=False, n=n: self.conn.send(t="group_kick", gid=gid, user=n))
            menu.addSeparator()
            menu.addAction("Leave group", lambda: self.leave_group(gid))
        else:
            name = key[1:]
            menu.addAction("Start a group with them...", lambda: self.new_group([name]))
            menu.addAction("Close", lambda: self.close_dm(key))
        menu.exec(self.dm_list.mapToGlobal(pos))

    def close_dm(self, key):
        self.convs.pop(key, None)
        self.loaded.discard(key)
        self.unread.pop(key, None)
        if key == self.current:
            self.open_conv("#general")
        self.refresh_dms()

    # ---------------------------------------------------------- voice
    def set_voice_state(self, channels):
        old_streams = set(self.channel_streams())
        self.voice_channels = channels
        self.after_voice_change(old_streams)
        self.refresh_voice()

    def after_voice_change(self, old_streams):
        new_streams = set(self.channel_streams())
        for user, kind in new_streams - old_streams:
            if kind == "screen" and self.my_voice:
                self.statusBar().showMessage(f"{user} started sharing their screen - open Video to watch", 8000)
        self.sync_video()
        if self.engine is not None and self.my_voice:
            here = {u["name"] for u in self.voice_members(self.my_voice)}
            for user in list(self.engine.decoders):
                if user not in here:
                    self.engine.forget(user)

    # ---------------------------------------------------------- group calls
    def set_call_state(self, gid, users):
        old_streams = set(self.channel_streams())
        if users:
            self.calls[gid] = users
        else:
            self.calls.pop(gid, None)
            if self.ring is not None and self.ring.gid == gid:
                self.ring.reject()
        self.after_voice_change(old_streams)
        self.refresh_dms()
        self.refresh_call_bar()

    def incoming_call(self, gid, caller, name):
        if self.my_voice == f"g:{gid}":
            return
        if self.ring is not None:
            self.ring.reject()
        QApplication.beep()
        QApplication.alert(self)
        self.ring = RingDialog(self, gid, caller, name)
        self.ring.finished.connect(lambda result, g=gid: self.ring_done(g, result))
        self.ring.show()

    def ring_done(self, gid, result):
        self.ring = None
        if result == QDialog.DialogCode.Accepted and gid in self.groups:
            self.open_conv(f"g:{gid}")
            self.join_voice(f"g:{gid}")

    def refresh_call_bar(self, talking=None):
        if not self.current.startswith("g:"):
            self.call_bar.hide()
            return
        gid = int(self.current[2:])
        users = self.calls.get(gid, [])
        in_this = self.my_voice == f"g:{gid}"
        talking = talking if talking is not None else set()
        if users:
            names = []
            for u in users:
                n = html.escape(u["name"])
                if u["name"] in talking:
                    n = f'<b style="color:{SPEAKING_COLOR}">{n}</b>'
                extra = ("\U0001f507" if u["muted"] or u.get("server_muted") else "") + \
                        ("\U0001f4f7" if u.get("camera") else "") + ("\U0001f5a5\ufe0f" if u.get("screen") else "")
                names.append(n + (f" {extra}" if extra else ""))
            self.call_label.setText(f"\U0001f50a <b>In call</b> ({len(users)}): " + ", ".join(names))
            self.call_btn.setText("Leave call" if in_this else "Join call")
            self.call_btn.setObjectName("danger" if in_this else "call")
        else:
            self.call_label.setText("No one's in a call here.")
            self.call_btn.setText("Start call")
            self.call_btn.setObjectName("call")
        self.call_btn.style().unpolish(self.call_btn)
        self.call_btn.style().polish(self.call_btn)
        self.call_video_btn.setVisible(in_this)
        self.call_bar.show()

    def call_button(self):
        if not self.current.startswith("g:"):
            return
        chan = self.current if self.current.startswith("g:") else None
        if self.my_voice == chan:
            self.leave_voice()
        else:
            self.join_voice(chan)

    def refresh_voice(self):
        self.voice_tree.clear()
        self.voice_items = {}
        for ch in self.voice_channels:
            users = ch["users"]
            top = QTreeWidgetItem([f"\U0001f50a  {ch['name']}" + (f"   {len(users)}" if users else "")])
            top.setData(0, Qt.ItemDataRole.UserRole, ("chan", ch["name"]))
            top.setToolTip(0, "Click to join" if ch["name"] != self.my_voice else "You're here")
            if ch["name"] == self.my_voice:
                f = top.font(0); f.setBold(True); top.setFont(0, f)
            self.voice_tree.addTopLevelItem(top)
            for u in users:
                flags = ""
                if u["muted"] or u.get("server_muted"):
                    flags += "  \U0001f507"
                if u["deaf"]:
                    flags += "  \U0001f3a7"
                if u.get("camera"):
                    flags += "  \U0001f4f7"
                if u.get("screen"):
                    flags += "  \U0001f5a5\ufe0f LIVE"
                child = QTreeWidgetItem([u["name"] + flags])
                child.setIcon(0, self.icon_for(u["name"]))
                child.setData(0, Qt.ItemDataRole.UserRole, ("user", u["name"]))
                tips = (["muted"] if u["muted"] else []) + (["muted by an admin"] if u.get("server_muted") else []) \
                    + (["deafened"] if u["deaf"] else [])
                child.setToolTip(0, ", ".join(tips))
                top.addChild(child)
                if ch["name"] == self.my_voice:
                    self.voice_items[u["name"]] = child
            top.setExpanded(True)
        self.update_speaking()

    def update_speaking(self):
        talking = set()
        if self.engine is not None:
            talking = self.engine.speaking()
            if self.engine.transmitting():
                talking.add(self.me)
        if self.current.startswith("g:") and self.calls.get(int(self.current[2:])):
            self.refresh_call_bar(talking)
        for name, item in self.voice_items.items():
            on = name in talking
            item.setForeground(0, QColor(SPEAKING_COLOR if on else "#dbdee1"))
            f = item.font(0)
            if f.bold() != on:
                f.setBold(on)
                item.setFont(0, f)

    def voice_clicked(self, item, _col=0):
        kind, name = item.data(0, Qt.ItemDataRole.UserRole)
        if kind == "chan":
            self.join_voice(name)

    def join_voice(self, name):
        missing = voice.missing_packages()
        if missing:
            QMessageBox.information(self, "Voice", f"Voice chat needs a few extra packages. Close Campfire, run:\n\n"
                                                   f"    {missing}\n\nthen start it again.")
            return
        if name == self.my_voice:
            return
        self.conn.send(t="voice_join", channel=name, muted=self.self_muted, deaf=self.self_deaf)

    def voice_joined(self, channel):
        first = self.my_voice is None or self.engine is None
        self.my_voice = channel
        if first:
            self.engine = voice.VoiceEngine(lambda d: self.conn.send(t="voice_data", d=d))
            self.apply_voice_settings()
            vs = voice_settings()
            try:
                problems = self.engine.start(device_index(vs["input"]), device_index(vs["output"]))
            except Exception as e:
                self.engine.stop()
                self.engine = None
                self.my_voice = None
                self.conn.send(t="voice_leave")
                self.notice(f"Couldn't open your speakers/headphones: {e}. Check Settings > Speakers.", error=True)
                return
            for p in problems:
                self.notice(p, error=True)
            self.speak_timer.start()
        else:
            self.engine.mixer.clear()             # switched channels: drop audio from the old one
            self.engine.decoders.clear()
        prefix = "In call" if channel.startswith("g:") else "Voice connected"
        name = self.groups.get(int(channel[2:]), {}).get("name", "group") if channel.startswith("g:") else channel
        self.voice_label.setText(f"{prefix}  \u00b7  {name}")
        self.voice_label.setToolTip(f"{prefix} - {name}")
        self.refresh_call_bar()
        self.voice_bar.show()
        self.refresh_voice()
        # new channel or reconnect: the server has forgotten our streams and what we watch
        self.watching_sent.clear()
        for kind, pub in self.publishers.items():
            self.conn.send(t="video_start", kind=kind)
            pub.request_keyframe()
        self.sync_video()

    def stop_voice(self):
        self.stop_video_all()
        self.clear_stage()
        if self.engine is not None:
            self.engine.stop()
            self.engine = None
        self.my_voice = None
        self.speak_timer.stop()
        self.voice_bar.hide()
        self.refresh_voice()
        self.refresh_call_bar()

    def leave_voice(self):
        self.conn.send(t="voice_leave")
        self.stop_voice()

    def apply_voice_settings(self, restart=False):
        vs = voice_settings()
        self.ptt_key = PTT_KEYS.get(vs["ptt_key"], Qt.Key.Key_QuoteLeft)
        ptt = vs["mode"] == "ptt"
        self.ptt_btn.setVisible(ptt)
        self.ptt_btn.setText(f"Hold to talk  ({vs['ptt_key'].split(' ')[0]})")
        if self.engine is None:
            return
        self.engine.mode = vs["mode"]
        self.engine.threshold_db = vs["threshold"]
        self.engine.muted = self.self_muted
        self.engine.deaf = self.self_deaf
        for key in settings.allKeys():
            if key.startswith("voice_volume/"):
                self.engine.set_volume(key.split("/", 1)[1], int(settings.value(key)) / 100)
        if restart and self.my_voice:
            chan = self.my_voice
            self.engine.stop()
            self.engine = None
            self.voice_joined(chan)

    def send_voice_status(self):
        if self.my_voice:
            self.conn.send(t="voice_status", muted=self.self_muted, deaf=self.self_deaf)

    def toggle_mute(self, on):
        self.self_muted = on
        self.mute_btn.setText("Unmute" if on else "Mute")
        if self.engine:
            self.engine.muted = on
        self.send_voice_status()

    def toggle_deaf(self, on):
        self.self_deaf = on
        self.deaf_btn.setText("Undeafen" if on else "Deafen")
        if self.engine:
            self.engine.deaf = on
            self.engine.mixer.clear()
        self.send_voice_status()

    def set_ptt(self, down):
        if self.engine is not None:
            self.engine.ptt_down = down

    def eventFilter(self, obj, ev):
        if (self.engine is not None and self.engine.mode == "ptt"
                and ev.type() in (QEvent.Type.KeyPress, QEvent.Type.KeyRelease)
                and ev.key() == getattr(self, "ptt_key", None)):
            if not ev.isAutoRepeat():
                self.set_ptt(ev.type() == QEvent.Type.KeyPress)
            return True                          # don't type the push-to-talk key into the chat box
        return super().eventFilter(obj, ev)

    def create_voice(self):
        self.ask_then("New voice channel", "Voice channel name:", lambda n: self.conn.send(t="voice_create", name=n))

    def voice_menu(self, pos):
        it = self.voice_tree.itemAt(pos)
        if not it:
            return
        kind, name = it.data(0, Qt.ItemDataRole.UserRole)
        menu = QMenu(self)
        if kind == "chan":
            if name != self.my_voice:
                menu.addAction("Join", lambda: self.join_voice(name))
            else:
                menu.addAction("Leave", self.leave_voice)
            if self.is_admin:
                menu.addAction("Delete voice channel...", lambda: self.delete_voice(name))
        elif name != self.me:
            menu.addAction("Volume...", lambda: self.set_user_volume(name))
            menu.addAction("Message", lambda: self.open_dm(name))
            if self.is_admin and not self.users.get(name, {}).get("admin"):
                menu.addAction("Disconnect from voice", lambda: self.admin("voicekick", user=name))
        if not menu.isEmpty():
            menu.exec(self.voice_tree.mapToGlobal(pos))

    def set_user_volume(self, name):
        current = int(settings.value(f"voice_volume/{name}", 100))
        vol, ok = QInputDialog.getInt(self, f"Volume for {name}", "Volume (%):", current, 0, 200, 10)
        if ok:
            settings.setValue(f"voice_volume/{name}", vol)
            if self.engine:
                self.engine.set_volume(name, vol / 100)

    def delete_voice(self, name):
        if QMessageBox.question(self, "Delete voice channel", f"Delete {name}?") == QMessageBox.StandardButton.Yes:
            self.admin("delvoice", room=name)

    # ---------------------------------------------------------- camera & screen sharing
    def voice_members(self, chan):
        """Users in a voice channel or group call."""
        if not chan:
            return []
        if chan.startswith("g:"):
            return self.calls.get(int(chan[2:]), [])
        ch = next((c for c in self.voice_channels if c["name"] == chan), None)
        return ch["users"] if ch else []

    def voice_display(self, chan):
        if chan and chan.startswith("g:"):
            return "Call \u00b7 " + self.groups.get(int(chan[2:]), {}).get("name", "group")
        return chan or ""

    def channel_streams(self):
        """[(user, kind)] being streamed by others in our voice channel or call."""
        out = []
        for u in self.voice_members(self.my_voice):
            if u["name"] == self.me:
                continue
            for kind in ("camera", "screen"):
                if u.get(kind):
                    out.append((u["name"], kind))
        return out

    def video_send(self, kind, data, key):
        self.conn.send(t="video_data", kind=kind, d=data, k=key)

    def ensure_receiver(self):
        if self.receiver is None:
            self.receiver = video.Receiver()
            self.receiver.frame.connect(self.on_video_frame)

    def sync_video(self):
        """Make tiles and server subscriptions match what's being streamed in our channel."""
        streams = set(self.channel_streams()) if self.my_voice else set()
        own = {(self.me, k) for k in self.publishers}
        wanted_tiles = streams | own
        for key in list(self.tiles):
            if key not in wanted_tiles:
                tile = self.tiles.pop(key)
                tile.setParent(None)
                tile.deleteLater()
                if key in self.popouts:
                    self.popouts.pop(key).close()
                if self.receiver:
                    self.receiver.reset(*key)
                if self.stage_focus == key:
                    self.stage_focus = None
        self.want_watch &= streams
        for key in sorted(wanted_tiles - set(self.tiles), key=lambda k: (k[0] != self.me, k)):
            self.tiles[key] = VideoTile(self, key[0], key[1], own=key in own)
        # cameras are watched automatically, screen shares only when you click Watch
        desired = {k for k in streams if k[1] == "camera"} | self.want_watch
        if desired:
            self.ensure_receiver()
        for user, kind in desired - self.watching_sent:
            self.conn.send(t="video_watch", user=user, kind=kind)
        for user, kind in self.watching_sent - desired:
            self.conn.send(t="video_unwatch", user=user, kind=kind)
            if self.receiver:
                self.receiver.reset(user, kind)
        self.watching_sent = desired & streams
        for key, tile in self.tiles.items():
            tile.watching = tile.own or key in self.watching_sent
            tile.update()
        self.layout_stage()

    def layout_stage(self):
        while self.grid.count():
            self.grid.takeAt(0)
        tiles = list(self.tiles.values())
        if self.stage_focus in self.tiles:
            tiles = [self.tiles[self.stage_focus]]
        for t in self.tiles.values():
            t.setVisible(t in tiles)
        cols = max(1, math.ceil(math.sqrt(len(tiles))))
        for i, t in enumerate(tiles):
            self.grid.addWidget(t, i // cols, i % cols)
        self.stage_empty.setVisible(not self.tiles)
        self.grid_host.setVisible(bool(self.tiles))
        n = len(self.tiles)
        self.stage_title.setText(f"\U0001f50a  {self.voice_display(self.my_voice)}" + (f"   \u00b7  {n} video" if n else ""))
        self.stage_btn.setText(f"Video ({n})" if n else "Video")

    def clear_stage(self):
        for key in list(self.popouts):
            self.popouts.pop(key).close()
        for tile in self.tiles.values():
            tile.setParent(None)
            tile.deleteLater()
        self.tiles.clear()
        self.want_watch.clear()
        self.watching_sent.clear()
        self.stage_focus = None
        if self.receiver is not None:
            self.receiver.stop()
            self.receiver = None
        self.layout_stage()
        if self.center_stack.currentIndex() == 1:
            self.show_chat()

    def show_stage(self):
        self.layout_stage()
        self.center_stack.setCurrentIndex(1)

    def show_chat(self):
        self.center_stack.setCurrentIndex(0)

    def on_video_frame(self, user, kind, img):
        key = (user, kind)
        if key in self.tiles and key in self.watching_sent:
            self.tiles[key].set_image(img)
        if key in self.popouts:
            self.popouts[key].set_image(img)

    def on_local_frame(self, kind, img):
        key = (self.me, kind)
        if key in self.tiles:
            self.tiles[key].set_image(img)
        if key in self.popouts:
            self.popouts[key].set_image(img)

    def watch_stream(self, user, kind):
        self.want_watch.add((user, kind))
        self.sync_video()

    def unwatch_stream(self, user, kind):
        self.want_watch.discard((user, kind))
        self.sync_video()
        tile = self.tiles.get((user, kind))
        if tile:
            tile.image = None
            tile.update()

    def toggle_focus(self, key):
        self.stage_focus = None if self.stage_focus == key else key
        self.layout_stage()
        for t in self.tiles.values():
            t.update()

    def pop_out(self, key):
        if key in self.popouts:
            self.popouts[key].raise_()
            return
        tile = self.tiles.get(key)
        w = VideoWindow(key, f"Campfire - {tile.label() if tile else key[0]}")
        w.closed.connect(lambda k: self.popouts.pop(k, None))
        if tile and tile.image is not None:
            w.set_image(tile.image)
        self.popouts[key] = w
        w.show()

    def start_source(self, kind, source, max_size, fps, bitrate):
        if not self.my_voice:
            return False
        source.frame.connect(lambda img, k=kind: self.publishers[k].push(img) if k in self.publishers else None)
        source.frame.connect(lambda img, k=kind: self.on_local_frame(k, img))
        source.error.connect(lambda text, k=kind: self.video_error(k, text))
        self.publishers[kind] = video.Publisher(kind, self.video_send, max_size, fps, bitrate)
        self.sources[kind] = source
        if not source.start(*([] if kind == "screen" else [video_settings()["camera"]])):
            self.stop_video(kind)
            return False
        self.conn.send(t="video_start", kind=kind)
        self.sync_video()
        self.show_stage()
        return True

    def stop_video(self, kind, tell_server=True):
        src = self.sources.pop(kind, None)
        if src is not None:
            src.stop()
        pub = self.publishers.pop(kind, None)
        if pub is not None:
            pub.stop()
            if tell_server and self.my_voice:
                self.conn.send(t="video_stop", kind=kind)
        btn = self.cam_btn if kind == "camera" else self.screen_btn
        btn.blockSignals(True)
        btn.setChecked(False)
        btn.blockSignals(False)
        self.sync_video()

    def stop_video_all(self):
        for kind in list(self.publishers):
            self.stop_video(kind)

    def video_error(self, kind, text):
        self.notice(text, error=True)
        if kind in self.sources:
            self.stop_video(kind)

    def toggle_camera(self, on):
        if not on:
            self.stop_video("camera")
            return
        if not video.HAVE_CODEC:
            self.notice(f"Video needs: {voice.missing_packages() or 'pip install numpy av'}", error=True)
            self.stop_video("camera")
            return
        c = video.CAMERA
        if not self.start_source("camera", video.CameraSource(c["fps"]), c["size"], c["fps"], c["bitrate"]):
            self.stop_video("camera", tell_server=False)

    def toggle_screen(self, on):
        if not on:
            self.stop_video("screen")
            return
        if not video.HAVE_CODEC:
            self.notice(f"Video needs: {voice.missing_packages() or 'pip install numpy av'}", error=True)
            self.stop_video("screen", tell_server=False)
            return
        screens = video.list_screens()
        index = 0
        if len(screens) > 1:
            names = [label for _, label in screens]
            choice, ok = QInputDialog.getItem(self, "Share screen", "Which screen?", names, 0, False)
            if not ok:
                self.stop_video("screen", tell_server=False)
                return
            index = names.index(choice)
        vs = video_settings()
        size = video.SCREEN_SIZES.get(vs["screen_size"], video.SCREEN_SIZES["720p"])
        fps = vs["screen_fps"]
        if not self.start_source("screen", video.ScreenSource(index, fps), size, fps,
                                 video.screen_bitrate(vs["screen_size"], fps)):
            self.stop_video("screen", tell_server=False)

    def load_history(self, key, messages):
        live = [m for m in self.convs.get(key, []) if m.get("live")]
        seen = {(m["user"], m["text"], m["ts"]) for m in messages}
        self.convs[key] = messages + [m for m in live if (m["user"], m["text"], m["ts"]) not in seen]
        self.loaded.add(key)
        if key == self.current:
            self.redraw()

    def add_message(self, key, msg, count_unread=True):
        msg = {"user": msg["user"], "text": msg["text"], "ts": msg["ts"], "live": True,
               "admin": msg.get("admin", False), "kind": msg.get("kind", "")}
        self.convs.setdefault(key, []).append(msg)
        if key == self.current:
            self.view.append(self.format(msg))
            self.scroll_down()
        elif count_unread and msg["user"] != self.me:
            self.unread[key] = self.unread.get(key, 0) + 1
            self.refresh_rooms() if key.startswith("#") else self.refresh_dms()

    def notice(self, text, error=False):
        color = "#f23f42" if error else "#949ba4"
        self.view.append(f'<div style="color:{color}"><i>{html.escape(text)}</i></div>')
        self.scroll_down()

    def format(self, m):
        when = time.strftime("%H:%M", time.localtime(m["ts"]))
        if m.get("kind") == "announce":
            return (f'<table width="100%" cellspacing="0" cellpadding="8" style="margin-top:6px; margin-bottom:6px">'
                    f'<tr><td width="4" bgcolor="{ADMIN_COLOR}"></td><td bgcolor="#3d3420">'
                    f'<b style="color:{ADMIN_COLOR}">{ADMIN_BADGE} Announcement from {html.escape(m["user"])}</b>'
                    f'&nbsp;&nbsp;<span style="color:#6d6f78">{when}</span><br>{render_text(m["text"])}'
                    f'</td></tr></table>')
        if m.get("kind") == "event":
            return (f'<div style="color:#949ba4"><span style="color:#6d6f78">{when}</span>&nbsp;&nbsp;'
                    f'<i>{html.escape(m["text"])}</i></div>')
        color = name_color(m["user"])
        who = f'<b style="color:{color}">{html.escape(m["user"])}</b>'
        if m.get("admin"):
            who += f' <span style="color:{ADMIN_COLOR}" title="Admin">{ADMIN_BADGE}</span>'
        return (f'<div><span style="color:#6d6f78">{when}</span>&nbsp;&nbsp;{who}&nbsp;&nbsp;'
                f'{render_text(m["text"])}</div>')

    def redraw(self):
        msgs = self.convs.get(self.current, [])
        if msgs:
            body = "".join(self.format(m) for m in msgs)
        elif self.current in self.loaded:
            body = '<div style="color:#949ba4"><i>No messages yet - say hi!</i></div>'
        else:
            body = ""
        self.view.setHtml(body)
        self.scroll_down()

    def scroll_down(self):
        sb = self.view.verticalScrollBar()
        sb.setValue(sb.maximum())

    # ---------------------------------------------------------- lists
    def label(self, key, text):
        n = self.unread.get(key)
        return f"{text}  ({n})" if n else text

    def refresh_rooms(self):
        self.room_list.clear()
        for r in self.rooms:
            it = QListWidgetItem(self.label(r, r))
            it.setData(Qt.ItemDataRole.UserRole, r)
            if self.unread.get(r):
                f = it.font(); f.setBold(True); it.setFont(f)
            self.room_list.addItem(it)
            if r == self.current:
                it.setSelected(True)

    def refresh_dms(self):
        self.dm_list.clear()
        groups = sorted(self.groups.items(), key=lambda kv: kv[1]["name"].lower())
        dms = sorted((k for k in self.convs if k.startswith("@")), key=str.lower)
        rows = [(f"g:{gid}", g["name"] + (f"   \U0001f50a {len(self.calls[gid])}" if self.calls.get(gid) else ""),
                 group_icon(g["name"]),
                 f"{len(g['members'])} members" + (" - call in progress" if self.calls.get(gid) else ""))
                for gid, g in groups]
        rows += [(k, k[1:], self.icon_for(k[1:]), "Online" if k[1:] in self.users else "Offline") for k in dms]
        for key, name, icon, tip in rows:
            it = QListWidgetItem(icon, self.label(key, name))
            it.setToolTip(tip)
            it.setData(Qt.ItemDataRole.UserRole, key)
            if self.unread.get(key):
                f = it.font(); f.setBold(True); it.setFont(f)
            self.dm_list.addItem(it)
            if key == self.current:
                it.setSelected(True)

    def icon_for(self, name):
        if name not in self.icons:
            self.icons[name] = avatar_icon(name, self.users.get(name, {}).get("avatar", ""))
        return self.icons[name]

    def refresh_users(self):
        self.user_list.clear()
        # admins first, then everyone else, alphabetically
        for name in sorted(self.users, key=lambda n: (not self.users[n].get("admin"), n.lower())):
            admin = self.users[name].get("admin")
            it = QListWidgetItem(self.icon_for(name), f"{name}  {ADMIN_BADGE}" if admin else name)
            it.setData(Qt.ItemDataRole.UserRole, name)
            tips = (["Admin"] if admin else []) + (["You"] if name == self.me else [])
            it.setToolTip(" · ".join(tips))
            if admin:
                it.setForeground(QColor(ADMIN_COLOR))
            self.user_list.addItem(it)
        self.online_heading.setText(f"ONLINE - {len(self.users)}")
        self.refresh_dms()
        self.refresh_members()

    def set_friends(self, friends, requests):
        self.friends = friends
        self.refresh_friends()
        self.req_list.clear()
        self.req_list.addItems(requests)
        self.req_heading.setText(f"FRIEND REQUESTS - {len(requests)}" if requests else "FRIEND REQUESTS")
        for w in (self.accept_btn, self.decline_btn):
            w.setEnabled(bool(requests))
        if requests:
            self.req_list.setCurrentRow(0)

    def refresh_friends(self):
        self.friend_list.clear()
        for f in self.friends:
            dot = "●" if f["online"] else "○"
            it = QListWidgetItem(f"{dot}  {f['name']}")
            it.setData(Qt.ItemDataRole.UserRole, f["name"])
            it.setForeground(QColor("#23a55a" if f["online"] else "#80848e"))
            self.friend_list.addItem(it)

    def person_menu(self, widget, pos):
        it = widget.itemAt(pos)
        if not it:
            return
        name = it.data(Qt.ItemDataRole.UserRole) or it.text()
        if name == self.me:
            return
        is_friend = any(f["name"] == name for f in self.friends)
        menu = QMenu(self)
        menu.addAction("Message", lambda: self.open_dm(name))
        if is_friend:
            menu.addAction("Remove friend", lambda: self.conn.send(t="friend_remove", user=name))
        else:
            menu.addAction("Add friend", lambda: self.conn.send(t="friend_request", to=name))
        g = self.current_group()
        if widget is self.member_list and g and g["owner"] == self.me:
            gid = int(self.current[2:])
            menu.addAction("Remove from group", lambda: self.conn.send(t="group_kick", gid=gid, user=name))
        target_is_admin = self.users.get(name, {}).get("admin")
        if self.is_admin and not target_is_admin:
            menu.addSeparator()
            menu.addAction("Kick...", lambda: self.moderate("kick", name))
            mute = menu.addMenu("Mute")
            for label, minutes in MUTE_CHOICES:
                mute.addAction(label, lambda _=False, m=minutes: self.moderate("mute", name, minutes=m))
            menu.addAction("Unmute", lambda: self.admin("unmute", user=name))
            menu.addAction("Ban...", lambda: self.moderate("ban", name))
            menu.addAction("Ban IP address...", lambda: self.moderate("ipban", name))
            menu.addAction("Reset password...", lambda: self.reset_password(name))
        menu.exec(widget.mapToGlobal(pos))

    def room_menu(self, pos):
        it = self.room_list.itemAt(pos)
        if not it or not self.is_admin:
            return
        room = it.data(Qt.ItemDataRole.UserRole)
        menu = QMenu(self)
        menu.addAction("Clear history...", lambda: self.clear_room(room))
        if room != "#general":
            menu.addAction("Delete channel...", lambda: self.delete_room(room))
        menu.exec(self.room_list.mapToGlobal(pos))

    # ---------------------------------------------------------- admin actions
    def admin(self, cmd, **kw):
        if not self.conn.send(t="admin", cmd=cmd, **kw):
            self.notice("Not connected", error=True)

    def ask_then(self, title, label, then):
        text, ok = QInputDialog.getText(self, title, label)
        if ok and text.strip():
            then(text.strip())

    def moderate(self, cmd, name, **kw):
        verb = {"kick": "Kick", "ban": "Ban", "ipban": "Ban the IP address of", "mute": "Mute"}[cmd]
        reason, ok = QInputDialog.getText(self, f"{verb} {name}", "Reason (optional, they'll see it):")
        if not ok:
            return
        if cmd in ("ban", "ipban"):
            extra = ("\n\nEveryone connecting from that address will be blocked." if cmd == "ipban" else "")
            if QMessageBox.question(self, "Confirm", f"{verb} {name}?{extra}") != QMessageBox.StandardButton.Yes:
                return
        self.admin(cmd, user=name, reason=reason.strip(), **kw)

    def reset_password(self, name):
        pw, ok = QInputDialog.getText(self, f"Reset password for {name}",
                                      "New password (tell them what it is):", QLineEdit.EchoMode.Normal)
        if ok and pw:
            self.admin("resetpass", user=name, password=pw)

    def clear_room(self, room):
        if not room.startswith("#"):
            self.notice("Open a channel first", error=True)
            return
        if QMessageBox.question(self, "Clear history",
                                f"Delete every message in {room} for everyone?") == QMessageBox.StandardButton.Yes:
            self.admin("clearhistory", room=room)

    def delete_room(self, room):
        if QMessageBox.question(self, "Delete channel",
                                f"Delete {room} and all its messages?") == QMessageBox.StandardButton.Yes:
            self.admin("delroom", room=room)

    def announce_prompt(self):
        self.ask_then("Announcement", "Message to everyone:", lambda t: self.admin("announce", text=t))

    def respond_request(self, accept):
        it = self.req_list.currentItem()
        if it:
            self.conn.send(t="friend_respond", user=it.text(), accept=accept)

    # ---------------------------------------------------------- actions
    def send_input(self):
        text = self.input.text().strip()
        if not text:
            return
        if text.startswith("/") and not text.startswith("//"):
            self.command(text)
        else:
            if text.startswith("//"):
                text = text[1:]
            if self.current.startswith("#"):
                ok = self.conn.send(t="msg", room=self.current, text=text)
            elif self.current.startswith("g:"):
                ok = self.conn.send(t="group_msg", gid=int(self.current[2:]), text=text)
            else:
                ok = self.conn.send(t="dm", to=self.current[1:], text=text)
            if not ok:
                self.notice("Not connected - message not sent", error=True)
                return
        self.input.clear()

    def command(self, text):
        cmd, _, arg = text[1:].partition(" ")
        cmd, arg = cmd.lower(), arg.strip()
        if cmd in ("join", "room") and arg:
            room = arg if arg.startswith("#") else "#" + arg
            room = room.lower()
            if room in self.rooms:
                self.open_conv(room)
            else:
                self.pending_room = room          # opened when the server confirms it
                self.conn.send(t="create_room", room=room)
        elif cmd in ("dm", "msg", "w") and arg:
            name, _, body = arg.partition(" ")
            if body:
                self.conn.send(t="dm", to=name, text=body)
            self.open_dm(name)
        elif cmd == "friend" and arg:
            self.conn.send(t="friend_request", to=arg)
        elif cmd in ("nick", "username", "name") and arg:
            self.conn.send(t="rename", name=arg)
        elif cmd == "group":
            self.group_command(arg)
        elif cmd == "clear":
            self.view.clear()
        elif cmd in ADMIN_COMMANDS and self.is_admin:
            self.admin_command(cmd, arg)
        else:
            self.notice("Commands: /join #room · /dm <user> [message] · /friend <user> · "
                        "/nick <new name> · /clear · start with // to send a literal /")
            self.notice(f"Groups (up to {GROUP_MAX} people): /group new user1 user2 ... · /group add user ... · "
                        "/group name <new name> · /group remove user · /group leave · /group members · /group call")
            if self.is_admin:
                self.notice("Admin: /kick user [reason] · /ban user [reason] · /unban user · "
                            "/ipban user-or-ip [reason] · /unipban user-or-ip · /mute user [minutes] [reason] · "
                            "/unmute user · /announce text · /clearhistory [#room] · /delroom [#room] · "
                            "/resetpass user newpass · /bans · /voicekick user · /delvoice name")

    def group_command(self, arg):
        sub, _, rest = arg.partition(" ")
        sub, names = sub.lower(), rest.replace(",", " ").split()
        if sub in ("new", "create"):
            if names:
                self.conn.send(t="group_create", name="", members=names)
            else:
                self.new_group()
            return
        g = self.current_group()
        if g is None:
            self.notice("Open a group first (or use /group new user1 user2 ...)", error=True)
            return
        gid = int(self.current[2:])
        if sub == "add" and names:
            self.conn.send(t="group_add", gid=gid, users=names)
        elif sub in ("name", "rename") and rest.strip():
            self.conn.send(t="group_rename", gid=gid, name=rest.strip())
        elif sub in ("remove", "kick") and names:
            self.conn.send(t="group_kick", gid=gid, user=names[0])
        elif sub == "leave":
            self.conn.send(t="group_leave", gid=gid)
        elif sub == "call":
            self.call_button()
        elif sub == "members":
            self.notice(f"{len(g['members'])}/{GROUP_MAX}: " + ", ".join(g["members"]) + f"  (owner: {g['owner']})")
        else:
            self.notice("Try /group new, add, name, remove, leave or members", error=True)

    def admin_command(self, cmd, arg):
        first, _, rest = arg.partition(" ")
        rest = rest.strip()
        if cmd in ("kick", "ban", "ipban") and first:
            self.admin(cmd, user=first, reason=rest)
        elif cmd in ("unban", "unipban", "unmute") and first:
            self.admin(cmd, user=first)
        elif cmd == "mute" and first:
            minutes, _, reason = rest.partition(" ")
            if not minutes.replace(".", "", 1).isdigit():      # no number given: it's all reason
                minutes, reason = "0", rest
            self.admin("mute", user=first, minutes=float(minutes), reason=reason.strip())
        elif cmd == "announce" and arg:
            self.admin("announce", text=arg)
        elif cmd in ("clearhistory", "delroom"):
            room = first or self.current
            if not room.startswith("#"):
                room = "#" + room if first else room
            self.admin(cmd, room=room)
        elif cmd == "resetpass" and first and rest:
            self.admin("resetpass", user=first, password=rest)
        elif cmd == "bans":
            self.admin("bans")
        elif cmd == "voicekick" and first:
            self.admin("voicekick", user=first, reason=rest)
        elif cmd == "delvoice" and arg:
            self.admin("delvoice", room=arg)
        else:
            self.notice(f"Missing arguments for /{cmd} - type /help", error=True)

    def create_room(self):
        name, ok = QInputDialog.getText(self, "New channel", "Channel name:")
        if ok and name.strip():
            self.command(f"/join {name.strip()}")

    def add_friend_prompt(self):
        name, ok = QInputDialog.getText(self, "Add friend", "Username:")
        if ok and name.strip():
            self.conn.send(t="friend_request", to=name.strip())

    def open_settings(self):
        SettingsDialog(self).exec()

    def logout(self):
        self.leave("Logged out")

    def leave(self, reason):
        self.leaving = True
        self.stop_video_all()
        self.clear_stage()
        if self.engine is not None:
            self.engine.stop()
            self.engine = None
        QApplication.instance().removeEventFilter(self)
        self.ping_timer.stop()
        self.conn.close()
        self.close()
        self.logged_out.emit(reason)

    def closeEvent(self, e):
        for key in list(self.popouts):
            self.popouts.pop(key).close()
        for kind in list(self.publishers):
            self.publishers.pop(kind).stop()
        for kind in list(self.sources):
            self.sources.pop(kind).stop()
        if self.engine is not None:
            self.engine.stop()
            self.engine = None
        if not self.leaving:
            self.leaving = True
            self.conn.close()
            QApplication.quit()
        super().closeEvent(e)


def main():
    app = QApplication(sys.argv)
    app.setApplicationName("Campfire")
    app.setStyleSheet(STYLE)
    if os.path.exists(asset("icon.png")):
        app.setWindowIcon(QIcon(asset("icon.png")))
    login = LoginWindow()
    login.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
