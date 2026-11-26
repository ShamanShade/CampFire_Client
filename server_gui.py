"""
Campfire server window.

Opens when you run `python server.py` (use --nogui for the old console mode). The chat
server runs on a background thread inside this window; closing the window stops it.
"""

from __future__ import annotations

import asyncio
import datetime
import logging
import os
import shutil
import socket
import sys
import threading
import time

from PyQt6.QtCore import QObject, QProcess, QSettings, Qt, QTimer, pyqtSignal
from PyQt6.QtGui import QAction, QColor, QFont, QGuiApplication, QIcon
from PyQt6.QtWidgets import (
    QAbstractItemView, QApplication, QCheckBox, QFileDialog, QFrame, QHBoxLayout, QHeaderView, QInputDialog, QLabel, QLineEdit,
    QProgressDialog,
    QListWidget, QMainWindow, QMenu, QMessageBox, QPlainTextEdit, QPushButton, QSpinBox, QTableWidget,
    QTableWidgetItem, QTabWidget, QVBoxLayout, QWidget, QGridLayout,
)

import campfire_config
import netcheck
import server as core
import server_update
from campfire_config import SERVER_HOST
from transfers import human_size

log = logging.getLogger("campfire")
prefs = QSettings("Campfire", "Server")

STYLE = """
QWidget { background: #2b2d31; color: #dbdee1; font-size: 10pt; }
QTabWidget::pane { border: none; }
QTabBar::tab { background: #232428; padding: 8px 16px; border-top-left-radius: 6px; border-top-right-radius: 6px; margin-right: 2px; }
QTabBar::tab:selected { background: #404249; color: white; }
QTableWidget, QListWidget, QPlainTextEdit { background: #232428; border: none; border-radius: 6px; gridline-color: #2b2d31; }
QTableWidget::item:selected, QListWidget::item:selected { background: #404249; color: white; }
QHeaderView::section { background: #1e1f22; color: #949ba4; border: none; padding: 6px; font-weight: bold; }
QLineEdit, QSpinBox { background: #383a40; border: 1px solid #1e1f22; border-radius: 6px; padding: 6px; }
QPushButton { background: #5865f2; color: white; border: none; border-radius: 6px; padding: 7px 14px; }
QPushButton:hover { background: #4752c4; }
QPushButton:disabled { background: #4e5058; color: #a0a0a0; }
QPushButton[flat="true"] { background: #3a3c42; }
QPushButton[flat="true"]:hover { background: #4a4c52; }
QPushButton#danger { background: #da373c; }
QPushButton#danger:hover { background: #a12828; }
QPushButton#go { background: #248046; }
QPushButton#go:hover { background: #1a6334; }
QLabel#stat { font-size: 14pt; font-weight: bold; color: white; background: transparent; }
QLabel#statname { color: #949ba4; font-size: 8pt; font-weight: bold; background: transparent; }
QWidget#card { background: #232428; border-radius: 8px; }
QMenu { background: #111214; border: 1px solid #1e1f22; }
QMenu::item:selected { background: #5865f2; }
"""


def lan_addresses():
    ips = set()
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.connect(("10.255.255.255", 1))           # no packet is sent; just picks the LAN interface
            ips.add(s.getsockname()[0])
    except OSError:
        pass
    try:
        for info in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
            ips.add(info[4][0])
    except OSError:
        pass
    return sorted(ip for ip in ips if not ip.startswith("127."))


def fmt_uptime(sec):
    sec = int(sec)
    d, sec = divmod(sec, 86400)
    h, sec = divmod(sec, 3600)
    m, sec = divmod(sec, 60)
    return (f"{d}d " if d else "") + f"{h:02d}:{m:02d}:{sec:02d}"


# ---------------------------------------------------------------- running the server
class ServerThread(QObject):
    """Runs the asyncio chat server on its own thread. Everything that touches the server
    (including its database) goes through call(), which runs on that thread."""

    started = pyqtSignal(bool, str)
    stopped = pyqtSignal()

    def __init__(self):
        super().__init__()
        self.loop: asyncio.AbstractEventLoop | None = None
        self.server: core.Server | None = None
        self.srv = None
        self.thread = None

    @property
    def running(self):
        return self.server is not None

    def start(self, args, port):
        self.thread = threading.Thread(target=self._run, args=(args, port), daemon=True)
        self.thread.start()

    def _run(self, args, port):
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        self.loop = loop
        try:
            self.server, self.srv = loop.run_until_complete(core.start(args, port))
        except Exception as e:
            self.loop = None
            msg = str(e)
            if isinstance(e, OSError) and e.errno in (98, 10048):
                msg = f"Port {port} is already in use - is another Campfire server already running?"
            self._emit("started", False, msg)
            loop.close()
            return
        self._emit("started", True, "")
        try:
            loop.run_forever()
        finally:
            loop.close()
            self._emit("stopped")

    def _emit(self, name, *args):
        try:
            getattr(self, name).emit(*args)
        except RuntimeError:
            pass

    def call(self, fn, *args, **kw):
        """Run a server coroutine function on the server thread; returns a concurrent Future."""
        if self.loop is None:
            return None
        return asyncio.run_coroutine_threadsafe(fn(*args, **kw), self.loop)

    def stop(self, wait=True, restarting=False):
        if self.loop is None:
            return

        async def shut():
            await self.server.shutdown(restarting)
            self.srv.close()
            try:
                await asyncio.wait_for(self.srv.wait_closed(), 3)
            except asyncio.TimeoutError:
                pass

        fut = asyncio.run_coroutine_threadsafe(shut(), self.loop)
        try:
            fut.result(6)
        except Exception:
            pass
        self.loop.call_soon_threadsafe(self.loop.stop)
        if wait and self.thread:
            self.thread.join(5)
        self.loop = self.server = self.srv = None


class LogSignal(QObject):
    line = pyqtSignal(str)


class LogBridge(logging.Handler):
    """Forwards log lines (from any thread) to the window through a Qt signal."""

    def __init__(self):
        super().__init__()
        self.signal = LogSignal()
        self.line = self.signal.line
        self.setFormatter(logging.Formatter("%(asctime)s  %(message)s", "%H:%M:%S"))

    def emit(self, record):
        try:
            self.line.emit(self.format(record))
        except RuntimeError:
            pass


# ---------------------------------------------------------------- moving the storage folder
def stored_files(folder):
    """[(source path, path relative to the folder, size)] for everything the server keeps there."""
    out = []
    if not os.path.isdir(folder):
        return out
    for root, _dirs, names in os.walk(folder):
        for name in names:
            src = os.path.join(root, name)
            try:
                out.append((src, os.path.relpath(src, folder), os.path.getsize(src)))
            except OSError:
                pass
    return out


