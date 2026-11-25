"""The server window running a real server, with real client windows connected to it.
Run:  QT_QPA_PLATFORM=offscreen python tests/test_server_gui.py [screenshot-dir]"""

import os
import sys

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "tests"))

from test_gui import TMP, cc, free_port, login, pump  # noqa: E402
from PyQt6.QtWidgets import QApplication, QMessageBox  # noqa: E402

import server as core  # noqa: E402
import server_gui  # noqa: E402
from PyQt6.QtCore import QSettings  # noqa: E402

server_gui.prefs = QSettings(os.path.join(TMP, "server.ini"), QSettings.Format.IniFormat)

# no modal popups in a headless test: record them and say Yes
popups = []
QMessageBox.warning = staticmethod(lambda *a, **k: popups.append(a[2]) or QMessageBox.StandardButton.Ok)
QMessageBox.information = staticmethod(lambda *a, **k: popups.append(a[2]) or QMessageBox.StandardButton.Ok)
QMessageBox.question = staticmethod(lambda *a, **k: QMessageBox.StandardButton.Yes)


def row_names(tbl):
    return [tbl.item(r, 0).text() for r in range(tbl.rowCount())]


def main(shots=None):
    app = QApplication(sys.argv)
    port = free_port()
    args = core.parse_args(["--port", str(port), "--db", os.path.join(TMP, "servergui.db"),
                            "--files", os.path.join(TMP, "servergui_files")])
    w = server_gui.ServerWindow(args)
    app.setStyleSheet(server_gui.STYLE)
    w.show()
    pump(lambda: w.st.running and "Running" in w.status.text())

    app.setStyleSheet(cc.STYLE + server_gui.STYLE)
    la = login(port, "alice")
    lb = login(port, "bob")
    a, b = la.chat, lb.chat
    pump(lambda: set(row_names(w.online_table)) == {"alice", "bob"})
    assert w.stats["online"].text() == "2" and w.stats["accounts"].text() == "2"
    assert "127.0.0.1" in w.online_table.item(0, 2).text()

    # make alice admin from the window: her client updates without logging out
    w.run(w.st.server.set_admin, "alice", True)
    pump(lambda: a.is_admin and a.admin_menu is not None and "(admin)" in a.windowTitle())
    pump(lambda: b.users["alice"]["admin"])                          # others see the star
    pump(lambda: "Admin" in w.acct_table.item(row_names(w.acct_table).index("alice"), 1).text())

    # GIPHY key from the Settings tab reaches clients straight away (and is checked for typos)
    pump(lambda: "off" in w.giphy_status.text())
    w.giphy_input.setText("not a key!")
    w.save_giphy()
    pump(lambda: popups and "doesn't look like" in popups[-1])
    w.giphy_input.setText("AbCdEf0123456789XyZ")
    w.save_giphy()
    pump(lambda: a.giphy_key == "AbCdEf0123456789XyZ" and "on" in w.giphy_status.text())
    w.giphy_input.clear()
    w.save_giphy()
    pump(lambda: a.giphy_key == "")

    # channels
    w.run(w.st.server.add_room, "Movies")
    pump(lambda: "#movies" in b.rooms and "#movies" in [w.text_list.item(i).text() for i in range(w.text_list.count())])
    w.run(w.st.server.add_voice_room, "Study Hall")
    pump(lambda: any(c["name"] == "Study Hall" for c in b.voice_channels))

    # announcement from the log tab
    w.announce_input.setText("Maintenance at 9")
    w.announce()
    pump(lambda: "Announcement from Server" in b.view.toPlainText())

    # files show up on the Files tab, and can be deleted from there
    src = os.path.join(TMP, "report.pdf")
    open(src, "wb").write(os.urandom(200_000))
    b.upload_file(src)
    pump(lambda: w.files_table.rowCount() == 1 and w.files_table.item(0, 0).text() == "report.pdf", timeout=10)
    assert w.files_table.item(0, 2).text() == "bob" and w.stats["files"].text().startswith("1")
    w.files_table.selectRow(0)
    w.delete_file()
    pump(lambda: w.files_table.rowCount() == 0 and "(file deleted)" in a.view.toPlainText())

    # move storage to another folder: files move, the server restarts there, downloads still work
    keep = os.path.join(TMP, "keep.bin")
    payload = os.urandom(1_500_000)
    open(keep, "wb").write(payload)
    b.upload_file(keep)
    pump(lambda: w.files_table.rowCount() == 1, timeout=10)
    kept = w.snap["files_list"][0]
    old_dir = w.files_dir
    new_dir = os.path.join(TMP, "bigger_drive", "campfire uploads")
    from PyQt6.QtWidgets import QFileDialog
    QFileDialog.getExistingDirectory = staticmethod(lambda *a, **k: new_dir)
    orig_exec = QMessageBox.exec
    QMessageBox.exec = lambda box: next(bt for bt in box.buttons() if bt.text() == "Move them").click()
    w.change_storage()
    QMessageBox.exec = orig_exec
    pump(lambda: w.files_dir == new_dir and w.st.running, timeout=15)
    pump(lambda: w.snap.get("files_dir") == new_dir, timeout=5)
    assert len([f for f in os.listdir(new_dir) if f != "partial"]) == 1 and not os.path.exists(old_dir)
    assert server_gui.prefs.value("files_dir") == new_dir and new_dir in w.storage_label.text()
    pump(lambda: a.statusBar().currentMessage() == "Connected" and b.statusBar().currentMessage() == "Connected",
         timeout=20)                                     # clients reconnected by themselves
    dest = os.path.join(TMP, "kept_again.bin")
    a.download_file(kept["id"], dest=dest)
    pump(lambda: os.path.exists(dest), timeout=10)
    assert open(dest, "rb").read() == payload

    # mute, then kick, from the window (the server can act on admins too)
    w.run(w.st.server.console, "mute", user="bob", minutes=5, reason="calm down")
    pump(lambda: "muted by Server" in b.view.toPlainText())
    w.run(w.st.server.console, "unmute", user="bob")
    pump(lambda: "Server unmuted you" in b.view.toPlainText())

    if shots:
        w.resize(1000, 680)
        w.tabs.setCurrentIndex(0)
        pump(lambda: True, 1.2)
        w.grab().save(os.path.join(shots, "server_window.png"))
        w.tabs.setCurrentIndex(4)
        pump(lambda: True, 0.3)
        w.grab().save(os.path.join(shots, "server_log.png"))

    w.run(w.st.server.console, "kick", user="bob", reason="testing")
    pump(lambda: lb.isVisible() and "Kicked by Server" in lb.status.text())
    pump(lambda: row_names(w.online_table) == ["alice"])

    # ban shows on the Bans tab, then unban
    w.run(w.st.server.console, "ban", user="bob", reason="spam")
    pump(lambda: any("bob" in w.ban_list.item(i).text() for i in range(w.ban_list.count())))
    w.ban_list.setCurrentRow(0)
    w.unban_selected()
    pump(lambda: w.ban_list.count() == 0)

    # a second server on the same port fails with a clear message
    w2 = server_gui.ServerWindow(args)
    pump(lambda: popups and "already in use" in popups[-1])
    assert not w2.st.running
    w2.close()

    # the log shows what happened
    log = w.log_view.toPlainText()
    for needle in ("listening on", "login alice", "Made alice an admin", "Kicked bob"):
        assert needle in log, needle

    # stopping the server disconnects everyone
    w.toggle()
    pump(lambda: not w.st.running and "Stopped" in w.status.text())
    pump(lambda: la.isVisible() and "shutting down" in la.status.text())
    # and it can start again
    w.toggle()
    pump(lambda: w.st.running)
    w.close()
    assert not w.st.running
    print("server gui tests passed")


if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else None)
