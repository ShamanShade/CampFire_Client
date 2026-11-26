"""'Playing ...': finding the game, the server passing it on, and the app showing it.
Run:  QT_QPA_PLATFORM=offscreen python tests/test_games.py"""

import os
import sys

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "tests"))

import games  # noqa: E402


def test_detect():
    W = "C:\\Program Files (x86)\\Steam\\steamapps\\common\\"
    cases = [
        ([(1, "C:\\Riot Games\\League of Legends\\Game\\League of Legends.exe", ["League of Legends (TM) Client"])], "League of Legends"),
        ([(1, W + "Hades II\\Ship\\Hades2.exe", ["Hades II"])], "Hades II"),               # any Steam game: its folder
        ([(1, "D:\\Epic Games\\Satisfactory\\FactoryGame\\Binaries\\Win64\\FactoryGame-Win64-Shipping.exe", ["x"])], "Satisfactory"),
        ([(1, "C:\\XboxGames\\Grounded 2\\Content\\Grounded2.exe", ["Grounded 2"])], "Grounded 2"),
        ([(1, "C:\\Program Files\\Java\\bin\\javaw.exe", ["Minecraft* 1.21.4"])], "Minecraft"),
        ([(1, "C:\\Users\\x\\.lunarclient\\jre\\bin\\javaw.exe", ["Lunar Client 1.8.9 (v3)"])], "Minecraft"),
        ([(1, "C:\\Program Files\\Java\\bin\\javaw.exe", ["Some Java Tool"])], ""),       # other Java programs aren't games
        ([(1, W + "Hades II\\UnityCrashHandler64.exe", [])], ""),                         # helpers next to a game
        ([(1, W + "wallpaper_engine\\wallpaper64.exe", ["Wallpaper"])], "wallpaper engine" and ""),
        ([(1, "C:\\Windows\\explorer.exe", ["Desktop"]), (2, "C:\\Program Files\\Chrome\\chrome.exe", ["YouTube"])], ""),
        ([(1, "C:\\Games\\MyIndie\\indie.exe", ["Indie"])], ""),
    ]
    for procs, want in cases:
        got = games.detect(procs)
        assert got == want, (procs, got, want)
    # a game you added yourself
    assert games.detect([(1, "C:\\Games\\MyIndie\\indie.exe", ["Indie"])], {"indie.exe": "My Indie Game"}) == "My Indie Game"
    # two games open: the one in front wins
    both = [(1, W + "Terraria\\Terraria.exe", ["Terraria"]), (2, "C:\\Riot Games\\League of Legends.exe", ["League"])]
    assert games.detect(both, front_pid=2) == "League of Legends"
    assert games.detect(both, front_pid=1) == "Terraria"
    assert games.clean("  Some__Game   Name ") == "Some Game Name"
    if sys.platform.startswith("linux"):
        assert isinstance(games.current_game(), str)       # the real process list doesn't break
    print("detection ok")


def test_app():
    from test_gui import TMP, cc, free_port, login, pump, start_server
    from PyQt6.QtWidgets import QApplication
    app = QApplication(sys.argv)
    port = free_port()
    server = start_server(port, os.path.join(TMP, "games.db"))
    playing = {"alice": "Minecraft"}
    cc.games.current_game = lambda custom=None: playing.get("alice", "")
    try:
        a = login(port, "alice").chat
        b = login(port, "bob").chat
        a.show(), b.show()
        a.check_game()
        pump(lambda: b.users.get("alice", {}).get("activity") == "Minecraft")
        assert b.users["alice"]["activity_since"] > 0
        lines = {b.user_list.item(i).data(cc.Qt.ItemDataRole.UserRole):
                 b.user_list.item(i).data(cc.Qt.ItemDataRole.UserRole + 1) for i in range(b.user_list.count())}
        assert lines["alice"] == "\U0001f3ae Playing Minecraft", lines
        b.show_profile("alice")
        pump(lambda: b.card.game.text().startswith("\U0001f3ae Playing Minecraft"))
        assert "just started" in b.card.game.text()
        b.card.close()
        # someone logging in later sees it too
        c = login(port, "carl").chat
        assert c.users["alice"]["activity"] == "Minecraft"
        # stop playing -> it goes away; turned off in Settings -> never shown
        playing["alice"] = ""
        a.check_game()
        pump(lambda: not b.users["alice"].get("activity"))
        playing["alice"] = "Roblox"
        cc.settings.setValue("activity/show", False)
        a.check_game()
        pump(lambda: True, timeout=0.6)
        assert not b.users["alice"].get("activity")
        cc.settings.setValue("activity/show", True)
        a.check_game()
        pump(lambda: b.users["alice"].get("activity") == "Roblox")
        # adding your own game in Settings
        dlg = cc.SettingsDialog(a)
        dlg.add_game("C:\\Games\\Indie\\indie.exe", "My Indie Game")
        assert dlg.game_list.count() == 1 and "My Indie Game" in dlg.game_list.item(0).text()
        dlg.save()
        assert cc.custom_games() == {"indie.exe": "My Indie Game"}
        # logging out clears it for everyone
        a.close()
        a.conn.close() if hasattr(a.conn, "close") else None
        pump(lambda: "alice" not in b.users, timeout=6)
        print("app ok")
    finally:
        server.terminate()


if __name__ == "__main__":
    test_detect()
    test_app()
    print("games tests passed")
