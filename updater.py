"""
Update check for the installed Windows app.

When Campfire starts it reads version.json from the latest GitHub release. If that's newer
than this copy it asks: Update now / Later / Skip this version. "Update now" downloads
Campfire-Setup.exe, runs it silently (it closes Campfire, installs over it and opens it again).

Campfire Admin's installer is a private draft that the app can't download by itself, so for
the admin build "Update now" opens the Releases page in the browser instead.

Only installed builds check (copies run from source are updated with git pull instead).
"""

from __future__ import annotations

import json
import os
import sys
import tempfile

from PyQt6.QtCore import QObject, QProcess, QSettings, QTimer, QUrl
from PyQt6.QtGui import QDesktopServices
from PyQt6.QtNetwork import QNetworkAccessManager, QNetworkReply, QNetworkRequest
from PyQt6.QtWidgets import QApplication, QMessageBox, QProgressDialog

try:
    from _version import VERSION            # written by the Windows build
except ImportError:
    VERSION = "dev"

REPO = "ShamanShade/CampFire_Client"
RELEASE_BASE = f"https://github.com/{REPO}/releases/download/latest"
RELEASES_PAGE = f"https://github.com/{REPO}/releases"


def parse(version: str):
    try:
        return tuple(int(x) for x in version.strip().lstrip("v").split("."))
    except ValueError:
        return None


def is_newer(remote: str, local: str) -> bool:
    r, l = parse(remote), parse(local)
    return r is not None and l is not None and r > l


def can_update() -> bool:
    """Installed builds only: a frozen app with a real version number."""
    return bool(getattr(sys, "frozen", False)) and parse(VERSION) is not None


class Updater(QObject):
    def __init__(self, parent, settings: QSettings, admin=False):
        super().__init__(parent)
        self.window, self.settings, self.admin = parent, settings, admin
        self.net = QNetworkAccessManager(self)
        self.busy = False
        self.base = RELEASE_BASE
        self.releases_page = RELEASES_PAGE

    # -- checking
    def check(self, manual=False):
        """Look for a newer version. manual=True also says "you're up to date"."""
        if self.busy:
            return
        if not manual and not self.settings.value("update/check", True, type=bool):
            return
        self.busy = True
        req = QNetworkRequest(QUrl(f"{self.base}/version.json"))
        req.setAttribute(QNetworkRequest.Attribute.CacheLoadControlAttribute,
                         QNetworkRequest.CacheLoadControl.AlwaysNetwork)
        reply = self.net.get(req)
        reply.finished.connect(lambda: self.checked(reply, manual))

    def checked(self, reply, manual):
        self.busy = False
        ok = reply.error() == QNetworkReply.NetworkError.NoError
        data = bytes(reply.readAll())
        reply.deleteLater()
        try:
            info = json.loads(data) if ok else None
        except ValueError:
            info = None
        if not info or "version" not in info:
            if manual:
                QMessageBox.information(self.window, "Updates", "Couldn't check for updates right now.")
            return
        latest = info["version"]
        if not is_newer(latest, VERSION):
            if manual:
                QMessageBox.information(self.window, "Updates", f"You have the latest version ({VERSION}).")
            return
        if not manual and self.settings.value("update/skip", "") == latest:
            return
        self.offer(latest, info)

    def offer(self, latest, info):
        name = "Campfire Admin" if self.admin else "Campfire"
        box = QMessageBox(self.window)
        box.setWindowTitle("Update available")
        box.setIcon(QMessageBox.Icon.Information)
        extra = ("\n\nThe admin installer is private, so this opens your Releases page - download "
                 "Campfire-Admin-Setup.exe from the \"Campfire Admin (private)\" draft while logged in to GitHub."
                 if self.admin else "")
        box.setText(f"{name} {latest} is available. You have {VERSION}.{extra}")
        now = box.addButton("Update now", QMessageBox.ButtonRole.AcceptRole)
        box.addButton("Later", QMessageBox.ButtonRole.RejectRole)
        skip = box.addButton("Skip this version", QMessageBox.ButtonRole.DestructiveRole)
        box.exec()
        clicked = box.clickedButton()
        if clicked is skip:
            self.settings.setValue("update/skip", latest)
        elif clicked is now:
            if self.admin:
                QDesktopServices.openUrl(QUrl(self.releases_page))
            else:
                self.download(info.get("setup", "Campfire-Setup.exe"), latest)

    # -- updating
    def download(self, setup_name, latest):
        self.target = os.path.join(tempfile.gettempdir(), f"Campfire-Setup-{latest}.exe")
        self.part = open(self.target + ".part", "wb")
        self.progress = QProgressDialog(f"Downloading Campfire {latest}...", "Cancel", 0, 100, self.window)
        self.progress.setWindowTitle("Updating Campfire")
        self.progress.setMinimumDuration(0)
        self.progress.setValue(0)
        reply = self.net.get(QNetworkRequest(QUrl(f"{self.base}/{setup_name}")))
        self.reply = reply
        reply.readyRead.connect(lambda: self.part.write(bytes(reply.readAll())))
        reply.downloadProgress.connect(
            lambda got, total: self.progress.setValue(int(100 * got / total)) if total > 0 else None)
        self.progress.canceled.connect(reply.abort)
        reply.finished.connect(lambda: self.downloaded(reply))

    def downloaded(self, reply):
        self.part.write(bytes(reply.readAll()))
        self.part.close()
        ok = reply.error() == QNetworkReply.NetworkError.NoError
        reply.deleteLater()
        self.progress.close()
        if not ok:
            try:
                os.remove(self.target + ".part")
            except OSError:
                pass
            if reply.error() != QNetworkReply.NetworkError.OperationCanceledError:
                QMessageBox.warning(self.window, "Update", "The download didn't finish. Try again later.")
            return
        os.replace(self.target + ".part", self.target)
        self.install(self.target)

    def install(self, setup_path):
        # /SILENT shows a small progress window; /relaunch=1 makes the setup open Campfire again
        args = ["/SILENT", "/SUPPRESSMSGBOXES", "/NORESTART", "/CLOSEAPPLICATIONS", "/CURRENTUSER", "/relaunch=1"]
        if not QProcess.startDetached(setup_path, args):
            QMessageBox.warning(self.window, "Update", f"Couldn't start the installer. It's saved at:\n{setup_path}")
            return
        QApplication.quit()


def schedule_check(window, settings, admin=False, delay_ms=2500):
    """Called once at start-up: checks a moment after the window appears."""
    if not can_update():
        return None
    updater = Updater(window, settings, admin)
    QTimer.singleShot(delay_ms, updater.check)
    return updater
