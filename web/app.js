/* Campfire in the browser. Talks to the server over a WebSocket with the same JSON messages as
   the desktop app (see server.py). No libraries, no build step. */
"use strict";

const $ = (id) => document.getElementById(id);
const ADMIN_BADGE = "★";
const GROUP_MAX = 50, MAX_AVATAR = 1024 * 1024, MAX_BIO = 190, MAX_FILE = 5 * 1024 ** 3;
const URL_RE = /(https?:\/\/[^\s<]+)/g;
const IMAGE_EXT = /\.(png|jpe?g|gif|webp|bmp)$/i, VIDEO_EXT = /\.(mp4|webm|mov|m4v)$/i, AUDIO_EXT = /\.(mp3|wav|ogg|m4a|flac)$/i;
const THEMES = ["Dark", "Midnight", "Campfire", "Light"];
const LIGHT = { Light: true };

// Which server: config.js says (the GitHub website points at your DuckDNS name; the page the
// server hands out itself uses its own address), "?server=..." overrides, or the login box.
const CONFIG = window.CAMPFIRE || { server: null };
const DEFAULT_PORT = 55555;
function cleanServer(text) {
  let t = String(text || "").trim().replace(/^(https?|wss?):\/\//i, "").replace(/\/.*$/, "");
  if (!t) return "";
  if (!t.includes(".") && !t.includes(":")) t += ".duckdns.org";      // just the DuckDNS name
  if (!/:\d+$/.test(t)) t += ":" + DEFAULT_PORT;
  return t.toLowerCase();
}
const SERVER = {
  host: "",                         // "" = the server this page came from
  init() {
    const q = new URLSearchParams(location.search).get("server");
    if (q) { store.set("server", cleanServer(q)); history.replaceState(null, "", location.pathname); }
    if (CONFIG.server === "") this.host = "";
    else this.host = store.get("server", "") || cleanServer(CONFIG.server || "");
  },
  pick: CONFIG.server !== "",       // on GitHub: people can choose a server
  api(path) { return this.host ? `https://${this.host}${path}` : path; },
  ws() { return this.host ? `wss://${this.host}/ws` : `${location.protocol === "https:" ? "wss:" : "ws:"}//${location.host}/ws`; },
};

const S = {                         // everything we know
  ws: null, me: "", admin: false, token: "", features: [], rooms: [], users: {}, colors: {}, avatars: {},
  friends: [], requests: [], groups: [], voice: [], calls: {}, convs: {}, loaded: new Set(), current: "",
  unread: {}, dms: [], profiles: {}, giphy: "", creds: null, retry: 0, kicked: false, connected: false,
};

// ------------------------------------------------------------------ small helpers
function el(tag, attrs, ...kids) {
  const e = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs || {})) {
    if (v === undefined || v === null || v === false) continue;
    if (k === "class") e.className = v;
    else if (k === "text") e.textContent = v;
    else if (k.startsWith("on")) e.addEventListener(k.slice(2), v);
    else if (k === "style" && typeof v === "object") Object.assign(e.style, v);
    else e.setAttribute(k, v === true ? "" : v);
  }
  for (const kid of kids.flat()) if (kid !== null && kid !== undefined && kid !== false)
    e.append(kid instanceof Node ? kid : document.createTextNode(String(kid)));
  return e;
}
const store = {
  get(k, d) { try { const v = localStorage.getItem("campfire." + k); return v === null ? d : JSON.parse(v); } catch { return d; } },
  set(k, v) { try { localStorage.setItem("campfire." + k, JSON.stringify(v)); } catch { } },
  del(k) { try { localStorage.removeItem("campfire." + k); } catch { } },
};
function send(obj) {
  if (!S.ws || S.ws.readyState !== 1) { toast("Not connected - try again in a moment", true); return false; }
  S.ws.send(JSON.stringify(obj));
  return true;
}
function toast(text, err) {
  const t = el("div", { class: "toast" + (err ? " err" : ""), text });
  $("toasts").append(t);
  setTimeout(() => t.remove(), err ? 6000 : 4000);
}
function human(n) {
  for (const u of ["bytes", "KB", "MB", "GB"]) { if (n < 1024 || u === "GB") return (u === "bytes" ? n : n.toFixed(1)) + " " + u; n /= 1024; }
}
function clock(ts) { return new Date(ts * 1000).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" }); }
function when(ts) {
  const d = new Date(ts * 1000), now = new Date();
  const day = d.toDateString() === now.toDateString() ? "Today" :
    d.toDateString() === new Date(now - 864e5).toDateString() ? "Yesterday" : d.toLocaleDateString();
  return `${day} at ${clock(ts)}`;
}
function ci(a, b) { return a.toLowerCase() === b.toLowerCase(); }

// same colours as the desktop app: md5(name) picks from the palette
function md5hex(str) {
  const k = [], r = [7, 12, 17, 22, 5, 9, 14, 20, 4, 11, 16, 23, 6, 10, 15, 21];
  for (let i = 0; i < 64; i++) k[i] = Math.floor(Math.abs(Math.sin(i + 1)) * 2 ** 32) | 0;
  const bytes = new TextEncoder().encode(str), n = bytes.length, words = new Array(((n + 8) >> 6) * 16 + 16).fill(0);
  for (let i = 0; i < n; i++) words[i >> 2] |= bytes[i] << ((i % 4) * 8);
  words[n >> 2] |= 0x80 << ((n % 4) * 8);
  const total = (((n + 8) >> 6) + 1) * 16;
  words[total - 2] = n * 8;
  let [a0, b0, c0, d0] = [0x67452301, 0xefcdab89 | 0, 0x98badcfe | 0, 0x10325476];
  for (let o = 0; o < total; o += 16) {
    let [a, b, c, d] = [a0, b0, c0, d0];
    for (let i = 0; i < 64; i++) {
      let f, g;
      if (i < 16) { f = (b & c) | (~b & d); g = i; }
      else if (i < 32) { f = (d & b) | (~d & c); g = (5 * i + 1) % 16; }
      else if (i < 48) { f = b ^ c ^ d; g = (3 * i + 5) % 16; }
      else { f = c ^ (b | ~d); g = (7 * i) % 16; }
      const tmp = d; d = c; c = b;
      const x = (a + f + k[i] + words[o + g]) | 0, s = r[(i >> 4) * 4 + (i % 4)];
      b = (b + ((x << s) | (x >>> (32 - s)))) | 0; a = tmp;
    }
    a0 = (a0 + a) | 0; b0 = (b0 + b) | 0; c0 = (c0 + c) | 0; d0 = (d0 + d) | 0;
  }
  return [a0, b0, c0, d0].map((v) => [0, 8, 16, 24].map((s) => ((v >>> s) & 255).toString(16).padStart(2, "0")).join("")).join("");
}
const PALETTE = ["#ff8a65", "#ffd54f", "#81c784", "#4fc3f7", "#ba68c8", "#f06292", "#4db6ac", "#aed581"];
const _colorCache = {};
function nameColor(name) {
  const key = name.toLowerCase();
  if (!(key in _colorCache)) _colorCache[key] = PALETTE[Number(BigInt("0x" + md5hex(key)) % BigInt(PALETTE.length))];
  return _colorCache[key];
}
function userColor(name) {
  let c = S.colors[name] || nameColor(name);
  if (LIGHT[look().theme] && !S.colors[name]) c = shade(c, 0.6);
  return c;
}
function shade(hex, f) {
  const n = parseInt(hex.slice(1), 16);
  const ch = (v) => Math.max(0, Math.min(255, Math.round(v * f))).toString(16).padStart(2, "0");
  return "#" + ch(n >> 16) + ch((n >> 8) & 255) + ch(n & 255);
}
function avatarUrl(name) { const av = S.avatars[name]; return av ? SERVER.api(`/av/${av}`) : "avatar.png"; }
function avatar(name, big) {
  return el("img", { class: "av" + (big ? " big" : ""), src: avatarUrl(name), alt: "", loading: "lazy", draggable: "false" });
}
function fileUrl(f, download) {
  return SERVER.api(`/file/${f.id}/${encodeURIComponent(f.name)}?t=${encodeURIComponent(S.token)}${download ? "&dl=1" : ""}`);
}

// ------------------------------------------------------------------ look (theme, size)
function look() { return Object.assign({ theme: "Dark", accent: "", size: "Normal", animations: true, notify: true }, store.get("look", {})); }
function applyLook() {
  const l = look();
  document.documentElement.dataset.theme = l.theme;
  document.documentElement.style.setProperty("--accent", l.accent || (l.theme === "Campfire" ? "#e8742c" : "#5865f2"));
  document.body.classList.remove("size-Small", "size-Large", "size-Extra");
  if (l.size !== "Normal") document.body.classList.add("size-" + l.size);
  document.querySelector('meta[name="theme-color"]').content = getComputedStyle(document.documentElement).getPropertyValue("--bg").trim();
}

// ------------------------------------------------------------------ connection
function connect() {
  let ws;
  try { ws = new WebSocket(SERVER.ws()); }
  catch (e) { loginError("That server address doesn't work: " + e.message); return; }
  S.ws = ws;
  ws.onmessage = (ev) => { let m; try { m = JSON.parse(ev.data); } catch { return; } handle(m); };
  ws.onclose = () => {
    if (S.ws !== ws) return;
    const was = S.connected;
    S.connected = false;
    if (S.kicked || !S.creds) return;
    if (!was && !S.everConnected) {           // never got in: say why instead of retrying forever
      loginError(SERVER.host ? `Couldn't reach ${SERVER.host}. Is the server running, with https set up?` : "Couldn't reach the server.");
      S.creds = null;
      return;
    }
    if (was) setStatus("Reconnecting...");
    const wait = Math.min(15000, 1000 * 2 ** S.retry++);
    setTimeout(() => { if (S.creds && !S.kicked) connect(); }, wait);
  };
}
function loginError(text) {
  $("loginerr").textContent = text;
  $("loginbtn").disabled = false; $("loginbtn").textContent = "Log in";
  showLogin();
}
function setStatus(text) { $("status").textContent = text; $("status").classList.toggle("hidden", !text); }

function login(user, pass, remember) {
  S.creds = { user, pass, remember };
  S.kicked = false;
  $("loginerr").textContent = "";
  $("loginbtn").disabled = true;
  $("loginbtn").textContent = "Connecting...";
  if (S.ws && S.ws.readyState === 1) send({ t: "login", user, pass });
  else connect();
}

const H = {};                      // message type -> handler
function handle(m) { const f = H[m.t]; if (f) f(m); }

H.hello = (m) => { S.features = m.features || []; if (S.creds) S.ws.send(JSON.stringify({ t: "login", user: S.creds.user, pass: S.creds.pass })); };
H.login_fail = (m) => {
  $("loginerr").textContent = m.reason;
  $("loginbtn").disabled = false; $("loginbtn").textContent = "Log in";
  S.creds = null; store.del("creds");
  showLogin();
};
H.login_ok = (m) => {
  S.me = m.user; S.admin = !!m.admin; S.connected = true; S.retry = 0; S.everConnected = true;
  S.features = m.features || S.features;
  S.rooms = m.rooms; S.colors = m.colors || {}; S.avatars = m.avatars || {};
  S.users = {}; for (const u of m.users) S.users[u.name] = u;
  if (S.creds.remember) store.set("creds", { user: S.creds.user, pass: S.creds.pass }); else store.del("creds");
  S.creds.user = m.user;
  const closed = closedDms();
  for (const name of m.dms || []) if (!closed.has(name) && name !== S.me && !S.dms.includes(name)) S.dms.push(name);
  S.loaded.clear();
  setStatus("");
  showApp();
  const last = store.get("last." + S.me, "");
  const ok = last && (S.rooms.includes(last) || last.startsWith("@") || last.startsWith("g:"));
  openConv(S.current && (S.rooms.includes(S.current) || !S.current.startsWith("#")) ? S.current : ok ? last : S.rooms[0], true);
  send({ t: "get_profile", user: S.me });
  if (V.chan) {                                                    // back in after a reconnect
    send({ t: "voice_join", channel: V.chan, muted: V.muted, deaf: V.deaf });
    for (const kind of Object.keys(VID.out)) { send({ t: "video_start", kind }); VID.out[kind].wantKey = true; }
    for (const w of VID.watching.values()) { newDecoder(w); send({ t: "video_watch", user: w.user, kind: w.kind }); }
  }
  if (m.registered) toast(`Welcome to Campfire, ${S.me}! Your account is ready.`);
};
H.web_token = (m) => { S.token = m.token; if (S.current) renderMessages(true); };
H.kicked = (m) => {
  S.kicked = true; S.creds = null; store.del("creds");
  if (S.ws) S.ws.close();
  showLogin(); $("loginerr").textContent = m.reason || "Disconnected";
};
H.config = (m) => { S.giphy = m.giphy_key || ""; S.webUrl = m.web_url || ""; $("gifbtn").classList.toggle("hidden", !S.giphy); };
H.admin_changed = (m) => { S.admin = !!m.admin; toast(S.admin ? "You're an admin now" : "You're no longer an admin"); renderSide(); renderMe(); };
H.rooms = (m) => {
  S.rooms = m.rooms;
  if (S.pendingRoom && S.rooms.includes(S.pendingRoom)) { openConv(S.pendingRoom); S.pendingRoom = null; }
  else if (S.current.startsWith("#") && !S.rooms.includes(S.current)) openConv(S.rooms[0]);
  renderSide();
};
H.friends = (m) => { S.friends = m.friends; S.requests = m.requests; renderPeople(); };
H.groups = (m) => {
  S.groups = m.groups;
  if (S.current.startsWith("g:") && !group(S.current)) openConv(S.rooms[0]);
  renderSide(); renderHeader();
};
H.group_created = (m) => openConv("g:" + m.gid);
H.voice_state = (m) => { S.voice = m.channels; renderSide(); renderStage(); };
H.call_state = (m) => { S.calls[m.gid] = m.users; renderSide(); if (S.current === "g:" + m.gid) renderHeader(); if (V.chan === "g:" + m.gid) renderStage(); };
H.call_ring = (m) => { toast(`📞 ${m.from} started a call in ${m.name}`); notify(`${m.from} is calling`, m.name, "g:" + m.gid); };
H.user_join = (m) => { S.users[m.name] = m; noteAvatar(m); renderPeople(); };
H.user_leave = (m) => { delete S.users[m.name]; renderPeople(); };
H.user_update = (m) => {
  const old = m.old || m.name;
  if (old in S.users) { delete S.users[old]; S.users[m.name] = m; }
  if (m.color) S.colors[m.name] = m.color; else delete S.colors[m.name];
  if (old !== m.name) { delete S.colors[old]; delete S.avatars[old]; }
  noteAvatar(m);
  delete S.profiles[old]; delete S.profiles[m.name];
  if (m.name === S.me) send({ t: "get_profile", user: S.me });
  renderPeople(); renderMessages(false, true); renderMe();
};
H.renamed = (m) => {
  const old = S.me; S.me = m.name;
  if (S.creds) S.creds.user = m.name;
  const saved = store.get("creds"); if (saved) store.set("creds", { ...saved, user: m.name });
  toast(`You're now ${m.name}`);
  if (old !== m.name) renderMe();
};
H.profile_info = (m) => { S.profiles[m.name] = m; if (S.card && S.card.name.toLowerCase() === m.name.toLowerCase()) fillCard(m); };
H.history = (m) => setHistory(m.room, m.messages);
H.dm_history = (m) => setHistory("@" + m.with, m.messages);
H.group_history = (m) => setHistory("g:" + m.gid, m.messages);
H.msg = (m) => arrive(m.room, m);
H.dm = (m) => {
  const other = ci(m.from, S.me) ? m.to : m.from;
  const key = "@" + other;
  if (!S.dms.includes(other)) S.dms.unshift(other);
  const closed = closedDms(); if (closed.delete(other)) saveClosed(closed);
  arrive(key, { ...m, user: m.from });
};
H.group_msg = (m) => arrive("g:" + m.gid, m);
H.group_notice = (m) => arrive("g:" + m.gid, { kind: "event", text: m.text, ts: m.ts }, true);
H.msg_deleted = (m) => {
  for (const [key, msgs] of Object.entries(S.convs)) {
    const i = msgs.findIndex((x) => x.id === m.id);
    if (i >= 0) { msgs.splice(i, 1); if (key === S.current) renderMessages(false, true); }
  }
};
H.file_deleted = (m) => {
  for (const msgs of Object.values(S.convs)) for (const x of msgs) if (x.file && x.file.id === m.fid) x.file = { id: m.fid, deleted: true };
  renderMessages(false, true);
};
H.cleared = (m) => { S.convs[m.room] = [{ kind: "event", text: `${m.by} cleared this channel`, ts: Date.now() / 1000 }]; if (S.current === m.room) renderMessages(); };
H.announce = (m) => { for (const key of Object.keys(S.convs)) if (key.startsWith("#")) S.convs[key].push({ kind: "announce", ...m }); renderMessages(false); notify(`Announcement from ${m.from}`, m.text); };
H.notice = (m) => toast(m.msg);
H.error = (m) => toast(m.msg, true);
H.muted = (m) => toast(`You've been muted${m.reason ? ": " + m.reason : ""}`, true);

function noteAvatar(u) { if ("av" in u) { if (u.av) S.avatars[u.name] = u.av; else delete S.avatars[u.name]; } }

// ------------------------------------------------------------------ conversations
function group(key) { const gid = Number(String(key).slice(2)); return S.groups.find((g) => g.id === gid); }
function convName(key) {
  if (key.startsWith("#")) return key;
  if (key.startsWith("@")) return key.slice(1);
  const g = group(key); return g ? g.name : "Group";
}
function closedKey() { return `closed.${location.host}.${S.me}`; }
function closedDms() { return new Set(store.get(closedKey(), [])); }
function saveClosed(set) { store.set(closedKey(), [...set]); }

function openConv(key, force) {
  if (!key) return;
  if (key === S.current && !force) { closeDrawers(); return; }
  S.current = key;
  store.set("last." + S.me, key);
  delete S.unread[key];
  if (key.startsWith("@")) {
    const name = key.slice(1);
    if (!S.dms.includes(name)) S.dms.unshift(name);
    const closed = closedDms(); if (closed.delete(name)) saveClosed(closed);
  }
  if (!S.loaded.has(key)) {
    S.loaded.add(key);
    if (key.startsWith("#")) send({ t: "history", room: key });
    else if (key.startsWith("@")) send({ t: "dm_history", with: key.slice(1) });
    else send({ t: "group_history", gid: Number(key.slice(2)) });
  }
  closeDrawers();
  renderSide(); renderHeader(); renderMessages(true);
  updateTitle();
  if (window.matchMedia("(min-width: 821px)").matches) $("input").focus();
}
function openDm(name) { if (name && !ci(name, S.me)) openConv("@" + name); }

function setHistory(key, messages) {
  const live = (S.convs[key] || []).filter((m) => !m.id || !messages.some((x) => x.id === m.id));
  S.convs[key] = [...messages, ...live.filter((m) => m.kind)];
  if (key === S.current) renderMessages(true);
}
function arrive(key, m, quiet) {
  const list = S.convs[key] || (S.convs[key] = []);
  if (m.id && list.some((x) => x.id === m.id)) return;
  list.push(m);
  if (list.length > 500) list.splice(0, list.length - 500);
  const mine = m.user && ci(m.user, S.me);
  if (key === S.current) {
    appendMessage(m);
    if (document.hidden && !mine && !quiet) { S.unread[key] = (S.unread[key] || 0) + 1; updateTitle(); }
  } else if (!mine && !quiet) {
    S.unread[key] = (S.unread[key] || 0) + 1;
    renderSide(); updateTitle();
  }
  if (!mine && !quiet && m.user) {
    const mention = m.text && new RegExp(`@${S.me}\\b`, "i").test(m.text);
    if (!key.startsWith("#") || mention)
      if (document.hidden || key !== S.current) notify(`${m.user} ${key.startsWith("#") ? "in " + key : key.startsWith("g:") ? "in " + convName(key) : ""}`, m.text || (m.file ? m.file.name : ""), key);
  }
}
function updateTitle() {
  const n = Object.values(S.unread).reduce((a, b) => a + b, 0);
  document.title = (n ? `(${n}) ` : "") + (S.current ? convName(S.current) + " - " : "") + "Campfire";
}
function notify(title, body, key) {
  if (!look().notify || !("Notification" in window) || Notification.permission !== "granted") return;
  try {
    const n = new Notification(title, { body: (body || "").slice(0, 200), icon: "icon.png", tag: key || "campfire" });
    n.onclick = () => { window.focus(); if (key) openConv(key); n.close(); };
  } catch { }
}
document.addEventListener("visibilitychange", () => {
  if (!document.hidden && S.current && S.unread[S.current]) { delete S.unread[S.current]; updateTitle(); renderSide(); }
});

// ------------------------------------------------------------------ rendering: sidebar
function item(label, key, opts = {}) {
  const n = S.unread[key];
  return el("div", {
    class: "item" + (key === S.current ? " on" : "") + (n ? " unread" : ""), title: opts.title || label,
    onclick: () => openConv(key), oncontextmenu: opts.menu,
  }, opts.icon || null, el("span", { class: "name", text: label }), n ? el("span", { class: "badge", text: n > 99 ? "99+" : n }) : null,
    opts.close ? el("span", { class: "x", title: "Close", text: "✕", onclick: (e) => { e.stopPropagation(); opts.close(); } }) : null);
}
function sectionHead(title, button) {
  return el("div", { class: "section" }, el("span", { text: title }), button || null);
}
function renderSide() {
  const side = $("side");
  const top = side.scrollTop;
  side.replaceChildren();
  side.append(el("div", { class: "item", style: { color: "var(--text)", fontWeight: 700, cursor: "default" } },
    el("img", { src: "icon.png", class: "av" }), "Campfire"));
  side.append(sectionHead("Channels", S.admin ? el("button", { class: "icon", title: "Create a channel (admins)", text: "+", onclick: createRoom }) : null));
  for (const r of S.rooms) side.append(item(r.slice(1), r, { icon: el("span", { class: "muted", text: "#" }), title: r }));
  if (S.voice.length) {
    side.append(sectionHead("Voice channels"));
    for (const v of S.voice) {
      const here = V.chan === v.name;
      side.append(el("div", { class: "item" + (here ? " on" : ""), title: here ? "You're here (click to leave)" : "Join voice", onclick: () => here ? leaveVoice() : joinVoice(v.name) },
        el("span", { text: "🔊" }), el("span", { class: "name", text: v.name })));
      for (const u of v.users) side.append(el("div", { class: "voiceuser", "data-voice-user": u.name }, avatar(u.name), u.name,
        u.muted ? " 🔇" : "", u.deaf ? " 🔕" : "", u.camera ? " 📷" : "", u.screen ? " 🖥" : ""));
    }
  }
  side.append(sectionHead("Group DMs", el("button", { class: "icon", title: "New group DM", text: "+", onclick: () => groupDialog() })));
  for (const g of S.groups) {
    const key = "g:" + g.id, call = (S.calls[g.id] || []).length;
    side.append(item(g.name, key, { icon: el("span", { text: call ? "🔊" : "👥" }), title: `${g.name} (${g.members.length} people)` }));
  }
  side.append(sectionHead("Direct messages"));
  for (const name of S.dms) {
    side.append(item(name, "@" + name, {
      icon: avatar(name), close: () => {
        const closed = closedDms(); closed.add(name); saveClosed(closed);
        S.dms = S.dms.filter((n) => n !== name);
        if (S.current === "@" + name) openConv(S.rooms[0]); else renderSide();
      },
    }));
  }
  side.append(meBox());
  renderVoiceBar();
  side.scrollTop = top;
}
function meBox() {
  const me = S.users[S.me] || {};
  return el("div", { id: "me" },
    el("img", { class: "av", src: avatarUrl(S.me), style: { cursor: "pointer" }, onclick: (e) => showCard(S.me, e) }),
    el("div", { class: "who" },
      el("div", { class: "nm", style: { color: userColor(S.me), fontWeight: 600, overflow: "hidden", textOverflow: "ellipsis" } }, S.me, S.admin ? el("span", { class: "star", text: " " + ADMIN_BADGE }) : null),
      el("div", { class: "muted", style: { fontSize: ".78em", overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }, text: playing(me) || me.status || (S.connected ? "Online" : "Offline") })),
    el("button", { class: "icon", title: "Settings", text: "⚙", onclick: settingsDialog }));
}
function renderMe() { const old = $("me"); if (old) old.replaceWith(meBox()); }

// ------------------------------------------------------------------ rendering: people
function playing(u) { return u && u.activity ? `🎮 Playing ${u.activity}` : ""; }
function playedFor(since) {
  const m = since ? Math.max(0, Math.floor((Date.now() / 1000 - since) / 60)) : 0;
  return m < 1 ? "just started" : m < 60 ? `for ${m} min` : `for ${Math.floor(m / 60)} h ${String(m % 60).padStart(2, "0")} min`;
}
function person(name, opts = {}) {
  const u = S.users[name] || {};
  return el("div", {
    class: "person", title: name,
    onclick: (e) => showCard(name, e), ondblclick: () => openDm(name),
    oncontextmenu: (e) => personMenu(e, name),
  }, avatar(name), el("div", { class: "who" },
    el("div", { class: "nm", style: { color: userColor(name) } }, name, (u.admin || opts.admin) ? el("span", { class: "star", text: " " + ADMIN_BADGE }) : null),
    (playing(u) || u.status || opts.sub) ? el("div", { class: "st" + (playing(u) ? " game" : ""), text: playing(u) || u.status || opts.sub }) : null), opts.right || null);
}
function renderPeople() {
  const p = $("people");
  const top = p.scrollTop;
  p.replaceChildren();
  const names = Object.keys(S.users).sort((a, b) => (S.users[b].admin - S.users[a].admin) || a.localeCompare(b, undefined, { sensitivity: "base" }));
  p.append(sectionHead(`Online — ${names.length}`));
  for (const n of names) p.append(person(n));
  p.append(sectionHead("Friends", el("button", { class: "icon", title: "Add a friend", text: "+", onclick: addFriend })));
  if (S.requests.length) {
    for (const r of S.requests) p.append(el("div", { class: "request" }, avatar(r), el("span", { text: r }),
      el("button", { title: "Accept", text: "✓", onclick: () => send({ t: "friend_respond", user: r, accept: true }) }),
      el("button", { class: "flat", title: "Decline", text: "✕", onclick: () => send({ t: "friend_respond", user: r, accept: false }) })));
  }
  if (!S.friends.length && !S.requests.length) p.append(el("div", { class: "muted", style: { padding: "4px 8px", fontSize: ".85em" }, text: "No friends yet. Right-click someone > Add friend." }));
  for (const f of S.friends) p.append(person(f.name, { sub: f.online ? null : "Offline", right: el("span", { class: "dot" + (f.online ? "" : " off"), style: { marginLeft: "auto" } }) }));
  p.scrollTop = top;
}
function personMenu(e, name) {
  e.preventDefault();
  const friend = S.friends.some((f) => f.name === name);
  const items = [["View profile", () => showCard(name, e)]];
  if (!ci(name, S.me)) {
    items.push(["Message", () => openDm(name)]);
    items.push(friend ? ["Remove friend", () => send({ t: "friend_remove", user: name }), true] : ["Add friend", () => send({ t: "friend_request", to: name })]);
    const g = group(S.current);
    if (g && g.owner === S.me && g.members.includes(name)) items.push([`Remove from ${g.name}`, () => send({ t: "group_kick", gid: g.id, user: name }), true]);
    if (S.admin) {
      items.push(["Mute 10 minutes", () => send({ t: "admin", cmd: "mute", user: name, minutes: 10 }), true]);
      items.push(["Kick", () => send({ t: "admin", cmd: "kick", user: name }), true]);
    }
  } else items.push(["Edit profile", settingsDialog]);
  menu(e, items);
}

// ------------------------------------------------------------------ rendering: header + messages
function renderHeader() {
  const key = S.current;
  const tools = $("headtools"); tools.replaceChildren();
  let title = convName(key), sub = "";
  if (key.startsWith("#")) sub = "";
  else if (key.startsWith("@")) {
    const u = S.users[key.slice(1)];
    sub = u ? (u.status || "Online") : "Offline";
  } else {
    const g = group(key);
    if (g) {
      sub = `${g.members.length} people: ${g.members.join(", ")}`;
      const call = S.calls[g.id] || [];
      if (call.length) sub = `🔊 in the call: ${call.map((u) => u.name).join(", ")} · ` + sub;
      const inCall = V.chan === key;
      tools.append(el("button", { class: inCall ? "danger" : "", title: inCall ? "Leave the call" : (call.length ? "Join the call" : "Start a call"),
        text: inCall ? "Leave call" : (call.length ? "📞 Join call" : "📞 Call"), onclick: () => inCall ? leaveVoice() : joinVoice(key) }));
      tools.append(el("button", { class: "icon", title: "Group options", text: "⋯", onclick: (e) => groupMenu(e, g) }));
    }
  }
  $("title").textContent = (key.startsWith("@") ? "@" : "") + title;
  $("subtitle").textContent = sub;
  $("input").placeholder = `Message ${key.startsWith("#") ? key : key.startsWith("@") ? "@" + title : title}`;
}

function follows(prev, m) {
  return prev && !prev.kind && !m.kind && prev.user === m.user && m.ts - prev.ts >= 0 && m.ts - prev.ts < 420;
}
function renderMessages(toBottom, keepScroll) {
  const box = $("msgs");
  if (!S.current) return;
  const atBottom = box.scrollTop + box.clientHeight >= box.scrollHeight - 30;
  const old = box.scrollTop;
  box.replaceChildren();
  const msgs = S.convs[S.current];
  if (!msgs) { box.append(el("div", { class: "empty", text: "Loading..." })); return; }
  if (!msgs.length) box.append(el("div", { class: "empty", text: `This is the start of ${S.current.startsWith("@") ? "your messages with " : ""}${convName(S.current)}.` }));
  let prev = null;
  for (const m of msgs) { box.append(messageEl(m, follows(prev, m))); prev = m; }
  if (toBottom || atBottom) { box.scrollTop = box.scrollHeight; stick = true; }
  else box.scrollTop = old;
  if (toBottom && look().animations) { box.classList.remove("fade"); void box.offsetWidth; box.classList.add("fade"); }
}
function appendMessage(m) {
  const box = $("msgs"), msgs = S.convs[S.current];
  const atBottom = box.scrollTop + box.clientHeight >= box.scrollHeight - 60;
  box.querySelector(".empty")?.remove();
  box.append(messageEl(m, follows(msgs[msgs.length - 2], m)));
  if (atBottom || ci(m.user || "", S.me)) box.scrollTop = box.scrollHeight;
}
function canDelete(m) {
  if (!m.id) return false;
  if (ci(m.user, S.me)) return true;
  if (S.current.startsWith("#")) return S.admin;
  const g = group(S.current); return !!(g && g.owner === S.me);
}
function messageEl(m, grouped) {
  if (m.kind === "event") return el("div", { class: "event", text: `${m.text} · ${clock(m.ts)}` });
  if (m.kind === "announce") return el("div", { class: "announce" }, el("b", { text: `📢 Announcement from ${m.from}` }), el("div", { class: "text", text: m.text }));
  const body = el("div", { class: "body" });
  if (!grouped) body.append(el("div", {},
    el("span", { class: "nm", style: { color: userColor(m.user) }, text: m.user, onclick: (e) => showCard(m.user, e), oncontextmenu: (e) => personMenu(e, m.user) }),
    m.admin ? el("span", { class: "star", title: "Admin", text: " " + ADMIN_BADGE }) : null,
    el("span", { class: "when", text: when(m.ts) })));
  if (m.text) body.append(textEl(m.text, !m.file));
  if (m.file) body.append(fileEl(m.file));
  const row = el("div", { class: "msg" + (grouped ? "" : " first"), "data-id": m.id },
    el("div", { class: "gut" }, grouped ? el("span", { class: "t", text: clock(m.ts) }) :
      el("img", { class: "av big", src: avatarUrl(m.user), alt: "", loading: "lazy", onclick: (e) => showCard(m.user, e) })), body);
  if (canDelete(m)) row.append(el("div", { class: "tools" },
    el("button", { class: "icon", title: "Delete message", text: "🗑", onclick: () => deleteMsg(m) })));
  return row;
}
const EMOJI_ONLY = /^(?:\p{Extended_Pictographic}(?:️|‍|\p{Emoji_Modifier}|\p{Extended_Pictographic})*\s*){1,6}$/u;
function textEl(text, mediaOk) {
  const div = el("div", { class: "text" + (EMOJI_ONLY.test(text.trim()) ? " jumbo" : "") });
  let last = 0, media = null;
  for (const match of text.matchAll(URL_RE)) {
    const url = match[0].replace(/[).,!?]+$/, "");
    div.append(text.slice(last, match.index));
    div.append(el("a", { href: url, target: "_blank", rel: "noopener noreferrer", text: url }));
    last = match.index + url.length;
    if (mediaOk && !media) media = mediaFor(url);
  }
  div.append(text.slice(last));
  if (media) {
    const frag = el("div");
    if (text.trim() !== media.src && text.trim() !== media.link) frag.append(div);
    frag.append(el("img", { class: "media", src: media.src, alt: "", loading: "lazy", referrerpolicy: "no-referrer", onclick: () => viewImage(media.src) }));
    return frag;
  }
  return div;
}
function mediaFor(url) {
  let u; try { u = new URL(url); } catch { return null; }
  if (IMAGE_EXT.test(u.pathname)) return { src: url, link: url };
  if (u.hostname.endsWith("giphy.com") && u.pathname.includes("/gifs/")) {
    const id = u.pathname.replace(/\/$/, "").split("/").pop().split("-").pop();
    if (id) return { src: `https://media.giphy.com/media/${id}/giphy.gif`, link: url };
  }
  return null;
}
function fileEl(f) {
  if (f.deleted || !f.name) return el("div", { class: "deleted", text: "(file deleted)" });
  if (!S.token) return el("div", { class: "muted", text: f.name });
  if (IMAGE_EXT.test(f.name) && f.size < 40 * 1024 * 1024) {
    const src = fileUrl(f);
    return el("img", { class: "media", src, alt: f.name, title: `${f.name} (${human(f.size)})`, loading: "lazy", onclick: () => viewImage(src) });
  }
  if (VIDEO_EXT.test(f.name)) return el("div", {}, el("video", { class: "media", src: fileUrl(f), controls: true, preload: "metadata" }), fileCard(f));
  if (AUDIO_EXT.test(f.name)) return el("div", {}, el("audio", { src: fileUrl(f), controls: true, preload: "none", style: { marginTop: "4px", maxWidth: "100%" } }));
  return fileCard(f);
}
function fileCard(f) {
  return el("div", { class: "filecard" }, el("span", { class: "fi", text: "📄" }),
    el("div", {}, el("div", { class: "fn", text: f.name }), el("div", { class: "muted", style: { fontSize: ".85em" }, text: human(f.size) })),
    el("a", { href: fileUrl(f, true), download: f.name }, el("button", { class: "flat", text: "Download" })));
}
function viewImage(src) {
  const v = el("div", { id: "viewer", onclick: () => v.remove() }, el("img", { src, alt: "" }));
  document.body.append(v);
}
function deleteMsg(m) {
  if (!confirm("Delete this message? Everyone stops seeing it" + (m.file ? ", and the file is removed from the server." : "."))) return;
  send({ t: "delete_msg", id: m.id });
}

