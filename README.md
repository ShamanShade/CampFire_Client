# Campfire

A small Discord-style chat app: a Python server and a PyQt6 desktop client.

- Channels with saved history, unread counters and `+` to create new ones
- Direct messages (double-click someone, or `/dm name hi`)
- Group DMs with up to 50 people (the `+` next to Direct messages)
- Voice channels (Opus audio over the same connection - no extra ports to open)
- Camera and screen sharing inside voice channels (VP8 video)
- File sharing up to 5 GB per file, with resume, progress and picture previews
- Emoji picker and :shortcodes:, animated GIFs, GIF and image links, paste to send
- Delete messages (right-click a message, or click its time)
- Friends with online/offline status, and requests you can accept or decline
- Accounts are created on first login; passwords are stored as salted scrypt hashes
- Avatars (Settings → Choose image), and changing your username
- The client reconnects on its own if the server restarts or the connection drops
- Optional TLS encryption
- Admin accounts with kick, ban, IP ban, mute, announcements, channel cleanup and password resets

## Quick start (double-click)

**With Python 3.9+ installed** (from python.org): in this folder, double-click

| File | What it's for |
|---|---|
| **Campfire.pyw** | The chat app, for everyone (including the PC that runs the server). |
| **Campfire Server.pyw** | Runs the server (opens the server window). Only on the PC that hosts it. |

The first time, they install what they need by themselves (a small "Setting up" window shows while
that happens). If a .pyw opens in a text editor instead, right-click it > **Open with** > **Python**.
If anything goes wrong you get a message, and the details are saved in `campfire_error.log`.

To put one on your desktop: right-click it > **Show more options** > **Send to** > **Desktop
(create shortcut)**.

