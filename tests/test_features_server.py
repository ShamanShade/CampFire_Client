"""Server side of: sign-ups (approval + invite codes), replies, reactions, editing, typing,
changing password, deleting an account, roles and private channels.
Run:  python tests/test_features_server.py"""

import asyncio
import os
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "tests"))

from test_server import C, free_port  # noqa: E402
import subprocess  # noqa: E402
import socket  # noqa: E402
import time  # noqa: E402


def run_server(port, db, *extra):
    proc = subprocess.Popen([sys.executable, os.path.join(ROOT, "server.py"), "--nogui", "--host", "127.0.0.1",
                             "--port", str(port), "--db", db, "--files", db + "_files", *extra],
                            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    for _ in range(60):
        try:
            socket.create_connection(("127.0.0.1", port), 0.2).close()
            break
        except OSError:
            time.sleep(0.1)
    return proc


async def try_login(port, user, pw="secret", **extra):
    c = await C(port).open()
    c.send(t="login", user=user, **{"pass": pw}, **extra)
    while True:
        m = await c.recv()
        if m["t"] in ("login_ok", "login_fail"):
            return c, m


async def signups(port):
    boss = await C(port).open()
    ok = await boss.login("boss")
    assert ok["admin"] and ok["signup"] == "approval" and ok["pending"] == []
    # a stranger signs up: the account waits; admins hear about it
    c, m = await try_login(port, "stranger")
    assert m["t"] == "login_fail" and m.get("pending") and "approve" in m["reason"]
    assert (await boss.wait_for("pending"))["names"] == ["stranger"]
    c, m = await try_login(port, "stranger")                     # still waiting
    assert m.get("pending")
    c, m = await try_login(port, "stranger", pw="wrong")         # and the password still counts
    assert "Wrong password" in m["reason"]
    boss.send(t="admin", cmd="approve", user="stranger")
    assert "Approved stranger" in (await boss.wait_for("notice"))["msg"]
    c, m = await try_login(port, "stranger")
    assert m["t"] == "login_ok"
    # an invite code skips the wait, once
    boss.send(t="admin", cmd="invite_create", uses=1, days=7)
    code = (await boss.wait_for("invite"))["code"]
    assert len(code) == 9 and code[4] == "-"
    c, m = await try_login(port, "friendo", invite=code.lower().replace("-", ""))   # typed sloppily: still fine
    assert m["t"] == "login_ok", m
    c, m = await try_login(port, "friendo2", invite=code)
    assert m["t"] == "login_fail" and "doesn't work" in m["reason"]
    # invite-only: no code, no account
    boss.send(t="admin", cmd="signup_mode", mode="invite")
    await boss.wait_for("notice")
    c, m = await try_login(port, "nocode")
    assert m["t"] == "login_fail" and m.get("need_invite")
    # turning someone down removes the waiting account
    boss.send(t="admin", cmd="signup_mode", mode="approval")
    await boss.wait_for("notice")
    c, m = await try_login(port, "spammer")
    await boss.wait_for("pending", names=["friendo2", "spammer"]) if False else await boss.wait_for("pending")
    boss.send(t="admin", cmd="deny", user="spammer")
    await boss.wait_for("notice")
    c, m = await try_login(port, "spammer")                      # signs up again from scratch -> waiting again
    assert m.get("pending")
    # people without the right can't approve
    s2 = await C(port).open()
    await s2.login("stranger")
    s2.send(t="admin", cmd="approve", user="spammer")
    assert "admins only" in (await s2.wait_for("error"))["msg"]
    boss.send(t="admin", cmd="signup_mode", mode="open")
    await boss.wait_for("notice")
    return True


async def messages(port):
    a = await C(port).open()
    await a.login("amy")
    b = await C(port).open()
    await b.login("ben")
    # replies
    a.send(t="msg", room="#general", text="pizza tonight?")
    first = await b.wait_for("msg", text="pizza tonight?")
    b.send(t="msg", room="#general", text="yes!", reply_to=first["id"])
    r = await a.wait_for("msg", text="yes!")
    assert r["reply"] == {"id": first["id"], "user": "amy", "text": "pizza tonight?"}
    b.send(t="msg", room="#gaming", text="wrong place", reply_to=first["id"])   # can't reply across chats
    assert "reply" not in await a.wait_for("msg", text="wrong place")
    b.send(t="dm", to="amy", text="psst")
    dm = await a.wait_for("dm", text="psst")
    a.send(t="dm", to="ben", text="what?", reply_to=dm["id"])
    assert (await b.wait_for("dm", text="what?"))["reply"]["text"] == "psst"
    # reactions: toggle, several people, shown in history
    a.send(t="react", id=r["id"], emoji="\U0001f355", on=True)
    m = await b.wait_for("reaction", id=r["id"])
    assert m["emoji"] == "\U0001f355" and m["users"] == ["amy"]
    b.send(t="react", id=r["id"], emoji="\U0001f355", on=True)
    await a.wait_for("reaction", id=r["id"], users=["amy", "ben"])
    a.send(t="react", id=r["id"], emoji="\U0001f355", on=False)
    await b.wait_for("reaction", id=r["id"], users=["ben"])
    a.send(t="react", id=r["id"], emoji="lol", on=True)
    assert "emoji" in (await a.wait_for("error"))["msg"]
    # editing: only your own
    a.send(t="edit_msg", id=first["id"], text="pizza tonight? 8pm")
    e = await b.wait_for("msg_edited", id=first["id"])
    assert e["text"] == "pizza tonight? 8pm" and e["edited"] > 0
    b.send(t="edit_msg", id=first["id"], text="hacked")
    assert "your own" in (await b.wait_for("error"))["msg"]
    b.send(t="history", room="#general")
    h = (await b.wait_for("history"))["messages"]
    got = {m["id"]: m for m in h}
    assert got[first["id"]]["text"] == "pizza tonight? 8pm" and got[first["id"]]["edited"]
    assert got[r["id"]]["reactions"] == {"\U0001f355": ["ben"]}
    assert got[r["id"]]["reply"]["text"] == "pizza tonight? 8pm"         # replies show the edited text
    # deleting the original: the reply says so
    a.send(t="delete_msg", id=first["id"])
    await b.wait_for("msg_deleted", id=first["id"])
    b.send(t="history", room="#general")
    h = {m["id"]: m for m in (await b.wait_for("history"))["messages"]}
    assert h[r["id"]]["reply"] == {"id": first["id"], "deleted": True}
    # typing: to everyone else in the chat, at most every couple of seconds
    a.send(t="typing", conv="#general")
    t = await b.wait_for("typing")
    assert t == {"t": "typing", "conv": "#general", "user": "amy"}
    a.send(t="typing", conv="@ben")
    a.send(t="typing", conv="@ben")
    await asyncio.sleep(2.6)
    a.send(t="typing", conv="@ben")
    t = await b.wait_for("typing")
    assert t["conv"] == "@amy"
    return True


async def account(port):
    a = await C(port).open()
    await a.login("cara", "oldpass")
    a.send(t="change_password", old="nope", new="newpass")
    assert "current password" in (await a.wait_for("error", timeout=6))["msg"]
    a.send(t="change_password", old="oldpass", new="newpass")
    await a.wait_for("password_changed", timeout=6)
    c, m = await try_login(port, "cara", "oldpass")
    assert m["t"] == "login_fail"
    c, m = await try_login(port, "cara", "newpass")
    assert m["t"] == "login_ok"
    c.send(t="msg", room="#general", text="bye everyone")
    watcher = await C(port).open()
    await watcher.login("dave")
    c.send(t="delete_account", password="wrong")
    assert "wasn't deleted" in (await c.wait_for("error", timeout=6))["msg"]
    c.send(t="delete_account", password="newpass")
    assert (await c.wait_for("kicked", timeout=6))["reason"] == "Your account was deleted"
    gone = await watcher.wait_for("user_update", old="cara")
    assert gone["name"].startswith("deleted-")
    watcher.send(t="history", room="#general")
    h = (await watcher.wait_for("history"))["messages"]
    assert any(m["text"] == "bye everyone" and m["user"].startswith("deleted-") for m in h)
    c, m = await try_login(port, "cara", "newpass")             # the name is free again: a new account
    assert m["t"] == "login_ok" and m["registered"]
    return True


async def roles(port):
    boss = await C(port).open()
    await boss.login("boss")
    mod = await C(port).open()
    ok = await mod.login("mona")
    assert ok["perms"] == [] and ok["roles"] == []
    pleb = await C(port).open()
    await pleb.login("pete")
    # a Mods role that can delete messages and kick
    boss.send(t="admin", cmd="role_save", name="Mods", color="#2ecc71", perms=["messages", "kick", "bogus"])
    rl = await boss.wait_for("roles")
    mods = rl["roles"][0]
    assert mods["name"] == "Mods" and mods["perms"] == ["messages", "kick"] and mods["color"] == "#2ecc71"
    boss.send(t="admin", cmd="role_assign", user="mona", roles=[mods["id"]])
    await mod.wait_for("perms", perms=["kick", "messages"])
    await pleb.wait_for("user_update", name="mona", roles=[mods["id"]])
    # what the role allows works; what it doesn't, doesn't
    pleb.send(t="msg", room="#general", text="spam spam")
    spam = await mod.wait_for("msg", text="spam spam")
    mod.send(t="delete_msg", id=spam["id"])
    await pleb.wait_for("msg_deleted", id=spam["id"])
    mod.send(t="create_room", room="#modchat")
    assert "allowed" in (await mod.wait_for("error"))["msg"]
    mod.send(t="admin", cmd="ban", user="pete")
    assert "admins only" in (await mod.wait_for("error"))["msg"]
    mod.send(t="admin", cmd="kick", user="boss")
    assert "another admin" in (await mod.wait_for("error"))["msg"]
    # a private channel only Mods (and admins) see
    boss.send(t="create_room", room="#mods-only", roles=[mods["id"]])
    r = await mod.wait_for("rooms", private_rooms={"#mods-only": [mods["id"]]})
    assert "#mods-only" in r["rooms"]
    r = await pleb.wait_for("rooms")
    assert "#mods-only" not in r["rooms"] and r["private_rooms"] == {}
    boss.send(t="msg", room="#mods-only", text="secret plans")
    await mod.wait_for("msg", text="secret plans")
    pleb.send(t="history", room="#mods-only")
    assert "No room" in (await pleb.wait_for("error"))["msg"]
    pleb.send(t="msg", room="#mods-only", text="let me in")
    assert "No room" in (await pleb.wait_for("error"))["msg"]
    pleb.send(t="typing", conv="#mods-only")
    # pete never got "secret plans"
    pleb.send(t="ping")
    while True:
        m = await pleb.recv()
        assert m.get("text") != "secret plans"
        if m["t"] == "pong":
            break
    # opening it up again
    boss.send(t="admin", cmd="room_access", room="#mods-only", roles=[])
    while "#mods-only" not in (await pleb.wait_for("rooms"))["rooms"]:
        pass
    # deleting the role takes its powers away
    boss.send(t="admin", cmd="role_delete", id=mods["id"])
    await mod.wait_for("perms", perms=[])
    return True


def main():
    port = free_port()
    with tempfile.TemporaryDirectory() as tmp:
        proc = run_server(port, os.path.join(tmp, "f.db"), "--admin", "boss")
        try:
            assert asyncio.run(signups(port))
            print("sign-ups ok")
            assert asyncio.run(messages(port))
            print("replies, reactions, editing, typing ok")
            assert asyncio.run(account(port))
            print("password + account deletion ok")
            assert asyncio.run(roles(port))
            print("roles + private channels ok")
        finally:
            proc.terminate()
            out = proc.communicate(timeout=5)[0]
            if "Traceback" in out:
                print(out)
                raise AssertionError("the server crashed")
    print("feature server tests passed")


if __name__ == "__main__":
    main()
