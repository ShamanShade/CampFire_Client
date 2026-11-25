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
                          "--port", str(port), "--db", db, "--admin", "alice"], stdout=subprocess.DEVNULL, stderr=subprocess.STDOUT)
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
    cc.SERVER_HOST, cc.SERVER_PORT = "127.0.0.1", port      # the client is locked to one server
    w = cc.LoginWindow()
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
        cc.SERVER_HOST, cc.SERVER_PORT = "127.0.0.1", 1
        bad = cc.LoginWindow()
        assert not hasattr(bad, "server_input")                # no way to pick another server
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

        # group DMs: alice makes a group with bob through the dialog, both chat in it
        lc0 = login(port, "carl")
        pump(lambda: "carl" in a.users)
        dlg = cc.GroupDialog(a, "New group DM", a.people_i_know(), cc.GROUP_MAX - 1, ask_name=True)
        assert {"bob", "carl"} <= set(dlg.list.item(i).text() for i in range(dlg.list.count()))
        assert not dlg.buttons.button(cc.QDialogButtonBox.StandardButton.Ok).isEnabled()
        dlg.add_person("bob", checked=True)
        dlg.update_count()
        assert dlg.picked() == ["bob"]
        a.conn.send(t="group_create", name="Weekend plans", members=dlg.picked())
        pump(lambda: a.current.startswith("g:") and len(b.groups) == 1)
        gid = next(iter(b.groups))
        assert a.members_box.isVisible() and a.member_list.count() == 2
        assert "Weekend plans" in a.title.text() and "2 members" in a.title.text()
        b.open_conv(f"g:{gid}")
        b.input.setText("pizza saturday?"); b.send_input()
        pump(lambda: any(m["text"] == "pizza saturday?" for m in a.convs[f"g:{gid}"]))
        assert not any(m.get("text") == "pizza saturday?" for msgs in lc0.chat.convs.values() for m in msgs)
        a.input.setText("/group add carl"); a.send_input()
        pump(lambda: gid in lc0.chat.groups and a.member_list.count() == 3)
        pump(lambda: "alice added carl" in a.view.toPlainText())
        # carl sees the history from before he joined
        c0 = lc0.chat
        c0.open_conv(f"g:{gid}")
        pump(lambda: "pizza saturday?" in c0.view.toPlainText())
        if shots:
            a.resize(1150, 700)
            a.grab().save(os.path.join(shots, "group.png"))
        c0.input.setText("/group leave"); c0.send_input()
        pump(lambda: gid not in c0.groups and c0.current == "#general" and a.member_list.count() == 2)
        c0.leave("bye")
        a.open_conv("#general")

        # rename shows up for the other side, DM conversation follows
        b.conn.send(t="rename", name="bobby")
        pump(lambda: "bobby" in a.users and "@bobby" in a.convs and b.me == "bobby")

        # ---- admin tools (alice is admin, started with --admin alice)
        assert a.is_admin and not b.is_admin and "(admin)" in a.windowTitle()
        assert a.admin_menu is not None and a.admin_menu.menuAction().isVisible()
        pump(lambda: b.users.get("alice", {}).get("admin"))
        a.open_conv("#general")
        b.open_conv("#general")
        a.input.setText("welcome everyone"); a.send_input()
        pump(lambda: any(m["text"] == "welcome everyone" for m in b.convs["#general"]))
        assert "\u2605" in b.view.toPlainText()
        # non-admin can't use admin commands; /kick just shows help for them
        b.input.setText("/kick alice"); b.send_input()
        a.input.setText("/announce Server maintenance tonight at 9"); a.send_input()
        pump(lambda: "Announcement from alice" in b.view.toPlainText())
        a.input.setText("/mute bobby 5 spamming"); a.send_input()
        pump(lambda: "You have been muted by alice" in b.view.toPlainText())
        b.input.setText("can I talk?"); b.send_input()
        pump(lambda: "You are muted for 5 more minutes (spamming)" in b.view.toPlainText())
        a.input.setText("/unmute bobby"); a.send_input()
        pump(lambda: "alice unmuted you" in b.view.toPlainText())

        if shots:
            a.resize(1150, 700)
            a.grab().save(os.path.join(shots, "admin.png"))

        a.input.setText("/clearhistory #music"); a.send_input()
        pump(lambda: b.convs["#music"] == [] )
        a.input.setText("/delroom #music"); a.send_input()
        pump(lambda: "#music" not in b.rooms and "#music" not in a.rooms)

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
        c = lc.chat

        # admin kicks and bans through the client; bob lands on the login screen
        lb2 = login(port, "bobby")
        pump(lambda: "bobby" in c.users)
        c.input.setText("/kick bobby go outside"); c.send_input()
        pump(lambda: lb2.isVisible() and "go outside" in lb2.status.text())
        c.input.setText("/ban bobby"); c.send_input()
        pump(lambda: "Banned bobby" in c.view.toPlainText())
        lb3 = login(port, "bobby")
        assert lb3.chat is None and "banned" in lb3.status.text(), lb3.status.text()
        print("gui tests passed")
    finally:
        server.terminate()


if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else None)
