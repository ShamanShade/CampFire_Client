"""Server self-update: checking, downloading, swapping files, git pulls, the server window banner,
and the app telling people when the server is too old.
Run:  QT_QPA_PLATFORM=offscreen python tests/test_server_update.py"""

import http.server
import io
import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
import zipfile

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "tests"))

import server_update as su  # noqa: E402

TMP = tempfile.mkdtemp(prefix="cf_update_")


def tree(path):
    return sorted(os.path.relpath(os.path.join(r, f), path) for r, _d, fs in os.walk(path) for f in fs)


def write(path, text):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as f:
        f.write(text)


def read(path):
    with open(path) as f:
        return f.read()


def fake_release(files):
    """A tiny web server standing in for the GitHub release."""
    class H(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            name = self.path.rsplit("/", 1)[-1]
            if name not in files:
                self.send_response(404)
                self.end_headers()
                return
            body = files[name]
            self.send_response(200)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *a):
            pass

    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return f"http://127.0.0.1:{srv.server_address[1]}", srv


def make_zip(entries):
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        for name, text in entries.items():
            z.writestr(name, text)
    return buf.getvalue()


def test_copy_over():
    src, dst = os.path.join(TMP, "new"), os.path.join(TMP, "app")
    write(os.path.join(src, "Campfire Server.exe"), "new server")
    write(os.path.join(src, "Campfire.exe"), "new client")
    write(os.path.join(src, "_internal", "lib.dll"), "new lib")
    write(os.path.join(dst, "Campfire Server.exe"), "old server")
    write(os.path.join(dst, "_internal", "lib.dll"), "old lib")
    write(os.path.join(dst, "_internal", "gone.dll"), "removed in the new version")
    write(os.path.join(dst, "campfire.db"), "ACCOUNTS")
    write(os.path.join(dst, "campfire.db-wal"), "WAL")
    write(os.path.join(dst, "campfire_files", "abc"), "A FILE")
    logs = []
    su.copy_over(src, dst, logs.append)
    assert read(os.path.join(dst, "Campfire Server.exe")) == "new server"
    assert read(os.path.join(dst, "Campfire.exe")) == "new client"
    assert read(os.path.join(dst, "_internal", "lib.dll")) == "new lib"
    assert not os.path.exists(os.path.join(dst, "_internal", "gone.dll"))
    assert read(os.path.join(dst, "campfire.db")) == "ACCOUNTS"
    assert read(os.path.join(dst, "campfire_files", "abc")) == "A FILE"
    assert not any(n.endswith(".old") for n in os.listdir(dst))

    # a failure halfway puts the old version back exactly
    write(os.path.join(src, "Campfire Server.exe"), "newer server")
    write(os.path.join(src, "_internal", "lib.dll"), "newer lib")
    before = {p: read(os.path.join(dst, p)) for p in tree(dst)}
    real = shutil.copy2

    def broken(s, d, *a, **k):
        if s.endswith("Campfire.exe"):
            raise OSError(13, "in use", s)
        return real(s, d, *a, **k)

    shutil.copy2 = broken
    su.retry.__kwdefaults__ = {"tries": 2}
    try:
        su.copy_over(src, dst, logs.append)
        raise AssertionError("should have failed")
    except OSError:
        pass
    finally:
        shutil.copy2 = real
        su.retry.__kwdefaults__ = {"tries": 40}
    after = {p: read(os.path.join(dst, p)) for p in tree(dst)}
    assert after == before, (before, after)
    print("copy_over ok")


