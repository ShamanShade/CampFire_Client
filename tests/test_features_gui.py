"""Replies, reactions, editing, @mentions, typing, muting, roles, private channels,
account settings and sign-up approval between real client windows.
Run:  QT_QPA_PLATFORM=offscreen python tests/test_features_gui.py"""

import os
import socket
import subprocess
import sys
import time

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "tests"))

from test_gui import TMP, cc, free_port, login, pump, start_server  # noqa: E402
from PyQt6.QtWidgets import QApplication, QMenu, QMessageBox  # noqa: E402

QMessageBox.question = staticmethod(lambda *a, **k: QMessageBox.StandardButton.Yes)
QMessageBox.warning = staticmethod(lambda *a, **k: QMessageBox.StandardButton.Yes)
QMessageBox.information = staticmethod(lambda *a, **k: QMessageBox.StandardButton.Ok)
menus = []
QMenu.exec = lambda self, *a: menus.append(self)


def texts(menu):
    return [a.text() for a in menu.actions() if a.text()]


def find(win, text):
    return next((m for m in win.convs[win.current] if m.get("text") == text), {})


def say(win, text):
    win.input.setText(text)
    win.send_input()


def main():
    app = QApplication(sys.argv)
    app.setStyleSheet(cc.STYLE)
    port = free_port()
    db = os.path.join(TMP, "features_gui.db")
    for f in (db,):
        if os.path.exists(f):
            os.remove(f)
    server = start_server(port, db)                  # alice is admin
    try:
        a = login(port, "alice").chat
        b = login(port, "bob").chat
        c = login(port, "carl").chat
        for w in (a, b, c):
            w.open_conv("#general")
        pump(lambda: all({"alice", "bob", "carl"} <= set(w.users) for w in (a, b, c)))

        # ---- reply
        say(a, "who wants pizza")
        pump(lambda: any(m["text"] == "who wants pizza" for m in b.convs["#general"]))
        b.start_reply(find(b, "who wants pizza"))
        assert b.compose_bar.isVisibleTo(b) and "alice" in b.compose_label.text()
        say(b, "me!")
        pump(lambda: any(m["text"] == "me!" and m.get("reply") for m in a.convs["#general"]))
        assert find(a, "me!")["reply"]["user"] == "alice"
        assert not b.compose_bar.isVisibleTo(b)
        assert "who wants pizza" in a.view.toPlainText()
        # replying to alice counts as mentioning her
        assert a.mentions_me(find(a, "me!"))

        # ---- reactions
        b.toggle_reaction(find(b, "who wants pizza"), "👍")
        c.toggle_reaction(find(c, "who wants pizza"), "👍")
        pump(lambda: find(a, "who wants pizza").get("reactions", {}).get("👍") == ["bob", "carl"])
        assert "👍" in a.view.toPlainText() and "2" in a.view.toPlainText()
        b.toggle_reaction(find(b, "who wants pizza"), "👍")      # again: takes it back
        pump(lambda: find(a, "who wants pizza").get("reactions", {}).get("👍") == ["carl"])

        # ---- right-click menu
        menu = QMenu()
        b.add_message_actions(menu, find(b, "me!"))
        assert {"Reply", "Add reaction", "Edit message"} <= set(texts(menu)), texts(menu)
        menu = QMenu()
        b.add_message_actions(menu, find(b, "who wants pizza"))
        assert "Edit message" not in texts(menu)

        # ---- editing (Up arrow edits your last one)
        assert b.edit_last() and b.editing["text"] == "me!"
        b.input.setText("me! (pepperoni)")
        b.send_input()
        pump(lambda: find(a, "me! (pepperoni)").get("edited"))
        assert "(edited)" in a.view.toPlainText()
        assert b.editing is None and not b.input.text()

        # ---- @mentions: autocomplete and highlight
        a.input.setText("hey @ca")
        a.input.setCursorPosition(len(a.input.text()))
        a.update_mentions()
        assert a.mention_list.isVisibleTo(a) and a.mention_list.item(0).text().startswith("carl")
        a.pick_mention("carl")
        assert a.input.text() == "hey @carl "
        a.send_input()
        pump(lambda: any(m["text"] == "hey @carl" for m in c.convs["#general"]))
        assert c.mentions_me(find(c, "hey @carl")) and not b.mentions_me(find(b, "hey @carl"))

        # ---- typing indicator
        b.typed_at = 0
        b.maybe_send_typing("hel")
        pump(lambda: (a.show_typing() or True) and "bob is typing" in a.typing_label.text())
        say(b, "hello")                                   # the message clears it
        pump(lambda: (a.show_typing() or True) and "bob" not in a.typing_label.text())

        # ---- muting a channel: no unread count, except when you're mentioned
        a.input.setText("/join #random"); a.send_input()
        pump(lambda: "#random" in c.rooms)
        c.open_conv("#general")
        c.set_muted("#random", True)
        assert c.is_muted("#random")
        assert any("\U0001f515" in c.room_list.item(i).text() for i in range(c.room_list.count()))
        a.open_conv("#random")
        say(a, "quiet in here")
        for _ in range(40):
            QApplication.processEvents(); time.sleep(0.02)
        assert not c.unread.get("#random"), c.unread
        say(a, "@carl you there?")
        pump(lambda: c.unread.get("#random"))
        c.set_muted("#random", False)
        assert not c.is_muted("#random")

        # ---- roles: make one, give it to bob, it colours him and lets him make channels
        assert not b.can("channels")
        a.admin("role_save", id=None, name="Mods", color="#e67e22", perms=["channels", "kick"])
        pump(lambda: any(r["name"] == "Mods" for r in b.roles))
        rid = next(r["id"] for r in b.roles if r["name"] == "Mods")
        # alice's right-click menu on bob has Roles; carl's doesn't
        a.person_menu(a.user_list, a.user_list.visualItemRect(
            next(a.user_list.item(i) for i in range(a.user_list.count())
                 if a.user_list.item(i).data(cc.Qt.ItemDataRole.UserRole) == "bob")).center())
        assert "Roles" in texts(menus[-1])
        a.admin("role_assign", user="bob", roles=[rid])
        pump(lambda: b.can("channels") and b.can("kick") and not b.can("ban"))
        pump(lambda: c.user_roles.get("bob") == [rid])
        assert c.user_color("bob") == "#e67e22"
        heads = [c.user_list.item(i).text() for i in range(c.user_list.count())]
        assert any(h.startswith("MODS") for h in heads), heads
        b.input.setText("/join #mods"); b.send_input()
        pump(lambda: "#mods" in a.rooms)

        # ---- private channel: only Mods (and admins) see it
        a.admin("room_access", room="#mods", roles=[rid])
        pump(lambda: "#mods" not in c.rooms)
        assert "#mods" in b.rooms and "#mods" in a.private_rooms
        assert any("\U0001f512" in a.room_list.item(i).text() for i in range(a.room_list.count()))
        a.admin("room_access", room="#mods", roles=[])
        pump(lambda: "#mods" in c.rooms)

        # ---- change password and delete account (carl)
        c.conn.send(t="change_password", old="wrong", new="newpass")
        pump(lambda: "isn't right" in c.view.toPlainText() or "isn't right" in c.statusBar().currentMessage())
        c.conn.send(t="change_password", old="secret", new="newpass")
        time.sleep(0.3)
        for _ in range(20):
            QApplication.processEvents(); time.sleep(0.02)
        dlg = cc.SettingsDialog(b)                        # the Account buttons are there
        from PyQt6.QtWidgets import QPushButton
        labels = [x.text() for x in dlg.findChildren(QPushButton)]
        assert "Change password..." in labels and "Delete account..." in labels
        dlg.reject()
        c.conn.send(t="delete_account", password="newpass")
        pump(lambda: "carl" not in a.users)

        # ---- sign-up approval
        a.admin("signup_mode", mode="approval")
        pump(lambda: a.signup_mode == "approval")
        w = login(port, "dave")
        assert w.chat is None and "approv" in w.status.text().lower(), w.status.text()
        pump(lambda: "dave" in a.pending_names)
        a.open_signups()
        dlg = a.signups_dlg
        assert dlg.pending.item(0).text() == "dave"
        dlg.pending.setCurrentRow(0)
        dlg.decide("approve")
        pump(lambda: "dave" not in a.pending_names)
        assert login(port, "dave").chat is not None
        # invite mode: a code lets someone in straight away
        a.admin("signup_mode", mode="invite")
        pump(lambda: a.signup_mode == "invite")
        a.admin("invite_create", uses=1, days=1)
        pump(lambda: dlg.invites.count() > 0 and dlg.invites.item(0).data(cc.Qt.ItemDataRole.UserRole))
        code = dlg.invites.item(0).data(cc.Qt.ItemDataRole.UserRole)
        w = login(port, "erin")
        assert w.chat is None
        w.invite_input.setText(code)
        w.login()
        pump(lambda: w.chat is not None)
        print("features gui tests passed")
    finally:
        server.terminate()


if __name__ == "__main__":
    main()
