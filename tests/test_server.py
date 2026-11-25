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

    async def wait_for(self, kind, timeout=5, **match):
        end = time.monotonic() + timeout
        while True:
            m = await self.recv(end - time.monotonic())
            if m["t"] == kind and all(m.get(k) == v for k, v in match.items()):
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
    print("server tests passed")
