"""
Campfire chat server.

Protocol: one JSON object per line (UTF-8, newline terminated) in both
directions. Every object has a "t" field naming its type. See README.md.

Run:  python server.py                     (listens on 0.0.0.0:55555)
      python server.py --port 6000 --db campfire.db
      python server.py --certfile cert.pem --keyfile key.pem   (TLS)
      python server.py --admin YourName       (make an account an admin)
      python server.py --remove-admin Name    (take admin away)
"""

from __future__ import annotations

import argparse
import asyncio
import base64
import hashlib
import hmac
import ipaddress
import json
import logging
import os
import re
import sqlite3
import ssl
import time
from dataclasses import dataclass, field

log = logging.getLogger("campfire")

# ---------------------------------------------------------------- limits
MAX_LINE = 256 * 1024          # bytes per protocol line (avatars are the big ones)
MAX_TEXT = 2000                # characters per chat message
MAX_AVATAR = 48 * 1024         # bytes of decoded avatar image
HISTORY = 100                  # messages sent when opening a room or DM
LOGIN_TIMEOUT = 30             # seconds to finish logging in
RATE = 5.0                     # messages per second, sustained
BURST = 10.0                   # messages allowed in a burst
DEFAULT_ROOMS = ["#general", "#gaming"]

NAME_RE = re.compile(r"^[A-Za-z0-9_\-]{3,20}$")
ROOM_RE = re.compile(r"^#[a-z0-9_\-]{1,24}$")


