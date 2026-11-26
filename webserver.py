"""
Campfire in a web browser.

The server's normal port (55555) also answers web browsers, so nothing extra needs forwarding:
- http://SERVER:55555/          the web app (web/index.html, app.js, app.css)
- /ws                           WebSocket: the same JSON messages the desktop app sends, one per frame
- /av/<id>                      avatar pictures (by their id, cached forever)
- /file/<id>/<name>?t=TOKEN     downloads (with Range for seeking videos); TOKEN comes with login
- POST /upload?t=TOKEN&conv=..  uploads up to 5 GB, streamed to disk

A browser speaks first ("GET ..."); the desktop app waits for the server's hello. Server.handle
looks at the first bytes for a moment to tell them apart.

Voice and video stay in the desktop app: browsers only allow the microphone and camera on https.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import logging
import mimetypes
import os
import secrets
import shutil
import ssl
import struct
import sys
import time
import urllib.parse

log = logging.getLogger("campfire.web")

HTTP_START = (b"GET ", b"HEAD", b"POST", b"OPTI", b"PUT ")
WS_GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"
MAX_HEADER = 16 * 1024
MAX_FRAME = 2 * 1024 * 1024 + 4096       # same as the server's longest line
CHUNK = 256 * 1024
BASE = getattr(sys, "_MEIPASS", os.path.dirname(os.path.abspath(__file__)))
WEB_DIR = os.path.join(BASE, "web")
STATIC = {                                   # url -> file (only these are served)
    "/": ("web", "index.html"), "/index.html": ("web", "index.html"), "/app.js": ("web", "app.js"),
    "/app.css": ("web", "app.css"), "/icon.png": ("", "icon.png"), "/avatar.png": ("", "avatar.png"),
    "/favicon.ico": ("", "icon.ico"), "/manifest.json": ("web", "manifest.json"),
    "/voice-worklet.js": ("web", "voice-worklet.js"),
}
INLINE = {".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp", ".mp4", ".webm", ".mov", ".m4v", ".mp3",
          ".wav", ".ogg", ".m4a", ".flac", ".txt", ".log", ".pdf"}   # never .html/.svg: those could run script
REASONS = {200: "OK", 204: "No Content", 206: "Partial Content", 304: "Not Modified", 400: "Bad Request",
           403: "Forbidden", 404: "Not Found", 405: "Method Not Allowed", 413: "Payload Too Large",
           416: "Range Not Satisfiable", 500: "Internal Server Error", 507: "Insufficient Storage"}


# ---------------------------------------------------------------- WebSocket <-> the chat session
class WSReader:
    """Gives Server.read() a readline() over WebSocket text messages."""

    def __init__(self, reader: asyncio.StreamReader, out: "WSWriter"):
        self.reader, self.out = reader, out

    async def readline(self) -> bytes:
        try:
            msg = await self.message()
        except asyncio.IncompleteReadError:
            return b""
        if msg is None:
            return b""
        return msg.rstrip(b"\r\n") + b"\n"

    async def message(self):
        data = bytearray()
        while True:
            head = await self.reader.readexactly(2)
            fin, op = head[0] & 0x80, head[0] & 0x0F
            masked, n = head[1] & 0x80, head[1] & 0x7F
            if n == 126:
                n = struct.unpack("!H", await self.reader.readexactly(2))[0]
            elif n == 127:
                n = struct.unpack("!Q", await self.reader.readexactly(8))[0]
            if not masked:
                raise ConnectionError("browsers must mask what they send")
            if n + len(data) > MAX_FRAME:
                raise ValueError("message too big")
            mask = await self.reader.readexactly(4)
            payload = unmask(await self.reader.readexactly(n), mask)
            if op == 0x8:                        # close
                self.out.close()
                return None
            if op == 0x9:                        # ping
                self.out.frame(0xA, payload)
                continue
            if op == 0xA:                        # pong
                continue
            data += payload
            if fin:
                return bytes(data)


def unmask(data: bytes, mask: bytes) -> bytes:
    if not data:
        return data
    n = len(data)
    key = (mask * (n // 4 + 1))[:n]
    return (int.from_bytes(data, "big") ^ int.from_bytes(key, "big")).to_bytes(n, "big")


class WSWriter:
    """Looks enough like a StreamWriter for Session: each write() is one JSON line -> one text frame."""

    def __init__(self, writer: asyncio.StreamWriter):
        self.w = writer
        self.transport = writer.transport
        self.closed = False

    def frame(self, op, payload: bytes):
        n = len(payload)
        if n < 126:
            head = struct.pack("!BB", 0x80 | op, n)
        elif n < 65536:
            head = struct.pack("!BBH", 0x80 | op, 126, n)
        else:
            head = struct.pack("!BBQ", 0x80 | op, 127, n)
        self.w.write(head + payload)

    def write(self, data: bytes):
        if self.closed:
            raise ConnectionError("closed")
        for line in data.split(b"\n"):
            if line:
                self.frame(0x1, line)

    async def drain(self):
        await self.w.drain()

    def close(self):
        if not self.closed:
            self.closed = True
            try:
                self.frame(0x8, struct.pack("!H", 1000))
            except Exception:
                pass
        self.w.close()

    async def wait_closed(self):
        await self.w.wait_closed()

    def get_extra_info(self, key, default=None):
        return self.w.get_extra_info(key, default)

    def is_closing(self):
        return self.w.is_closing()


# ---------------------------------------------------------------- HTTP
class Request:
    def __init__(self, method, path, query, headers):
        self.method, self.path, self.query, self.headers = method, path, query, headers

    def arg(self, name, default=""):
        return self.query.get(name, [default])[0]


async def read_request(reader, first: bytes):
    raw = first + await reader.readuntil(b"\r\n\r\n")
    if len(raw) > MAX_HEADER:
        raise ValueError("headers too big")
    lines = raw.decode("latin-1").split("\r\n")
    method, target, _version = lines[0].split(" ", 2)
    headers = {}
    for line in lines[1:]:
        if ":" in line:
            k, v = line.split(":", 1)
            headers[k.strip().lower()] = v.strip()
    url = urllib.parse.urlsplit(target)
    return Request(method.upper(), urllib.parse.unquote(url.path), urllib.parse.parse_qs(url.query), headers)


CORS = {"Access-Control-Allow-Origin": "*",          # the website on GitHub Pages lives elsewhere; files
        "Access-Control-Allow-Methods": "GET, POST, OPTIONS",   # need the login token, not cookies
        "Access-Control-Allow-Headers": "Content-Type, Range",
        "Access-Control-Expose-Headers": "Content-Range, Content-Length",
        "Access-Control-Max-Age": "86400"}


def respond(writer, status, body=b"", ctype="text/plain; charset=utf-8", extra=None, length=None):
    head = [f"HTTP/1.1 {status} {REASONS.get(status, 'OK')}",
            f"Content-Type: {ctype}",
            f"Content-Length: {len(body) if length is None else length}",
            "Connection: close", "X-Content-Type-Options: nosniff", "Referrer-Policy: no-referrer",
            *(f"{k}: {v}" for k, v in CORS.items())]
    for k, v in (extra or {}).items():
        head.append(f"{k}: {v}")
    writer.write(("\r\n".join(head) + "\r\n\r\n").encode("latin-1") + (body if isinstance(body, bytes) else body.encode()))


def respond_json(writer, status, obj):
    respond(writer, status, json.dumps(obj).encode(), "application/json")


async def serve(server, reader, writer, first: bytes):
    """One HTTP request (or a WebSocket that becomes a chat session)."""
    try:
        req = await asyncio.wait_for(read_request(reader, first), 15)
    except Exception:
        writer.close()
        return
    try:
        if req.path == "/ws" and req.headers.get("upgrade", "").lower() == "websocket":
            await websocket(server, reader, writer, req)
            return
        if req.method == "OPTIONS":
            respond(writer, 204)
        elif req.method == "POST" and req.path == "/upload":
            await upload(server, reader, writer, req)
        elif req.method not in ("GET", "HEAD"):
            respond(writer, 405)
        elif req.path in STATIC:
            static(writer, req)
        elif req.path == "/config.js":
            st = server.https_status()
            body = ("window.CAMPFIRE = " + json.dumps({"server": "", "web_url": st["web_url"]}) + ";\n").encode()
            respond(writer, 200, body, "text/javascript; charset=utf-8", {"Cache-Control": "no-cache"})
        elif req.path == "/emoji.json":
            emoji(writer)
        elif req.path.startswith("/av/"):
            avatar(server, writer, req)
        elif req.path.startswith("/bn/"):
            avatar(server, writer, req, column="banner", hashcol="bnhash")
        elif req.path.startswith("/deco/"):
            decoration(writer, req)
        elif req.path.startswith("/file/"):
            await download(server, writer, req)
        else:
            respond(writer, 404, b"Not found")
        await writer.drain()
    except (ConnectionError, asyncio.IncompleteReadError):
        pass
    except Exception:
        log.exception("web request %s %s", req.method, req.path)
        try:
            respond(writer, 500, b"Something went wrong")
        except Exception:
            pass
    finally:
        if not writer.is_closing():
            writer.close()


async def websocket(server, reader, writer, req):
    key = req.headers.get("sec-websocket-key", "")
    if not key:
        respond(writer, 400)
        writer.close()
        return
    accept = base64.b64encode(hashlib.sha1((key + WS_GUID).encode()).digest()).decode()
    writer.write(("HTTP/1.1 101 Switching Protocols\r\nUpgrade: websocket\r\nConnection: Upgrade\r\n"
                  f"Sec-WebSocket-Accept: {accept}\r\n\r\n").encode())
    await writer.drain()
    out = WSWriter(writer)
    await server.handle_session(WSReader(reader, out), out, web=True)


def static(writer, req):
    folder, name = STATIC[req.path]
    path = os.path.join(WEB_DIR if folder == "web" else BASE, name)
    try:
        with open(path, "rb") as f:
            body = f.read()
    except OSError:
        respond(writer, 404, b"Not found")
        return
    ctype = mimetypes.guess_type(name)[0] or "application/octet-stream"
    if ctype.startswith("text/") or name.endswith((".js", ".json")):
        ctype = ("text/javascript" if name.endswith(".js") else ctype) + "; charset=utf-8"
    etag = '"' + hashlib.sha1(body).hexdigest()[:16] + '"'
    extra = {"Cache-Control": "no-cache", "ETag": etag}
    if name == "index.html":
        extra["Content-Security-Policy"] = (
            "default-src 'self'; img-src 'self' data: blob: https:; media-src 'self' blob: https:; "
            "connect-src 'self' ws: wss: https://api.giphy.com; style-src 'self' 'unsafe-inline'; "
            "script-src 'self'; frame-ancestors 'none'")
    if req.headers.get("if-none-match") == etag:
        respond(writer, 304, b"", ctype, extra)
        return
    respond(writer, 200, b"" if req.method == "HEAD" else body, ctype, extra, length=len(body))


_emoji_cache = None


def emoji(writer):
    global _emoji_cache
    if _emoji_cache is None:
        import emoji_data
        _emoji_cache = json.dumps({"categories": emoji_data.CATEGORIES, "icons": emoji_data.CATEGORY_ICONS,
                                   "aliases": emoji_data.ALIASES,
                                   "names": {e: emoji_data.name_of(e) for cat in emoji_data.CATEGORIES.values()
                                             for e in cat}}, ensure_ascii=False).encode()
    respond(writer, 200, _emoji_cache, "application/json; charset=utf-8", {"Cache-Control": "max-age=86400"})


def avatar(server, writer, req, column="avatar", hashcol="avhash"):
    av = req.path[4:].split(".")[0]
    row = server.store.db.execute(f"SELECT {column} AS pic FROM users WHERE {hashcol}=? AND {column} != '' LIMIT 1",
                                  (av,)).fetchone()
    if row is None:
        respond(writer, 404, b"Not found")
        return
    raw = base64.b64decode(row["pic"])
    kind = server_image_kind(raw) or "png"
    respond(writer, 200, raw, f"image/{'jpeg' if kind == 'jpg' else kind}",
            {"Cache-Control": "public, max-age=31536000, immutable"})


def decoration(writer, req):
    import server as core
    name = req.path[len("/deco/"):].removesuffix(".svg")
    path = os.path.join(BASE, "decorations", name + ".svg")
    if name not in core.DECORATIONS or not os.path.exists(path):
        respond(writer, 404, b"Not found")
        return
    with open(path, "rb") as f:
        body = f.read()
    respond(writer, 200, body, "image/svg+xml", {"Cache-Control": "public, max-age=86400"})


def server_image_kind(raw):
    import server as core
    return core.image_kind(raw)


def who(server, req):
    """The account a download/upload is for, from the token its WebSocket login got."""
    return server.web_tokens.get(req.arg("t"))


async def download(server, writer, req):
    uid = who(server, req)
    if uid is None:
        respond(writer, 403, b"Log in again to get this file")
        return
    try:
        fid = int(req.path.split("/")[2])
    except (IndexError, ValueError):
        respond(writer, 404, b"Not found")
        return
    row = server.store.file(fid)
    if row is None or not server.can_access(uid, row["conv"]):
        respond(writer, 404, b"That file doesn't exist any more")
        return
    path = os.path.join(server.files_dir, row["path"])
    if not os.path.exists(path):
        respond(writer, 404, b"That file is missing on the server")
        return
    size = row["size"]
    name = row["name"]
    ext = os.path.splitext(name)[1].lower()
    ctype = mimetypes.guess_type(name)[0] or "application/octet-stream"
    inline = ext in INLINE and not req.arg("dl")
    if ctype.startswith("text/"):
        ctype = "text/plain; charset=utf-8"
    quoted = urllib.parse.quote(name)
    extra = {"Accept-Ranges": "bytes", "Cache-Control": "private, max-age=3600",
             "Content-Disposition": f"{'inline' if inline else 'attachment'}; filename*=UTF-8''{quoted}",
             "Content-Security-Policy": "default-src 'none'; img-src 'self'; media-src 'self'; style-src 'unsafe-inline'"}
    start, end = 0, size - 1
    status = 200
    rng = req.headers.get("range", "")
    if rng.startswith("bytes=") and size:
        try:
            a, b = rng[6:].split(",")[0].split("-")
            if a:
                start, end = int(a), (min(int(b), size - 1) if b else size - 1)
            else:
                start, end = max(0, size - int(b)), size - 1
            if start > end or start >= size:
                raise ValueError
            status = 206
            extra["Content-Range"] = f"bytes {start}-{end}/{size}"
        except ValueError:
            respond(writer, 416, b"", extra={"Content-Range": f"bytes */{size}"})
            return
    length = end - start + 1 if size else 0
    respond(writer, status, b"", ctype, extra, length=length)
    if req.method == "HEAD":
        return
    loop = asyncio.get_running_loop()
    with open(path, "rb") as f:
        f.seek(start)
        remaining = length
        while remaining:
            chunk = await loop.run_in_executor(None, f.read, min(CHUNK, remaining))
            if not chunk:
                break
            writer.write(chunk)
            remaining -= len(chunk)
            await writer.drain()


async def upload(server, reader, writer, req):
    import server as core
    uid = who(server, req)
    if uid is None:
        respond_json(writer, 403, {"error": "Log in again to send files"})
        return
    try:
        size = int(req.headers.get("content-length", "-1"))
    except ValueError:
        size = -1
    if size <= 0:
        respond_json(writer, 400, {"error": "That file is empty"})
        return
    if size > core.MAX_FILE:
        respond_json(writer, 413, {"error": "Files can be at most 5 GB"})
        return
    target, conv = server.resolve_conv(uid, req.arg("conv"))
    if target is None:
        respond_json(writer, 400, {"error": conv})
        return
    if server.store.user_by_id(uid)["muted_until"] > time.time():
        respond_json(writer, 403, {"error": "You can't upload files while muted"})
        return
    if shutil.disk_usage(server.files_dir).free < size + core.DISK_MARGIN:
        respond_json(writer, 507, {"error": "The server doesn't have enough free disk space for that file"})
        return
    if req.headers.get("expect", "").lower() == "100-continue":
        writer.write(b"HTTP/1.1 100 Continue\r\n\r\n")
        await writer.drain()
    partial = os.path.join(server.partial_dir, f"{uid}-web-{secrets.token_hex(6)}")
    loop = asyncio.get_running_loop()
    remaining = size
    try:
        with open(partial, "wb") as f:
            while remaining:
                chunk = await reader.read(min(CHUNK, remaining))
                if not chunk:
                    raise ConnectionError("upload interrupted")
                await loop.run_in_executor(None, f.write, chunk)
                remaining -= len(chunk)
    except BaseException:
        try:
            os.remove(partial)
        except OSError:
            pass
        raise
    stored = secrets.token_hex(12)
    os.replace(partial, os.path.join(server.files_dir, stored))
    name = server.safe_name(req.arg("name", "file"))
    text = req.arg("text").replace("\r", "").strip()[:core.MAX_TEXT]
    fid = server.store.add_file(uid, conv, name, size, stored)
    kind, where = target
    if kind == "room":
        server.deliver_room(uid, where, text, fid)
    elif kind == "dm":
        server.deliver_dm(uid, where, text, fid)
    else:
        server.deliver_group(uid, where, text, fid)
    log.info("upload (web): %s sent %s (%d bytes) to %s", server.store.user_by_id(uid)["name"], name, size,
             req.arg("conv"))
    respond_json(writer, 200, {"fid": fid})


def new_token():
    return secrets.token_urlsafe(24)


# ---------------------------------------------------------------- one port for everything
def peek(conn, timeout):
    """The first bytes a new connection sends, without taking them (b"" = it's waiting for us,
    None = it closed). Runs in a worker thread."""
    import select
    import socket as _socket
    try:
        ready, _w, _x = select.select([conn], [], [], timeout)
        if not ready:
            return b""
        data = conn.recv(8, _socket.MSG_PEEK)
        return data if data else None
    except (BlockingIOError, InterruptedError):
        return b""
    except OSError:
        return None


class Listener:
    """Accepts connections on the server's port and sends each where it belongs:
    TLS (https / wss, the GitHub website and voice) -> decrypted, then sorted again;
    a browser (GET/POST) -> the website; anything else -> the desktop app's chat protocol.
    Works like asyncio.Server for the rest of the code (close, wait_closed, sockets)."""

    def __init__(self, server, host, port, limit, sniff=0.25):
        import socket as _socket
        self.server, self.limit, self.sniff = server, limit, sniff
        family = _socket.AF_INET6 if host and ":" in host else _socket.AF_INET
        self.sock = _socket.create_server((host or "", port), family=family, backlog=128)
        self.sock.setblocking(False)
        self.sockets = [self.sock]
        self.task = None
        self.conns: set = set()

    def start(self):
        self.task = asyncio.get_running_loop().create_task(self.accept_loop())
        return self

    async def accept_loop(self):
        loop = asyncio.get_running_loop()
        while True:
            try:
                conn, _addr = await loop.sock_accept(self.sock)
            except asyncio.CancelledError:
                raise
            except OSError:
                if self.sock.fileno() < 0:
                    return
                await asyncio.sleep(0.05)
                continue
            t = loop.create_task(self.route(conn))
            self.conns.add(t)
            t.add_done_callback(self.conns.discard)

    async def route(self, conn):
        loop = asyncio.get_running_loop()
        try:
            conn.setblocking(False)
            head = await loop.run_in_executor(None, peek, conn, self.sniff)
            if head is None:
                conn.close()
                return
            tls = head[:1] == b"\x16"                    # a TLS ClientHello: https / wss
            ctx = self.server.tls_context() if tls else None
            if tls and ctx is None:
                conn.close()                              # no certificate set up yet
                return
            reader = asyncio.StreamReader(limit=self.limit)
            proto = asyncio.StreamReaderProtocol(reader)
            kw = {"ssl": ctx, "ssl_handshake_timeout": 15} if tls else {}
            transport, _p = await loop.connect_accepted_socket(lambda: proto, conn, **kw)
            writer = asyncio.StreamWriter(transport, proto, reader, loop)
        except (OSError, ssl.SSLError, asyncio.TimeoutError, ConnectionError) as e:
            log.debug("connection dropped before it started: %s", e)
            try:
                conn.close()
            except OSError:
                pass
            return
        if tls:
            await self.server.handle(reader, writer)                  # sniffs again, inside TLS
        elif head[:4] in HTTP_START:
            await serve(self.server, reader, writer, await reader.readexactly(4))
        else:
            await self.server.handle_session(reader, writer)          # the desktop app

    def close(self):
        if self.task:
            self.task.cancel()
        try:
            self.sock.close()
        except OSError:
            pass

    async def wait_closed(self):
        if self.task:
            try:
                await self.task
            except (asyncio.CancelledError, Exception):
                pass

    async def serve_forever(self):
        await self.task

