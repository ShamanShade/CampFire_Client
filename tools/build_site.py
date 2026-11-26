"""Builds the website for GitHub Pages into a folder (default _site).
Used by .github/workflows/pages.yml (and the tests).

config.js tells the page which server to talk to: WEB_SERVER from campfire_config.py, as
"name.duckdns.org:55555". If that's empty the page asks (and remembers the answer)."""

import json
import os
import re
import shutil
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def server_from_config(path=os.path.join(ROOT, "campfire_config.py")):
    src = open(path, encoding="utf-8").read()
    m = re.search(r'^WEB_SERVER\s*=\s*"([^"]*)"', src, re.M)
    port = re.search(r"^SERVER_PORT\s*=\s*(\d+)", src, re.M)
    server = (m.group(1) if m else "").strip().lower()
    if server and "." not in server.split(":")[0]:
        server += ".duckdns.org"
    if server and ":" not in server:
        server += f":{port.group(1) if port else 55555}"
    return server


def build(out, server=None):
    server = server_from_config() if server is None else server
    shutil.rmtree(out, ignore_errors=True)
    shutil.copytree(os.path.join(ROOT, "web"), out)
    for name in ("icon.png", "avatar.png", "icon.ico"):
        shutil.copy2(os.path.join(ROOT, name), out)
    with open(os.path.join(out, "config.js"), "w") as f:      # null = ask; "" would mean "this server"
        f.write("window.CAMPFIRE = " + json.dumps({"server": server or None}) + ";\n")
    open(os.path.join(out, ".nojekyll"), "w").close()
    return server


if __name__ == "__main__":
    out = sys.argv[1] if len(sys.argv) > 1 else "_site"
    print("website server:", build(out) or "(the page asks)")