// ------------------------------------------------------------------ sending
function sendText(text) {
  const key = S.current;
  if (key.startsWith("#")) return send({ t: "msg", room: key, text });
  if (key.startsWith("g:")) return send({ t: "group_msg", gid: Number(key.slice(2)), text });
  return send({ t: "dm", to: key.slice(1), text });
}
function shortcodes(text) {
  if (!EMOJI) return text;
  return text.replace(/:([a-z0-9_+\-]{1,40}):/gi, (all, code) => EMOJI.codes[code.toLowerCase()] || all);
}
function submit() {
  const input = $("input");
  let text = input.value.trim();
  if (!text) return;
  if (text.startsWith("/") && !text.startsWith("//")) { if (command(text)) { input.value = ""; grow(); } return; }
  if (text.startsWith("//")) text = text.slice(1);
  if (text.length > 2000) { toast("Message too long (max 2000 characters)", true); return; }
  if (sendText(shortcodes(text))) { input.value = ""; grow(); }
}
function command(text) {
  const [cmd, ...rest] = text.slice(1).split(" ");
  const arg = rest.join(" ").trim(), c = cmd.toLowerCase();
  const [first, ...more] = arg.split(" "), tail = more.join(" ").trim();
  if ((c === "join" || c === "room") && arg) {
    const room = (arg.startsWith("#") ? arg : "#" + arg).toLowerCase();
    if (S.rooms.includes(room)) openConv(room);
    else if (S.admin) { S.pendingRoom = room; send({ t: "create_room", room }); }
    else toast("Only admins can create channels", true);
  } else if (["dm", "msg", "w"].includes(c) && first) {
    if (tail) send({ t: "dm", to: first, text: tail });
    openDm(first);
  } else if (c === "friend" && arg) send({ t: "friend_request", to: arg });
  else if (["nick", "name", "username"].includes(c) && arg) send({ t: "rename", name: arg });
  else if (S.admin && ["kick", "ban", "unban", "unmute", "resetpass"].includes(c) && first) {
    send(c === "resetpass" ? { t: "admin", cmd: c, user: first, password: tail } : { t: "admin", cmd: c, user: first, reason: tail });
  } else if (S.admin && c === "mute" && first) {
    const mins = parseFloat(more[0]); send({ t: "admin", cmd: "mute", user: first, minutes: isNaN(mins) ? 0 : mins, reason: isNaN(mins) ? tail : more.slice(1).join(" ") });
  } else if (S.admin && c === "announce" && arg) send({ t: "admin", cmd: "announce", text: arg });
  else if (S.admin && c === "clear" && S.current.startsWith("#")) { if (confirm(`Delete every message in ${S.current}?`)) send({ t: "admin", cmd: "clearhistory", room: S.current }); }
  else if (S.admin && c === "delroom" && S.current.startsWith("#")) { if (confirm(`Delete the channel ${S.current}?`)) send({ t: "admin", cmd: "delroom", room: S.current }); }
  else {
    toast("Commands: /join #room · /dm name message · /friend name · /nick newname" +
      (S.admin ? " · admins: /kick /ban /unban /mute name [minutes] /unmute /announce /clear /delroom /resetpass" : "") +
      " · start with // to send a /");
    return false;
  }
  return true;
}
function grow() { const t = $("input"); t.style.height = "auto"; t.style.height = Math.min(t.scrollHeight, window.innerHeight * 0.4) + "px"; }

