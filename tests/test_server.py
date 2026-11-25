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
        self.r, self.w = await asyncio.open_connection("127.0.0.1", self.port, limit=4 << 20)
        assert (await self.recv())["t"] == "hello"
        return self

    def send(self, **obj):
        self.w.write((json.dumps(obj) + "\n").encode())

    async def recv(self, timeout=5):
        return json.loads(await asyncio.wait_for(self.r.readline(), timeout))

    async def wait_for(self, msg_type, timeout=5, contains=None, **match):
        end = time.monotonic() + timeout
        while True:
            m = await self.recv(end - time.monotonic())
            if m["t"] == msg_type and all(m.get(k) == v for k, v in match.items()):
                if contains is None or contains in m.get("msg", ""):
                    return m

    async def login(self, user, pw="secret"):
        self.send(t="login", user=user, **{"pass": pw})
        m = await self.wait_for("login_ok")
        await self.wait_for("friends")
        await self.wait_for("groups")
        await self.wait_for("voice_state")
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


async def group_scenario(port):
    a = await C(port).open(); await a.login("ann")
    b = await C(port).open(); await b.login("ben")
    c = await C(port).open(); await c.login("cat")
    d = await C(port).open(); await d.login("dan")

    a.send(t="group_create", name="Squad", members=["ben", "cat"])
    g = await b.wait_for("groups")
    assert g["groups"][0]["name"] == "Squad"
    assert g["groups"][0]["members"] == ["ann", "ben", "cat"] and g["groups"][0]["owner"] == "ann"
    gid = (await a.wait_for("group_created"))["gid"]

    # messages reach members only
    b.send(t="group_msg", gid=gid, text="yo squad")
    m = await c.wait_for("group_msg")
    assert m["user"] == "ben" and m["gid"] == gid
    d.send(t="group_msg", gid=gid, text="let me in")
    assert "not in that group" in (await d.wait_for("error"))["msg"]
    d.send(t="group_history", gid=gid)
    await d.wait_for("error")

    # anyone in the group can add people; history is there for them
    c.send(t="group_add", gid=gid, users=["dan"])
    await d.wait_for("groups")
    n = await d.wait_for("group_notice")
    assert n["text"] == "cat added dan"
    d.send(t="group_history", gid=gid)
    h = await d.wait_for("group_history")
    assert [x["text"] for x in h["messages"]] == ["yo squad"]

    # only the owner can remove people
    b.send(t="group_kick", gid=gid, user="dan")
    assert "Only the group owner" in (await b.wait_for("error"))["msg"]
    a.send(t="group_kick", gid=gid, user="dan")
    g = await d.wait_for("groups")
    assert g["groups"] == []

    # rename
    b.send(t="group_rename", gid=gid, name="The Crew")
    while (await a.wait_for("groups"))["groups"][0]["name"] != "The Crew":
        pass

    # owner leaves: ownership passes on
    a.send(t="group_leave", gid=gid)
    while (g := await b.wait_for("groups"))["groups"][0]["owner"] != "ben":
        pass
    assert "ann" not in g["groups"][0]["members"]

    # unknown users are rejected, and a group needs someone else in it
    a.send(t="group_create", members=["ghost"])
    assert "No such user: ghost" in (await a.wait_for("error"))["msg"]
    a.send(t="group_create", members=[])
    await a.wait_for("error")

    # 50-person limit: make 50 more accounts
    names = [f"user{i:02d}" for i in range(50)]
    for n in names:
        x = await C(port).open()
        await x.login(n)
        x.w.close()
    a.send(t="group_create", name="Too big", members=names)          # 51 with ann
    assert "at most 50" in (await a.wait_for("error"))["msg"]
    a.send(t="group_create", name="Full", members=names[:49])        # exactly 50
    g = await a.wait_for("group_created")
    a.send(t="group_add", gid=g["gid"], users=["ben"])
    assert "room for 0 more" in (await a.wait_for("error", contains="at most"))["msg"]
    return True


