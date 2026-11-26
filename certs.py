"""
https for Campfire Server: a free DuckDNS name + a free Let's Encrypt certificate.

Why: the website on GitHub Pages is https, and an https page may only talk to an https server
(and browsers only allow the microphone on https). So the server gets a real certificate for a
name like campfire-shaman.duckdns.org, on the same port as everything else (55555).

- DuckDNS (https://www.duckdns.org, sign in with GitHub) gives a free name and a token. The server
  keeps the name pointed at this network's public IP (it checks every few minutes), so it keeps
  working if the home IP changes.
- Let's Encrypt gives the certificate. We prove we own the name with a DNS TXT record (dns-01),
  set through the DuckDNS token, so no extra router ports are needed. Certificates last 90 days;
  the server renews them a month early, by itself.

This is a small ACME (RFC 8555) client using the "cryptography" package.
"""

from __future__ import annotations

import base64
import datetime
import hashlib
import json
import logging
import os
import re
import ssl
import time
import urllib.error
import urllib.parse
import urllib.request

log = logging.getLogger("campfire.certs")

LETSENCRYPT = "https://acme-v02.api.letsencrypt.org/directory"
DUCKDNS = "https://www.duckdns.org/update"
RENEW_DAYS = 30                       # renew when less than this many days are left
NAME_RE = re.compile(r"^[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?$")
TOKEN_RE = re.compile(r"^[0-9a-fA-F-]{36}$")


class CertError(Exception):
    pass


def b64u(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def duck_name(text: str) -> str:
    """'Campfire-Shaman', 'campfire-shaman.duckdns.org' or a URL -> 'campfire-shaman' ('' if invalid)."""
    text = text.strip().lower()
    text = re.sub(r"^https?://", "", text).split("/")[0].split(":")[0]
    if text.endswith(".duckdns.org"):
        text = text[: -len(".duckdns.org")]
    return text if NAME_RE.match(text) else ""


# ------------------------------------------------------------------ DuckDNS
class DuckDNS:
    def __init__(self, name: str, token: str, url: str = DUCKDNS, opener=None):
        self.name, self.token, self.url = name, token, url
        self.domain = f"{name}.duckdns.org"
        self.opener = opener or urllib.request.urlopen

    def call(self, **params) -> str:
        q = urllib.parse.urlencode({"domains": self.name, "token": self.token, **params})
        try:
            with self.opener(f"{self.url}?{q}", timeout=20) as r:
                return r.read().decode(errors="replace").strip()
        except Exception as e:
            raise CertError(f"Couldn't reach DuckDNS ({e})") from e

    def update_ip(self) -> str:
        """Point the name at our public IP (DuckDNS sees it). Returns the IP."""
        answer = self.call(ip="", verbose="true")
        lines = answer.splitlines()
        if not lines or lines[0] != "OK":
            raise CertError("DuckDNS said no - check the name and token (from duckdns.org)")
        return lines[1] if len(lines) > 1 else ""

    def set_txt(self, value: str):
        if self.call(txt=value, verbose="true").splitlines()[0] != "OK":
            raise CertError("DuckDNS wouldn't set the TXT record - check the token")

    def clear_txt(self):
        try:
            self.call(txt="", clear="true")
        except CertError:
            pass


# ------------------------------------------------------------------ keys
def _crypto():
    try:
        from cryptography import x509
        from cryptography.hazmat.primitives import hashes, serialization
        from cryptography.hazmat.primitives.asymmetric import ec
        from cryptography.hazmat.primitives.asymmetric.utils import decode_dss_signature
        from cryptography.x509.oid import NameOID
    except ImportError as e:
        raise CertError("The 'cryptography' package is missing (pip install cryptography)") from e
    return x509, hashes, serialization, ec, decode_dss_signature, NameOID


def load_or_make_key(path):
    _x509, _hashes, serialization, ec, _d, _n = _crypto()
    if os.path.exists(path):
        with open(path, "rb") as f:
            return serialization.load_pem_private_key(f.read(), password=None)
    key = ec.generate_private_key(ec.SECP256R1())
    write_private(path, key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                                          serialization.NoEncryption()))
    return key


def write_private(path, data: bytes):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "wb") as f:
        f.write(data)
    os.replace(tmp, path)


def cert_expiry(path) -> float | None:
    """When the certificate at path expires (unix time), or None."""
    try:
        x509 = _crypto()[0]
        with open(path, "rb") as f:
            cert = x509.load_pem_x509_certificate(f.read())
        end = getattr(cert, "not_valid_after_utc", None) or cert.not_valid_after.replace(tzinfo=datetime.timezone.utc)
        return end.timestamp()
    except Exception:
        return None


