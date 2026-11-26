"""
Keeps Campfire Server up to date (the chat app's installer doesn't include the server).

Two ways a server can be running:
- "frozen": Campfire Server.exe from Campfire-Windows.zip. Updating downloads the newest zip,
  unpacks it into _update next to the exe, then starts the NEW Campfire Server.exe from there with
  --finish-update: it waits for the old server to close, copies itself over the old program files
  (never touching campfire.db, campfire_files or logs), and starts the updated server again.
- "installed": Campfire Server.exe from Campfire-Server-Setup.exe. Updating downloads the newest
  setup and runs it quietly; it closes the server, installs over it and starts it again. Data in
  the install folder is kept.
- "git": server.py from a git clone. Updating runs `git pull --ff-only`, then restarts.

The network/git parts are plain functions (run them off the GUI thread).
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import time
import urllib.request
import zipfile

try:
    from _version import VERSION            # written by the Windows build
except ImportError:
    VERSION = "dev"

REPO = "ShamanShade/CampFire_Client"
RELEASE_BASE = f"https://github.com/{REPO}/releases/download/latest"
ZIP_NAME = "Campfire-Windows.zip"
SETUP_NAME = "Campfire-Server-Setup.exe"
FROZEN = bool(getattr(sys, "frozen", False))
APP_DIR = os.path.dirname(os.path.abspath(sys.executable if FROZEN else __file__))
UPDATE_DIR = os.path.join(APP_DIR, "_update")
KEEP = ("campfire.db", "campfire_files", "campfire_server.log", "campfire_error.log", "campfire_update.log",
        "_update", "cert.pem", "key.pem")
NO_WINDOW = 0x08000000 if sys.platform == "win32" else 0


def parse(version: str):
    try:
        return tuple(int(x) for x in version.strip().lstrip("v").split("."))
    except ValueError:
        return None


def mode() -> str:
    if FROZEN and parse(VERSION):
        installed = any(n.lower().startswith("unins") and n.lower().endswith(".exe") for n in os.listdir(APP_DIR))
        return "installed" if installed else "frozen"
    if os.path.isdir(os.path.join(APP_DIR, ".git")) and shutil.which("git"):
        return "git"
    return "none"


def git(*args, timeout=60):
    p = subprocess.run(["git", *args], cwd=APP_DIR, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                       text=True, timeout=timeout, creationflags=NO_WINDOW)
    return p.returncode, p.stdout.strip()


def describe() -> str:
    """What's running, for the server window."""
    m = mode()
    if m in ("frozen", "installed"):
        return f"Version {VERSION}"
    if m == "git":
        code, out = git("log", "-1", "--format=%h %cd", "--date=short")
        return f"From source ({out})" if code == 0 else "From source"
    return "From source"