// ------------------------------------------------------------------ uploads (straight to the server over HTTP)
function uploadFiles(files) {
  for (const f of files) {
    if (!f.size) { toast(`${f.name} is empty`, true); continue; }
    if (f.size > MAX_FILE) { toast(`${f.name} is bigger than 5 GB`, true); continue; }
    upload(f, S.current);
  }
}
function upload(file, conv) {
  const name = file.name || `pasted-${new Date().toISOString().replace(/[:.]/g, "-")}.png`;
  const bar = el("progress", { max: 1, value: 0 }), label = el("span", { text: "0%" });
  const xhr = new XMLHttpRequest();
  const row = el("div", { class: "up" }, el("span", { text: `⬆ ${name}` }), bar, label,
    el("button", { class: "icon", title: "Cancel", text: "✕", onclick: () => { xhr.abort(); row.remove(); } }));
  $("uploads").append(row);
  const q = new URLSearchParams({ t: S.token, conv, name });
  xhr.open("POST", SERVER.api("/upload?" + q.toString()));
  xhr.upload.onprogress = (e) => { if (e.lengthComputable) { bar.value = e.loaded / e.total; label.textContent = `${Math.floor(100 * e.loaded / e.total)}% of ${human(e.total)}`; } };
  xhr.onload = () => {
    row.remove();
    if (xhr.status !== 200) { let msg = "Upload failed"; try { msg = JSON.parse(xhr.responseText).error || msg; } catch { } toast(`${name}: ${msg}`, true); }
  };
  xhr.onerror = () => { row.remove(); toast(`${name}: the upload was interrupted`, true); };
  xhr.send(file);
}

