"""File sharing between real client windows through the server.
Run:  QT_QPA_PLATFORM=offscreen python tests/test_files_gui.py [screenshot-dir]"""

import os
import sys

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "tests"))

from test_gui import TMP, cc, free_port, login, pump, start_server  # noqa: E402
from test_video import test_frame  # noqa: E402
from PyQt6.QtCore import QUrl  # noqa: E402
from PyQt6.QtWidgets import QApplication  # noqa: E402


def main(shots=None):
    app = QApplication(sys.argv)
    app.setStyleSheet(cc.STYLE)
    port = free_port()
    server = start_server(port, os.path.join(TMP, "files.db"))
    try:
        a = login(port, "alice").chat
        b = login(port, "bob").chat
        c = login(port, "carl").chat
        b.open_conv("#general")

        # upload with a caption taken from the message box
        data = os.urandom(5 * 1024 * 1024 + 123)
        src = os.path.join(TMP, "holiday.zip")
        open(src, "wb").write(data)
        a.input.setText("photos from the trip")
        job = a.upload_file(src)
        assert job is not None and a.input.text() == ""
        assert a.transfer_box.isVisible()
        pump(lambda: any((m.get("file") or {}).get("name") == "holiday.zip" for m in b.convs["#general"]), timeout=15)
        msg = next(m for m in b.convs["#general"] if (m.get("file") or {}).get("name") == "holiday.zip")
        assert msg["text"] == "photos from the trip" and msg["file"]["size"] == len(data)
        page = b.view.toHtml()
        assert "holiday.zip" in page and "campfire://download/" in page and "5.0 MB" in page

        # bob downloads it (the Download link, with the save dialog skipped)
        dest = os.path.join(TMP, "bob_holiday.zip")
        b.download_file(msg["file"]["id"], dest=dest)
        pump(lambda: os.path.exists(dest) and msg["file"]["id"] in b.downloaded, timeout=15)
        assert open(dest, "rb").read() == data
        b.redraw()
        assert "campfire://open/" in b.view.toHtml()

        # pictures show a preview for everyone
        img_path = os.path.join(TMP, "sunset.png")
        test_frame(1600, 900, 5).save(img_path)
        a.upload_file(img_path)
        pump(lambda: any((m.get("file") or {}).get("name") == "sunset.png" for m in b.convs["#general"]))
        fid = next(m["file"]["id"] for m in b.convs["#general"] if (m.get("file") or {}).get("name") == "sunset.png")
        pump(lambda: f"f{fid}" in b.view.thumbs, timeout=10)
        assert b.view.thumbs[f"f{fid}"].width() == 360
        assert f"campfire-img:f{fid}" in b.view.toHtml()

        # files in a DM stay between the two people
        a.open_dm("bob")
        a.upload_file(src)
        pump(lambda: any(m.get("file") for m in b.convs.get("@alice", [])), timeout=15)
        dm_fid = next(m["file"]["id"] for m in b.convs["@alice"] if m.get("file"))
        assert not any(m.get("file") for msgs in c.convs.values() for m in msgs if (m.get("file") or {}).get("id") == dm_fid)

        # too big is refused before anything is sent (5 GB limit), empty files too
        empty = os.path.join(TMP, "empty.txt"); open(empty, "wb").close()
        assert a.upload_file(empty) is None and "is empty" in a.view.toPlainText()
        orig = cc.transfers.MAX_FILE
        cc.transfers.MAX_FILE = 1000
        assert a.upload_file(src) is None and "at most 5 GB" in a.view.toPlainText()
        cc.transfers.MAX_FILE = orig

        if shots:
            b.open_conv("#general")
            b.resize(1150, 700)
            pump(lambda: True, 0.5)
            b.grab().save(os.path.join(shots, "files.png"))
            # a progress row mid-upload, for the screenshot
            big = os.path.join(TMP, "big.bin"); open(big, "wb").write(os.urandom(1024))
            j = a.upload_file(big)
            j.row.update_progress(1_300_000_000, 4_200_000_000)
            j.row.samples = [(0, 0), (100, 1_300_000_000)]
            j.row.update_progress(1_300_000_000, 4_200_000_000)
            a.open_conv("#general"); a.resize(1150, 700)
            pump(lambda: True, 0.2)
            a.grab().save(os.path.join(shots, "upload_progress.png"))

        # admin deletes a file: the card turns into "(file deleted)"
        a.admin("delfile", fid=fid)
        pump(lambda: f"f{fid}" not in b.view.thumbs and "(file deleted)" in b.view.toPlainText())

        # clicking an outside link isn't treated as a file action
        b.link_clicked(QUrl("campfire://download/999999"))       # unknown file: nothing happens
        print("files gui tests passed")
    finally:
        server.terminate()


if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else None)
