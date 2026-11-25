"""
Campfire admin client.

Exactly the same app as campfire_client.py, but it connects to the server running on
THIS computer. Use it on the PC that runs server.py: connecting to your own public IP
from inside your house usually fails, so the normal client can't log you in there.

To have admin powers, start the server with your name:  python server.py --admin YourName
"""

import campfire_client as cc

cc.SERVER_HOST = "127.0.0.1"
cc.LOGIN_NOTE = "Admin client - connects to the server on this PC"

if __name__ == "__main__":
    cc.main()
