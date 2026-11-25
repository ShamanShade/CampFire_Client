"""Two clients in a voice channel sharing camera and screen through the real server.
The camera is replaced by a fake that paints frames; the screen is the virtual offscreen one.
Run:  QT_QPA_PLATFORM=offscreen python tests/test_video_gui.py [screenshot-dir]"""

import os
import sys

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "tests"))

import test_voice_gui  # noqa: E402,F401  (replaces the sound card)
from test_gui import TMP, cc, free_port, login, pump, start_server  # noqa: E402
from test_video import test_frame  # noqa: E402
from PyQt6.QtCore import QObject, QPoint, QTimer, pyqtSignal  # noqa: E402
from PyQt6.QtGui import QImage  # noqa: E402
from PyQt6.QtWidgets import QApplication  # noqa: E402

import video  # noqa: E402


class FakeCamera(QObject):
    frame = pyqtSignal(QImage)
    error = pyqtSignal(str)

    def __init__(self, fps=15):
        super().__init__()
        self.i = 0
        self.timer = QTimer(self)
        self.timer.timeout.connect(self.tick)

    def start(self, device_id=None):
        self.timer.start(66)
        return True

    def tick(self):
        self.i += 1
        self.frame.emit(test_frame(1280, 720, self.i))

    def stop(self):
        self.timer.stop()


video.CameraSource = FakeCamera


def user_state(win, name):
    return next(u for ch in win.voice_channels for u in ch["users"] if u["name"] == name)


def main(shots=None):
    app = QApplication(sys.argv)
    app.setStyleSheet(cc.STYLE)
    port = free_port()
    server = start_server(port, os.path.join(TMP, "video.db"))
    try:
        a = login(port, "alice").chat
        b = login(port, "bob").chat
        c = login(port, "carl").chat
        for w in (a, b):
            w.join_voice("Lounge")
        c.join_voice("Gaming")
        pump(lambda: a.my_voice and b.my_voice and c.my_voice)

        # camera needs a voice channel; in one, it starts and the stage opens
        a.cam_btn.setChecked(True)
        pump(lambda: user_state(b, "alice")["camera"])
        assert a.center_stack.currentIndex() == 1 and ("alice", "camera") in a.tiles
        pump(lambda: a.tiles[("alice", "camera")].image is not None)          # own preview

        # bob sees it automatically
        pump(lambda: ("alice", "camera") in b.tiles)
        tile = b.tiles[("alice", "camera")]
        pump(lambda: tile.image is not None, timeout=8)
        px = tile.image.pixelColor(QPoint(tile.image.width() // 2, 20))
        assert abs(px.red() - 0x20) < 30 and abs(px.blue() - 0x40) < 30, px.name()
        assert "\U0001f4f7" in b.voice_tree.topLevelItem(0).child(0).text(0)
        # carl, in another channel, gets nothing
        assert not c.tiles and c.receiver is None

        # screen share: bob has to click Watch
        a.screen_btn.setChecked(True)
        pump(lambda: ("alice", "screen") in b.tiles)
        st = b.tiles[("alice", "screen")]
        assert not st.watching and st.image is None
        b.show_stage()
        if shots:
            b.resize(1150, 700)
            pump(lambda: True, 0.5)
            b.grab().save(os.path.join(shots, "video_before_watch.png"))
        b.watch_stream("alice", "screen")
        pump(lambda: st.watching and st.image is not None, timeout=8)
        assert st.image.width() <= 1280

        # pop-out window mirrors the stream
        b.pop_out(("alice", "screen"))
        win = b.popouts[("alice", "screen")]
        pump(lambda: win.image is not None)
        if shots:
            b.resize(1150, 700)
            pump(lambda: True, 0.5)
            b.grab().save(os.path.join(shots, "video.png"))
        win.close()
        assert ("alice", "screen") not in b.popouts

        # stop watching / stop sharing
        b.unwatch_stream("alice", "screen")
        assert ("alice", "screen") not in b.watching_sent
        a.screen_btn.setChecked(False)
        pump(lambda: ("alice", "screen") not in b.tiles)

        # switching channel keeps the camera on, in the new channel
        a.join_voice("Gaming")
        pump(lambda: ("alice", "camera") in c.tiles and ("alice", "camera") not in b.tiles)
        pump(lambda: c.tiles[("alice", "camera")].image is not None, timeout=8)

        # leaving voice turns video off for everyone
        a.leave_voice()
        pump(lambda: not c.tiles and not a.publishers and not a.tiles)
        assert not a.cam_btn.isChecked()
        print("video gui tests passed")
    finally:
        server.terminate()


if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else None)
