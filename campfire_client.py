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
import shutil
import socket
import ssl
import sys
import threading
import time
from urllib.parse import quote, unquote

from PyQt6.QtCore import (QBuffer, QByteArray, QEasingCurve, QEvent, QIODevice, QObject, QPoint, QPropertyAnimation,
                          QRect, QSettings, QSize,
                          QStandardPaths, Qt, QTimer, QUrl, QUrlQuery, pyqtSignal)
from PyQt6.QtGui import (QAction, QBrush, QColor, QCursor, QDesktopServices, QFont, QIcon, QImage, QKeySequence, QMovie,
                         QPainter, QPixmap, QPen, QTextDocument)
from PyQt6.QtNetwork import QNetworkAccessManager, QNetworkReply, QNetworkRequest
from PyQt6.QtWidgets import (
    QApplication, QCheckBox, QComboBox, QDialog, QFrame, QLayout, QListView, QScrollArea, QTabBar, QDialogButtonBox, QFileDialog, QFormLayout, QHBoxLayout,
    QInputDialog, QLabel, QLineEdit, QListWidget, QListWidgetItem, QMainWindow, QMenu,
    QGridLayout, QMessageBox, QProgressBar, QPushButton, QSlider, QSplitter, QStackedWidget, QTextBrowser,
    QTreeWidget, QTreeWidgetItem, QPlainTextEdit,
    QVBoxLayout, QWidget, QColorDialog, QGraphicsOpacityEffect, QStyle, QStyledItemDelegate,
)

import emoji_data
import transfers
import games  # noqa: E402
import updater
import video
import voice

APP_DIR = os.path.dirname(os.path.abspath(__file__))
# ---- The one server this client talks to: set in campfire_config.py
from campfire_config import SERVER_HOST, SERVER_PORT, WEB_SERVER, WEB_URL  # noqa: E402
USE_TLS = False              # set True once the server runs with --certfile/--keyfile
TLS_CERT = "cert.pem"        # the server's certificate, shipped next to this file
LOGIN_NOTE = ""              # optional extra line on the login screen
APP_ID = "Campfire.Client"   # Windows taskbar identity
HELLO_TIMEOUT = 10           # seconds to wait for the server to greet us
LOCAL_HOSTS = {"localhost", "127.0.0.1", "::1"}
LOCAL_TIMEOUT = 0.8          # how long to look for a Campfire server on this PC before using SERVER_HOST
ADMIN_COMMANDS = {"kick", "ban", "unban", "ipban", "unipban", "mute", "unmute", "announce",
                  "clearhistory", "delroom", "resetpass", "bans", "voicekick", "delvoice"}
MAX_LINE = 4 * 1024 * 1024    # GIF avatars are up to 1 MB
MAX_AVATAR = 1024 * 1024      # must match the server
MAX_BIO = 190
CHAT_AVATAR = 40              # size of the pictures next to messages
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

    def __init__(self, host: str, port: int, use_tls=False, ca_file: str | None = None, try_local=False):
        super().__init__()
        self.host, self.port = host, port
        self.use_tls, self.ca_file = use_tls, ca_file
        self.try_local = try_local and host not in LOCAL_HOSTS
        self.local = host in LOCAL_HOSTS         # True once we're talking to a server on this PC
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

    def _open(self, host, timeout, local):
        sock = socket.create_connection((host, self.port), timeout=timeout)
        if self.use_tls:
            ctx = ssl.create_default_context(cafile=self.ca_file or None)
            if local:                            # same certificate, but it was made for the public address
                ctx.check_hostname = False
            sock = ctx.wrap_socket(sock, server_hostname=None if local else host)
        return sock

    def _try_local(self):
        """If the Campfire server runs on this PC, use it: from inside your own network your public
        address usually can't be reached. It has to answer like a Campfire server, quickly."""
        sock = None
        try:
            sock = self._open("127.0.0.1", LOCAL_TIMEOUT, True)
            sock.settimeout(LOCAL_TIMEOUT)
            stream = sock.makefile("rb")
            first = json.loads(stream.readline(4096))
            if isinstance(first, dict) and first.get("t") in ("hello", "kicked"):
                return sock, stream, first
        except Exception:
            pass
        if sock is not None:
            try:
                sock.close()
            except OSError:
                pass
        return None

    def _run(self):
        first = None
        found = self._try_local() if self.try_local else None
        if found is not None:
            sock, stream, first = found
            self.host, self.local = "127.0.0.1", True
        else:
            try:
                sock = self._open(self.host, 8, self.local)
                stream = sock.makefile("rb")
            except Exception as e:
                self._emit("failed", friendly_error(e))
                return
        sock.settimeout(None)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_KEEPALIVE, 1)
        self.sock = sock
        self._emit("connected")

        reason = "Connection closed by server"
        try:
            if first is not None:                  # the hello we already read while checking
                self.seq += 1
                first["_seq"] = self.seq
                self._emit("received", first)
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


_base_avatar: QPixmap | None = None


def default_avatar(name: str, size=64) -> QPixmap:
    """The picture for anyone who hasn't chosen an avatar: avatar.png, or a letter if it's missing."""
    global _base_avatar
    if _base_avatar is None and os.path.exists(asset("avatar.png")):
        _base_avatar = QPixmap(asset("avatar.png"))
    if _base_avatar is not None and not _base_avatar.isNull():
        return _base_avatar.scaled(size, size, Qt.AspectRatioMode.KeepAspectRatio,
                                   Qt.TransformationMode.SmoothTransformation)
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


