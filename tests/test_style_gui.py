"""Profile banners (pictures/GIFs/colours), card colours, avatar decorations and profile effects.
Run:  QT_QPA_PLATFORM=offscreen python tests/test_style_gui.py [shots_dir]   (needs Pillow)"""

import os
import sys

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "tests"))

from test_gui import TMP, cc, free_port, login, pump, start_server  # noqa: E402
from PyQt6.QtWidgets import QApplication, QMessageBox  # noqa: E402
from PIL import Image  # noqa: E402
import effects  # noqa: E402

warnings = []
QMessageBox.warning = staticmethod(lambda *a, **k: warnings.append(a[2]))


def make_gif(path, size=(900, 400), frames=16):
    imgs = []
    for i in range(frames):
        im = Image.new("RGB", size, (20 + i * 10, 40, 120))
        for x in range(0, size[0], 60):
            for y in range(size[1]):
                im.putpixel(((x + i * 7) % size[0], y), (255, 160, 40))
        imgs.append(im)
    imgs[0].save(path, save_all=True, append_images=imgs[1:], duration=70, loop=0)
    return path


def main(shots=None):
    app = QApplication(sys.argv)
    cc.apply_appearance(app)
    port = free_port()
    server = start_server(port, os.path.join(TMP, "style.db"))
    try:
        a = login(port, "alice").chat
        b = login(port, "bob").chat
        for w in (a, b):
            w.resize(1150, 700)
            w.show()

        # every decoration and effect draws without trouble
        base = cc.round_image(cc.default_avatar("x", 80).toImage(), 40)
        for key, _label in effects.DECORATIONS[1:]:
            out = effects.decorate(base, key, 40)
            assert out.width() == 80 and not out.isNull(), key
        layer = effects.EffectLayer(None)
        layer.resize(300, 400)
        for key, _label in effects.EFFECTS[1:]:
            layer.set_effect(key)
            for _ in range(60):
                layer.tick() if layer.isVisible() else effects.step(key, layer.parts, 1 / 30, 300, 400, layer.rnd)
            assert len(layer.parts) == effects.COUNT[key]
            img = cc.QImage(300, 400, cc.QImage.Format.Format_ARGB32)
            img.fill(0)
            p = cc.QPainter(img)
            effects.draw(key, layer.parts, p)
            p.end()

        # alice styles her profile
        dlg = cc.SettingsDialog(a)
        dlg.pick_banner(make_gif(os.path.join(TMP, "banner.gif")))
        assert not warnings, warnings
        raw = cc.base64.b64decode(dlg.banner_data)
        assert cc.is_gif(raw) and len(raw) <= cc.MAX_BANNER
        with Image.open(cc.io.BytesIO(raw) if hasattr(cc, "io") else __import__("io").BytesIO(raw)) as im:
            assert im.is_animated and im.size[0] <= 600 and abs(im.size[0] / im.size[1] - 600 / 212) < 0.1, im.size
        dlg.set_style("theme", "#4a2c6e,#1b3a5c")
        dlg.set_style("banner_color", "#e8742c")
        dlg.deco_combo.setCurrentIndex(dlg.deco_combo.findData("flame"))
        dlg.effect_combo.setCurrentIndex(dlg.effect_combo.findData("embers"))
        dlg.preview_profile()                                    # the preview shows the unsaved style
        pc = dlg.preview_card
        assert pc.banner.height() == 106 and pc.deco.isVisible() and pc.effect.effect == "embers"
        pc.close()
        dlg.save()

        # bob sees it: decoration in the lists and chat, banner + colours + effect on the card
        pump(lambda: b.decos.get("alice") == "flame")
        a.open_conv("#general")
        b.open_conv("#general")
        a.input.setText("look at my profile!")
        a.send_input()
        pump(lambda: any(m["text"] == "look at my profile!" for m in b.convs["#general"]))
        b.redraw()
        assert f"campfire-img:{b.avatar_keys['alice']}" in b.view.toHtml() and b.avatar_keys["alice"].endswith("-flame")
        b.show_profile("alice")
        card = b.card
        pump(lambda: card.banner.height() == 106 and card.banner.movie is not None, timeout=10)
        assert card.deco.isVisible() and card.effect.effect == "embers" and card.effect.timer.isActive()
        assert "#4a2c6e" in card.styleSheet() and "qlineargradient" in card.styleSheet()
        pump(lambda: True, timeout=0.6)                          # let the embers rise
        if shots:
            card.grab().save(os.path.join(shots, "style_card.png"))
            b.grab().save(os.path.join(shots, "style_chat.png"))
        card.close()

        # taking it all off again
        dlg = cc.SettingsDialog(a)
        dlg.clear_banner()
        dlg.set_style("theme", "")
        dlg.deco_combo.setCurrentIndex(0)
        dlg.effect_combo.setCurrentIndex(0)
        dlg.save()
        pump(lambda: "alice" not in b.decos)
        b.profiles.pop("alice", None)
        b.show_profile("alice")
        pump(lambda: b.profiles.get("alice", {}).get("bn") == "")
        b.card.fill(b.profiles["alice"])
        assert b.card.banner.height() == 60 and not b.card.deco.isVisible() and not b.card.effect.timer.isActive()

        # the server refuses made-up decorations and effects
        a.conn.send(t="profile", deco="dragon")
        a.conn.send(t="profile", effect="lasers")
        pump(lambda: True, timeout=0.5)
        assert "alice" not in b.decos
        print("style gui tests passed")
    finally:
        server.terminate()


if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else None)