def cert_names(path) -> list[str]:
    try:
        x509 = _crypto()[0]
        with open(path, "rb") as f:
            cert = x509.load_pem_x509_certificate(f.read())
        return cert.extensions.get_extension_for_class(x509.SubjectAlternativeName).value.get_values_for_type(x509.DNSName)
    except Exception:
        return []


# ------------------------------------------------------------------ ACME
class Acme:
    def __init__(self, directory: str, account_key, cafile: str | None = None):
        self.directory_url = directory
        self.key = account_key
        self.ctx = ssl.create_default_context(cafile=cafile) if cafile else None
        self.kid = None
        self.nonce = None
        self.dir = self.get_json(directory)

    # -- HTTP
    def request(self, url, data=None, headers=None, method=None):
        req = urllib.request.Request(url, data=data, headers={"User-Agent": "Campfire-Server", **(headers or {})},
                                     method=method)
        try:
            r = urllib.request.urlopen(req, timeout=30, context=self.ctx)
            body, status, hdrs = r.read(), r.status, r.headers
        except urllib.error.HTTPError as e:
            body, status, hdrs = e.read(), e.code, e.headers
        except Exception as e:
            raise CertError(f"Couldn't reach Let's Encrypt ({e})") from e
        if hdrs.get("Replay-Nonce"):
            self.nonce = hdrs["Replay-Nonce"]
        return status, hdrs, body

    def get_json(self, url):
        status, _h, body = self.request(url)
        if status != 200:
            raise CertError(f"Let's Encrypt answered {status}")
        return json.loads(body)

    def jwk(self):
        nums = self.key.public_key().public_numbers()
        return {"crv": "P-256", "kty": "EC", "x": b64u(nums.x.to_bytes(32, "big")), "y": b64u(nums.y.to_bytes(32, "big"))}

    def thumbprint(self):
        return b64u(hashlib.sha256(json.dumps(self.jwk(), sort_keys=True, separators=(",", ":")).encode()).digest())

    def sign(self, data: bytes) -> bytes:
        _x, hashes, _s, ec, decode_dss_signature, _n = _crypto()
        r, s = decode_dss_signature(self.key.sign(data, ec.ECDSA(hashes.SHA256())))
        return r.to_bytes(32, "big") + s.to_bytes(32, "big")

    def post(self, url, payload, tries=3):
        """Signed POST. payload None = POST-as-GET."""
        for attempt in range(tries):
            if not self.nonce:
                self.request(self.dir["newNonce"], method="HEAD")
            protected = {"alg": "ES256", "nonce": self.nonce, "url": url}
            if self.kid:
                protected["kid"] = self.kid
            else:
                protected["jwk"] = self.jwk()
            self.nonce = None
            p64 = b64u(json.dumps(protected).encode())
            b64 = "" if payload is None else b64u(json.dumps(payload).encode())
            body = json.dumps({"protected": p64, "payload": b64,
                               "signature": b64u(self.sign(f"{p64}.{b64}".encode()))}).encode()
            status, hdrs, data = self.request(url, body, {"Content-Type": "application/jose+json"})
            if status == 400 and b"badNonce" in data and attempt < tries - 1:
                continue
            if status >= 400:
                try:
                    detail = json.loads(data).get("detail", "")
                except ValueError:
                    detail = data[:200].decode(errors="replace")
                raise CertError(f"Let's Encrypt said: {detail or status}")
            return status, hdrs, data
        raise CertError("Let's Encrypt kept refusing our requests")

    def post_json(self, url, payload):
        status, hdrs, data = self.post(url, payload)
        return hdrs, (json.loads(data) if data else {})

    def poll(self, url, done=("valid",), bad=("invalid",), timeout=180):
        end = time.time() + timeout
        while True:
            _h, obj = self.post_json(url, None)
            status = obj.get("status")
            if status in done:
                return obj
            if status in bad:
                err = (obj.get("error") or {}).get("detail") or json.dumps(obj.get("challenges", obj))[:300]
                raise CertError(f"Let's Encrypt couldn't verify the name: {err}")
            if time.time() > end:
                raise CertError("Let's Encrypt took too long")
            time.sleep(2)

    # -- the whole thing
    def obtain(self, domain: str, dns: DuckDNS, cert_key, wait_dns=None, progress=lambda text: None) -> bytes:
        x509, hashes, serialization, _ec, _d, NameOID = _crypto()
        progress("Signing up with Let's Encrypt")
        hdrs, _acct = self.post_json(self.dir["newAccount"], {"termsOfServiceAgreed": True})
        self.kid = hdrs["Location"]
        progress(f"Asking for a certificate for {domain}")
        hdrs, order = self.post_json(self.dir["newOrder"], {"identifiers": [{"type": "dns", "value": domain}]})
        order_url = hdrs["Location"]
        try:
            for auth_url in order["authorizations"]:
                _h, auth = self.post_json(auth_url, None)
                if auth.get("status") == "valid":
                    continue
                chal = next((c for c in auth["challenges"] if c["type"] == "dns-01"), None)
                if chal is None:
                    raise CertError("Let's Encrypt didn't offer a DNS check")
                txt = b64u(hashlib.sha256(f"{chal['token']}.{self.thumbprint()}".encode()).digest())
                progress("Proving the name is yours (DuckDNS TXT record)")
                dns.set_txt(txt)
                (wait_dns or wait_for_txt)(f"_acme-challenge.{domain}", txt)
                self.post_json(chal["url"], {})
                progress("Waiting for Let's Encrypt to check it")
                self.poll(auth_url)
        finally:
            dns.clear_txt()
        csr = (x509.CertificateSigningRequestBuilder()
               .subject_name(x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, domain)]))
               .add_extension(x509.SubjectAlternativeName([x509.DNSName(domain)]), critical=False)
               .sign(cert_key, hashes.SHA256()))
        progress("Getting the certificate")
        self.post_json(order["finalize"], {"csr": b64u(csr.public_bytes(serialization.Encoding.DER))})
        order = self.poll(order_url, done=("valid",), bad=("invalid",))
        _s, _h, pem = self.post(order["certificate"], None)
        return pem


