"""'Can friends connect?' check and the router/firewall fixes, against a fake router (UPnP) and a
fake reachability service. Run:  QT_QPA_PLATFORM=offscreen python tests/test_netcheck.py"""

import http.server
import json
import os
import re
import socket
import sys
import threading

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "tests"))

import netcheck  # noqa: E402

PUBLIC = "203.0.113.7"
mappings, router_mode = [], {"mode": "picky"}


def serve_http(handler_cls):
    httpd = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler_cls)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    return httpd


class Reach(http.server.BaseHTTPRequestHandler):
    """Like ifconfig.co/port/N: tries to connect back to the port."""
    def log_message(self, *a):
        pass

    def do_GET(self):
        port = int(self.path.rsplit("/", 1)[1])
        try:
            socket.create_connection(("127.0.0.1", port), 1).close()
            ok = True
        except OSError:
            ok = False
        body = json.dumps({"ip": PUBLIC, "port": port, "reachable": ok}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(body)


class Router(http.server.BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def do_GET(self):
        xml = ('<?xml version="1.0"?><root xmlns="urn:schemas-upnp-org:device-1-0"><device>'
               '<deviceType>urn:schemas-upnp-org:device:InternetGatewayDevice:1</deviceType><deviceList><device>'
               '<deviceList><device><serviceList><service>'
               '<serviceType>urn:schemas-upnp-org:service:WANIPConnection:1</serviceType>'
               '<controlURL>/ctl/IPConn</controlURL></service></serviceList></device></deviceList>'
               '</device></deviceList></device></root>').encode()
        self.send_response(200)
        self.end_headers()
        self.wfile.write(xml)

    def do_POST(self):
        body = self.rfile.read(int(self.headers["Content-Length"])).decode()
        action = self.headers["SOAPAction"].strip('"').split("#")[1]
        fields = dict(re.findall(r"<(New\w+)>([^<]*)</New\w+>", body))
        if action == "AddPortMapping":
            if router_mode["mode"] == "refuse" or (router_mode["mode"] == "picky" and fields["NewLeaseDuration"] == "0"):
                code = "718" if router_mode["mode"] == "refuse" else "402"
                err = (f'<s:Envelope xmlns:s="http://schemas.xmlsoap.org/soap/envelope/"><s:Body><s:Fault><detail>'
                       f'<UPnPError><errorCode>{code}</errorCode><errorDescription>'
                       f'{"ConflictInMappingEntry" if code == "718" else "Invalid Args"}</errorDescription>'
                       f'</UPnPError></detail></s:Fault></s:Body></s:Envelope>').encode()
                self.send_response(500)
                self.end_headers()
                self.wfile.write(err)
                return
            mappings.append(fields)
            out = ""
        else:
            out = f"<NewExternalIPAddress>{PUBLIC}</NewExternalIPAddress>"
        resp = (f'<s:Envelope xmlns:s="http://schemas.xmlsoap.org/soap/envelope/"><s:Body>'
                f'<u:{action}Response xmlns:u="urn:schemas-upnp-org:service:WANIPConnection:1">{out}'
                f'</u:{action}Response></s:Body></s:Envelope>').encode()
        self.send_response(200)
        self.end_headers()
        self.wfile.write(resp)


def fake_ssdp(location):
    """Answers M-SEARCH like a router would (on a normal UDP port instead of multicast)."""
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.bind(("127.0.0.1", 0))

    def run():
        while True:
            data, addr = sock.recvfrom(4096)
            if b"M-SEARCH" in data:
                sock.sendto(f"HTTP/1.1 200 OK\r\nST: x\r\nLOCATION: {location}\r\n\r\n".encode(), addr)

    threading.Thread(target=run, daemon=True).start()
    return sock.getsockname()


def free_udp():
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()


def main():
    reach = serve_http(Reach)
    reach_url = f"http://127.0.0.1:{reach.server_address[1]}/port/{{port}}"
    router = serve_http(Router)
    ssdp = fake_ssdp(f"http://127.0.0.1:{router.server_address[1]}/desc.xml")

    # reachability: closed port, then open
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    port = listener.getsockname()[1]
    assert netcheck.check_from_internet(port, reach_url) == {"ip": PUBLIC, "reachable": False}
    listener.listen()
    assert netcheck.check_from_internet(port, reach_url) == {"ip": PUBLIC, "reachable": True}
    try:
        netcheck.check_from_internet(port, "http://127.0.0.1:9/nothing/{port}")
        raise AssertionError("should fail")
    except netcheck.CheckError as e:
        assert "checking service" in str(e)

    # UPnP: finds the router, retries with a lease when 0 is refused, reports the public address
    msg = netcheck.open_port(55555, target=ssdp)
    assert "forwards port 55555" in msg and PUBLIC in msg, msg
    assert mappings[-1]["NewInternalClient"] == netcheck.lan_ip() and mappings[-1]["NewLeaseDuration"] != "0"
    assert mappings[-1]["NewExternalPort"] == "55555" and mappings[-1]["NewProtocol"] == "TCP"
    router_mode["mode"] = "refuse"
    try:
        netcheck.open_port(55555, target=ssdp)
        raise AssertionError("should fail")
    except netcheck.CheckError as e:
        assert "ConflictInMappingEntry" in str(e), e
    try:
        netcheck.open_port(55555, target=free_udp())
        raise AssertionError("should fail")
    except netcheck.CheckError as e:
        assert "No router answered" in str(e)

    # the server window shows the right verdicts
    from test_gui import TMP, pump  # noqa: E402
    from PyQt6.QtCore import QSettings
    from PyQt6.QtWidgets import QApplication, QMessageBox
    import campfire_config
    import server as core
    import server_gui
    app = QApplication(sys.argv)
    server_gui.prefs = QSettings(os.path.join(TMP, "netcheck.ini"), QSettings.Format.IniFormat)
    QMessageBox.question = staticmethod(lambda *a, **k: QMessageBox.StandardButton.Yes)
    from test_gui import free_port
    sport = free_port()
    w = server_gui.ServerWindow(core.parse_args(["--port", str(sport), "--db", os.path.join(TMP, "nc.db"),
                                                 "--files", os.path.join(TMP, "nc_files")]))
    pump(lambda: w.st.running)
    w.tabs.setCurrentIndex([w.tabs.tabText(i) for i in range(w.tabs.count())].index("Internet"))
    assert str(campfire_config.SERVER_PORT) in w.net_info.text()
    netcheck.REACH_URL = reach_url
    orig = netcheck.check_from_internet
    netcheck.check_from_internet = lambda p, url=reach_url: orig(p, url)

    campfire_config.SERVER_HOST = PUBLIC              # the app's address matches; the port is open
    w.run_net_test()
    pump(lambda: w.test_btn.isEnabled() and "Friends can connect" in w.net_result.text(), timeout=10)

    campfire_config.SERVER_HOST = "198.51.100.1"      # address changed since the app was built
    w.run_net_test()
    pump(lambda: w.test_btn.isEnabled() and "has changed" in w.net_result.text(), timeout=10)
    assert PUBLIC in w.net_result.text() and "Friends can connect" not in w.net_result.text()

    netcheck.check_from_internet = lambda p, url=None: {"ip": PUBLIC, "reachable": False}
    campfire_config.SERVER_HOST = PUBLIC              # port not reachable: next steps + by-hand help
    w.run_net_test()
    pump(lambda: w.test_btn.isEnabled() and "can't be reached" in w.net_result.text(), timeout=10)
    assert "Open the port on my router" in w.net_result.text() and "Port forwarding" in w.net_help.text()

    netcheck.open_port = lambda p: (_ for _ in ()).throw(netcheck.CheckError("No router answered."))
    w.run_upnp()
    pump(lambda: w.upnp_btn.isEnabled() and "Couldn't open the port automatically" in w.net_result.text())

    w.close()
    print("netcheck tests passed")


if __name__ == "__main__":
    main()
