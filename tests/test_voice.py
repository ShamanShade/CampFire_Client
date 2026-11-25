"""Voice engine tests with synthetic audio (no sound card needed).
Run:  python tests/test_voice.py"""

import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import numpy as np  # noqa: E402

import voice  # noqa: E402


def tone(freq, seconds, amp=8000):
    t = np.arange(int(voice.RATE * seconds)) / voice.RATE
    return (np.sin(2 * np.pi * freq * t) * amp).astype(np.int16)


def dominant_freq(pcm):
    spec = np.abs(np.fft.rfft(pcm.astype(np.float32)))
    return np.fft.rfftfreq(len(pcm), 1 / voice.RATE)[spec.argmax()]


def engine():
    sent = []
    e = voice.VoiceEngine(sent.append)
    e.running = True
    import threading
    e.sender = threading.Thread(target=e._send_loop, daemon=True)
    e.sender.start()
    return e, sent


def wait(cond, timeout=3):
    end = time.time() + timeout
    while time.time() < end:
        if cond():
            return True
        time.sleep(0.01)
    raise AssertionError("timed out")


def feed(e, pcm):
    """Feed mic audio at roughly real time, so the send queue never overflows."""
    for i in range(0, len(pcm), voice.FRAME):
        e.process_mic(pcm[i:i + voice.FRAME])
        time.sleep(0.002)


def test_round_trip():
    a, sent = engine()
    b, _ = engine()
    feed(a, tone(440, 1.0))
    wait(lambda: len(sent) >= 48)
    sizes = [len(p) for p in sent]
    assert max(sizes) < 300, sizes                 # small packets: ~30 kbps
    for p in sent:
        b.receive("alice", p)
    out = b.mixer.mix(voice.RATE // 2)
    assert abs(dominant_freq(out) - 440) < 5, dominant_freq(out)
    assert np.abs(out).max() > 4000
    assert "alice" in b.speaking()
    a.stop(); b.stop()


def test_mixing_two_people():
    a, sa = engine()
    c, sc = engine()
    b, _ = engine()
    feed(a, tone(300, 0.5))
    feed(c, tone(900, 0.5))
    wait(lambda: len(sa) >= 23 and len(sc) >= 23)
    for p, q in zip(sa, sc):
        b.receive("a", p)
        b.receive("c", q)
    out = b.mixer.mix(voice.RATE // 4)
    spec = np.abs(np.fft.rfft(out.astype(np.float32)))
    freqs = np.fft.rfftfreq(len(out), 1 / voice.RATE)
    peaks = sorted(freqs[np.argsort(spec)[-2:]])
    assert abs(peaks[0] - 300) < 10 and abs(peaks[1] - 900) < 10, peaks
    # per-person volume
    b.set_volume("c", 0.0)
    for q in sc[:10]:
        b.receive("c", q)
    for p in sa[:10]:
        b.receive("a", p)
    out = b.mixer.mix(voice.FRAME * 6)
    assert abs(dominant_freq(out) - 300) < 10
    for e in (a, b, c):
        e.stop()


def test_gating():
    e, sent = engine()
    feed(e, tone(440, 0.3, amp=20))                # very quiet: below -45 dB, nothing sent
    time.sleep(0.2)
    assert sent == [], len(sent)
    e.muted = True
    feed(e, tone(440, 0.3))
    time.sleep(0.2)
    assert sent == []
    e.muted = False
    e.mode = "ptt"
    feed(e, tone(440, 0.3))
    time.sleep(0.2)
    assert sent == []                              # push-to-talk not held
    e.ptt_down = True
    feed(e, tone(440, 0.3))
    wait(lambda: len(sent) >= 10)
    assert e.transmitting()
    e.stop()


def test_jitter_buffer():
    m = voice.Mixer()
    f = tone(440, 0.02)
    m.push("x", f)
    assert not m.mix(voice.FRAME).any()            # waits for PREFILL frames first
    for _ in range(30):
        m.push("x", f)
    assert len(m.queues["x"]) == voice.MAX_QUEUE   # old audio dropped instead of lagging
    out = m.mix(voice.FRAME * 3)
    assert np.abs(out).max() > 4000
    # odd output sizes still line up
    assert len(m.mix(441)) == 441 and len(m.mix(1500)) == 1500


if __name__ == "__main__":
    test_jitter_buffer()
    test_gating()
    test_round_trip()
    test_mixing_two_people()
    print("voice tests passed")
