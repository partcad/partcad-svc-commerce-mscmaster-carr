#
# partcad-svc-commerce-mcmaster, 2026
#
# Licensed under Apache License, Version 2.0.
#
"""A stand-in for McMaster-Carr's Product Information API, to test the plugin against.

McMaster-Carr's API is for its approved customers, so nobody can try this
package against it without an account, a certificate and McMaster-Carr's
consent. This server answers the same requests the same way - as McMaster-
Carr's own documentation and Postman examples show them - over HTTPS that
demands a client certificate, from a catalogue of its own (mock/catalog.json,
with CAD files under mock/cad/ that are this repository's drawings, not
McMaster-Carr's). The plugin is pointed at it with its 'api' and 'ca'
parameters, and does not know the difference.

    python mock_mcmaster.py                 # https://127.0.0.1:8443, certificates in ./mock-certs
    python mock_mcmaster.py --port 9443 --certs /tmp/certs

The first run makes the certificates: a certificate authority, the server's
certificate (for 127.0.0.1 and localhost), and a client certificate the
authority signed, as a PKCS#12 file with a password - which is what McMaster-
Carr sends a customer. It prints the parameters to give the plugin.

What it answers, as McMaster-Carr does:

    POST   /v1/login                  {"UserName", "Password"} -> {"AuthToken", "ExpirationTS"}
    POST   /v1/logout                 204
    PUT    /v1/products               {"URL": "https://mcmaster.com/<part>"}: subscribe;
                                      201 the first time, 200 after, with the part's information
    DELETE /v1/products               unsubscribe: 204, or 404 when not subscribed
    GET    /v1/products/<part>        the information of a part subscribed to (403 otherwise)
    GET    /v1/products/<part>/price  [{"Amount", "MinimumQuantity", "UnitOfMeasure"}]
    GET    /v1/cad/<path>             a CAD file a part's 'Links' name
    GET    /v1/images/<path>          an image a part's 'Links' name

Every error is {"ErrorCode", "ErrorMessage", "ErrorDescription"}: BAD_REQUEST,
LOGIN_FAILED, EXPIRED_AUTHORIZATION_TOKEN, NOT_SUBSCRIBED_TO_PRODUCT. A
connection without a certificate the authority signed is refused in the TLS
handshake, before any of it. The limits McMaster-Carr does not publish -
subscriptions an account may hold, and add a day - are 'max_subscriptions' and
'max_subscriptions_per_day' of the catalogue, so that running into one can be
tested too.
"""

import argparse
import datetime
import json
import os
import re
import secrets
import ssl
import threading
import time
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

HERE = os.path.dirname(os.path.abspath(__file__))
CATALOG = os.path.join(HERE, "mock", "catalog.json")

# The account the mock knows, and the password of the client certificate it makes.
USERNAME = "partcad@example.com"
PASSWORD = "mock-password"
CERTIFICATE_PASSWORD = "mock-certificate"
TOKEN_LIFETIME = 24 * 3600


def error(code, message, description):
    return code, {"ErrorCode": code, "ErrorMessage": message, "ErrorDescription": description}