// ------------------------------------------------------------------ popups
function closePops() { document.querySelectorAll(".pop").forEach((p) => p.remove()); S.card = null; }
function place(pop, x, y) {
  document.body.append(pop);
  const r = pop.getBoundingClientRect();
  pop.style.left = Math.max(8, Math.min(x, innerWidth - r.width - 8)) + "px";
  pop.style.top = Math.max(8, Math.min(y, innerHeight - r.height - 8)) + "px";
}
document.addEventListener("mousedown", (e) => { if (!e.target.closest(".pop") && !e.target.closest("#emojibtn,#gifbtn")) closePops(); });
document.addEventListener("keydown", (e) => { if (e.key === "Escape") { closePops(); $("modalbg")?.remove(); $("viewer")?.remove(); } });

function menu(e, items) {
  closePops();
  const m = el("div", { class: "pop menu" }, items.map(([label, fn, bad]) => el("div", { class: bad ? "bad" : "", text: label, onclick: () => { closePops(); fn(); } })));
  place(m, e.clientX, e.clientY);
}

function showCard(name, e) {
  e && e.stopPropagation();
  closePops();
  const card = el("div", { class: "pop card" });
  S.card = { name, el: card };
  fillCard(S.profiles[name] || { name, ...(S.users[name] || {}), online: name in S.users });
  const r = e && e.currentTarget && e.currentTarget.getBoundingClientRect ? e.currentTarget.getBoundingClientRect() : null;
  let x = e ? e.clientX + 12 : 100, y = e ? e.clientY - 40 : 100;
  if (r && e.currentTarget.closest("#people")) x = r.left - 312;       // beside the list
  place(card, x, y);
  send({ t: "get_profile", user: name });
}
function fillCard(info) {
  const card = S.card.el, name = info.name;
  S.card.name = name;
  const color = userColor(name);
  card.replaceChildren(
    el("div", { class: "banner", style: { background: shade(S.colors[name] || nameColor(name), 0.6) } }),
    el("img", { class: "cav", src: avatarUrl(name), alt: "" }),
    el("div", { class: "in" },
      el("div", { class: "cname", style: { color } }, name, info.admin ? el("span", { class: "star", style: { fontSize: ".7em" }, text: ` ${ADMIN_BADGE} Admin` }) : null),
      info.status ? el("div", { class: "muted", text: info.status }) : null,
      info.activity ? el("div", { class: "game", text: `🎮 Playing ${info.activity} · ${playedFor(info.activity_since)}` }) : null,
      el("h4", { text: "About me" }),
      info.bio ? textEl(info.bio, false) : el("div", { class: "muted", style: { fontStyle: "italic" }, text: "Nothing here yet" }),
      el("h4", { text: "Member since" }),
      el("div", { text: info.joined ? new Date(info.joined * 1000).toLocaleDateString(undefined, { year: "numeric", month: "long", day: "numeric" }) : "..." }),
      "online" in info ? el("div", { class: "muted", style: { marginTop: "6px" } },
        info.online ? el("span", { style: { color: "var(--good)" }, text: "● " }) : null, info.online ? "Online" : "Offline",
        info.voice ? ` · 🔊 in ${info.voice}` : "") : null,
      el("div", { class: "btns" }, ...(ci(name, S.me) ? [el("button", { class: "flat", text: "Edit profile", onclick: () => { closePops(); settingsDialog(); } })] : [
        el("button", { text: "Message", onclick: () => { closePops(); openDm(name); } }),
        info.friend === false ? el("button", { class: "flat", text: "Add friend", onclick: () => { send({ t: "friend_request", to: name }); closePops(); } }) : null,
      ]))));
}

