"""The web app: HTTP + WebSocket on the server's normal port, and the real page in a browser.
Run:  python tests/test_web.py [shots_dir]
(the browser part needs Playwright and Chrome/Chromium; it's skipped if they aren't installed)"""

import base64
import hashlib
import json
import os
import shutil
import socket
import struct
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TMP = tempfile.mkdtemp(prefix="cf_web_")


def free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def start_server(port):
    db = os.path.join(TMP, f"web{port}.db")
    p = subprocess.Popen([sys.executable, os.path.join(ROOT, "server.py"), "--nogui", "--signup", "open", "--host", "127.0.0.1",
                          "--port", str(port), "--db", db, "--files", db + "_files", "--admin", "alice"],
                         stdout=subprocess.DEVNULL, stderr=subprocess.STDOUT)
    for _ in range(80):
        try:
            socket.create_connection(("127.0.0.1", port), 0.2).close()
            return p
        except OSError:
            time.sleep(0.1)
    raise RuntimeError("server didn't start")


class Desktop:
    """The desktop app's connection: newline-delimited JSON, server speaks first."""

    def __init__(self, port):
        t0 = time.time()
        self.sock = socket.create_connection(("127.0.0.1", port), 5)
        self.f = self.sock.makefile("rb")
        self.hello = json.loads(self.f.readline())
        self.hello_after = time.time() - t0

    def send(self, **obj):
        self.sock.sendall((json.dumps(obj) + "\n").encode())

    def wait(self, msg_type, timeout=8, **match):
        self.sock.settimeout(timeout)
        while True:
            m = json.loads(self.f.readline())
            if m.get("t") == msg_type and all(m.get(k) == v for k, v in match.items()):
                return m


class WS:
    """A tiny WebSocket client, like a browser's."""

    def __init__(self, port, path="/ws"):
        self.sock = socket.create_connection(("127.0.0.1", port), 5)
        key = base64.b64encode(os.urandom(16)).decode()
        self.sock.sendall((f"GET {path} HTTP/1.1\r\nHost: 127.0.0.1:{port}\r\nUpgrade: websocket\r\n"
                           f"Connection: Upgrade\r\nSec-WebSocket-Key: {key}\r\nSec-WebSocket-Version: 13\r\n\r\n").encode())
        head = b""
        while b"\r\n\r\n" not in head:
            head += self.sock.recv(1)
        assert head.startswith(b"HTTP/1.1 101"), head
        want = base64.b64encode(hashlib.sha1((key + "258EAFA5-E914-47DA-95CA-C5AB0DC85B11").encode()).digest())
        assert want in head
        self.buf = b""

    def raw_send(self, op, payload):
        mask = os.urandom(4)
        n = len(payload)
        head = bytes([0x80 | op]) + (bytes([0x80 | n]) if n < 126 else
                                     bytes([0x80 | 126]) + struct.pack("!H", n) if n < 65536 else
                                     bytes([0x80 | 127]) + struct.pack("!Q", n))
        body = bytes(b ^ mask[i % 4] for i, b in enumerate(payload))
        self.sock.sendall(head + mask + body)

    def send(self, **obj):
        self.raw_send(0x1, json.dumps(obj).encode())

    def read(self, n):
        while len(self.buf) < n:
            chunk = self.sock.recv(65536)
            if not chunk:
                raise ConnectionError("closed")
            self.buf += chunk
        out, self.buf = self.buf[:n], self.buf[n:]
        return out

    def recv(self):
        b0, b1 = self.read(2)
        n = b1 & 0x7F
        if n == 126:
            n = struct.unpack("!H", self.read(2))[0]
        elif n == 127:
            n = struct.unpack("!Q", self.read(8))[0]
        assert not b1 & 0x80, "server frames are never masked"
        data = self.read(n)
        if b0 & 0x0F == 0x9:
            return self.recv()
        return b0 & 0x0F, data

    def wait(self, kind, timeout=8, **match):
        self.sock.settimeout(timeout)
        while True:
            op, data = self.recv()
            if op == 0x8:
                raise ConnectionError("closed by server")
            m = json.loads(data)
            if m.get("t") == kind and all(m.get(k) == v for k, v in match.items()):
                return m