class Catalog:
    """The parts the mock sells, and the state of the account: its tokens and subscriptions."""

    def __init__(self, path=CATALOG, token_lifetime=TOKEN_LIFETIME):
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
        self.root = os.path.dirname(os.path.abspath(path))
        self.parts = {k.upper(): v for k, v in data["parts"].items()}
        self.max_subscriptions = data.get("max_subscriptions", 1000)
        self.max_per_day = data.get("max_subscriptions_per_day", 500)
        self.token_lifetime = token_lifetime
        self.tokens = {}  # token: expiry
        self.subscribed = []  # part numbers, in order
        self.added_today = 0
        self.lock = threading.Lock()
        self.log = []  # (method, path), for the tests

    # -- the account --

    def login(self, body):
        if not isinstance(body, dict) or body.get("UserName") != USERNAME or body.get("Password") != PASSWORD:
            return error(401, "LOGIN_FAILED", "Incorrect username or password.")
        token = secrets.token_hex(16)
        expires = time.time() + self.token_lifetime
        with self.lock:
            self.tokens[token] = expires
        stamp = datetime.datetime.fromtimestamp(expires, datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f0Z")
        return 200, {"AuthToken": token, "ExpirationTS": stamp}

    def authorized(self, headers):
        """None when the request carries a live token; otherwise the error to answer."""
        said = headers.get("Authorization") or ""
        token = said[len("Bearer ") :] if said.startswith("Bearer ") else None
        with self.lock:
            expires = self.tokens.get(token)
        if expires is None or expires < time.time():
            return error(403, "EXPIRED_AUTHORIZATION_TOKEN", "The authorization token has expired.")
        return None

    def logout(self, headers):
        said = headers.get("Authorization") or ""
        with self.lock:
            self.tokens.pop(said[len("Bearer ") :], None)
        return 204, None

    # -- the parts --

    @staticmethod
    def part_of(url):
        """The part number in a product's address: 'https://mcmaster.com/91290A115' is 91290A115."""
        if not isinstance(url, str):
            return None
        path = urllib.parse.urlsplit(url).path.strip("/")
        return path.upper() or None

    def info(self, number):
        part = self.parts[number]
        info = {k: v for k, v in part.items() if k not in ("Price", "Files")}
        info["PartNumber"] = number
        return info

    def subscribe(self, body):
        number = self.part_of((body or {}).get("URL"))
        if number not in self.parts:
            return error(400, "BAD_REQUEST", "Invalid part number: %s" % (number or (body or {}).get("URL")))
        with self.lock:
            if number in self.subscribed:
                return 200, self.info(number)
            if len(self.subscribed) >= self.max_subscriptions or self.added_today >= self.max_per_day:
                return error(400, "BAD_REQUEST", "Subscription limit reached.")
            self.subscribed.append(number)
            self.added_today += 1
        return 201, self.info(number)

    def unsubscribe(self, body):
        number = self.part_of((body or {}).get("URL"))
        with self.lock:
            if number not in self.subscribed:
                return error(404, "NOT_FOUND", "Not subscribed to %s." % number)
            self.subscribed.remove(number)
        return 204, None

    def subscribed_part(self, number):
        number = (number or "").upper()
        if number not in self.parts:
            return None, error(400, "BAD_REQUEST", "Invalid part number: %s" % number)
        with self.lock:
            if number not in self.subscribed:
                return None, error(403, "NOT_SUBSCRIBED_TO_PRODUCT", "Not subscribed to product %s." % number)
        return number, None

    def file(self, kind, path):
        """(status, bytes) of a file a part's 'Links' name, for a part subscribed to."""
        wanted = "/v1/%s/%s" % (kind, path)
        for number, part in self.parts.items():
            for link in part.get("Links") or []:
                if urllib.parse.unquote(link.get("Value") or "") == urllib.parse.unquote(wanted):
                    _, refused = self.subscribed_part(number)
                    if refused:
                        return refused
                    source = (part.get("Files") or {}).get(link["Value"])
                    if not source:
                        return error(400, "BAD_REQUEST", "Invalid file path requested")
                    with open(os.path.join(self.root, source), "rb") as f:
                        return 200, f.read()
        return error(400, "BAD_REQUEST", "Invalid file path requested")

    # -- the dispatch --

    def answer(self, method, path, headers, body):
        """(status, JSON-able body or bytes or None) for one request."""
        with self.lock:
            self.log.append((method, path))
        path = urllib.parse.urlsplit(path).path
        if method == "POST" and path == "/v1/login":
            return self.login(body)
        refused = self.authorized(headers)
        if refused:
            return refused
        if method == "POST" and path == "/v1/logout":
            return self.logout(headers)
        if path == "/v1/products" and method == "PUT":
            return self.subscribe(body)
        if path == "/v1/products" and method == "DELETE":
            return self.unsubscribe(body)
        match = re.fullmatch(r"/v1/products/([^/]+)(/price)?", path)
        if match and method == "GET":
            number, refused = self.subscribed_part(urllib.parse.unquote(match.group(1)))
            if refused:
                return refused
            return 200, (self.parts[number].get("Price") or []) if match.group(2) else self.info(number)
        match = re.fullmatch(r"/v1/(cad|images|datasheets)/(.+)", path)
        if match and method == "GET":
            return self.file(match.group(1), match.group(2))
        return error(404, "NOT_FOUND", "No such resource: %s %s" % (method, path))


class Handler(BaseHTTPRequestHandler):
    server_version = "McMasterCarrMock/1"
    protocol_version = "HTTP/1.1"

    def _serve(self, method):
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length) if length else b""
        try:
            body = json.loads(raw.decode("utf-8")) if raw else None
        except ValueError:
            status, answer = error(400, "BAD_REQUEST", "The body is not JSON.")
        else:
            status, answer = self.server.catalog.answer(method, self.path, self.headers, body)
        if answer is None:
            payload, kind = b"", None
        elif isinstance(answer, bytes):
            payload, kind = answer, "application/octet-stream"
        else:
            payload, kind = json.dumps(answer).encode("utf-8"), "application/json; charset=utf-8"
        self.send_response(status)
        if kind:
            self.send_header("Content-Type", kind)
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def do_GET(self):
        self._serve("GET")

    def do_POST(self):
        self._serve("POST")

    def do_PUT(self):
        self._serve("PUT")

    def do_DELETE(self):
        self._serve("DELETE")

    def log_message(self, format, *args):
        if self.server.verbose:
            super().log_message(format, *args)


# --- the certificates ----------------------------------------------------------------


