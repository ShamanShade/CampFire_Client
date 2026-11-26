"""https on the server's port: a DuckDNS name + a certificate from an ACME server (Let's Encrypt's
protocol), then https / wss / the desktop app all on one port.

The ACME server here is a stand-in for Let's Encrypt that checks what the real one checks: every
request is signed by the account key (ES256 JWS), nonces are used once, URLs match, and the
DNS TXT record (set through a fake DuckDNS) proves the name. It then issues a certificate from a
throwaway CA, which the test trusts.
Run:  python tests/test_https.py"""

import asyncio
import base64
import datetime
import hashlib
import http.server
import json
import os
import socket
import ssl
import struct
import sys
import tempfile
import threading
import urllib.parse

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from cryptography import x509  # noqa: E402
from cryptography.hazmat.primitives import hashes, serialization  # noqa: E402
from cryptography.hazmat.primitives.asymmetric import ec  # noqa: E402
from cryptography.hazmat.primitives.asymmetric.utils import encode_dss_signature  # noqa: E402
from cryptography.x509.oid import NameOID  # noqa: E402

import certs  # noqa: E402
import server as core  # noqa: E402

TMP = tempfile.mkdtemp(prefix="cf_https_")
TOKEN = "1a2b3c4d-1111-2222-3333-444455556666"


def b64u(b):
    return base64.urlsafe_b64encode(b).rstrip(b"=").decode()


def unb64u(s):
    return base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))


# ------------------------------------------------------------------ a throwaway CA
ca_key = ec.generate_private_key(ec.SECP256R1())
ca_name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "Campfire Test CA")])
now = datetime.datetime.now(datetime.timezone.utc)
ca_cert = (x509.CertificateBuilder().subject_name(ca_name).issuer_name(ca_name).public_key(ca_key.public_key())
           .serial_number(1).not_valid_before(now - datetime.timedelta(days=1))
           .not_valid_after(now + datetime.timedelta(days=30))
           .add_extension(x509.BasicConstraints(ca=True, path_length=None), critical=True)
           .add_extension(x509.KeyUsage(digital_signature=True, key_cert_sign=True, crl_sign=True, content_commitment=False,
                                        key_encipherment=False, data_encipherment=False, key_agreement=False,
                                        encipher_only=False, decipher_only=False), critical=True)
           .add_extension(x509.SubjectKeyIdentifier.from_public_key(ca_key.public_key()), critical=False)
           .sign(ca_key, hashes.SHA256()))
CA_PEM = os.path.join(TMP, "ca.pem")
with open(CA_PEM, "wb") as f:
    f.write(ca_cert.public_bytes(serialization.Encoding.PEM))


# ------------------------------------------------------------------ fake DuckDNS
class Duck:
    txt = ""
    ip_updates = 0
    seen = []