// ------------------------------------------------------------------ emoji + GIF picker
let EMOJI = null;
async function loadEmoji() {
  if (EMOJI) return EMOJI;
  const r = await fetch(SERVER.api("/emoji.json"));
  const data = await r.json();
  data.codes = { ...data.aliases };
  for (const [e, n] of Object.entries(data.names)) if (n) data.codes[n.toLowerCase().replace(/[\s-]+/g, "_").replace(/[^a-z0-9_+]/g, "")] ??= e;
  EMOJI = data;
  return data;
}
async function picker(tab) {
  if ($("picker")) { const same = $("picker").dataset.tab === tab; closePops(); if (same) return; }
  closePops();
  const p = el("div", { id: "picker", class: "pop", "data-tab": tab });
  const search = el("input", { placeholder: tab === "gif" ? "Search GIPHY" : "Search emoji" });
  const cats = el("div", { class: "cats" }), grid = el("div", { class: "grid" + (tab === "gif" ? " gifs" : "") });
  p.append(el("div", { class: "tabs" },
    el("button", { class: tab === "emoji" ? "on" : "", text: "Emoji", onclick: () => { closePops(); picker("emoji"); } }),
    S.giphy ? el("button", { class: tab === "gif" ? "on" : "", text: "GIFs", onclick: () => { closePops(); picker("gif"); } }) : null), search);
  if (tab === "emoji") p.append(cats);
  p.append(grid);
  if (tab === "gif") p.append(el("div", { class: "credit", text: "Powered by GIPHY" }));
  const btn = $("emojibtn").getBoundingClientRect();
  place(p, btn.right - 370, btn.top - 410);
  search.focus();
  if (tab === "emoji") {
    const data = await loadEmoji();
    const show = (list) => {
      grid.replaceChildren(...list.map((e) => el("span", { class: "e", title: data.names[e] || "", text: e, onclick: () => insert(e) })));
    };
    const recent = store.get("recentEmoji", []);
    const names = Object.keys(data.categories);
    const tabs = (recent.length ? ["Recent", ...names] : names);
    const pick = (c) => {
      [...cats.children].forEach((b) => b.classList.toggle("on", b.dataset.c === c));
      show(c === "Recent" ? recent : data.categories[c]);
    };
    for (const c of tabs) cats.append(el("button", { title: c, "data-c": c, text: c === "Recent" ? "🕒" : (data.icons[c] || c[0]), onclick: () => pick(c) }));
    pick(tabs[0]);
    search.oninput = () => {
      const q = search.value.trim().toLowerCase();
      if (!q) { pick(tabs[0]); return; }
      const out = [];
      for (const list of Object.values(data.categories)) for (const e of list) if ((data.names[e] || "").toLowerCase().includes(q)) out.push(e);
      show(out.slice(0, 300));
    };
  } else {
    let timer = null;
    const load = async (q) => {
      const url = `https://api.giphy.com/v1/gifs/${q ? "search" : "trending"}?api_key=${encodeURIComponent(S.giphy)}&limit=30&rating=pg-13${q ? "&q=" + encodeURIComponent(q) : ""}`;
      try {
        const r = await fetch(url); const data = await r.json();
        grid.replaceChildren(...(data.data || []).map((g) => el("img", {
          src: (g.images.fixed_width || g.images.downsized).url, alt: g.title, loading: "lazy",
          onclick: () => { sendText(g.images.original.url.split("?")[0]); closePops(); },
        })));
        if (!(data.data || []).length) grid.replaceChildren(el("div", { class: "muted", text: q ? "No GIFs found" : "GIF search isn't working right now" }));
      } catch { grid.replaceChildren(el("div", { class: "muted", text: "Couldn't reach GIPHY" })); }
    };
    load("");
    search.oninput = () => { clearTimeout(timer); timer = setTimeout(() => load(search.value.trim()), 350); };
  }
}
function insert(text) {
  const t = $("input");
  const a = t.selectionStart ?? t.value.length, b = t.selectionEnd ?? t.value.length;
  t.value = t.value.slice(0, a) + text + t.value.slice(b);
  t.selectionStart = t.selectionEnd = a + text.length;
  t.focus(); grow();
  const recent = store.get("recentEmoji", []).filter((e) => e !== text);
  recent.unshift(text); store.set("recentEmoji", recent.slice(0, 32));
}

// ------------------------------------------------------------------ dialogs
function modal(title, ...content) {
  $("modalbg")?.remove();
  const bg = el("div", { id: "modalbg", onmousedown: (e) => { if (e.target === bg) bg.remove(); } },
    el("div", { class: "modal" }, el("h2", { text: title }), ...content));
  document.body.append(bg);
  return bg;
}
function createRoom() {
  const name = prompt("New channel name (letters, numbers, _ or -):");
  if (!name) return;
  const room = (name.startsWith("#") ? name : "#" + name).toLowerCase().trim();
  S.pendingRoom = room;
  send({ t: "create_room", room });
}
function addFriend() {
  const name = prompt("Friend's username:");
  if (name && name.trim()) send({ t: "friend_request", to: name.trim() });
}
function knownPeople(exclude = []) {
  const set = new Set([...Object.keys(S.users), ...S.friends.map((f) => f.name), ...S.dms]);
  set.delete(S.me); exclude.forEach((n) => set.delete(n));
  return [...set].sort((a, b) => a.localeCompare(b, undefined, { sensitivity: "base" }));
}
function peoplePicker(names) {
  const list = el("div", { class: "picklist" }, names.length ? names.map((n) => el("label", {}, el("input", { type: "checkbox", value: n }), avatar(n), n)) :
    el("div", { class: "muted", style: { padding: "6px" }, text: "Nobody online yet - type names below" }));
  const extra = el("input", { placeholder: "Or type usernames, separated by commas" });
  const chosen = () => [...new Set([...list.querySelectorAll("input:checked")].map((i) => i.value)
    .concat(extra.value.split(",").map((s) => s.trim()).filter(Boolean)))];
  return { list, extra, chosen };
}
function groupDialog() {
  const name = el("input", { placeholder: "Group name (optional)", maxlength: 50 });
  const pp = peoplePicker(knownPeople());
  const bg = modal("New group DM", el("div", { class: "muted", text: `Up to ${GROUP_MAX} people, including you.` }), name, pp.list, pp.extra,
    el("div", { class: "btns" }, el("button", { class: "flat", text: "Cancel", onclick: () => bg.remove() }),
      el("button", {
        text: "Create group", onclick: () => {
          const members = pp.chosen();
          if (!members.length) { toast("Pick at least one person", true); return; }
          send({ t: "group_create", name: name.value.trim(), members }); bg.remove();
        },
      })));
  name.focus();
}
function groupMenu(e, g) {
  const items = [
    ["Add people", () => {
      const pp = peoplePicker(knownPeople(g.members));
      const bg = modal(`Add people to ${g.name}`, pp.list, pp.extra, el("div", { class: "btns" },
        el("button", { class: "flat", text: "Cancel", onclick: () => bg.remove() }),
        el("button", { text: "Add", onclick: () => { const users = pp.chosen(); if (users.length) send({ t: "group_add", gid: g.id, users }); bg.remove(); } })));
    }],
    ["Members", () => {
      const bg = modal(`${g.name} — ${g.members.length} people`, el("div", { class: "picklist" }, g.members.map((n) =>
        person(n, { sub: n === g.owner ? "Owner" : null, right: g.owner === S.me && n !== S.me ? el("button", { class: "flat", style: { marginLeft: "auto" }, text: "Remove", onclick: (ev) => { ev.stopPropagation(); send({ t: "group_kick", gid: g.id, user: n }); bg.remove(); } }) : null }))),
        el("div", { class: "btns" }, el("button", { text: "Close", onclick: () => bg.remove() })));
    }],
    ["Rename", () => { const n = prompt("New group name:", g.name); if (n && n.trim()) send({ t: "group_rename", gid: g.id, name: n.trim() }); }],
    ["Leave group", () => { if (confirm(`Leave ${g.name}?`)) send({ t: "group_leave", gid: g.id }); }, true],
  ];
  const r = e.currentTarget.getBoundingClientRect();
  menu({ clientX: r.left - 120, clientY: r.bottom + 4, preventDefault() { } }, items);
}

