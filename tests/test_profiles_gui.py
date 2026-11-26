"""Profile cards, GIF avatars and avatars next to messages, between real client windows.
Run:  QT_QPA_PLATFORM=offscreen python tests/test_profiles_gui.py [shots_dir]   (needs Pillow)"""

import base64
import os
import sys

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "tests"))

from test_gui import TMP, cc, free_port, login, pump, start_server  # noqa: E402
from PyQt6.QtCore import QUrl  # noqa: E402
from PyQt6.QtGui import QImage  # noqa: E402
from PyQt6.QtWidgets import QApplication, QMessageBox  # noqa: E402
from PIL import Image  # noqa: E402

warnings = []
QMessageBox.warning = staticmethod(lambda *a, **k: warnings.append(a[2]))


def make_gif(path, size=(480, 320), frames=40):
    """A big, colourful animated GIF (several MB before shrinking)."""
    import random
    rnd = random.Random(1)
    imgs = []
    for i in range(frames):
        im = Image.new("RGB", size)
        px = im.load()
        for y in range(0, size[1], 2):
            for x in range(0, size[0], 2):
                c = (rnd.randrange(256), (x + i * 9) % 256, (y * 3 + i * 17) % 256)
                px[x, y] = c
        imgs.append(im)
    imgs[0].save(path, save_all=True, append_images=imgs[1:], duration=60, loop=0)
    return path


def point_of(win, text):
    cur = win.view.document().find(text)
    assert not cur.isNull(), text
    return win.view.cursorRect(cur).center()


