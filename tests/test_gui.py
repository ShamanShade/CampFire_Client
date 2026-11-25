"""Headless GUI test: two real client windows talking through a real server.
Run:  QT_QPA_PLATFORM=offscreen python tests/test_gui.py [screenshot-dir]"""

import os
import socket
import subprocess
import sys
import tempfile
import time

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from PyQt6.QtCore import QSettings  # noqa: E402
from PyQt6.QtWidgets import QApplication  # noqa: E402

QSettings.setDefaultFormat(QSettings.Format.IniFormat)
TMP = tempfile.mkdtemp()
QSettings.setPath(QSettings.Format.IniFormat, QSettings.Scope.UserScope, TMP)

import campfire_client as cc  # noqa: E402


def free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def start_server(port, db):
    p = subprocess.Popen([sys.executable, os.path.join(ROOT, "server.py"), "--host", "127.0.0.1",
                          "--port", str(port), "--db", db], stdout=subprocess.DEVNULL, stderr=subprocess.STDOUT)
    for _ in range(50):
        try:
            socket.create_connection(("127.0.0.1", port), 0.2).close()
            return p
        except OSError:
            time.sleep(0.1)
    raise RuntimeError("server didn't start")


def pump(cond, timeout=6):
    end = time.time() + timeout
    while time.time() < end:
        QApplication.processEvents()
        if cond():
            return True
        time.sleep(0.01)
    raise AssertionError("timed out waiting")


def login(port, user, pw="secret"):
    w = cc.LoginWindow()
    w.server_input.setText(f"127.0.0.1:{port}")
    w.user_input.setText(user)
    w.pass_input.setText(pw)
    w.show()
    w.login()
    pump(lambda: w.chat is not None or (w.status.text() and w.btn.isEnabled()))
    return w


def main(shots=None):
    app = QApplication(sys.argv)
    app.setStyleSheet(cc.STYLE)
    port = free_port()
    db = os.path.join(TMP, "gui.db")
    server = start_server(port, db)
    try:
        # bad connection address gives a clear error and the button comes back
        bad = cc.LoginWindow()
        bad.server_input.setText("127.0.0.1:1")
        bad.user_input.setText("x"); bad.pass_input.setText("yyyy")
        bad.login()
        pump(lambda: bad.btn.isEnabled())
        assert "not running" in bad.status.text(), bad.status.text()

        la = login(port, "alice")
        a = la.chat
        lb = login(port, "bob")
        b = lb.chat
        # wrong password stays on login screen, and retry on the same connection works
        lx = login(port, "alice", "wrong")
        assert lx.chat is None and "Wrong password" in lx.status.text()

        pump(lambda: "bob" in a.users)
        b.input.setText("hello <script>x</script> https://example.com")
        b.send_input()
        pump(lambda: any(m["user"] == "bob" for m in a.convs["#general"]))
        page = a.view.toHtml()
        assert "&lt;script&gt;" in page and 'href="https://example.com"' in page

        # unread counter in another room
        b.input.setText("/join #music"); b.send_input()
        pump(lambda: "#music" in a.rooms and b.current == "#music")
        b.input.setText("anyone here?"); b.send_input()
        pump(lambda: a.unread.get("#music") == 1)
        a.open_conv("#music")
        pump(lambda: "#music" in a.loaded and "anyone here?" in a.view.toPlainText())

        # DMs + friend requests through the UI
        b.open_dm("alice")
        b.input.setText("secret plans"); b.send_input()
        pump(lambda: a.convs.get("@bob"))
        b.conn.send(t="friend_request", to="alice")
        pump(lambda: a.req_list.count() == 1 and a.accept_btn.isEnabled())
        a.respond_request(True)
        pump(lambda: b.friends == [{"name": "alice", "online": True}])

        # rename shows up for the other side, DM conversation follows
        b.conn.send(t="rename", name="bobby")
        pump(lambda: "bobby" in a.users and "@bobby" in a.convs and b.me == "bobby")

        a.open_conv("#general")
        if shots:
            a.resize(1150, 700)
            a.grab().save(os.path.join(shots, "chat.png"))
            lx.grab().save(os.path.join(shots, "login.png"))

        # server restart: clients reconnect and log back in by themselves
        server.terminate(); server.wait()
        pump(lambda: "reconnecting" in a.statusBar().currentMessage())
        server = start_server(port, db)
        pump(lambda: a.statusBar().currentMessage() == "Connected" and "bobby" in a.users, timeout=15)
        a.input.setText("we're back"); a.send_input()
        pump(lambda: any(m["text"] == "we're back" for m in b.convs["#general"]))

        # logging in elsewhere kicks the old window back to the login screen
        lc = login(port, "alice")
        pump(lambda: la.isVisible() and "another location" in la.status.text())
        assert lc.chat is not None
        print("gui tests passed")
    finally:
        server.terminate()


if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else None)