def round_image(img: QImage, size: int) -> QImage:
    """A picture cropped to a square, scaled and cut into a circle."""
    if img.isNull():
        return img
    side = min(img.width(), img.height())
    img = img.copy((img.width() - side) // 2, (img.height() - side) // 2, side, side)
    dpr = 2                                             # sharp on high-DPI screens
    img = img.scaled(size * dpr, size * dpr, Qt.AspectRatioMode.IgnoreAspectRatio,
                     Qt.TransformationMode.SmoothTransformation)
    out = QImage(size * dpr, size * dpr, QImage.Format.Format_ARGB32_Premultiplied)
    out.fill(Qt.GlobalColor.transparent)
    p = QPainter(out)
    p.setRenderHint(QPainter.RenderHint.Antialiasing)
    p.setBrush(QBrush(img))
    p.setPen(Qt.PenStyle.NoPen)
    p.drawEllipse(0, 0, size * dpr, size * dpr)
    p.end()
    out.setDevicePixelRatio(dpr)
    return out


def avatar_icon(name: str, data: str) -> QIcon:
    if data:
        pm = QPixmap()
        try:
            if pm.loadFromData(base64.b64decode(data)):
                return QIcon(QPixmap.fromImage(round_image(pm.toImage(), 64)))
        except ValueError:
            pass
    return QIcon(QPixmap.fromImage(round_image(default_avatar(name).toImage(), 64)))


def is_gif(raw: bytes) -> bool:
    return raw[:6] in (b"GIF87a", b"GIF89a")


def encode_avatar(path: str):
    """A picture file -> (base64 data, None) or (None, why not). GIFs stay animated (resized with
    Pillow when it's installed); other pictures become a 128px PNG/JPG."""
    try:
        with open(path, "rb") as f:
            raw = f.read()
    except OSError as e:
        return None, f"Couldn't read that file ({e.strerror})"
    if is_gif(raw):
        anim = gif_avatar(path)
        if anim is not None:
            return (base64.b64encode(anim).decode(), None) if len(anim) <= MAX_AVATAR else \
                (None, "That GIF is too big even when shrunk. Try a shorter one.")
        if len(raw) <= MAX_AVATAR:
            return base64.b64encode(raw).decode(), None
        return None, f"That GIF is {transfers.human_size(len(raw))} - avatars can be 1 MB at most."
    img = QImage(path)
    if img.isNull():
        return None, "Couldn't use that image."
    side = min(img.width(), img.height())
    img = img.copy((img.width() - side) // 2, (img.height() - side) // 2, side, side)
    img = img.scaled(128, 128, Qt.AspectRatioMode.IgnoreAspectRatio, Qt.TransformationMode.SmoothTransformation)
    for fmt in ("PNG", "JPG"):
        buf = QByteArray()
        dev = QBuffer(buf)
        dev.open(QIODevice.OpenModeFlag.WriteOnly)
        img.save(dev, fmt, 88)
        if buf.size() < 200 * 1024:
            return base64.b64encode(bytes(buf)).decode(), None
    return None, "Couldn't use that image."


def gif_avatar(path: str):
    """Square-crop and shrink an animated GIF, keeping every frame. None without Pillow."""
    try:
        from PIL import Image, ImageSequence
    except ImportError:
        return None
    import io
    try:
        src = Image.open(path)
        frames, durations = [], []
        for fr in ImageSequence.Iterator(src):
            frames.append(fr.convert("RGBA"))
            durations.append(fr.info.get("duration", src.info.get("duration", 80)) or 80)
            if len(frames) >= 300:
                break
        if not frames:
            return None
        w, h = frames[0].size
        side = min(w, h)
        box = ((w - side) // 2, (h - side) // 2, (w - side) // 2 + side, (h - side) // 2 + side)
        best = None
        for size in (128, 96, 72, 56):
            out = [f.crop(box).resize((min(size, side), min(size, side)), Image.LANCZOS) for f in frames]
            buf = io.BytesIO()
            out[0].save(buf, "GIF", save_all=True, append_images=out[1:], duration=durations, loop=0,
                        disposal=2, optimize=True)
            best = buf.getvalue()
            if len(best) <= MAX_AVATAR:
                return best
        return best
    except Exception:
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
QWidget#transfer { background: #232428; border-radius: 6px; }
QFrame#picker { background: #2b2d31; border: 1px solid #1e1f22; border-radius: 8px; }
QFrame#picker QListWidget { background: #2b2d31; }
QFrame#picker QPushButton:checked { background: #5865f2; }
QFrame#picker QListWidget::item { border-radius: 6px; padding: 0; }
QFrame#picker QListWidget::item:hover { background: #404249; }
QTabBar::tab { background: transparent; padding: 4px 6px; font-size: 13pt; border-radius: 6px; }
QTabBar::tab:selected { background: #404249; }
QTabBar::tab:hover { background: #35373c; }
QWidget#transfer QLabel { background: transparent; }
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


# ---- appearance: themes swap the colours of the stylesheet above
BASE = {"bg": "#2b2d31", "panel": "#232428", "view": "#313338", "input": "#383a40", "sel": "#404249",
        "hover": "#35373c", "border": "#1e1f22", "text": "#dbdee1", "muted": "#949ba4", "flat": "#3a3c42",
        "flathover": "#4a4c52", "disabled": "#4e5058", "menu": "#111214"}
THEMES = {
    "Dark": dict(BASE),
    "Midnight": {"bg": "#121214", "panel": "#0b0b0d", "view": "#18181b", "input": "#232327", "sel": "#2c2c33",
                 "hover": "#1f1f24", "border": "#000000", "text": "#e3e3e8", "muted": "#8e8e99", "flat": "#26262c",
                 "flathover": "#33333a", "disabled": "#3a3a42", "menu": "#000000"},
    "Campfire": {"bg": "#2a2320", "panel": "#211b19", "view": "#302824", "input": "#3a302b", "sel": "#4a3b33",
                 "hover": "#3a2f29", "border": "#17120f", "text": "#f0e3d8", "muted": "#b09a8a", "flat": "#43362f",
                 "flathover": "#54443b", "disabled": "#4f433c", "menu": "#120e0c"},
    "Light": {"bg": "#f2f3f5", "panel": "#e3e5e8", "view": "#ffffff", "input": "#ebedef", "sel": "#d4d7dc",
              "hover": "#dcdfe3", "border": "#c7ccd1", "text": "#2e3338", "muted": "#5c5e66", "flat": "#d4d7dc",
              "flathover": "#c4c9ce", "disabled": "#c4c9ce", "menu": "#ffffff"},
}
THEME_ACCENTS = {"Dark": "#5865f2", "Midnight": "#5865f2", "Campfire": "#e8742c", "Light": "#5865f2"}
TEXT_SIZES = {"Small": 9, "Normal": 10, "Large": 11.5, "Extra large": 13}


def appearance() -> dict:
    theme = settings.value("look/theme", "Dark")
    theme = theme if theme in THEMES else "Dark"
    return {"theme": theme, "accent": settings.value("look/accent", "") or THEME_ACCENTS[theme],
            "size": settings.value("look/size", "Normal"),
            "animations": settings.value("look/animations", True, type=bool)}


def themed_style(theme="Dark", accent="#5865f2", size="Normal"):
    pal = THEMES.get(theme, THEMES["Dark"])
    swap = {BASE[k].lower(): v for k, v in pal.items()}
    swap["#5865f2"] = accent
    swap["#4752c4"] = QColor(accent).darker(125).name()
    css = re.sub(r"#[0-9a-fA-F]{6}", lambda m: swap.get(m.group(0).lower(), m.group(0)), STYLE)
    if theme == "Light":                         # selected items: dark text on the light highlight
        css = css.replace("background: #d4d7dc; color: white;", "background: #d4d7dc; color: #060607;")
    pt = TEXT_SIZES.get(size, 10)
    css = css.replace("font-size: 10pt;", f"font-size: {pt}pt;").replace("font-size: 10.5pt;", f"font-size: {pt + 0.5}pt;")
    return css


def apply_appearance(app=None):
    look = appearance()
    (app or QApplication.instance()).setStyleSheet(themed_style(look["theme"], look["accent"], look["size"]))
    return look


class PersonDelegate(QStyledItemDelegate):
    """Draws people in the Online list: avatar, name in their colour (gold star for admins),
    and their status message underneath."""

    def __init__(self, win):
        super().__init__(win)
        self.win = win

    def sizeHint(self, option, index):
        return QSize(option.rect.width(), 46 if index.data(Qt.ItemDataRole.UserRole + 1) else 34)

    def paint(self, p, option, index):
        self.initStyleOption(option, index)
        name = index.data(Qt.ItemDataRole.UserRole) or ""
        status = index.data(Qt.ItemDataRole.UserRole + 1) or ""
        option.text = ""
        option.icon = QIcon()
        style = option.widget.style() if option.widget else QApplication.style()
        style.drawControl(QStyle.ControlElement.CE_ItemViewItem, option, p, option.widget)
        r = option.rect.adjusted(8, 0, -6, 0)
        p.drawPixmap(QRect(r.left(), r.center().y() - 12, 24, 24), self.win.avatar_pixmap(name, 24))
        text_left = r.left() + 32
        f = QFont(option.font)
        p.save()
        f.setBold(True)
        p.setFont(f)
        p.setPen(QColor(self.win.user_color(name)))
        name_rect = QRect(text_left, r.top() + (6 if status else 0), r.width() - 32, 18 if status else r.height())
        flags = Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft
        p.drawText(name_rect, flags, name)
        if self.win.users.get(name, {}).get("admin"):
            p.setPen(QColor(ADMIN_COLOR))
            p.drawText(name_rect.adjusted(p.fontMetrics().horizontalAdvance(name) + 6, 0, 0, 0), flags, ADMIN_BADGE)
        if status:
            f.setBold(False)
            f.setPointSizeF(max(7.0, f.pointSizeF() - 1.5))
            p.setFont(f)
            p.setPen(QColor(THEMES[appearance()["theme"]]["muted"]))
            st = p.fontMetrics().elidedText(status, Qt.TextElideMode.ElideRight, r.width() - 32)
            p.drawText(QRect(text_left, r.top() + 24, r.width() - 32, 16), flags, st)
        p.restore()


def heading(text):
    lbl = QLabel(text.upper())
    lbl.setObjectName("heading")
    return lbl


def file_emoji(name: str) -> str:
    ext = os.path.splitext(name)[1].lower()
    if ext in transfers.IMAGE_EXTS:
        return "\U0001f5bc\ufe0f"
    if ext in (".mp4", ".mkv", ".mov", ".avi", ".webm"):
        return "\U0001f39e\ufe0f"
    if ext in (".mp3", ".wav", ".ogg", ".flac", ".m4a"):
        return "\U0001f3b5"
    if ext in (".zip", ".rar", ".7z", ".tar", ".gz"):
        return "\U0001f4e6"
    return "\U0001f4c4"


class ChatView(QTextBrowser):
    """The message view. Serves image previews (and the current frame of GIFs) to the HTML
    through the campfire-img:<key> scheme. Keys are "f<file id>" or "u<hash of a link>"."""

    context = pyqtSignal(QPoint)

    def __init__(self):
        super().__init__()
        self.thumbs: dict[str, QImage] = {}

    def loadResource(self, kind, url):
        if url.scheme() == "campfire-img":
            img = self.thumbs.get(url.path())
            if img is not None:
                return img
        return super().loadResource(kind, url)

    def contextMenuEvent(self, ev):
        self.context.emit(ev.pos())


def cache_folder(name):
    base = QStandardPaths.writableLocation(QStandardPaths.StandardLocation.CacheLocation) or os.path.join(
        os.path.expanduser("~"), ".campfire_cache")
    folder = os.path.join(base, name)
    os.makedirs(folder, exist_ok=True)
    return folder


def favorites_folder():
    base = QStandardPaths.writableLocation(QStandardPaths.StandardLocation.AppDataLocation) or os.path.join(
        os.path.expanduser("~"), ".campfire")
    folder = os.path.join(base, "favorite_gifs")
    os.makedirs(folder, exist_ok=True)
    return folder


def media_link(url: str):
    """If a link points at a picture or GIF we can show, return (fetch url, is_web_page)."""
    q = QUrl(url)
    host, path = q.host().lower(), q.path()
    if os.path.splitext(path)[1].lower() in transfers.IMAGE_EXTS:
        return url, False
    if host.endswith("giphy.com") and "/gifs/" in path:
        gif_id = path.rstrip("/").split("/")[-1].split("-")[-1]
        if gif_id:
            return f"https://media.giphy.com/media/{gif_id}/giphy.gif", False
    if host.endswith("tenor.com") and path.startswith("/view/"):
        return url, True                    # the GIF's address is inside the page
    return None


# ====================================================================== GIPHY
GIPHY_API = "https://api.giphy.com/v1/gifs"
GIPHY_RATING = "pg-13"
_giphy_cache: dict[str, tuple[float, list]] = {}     # query -> (time, results); saves the hourly quota


class GifThumb(QLabel):
    """One animated GIPHY result; click to send."""

    clicked = pyqtSignal(str)

    def __init__(self, item: dict, net: QNetworkAccessManager, cell=QSize(118, 96)):
        super().__init__()
        self.send_url = item["send"]
        self.setFixedSize(cell)
        self.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.setStyleSheet("background:#1e1f22; border-radius:6px;")
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setToolTip(item.get("title") or "GIF")
        w, h = item.get("w") or cell.width(), item.get("h") or cell.height()
        scale = min(cell.width() / w, cell.height() / h)
        self.size_ = QSize(max(1, int(w * scale)), max(1, int(h * scale)))
        self.movie = self.buf = None
        reply = net.get(QNetworkRequest(QUrl(item["preview"])))
        reply.finished.connect(lambda: self.loaded(reply))

    def loaded(self, reply):
        try:
            data = bytes(reply.readAll()) if reply.error() == QNetworkReply.NetworkError.NoError else b""
            reply.deleteLater()
            if not data:
                return
            self.buf = QBuffer()
            self.buf.setData(QByteArray(data))
            self.buf.open(QIODevice.OpenModeFlag.ReadOnly)
            self.movie = QMovie(self.buf, QByteArray(b"gif"), self)
            self.movie.setScaledSize(self.size_)
            self.setMovie(self.movie)
            self.movie.start()
        except RuntimeError:                       # picker closed while loading
            pass

    def mousePressEvent(self, ev):
        if ev.button() == Qt.MouseButton.LeftButton:
            self.clicked.emit(self.send_url)


def giphy_items(payload: dict) -> list[dict]:
    """The bits of a GIPHY search/trending response we use."""
    out = []
    for g in payload.get("data", []):
        imgs = g.get("images", {})
        prev = imgs.get("fixed_width_small") or imgs.get("fixed_width") or imgs.get("downsized") or {}
        send = imgs.get("downsized") or imgs.get("fixed_width") or imgs.get("original") or {}
        if not prev.get("url") or not send.get("url"):
            continue
        out.append({"id": g.get("id"), "title": g.get("title", ""), "preview": prev["url"], "send": send["url"],
                    "w": int(prev.get("width") or 0), "h": int(prev.get("height") or 0)})
    return out


# ====================================================================== emoji & GIF picker
class EmojiPicker(QFrame):
    """Popup with emoji (search, categories, recently used) and a tab of favorite GIFs."""

    picked = pyqtSignal(str)
    gif_picked = pyqtSignal(str)
    giphy_picked = pyqtSignal(str)

    def __init__(self, parent, giphy_key="", net=None):
        super().__init__(parent, Qt.WindowType.Popup)
        self.giphy_key, self.net = giphy_key, net
        self.giphy_timer = QTimer(self, singleShot=True, interval=450, timeout=self.giphy_search)
        self.giphy_reply = None
        self.setObjectName("picker")
        self.resize(400, 420)
        v = QVBoxLayout(self)
        v.setContentsMargins(8, 8, 8, 8)
        self.search = QLineEdit()
        self.search.setPlaceholderText("Search emoji (e.g. fire, laugh, cat)")
        self.search.textChanged.connect(self.filter)
        v.addWidget(self.search)

        self.tabs = QTabBar()
        self.tabs.setExpanding(False)
        self.tab_keys = ["Recent"] + list(emoji_data.CATEGORIES) + ["GIFs"]
        self.tabs.addTab("\U0001f552")
        for cat in emoji_data.CATEGORIES:
            self.tabs.addTab(emoji_data.CATEGORY_ICONS[cat])
        self.tabs.addTab("GIF")
        for i, key in enumerate(self.tab_keys):
            self.tabs.setTabToolTip(i, "Recently used" if key == "Recent" else
                                    "Favorite GIFs" if key == "GIFs" else key)
        self.tabs.currentChanged.connect(self.show_tab)
        v.addWidget(self.tabs)

        self.stack = QStackedWidget()
        v.addWidget(self.stack, 1)
        self.grid = QListWidget()
        self.grid.setViewMode(QListView.ViewMode.IconMode)
        self.grid.setGridSize(QSize(42, 42))
        self.grid.setMovement(QListView.Movement.Static)
        self.grid.setResizeMode(QListView.ResizeMode.Adjust)
        self.grid.setUniformItemSizes(True)
        self.grid.setMouseTracking(True)
        self.grid.setStyleSheet("font-size: 24px;")      # the app-wide 10pt would make them tiny
        self.grid.itemClicked.connect(lambda it: self.choose(it.text()))
        self.grid.itemEntered.connect(lambda it: self.name_label.setText(self.describe(it.text())))
        self.stack.addWidget(self.grid)

        gif_page = QWidget()
        gv = QVBoxLayout(gif_page)
        gv.setContentsMargins(0, 0, 0, 0)
        modes = QHBoxLayout()
        self.giphy_btn = QPushButton("GIPHY")
        self.fav_btn = QPushButton("Favorites")
        for b in (self.giphy_btn, self.fav_btn):
            b.setCheckable(True)
            b.setProperty("flat", True)
            modes.addWidget(b)
        modes.addStretch()
        self.giphy_btn.clicked.connect(lambda: self.gif_mode("giphy"))
        self.fav_btn.clicked.connect(lambda: self.gif_mode("favorites"))
        if not giphy_key:
            self.giphy_btn.setEnabled(False)
            self.giphy_btn.setToolTip("GIF search is off on this server - the server owner can add a GIPHY key")
        gv.addLayout(modes)
        self.gif_stack = QStackedWidget()
        gv.addWidget(self.gif_stack, 1)

        giphy_page = QWidget()
        gp = QVBoxLayout(giphy_page)
        gp.setContentsMargins(0, 0, 0, 0)
        self.giphy_scroll = QScrollArea()
        self.giphy_scroll.setWidgetResizable(True)
        self.giphy_scroll.setFrameShape(QFrame.Shape.NoFrame)
        self.giphy_host = QWidget()
        self.giphy_grid = QGridLayout(self.giphy_host)
        self.giphy_grid.setSpacing(6)
        self.giphy_grid.setContentsMargins(0, 0, 0, 0)
        self.giphy_scroll.setWidget(self.giphy_host)
        gp.addWidget(self.giphy_scroll, 1)
        bottom = QHBoxLayout()
        self.giphy_status = QLabel("")
        self.giphy_status.setStyleSheet("color:#949ba4; font-size:8.5pt")
        bottom.addWidget(self.giphy_status, 1)
        powered = QLabel("Powered by <b>GIPHY</b>")
        powered.setStyleSheet("color:#949ba4; font-size:8.5pt")
        bottom.addWidget(powered)
        gp.addLayout(bottom)
        self.gif_stack.addWidget(giphy_page)

        fav_page = QWidget()
        gv = QVBoxLayout(fav_page)
        gv.setContentsMargins(0, 0, 0, 0)
        self.gifs = QListWidget()
        self.gifs.setViewMode(QListView.ViewMode.IconMode)
        self.gifs.setIconSize(QSize(110, 110))
        self.gifs.setGridSize(QSize(118, 118))
        self.gifs.setMovement(QListView.Movement.Static)
        self.gifs.setResizeMode(QListView.ResizeMode.Adjust)
        self.gifs.itemClicked.connect(lambda it: (self.gif_picked.emit(it.data(Qt.ItemDataRole.UserRole)), self.close()))
        self.gifs.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.gifs.customContextMenuRequested.connect(self.gif_menu)
        gv.addWidget(self.gifs, 1)
        row = QHBoxLayout()
        add = QPushButton("Add GIF from file...")
        add.setProperty("flat", True)
        add.clicked.connect(self.add_gif_file)
        row.addWidget(add)
        row.addStretch()
        gv.addLayout(row)
        self.gif_hint = QLabel("Right-click any GIF in the chat and choose \"Add to favorite GIFs\".")
        self.gif_hint.setWordWrap(True)
        self.gif_hint.setStyleSheet("color:#949ba4; font-size:8.5pt")
        gv.addWidget(self.gif_hint)
        self.gif_stack.addWidget(fav_page)
        self.stack.addWidget(gif_page)
        self.mode = "giphy" if giphy_key else "favorites"

        self.name_label = QLabel(" ")
        self.name_label.setStyleSheet("color:#949ba4")
        v.addWidget(self.name_label)
        self.show_tab(1 if not self.recent() else 0)
        self.tabs.setCurrentIndex(1 if not self.recent() else 0)

    @staticmethod
    def recent():
        return [e for e in (settings.value("emoji/recent", "") or "").split(" ") if e]

    @staticmethod
    def describe(e):
        aliases = [k for k, v in emoji_data.ALIASES.items() if v == e]
        code = aliases[0] if aliases else "_".join(emoji_data.name_of(e).split())
        return f"{e}   :{code}:"

    def fill(self, items):
        self.grid.clear()
        for e in items:
            it = QListWidgetItem(e)
            it.setTextAlignment(Qt.AlignmentFlag.AlignCenter)
            it.setToolTip(self.describe(e))
            self.grid.addItem(it)

    def show_tab(self, index):
        key = self.tab_keys[index]
        if key == "GIFs":
            self.stack.setCurrentIndex(1)
            self.gif_mode(self.mode)
            return
        self.search.setPlaceholderText("Search emoji (e.g. fire, laugh, cat)")
        self.stack.setCurrentIndex(0)
        self.search.blockSignals(True)
        self.search.clear()
        self.search.blockSignals(False)
        self.fill(self.recent() if key == "Recent" else emoji_data.CATEGORIES[key])

    def on_gif_tab(self):
        return self.tab_keys[self.tabs.currentIndex()] == "GIFs"

    def gif_mode(self, mode):
        self.mode = mode
        self.giphy_btn.setChecked(mode == "giphy")
        self.fav_btn.setChecked(mode == "favorites")
        if mode == "giphy":
            self.gif_stack.setCurrentIndex(0)
            self.search.setPlaceholderText("Search GIPHY")
            self.giphy_search()
        else:
            self.gif_stack.setCurrentIndex(1)
            self.search.setPlaceholderText("Search emoji (e.g. fire, laugh, cat)")
            self.load_gifs()

    def giphy_search(self):
        """Trending when the box is empty, otherwise search. Results are cached for 10 minutes
        because a free GIPHY key only allows about 100 searches an hour."""
        if not self.giphy_key or self.net is None:
            return
        q = self.search.text().strip() if self.on_gif_tab() else ""
        cached = _giphy_cache.get(q)
        if cached and time.time() - cached[0] < 600:
            self.show_giphy(cached[1], q)
            return
        url = QUrl(f"{GIPHY_API}/{'search' if q else 'trending'}")
        query = QUrlQuery()
        query.addQueryItem("api_key", self.giphy_key)
        if q:
            query.addQueryItem("q", q)
        query.addQueryItem("limit", "24")
        query.addQueryItem("rating", GIPHY_RATING)
        url.setQuery(query)
        if self.giphy_reply is not None:
            try:
                self.giphy_reply.abort()
            except RuntimeError:
                pass
        self.giphy_status.setText("Searching..." if q else "Loading trending GIFs...")
        reply = self.net.get(QNetworkRequest(url))
        self.giphy_reply = reply
        reply.finished.connect(lambda: self.giphy_done(reply, q))

    def giphy_done(self, reply, q):
        try:
            status = reply.attribute(QNetworkRequest.Attribute.HttpStatusCodeAttribute)
            error = reply.error()
            data = bytes(reply.readAll())
            reply.deleteLater()
        except RuntimeError:
            return
        if reply is not self.giphy_reply:
            return                                   # an older search finished late
        self.giphy_reply = None
        if error == QNetworkReply.NetworkError.OperationCanceledError:
            return
        if status == 429:
            self.giphy_status.setText("GIPHY's hourly search limit was reached - try again later")
            return
        if status in (401, 403):
            self.giphy_status.setText("The server's GIPHY key isn't working - tell the server owner")
            return
        try:
            items = giphy_items(json.loads(data))
        except ValueError:
            items = None
        if error != QNetworkReply.NetworkError.NoError or items is None:
            self.giphy_status.setText("Couldn't reach GIPHY")
            return
        _giphy_cache[q] = (time.time(), items)
        self.show_giphy(items, q)

    def show_giphy(self, items, q):
        while self.giphy_grid.count():
            w = self.giphy_grid.takeAt(0).widget()
            if w is not None:
                w.deleteLater()
        for i, item in enumerate(items):
            thumb = GifThumb(item, self.net)
            thumb.clicked.connect(self.pick_giphy)
            self.giphy_grid.addWidget(thumb, i // 3, i % 3)
        self.giphy_status.setText((f'"{q}"' if q else "Trending") + ("" if items else " - nothing found"))
        self.giphy_scroll.verticalScrollBar().setValue(0)

    def pick_giphy(self, url):
        self.giphy_picked.emit(url)
        self.close()

    def filter(self, text):
        if self.on_gif_tab() and self.mode == "giphy":
            self.giphy_timer.start()
            return
        if text.strip():
            self.stack.setCurrentIndex(0)
            self.fill(emoji_data.search(text))
        else:
            self.show_tab(self.tabs.currentIndex())

    def choose(self, e):
        recent = [e] + [r for r in self.recent() if r != e]
        settings.setValue("emoji/recent", " ".join(recent[:40]))
        self.picked.emit(e)
        self.close()

    def load_gifs(self):
        self.gifs.clear()
        folder = favorites_folder()
        files = sorted((os.path.join(folder, n) for n in os.listdir(folder)), key=os.path.getmtime, reverse=True)
        for path in files:
            img = QImage(path)
            if img.isNull():
                continue
            it = QListWidgetItem(QIcon(QPixmap.fromImage(img.scaled(
                110, 110, Qt.AspectRatioMode.KeepAspectRatio, Qt.TransformationMode.SmoothTransformation))), "")
            it.setData(Qt.ItemDataRole.UserRole, path)
            it.setToolTip("Click to send")
            self.gifs.addItem(it)
        self.gif_hint.setVisible(self.gifs.count() == 0)

    def gif_menu(self, pos):
        it = self.gifs.itemAt(pos)
        if it:
            m = QMenu(self)
            m.addAction("Remove from favorites", lambda: (os.remove(it.data(Qt.ItemDataRole.UserRole)), self.load_gifs()))
            m.exec(self.gifs.mapToGlobal(pos))

    def add_gif_file(self):
        path, _ = QFileDialog.getOpenFileName(self, "Add a GIF", "", "GIFs (*.gif)")
        if path:
            add_favorite_gif(path)
            self.load_gifs()


def add_favorite_gif(path, name=None):
    with open(path, "rb") as f:
        digest = hashlib.sha1(f.read()).hexdigest()
    stem = os.path.splitext(os.path.basename(name or path))[0]
    if re.fullmatch(r"(u[0-9a-f]{16}|.*-\d+)", stem) or len(stem) > 40:   # cache names aren't nice to send
        stem = "gif"
    existing = [n for n in os.listdir(favorites_folder()) if n.endswith(f"-{digest[:8]}.gif")]
    if existing:
        return os.path.join(favorites_folder(), existing[0])
    dest = os.path.join(favorites_folder(), f"{stem}-{digest[:8]}.gif")
    if not os.path.exists(dest):
        shutil.copyfile(path, dest)
    return dest


class TransferRow(QWidget):
    """One upload or download with a progress bar, speed and a Cancel button."""

    def __init__(self, job: transfers.TransferJob):
        super().__init__()
        self.setObjectName("transfer")
        self.job = job
        self.started = time.monotonic()
        self.samples: list[tuple[float, int]] = []
        h = QHBoxLayout(self)
        h.setContentsMargins(10, 4, 6, 4)
        arrow = "\u2b06" if job.kind == "upload" else "\u2b07"
        self.title = QLabel(f"{arrow}  {html.escape(job.name)}")
        self.title.setMinimumWidth(160)
        self.title.setMaximumWidth(260)
        h.addWidget(self.title)
        self.bar = QProgressBar()
        self.bar.setRange(0, 1000)
        self.bar.setTextVisible(False)
        self.bar.setFixedHeight(8)
        h.addWidget(self.bar, 1)
        self.detail = QLabel("Starting...")
        self.detail.setStyleSheet("color:#949ba4; font-size:8.5pt")
        self.detail.setMinimumWidth(230)
        h.addWidget(self.detail)
        self.cancel_btn = QPushButton("Cancel")
        self.cancel_btn.setProperty("flat", True)
        self.cancel_btn.clicked.connect(job.cancel)
        h.addWidget(self.cancel_btn)
        job.progress.connect(self.update_progress)

    def update_progress(self, done, total):
        now = time.monotonic()
        self.samples.append((now, done))
        self.samples = [x for x in self.samples if now - x[0] < 4]
        speed = 0.0
        if len(self.samples) > 1 and self.samples[-1][0] > self.samples[0][0]:
            speed = (self.samples[-1][1] - self.samples[0][1]) / (self.samples[-1][0] - self.samples[0][0])
        self.bar.setValue(int(1000 * done / total) if total else 0)
        text = f"{transfers.human_size(done)} of {transfers.human_size(total)}"
        if speed > 0 and done < total:
            text += f"  \u00b7  {transfers.human_size(speed)}/s  \u00b7  {transfers.human_time((total - done) / speed)} left"
        self.detail.setText(text)

    def finish(self, ok, message):
        self.cancel_btn.hide()
        self.bar.setValue(1000 if ok else self.bar.value())
        self.detail.setText("Done" if ok else message)
        self.detail.setStyleSheet(f"color:{'#23a55a' if ok else '#f23f42'}; font-size:8.5pt")


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
        # installed builds look for a newer version a moment after opening
        self.updater = updater.schedule_check(self, settings)

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

        conn = Connection(SERVER_HOST, SERVER_PORT, USE_TLS, asset(TLS_CERT) if USE_TLS else None, try_local=True)
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
            text += "\nIs the server running? (On the server PC, Campfire Server must be open.)"
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
class AvatarLabel(QLabel):
    """A round avatar; animated GIFs play (unless GIF animation is turned off)."""

    def __init__(self, size, parent=None):
        super().__init__(parent)
        self.side = size
        self.setFixedSize(size, size)
        self.movie: QMovie | None = None
        self.buf: QBuffer | None = None

    def show_avatar(self, name, raw: bytes | None):
        if self.movie is not None:
            self.movie.stop()
            self.movie.deleteLater()
            self.movie = None
        if raw and is_gif(raw) and settings.value("media/animate", True, type=bool):
            self.buf = QBuffer(self)
            self.buf.setData(QByteArray(raw))
            self.buf.open(QIODevice.OpenModeFlag.ReadOnly)
            mv = QMovie(self.buf, QByteArray(b"gif"), self)
            if mv.isValid() and mv.frameCount() != 1:
                mv.setCacheMode(QMovie.CacheMode.CacheAll)
                mv.frameChanged.connect(lambda _n: self.setPixmap(
                    QPixmap.fromImage(round_image(mv.currentImage(), self.side))))
                self.movie = mv
                mv.start()
                return
        img = QImage.fromData(raw) if raw else QImage()
        if img.isNull():
            img = default_avatar(name, self.side * 2).toImage()
        self.setPixmap(QPixmap.fromImage(round_image(img, self.side)))


class ProfileCard(QFrame):
    """The little card that pops up when you click someone's name or picture."""

    def __init__(self, win: "ChatWindow", name: str):
        super().__init__(win, Qt.WindowType.Popup)
        self.win, self.name = win, name
        self.setObjectName("profilecard")
        th = THEMES[appearance()["theme"]]
        self.setStyleSheet(f"#profilecard {{ background:{th['panel']}; border:1px solid {th['border']}; "
                           f"border-radius:8px; }} QLabel {{ background:transparent; }}")
        v = QVBoxLayout(self)
        v.setContentsMargins(0, 0, 0, 14)
        v.setSpacing(0)
        v.setSizeConstraint(QLayout.SizeConstraint.SetFixedSize)     # grows to fit whatever it shows
        self.banner = QFrame()
        self.banner.setFixedSize(300, 56)
        v.addWidget(self.banner)
        body = QVBoxLayout()
        body.setContentsMargins(16, 0, 16, 0)
        body.setSpacing(6)
        self.avatar = AvatarLabel(80, self)              # sits over the banner's edge
        self.avatar.move(16, 16)
        body.addSpacing(46)
        self.title = QLabel()
        self.title.setTextFormat(Qt.TextFormat.RichText)
        self.title.setWordWrap(True)
        self.title.setFixedWidth(268)
        body.addWidget(self.title)
        self.status = QLabel()
        self.status.setWordWrap(True)
        self.status.setStyleSheet(f"color:{th['muted']}")
        body.addWidget(self.status)
        self.game = QLabel()
        self.game.setWordWrap(True)
        self.game.setStyleSheet("color:#23a55a; font-weight:600")
        body.addWidget(self.game)
        line = QFrame()
        line.setFixedHeight(1)
        line.setStyleSheet(f"background:{th['border']}")
        body.addSpacing(4)
        body.addWidget(line)
        self.about_head = heading("About me")
        body.addWidget(self.about_head)
        self.bio = QLabel()
        self.bio.setWordWrap(True)
        self.bio.setFixedWidth(268)
        self.bio.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse |
                                         Qt.TextInteractionFlag.LinksAccessibleByMouse)
        self.bio.setOpenExternalLinks(True)
        body.addWidget(self.bio)
        body.addWidget(heading("Member since"))
        self.joined = QLabel("...")
        body.addWidget(self.joined)
        self.where = QLabel()
        self.where.setStyleSheet(f"color:{th['muted']}")
        body.addWidget(self.where)
        body.addSpacing(6)
        self.buttons = QHBoxLayout()
        body.addLayout(self.buttons)
        v.addLayout(body)
        self.fill(win.profiles.get(name, {"name": name}))
        self.avatar.show_avatar(name, win.avatar_raw(name))

    def fill(self, info):
        win, name = self.win, info.get("name", self.name)
        self.name = name
        color = win.user_color(name)
        self.banner.setStyleSheet(f"background:{QColor(color).darker(140).name()}; "
                                  f"border-top-left-radius:8px; border-top-right-radius:8px")
        title = f'<span style="font-size:15pt; font-weight:bold; color:{color}">{html.escape(name)}</span>'
        if info.get("admin"):
            title += f' <span style="color:{ADMIN_COLOR}">{ADMIN_BADGE} Admin</span>'
        self.title.setText(title)
        status = info.get("status", "")
        self.status.setText(status)
        self.status.setVisible(bool(status))
        game = info.get("activity", "")
        self.game.setText(f"\U0001f3ae Playing {game} \u00b7 {played_for(info.get('activity_since'))}" if game else "")
        self.game.setVisible(bool(game))
        bio = info.get("bio", "")
        self.bio.setText(render_text(bio) if bio else '<i style="color:#949ba4">Nothing here yet</i>')
        joined = info.get("joined")
        self.joined.setText(time.strftime("%B %d, %Y", time.localtime(joined)).replace(" 0", " ")
                            if joined else "...")
        where = []
        if "online" in info:
            where.append('<span style="color:#23a55a">\u25cf</span> Online' if info["online"] else "Offline")
        if info.get("voice"):
            where.append(f"🔊 in {info['voice']}")
        self.where.setText("  ·  ".join(where))
        while self.buttons.count():
            w = self.buttons.takeAt(0).widget()
            if w:
                w.hide()
                w.deleteLater()
        if info.get("you") or name == win.me:
            self.add_button("Edit profile", lambda: (self.close(), win.open_settings()))
        else:
            self.add_button("Message", lambda: (self.close(), win.open_dm(name)), "go")
            if "friend" in info and not info["friend"]:
                self.add_button("Add friend", lambda: (win.conn.send(t="friend_request", to=name), self.close()))
        self.adjustSize()

    def showEvent(self, ev):
        self.avatar.raise_()
        super().showEvent(ev)

    def add_button(self, text, slot, kind=None):
        b = QPushButton(text)
        if kind is None:
            b.setProperty("flat", True)
        b.clicked.connect(slot)
        self.buttons.addWidget(b)

    def popup(self, pos: QPoint):
        self.adjustSize()
        screen = QApplication.screenAt(pos) or QApplication.primaryScreen()
        area = screen.availableGeometry()
        x = min(max(pos.x(), area.left()), area.right() - self.width())
        y = min(max(pos.y(), area.top()), area.bottom() - self.height())
        self.move(x, y)
        self.show()


class SettingsDialog(QDialog):
    def __init__(self, parent: "ChatWindow"):
        super().__init__(parent)
        self.setWindowTitle("Settings")
        self.win = parent
        self.avatar_data: str | None = None

        self.resize(560, 680)
        outer = QVBoxLayout(self)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        inner = QWidget()
        scroll.setWidget(inner)
        outer.addWidget(scroll, 1)
        form = QFormLayout(inner)
        self.name_input = QLineEdit(parent.me)
        form.addRow("Username", self.name_input)

        row = QHBoxLayout()
        self.preview = AvatarLabel(56)
        self.preview.show_avatar(parent.me, parent.avatar_raw(parent.me))
        pick = QPushButton("Choose image or GIF...")
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
        tip = QLabel("PNG, JPG or an animated GIF (up to 1 MB after shrinking). Everyone sees it next to your messages.")
        tip.setWordWrap(True)
        tip.setStyleSheet("color:#949ba4; font-size:8.5pt")
        form.addRow(tip)

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

        # ---- chat
        form.addRow(heading("Appearance"))
        look = appearance()
        self.theme_combo = QComboBox()
        self.theme_combo.addItems(list(THEMES))
        self.theme_combo.setCurrentText(look["theme"])
        form.addRow("Theme", self.theme_combo)
        self.accent = settings.value("look/accent", "")
        arow = QHBoxLayout()
        self.accent_btn = QPushButton("Choose...")
        self.accent_btn.setProperty("flat", True)
        self.accent_btn.clicked.connect(self.pick_accent)
        reset = QPushButton("Theme default")
        reset.setProperty("flat", True)
        reset.clicked.connect(lambda: self.set_accent(""))
        self.accent_swatch = QLabel()
        self.accent_swatch.setFixedSize(22, 22)
        arow.addWidget(self.accent_swatch)
        arow.addWidget(self.accent_btn)
        arow.addWidget(reset)
        arow.addStretch()
        form.addRow("Accent colour", arow)
        self.theme_combo.currentTextChanged.connect(lambda _t: self.set_accent(self.accent))
        self.set_accent(self.accent)
        self.size_combo2 = QComboBox()
        self.size_combo2.addItems(list(TEXT_SIZES))
        self.size_combo2.setCurrentText(look["size"])
        form.addRow("Text size", self.size_combo2)
        self.anim_switch = QCheckBox("Animations when switching chats")
        self.anim_switch.setChecked(look["animations"])
        form.addRow(self.anim_switch)

        form.addRow(heading("Profile (everyone sees this)"))
        me = parent.users.get(parent.me, {})
        self.name_color = parent.colors.get(parent.me, "")
        crow = QHBoxLayout()
        self.name_preview = QLabel(parent.me)
        self.name_preview.setStyleSheet("font-weight: bold")
        pick_c = QPushButton("Name colour...")
        pick_c.setProperty("flat", True)
        pick_c.clicked.connect(self.pick_name_color)
        clear_c = QPushButton("Default")
        clear_c.setProperty("flat", True)
        clear_c.clicked.connect(lambda: self.set_name_color(""))
        crow.addWidget(self.name_preview)
        crow.addWidget(pick_c)
        crow.addWidget(clear_c)
        crow.addStretch()
        form.addRow("Name", crow)
        self.set_name_color(self.name_color)
        self.status_input = QLineEdit(me.get("status", ""))
        self.status_input.setMaxLength(80)
        self.status_input.setPlaceholderText("e.g. playing Minecraft, brb, studying")
        form.addRow("Status", self.status_input)
        self.bio_input = QPlainTextEdit()
        self.bio_input.setPlaceholderText("About me: a few words for your profile card")
        self.bio_input.setFixedHeight(72)
        self.bio_start = parent.profiles.get(parent.me, {}).get("bio", "")
        self.bio_input.setPlainText(self.bio_start)
        self.bio_count = QLabel()
        self.bio_count.setStyleSheet("color:#949ba4; font-size:8pt")
        self.bio_input.textChanged.connect(self.bio_changed)
        self.bio_changed()
        bio_box = QVBoxLayout()
        bio_box.addWidget(self.bio_input)
        bio_box.addWidget(self.bio_count, alignment=Qt.AlignmentFlag.AlignRight)
        form.addRow("About me", bio_box)

        form.addRow(heading("Playing a game"))
        self.game_switch = QCheckBox("Show what game I'm playing")
        self.game_switch.setChecked(settings.value("activity/show", True, type=bool))
        form.addRow(self.game_switch)
        now = getattr(parent, "detected_game", "")
        self.game_now = QLabel(f"Right now: \U0001f3ae {now}" if now else "Right now: no game found")
        self.game_now.setStyleSheet("color:#949ba4")
        form.addRow(self.game_now)
        self.custom = custom_games()
        self.game_list = QListWidget()
        self.game_list.setMaximumHeight(90)
        form.addRow("Your games", self.game_list)
        row = QHBoxLayout()
        add = QPushButton("Add a game...")
        add.clicked.connect(lambda: self.add_game())
        rm = QPushButton("Remove")
        rm.setProperty("flat", True)
        rm.clicked.connect(self.remove_game)
        row.addWidget(add)
        row.addWidget(rm)
        row.addStretch()
        form.addRow(row)
        tip = QLabel("Most games are found by themselves (popular games, anything from Steam, Epic or the Xbox app, "
                     "and Minecraft). Missing one? Add its .exe. Only the game's name is shared.")
        tip.setWordWrap(True)
        tip.setStyleSheet("color:#949ba4; font-size:8.5pt")
        form.addRow(tip)
        self.fill_games()

        form.addRow(heading("Chat"))
        self.link_box = QCheckBox("Show pictures and GIFs from links (Giphy, Tenor, image links)")
        self.link_box.setChecked(settings.value("media/link_previews", True, type=bool))
        form.addRow(self.link_box)
        self.anim_box = QCheckBox("Play GIFs")
        self.anim_box.setChecked(settings.value("media/animate", True, type=bool))
        form.addRow(self.anim_box)
        self.update_box = QCheckBox(f"Check for updates when Campfire starts (version {updater.VERSION})")
        self.update_box.setChecked(settings.value("update/check", True, type=bool))
        self.update_box.setVisible(updater.can_update())
        form.addRow(self.update_box)

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
        outer.addWidget(buttons)

    def set_accent(self, color):
        self.accent = color
        shown = color or THEME_ACCENTS[self.theme_combo.currentText()]
        self.accent_swatch.setStyleSheet(f"background:{shown}; border-radius: 11px;")

    def pick_accent(self):
        c = QColorDialog.getColor(QColor(self.accent or THEME_ACCENTS[self.theme_combo.currentText()]), self,
                                  "Accent colour")
        if c.isValid():
            self.set_accent(c.name())

    def set_name_color(self, color):
        self.name_color = color
        self.name_preview.setStyleSheet(f"font-weight: bold; color: {color or name_color(self.win.me)};")

    def pick_name_color(self):
        c = QColorDialog.getColor(QColor(self.name_color or name_color(self.win.me)), self, "Name colour")
        if c.isValid():
            self.set_name_color(c.name())

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

    def fill_games(self):
        self.game_list.clear()
        for path, name in sorted(self.custom.items(), key=lambda kv: kv[1].lower()):
            it = QListWidgetItem(f"{name}  ({path})")
            it.setData(Qt.ItemDataRole.UserRole, path)
            self.game_list.addItem(it)
        if not self.custom:
            it = QListWidgetItem("None added")
            it.setFlags(Qt.ItemFlag.NoItemFlags)
            self.game_list.addItem(it)

    def add_game(self, path=None, name=None):
        if not path:
            path, _ = QFileDialog.getOpenFileName(self, "Choose the game's program", "",
                                                  "Programs (*.exe);;All files (*)")
        if not path:
            return
        if name is None:
            guess = os.path.splitext(os.path.basename(path))[0].replace("_", " ").replace("-Win64-Shipping", "")
            name, ok = QInputDialog.getText(self, "Add a game", "What's the game called?", text=guess)
            if not ok:
                return
        name = " ".join(name.split())[:64]
        if name:
            self.custom[re.split(r"[\\/]", path)[-1]] = name
            self.fill_games()

    def remove_game(self):
        it = self.game_list.currentItem()
        if it and it.data(Qt.ItemDataRole.UserRole):
            self.custom.pop(it.data(Qt.ItemDataRole.UserRole), None)
            self.fill_games()

    def bio_changed(self):
        text = self.bio_input.toPlainText()
        if len(text) > MAX_BIO:
            cur = self.bio_input.textCursor()
            self.bio_input.setPlainText(text[:MAX_BIO])
            cur.setPosition(MAX_BIO)
            self.bio_input.setTextCursor(cur)
            return
        self.bio_count.setText(f"{len(text)}/{MAX_BIO}")

    def pick_avatar(self, path=None):
        if not path:
            path, _ = QFileDialog.getOpenFileName(self, "Choose avatar", "",
                                                  "Images and GIFs (*.png *.jpg *.jpeg *.gif *.bmp *.webp)")
        if not path:
            return
        QApplication.setOverrideCursor(Qt.CursorShape.WaitCursor)
        try:
            data, why = encode_avatar(path)
        finally:
            QApplication.restoreOverrideCursor()
        if data is None:
            QMessageBox.warning(self, "Avatar", why)
            return
        if is_gif(base64.b64decode(data)) and not self.win.server_has("gif_avatars"):
            self.win.server_too_old("GIF avatars")
            return
        self.avatar_data = data
        self.preview.show_avatar(self.win.me, base64.b64decode(data))

    def clear_avatar(self):
        self.avatar_data = ""
        self.preview.show_avatar(self.win.me, None)

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
        settings.setValue("look/theme", self.theme_combo.currentText())
        settings.setValue("look/accent", self.accent)
        settings.setValue("look/size", self.size_combo2.currentText())
        settings.setValue("look/animations", self.anim_switch.isChecked())
        settings.setValue("activity/show", self.game_switch.isChecked())
        settings.setValue("activity/custom", json.dumps(self.custom))
        apply_appearance()
        QTimer.singleShot(0, self.win.check_game)
        me = self.win.users.get(self.win.me, {})
        status = " ".join(self.status_input.text().split())
        if self.name_color != self.win.colors.get(self.win.me, "") or status != me.get("status", ""):
            if self.win.server_has("profile"):
                self.win.conn.send(t="profile", color=self.name_color, status=status)
            else:
                self.win.server_too_old("Name colours and statuses")
        bio = self.bio_input.toPlainText().strip()
        if bio != self.bio_start.strip():
            if self.win.server_has("profiles"):
                self.win.conn.send(t="profile", bio=bio)
                self.win.profiles.setdefault(self.win.me, {})["bio"] = bio
            else:
                self.win.server_too_old("Profiles")
        self.win.redraw()
        settings.setValue("media/link_previews", self.link_box.isChecked())
        settings.setValue("media/animate", self.anim_box.isChecked())
        settings.setValue("update/check", self.update_box.isChecked())
        if not self.anim_box.isChecked():
            for mv in self.win.movies.values():
                mv.stop()
            self.win.movies.clear()
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
def custom_games() -> dict:
    """Games added by hand in Settings: {program path or name: game}."""
    try:
        data = json.loads(settings.value("activity/custom", "{}") or "{}")
        return data if isinstance(data, dict) else {}
    except ValueError:
        return {}


def activity_line(info) -> str:
    """'Playing Minecraft' when someone's in a game, otherwise their status."""
    game = info.get("activity", "")
    return f"\U0001f3ae Playing {game}" if game else info.get("status", "")


def played_for(since) -> str:
    mins = int(max(0, time.time() - since) // 60) if since else 0
    if mins < 1:
        return "just started"
    if mins < 60:
        return f"for {mins} min"
    return f"for {mins // 60} h {mins % 60:02d} min"


class ChatWindow(QMainWindow):
    logged_out = pyqtSignal(str)
    game_found = pyqtSignal(str)                    # from the game-detection thread

    def __init__(self, conn: Connection, password: str, login: dict, handoff=None):
        super().__init__()
        self.handoff = handoff
        self.last_seq = login.get("_seq", 0)
        self.conn = conn
        self.password = password
        self.me = login["user"]
        self.rooms: list[str] = []
        self.users: dict[str, dict] = {}            # online name -> {"avatar", "admin", "color", "status"}
        self.colors: dict[str, str] = {}            # name -> chosen name colour (anyone, online or not)
        self.is_admin = False
        self.admin_menu: QMenu | None = None
        self.icons: dict[str, QIcon] = {}
        self.avatar_ids: dict[str, str] = {}        # name -> id of their picture (cached on disk by id)
        self.avatar_wait: set[str] = set()           # pictures we've asked the server for
        self.list_movies: dict[str, QMovie] = {}    # animated avatars in the Online list
        self.profiles: dict[str, dict] = {}         # name -> profile card info
        self.avatar_keys: dict[str, str] = {}       # name -> picture key used in the chat view
        self.card: ProfileCard | None = None
        self.my_game = ""                               # what we last told the server we're playing
        self.game_busy = False
        self.game_found.connect(self.on_game_found)
        self.game_timer = QTimer(self, interval=15_000, timeout=self.check_game)
        self.game_timer.start()
        QTimer.singleShot(3000, self.check_game)
        self.pending_card = None
        self.card_timer = QTimer(self, singleShot=True, timeout=self.show_pending_card)
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
        self.jobs: list = []                         # active uploads/downloads (with rows)
        self.queued: list = []                       # waiting for a free connection
        self.preview_jobs: dict[str, object] = {}    # media key -> job fetching a picture preview
        self.preview_queue: list[tuple] = []
        self.media_paths: dict[str, str] = {}        # media key -> cached file (for open / favorites)
        self.movies: dict[str, QMovie] = {}          # media key -> animation for GIFs
        self.visible_media: set[str] = set()         # media keys in the conversation on screen
        self.net: QNetworkAccessManager | None = None
        self.giphy_key = ""
        self.downloaded: dict[int, str] = {}         # fid -> where we saved it
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
        self.setAcceptDrops(True)
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
        self.room_add = plus_button("Create a channel (admins)", self.create_room)
        top.addWidget(self.room_add)
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
        self.voice_add = plus_button("Create a voice channel (admins)", self.create_voice)
        top.addWidget(self.voice_add)
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
        self.view = ChatView()
        self.view.setOpenLinks(False)
        self.view.anchorClicked.connect(self.link_clicked)
        self.view.context.connect(self.view_menu)
        self.view.document().setDefaultStyleSheet("a { color: #00a8fc; }")
        cv.addWidget(self.view, 1)
        self.transfer_box = QWidget()
        self.transfer_layout = QVBoxLayout(self.transfer_box)
        self.transfer_layout.setContentsMargins(0, 0, 0, 0)
        self.transfer_layout.setSpacing(4)
        self.transfer_box.hide()
        cv.addWidget(self.transfer_box)
        row = QHBoxLayout()
        attach = QPushButton("\U0001f4ce")
        attach.setProperty("flat", True)
        attach.setFixedWidth(40)
        attach.setToolTip("Send a file (up to 5 GB) - you can also drag files onto the window")
        attach.clicked.connect(self.pick_files)
        row.addWidget(attach)
        self.emoji_btn = QPushButton("\U0001f600")
        self.emoji_btn.setProperty("flat", True)
        self.emoji_btn.setFixedWidth(40)
        self.emoji_btn.setToolTip("Emoji and GIFs")
        self.emoji_btn.clicked.connect(self.open_picker)
        row.addWidget(self.emoji_btn)
        self.input = QLineEdit()
        self.input.textEdited.connect(self.input_edited)
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
        self.member_list.itemDoubleClicked.connect(lambda it: self.person_double_clicked(it))
        self.member_list.itemClicked.connect(lambda it, w=self.member_list: self.person_clicked(w, it))
        self.member_list.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.member_list.customContextMenuRequested.connect(lambda p: self.person_menu(self.member_list, p))
        mv.addWidget(self.member_list)
        self.members_box.hide()
        rv.addWidget(self.members_box, 3)
        self.online_heading = heading("Online")
        rv.addWidget(self.online_heading)
        self.user_list = QListWidget()
        self.user_list.setItemDelegate(PersonDelegate(self))
        self.user_list.itemDoubleClicked.connect(lambda it: self.person_double_clicked(it))
        self.user_list.itemClicked.connect(lambda it, w=self.user_list: self.person_clicked(w, it))
        self.user_list.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.user_list.customContextMenuRequested.connect(lambda p: self.person_menu(self.user_list, p))
        rv.addWidget(self.user_list, 3)
        rv.addWidget(heading("Friends"))
        self.friend_list = QListWidget()
        self.friend_list.itemDoubleClicked.connect(lambda it: self.person_double_clicked(it))
        self.friend_list.itemClicked.connect(lambda it, w=self.friend_list: self.person_clicked(w, it))
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
            (f"Check for updates (you have {updater.VERSION})...", self.check_updates, None),
            ("Open Campfire in a browser", lambda: QDesktopServices.openUrl(QUrl(self.website())), None),
            ("Log out", self.logout, None),
            ("Quit", QApplication.quit, "Ctrl+Q"),
        ):
            act = QAction(text, self)
            act.triggered.connect(slot)
            if key:
                act.setShortcut(key)
            m.addAction(act)

    def website(self):
        """The GitHub website once the server has https; before that, the page the server hands out."""
        domain = getattr(self, "https_domain", "")
        if domain:
            name = domain.split(".")[0]
            return WEB_URL if WEB_SERVER and WEB_SERVER.split(".")[0] == name else f"{WEB_URL}?server={name}"
        return f"http://{SERVER_HOST}:{SERVER_PORT}/"

    # ---------------------------------------------------------- "Playing ..." (games.py)
    def check_game(self):
        if self.game_busy or not settings.value("activity/show", True, type=bool):
            if not settings.value("activity/show", True, type=bool) and self.my_game:
                self.on_game_found("")
            return
        self.game_busy = True
        custom = custom_games()

        def work():
            try:
                game = games.current_game(custom)
            except Exception:
                game = ""
            self.game_found.emit(game)

        threading.Thread(target=work, daemon=True).start()

    def on_game_found(self, game):
        self.game_busy = False
        if not settings.value("activity/show", True, type=bool):
            game = ""
        self.detected_game = game
        if game != self.my_game and self.server_has("activity") and self.conn is not None:
            self.my_game = game
            self.conn.send(t="activity", text=game)

    def server_has(self, feature):
        return feature in getattr(self, "server_features", set())

    def server_too_old(self, what):
        """The server hasn't been updated (the app's installer doesn't include the server)."""
        who = ("Open the Campfire Server window and click <b>Update server</b> (top of the window), "
               "or close it and start it again if you already updated it."
               if self.is_admin else "Ask whoever runs the server to update it (Update server in the Campfire Server window).")
        QMessageBox.information(self, "Server needs updating",
                                f"{what} need a newer Campfire Server than the one you're connected to.<br><br>{who}")

    def admin_controls(self):
        """Only admins can create channels, so only they see the + buttons."""
        for b in (getattr(self, "room_add", None), getattr(self, "voice_add", None)):
            if b is not None:
                b.setVisible(self.is_admin)

    def update_title(self):
        where = "  \u00b7  server on this PC" if getattr(getattr(self, "conn", None), "local", False) else ""
        self.setWindowTitle(f"Campfire - {self.me}" + ("  (admin)" if self.is_admin else "") + where)

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
        self.colors = dict(login.get("colors", {}))
        self.server_features = set(login.get("features", []))
        self.my_game = ""                               # a new session: tell the server again
        QTimer.singleShot(1500, self.check_game)
        self.server_version = login.get("server", "")
        # DMs you've had come back every time (unless you closed them)
        closed = self.closed_dms()
        for name in login.get("dms", []):
            if name not in closed and name != self.me:
                self.convs.setdefault("@" + name, [])
        self.is_admin = bool(login.get("admin"))
        self.update_title()
        self.admin_controls()
        if self.is_admin and not self.server_has("profile"):
            QTimer.singleShot(3200, lambda: self.statusBar().showMessage(
                "The server is older than this app - open the Campfire Server window and click Update server", 60000))
        if self.is_admin and self.admin_menu is None:
            self.build_admin_menu()
        if self.admin_menu is not None:
            self.admin_menu.menuAction().setVisible(self.is_admin)
        self.icons.clear()
        self.profiles.clear()
        self.avatar_ids = dict(login.get("avatars", {}))
        for u in login["users"]:
            self.note_avatar(u)
        if self.server_has("profiles"):
            self.conn.send(t="get_profile", user=self.me)     # our own "About me" for Settings
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
        # look again: the server on this PC may have stopped, or started
        conn = Connection(SERVER_HOST, SERVER_PORT, old.use_tls, old.ca_file, try_local=True)
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
                                           "admin": msg.get("admin", False), "file": msg.get("file"),
                                           "id": msg.get("id")})
            if msg["from"] != self.me and not self.isActiveWindow():
                QApplication.alert(self)
        elif kind == "user_join":
            self.users[msg["name"]] = msg
            self.note_avatar(msg)
            self.refresh_users()
        elif kind == "avatar_data":
            self.avatar_arrived(msg)
        elif kind == "profile_info":
            self.profiles[msg["name"]] = msg
            if self.card is not None and self.card.isVisible() and self.card.name.lower() == msg["name"].lower():
                self.card.fill(msg)
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
        elif kind == "config":
            self.giphy_key = msg.get("giphy_key", "")
            self.https_domain = msg.get("https_domain", "")
        elif kind == "admin_changed":
            self.is_admin = bool(msg["admin"])
            self.update_title()
            self.admin_controls()
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
        elif kind == "msg_deleted":
            self.message_deleted(msg["id"])
        elif kind == "file_deleted":
            fid = msg["fid"]
            for msgs in self.convs.values():
                for m in msgs:
                    if (m.get("file") or {}).get("id") == fid:
                        m["file"] = {"id": fid, "deleted": True}
            self.forget_media(f"f{fid}")
            self.redraw()
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
            m = re.match(r"Unknown command '(\w+)'", msg.get("msg", ""))
            if m:                                    # an older server: say so plainly
                self.notice(f"The server is out of date and doesn't know '{m.group(1)}' yet - "
                            "it needs updating (Update server in the Campfire Server window)", error=True)
            else:
                self.notice(msg["msg"], error=True)

    def user_updated(self, msg):
        old, new = msg["old"], msg["name"]
        self.colors.pop(old, None)
        if msg.get("color"):
            self.colors[new] = msg["color"]
        self.users.pop(old, None)
        self.users[new] = msg
        self.icons.pop(old, None)
        self.icons.pop(new, None)
        self.profiles.pop(old, None)
        self.profiles.pop(new, None)
        if new == self.me and self.server_has("profiles"):
            self.conn.send(t="get_profile", user=new)        # keep our own About me current
        if old != new:
            self.avatar_ids.pop(old, None)
        changed = self.note_avatar(msg)
        if changed or old != new:
            self.avatar_changed(new)
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

    def closed_key(self):
        key = f"dms/closed/{SERVER_HOST}/{self.me}"
        if not settings.contains(key):          # the server's address changed (IP -> DuckDNS name): keep the list
            for old in settings.allKeys():
                if old.startswith("dms/closed/") and old.endswith(f"/{self.me}") and old != key:
                    settings.setValue(key, settings.value(old, ""))
                    break
        return key

    def closed_dms(self):
        return set(filter(None, (settings.value(self.closed_key(), "") or "").split("\n")))

    def set_closed(self, names):
        settings.setValue(self.closed_key(), "\n".join(sorted(names)))

    def ensure_dm(self, name):
        closed = self.closed_dms()
        if name in closed:                          # a new message (or opening it) brings it back
            self.set_closed(closed - {name})
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
        self.fade(self.view)
        self.fade(self.title, 140)

    def fade(self, widget, ms=180):
        """A short fade-in when the content changes (turn off in Settings > Appearance)."""
        if not appearance()["animations"] or not self.isVisible():
            return
        effect = QGraphicsOpacityEffect(widget)
        widget.setGraphicsEffect(effect)
        anim = QPropertyAnimation(effect, b"opacity", widget)
        anim.setDuration(ms)
        anim.setStartValue(0.0)
        anim.setEndValue(1.0)
        anim.setEasingCurve(QEasingCurve.Type.OutCubic)
        anim.finished.connect(lambda: widget.setGraphicsEffect(None))   # effects slow drawing; drop it after
        anim.start(QPropertyAnimation.DeletionPolicy.DeleteWhenStopped)
        widget._fade = anim

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
        self.set_closed(self.closed_dms() | {key[1:]})
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
        if (obj is self.input and ev.type() == QEvent.Type.KeyPress
                and ev.matches(QKeySequence.StandardKey.Paste) and self.paste_media()):
            return True
        if (self.engine is not None and self.engine.mode == "ptt"
                and ev.type() in (QEvent.Type.KeyPress, QEvent.Type.KeyRelease)
                and ev.key() == getattr(self, "ptt_key", None)):
            if not ev.isAutoRepeat():
                self.set_ptt(ev.type() == QEvent.Type.KeyPress)
            return True                          # don't type the push-to-talk key into the chat box
        return super().eventFilter(obj, ev)

    def create_voice(self):
        if not self.is_admin:
            self.notice("Only admins can create channels", error=True)
            return
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
        self.fade(self.center_stack.currentWidget(), 220)
        self.sync_movies()

    def show_chat(self):
        self.center_stack.setCurrentIndex(0)
        self.fade(self.center_stack.currentWidget(), 220)
        self.sync_movies()

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

    # ---------------------------------------------------------- files
    def server_info(self):
        return (self.conn.host, self.conn.port, self.conn.use_tls, self.conn.ca_file)

    def pick_files(self):
        paths, _ = QFileDialog.getOpenFileNames(self, "Send files", "", "All files (*)")
        for p in paths:
            self.upload_file(p)

    def upload_file(self, path, conv=None):
        conv = conv or self.current
        try:
            size = os.path.getsize(path)
        except OSError:
            self.notice(f"Can't read {path}", error=True)
            return None
        if size == 0:
            self.notice(f"{os.path.basename(path)} is empty", error=True)
            return None
        if size > transfers.MAX_FILE:
            self.notice(f"{os.path.basename(path)} is {transfers.human_size(size)} - files can be at most 5 GB",
                        error=True)
            return None
        caption = self.input.text().strip()
        if caption and not caption.startswith("/"):
            self.input.clear()
        else:
            caption = ""
        job = transfers.TransferJob(self.server_info(), self.me, self.password, "upload",
                                    path=path, conv=conv, text=caption, size=size)
        self.queue_job(job)
        return job

    def download_file(self, fid, dest=None):
        f = self.find_file(fid)
        if f is None:
            return None
        if dest is None:
            folder = QStandardPaths.writableLocation(QStandardPaths.StandardLocation.DownloadLocation) or os.getcwd()
            dest, _ = QFileDialog.getSaveFileName(self, "Save file", os.path.join(folder, f["name"]))
            if not dest:
                return None
        job = transfers.TransferJob(self.server_info(), self.me, self.password, "download",
                                    fid=fid, dest=dest, name=f["name"], size=f["size"])
        self.queue_job(job)
        return job

    def queue_job(self, job):
        row = TransferRow(job)
        job.row = row
        self.transfer_layout.addWidget(row)
        self.transfer_box.show()
        job.finished.connect(lambda ok, text, result, j=job: self.job_done(j, ok, text, result))
        self.queued.append(job)
        self.start_jobs()

    def start_jobs(self):
        while self.queued and len(self.jobs) < 3:     # the server allows 4 file connections each
            job = self.queued.pop(0)
            self.jobs.append(job)
            job.start()

    def job_done(self, job, ok, text, result):
        if job in self.jobs:
            self.jobs.remove(job)
        job.row.finish(ok, text)
        if job.kind == "download" and ok:
            fid = job.params["fid"]
            self.downloaded[fid] = result
            self.view.append(f'<div style="color:#949ba4"><i>Saved {html.escape(job.name)}</i> - '
                             f'<a href="campfire://open/{fid}">Open</a> &nbsp;'
                             f'<a href="campfire://folder/{fid}">Show in folder</a></div>')
            self.scroll_down()
        elif not ok and text != "Cancelled":
            self.notice(f"{'Upload' if job.kind == 'upload' else 'Download'} of {job.name} failed: {text}",
                        error=True)
        QTimer.singleShot(4000, lambda: self.remove_row(job))
        self.start_jobs()

    def remove_row(self, job):
        try:
            job.row.setParent(None)
            job.row.deleteLater()
        except RuntimeError:
            pass
        if self.transfer_layout.count() == 0:
            self.transfer_box.hide()

    def find_file(self, fid):
        for msgs in self.convs.values():
            for m in msgs:
                f = m.get("file") or {}
                if f.get("id") == fid and not f.get("deleted"):
                    return f
        return None

    def link_clicked(self, url: QUrl):
        if url.scheme() != "campfire":
            QDesktopServices.openUrl(url)
            return
        action = url.host()
        if action == "user":
            self.show_profile(unquote(url.path().strip("/")))
            return
        if action == "msg":
            try:
                self.message_menu(int(url.path().strip("/")), QCursor.pos())
            except ValueError:
                pass
            return
        if action == "view":
            path = self.media_paths.get(url.path().strip("/"))
            if path and os.path.exists(path):
                QDesktopServices.openUrl(QUrl.fromLocalFile(path))
            return
        try:
            fid = int(url.path().strip("/"))
        except ValueError:
            return
        if action == "download":
            self.download_file(fid)
        elif action in ("open", "folder") and fid in self.downloaded:
            path = self.downloaded[fid]
            QDesktopServices.openUrl(QUrl.fromLocalFile(path if action == "open" else os.path.dirname(path)))

    # ---------------------------------------------------------- pictures, GIFs and emoji
    def media_img(self, key):
        img = self.view.thumbs[key]
        return (f'<a href="campfire://view/{key}"><img src="campfire-img:{key}" '
                f'width="{img.width()}" height="{img.height()}"></a>')

    def preview_path(self, fid):
        f = self.find_file(fid)
        if f is None:
            return None
        return os.path.join(cache_folder("previews"),
                            f"{self.conn.host}-{fid}{os.path.splitext(f['name'])[1].lower()}")

    def want_preview(self, f):
        key = f"f{f['id']}"
        if key in self.preview_jobs or key in self.view.thumbs:
            return
        path = self.preview_path(f["id"])
        self.preview_jobs[key] = None
        if path and os.path.exists(path):
            QTimer.singleShot(0, lambda: self.preview_ready(key, path))
            return
        self.preview_queue.append((key, f))
        self.next_preview()

    def next_preview(self):
        if any(j is not None for j in self.preview_jobs.values()) or not self.preview_queue:
            return
        key, f = self.preview_queue.pop(0)
        dest = self.preview_path(f["id"])
        if dest is None:
            self.preview_jobs.pop(key, None)
            return
        job = transfers.TransferJob(self.server_info(), self.me, self.password, "download",
                                    fid=f["id"], dest=dest, name=f["name"], size=f["size"])
        job.finished.connect(lambda ok, text, result, k=key: self.preview_ready(k, result if ok else None))
        self.preview_jobs[key] = job
        job.start()

    def preview_ready(self, key, path):
        self.preview_jobs.pop(key, None)
        img = QImage(path) if path else QImage()
        if not img.isNull():
            thumb = img if img.width() <= 360 and img.height() <= 240 else img.scaled(
                360, 240, Qt.AspectRatioMode.KeepAspectRatio, Qt.TransformationMode.SmoothTransformation)
            self.view.thumbs[key] = thumb
            self.media_paths[key] = path
            self.make_movie(key, path, thumb.size())
            if key in self.visible_media:
                bar = self.view.verticalScrollBar()
                at_bottom = bar.value() >= bar.maximum() - 4
                old = bar.value()
                self.redraw()
                bar.setValue(bar.maximum() if at_bottom else old)
        self.next_preview()

    def forget_media(self, key):
        self.view.thumbs.pop(key, None)
        self.media_paths.pop(key, None)
        mv = self.movies.pop(key, None)
        if mv is not None:
            mv.stop()

    # GIFs: a QMovie per animated picture; each new frame replaces the picture in the chat
    def make_movie(self, key, path, size):
        if key in self.movies or not settings.value("media/animate", True, type=bool):
            return
        mv = QMovie(path)
        if not mv.isValid() or mv.frameCount() == 1:
            return
        if not key.startswith("a"):                    # avatars are scaled (and rounded) per frame
            mv.setScaledSize(size)
        mv.setCacheMode(QMovie.CacheMode.CacheAll)
        mv.frameChanged.connect(lambda _n, k=key: self.gif_frame(k))
        self.movies[key] = mv
        self.sync_movies()

    def gif_frame(self, key):
        mv = self.movies.get(key)
        if mv is None:
            return
        img = mv.currentImage()
        if key.startswith("a"):                         # avatars are round
            img = round_image(img, CHAT_AVATAR)
        self.view.thumbs[key] = img
        self.view.document().addResource(QTextDocument.ResourceType.ImageResource.value,
                                         QUrl(f"campfire-img:{key}"), img)
        self.view.viewport().update()

    def sync_movies(self):
        """Only GIFs in the conversation on screen play; the rest pause."""
        for key, mv in self.movies.items():
            if key in self.visible_media and self.isVisible() and self.center_stack.currentIndex() == 0:
                if mv.state() == QMovie.MovieState.NotRunning:
                    mv.start()
                elif mv.state() == QMovie.MovieState.Paused:
                    mv.setPaused(False)
            elif mv.state() == QMovie.MovieState.Running:
                mv.setPaused(True)

    # pictures and GIFs from links (direct image links, Giphy, Tenor)
    def link_media(self, url):
        if not settings.value("media/link_previews", True, type=bool):
            return ""
        found = media_link(url)
        if found is None:
            return ""
        key = "u" + hashlib.sha1(url.encode()).hexdigest()[:16]
        self.visible_media.add(key)
        if key in self.view.thumbs:
            return "<br>" + self.media_img(key)
        if key not in self.preview_jobs:
            self.preview_jobs[key] = None
            cached = [n for n in os.listdir(cache_folder("links")) if n.startswith(key + ".")]
            if cached:
                path = os.path.join(cache_folder("links"), cached[0])
                QTimer.singleShot(0, lambda: self.preview_ready(key, path))
            else:
                self.fetch_media(found[0], key, found[1])
        return ""

    def fetch_media(self, url, key, is_page):
        if self.net is None:
            self.net = QNetworkAccessManager(self)
        req = QNetworkRequest(QUrl(url))
        req.setHeader(QNetworkRequest.KnownHeaders.UserAgentHeader, "Mozilla/5.0 (Campfire chat)")
        reply = self.net.get(req)
        reply.downloadProgress.connect(
            lambda got, total: reply.abort() if max(got, total) > transfers.PREVIEW_MAX else None)
        reply.finished.connect(lambda: self.media_fetched(reply, key, is_page))

    def media_fetched(self, reply, key, is_page):
        ok = reply.error() == QNetworkReply.NetworkError.NoError
        data = bytes(reply.readAll())
        ctype = (reply.header(QNetworkRequest.KnownHeaders.ContentTypeHeader) or "").lower()
        reply.deleteLater()
        if not ok or not data:
            return
        if is_page:                                   # Tenor page: find the GIF it shows
            page = data.decode("utf-8", "replace")
            m = re.search(r'https://media\d*\.tenor\.com/[^"\s<>]+?\.gif', page)
            if m:
                self.fetch_media(m.group(0), key, False)
            return
        ext = {"image/gif": ".gif", "image/png": ".png", "image/jpeg": ".jpg", "image/webp": ".webp"}.get(
            ctype.split(";")[0].strip())
        if ext is None:
            ext = ".gif" if data[:4] == b"GIF8" else ".png" if data[:4] == b"\x89PNG" else \
                ".jpg" if data[:2] == b"\xff\xd8" else None
        if ext is None:
            return
        path = os.path.join(cache_folder("links"), key + ext)
        with open(path, "wb") as f:
            f.write(data)
        self.preview_ready(key, path)

    # right-click on the chat: favorites, open, save
    # ---------------------------------------------------------- deleting messages
    def message_at(self, pos):
        """The message under a point in the chat view: each message starts with its timestamp link."""
        block = self.view.cursorForPosition(pos).block()

        def links(b):
            it = b.begin()
            while not it.atEnd():
                href = it.fragment().charFormat().anchorHref()
                if href:
                    yield href
                it += 1

        here = list(links(block))
        if here and all(h.startswith("campfire://user/") for h in here):    # the avatar: message is next
            for h in links(block.next()):
                if h.startswith("campfire://msg/"):
                    return self.find_message(int(h.rsplit("/", 1)[1]))
        while block.isValid():
            it = block.begin()
            while not it.atEnd():
                href = it.fragment().charFormat().anchorHref()
                if href.startswith("campfire://msg/"):
                    return self.find_message(int(href.rsplit("/", 1)[1]))
                it += 1
            block = block.previous()
        return None

    def find_message(self, mid):
        for msgs in self.convs.values():
            for m in msgs:
                if m.get("id") == mid:
                    return m
        return None

    def can_delete(self, m):
        if m["user"] == self.me:
            return True
        if self.current.startswith("#"):
            return self.is_admin
        g = self.current_group()
        return g is not None and g["owner"] == self.me

    def add_message_actions(self, menu, m):
        menu.addAction("Copy text", lambda: QApplication.clipboard().setText(m["text"]))
        if self.can_delete(m):
            label = "Delete message" if m["user"] == self.me else f"Delete {m['user']}'s message"
            menu.addAction(label, lambda: self.delete_message(m))

    def message_menu(self, mid, global_pos):
        m = self.find_message(mid)
        if m is None:
            return
        menu = QMenu(self)
        self.add_message_actions(menu, m)
        menu.exec(global_pos)

    def delete_message(self, m):
        preview = m["text"] if len(m["text"]) <= 80 else m["text"][:77] + "..."
        if m.get("file"):
            preview = (preview + "\n" if preview else "") + f"(and the file {m['file'].get('name', '')})"
        if QMessageBox.question(self, "Delete message", f"Delete this message for everyone?\n\n{preview}") \
                == QMessageBox.StandardButton.Yes:
            self.conn.send(t="delete_msg", id=m["id"])

    def message_deleted(self, mid):
        for key, msgs in self.convs.items():
            for i, m in enumerate(msgs):
                if m.get("id") == mid:
                    del msgs[i]
                    if m.get("file"):
                        self.forget_media(f"f{m['file'].get('id')}")
                    if key == self.current:
                        bar = self.view.verticalScrollBar()
                        at_bottom, old = bar.value() >= bar.maximum() - 4, bar.value()
                        self.redraw()
                        bar.setValue(bar.maximum() if at_bottom else old)
                    return

    def view_menu(self, pos):
        menu = self.view.createStandardContextMenu()
        m = self.message_at(pos)
        if m is not None and m.get("id"):
            menu.addSeparator()
            self.add_message_actions(menu, m)
        anchor = self.view.anchorAt(pos)
        if anchor.startswith("campfire://view/"):
            key = anchor.rsplit("/", 1)[1]
            path = self.media_paths.get(key)
            if path and os.path.exists(path):
                menu.addSeparator()
                if path.lower().endswith(".gif"):
                    f = self.find_file(int(key[1:])) if key.startswith("f") else None
                    menu.addAction("Add to favorite GIFs", lambda: (add_favorite_gif(path, f and f["name"]),
                                                                     self.statusBar().showMessage(
                                                                         "Added to your favorite GIFs", 4000)))
                menu.addAction("Open image", lambda: QDesktopServices.openUrl(QUrl.fromLocalFile(path)))
                menu.addAction("Save image as...", lambda: self.save_media(path))
        menu.exec(self.view.viewport().mapToGlobal(pos))

    def save_media(self, path):
        dest, _ = QFileDialog.getSaveFileName(self, "Save image", os.path.basename(path))
        if dest:
            shutil.copyfile(path, dest)

    # emoji picker and :shortcodes:
    def open_picker(self):
        if self.net is None:
            self.net = QNetworkAccessManager(self)
        picker = EmojiPicker(self, self.giphy_key, self.net)
        picker.picked.connect(self.insert_text)
        picker.gif_picked.connect(self.send_gif)
        picker.giphy_picked.connect(self.send_text)
        top_left = self.emoji_btn.mapToGlobal(QPoint(0, 0))
        picker.move(top_left.x(), top_left.y() - picker.height() - 6)
        picker.show()
        picker.search.setFocus()
        self.picker = picker

    def send_text(self, text):
        """Send a message to whatever conversation is open (used for GIPHY picks)."""
        if self.current.startswith("#"):
            self.conn.send(t="msg", room=self.current, text=text)
        elif self.current.startswith("g:"):
            self.conn.send(t="group_msg", gid=int(self.current[2:]), text=text)
        else:
            self.conn.send(t="dm", to=self.current[1:], text=text)

    def insert_text(self, text):
        self.input.insert(text)
        self.input.setFocus()

    def send_gif(self, path):
        self.upload_file(path)

    def input_edited(self, _text=None):
        text = self.input.text()
        pos = min(self.input.cursorPosition(), len(text))
        if pos == 0 or text[pos - 1] != ":":
            return
        before = emoji_data.replace_shortcodes(text[:pos])
        if before != text[:pos]:
            self.input.setText(before + text[pos:])
            self.input.setCursorPosition(len(before))

    # paste pictures, GIFs or files straight into the chat
    def paste_media(self):
        md = QApplication.clipboard().mimeData()
        files = [u.toLocalFile() for u in md.urls() if u.isLocalFile() and os.path.isfile(u.toLocalFile())]
        if files:
            for f in files:
                self.upload_file(f)
            return True
        if md.hasText():
            return False
        stamp = time.strftime("%Y%m%d-%H%M%S")
        if "image/gif" in md.formats():
            path = os.path.join(cache_folder("pasted"), f"pasted-{stamp}.gif")
            with open(path, "wb") as f:
                f.write(bytes(md.data("image/gif")))
            self.upload_file(path)
            return True
        if md.hasImage():
            img = QImage(md.imageData())
            if not img.isNull():
                path = os.path.join(cache_folder("pasted"), f"pasted-{stamp}.png")
                img.save(path, "PNG")
                self.upload_file(path)
                return True
        return False

    # drag and drop files onto the window
    def dragEnterEvent(self, ev):
        if ev.mimeData().hasUrls() and any(u.isLocalFile() for u in ev.mimeData().urls()):
            ev.acceptProposedAction()

    def dropEvent(self, ev):
        for u in ev.mimeData().urls():
            if u.isLocalFile() and os.path.isfile(u.toLocalFile()):
                self.upload_file(u.toLocalFile())

    def load_history(self, key, messages):
        live = [m for m in self.convs.get(key, []) if m.get("live")]
        seen = {(m["user"], m["text"], m["ts"]) for m in messages}
        self.convs[key] = messages + [m for m in live if (m["user"], m["text"], m["ts"]) not in seen]
        self.loaded.add(key)
        if key == self.current:
            self.redraw()

    def add_message(self, key, msg, count_unread=True):
        msg = {"user": msg["user"], "text": msg["text"], "ts": msg["ts"], "live": True,
               "admin": msg.get("admin", False), "kind": msg.get("kind", ""), "file": msg.get("file"),
               "id": msg.get("id")}
        was_empty = not self.convs.get(key)
        self.convs.setdefault(key, []).append(msg)
        if key == self.current:
            if was_empty:
                self.redraw()                     # replaces the "No messages yet" placeholder
            else:
                self.view.append(self.format(msg))
                QTimer.singleShot(0, self.sync_movies)
            self.scroll_down()
        elif count_unread and msg["user"] != self.me:
            self.unread[key] = self.unread.get(key, 0) + 1
            self.refresh_rooms() if key.startswith("#") else self.refresh_dms()

    def notice(self, text, error=False):
        color = "#f23f42" if error else "#949ba4"
        self.view.append(f'<div style="color:{color}"><i>{html.escape(text)}</i></div>')
        self.scroll_down()

    def format(self, m, grouped=False):
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
        color = self.user_color(m["user"])
        link = "campfire://user/" + quote(m["user"])
        who = f'<a href="{link}" style="color:{color}; text-decoration:none"><b>{html.escape(m["user"])}</b></a>'
        if m.get("admin"):
            who += f' <span style="color:{ADMIN_COLOR}" title="Admin">{ADMIN_BADGE}</span>'
        if not m.get("file") and emoji_data.only_emoji(m["text"]):
            body = f'<span style="font-size:32px">{html.escape(m["text"])}</span>'
        else:
            body = render_text(m["text"])
        if m.get("file"):
            body = (body + "<br>" if body else "") + self.file_card(m["file"])
        urls = URL_RE.findall(m["text"])[:3]
        media = "".join(self.link_media(url) for url in urls)
        if media and not m.get("file") and len(urls) == 1 and m["text"].strip() == urls[0]:
            body = media[len("<br>"):]                 # just the GIF, like Discord
        else:
            body += media
        small = 'style="color:#6d6f78; text-decoration:none; font-size:7.5pt"'
        stamp = (f'<a href="campfire://msg/{m["id"]}" {small}>{when}</a>' if m.get("id")
                 else f'<span {small}>{when}</span>')
        gutter = CHAT_AVATAR + 14
        if grouped:     # same person a moment later: no picture or name again, the time in the gutter
            return (f'<table width="100%" cellspacing="0" cellpadding="0" style="margin-top:2px"><tr>'
                    f'<td width="{gutter}" valign="top" align="center">{stamp}</td>'
                    f'<td valign="top">{body}</td></tr></table>')
        key = self.avatar_key(m["user"])
        pic = (f'<a href="{link}"><img src="campfire-img:{key}" width="{CHAT_AVATAR}" '
               f'height="{CHAT_AVATAR}"></a>')
        return (f'<table width="100%" cellspacing="0" cellpadding="0" style="margin-top:12px"><tr>'
                f'<td width="{gutter}" valign="top">{pic}</td>'
                f'<td valign="top">{who}&nbsp;&nbsp;{stamp}<br>{body}</td></tr></table>')

    @staticmethod
    def follows(prev, m):
        """Discord-style grouping: the same person again within 7 minutes."""
        return (prev is not None and not prev.get("kind") and not m.get("kind")
                and prev.get("user") == m.get("user") and 0 <= m.get("ts", 0) - prev.get("ts", 0) < 420)

    def file_card(self, f):
        if f.get("deleted") or not f.get("name"):
            return '<i style="color:#949ba4">(file deleted)</i>'
        fid, name, size = f["id"], f["name"], f["size"]
        links = [f'<a href="campfire://download/{fid}">Download</a>']
        if fid in self.downloaded and os.path.exists(self.downloaded[fid]):
            links = [f'<a href="campfire://open/{fid}">Open</a>', f'<a href="campfire://folder/{fid}">Show in folder</a>']
        preview = ""
        if os.path.splitext(name)[1].lower() in transfers.IMAGE_EXTS and size <= transfers.PREVIEW_MAX:
            key = f"f{fid}"
            self.visible_media.add(key)
            if key in self.view.thumbs:
                preview = "<br>" + self.media_img(key)
            else:
                self.want_preview(f)
        return (f'<table cellpadding="8" cellspacing="0" style="margin-top:4px"><tr><td bgcolor="#232428">'
                f'{file_emoji(name)}&nbsp; <b>{html.escape(name)}</b> '
                f'<span style="color:#949ba4">({transfers.human_size(size)})</span>&nbsp;&nbsp; '
                f'{" &nbsp;".join(links)}{preview}</td></tr></table>')

    def redraw(self):
        self.visible_media = set()
        QTimer.singleShot(0, self.sync_movies)
        msgs = self.convs.get(self.current, [])
        if msgs:
            parts, prev = [], None
            for m in msgs:
                parts.append(self.format(m, grouped=self.follows(prev, m)))
                prev = m
            body = "".join(parts)
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
            self.icons[name] = QIcon(QPixmap.fromImage(self.avatar_image(name, 64)))
        return self.icons[name]

    # ---------------------------------------------------------- avatars and profiles
    def note_avatar(self, entry) -> bool:
        """A user entry told us which picture someone has. True if it changed."""
        if "av" not in entry:
            return False
        name, av = entry["name"], entry.get("av") or ""
        if self.avatar_ids.get(name, "") == av:
            return False
        if av:
            self.avatar_ids[name] = av
        else:
            self.avatar_ids.pop(name, None)
        return True

    def avatar_path(self, name):
        """The cached picture file for someone, or None (it's then fetched, once)."""
        av = self.avatar_ids.get(name)
        if not av:
            return None
        path = os.path.join(cache_folder("avatars"), av)
        if os.path.exists(path):
            return path
        if name not in self.avatar_wait and self.conn is not None:
            self.avatar_wait.add(name)
            self.conn.send(t="get_avatar", user=name)
        return None

    def avatar_raw(self, name):
        path = self.avatar_path(name)
        if path:
            try:
                with open(path, "rb") as f:
                    return f.read()
            except OSError:
                pass
        return None

    def avatar_arrived(self, msg):
        name, av, data = msg["user"], msg.get("av", ""), msg.get("data", "")
        self.avatar_wait.discard(name)
        if av and data:
            try:
                raw = base64.b64decode(data)
            except ValueError:
                return
            path = os.path.join(cache_folder("avatars"), av)
            with open(path + ".part", "wb") as f:
                f.write(raw)
            os.replace(path + ".part", path)
        self.note_avatar({"name": name, "av": av})
        self.avatar_changed(name)

    def avatar_image(self, name, size) -> QImage:
        path = self.avatar_path(name)
        img = QImage(path) if path else QImage()
        if img.isNull():
            img = default_avatar(name, size * 2).toImage()
        return round_image(img, size)

    def avatar_pixmap(self, name, size):
        """For the Online list: the current frame of an animated avatar, or the picture."""
        mv = self.list_movies.get(name)
        if mv is None and settings.value("media/animate", True, type=bool):
            path = self.avatar_path(name)
            if path and is_gif(open(path, "rb").read(6)):
                mv = QMovie(path, QByteArray(b"gif"), self)
                if mv.isValid() and mv.frameCount() != 1:
                    mv.setCacheMode(QMovie.CacheMode.CacheAll)
                    mv.frameChanged.connect(lambda _n: self.user_list.viewport().update())
                    mv.start()
                    self.list_movies[name] = mv
                else:
                    mv = None
        if mv is not None:
            return QPixmap.fromImage(round_image(mv.currentImage(), size))
        return self.icon_for(name).pixmap(size, size)

    def avatar_key(self, name):
        """The picture next to someone's messages, as a campfire-img key."""
        av = self.avatar_ids.get(name)
        path = self.avatar_path(name) if av else None
        key = f"a{av}" if path else "a-" + hashlib.sha1(name.lower().encode()).hexdigest()[:10]
        if key not in self.view.thumbs:
            self.view.thumbs[key] = self.avatar_image(name, CHAT_AVATAR)
            if path:
                self.media_paths[key] = path
                self.make_movie(key, path, QSize(CHAT_AVATAR, CHAT_AVATAR))
        self.visible_media.add(key)
        self.avatar_keys[name] = key
        return key

    def avatar_changed(self, name):
        """Someone's picture changed (or finally arrived): redraw everywhere it shows."""
        self.icons.pop(name, None)
        mv = self.list_movies.pop(name, None)
        if mv is not None:
            mv.stop()
            mv.deleteLater()
        key = self.avatar_keys.pop(name, None)
        if key and key not in self.avatar_keys.values():
            self.forget_media(key)
        self.refresh_users()
        self.refresh_friends()
        self.refresh_voice()
        if any(m.get("user") == name for m in self.convs.get(self.current, [])):
            bar = self.view.verticalScrollBar()
            at_bottom = bar.value() >= bar.maximum() - 4
            old = bar.value()
            self.redraw()
            bar.setValue(bar.maximum() if at_bottom else old)
        if self.card is not None and self.card.isVisible() and self.card.name == name:
            self.card.avatar.show_avatar(name, self.avatar_raw(name))

    def show_profile(self, name, pos=None):
        if self.card is not None:
            self.card.close()
            self.card.deleteLater()
        self.card = ProfileCard(self, name)
        self.card.popup(pos or QCursor.pos())
        if self.server_has("profiles"):
            self.conn.send(t="get_profile", user=name)
        else:
            info = dict(self.users.get(name, {"name": name}))
            info["online"] = name in self.users
            self.card.fill(info)

    def user_color(self, name):
        """Name colour: the one they chose, otherwise one picked from their name.
        On the Light theme it's darkened so it stays readable on white."""
        c = QColor(self.colors.get(name) or name_color(name))
        if appearance()["theme"] == "Light" and c.lightness() > 140:
            c = c.darker(160)
        return c.name()

    def refresh_users(self):
        self.user_list.clear()
        # admins first, then everyone else, alphabetically
        for name in sorted(self.users, key=lambda n: (not self.users[n].get("admin"), n.lower())):
            info = self.users[name]
            it = QListWidgetItem(name)
            it.setData(Qt.ItemDataRole.UserRole, name)
            it.setData(Qt.ItemDataRole.UserRole + 1, activity_line(info))
            tips = (["Admin"] if info.get("admin") else []) + (["You"] if name == self.me else []) + \
                ([activity_line(info) + (" " + played_for(info.get("activity_since")) if info.get("activity") else "")]
                 if activity_line(info) else [])
            it.setToolTip(" · ".join(tips))
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

    def person_clicked(self, widget, it):
        """One click on someone in a list shows their profile card beside the list. It waits a
        double-click's length first, because a double-click opens a DM instead."""
        name = it.data(Qt.ItemDataRole.UserRole)
        if not name:
            return
        rect = widget.visualItemRect(it)
        left = widget.mapToGlobal(QPoint(0, rect.top()))
        right = widget.mapToGlobal(QPoint(widget.width(), rect.top()))
        self.pending_card = (name, left, right)
        self.card_timer.start(QApplication.doubleClickInterval())

    def person_double_clicked(self, it):
        self.card_timer.stop()
        self.pending_card = None
        self.open_dm(it.data(Qt.ItemDataRole.UserRole))

    def show_pending_card(self):
        if not self.pending_card:
            return
        name, left, right = self.pending_card
        self.pending_card = None
        self.show_profile(name, left)
        card = self.card
        card.adjustSize()
        # next to the list: to its left if there's room (the Online list is on the right), else its right
        screen = QApplication.screenAt(left) or QApplication.primaryScreen()
        area = screen.availableGeometry()
        x = left.x() - card.width() - 8
        if x < area.left():
            x = right.x() + 8
        y = min(max(left.y() - 20, area.top()), area.bottom() - card.height())
        card.move(x, y)

    def person_menu(self, widget, pos):
        it = widget.itemAt(pos)
        if not it:
            return
        name = it.data(Qt.ItemDataRole.UserRole) or it.text()
        if name == self.me:
            menu = QMenu(self)
            menu.addAction("View profile", lambda: self.show_profile(name, widget.mapToGlobal(pos)))
            menu.addAction("Edit profile...", self.open_settings)
            menu.exec(widget.mapToGlobal(pos))
            return
        is_friend = any(f["name"] == name for f in self.friends)
        menu = QMenu(self)
        menu.addAction("View profile", lambda: self.show_profile(name, widget.mapToGlobal(pos)))
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
            text = emoji_data.replace_shortcodes(text)
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
            self.notice("Emoji: type :name: (e.g. :fire: :joy: :thumbsup:) or use the \U0001f600 button. "
                        "Paste pictures/GIFs with Ctrl+V.")
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
        if not self.is_admin:
            self.notice("Only admins can create channels", error=True)
            return
        name, ok = QInputDialog.getText(self, "New channel", "Channel name:")
        if ok and name.strip():
            self.command(f"/join {name.strip()}")

    def add_friend_prompt(self):
        name, ok = QInputDialog.getText(self, "Add friend", "Username:")
        if ok and name.strip():
            self.conn.send(t="friend_request", to=name.strip())

    def open_settings(self):
        SettingsDialog(self).exec()

    def check_updates(self):
        if not updater.can_update():
            QMessageBox.information(self, "Updates", "This copy runs from the source code - update it with "
                                                     "git pull (or download the new files from GitHub).")
            return
        self.updater = updater.Updater(self, settings)
        self.updater.check(manual=True)

    def logout(self):
        self.leave("Logged out")

    def cancel_transfers(self):
        for job in self.jobs + self.queued + [j for j in self.preview_jobs.values() if j is not None]:
            job.cancel()
        self.queued.clear()

    def leave(self, reason):
        self.leaving = True
        self.cancel_transfers()
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
        if self.jobs and not self.leaving and QMessageBox.question(
                self, "Quit", f"{len(self.jobs)} file transfer(s) still running. Quit anyway?") \
                != QMessageBox.StandardButton.Yes:
            e.ignore()
            return
        self.cancel_transfers()
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


def set_windows_app_id(app_id):
    """Without this, Windows shows Python's icon in the taskbar instead of ours."""
    if sys.platform == "win32":
        try:
            import ctypes
            ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID(app_id)
        except Exception:
            pass


def selftest(report_path):
    """Used by the Windows build to check the packaged app has everything it needs."""
    lines, ok = [], True

    def check(name, fn):
        nonlocal ok
        try:
            fn()
            lines.append(f"ok   {name}")
        except Exception as e:                       # pragma: no cover - only fails in a broken build
            ok = False
            lines.append(f"FAIL {name}: {e!r}")

    def require(condition, problem):
        if not condition:
            raise RuntimeError(problem)

    def voice_round_trip():
        require(voice.HAVE_CODEC, "numpy/av missing")
        import numpy
        voice.Decoder().decode(voice.Encoder().encode(numpy.zeros(voice.FRAME, dtype="int16"))[0])

    app = QApplication.instance() or QApplication(sys.argv)
    check("login window", LoginWindow)
    check("icon and avatar", lambda: require(not QImage(asset("icon.png")).isNull()
                                             and not QImage(asset("avatar.png")).isNull(), "image files missing"))
    check("opus voice codec", voice_round_trip)
    check("audio devices library", lambda: require(voice.HAVE_AUDIO, "sounddevice/PortAudio missing"))
    check("vp8 video codec", lambda: video.VideoEncoder(64, 64, 15, 100_000))
    check("camera support", lambda: require(video.HAVE_CAMERA_API, "Qt Multimedia missing"))
    check("emoji", lambda: require(emoji_data.replace_shortcodes(":fire:") == "\U0001f525", "shortcodes broken"))
    check("network (GIPHY, link previews)", QNetworkAccessManager)

    def gif_avatars():
        from PIL import Image
        import tempfile
        path = os.path.join(tempfile.gettempdir(), "campfire-selftest.gif")
        frames = [Image.new("RGB", (200, 150), (i * 40, 80, 160)) for i in range(4)]
        frames[0].save(path, save_all=True, append_images=frames[1:], duration=50, loop=0)
        out = gif_avatar(path)
        require(out is not None and is_gif(out), "couldn't shrink a GIF")

    check("GIF avatars (Pillow)", gif_avatars)

    def game_detection():
        if sys.platform == "win32":
            procs, _front = games.windows_snapshot()
            require(len(procs) > 5 and any(p[1].lower().endswith(".exe") for p in procs), "couldn't list programs")
        require(isinstance(games.current_game(), str), "game check failed")

    check("game detection", game_detection)
    check(f"version {updater.VERSION}", lambda: require(updater.VERSION == "dev" or updater.parse(updater.VERSION),
                                                        "bad version number"))
    with open(report_path, "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")
    return 0 if ok else 1


def main():
    if "--selftest" in sys.argv:                     # e.g. Campfire.exe --selftest report.txt
        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
        sys.exit(selftest(sys.argv[sys.argv.index("--selftest") + 1]))
    set_windows_app_id(APP_ID)
    app = QApplication(sys.argv)
    app.setApplicationName("Campfire")
    font = app.font()                         # colour emoji fonts as fallbacks on every system
    font.setFamilies([font.family(), "Segoe UI Emoji", "Apple Color Emoji", "Noto Color Emoji"])
    app.setFont(font)
    apply_appearance(app)
    if os.path.exists(asset("icon.png")):
        app.setWindowIcon(QIcon(asset("icon.png")))
    login = LoginWindow()
    login.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