class Mover(QObject):
    """Moves the stored files to a new folder on a background thread."""

    progress = pyqtSignal(object, object)          # bytes moved, total
    done = pyqtSignal(bool, str)

    def __init__(self, src_dir, dst_dir):
        super().__init__()
        self.src_dir, self.dst_dir = src_dir, dst_dir

    def start(self):
        threading.Thread(target=self._run, daemon=True).start()

    def _run(self):
        files = stored_files(self.src_dir)
        total = sum(size for _, _, size in files)
        moved = 0
        try:
            for src, rel, size in files:
                dst = os.path.join(self.dst_dir, rel)
                os.makedirs(os.path.dirname(dst), exist_ok=True)
                shutil.move(src, dst)                   # a rename on the same drive, a copy otherwise
                moved += size
                self.progress.emit(moved, total)
        except Exception as e:
            self.done.emit(False, f"Moving stopped after {human_size(moved)}: {e}")
            return
        for root, dirs, _names in os.walk(self.src_dir, topdown=False):   # tidy up the empty old folder
            for d in dirs:
                try:
                    os.rmdir(os.path.join(root, d))
                except OSError:
                    pass
        try:
            os.rmdir(self.src_dir)
        except OSError:
            pass
        self.done.emit(True, f"Moved {len(files)} file{'s' if len(files) != 1 else ''} ({human_size(total)})")


# ---------------------------------------------------------------- window
def table(headers):
    t = QTableWidget(0, len(headers))
    t.setHorizontalHeaderLabels(headers)
    t.verticalHeader().hide()
    t.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
    t.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
    t.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
    t.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Stretch)
    t.setShowGrid(False)
    t.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
    return t


def button(text, slot, kind=None):
    b = QPushButton(text)
    if kind == "flat":
        b.setProperty("flat", True)
    elif kind:
        b.setObjectName(kind)
    b.clicked.connect(slot)
    return b