def http(port, path, data=None, headers=None, method=None):
    req = urllib.request.Request(f"http://127.0.0.1:{port}{path}", data=data, headers=headers or {}, method=method)
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            return r.status, dict(r.headers), r.read()
    except urllib.error.HTTPError as e:
        return e.code, dict(e.headers), e.read()


def protocol(port):
    # the web page and its files
    st, h, body = http(port, "/")
    assert st == 200 and b"<title>Campfire</title>" in body and "Content-Security-Policy" in h
    for path, kind in (("/app.js", "javascript"), ("/app.css", "css"), ("/icon.png", "image/png"),
                       ("/avatar.png", "image/png"), ("/manifest.json", "json")):
        st, h, body = http(port, path)
        assert st == 200 and kind in h["Content-Type"] and body, (path, st, h)
    st, h, _ = http(port, "/app.js", headers={"If-None-Match": http(port, "/app.js")[1]["ETag"]})
    assert st == 304
    st, _h, body = http(port, "/emoji.json")
    emoji = json.loads(body)
    assert "Smileys" in emoji["categories"] and emoji["aliases"]["smile"] == "\U0001f604"
    assert http(port, "/../server.py")[0] == 404 and http(port, "/campfire.db")[0] == 404

    # the desktop app still gets its hello quickly
    bob = Desktop(port)
    assert bob.hello["t"] == "hello" and "web" in bob.hello["features"], bob.hello
    assert bob.hello_after < 0.8, bob.hello_after       # the app's "server on this PC" check waits 0.8 s
    bob.send(t="login", user="bob", **{"pass": "secret"})
    bob.wait("login_ok")

    # a browser logs in over a WebSocket and gets a token for files
    web = WS(port)
    assert web.wait("hello")["t"] == "hello"
    web.send(t="login", user="alice", **{"pass": "secret"})
    ok = web.wait("login_ok")
    assert ok["admin"]
    token = web.wait("web_token")["token"]

    # chatting both ways
    web.send(t="msg", room="#general", text="hi from the browser \U0001f525")
    m = bob.wait("msg", text="hi from the browser \U0001f525")
    assert m["user"] == "alice"
    bob.send(t="dm", to="alice", text="hi from the app")
    assert web.wait("dm")["text"] == "hi from the app"
    big = "x" * 1999                                  # long messages (needs 16-bit frame lengths)
    web.send(t="msg", room="#general", text=big)
    assert bob.wait("msg", user="alice")["text"] == big
    web.raw_send(0x9, b"ping?")                       # pings get pongs; the session carries on
    while True:
        op, data = web.recv()
        if op == 0xA:
            break
    assert data == b"ping?"

    # uploads over HTTP land in the conversation like any other file
    payload = os.urandom(300_000)
    st, _h, body = http(port, f"/upload?t={token}&conv=%23general&name=photo.png", data=payload,
                        headers={"Content-Type": "application/octet-stream"})
    assert st == 200, body
    fid = json.loads(body)["fid"]
    m = bob.wait("msg", user="alice")
    assert m["file"]["name"] == "photo.png" and m["file"]["size"] == len(payload)
    st, h, body = http(port, f"/upload?t=nope&conv=%23general&name=x.bin", data=b"123")
    assert st == 403
    st, h, body = http(port, f"/upload?t={token}&conv=%23nowhere&name=x.bin", data=b"123")
    assert st == 400 and b"No room" in body

    # downloads: whole, a range (for seeking video), and only with the right token
    st, h, body = http(port, f"/file/{fid}/photo.png?t={token}")
    assert st == 200 and body == payload and h["Content-Type"] == "image/png" and "inline" in h["Content-Disposition"]
    st, h, body = http(port, f"/file/{fid}/photo.png?t={token}", headers={"Range": "bytes=1000-1999"})
    assert st == 206 and body == payload[1000:2000] and h["Content-Range"] == f"bytes 1000-1999/{len(payload)}"
    st, h, body = http(port, f"/file/{fid}/photo.png?t={token}&dl=1")
    assert "attachment" in h["Content-Disposition"]
    assert http(port, f"/file/{fid}/photo.png")[0] == 403
    assert http(port, f"/file/{fid}/photo.png?t=wrong")[0] == 403

    # a web page someone uploads is never shown as a page (it could run script)
    st, _h, body = http(port, f"/upload?t={token}&conv=%40bob&name=evil.html", data=b"<script>alert(1)</script>")
    hfid = json.loads(body)["fid"]
    st, h, _ = http(port, f"/file/{hfid}/evil.html?t={token}")
    assert "attachment" in h["Content-Disposition"] and h["X-Content-Type-Options"] == "nosniff"

    # someone else's DM file isn't reachable with your token
    carl = WS(port)
    carl.wait("hello")
    carl.send(t="login", user="carl", **{"pass": "secret"})
    carl.wait("login_ok")
    ctok = carl.wait("web_token")["token"]
    assert http(port, f"/file/{hfid}/evil.html?t={ctok}")[0] == 404
    assert http(port, f"/file/{fid}/photo.png?t={ctok}")[0] == 200          # #general is for everyone

    # avatars by id
    pic = base64.b64decode(
        "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNk+M9QDwADhgGAWjR9awAAAABJRU5ErkJggg==")
    web.send(t="avatar", data=base64.b64encode(pic).decode())
    u = bob.wait("user_update", name="alice")
    st, h, body = http(port, f"/av/{u['av']}")
    assert st == 200 and body == pic and "immutable" in h["Cache-Control"]
    assert http(port, "/av/0000000000000000")[0] == 404

    # logging out ends the token
    web.sock.close()
    time.sleep(0.5)
    assert http(port, f"/file/{fid}/photo.png?t={token}")[0] == 403
    bob.sock.close()
    carl.sock.close()
    print("web protocol ok")


