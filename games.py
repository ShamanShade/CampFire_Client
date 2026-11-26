"""
Working out which game you're playing, for "Playing ..." under your name (like Discord).

Every few seconds the app looks at the programs running on this PC (Windows):
- a list of well-known games, by program name;
- anything installed by Steam, Epic Games or the Xbox app: the game's folder name
  (steamapps/common/<Game>, Epic Games/<Game>, XboxGames/<Game>);
- Minecraft (Java), which runs as java/javaw, by its window title - including Lunar, Badlion,
  Feather and other launchers' clients;
- games you added yourself in Settings (pick the .exe, give it a name).
If several match, the one in the front window wins.

Nothing about other programs is sent anywhere: only the name of the game found.
"""

from __future__ import annotations

import os
import re
import sys

# program name (lowercase) -> game
KNOWN = {
    "minecraft.windows.exe": "Minecraft", "fortniteclient-win64-shipping.exe": "Fortnite",
    "valorant-win64-shipping.exe": "VALORANT", "league of legends.exe": "League of Legends",
    "robloxplayerbeta.exe": "Roblox", "gta5.exe": "Grand Theft Auto V", "gta5_enhanced.exe": "Grand Theft Auto V",
    "cs2.exe": "Counter-Strike 2", "overwatch.exe": "Overwatch 2", "r5apex.exe": "Apex Legends",
    "r5apex_dx12.exe": "Apex Legends", "rocketleague.exe": "Rocket League", "rustclient.exe": "Rust",
    "terraria.exe": "Terraria", "among us.exe": "Among Us", "fallguys_client_game.exe": "Fall Guys",
    "genshinimpact.exe": "Genshin Impact", "starrail.exe": "Honkai: Star Rail", "zenlesszonezero.exe": "Zenless Zone Zero",
    "dota2.exe": "Dota 2", "tf_win64.exe": "Team Fortress 2", "eldenring.exe": "ELDEN RING",
    "cod.exe": "Call of Duty", "tslgame.exe": "PUBG: BATTLEGROUNDS", "rainbowsix.exe": "Rainbow Six Siege",
    "rainbowsix_vulkan.exe": "Rainbow Six Siege", "haloinfinite.exe": "Halo Infinite", "destiny2.exe": "Destiny 2",
    "palworld-win64-shipping.exe": "Palworld", "lethal company.exe": "Lethal Company",
    "stardew valley.exe": "Stardew Valley", "hollow_knight.exe": "Hollow Knight", "osu!.exe": "osu!",
    "wow.exe": "World of Warcraft", "hearthstone.exe": "Hearthstone", "deadbydaylight-win64-shipping.exe": "Dead by Daylight",
    "marvel-win64-shipping.exe": "Marvel Rivals", "helldivers2.exe": "HELLDIVERS 2", "bg3.exe": "Baldur's Gate 3",
    "bg3_dx11.exe": "Baldur's Gate 3", "cyberpunk2077.exe": "Cyberpunk 2077", "ts4_x64.exe": "The Sims 4",
    "rdr2.exe": "Red Dead Redemption 2", "witcher3.exe": "The Witcher 3", "factorio.exe": "Factorio",
    "geometrydash.exe": "Geometry Dash", "brawlhalla.exe": "Brawlhalla", "paladins.exe": "Paladins",
    "smite.exe": "SMITE", "warframe.x64.exe": "Warframe", "pathofexile.exe": "Path of Exile",
    "pathofexile_x64.exe": "Path of Exile", "pathofexile2.exe": "Path of Exile 2", "diablo iv.exe": "Diablo IV",
    "eafc25.exe": "EA SPORTS FC 25", "eafc26.exe": "EA SPORTS FC 26", "nba2k25.exe": "NBA 2K25", "nba2k26.exe": "NBA 2K26",
    "forzahorizon5.exe": "Forza Horizon 5", "deltaforce-win64-shipping.exe": "Delta Force",
    "thefinals.exe": "THE FINALS", "bf2042.exe": "Battlefield 2042", "bf6.exe": "Battlefield 6",
    "repo.exe": "R.E.P.O.", "peak.exe": "PEAK", "schedule i.exe": "Schedule I", "sonsoftheforest.exe": "Sons Of The Forest",
    "phasmophobia.exe": "Phasmophobia", "valheim.exe": "Valheim", "subnautica.exe": "Subnautica",
    "no man's sky.exe": "No Man's Sky", "nms.exe": "No Man's Sky", "satisfactory.exe": "Satisfactory",
    "projectzomboid64.exe": "Project Zomboid", "7daystodie.exe": "7 Days to Die", "deadlock.exe": "Deadlock",
    "vrchat.exe": "VRChat", "beatsaber.exe": "Beat Saber", "rec room.exe": "Rec Room",
}
# window titles of Java games (Minecraft and its clients)
JAVA_TITLES = [
    (re.compile(r"^(minecraft|lunar client|badlion|feather|labymod|gh0st client|pvp lounge)\b", re.I), "Minecraft"),
]
JAVA = {"java.exe", "javaw.exe", "java", "javaw"}
STORE_FOLDERS = [                                      # <...>/<this>/<Game>/... -> Game
    re.compile(r"[\\/]steamapps[\\/]common[\\/]([^\\/]+)[\\/]", re.I),
    re.compile(r"[\\/]epic games[\\/]([^\\/]+)[\\/]", re.I),
    re.compile(r"[\\/]xboxgames[\\/]([^\\/]+)[\\/]", re.I),
]
NOT_GAMES = {"wallpaper engine", "steamvr", "steamworks shared", "steam controller configs", "launcher",
             "epic online services", "directxredist", "_commonredist"}
