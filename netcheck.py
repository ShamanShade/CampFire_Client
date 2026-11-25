"""
Helps the server owner work out (and fix) why friends can't connect.

- check_from_internet(port): asks an outside service (ifconfig.co) for this network's public IP
  address and whether it can open a connection to that port. That's exactly what a friend's
  Campfire does.
- UPnP: most home routers let programs on the network ask them to forward a port. open_port()
  finds the router (SSDP), then asks it to forward TCP <port> to this PC (AddPortMapping).
- Windows Firewall: allow_through_firewall() adds an inbound rule for the port (and removes
  "block" rules Windows makes if its first pop-up was cancelled). This needs admin, so Windows
  shows a prompt.

Everything here blocks; the server window runs it on a background thread.
"""

from __future__ import annotations

import json
import os
import re
import socket
import subprocess
import sys
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET

REACH_URL = "https://ifconfig.co/port/{port}"         # {"ip": "...", "port": 55555, "reachable": true}
USER_AGENT = "Campfire-Server (connection check)"
RULE_NAME = "Campfire Server"

SSDP_ADDR = ("239.255.255.250", 1900)
SEARCH_TARGETS = [
    "urn:schemas-upnp-org:service:WANIPConnection:1",
    "urn:schemas-upnp-org:service:WANIPConnection:2",
    "urn:schemas-upnp-org:service:WANPPPConnection:1",
    "urn:schemas-upnp-org:device:InternetGatewayDevice:1",
]


class CheckError(Exception):
    pass


def lan_ip():
    """This PC's address on the home network (what the router should forward to)."""
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.connect(("10.255.255.255", 1))               # no packet is sent; just picks the interface
            return s.getsockname()[0]
    except OSError:
        return "127.0.0.1"


def http_get(url, timeout=20, headers=None):
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, **(headers or {})})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read()


def check_from_internet(port, url=REACH_URL):
    """{"ip": public address, "reachable": bool}. Raises CheckError if the check service can't be reached."""
    try:
        data = json.loads(http_get(url.format(port=port), headers={"Accept": "application/json"}))
    except Exception as e:
        raise CheckError(f"Couldn't reach the checking service ({e})") from e
    if "ip" not in data or "reachable" not in data:
        raise CheckError("The checking service gave an unexpected answer")
    return {"ip": str(data["ip"]), "reachable": bool(data["reachable"])}


# ---------------------------------------------------------------- UPnP (router port forwarding)
def discover(timeout=3.0, target=SSDP_ADDR):
    """LOCATION urls of UPnP internet gateways (routers) on the network."""
    found = []
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
    try:
        sock.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_TTL, 2)
        sock.settimeout(timeout)
        for st in SEARCH_TARGETS:
            msg = ("M-SEARCH * HTTP/1.1\r\n"
                   f"HOST: {SSDP_ADDR[0]}:{SSDP_ADDR[1]}\r\n"
                   'MAN: "ssdp:discover"\r\n'
                   "MX: 2\r\n"
                   f"ST: {st}\r\n\r\n")
            sock.sendto(msg.encode(), target)
        while True:
            try:
                data, _ = sock.recvfrom(65535)
            except socket.timeout:
                break
            m = re.search(rb"(?im)^location:\s*(\S+)", data)
            if m:
                loc = m.group(1).decode(errors="replace")
                if loc not in found:
                    found.append(loc)
    finally:
        sock.close()
    return found


def find_wan_service(location):
    """(control url, service type) of the router's WAN connection service, or None."""
    root = ET.fromstring(http_get(location, timeout=5))
    ns = ""
    if root.tag.startswith("{"):
        ns = root.tag.split("}")[0] + "}"
    base = root.findtext(f"{ns}URLBase") or location
    for svc in root.iter(f"{ns}service"):
        stype = svc.findtext(f"{ns}serviceType") or ""
        if "WANIPConnection" in stype or "WANPPPConnection" in stype:
            control = svc.findtext(f"{ns}controlURL") or ""
            return urllib.parse.urljoin(base, control), stype
    return None


def soap(control_url, service, action, args=None, timeout=8):
    body = "".join(f"<{k}>{v}</{k}>" for k, v in (args or {}).items())
    envelope = ('<?xml version="1.0"?>'
                '<s:Envelope xmlns:s="http://schemas.xmlsoap.org/soap/envelope/" '
                's:encodingStyle="http://schemas.xmlsoap.org/soap/encoding/"><s:Body>'
                f'<u:{action} xmlns:u="{service}">{body}</u:{action}>'
                '</s:Body></s:Envelope>')
    req = urllib.request.Request(control_url, data=envelope.encode(), method="POST", headers={
        "Content-Type": 'text/xml; charset="utf-8"', "SOAPAction": f'"{service}#{action}"',
        "User-Agent": USER_AGENT})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            text = r.read()
    except urllib.error.HTTPError as e:              # UPnP errors come back as HTTP 500 with a code
        detail = e.read()
        code = re.search(rb"<errorCode>(\d+)</errorCode>", detail)
        desc = re.search(rb"<errorDescription>([^<]*)</errorDescription>", detail)
        raise CheckError(f"router said {(desc.group(1).decode() if desc else 'no')} "
                         f"(error {code.group(1).decode() if code else e.code})") from e
    out = {}
    for el in ET.fromstring(text).iter():
        tag = el.tag.split("}")[-1]
        if el.text and not list(el):
            out[tag] = el.text
    return out