def make_certificates(directory, password=CERTIFICATE_PASSWORD):
    """A certificate authority, a server certificate and a PKCS#12 client certificate, in 'directory'.

    Returns {'ca', 'server_cert', 'server_key', 'client'}: the paths. Made once;
    a directory that has them is used as it is.
    """
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import ec
    from cryptography.hazmat.primitives.serialization import pkcs12
    from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID

    paths = {
        "ca": os.path.join(directory, "ca.pem"),
        "server_cert": os.path.join(directory, "server.pem"),
        "server_key": os.path.join(directory, "server.key"),
        "client": os.path.join(directory, "client.pfx"),
    }
    if all(os.path.exists(p) for p in paths.values()):
        return paths
    os.makedirs(directory, exist_ok=True)
    now = datetime.datetime.now(datetime.timezone.utc)

    def name(common):
        return x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, common)])

    def certificate(subject, key, issuer, issuer_key, *extensions):
        # The identifiers of both keys, which a strict verifier (Python 3.13's default) requires.
        extensions += (
            (x509.SubjectKeyIdentifier.from_public_key(key.public_key()), False),
            (x509.AuthorityKeyIdentifier.from_issuer_public_key(issuer_key.public_key()), False),
        )
        builder = (
            x509.CertificateBuilder()
            .subject_name(subject)
            .issuer_name(issuer)
            .public_key(key.public_key())
            .serial_number(x509.random_serial_number())
            .not_valid_before(now - datetime.timedelta(days=1))
            .not_valid_after(now + datetime.timedelta(days=3650))
        )
        for extension, critical in extensions:
            builder = builder.add_extension(extension, critical=critical)
        return builder.sign(issuer_key, hashes.SHA256())

    ca_key = ec.generate_private_key(ec.SECP256R1())
    ca = certificate(
        name("McMaster-Carr mock CA"),
        ca_key,
        name("McMaster-Carr mock CA"),
        ca_key,
        (x509.BasicConstraints(ca=True, path_length=None), True),
        (x509.KeyUsage(False, False, False, False, False, True, True, False, False), True),
    )
    server_key = ec.generate_private_key(ec.SECP256R1())
    server = certificate(
        name("localhost"),
        server_key,
        ca.subject,
        ca_key,
        (x509.SubjectAlternativeName([x509.DNSName("localhost"), x509.IPAddress(_ip("127.0.0.1"))]), False),
        (x509.ExtendedKeyUsage([ExtendedKeyUsageOID.SERVER_AUTH]), False),
    )
    client_key = ec.generate_private_key(ec.SECP256R1())
    client = certificate(
        name(USERNAME),
        client_key,
        ca.subject,
        ca_key,
        (x509.ExtendedKeyUsage([ExtendedKeyUsageOID.CLIENT_AUTH]), False),
    )
    pem = serialization.Encoding.PEM
    with open(paths["ca"], "wb") as f:
        f.write(ca.public_bytes(pem))
    with open(paths["server_cert"], "wb") as f:
        f.write(server.public_bytes(pem) + ca.public_bytes(pem))
    with open(paths["server_key"], "wb") as f:
        f.write(server_key.private_bytes(pem, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()))
    with open(paths["client"], "wb") as f:
        f.write(
            pkcs12.serialize_key_and_certificates(
                b"partcad",
                client_key,
                client,
                [ca],
                serialization.BestAvailableEncryption(password.encode("utf-8")),
            )
        )
    return paths


def _ip(text):
    import ipaddress

    return ipaddress.ip_address(text)


# --- the server ----------------------------------------------------------------------


class MockServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, certs, port=0, catalog=None, verbose=False, host="127.0.0.1"):
        super().__init__((host, port), Handler)
        self.catalog = catalog or Catalog()
        self.verbose = verbose
        self.certs = certs
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        context.load_cert_chain(certs["server_cert"], certs["server_key"])
        # What McMaster-Carr asks of every connection: a client certificate it issued.
        context.verify_mode = ssl.CERT_REQUIRED
        context.load_verify_locations(certs["ca"])
        self.socket = context.wrap_socket(self.socket, server_side=True)

    @property
    def url(self):
        return "https://%s:%d" % (self.server_address[0], self.server_address[1])

    def parameters(self):
        """What to set the plugin's parameters to, to use this server."""
        return {
            "api": self.url,
            "ca": self.certs["ca"],
            "username": USERNAME,
            "password": PASSWORD,
            "certificate": self.certs["client"],
            "certificate_password": CERTIFICATE_PASSWORD,
        }

    def start(self):
        thread = threading.Thread(target=self.serve_forever, daemon=True)
        thread.start()
        return self


def main():
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--port", type=int, default=8443)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--certs", default=os.path.join(HERE, "mock-certs"), help="where the certificates are kept")
    parser.add_argument("--catalog", default=CATALOG)
    arguments = parser.parse_args()
    certs = make_certificates(arguments.certs)
    server = MockServer(certs, arguments.port, Catalog(arguments.catalog), verbose=True, host=arguments.host)
    print("McMaster-Carr mock at %s. Give the plugins these parameters, in ~/.partcad/config.yaml:" % server.url)
    print()
    print("parameters:")
    for plugin in ("mcmaster", "mcmaster_repo"):
        print("  //pub/svc/commerce/mcmaster:%s:" % plugin)
        for key, value in server.parameters().items():
            print("    %s: %s" % (key, json.dumps(value)))
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
