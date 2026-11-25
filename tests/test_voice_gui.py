"""Two client windows in a voice channel, audio flowing through the real server.
The sound card is replaced by direct calls, so this runs headless.
Run:  QT_QPA_PLATFORM=offscreen python tests/test_voice_gui.py [screenshot-dir]"""

import os
import sys
import time

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "tests"))

from test_gui import TMP, cc, free_port, login, pump, start_server  # noqa: E402
from test_voice import dominant_freq, tone  # noqa: E402
from PyQt6.QtWidgets import QApplication  # noqa: E402

import voice  # noqa: E402

opened = []


def fake_start(self, input_device=None, output_device=None):
    """No sound card here: start the sender thread only and pretend there's a mic."""
    import threading
    self.running = True
    self.has_mic = True
    self.sender = threading.Thread(target=self._send_loop, daemon=True)
    self.sender.start()
    opened.append((input_device, output_device))
    return []


voice.VoiceEngine.start = fake_start


def chan(win, name):
    return next(c for c in win.voice_channels if c["name"] == name)


def main(shots=None):
    app = QApplication(sys.argv)
    app.setStyleSheet(cc.STYLE)
    port = free_port()
    server = start_server(port, os.path.join(TMP, "voice.db"))      # alice is admin
    try:
        a = login(port, "alice").chat
        b = login(port, "bob").chat
        c = login(port, "carl").chat
        pump(lambda: len(a.voice_channels) == 2 and a.voice_tree.topLevelItemCount() == 2)

        # join by clicking the channel
        a.voice_clicked(a.voice_tree.topLevelItem(0))
        pump(lambda: a.my_voice == "Lounge" and a.engine is not None and a.voice_bar.isVisible())
        b.join_voice("Lounge")
        c.join_voice("Gaming")
        pump(lambda: [u["name"] for u in chan(a, "Lounge")["users"]] == ["alice", "bob"]
             and [u["name"] for u in chan(a, "Gaming")["users"]] == ["carl"])
        assert a.voice_tree.topLevelItem(0).childCount() == 2

        # bob talks: alice hears a 440 Hz tone, carl (other channel) hears nothing
        for i in range(0, 48000, voice.FRAME):
            b.engine.process_mic(tone(440, 1.0)[i:i + voice.FRAME])
            QApplication.processEvents()
            time.sleep(0.003)
        pump(lambda: len(a.engine.mixer.queues.get("bob", [])) >= 5)
        heard = a.engine.mixer.mix(voice.FRAME * 5)
        assert abs(dominant_freq(heard) - 440) < 5
        assert not c.engine.mixer.queues
        a.update_speaking()
        assert a.voice_items["bob"].font(0).bold()                 # speaking indicator

        # mute / deafen show up for others
        b.mute_btn.setChecked(True)
        pump(lambda: chan(a, "Lounge")["users"][1]["muted"])
        a.refresh_voice()
        assert "\U0001f507" in a.voice_items["bob"].text(0)
        b.engine.process_mic(tone(440, 0.2))
        time.sleep(0.2)
        n_before = len(a.engine.mixer.queues.get("bob", []))
        pump(lambda: True, 0.3)
        assert len(a.engine.mixer.queues.get("bob", [])) == n_before
        b.mute_btn.setChecked(False)
        a.deaf_btn.setChecked(True)
        pump(lambda: chan(b, "Lounge")["users"][0]["deaf"])

        if shots:
            a.deaf_btn.setChecked(False)
            pump(lambda: True, 0.3)
            a.voice_items["bob"].setForeground(0, cc.QColor(cc.SPEAKING_COLOR))
            a.resize(1150, 760)
            a.grab().save(os.path.join(shots, "voice.png"))

        # switching channels
        b.join_voice("Gaming")
        pump(lambda: [u["name"] for u in chan(a, "Gaming")["users"]] == ["carl", "bob"])

        # admin disconnects carl; carl's client stops voice and says why
        a.admin("voicekick", user="carl")
        pump(lambda: c.engine is None and not c.voice_bar.isVisible()
             and "disconnected you from voice" in c.view.toPlainText())

        # leaving and logging out clean up
        a.leave_voice()
        pump(lambda: a.engine is None and chan(b, "Lounge")["users"] == [])
        b.leave("bye")
        pump(lambda: chan(a, "Gaming")["users"] == [])

        # push-to-talk mode: the key only transmits while held, and isn't typed into the chat box
        cc.settings.setValue("voice/mode", "ptt")
        a.join_voice("Lounge")
        pump(lambda: a.engine is not None)
        assert a.engine.mode == "ptt" and a.ptt_btn.isVisible() is not None
        from PyQt6.QtCore import QEvent
        from PyQt6.QtGui import QKeyEvent
        a.input.setFocus()
        press = QKeyEvent(QEvent.Type.KeyPress, cc.Qt.Key.Key_QuoteLeft, cc.Qt.KeyboardModifier.NoModifier, "`")
        QApplication.sendEvent(a.input, press)
        assert a.engine.ptt_down and a.input.text() == ""
        release = QKeyEvent(QEvent.Type.KeyRelease, cc.Qt.Key.Key_QuoteLeft, cc.Qt.KeyboardModifier.NoModifier, "`")
        QApplication.sendEvent(a.input, release)
        assert not a.engine.ptt_down
        cc.settings.setValue("voice/mode", "vad")
        print("voice gui tests passed")
    finally:
        server.terminate()


if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else None)