def browser(port, shots):
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        print("browser part skipped (no Playwright)")
        return
    chrome = shutil.which("google-chrome") or shutil.which("chromium") or shutil.which("chromium-browser")
    bob = Desktop(port)
    bob.send(t="login", user="bob", **{"pass": "secret"})
    bob.wait("login_ok")
    with sync_playwright() as p:
        flags = ["--use-fake-ui-for-media-stream", "--use-fake-device-for-media-stream",
                 "--autoplay-policy=no-user-gesture-required",        # a pretend microphone (beeps) and camera
                 "--auto-select-tab-capture-source-by-title=Campfire", "--enable-usermedia-screen-capturing"]
        b = (p.chromium.launch(executable_path=chrome, args=flags) if chrome else p.chromium.launch(args=flags))
        page = b.new_page(viewport={"width": 1280, "height": 760})
        errors = []
        page.on("pageerror", lambda e: errors.append(str(e)))
        page.on("console", lambda m: errors.append(m.text) if m.type == "error" else None)
        page.goto(f"http://127.0.0.1:{port}/")
        page.fill("#user", "alice")
        page.fill("#pass", "secret")
        page.click("#loginbtn")
        page.wait_for_selector("#app:not(.hidden)")
        page.wait_for_function("window.Campfire.token")
        page.wait_for_selector(".msg >> text=hi from the browser")            # history from before
        page.fill("#input", "hello bob :fire:")
        page.keyboard.press("Enter")
        m = bob.wait("msg", user="alice", text="hello bob \U0001f525")         # shortcodes become emoji
        bob.send(t="msg", room="#general", text="hey alice https://example.com/cat.gif")
        page.wait_for_selector(".msg >> text=hey alice")
        assert page.locator("img.media[src='https://example.com/cat.gif']").count() == 1
        bob.send(t="msg", room="#general", text="\U0001f389\U0001f389")
        page.wait_for_selector(".text.jumbo")
        # a DM from the app pops up in the sidebar with an unread badge
        bob.send(t="dm", to="alice", text="psst")
        page.wait_for_selector("#side .item.unread >> text=bob")
        page.click("#side .item >> text=bob")
        page.wait_for_selector(".msg >> text=psst")
        page.click("#side .item >> text=general")
        # profile card from a name in chat
        page.click(".msg .nm >> text=bob")
        page.wait_for_selector(".card .cname >> text=bob")
        assert page.locator(".card button >> text=Message").count() == 1
        if shots:
            page.screenshot(path=os.path.join(shots, "web_profile.png"))
        page.keyboard.press("Escape")
        # emoji picker inserts into the message box
        page.click("#emojibtn")
        page.wait_for_selector("#picker .e")
        page.locator("#picker .e").first.click()
        assert page.input_value("#input")
        page.fill("#input", "")
        page.keyboard.press("Escape")
        # upload through the file button; the app's user sees it, and the browser shows the picture
        png = os.path.join(TMP, "dot.png")
        with open(png, "wb") as f:
            f.write(base64.b64decode(
                "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNk+M9QDwADhgGAWjR9awAAAABJRU5ErkJggg=="))
        page.set_input_files("#file", png)
        m = bob.wait("msg", user="alice")
        assert m["file"]["name"] == "dot.png"
        page.wait_for_selector(f"img.media[src*='/file/{m['file']['id']}/']")
        # deleting your own message
        page.hover(".msg >> text=hello bob")
        page.once("dialog", lambda d: d.accept())
        page.locator(".msg", has_text="hello bob").locator(".tools button[title='Delete message']").click()
        bob.wait("msg_deleted")
        page.wait_for_selector(".msg >> text=hello bob", state="detached")
        # admin: the + makes a channel
        page.once("dialog", lambda d: d.accept("music"))
        page.click("#side .section button[title^='Create a channel']")
        page.wait_for_selector("#title >> text=#music")
        bob.wait("rooms")
        # settings: status and theme
        page.click("#me button[title=Settings]")
        page.fill(".modal input[placeholder^='e.g.']", "on the website")
        page.select_option(".modal select:has(option[value=Campfire])", "Campfire")
        page.click(".modal button >> text=Save")
        u = bob.wait("user_update", name="alice")
        assert u["status"] == "on the website"
        assert page.evaluate("document.documentElement.dataset.theme") == "Campfire"
        page.click("#side .item >> text=general")
        page.wait_for_timeout(300)
        if shots:
            page.screenshot(path=os.path.join(shots, "web_desktop.png"))
        # profile style: banner picture, card colours, decoration, effect
        page.click("#me button[title=Settings]")
        page.click(".decopick button[title='Campfire flames']")
        page.select_option(".modal select:has(option[value=embers])", "embers")
        page.evaluate("""() => {      // name colour, banner colour, card colour 1, card colour 2, accent
            const t = [...document.querySelectorAll('.modal input[type=color]')];
            t[2].value = '#4a2c6e'; t[3].value = '#1b3a5c'; t[2].dispatchEvent(new Event('input')); }""")
        page.set_input_files(".modal input[accept^='image/png,image/jpeg,image/gif,image/webp']:not([accept*='bmp'])", png)
        page.wait_for_selector(".modal >> text=new picture")
        page.click(".modal button >> text=Save")
        u = bob.wait("user_update", name="alice", deco="flame")
        page.wait_for_selector(".msg .avw .deco[src='deco/flame.svg']")
        page.click("#me .avw")
        page.wait_for_selector(".card .cavw .deco[src='deco/flame.svg']")
        page.wait_for_selector(".card .banner.pic img")
        page.wait_for_function("document.querySelector('.card').style.background.includes('linear-gradient')")
        page.wait_for_timeout(600)                                    # embers rising
        lit = page.evaluate("""() => { const c = document.querySelector('.card canvas.fx');
            const d = c.getContext('2d').getImageData(0, 0, c.width, c.height).data;
            let n = 0; for (let i = 3; i < d.length; i += 4) if (d[i] > 30) n++; return n; }""")
        assert lit > 50, lit
        box = page.evaluate("JSON.stringify(document.querySelector('.card').getBoundingClientRect())")
        assert json.loads(box)["bottom"] <= 760, box                    # the card is on screen
        if shots:
            page.screenshot(path=os.path.join(shots, "web_style.png"))
        page.keyboard.press("Escape")
        features_test(page, bob, shots, port)
        # voice chat: the browser and the desktop app hear each other (same Opus packets)
        voice_test(page, bob)
        # reload: still logged in, back where we were
        page.reload()
        page.wait_for_selector("#app:not(.hidden)")
        page.wait_for_selector("#title >> text=#general")
        # phone-sized
        phone = b.new_page(viewport={"width": 390, "height": 780}, device_scale_factor=2, is_mobile=True, has_touch=True)
        phone.on("pageerror", lambda e: errors.append(str(e)))
        phone.goto(f"http://127.0.0.1:{port}/")
        phone.fill("#user", "dana")
        phone.fill("#pass", "secret")
        phone.click("#loginbtn")
        phone.wait_for_selector(".msg >> text=hey alice")
        assert not phone.locator("#side").is_visible() or phone.evaluate(
            "document.getElementById('side').getBoundingClientRect().right <= 0")
        if shots:
            phone.screenshot(path=os.path.join(shots, "web_phone.png"))
        phone.click("#openside")
        phone.wait_for_timeout(350)
        if shots:
            phone.screenshot(path=os.path.join(shots, "web_phone_menu.png"))
        phone.click("#side .item >> text=music")
        phone.wait_for_selector("#title >> text=#music")
        b.close()
    errors = [e for e in errors if "ERR_TUNNEL" not in e and "ERR_NAME_NOT_RESOLVED" not in e and "ERR_PROXY" not in e]
    assert not errors, errors
    bob.sock.close()
    print("web browser ok")