async def voice_scenario(port):
    a = await C(port).open(); ok = await a.login("vic")
    b = await C(port).open(); await b.login("val")
    c = await C(port).open(); await c.login("vin")
    boss = await C(port).open(); await boss.login("boss")

    # default channels exist and everyone sees who's in them
    a.send(t="voice_join", channel="lounge")                 # case-insensitive
    assert (await a.wait_for("voice_joined"))["channel"] == "Lounge"
    st = await b.wait_for("voice_state")
    lounge = next(ch for ch in st["channels"] if ch["name"] == "Lounge")
    assert [u["name"] for u in lounge["users"]] == ["vic"]
    b.send(t="voice_join", channel="Lounge"); await b.wait_for("voice_joined")
    c.send(t="voice_join", channel="Gaming"); await c.wait_for("voice_joined")

    # audio goes to others in the same channel only, never back to the speaker
    a.send(t="voice_data", d="AAAA")
    v = await b.wait_for("voice_data")
    assert v == {"t": "voice_data", "u": "vic", "d": "AAAA"}
    c.send(t="msg", room="#general", text="marker")
    m = await c.wait_for("msg")                    # c got nothing voice-related before its own marker
    a.send(t="msg", room="#general", text="marker2")
    while (x := await a.recv())["t"] != "msg" or x["text"] != "marker2":
        assert x["t"] != "voice_data", "speaker got own audio"

    # deafened listeners get no audio; self-muted speakers send none
    b.send(t="voice_status", muted=False, deaf=True)
    await a.wait_for("voice_state")
    a.send(t="voice_data", d="BBBB")
    b.send(t="voice_status", muted=True, deaf=False)
    await a.wait_for("voice_state")
    b.send(t="voice_data", d="CCCC")              # muted: dropped
    a.send(t="voice_data", d="DDDD")
    v = await b.wait_for("voice_data")
    assert v["d"] == "DDDD", v                     # BBBB skipped while deaf
    a.send(t="msg", room="#general", text="m3")
    while (x := await a.recv())["t"] != "msg" or x["text"] != "m3":
        assert x["t"] != "voice_data", x

    # create a channel, move into it
    c.send(t="voice_create", name="Late Night")
    st = await a.wait_for("voice_state")
    while "Late Night" not in [ch["name"] for ch in st["channels"]]:
        st = await a.wait_for("voice_state")
    c.send(t="voice_join", channel="Late Night"); await c.wait_for("voice_joined")
    st = await a.wait_for("voice_state")
    gaming = next(ch for ch in st["channels"] if ch["name"] == "Gaming")
    assert gaming["users"] == []

    # oversized packets are ignored
    a.send(t="voice_data", d="x" * 5000)

    # admin mute silences voice too, and the state shows it
    boss.send(t="admin", cmd="mute", user="vic", minutes=5)
    await a.wait_for("muted")
    st = await b.wait_for("voice_state")
    while not next(ch for ch in st["channels"] if ch["name"] == "Lounge")["users"][0]["server_muted"]:
        st = await b.wait_for("voice_state")
    b.send(t="voice_status", muted=False, deaf=False)
    a.send(t="voice_data", d="EEEE")
    b.send(t="voice_data", d="FFFF")
    v = await a.wait_for("voice_data")
    assert v["d"] == "FFFF"
    boss.send(t="admin", cmd="unmute", user="vic")

    # admin can disconnect someone from voice, and delete a channel
    boss.send(t="admin", cmd="voicekick", user="vin")
    left = await c.wait_for("voice_left")
    assert "boss disconnected you" in left["reason"]
    c.send(t="voice_join", channel="Late Night"); await c.wait_for("voice_joined")
    boss.send(t="admin", cmd="delvoice", room="Late Night")
    left = await c.wait_for("voice_left")
    assert "deleted Late Night" in left["reason"]
    st = await a.wait_for("voice_state")
    while "Late Night" in [ch["name"] for ch in st["channels"]]:
        st = await a.wait_for("voice_state")

    # disconnecting removes you from voice
    b.w.close()
    st = await a.wait_for("voice_state")
    while [u["name"] for u in next(ch for ch in st["channels"] if ch["name"] == "Lounge")["users"]] != ["vic"]:
        st = await a.wait_for("voice_state")

    # leave
    a.send(t="voice_leave")
    await a.wait_for("voice_left")
    return True


