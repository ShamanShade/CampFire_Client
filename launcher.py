"""
Shared start-up for the double-click launchers (Campfire.pyw and Campfire Server.pyw).

- Runs from the Campfire folder, whatever folder Windows starts it in.
- On the first run, installs any missing packages (with a small "Setting up" window).
- If something goes wrong, shows a message box and writes campfire_error.log, instead of
  silently doing nothing (a .pyw has no console to print to).
"""

from __future__ import annotations

import importlib
import importlib.util
import os
import subprocess
import sys
import threading
import traceback

HERE = os.path.dirname(os.path.abspath(__file__))

# what each app needs: import name -> pip name
NEEDS = {
    "client": {"PyQt6": "PyQt6", "numpy": "numpy", "av": "av", "sounddevice": "sounddevice", "PIL": "Pillow"},
    "server": {"PyQt6": "PyQt6"},
}
TITLES = {"client": "Campfire", "server": "Campfire Server"}


def message(title, text, error=False):
    """A plain message box that works before PyQt6 is installed."""
    if sys.platform == "win32":
        import ctypes
        ctypes.windll.user32.MessageBoxW(None, text, title, 0x10 if error else 0x40)
        return
    try:
        import tkinter
        from tkinter import messagebox
        root = tkinter.Tk()
        root.withdraw()
        (messagebox.showerror if error else messagebox.showinfo)(title, text)
        root.destroy()
    except Exception:
        print(f"{title}: {text}", file=sys.stderr)


def missing(app):
    return [pip for mod, pip in NEEDS[app].items() if importlib.util.find_spec(mod) is None]


def pip_install(packages, log):
    cmd = [sys.executable, "-m", "pip", "install", "--disable-pip-version-check", *packages]
    if sys.platform == "win32" and os.path.basename(sys.executable).lower() == "pythonw.exe":
        cmd[0] = os.path.join(os.path.dirname(sys.executable), "python.exe")
    flags = 0x08000000 if sys.platform == "win32" else 0          # CREATE_NO_WINDOW
    proc = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, creationflags=flags)
    log.append(proc.stdout)
    return proc.returncode == 0


def install(app, packages):
    """Install packages, showing a small window while it works. Returns True on success."""
    title = TITLES[app]
    log: list[str] = []
    result = {}
    try:
        import tkinter
    except ImportError:
        tkinter = None
    if tkinter is None:
        message(title, "Campfire needs to install a few things the first time it runs:\n\n"
                       + ", ".join(packages) + "\n\nThis can take a few minutes. Click OK to start.")
        return pip_install(packages, log) or fail_install(app, log)

    root = tkinter.Tk()
    root.title(f"Setting up {title}")
    root.geometry("420x120")
    root.resizable(False, False)
    root.configure(bg="#2b2d31")
    tkinter.Label(root, text=f"Setting up {title} (first run only)", fg="white", bg="#2b2d31",
                  font=("Segoe UI", 11, "bold")).pack(pady=(18, 4))
    tkinter.Label(root, text="Installing " + ", ".join(packages) + "...\nThis can take a few minutes.",
                  fg="#b5bac1", bg="#2b2d31", font=("Segoe UI", 9)).pack()

    def work():
        result["ok"] = pip_install(packages, log)
        root.after(0, root.destroy)

    threading.Thread(target=work, daemon=True).start()
    root.mainloop()
    return result.get("ok") or fail_install(app, log)


def fail_install(app, log):
    tail = "\n".join("".join(log).strip().splitlines()[-8:])
    write_log("Package install failed:\n" + "".join(log))
    message(TITLES[app], "Couldn't install what Campfire needs.\n\nCheck your internet connection and try "
                         "again. Details were saved to campfire_error.log.\n\n" + tail, error=True)
    return False


def write_log(text):
    try:
        with open(os.path.join(HERE, "campfire_error.log"), "a", encoding="utf-8") as f:
            f.write(text + "\n")
    except OSError:
        pass


def run(app):
    os.chdir(HERE)
    if HERE not in sys.path:
        sys.path.insert(0, HERE)
    if sys.version_info < (3, 9):
        message(TITLES[app], "Campfire needs Python 3.9 or newer. Get it from python.org.", error=True)
        return
    need = missing(app)
    if need:
        if not install(app, need):
            return
        importlib.invalidate_caches()
    try:
        if app == "server":
            import server
            sys.argv = [sys.argv[0]]                 # the window, not the console mode
            server.main()
        else:
            import campfire_client
            campfire_client.main()
    except SystemExit:
        raise
    except Exception:
        details = traceback.format_exc()
        write_log(details)
        message(TITLES[app], "Campfire hit a problem and had to close.\n\nThe details were saved to "
                             "campfire_error.log in the Campfire folder - send that file to whoever "
                             "looks after Campfire.\n\n" + details.strip().splitlines()[-1], error=True)
