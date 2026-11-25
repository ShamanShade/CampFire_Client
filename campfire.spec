# PyInstaller recipe for the Windows download: builds Campfire.exe, Campfire Admin.exe and
# Campfire Server.exe into one "Campfire" folder that shares its libraries.
#   pip install pyinstaller  &&  pyinstaller --noconfirm campfire.spec   ->  dist/Campfire/
# (The GitHub Actions workflow in .github/workflows runs this on Windows for every push.)

datas = [("icon.png", "."), ("avatar.png", "."), ("icon.ico", ".")]
hidden = ["voice", "video", "transfers", "emoji_data", "server", "server_gui", "campfire_client"]


def analysis(script):
    return Analysis([script], pathex=["."], datas=datas, hiddenimports=hidden,
                    excludes=["tkinter", "matplotlib", "IPython", "pytest"], noarchive=False)


def program(a, name):
    return EXE(PYZ(a.pure), a.scripts, [], exclude_binaries=True, name=name, icon="icon.ico",
               console=False, upx=False)


client, admin, srv = analysis("campfire_client.py"), analysis("campfire_admin.py"), analysis("server.py")
coll = COLLECT(
    program(client, "Campfire"), client.binaries, client.datas,
    program(admin, "Campfire Admin"), admin.binaries, admin.datas,
    program(srv, "Campfire Server"), srv.binaries, srv.datas,
    name="Campfire", upx=False,
)
