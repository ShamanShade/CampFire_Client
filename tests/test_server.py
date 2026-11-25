"""End-to-end tests for server.py. Run:  python -m pytest tests  (or python tests/test_server.py)"""

import asyncio
import json
import os
import socket
import subprocess
import sys
import tempfile
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class C:
    """Tiny protocol client for tests."""

    def __init__(self, port):
        self.port = port

    async def open(self):
        self.r, self.w = await asyncio.open_connection("127.0.0.1", self.port, limit=1 << 20)
        assert (await self.recv())["t"] == "hello"
        return self

    def send(self, **obj):
        self.w.write((json.dumps(obj) + "\n").encode())

    async def recv(self, timeout=5):
        return json.loads(await asyncio.wait_for(self.r.readline(), timeout))

    async def wait_for(self, kind, timeout=5, contains=None, **match):
        end = time.monotonic() + timeout
        while True:
            m = await self.recv(end - time.monotonic())
            if m["t"] == kind and all(m.get(k) == v for k, v in match.items()):
                if contains is None or contains in m.get("msg", ""):
                    return m

    async def login(self, user, pw="secret"):
        self.send(t="login", user=user, **{"pass": pw})
        m = await self.wait_for("login_ok")
        await self.wait_for("friends")
        return m


async def scenario(port):
    a = await C(port).open()
    ok = await a.login("alice")
    assert ok["registered"] and "#general" in ok["rooms"]

    # wrong password, then bad name
    x = await C(port).open()
    x.send(t="login", user="alice", **{"pass": "nope"})
    assert (await x.recv())["t"] == "login_fail"
    x.send(t="login", user="a!", **{"pass": "secret"})
    assert (await x.recv())["t"] == "login_fail"
    x.w.close()

    b = await C(port).open()
    await b.login("bob")
    j = await a.wait_for("user_join")
    assert j["name"] == "bob"

    # room message goes to everyone, and is kept in history
    b.send(t="msg", room="#general", text="hi | there <b>")
    m = await a.wait_for("msg")
    assert m["user"] == "bob" and m["text"] == "hi | there <b>"
    a.send(t="history", room="#general")
    h = await a.wait_for("history")
    assert h["messages"][-1]["text"] == "hi | there <b>"

    # new room
    a.send(t="create_room", room="Music")
    r = await b.wait_for("rooms")
    assert "#music" in r["rooms"]

    # DMs
    a.send(t="dm", to="bob", text="psst")
    d = await b.wait_for("dm")
    assert d["from"] == "alice" and d["to"] == "bob"
    b.send(t="dm_history", **{"with": "alice"})
    dh = await b.wait_for("dm_history")
    assert dh["messages"][0]["text"] == "psst"

    # friends
    a.send(t="friend_request", to="bob")
    f = await b.wait_for("friends")
    assert f["requests"] == ["alice"]
    b.send(t="friend_respond", user="alice", accept=True)
    fa = await a.wait_for("friends")
    assert fa["friends"] == [{"name": "bob", "online": True}]

    # rename propagates
    b.send(t="rename", name="bobby")
    u = await a.wait_for("user_update")
    assert u["old"] == "bob" and u["name"] == "bobby"

    # rate limiting
    for i in range(30):
        a.send(t="msg", room="#general", text=f"spam {i}")
    await a.wait_for("error")

    # friend sees offline status after disconnect
    b.w.close()
    await a.wait_for("user_leave", name="bobby")
    fa = await a.wait_for("friends")
    assert fa["friends"] == [{"name": "bobby", "online": False}]

    # log in again under new name with same password; old name is free
    b2 = await C(port).open()
    ok = await b2.login("bobby")
    assert not ok["registered"]

    # second login kicks the first session
    a2 = await C(port).open()
    a2.send(t="login", user="alice", **{"pass": "secret"})
    await a.wait_for("kicked")
    await a2.wait_for("login_ok")
    return True