class DuckHandler(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        q = urllib.parse.parse_qs(urllib.parse.urlsplit(self.path).query, keep_blank_values=True)
        Duck.seen.append(q)
        ok = q.get("token", [""])[0] == TOKEN and q.get("domains", [""])[0] == "test-camp"
        if ok and "txt" in q:
            Duck.txt = "" if q.get("clear") else q["txt"][0]
            body = "OK"
        elif ok and "ip" in q:
            Duck.ip_updates += 1
            body = "OK\n203.0.113.7\n\nUPDATED"
        else:
            body = "KO"
        self.send_response(200)
        self.end_headers()
        self.wfile.write(body.encode())

    def log_message(self, *a):
        pass


# ------------------------------------------------------------------ fake ACME (checks like Let's Encrypt)
class Acme:
    nonces = set()
    accounts = {}            # kid -> public key
    orders = {}
    errors = []
    issued = 0


def verify_jws(handler, body):
    jws = json.loads(body)
    protected = json.loads(unb64u(jws["protected"]))
    assert protected["alg"] == "ES256"
    assert protected["nonce"] in Acme.nonces, "bad nonce"
    Acme.nonces.discard(protected["nonce"])                    # each nonce works once
    assert protected["url"] == f"http://127.0.0.1:{handler.server.server_port}{handler.path}", "url mismatch"
    if "jwk" in protected:
        jwk = protected["jwk"]
        key = ec.EllipticCurvePublicNumbers(int.from_bytes(unb64u(jwk["x"]), "big"),
                                            int.from_bytes(unb64u(jwk["y"]), "big"), ec.SECP256R1()).public_key()
    else:
        key = Acme.accounts[protected["kid"]][0]
        jwk = Acme.accounts[protected["kid"]][1]
    sig = unb64u(jws["signature"])
    key.verify(encode_dss_signature(int.from_bytes(sig[:32], "big"), int.from_bytes(sig[32:], "big")),
               f"{jws['protected']}.{jws['payload']}".encode(), ec.ECDSA(hashes.SHA256()))
    payload = json.loads(unb64u(jws["payload"])) if jws["payload"] else None
    return protected, payload, key, jwk


class AcmeHandler(http.server.BaseHTTPRequestHandler):
    def base(self):
        return f"http://127.0.0.1:{self.server.server_port}"

    def reply(self, status, obj=None, headers=None, raw=None, ctype="application/json"):
        nonce = b64u(os.urandom(12))
        Acme.nonces.add(nonce)
        self.send_response(status)
        self.send_header("Replay-Nonce", nonce)
        for k, v in (headers or {}).items():
            self.send_header(k, v)
        body = raw if raw is not None else (json.dumps(obj).encode() if obj is not None else b"")
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        b = self.base()
        self.reply(200, {"newNonce": b + "/nonce", "newAccount": b + "/acct", "newOrder": b + "/order"})

    def do_HEAD(self):
        self.reply(200)

    def do_POST(self):
        try:
            body = self.rfile.read(int(self.headers["Content-Length"]))
            protected, payload, key, jwk = verify_jws(self, body)
            b, path = self.base(), self.path
            if path == "/acct":
                assert payload["termsOfServiceAgreed"] is True
                kid = f"{b}/acct/{len(Acme.accounts) + 1}"
                Acme.accounts[kid] = (key, jwk)
                return self.reply(201, {"status": "valid"}, {"Location": kid})
            assert "kid" in protected, "requests after sign-up must use the account id"
            if path == "/order":
                domain = payload["identifiers"][0]["value"]
                oid = str(len(Acme.orders) + 1)
                Acme.orders[oid] = {"domain": domain, "token": b64u(os.urandom(16)), "status": "pending",
                                    "auth": "pending", "jwk": jwk, "cert": None}
                return self.reply(201, self.order_obj(oid), {"Location": f"{b}/order/{oid}"})
            kind, oid = path.strip("/").split("/")[:2]
            o = Acme.orders[oid]
            if kind == "order":
                return self.reply(200, self.order_obj(oid))
            if kind == "authz":
                return self.reply(200, {"status": o["auth"], "identifier": {"type": "dns", "value": o["domain"]},
                                        "challenges": [{"type": "http-01", "url": f"{b}/chal-http/{oid}", "token": "x"},
                                                       {"type": "dns-01", "url": f"{b}/chal/{oid}", "token": o["token"]}]})
            if kind == "chal":
                thumb = b64u(hashlib.sha256(json.dumps(o["jwk"], sort_keys=True, separators=(",", ":")).encode()).digest())
                want = b64u(hashlib.sha256(f"{o['token']}.{thumb}".encode()).digest())
                o["auth"] = "valid" if Duck.txt == want else "invalid"      # what Let's Encrypt checks in DNS
                if o["auth"] == "valid":
                    o["status"] = "ready"
                return self.reply(200, {"status": "processing"})
            if kind == "finalize":
                assert o["status"] == "ready", "finalized before the name was proven"
                csr = x509.load_der_x509_csr(unb64u(payload["csr"]))
                assert csr.is_signature_valid
                names = csr.extensions.get_extension_for_class(x509.SubjectAlternativeName).value.get_values_for_type(x509.DNSName)
                assert names == [o["domain"]], names
                t = datetime.datetime.now(datetime.timezone.utc)
                cert = (x509.CertificateBuilder().subject_name(csr.subject).issuer_name(ca_name)
                        .public_key(csr.public_key()).serial_number(x509.random_serial_number())
                        .not_valid_before(t - datetime.timedelta(minutes=1)).not_valid_after(t + datetime.timedelta(days=90))
                        .add_extension(x509.SubjectAlternativeName([x509.DNSName(o["domain"])]), critical=False)
                        .add_extension(x509.AuthorityKeyIdentifier.from_issuer_public_key(ca_key.public_key()), critical=False)
                        .add_extension(x509.ExtendedKeyUsage([x509.oid.ExtendedKeyUsageOID.SERVER_AUTH]), critical=False)
                        .sign(ca_key, hashes.SHA256()))
                o["cert"] = cert.public_bytes(serialization.Encoding.PEM) + ca_cert.public_bytes(serialization.Encoding.PEM)
                o["status"] = "valid"
                Acme.issued += 1
                return self.reply(200, self.order_obj(oid))
            if kind == "cert":
                return self.reply(200, raw=o["cert"], ctype="application/pem-certificate-chain")
            self.reply(404, {"detail": "no"})
        except Exception as e:
            Acme.errors.append(repr(e))
            self.reply(400, {"type": "urn:ietf:params:acme:error:malformed", "detail": repr(e)})

    def order_obj(self, oid):
        b, o = self.base(), Acme.orders[oid]
        out = {"status": o["status"], "authorizations": [f"{b}/authz/{oid}"], "finalize": f"{b}/finalize/{oid}"}
        if o["cert"]:
            out["certificate"] = f"{b}/cert/{oid}"
        return out

    def log_message(self, *a):
        pass


def serve(handler):
    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv


# ------------------------------------------------------------------ the test
def tls_client(port):
    ctx = ssl.create_default_context(cafile=CA_PEM)
    raw = socket.create_connection(("127.0.0.1", port), 5)
    return ctx.wrap_socket(raw, server_hostname="test-camp.duckdns.org")


def https_get(port, path):
    s = tls_client(port)
    s.sendall(f"GET {path} HTTP/1.1\r\nHost: test-camp.duckdns.org\r\n\r\n".encode())
    data = b""
    while True:
        chunk = s.recv(65536)
        if not chunk:
            break
        data += chunk
    s.close()
    head, _, body = data.partition(b"\r\n\r\n")
    return head.decode(), body


def wss_login(port, user):
    s = tls_client(port)
    key = base64.b64encode(os.urandom(16)).decode()
    s.sendall((f"GET /ws HTTP/1.1\r\nHost: test-camp.duckdns.org\r\nUpgrade: websocket\r\nConnection: Upgrade\r\n"
               f"Sec-WebSocket-Key: {key}\r\nSec-WebSocket-Version: 13\r\nOrigin: https://shamanshade.github.io\r\n\r\n").encode())
    head = b""
    while b"\r\n\r\n" not in head:
        head += s.recv(1)
    assert head.startswith(b"HTTP/1.1 101"), head
    buf = b""

    def frames():
        nonlocal buf
        while True:
            while len(buf) < 2:
                buf += s.recv(65536)
            n = buf[1] & 0x7F
            off = 2
            if n == 126:
                while len(buf) < 4:
                    buf += s.recv(65536)
                n, off = struct.unpack("!H", buf[2:4])[0], 4
            while len(buf) < off + n:
                buf += s.recv(65536)
            data, buf = buf[off:off + n], buf[off + n:]
            yield json.loads(data)

    def send(obj):
        payload = json.dumps(obj).encode()
        mask = os.urandom(4)
        s.sendall(bytes([0x81, 0x80 | len(payload)]) + mask + bytes(b ^ mask[i % 4] for i, b in enumerate(payload)))

    it = frames()
    assert next(it)["t"] == "hello"
    send({"t": "login", "user": user, "pass": "secret"})
    for m in it:
        if m["t"] == "login_ok":
            return s, it, send


def desktop_hello(port):
    s = socket.create_connection(("127.0.0.1", port), 5)
    line = s.makefile("rb").readline()
    s.close()
    return json.loads(line)


def github_site(pnum):
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        print("browser part skipped (no Playwright)")
        return
    import base64 as b64
    import shutil as sh
    import subprocess
    sys.path.insert(0, os.path.join(ROOT, "tools"))
    sys.path.insert(0, ROOT)
    import build_site
    import voice
    site = os.path.join(TMP, "site")
    build_site.build(site, server=None)                       # like WEB_SERVER = "": the page asks
    web = subprocess.Popen([sys.executable, "-m", "http.server", "0", "--bind", "127.0.0.1", "--directory", site],
                           stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    line = web.stdout.readline()
    wport = int(line.split("port ")[1].split()[0])
    bob = socket.create_connection(("127.0.0.1", pnum), 5)
    bf = bob.makefile("rb")

    def bsend(**o):
        bob.sendall((json.dumps(o) + "\n").encode())

    def bwait(kind, **match):
        bob.settimeout(10)
        while True:
            m = json.loads(bf.readline())
            if m.get("t") == kind and all(m.get(k) == v for k, v in match.items()):
                return m

    json.loads(bf.readline())
    bsend(t="login", user="bob", **{"pass": "secret"})
    bwait("login_ok")
    chrome = sh.which("google-chrome") or sh.which("chromium")
    flags = ["--host-resolver-rules=MAP test-camp.duckdns.org 127.0.0.1", "--ignore-certificate-errors", "--no-proxy-server",
             "--use-fake-ui-for-media-stream", "--use-fake-device-for-media-stream",
             "--autoplay-policy=no-user-gesture-required"]
    errors = []
    try:
        with sync_playwright() as p:
            br = p.chromium.launch(executable_path=chrome, args=flags) if chrome else p.chromium.launch(args=flags)
            page = br.new_page()
            page.on("pageerror", lambda e: errors.append(str(e)))
            page.goto(f"http://127.0.0.1:{wport}/")
            assert page.is_visible("#server")                  # GitHub page: asks which server
            page.fill("#server", f"https://test-camp.duckdns.org:{pnum}/")
            page.fill("#user", "carol")
            page.fill("#pass", "secret")
            page.on("console", lambda m: errors.append("console: " + m.text) if m.type == "error" else None)
            page.click("#loginbtn")
            try:
                page.wait_for_selector("#app:not(.hidden)", timeout=10000)
            except Exception:
                raise AssertionError(f"login failed: {page.text_content('#loginerr')!r} {errors}")
            page.wait_for_function("window.Campfire.token")
            page.fill("#input", "hello from github")
            page.keyboard.press("Enter")
            assert bwait("msg", user="carol")["text"] == "hello from github"
            bsend(t="msg", room="#general", text="hi carol")
            page.wait_for_selector(".msg >> text=hi carol")
            # uploads and pictures go to the server's https address (another origin: CORS)
            png = os.path.join(TMP, "p.png")
            open(png, "wb").write(b64.b64decode(
                "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNk+M9QDwADhgGAWjR9awAAAABJRU5ErkJggg=="))
            page.set_input_files("#file", png)
            m = bwait("msg", user="carol")
            assert m["file"]["name"] == "p.png"
            img = page.wait_for_selector(f"img.media[src^='https://test-camp.duckdns.org:{pnum}/file/']")
            page.wait_for_function("(i) => i.complete && i.naturalWidth > 0", arg=img)
            # remembered for next time
            page.reload()
            page.wait_for_selector("#app:not(.hidden)")
            assert page.evaluate("localStorage.getItem('campfire.server')") == json.dumps(f"test-camp.duckdns.org:{pnum}")
            # voice over the https server
            page.evaluate("V.threshold = -200")
            page.click("#side .item >> text=Lounge")
            page.wait_for_selector("#voicebar")
            bsend(t="voice_join", channel="Lounge", muted=False, deaf=False)
            dec = voice.Decoder()
            got = 0
            for _ in range(20):
                m = bwait("voice_data", u="carol")
                got += sum(len(x) for x in dec.decode(b64.b64decode(m["d"])))
            assert got == 20 * 960
            page.click("#voicebar button[title='Leave voice']")
            br.close()
    finally:
        web.terminate()
        bob.close()
    assert not errors, errors
    print("github website over https ok")


async def main():
    duck, acme = serve(DuckHandler), serve(AcmeHandler)
    port = socket.socket()
    port.bind(("127.0.0.1", 0))
    pnum = port.getsockname()[1]
    port.close()
    args = core.parse_args(["--nogui", "--host", "127.0.0.1", "--port", str(pnum), "--db", os.path.join(TMP, "h.db"),
                            "--files", os.path.join(TMP, "files")])
    args.acme_test = {"directory": f"http://127.0.0.1:{acme.server_port}/dir",
                      "duck_url": f"http://127.0.0.1:{duck.server_port}/update", "wait_dns": lambda n, v: None}
    server, srv = await core.start(args)
    loop = asyncio.get_running_loop()
    try:
        assert not server.https_status()["https_ready"]
        # no certificate yet: an https attempt is simply refused, everything else works
        try:
            await loop.run_in_executor(None, https_get, pnum, "/")
            raise AssertionError("https worked without a certificate?")
        except (ssl.SSLError, ConnectionError, OSError):
            pass

        ok, text = await server.set_duckdns("nope!", TOKEN)
        assert not ok and "DuckDNS name" in text
        ok, text = await server.set_duckdns("test-camp", "not-a-token")
        assert not ok and "token" in text
        ok, text = await server.set_duckdns("https://Test-Camp.duckdns.org/", TOKEN)
        assert ok, (text, Acme.errors)
        assert "test-camp.duckdns.org" in text and Acme.issued == 1, text
        assert not Acme.errors, Acme.errors
        assert Duck.txt == "" and Duck.ip_updates >= 1              # TXT cleaned up afterwards
        st = server.https_status()
        assert st["https_ready"] and st["web_url"] == f"https://test-camp.duckdns.org:{pnum}" and st["https_days"] > 80
        assert server.config_payload()["web_url"] == st["web_url"]
        assert server.store.setting("duck_name") == "test-camp"

        # https, wss and the desktop app, all on the same port
        head, body = await loop.run_in_executor(None, https_get, pnum, "/")
        assert head.startswith("HTTP/1.1 200") and b"<title>Campfire</title>" in body, head
        assert "Access-Control-Allow-Origin: *" in head
        head, _ = await loop.run_in_executor(None, https_get, pnum, "/emoji.json")
        assert head.startswith("HTTP/1.1 200")
        sock, frames, send = await loop.run_in_executor(None, wss_login, pnum, "alice")
        hello = await loop.run_in_executor(None, desktop_hello, pnum)
        assert hello["t"] == "hello"

        # the website as it's published on GitHub (another address), talking to this server over https
        await loop.run_in_executor(None, github_site, pnum)

        # renewals: nothing to do while the certificate is fresh; forced, it gets a new one
        assert await server.renew_https() is None
        ok, text = await server.renew_https(force=True)
        assert ok and Acme.issued == 2
        head, _ = await loop.run_in_executor(None, https_get, pnum, "/")     # the new one is used
        assert head.startswith("HTTP/1.1 200")

        # a bad token is reported, the old certificate keeps working
        server.store.set_setting("duck_token", "ffffffff-ffff-ffff-ffff-ffffffffffff")
        ok, text = await server.renew_https(force=True)
        assert not ok and "DuckDNS" in text and server.https_status()["https_error"]
        head, _ = await loop.run_in_executor(None, https_get, pnum, "/")
        assert head.startswith("HTTP/1.1 200")
        sock.close()
        ok, text = await server.clear_duckdns()
        assert ok and not server.store.setting("duck_name")
        print("https tests passed")
    finally:
        srv.close()
        await server.shutdown()


if __name__ == "__main__":
    asyncio.run(main())
