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
import os
import re
import socket
import ssl
import sys
import threading
import time

from PyQt6.QtCore import QBuffer, QByteArray, QIODevice, QObject, QSettings, Qt, QTimer, pyqtSignal
from PyQt6.QtGui import QAction, QColor, QFont, QIcon, QImage, QPainter, QPixmap
from PyQt6.QtWidgets import (
    QApplication, QCheckBox, QDialog, QDialogButtonBox, QFileDialog, QFormLayout, QHBoxLayout,
    QInputDialog, QLabel, QLineEdit, QListWidget, QListWidgetItem, QMainWindow, QMenu,
    QMessageBox, QPushButton, QSplitter, QTextBrowser, QVBoxLayout, QWidget,
)

APP_DIR = os.path.dirname(os.path.abspath(__file__))
DEFAULT_SERVER = "74.132.179.199:55555"
DEFAULT_PORT = 55555
MAX_LINE = 1024 * 1024
URL_RE = re.compile(r"(https?://[^\s<]+)")

settings = QSettings("Campfire", "Campfire")


def asset(name: str) -> str:
    return os.path.join(APP_DIR, name)


def parse_server(text: str) -> tuple[str, int]:
    text = text.strip() or DEFAULT_SERVER
    if text.startswith("[") and "]" in text:                   # [ipv6]:port
        host, _, rest = text[1:].partition("]")
        return host, int(rest.lstrip(":") or DEFAULT_PORT)
    if text.count(":") == 1:
        host, port = text.split(":")
        return host, int(port)
    return text, DEFAULT_PORT


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

    def start(self):
        threading.Thread(target=self._run, daemon=True).start()

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
            self.failed.emit(friendly_error(e))
            return
        self.connected.emit()

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
                    self.received.emit(msg)
        except Exception as e:
            reason = friendly_error(e)
        if not self.closing:
            self.disconnected.emit(reason)

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


def heading(text):
    lbl = QLabel(text.upper())
    lbl.setObjectName("heading")
    return lbl


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

        self.server_input = QLineEdit(settings.value("server", DEFAULT_SERVER))
        self.server_input.setPlaceholderText("Server (host:port)")
        self.user_input = QLineEdit(settings.value("username", ""))
        self.user_input.setPlaceholderText("Username")
        self.pass_input = QLineEdit()
        self.pass_input.setPlaceholderText("Password")
        self.pass_input.setEchoMode(QLineEdit.EchoMode.Password)
        self.tls_box = QCheckBox("Use TLS (encrypted)")
        self.tls_box.setChecked(settings.value("tls", False, type=bool))

        for w in (self.server_input, self.user_input, self.pass_input):
            w.returnPressed.connect(self.login)
            layout.addWidget(w)
        layout.addWidget(self.tls_box)

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
        self.status.setText(text)

    def login(self):
        user, pw = self.user_input.text().strip(), self.pass_input.text()
        if not user or not pw:
            self.status.setText("Enter a username and password")
            return
        try:
            host, port = parse_server(self.server_input.text())
        except ValueError:
            self.status.setText("Server should look like host:port")
            return
        self.set_busy(True)

        if self.conn:                       # already connected (e.g. retry after wrong password)
            self.conn.send(t="login", user=user, **{"pass": pw})
            return

        conn = Connection(host, port, self.tls_box.isChecked(), settings.value("tls_ca", "") or None)
        conn.failed.connect(self.on_failed)
        conn.connected.connect(lambda: None)
        conn.received.connect(self.on_message)
        conn.disconnected.connect(self.on_disconnected)
        self.conn = conn
        conn.start()

    def on_failed(self, reason):
        self.conn = None
        self.set_busy(False, f"Couldn't connect: {reason}")

    def on_disconnected(self, reason):
        self.conn = None
        self.set_busy(False, f"Disconnected: {reason}")

    def on_message(self, msg):
        kind = msg.get("t")
        if kind == "hello":
            self.conn.send(t="login", user=self.user_input.text().strip(), **{"pass": self.pass_input.text()})
        elif kind == "login_fail":
            self.set_busy(False, msg.get("reason", "Login failed"))
            self.pass_input.selectAll()
            self.pass_input.setFocus()
        elif kind == "login_ok":
            settings.setValue("server", self.server_input.text().strip())
            settings.setValue("username", msg["user"])
            settings.setValue("tls", self.tls_box.isChecked())
            conn, self.conn = self.conn, None
            for sig in (conn.failed, conn.received, conn.disconnected):
                sig.disconnect()
            self.chat = ChatWindow(conn, self.pass_input.text(), msg)
            self.chat.logged_out.connect(self.back_to_login)
            self.chat.show()
            self.pass_input.clear()
            self.hide()
            self.set_busy(False)

    def back_to_login(self, reason: str):
        self.chat = None
        self.status.setText(reason)
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

        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Save | QDialogButtonBox.StandardButton.Cancel)
        buttons.accepted.connect(self.save)
        buttons.rejected.connect(self.reject)
        form.addRow(buttons)

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
        self.accept()