def features_test(page, bob, shots, port):
    """Replies, reactions, editing, @mentions, typing, muting, roles, private channels, sign-ups, password."""
    page.click("#side .item >> text=general")
    bob.send(t="msg", room="#general", text="pizza tonight?")
    pizza = bob.wait("msg", text="pizza tonight?")
    page.wait_for_selector(".msg >> text=pizza tonight?")
    # reply
    page.locator(".msg", has_text="pizza tonight?").first.hover()
    page.locator(".msg", has_text="pizza tonight?").locator("button[title=Reply]").click()
    page.wait_for_selector("#composebar:not(.hidden) >> text=bob")
    page.fill("#input", "yes please")
    page.keyboard.press("Enter")
    m = bob.wait("msg", text="yes please")
    assert m["reply"]["id"] == pizza["id"] and m["reply"]["user"] == "bob", m
    page.wait_for_selector(".msg .quote >> text=pizza tonight?")
    assert page.is_hidden("#composebar")
    # a reply to you counts as a mention
    bob.send(t="msg", room="#general", text="great", reply_to=m["id"])
    page.wait_for_selector(".msg.mentioned >> text=great")
    # reactions: quick picker, the app's reaction shows up, clicking a pill takes yours back
    page.locator(".msg", has_text="pizza tonight?").first.hover()
    page.locator(".msg", has_text="pizza tonight?").first.locator("button[title='Add a reaction']").click()
    page.click(".menu.quick .e >> text=🔥")
    r = bob.wait("reaction", id=pizza["id"])
    assert r["emoji"] == "\U0001f525" and r["users"] == ["alice"]
    bob.send(t="react", id=pizza["id"], emoji="\U0001f525", on=True)
    page.wait_for_selector(".react.mine >> text=2")
    page.click(".react.mine")
    bob.wait("reaction", id=pizza["id"], users=["bob"])
    page.wait_for_selector(".react:not(.mine) >> text=1")
    # editing with the Up arrow
    page.click("#input")
    page.keyboard.press("ArrowUp")
    assert page.input_value("#input") == "yes please"
    page.fill("#input", "yes please, pepperoni")
    page.keyboard.press("Enter")
    e = bob.wait("msg_edited", id=m["id"])
    assert e["text"] == "yes please, pepperoni"
    page.wait_for_selector(".msg >> text=(edited)")
    # @mention suggestions
    page.wait_for_timeout(2600)                                  # typing pings are spaced out
    page.evaluate("() => { window.Campfire.typedAt = 0; }")
    page.type("#input", "hey @bo")
    page.wait_for_selector("#mentions:not(.hidden) .opt >> text=bob")
    page.keyboard.press("Tab")
    assert page.input_value("#input") == "hey @bob ", page.input_value("#input")
    bob.wait("typing", conv="#general", user="alice")           # typing showed on the app
    page.keyboard.press("Enter")
    bob.wait("msg", text="hey @bob")
    page.wait_for_selector(".msg .mention >> text=@bob")
    # the app types: the page says so
    bob.send(t="typing", conv="#general")
    page.wait_for_selector("#typing >> text=bob is typing...")
    bob.send(t="msg", room="#general", text="done typing")
    page.wait_for_function("() => document.getElementById('typing').textContent === ''")
    # muting #music: no unread badge for it
    page.click("#side .item[data-key='#music']", button="right")
    page.click(".menu div >> text=Mute")
    page.wait_for_selector("#side .item.quiet[data-key='#music']")
    bob.send(t="msg", room="#music", text="la la")
    page.wait_for_timeout(500)
    assert page.locator("#side .item.unread[data-key='#music']").count() == 0
    bob.send(t="msg", room="#music", text="@alice listen")
    page.wait_for_selector("#side .item.unread[data-key='#music']")
    # roles: make one in settings, give it to bob, he's grouped and coloured
    page.click("#me button[title=Settings]")
    page.click(".modal button >> text=Roles")
    page.fill(".modal input[placeholder^='Role name']", "DJs")
    page.evaluate("""() => { const c = document.querySelector('[data-kind=roles] input[type=color]');
        c.value = '#1abc9c'; c.dispatchEvent(new Event('input')); }""")
    page.click(".modal label.row:has-text('Create and delete channels') input")
    page.click(".modal button >> text=Save role")
    roles = bob.wait("roles")
    rid = next(r["id"] for r in roles["roles"] if r["name"] == "DJs")
    assert roles["roles"][0]["perms"] == ["channels"] and roles["roles"][0]["color"] == "#1abc9c"
    page.keyboard.press("Escape")
    page.click("#people .person >> text=bob", button="right")
    page.click(".menu div >> text=Roles...")
    page.check(f".modal input[value='{rid}']")
    page.click(".modal button >> text=Save")
    bob.wait("perms", perms=["channels"])
    page.wait_for_selector("#people .section >> text=DJs — 1")
    col = page.evaluate("getComputedStyle(document.querySelector('#people .person[title^=bob] .nm')).color")
    assert col == "rgb(26, 188, 156)", col
    # private channel: bob's role only
    page.click("#side .item[data-key='#music']", button="right")
    page.click(".menu div >> text=Who can see it...")
    page.check(f".modal input[value='{rid}']")
    page.click(".modal button >> text=Save")
    page.wait_for_selector("#side .item[data-key='#music'] .tag >> text=🔒")
    if shots:
        page.screenshot(path=os.path.join(shots, "web_roles.png"))
    page.click("#side .item[data-key='#music']", button="right")
    page.click(".menu div >> text=Who can see it...")
    page.uncheck(f".modal input[value='{rid}']")
    page.click(".modal button >> text=Save")
    page.wait_for_selector("#side .item[data-key='#music'] .tag >> text=🔒", state="detached")
    # sign-ups: switch to approval, someone asks, the page approves them; invite codes
    page.click("#me button[title=Settings]")
    page.click(".modal button >> text=Sign-ups and invites")
    page.select_option("[data-kind=signups] select", "approval")
    page.wait_for_function("() => window.Campfire.signup === 'approval'")
    zed = Desktop(port)
    zed.send(t="login", user="zed", **{"pass": "secret"})
    fail = zed.wait("login_fail")
    assert fail.get("pending"), fail
    page.wait_for_selector("[data-kind=signups] .pend >> text=zed")
    page.click("[data-kind=signups] .pend button >> text=Approve")
    page.wait_for_selector("[data-kind=signups] >> text=Nobody is waiting")
    zed.send(t="login", user="zed", **{"pass": "secret"})
    zed.wait("login_ok")
    page.click("[data-kind=signups] button >> text=Make a code")
    page.wait_for_selector("[data-kind=signups] code")
    code = page.inner_text("[data-kind=signups] code")
    page.select_option("[data-kind=signups] select", "open")
    if shots:
        page.screenshot(path=os.path.join(shots, "web_signups.png"))
    page.keyboard.press("Escape")
    assert len(code) == 9 and code[4] == "-", code
    # change password
    page.click("#me button[title=Settings]")
    page.click(".modal button >> text=Change password")
    page.fill(".modal input[placeholder='Current password']", "secret")
    page.fill(".modal input[placeholder='New password']", "secret2")
    page.fill(".modal input[placeholder='New password again']", "secret2")
    page.click(".modal button >> text=Change")
    page.wait_for_selector(".toast >> text=Your password is changed")
    page.click("#me button[title=Settings]")
    page.click(".modal button >> text=Change password")
    page.fill(".modal input[placeholder='Current password']", "secret2")
    page.fill(".modal input[placeholder='New password']", "secret")
    page.fill(".modal input[placeholder='New password again']", "secret")
    page.click(".modal button >> text=Change")
    page.wait_for_function("() => document.querySelectorAll('.toast').length >= 1")
    page.wait_for_timeout(300)
    zed.sock.close()
    page.click("#side .item >> text=general")
    if shots:
        page.screenshot(path=os.path.join(shots, "web_features.png"))


