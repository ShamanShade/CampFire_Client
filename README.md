# Campfire

A small Discord-style chat app: a Python server and a PyQt6 desktop client.

- Channels with saved history, unread counters and `+` to create new ones
- Direct messages (double-click someone, or `/dm name hi`)
- Friends with online/offline status, and requests you can accept or decline
- Accounts are created on first login; passwords are stored as salted scrypt hashes
- Avatars (Settings → Choose image), and changing your username
- The client reconnects on its own if the server restarts or the connection drops
- Optional TLS encryption

## Running the server

Needs Python 3.9+ and nothing else.

```
python server.py                       # listens on 0.0.0.0:55555, saves to campfire.db
python server.py --port 6000 --db data.db
```

For friends to connect from outside your network, forward TCP port 55555 on your
router to the machine running the server and allow it through the firewall.

## Running the client

```
pip install -r requirements.txt
python campfire_client.py
```

Type the server as `host:port` on the login screen (it remembers the last one). Put an
`icon.png` next to `campfire_client.py` to brand the window.

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

## Turning on TLS (recommended over the internet)

Without TLS, passwords and messages cross the network unencrypted. Make a certificate
for your server's address (replace the IP):

```
openssl req -x509 -newkey rsa:2048 -nodes -days 3650 -keyout key.pem -out cert.pem \
  -subj "/CN=campfire" -addext "subjectAltName=IP:74.132.179.199"
python server.py --certfile cert.pem --keyfile key.pem
```

Give `cert.pem` (never `key.pem`) to your friends. The client must trust it: set the
`tls_ca` setting to the path of `cert.pem` (Windows registry
`HKCU\Software\Campfire\Campfire`, or `~/.config/Campfire/Campfire.conf` on Linux), then
tick **Use TLS** on the login screen.

## Tests

```
python tests/test_server.py
QT_QPA_PLATFORM=offscreen python tests/test_gui.py
```

## Protocol

Newline-delimited JSON in both directions; every object has a `"t"` type.

Client → server: `login {user, pass}`, `msg {room, text}`, `history {room}`,
`create_room {room}`, `dm {to, text}`, `dm_history {with}`, `friend_request {to}`,
`friend_respond {user, accept}`, `friend_remove {user}`, `avatar {data}` (base64 image),
`rename {name}`, `ping`.

Server → client: `hello`, `login_ok {user, registered, rooms, users}`, `login_fail {reason}`,
`msg {room, user, text, ts}`, `history {room, messages}`, `dm {from, to, text, ts}`,
`dm_history {with, messages}`, `rooms {rooms}`, `user_join {name, avatar}`,
`user_leave {name}`, `user_update {old, name, avatar}`, `renamed {name}`,
`friends {friends, requests}`, `kicked {reason}`, `notice {msg}`, `error {msg}`, `pong`.