**Without Python** (e.g. for friends): on the repo's
[Releases page](https://github.com/ShamanShade/CampFire_Client/releases/tag/latest) there are three
downloads:

- **Campfire-Setup.exe**: the normal installer for the chat app. Double-click it, click through,
  and Campfire is in the Start menu (and on the desktop if you tick the box). No admin rights
  needed; it installs just for you in `%LocalAppData%\Programs\Campfire`. To update, run a newer
  Campfire-Setup.exe; to remove it, use Windows Settings > Apps > Installed apps > Campfire >
  Uninstall. Your login and settings are kept.
  **Updates:** when a new version is out, Campfire asks when it starts (**Update now** / **Later** /
  **Skip this version**). Update now downloads the new setup, installs it and reopens Campfire.
  You can also use Campfire > *Check for updates*, or turn the check off in Settings.
- **Campfire-Server-Setup.exe**: installs the server (with its window) on the PC that hosts
  Campfire: Start menu shortcut, no Python, no admin rights (`%LocalAppData%\Programs\Campfire Server`).
  Accounts, messages and uploads live in that folder and are kept when it updates or is
  uninstalled. It updates itself: **Update server** in its window. Moving over from the .pyw or the
  zip? Close the old server, then in the new one use **Settings > Import from an old server** and
  pick the old folder (the one with `campfire.db`).
- **Campfire-Windows.zip**: both programs with no install. Unzip it anywhere and double-click
  **Campfire.exe**, or **Campfire Server.exe** on the PC that hosts the server.
GitHub rebuilds the zip automatically every time the code changes, and checks each build
(voice, video, emoji and icons are there, and the server starts) before publishing it. Windows may say
"Windows protected your PC" the first time because the app isn't signed: click **More info > Run
anyway**.

## Campfire in a web browser

The website is on GitHub: **https://shamanshade.github.io/CampFire_Client/**. No install, works on
phones, Chromebooks and school or work PCs, and it has **voice chat**.

It needs https on your server (browsers only let an https page talk to an https server, and only
allow the microphone on https). Set it up once in the server window, **Settings > Website and voice
in the browser**:

1. Go to [duckdns.org](https://www.duckdns.org), sign in with GitHub and add a name, e.g.
   `campfire-shaman` (it becomes `campfire-shaman.duckdns.org`).
2. Paste the name and the token (shown at the top of duckdns.org) into the server window and click
   **Set up https**. It gets a free Let's Encrypt certificate in a minute or two.

3. Optional, but needed to use the website **at home**: add a second DuckDNS name (e.g.
   `campfire-test`) and put it in the *home network* box. Many routers can't send you back in
   through your own public address, so at home the website can't reach the server through the
   main name. The second name points at the server PC's address on your home network instead, and
   the certificate covers both. At home (on the server PC, or phones on your Wi-Fi) use
   `https://shamanshade.github.io/CampFire_Client/?server=campfire-test`. The page remembers it;
   to switch back, type the main name in the *Server* box on the login screen. If your browser asks
   to "access devices on your local network", allow it.

That's it: no new router ports (https uses the same port, 55555), the name follows your home IP,
and the certificate renews itself. The server window then shows the link to share, like
`https://shamanshade.github.io/CampFire_Client/?server=campfire-shaman`. The website remembers the
server, so after the first time the plain address works. To skip the `?server=` part for good, put
the name in `WEB_SERVER` in `campfire_config.py`.

- Same server, same accounts: log in with your Campfire username and password (a new name makes a
  new account, like in the app). Tick *Keep me logged in* to stay logged in.
- Channels, DMs, group DMs, friends, profiles (with GIF avatars), emoji, GIPHY GIFs, file uploads
  up to 5 GB (button, drag and drop, or paste), pictures and videos in chat, deleting messages,
  themes, notifications, and the admin `+` and slash commands (`/kick`, `/ban`, `/mute name 10`...).
- **Voice chat**: click a voice channel, or **Call** in a group DM. The browser asks for the
  microphone the first time. Mute, deafen and leave are at the bottom left; a green ring shows
  who's talking. Website and app users hear each other. Works in Chrome, Edge, Firefox and new
  Safari.
- **Camera and screen sharing**: the 📷 and 🖥 buttons in the voice bar (the browser asks which
  camera, or which screen/window/tab to share). Everyone's video shows above the chat: cameras
  play by themselves, screens have a *Watch* button (they use more data). ⛶ makes a tile full
  screen. Website and app users see each other. Best in Chrome or Edge; phones can use the camera
  but usually can't share their screen.
- On a phone the channel list and people list slide in from the sides (the buttons at the top).
  "Add to Home Screen" makes it open like an app.
- The server also hands out the website itself at `http://<server address>:55555` (on the server
  PC: `http://127.0.0.1:55555`): everything but voice there, since that address isn't https.

## Running the server

```
python server.py                       # opens the server window, listens on port 55555
                                       # (same as double-clicking Campfire Server.pyw)
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
- **Sign-ups**: who can make an account (anyone / an admin approves / invite code only), people
  waiting to be approved (Approve or Turn down), and **Make a code** for invite codes (copied for you)
- **Bans**: banned accounts and IP addresses; unban, or ban an IP
- **Log**: everything the server does, live, plus a box to send announcements

Closing the window stops the server (it asks first if people are online). The console mode
needs only Python; the window needs PyQt6.

For friends to connect from outside your network, forward TCP port 55555 on your
router to the machine running the server and allow it through the firewall.

### Updating the server

The Campfire app updates itself, but the server is separate. When there's a server update, a bar
at the top of the server window says so: click **Update server**. Everyone online is disconnected
for a few seconds and reconnects by themselves; accounts, messages and files are kept.

- Installed with Campfire-Server-Setup.exe: it downloads the newest server setup, which closes the
  server, installs over it and starts it again.
- From the zip (Campfire Server.exe): it downloads the newest Campfire-Windows.zip, swaps the
  program files and starts again. Close Campfire (the chat app) first if you run it from the
  same folder. Details go to `campfire_update.log`.
- Running from a git clone: it runs `git pull` and restarts. If you pull by hand while the
  server is running, the bar offers **Restart now**.
- **Settings > Updates** has **Check for updates** and the start-up check switch.

If the app says the server needs updating (or shows "Unknown command"), this is the fix.

## New accounts: approval and invite codes

By default a new name typed on the login screen makes an account that **waits for an admin to
approve it** (the person sees "waiting for approval"). Admins, and people with a role that allows
it, get a message and approve them in **Admin > Sign-ups and invites...** (the website: ⚙ >
Sign-ups and invites, or the server window's Sign-ups tab).

- **Invite codes** (like `ABCD-EFGH`) let someone in straight away: they type it in the **Invite
  code** box under the password. Pick how many uses and how many days it lasts.
- The mode can be **Anyone can make an account**, **An admin approves new accounts** or **Only
  people with an invite code**. Existing accounts are never affected.
- Start the server with `--signup open` (or `approval` / `invite`) to set it from the command line.

## Roles and private channels

Admins make roles in **Admin > Roles...** (website: ⚙ > Roles): a name, a colour and what the
role allows - create/delete channels, delete other people's messages, kick, mute, ban, approve new
accounts, see every private channel. Give someone roles by right-clicking them > **Roles**.

- People show under their top role in the Online list, and a role's colour colours their name
  (unless they picked their own name colour).
- Right-click a channel > **Who can see it...** to make it private to some roles (🔒). Admins
  always see every channel. When creating a channel, admins can make it private straight away.

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
```

Change those lines if your server's address changes. There's no way to pick a different
server from the app.

### On the PC that runs the server

Use the normal Campfire there too. When it starts it first looks for a Campfire server on the
same PC (for under a second) and uses it if one answers; otherwise it connects to
`SERVER_HOST`. The window title then says *server on this PC*. This matters because connecting
to your own public IP from inside your house usually fails (most routers don't loop that traffic
back in). Only a program that answers like a Campfire server is used, and it's checked again
every time Campfire reconnects.

(There used to be a separate "Campfire Admin" app for this. It's no longer needed: installing
Campfire removes it, and old Campfire Admin apps update themselves into Campfire.)

### Friends can't connect?

Open the server window's **Internet** tab and press **Test now** (with the server running). An
outside service tries to connect to you, exactly like a friend's Campfire does, and it tells you:

- whether port 55555 can be reached from the internet,
- your real internet address, and whether it's still the one Campfire connects to
  (`SERVER_HOST` in `campfire_config.py`; home addresses change now and then; if yours has,
  change it there and push, and everyone gets the update),
- whether Windows Firewall is blocking Campfire Server.

Fixes, one click each: **Open the port on my router** (asks the router to forward the port, if it
supports UPnP) and **Allow through Windows Firewall** (asks for admin). If the router won't do it
automatically, the tab shows how to forward the port by hand. Then press **Test now** again.

### Can't log in?

- **"Server is not running at that address" / "timed out"**: start `python server.py` and
  leave its window open (on the server PC, Campfire finds it by itself). For friends, port
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
| `/join #room` | Open a channel (admins: creates it if needed) |
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
- Admins: `+` creates a voice channel. Up to 25 people per channel.
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

## Customizing

**Campfire > Settings** (it scrolls):

- **Appearance** (just for you): theme (Dark, Midnight, Campfire, Light), accent colour,
  text size, and a short fade when switching chats (turn it off if you don't like it).
- **Profile** (everyone sees it): your avatar, name colour, a status line under your name
  (e.g. "playing Minecraft", up to 80 characters) and **About me** (up to 190 characters).

### Profile style

**Settings > Profile style** (in the app and on the website), seen by everyone:

- **Banner**: a picture or an animated GIF at the top of your profile card (big GIFs are cropped
  and shrunk to fit, 1.4 MB at most), or just a colour.
- **Card colours**: two colours for your card's background, top to bottom.
- **Avatar decoration**: a frame around your picture everywhere it shows (chat, lists, your card):
  campfire flames, gold ring, neon, rainbow, crown, halo, cat ears, hearts, frost, leaf wreath.
  They animate on the website.
- **Profile effect**: animated particles over your card: snow, sparkles, floating hearts, campfire
  embers, confetti, cherry blossoms, bubbles or a starry night.
- **Preview my profile** shows your card before you save.

### Playing a game

While you're in a game, everyone sees **🎮 Playing ...** under your name (and on your profile card,
with how long you've been playing), in the app and on the website. The app (on Windows) notices:
popular games, anything installed by Steam, Epic Games or the Xbox app, and Minecraft (including
Lunar, Badlion, Feather and other clients). Missing one? **Settings > Playing a game > Add a game**
and pick its .exe. Turn it off there too. Only the game's name is shared.

### Profiles and avatars

- Click anyone in the Online, Friends or group member list, or their name or picture in chat
  (or right-click them > **View profile**), for their
  profile card: avatar, status, About me, when they joined, online or in which voice channel,
  and **Message** / **Add friend**. Your own card has **Edit profile**.
- Avatars can be **animated GIFs**: Settings > Avatar > *Choose image or GIF*. Big GIFs are
  cropped square and shrunk to fit (1 MB at most), keeping every frame. PNG/JPG pictures work too.
- Pictures show next to messages, like Discord: messages from the same person within a few
  minutes are grouped under one picture and name. GIF avatars play in chat, in the Online list
  and on profile cards (turn GIF animation off in Settings if you prefer still pictures).
- Each picture is downloaded once and cached, so logging in stays fast.

DMs you've talked in come back every time you open Campfire. Right-click a DM > Close to hide
it; it comes back by itself when that person messages you again.

Only admins (and roles allowed to) can create channels: the `+` next to **Channels** and **Voice
channels** only shows for them (or use the server window's Channels tab).

## Replies, reactions, editing and @mentions

- **Reply**: right-click a message > **Reply** (website: hover it > ↩). The reply shows the
  message it answers; click that to jump to it. Esc cancels.
- **Reactions**: right-click > **Add reaction** for quick emoji or **More...** for the whole
  picker (website: hover > 😀). Click a reaction under a message to add or take back yours.
- **Edit**: right-click your message > **Edit message**, or press **Up** in an empty message box
  to edit your last one. Edited messages say "(edited)". Saving it empty deletes it.
- **@mentions**: type `@` and a list of names pops up (Tab or Enter picks one). Mentions of you,
  `@everyone`, `@here` and replies to your messages are highlighted and always notify you.
- **Typing**: "bob is typing..." shows under the message box.
- **Mute a chat**: right-click a channel, DM or group > **Mute** (🔕). Muted chats don't get
  unread counts or alerts unless someone mentions you. This is per computer / browser.

## Your account

**Settings > Account** has **Change password** and **Delete account** (asks for your password).
Deleting removes your profile, friends and roles and logs you out; messages you sent stay, shown
as a deleted user.

## Deleting messages

Right-click a message (or click its time) for **Delete message** and **Copy text**. It asks first,
then the message disappears for everyone, along with any file attached to it (the file is removed
from the server too).

- Anyone can delete their own messages, anywhere.
- Admins (and roles allowed to) can delete anyone's message in channels (not in DMs or group DMs).
- A group DM's owner can delete anyone's message in that group.

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
  -subj "/CN=campfire" -addext "subjectAltName=IP:YOUR.PUBLIC.IP.HERE"
python server.py --certfile cert.pem --keyfile key.pem
```

Put `cert.pem` (never `key.pem`) next to `campfire_client.py`, set `USE_TLS = True` at the
top of that file, and give that copy of the client to your friends.

## Tests

```
python tests/test_server.py
python tests/test_voice.py
python campfire_client.py --selftest report.txt                   # checks voice/video/emoji/icons load
QT_QPA_PLATFORM=offscreen python tests/test_gui.py
QT_QPA_PLATFORM=offscreen python tests/test_voice_gui.py
QT_QPA_PLATFORM=offscreen python tests/test_video.py
QT_QPA_PLATFORM=offscreen python tests/test_video_gui.py
QT_QPA_PLATFORM=offscreen python tests/test_call_gui.py
QT_QPA_PLATFORM=offscreen python tests/test_server_gui.py
QT_QPA_PLATFORM=offscreen python tests/test_files_gui.py
QT_QPA_PLATFORM=offscreen python tests/test_emoji_gif_gui.py      # needs pillow to make test GIFs
QT_QPA_PLATFORM=offscreen python tests/test_giphy_gui.py          # uses a fake GIPHY, no key needed
QT_QPA_PLATFORM=offscreen python tests/test_updater.py            # uses a fake GitHub release
QT_QPA_PLATFORM=offscreen python tests/test_delete_gui.py
QT_QPA_PLATFORM=offscreen python tests/test_custom_gui.py
QT_QPA_PLATFORM=offscreen python tests/test_server_update.py     # fake release + local git repos
QT_QPA_PLATFORM=offscreen python tests/test_profiles_gui.py       # needs Pillow
python tests/test_web.py                  # the website: HTTP/WebSocket, then the page in Chrome (Playwright), voice
python tests/test_style_gui.py            # banners, card colours, decorations, effects
python tests/test_games.py                # game detection + 'Playing ...' between apps
python tests/test_https.py                # DuckDNS + certificate (stand-in Let's Encrypt), then the GitHub site over https
QT_QPA_PLATFORM=offscreen python tests/test_netcheck.py           # fake router and reachability service
QT_QPA_PLATFORM=offscreen python tests/test_login.py
python tests/test_features_server.py      # sign-ups, replies, reactions, editing, typing, roles, private channels
QT_QPA_PLATFORM=offscreen python tests/test_features_gui.py
```

## Building the Windows app yourself

```
pip install -r requirements.txt pyinstaller
pyinstaller --noconfirm campfire.spec        # -> dist/Campfire/ with Campfire.exe and Campfire Server.exe
```

Run that on a Windows PC to get Windows programs (PyInstaller builds for the system it runs on).
For the installer, install [Inno Setup 6](https://jrsoftware.org/isinfo.php) and then run
`"C:\Program Files (x86)\Inno Setup 6\ISCC.exe" installer\campfire.iss` -> `dist\Campfire-Setup.exe`.

Each GitHub build is numbered (version 1.0.<build>): it writes `_version.py` before PyInstaller
and publishes `version.json` with the release, which is what installed copies check for updates.
The old `admin-build` pre-release now holds the same installer, so old Campfire Admin apps update
into Campfire; the installer removes Campfire Admin.

`.github/workflows/build-windows.yml` does all of this on GitHub's Windows machines for every push
to `main` (or by hand: Actions tab > Build Windows app > Run workflow). It checks each build:
the self-test, that the server answers, and that the installer installs, runs, creates its
shortcuts and uninstalls cleanly. Then it publishes the zip and the setup file.
`dist/Campfire` is the folder to zip and share. Check a build with
`"dist/Campfire/Campfire.exe" --selftest report.txt` (exit code 0 and every line "ok").

## Protocol

Newline-delimited JSON in both directions; every object has a `"t"` type.

Client → server: `login {user, pass}`, `msg {room, text}`, `history {room}`,
`create_room {room}`, `dm {to, text}`, `dm_history {with}`, `group_create {name, members}`, `group_add {gid, users}`,
`group_leave {gid}`, `group_kick {gid, user}`, `group_rename {gid, name}`, `group_msg {gid, text}`,
`group_history {gid}`, `profile {color, status, bio}`, `get_profile {user}` → `profile_info`, `get_avatar {user}` → `avatar_data {user, av, data}`, `voice_create {name}` (create_room/voice_create: admins only), `voice_join {channel, muted, deaf}` (channel `g:<gid>` for a group call), `voice_leave`,
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
`admin_changed {admin}`, `file_deleted {fid}`, `config {giphy_key}`, `msg_deleted {id}`. Messages carry an `id`; delete one with
`delete_msg {id}`. Messages can carry `file {id, name, size}`.

File transfers use a second connection that logs in with `login {user, pass, transfer: true}`,
then `upload {id, conv, name, size, text}` → `upload_ready {offset}` → raw bytes →
`upload_done {fid}`, or `download {fid, offset}` → `download_ready {name, size, offset}` → raw
bytes. Errors come back as `transfer_error {msg}`. `kicked {reason}`, `muted {until, reason}`, `announce {from, text, ts}`,
`cleared {room}`, `notice {msg}`, `error {msg}`, `pong`. User entries and messages carry an `admin` flag.
