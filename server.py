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
import secrets
import shutil
import sqlite3
import ssl
import sys
import time
from dataclasses import dataclass, field

log = logging.getLogger("campfire")

# ---------------------------------------------------------------- limits
MAX_LINE = 2 * 1024 * 1024     # bytes per protocol line (video keyframes are the big ones)
MAX_TEXT = 2000                # characters per chat message
MAX_AVATAR = 48 * 1024         # bytes of decoded avatar image
HISTORY = 100                  # messages sent when opening a room or DM
LOGIN_TIMEOUT = 30             # seconds to finish logging in
RATE = 5.0                     # messages per second, sustained
BURST = 10.0                   # messages allowed in a burst
DEFAULT_ROOMS = ["#general", "#gaming"]
GROUP_MAX = 50                 # people in one group DM, including its creator
DEFAULT_VOICE = ["Lounge", "Gaming"]
VOICE_MAX = 25                 # people in one voice channel
VOICE_PACKET_MAX = 2000        # characters of base64 per audio packet (Opus 20 ms is ~130)
VOICE_RATE = 60                # audio packets per second allowed per speaker
VOICE_BACKLOG = 256 * 1024     # skip audio to a listener whose connection is this far behind
VIDEO_KINDS = ("camera", "screen")
VIDEO_PACKET_MAX = 1536 * 1024 # characters of base64 per video frame
VIDEO_RATE = 40                # video frames per second allowed per stream
VIDEO_BACKLOG = 1024 * 1024    # a viewer this far behind skips frames until the next keyframe
MAX_FILE = 5 * 1024 ** 3      # 5 GB per file
# where the server keeps its data: next to server.py, or next to Campfire Server.exe when packaged
DATA_DIR = (os.path.dirname(os.path.abspath(sys.executable)) if getattr(sys, "frozen", False)
            else os.path.dirname(os.path.abspath(__file__)))