# ====================================================================== chat
class ChatWindow(QMainWindow):
    logged_out = pyqtSignal(str)

    def __init__(self, conn: Connection, password: str, login: dict):
        super().__init__()
        self.conn = conn
        self.password = password
        self.me = login["user"]
        self.rooms: list[str] = []
        self.users: dict[str, str] = {}             # online name -> avatar data
        self.icons: dict[str, QIcon] = {}
        self.friends: list[dict] = []
        self.convs: dict[str, list[dict]] = {}       # "#room" or "@user" -> messages
        self.loaded: set[str] = set()
        self.unread: dict[str, int] = {}
        self.current = "#general"
        self.reconnect_delay = 1
        self.leaving = False
        self.pending_room: str | None = None

        self.setWindowTitle(f"Campfire - {self.me}")
        self.resize(1150, 700)
        if os.path.exists(asset("icon.png")):
            self.setWindowIcon(QIcon(asset("icon.png")))
        self.build_ui()
        self.build_menu()
        self.attach(conn)
        self.apply_login(login)

        self.ping_timer = QTimer(self, interval=30_000, timeout=lambda: self.conn.send(t="ping"))
        self.ping_timer.start()

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
        add = QPushButton("+")
        add.setProperty("flat", True)
        add.setFixedSize(28, 24)
        add.setStyleSheet("padding: 0; font-size: 12pt; font-weight: bold;")
        add.setToolTip("Create a channel")
        add.clicked.connect(self.create_room)
        top.addWidget(add)
        lv.addLayout(top)
        self.room_list = QListWidget()
        self.room_list.itemClicked.connect(lambda it: self.open_conv(it.data(Qt.ItemDataRole.UserRole)))
        lv.addWidget(self.room_list, 2)
        lv.addWidget(heading("Direct messages"))
        self.dm_list = QListWidget()
        self.dm_list.itemClicked.connect(lambda it: self.open_conv(it.data(Qt.ItemDataRole.UserRole)))
        lv.addWidget(self.dm_list, 3)

        # center: chat
        center = QWidget()
        cv = QVBoxLayout(center)
        cv.setContentsMargins(4, 8, 4, 8)
        self.title = QLabel()
        self.title.setObjectName("title")
        cv.addWidget(self.title)
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
        self.online_heading = heading("Online")
        rv.addWidget(self.online_heading)
        self.user_list = QListWidget()
        self.user_list.itemDoubleClicked.connect(lambda it: self.open_dm(it.text()))
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
        split.addWidget(center)
        split.addWidget(right)
        split.setSizes([220, 700, 230])
        split.setStretchFactor(1, 1)
        self.input.setFocus()

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

    # ---------------------------------------------------------- connection
    def attach(self, conn: Connection):
        self.conn = conn
        conn.received.connect(self.on_message)
        conn.disconnected.connect(self.on_disconnected)
        conn.failed.connect(self.on_disconnected)

    def apply_login(self, login: dict):
        self.me = login["user"]
        self.setWindowTitle(f"Campfire - {self.me}")
        self.reconnect_delay = 1
        self.statusBar().showMessage("Connected", 3000)
        self.users = {u["name"]: u["avatar"] for u in login["users"]}
        self.icons.clear()
        self.set_rooms(login["rooms"])
        self.refresh_users()
        # anything we had cached may be stale after a reconnect
        self.loaded.clear()
        if self.current not in self.convs and self.current.startswith("#") and self.current not in self.rooms:
            self.current = "#general"
        self.open_conv(self.current)
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
            self.add_message("@" + other, {"user": msg["from"], "text": msg["text"], "ts": msg["ts"]})
            if msg["from"] != self.me and not self.isActiveWindow():
                QApplication.alert(self)
        elif kind == "user_join":
            self.users[msg["name"]] = msg["avatar"]
            self.refresh_users()
        elif kind == "user_leave":
            self.users.pop(msg["name"], None)
            self.refresh_users()
        elif kind == "user_update":
            self.user_updated(msg)
        elif kind == "renamed":
            self.me = msg["name"]
            self.setWindowTitle(f"Campfire - {self.me}")
            settings.setValue("username", self.me)
            self.notice(f"You are now {self.me}")
        elif kind == "friends":
            self.set_friends(msg["friends"], msg["requests"])
        elif kind == "notice":
            self.notice(msg["msg"])
        elif kind == "error":
            self.notice(msg["msg"], error=True)

    def user_updated(self, msg):
        old, new = msg["old"], msg["name"]
        self.users.pop(old, None)
        self.users[new] = msg["avatar"]
        self.icons.pop(old, None)
        self.icons.pop(new, None)
        if old != new and "@" + old in self.convs:
            self.convs["@" + new] = self.convs.pop("@" + old)
            if self.current == "@" + old:
                self.current = "@" + new
            self.refresh_dms()
        self.refresh_users()
        self.refresh_friends()

    # ---------------------------------------------------------- conversations
    def set_rooms(self, rooms):
        self.rooms = rooms
        for r in rooms:
            self.convs.setdefault(r, [])
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
        self.current = key
        self.unread.pop(key, None)
        self.convs.setdefault(key, [])
        if key not in self.loaded:
            if key.startswith("#"):
                self.conn.send(t="history", room=key)
            else:
                self.conn.send(t="dm_history", **{"with": key[1:]})
        self.title.setText(key if key.startswith("#") else f"@ {key[1:]}")
        self.refresh_rooms()
        self.refresh_dms()
        self.redraw()

    def load_history(self, key, messages):
        live = [m for m in self.convs.get(key, []) if m.get("live")]
        seen = {(m["user"], m["text"], m["ts"]) for m in messages}
        self.convs[key] = messages + [m for m in live if (m["user"], m["text"], m["ts"]) not in seen]
        self.loaded.add(key)
        if key == self.current:
            self.redraw()

    def add_message(self, key, msg):
        msg = {"user": msg["user"], "text": msg["text"], "ts": msg["ts"], "live": True}
        self.convs.setdefault(key, []).append(msg)
        if key == self.current:
            self.view.append(self.format(msg))
            self.scroll_down()
        elif msg["user"] != self.me:
            self.unread[key] = self.unread.get(key, 0) + 1
            self.refresh_rooms() if key.startswith("#") else self.refresh_dms()

    def notice(self, text, error=False):
        color = "#f23f42" if error else "#949ba4"
        self.view.append(f'<div style="color:{color}"><i>{html.escape(text)}</i></div>')
        self.scroll_down()

    def format(self, m):
        when = time.strftime("%H:%M", time.localtime(m["ts"]))
        color = name_color(m["user"])
        who = f'<b style="color:{color}">{html.escape(m["user"])}</b>'
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
        for key in sorted((k for k in self.convs if k.startswith("@")), key=str.lower):
            name = key[1:]
            it = QListWidgetItem(self.icon_for(name), self.label(key, name))
            it.setData(Qt.ItemDataRole.UserRole, key)
            if self.unread.get(key):
                f = it.font(); f.setBold(True); it.setFont(f)
            self.dm_list.addItem(it)
            if key == self.current:
                it.setSelected(True)

    def icon_for(self, name):
        if name not in self.icons:
            self.icons[name] = avatar_icon(name, self.users.get(name, ""))
        return self.icons[name]

    def refresh_users(self):
        self.user_list.clear()
        for name in sorted(self.users, key=str.lower):
            it = QListWidgetItem(self.icon_for(name), name)
            if name == self.me:
                it.setToolTip("You")
            self.user_list.addItem(it)
        self.online_heading.setText(f"ONLINE - {len(self.users)}")
        self.refresh_dms()

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
        menu.exec(widget.mapToGlobal(pos))

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
            ok = (self.conn.send(t="msg", room=self.current, text=text) if self.current.startswith("#")
                  else self.conn.send(t="dm", to=self.current[1:], text=text))
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
        elif cmd == "clear":
            self.view.clear()
        else:
            self.notice("Commands: /join #room · /dm <user> [message] · /friend <user> · "
                        "/nick <new name> · /clear · start with // to send a literal /")

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
        self.ping_timer.stop()
        self.conn.close()
        self.close()
        self.logged_out.emit(reason)

    def closeEvent(self, e):
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