async def video_scenario(port):
    a = await C(port).open(); await a.login("ava")
    b = await C(port).open(); await b.login("bea")
    c = await C(port).open(); await c.login("cal")
    boss = await C(port).open(); await boss.login("boss")

    a.send(t="video_start", kind="camera")
    assert "Join a voice channel" in (await a.wait_for("error"))["msg"]
    for x in (a, b, c):
        x.send(t="voice_join", channel="Lounge")
        await x.wait_for("voice_joined")

    a.send(t="video_start", kind="camera")
    st = await b.wait_for("voice_state")
    while not next(u for ch in st["channels"] for u in ch["users"] if u["name"] == "ava")["camera"]:
        st = await b.wait_for("voice_state")

    # nobody watching: frames go nowhere
    a.send(t="video_data", kind="camera", d="KEY0", k=True)

    # bob watches: ava is asked for a keyframe, bob skips delta frames until it arrives
    b.send(t="video_watch", user="ava", kind="camera")
    await a.wait_for("video_keyframe", kind="camera")
    a.send(t="video_data", kind="camera", d="DELTA1", k=False)
    a.send(t="video_data", kind="camera", d="KEY2", k=True)
    a.send(t="video_data", kind="camera", d="DELTA3", k=False)
    v = await b.wait_for("video_data")
    assert v == {"t": "video_data", "u": "ava", "kind": "camera", "d": "KEY2", "k": True}, v
    v = await b.wait_for("video_data")
    assert v["d"] == "DELTA3"

    # cal didn't ask to watch: gets nothing. ava never gets her own video back.
    c.send(t="msg", room="#general", text="c-marker")
    while (x := await c.recv())["t"] != "msg" or x["text"] != "c-marker":
        assert x["t"] != "video_data", x
    a.send(t="msg", room="#general", text="a-marker")
    while (x := await a.recv())["t"] != "msg" or x["text"] != "a-marker":
        assert x["t"] != "video_data", x

    # screen share is a separate stream
    a.send(t="video_start", kind="screen")
    c.send(t="video_watch", user="ava", kind="screen")
    await a.wait_for("video_keyframe", kind="screen")
    a.send(t="video_data", kind="screen", d="SKEY", k=True)
    v = await c.wait_for("video_data")
    assert v["kind"] == "screen" and v["d"] == "SKEY"

    # unwatch stops delivery
    b.send(t="video_unwatch", user="ava", kind="camera")
    b.send(t="msg", room="#general", text="sync")
    await b.wait_for("msg", text="sync")
    a.send(t="video_data", kind="camera", d="AFTER", k=True)
    b.send(t="msg", room="#general", text="b-marker")
    while (x := await b.recv())["t"] != "msg" or x["text"] != "b-marker":
        assert x["t"] != "video_data", x

    # stopping a stream updates the state
    a.send(t="video_stop", kind="screen")
    st = await c.wait_for("voice_state")
    while next(u for ch in st["channels"] for u in ch["users"] if u["name"] == "ava")["screen"]:
        st = await c.wait_for("voice_state")

    # admin mute ends video
    boss.send(t="admin", cmd="mute", user="ava", minutes=1)
    stopped = await a.wait_for("video_stopped")
    assert "Muted" in stopped["reason"]
    a.send(t="video_start", kind="camera")
    assert "muted by an admin" in (await a.wait_for("error"))["msg"]
    boss.send(t="admin", cmd="unmute", user="ava")
    await a.wait_for("notice", contains="unmuted")

    # leaving voice ends your streams and your watching
    a.send(t="video_start", kind="camera")
    b.send(t="video_watch", user="ava", kind="camera")
    await a.wait_for("video_keyframe")
    a.send(t="voice_leave")
    st = await b.wait_for("voice_state")
    while any(u["name"] == "ava" for ch in st["channels"] for u in ch["users"]):
        st = await b.wait_for("voice_state")
    a.send(t="video_data", kind="camera", d="GONE", k=True)       # not streaming any more: ignored
    b.send(t="msg", room="#general", text="end")
    while (x := await b.recv())["t"] != "msg" or x["text"] != "end":
        assert x["t"] != "video_data", x

    # a big keyframe (~1 MB) gets through intact
    a.send(t="voice_join", channel="Lounge"); await a.wait_for("voice_joined")
    a.send(t="video_start", kind="screen")
    b.send(t="video_watch", user="ava", kind="screen")
    await a.wait_for("video_keyframe")
    big = "Q" * (1024 * 1024)
    a.send(t="video_data", kind="screen", d=big, k=True)
    v = await b.wait_for("video_data")
    assert len(v["d"]) == len(big)
    return True