def test_check_and_download():
    exe = "MZ new server exe"
    files = {"version.json": json.dumps({"version": "1.0.12"}).encode(),
             su.ZIP_NAME: make_zip({"Campfire/Campfire Server.exe": exe, "Campfire/_internal/x.dll": "x",
                                    "Campfire/README.md": "readme"})}
    base, srv = fake_release(files)
    real_mode, real_version, real_dir = su.mode, su.VERSION, su.UPDATE_DIR
    try:
        su.mode = lambda: "frozen"
        su.VERSION = "1.0.9"
        r = su.check(base)
        assert r["available"] and r["latest"] == "1.0.12", r
        su.VERSION = "1.0.12"
        assert not su.check(base)["available"]
        su.VERSION = "1.0.9"
        assert su.check("http://127.0.0.1:9")["error"]          # offline: an error, not a crash

        su.UPDATE_DIR = os.path.join(TMP, "_update")
        seen = []
        new = su.download(base, lambda got, total: seen.append((got, total)))
        assert read(os.path.join(new, "Campfire Server.exe")) == exe and seen
        cmd = su.finisher_command(new, ["--port", "5"])
        assert "--finish-update" in cmd and "--wait-pid" in cmd

        files[su.ZIP_NAME] = make_zip({"../evil.txt": "no"})     # no writing outside the update folder
        try:
            su.download(base)
            raise AssertionError("zip slip not caught")
        except ValueError:
            pass
        assert not os.path.exists(os.path.join(TMP, "evil.txt"))
    finally:
        su.mode, su.VERSION, su.UPDATE_DIR = real_mode, real_version, real_dir
        srv.shutdown()
    print("check/download ok")


def free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def test_finish_end_to_end():
    """The new copy waits for the old server, swaps the files in and starts the updated server."""
    target, staging = os.path.join(TMP, "server_app"), os.path.join(TMP, "staging", "Campfire")
    for d in (target, staging):
        os.makedirs(d, exist_ok=True)
        for f in os.listdir(ROOT):
            if f.endswith(".py"):
                shutil.copy2(os.path.join(ROOT, f), d)
    with open(os.path.join(staging, "server.py"), "a") as f:
        f.write("\n# UPDATED-MARKER\n")
    write(os.path.join(target, "campfire.db"), "")            # the old server's data
    os.remove(os.path.join(target, "campfire.db"))
    old = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(1.5)"])   # "the old server"
    threading.Thread(target=old.wait, daemon=True).start()                          # reap it when it ends
    port = free_port()
    db = os.path.join(target, "campfire.db")
    args = ["--nogui", "--signup", "open", "--host", "127.0.0.1", "--port", str(port), "--db", db, "--files", db + "_files"]
    fin = subprocess.run([sys.executable, os.path.join(staging, "server.py"), "--finish-update", target,
                          "--wait-pid", str(old.pid), "--relaunch-args", json.dumps(args)], timeout=60)
    old.wait()
    assert fin.returncode == 0, read(os.path.join(target, "campfire_update.log"))
    assert "UPDATED-MARKER" in read(os.path.join(target, "server.py"))
    for _ in range(80):
        try:
            socket.create_connection(("127.0.0.1", port), 0.2).close()
            break
        except OSError:
            time.sleep(0.1)
    else:
        raise AssertionError("updated server didn't start: " + read(os.path.join(target, "campfire_update.log")))
    subprocess.run(["pkill", "-f", "--", f"--port {port}"], check=False)
    subprocess.run(["pkill", "-f", os.path.join(target, "server.py")], check=False)
    print("finish ok")


def test_git_mode():
    if not shutil.which("git"):
        print("git mode skipped (no git)")
        return
    origin, clone = os.path.join(TMP, "origin.git"), os.path.join(TMP, "clone")
    g = ["git", "-c", "user.name=t", "-c", "user.email=t@t", "-c", "init.defaultBranch=main"]
    subprocess.run(g + ["init", "-q", "--bare", origin], check=True)
    work = os.path.join(TMP, "work")
    subprocess.run(g + ["clone", "-q", origin, work], check=True, stderr=subprocess.DEVNULL)
    write(os.path.join(work, "server.py"), "v1")
    subprocess.run(g + ["-C", work, "add", "."], check=True)
    subprocess.run(g + ["-C", work, "commit", "-qm", "v1"], check=True)
    subprocess.run(g + ["-C", work, "push", "-q", "origin", "HEAD:main"], check=True)
    subprocess.run(g + ["clone", "-q", origin, clone], check=True)
    real_dir, real_frozen = su.APP_DIR, su.FROZEN
    su.APP_DIR, su.FROZEN = clone, False
    try:
        assert su.mode() == "git"
        r = su.check()
        assert not r["available"], r
        write(os.path.join(work, "server.py"), "v2")
        subprocess.run(g + ["-C", work, "commit", "-qam", "v2"], check=True)
        subprocess.run(g + ["-C", work, "push", "-q", "origin", "HEAD:main"], check=True)
        r = su.check()
        assert r["available"] and "1 new change" in r["detail"], r
        ok, out = su.git_pull()
        assert ok, out
        assert read(os.path.join(clone, "server.py")) == "v2"
        assert not su.check()["available"]
        assert su.describe().startswith("From source")
    finally:
        su.APP_DIR, su.FROZEN = real_dir, real_frozen
    print("git mode ok")