# ---------------------------------------------------------------- storage
class Store:
    def __init__(self, path: str):
        self.db = sqlite3.connect(path)
        self.db.row_factory = sqlite3.Row
        self.db.executescript(
            """
            PRAGMA journal_mode=WAL;
            PRAGMA foreign_keys=ON;
            CREATE TABLE IF NOT EXISTS users (
                id      INTEGER PRIMARY KEY,
                name    TEXT UNIQUE COLLATE NOCASE NOT NULL,
                salt    BLOB NOT NULL,
                pwhash  BLOB NOT NULL,
                avatar  TEXT NOT NULL DEFAULT '',
                created REAL NOT NULL
            );
            CREATE TABLE IF NOT EXISTS rooms (
                name TEXT PRIMARY KEY
            );
            CREATE TABLE IF NOT EXISTS messages (
                id    INTEGER PRIMARY KEY,
                conv  TEXT NOT NULL,          -- '#room' or 'dm:<id>:<id>'
                uid   INTEGER NOT NULL REFERENCES users(id),
                text  TEXT NOT NULL,
                ts    REAL NOT NULL
            );
            CREATE INDEX IF NOT EXISTS messages_conv ON messages(conv, id);
            CREATE TABLE IF NOT EXISTS friends (      -- one row per direction
                a INTEGER NOT NULL REFERENCES users(id),
                b INTEGER NOT NULL REFERENCES users(id),
                PRIMARY KEY (a, b)
            );
            CREATE TABLE IF NOT EXISTS friend_requests (
                src INTEGER NOT NULL REFERENCES users(id),
                dst INTEGER NOT NULL REFERENCES users(id),
                PRIMARY KEY (src, dst)
            );
            CREATE TABLE IF NOT EXISTS ip_bans (
                ip     TEXT PRIMARY KEY,
                reason TEXT NOT NULL DEFAULT '',
                by     TEXT NOT NULL DEFAULT '',
                ts     REAL NOT NULL
            );
            """
        )
        # columns added after the first release: add them to older databases
        cols = {r["name"] for r in self.db.execute("PRAGMA table_info(users)")}
        for col, decl in (
            ("is_admin", "INTEGER NOT NULL DEFAULT 0"),
            ("banned", "INTEGER NOT NULL DEFAULT 0"),
            ("ban_reason", "TEXT NOT NULL DEFAULT ''"),
            ("muted_until", "REAL NOT NULL DEFAULT 0"),
            ("mute_reason", "TEXT NOT NULL DEFAULT ''"),
            ("last_ip", "TEXT NOT NULL DEFAULT ''"),
        ):
            if col not in cols:
                self.db.execute(f"ALTER TABLE users ADD COLUMN {col} {decl}")
        for r in DEFAULT_ROOMS:
            self.db.execute("INSERT OR IGNORE INTO rooms(name) VALUES (?)", (r,))
        self.db.commit()

    # users
    def user_by_name(self, name):
        return self.db.execute("SELECT * FROM users WHERE name=?", (name,)).fetchone()

    def user_by_id(self, uid):
        return self.db.execute("SELECT * FROM users WHERE id=?", (uid,)).fetchone()

    def create_user(self, name, salt, pwhash):
        cur = self.db.execute(
            "INSERT INTO users(name, salt, pwhash, created) VALUES (?,?,?,?)",
            (name, salt, pwhash, time.time()),
        )
        self.db.commit()
        return cur.lastrowid

    def rename(self, uid, name):
        self.db.execute("UPDATE users SET name=? WHERE id=?", (name, uid))
        self.db.commit()

    def set_avatar(self, uid, data):
        self.db.execute("UPDATE users SET avatar=? WHERE id=?", (data, uid))
        self.db.commit()

    def set_fields(self, uid, **fields):
        cols = ", ".join(f"{k}=?" for k in fields)
        self.db.execute(f"UPDATE users SET {cols} WHERE id=?", (*fields.values(), uid))
        self.db.commit()

    def banned_users(self):
        return self.db.execute("SELECT name, ban_reason FROM users WHERE banned=1 ORDER BY name").fetchall()

    def ip_bans(self):
        return self.db.execute("SELECT ip, reason FROM ip_bans ORDER BY ts").fetchall()

    def ip_banned(self, ip):
        return self.db.execute("SELECT reason FROM ip_bans WHERE ip=?", (ip,)).fetchone()

    def ban_ip(self, ip, reason, by):
        self.db.execute("INSERT OR REPLACE INTO ip_bans VALUES (?,?,?,?)", (ip, reason, by, time.time()))
        self.db.commit()

    def unban_ip(self, ip):
        n = self.db.execute("DELETE FROM ip_bans WHERE ip=?", (ip,)).rowcount
        self.db.commit()
        return n

    # rooms
    def rooms(self):
        return [r["name"] for r in self.db.execute("SELECT name FROM rooms ORDER BY rowid")]

    def add_room(self, name):
        self.db.execute("INSERT OR IGNORE INTO rooms(name) VALUES (?)", (name,))
        self.db.commit()

    def delete_room(self, name):
        self.db.execute("DELETE FROM rooms WHERE name=?", (name,))
        self.db.execute("DELETE FROM messages WHERE conv=?", (name,))
        self.db.commit()

    def clear_history(self, conv):
        n = self.db.execute("DELETE FROM messages WHERE conv=?", (conv,)).rowcount
        self.db.commit()
        return n

    # messages
    def add_message(self, conv, uid, text, ts):
        self.db.execute(
            "INSERT INTO messages(conv, uid, text, ts) VALUES (?,?,?,?)", (conv, uid, text, ts)
        )
        self.db.commit()

    def history(self, conv, limit=HISTORY):
        rows = self.db.execute(
            """SELECT u.name AS user, u.is_admin AS admin, m.text, m.ts FROM messages m
               JOIN users u ON u.id = m.uid
               WHERE m.conv=? ORDER BY m.id DESC LIMIT ?""",
            (conv, limit),
        ).fetchall()
        return [{**dict(r), "admin": bool(r["admin"])} for r in reversed(rows)]

    # friends
    def friend_ids(self, uid):
        return [r["b"] for r in self.db.execute("SELECT b FROM friends WHERE a=?", (uid,))]

    def incoming_requests(self, uid):
        return [r["src"] for r in self.db.execute("SELECT src FROM friend_requests WHERE dst=?", (uid,))]

    def has_request(self, src, dst):
        return self.db.execute(
            "SELECT 1 FROM friend_requests WHERE src=? AND dst=?", (src, dst)
        ).fetchone() is not None

    def add_request(self, src, dst):
        self.db.execute("INSERT OR IGNORE INTO friend_requests VALUES (?,?)", (src, dst))
        self.db.commit()

    def drop_request(self, src, dst):
        self.db.execute("DELETE FROM friend_requests WHERE src=? AND dst=?", (src, dst))
        self.db.commit()

    def make_friends(self, a, b):
        self.db.execute("INSERT OR IGNORE INTO friends VALUES (?,?)", (a, b))
        self.db.execute("INSERT OR IGNORE INTO friends VALUES (?,?)", (b, a))
        self.db.execute("DELETE FROM friend_requests WHERE (src=? AND dst=?) OR (src=? AND dst=?)", (a, b, b, a))
        self.db.commit()

    def unfriend(self, a, b):
        self.db.execute("DELETE FROM friends WHERE (a=? AND b=?) OR (a=? AND b=?)", (a, b, b, a))
        self.db.commit()