async def call_scenario(port):
    a = await C(port).open(); await a.login("amy")
    b = await C(port).open(); await b.login("bob")
    c = await C(port).open(); await c.login("cyd")
    out = await C(port).open(); await out.login("otto")       # not in the group

    a.send(t="group_create", name="Crew", members=["bob", "cyd"])
    gid = (await a.wait_for("group_created"))["gid"]

    # outsiders can't join
    out.send(t="voice_join", channel=f"g:{gid}")
    assert "not in that group" in (await out.wait_for("error"))["msg"]

    # amy starts the call: bo and cy are rung, and see who's in it
    a.send(t="voice_join", channel=f"g:{gid}")
    assert (await a.wait_for("voice_joined"))["channel"] == f"g:{gid}"
    ring = await b.wait_for("call_ring")
    assert ring == {"t": "call_ring", "gid": gid, "from": "amy", "name": "Crew"}
    await c.wait_for("call_ring")
    cs = await b.wait_for("call_state")
    assert [u["name"] for u in cs["users"]] == ["amy"]

    # bo joins: no second ring, audio flows between them
    b.send(t="voice_join", channel=f"g:{gid}")
    await b.wait_for("voice_joined")
    while [u["name"] for u in (await a.wait_for("call_state"))["users"]] != ["amy", "bob"]:
        pass
    a.send(t="voice_data", d="HELLO")
    assert (await b.wait_for("voice_data"))["d"] == "HELLO"

    # video works in calls too
    a.send(t="video_start", kind="camera")
    b.send(t="video_watch", user="amy", kind="camera")
    await a.wait_for("video_keyframe")
    a.send(t="video_data", kind="camera", d="KF", k=True)
    assert (await b.wait_for("video_data"))["d"] == "KF"

    # the outsider never hears about the call, and public voice lists don't show it
    out.send(t="msg", room="#general", text="o-marker")
    while (x := await out.recv())["t"] != "msg" or x["text"] != "o-marker":
        assert x["t"] not in ("call_state", "call_ring", "voice_data", "video_data"), x
        if x["t"] == "voice_state":
            assert all(not ch["users"] for ch in x["channels"]), x

    # a late joiner of the group gets the call state at login
    c.w.close()
    c2 = await C(port).open()
    c2.send(t="login", user="cyd", **{"pass": "secret"})
    await c2.wait_for("login_ok")
    cs = await c2.wait_for("call_state")
    assert cs["gid"] == gid and len(cs["users"]) == 2

    # kicked from the group: dropped from the call
    a.send(t="group_kick", gid=gid, user="bob")
    left = await b.wait_for("voice_left")
    assert "no longer in that group" in left["reason"]

    # last one out ends the call; members see it empty
    a.send(t="voice_leave")
    cs = await c2.wait_for("call_state")
    while cs["users"]:
        cs = await c2.wait_for("call_state")
    return True