def wait_for_txt(name, value, timeout=180):
    """Wait until public DNS shows the TXT record (Let's Encrypt would fail otherwise)."""
    end = time.time() + timeout
    url = f"https://dns.google/resolve?name={urllib.parse.quote(name)}&type=TXT"
    while time.time() < end:
        try:
            with urllib.request.urlopen(urllib.request.Request(url, headers={"Cache-Control": "no-cache"}), timeout=15) as r:
                answers = json.loads(r.read()).get("Answer", [])
            if any(a.get("data", "").strip('"') == value for a in answers):
                time.sleep(5)                               # let the other DNS servers catch up too
                return
        except Exception:
            pass
        time.sleep(5)
    log.warning("couldn't see the TXT record in public DNS yet; trying anyway")


# ------------------------------------------------------------------ what the server calls
class CertManager:
    """Keeps tls/cert.pem + tls/key.pem valid for the DuckDNS name, and the name pointed at us."""

    def __init__(self, folder: str, directory: str = LETSENCRYPT, cafile=None, duck_url: str = DUCKDNS,
                 wait_dns=None):
        self.folder = folder
        self.directory, self.cafile, self.duck_url, self.wait_dns = directory, cafile, duck_url, wait_dns
        self.cert_path = os.path.join(folder, "cert.pem")
        self.key_path = os.path.join(folder, "key.pem")
        self.account_path = os.path.join(folder, "account.key")

    def has_cert_for(self, domain) -> bool:
        return os.path.exists(self.key_path) and domain in cert_names(self.cert_path)

    def days_left(self) -> float | None:
        end = cert_expiry(self.cert_path)
        return None if end is None else (end - time.time()) / 86400

    def needs_renewal(self, domain) -> bool:
        left = self.days_left()
        return not self.has_cert_for(domain) or left is None or left < RENEW_DAYS

    def context(self) -> ssl.SSLContext | None:
        if not (os.path.exists(self.cert_path) and os.path.exists(self.key_path)):
            return None
        ctx = ssl.create_default_context(ssl.Purpose.CLIENT_AUTH)
        ctx.load_cert_chain(self.cert_path, self.key_path)
        return ctx

    def renew(self, name, token, progress=lambda text: None) -> str:
        """Point the name at us and get a fresh certificate. Returns a summary. Runs for a minute or two."""
        dns = DuckDNS(name, token, self.duck_url)
        progress(f"Pointing {dns.domain} at this network")
        ip = dns.update_ip()
        account = load_or_make_key(self.account_path)
        acme = Acme(self.directory, account, self.cafile)
        cert_key = load_or_make_key(self.key_path + ".new")
        pem = acme.obtain(dns.domain, dns, cert_key, self.wait_dns, progress)
        _x, _h, serialization, _ec, _d, _n = _crypto()
        os.makedirs(self.folder, exist_ok=True)
        with open(self.cert_path + ".tmp", "wb") as f:
            f.write(pem)
        os.replace(self.key_path + ".new", self.key_path)
        os.replace(self.cert_path + ".tmp", self.cert_path)
        days = self.days_left() or 0
        return f"Certificate for {dns.domain} is ready ({int(days)} days, renews itself)" + (f" - name points at {ip}" if ip else "")
