"""Video engine tests with synthetic frames (no camera needed).
Run:  QT_QPA_PLATFORM=offscreen python tests/test_video.py"""

import os
import sys
import time

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import numpy as np  # noqa: E402
from PyQt6.QtCore import QPoint  # noqa: E402
from PyQt6.QtGui import QColor, QImage, QPainter  # noqa: E402
from PyQt6.QtWidgets import QApplication  # noqa: E402

import video  # noqa: E402


def test_frame(w, h, i):
    img = QImage(w, h, QImage.Format.Format_RGB32)
    img.fill(QColor("#203040"))
    p = QPainter(img)
    p.fillRect((i * 20) % (w - 100), h // 3, 100, h // 3, QColor("#ff4000"))   # moving orange block
    p.end()
    return img


def pump(cond, timeout=5):
    end = time.time() + timeout
    while time.time() < end:
        QApplication.processEvents()
        if cond():
            return
        time.sleep(0.005)
    raise AssertionError("timed out")


def main():
    app = QApplication(sys.argv)

    # conversion keeps colours and makes sizes even / scaled down
    rgb = video.qimage_to_rgb(test_frame(1921, 1081, 0), 1280, 720)
    assert rgb.shape[2] == 3 and rgb.shape[0] % 2 == 0 and rgb.shape[1] % 2 == 0 and rgb.shape[1] <= 1280
    back = video.rgb_to_qimage(rgb)
    assert back.width() == rgb.shape[1]

    # publisher -> (base64 packets) -> receiver
    packets = []
    pub = video.Publisher("camera", lambda kind, d, k: packets.append((kind, d, k)), (640, 360), 15, 600_000)
    for i in range(20):
        pub.push(test_frame(1280, 720, i))
        time.sleep(0.03)
    pump(lambda: len(packets) >= 15)
    assert packets[0][2] is True, "first frame must be a keyframe"
    assert sum(len(d) for _, d, _ in packets[1:]) / (len(packets) - 1) < 20000   # deltas are small

    got = []
    rx = video.Receiver()
    rx.frame.connect(lambda u, k, img: got.append((u, k, img)))
    for kind, d, k in packets:
        rx.push("bob", kind, d, k)
    pump(lambda: len(got) >= len(packets) - 1)
    user, kind, img = got[-1]
    assert (user, kind) == ("bob", "camera") and (img.width(), img.height()) == (640, 360)
    c = img.pixelColor(QPoint(img.width() // 2, img.height() // 2 - 60))
    assert abs(c.red() - 0x20) < 30 and abs(c.blue() - 0x40) < 30, c.name()      # background colour survived

    # a viewer joining mid-stream skips frames until a keyframe, then decodes
    got.clear()
    rx2 = video.Receiver()
    rx2.frame.connect(lambda u, k, img: got.append(img))
    for kind, d, k in packets[5:10]:            # deltas only: nothing decodable
        rx2.push("bob", kind, d, k)
    time.sleep(0.3); QApplication.processEvents()
    n0 = len(got)
    pub.request_keyframe()
    packets.clear()
    pub.push(test_frame(1280, 720, 3))
    pump(lambda: packets)
    assert packets[0][2] is True
    rx2.push("bob", "camera", packets[0][1], True)
    pump(lambda: len(got) > n0)

    # screen source works headless (captures the virtual screen)
    shots = []
    src = video.ScreenSource(0, 5)
    src.frame.connect(shots.append)
    assert src.start()
    pump(lambda: shots)
    src.stop()
    assert shots[0].width() > 0

    pub.stop(); rx.stop(); rx2.stop()
    print("video tests passed")


if __name__ == "__main__":
    main()