def fetch_json(url, timeout=15):
    req = urllib.request.Request(url, headers={"User-Agent": "Campfire-Server", "Cache-Control": "no-cache"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode())


def check(base=RELEASE_BASE) -> dict:
    """{"available": bool, "latest": str, "detail": str, "error": str?}"""
    m = mode()
    try:
        if m in ("frozen", "installed"):
            info = fetch_json(f"{base}/version.json")
            latest = info.get("version", "")
            newer = bool(parse(latest)) and parse(latest) > parse(VERSION)
            return {"available": newer, "latest": latest, "detail": f"Version {latest}"}
        if m == "git":
            code, out = git("fetch", "--quiet", "origin")
            if code != 0:
                return {"available": False, "latest": "", "detail": "", "error": out or "git fetch failed"}
            code, branch = git("rev-parse", "--abbrev-ref", "@{u}")
            if code != 0:
                branch = "origin/main"
            code, count = git("rev-list", "--count", f"HEAD..{branch}")
            n = int(count) if code == 0 and count.isdigit() else 0
            return {"available": n > 0, "latest": branch, "detail": f"{n} new change{'s' * (n != 1)} on GitHub"}
        return {"available": False, "latest": "", "detail": "",
                "error": "This copy can't update itself (not the Windows download or a git clone)."}
    except Exception as e:                           # offline, GitHub down...
        return {"available": False, "latest": "", "detail": "", "error": str(e)}


# ---- updating a git clone
def git_pull() -> tuple[bool, str]:
    code, out = git("pull", "--ff-only", timeout=180)
    return code == 0, out


# ---- updating an installed server
def download_setup(base=RELEASE_BASE, progress=None) -> str:
    os.makedirs(UPDATE_DIR, exist_ok=True)
    path = os.path.join(UPDATE_DIR, SETUP_NAME)
    fetch_file(f"{base}/{SETUP_NAME}", path, progress)
    return path


def setup_command(path) -> list[str]:
    # the installer closes this server, installs over it and (relaunch=1) starts it again
    return [path, "/SILENT", "/SUPPRESSMSGBOXES", "/NORESTART", "/CLOSEAPPLICATIONS", "/CURRENTUSER", "/relaunch=1"]


def fetch_file(url, path, progress=None):
    req = urllib.request.Request(url, headers={"User-Agent": "Campfire-Server"})
    with urllib.request.urlopen(req, timeout=30) as r, open(path + ".part", "wb") as f:
        total = int(r.headers.get("Content-Length") or 0)
        got = 0
        while True:
            chunk = r.read(1 << 16)
            if not chunk:
                break
            f.write(chunk)
            got += len(chunk)
            if progress:
                progress(got, total)
    os.replace(path + ".part", path)


# ---- updating the Windows download (zip)
def download(base=RELEASE_BASE, progress=None) -> str:
    """Downloads and unpacks the newest zip. Returns the folder holding the new Campfire Server.exe."""
    shutil.rmtree(UPDATE_DIR, ignore_errors=True)
    os.makedirs(UPDATE_DIR, exist_ok=True)
    path = os.path.join(UPDATE_DIR, ZIP_NAME)
    fetch_file(f"{base}/{ZIP_NAME}", path, progress)
    new = os.path.join(UPDATE_DIR, "new")
    with zipfile.ZipFile(path) as z:
        for name in z.namelist():                    # no ../ tricks
            target = os.path.realpath(os.path.join(new, name))
            if not target.startswith(os.path.realpath(new) + os.sep):
                raise ValueError(f"bad path in update: {name}")
        z.extractall(new)
    os.remove(path)
    # the zip holds a "Campfire" folder
    for root, _dirs, files in os.walk(new):
        if "Campfire Server.exe" in files or (not FROZEN and "server.py" in files):
            return root
    raise ValueError("the download doesn't contain Campfire Server")


def finisher_command(new_dir, relaunch_args) -> list[str]:
    """How to start the new copy so it can replace this one."""
    exe = os.path.join(new_dir, "Campfire Server.exe")
    if not os.path.exists(exe):                      # tests: a source copy stands in for the exe
        exe_cmd = [sys.executable, os.path.join(new_dir, "server.py")]
    else:
        exe_cmd = [exe]
    return exe_cmd + ["--finish-update", APP_DIR, "--wait-pid", str(os.getpid()),
                      "--relaunch-args", json.dumps(relaunch_args)]


def wait_for_exit(pid, timeout=90):
    end = time.time() + timeout
    if sys.platform == "win32":
        import ctypes
        SYNCHRONIZE = 0x00100000
        h = ctypes.windll.kernel32.OpenProcess(SYNCHRONIZE, False, pid)
        if not h:
            return True
        try:
            return ctypes.windll.kernel32.WaitForSingleObject(h, int(timeout * 1000)) == 0
        finally:
            ctypes.windll.kernel32.CloseHandle(h)
    while time.time() < end:
        try:
            os.kill(pid, 0)
        except OSError:
            return True
        time.sleep(0.2)
    return False


def retry(fn, *args, tries=40):
    for i in range(tries):
        try:
            return fn(*args)
        except OSError:
            if i == tries - 1:
                raise
            time.sleep(0.5)                          # antivirus or a closing program can hold files briefly


def keep(name):
    return name.lower().startswith(KEEP) or name.endswith(".old")


def copy_over(src, dst, log):
    """All or nothing: first move the old files aside (fails cleanly if a program is still using
    them), then copy the new ones in, and only then delete the old ones."""
    names = [n for n in os.listdir(src) if not keep(n)]
    moved = []
    try:
        # folders first: Windows won't rename a folder whose libraries are in use
        for name in sorted(names, key=lambda n: not os.path.isdir(os.path.join(src, n))):
            d = os.path.join(dst, name)
            if os.path.exists(d):
                old = d + ".old"
                if os.path.isdir(old):
                    shutil.rmtree(old, ignore_errors=True)
                elif os.path.exists(old):
                    os.remove(old)
                retry(os.rename, d, old, tries=10)
                moved.append((d, old))
    except OSError as e:
        for d, old in reversed(moved):
            os.rename(old, d)
        raise RuntimeError(f"{os.path.basename(e.filename or '')} is in use - is Campfire (the chat app) "
                           "running from the server's folder? Close it and try again.") from e
    try:
        for name in names:
            s, d = os.path.join(src, name), os.path.join(dst, name)
            if os.path.isdir(s):
                retry(shutil.copytree, s, d)
            else:
                retry(shutil.copy2, s, d)
            log(f"updated {name}")
    except Exception:
        for name in names:                           # put the old version back
            d = os.path.join(dst, name)
            shutil.rmtree(d, ignore_errors=True) if os.path.isdir(d) else (os.path.exists(d) and os.remove(d))
        for d, old in reversed(moved):
            os.rename(old, d)
        raise
    for _d, old in moved:                            # a running exe can't be deleted: cleanup() gets it later
        shutil.rmtree(old, ignore_errors=True) if os.path.isdir(old) else _try_remove(old)


def _try_remove(path):
    try:
        os.remove(path)
    except OSError:
        pass


def finish(argv) -> int:
    """Runs in the NEW copy: --finish-update TARGET --wait-pid PID --relaunch-args JSON."""
    target = argv[argv.index("--finish-update") + 1]
    pid = int(argv[argv.index("--wait-pid") + 1]) if "--wait-pid" in argv else 0
    relaunch = json.loads(argv[argv.index("--relaunch-args") + 1]) if "--relaunch-args" in argv else []
    src = os.path.dirname(os.path.abspath(sys.executable if FROZEN else sys.argv[0] or __file__))
    logf = open(os.path.join(target, "campfire_update.log"), "a", encoding="utf-8")

    def log(text):
        logf.write(time.strftime("%Y-%m-%d %H:%M:%S ") + text + "\n")
        logf.flush()

    log(f"updating {target} from {src}")
    ok = True
    try:
        if pid and not wait_for_exit(pid):
            raise RuntimeError("the old server didn't close")
        copy_over(src, target, log)
    except Exception as e:
        ok = False
        log(f"update failed: {e!r}")
    exe = os.path.join(target, "Campfire Server.exe")
    cmd = [exe] if FROZEN or os.path.exists(exe) else [sys.executable, os.path.join(target, "server.py")]
    cmd += [a for a in relaunch if a not in ("--updated", "--update-failed")] + (["--updated"] if ok else ["--update-failed"])
    log("starting " + " ".join(cmd))
    subprocess.Popen(cmd, cwd=target, close_fds=True, creationflags=NO_WINDOW if not FROZEN else 0)
    logf.close()
    return 0 if ok else 1


def cleanup():
    """Called when the server starts: the _update folder isn't needed once we're running from APP_DIR."""
    if os.path.isdir(UPDATE_DIR):
        shutil.rmtree(UPDATE_DIR, ignore_errors=True)
    for name in os.listdir(APP_DIR):
        if name.endswith(".old"):
            p = os.path.join(APP_DIR, name)
            shutil.rmtree(p, ignore_errors=True) if os.path.isdir(p) else _try_remove(p)


def restart_command(relaunch_args) -> list[str]:
    if FROZEN:
        return [sys.executable] + relaunch_args
    return [sys.executable, os.path.join(APP_DIR, "server.py")] + relaunch_args
