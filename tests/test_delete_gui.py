"""Deleting messages between real client windows.
Run:  QT_QPA_PLATFORM=offscreen python tests/test_delete_gui.py"""

import os
import sys

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "tests"))

from test_gui import TMP, cc, free_port, login, pump, start_server  # noqa: E402
from PyQt6.QtCore import QUrl  # noqa: E402
from PyQt6.QtWidgets import QApplication, QMenu, QMessageBox  # noqa: E402

QMessageBox.question = staticmethod(lambda *a, **k: QMessageBox.StandardButton.Yes)
menus = []
QMenu.exec = lambda self, *a: menus.append([act.text() for act in self.actions()])


def point_of(win, text):
    """Where a piece of text is drawn in a window's chat view."""
    cur = win.view.document().find(text)
    assert not cur.isNull(), text
    return win.view.cursorRect(cur).center()


def actions_for(win, text):
    m = win.message_at(point_of(win, text))
    assert m is not None and (text in m["text"] or text == (m.get("file") or {}).get("name")), (text, m)
    menu = QMenu()
    win.add_message_actions(menu, m)
    return m, [a.text() for a in menu.actions()]


def main():
    app = QApplication(sys.argv)
    port = free_port()
    server = start_server(port, os.path.join(TMP, "delete.db"))      # alice is admin
    try:
        a = login(port, "alice").chat
        b = login(port, "bob").chat
        c = login(port, "carl").chat
        for w in (a, b, c):
            w.open_conv("#general")
        for text in ("first from bob", "second from bob"):
            b.input.setText(text)
            b.send_input()
        c.input.setText("carl says hi")
        c.send_input()
        pump(lambda: all(len(w.convs["#general"]) == 3 for w in (a, b, c)))

        # right-click finds the right message; who gets "Delete"
        m, acts = actions_for(b, "second from bob")
        assert acts == ["Copy text", "Delete message"]
        _, acts = actions_for(c, "second from bob")
        assert acts == ["Copy text"]                                   # not carl's message
        _, acts = actions_for(a, "carl says hi")
        assert acts == ["Copy text", "Delete carl's message"]          # admin, public channel

        # bob deletes his own: gone for everyone, the rest stay
        b.delete_message(m)
        pump(lambda: all("second from bob" not in w.view.toPlainText() for w in (a, b, c)))
        assert all("first from bob" in w.view.toPlainText() for w in (a, b, c))

        # the admin deletes carl's
        m, _ = actions_for(a, "carl says hi")
        a.delete_message(m)
        pump(lambda: "carl says hi" not in c.view.toPlainText())

        # clicking a timestamp opens the same menu
        mid = next(x["id"] for x in b.convs["#general"] if x["text"] == "first from bob")
        b.link_clicked(QUrl(f"campfire://msg/{mid}"))
        assert menus[-1] == ["Copy text", "Delete message"]

        # copy text
        _, _ = actions_for(c, "first from bob")
        menu = QMenu()
        c.add_message_actions(menu, c.find_message(mid))
        menu.actions()[0].trigger()
        assert QApplication.clipboard().text() == "first from bob"

        # DMs: the admin can't delete the other person's DM message
        b.open_dm("alice")
        b.input.setText("private note")
        b.send_input()
        pump(lambda: any(x["text"] == "private note" for x in a.convs.get("@bob", [])))
        a.open_conv("@bob")
        _, acts = actions_for(a, "private note")
        assert acts == ["Copy text"]

        # deleting a message with a file removes the card too
        a.open_conv("#general")
        b.open_conv("#general")
        src = os.path.join(TMP, "notes.txt")
        open(src, "w").write("hello")
        b.upload_file(src)
        pump(lambda: "notes.txt" in a.view.toPlainText(), timeout=10)
        m, acts = actions_for(b, "notes.txt")
        assert "Delete message" in acts and m.get("file")
        b.delete_message(m)
        pump(lambda: "notes.txt" not in a.view.toPlainText())

        # history after reconnecting doesn't bring deleted messages back
        a.loaded.discard("#general")
        a.open_conv("#general")
        pump(lambda: "#general" in a.loaded)
        page = a.view.toPlainText()
        assert "first from bob" in page and "second from bob" not in page and "carl says hi" not in page
        print("delete gui tests passed")
    finally:
        server.terminate()


if __name__ == "__main__":
    main()