function settingsDialog() {
  closePops();
  const l = look(), me = S.users[S.me] || {}, prof = S.profiles[S.me] || {};
  let avatarData = null, color = S.colors[S.me] || "";
  const preview = el("img", { class: "av", style: { width: "64px", height: "64px" }, src: avatarUrl(S.me) });
  const fileIn = el("input", { type: "file", accept: "image/png,image/jpeg,image/gif,image/webp,image/bmp", class: "hidden" });
  fileIn.onchange = async () => {
    const f = fileIn.files[0]; if (!f) return;
    try { avatarData = await encodeAvatar(f); preview.src = "data:image;base64," + avatarData; }
    catch (err) { toast(err.message, true); }
  };
  const swatch = el("span", { class: "swatch", style: { background: color || nameColor(S.me) } });
  const colorIn = el("input", { type: "color", value: color || nameColor(S.me), class: "hidden" });
  colorIn.oninput = () => { color = colorIn.value; swatch.style.background = color; };
  swatch.onclick = () => colorIn.click();
  const status = el("input", { maxlength: 80, placeholder: "e.g. playing Minecraft", value: me.status || "" });
  const bio = el("textarea", { maxlength: MAX_BIO, rows: 3, placeholder: "A few words for your profile card" });
  bio.value = prof.bio || "";
  const count = el("span", { class: "muted", style: { fontSize: ".8em" }, text: `${bio.value.length}/${MAX_BIO}` });
  bio.oninput = () => { count.textContent = `${bio.value.length}/${MAX_BIO}`; };
  const theme = el("select", {}, THEMES.map((t) => el("option", { value: t, selected: t === l.theme, text: t })));
  const size = el("select", {}, ["Small", "Normal", "Large", "Extra"].map((t) => el("option", { value: t, selected: t === l.size, text: t === "Extra" ? "Extra large" : t })));
  const accent = el("input", { type: "color", value: l.accent || getComputedStyle(document.documentElement).getPropertyValue("--accent").trim() || "#5865f2" });
  let accentSet = !!l.accent;
  accent.oninput = () => { accentSet = true; };
  const anim = el("input", { type: "checkbox", checked: l.animations });
  const notif = el("input", { type: "checkbox", checked: l.notify && "Notification" in window && Notification.permission === "granted" });
  const bg = modal("Settings",
    el("h3", { text: "Profile (everyone sees this)" }),
    el("div", { class: "row" }, preview, el("button", { class: "flat", text: "Choose image or GIF...", onclick: () => fileIn.click() }),
      el("button", { class: "flat", text: "Remove", onclick: () => { avatarData = ""; preview.src = "avatar.png"; } }), fileIn),
    el("div", { class: "muted", style: { fontSize: ".82em" }, text: "PNG, JPG or an animated GIF up to 1 MB. Bigger GIFs: the Campfire app shrinks them for you." }),
    el("div", { class: "row" }, "Name colour", swatch, colorIn, el("button", { class: "flat", text: "Default", onclick: () => { color = ""; swatch.style.background = nameColor(S.me); } })),
    el("label", {}, "Status"), status,
    el("label", {}, "About me ", count), bio,
    el("h3", { text: "Appearance (just this browser)" }),
    el("div", { class: "row" }, "Theme", theme, "Text size", size),
    el("div", { class: "row" }, "Accent colour", accent, el("button", { class: "flat", text: "Theme default", onclick: () => { accentSet = false; } })),
    el("label", { class: "row" }, anim, "Fade when switching chats"),
    el("label", { class: "row" }, notif, "Desktop notifications for DMs, groups and @mentions"),
    el("h3", { text: "Account" }),
    el("div", { class: "row" }, el("button", { class: "flat", text: "Change username", onclick: () => { const n = prompt("New username:", S.me); if (n && n.trim() && n.trim() !== S.me) send({ t: "rename", name: n.trim() }); } }),
      el("button", { class: "danger", text: "Log out", onclick: () => { bg.remove(); logout(); } })),
    el("div", { class: "btns" }, el("button", { class: "flat", text: "Cancel", onclick: () => bg.remove() }),
      el("button", {
        text: "Save", onclick: async () => {
          if (notif.checked && "Notification" in window && Notification.permission !== "granted") {
            try { await Notification.requestPermission(); } catch { }
          }
          store.set("look", { theme: theme.value, size: size.value, accent: accentSet ? accent.value : "", animations: anim.checked, notify: notif.checked });
          applyLook();
          if (avatarData !== null) send({ t: "avatar", data: avatarData });
          const prof2 = {};
          if (color !== (S.colors[S.me] || "")) prof2.color = color;
          if (status.value.trim() !== (me.status || "")) prof2.status = status.value.trim();
          if (bio.value.trim() !== (prof.bio || "").trim()) prof2.bio = bio.value.trim();
          if (Object.keys(prof2).length) send({ t: "profile", ...prof2 });
          bg.remove(); renderSide(); renderPeople(); renderMessages(false, true);
        },
      })));
}
async function encodeAvatar(file) {
  const buf = new Uint8Array(await file.arrayBuffer());
  const isGif = buf[0] === 0x47 && buf[1] === 0x49 && buf[2] === 0x46;
  const b64 = (bytes) => { let s = ""; for (let i = 0; i < bytes.length; i += 0x8000) s += String.fromCharCode(...bytes.subarray(i, i + 0x8000)); return btoa(s); };
  if (isGif) {
    if (buf.length > MAX_AVATAR) throw new Error(`That GIF is ${human(buf.length)} - avatars can be 1 MB at most (the Campfire app can shrink it).`);
    return b64(buf);
  }
  const img = await createImageBitmap(file);
  const side = Math.min(img.width, img.height), c = document.createElement("canvas");
  c.width = c.height = 128;
  c.getContext("2d").drawImage(img, (img.width - side) / 2, (img.height - side) / 2, side, side, 0, 0, 128, 128);
  const blob = await new Promise((res) => c.toBlob(res, "image/png"));
  return b64(new Uint8Array(await blob.arrayBuffer()));
}

// ------------------------------------------------------------------ screens
function showLogin() {
  $("app").classList.add("hidden"); $("login").classList.remove("hidden");
  $("loginbtn").disabled = false; $("loginbtn").textContent = "Log in";
  document.title = "Campfire";
}
function showApp() {
  $("login").classList.add("hidden"); $("app").classList.remove("hidden");
  renderSide(); renderPeople();
  loadEmoji().catch(() => { });          // for :shortcodes: in messages
}
function logout() {
  leaveVoice(true);
  S.creds = null; S.kicked = true; store.del("creds");
  S.convs = {}; S.dms = []; S.current = ""; S.unread = {}; S.token = "";
  if (S.ws) S.ws.close();
  $("pass").value = "";
  showLogin();
}
function closeDrawers() { document.body.classList.remove("show-side", "show-people"); }

