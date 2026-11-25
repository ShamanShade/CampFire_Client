# Campfire

A small Discord-style chat app: a Python server and a PyQt6 desktop client.

- Channels with saved history, unread counters and `+` to create new ones
- Direct messages (double-click someone, or `/dm name hi`)
- Group DMs with up to 50 people (the `+` next to Direct messages)
- Voice channels (Opus audio over the same connection - no extra ports to open)
- Camera and screen sharing inside voice channels (VP8 video)
- File sharing up to 5 GB per file, with resume, progress and picture previews
- Emoji picker and :shortcodes:, animated GIFs, GIF and image links, paste to send
- Friends with online/offline status, and requests you can accept or decline
- Accounts are created on first login; passwords are stored as salted scrypt hashes
- Avatars (Settings → Choose image), and changing your username
- The client reconnects on its own if the server restarts or the connection drops
- Optional TLS encryption
- Admin accounts with kick, ban, IP ban, mute, announcements, channel cleanup and password resets

## Running the server

```
python server.py                       # opens the server window, listens on port 55555
python server.py --nogui               # console only (e.g. on a VPS with no screen)
python server.py --port 6000 --db data.db
```

The **server window** starts the server straight away and shows:

- Status, a Start/Stop button and the port, plus the addresses people connect to
- Live counts: online, accounts, people in voice, channels, group DMs, messages, uptime
- **Online**: who's connected, their IP, voice channel or call, video and mute, with Kick,
  Mute, Disconnect from voice and Ban (right-click for everything)
- **Accounts**: every account with search; **Make admin / Remove admin** (takes effect
  immediately, no need to log out), reset password, mute, ban, unban
- **Channels**: add, clear and delete text and voice channels
- **Bans**: banned accounts and IP addresses; unban, or ban an IP
- **Log**: everything the server does, live, plus a box to send announcements

Closing the window stops the server (it asks first if people are online). The console mode
needs only Python; the window needs PyQt6.

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

### On the PC that runs the server: use `campfire_admin.py`

Connecting to your own public IP from inside your house usually fails (most routers don't
loop that traffic back in), so the normal client can't log you in on the server PC. Run the
admin client there instead. It's the same app, but it connects to the server on that PC:

```
python campfire_admin.py
```

Give friends `campfire_client.py`; keep `campfire_admin.py` for yourself.

### Can't log in?

- **"Server is not running at that address" / "timed out"**: start `python server.py` and
  leave its window open. On the server PC, use `campfire_admin.py`. For friends, port
  55555 must be forwarded on your router and allowed through Windows Firewall.
- **"The server isn't answering"**: the old server is still running. Close it and start the
  new `server.py`.
- **"Wrong password for that username"**: that name already has an account with another
  password. An admin can reset it with `/resetpass name newpass`, or pick another name.

`icon.png` is the app icon (windows, taskbar, login screen and the server window) and
`avatar.png` is the picture for anyone who hasn't chosen their own avatar. Replace either file
to change them; `icon.ico` is the same icon for Windows shortcuts.

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
| `/group call` | Start, join or leave the group's call |

### Group calls

Open a group DM and press **Start call** in the bar under its name. Everyone else in the
group gets a ring with **Join** / **Ignore**, and the sidebar shows 🔊 and how many people
are in the call. Calls work like voice channels: mute, deafen, camera, screen sharing and the
video stage all work, and only group members can see or join. A call holds up to 50 people
and ends when the last person leaves. Being removed from the group drops you from its call.

## Voice channels

Click a channel under **Voice channels** to join; click another to switch. Everyone can see
who's in each channel, and the name of whoever is talking turns green.