def test_gui():
    from PyQt6.QtCore import QSettings
    from PyQt6.QtWidgets import QApplication, QMessageBox
    import server_gui
    import server as core

    app = QApplication.instance() or QApplication(sys.argv)
    server_gui.prefs = QSettings(os.path.join(TMP, "srv.ini"), QSettings.Format.IniFormat)
    server_gui.prefs.setValue("update/check", False)
    QMessageBox.question = staticmethod(lambda *a, **k: QMessageBox.StandardButton.Yes)
    infos = []
    QMessageBox.information = staticmethod(lambda *a, **k: infos.append(a[2]))
    files = {"version.json": json.dumps({"version": "1.0.50"}).encode()}
    base, srv = fake_release(files)
    real_mode, real_version = su.mode, su.VERSION
    su.mode, su.VERSION = (lambda: "frozen"), "1.0.9"
    args = core.parse_args(["--signup", "open", "--port", str(free_port()), "--db", os.path.join(TMP, "gui.db"),
                            "--files", os.path.join(TMP, "gui_files")])
    w = server_gui.ServerWindow(args)
    try:
        w.show()
        w.update_base = base

        def pump(cond, timeout=8):
            end = time.time() + timeout
            while time.time() < end:
                app.processEvents()
                if cond():
                    return
                time.sleep(0.02)
            raise AssertionError("timed out")

        pump(lambda: w.st.running)
        assert w.version_label.text() == "Version 1.0.9"
        w.check_updates()
        pump(lambda: w.update_bar.isVisible())
        assert "update is available" in w.update_text.text() and w.update_btn.text() == "Update server"
        assert "update available" in w.update_state.text()
        w.update_bar.hide()
        files["version.json"] = json.dumps({"version": "1.0.9"}).encode()
        w.check_updates(manual=True)
        pump(lambda: infos)
        assert "up to date" in infos[-1] and not w.update_bar.isVisible()

        # files changed on disk while running (a git pull by hand) -> offer a restart
        w.code_stamp -= 100
        w.watch_code()
        assert w.update_bar.isVisible() and w.update_btn.text() == "Restart now"
        restarted = []
        w.restart = lambda cmd: restarted.append(cmd)
        w.do_update()
        assert restarted and restarted[0][-1] == "--updated"

        # "none" mode explains instead of failing
        su.mode = lambda: "none"
        w.update_btn.setText("Update server")
        w.do_update()
        assert "can't update itself" in infos[-1]
    finally:
        su.mode, su.VERSION = real_mode, real_version
        w.st.stop()
        w.close()
        srv.shutdown()
    print("server window updates ok")


