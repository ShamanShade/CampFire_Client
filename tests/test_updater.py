"""Update check against a fake GitHub release on this machine.
Run:  QT_QPA_PLATFORM=offscreen python tests/test_updater.py"""

import http.server
import json
import os
import sys
import threading
import time

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "tests"))

from test_gui import TMP, cc, pump  # noqa: E402
from PyQt6.QtWidgets import QApplication, QMessageBox, QWidget  # noqa: E402

import updater  # noqa: E402

shown, launched, opened, quit_called = [], [], [], []
answer = {"button": "Later"}


def fake_exec(box):
    shown.append(box.text())
    next(b for b in box.buttons() if b.text() == answer["button"]).click()


def main():
    app = QApplication(sys.argv)
    folder = os.path.join(TMP, "release")
    os.makedirs(folder, exist_ok=True)
    setup_bytes = os.urandom(3 * 1024 * 1024)
    open(os.path.join(folder, "Campfire-Setup.exe"), "wb").write(setup_bytes)
    json.dump({"version": "1.0.9", "setup": "Campfire-Setup.exe"}, open(os.path.join(folder, "version.json"), "w"))
    handler = lambda *a, **k: http.server.SimpleHTTPRequestHandler(*a, directory=folder, **k)  # noqa: E731
    httpd = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
    httpd.RequestHandlerClass.log_message = lambda *a: None
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{httpd.server_address[1]}"

    QMessageBox.exec = fake_exec
    QMessageBox.information = staticmethod(lambda *a, **k: shown.append(a[2]))
    QMessageBox.warning = staticmethod(lambda *a, **k: shown.append(a[2]))
    updater.QProcess.startDetached = staticmethod(lambda prog, args: launched.append((prog, args)) or True)
    updater.QApplication.quit = staticmethod(lambda: quit_called.append(1))
    updater.QDesktopServices.openUrl = staticmethod(lambda url: opened.append(url.toString()) or True)

    # version maths and when checks happen at all
    assert updater.is_newer("1.0.10", "1.0.9") and not updater.is_newer("1.0.3", "1.0.3")
    assert not updater.is_newer("1.0.4", "dev") and updater.parse("v1.2.3") == (1, 2, 3)
    assert updater.schedule_check(QWidget(), cc.settings) is None          # running from source: no checks
    sys.frozen = True
    updater.VERSION = "1.0.3"
    assert updater.can_update()

    win = QWidget()

    def run_check(manual=False, admin=False):
        u = updater.Updater(win, cc.settings, admin=admin)
        u.base = base
        n = len(shown)
        u.check(manual=manual)
        pump(lambda: not u.busy, timeout=5)
        pump(lambda: True, 0.1)
        return u, shown[n:]

    # newer version found -> asked; Later does nothing
    answer["button"] = "Later"
    u, asked = run_check()
    assert asked and "1.0.9 is available. You have 1.0.3" in asked[0], asked
    assert not launched

    # Skip this version -> not asked again at start-up (but a manual check still offers it)
    answer["button"] = "Skip this version"
    run_check()
    assert cc.settings.value("update/skip") == "1.0.9"
    _, asked = run_check()
    assert asked == []
    answer["button"] = "Later"
    _, asked = run_check(manual=True)
    assert asked and "available" in asked[0]

    # the check can be turned off in Settings
    cc.settings.setValue("update/skip", "")
    cc.settings.setValue("update/check", False)
    _, asked = run_check()
    assert asked == []
    cc.settings.setValue("update/check", True)

    # Update now: downloads the setup file, runs it silently with relaunch, and closes Campfire
    answer["button"] = "Update now"
    u, _ = run_check()
    pump(lambda: launched, timeout=10)
    prog, args = launched[-1]
    assert open(prog, "rb").read() == setup_bytes and prog.endswith("Campfire-Setup-1.0.9.exe")
    assert "/SILENT" in args and "/relaunch=1" in args and "/CLOSEAPPLICATIONS" in args
    assert quit_called

    # Campfire Admin: Update now opens the Releases page (its installer is private)
    n = len(launched)
    _, asked = run_check(admin=True)
    assert "Campfire Admin 1.0.9" in asked[0] and "private" in asked[0]
    assert opened and opened[-1].endswith("/releases") and len(launched) == n

    # already up to date / can't reach GitHub
    updater.VERSION = "1.0.9"
    _, asked = run_check(manual=True)
    assert asked == ["You have the latest version (1.0.9)."], asked
    httpd.shutdown()
    httpd.server_close()
    _, asked = run_check(manual=True)
    assert asked and "Couldn't check" in asked[0], asked
    print("updater tests passed")


if __name__ == "__main__":
    main()