- **Mute / Deafen / Leave** are in the bar at the bottom left. Deafen also stops you sending.
- **Settings > Voice**: pick your microphone and speakers, and choose *Voice activity*
  (drag the sensitivity slider so your voice passes it and background noise doesn't) or
  *Push to talk* (hold the chosen key, default `` ` ``, or the "Hold to talk" button).
  The push-to-talk key only works while the Campfire window is focused.
- Right-click someone in voice to change their volume (0-200%).
- Use headphones: there's no echo cancellation, so speakers feed back into your mic.
- `+` creates a voice channel. Up to 25 people per channel.
- Admins: right-click a person to **Disconnect from voice**, right-click a channel to delete
  it, or `/voicekick user` and `/delvoice name`. Admin mutes also silence voice.

Voice needs three extra packages, included in `pip install -r requirements.txt`
(`numpy`, `av` for the Opus codec, `sounddevice` for the mic and speakers). Each person
talking uses about 70 kbps for each listener, which is fine on home internet for a handful of
people talking at once.

## Camera and screen sharing

Join a voice channel, then use **Camera** or **Screen** in the voice bar. **Video** opens the
video stage for your channel (click a text channel or **Back to chat** to return).

- Cameras show up for everyone in the channel automatically. Screen shares show a
  **Watch stream** button, so nobody downloads a stream they didn't ask for.
- Double-click a video to focus it; right-click for **Pop out** (its own window; double-click
  or F11 there for full screen) and **Stop watching**.
- **Settings > Camera & screen sharing**: pick your camera, and screen share quality
  (720p or 1080p, 5/15/30 fps). With more than one monitor you're asked which to share.
- People streaming have a camera or LIVE icon in the voice channel list.
- Video stays on if you switch voice channels and turns off when you leave voice. An admin
  mute also stops it.
- Screen sharing sends the picture only (no computer audio), and the mouse pointer isn't
  captured.
- On Windows, if the camera doesn't start, check Settings > Privacy & security > Camera >
  "Let desktop apps access your camera".

Bandwidth: a camera is about 0.6 Mbps and a 720p/15 fps screen share about 1.5 Mbps, sent
once to the server and then once **per viewer** by the server. The server PC's upload speed is
what limits how many people can watch at once.

## Emoji and GIFs

- **😀 button** next to the message box: emoji by category, a search box ("fire", "laugh",
  "cat"), recently used emoji, and a **GIF** tab with your favorite GIFs.
- **Shortcodes** turn into emoji as you type: `:fire:` `:joy:` `:thumbsup:` `:heart:` `:skull:`
  `:pray:` `:100:` `:eyes:` and more. Any emoji's Unicode name also works, like
  `:rolling_on_the_floor_laughing:`.
- Messages that are only emoji (up to 10) are shown big.
- **GIFs play in the chat**, whether they were sent as files, pasted, or linked. They pause when
  you're looking at another conversation. Turn playing off in Settings > Chat.
- **Links to pictures and GIFs show inline**: direct image links, Giphy links and Tenor links.
  Your client fetches them itself (so the site sees your IP address); turn it off in
  Settings > Chat.
- **Paste with Ctrl+V** to send a screenshot, a copied GIF or copied files. Pasting text still
  pastes text.
- **Favorite GIFs**: right-click a GIF in the chat > **Add to favorite GIFs**, or **Add GIF from
  file...** in the picker. Click one in the picker's GIF tab to send it. Favorites are kept on
  your computer only.
- **GIPHY search**: the picker's GIF tab has **GIPHY** (trending, and search as you type) and
  **Favorites**. Click a GIF to send it; it shows as just the GIF. To turn it on, the server
  owner pastes a GIPHY API key into the server window's **Settings** tab (free from
  developers.giphy.com > Create an App > API). The key is kept in the server's database, not in
  the code, and given to clients when they log in. In console mode use `--giphy-key KEY` or the
  `GIPHY_API_KEY` environment variable. A free key allows about 100 searches an hour for
  everyone together, so results are cached for 10 minutes; apply for a production key on the
  GIPHY dashboard if you hit the limit.

## Sending files

Click 📎 next to the message box, or drag files onto the window. Files go to whatever is
open: a channel, a DM or a group DM. Anything typed in the message box is sent as the caption.

- Up to **5 GB** per file. A progress bar shows speed and time left, with **Cancel**.
- Files travel on their own connection (same server and port), so chat, voice and video keep
  working during a big transfer. If the connection drops, the transfer reconnects and
  continues from where it stopped, for both uploads and downloads.
- Files show as cards with **Download**. Pictures up to 10 MB show a preview; click it to open.
  After downloading: **Open** and **Show in folder**.
- Only people who can see the message can download the file (DM and group files stay private).
- The server keeps files in a `campfire_files` folder next to `server.py` by default. To
  keep them somewhere else (a bigger drive, say), open the server window's **Files** tab and
  click **Change...** next to *Storage folder*. It offers to move the existing files there
  (with a progress bar), restarts the server on the new folder (people reconnect by
  themselves), and remembers the choice. In console mode use `--files "D:\Campfire Files"`.
- The **Files** tab also lists every file with size, sender and where it was sent, shows the
  free space on the storage drive, and can delete files. Admins can also delete a file; clearing or deleting a
  channel deletes its files too. Unfinished uploads are kept for 2 days so they can resume.
- The server refuses an upload if its disk would get too full. Remember downloads use the
  server PC's upload speed.

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
python tests/test_voice.py
QT_QPA_PLATFORM=offscreen python tests/test_gui.py
QT_QPA_PLATFORM=offscreen python tests/test_voice_gui.py
QT_QPA_PLATFORM=offscreen python tests/test_video.py
QT_QPA_PLATFORM=offscreen python tests/test_video_gui.py
QT_QPA_PLATFORM=offscreen python tests/test_call_gui.py
QT_QPA_PLATFORM=offscreen python tests/test_server_gui.py
QT_QPA_PLATFORM=offscreen python tests/test_files_gui.py
QT_QPA_PLATFORM=offscreen python tests/test_emoji_gif_gui.py      # needs pillow to make test GIFs
QT_QPA_PLATFORM=offscreen python tests/test_giphy_gui.py          # uses a fake GIPHY, no key needed
QT_QPA_PLATFORM=offscreen python tests/test_login.py
```

## Protocol

Newline-delimited JSON in both directions; every object has a `"t"` type.

Client → server: `login {user, pass}`, `msg {room, text}`, `history {room}`,
`create_room {room}`, `dm {to, text}`, `dm_history {with}`, `group_create {name, members}`, `group_add {gid, users}`,
`group_leave {gid}`, `group_kick {gid, user}`, `group_rename {gid, name}`, `group_msg {gid, text}`,
`group_history {gid}`, `voice_create {name}`, `voice_join {channel, muted, deaf}` (channel `g:<gid>` for a group call), `voice_leave`,
`voice_status {muted, deaf}`, `voice_data {d}` (base64 Opus, 20 ms), `video_start {kind}`,
`video_stop {kind}`, `video_watch {user, kind}`, `video_unwatch {user, kind}`,
`video_data {kind, d, k}` (base64 VP8 frame, k = keyframe; kind is camera or screen), `friend_request {to}`,
`friend_respond {user, accept}`, `friend_remove {user}`, `avatar {data}` (base64 image),
`rename {name}`, `ping`, and for admins `admin {cmd, user?, reason?, minutes?, room?, text?, password?}`
where `cmd` is one of `kick ban unban ipban unipban mute unmute announce clearhistory delroom
resetpass bans voicekick delvoice delfile`.

Server → client: `hello`, `login_ok {user, registered, rooms, users}`, `login_fail {reason}`,
`msg {room, user, text, ts}`, `history {room, messages}`, `dm {from, to, text, ts}`,
`dm_history {with, messages}`, `rooms {rooms}`, `user_join {name, avatar}`,
`user_leave {name}`, `user_update {old, name, avatar}`, `renamed {name}`,
`friends {friends, requests}`, `groups {groups: [{id, name, owner, members}]}`, `group_created {gid}`,
`group_msg {gid, user, text, ts}`, `group_history {gid, messages}`, `group_notice {gid, text, ts}`,
`voice_state {channels: [{name, users: [{name, muted, deaf, server_muted, camera, screen}]}]}`, `voice_joined {channel}`,
`voice_left {reason}`, `voice_data {u, d}`, `video_data {u, kind, d, k}`, `video_keyframe {kind}`,
`video_stopped {reason}`, `call_state {gid, users}`, `call_ring {gid, from, name}`,
`admin_changed {admin}`, `file_deleted {fid}`, `config {giphy_key}`. Messages can carry `file {id, name, size}`.

File transfers use a second connection that logs in with `login {user, pass, transfer: true}`,
then `upload {id, conv, name, size, text}` → `upload_ready {offset}` → raw bytes →
`upload_done {fid}`, or `download {fid, offset}` → `download_ready {name, size, offset}` → raw
bytes. Errors come back as `transfer_error {msg}`. `kicked {reason}`, `muted {until, reason}`, `announce {from, text, ts}`,
`cleared {room}`, `notice {msg}`, `error {msg}`, `pong`. User entries and messages carry an `admin` flag.