HELPERS = re.compile(r"(crash|report|setup|install|unins|redist|launcher|updater|helper|vc_redist|dxsetup|"
                     r"easyanticheat|battleye|be_service|cefprocess|webhelper)", re.I)


def clean(name: str) -> str:
    return " ".join(name.replace("_", " ").split())[:64]


def from_process(path: str, titles=(), custom=None) -> str:
    """The game this program is, or ''."""
    exe = re.split(r"[\\/]", path)[-1].lower()
    if custom:
        for want, name in custom.items():
            if exe == re.split(r"[\\/]", want)[-1].lower():
                return clean(name)
    if exe in KNOWN:
        return KNOWN[exe]
    if exe in JAVA:
        for title in titles:
            for rx, game in JAVA_TITLES:
                if rx.search(title.strip()):
                    return game
        return ""
    for rx in STORE_FOLDERS:
        m = rx.search(path)
        if m:
            folder = m.group(1)
            if clean(folder).lower() in NOT_GAMES or HELPERS.search(exe):
                return ""
            return clean(folder)
    return ""


def detect(processes, custom=None, front_pid=None) -> str:
    """processes: [(pid, exe path, [window titles])]. Returns the game being played, or ''."""
    found = []
    for pid, path, titles in processes:
        game = from_process(path or "", titles or (), custom)
        if game:
            found.append((pid != front_pid, not titles, game))       # front window first, then windowed ones
    found.sort(key=lambda x: (x[0], x[1]))
    return found[0][2] if found else ""


# ------------------------------------------------------------------ what's running (Windows)
def windows_snapshot():
    """[(pid, exe path, [visible window titles])], and the pid of the front window."""
    import ctypes
    from ctypes import wintypes
    k32, u32 = ctypes.windll.kernel32, ctypes.windll.user32
    titles: dict[int, list[str]] = {}

    @ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
    def each_window(hwnd, _l):
        if u32.IsWindowVisible(hwnd):
            n = u32.GetWindowTextLengthW(hwnd)
            if n:
                buf = ctypes.create_unicode_buffer(n + 1)
                u32.GetWindowTextW(hwnd, buf, n + 1)
                pid = wintypes.DWORD()
                u32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
                titles.setdefault(pid.value, []).append(buf.value)
        return True

    u32.EnumWindows(each_window, 0)
    front = wintypes.DWORD()
    u32.GetWindowThreadProcessId(u32.GetForegroundWindow(), ctypes.byref(front))

    class ENTRY(ctypes.Structure):
        _fields_ = [("dwSize", wintypes.DWORD), ("cntUsage", wintypes.DWORD), ("th32ProcessID", wintypes.DWORD),
                    ("th32DefaultHeapID", ctypes.c_void_p), ("th32ModuleID", wintypes.DWORD),
                    ("cntThreads", wintypes.DWORD), ("th32ParentProcessID", wintypes.DWORD),
                    ("pcPriClassBase", ctypes.c_long), ("dwFlags", wintypes.DWORD), ("szExeFile", ctypes.c_wchar * 260)]

    k32.CreateToolhelp32Snapshot.restype = wintypes.HANDLE
    snap = k32.CreateToolhelp32Snapshot(0x2, 0)          # TH32CS_SNAPPROCESS
    out = []
    try:
        e = ENTRY()
        e.dwSize = ctypes.sizeof(ENTRY)
        ok = k32.Process32FirstW(snap, ctypes.byref(e))
        while ok:
            pid, path = e.th32ProcessID, e.szExeFile
            h = k32.OpenProcess(0x1000, False, pid)        # PROCESS_QUERY_LIMITED_INFORMATION
            if h:
                buf = ctypes.create_unicode_buffer(1024)
                size = wintypes.DWORD(1024)
                if k32.QueryFullProcessImageNameW(h, 0, buf, ctypes.byref(size)):
                    path = buf.value
                k32.CloseHandle(h)
            out.append((pid, path, titles.get(pid, [])))
            ok = k32.Process32NextW(snap, ctypes.byref(e))
    finally:
        k32.CloseHandle(snap)
    return out, front.value


def linux_snapshot():
    """For testing on Linux: processes from /proc (no window titles)."""
    out = []
    for pid in os.listdir("/proc"):
        if pid.isdigit():
            try:
                out.append((int(pid), os.readlink(f"/proc/{pid}/exe"), []))
            except OSError:
                pass
    return out, None


def current_game(custom=None) -> str:
    if sys.platform == "win32":
        procs, front = windows_snapshot()
    elif sys.platform.startswith("linux"):
        procs, front = linux_snapshot()
    else:
        return ""
    return detect(procs, custom, front)
