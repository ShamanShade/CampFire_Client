"""Login troubleshooting paths. Run:  QT_QPA_PLATFORM=offscreen python tests/test_login.py"""

import os
import socket
import sys
import threading
import time

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "tests"))

from test_gui import TMP, cc, free_port, pump, start_server  # noqa: E402  (also isolates settings)
from PyQt6.QtWidgets import QApplication  # noqa: E402


def old_style_server(port):
    """Behaves like the original server.py: says USERNAME with no newline and waits."""
    srv = socket.socket()
    srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    srv.bind(("127.0.0.1", port))
    srv.listen()

    def run():
        c, _ = srv.accept()
        c.send(b"USERNAME")
        time.sleep(30)

    threading.Thread(target=run, daemon=True).start()
    return srv


def main():
    app = QApplication(sys.argv)

    # 1. one app for everyone: the normal client finds a server on this PC by itself
    #    ("public" server on 127.0.0.2 stands in for your internet address; "local" on 127.0.0.1)
    import subprocess
    cc.LOCAL_HOSTS.discard("127.0.0.2")
    port = free_port()

    def run_server(host, db):
        p = subprocess.Popen([sys.executable, os.path.join(ROOT, "server.py"), "--nogui", "--signup", "open", "--host", host,
                              "--port", str(port), "--db", os.path.join(TMP, db), "--files",
                              os.path.join(TMP, db + "_files"), "--admin", "alice"],
                             stdout=subprocess.DEVNULL, stderr=subprocess.STDOUT)
        for _ in range(50):
            try:
                socket.create_connection((host, port), 0.2).close()
                return p
            except OSError:
                time.sleep(0.1)
        raise RuntimeError("server didn't start")

    public = run_server("127.0.0.2", "public.db")
    local = run_server("127.0.0.1", "local.db")
    try:
        cc.SERVER_HOST, cc.SERVER_PORT = "127.0.0.2", port
        w = cc.LoginWindow()
        w.user_input.setText("alice"); w.pass_input.setText("secret")
        w.login()
        pump(lambda: w.chat is not None)
        assert w.chat.conn.local and w.chat.conn.host == "127.0.0.1"
        assert "server on this PC" in w.chat.windowTitle() and w.chat.is_admin
        # the local server stops: reconnecting looks again and ends up on the public address
        local.terminate(); local.wait()
        pump(lambda: w.chat.conn.host == "127.0.0.2" and w.chat.statusBar().currentMessage() == "Connected",
             timeout=15)
        assert not w.chat.conn.local and "server on this PC" not in w.chat.windowTitle()
        w.chat.leave("done")

        # something else using Campfire's port on this PC is ignored (it has to talk like Campfire)
        impostor = socket.socket()
        impostor.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        impostor.bind(("127.0.0.1", port)); impostor.listen()
        threading.Thread(target=lambda: [impostor.accept()[0].send(b"HTTP/1.0 200 OK\r\n\r\n") for _ in range(3)],
                         daemon=True).start()
        t0 = time.time()
        w = cc.LoginWindow()
        w.user_input.setText("alice"); w.pass_input.setText("secret")
        w.login()
        pump(lambda: w.chat is not None)
        assert w.chat.conn.host == "127.0.0.2" and time.time() - t0 < 5
        w.chat.leave("done")
        impostor.close()
    finally:
        public.terminate()
        local.terminate()

    cc.SERVER_HOST = "127.0.0.1"
    port = free_port()
    server = start_server(port, os.path.join(TMP, "login.db"))      # started with --admin alice
    try:
        cc.SERVER_PORT = port

        w = cc.LoginWindow()                               # make alice's account on this server
        w.user_input.setText("alice"); w.pass_input.setText("secret")
        w.login()
        pump(lambda: w.chat is not None)
        w.chat.leave("done")

        # things waiting for you at login show up straight away (regression: they used to be lost)
        z = cc.LoginWindow()
        z.user_input.setText("zed"); z.pass_input.setText("secret")
        z.login()
        pump(lambda: z.chat is not None)
        z.chat.conn.send(t="friend_request", to="alice")
        z.chat.conn.send(t="group_create", name="Waiting", members=["alice"])
        pump(lambda: len(z.chat.groups) == 1)
        w2 = cc.LoginWindow()
        w2.user_input.setText("alice"); w2.pass_input.setText("secret")
        w2.login()
        pump(lambda: w2.chat is not None)
        pump(lambda: w2.chat.req_list.count() == 1 and len(w2.chat.groups) == 1
             and len(w2.chat.voice_channels) == 2, timeout=3)
        w2.chat.leave("done")
        z.chat.leave("done")
    finally:
        server.terminate()

    # 2. an old server that never answers properly: clear message instead of hanging forever
    cc.HELLO_TIMEOUT = 1
    port = free_port()
    old = old_style_server(port)
    cc.SERVER_PORT = port
    w = cc.LoginWindow()
    w.user_input.setText("alice"); w.pass_input.setText("secret")
    w.login()
    pump(lambda: w.btn.isEnabled(), timeout=5)
    assert "NEW server.py" in w.status.text(), w.status.text()
    old.close()

    # 3. nothing listening anywhere: a clear message
    cc.SERVER_HOST, cc.SERVER_PORT = "127.0.0.2", free_port()
    w = cc.LoginWindow()
    w.user_input.setText("alice"); w.pass_input.setText("secret")
    w.login()
    pump(lambda: w.btn.isEnabled(), timeout=12)
    assert "Is the server running?" in w.status.text(), w.status.text()
    print("login tests passed")


if __name__ == "__main__":
    main()
