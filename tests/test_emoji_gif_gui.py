"""Emoji picker, :shortcodes:, big emoji, animated GIFs (uploaded, linked, pasted) and favorites.
Run:  QT_QPA_PLATFORM=offscreen python tests/test_emoji_gif_gui.py [screenshot-dir]"""

import http.server
import os
import sys
import threading

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "tests"))

from test_gui import TMP, cc, free_port, login, pump, start_server  # noqa: E402
from PyQt6.QtCore import QEvent, QPoint, QStandardPaths, Qt  # noqa: E402
from PyQt6.QtGui import QColor, QImage, QKeyEvent, QKeySequence  # noqa: E402
from PyQt6.QtWidgets import QApplication  # noqa: E402

QStandardPaths.setTestModeEnabled(True)         # caches and favorites go to a throwaway place
for _loc in (QStandardPaths.StandardLocation.CacheLocation, QStandardPaths.StandardLocation.AppDataLocation):
    import shutil as _sh
    _sh.rmtree(QStandardPaths.writableLocation(_loc), ignore_errors=True)   # start clean every run


def make_gif(path, colors=("#ff0000", "#00ff00", "#0000ff"), size=(160, 120)):
    from PIL import Image
    frames = [Image.new("RGB", size, c) for c in colors]
    frames[0].save(path, save_all=True, append_images=frames[1:], duration=80, loop=0)


def serve(folder):
    """A tiny web server on this machine standing in for Giphy/Tenor/any image host."""
    handler = lambda *a, **k: http.server.SimpleHTTPRequestHandler(*a, directory=folder, **k)  # noqa: E731
    httpd = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    return httpd


