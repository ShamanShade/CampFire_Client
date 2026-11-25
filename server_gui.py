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
import socket
import sys
import threading
import time

from PyQt6.QtCore import QObject, QSettings, Qt, QTimer, pyqtSignal
from PyQt6.QtGui import QAction, QColor, QFont, QGuiApplication, QIcon
from PyQt6.QtWidgets import (
    QAbstractItemView, QApplication, QHBoxLayout, QHeaderView, QInputDialog, QLabel, QLineEdit,
    QListWidget, QMainWindow, QMenu, QMessageBox, QPlainTextEdit, QPushButton, QSpinBox, QTableWidget,
    QTableWidgetItem, QTabWidget, QVBoxLayout, QWidget, QGridLayout,
)

import server as core
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
QLabel#stat { font-size: 16pt; font-weight: bold; color: white; background: transparent; }
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

    def stop(self, wait=True):
        if self.loop is None:
            return

        async def shut():
            await self.server.shutdown()
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
        top.addStretch()
        top.addWidget(QLabel("Port"))
        self.port = QSpinBox()
        self.port.setRange(1, 65535)
        self.port.setValue(int(prefs.value("port", args.port)) if args.port == 55555 else args.port)
        top.addWidget(self.port)
        self.start_btn = button("Start server", self.toggle, "go")
        top.addWidget(self.start_btn)
        v.addLayout(top)
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

        # logging -> Log tab
        self.bridge = LogBridge()
        self.bridge.line.connect(self.add_log)
        root = logging.getLogger()
        root.setLevel(logging.INFO)
        root.addHandler(self.bridge)

        self.poll = QTimer(self, interval=1000, timeout=self.refresh)
        self.set_running(False)
        self.show_addresses()
        QTimer.singleShot(0, self.toggle)                 # start straight away

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
        self.files_info = QLabel()
        self.files_info.setStyleSheet("color:#949ba4")
        self.files_info.setWordWrap(True)
        v.addWidget(self.files_info)
        self.files_table = table(["File", "Size", "Sent by", "Where", "When"])
        v.addWidget(self.files_table, 1)
        row = QHBoxLayout()
        row.addWidget(button("Open folder", self.open_files_folder, "flat"))
        row.addWidget(button("Delete file", self.delete_file, "danger"))
        row.addStretch()
        v.addLayout(row)
        return w

    def open_files_folder(self):
        from PyQt6.QtCore import QUrl
        from PyQt6.QtGui import QDesktopServices
        if self.snap.get("files_dir"):
            QDesktopServices.openUrl(QUrl.fromLocalFile(self.snap["files_dir"]))

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
        parts = [f"This PC: 127.0.0.1:{port} (campfire_admin.py)"]
        if ips:
            parts.append("Your network: " + ", ".join(f"{ip}:{port}" for ip in ips))
        parts.append("Friends use your public IP - forward TCP port %d on your router to %s" %
                     (port, ips[0] if ips else "this PC"))
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
            self.st.start(self.args, self.port.value())

    def on_started(self, ok, error):
        if ok:
            self.set_running(True)
            self.refresh()
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
        self.stats["files"].setText(f"{snap['files']}  ({human_size(snap['file_bytes'])})" if snap["files"] else "0")
        self.files_info.setText(f"Uploaded files are kept in {snap['files_dir']}  \u00b7  "
                                f"{human_size(snap['file_bytes'])} used, {human_size(snap['disk_free'])} free on that disk  "
                                f"\u00b7  up to 5 GB per file")
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
                if c == 1 and val:
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