class T:
    """A file-transfer connection (logs in with transfer=True)."""

    def __init__(self, port):
        self.port = port

    async def open(self, user, pw="secret"):
        self.r, self.w = await asyncio.open_connection("127.0.0.1", self.port, limit=4 << 20)
        assert json.loads(await self.r.readline())["t"] == "hello"
        self.send(t="login", user=user, transfer=True, **{"pass": pw})
        m = json.loads(await asyncio.wait_for(self.r.readline(), 5))
        assert m.get("transfer") is True, m
        return self

    def send(self, **obj):
        self.w.write((json.dumps(obj) + "\n").encode())

    async def line(self):
        return json.loads(await asyncio.wait_for(self.r.readline(), 10))

    async def upload(self, conv, name, data, text="", uid=None, stop_after=None):
        uid = uid or os.urandom(8).hex()
        self.send(t="upload", id=uid, conv=conv, name=name, size=len(data), text=text)
        ready = await self.line()
        if ready["t"] != "upload_ready":
            return ready
        off = ready["offset"]
        end = len(data) if stop_after is None else stop_after
        for i in range(off, end, 256 * 1024):
            self.w.write(data[i:min(i + 256 * 1024, end)])
            await self.w.drain()
        if stop_after is not None:
            self.w.close()
            return {"t": "cut", "offset": off}
        return await self.line()

    async def download(self, fid, offset=0):
        self.send(t="download", fid=fid, offset=offset)
        ready = await self.line()
        if ready["t"] != "download_ready":
            return ready, b""
        return ready, await asyncio.wait_for(self.r.readexactly(ready["size"] - ready["offset"]), 30)


async def file_scenario(port, files_dir):
    import hashlib
    a = await C(port).open(); await a.login("fay")
    b = await C(port).open(); await b.login("gus")
    c = await C(port).open(); await c.login("hal")
    boss = await C(port).open(); await boss.login("boss")
    ta = await T(port).open("fay")

    # the chat connection is untouched by a transfer login (no kick, no join/leave)
    a.send(t="ping"); await a.wait_for("pong")

    # upload to a room: everyone sees the message with the file
    data = os.urandom(300_000)
    done = await ta.upload("#general", "../../evil<name>.bin", data, text="here you go")
    assert done["t"] == "upload_done", done
    m = await b.wait_for("msg")
    assert m["text"] == "here you go" and m["file"]["size"] == len(data)
    assert m["file"]["name"] == "evilname.bin", m["file"]          # path tricks removed
    fid = m["file"]["id"]
    b.send(t="history", room="#general")
    h = await b.wait_for("history")
    assert h["messages"][-1]["file"]["id"] == fid

    # download it back, also resuming from an offset
    tb = await T(port).open("gus")
    ready, got = await tb.download(fid)
    assert got == data and ready["name"] == "evilname.bin"
    ready, tail = await tb.download(fid, offset=100_000)
    assert tail == data[100_000:]

    # DM files are private to the two people
    secret = b"top secret" * 1000
    done = await ta.upload("@gus", "note.txt", secret)
    dm = await b.wait_for("dm")
    assert dm["file"]["name"] == "note.txt"
    tc = await T(port).open("hal")
    err, _ = await tc.download(dm["file"]["id"])
    assert err["t"] == "transfer_error", err
    _, got = await tb.download(dm["file"]["id"])
    assert got == secret

    # group files only for members
    a.send(t="group_create", name="Files", members=["gus"])
    gid = (await a.wait_for("group_created"))["gid"]
    await ta.upload(f"g:{gid}", "plan.pdf", b"%PDF" + os.urandom(5000))
    gm = await b.wait_for("group_msg")
    err, _ = await tc.download(gm["file"]["id"])
    assert err["t"] == "transfer_error"
    err = await tc.upload(f"g:{gid}", "x.bin", b"x")
    assert "not in that group" in err["msg"]

    # limits
    big = await ta.upload("#general", "huge.iso", b"")            # size 0
    assert big["t"] == "transfer_error"
    ta.send(t="upload", id=os.urandom(8).hex(), conv="#general", name="huge.iso", size=5 * 1024 ** 3 + 1)
    assert "at most 5 GB" in (await ta.line())["msg"]

    # resume: the connection drops half way, the next try continues from where it stopped
    big = os.urandom(3 * 1024 * 1024)
    upload_id = os.urandom(8).hex()
    tr = await T(port).open("fay")
    await tr.upload("#general", "resume.bin", big, uid=upload_id, stop_after=1_500_000)
    await asyncio.sleep(0.3)
    tr2 = await T(port).open("fay")
    tr2.send(t="upload", id=upload_id, conv="#general", name="resume.bin", size=len(big))
    ready = await tr2.line()
    assert ready["offset"] >= 1_000_000, ready                      # not starting over
    for i in range(ready["offset"], len(big), 256 * 1024):
        tr2.w.write(big[i:i + 256 * 1024])
    done = await tr2.line()
    assert done["t"] == "upload_done"
    _, got = await tb.download(done["fid"])
    assert hashlib.sha256(got).digest() == hashlib.sha256(big).digest()

    # a bigger file streams through without trouble
    huge = os.urandom(64 * 1024 * 1024)
    t0 = time.time()
    done = await ta.upload("#general", "video.mp4", huge)
    _, got = await tb.download(done["fid"])
    assert got == huge
    print(f"    64 MB up+down in {time.time() - t0:.1f}s")

    # muted people can't upload
    boss.send(t="admin", cmd="mute", user="hal", minutes=5)
    await c.wait_for("muted")
    err = await tc.upload("#general", "x.bin", b"x")
    assert "muted" in err["msg"]

    # admin deletes a file: it's gone from disk and people are told
    n_before = len([f for f in os.listdir(files_dir) if f != "partial"])
    boss.send(t="admin", cmd="delfile", fid=fid)
    d = await b.wait_for("file_deleted")
    assert d["fid"] == fid
    assert len([f for f in os.listdir(files_dir) if f != "partial"]) == n_before - 1
    err, _ = await tb.download(fid)
    assert err["t"] == "transfer_error"
    b.send(t="history", room="#general")
    h = await b.wait_for("history")
    assert any(m.get("file", {}).get("deleted") for m in h["messages"])

    # clearing a channel removes its files from disk
    boss.send(t="admin", cmd="clearhistory", room="#general")
    await b.wait_for("cleared")
    left = [f for f in os.listdir(files_dir) if f != "partial"]
    assert len(left) == 2, left                     # only the DM file and the group file remain

    # banning someone closes their file connections too
    boss.send(t="admin", cmd="ban", user="hal")
    await c.wait_for("kicked")
    assert await asyncio.wait_for(tc.r.read(), 5) == b""
    return True