def open_port(port, target=SSDP_ADDR, description="Campfire Server"):
    """Ask the router to forward TCP <port> to this PC. Returns a message; raises CheckError."""
    locations = discover(target=target)
    if not locations:
        raise CheckError("No router answered. UPnP may be turned off in your router's settings, "
                         "so the port has to be forwarded by hand.")
    errors = []
    for loc in locations:
        try:
            found = find_wan_service(loc)
            if not found:
                continue
            control, stype = found
            me = lan_ip()
            last = None
            for lease in (0, 7 * 86400):            # some routers only accept one of these
                try:
                    soap(control, stype, "AddPortMapping", {
                        "NewRemoteHost": "", "NewExternalPort": port, "NewProtocol": "TCP",
                        "NewInternalPort": port, "NewInternalClient": me, "NewEnabled": 1,
                        "NewPortMappingDescription": description, "NewLeaseDuration": lease})
                    last = None
                    break
                except CheckError as e:
                    last = e
            if last is not None:
                raise last
            try:
                ext = soap(control, stype, "GetExternalIPAddress").get("NewExternalIPAddress", "")
            except CheckError:
                ext = ""
            return f"Your router now forwards port {port} to this PC ({me})." + (
                f" Its internet address is {ext}." if ext else "")
        except CheckError as e:
            errors.append(str(e))
        except Exception as e:
            errors.append(f"{loc}: {e}")
    raise CheckError("The router refused: " + "; ".join(errors) if errors
                     else "Found a router, but it doesn't offer port forwarding over UPnP.")


# ---------------------------------------------------------------- Windows Firewall
def _ps(command, elevated=False):
    if sys.platform != "win32":
        raise CheckError("This only applies to Windows")
    if not elevated:
        flags = 0x08000000                                      # CREATE_NO_WINDOW
        return subprocess.run(["powershell", "-NoProfile", "-Command", command], capture_output=True,
                              text=True, creationflags=flags, timeout=30)
    import ctypes
    rc = ctypes.windll.shell32.ShellExecuteW(None, "runas", "powershell",
                                            f'-NoProfile -WindowStyle Hidden -Command "{command}"', None, 0)
    if rc <= 32:
        raise CheckError("Windows didn't allow it (the permission prompt was declined)")
    return None


def programs():
    """The program(s) that run the server: Campfire Server.exe, or python/pythonw."""
    exe = os.path.abspath(sys.executable)
    folder = os.path.dirname(exe)
    names = {os.path.basename(exe), "python.exe", "pythonw.exe"} if not getattr(sys, "frozen", False) else \
        {os.path.basename(exe)}
    return [os.path.join(folder, n) for n in sorted(names)]


def firewall_status(port):
    """'allowed', 'blocked' (a block rule for the program), 'unknown', or 'missing'."""
    if sys.platform != "win32":
        return "unknown"
    progs = ",".join(f"'{p}'" for p in programs())
    cmd = (f"$b = @({progs}) | ForEach-Object {{ Get-NetFirewallApplicationFilter -Program $_ -ErrorAction "
           "SilentlyContinue | Get-NetFirewallRule | Where-Object { $_.Enabled -eq 'True' -and $_.Direction -eq "
           "'Inbound' -and $_.Action -eq 'Block' } }; "
           f"$a = Get-NetFirewallRule -DisplayName '{RULE_NAME}' -ErrorAction SilentlyContinue; "
           "if ($b) { 'blocked' } elseif ($a) { 'allowed' } else { 'missing' }")
    try:
        out = _ps(cmd).stdout.strip().splitlines()
        return out[-1] if out and out[-1] in ("allowed", "blocked", "missing") else "unknown"
    except Exception:
        return "unknown"


def allow_through_firewall(port):
    """Remove Windows' block rules for the server program and allow the port (asks for admin)."""
    progs = ",".join(f"'{p}'" for p in programs())
    cmd = (f"@({progs}) | ForEach-Object {{ Get-NetFirewallApplicationFilter -Program $_ -ErrorAction "
           "SilentlyContinue | Get-NetFirewallRule | Where-Object { $_.Direction -eq 'Inbound' -and "
           "$_.Action -eq 'Block' } | Remove-NetFirewallRule }; "
           f"Remove-NetFirewallRule -DisplayName '{RULE_NAME}' -ErrorAction SilentlyContinue; "
           f"New-NetFirewallRule -DisplayName '{RULE_NAME}' -Direction Inbound -Action Allow -Protocol TCP "
           f"-LocalPort {int(port)} -Profile Any | Out-Null")
    _ps(cmd, elevated=True)