def video_test(page, bob):
    import numpy as np
    import video
    # the browser's camera -> the app (VP8, decoded with the app's own decoder)
    page.click("#voicebar button[title='Turn camera on']")
    page.wait_for_selector("#stage .tile.live video")                   # your own preview
    bob.wait("voice_state")
    bob.send(t="video_watch", user="alice", kind="camera")
    dec = video.VideoDecoder()
    frames = []
    first = None
    for _ in range(12):
        m = bob.wait("video_data", u="alice", kind="camera")
        first = m["k"] if first is None else first
        frames += dec.decode(base64.b64decode(m["d"]))
    assert first is True, "a new viewer must start with a keyframe"
    assert frames and frames[-1].shape == (360, 640, 3), frames[-1].shape if frames else None
    assert frames[-1].std() > 5, "the camera picture is blank"
    # the app's camera -> the browser (the page watches cameras by itself)
    bob.send(t="video_start", kind="camera")
    bob.wait("video_keyframe", kind="camera")                           # the browser asked to watch
    enc = video.VideoEncoder(640, 360, 15, 600_000)
    for i in range(20):
        img = np.zeros((360, 640, 3), np.uint8)
        img[:, : min(640, 40 * (i + 1))] = (40, 180, 90)
        for pkt, key in enc.encode(img, force_key=(i == 0)):
            bob.send(t="video_data", kind="camera", d=base64.b64encode(pkt).decode(), k=key)
        time.sleep(1 / 15)
    page.wait_for_selector("#stage .tile.live canvas", timeout=8000)
    page.wait_for_function("VID.stats.shown >= 10", timeout=8000)
    pixel = page.evaluate("""() => { const c = document.querySelector('#stage .tile.live canvas');
        const d = c.getContext('2d').getImageData(5, 180, 1, 1).data; return [c.width, c.height, d[1]]; }""")
    assert pixel[0] == 640 and pixel[1] == 360 and pixel[2] > 120, pixel        # the green bar arrived
    if os.environ.get("CAMPFIRE_SHOTS"):
        page.screenshot(path=os.path.join(os.environ["CAMPFIRE_SHOTS"], "web_video.png"))
    # screen sharing (Chrome picks this tab)
    shared = page.evaluate("""async () => { await startVideo('screen'); return !!VID.out.screen; }""")
    if shared:
        bob.send(t="video_watch", user="alice", kind="screen")
        m = bob.wait("video_data", u="alice", kind="screen")
        assert m["k"] and video.VideoDecoder().decode(base64.b64decode(m["d"]))
        page.click("#voicebar button[title='Stop sharing your screen']")
        print("web screen share ok")
    else:
        print("web screen share: this headless Chrome can't capture a screen (skipped)")
    bob.send(t="video_stop", kind="camera")
    page.click("#voicebar button[title='Turn camera off']")
    page.wait_for_selector("#stage", state="hidden", timeout=5000)
    print("web camera ok")