def test_import_and_installed():
    """Importing an old server's folder, and updating an installed server with its setup."""
    from PyQt6.QtCore import QSettings
    from PyQt6.QtWidgets import QApplication, QMessageBox
    import server_gui
    import server as core

    app = QApplication.instance() or QApplication(sys.argv)
    server_gui.prefs = QSettings(os.path.join(TMP, "srv2.ini"), QSettings.Format.IniFormat)
    server_gui.prefs.setValue("update/check", False)
    QMessageBox.question = staticmethod(lambda *a, **k: QMessageBox.StandardButton.Yes)
    infos = []
    QMessageBox.information = staticmethod(lambda *a, **k: infos.append(a[2]))
    QMessageBox.warning = staticmethod(lambda *a, **k: infos.append("WARN " + a[2]))

    # an old server folder with an account, a channel, a message and an uploaded file
    old = os.path.join(TMP, "old_server")
    os.makedirs(os.path.join(old, "campfire_files"))
    st = core.Store(os.path.join(old, "campfire.db"))
    uid = st.create_user("oldtimer", b"salt", b"hash")
    st.add_room("#memories")
    st.add_message("#memories", uid, "remember this?", time.time())
    write(os.path.join(old, "campfire_files", "42_photo.png"), "PNG")
    st.db.close()

    new = os.path.join(TMP, "new_server")
    os.makedirs(new)
    args = core.parse_args(["--signup", "open", "--port", str(free_port()), "--db", os.path.join(new, "campfire.db"),
                            "--files", os.path.join(new, "campfire_files")])
    w = server_gui.ServerWindow(args)
    w.show()

    def pump(cond, timeout=10):
        end = time.time() + timeout
        while time.time() < end:
            app.processEvents()
            if cond():
                return
            time.sleep(0.02)
        raise AssertionError("timed out")

    try:
        pump(lambda: w.st.running)
        w.import_data(os.path.join(TMP, "nowhere"))
        assert "no campfire.db" in infos[-1]
        w.import_data(old)
        pump(lambda: infos and infos[-1].startswith("Imported") and w.st.running)
        assert "(1 files)" in infos[-1]
        st = core.Store(os.path.join(new, "campfire.db"))
        assert st.user_by_name("oldtimer") is not None
        assert "#memories" in st.rooms()
        assert any(m["text"] == "remember this?" for m in st.history("#memories"))
        assert os.path.exists(os.path.join(new, "campfire.db.before-import"))
        assert read(os.path.join(new, "campfire_files", "42_photo.png")) == "PNG"
        assert os.path.exists(os.path.join(old, "campfire.db"))           # the old folder is untouched

        # an installed server updates by downloading and running the new setup
        files = {"version.json": json.dumps({"version": "1.0.99"}).encode(), su.SETUP_NAME: b"MZ setup"}
        base, srv = fake_release(files)
        real = su.mode, su.VERSION, su.UPDATE_DIR
        su.mode, su.VERSION, su.UPDATE_DIR = (lambda: "installed"), "1.0.9", os.path.join(TMP, "_upd")
        try:
            w.update_base = base
            w.check_updates()
            pump(lambda: w.update_bar.isVisible())
            started = []
            w.restart = lambda cmd: started.append(cmd)
            w.do_update()
            pump(lambda: started)
            cmd = started[0]
            assert cmd[0].endswith(su.SETUP_NAME) and read(cmd[0]) == "MZ setup"
            assert "/relaunch=1" in cmd and "/SILENT" in cmd
        finally:
            su.mode, su.VERSION, su.UPDATE_DIR = real
            srv.shutdown()
    finally:
        w.st.stop()
        w.close()
    print("import + installed update ok")


def test_client_old_server():
    """The app explains an out-of-date server instead of 'Unknown command'."""
    from test_gui import cc, free_port as fp, login, pump, start_server
    from PyQt6.QtWidgets import QApplication, QMessageBox
    app = QApplication.instance() or QApplication(sys.argv)
    infos = []
    QMessageBox.information = staticmethod(lambda *a, **k: infos.append((a[1], a[2])))
    port = fp()
    server = start_server(port, os.path.join(TMP, "old.db"))
    try:
        a = login(port, "alice").chat
        assert a.server_has("profile") and a.server_version
        a.server_features = set()                     # pretend it's an old server
        dlg = cc.SettingsDialog(a)
        dlg.status_input.setText("hello there")
        dlg.save()
        assert infos and infos[-1][0] == "Server needs updating" and "Update server" in infos[-1][1]
        a.open_conv("#general")
        a.handle({"t": "error", "msg": "Unknown command 'profile'"}) if hasattr(a, "handle") else \
            a.on_message({"t": "error", "msg": "Unknown command 'profile'"})
        assert "server is out of date" in a.view.toPlainText()
    finally:
        server.terminate()
    print("client old-server message ok")


if __name__ == "__main__":
    test_copy_over()
    test_check_and_download()
    test_git_mode()
    test_finish_end_to_end()
    test_gui()
    test_import_and_installed()
    test_client_old_server()
    print("server update tests passed")
