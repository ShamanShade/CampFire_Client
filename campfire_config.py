"""
Where Campfire connects. Change these if your server's address changes, then push to GitHub
(the Windows app is rebuilt, and people get the update).

Shared by the chat app (campfire_client.py) and the server window's "Can friends connect?" check.
"""

SERVER_HOST = "campfire-gh0st.duckdns.org"   # DuckDNS name: the server keeps it pointed at your home IP
SERVER_PORT = 55555

# The website, hosted on GitHub Pages (published by .github/workflows/pages.yml).
WEB_URL = "https://shamanshade.github.io/CampFire_Client/"
# The server's DuckDNS name (e.g. "campfire-shaman"), once https is set up in the server window.
# The website connects there. Empty: the website asks for it (or use WEB_URL?server=NAME).
WEB_SERVER = "campfire-gh0st"