DEFAULT_FILES_DIR = os.path.join(DATA_DIR, "campfire_files")
DEFAULT_DB = os.path.join(DATA_DIR, "campfire.db")
FILE_CHUNK = 1024 * 1024       # bytes per read/write while transferring
DISK_MARGIN = 512 * 1024 ** 2  # always leave this much free on the server's disk
PARTIAL_MAX_AGE = 2 * 86400    # unfinished uploads are kept this long so they can resume
TRANSFERS_PER_USER = 4         # file connections one person can have open at once
UPLOAD_ID_RE = re.compile(r"^[0-9a-f]{16,64}$")
VOICE_RE = re.compile(r"^[A-Za-z0-9 _\-]{1,24}$")
GROUP_NAME_MAX = 40

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
            CREATE TABLE IF NOT EXISTS groups (
                id      INTEGER PRIMARY KEY,
                name    TEXT NOT NULL,
                owner   INTEGER NOT NULL REFERENCES users(id),
                created REAL NOT NULL
            );
            CREATE TABLE IF NOT EXISTS group_members (
                gid    INTEGER NOT NULL REFERENCES groups(id) ON DELETE CASCADE,
                uid    INTEGER NOT NULL REFERENCES users(id),
                joined REAL NOT NULL,
                PRIMARY KEY (gid, uid)
            );
            CREATE TABLE IF NOT EXISTS files (
                id      INTEGER PRIMARY KEY,
                uid     INTEGER NOT NULL REFERENCES users(id),
                conv    TEXT NOT NULL,
                name    TEXT NOT NULL,
                size    INTEGER NOT NULL,
                path    TEXT NOT NULL,
                created REAL NOT NULL
            );
            CREATE INDEX IF NOT EXISTS files_conv ON files(conv);
            CREATE TABLE IF NOT EXISTS voice_rooms (
                name TEXT PRIMARY KEY COLLATE NOCASE
            );
            CREATE TABLE IF NOT EXISTS settings (
                key   TEXT PRIMARY KEY,
                value TEXT NOT NULL
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
        if "file_id" not in {r["name"] for r in self.db.execute("PRAGMA table_info(messages)")}:
            self.db.execute("ALTER TABLE messages ADD COLUMN file_id INTEGER")
        for r in DEFAULT_ROOMS:
            self.db.execute("INSERT OR IGNORE INTO rooms(name) VALUES (?)", (r,))
        for r in DEFAULT_VOICE:
            self.db.execute("INSERT OR IGNORE INTO voice_rooms(name) VALUES (?)", (r,))
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

    def setting(self, key, default=""):
        row = self.db.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
        return row["value"] if row else default

    def set_setting(self, key, value):
        self.db.execute("INSERT OR REPLACE INTO settings(key, value) VALUES (?,?)", (key, value))
        self.db.commit()

    def all_users(self):
        return self.db.execute("SELECT * FROM users ORDER BY name COLLATE NOCASE").fetchall()

    def counts(self):
        one = lambda q: self.db.execute(q).fetchone()[0]
        return {"accounts": one("SELECT COUNT(*) FROM users"), "messages": one("SELECT COUNT(*) FROM messages"),
                "groups": one("SELECT COUNT(*) FROM groups"), "files": one("SELECT COUNT(*) FROM files"),
                "file_bytes": one("SELECT COALESCE(SUM(size), 0) FROM files")}

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

    # group DMs
    def create_group(self, name, owner, member_ids):
        now = time.time()
        gid = self.db.execute("INSERT INTO groups(name, owner, created) VALUES (?,?,?)",
                              (name, owner, now)).lastrowid
        self.db.executemany("INSERT OR IGNORE INTO group_members VALUES (?,?,?)",
                            [(gid, uid, now) for uid in member_ids])
        self.db.commit()
        return gid

    def group(self, gid):
        return self.db.execute("SELECT * FROM groups WHERE id=?", (gid,)).fetchone()

    def group_member_ids(self, gid):
        return [r["uid"] for r in self.db.execute(
            "SELECT uid FROM group_members WHERE gid=? ORDER BY joined, uid", (gid,))]

    def groups_of(self, uid):
        return [r["gid"] for r in self.db.execute("SELECT gid FROM group_members WHERE uid=?", (uid,))]

    def add_group_members(self, gid, uids):
        now = time.time()
        self.db.executemany("INSERT OR IGNORE INTO group_members VALUES (?,?,?)", [(gid, u, now) for u in uids])
        self.db.commit()

    def remove_group_member(self, gid, uid):
        self.db.execute("DELETE FROM group_members WHERE gid=? AND uid=?", (gid, uid))
        self.db.commit()

    def update_group(self, gid, **fields):
        cols = ", ".join(f"{k}=?" for k in fields)
        self.db.execute(f"UPDATE groups SET {cols} WHERE id=?", (*fields.values(), gid))
        self.db.commit()

    def delete_group(self, gid):
        self.db.execute("DELETE FROM messages WHERE conv=?", (f"g:{gid}",))
        self.db.execute("DELETE FROM group_members WHERE gid=?", (gid,))
        self.db.execute("DELETE FROM groups WHERE id=?", (gid,))
        self.db.commit()

    # voice channels
    def voice_rooms(self):
        return [r["name"] for r in self.db.execute("SELECT name FROM voice_rooms ORDER BY rowid")]

    def voice_room(self, name):
        row = self.db.execute("SELECT name FROM voice_rooms WHERE name=?", (name,)).fetchone()
        return row["name"] if row else None

    def add_voice_room(self, name):
        self.db.execute("INSERT OR IGNORE INTO voice_rooms(name) VALUES (?)", (name,))
        self.db.commit()

    def delete_voice_room(self, name):
        self.db.execute("DELETE FROM voice_rooms WHERE name=?", (name,))
        self.db.commit()

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
    def add_message(self, conv, uid, text, ts, file_id=None):
        self.db.execute(
            "INSERT INTO messages(conv, uid, text, ts, file_id) VALUES (?,?,?,?,?)", (conv, uid, text, ts, file_id)
        )
        self.db.commit()

    def history(self, conv, limit=HISTORY):
        rows = self.db.execute(
            """SELECT u.name AS user, u.is_admin AS admin, m.text, m.ts, m.file_id,
                      f.name AS fname, f.size AS fsize FROM messages m
               JOIN users u ON u.id = m.uid
               LEFT JOIN files f ON f.id = m.file_id
               WHERE m.conv=? ORDER BY m.id DESC LIMIT ?""",
            (conv, limit),
        ).fetchall()
        out = []
        for r in reversed(rows):
            m = {"user": r["user"], "admin": bool(r["admin"]), "text": r["text"], "ts": r["ts"]}
            if r["file_id"] is not None:
                m["file"] = ({"id": r["file_id"], "name": r["fname"], "size": r["fsize"]} if r["fname"] is not None
                             else {"id": r["file_id"], "deleted": True})
            out.append(m)
        return out

    # files
    def add_file(self, uid, conv, name, size, path):
        fid = self.db.execute("INSERT INTO files(uid, conv, name, size, path, created) VALUES (?,?,?,?,?,?)",
                              (uid, conv, name, size, path, time.time())).lastrowid
        self.db.commit()
        return fid

    def file(self, fid):
        return self.db.execute("SELECT * FROM files WHERE id=?", (fid,)).fetchone()

    def files_in(self, conv):
        return self.db.execute("SELECT * FROM files WHERE conv=?", (conv,)).fetchall()

    def delete_file_row(self, fid):
        self.db.execute("DELETE FROM files WHERE id=?", (fid,))
        self.db.commit()

    def all_files(self):
        return self.db.execute(
            """SELECT f.*, u.name AS uploader FROM files f JOIN users u ON u.id = f.uid
               ORDER BY f.created DESC""").fetchall()

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
    vtokens: float = VOICE_RATE
    vlast: float = field(default_factory=time.monotonic)
    video_tokens: dict = field(default_factory=dict)      # kind -> (tokens, last)
    transfer: bool = False                                 # a file-transfer connection, not a chat one
    closed: bool = False

    def send(self, obj: dict):
        if self.closed:
            return
        try:
            self.writer.write((json.dumps(obj, separators=(",", ":")) + "\n").encode())
        except Exception:
            self.closed = True

    def allow_voice(self) -> bool:
        now = time.monotonic()
        self.vtokens = min(VOICE_RATE, self.vtokens + (now - self.vlast) * VOICE_RATE)
        self.vlast = now
        if self.vtokens >= 1:
            self.vtokens -= 1
            return True
        return False

    def allow_video(self, kind) -> bool:
        now = time.monotonic()
        tokens, last = self.video_tokens.get(kind, (VIDEO_RATE, now))
        tokens = min(VIDEO_RATE, tokens + (now - last) * VIDEO_RATE)
        ok = tokens >= 1
        self.video_tokens[kind] = (tokens - 1 if ok else tokens, now)
        return ok

    def backlog(self) -> int:
        try:
            return self.writer.transport.get_write_buffer_size()
        except Exception:
            return 0

    def allow(self) -> bool:
        now = time.monotonic()
        self.tokens = min(BURST, self.tokens + (now - self.last) * RATE)
        self.last = now
        if self.tokens >= 1:
            self.tokens -= 1
            return True
        return False


class ConsoleSession:
    """Stands in for an admin when the server owner acts from the server window."""
    console = True

    def __init__(self):
        self.uid, self.name, self.ip = None, "Server", ""
        self.sent: list[dict] = []
        self.closed = False

    def send(self, obj):
        self.sent.append(obj)


class Server:
    def __init__(self, store: Store, admins=(), remove_admins=(), files_dir="campfire_files"):
        self.store = store
        self.files_dir = os.path.abspath(files_dir)
        self.partial_dir = os.path.join(self.files_dir, "partial")
        os.makedirs(self.partial_dir, exist_ok=True)
        for name in os.listdir(self.partial_dir):              # forget uploads abandoned long ago
            path = os.path.join(self.partial_dir, name)
            try:
                if time.time() - os.path.getmtime(path) > PARTIAL_MAX_AGE:
                    os.remove(path)
            except OSError:
                pass
        self.transfers: dict[int, set] = {}                   # uid -> open file-transfer sessions
        self.online: dict[int, Session] = {}
        self.voice: dict[str, dict[int, dict]] = {}      # channel -> uid -> {"muted", "deaf"}
        self.voice_of: dict[int, str] = {}               # uid -> channel they're in
        self.streams: dict[tuple, set] = {}              # (uid, kind) -> viewer uids
        self.waiting_key: set[tuple] = set()             # (viewer, uid, kind) waiting for a keyframe
        self.key_asked: dict[tuple, float] = {}          # (uid, kind) -> last keyframe request time
        self.last_voice_payload = None
        self.started = time.time()
        self.last_call_state: dict[int, list] = {}       # gid -> users last sent to the group
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
            result = await asyncio.wait_for(self.login(s), LOGIN_TIMEOUT)
            if result == "transfer":
                await self.transfer_loop(s)
            elif result:
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

            if msg.get("transfer"):                     # second connection just for files
                open_now = self.transfers.setdefault(uid, set())
                if len(open_now) >= TRANSFERS_PER_USER:
                    s.send({"t": "login_fail", "reason": "Too many transfers at once - wait for one to finish"})
                    continue
                s.uid, s.name, s.transfer = uid, self.store.user_by_id(uid)["name"], True
                open_now.add(s)
                s.send({"t": "login_ok", "user": s.name, "transfer": True})
                return "transfer"

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
            s.send(self.groups_payload(uid))
            s.send(self.voice_payload())
            s.send(self.config_payload())
            for gid in self.store.groups_of(uid):
                if f"g:{gid}" in self.voice:
                    s.send(self.call_payload(gid))
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
        if s.transfer and s.uid is not None:
            self.transfers.get(s.uid, set()).discard(s)
        was_online = s.uid is not None and s.uid >= 0 and self.online.get(s.uid) is s
        if was_online:
            del self.online[s.uid]
        s.closed = True
        try:
            s.writer.close()
            await asyncio.wait_for(s.writer.wait_closed(), 2)
        except Exception:
            pass
        if was_online and s.uid in self.voice_of:
            self.voice_remove(s.uid)
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
            if kind == "voice_data":
                self.on_voice_data(s, msg)
                continue
            if kind == "video_data":
                self.on_video_data(s, msg)
                continue
            if kind in ("msg", "dm", "group_msg", "group_create", "friend_request", "create_room", "voice_create"):
                if not s.allow():
                    s.send({"t": "error", "msg": "Slow down - you're sending messages too fast"})
                    continue
                if kind in ("msg", "dm", "group_msg", "create_room") and self.muted_message(s):
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
        self.deliver_room(s.uid, room, text)

    # -- storing and sending messages (also used for file uploads)
    def file_info(self, fid):
        row = self.store.file(fid)
        return {"id": row["id"], "name": row["name"], "size": row["size"]} if row else None

    def _out(self, uid, text, ts, fid):
        row = self.store.user_by_id(uid)
        out = {"user": row["name"], "text": text, "ts": ts, "admin": bool(row["is_admin"])}
        if fid is not None:
            out["file"] = self.file_info(fid)
        return out

    def deliver_room(self, uid, room, text, fid=None):
        ts = time.time()
        self.store.add_message(room, uid, text, ts, fid)
        self.broadcast({"t": "msg", "room": room, **self._out(uid, text, ts, fid)})

    def deliver_dm(self, uid, target_id, text, fid=None):
        ts = time.time()
        self.store.add_message(dm_key(uid, target_id), uid, text, ts, fid)
        o = self._out(uid, text, ts, fid)
        out = {"t": "dm", "from": o.pop("user"), "to": self.store.user_by_id(target_id)["name"], **o}
        self.to_uid(uid, out)
        if target_id != uid:
            self.to_uid(target_id, out)

    def deliver_group(self, uid, gid, text, fid=None):
        ts = time.time()
        self.store.add_message(f"g:{gid}", uid, text, ts, fid)
        out = {"t": "group_msg", "gid": gid, **self._out(uid, text, ts, fid)}
        for member in self.store.group_member_ids(gid):
            self.to_uid(member, out)

    async def on_dm(self, s, msg):
        text = self.clean_text(s, msg)
        if text is None:
            return
        target = self.store.user_by_name(str(msg.get("to", "")))
        if target is None:
            s.send({"t": "error", "msg": "No such user"})
            return
        self.deliver_dm(s.uid, target["id"], text)

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
        for gid in self.store.groups_of(s.uid):
            self.push_groups(*self.store.group_member_ids(gid))
        if s.uid in self.voice_of:
            self.voice_update()
        log.info("rename %s -> %s", old, name)


    # -- group DMs
    def group_info(self, gid):
        g = self.store.group(gid)
        members = [self.store.user_by_id(u)["name"] for u in self.store.group_member_ids(gid)]
        return {"id": gid, "name": g["name"], "owner": self.store.user_by_id(g["owner"])["name"],
                "members": members}

    def groups_payload(self, uid):
        return {"t": "groups", "groups": [self.group_info(g) for g in sorted(self.store.groups_of(uid))]}

    def push_groups(self, *uids):
        for uid in set(uids):
            if uid in self.online:
                self.online[uid].send(self.groups_payload(uid))

    def group_event(self, gid, text, extra_uids=()):
        """Tell everyone in a group (and anyone just removed) what changed."""
        members = self.store.group_member_ids(gid) if self.store.group(gid) else []
        for uid in set(members) | set(extra_uids):
            self.to_uid(uid, {"t": "group_notice", "gid": gid, "text": text, "ts": time.time()})

    def my_group(self, s, msg):
        try:
            gid = int(msg.get("gid"))
        except (TypeError, ValueError):
            gid = None
        if gid is None or self.store.group(gid) is None or s.uid not in self.store.group_member_ids(gid):
            s.send({"t": "error", "msg": "You're not in that group"})
            return None
        return gid

    def users_by_names(self, s, names):
        if not isinstance(names, list):
            names = []
        found, missing = {}, []
        for n in names[:GROUP_MAX * 2]:
            row = self.store.user_by_name(str(n).strip())
            if row is None:
                missing.append(str(n))
            elif row["id"] != s.uid and not row["banned"]:
                found[row["id"]] = row["name"]
        if missing:
            s.send({"t": "error", "msg": "No such user: " + ", ".join(missing[:5])})
            return None
        return found

    def group_name(self, s, msg):
        name = " ".join(str(msg.get("name", "")).split())[:GROUP_NAME_MAX]
        return name

    async def on_group_create(self, s, msg):
        people = self.users_by_names(s, msg.get("members"))
        if people is None:
            return
        if not people:
            s.send({"t": "error", "msg": "Add at least one other person to the group"})
            return
        if len(people) + 1 > GROUP_MAX:
            s.send({"t": "error", "msg": f"Groups can have at most {GROUP_MAX} people (including you)"})
            return
        name = self.group_name(s, msg) or ", ".join([s.name, *people.values()])[:GROUP_NAME_MAX]
        gid = self.store.create_group(name, s.uid, [s.uid, *people])
        self.push_groups(s.uid, *people)
        s.send({"t": "group_created", "gid": gid})
        self.group_event(gid, f"{s.name} created the group")
        log.info("group %d '%s' created by %s with %d people", gid, name, s.name, len(people) + 1)

    async def on_group_add(self, s, msg):
        gid = self.my_group(s, msg)
        if gid is None:
            return
        people = self.users_by_names(s, msg.get("users"))
        if people is None:
            return
        current = set(self.store.group_member_ids(gid))
        new = {uid: n for uid, n in people.items() if uid not in current}
        if not new:
            s.send({"t": "error", "msg": "They're already in the group"})
            return
        if len(current) + len(new) > GROUP_MAX:
            room = GROUP_MAX - len(current)
            s.send({"t": "error", "msg": f"Groups can have at most {GROUP_MAX} people - "
                                          f"there's room for {room} more"})
            return
        self.store.add_group_members(gid, new)
        self.push_groups(*current, *new)
        self.group_event(gid, f"{s.name} added {', '.join(new.values())}")

    async def on_group_leave(self, s, msg):
        gid = self.my_group(s, msg)
        if gid is None:
            return
        await self.remove_from_group(gid, s.uid, f"{s.name} left the group")

    async def on_group_kick(self, s, msg):
        gid = self.my_group(s, msg)
        if gid is None:
            return
        if self.store.group(gid)["owner"] != s.uid:
            s.send({"t": "error", "msg": "Only the group owner can remove people"})
            return
        row = self.store.user_by_name(str(msg.get("user", "")).strip())
        if row is None or row["id"] not in self.store.group_member_ids(gid) or row["id"] == s.uid:
            s.send({"t": "error", "msg": "They're not in this group"})
            return
        await self.remove_from_group(gid, row["id"], f"{s.name} removed {row['name']}")

    async def remove_from_group(self, gid, uid, text):
        if self.voice_of.get(uid) == f"g:{gid}":
            self.voice_remove(uid)
            self.to_uid(uid, {"t": "voice_left", "reason": "You're no longer in that group"})
        self.store.remove_group_member(gid, uid)
        left = self.store.group_member_ids(gid)
        self.push_groups(uid)
        if not left:
            self.purge_files(f"g:{gid}")
            self.store.delete_group(gid)
            return
        if self.store.group(gid)["owner"] == uid:          # hand ownership to the longest member
            self.store.update_group(gid, owner=left[0])
            text += f" - {self.store.user_by_id(left[0])['name']} now owns it"
        self.push_groups(*left)
        self.group_event(gid, text, extra_uids=[uid])

    async def on_group_rename(self, s, msg):
        gid = self.my_group(s, msg)
        if gid is None:
            return
        name = self.group_name(s, msg)
        if not name:
            s.send({"t": "error", "msg": "Give the group a name"})
            return
        self.store.update_group(gid, name=name)
        self.push_groups(*self.store.group_member_ids(gid))
        self.group_event(gid, f"{s.name} renamed the group to {name}")

    async def on_group_msg(self, s, msg):
        gid = self.my_group(s, msg)
        if gid is None:
            return
        text = self.clean_text(s, msg)
        if text is None:
            return
        self.deliver_group(s.uid, gid, text)

    async def on_group_history(self, s, msg):
        gid = self.my_group(s, msg)
        if gid is None:
            return
        s.send({"t": "group_history", "gid": gid, "messages": self.store.history(f"g:{gid}")})

    # -- voice channels
    def voice_payload(self):
        now = time.time()
        chans = []
        for name in self.store.voice_rooms():
            chans.append({"name": name, "users": self.voice_users(name)})
        return {"t": "voice_state", "channels": chans}

    def voice_users(self, chan):
        now = time.time()
        users = []
        for uid, st in self.voice.get(chan, {}).items():
            row = self.store.user_by_id(uid)
            users.append({"name": row["name"], "muted": st["muted"], "deaf": st["deaf"],
                          "server_muted": row["muted_until"] > now,
                          "camera": (uid, "camera") in self.streams,
                          "screen": (uid, "screen") in self.streams})
        return users

    def call_payload(self, gid):
        return {"t": "call_state", "gid": gid, "users": self.voice_users(f"g:{gid}")}

    def voice_update(self):
        """Tell people about voice changes: public channels to everyone, group calls only to members."""
        pub = self.voice_payload()
        if pub != self.last_voice_payload:
            self.last_voice_payload = pub
            self.broadcast(pub)
        active = {int(c[2:]) for c in self.voice if c.startswith("g:")}
        for gid in active | set(self.last_call_state):
            payload = self.call_payload(gid)
            if payload["users"] == self.last_call_state.get(gid, []):
                continue
            if payload["users"]:
                self.last_call_state[gid] = payload["users"]
            else:
                self.last_call_state.pop(gid, None)
            if self.store.group(gid) is not None:
                for uid in self.store.group_member_ids(gid):
                    self.to_uid(uid, payload)

    def voice_remove(self, uid, broadcast=True):
        chan = self.voice_of.pop(uid, None)
        if chan is None:
            return None
        self.end_streams(uid)
        for viewers in self.streams.values():
            viewers.discard(uid)
        self.waiting_key = {w for w in self.waiting_key if w[0] != uid}
        members = self.voice.get(chan, {})
        members.pop(uid, None)
        if not members:
            self.voice.pop(chan, None)
        if broadcast:
            self.voice_update()
        return chan

    async def on_voice_create(self, s, msg):
        name = " ".join(str(msg.get("name", "")).split())
        if not VOICE_RE.match(name):
            s.send({"t": "error", "msg": "Voice channel names are 1-24 letters, numbers, spaces, _ or -"})
            return
        if self.store.voice_room(name) is None:
            self.store.add_voice_room(name)
            self.voice_update()

    async def on_voice_join(self, s, msg):
        wanted = str(msg.get("channel", "")).strip()
        gid = None
        if wanted.startswith("g:"):                       # a group DM call
            try:
                gid = int(wanted[2:])
            except ValueError:
                gid = -1
            if self.store.group(gid) is None or s.uid not in self.store.group_member_ids(gid):
                s.send({"t": "error", "msg": "You're not in that group"})
                return
            chan, limit = f"g:{gid}", GROUP_MAX
        else:
            chan, limit = self.store.voice_room(wanted), VOICE_MAX
        if chan is None:
            s.send({"t": "error", "msg": "No such voice channel"})
            return
        if self.voice_of.get(s.uid) == chan:
            s.send({"t": "voice_joined", "channel": chan})
            return
        if len(self.voice.get(chan, {})) >= limit:
            s.send({"t": "error", "msg": f"That call is full ({limit} people)"})
            return
        if gid is not None and not self.voice.get(chan):     # starting the call: ring everyone else
            name = self.store.group(gid)["name"]
            for uid in self.store.group_member_ids(gid):
                if uid != s.uid:
                    self.to_uid(uid, {"t": "call_ring", "gid": gid, "from": s.name, "name": name})
        self.voice_remove(s.uid, broadcast=False)
        self.voice.setdefault(chan, {})[s.uid] = {"muted": bool(msg.get("muted")), "deaf": bool(msg.get("deaf"))}
        self.voice_of[s.uid] = chan
        s.send({"t": "voice_joined", "channel": chan})
        self.voice_update()

    async def on_voice_leave(self, s, msg):
        if self.voice_remove(s.uid):
            s.send({"t": "voice_left", "reason": ""})

    async def on_voice_status(self, s, msg):
        chan = self.voice_of.get(s.uid)
        if chan is None:
            return
        self.voice[chan][s.uid] = {"muted": bool(msg.get("muted")), "deaf": bool(msg.get("deaf"))}
        self.voice_update()

    def on_voice_data(self, s, msg):
        chan = self.voice_of.get(s.uid)
        data = msg.get("d")
        if chan is None or not isinstance(data, str) or len(data) > VOICE_PACKET_MAX:
            return
        if not s.allow_voice():
            return
        me = self.voice[chan][s.uid]
        if me["muted"]:
            return
        if self.store.user_by_id(s.uid)["muted_until"] > time.time():
            return                                   # muted by an admin: audio goes nowhere
        out = {"t": "voice_data", "u": s.name, "d": data}
        for uid, st in self.voice[chan].items():
            if uid == s.uid or st["deaf"]:
                continue
            target = self.online.get(uid)
            if target and target.backlog() < VOICE_BACKLOG:
                target.send(out)

    # -- camera and screen sharing (inside voice channels)
    def end_streams(self, uid, kinds=VIDEO_KINDS):
        for kind in kinds:
            self.streams.pop((uid, kind), None)
            self.key_asked.pop((uid, kind), None)
            self.waiting_key = {w for w in self.waiting_key if (w[1], w[2]) != (uid, kind)}

    def ask_keyframe(self, uid, kind, force=False):
        now = time.monotonic()
        if force or now - self.key_asked.get((uid, kind), 0) > 1.0:
            self.key_asked[(uid, kind)] = now
            self.to_uid(uid, {"t": "video_keyframe", "kind": kind})

    async def on_video_start(self, s, msg):
        kind = msg.get("kind")
        if kind not in VIDEO_KINDS or s.uid not in self.voice_of:
            s.send({"t": "error", "msg": "Join a voice channel first"})
            return
        if self.store.user_by_id(s.uid)["muted_until"] > time.time():
            s.send({"t": "error", "msg": "You can't share video while muted by an admin"})
            return
        self.streams.setdefault((s.uid, kind), set())
        self.voice_update()

    async def on_video_stop(self, s, msg):
        kind = msg.get("kind")
        if (s.uid, kind) in self.streams:
            self.end_streams(s.uid, (kind,))
            self.voice_update()

    def stream_owner(self, s, msg):
        row = self.store.user_by_name(str(msg.get("user", "")))
        kind = msg.get("kind")
        if row is None or (row["id"], kind) not in self.streams:
            return None, kind
        if self.voice_of.get(row["id"]) != self.voice_of.get(s.uid):
            return None, kind
        return row["id"], kind

    async def on_video_watch(self, s, msg):
        owner, kind = self.stream_owner(s, msg)
        if owner is None or owner == s.uid:
            return
        self.streams[(owner, kind)].add(s.uid)
        self.waiting_key.add((s.uid, owner, kind))
        self.ask_keyframe(owner, kind, force=True)

    async def on_video_unwatch(self, s, msg):
        owner, kind = self.stream_owner(s, msg)
        if owner is not None:
            self.streams[(owner, kind)].discard(s.uid)
            self.waiting_key.discard((s.uid, owner, kind))

    def on_video_data(self, s, msg):
        kind = msg.get("kind")
        viewers = self.streams.get((s.uid, kind))
        data = msg.get("d")
        if viewers is None or not isinstance(data, str) or len(data) > VIDEO_PACKET_MAX:
            return
        if not s.allow_video(kind):
            return
        key = bool(msg.get("k"))
        out = {"t": "video_data", "u": s.name, "kind": kind, "d": data, "k": key}
        for viewer in list(viewers):
            target = self.online.get(viewer)
            if target is None:
                continue
            waiting = (viewer, s.uid, kind)
            if waiting in self.waiting_key:
                if not key:
                    continue                     # can't decode until the next keyframe
                self.waiting_key.discard(waiting)
            if target.backlog() > VIDEO_BACKLOG:
                self.waiting_key.add(waiting)   # too far behind: skip ahead to a fresh keyframe
                self.ask_keyframe(s.uid, kind)
                continue
            target.send(out)

    # -- file uploads and downloads (on their own connection, see login "transfer")
    def resolve_conv(self, uid, conv):
        """Where an upload goes. Returns (deliver function args, storage conv) or (None, error)."""
        conv = str(conv)
        if conv.startswith("#"):
            if conv not in self.store.rooms():
                return None, f"No room {conv}"
            return ("room", conv), conv
        if conv.startswith("@"):
            target = self.store.user_by_name(conv[1:])
            if target is None:
                return None, "No such user"
            return ("dm", target["id"]), dm_key(uid, target["id"])
        if conv.startswith("g:"):
            try:
                gid = int(conv[2:])
            except ValueError:
                return None, "No such group"
            if self.store.group(gid) is None or uid not in self.store.group_member_ids(gid):
                return None, "You're not in that group"
            return ("group", gid), f"g:{gid}"
        return None, "Unknown conversation"

    def can_access(self, uid, conv):
        if conv.startswith("#"):
            return conv in self.store.rooms()
        if conv.startswith("dm:"):
            return str(uid) in conv.split(":")[1:]
        if conv.startswith("g:"):
            gid = int(conv[2:])
            return self.store.group(gid) is not None and uid in self.store.group_member_ids(gid)
        return False

    @staticmethod
    def safe_name(name):
        name = os.path.basename(str(name).replace("\\", "/")).strip()
        name = "".join(ch for ch in name if ch.isprintable() and ch not in '<>:"/\\|?*')
        return (name or "file")[:200]

    def remove_file(self, row):
        try:
            os.remove(os.path.join(self.files_dir, row["path"]))
        except OSError:
            pass
        self.store.delete_file_row(row["id"])

    def purge_files(self, conv):
        for row in self.store.files_in(conv):
            self.remove_file(row)

    async def transfer_loop(self, s: Session):
        while not s.closed:
            msg = await self.read(s)
            if msg is None:
                continue
            kind = msg.get("t")
            if kind == "upload":
                await self.receive_upload(s, msg)
            elif kind == "download":
                await self.send_download(s, msg)
            elif kind == "ping":
                s.send({"t": "pong"})
            else:
                s.send({"t": "transfer_error", "msg": f"Unknown request {kind!r}"})
            await s.writer.drain()

    async def receive_upload(self, s: Session, msg):
        fail = lambda text: s.send({"t": "transfer_error", "msg": text})
        upload_id = str(msg.get("id", ""))
        try:
            size = int(msg.get("size"))
        except (TypeError, ValueError):
            size = -1
        if not UPLOAD_ID_RE.match(upload_id):
            return fail("Bad upload id")
        if size <= 0:
            return fail("That file is empty")
        if size > MAX_FILE:
            return fail("Files can be at most 5 GB")
        text = str(msg.get("text", "")).replace("\r", "").strip()[:MAX_TEXT]
        target, conv = self.resolve_conv(s.uid, msg.get("conv", ""))
        if target is None:
            return fail(conv)
        if self.store.user_by_id(s.uid)["muted_until"] > time.time():
            return fail("You can't upload files while muted")
        partial = os.path.join(self.partial_dir, f"{s.uid}-{upload_id}")
        offset = os.path.getsize(partial) if os.path.exists(partial) else 0
        if offset > size:
            offset = 0
        if shutil.disk_usage(self.files_dir).free < size - offset + DISK_MARGIN:
            return fail("The server doesn't have enough free disk space for that file")
        s.send({"t": "upload_ready", "offset": offset})
        await s.writer.drain()

        loop = asyncio.get_running_loop()
        remaining = size - offset
        with open(partial, "ab" if offset else "wb") as f:
            while remaining:
                chunk = await s.reader.read(min(FILE_CHUNK, remaining))
                if not chunk:
                    raise ConnectionError("upload interrupted")   # the partial file is kept for resuming
                await loop.run_in_executor(None, f.write, chunk)
                remaining -= len(chunk)

        stored = secrets.token_hex(12)
        os.replace(partial, os.path.join(self.files_dir, stored))
        name = self.safe_name(msg.get("name", "file"))
        fid = self.store.add_file(s.uid, conv, name, size, stored)
        kind, where = target
        if kind == "room":
            self.deliver_room(s.uid, where, text, fid)
        elif kind == "dm":
            self.deliver_dm(s.uid, where, text, fid)
        else:
            self.deliver_group(s.uid, where, text, fid)
        s.send({"t": "upload_done", "fid": fid})
        log.info("upload: %s sent %s (%d bytes) to %s", s.name, name, size, msg.get("conv"))

    async def send_download(self, s: Session, msg):
        try:
            fid = int(msg.get("fid"))
        except (TypeError, ValueError):
            fid = -1
        row = self.store.file(fid)
        if row is None or not self.can_access(s.uid, row["conv"]):
            s.send({"t": "transfer_error", "msg": "That file doesn't exist any more"})
            return
        path = os.path.join(self.files_dir, row["path"])
        if not os.path.exists(path):
            s.send({"t": "transfer_error", "msg": "That file is missing on the server"})
            return
        size = row["size"]
        try:
            offset = max(0, min(int(msg.get("offset", 0)), size))
        except (TypeError, ValueError):
            offset = 0
        s.send({"t": "download_ready", "fid": fid, "name": row["name"], "size": size, "offset": offset})
        loop = asyncio.get_running_loop()
        with open(path, "rb") as f:
            f.seek(offset)
            remaining = size - offset
            while remaining:
                chunk = await loop.run_in_executor(None, f.read, min(FILE_CHUNK, remaining))
                if not chunk:
                    break
                s.writer.write(chunk)
                remaining -= len(chunk)
                await s.writer.drain()

    async def admin_delfile(self, s, msg, reason):
        try:
            row = self.store.file(int(msg.get("fid")))
        except (TypeError, ValueError):
            row = None
        if row is None:
            s.send({"t": "error", "msg": "No such file"})
            return
        self.remove_file(row)
        self.broadcast({"t": "file_deleted", "fid": row["id"]})
        return f"Deleted file {row['name']}"

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
        for t in list(self.transfers.get(uid, ())):
            await self.drop(t, announce=False)

    def target_user(self, s, msg, allow_admin=False):
        """Look up the user an admin command is aimed at, refusing self and other admins."""
        row = self.store.user_by_name(str(msg.get("user", "")).strip())
        if row is None:
            s.send({"t": "error", "msg": "No such user"})
            return None
        if row["id"] == s.uid:
            s.send({"t": "error", "msg": "You can't do that to yourself"})
            return None
        if row["is_admin"] and not allow_admin and not getattr(s, "console", False):
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
        if any((row["id"], k) in self.streams for k in VIDEO_KINDS):
            self.end_streams(row["id"])
            self.to_uid(row["id"], {"t": "video_stopped", "reason": "Muted by an admin"})
        if row["id"] in self.voice_of:
            self.voice_update()
        span = f"for {minutes:g} minute{'s' if minutes != 1 else ''}" if minutes > 0 else "until unmuted"
        return f"Muted {row['name']} {span}"

    async def admin_unmute(self, s, msg, reason):
        row = self.store.user_by_name(str(msg.get("user", "")).strip())
        if row is None or row["muted_until"] <= time.time():
            s.send({"t": "error", "msg": "That user isn't muted"})
            return
        self.store.set_fields(row["id"], muted_until=0, mute_reason="")
        self.to_uid(row["id"], {"t": "notice", "msg": f"{s.name} unmuted you"})
        if row["id"] in self.voice_of:
            self.voice_update()
        return f"Unmuted {row['name']}"

    async def admin_delroom(self, s, msg, reason):
        room = self.room_arg(s, msg)
        if not room:
            return
        if room == DEFAULT_ROOMS[0]:
            s.send({"t": "error", "msg": f"{room} can't be deleted"})
            return
        self.purge_files(room)
        self.store.delete_room(room)
        self.broadcast({"t": "rooms", "rooms": self.store.rooms()})
        return f"Deleted {room}"

    async def admin_clearhistory(self, s, msg, reason):
        room = self.room_arg(s, msg)
        if not room:
            return
        self.purge_files(room)
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

    async def admin_voicekick(self, s, msg, reason):
        row = self.target_user(s, msg)
        if not row:
            return
        if self.voice_remove(row["id"]) is None:
            s.send({"t": "error", "msg": f"{row['name']} isn't in a voice channel"})
            return
        self.to_uid(row["id"], {"t": "voice_left",
                                "reason": f"{s.name} disconnected you from voice" + (f": {reason}" if reason else "")})
        return f"Disconnected {row['name']} from voice"

    async def admin_delvoice(self, s, msg, reason):
        chan = self.store.voice_room(str(msg.get("room", "")).strip())
        if chan is None:
            s.send({"t": "error", "msg": "No such voice channel"})
            return
        for uid in list(self.voice.get(chan, {})):
            self.voice_remove(uid, broadcast=False)
            self.to_uid(uid, {"t": "voice_left", "reason": f"{s.name} deleted {chan}"})
        self.store.delete_voice_room(chan)
        self.voice_update()
        return f"Deleted voice channel {chan}"

    async def admin_bans(self, s, msg, reason):
        users = self.store.banned_users()
        ips = self.store.ip_bans()
        if not users and not ips:
            s.send({"t": "notice", "msg": "Nobody is banned"})
            return
        lines = [f"{r['name']}" + (f" - {r['ban_reason']}" if r["ban_reason"] else "") for r in users]
        lines += [f"IP {r['ip']}" + (f" - {r['reason']}" if r["reason"] else "") for r in ips]
        s.send({"t": "notice", "msg": "Banned: " + "; ".join(lines)})

    # -- used by the server window (server_gui.py); all run on the server's event loop
    async def console(self, cmd, **kw):
        """Run an admin command as the server owner. Returns (ok, message)."""
        handler = getattr(self, f"admin_{cmd}", None)
        if handler is None:
            return False, f"Unknown command {cmd}"
        s = ConsoleSession()
        result = await handler(s, kw, str(kw.get("reason", "")).strip()[:200])
        errors = [m["msg"] for m in s.sent if m.get("t") == "error"]
        notices = [m["msg"] for m in s.sent if m.get("t") == "notice"]
        if result:
            log.info("server window: %s", result)
        return not errors, result or (errors + notices + [""])[0]

    async def set_admin(self, name, on):
        row = self.store.user_by_name(name)
        if row is None:
            return False, "No such user"
        self.store.set_fields(row["id"], is_admin=1 if on else 0)
        if on:
            self.admin_names.add(row["name"].lower())
        else:
            self.admin_names.discard(row["name"].lower())
        self.to_uid(row["id"], {"t": "admin_changed", "admin": bool(on)})
        if row["id"] in self.online:
            self.broadcast({"t": "user_update", "old": row["name"], **self.user_entry(row["id"])})
        text = f"{'Made' if on else 'Removed'} {row['name']} {'an admin' if on else 'as admin'}"
        log.info("server window: %s", text)
        return True, text

    async def add_room(self, name):
        name = name.strip().lower()
        room = name if name.startswith("#") else "#" + name
        if not ROOM_RE.match(room):
            return False, "Channel names are 1-24 lowercase letters, numbers, _ or -"
        self.store.add_room(room)
        self.broadcast({"t": "rooms", "rooms": self.store.rooms()})
        return True, f"Created {room}"

    async def add_voice_room(self, name):
        name = " ".join(name.split())
        if not VOICE_RE.match(name):
            return False, "Voice channel names are 1-24 letters, numbers, spaces, _ or -"
        self.store.add_voice_room(name)
        self.voice_update()
        return True, f"Created voice channel {name}"

    def voice_where(self, uid):
        chan = self.voice_of.get(uid)
        if chan and chan.startswith("g:"):
            g = self.store.group(int(chan[2:]))
            return f"Call: {g['name']}" if g else "Call"
        return chan or ""

    async def snapshot(self):
        now = time.time()
        online = []
        for uid, sess in self.online.items():
            row = self.store.user_by_id(uid)
            online.append({"name": row["name"], "admin": bool(row["is_admin"]), "ip": sess.ip,
                           "voice": self.voice_where(uid),
                           "video": ", ".join(k for k in VIDEO_KINDS if (uid, k) in self.streams),
                           "muted": row["muted_until"] > now})
        online.sort(key=lambda u: u["name"].lower())
        accounts = [{"name": r["name"], "admin": bool(r["is_admin"]), "banned": bool(r["banned"]),
                     "ban_reason": r["ban_reason"], "muted": r["muted_until"] > now, "last_ip": r["last_ip"],
                     "created": r["created"], "online": r["id"] in self.online}
                    for r in self.store.all_users()]
        files = [{"id": r["id"], "name": r["name"], "size": r["size"], "uploader": r["uploader"],
                  "conv": r["conv"], "created": r["created"]} for r in self.store.all_files()]
        return {"online": online, "accounts_list": accounts, "rooms": self.store.rooms(), "files_list": files,
                "files_dir": self.files_dir, "disk_free": shutil.disk_usage(self.files_dir).free,
                "voice_rooms": self.store.voice_rooms(), "ip_bans": [dict(r) for r in self.store.ip_bans()],
                "in_voice": len(self.voice_of), "uptime": now - self.started, **self.store.counts()}

    def config_payload(self):
        """Settings every client gets at login (and again if they change)."""
        return {"t": "config", "giphy_key": self.store.setting("giphy_key")}

    async def set_giphy_key(self, key):
        key = key.strip()
        if key and not re.fullmatch(r"[A-Za-z0-9]{10,64}", key):
            return False, "That doesn't look like a GIPHY API key (letters and numbers only)"
        self.store.set_setting("giphy_key", key)
        self.broadcast(self.config_payload())
        log.info("server window: GIPHY key %s", "saved" if key else "removed")
        return True, "GIPHY key saved - GIF search is on" if key else "GIPHY key removed - GIF search is off"

    async def get_settings(self):
        return {"giphy_key": self.store.setting("giphy_key")}

    async def shutdown(self, restarting=False):
        """Disconnect everyone. When restarting, clients aren't told to stop, so they reconnect."""
        for sess in list(self.online.values()):
            if not restarting:
                sess.send({"t": "kicked", "reason": "The server is shutting down"})
            await self.drop(sess, announce=False)
        for sessions in list(self.transfers.values()):
            for t in list(sessions):
                await self.drop(t, announce=False)


def parse_args(argv=None):
    ap = argparse.ArgumentParser(description="Campfire chat server")
    ap.add_argument("--host", default="0.0.0.0")
    ap.add_argument("--port", type=int, default=55555)
    ap.add_argument("--db", default=DEFAULT_DB, help="database file (default: campfire.db next to server.py)")
    ap.add_argument("--files", default=None,
                    help="folder where uploaded files are kept (default: campfire_files next to server.py, "
                         "or whatever was chosen in the server window)")
    ap.add_argument("--certfile", help="TLS certificate (PEM) - enables TLS")
    ap.add_argument("--keyfile", help="TLS private key (PEM)")
    ap.add_argument("--admin", action="append", default=[], metavar="NAME",
                    help="make this account an admin (repeatable; works before the account exists)")
    ap.add_argument("--remove-admin", action="append", default=[], metavar="NAME",
                    help="take admin away from this account")
    ap.add_argument("--nogui", action="store_true", help="run in the console without the server window")
    ap.add_argument("--giphy-key", default=os.environ.get("GIPHY_API_KEY"),
                    help="GIPHY API key for GIF search (saved; also settable in the server window)")
    return ap.parse_args(argv)


def make_tls(args):
    if not args.certfile:
        return None
    tls = ssl.create_default_context(ssl.Purpose.CLIENT_AUTH)
    tls.load_cert_chain(args.certfile, args.keyfile)
    return tls


async def start(args, port=None):
    """Create the server and start listening. Returns (Server, asyncio.Server)."""
    tls = make_tls(args)
    server = Server(Store(args.db), args.admin, args.remove_admin, args.files or DEFAULT_FILES_DIR)
    if getattr(args, "giphy_key", None):
        server.store.set_setting("giphy_key", args.giphy_key.strip())
    for name in args.admin:
        log.info("admin: %s", name)
    port = args.port if port is None else port
    srv = await asyncio.start_server(server.handle, args.host, port, limit=MAX_LINE, ssl=tls)
    log.info("Campfire server listening on %s:%d%s", args.host, port, " (TLS)" if tls else "")
    return server, srv


def gui_available():
    if sys.platform.startswith("linux") and not (os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY")):
        return False
    try:
        import PyQt6.QtWidgets  # noqa: F401
        return True
    except ImportError:
        return False


async def console_main(args):
    server, srv = await start(args)
    async with srv:
        await srv.serve_forever()


def main():
    args = parse_args()
    if not args.nogui and gui_available():
        import server_gui
        server_gui.run(args)
        return
    fmt = "%(asctime)s %(levelname)s %(message)s"
    if sys.stderr is None:            # packaged without a console: keep the log in a file instead
        logging.basicConfig(level=logging.INFO, format=fmt, filename=os.path.join(DATA_DIR, "campfire_server.log"))
    else:
        logging.basicConfig(level=logging.INFO, format=fmt)
    try:
        asyncio.run(console_main(args))
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