async def admin_scenario(port):
    boss = await C(port).open()
    ok = await boss.login("boss")
    assert ok["admin"] is True
    eve = await C(port).open()
    ok = await eve.login("eve")
    assert ok["admin"] is False
    j = await boss.wait_for("user_join", name="eve")
    assert j["admin"] is False

    # non-admins can't use admin commands
    eve.send(t="admin", cmd="kick", user="boss")
    e = await eve.wait_for("error")
    assert "admins only" in e["msg"]

    # admin messages carry the admin flag
    boss.send(t="msg", room="#general", text="rules: be nice")
    m = await eve.wait_for("msg")
    assert m["admin"] is True

    # announce
    boss.send(t="admin", cmd="announce", text="server restart at 5")
    a = await eve.wait_for("announce")
    assert a["from"] == "boss" and a["text"] == "server restart at 5"

    # mute: can't talk, then unmute
    boss.send(t="admin", cmd="mute", user="eve", minutes=10, reason="spam")
    mu = await eve.wait_for("muted")
    assert mu["reason"] == "spam"
    eve.send(t="msg", room="#general", text="hello?")
    e = await eve.wait_for("error")
    assert "muted for 10 more minutes (spam)" in e["msg"], e
    boss.send(t="admin", cmd="unmute", user="eve")
    await eve.wait_for("notice")
    eve.send(t="msg", room="#general", text="thanks")
    await boss.wait_for("msg", text="thanks")

    # clear history / delete room
    boss.send(t="admin", cmd="clearhistory", room="#general")
    c = await eve.wait_for("cleared")
    assert c["room"] == "#general"
    eve.send(t="history", room="#general")
    h = await eve.wait_for("history")
    assert h["messages"] == []
    eve.send(t="create_room", room="junk")
    await boss.wait_for("rooms")
    await eve.wait_for("rooms")
    boss.send(t="admin", cmd="delroom", room="#junk")
    r = await eve.wait_for("rooms")
    assert "#junk" not in r["rooms"]
    boss.send(t="admin", cmd="delroom", room="#general")
    await boss.wait_for("error")

    # can't target yourself
    boss.send(t="admin", cmd="ban", user="boss")
    e = await boss.wait_for("error")
    assert "yourself" in e["msg"]

    # kick
    boss.send(t="admin", cmd="kick", user="eve", reason="cool off")
    k = await eve.wait_for("kicked")
    assert "cool off" in k["reason"]
    await boss.wait_for("user_leave", name="eve")

    # ban: can't log in; unban: can
    boss.send(t="admin", cmd="ban", user="eve", reason="trolling")
    await boss.wait_for("notice")
    x = await C(port).open()
    x.send(t="login", user="eve", **{"pass": "secret"})
    f = await x.wait_for("login_fail")
    assert "banned: trolling" in f["reason"]
    boss.send(t="admin", cmd="bans")
    await boss.wait_for("notice", contains="eve - trolling")
    boss.send(t="admin", cmd="unban", user="eve")
    await boss.wait_for("notice")
    x.send(t="login", user="eve", **{"pass": "secret"})
    await x.wait_for("login_ok")

    # reset password kicks and the old password stops working
    boss.send(t="admin", cmd="resetpass", user="eve", password="newpass")
    await x.wait_for("kicked")
    y = await C(port).open()
    y.send(t="login", user="eve", **{"pass": "secret"})
    await y.wait_for("login_fail")
    y.send(t="login", user="eve", **{"pass": "newpass"})
    await y.wait_for("login_ok")

    # IP ban by raw address (not our own) then unban
    boss.send(t="admin", cmd="ipban", user="10.1.2.3", reason="bot")
    await boss.wait_for("notice", contains="Banned address 10.1.2.3")
    boss.send(t="admin", cmd="ipban", user="eve")    # eve is on 127.0.0.1, same as boss
    e = await boss.wait_for("error")
    assert "own address" in e["msg"]
    boss.send(t="admin", cmd="unipban", user="10.1.2.3")
    await boss.wait_for("notice", contains="Unbanned address 10.1.2.3")
    return True


def run_server(port, db, *extra):
    proc = subprocess.Popen(
        [sys.executable, os.path.join(ROOT, "server.py"), "--host", "127.0.0.1",
         "--port", str(port), "--db", db, *extra],
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
    )
    for _ in range(50):
        try:
            socket.create_connection(("127.0.0.1", port), 0.2).close()
            break
        except OSError:
            time.sleep(0.1)
    return proc


def test_admin():
    port = free_port()
    with tempfile.TemporaryDirectory() as tmp:
        proc = run_server(port, os.path.join(tmp, "a.db"), "--admin", "boss")
        try:
            assert asyncio.run(admin_scenario(port))
            # a banned address is turned away before it can even log in
            import sqlite3
            db = sqlite3.connect(os.path.join(tmp, "a.db"))
            db.execute("INSERT INTO ip_bans VALUES ('127.0.0.1', 'test', 'boss', 0)")
            db.commit()
            db.close()
            with socket.create_connection(("127.0.0.1", port), 2) as sock:
                first = json.loads(sock.makefile().readline())
            assert first == {"t": "kicked", "reason": "This address is banned: test"}, first
        finally:
            proc.terminate()
            out = proc.communicate(timeout=5)[0]
        assert "Traceback" not in out, out


def test_server():
    port = free_port()
    with tempfile.TemporaryDirectory() as tmp:
        proc = subprocess.Popen(
            [sys.executable, os.path.join(ROOT, "server.py"), "--host", "127.0.0.1",
             "--port", str(port), "--db", os.path.join(tmp, "t.db")],
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
        )
        try:
            for _ in range(50):
                try:
                    socket.create_connection(("127.0.0.1", port), 0.2).close()
                    break
                except OSError:
                    time.sleep(0.1)
            assert asyncio.run(scenario(port))
        finally:
            proc.terminate()
            out = proc.communicate(timeout=5)[0]
        assert "Traceback" not in out, out


if __name__ == "__main__":
    test_server()
    test_admin()
    print("server tests passed")
