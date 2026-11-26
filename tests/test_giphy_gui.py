"""GIPHY search in the picker, against a fake GIPHY running on this machine.
Run:  QT_QPA_PLATFORM=offscreen python tests/test_giphy_gui.py [screenshot-dir]"""

import http.server
import json
import os
import subprocess
import sys
import threading
import urllib.parse

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "tests"))

import test_emoji_gif_gui as eg  # noqa: E402  (clean caches, make_gif)
from test_gui import TMP, cc, free_port, login, pump  # noqa: E402
from PyQt6.QtWidgets import QApplication  # noqa: E402

KEY = "TestKey0123456789abcdef"
requests = []


def fake_giphy(folder):
    colors = ["#e74c3c", "#2ecc71", "#3498db", "#f1c40f", "#9b59b6", "#1abc9c"]
    for i, c in enumerate(colors):
        eg.make_gif(os.path.join(folder, f"g{i}.gif"), (c, "#ffffff", c), (200, 150))

    class H(http.server.SimpleHTTPRequestHandler):
        def __init__(self, *a, **k):
            super().__init__(*a, directory=folder, **k)

        def log_message(self, *a):
            pass

        def do_GET(self):
            u = urllib.parse.urlparse(self.path)
            if not u.path.startswith("/v1/gifs/"):
                return super().do_GET()
            qs = urllib.parse.parse_qs(u.query)
            requests.append((u.path, qs))
            if qs.get("api_key") != [KEY]:
                return self.reply(401, {"meta": {"status": 401, "msg": "Unauthorized"}})
            q = qs.get("q", [""])[0]
            if q == "limit":
                return self.reply(429, {"meta": {"status": 429, "msg": "Too many requests"}})
            n = 0 if q == "zzzz" else 3 if u.path.endswith("trending") else 6
            base = f"http://127.0.0.1:{self.server.server_address[1]}"
            data = [{"id": f"id{i}", "title": f"{q or 'trending'} {i}", "images": {
                "fixed_width_small": {"url": f"{base}/g{i}.gif?cid=abc", "width": "100", "height": "75"},
                "downsized": {"url": f"{base}/g{i}.gif?cid=abc&rid=giphy.gif", "width": "200", "height": "150"}}}
                for i in range(n)]
            self.reply(200, {"data": data, "meta": {"status": 200, "msg": "OK"}})

        def reply(self, code, obj):
            body = json.dumps(obj).encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    httpd = http.server.ThreadingHTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    return httpd


def main(shots=None):
    app = QApplication(sys.argv)
    app.setStyleSheet(cc.STYLE)
    web = os.path.join(TMP, "giphy")
    os.makedirs(web, exist_ok=True)
    httpd = fake_giphy(web)
    cc.GIPHY_API = f"http://127.0.0.1:{httpd.server_address[1]}/v1/gifs"
    port = free_port()
    db = os.path.join(TMP, "giphy.db")
    server = subprocess.Popen([sys.executable, os.path.join(ROOT, "server.py"), "--nogui", "--signup", "open", "--host", "127.0.0.1",
                               "--port", str(port), "--db", db, "--files", db + "_files", "--giphy-key", KEY],
                              stdout=subprocess.DEVNULL, stderr=subprocess.STDOUT)
    try:
        import socket, time  # noqa: E401
        for _ in range(50):
            try:
                socket.create_connection(("127.0.0.1", port), 0.2).close()
                break
            except OSError:
                time.sleep(0.1)
        a = login(port, "alice").chat
        b = login(port, "bob").chat
        pump(lambda: a.giphy_key == KEY)                    # handed over by the server at login

        # GIF tab opens on GIPHY trending, animated
        a.open_picker()
        p = a.picker
        p.tabs.setCurrentIndex(len(p.tab_keys) - 1)
        assert p.mode == "giphy" and p.giphy_btn.isChecked() and p.search.placeholderText() == "Search GIPHY"
        pump(lambda: p.giphy_grid.count() == 3 and all(p.giphy_grid.itemAt(i).widget().movie is not None
                                                       for i in range(3)), timeout=8)
        assert requests[-1][0].endswith("/trending") and requests[-1][1]["rating"] == ["pg-13"]

        # typing searches (once, after a short pause)
        n_before = len(requests)
        for ch in "cats":
            p.search.setText(p.search.text() + ch)
        pump(lambda: p.giphy_grid.count() == 6 and '"cats"' in p.giphy_status.text(), timeout=8)
        assert len(requests) == n_before + 1 and requests[-1][1]["q"] == ["cats"]
        # the same search again comes from the cache (saves the hourly limit)
        p.search.setText("")
        pump(lambda: p.giphy_grid.count() == 3, timeout=5)
        p.search.setText("cats")
        pump(lambda: p.giphy_grid.count() == 6, timeout=5)
        assert len(requests) == n_before + 1

        if shots:
            pump(lambda: True, 0.5)
            p.grab().save(os.path.join(shots, "giphy.png"))

        # click a GIF: it's sent and shows as just the GIF for everyone
        first = p.giphy_grid.itemAt(0).widget()
        sent_url = first.send_url
        first.clicked.emit(sent_url)
        assert not p.isVisible()
        pump(lambda: any(m["text"] == sent_url for m in b.convs["#general"]), timeout=5)
        pump(lambda: any(k.startswith("u") for k in b.movies), timeout=8)
        page = b.view.toPlainText()
        assert sent_url not in page, "the link text should be hidden behind the GIF"

        # error messages
        a.open_picker()
        p = a.picker
        p.tabs.setCurrentIndex(len(p.tab_keys) - 1)
        p.search.setText("limit")
        pump(lambda: "hourly" in p.giphy_status.text(), timeout=5)
        p.search.setText("zzzz")
        pump(lambda: "nothing found" in p.giphy_status.text(), timeout=5)
        a.giphy_key = "WrongKey123"
        cc._giphy_cache.clear()
        a.open_picker()
        a.picker.tabs.setCurrentIndex(len(a.picker.tab_keys) - 1)
        pump(lambda: "isn't working" in a.picker.giphy_status.text(), timeout=5)
        a.picker.close()

        # without a key, the GIF tab shows favorites and explains why GIPHY is off
        a.giphy_key = ""
        a.open_picker()
        a.picker.tabs.setCurrentIndex(len(a.picker.tab_keys) - 1)
        assert a.picker.mode == "favorites" and not a.picker.giphy_btn.isEnabled()
        a.picker.close()
        print("giphy gui tests passed")
    finally:
        server.terminate()
        httpd.shutdown()


if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else None)