def hash_password(password: str, salt: bytes) -> bytes:
    return hashlib.scrypt(password.encode(), salt=salt, n=2**14, r=8, p=1, dklen=32)


def dm_key(a: int, b: int) -> str:
    lo, hi = sorted((a, b))
    return f"dm:{lo}:{hi}"


# ---------------------------------------------------------------- sessions
@dataclass(eq=False)
class Session:
    reader: asyncio.StreamReader
    writer: asyncio.StreamWriter
    peer: str
    ip: str = ""
    uid: int | None = None
    name: str = ""
    tokens: float = BURST
    last: float = field(default_factory=time.monotonic)
    closed: bool = False

    def send(self, obj: dict):
        if self.closed:
            return
        try:
            self.writer.write((json.dumps(obj, separators=(",", ":")) + "\n").encode())
        except Exception:
            self.closed = True

    def allow(self) -> bool:
        now = time.monotonic()
        self.tokens = min(BURST, self.tokens + (now - self.last) * RATE)
        self.last = now
        if self.tokens >= 1:
            self.tokens -= 1
            return True
        return False


class Server:
    def __init__(self, store: Store, admins=(), remove_admins=()):
        self.store = store
        self.online: dict[int, Session] = {}
        # names from --admin get admin when they log in (accounts may not exist yet)
        self.admin_names = {a.lower() for a in admins}
        for name in admins:
            row = store.user_by_name(name)
            if row:
                store.set_fields(row["id"], is_admin=1)
        for name in remove_admins:
            self.admin_names.discard(name.lower())
            row = store.user_by_name(name)
            if row:
                store.set_fields(row["id"], is_admin=0)
                log.info("removed admin from %s", row["name"])

    # -- helpers
    def broadcast(self, obj, exclude: Session | None = None):
        for s in list(self.online.values()):
            if s is not exclude:
                s.send(obj)

    def to_uid(self, uid, obj):
        s = self.online.get(uid)
        if s:
            s.send(obj)

    def user_entry(self, uid):
        row = self.store.user_by_id(uid)
        return {"name": row["name"], "avatar": row["avatar"], "admin": bool(row["is_admin"])}

    def social(self, uid):
        friends = []
        for fid in self.store.friend_ids(uid):
            row = self.store.user_by_id(fid)
            friends.append({"name": row["name"], "online": fid in self.online})
        friends.sort(key=lambda f: f["name"].lower())
        requests = sorted(self.store.user_by_id(r)["name"] for r in self.store.incoming_requests(uid))
        return {"t": "friends", "friends": friends, "requests": requests}

    def push_social(self, *uids):
        for uid in uids:
            if uid in self.online:
                self.online[uid].send(self.social(uid))

    # -- connection lifecycle
    async def handle(self, reader, writer):
        addr = writer.get_extra_info("peername")[:2]
        peer = "%s:%s" % addr
        s = Session(reader, writer, peer, ip=str(addr[0]))
        log.info("connect %s", peer)
        ban = self.store.ip_banned(s.ip)
        if ban:
            log.info("refused banned address %s", s.ip)
            s.send({"t": "kicked", "reason": "This address is banned" + (f": {ban['reason']}" if ban["reason"] else "")})
            s.uid = -1                       # makes drop() close it quietly
            await self.drop(s, announce=False)
            return
        try:
            if await asyncio.wait_for(self.login(s), LOGIN_TIMEOUT):
                await self.loop(s)
        except (asyncio.TimeoutError, asyncio.IncompleteReadError, ConnectionError, ValueError):
            # ValueError: a line longer than MAX_LINE
            pass
        except Exception:
            log.exception("error in session %s", peer)
        finally:
            await self.drop(s)

    async def read(self, s: Session):
        line = await s.reader.readline()
        if not line:
            raise ConnectionError("closed")
        if not line.endswith(b"\n"):
            raise ConnectionError("connection closed mid-line")
        try:
            obj = json.loads(line)
            if not isinstance(obj, dict):
                raise ValueError
            return obj
        except ValueError:
            s.send({"t": "error", "msg": "Malformed message"})
            return None

    async def login(self, s: Session) -> bool:
        s.send({"t": "hello", "version": 2})
        while True:
            msg = await self.read(s)
            if not msg or msg.get("t") != "login":
                continue
            name = str(msg.get("user", "")).strip()
            password = str(msg.get("pass", ""))
            if not NAME_RE.match(name):
                s.send({"t": "login_fail", "reason": "Username must be 3-20 letters, numbers, _ or -"})
                continue
            if len(password) < 4:
                s.send({"t": "login_fail", "reason": "Password must be at least 4 characters"})
                continue

            loop = asyncio.get_running_loop()
            row = self.store.user_by_name(name)
            if row is None:
                salt = os.urandom(16)
                pw = await loop.run_in_executor(None, hash_password, password, salt)
                if self.store.user_by_name(name) is not None:   # raced with another register
                    s.send({"t": "login_fail", "reason": "Try again"})
                    continue
                uid = self.store.create_user(name, salt, pw)
                registered = True
                log.info("registered %s from %s", name, s.peer)
            else:
                pw = await loop.run_in_executor(None, hash_password, password, row["salt"])
                if not hmac.compare_digest(pw, row["pwhash"]):
                    await asyncio.sleep(1)                       # slow down guessing
                    s.send({"t": "login_fail", "reason": "Wrong password for that username"})
                    continue
                uid, registered = row["id"], False
                if row["banned"]:
                    reason = row["ban_reason"]
                    s.send({"t": "login_fail", "reason": "This account is banned" + (f": {reason}" if reason else "")})
                    continue

            self.store.set_fields(uid, last_ip=s.ip)
            if self.store.user_by_id(uid)["name"].lower() in self.admin_names:
                self.store.set_fields(uid, is_admin=1)

            old = self.online.get(uid)
            if old:
                old.send({"t": "kicked", "reason": "Logged in from another location"})
                await self.drop(old, announce=False)

            me = self.store.user_by_id(uid)
            s.uid, s.name = uid, me["name"]
            self.online[uid] = s
            s.send({
                "t": "login_ok",
                "user": s.name,
                "admin": bool(me["is_admin"]),
                "registered": registered,
                "rooms": self.store.rooms(),
                "users": [self.user_entry(u) for u in self.online],
            })
            s.send(self.social(uid))
            if me["muted_until"] > time.time():
                s.send({"t": "muted", "until": me["muted_until"], "reason": me["mute_reason"]})
            self.broadcast({"t": "user_join", **self.user_entry(uid)}, exclude=s)
            for fid in self.store.friend_ids(uid):
                self.push_social(fid)
            log.info("login %s (%s)", s.name, s.peer)
            return True

    async def drop(self, s: Session, announce=True):
        if s.closed and s.uid is None:
            return
        was_online = s.uid is not None and s.uid >= 0 and self.online.get(s.uid) is s
        if was_online:
            del self.online[s.uid]
        s.closed = True
        try:
            s.writer.close()
            await asyncio.wait_for(s.writer.wait_closed(), 2)
        except Exception:
            pass
        if was_online and announce:
            self.broadcast({"t": "user_leave", "name": s.name})
            for fid in self.store.friend_ids(s.uid):
                self.push_social(fid)
            log.info("logout %s", s.name)
        s.uid = None

    async def loop(self, s: Session):
        while not s.closed:
            msg = await self.read(s)
            if msg is None:
                continue
            kind = msg.get("t")
            handler = getattr(self, f"on_{kind}", None) if isinstance(kind, str) else None
            if handler is None:
                s.send({"t": "error", "msg": f"Unknown command {kind!r}"})
                continue
            if kind in ("msg", "dm", "friend_request", "create_room"):
                if not s.allow():
                    s.send({"t": "error", "msg": "Slow down - you're sending messages too fast"})
                    continue
                if kind != "friend_request" and self.muted_message(s):
                    continue
            await handler(s, msg)
            await s.writer.drain()

    # -- handlers
    def clean_text(self, s, msg):
        text = str(msg.get("text", "")).replace("\r", "").strip()
        if not text:
            return None
        if len(text) > MAX_TEXT:
            s.send({"t": "error", "msg": f"Message too long (max {MAX_TEXT} characters)"})
            return None
        return text

    async def on_ping(self, s, msg):
        s.send({"t": "pong"})

    async def on_history(self, s, msg):
        room = str(msg.get("room", ""))
        if room not in self.store.rooms():
            s.send({"t": "error", "msg": f"No room {room}"})
            return
        s.send({"t": "history", "room": room, "messages": self.store.history(room)})

    async def on_create_room(self, s, msg):
        room = str(msg.get("room", "")).strip().lower()
        if not room.startswith("#"):
            room = "#" + room
        if not ROOM_RE.match(room):
            s.send({"t": "error", "msg": "Room names are 1-24 lowercase letters, numbers, _ or -"})
            return
        if room not in self.store.rooms():
            self.store.add_room(room)
            self.broadcast({"t": "rooms", "rooms": self.store.rooms()})

    async def on_msg(self, s, msg):
        room = str(msg.get("room", ""))
        text = self.clean_text(s, msg)
        if text is None:
            return
        if room not in self.store.rooms():
            s.send({"t": "error", "msg": f"No room {room}"})
            return
        ts = time.time()
        self.store.add_message(room, s.uid, text, ts)
        self.broadcast({"t": "msg", "room": room, "user": s.name, "text": text, "ts": ts,
                        "admin": self.is_admin(s)})

    async def on_dm(self, s, msg):
        text = self.clean_text(s, msg)
        if text is None:
            return
        target = self.store.user_by_name(str(msg.get("to", "")))
        if target is None:
            s.send({"t": "error", "msg": "No such user"})
            return
        ts = time.time()
        self.store.add_message(dm_key(s.uid, target["id"]), s.uid, text, ts)
        out = {"t": "dm", "from": s.name, "to": target["name"], "text": text, "ts": ts,
               "admin": self.is_admin(s)}
        s.send(out)
        if target["id"] != s.uid:
            self.to_uid(target["id"], out)

    async def on_dm_history(self, s, msg):
        target = self.store.user_by_name(str(msg.get("with", "")))
        if target is None:
            s.send({"t": "error", "msg": "No such user"})
            return
        s.send({
            "t": "dm_history",
            "with": target["name"],
            "messages": self.store.history(dm_key(s.uid, target["id"])),
        })

    async def on_friend_request(self, s, msg):
        target = self.store.user_by_name(str(msg.get("to", "")))
        if target is None or target["id"] == s.uid:
            s.send({"t": "error", "msg": "No such user"})
            return
        tid = target["id"]
        if tid in self.store.friend_ids(s.uid):
            s.send({"t": "notice", "msg": f"You're already friends with {target['name']}"})
            return
        if self.store.has_request(tid, s.uid):           # they already asked us: accept
            self.store.make_friends(s.uid, tid)
            self.push_social(s.uid, tid)
            return
        self.store.add_request(s.uid, tid)
        s.send({"t": "notice", "msg": f"Friend request sent to {target['name']}"})
        self.push_social(tid)

    async def on_friend_respond(self, s, msg):
        other = self.store.user_by_name(str(msg.get("user", "")))
        if other is None or not self.store.has_request(other["id"], s.uid):
            return
        if msg.get("accept"):
            self.store.make_friends(s.uid, other["id"])
        else:
            self.store.drop_request(other["id"], s.uid)
        self.push_social(s.uid, other["id"])

    async def on_friend_remove(self, s, msg):
        other = self.store.user_by_name(str(msg.get("user", "")))
        if other is None:
            return
        self.store.unfriend(s.uid, other["id"])
        self.push_social(s.uid, other["id"])

    async def on_avatar(self, s, msg):
        data = str(msg.get("data", ""))
        if data:
            try:
                raw = base64.b64decode(data, validate=True)
            except ValueError:
                s.send({"t": "error", "msg": "Avatar is not valid base64"})
                return
            if len(raw) > MAX_AVATAR:
                s.send({"t": "error", "msg": "Avatar image is too big"})
                return
        self.store.set_avatar(s.uid, data)
        self.broadcast({"t": "user_update", "old": s.name, **self.user_entry(s.uid)})

    async def on_rename(self, s, msg):
        name = str(msg.get("name", "")).strip()
        if not NAME_RE.match(name):
            s.send({"t": "error", "msg": "Username must be 3-20 letters, numbers, _ or -"})
            return
        existing = self.store.user_by_name(name)
        if existing is not None and existing["id"] != s.uid:
            s.send({"t": "error", "msg": "That username is taken"})
            return
        old, s.name = s.name, name
        self.store.rename(s.uid, name)
        s.send({"t": "renamed", "name": name})
        self.broadcast({"t": "user_update", "old": old, **self.user_entry(s.uid)})
        for fid in self.store.friend_ids(s.uid):
            self.push_social(fid)
        log.info("rename %s -> %s", old, name)


    # -- admin
    def is_admin(self, s: Session) -> bool:
        row = self.store.user_by_id(s.uid)
        return bool(row and row["is_admin"])

    def muted_message(self, s: Session) -> bool:
        """Tell a muted user they can't talk. Returns True if they are muted."""
        row = self.store.user_by_id(s.uid)
        left = row["muted_until"] - time.time()
        if left <= 0:
            return False
        if left > 10 * 365 * 86400:
            when = "until an admin unmutes you"
        else:
            mins = int(left // 60) + 1
            when = f"for {mins} more minute{'s' if mins != 1 else ''}"
        reason = f" ({row['mute_reason']})" if row["mute_reason"] else ""
        s.send({"t": "error", "msg": f"You are muted {when}{reason}"})
        return True

    async def kick(self, uid, reason):
        target = self.online.get(uid)
        if target:
            target.send({"t": "kicked", "reason": reason})
            await self.drop(target)

    def target_user(self, s, msg, allow_admin=False):
        """Look up the user an admin command is aimed at, refusing self and other admins."""
        row = self.store.user_by_name(str(msg.get("user", "")).strip())
        if row is None:
            s.send({"t": "error", "msg": "No such user"})
            return None
        if row["id"] == s.uid:
            s.send({"t": "error", "msg": "You can't do that to yourself"})
            return None
        if row["is_admin"] and not allow_admin:
            s.send({"t": "error", "msg": "You can't do that to another admin"})
            return None
        return row

    def room_arg(self, s, msg):
        room = str(msg.get("room", "")).strip().lower()
        if room and not room.startswith("#"):
            room = "#" + room
        if room not in self.store.rooms():
            s.send({"t": "error", "msg": f"No room {room or '(none given)'}"})
            return None
        return room

    async def on_admin(self, s, msg):
        if not self.is_admin(s):
            s.send({"t": "error", "msg": "That command is for admins only"})
            return
        cmd = str(msg.get("cmd", ""))
        handler = getattr(self, f"admin_{cmd}", None)
        if handler is None:
            s.send({"t": "error", "msg": f"Unknown admin command {cmd!r}"})
            return
        reason = str(msg.get("reason", "")).strip()[:200]
        result = await handler(s, msg, reason)
        if result:
            log.info("admin %s: %s", s.name, result)
            s.send({"t": "notice", "msg": result})

    async def admin_kick(self, s, msg, reason):
        row = self.target_user(s, msg)
        if not row:
            return
        if row["id"] not in self.online:
            s.send({"t": "error", "msg": f"{row['name']} isn't online"})
            return
        await self.kick(row["id"], f"Kicked by {s.name}" + (f": {reason}" if reason else ""))
        return f"Kicked {row['name']}"

    async def admin_ban(self, s, msg, reason):
        row = self.target_user(s, msg)
        if not row:
            return
        self.store.set_fields(row["id"], banned=1, ban_reason=reason)
        await self.kick(row["id"], f"You were banned by {s.name}" + (f": {reason}" if reason else ""))
        return f"Banned {row['name']}"

    async def admin_unban(self, s, msg, reason):
        row = self.store.user_by_name(str(msg.get("user", "")).strip())
        if row is None or not row["banned"]:
            s.send({"t": "error", "msg": "That user isn't banned"})
            return
        self.store.set_fields(row["id"], banned=0, ban_reason="")
        return f"Unbanned {row['name']}"

    async def admin_ipban(self, s, msg, reason):
        """Ban by username (uses their last address) or by a raw IP address."""
        who = str(msg.get("user", "")).strip()
        row = self.store.user_by_name(who)
        if row is not None:
            row = self.target_user(s, msg)
            if not row:
                return
            ip = row["last_ip"]
            if not ip:
                s.send({"t": "error", "msg": f"No known address for {row['name']}"})
                return
        else:
            try:
                ip = str(ipaddress.ip_address(who))
            except ValueError:
                s.send({"t": "error", "msg": "Give a username or an IP address"})
                return
        if ip == s.ip:
            s.send({"t": "error", "msg": "That's your own address"})
            return
        self.store.ban_ip(ip, reason, s.name)
        kicked = []
        for sess in list(self.online.values()):
            if sess.ip == ip and not self.is_admin(sess):
                kicked.append(sess.name)
                sess.send({"t": "kicked", "reason": "This address was banned" + (f": {reason}" if reason else "")})
                await self.drop(sess)
        extra = f" (disconnected {', '.join(kicked)})" if kicked else ""
        return f"Banned address {ip}{extra}"

    async def admin_unipban(self, s, msg, reason):
        who = str(msg.get("user", "")).strip()
        row = self.store.user_by_name(who)
        ip = row["last_ip"] if row else who
        if not self.store.unban_ip(ip):
            s.send({"t": "error", "msg": f"{ip or who} isn't banned"})
            return
        return f"Unbanned address {ip}"

    async def admin_mute(self, s, msg, reason):
        row = self.target_user(s, msg)
        if not row:
            return
        try:
            minutes = float(msg.get("minutes") or 0)
        except (TypeError, ValueError):
            minutes = 0
        until = time.time() + minutes * 60 if minutes > 0 else 1e18
        self.store.set_fields(row["id"], muted_until=until, mute_reason=reason)
        self.to_uid(row["id"], {"t": "muted", "until": until, "reason": reason, "by": s.name})
        span = f"for {minutes:g} minute{'s' if minutes != 1 else ''}" if minutes > 0 else "until unmuted"
        return f"Muted {row['name']} {span}"

    async def admin_unmute(self, s, msg, reason):
        row = self.store.user_by_name(str(msg.get("user", "")).strip())
        if row is None or row["muted_until"] <= time.time():
            s.send({"t": "error", "msg": "That user isn't muted"})
            return
        self.store.set_fields(row["id"], muted_until=0, mute_reason="")
        self.to_uid(row["id"], {"t": "notice", "msg": f"{s.name} unmuted you"})
        return f"Unmuted {row['name']}"

    async def admin_delroom(self, s, msg, reason):
        room = self.room_arg(s, msg)
        if not room:
            return
        if room == DEFAULT_ROOMS[0]:
            s.send({"t": "error", "msg": f"{room} can't be deleted"})
            return
        self.store.delete_room(room)
        self.broadcast({"t": "rooms", "rooms": self.store.rooms()})
        return f"Deleted {room}"

    async def admin_clearhistory(self, s, msg, reason):
        room = self.room_arg(s, msg)
        if not room:
            return
        n = self.store.clear_history(room)
        self.broadcast({"t": "cleared", "room": room, "by": s.name})
        return f"Cleared {n} message{'s' if n != 1 else ''} from {room}"

    async def admin_announce(self, s, msg, reason):
        text = self.clean_text(s, msg)
        if text is None:
            return
        self.broadcast({"t": "announce", "from": s.name, "text": text, "ts": time.time()})
        log.info("announce from %s: %s", s.name, text)

    async def admin_resetpass(self, s, msg, reason):
        row = self.target_user(s, msg)
        if not row:
            return
        password = str(msg.get("password", ""))
        if len(password) < 4:
            s.send({"t": "error", "msg": "New password must be at least 4 characters"})
            return
        salt = os.urandom(16)
        pw = await asyncio.get_running_loop().run_in_executor(None, hash_password, password, salt)
        self.store.set_fields(row["id"], salt=salt, pwhash=pw)
        await self.kick(row["id"], f"{s.name} reset your password - log in with the new one")
        return f"Reset the password for {row['name']}"

    async def admin_bans(self, s, msg, reason):
        users = self.store.banned_users()
        ips = self.store.ip_bans()
        if not users and not ips:
            s.send({"t": "notice", "msg": "Nobody is banned"})
            return
        lines = [f"{r['name']}" + (f" - {r['ban_reason']}" if r["ban_reason"] else "") for r in users]
        lines += [f"IP {r['ip']}" + (f" - {r['reason']}" if r["reason"] else "") for r in ips]
        s.send({"t": "notice", "msg": "Banned: " + "; ".join(lines)})


async def main():
    ap = argparse.ArgumentParser(description="Campfire chat server")
    ap.add_argument("--host", default="0.0.0.0")
    ap.add_argument("--port", type=int, default=55555)
    ap.add_argument("--db", default="campfire.db")
    ap.add_argument("--certfile", help="TLS certificate (PEM) - enables TLS")
    ap.add_argument("--keyfile", help="TLS private key (PEM)")
    ap.add_argument("--admin", action="append", default=[], metavar="NAME",
                    help="make this account an admin (repeatable; works before the account exists)")
    ap.add_argument("--remove-admin", action="append", default=[], metavar="NAME",
                    help="take admin away from this account")
    args = ap.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

    tls = None
    if args.certfile:
        tls = ssl.create_default_context(ssl.Purpose.CLIENT_AUTH)
        tls.load_cert_chain(args.certfile, args.keyfile)

    server = Server(Store(args.db), args.admin, args.remove_admin)
    for name in args.admin:
        log.info("admin: %s", name)
    srv = await asyncio.start_server(server.handle, args.host, args.port, limit=MAX_LINE, ssl=tls)
    log.info("Campfire server listening on %s:%d%s", args.host, args.port, " (TLS)" if tls else "")
    async with srv:
        await srv.serve_forever()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
