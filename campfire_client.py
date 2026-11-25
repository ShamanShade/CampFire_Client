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
# ---- The one server this client talks to. Change these if your server moves.
SERVER_HOST = "74.132.179.199"
SERVER_PORT = 55555
USE_TLS = False              # set True once the server runs with --certfile/--keyfile
TLS_CERT = "cert.pem"        # the server's certificate, shipped next to this file
ADMIN_COMMANDS = {"kick", "ban", "unban", "ipban", "unipban", "mute", "unmute", "announce",
                  "clearhistory", "delroom", "resetpass", "bans"}
MAX_LINE = 1024 * 1024
URL_RE = re.compile(r"(https?://[^\s<]+)")
ADMIN_COLOR = "#f0b232"
ADMIN_BADGE = "\u2605"          # ★
GROUP_MAX = 50              # must match the server
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
        self.status.setText(text)

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
        conn.start()

    def on_failed(self, reason):
        self.conn = None
        self.set_busy(False, f"Couldn't connect: {reason}")

    def on_disconnected(self, reason):
        self.conn = None
        self.set_busy(False, f"Disconnected: {reason}")

    def on_message(self, msg):
        kind = msg.get("t")
        if kind == "kicked":                      # e.g. this address is banned
            self.conn.closing = True
            self.conn.close()
            self.conn = None
            self.set_busy(False, msg.get("reason", "Refused by server"))
        elif kind == "hello":
            self.conn.send(t="login", user=self.user_input.text().strip(), **{"pass": self.pass_input.text()})
        elif kind == "login_fail":
            self.set_busy(False, msg.get("reason", "Login failed"))
            self.pass_input.selectAll()
            self.pass_input.setFocus()
        elif kind == "login_ok":
            settings.setValue("username", msg["user"])
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


# ====================================================================== chat
class ChatWindow(QMainWindow):
    logged_out = pyqtSignal(str)

    def __init__(self, conn: Connection, password: str, login: dict):
        super().__init__()
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
        rows = [(f"g:{gid}", g["name"], group_icon(g["name"]), f"{len(g['members'])} members") for gid, g in groups]
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
                        "/group name <new name> · /group remove user · /group leave · /group members")
            if self.is_admin:
                self.notice("Admin: /kick user [reason] · /ban user [reason] · /unban user · "
                            "/ipban user-or-ip [reason] · /unipban user-or-ip · /mute user [minutes] [reason] · "
                            "/unmute user · /announce text · /clearhistory [#room] · /delroom [#room] · "
                            "/resetpass user newpass · /bans")

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