// ------------------------------------------------------------------ voice chat
// The same audio as the desktop app: 48 kHz mono Opus, one 20 ms packet per voice_data message.
// Needs https (browsers only give the microphone to secure pages) and WebCodecs.
const V = {
  chan: "", muted: store.get("voice.muted", false), deaf: false, ctx: null, mic: null, capture: null,
  player: null, encoder: null, decoders: new Map(), speaking: new Map(), sentAt: 0, ts: 0,
  threshold: store.get("voice.threshold", -50), stats: { sent: 0, got: 0, played: 0 },
};
function voiceProblem() {
  if (!window.isSecureContext) return "Voice chat only works on the https website" + (S.webUrl ? `: ${S.webUrl}` : " (set up https in the server window)");
  if (!("AudioEncoder" in window) || !("AudioDecoder" in window)) return "This browser can't do voice chat yet - try Chrome, Edge or Firefox";
  if (!navigator.mediaDevices?.getUserMedia) return "This browser doesn't allow a microphone here";
  return "";
}
async function joinVoice(chan) {
  const why = voiceProblem();
  if (why) { toast(why, true); return; }
  if (V.chan === chan) return;
  if (V.chan) leaveVoice(true);
  try { await startAudio(); }
  catch (e) {
    stopAudio();
    toast(e.name === "NotAllowedError" ? "Campfire needs microphone permission for voice chat (check the address bar)" : `Couldn't start voice: ${e.message}`, true);
    return;
  }
  V.chan = chan;
  send({ t: "voice_join", channel: chan, muted: V.muted, deaf: V.deaf });
  renderSide(); renderVoiceBar(); renderHeader();
}
function leaveVoice(quiet) {
  if (!V.chan) return;
  stopAllVideo();
  send({ t: "voice_leave" });
  V.chan = "";
  stopAudio();
  if (!quiet) { renderSide(); renderVoiceBar(); renderHeader(); }
}
async function startAudio() {
  V.ctx = new AudioContext({ sampleRate: 48000, latencyHint: "interactive" });
  await V.ctx.audioWorklet.addModule("voice-worklet.js");
  V.player = new AudioWorkletNode(V.ctx, "cf-player", { numberOfInputs: 0, outputChannelCount: [1] });
  V.player.connect(V.ctx.destination);
  const dev = store.get("voice.mic", "");
  V.mic = await navigator.mediaDevices.getUserMedia({
    audio: { deviceId: dev ? { ideal: dev } : undefined, channelCount: 1, echoCancellation: true, noiseSuppression: true, autoGainControl: true },
  });
  const src = V.ctx.createMediaStreamSource(V.mic);
  V.capture = new AudioWorkletNode(V.ctx, "cf-capture", { numberOfOutputs: 1, outputChannelCount: [1] });
  const sink = V.ctx.createGain(); sink.gain.value = 0;          // keeps the capture running
  src.connect(V.capture); V.capture.connect(sink); sink.connect(V.ctx.destination);
  V.encoder = new AudioEncoder({
    output: (chunk) => {
      const bytes = new Uint8Array(chunk.byteLength); chunk.copyTo(bytes);
      let bin = ""; for (const b of bytes) bin += String.fromCharCode(b);
      if (S.ws && S.ws.readyState === 1 && V.chan) { S.ws.send(JSON.stringify({ t: "voice_data", d: btoa(bin) })); V.stats.sent++; }
    },
    error: (e) => console.warn("voice encoder", e),
  });
  V.encoder.configure({ codec: "opus", sampleRate: 48000, numberOfChannels: 1, bitrate: 32000, opus: { frameDuration: 20000 } });
  V.capture.port.onmessage = (e) => micFrame(e.data);
  if (V.ctx.state === "suspended") await V.ctx.resume();
}
function stopAudio() {
  try { V.mic?.getTracks().forEach((t) => t.stop()); } catch { }
  try { V.encoder?.close(); } catch { }
  for (const d of V.decoders.values()) try { d.close(); } catch { }
  V.decoders.clear(); V.speaking.clear();
  try { V.ctx?.close(); } catch { }
  Object.assign(V, { ctx: null, mic: null, capture: null, player: null, encoder: null });
}
function micFrame(pcm) {
  let sum = 0; for (let i = 0; i < pcm.length; i++) sum += pcm[i] * pcm[i];
  const db = 10 * Math.log10(sum / pcm.length + 1e-12);
  const now = performance.now();
  if (db > V.threshold) V.sentAt = now;                         // talking (with a short hangover, like the app)
  const talking = !V.muted && now - V.sentAt < 350;
  setSpeaking(S.me, talking);
  if (!talking || !V.encoder || V.encoder.state !== "configured") return;
  V.ts += 20000;
  const data = new AudioData({ format: "f32", sampleRate: 48000, numberOfFrames: pcm.length, numberOfChannels: 1, timestamp: V.ts, data: pcm });
  V.encoder.encode(data);
  data.close();
}
function voiceData(m) {
  if (!V.ctx || V.deaf || !m.u || !m.d) return;
  V.stats.got++;
  let dec = V.decoders.get(m.u);
  if (!dec) {
    const user = m.u;
    dec = new AudioDecoder({
      output: (ad) => {
        const pcm = new Float32Array(ad.numberOfFrames);
        ad.copyTo(pcm, { planeIndex: 0, format: "f32-planar" });
        ad.close();
        V.player?.port.postMessage({ user, pcm }, [pcm.buffer]);
        V.stats.played++;
      },
      error: (e) => { console.warn("voice decoder", e); V.decoders.delete(user); },
    });
    dec.configure({ codec: "opus", sampleRate: 48000, numberOfChannels: 1 });
    dec.ts = 0;
    V.decoders.set(user, dec);
  }
  const bin = atob(m.d), bytes = new Uint8Array(bin.length);
  for (let i = 0; i < bin.length; i++) bytes[i] = bin.charCodeAt(i);
  dec.ts += 20000;
  try { dec.decode(new EncodedAudioChunk({ type: "key", timestamp: dec.ts, data: bytes })); } catch (e) { console.warn(e); }
  setSpeaking(m.u, true);
}
function setSpeaking(name, on) {
  const now = performance.now();
  if (on) V.speaking.set(name, now);
}
setInterval(() => {                 // green rings for whoever spoke in the last 300 ms
  const now = performance.now();
  document.querySelectorAll("[data-voice-user]").forEach((e) => {
    const t = V.speaking.get(e.dataset.voiceUser);
    e.classList.toggle("talking", !!t && now - t < 300);
  });
}, 150);
function setMuted(on) {
  V.muted = on; store.set("voice.muted", on);
  if (V.chan) send({ t: "voice_status", muted: V.muted, deaf: V.deaf });
  renderVoiceBar();
}
function setDeaf(on) {
  V.deaf = on;
  if (on) V.player?.port.postMessage({ clear: true });
  if (V.chan) send({ t: "voice_status", muted: V.muted, deaf: V.deaf });
  renderVoiceBar();
}
function voiceLabel(chan) { return chan.startsWith("g:") ? (group(chan)?.name || "Group call") : chan; }
function renderVoiceBar() {
  let bar = $("voicebar");
  if (!V.chan) { bar?.remove(); return; }
  const fresh = el("div", { id: "voicebar" },
    el("div", { class: "vinfo" }, el("div", { class: "vgood", text: "● Voice connected" }), el("div", { class: "muted", text: voiceLabel(V.chan) })),
    el("button", { class: "icon" + (V.muted ? " on" : ""), title: V.muted ? "Unmute" : "Mute", text: V.muted ? "🔇" : "🎙", onclick: () => setMuted(!V.muted) }),
    el("button", { class: "icon" + (V.deaf ? " on" : ""), title: V.deaf ? "Undeafen" : "Deafen", text: V.deaf ? "🔕" : "🎧", onclick: () => setDeaf(!V.deaf) }),
    el("button", { class: "icon" + (VID.out.camera ? " live" : ""), title: VID.out.camera ? "Turn camera off" : "Turn camera on", text: "📷", onclick: () => VID.out.camera ? stopVideo("camera") : startVideo("camera") }),
    navigator.mediaDevices?.getDisplayMedia ? el("button", { class: "icon" + (VID.out.screen ? " live" : ""), title: VID.out.screen ? "Stop sharing your screen" : "Share your screen", text: "🖥", onclick: () => VID.out.screen ? stopVideo("screen") : startVideo("screen") }) : null,
    el("button", { class: "icon hang", title: "Leave voice", text: "☎", onclick: () => leaveVoice() }));
  if (bar) bar.replaceWith(fresh); else $("me")?.before(fresh);
}
H.voice_joined = (m) => { V.chan = m.channel; renderSide(); renderVoiceBar(); };
H.voice_left = (m) => { if (V.chan) { stopAllVideo(true); V.chan = ""; stopAudio(); renderStage(); renderSide(); renderVoiceBar(); renderHeader(); } if (m.reason) toast(m.reason); };
H.voice_data = voiceData;
window.addEventListener("beforeunload", () => { if (V.chan) send({ t: "voice_leave" }); });

// ------------------------------------------------------------------ camera + screen sharing
// The same video as the desktop app: VP8 frames over the chat connection (video_data). The
// server only sends a stream to people who asked to watch it, starting from a keyframe.
const VIDEO = {
  camera: { w: 640, h: 360, fps: 15, bitrate: 600000 },
  screen: { "720p": { w: 1280, h: 720 }, "1080p": { w: 1920, h: 1080 }, fps: 15 },
};
const VID = { out: {}, watching: new Map(), tiles: new Map(), stats: { sent: 0, got: 0, shown: 0 } };
function videoProblem(kind) {
  if (!V.chan) return "Join a voice channel or call first";
  if (!window.isSecureContext) return "Camera and screen sharing only work on the https website";
  if (!("VideoEncoder" in window) || !("VideoDecoder" in window)) return "This browser can't do video yet - try Chrome or Edge";
  if (kind === "screen" && !navigator.mediaDevices?.getDisplayMedia) return "This browser can't share its screen (phones usually can't)";
  return "";
}
async function startVideo(kind) {
  const why = videoProblem(kind);
  if (why) { toast(why, true); return; }
  if (VID.out[kind]) return;
  let stream;
  try {
    if (kind === "camera") {
      const dev = store.get("video.camera", "");
      stream = await navigator.mediaDevices.getUserMedia({ video: { deviceId: dev ? { ideal: dev } : undefined, width: { ideal: 1280 }, height: { ideal: 720 }, frameRate: { ideal: 30 } } });
    } else {
      stream = await navigator.mediaDevices.getDisplayMedia({ video: { frameRate: { ideal: VIDEO.screen.fps } }, audio: false });
    }
  } catch (e) {
    if (e.name !== "NotAllowedError" || kind === "camera") toast(e.name === "NotAllowedError" ? "Campfire needs camera permission (check the address bar)" : `Couldn't start: ${e.message}`, true);
    return;
  }
  const track = stream.getVideoTracks()[0];
  const video = el("video", { muted: true, playsinline: true, autoplay: true });
  video.muted = true; video.srcObject = stream;
  await video.play().catch(() => { });
  const canvas = document.createElement("canvas"), g = canvas.getContext("2d", { alpha: false });
  const out = { kind, stream, track, video, canvas, encoder: null, w: 0, h: 0, ts: 0, lastKey: 0, wantKey: true, timer: null };
  VID.out[kind] = out;
  track.addEventListener("ended", () => stopVideo(kind));          // the browser's own "Stop sharing"
  send({ t: "video_start", kind });
  const fps = kind === "camera" ? VIDEO.camera.fps : VIDEO.screen.fps;
  out.timer = setInterval(() => videoTick(out, g), 1000 / fps);
  renderVoiceBar(); renderStage();
}
function fitSize(srcW, srcH, maxW, maxH) {
  const s = Math.min(1, maxW / srcW, maxH / srcH);
  return [Math.max(16, Math.round(srcW * s / 2) * 2), Math.max(16, Math.round(srcH * s / 2) * 2)];   // even sizes for VP8
}
function videoTick(out, g) {
  const v = out.video;
  if (!v.videoWidth || !S.ws || S.ws.readyState !== 1) return;
  const lim = out.kind === "camera" ? VIDEO.camera : VIDEO.screen[store.get("video.screenSize", "720p")] || VIDEO.screen["720p"];
  const [w, h] = fitSize(v.videoWidth, v.videoHeight, lim.w, lim.h);
  if (w !== out.w || h !== out.h || !out.encoder) {                // new size: new encoder, starting with a keyframe
    try { out.encoder?.close(); } catch { }
    out.w = w; out.h = h; out.canvas.width = w; out.canvas.height = h; out.wantKey = true;
    const fps = out.kind === "camera" ? VIDEO.camera.fps : VIDEO.screen.fps;
    const bitrate = out.kind === "camera" ? VIDEO.camera.bitrate : Math.round((w * h > 1e6 ? 3000000 : 1500000) * fps / 15);
    out.encoder = new VideoEncoder({
      output: (chunk) => {
        const bytes = new Uint8Array(chunk.byteLength); chunk.copyTo(bytes);
        let bin = ""; for (let i = 0; i < bytes.length; i += 0x8000) bin += String.fromCharCode(...bytes.subarray(i, i + 0x8000));
        if (S.ws && S.ws.readyState === 1) { S.ws.send(JSON.stringify({ t: "video_data", kind: out.kind, d: btoa(bin), k: chunk.type === "key" })); VID.stats.sent++; }
      },
      error: (e) => { console.warn("video encoder", e); out.encoder = null; },
    });
    out.encoder.configure({ codec: "vp8", width: w, height: h, bitrate, framerate: fps, latencyMode: "realtime" });
  }
  if (out.encoder.encodeQueueSize > 2 || S.ws.bufferedAmount > 1.5e6) { out.wantKey = true; return; }   // falling behind: skip
  out.g = g;
  g.drawImage(v, 0, 0, w, h);
  const now = performance.now();
  const key = out.wantKey || now - out.lastKey > 4000;
  out.ts += Math.round(1e6 / (out.kind === "camera" ? VIDEO.camera.fps : VIDEO.screen.fps));
  const frame = new VideoFrame(out.canvas, { timestamp: out.ts });
  try { out.encoder.encode(frame, { keyFrame: key }); if (key) { out.lastKey = now; out.wantKey = false; } }
  finally { frame.close(); }
}
function stopVideo(kind, quiet) {
  const out = VID.out[kind];
  if (!out) return;
  delete VID.out[kind];
  clearInterval(out.timer);
  try { out.encoder?.close(); } catch { }
  out.stream.getTracks().forEach((t) => t.stop());
  if (!quiet) send({ t: "video_stop", kind });
  renderVoiceBar(); renderStage();
}
function stopAllVideo(quiet) {
  for (const kind of Object.keys(VID.out)) stopVideo(kind, quiet);
  for (const key of [...VID.watching.keys()]) unwatch(key, true);
}