async def delete_scenario(port, files_dir):
    a = await C(port).open(); await a.login("ann")
    b = await C(port).open(); await b.login("ben")
    c = await C(port).open(); await c.login("cal")
    boss = await C(port).open(); await boss.login("boss")

    # messages now carry ids, live and in history
    a.send(t="msg", room="#general", text="oops typo")
    m = await b.wait_for("msg")
    mid = m["id"]
    assert isinstance(mid, int)
    b.send(t="history", room="#general")
    assert (await b.wait_for("history"))["messages"][-1]["id"] == mid

    # someone else can't delete it; the author can, and everyone is told
    b.send(t="delete_msg", id=mid)
    assert "only delete your own" in (await b.wait_for("error"))["msg"]
    a.send(t="delete_msg", id=mid)
    for x in (a, b, c, boss):
        assert (await x.wait_for("msg_deleted"))["id"] == mid
    b.send(t="history", room="#general")
    assert all(m["id"] != mid for m in (await b.wait_for("history"))["messages"])
    a.send(t="delete_msg", id=mid)
    assert "doesn't exist" in (await a.wait_for("error"))["msg"]

    # admins can delete anyone's message in a channel
    b.send(t="msg", room="#general", text="rude thing")
    mid = (await a.wait_for("msg"))["id"]
    boss.send(t="delete_msg", id=mid)
    await a.wait_for("msg_deleted", id=mid)

    # DMs: only the sender; admins can't; outsiders aren't told
    a.send(t="dm", to="ben", text="secret")
    dm_id = (await b.wait_for("dm"))["id"]
    for x in (b, boss):
        x.send(t="delete_msg", id=dm_id)
        await x.wait_for("error")
    a.send(t="delete_msg", id=dm_id)
    await b.wait_for("msg_deleted", id=dm_id)
    c.send(t="ping")
    while (x := await c.recv())["t"] != "pong":
        assert x.get("id") != dm_id, x               # cal never hears about ann and ben's DM

    # groups: the owner can delete others' messages, members can't
    a.send(t="group_create", name="Club", members=["ben", "cal"])
    gid = (await a.wait_for("group_created"))["gid"]
    b.send(t="group_msg", gid=gid, text="from ben")
    gm = (await c.wait_for("group_msg"))["id"]
    c.send(t="delete_msg", id=gm)
    await c.wait_for("error")
    a.send(t="delete_msg", id=gm)                   # ann owns the group
    await c.wait_for("msg_deleted", id=gm)

    # deleting a message with a file removes the file too
    ta = await T(port).open("ann")
    done = await ta.upload("#general", "pic.png", os.urandom(50_000))
    fm = await b.wait_for("msg")
    while not fm.get("file"):
        fm = await b.wait_for("msg")
    before = len([f for f in os.listdir(files_dir) if f != "partial"])
    a.send(t="delete_msg", id=fm["id"])
    await b.wait_for("msg_deleted", id=fm["id"])
    assert len([f for f in os.listdir(files_dir) if f != "partial"]) == before - 1
    tb = await T(port).open("ben")
    err, _ = await tb.download(done["fid"])
    assert err["t"] == "transfer_error"
    return True


