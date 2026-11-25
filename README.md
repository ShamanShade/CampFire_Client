# Campfire

A small Discord-style chat app: a Python server and a PyQt6 desktop client.

- Channels with saved history, unread counters and `+` to create new ones
- Direct messages (double-click someone, or `/dm name hi`)
- Group DMs with up to 50 people (the `+` next to Direct messages)
- Friends with online/offline status, and requests you can accept or decline
- Accounts are created on first login; passwords are stored as salted scrypt hashes
- Avatars (Settings → Choose image), and changing your username
- The client reconnects on its own if the server restarts or the connection drops
- Optional TLS encryption
- Admin accounts with kick, ban, IP ban, mute, announcements, channel cleanup and password resets

## Running the server

Needs Python 3.9+ and nothing else.

```
python server.py                       # listens on 0.0.0.0:55555, saves to campfire.db
python server.py --port 6000 --db data.db
```

For friends to connect from outside your network, forward TCP port 55555 on your
router to the machine running the server and allow it through the firewall.

## Admins

Admins are chosen on the server, so nobody can make themselves admin from the chat:

```
python server.py --admin YourName                 # repeat --admin for more than one
python server.py --remove-admin SomeName          # take it away again
```

The name doesn't need to exist yet; it becomes admin the first time it logs in. Admin is
saved in the database, so after that you can start the server normally. Admins get a gold
★ next to their name, an **Admin** menu, and extra options when right-clicking a person or
a channel. They can't use these on themselves or on other admins.

| Command | Does |
|---|---|
| `/kick user [reason]` | Disconnect someone (they can log back in) |
| `/ban user [reason]` / `/unban user` | Block an account from logging in |
| `/ipban user-or-ip [reason]` / `/unipban user-or-ip` | Block an address (stops new accounts too) |
| `/mute user [minutes] [reason]` / `/unmute user` | Stop someone sending messages; no minutes = until unmuted |
| `/announce text` | Highlighted message to everyone |
| `/clearhistory [#room]` | Delete a channel's messages (defaults to the current one) |
| `/delroom [#room]` | Delete a channel (not `#general`) |
| `/resetpass user newpass` | Set a new password for someone who forgot theirs |
| `/bans` | List banned accounts and addresses |

## Running the client

```
pip install -r requirements.txt
python campfire_client.py
```

The client only connects to one server, set at the top of `campfire_client.py`:

```python
SERVER_HOST = "74.132.179.199"
SERVER_PORT = 55555
```

Change those lines if your server's address changes. There's no way to pick a different
server from the app.

If the server runs on the same PC (or the same home network) as the client, connecting to
your public IP may not work, because many routers don't loop traffic back inside the
house. Test locally by temporarily setting `SERVER_HOST = "localhost"`, and change it back
before giving the client to friends.

Put an `icon.png` next to `campfire_client.py` to brand the window.

### Chat commands

| Command | Does |
|---|---|
| `/join #room` | Open a channel, creating it if needed |
| `/dm user [message]` | Open a DM, optionally sending a message |
| `/friend user` | Send a friend request |
| `/nick newname` | Change your username |
| `/clear` | Clear the view |
| `//text` | Send a message that starts with `/` |

Right-click anyone in Online or Friends for Message / Add friend / Remove friend.

### Group DMs

Click `+` next to **Direct messages**, tick the people you want (or type a username),
optionally name the group, and press OK. A group holds up to 50 people, including you.

- Anyone in the group can add people and rename it. The **Members** panel on the right
  shows who's in it, and new members can read the history from before they joined.
- The creator is the owner (♛) and is the only one who can remove people. If the owner
  leaves, the longest-standing member becomes owner. A group with nobody left is deleted.
- Right-click a group in the sidebar for Add people, Rename, Remove member and Leave.

| Command | Does |
|---|---|
| `/group new user1 user2 ...` | Start a group (no names opens the picker) |
| `/group add user ...` | Add people to the open group |
| `/group name New Name` | Rename the open group |
| `/group remove user` | Remove someone (owner only) |
| `/group leave` | Leave the open group |
| `/group members` | List who's in it |

## Turning on TLS (recommended over the internet)

Without TLS, passwords and messages cross the network unencrypted. Make a certificate
for your server's address (replace the IP):

```
openssl req -x509 -newkey rsa:2048 -nodes -days 3650 -keyout key.pem -out cert.pem \
  -subj "/CN=campfire" -addext "subjectAltName=IP:74.132.179.199"
python server.py --certfile cert.pem --keyfile key.pem
```

Put `cert.pem` (never `key.pem`) next to `campfire_client.py`, set `USE_TLS = True` at the
top of that file, and give that copy of the client to your friends.

## Tests

```
python tests/test_server.py
QT_QPA_PLATFORM=offscreen python tests/test_gui.py
```

## Protocol

Newline-delimited JSON in both directions; every object has a `"t"` type.

Client → server: `login {user, pass}`, `msg {room, text}`, `history {room}`,
`create_room {room}`, `dm {to, text}`, `dm_history {with}`, `group_create {name, members}`, `group_add {gid, users}`,
`group_leave {gid}`, `group_kick {gid, user}`, `group_rename {gid, name}`, `group_msg {gid, text}`,
`group_history {gid}`, `friend_request {to}`,
`friend_respond {user, accept}`, `friend_remove {user}`, `avatar {data}` (base64 image),
`rename {name}`, `ping`, and for admins `admin {cmd, user?, reason?, minutes?, room?, text?, password?}`
where `cmd` is one of `kick ban unban ipban unipban mute unmute announce clearhistory delroom
resetpass bans`.

Server → client: `hello`, `login_ok {user, registered, rooms, users}`, `login_fail {reason}`,
`msg {room, user, text, ts}`, `history {room, messages}`, `dm {from, to, text, ts}`,
`dm_history {with, messages}`, `rooms {rooms}`, `user_join {name, avatar}`,
`user_leave {name}`, `user_update {old, name, avatar}`, `renamed {name}`,
`friends {friends, requests}`, `groups {groups: [{id, name, owner, members}]}`, `group_created {gid}`,
`group_msg {gid, user, text, ts}`, `group_history {gid, messages}`, `group_notice {gid, text, ts}`,
`kicked {reason}`, `muted {until, reason}`, `announce {from, text, ts}`,
`cleared {room}`, `notice {msg}`, `error {msg}`, `pong`. User entries and messages carry an `admin` flag.
