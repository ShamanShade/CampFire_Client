"""Themes, profiles (name colour + status), saved DMs, fades and admin-only channel creation.
Run:  QT_QPA_PLATFORM=offscreen python tests/test_custom_gui.py [shots_dir]"""

import os
import sys

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "tests"))

from test_gui import TMP, cc, free_port, login, pump, start_server  # noqa: E402
from PyQt6.QtCore import Qt  # noqa: E402
from PyQt6.QtWidgets import QApplication, QGraphicsOpacityEffect  # noqa: E402


def relogin(win, port, user):
    win.close()
    win.conn.close() if hasattr(win.conn, "close") else None
    pump(lambda: True, timeout=0.3)
    return login(port, user).chat


def main(shots=None):
    app = QApplication(sys.argv)
    cc.apply_appearance(app)
    port = free_port()
    server = start_server(port, os.path.join(TMP, "custom.db"))      # alice is admin
    try:
        a = login(port, "alice").chat
        b = login(port, "bob").chat
        a.show(), b.show()

        # only admins see (and can use) the + buttons
        assert a.room_add.isVisible() and a.voice_add.isVisible()
        assert not b.room_add.isVisible() and not b.voice_add.isVisible()
        b.create_room()
        b.create_voice()                                  # refused without even asking for a name

        # themes change the stylesheet; text size too
        for theme in cc.THEMES:
            css = cc.themed_style(theme, "", "Normal")
            assert cc.THEMES[theme]["bg"] in css or theme == "Dark"
        css = cc.themed_style("Dark", "#ff0000", "Large")
        assert "#ff0000" in css and "11.5pt" in css
        cc.settings.setValue("look/theme", "Light")
        cc.apply_appearance(app)
        assert cc.THEMES["Light"]["bg"] in app.styleSheet()
        if shots:
            a.open_conv("#general")
            a.grab().save(os.path.join(shots, "theme_light.png"))
        cc.settings.setValue("look/theme", "Campfire")
        cc.apply_appearance(app)
        if shots:
            a.grab().save(os.path.join(shots, "theme_campfire.png"))
        cc.settings.setValue("look/theme", "Dark")
        cc.apply_appearance(app)

        # profile: name colour and status reach everyone
        b.conn.send(t="profile", color="#ff8800", status="playing Minecraft")
        pump(lambda: a.colors.get("bob") == "#ff8800" and a.users["bob"].get("status") == "playing Minecraft")
        assert a.user_color("bob") == "#ff8800"
        statuses = [a.user_list.item(i).data(Qt.ItemDataRole.UserRole + 1) for i in range(a.user_list.count())]
        assert "playing Minecraft" in statuses, statuses
        b.conn.send(t="profile", color="nope", status="")
        pump(lambda: True, timeout=0.5)
        assert a.colors.get("bob") == "#ff8800"           # bad colour refused

        # the settings dialog builds (it scrolls now) and saves the look
        dlg = cc.SettingsDialog(a)
        dlg.theme_combo.setCurrentText("Midnight")
        dlg.anim_switch.setChecked(True)
        dlg.status_input.setText("brb")
        dlg.save()
        assert cc.settings.value("look/theme") == "Midnight"
        pump(lambda: b.users["alice"].get("status") == "brb")
        if shots:
            dlg2 = cc.SettingsDialog(a)
            dlg2.show()
            pump(lambda: True, timeout=0.3)
            dlg2.grab().save(os.path.join(shots, "settings_appearance.png"))
            dlg2.close()

        # fade when switching chats
        a.open_conv("#general")
        assert isinstance(a.view.graphicsEffect(), QGraphicsOpacityEffect)
        pump(lambda: a.view.graphicsEffect() is None, timeout=2)
        cc.settings.setValue("look/animations", False)
        a.open_conv("#general")
        assert a.view.graphicsEffect() is None
        cc.settings.setValue("look/animations", True)

        # DMs come back after relaunching
        b.open_dm("alice")
        b.input.setText("hey alice")
        b.send_input()
        pump(lambda: any(x["text"] == "hey alice" for x in a.convs.get("@bob", [])))
        b = relogin(b, port, "bob")
        assert "@alice" in b.convs, list(b.convs)
        # ... unless you closed them
        b.close_dm("@alice")
        b = relogin(b, port, "bob")
        assert "@alice" not in b.convs
        # a new message brings it back, and it stays after that
        a.open_dm("bob")
        a.input.setText("you there?")
        a.send_input()
        pump(lambda: "@alice" in b.convs)
        b = relogin(b, port, "bob")
        assert "@alice" in b.convs
        cc.settings.setValue("look/theme", "Dark")
        print("customizing gui tests passed")
    finally:
        server.terminate()


if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else None)