def main(shots=None):
    app = QApplication(sys.argv)
    cc.apply_appearance(app)
    port = free_port()
    server = start_server(port, os.path.join(TMP, "profiles.db"))
    try:
        a = login(port, "alice").chat
        b = login(port, "bob").chat
        c = login(port, "carl").chat
        for w in (a, b, c):
            w.resize(1150, 700)
            w.show()

        # --- a big animated GIF becomes a small animated avatar
        gif = make_gif(os.path.join(TMP, "big.gif"))
        assert os.path.getsize(gif) > cc.MAX_AVATAR, os.path.getsize(gif)
        dlg = cc.SettingsDialog(a)
        dlg.pick_avatar(gif)
        assert not warnings, warnings
        raw = base64.b64decode(dlg.avatar_data)
        assert cc.is_gif(raw) and len(raw) <= cc.MAX_AVATAR
        with Image.open(__import__("io").BytesIO(raw)) as im:
            assert im.is_animated and im.n_frames == 40 and im.size[0] <= 128, (im.size, im.n_frames)
        assert dlg.preview.movie is not None                      # the preview plays
        dlg.bio_input.setPlainText("Campfire enjoyer.\nAsk me about s'mores https://example.com")
        dlg.status_input.setText("roasting marshmallows")
        dlg.save()
        pump(lambda: b.avatar_ids.get("alice") and os.path.exists(os.path.join(cc.cache_folder("avatars"),
                                                                                 b.avatar_ids["alice"])), timeout=10)

        # a still picture for bob
        png = os.path.join(TMP, "bob.png")
        img = QImage(300, 200, QImage.Format.Format_RGB32)
        img.fill(0x3BA55D)
        img.save(png)
        dlg = cc.SettingsDialog(b)
        dlg.pick_avatar(png)
        dlg.save()
        pump(lambda: a.avatar_raw("bob") is not None, timeout=10)

        # a file that isn't a picture is refused nicely
        junk = os.path.join(TMP, "junk.gif")
        open(junk, "wb").write(b"GIF89a" + os.urandom(cc.MAX_AVATAR + 10))
        cc.SettingsDialog(c).pick_avatar(junk)
        assert warnings and ("too big" in warnings[-1] or "1 MB" in warnings[-1]), warnings

        # --- avatars next to messages, grouped like Discord
        for w in (a, b, c):
            w.open_conv("#general")
        for who, text in ((a, "hey everyone"), (a, "who's up for a game"), (b, "me!"), (a, "nice")):
            who.input.setText(text)
            who.send_input()
            pump(lambda t=text: any(m["text"] == t for m in c.convs["#general"]))
        pump(lambda: c.avatar_raw("alice") and c.avatar_raw("bob"), timeout=10)
        c.redraw()
        page = c.view.toHtml()
        av_a, av_b = c.avatar_ids["alice"], c.avatar_ids["bob"]
        assert page.count(f"campfire-img:a{av_a}") == 2, page.count(f"campfire-img:a{av_a}")   # 2nd msg grouped
        assert page.count(f"campfire-img:a{av_b}") == 1
        assert f"a{av_a}" in c.movies                         # the GIF avatar is animated in chat
        pump(lambda: c.movies[f"a{av_a}"].currentFrameNumber() > 2, timeout=5)
        assert not c.view.thumbs[f"a{av_a}"].isNull()
        assert "alice" in c.list_movies or c.avatar_pixmap("alice", 24) is not None
        c.avatar_pixmap("alice", 24)
        assert "alice" in c.list_movies                        # and in the Online list

        # right-click/click finds the right message: on text, and on the avatar itself
        m = c.message_at(point_of(c, "who's up for a game"))
        assert m["text"] == "who's up for a game"
        m = c.message_at(point_of(c, "me!"))
        assert m["text"] == "me!"

        # --- profile cards
        c.link_clicked(QUrl("campfire://user/alice"))
        card = c.card
        pump(lambda: "Campfire enjoyer" in card.bio.text())
        assert "roasting marshmallows" == card.status.text()
        assert "Online" in card.where.text() and card.joined.text() != "..."
        labels = [card.buttons.itemAt(i).widget().text() for i in range(card.buttons.count())]
        assert labels == ["Message", "Add friend"], labels
        assert card.avatar.movie is not None
        if shots:
            pump(lambda: True, timeout=0.3)
            card.grab().save(os.path.join(shots, "profile_card.png"))
            c.grab().save(os.path.join(shots, "chat_avatars.png"))
        card.buttons.itemAt(0).widget().click()                # Message -> opens the DM
        pump(lambda: c.current == "@alice")
        c.show_profile("carl")
        pump(lambda: c.profiles.get("carl"))
        labels = [c.card.buttons.itemAt(i).widget().text() for i in range(c.card.buttons.count())]
        assert labels == ["Edit profile"], labels
        c.card.close()

        # one click in the Online / Friends list shows the card beside the list; double-click opens a DM
        from PyQt6.QtTest import QTest
        from PyQt6.QtCore import Qt as _Qt
        c.open_conv("#general")
        row = next(i for i in range(c.user_list.count()) if c.user_list.item(i).data(_Qt.ItemDataRole.UserRole) == "bob")
        spot = c.user_list.visualItemRect(c.user_list.item(row)).center()
        QTest.mouseClick(c.user_list.viewport(), _Qt.MouseButton.LeftButton, pos=spot)
        assert c.card is None or not c.card.isVisible() or c.card.name != "bob"     # waits for a possible double-click
        pump(lambda: c.card is not None and c.card.isVisible() and c.card.name == "bob")
        assert c.card.x() + c.card.width() <= c.user_list.mapToGlobal(spot).x()     # to the left of the list
        c.card.close()
        QTest.mouseDClick(c.user_list.viewport(), _Qt.MouseButton.LeftButton, pos=spot)
        pump(lambda: c.current == "@bob")
        pump(lambda: True, timeout=QApplication.doubleClickInterval() / 1000 + 0.3)
        assert not c.card.isVisible()                                             # no card after a double-click
        c.friends = [{"name": "alice", "online": True}]
        c.refresh_friends()
        spot = c.friend_list.visualItemRect(c.friend_list.item(0)).center()
        QTest.mouseClick(c.friend_list.viewport(), _Qt.MouseButton.LeftButton, pos=spot)
        pump(lambda: c.card.isVisible() and c.card.name == "alice")
        c.card.close()

        # About me reaches Settings next time
        pump(lambda: a.profiles.get("alice", {}).get("bio", "").startswith("Campfire enjoyer"))
        assert cc.SettingsDialog(a).bio_input.toPlainText().startswith("Campfire enjoyer")

        # --- someone offline still has their picture next to old messages
        a.close()
        a.conn.close() if hasattr(a.conn, "close") else None
        pump(lambda: "alice" not in c.users, timeout=6)
        d = login(port, "dave").chat
        d.open_conv("#general")
        pump(lambda: len(d.convs["#general"]) >= 4)
        assert d.avatar_ids.get("alice") == av_a                # from the login list, not the online list
        pump(lambda: d.avatar_raw("alice") is not None, timeout=10)
        d.show_profile("alice")
        pump(lambda: "Offline" in d.card.where.text())
        print("profiles gui tests passed")
    finally:
        server.terminate()


if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else None)