def voice_test(page, bob):
    import numpy as np
    sys.path.insert(0, ROOT)
    import voice
    page.evaluate("V.threshold = -200")                 # the fake mic is quiet between beeps: send it all
    page.click("#side .item >> text=Lounge")
    page.wait_for_selector("#voicebar")
    bob.send(t="voice_join", channel="Lounge", muted=False, deaf=False)
    bob.wait("voice_joined")
    dec = voice.Decoder()
    loud = 0
    for _ in range(60):                                  # 1.2 s of the browser's audio
        m = bob.wait("voice_data", u="alice")
        for pcm in dec.decode(base64.b64decode(m["d"])):
            assert len(pcm) == 960, len(pcm)            # 20 ms at 48 kHz, like the app
            loud = max(loud, int(np.abs(pcm.astype(np.int32)).max()))
    assert loud > 1000, f"the browser's audio came through silent ({loud})"
    # now the app talks: a 440 Hz tone for a second
    enc = voice.Encoder()
    t = np.arange(960 * 50) / 48000
    tone = (np.sin(2 * np.pi * 440 * t) * 9000).astype(np.int16)
    before = page.evaluate("V.stats.played")
    for i in range(50):
        for pkt in enc.encode(tone[i * 960:(i + 1) * 960]):
            bob.send(t="voice_data", d=base64.b64encode(pkt).decode())
        time.sleep(0.02)
    page.wait_for_function(f"V.stats.played >= {before} + 40", timeout=8000)
    page.wait_for_selector("[data-voice-user=bob].talking", timeout=3000)
    video_test(page, bob)
    # mute stops the sending; leaving hangs up
    page.click("#voicebar button[title=Mute]")
    sent = page.evaluate("V.stats.sent")
    page.wait_for_timeout(500)
    assert page.evaluate("V.stats.sent") - sent <= 2
    bob.wait("voice_state")
    page.click("#voicebar button[title='Leave voice']")
    page.wait_for_selector("#voicebar", state="detached")
    page.click("#voicebar button[title=Unmute]") if page.locator("#voicebar").count() else None
    page.evaluate("setMuted(false)")
    bob.send(t="voice_leave")
    print("web voice ok")


def main(shots=None):
    port = free_port()
    server = start_server(port)
    try:
        protocol(port)
        browser(port, shots)
        print("web tests passed")
    finally:
        server.terminate()


if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else None)