class ServerWindow(QMainWindow):
    invoke = pyqtSignal(object)          # run a callable on the GUI thread (from the server thread)

    def __init__(self, args):
        super().__init__()
        self.invoke.connect(lambda fn: fn())
        self.args = args
        # where uploads are kept: --files wins, then the folder chosen here before, then the default
        self.files_dir = os.path.abspath(args.files or prefs.value("files_dir", core.DEFAULT_FILES_DIR))
        self.st = ServerThread()
        self.st.started.connect(self.on_started)
        self.st.stopped.connect(self.on_stopped)
        self.snap: dict = {}
        self.busy = False

        self.setWindowTitle("Campfire Server")
        if os.path.exists(ICON):
            self.setWindowIcon(QIcon(ICON))
        self.resize(1000, 680)

        central = QWidget()
        self.setCentralWidget(central)
        v = QVBoxLayout(central)
        v.setContentsMargins(14, 12, 14, 12)

        # --- top: status, start/stop, port, addresses
        top = QHBoxLayout()
        self.status = QLabel()
        self.status.setStyleSheet("font-size: 13pt; font-weight: bold")
        top.addWidget(self.status)
        self.version_label = QLabel(server_update.describe())
        self.version_label.setStyleSheet("color:#949ba4; margin-left:10px")
        top.addWidget(self.version_label)
        top.addStretch()
        top.addWidget(QLabel("Port"))
        self.port = QSpinBox()
        self.port.setRange(1, 65535)
        self.port.setValue(int(prefs.value("port", args.port)) if args.port == 55555 else args.port)
        top.addWidget(self.port)
        self.start_btn = button("Start server", self.toggle, "go")
        top.addWidget(self.start_btn)
        v.addLayout(top)

        # --- "an update is available" / "restart to finish updating"
        self.update_bar = QFrame()
        self.update_bar.setObjectName("updatebar")
        self.update_bar.setStyleSheet("#updatebar { background:#3b3f8f; border-radius:6px; }")
        ub = QHBoxLayout(self.update_bar)
        ub.setContentsMargins(12, 6, 8, 6)
        self.update_text = QLabel()
        self.update_text.setWordWrap(True)
        ub.addWidget(self.update_text, 1)
        self.update_btn = button("Update server", self.do_update, "go")
        ub.addWidget(self.update_btn)
        ub.addWidget(button("Later", self.update_bar.hide, "flat"))
        self.update_bar.hide()
        v.addWidget(self.update_bar)
        self.update_base = server_update.RELEASE_BASE
        self.update_info: dict = {}
        self.code_stamp = self.code_mtime()
        self.update_timer = QTimer(self, interval=60_000, timeout=self.watch_code)
        self.update_timer.start()

        self.addr = QLabel()
        self.addr.setStyleSheet("color:#949ba4")
        self.addr.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        self.addr.setWordWrap(True)
        v.addWidget(self.addr)

        # --- stats cards
        cards = QGridLayout()
        self.stats = {}
        for i, (key, label) in enumerate([("online", "ONLINE"), ("accounts", "ACCOUNTS"), ("in_voice", "IN VOICE"),
                                          ("rooms", "TEXT CHANNELS"), ("voice_rooms", "VOICE CHANNELS"),
                                          ("groups", "GROUP DMS"), ("messages", "MESSAGES"), ("files", "FILES"),
                                          ("uptime", "UPTIME")]):
            card = QWidget()
            card.setObjectName("card")
            cl = QVBoxLayout(card)
            cl.setContentsMargins(12, 8, 12, 8)
            num = QLabel("-")
            num.setObjectName("stat")
            name = QLabel(label)
            name.setObjectName("statname")
            cl.addWidget(num)
            cl.addWidget(name)
            self.stats[key] = num
            cards.addWidget(card, 0, i)
        v.addLayout(cards)

        # --- tabs
        self.tabs = QTabWidget()
        v.addWidget(self.tabs, 1)
        self.tabs.addTab(self.build_online(), "Online")
        self.tabs.addTab(self.build_accounts(), "Accounts")
        self.tabs.addTab(self.build_channels(), "Channels")
        self.tabs.addTab(self.build_files(), "Files")
        self.tabs.addTab(self.build_bans(), "Bans")
        self.tabs.addTab(self.build_log(), "Log")
        self.tabs.addTab(self.build_internet(), "Internet")
        self.tabs.addTab(self.build_settings(), "Settings")

        # logging -> Log tab
        self.bridge = LogBridge()
        self.bridge.line.connect(self.add_log)
        root = logging.getLogger()
        root.setLevel(logging.INFO)
        root.addHandler(self.bridge)

        self.poll = QTimer(self, interval=1000, timeout=self.refresh)
        self.set_running(False)
        self.show_addresses()
        self.show_storage()
        QTimer.singleShot(0, self.toggle)                 # start straight away
        if not server_update.APP_DIR.startswith(server_update.UPDATE_DIR):
            server_update.cleanup()
        if getattr(args, "updated", False):
            log.info("Updated: %s", server_update.describe())
            self.statusBar().showMessage(f"Campfire Server updated - {server_update.describe()}", 15000)
        if getattr(args, "update_failed", False):
            QTimer.singleShot(500, lambda: QMessageBox.warning(
                self, "Update server", "The update didn't finish, so the server is still on the old version.\n\n"
                "Close Campfire (the chat app) if it's running from this folder, then try Update server again. "
                "Details are in campfire_update.log."))
        if prefs.value("update/check", True, type=bool):
            QTimer.singleShot(4000, self.check_updates)

    # --- tabs
    def build_online(self):
        w = QWidget()
        v = QVBoxLayout(w)
        self.online_table = table(["Name", "Role", "IP address", "Voice", "Video", "Muted"])
        self.online_table.customContextMenuRequested.connect(lambda p: self.user_menu(self.online_table, p))
        v.addWidget(self.online_table, 1)
        row = QHBoxLayout()
        for text, slot, kind in (("Kick", lambda: self.on_selected(self.online_table, "kick"), "flat"),
                                 ("Mute 10 min", lambda: self.on_selected(self.online_table, "mute", minutes=10), "flat"),
                                 ("Disconnect from voice", lambda: self.on_selected(self.online_table, "voicekick"), "flat"),
                                 ("Ban", lambda: self.on_selected(self.online_table, "ban"), "danger")):
            row.addWidget(button(text, slot, kind))
        row.addStretch()
        v.addLayout(row)
        return w

    def build_accounts(self):
        w = QWidget()
        v = QVBoxLayout(w)
        self.search = QLineEdit()
        self.search.setPlaceholderText("Search accounts...")
        self.search.textChanged.connect(lambda: self.fill_accounts())
        v.addWidget(self.search)
        self.acct_table = table(["Name", "Role", "Status", "Banned", "Muted", "Last IP", "Joined"])
        self.acct_table.customContextMenuRequested.connect(lambda p: self.user_menu(self.acct_table, p))
        v.addWidget(self.acct_table, 1)
        row = QHBoxLayout()
        row.addWidget(button("Make admin", lambda: self.admin_selected(True), "go"))
        row.addWidget(button("Remove admin", lambda: self.admin_selected(False), "flat"))
        row.addWidget(button("Reset password", self.reset_selected, "flat"))
        row.addWidget(button("Mute", lambda: self.on_selected(self.acct_table, "mute", minutes=0), "flat"))
        row.addWidget(button("Unmute", lambda: self.on_selected(self.acct_table, "unmute"), "flat"))
        row.addWidget(button("Unban", lambda: self.on_selected(self.acct_table, "unban"), "flat"))
        row.addWidget(button("Ban", lambda: self.on_selected(self.acct_table, "ban"), "danger"))
        row.addStretch()
        v.addLayout(row)
        return w

    def build_channels(self):
        w = QWidget()
        h = QHBoxLayout(w)
        for kind, title in (("text", "Text channels"), ("voice", "Voice channels")):
            col = QVBoxLayout()
            lbl = QLabel(title.upper())
            lbl.setStyleSheet("color:#949ba4; font-weight:bold; font-size:8.5pt")
            col.addWidget(lbl)
            lst = QListWidget()
            col.addWidget(lst, 1)
            row = QHBoxLayout()
            row.addWidget(button("Add...", lambda _=False, k=kind: self.add_channel(k), "flat"))
            if kind == "text":
                row.addWidget(button("Clear history", self.clear_channel, "flat"))
            row.addWidget(button("Delete", lambda _=False, k=kind: self.delete_channel(k), "danger"))
            row.addStretch()
            col.addLayout(row)
            h.addLayout(col)
            if kind == "text":
                self.text_list = lst
            else:
                self.voice_list = lst
        return w

    def build_files(self):
        w = QWidget()
        v = QVBoxLayout(w)
        top = QHBoxLayout()
        self.storage_label = QLabel()
        self.storage_label.setWordWrap(True)
        self.storage_label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        top.addWidget(self.storage_label, 1)
        top.addWidget(button("Change...", self.change_storage, "flat"))
        top.addWidget(button("Open folder", self.open_files_folder, "flat"))
        v.addLayout(top)
        self.files_info = QLabel()
        self.files_info.setStyleSheet("color:#949ba4")
        self.files_info.setWordWrap(True)
        v.addWidget(self.files_info)
        self.files_table = table(["File", "Size", "Sent by", "Where", "When"])
        v.addWidget(self.files_table, 1)
        row = QHBoxLayout()
        row.addWidget(button("Delete file", self.delete_file, "danger"))
        row.addStretch()
        v.addLayout(row)
        return w

    def show_storage(self):
        try:
            free = human_size(shutil.disk_usage(self.files_dir if os.path.isdir(self.files_dir)
                                                else os.path.dirname(self.files_dir) or ".").free)
        except OSError:
            free = "?"
        self.storage_label.setText(f"<b>Storage folder:</b> {self.files_dir}<br>"
                                   f"<span style='color:#949ba4'>{free} free on that drive</span>")

    def open_files_folder(self):
        from PyQt6.QtCore import QUrl
        from PyQt6.QtGui import QDesktopServices
        os.makedirs(self.files_dir, exist_ok=True)
        QDesktopServices.openUrl(QUrl.fromLocalFile(self.files_dir))

    def change_storage(self):
        new = QFileDialog.getExistingDirectory(self, "Where should uploaded files be kept?", self.files_dir)
        if not new:
            return
        new = os.path.abspath(new)
        old = self.files_dir
        if os.path.normcase(new) == os.path.normcase(old):
            return
        if os.path.normcase(new).startswith(os.path.normcase(old) + os.sep):
            QMessageBox.warning(self, "Storage folder", "Pick a folder that isn't inside the current one.")
            return
        try:
            os.makedirs(new, exist_ok=True)
            probe = os.path.join(new, ".campfire_write_test")
            open(probe, "w").close()
            os.remove(probe)
        except OSError as e:
            QMessageBox.warning(self, "Storage folder", f"Campfire can't write to that folder: {e}")
            return

        files = stored_files(old)
        total = sum(size for _, _, size in files)
        move = False
        if files:
            box = QMessageBox(self)
            box.setWindowTitle("Storage folder")
            box.setText(f"There are {len(files)} stored file(s) ({human_size(total)}) in the current folder.\n\n"
                        f"Move them to the new folder? If you don't, they can't be downloaded any more.")
            move_btn = box.addButton("Move them", QMessageBox.ButtonRole.AcceptRole)
            leave_btn = box.addButton("Leave them", QMessageBox.ButtonRole.DestructiveRole)
            box.addButton(QMessageBox.StandardButton.Cancel)
            box.exec()
            if box.clickedButton() not in (move_btn, leave_btn):
                return
            move = box.clickedButton() is move_btn
            if move and shutil.disk_usage(new).free < total + 256 * 1024 ** 2 and \
                    os.stat(new).st_dev != os.stat(old).st_dev:
                QMessageBox.warning(self, "Storage folder", f"There isn't room for {human_size(total)} on that drive.")
                return

        was_running = self.st.running
        if was_running:
            n = len(self.snap.get("online", []))
            if QMessageBox.question(self, "Storage folder",
                                    "The server restarts to switch folders" +
                                    (f" ({n} people will be disconnected for a moment and reconnect)" if n else "") +
                                    ". Continue?") != QMessageBox.StandardButton.Yes:
                return
            self.st.stop(restarting=True)
            self.set_running(False)

        def switch(ok=True, message=""):
            if ok:
                self.files_dir = new
                prefs.setValue("files_dir", new)
                log.info("Storage folder is now %s%s", new, f" ({message})" if message else "")
            else:
                log.info("Storage folder unchanged: %s", message)
                QMessageBox.warning(self, "Storage folder", message + "\n\nThe server keeps using the old folder.")
            self.show_storage()
            if was_running:
                self.toggle()

        if not move:
            switch()
            return
        dlg = QProgressDialog(f"Moving {human_size(total)} to {new}...", None, 0, 1000, self)
        dlg.setWindowTitle("Moving files")
        dlg.setMinimumDuration(0)
        dlg.setValue(0)
        self.mover = Mover(old, new)
        self.mover.progress.connect(lambda done, tot: dlg.setValue(int(1000 * done / tot) if tot else 1000))
        self.mover.done.connect(lambda ok, msg: (dlg.close(), switch(ok, msg)))
        self.mover.start()

    def delete_file(self):
        rows = self.files_table.selectionModel().selectedRows()
        if not rows or not self.st.running:
            QMessageBox.information(self, "Campfire Server", "Select a file first.")
            return
        f = self.snap["files_list"][rows[0].row()]
        if QMessageBox.question(self, "Delete file", f"Delete {f['name']} ({human_size(f['size'])})?") \
                == QMessageBox.StandardButton.Yes:
            self.run(self.st.server.console, "delfile", fid=f["id"])

    def build_bans(self):
        w = QWidget()
        v = QVBoxLayout(w)
        self.ban_list = QListWidget()
        v.addWidget(self.ban_list, 1)
        row = QHBoxLayout()
        row.addWidget(button("Unban selected", self.unban_selected, "flat"))
        self.ip_input = QLineEdit()
        self.ip_input.setPlaceholderText("IP address to ban")
        row.addWidget(self.ip_input)
        row.addWidget(button("Ban IP", self.ban_ip, "danger"))
        v.addLayout(row)
        return w

    # ---------------------------------------------------------- can friends connect?
    def build_internet(self):
        w = QWidget()
        v = QVBoxLayout(w)
        v.addWidget(QLabel("<b>Can friends connect?</b>"))
        self.net_info = QLabel()
        self.net_info.setWordWrap(True)
        self.net_info.setStyleSheet("color:#949ba4")
        self.net_info.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        v.addWidget(self.net_info)
        row = QHBoxLayout()
        self.test_btn = button("Test now", self.run_net_test, "go")
        row.addWidget(self.test_btn)
        self.upnp_btn = button("Open the port on my router", self.run_upnp, "flat")
        self.upnp_btn.setToolTip("Asks your router (UPnP) to forward the port to this PC")
        row.addWidget(self.upnp_btn)
        self.fw_btn = button("Allow through Windows Firewall", self.run_firewall, "flat")
        self.fw_btn.setVisible(sys.platform == "win32")
        row.addWidget(self.fw_btn)
        row.addStretch()
        v.addLayout(row)
        self.net_result = QLabel("Press <b>Test now</b> while the server is running. It asks an outside service "
                                 "to connect to you, exactly like a friend's Campfire does.")
        self.net_result.setWordWrap(True)
        self.net_result.setTextFormat(Qt.TextFormat.RichText)
        self.net_result.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        v.addWidget(self.net_result)
        self.net_help = QLabel()
        self.net_help.setWordWrap(True)
        self.net_help.setStyleSheet("color:#949ba4")
        self.net_help.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        v.addWidget(self.net_help)
        v.addStretch()
        self.show_net_info()
        return w

    def show_net_info(self):
        port = self.port.value()
        self.net_info.setText(
            f"Friends' Campfire connects to <b>{campfire_config.SERVER_HOST}:{campfire_config.SERVER_PORT}</b>. "
            f"This PC on your home network is <b>{netcheck.lan_ip()}</b>; the server listens on port <b>{port}</b>."
            + ("" if port == campfire_config.SERVER_PORT else
               f"<br><span style='color:#f23f42'>The app uses port {campfire_config.SERVER_PORT} but the server is "
               f"on {port} - set the port above to {campfire_config.SERVER_PORT}.</span>"))

    def background(self, work, done):
        """Run work() on a thread, then done(result or exception) on the window."""
        def run():
            try:
                result = work()
            except Exception as e:
                result = e
            self.invoke.emit(lambda: done(result))
        threading.Thread(target=run, daemon=True).start()

    def run_net_test(self):
        if not self.st.running:
            QMessageBox.information(self, "Campfire Server", "Start the server first, then test.")
            return
        port = self.port.value()
        self.test_btn.setEnabled(False)
        self.net_result.setText("Testing... (an outside service is trying to connect to you)")

        def work():
            res = netcheck.check_from_internet(port)
            try:
                res["app_ip"] = socket.gethostbyname(campfire_config.SERVER_HOST)
            except OSError:
                res["app_ip"] = None
            res["firewall"] = netcheck.firewall_status(port)
            return res

        self.background(work, self.show_net_result)

    def show_net_result(self, res):
        self.test_btn.setEnabled(True)
        ok_c, bad_c = "#23a55a", "#f23f42"
        if isinstance(res, Exception):
            self.net_result.setText(f"<span style='color:{bad_c}'>Couldn't run the test: {res}</span>")
            return
        lines = []
        port, lan = self.port.value(), netcheck.lan_ip()
        same_ip = res["app_ip"] == res["ip"]
        if res["reachable"]:
            lines.append(f"<span style='color:{ok_c}'>\u2714 Port {port} can be reached from the internet "
                         f"at {res['ip']}.</span>")
        else:
            lines.append(f"<span style='color:{bad_c}'>\u2718 Port {port} can't be reached from the internet "
                         f"(your address is {res['ip']}).</span>")
        if same_ip:
            lines.append(f"<span style='color:{ok_c}'>\u2714 Campfire connects to the right address "
                         f"({campfire_config.SERVER_HOST}).</span>")
        else:
            lines.append(f"<span style='color:{bad_c}'>\u2718 Your internet address is now <b>{res['ip']}</b>, but "
                         f"Campfire connects to <b>{campfire_config.SERVER_HOST}</b>. Your address has changed, so "
                         f"Campfire needs updating: change SERVER_HOST in campfire_config.py to {res['ip']} and push "
                         f"(or ask Claude to).</span>")
        fw = res.get("firewall")
        if fw == "blocked":
            lines.append(f"<span style='color:{bad_c}'>\u2718 Windows Firewall is blocking Campfire Server. "
                         f"Click <b>Allow through Windows Firewall</b>.</span>")
        elif fw == "missing" and not res["reachable"]:
            lines.append("Windows Firewall has no rule for Campfire yet: click <b>Allow through Windows Firewall</b>.")
        if res["reachable"] and same_ip:
            lines.append(f"<b style='color:{ok_c}'>Friends can connect.</b>")
            self.net_help.setText("")
        elif not res["reachable"]:
            lines.append("<b>Next:</b> click <b>Open the port on my router</b> (and on Windows, <b>Allow through "
                         "Windows Firewall</b>), then <b>Test now</b> again.")
            self.net_help.setText(self.manual_help())
        self.net_result.setText("<br>".join(lines))

    def manual_help(self):
        port, lan = self.port.value(), netcheck.lan_ip()
        return (f"If your router doesn't do it automatically, forward it by hand: open your router's page (usually "
                f"http://192.168.0.1 or http://192.168.1.1; the login is often on a sticker on the router), find "
                f"<i>Port forwarding</i> (sometimes under Advanced / NAT / Virtual server), and add: protocol TCP, "
                f"external port {port}, internal port {port}, internal IP {lan}. Your PC's address can change too, "
                f"so if the router lets you, reserve {lan} for this PC (DHCP reservation).")

    def run_upnp(self):
        port = self.port.value()
        self.upnp_btn.setEnabled(False)
        self.net_result.setText("Asking your router to forward the port...")

        def done(res):
            self.upnp_btn.setEnabled(True)
            if isinstance(res, Exception):
                self.net_result.setText(f"<span style='color:#f23f42'>Couldn't open the port automatically: {res}"
                                        f"</span><br>Forward it by hand (see below), then <b>Test now</b>.")
                self.net_help.setText(self.manual_help())
            else:
                log.info("router: %s", res)
                self.net_result.setText(f"<span style='color:#23a55a'>\u2714 {res}</span><br>Now press "
                                        f"<b>Test now</b>.")

        self.background(lambda: netcheck.open_port(port), done)

    def run_firewall(self):
        port = self.port.value()
        try:
            netcheck.allow_through_firewall(port)
        except Exception as e:
            QMessageBox.warning(self, "Windows Firewall", str(e))
            return
        self.net_result.setText("Approve the Windows prompt; then press <b>Test now</b>.")
        log.info("asked Windows Firewall to allow port %d", port)

    def build_settings(self):
        w = QWidget()
        v = QVBoxLayout(w)
        title = QLabel("<b>GIF search (GIPHY)</b>")
        v.addWidget(title)
        info = QLabel("Paste your GIPHY API key to turn on GIF search in everyone's emoji picker. "
                      "The key is kept on this server (not in the code) and sent to people when they log in. "
                      "Get one free at developers.giphy.com > Create an App > API.")
        info.setWordWrap(True)
        info.setStyleSheet("color:#949ba4")
        v.addWidget(info)
        row = QHBoxLayout()
        self.giphy_input = QLineEdit()
        self.giphy_input.setPlaceholderText("GIPHY API key")
        self.giphy_input.setEchoMode(QLineEdit.EchoMode.Password)
        row.addWidget(self.giphy_input, 1)
        show = QPushButton("Show")
        show.setProperty("flat", True)
        show.setCheckable(True)
        show.toggled.connect(lambda on: (self.giphy_input.setEchoMode(
            QLineEdit.EchoMode.Normal if on else QLineEdit.EchoMode.Password), show.setText("Hide" if on else "Show")))
        row.addWidget(show)
        row.addWidget(button("Save", self.save_giphy, "go"))
        row.addWidget(button("Remove", lambda: (self.giphy_input.clear(), self.save_giphy()), "flat"))
        v.addLayout(row)
        self.giphy_status = QLabel()
        v.addWidget(self.giphy_status)

        v.addSpacing(18)
        v.addWidget(QLabel("<b>Updates</b>"))
        info = QLabel("The Campfire app updates itself, but the server is separate: update it here so it "
                      "understands everything the newest app can do. People online are disconnected for a "
                      "few seconds and reconnect by themselves. Accounts, messages and files are kept.")
        info.setWordWrap(True)
        info.setStyleSheet("color:#949ba4")
        v.addWidget(info)
        row = QHBoxLayout()
        self.update_state = QLabel(server_update.describe())
        row.addWidget(self.update_state, 1)
        row.addWidget(button("Check for updates", lambda: self.check_updates(manual=True)))
        v.addLayout(row)
        self.auto_check = QCheckBox("Check for updates when the server starts")
        self.auto_check.setChecked(prefs.value("update/check", True, type=bool))
        self.auto_check.toggled.connect(lambda on: prefs.setValue("update/check", on))
        v.addWidget(self.auto_check)

        v.addSpacing(18)
        v.addWidget(QLabel("<b>Bring in an old server</b>"))
        info = QLabel("Moving to a new copy of Campfire Server (like the installer)? Pick the folder of the old "
                      "server - the one with campfire.db in it - to copy its accounts, messages, channels, "
                      "settings and uploaded files here. This server's current data is backed up first.")
        info.setWordWrap(True)
        info.setStyleSheet("color:#949ba4")
        v.addWidget(info)
        row = QHBoxLayout()
        self.data_label = QLabel(f"This server's data: {os.path.dirname(os.path.abspath(self.args.db))}")
        self.data_label.setStyleSheet("color:#949ba4")
        self.data_label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        row.addWidget(self.data_label, 1)
        row.addWidget(button("Import from an old server...", self.import_data))
        v.addLayout(row)
        v.addStretch()
        return w

    def load_settings(self):
        if not self.st.running:
            return
        fut = self.st.call(self.st.server.get_settings)

        def done(f):
            try:
                cfg = f.result()
            except Exception:
                return
            self.invoke.emit(lambda: self.show_giphy(cfg.get("giphy_key", "")))

        fut.add_done_callback(done)

    def show_giphy(self, key):
        self.giphy_input.setText(key)
        self.giphy_status.setText('<span style="color:#23a55a">GIF search is on</span>' if key
                                  else '<span style="color:#949ba4">GIF search is off - no key saved</span>')

    def save_giphy(self):
        if not self.st.running:
            QMessageBox.information(self, "Campfire Server", "Start the server first.")
            return
        key = self.giphy_input.text().strip()
        self.run(self.st.server.set_giphy_key, key,
                 after=lambda r: (self.finished(r, None), self.show_giphy(key) if r[0] else None))

    # --- bringing in an old server's data
    def import_data(self, folder=None):
        if not folder:
            folder = QFileDialog.getExistingDirectory(self, "The old server's folder (where campfire.db is)",
                                                      os.path.expanduser("~"))
        if not folder:
            return
        src = os.path.join(folder, "campfire.db")
        dst = os.path.abspath(self.args.db)
        if not os.path.isfile(src):
            QMessageBox.warning(self, "Import", f"There's no campfire.db in\n{folder}")
            return
        if os.path.abspath(src) == dst:
            QMessageBox.information(self, "Import", "That's this server's own folder.")
            return
        old_files = os.path.join(folder, "campfire_files")
        n = len(self.snap.get("online", [])) if self.st.running else 0
        if QMessageBox.question(
                self, "Import",
                f"Replace this server's accounts, messages and channels with the ones from\n{folder}?\n\n"
                f"This server's current data is saved as {os.path.basename(dst)}.before-import."
                + (f"\n\n{n} people online will be disconnected for a moment." if n else "")) \
                != QMessageBox.StandardButton.Yes:
            return
        was_running = self.st.running
        if was_running:
            self.st.stop(restarting=True)
            self.set_running(False)
        dlg = QProgressDialog("Copying the old server's data...", None, 0, 0, self)
        dlg.setWindowTitle("Import")
        dlg.setMinimumDuration(0)
        dlg.show()

        def work():
            import sqlite3
            try:
                if os.path.exists(dst):
                    with sqlite3.connect(dst) as cur_db, sqlite3.connect(dst + ".before-import") as backup:
                        cur_db.backup(backup)
                with sqlite3.connect(f"file:{src}?mode=ro", uri=True) as old_db, sqlite3.connect(dst) as new_db:
                    old_db.backup(new_db)              # a consistent copy, even of a WAL database
                copied = 0
                if os.path.isdir(old_files) and os.path.abspath(old_files) != os.path.abspath(self.files_dir):
                    for root, _dirs, files in os.walk(old_files):
                        rel = os.path.relpath(root, old_files)
                        os.makedirs(os.path.join(self.files_dir, rel), exist_ok=True)
                        for f in files:
                            shutil.copy2(os.path.join(root, f), os.path.join(self.files_dir, rel, f))
                            copied += 1
                result = (True, f"Imported from {folder}" + (f" ({copied} files)" if copied else ""))
            except Exception as e:
                result = (False, str(e))
            self.invoke.emit(lambda: done(*result))

        def done(ok, text):
            dlg.close()
            log.info("%s", text if ok else f"Import failed: {text}")
            if not ok:
                QMessageBox.warning(self, "Import", f"The import didn't work:\n\n{text}")
            else:
                QMessageBox.information(self, "Import", text + ".\n\nThe old folder wasn't changed.")
            if was_running or ok:
                self.toggle()

        threading.Thread(target=work, daemon=True).start()

    # --- updates
    def check_updates(self, manual=False):
        self.update_state.setText("Checking...")

        def work():
            res = server_update.check(self.update_base)
            self.invoke.emit(lambda: self.show_update(res, manual))

        threading.Thread(target=work, daemon=True).start()

    def show_update(self, res, manual=False):
        self.update_info = res
        if res.get("error"):
            self.update_state.setText(f"{server_update.describe()} - couldn't check: {res['error'][:120]}")
            if manual:
                QMessageBox.information(self, "Updates", f"Couldn't check for updates:\n\n{res['error']}")
            return
        if res.get("available"):
            self.update_state.setText(f"{server_update.describe()} - update available ({res['detail']})")
            self.update_text.setText(f"<b>A server update is available</b> ({res['detail']}). "
                                     "Update so the server works with the newest Campfire app.")
            self.update_btn.setText("Update server")
            self.update_bar.show()
        else:
            self.update_state.setText(f"{server_update.describe()} - up to date")
            if manual:
                QMessageBox.information(self, "Updates", "The server is up to date.")

    def code_mtime(self):
        path = sys.executable if server_update.FROZEN else os.path.abspath(core.__file__)
        try:
            return os.path.getmtime(path)
        except OSError:
            return 0

    def watch_code(self):
        """The code on disk changed (git pull by hand): offer a restart to use it."""
        if self.code_stamp and self.code_mtime() != self.code_stamp and self.update_btn.text() != "Restart now":
            self.update_text.setText("<b>New server files are installed.</b> Restart the server to start using them.")
            self.update_btn.setText("Restart now")
            self.update_bar.show()

    def relaunch_args(self):
        return [a for a in sys.argv[1:] if a not in ("--updated", "--update-failed")]

    def confirm_restart(self, what):
        n = len(self.snap.get("online", [])) if self.st.running else 0
        if not n:
            return True
        return QMessageBox.question(
            self, what, f"{n} {'person is' if n == 1 else 'people are'} online. They'll be disconnected for a few "
            "seconds and reconnect by themselves. Continue?") == QMessageBox.StandardButton.Yes

    def do_update(self):
        if self.update_btn.text() == "Restart now":
            if self.confirm_restart("Restart server"):
                self.restart(server_update.restart_command(self.relaunch_args() + ["--updated"]))
            return
        m = server_update.mode()
        if m == "none":
            QMessageBox.information(self, "Update server",
                                    "This copy can't update itself. Download the newest Campfire-Windows.zip from\n"
                                    f"https://github.com/{server_update.REPO}/releases\nand unzip it over this folder "
                                    "(your campfire.db and campfire_files are kept).")
            return
        if not self.confirm_restart("Update server"):
            return
        self.update_btn.setEnabled(False)
        dlg = QProgressDialog("Getting the newest code (git pull)..." if m == "git" else "Downloading the update...",
                              None, 0, 0 if m == "git" else 1000, self)
        dlg.setWindowTitle("Updating Campfire Server")
        dlg.setMinimumDuration(0)
        dlg.show()
        self.update_dlg = dlg

        def progress(got, total):
            if total:
                self.invoke.emit(lambda: dlg.setValue(int(1000 * got / total)))

        def work():
            try:
                if m == "git":
                    ok, out = server_update.git_pull()
                    if not ok:
                        raise RuntimeError(out)
                    cmd = server_update.restart_command(self.relaunch_args() + ["--updated"])
                elif m == "installed":
                    cmd = server_update.setup_command(server_update.download_setup(self.update_base, progress))
                else:
                    new = server_update.download(self.update_base, progress)
                    cmd = server_update.finisher_command(new, self.relaunch_args())
                self.invoke.emit(lambda: (dlg.close(), self.restart(cmd)))
            except Exception as e:
                err = str(e)
                self.invoke.emit(lambda: (dlg.close(), self.update_btn.setEnabled(True),
                                          QMessageBox.warning(self, "Update server", f"The update didn't work:\n\n{err}")))

        threading.Thread(target=work, daemon=True).start()

    def restart(self, cmd):
        """Stop (people reconnect by themselves), start cmd, and close this window."""
        log.info("Restarting: %s", " ".join(cmd))
        if self.st.running:
            self.st.stop(restarting=True)
        if not QProcess.startDetached(cmd[0], cmd[1:], server_update.APP_DIR)[0]:
            QMessageBox.warning(self, "Campfire Server", "Couldn't start the new server. Start it again by hand.")
        logging.getLogger().removeHandler(self.bridge)
        self.restarting = True
        QApplication.quit()

    def build_log(self):
        w = QWidget()
        v = QVBoxLayout(w)
        self.log_view = QPlainTextEdit()
        self.log_view.setReadOnly(True)
        self.log_view.setMaximumBlockCount(5000)
        f = QFont("Consolas")
        f.setStyleHint(QFont.StyleHint.Monospace)
        self.log_view.setFont(f)
        v.addWidget(self.log_view, 1)
        row = QHBoxLayout()
        self.announce_input = QLineEdit()
        self.announce_input.setPlaceholderText("Announcement to everyone online...")
        self.announce_input.returnPressed.connect(self.announce)
        row.addWidget(self.announce_input, 1)
        row.addWidget(button("Announce", self.announce))
        v.addLayout(row)
        return w

    # --- running
    def show_addresses(self):
        port = self.port.value()
        ips = lan_addresses()
        parts = [f"This PC: Campfire connects to 127.0.0.1:{port} by itself"]
        if ips:
            parts.append("Your network: " + ", ".join(f"{ip}:{port}" for ip in ips))
        parts.append("Friends use your public IP - forward TCP port %d on your router to %s" %
                     (port, ips[0] if ips else "this PC"))
        parts.append(f"Website (same port, no app needed): http://{SERVER_HOST}:{port}  -  "
                     f"on this PC: http://127.0.0.1:{port}")
        self.addr.setText("\n".join(parts))

    def set_running(self, running):
        self.port.setEnabled(not running)
        self.start_btn.setEnabled(True)
        self.start_btn.setText("Stop server" if running else "Start server")
        self.start_btn.setObjectName("danger" if running else "go")
        self.start_btn.style().unpolish(self.start_btn)
        self.start_btn.style().polish(self.start_btn)
        dot = "#23a55a" if running else "#80848e"
        text = f"Running on port {self.port.value()}" if running else "Stopped"
        self.status.setText(f'<span style="color:{dot}">●</span>  {text}')
        if running:
            self.poll.start()
        else:
            self.poll.stop()
            for lbl in self.stats.values():
                lbl.setText("-")

    def toggle(self):
        if self.st.running:
            if self.snap.get("online") and QMessageBox.question(
                    self, "Stop server", f"{len(self.snap['online'])} people are online. Stop the server?") \
                    != QMessageBox.StandardButton.Yes:
                return
            self.start_btn.setEnabled(False)
            self.st.stop()
            self.set_running(False)
            log.info("Server stopped")
        else:
            self.start_btn.setEnabled(False)
            self.start_btn.setText("Starting...")
            prefs.setValue("port", self.port.value())
            self.show_addresses()
            self.args.files = self.files_dir
            self.st.start(self.args, self.port.value())

    def on_started(self, ok, error):
        if ok:
            self.set_running(True)
            self.refresh()
            self.load_settings()
        else:
            self.set_running(False)
            self.add_log(f"Couldn't start: {error}")
            QMessageBox.warning(self, "Couldn't start the server", error)

    def on_stopped(self):
        pass

    # --- talking to the server thread
    def run(self, fn, *args, after=None, **kw):
        fut = self.st.call(fn, *args, **kw)
        if fut is None:
            QMessageBox.information(self, "Campfire Server", "Start the server first.")
            return

        def done(f):
            try:
                result = f.result()
            except Exception as e:                      # pragma: no cover
                result = (False, str(e))
            self.invoke.emit(lambda: self.finished(result, after))

        fut.add_done_callback(done)

    def finished(self, result, after):
        if after:
            after(result)
        elif isinstance(result, tuple):
            ok, text = result
            if text:
                self.statusBar().showMessage(text, 6000)
            if not ok and text:
                QMessageBox.warning(self, "Campfire Server", text)
        self.refresh()

    def refresh(self):
        if not self.st.running or self.busy:
            return
        self.busy = True
        fut = self.st.call(self.st.server.snapshot)
        if fut is None:
            self.busy = False
            return

        def done(f):
            try:
                snap = f.result()
            except Exception:
                snap = None
            self.invoke.emit(lambda: self.apply(snap))

        fut.add_done_callback(done)

    def apply(self, snap):
        self.busy = False
        if snap is None or not self.st.running:
            return
        self.snap = snap
        self.stats["online"].setText(str(len(snap["online"])))
        self.stats["accounts"].setText(str(snap["accounts"]))
        self.stats["in_voice"].setText(str(snap["in_voice"]))
        self.stats["rooms"].setText(str(len(snap["rooms"])))
        self.stats["voice_rooms"].setText(str(len(snap["voice_rooms"])))
        self.stats["groups"].setText(str(snap["groups"]))
        self.stats["messages"].setText(str(snap["messages"]))
        self.stats["files"].setText(str(snap["files"]))
        self.stats["files"].setToolTip(human_size(snap["file_bytes"]))
        self.files_info.setText(f"{snap['files']} file{'s' if snap['files'] != 1 else ''}, "
                                f"{human_size(snap['file_bytes'])} used  \u00b7  up to 5 GB per file")
        convs = {}
        for f in snap["files_list"]:
            c = f["conv"]
            convs[f["id"]] = c if c.startswith("#") else ("Group DM" if c.startswith("g:") else "Direct message")
        self.fill(self.files_table, [
            [f["name"], human_size(f["size"]), f["uploader"], convs[f["id"]],
             datetime.datetime.fromtimestamp(f["created"]).strftime("%Y-%m-%d %H:%M")] for f in snap["files_list"]])
        self.stats["uptime"].setText(fmt_uptime(snap["uptime"]))

        self.fill(self.online_table, [
            [u["name"], "Admin ★" if u["admin"] else "", u["ip"], u["voice"],
             u["video"], "Muted" if u["muted"] else ""] for u in snap["online"]])
        self.fill_accounts()
        for lst, items in ((self.text_list, snap["rooms"]), (self.voice_list, snap["voice_rooms"])):
            self.fill_list(lst, items)
        bans = [f"Account: {a['name']}" + (f"  -  {a['ban_reason']}" if a["ban_reason"] else "")
                for a in snap["accounts_list"] if a["banned"]]
        bans += [f"IP: {b['ip']}" + (f"  -  {b['reason']}" if b["reason"] else "") for b in snap["ip_bans"]]
        self.fill_list(self.ban_list, bans)

    def fill_accounts(self):
        if not self.snap:
            return
        q = self.search.text().strip().lower()
        rows = []
        for a in self.snap["accounts_list"]:
            if q and q not in a["name"].lower():
                continue
            joined = datetime.datetime.fromtimestamp(a["created"]).strftime("%Y-%m-%d")
            rows.append([a["name"], "Admin ★" if a["admin"] else "", "Online" if a["online"] else "Offline",
                         "Banned" if a["banned"] else "", "Muted" if a["muted"] else "", a["last_ip"], joined])
        self.fill(self.acct_table, rows)

    @staticmethod
    def fill(tbl, rows):
        selected = ServerWindow.selected_name(tbl)
        tbl.setRowCount(len(rows))
        for r, row in enumerate(rows):
            for c, val in enumerate(row):
                item = QTableWidgetItem(str(val))
                if c == 1 and str(val).startswith("Admin"):
                    item.setForeground(QColor("#f0b232"))
                if val in ("Online",):
                    item.setForeground(QColor("#23a55a"))
                if val in ("Banned", "Muted"):
                    item.setForeground(QColor("#f23f42"))
                tbl.setItem(r, c, item)
            if row[0] == selected:
                tbl.selectRow(r)

    @staticmethod
    def fill_list(lst, items):
        current = lst.currentItem().text() if lst.currentItem() else None
        if [lst.item(i).text() for i in range(lst.count())] == items:
            return
        lst.clear()
        lst.addItems(items)
        for i in range(lst.count()):
            if lst.item(i).text() == current:
                lst.setCurrentRow(i)

    @staticmethod
    def selected_name(tbl):
        rows = tbl.selectionModel().selectedRows() if tbl.selectionModel() else []
        return tbl.item(rows[0].row(), 0).text() if rows and tbl.item(rows[0].row(), 0) else None

    # --- actions
    def need_selection(self, tbl):
        name = self.selected_name(tbl)
        if not name:
            QMessageBox.information(self, "Campfire Server", "Select someone in the list first.")
        return name

    def on_selected(self, tbl, cmd, **kw):
        name = self.need_selection(tbl)
        if not name:
            return
        if cmd in ("kick", "ban", "mute"):
            label = {"kick": "Kick", "ban": "Ban", "mute": "Mute"}[cmd]
            reason, ok = QInputDialog.getText(self, f"{label} {name}", "Reason (optional, they'll see it):")
            if not ok:
                return
            kw["reason"] = reason
            if cmd == "ban" and QMessageBox.question(self, "Ban", f"Ban {name}?") != QMessageBox.StandardButton.Yes:
                return
        self.run(self.st.server.console, cmd, user=name, **kw) if self.st.running else None

    def admin_selected(self, on):
        name = self.need_selection(self.acct_table)
        if name and self.st.running:
            self.run(self.st.server.set_admin, name, on)

    def reset_selected(self):
        name = self.need_selection(self.acct_table)
        if not name or not self.st.running:
            return
        pw, ok = QInputDialog.getText(self, f"Reset password for {name}", "New password (tell them what it is):")
        if ok and pw:
            self.run(self.st.server.console, "resetpass", user=name, password=pw)

    def user_menu(self, tbl, pos):
        item = tbl.itemAt(pos)
        if not item:
            return
        tbl.selectRow(item.row())
        name = tbl.item(item.row(), 0).text()
        m = QMenu(self)
        m.addAction("Kick", lambda: self.on_selected(tbl, "kick"))
        mute = m.addMenu("Mute")
        for label, minutes in (("10 minutes", 10), ("1 hour", 60), ("1 day", 1440), ("Until unmuted", 0)):
            mute.addAction(label, lambda _=False, mn=minutes: self.on_selected(tbl, "mute", minutes=mn))
        m.addAction("Unmute", lambda: self.on_selected(tbl, "unmute"))
        m.addAction("Disconnect from voice", lambda: self.on_selected(tbl, "voicekick"))
        m.addSeparator()
        m.addAction("Make admin", lambda: self.run(self.st.server.set_admin, name, True))
        m.addAction("Remove admin", lambda: self.run(self.st.server.set_admin, name, False))
        m.addAction("Reset password...", lambda: (tbl.selectRow(item.row()), self.reset_selected_from(tbl)))
        m.addSeparator()
        m.addAction("Ban...", lambda: self.on_selected(tbl, "ban"))
        m.addAction("Ban IP address...", lambda: self.on_selected(tbl, "ipban"))
        m.addAction("Unban", lambda: self.on_selected(tbl, "unban"))
        m.exec(tbl.viewport().mapToGlobal(pos))

    def reset_selected_from(self, tbl):
        name = self.selected_name(tbl)
        if not name or not self.st.running:
            return
        pw, ok = QInputDialog.getText(self, f"Reset password for {name}", "New password (tell them what it is):")
        if ok and pw:
            self.run(self.st.server.console, "resetpass", user=name, password=pw)

    def add_channel(self, kind):
        name, ok = QInputDialog.getText(self, "New channel", "Channel name:")
        if ok and name.strip() and self.st.running:
            self.run(self.st.server.add_room if kind == "text" else self.st.server.add_voice_room, name.strip())

    def delete_channel(self, kind):
        lst = self.text_list if kind == "text" else self.voice_list
        item = lst.currentItem()
        if not item or not self.st.running:
            return
        name = item.text()
        if QMessageBox.question(self, "Delete channel", f"Delete {name}?") == QMessageBox.StandardButton.Yes:
            self.run(self.st.server.console, "delroom" if kind == "text" else "delvoice", room=name)

    def clear_channel(self):
        item = self.text_list.currentItem()
        if item and self.st.running and QMessageBox.question(
                self, "Clear history", f"Delete every message in {item.text()}?") == QMessageBox.StandardButton.Yes:
            self.run(self.st.server.console, "clearhistory", room=item.text())

    def unban_selected(self):
        item = self.ban_list.currentItem()
        if not item or not self.st.running:
            return
        text = item.text()
        kind, _, rest = text.partition(": ")
        target = rest.split("  -  ")[0]
        self.run(self.st.server.console, "unban" if kind == "Account" else "unipban", user=target)

    def ban_ip(self):
        ip = self.ip_input.text().strip()
        if ip and self.st.running:
            self.run(self.st.server.console, "ipban", user=ip,
                     after=lambda r: (self.ip_input.clear() if r[0] else None, self.finished(r, None)))

    def announce(self):
        text = self.announce_input.text().strip()
        if text and self.st.running:
            self.run(self.st.server.console, "announce", text=text,
                     after=lambda r: self.announce_input.clear())

    def add_log(self, line):
        self.log_view.appendPlainText(line)

    def closeEvent(self, ev):
        if self.st.running:
            n = len(self.snap.get("online", []))
            if n and QMessageBox.question(self, "Quit", f"{n} people are online. Stop the server and quit?") \
                    != QMessageBox.StandardButton.Yes:
                ev.ignore()
                return
            self.st.stop()
        logging.getLogger().removeHandler(self.bridge)
        super().closeEvent(ev)


ICON = os.path.join(os.path.dirname(os.path.abspath(__file__)), "icon.png")


def run(args):
    if sys.platform == "win32":                    # our icon in the taskbar, not Python's
        try:
            import ctypes
            ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID("Campfire.Server")
        except Exception:
            pass
    app = QApplication.instance() or QApplication(sys.argv)
    app.setApplicationName("Campfire Server")
    if os.path.exists(ICON):
        app.setWindowIcon(QIcon(ICON))
    app.setStyleSheet(STYLE)
    w = ServerWindow(args)
    w.show()
    sys.exit(app.exec())