def center_color(img):
    return img.pixelColor(QPoint(img.width() // 2, img.height() // 2)).name()


def main(shots=None):
    app = QApplication(sys.argv)
    app.setStyleSheet(cc.STYLE)
    port = free_port()
    server = start_server(port, os.path.join(TMP, "emoji.db"))
    web_dir = os.path.join(TMP, "web")
    os.makedirs(web_dir, exist_ok=True)
    make_gif(os.path.join(web_dir, "dance.gif"), ("#ffaa00", "#00aaff", "#aa00ff"))
    httpd = serve(web_dir)
    try:
        a = login(port, "alice").chat
        b = login(port, "bob").chat

        # picker: categories, search, recents
        a.open_picker()
        p = a.picker
        assert p.grid.count() > 50                                  # first category on first open
        p.search.setText("fire")
        assert p.grid.item(0).text() == "\U0001f525"
        p.choose(p.grid.item(0).text())
        assert a.input.text() == "\U0001f525" and not p.isVisible()
        a.open_picker()
        assert a.picker.tabs.currentIndex() == 0 and a.picker.grid.item(0).text() == "\U0001f525"   # recent
        a.picker.close()

        # :shortcodes: turn into emoji as you type
        a.input.setText("gg :thumbsup")
        a.input.insert(":")
        a.input_edited(a.input.text())
        assert a.input.text() == "gg \U0001f44d", a.input.text()
        a.input.clear()
        a.input.setText("lol :joy: :pizza: :rolling_on_the_floor_laughing: :nope: 10:30:")
        a.send_input()
        pump(lambda: any(m["text"].startswith("lol") for m in b.convs["#general"]))
        sent = next(m["text"] for m in b.convs["#general"] if m["text"].startswith("lol"))
        assert sent == "lol \U0001f602 \U0001f355 \U0001f923 :nope: 10:30:", sent

        # emoji-only messages are big
        a.input.setText(":fire::fire::fire:")
        a.send_input()
        pump(lambda: any(m["text"] == "\U0001f525\U0001f525\U0001f525" for m in b.convs["#general"]))
        assert "font-size:32px" in b.view.toHtml()

        # an uploaded GIF plays in the chat
        gif = os.path.join(TMP, "party.gif")
        make_gif(gif)
        a.upload_file(gif)
        pump(lambda: any((m.get("file") or {}).get("name") == "party.gif" for m in b.convs["#general"]), timeout=10)
        fid = next(m["file"]["id"] for m in b.convs["#general"] if (m.get("file") or {}).get("name") == "party.gif")
        key = f"f{fid}"
        pump(lambda: key in b.movies, timeout=10)
        seen = set()
        pump(lambda: seen.add(center_color(b.view.thumbs[key])) or len(seen) >= 3, timeout=5)
        assert {"#ff0000", "#00ff00", "#0000ff"} <= seen, seen        # all three frames shown

        # GIFs from links show up too (a local server stands in for Giphy/Tenor)
        link = f"http://127.0.0.1:{httpd.server_address[1]}/dance.gif"
        a.input.setText(f"look {link}")
        a.send_input()
        pump(lambda: any(k.startswith("u") for k in b.movies), timeout=10)
        ukey = next(k for k in b.movies if k.startswith("u"))
        assert f"campfire-img:{ukey}" in b.view.toHtml()
        # Giphy page links are turned into the GIF's own address
        assert cc.media_link("https://giphy.com/gifs/cat-dancing-JIX9t2j0ZTN9S")[0] == \
            "https://media.giphy.com/media/JIX9t2j0ZTN9S/giphy.gif"
        assert cc.media_link("https://example.com/page") is None

        # GIFs pause when you look at another conversation
        b.open_conv("#gaming")
        QApplication.processEvents()
        assert all(mv.state() != cc.QMovie.MovieState.Running for mv in b.movies.values())
        b.open_conv("#general")
        pump(lambda: b.movies[key].state() == cc.QMovie.MovieState.Running)

        # favorites: right-click -> Add to favorite GIFs, then send from the picker's GIF tab
        fav = cc.add_favorite_gif(b.media_paths[key], "party.gif")
        assert os.path.basename(fav).startswith("party-") and cc.add_favorite_gif(b.media_paths[key]) == fav
        b.open_picker()
        b.picker.tabs.setCurrentIndex(len(b.picker.tab_keys) - 1)
        assert b.picker.gifs.count() == 1
        before = len([m for m in a.convs["#general"] if m.get("file")])
        b.picker.gifs.itemClicked.emit(b.picker.gifs.item(0))
        pump(lambda: len([m for m in a.convs["#general"] if m.get("file")]) == before + 1, timeout=10)

        # paste a screenshot with Ctrl+V
        img = QImage(200, 100, QImage.Format.Format_RGB32)
        img.fill(QColor("#336699"))
        QApplication.clipboard().setImage(img)
        a.input.setFocus()
        QApplication.sendEvent(a.input, QKeyEvent(QEvent.Type.KeyPress, Qt.Key.Key_V,
                                                  Qt.KeyboardModifier.ControlModifier))
        pump(lambda: any((m.get("file") or {}).get("name", "").startswith("pasted-") for m in b.convs["#general"]),
             timeout=10)
        # ...but pasting text still pastes text
        QApplication.clipboard().setText("just text")
        QApplication.sendEvent(a.input, QKeyEvent(QEvent.Type.KeyPress, Qt.Key.Key_V,
                                                  Qt.KeyboardModifier.ControlModifier))
        assert "just text" in a.input.text()

        if shots:
            b.resize(1150, 700)
            pump(lambda: not b.preview_jobs, timeout=8)
            b.view.verticalScrollBar().setValue(b.view.verticalScrollBar().maximum())
            pump(lambda: True, 0.6)
            b.grab().save(os.path.join(shots, "emoji_gifs.png"))
            a.input.clear()
            a.open_picker()
            a.picker.tabs.setCurrentIndex(1)
            pump(lambda: True, 0.3)
            a.picker.grab().save(os.path.join(shots, "picker.png"))
            a.picker.close()
        print("emoji & gif gui tests passed")
    finally:
        server.terminate()
        httpd.shutdown()


if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else None)
