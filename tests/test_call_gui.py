"""Group DM calls between real client windows through the server (sound card and camera faked).
Run:  QT_QPA_PLATFORM=offscreen python tests/test_call_gui.py [screenshot-dir]"""

import os
import sys
import time

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "tests"))

import test_video_gui  # noqa: E402,F401  (fakes the sound card and camera)
from test_gui import TMP, cc, free_port, login, pump, start_server  # noqa: E402
from test_voice import dominant_freq, tone  # noqa: E402
from PyQt6.QtWidgets import QApplication  # noqa: E402

import voice  # noqa: E402


def main(shots=None):
    app = QApplication(sys.argv)
    app.setStyleSheet(cc.STYLE)
    port = free_port()
    server = start_server(port, os.path.join(TMP, "calls.db"))
    try:
        a = login(port, "alice").chat
        b = login(port, "bob").chat
        c = login(port, "carl").chat
        o = login(port, "otto").chat          # not in the group
        a.conn.send(t="group_create", name="Squad", members=["bob", "carl"])
        pump(lambda: len(b.groups) == 1 and len(c.groups) == 1 and a.current.startswith("g:"))
        gid = next(iter(a.groups))
        key = f"g:{gid}"
        assert a.call_bar.isVisible() and a.call_btn.text() == "Start call"

        # alice starts the call from the group chat
        a.call_button()
        pump(lambda: a.my_voice == key and b.ring is not None and c.ring is not None)
        assert "alice" in b.ring.findChildren(cc.QLabel)[1].text()
        assert o.ring is None and not o.calls
        if shots:
            b.ring.grab().save(os.path.join(shots, "ring.png"))

        # bob answers, carl ignores
        b.ring.accept()
        c.ring.reject()
        pump(lambda: b.my_voice == key and b.current == key)
        pump(lambda: [u["name"] for u in a.calls.get(gid, [])] == ["alice", "bob"])
        assert "In call" in a.call_label.text() and a.call_btn.text() == "Leave call"
        pump(lambda: "\U0001f50a 2" in c.dm_list.item(0).text())          # carl sees a call going on
        c.open_conv(key)
        assert c.call_btn.text() == "Join call"

        # audio flows inside the call
        for i in range(0, 24000, voice.FRAME):
            b.engine.process_mic(tone(523, 0.5)[i:i + voice.FRAME])
            QApplication.processEvents()
            time.sleep(0.003)
        pump(lambda: len(a.engine.mixer.queues.get("bob", [])) >= 5)
        assert abs(dominant_freq(a.engine.mixer.mix(voice.FRAME * 5)) - 523) < 6

        # camera inside the call shows on bob's stage
        a.cam_btn.setChecked(True)
        pump(lambda: ("alice", "camera") in b.tiles)
        pump(lambda: b.tiles[("alice", "camera")].image is not None, timeout=8)
        assert "Squad" in b.stage_title.text()

        if shots:
            b.show_chat()
            b.open_conv(key)
            b.update_speaking()
            b.resize(1150, 700)
            pump(lambda: True, 0.4)
            b.grab().save(os.path.join(shots, "call.png"))

        # carl joins late from the call bar
        c.call_button()
        pump(lambda: len(a.calls.get(gid, [])) == 3)

        # leaving: last one out ends it
        b.leave_voice()
        c.leave_voice()
        a.call_button()                                   # "Leave call"
        pump(lambda: not a.calls and not b.calls and a.my_voice is None)
        assert a.call_btn.text() == "Start call" and not a.cam_btn.isChecked()
        print("call gui tests passed")
    finally:
        server.terminate()


if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else None)
