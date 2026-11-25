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

    # 1. the admin client connects to this PC and logs the admin in
    import campfire_admin  # noqa: F401  (sets cc.SERVER_HOST / LOGIN_NOTE)
    assert cc.SERVER_HOST == "127.0.0.1" and "Admin client" in cc.LOGIN_NOTE
    port = free_port()
    server = start_server(port, os.path.join(TMP, "login.db"))      # started with --admin alice
    try:
        cc.SERVER_PORT = port
        w = cc.LoginWindow()
        w.user_input.setText("alice"); w.pass_input.setText("secret")
        w.login()
        pump(lambda: w.chat is not None)
        assert w.chat.is_admin and "(admin)" in w.chat.windowTitle()
        w.chat.leave("done")
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

    # 3. public address with nothing listening: tells you about campfire_admin.py
    cc.SERVER_HOST, cc.SERVER_PORT = "127.0.0.2", free_port()
    cc.LOCAL_HOSTS.discard("127.0.0.2")
    w = cc.LoginWindow()
    w.user_input.setText("alice"); w.pass_input.setText("secret")
    w.login()
    pump(lambda: w.btn.isEnabled(), timeout=12)
    assert "campfire_admin.py" in w.status.text(), w.status.text()
    print("login tests passed")


if __name__ == "__main__":
    main()