// ---- watching
function streamsHere() {
  if (!V.chan) return [];
  const users = V.chan.startsWith("g:") ? (S.calls[Number(V.chan.slice(2))] || []) : (S.voice.find((c) => c.name === V.chan)?.users || []);
  const out = [];
  for (const u of users) for (const kind of ["camera", "screen"]) if (u[kind]) out.push({ user: u.name, kind, key: `${u.name}/${kind}` });
  return out;
}
function watch(user, kind) {
  const key = `${user}/${kind}`;
  if (VID.watching.has(key) || ci(user, S.me)) return;
  const w = { user, kind, decoder: null, ready: false };
  VID.watching.set(key, w);
  newDecoder(w);
  send({ t: "video_watch", user, kind });
  renderStage();
}
function newDecoder(w) {
  try { w.decoder?.close(); } catch { }
  w.ready = false;
  w.decoder = new VideoDecoder({
    output: (frame) => {
      const tile = VID.tiles.get(`${w.user}/${w.kind}`);
      const c = tile && tile.querySelector("canvas");
      if (c) {
        if (c.width !== frame.displayWidth || c.height !== frame.displayHeight) { c.width = frame.displayWidth; c.height = frame.displayHeight; }
        c.getContext("2d").drawImage(frame, 0, 0);
        tile.classList.add("live");
        VID.stats.shown++;
      }
      frame.close();
    },
    error: (e) => {                                              // start over from the next keyframe
      console.warn("video decoder", e);
      newDecoder(w);
      send({ t: "video_watch", user: w.user, kind: w.kind });
    },
  });
  w.decoder.configure({ codec: "vp8", optimizeForLatency: true });
}
function unwatch(key, quiet) {
  const w = VID.watching.get(key);
  if (!w) return;
  VID.watching.delete(key);
  try { w.decoder.close(); } catch { }
  if (!quiet && S.connected) send({ t: "video_unwatch", user: w.user, kind: w.kind });
}
function videoData(m) {
  const w = VID.watching.get(`${m.u}/${m.kind}`);
  if (!w || !m.d || w.decoder.state !== "configured") return;
  if (!w.ready && !m.k) return;                                   // can't start mid-stream
  if (w.decoder.decodeQueueSize > 4 && !m.k) return;              // behind: wait for a keyframe
  w.ready = true;
  const bin = atob(m.d), bytes = new Uint8Array(bin.length);
  for (let i = 0; i < bin.length; i++) bytes[i] = bin.charCodeAt(i);
  w.ts = (w.ts || 0) + 66666;
  try { w.decoder.decode(new EncodedVideoChunk({ type: m.k ? "key" : "delta", timestamp: w.ts, data: bytes })); VID.stats.got++; }
  catch (e) { console.warn(e); }
}
H.video_data = videoData;
H.video_keyframe = (m) => { const out = VID.out[m.kind]; if (out) out.wantKey = true; };

// ---- the stage: a tile for each camera and screen in your call
function renderStage() {
  const stage = $("stage");
  const streams = streamsHere();
  const mine = Object.keys(VID.out);
  const live = new Set(streams.map((s) => s.key));
  for (const key of [...VID.watching.keys()]) if (!live.has(key)) unwatch(key, true);   // stream ended
  for (const s of streams) if (s.kind === "camera" && !ci(s.user, S.me)) watch(s.user, s.kind);   // cameras: always
  const wanted = [...mine.map((k) => ({ user: S.me, kind: k, key: `${S.me}/${k}`, own: true })), ...streams.filter((s) => !ci(s.user, S.me))];
  if (!wanted.length) { stage.classList.add("hidden"); stage.replaceChildren(); VID.tiles.clear(); return; }
  stage.classList.remove("hidden");
  const keep = new Set(wanted.map((s) => s.key));
  for (const [key, tile] of VID.tiles) if (!keep.has(key)) { tile.remove(); VID.tiles.delete(key); }
  for (const s of wanted) {
    let tile = VID.tiles.get(s.key);
    if (!tile) {
      const label = el("div", { class: "tlabel", text: `${s.kind === "screen" ? "🖥" : "📷"} ${s.own ? "You" : s.user}` });
      const full = el("button", { class: "icon tfull", title: "Full screen", text: "⛶", onclick: () => (tile.querySelector("video,canvas")).requestFullscreen?.() });
      if (s.own) {
        const v = el("video", { muted: true, playsinline: true, autoplay: true, class: s.kind === "camera" ? "mirror" : "" });
        v.muted = true; v.srcObject = VID.out[s.kind].stream; v.play().catch(() => { });
        tile = el("div", { class: "tile live", "data-key": s.key }, v, label, full);
      } else {
        tile = el("div", { class: "tile", "data-key": s.key }, el("canvas", { width: 640, height: 360 }), label, full);
        if (s.kind === "screen") tile.append(el("button", { class: "twatch", text: `Watch ${s.user}'s screen`, onclick: () => watch(s.user, "screen") }));
      }
      VID.tiles.set(s.key, tile);
      stage.append(tile);
    }
    tile.classList.toggle("watching", s.own || VID.watching.has(s.key));
  }
  stage.dataset.count = Math.min(wanted.length, 4);
}

// ------------------------------------------------------------------ wiring
applyLook();
$("loginform").addEventListener("submit", (e) => {
  e.preventDefault();
  if (SERVER.pick) {
    const host = cleanServer($("server").value);
    if (!host) { loginError("Type the server's address (its DuckDNS name, e.g. campfire-shaman)"); return; }
    SERVER.host = host; store.set("server", host); $("server").value = host;
  }
  S.everConnected = false;
  login($("user").value.trim(), $("pass").value, $("remember").checked);
});
$("input").addEventListener("keydown", (e) => {
  if (e.key === "Enter" && !e.shiftKey && !e.isComposing) { e.preventDefault(); submit(); }
});
$("input").addEventListener("input", grow);
$("input").addEventListener("paste", (e) => {
  const files = [...(e.clipboardData?.files || [])];
  if (files.length) { e.preventDefault(); uploadFiles(files); }
});
$("attach").onclick = () => $("file").click();
$("file").onchange = () => { uploadFiles([...$("file").files]); $("file").value = ""; };
$("emojibtn").onclick = () => picker("emoji");
$("gifbtn").onclick = () => picker("gif");
$("openside").onclick = () => document.body.classList.toggle("show-side");
$("openpeople").onclick = () => document.body.classList.toggle("show-people");
$("msgs").addEventListener("click", closeDrawers);
// stay at the newest message while pictures finish loading (they change the height)
let stick = true;
$("msgs").addEventListener("scroll", () => { const b = $("msgs"); stick = b.scrollTop + b.clientHeight >= b.scrollHeight - 40; });
$("msgs").addEventListener("load", () => { if (stick) $("msgs").scrollTop = $("msgs").scrollHeight; }, true);
$("msgs").addEventListener("loadedmetadata", () => { if (stick) $("msgs").scrollTop = $("msgs").scrollHeight; }, true);
let dragDepth = 0;
document.addEventListener("dragenter", (e) => {
  if (!S.connected || ![...(e.dataTransfer?.types || [])].includes("Files")) return;
  if (dragDepth++ === 0) document.body.append(el("div", { id: "drop", text: `Drop to send to ${convName(S.current)}` }));
});
document.addEventListener("dragleave", () => { if (--dragDepth <= 0) { dragDepth = 0; $("drop")?.remove(); } });
document.addEventListener("dragover", (e) => e.preventDefault());
document.addEventListener("drop", (e) => {
  e.preventDefault(); dragDepth = 0; $("drop")?.remove();
  if (S.connected && e.dataTransfer?.files?.length) uploadFiles([...e.dataTransfer.files]);
});
setInterval(() => { if (S.connected) send({ t: "ping" }); }, 30000);

SERVER.init();
if (SERVER.pick) {
  $("serverrow").classList.remove("hidden");
  $("server").value = SERVER.host;
}
const saved = store.get("creds");
if (saved && saved.user) {
  $("user").value = saved.user;
  login(saved.user, saved.pass, true);
}
window.Campfire = S;                // handy for debugging (and the tests)