def test_delete():
    port = free_port()
    with tempfile.TemporaryDirectory() as tmp:
        db = os.path.join(tmp, "d.db")
        proc = run_server(port, db, "--admin", "boss")
        try:
            assert asyncio.run(delete_scenario(port, db + "_files"))
        finally:
            proc.terminate()
            out = proc.communicate(timeout=5)[0]
        assert "Traceback" not in out, out


def test_files():
    port = free_port()
    with tempfile.TemporaryDirectory() as tmp:
        db = os.path.join(tmp, "f.db")
        proc = run_server(port, db, "--admin", "boss")
        try:
            assert asyncio.run(file_scenario(port, db + "_files"))
        finally:
            proc.terminate()
            out = proc.communicate(timeout=5)[0]
        assert "Traceback" not in out, out


def test_calls():
    port = free_port()
    with tempfile.TemporaryDirectory() as tmp:
        proc = run_server(port, os.path.join(tmp, "call.db"))
        try:
            assert asyncio.run(call_scenario(port))
        finally:
            proc.terminate()
            out = proc.communicate(timeout=5)[0]
        assert "Traceback" not in out, out


def test_video():
    port = free_port()
    with tempfile.TemporaryDirectory() as tmp:
        proc = run_server(port, os.path.join(tmp, "vid.db"), "--admin", "boss")
        try:
            assert asyncio.run(video_scenario(port))
        finally:
            proc.terminate()
            out = proc.communicate(timeout=5)[0]
        assert "Traceback" not in out, out


def test_voice():
    port = free_port()
    with tempfile.TemporaryDirectory() as tmp:
        proc = run_server(port, os.path.join(tmp, "v.db"), "--admin", "boss")
        try:
            assert asyncio.run(voice_scenario(port))
        finally:
            proc.terminate()
            out = proc.communicate(timeout=5)[0]
        assert "Traceback" not in out, out


def test_groups():
    port = free_port()
    with tempfile.TemporaryDirectory() as tmp:
        proc = run_server(port, os.path.join(tmp, "g.db"))
        try:
            assert asyncio.run(group_scenario(port))
        finally:
            proc.terminate()
            out = proc.communicate(timeout=5)[0]
        assert "Traceback" not in out, out


def run_server(port, db, *extra):
    proc = subprocess.Popen(
        [sys.executable, os.path.join(ROOT, "server.py"), "--nogui", "--host", "127.0.0.1",
         "--port", str(port), "--db", db, "--files", db + "_files", *extra],
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
            [sys.executable, os.path.join(ROOT, "server.py"), "--nogui", "--host", "127.0.0.1",
             "--port", str(port), "--db", os.path.join(tmp, "t.db"), "--files", os.path.join(tmp, "files")],
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
    test_groups()
    test_voice()
    test_video()
    test_calls()
    test_files()
    test_delete()
    print("server tests passed")
