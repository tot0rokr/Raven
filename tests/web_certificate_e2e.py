"""Check certificate info, renewal, import, refusal and rollback on an isolated web install (roadmap M8).

Uses its own instance, data volume, runtime directory, HOME/XDG directories and a named test desktop; the
systemd part puts its unit in the user manager's runtime directory. Everything it made is removed at the end.
"""

import hashlib
import http.client
import json
import os
from pathlib import Path
import re
import secrets
import shutil
import socket
import ssl
import subprocess
import sys
import tempfile
from urllib.parse import urlsplit

from web_config_e2e import allocate_ports, container_args, inspect, test_container_names
from web_e2e import COMMAND_TIMEOUT, SOURCE, WEB_IMAGE, provision, run
from raven_web_host import Raven

MANAGER = {"XDG_RUNTIME_DIR": f"/run/user/{os.getuid()}"}
UNIT_DIR = Path(f"/run/user/{os.getuid()}/systemd/user")
CONTAINER_LIMITS = ["--memory", "512m", "--memory-swap", "512m", "--cpus", "1", "--pids-limit", "128",
                    "--user", f"{os.getuid()}:{os.getgid()}", "--read-only", "--cap-drop", "ALL",
                    "--security-opt", "no-new-privileges"]
GENERATE = r"""
import datetime, os
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec, rsa
from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID

now = datetime.datetime.now(datetime.timezone.utc)
day = datetime.timedelta(days=1)


def build(key, subject, issuer, signer, names=(), ca=False, before=None, after=None, usage=None):
    builder = (x509.CertificateBuilder()
               .subject_name(x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, subject)]))
               .issuer_name(x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, issuer)]))
               .public_key(key.public_key()).serial_number(x509.random_serial_number())
               .not_valid_before(before or now - day).not_valid_after(after or now + 200 * day)
               .add_extension(x509.BasicConstraints(ca=ca, path_length=None), critical=True))
    if names:
        builder = builder.add_extension(x509.SubjectAlternativeName([x509.DNSName(name) for name in names]),
                                        critical=False)
        builder = builder.add_extension(x509.ExtendedKeyUsage([ExtendedKeyUsageOID.SERVER_AUTH]), critical=False)
    if usage:
        builder = builder.add_extension(usage, critical=True)
    return builder.sign(signer, hashes.SHA256())


def save(name, data, mode=0o600):
    descriptor = os.open(os.path.join("/out", name), os.O_WRONLY | os.O_CREAT | os.O_EXCL, mode)
    with os.fdopen(descriptor, "wb") as output:
        output.write(data)


def certificates(*items):
    return b"".join(item.public_bytes(serialization.Encoding.PEM) for item in items)


def private(key):
    return key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                             serialization.NoEncryption())


root_key, intermediate_key = ec.generate_private_key(ec.SECP384R1()), ec.generate_private_key(ec.SECP384R1())
root = build(root_key, "Raven e2e root", "Raven e2e root", root_key, ca=True, after=now + 400 * day)
intermediate = build(intermediate_key, "Raven e2e intermediate", "Raven e2e root", root_key, ca=True,
                     after=now + 300 * day)
leaf_key = ec.generate_private_key(ec.SECP256R1())
leaf = build(leaf_key, "localhost", "Raven e2e intermediate", intermediate_key, names=("localhost",))
save("root.crt", certificates(root), 0o644)
save("chain.crt", certificates(leaf, intermediate), 0o644)
save("chain.key", private(leaf_key))
other_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
save("other.crt", certificates(build(other_key, "other", "other", other_key, names=("other.example.test",))), 0o644)
save("other.key", private(other_key))
expired_key = ec.generate_private_key(ec.SECP256R1())
expired = build(expired_key, "localhost", "localhost", expired_key, names=("localhost",), before=now - 30 * day,
                after=now - day)
save("expired.crt", certificates(expired), 0o644)
save("expired.key", private(expired_key))
# Every check passes, but TLS clients refuse a server certificate that may only sign certificates.
signing_key = ec.generate_private_key(ec.SECP256R1())
signing = build(signing_key, "localhost", "localhost", signing_key, names=("localhost",),
                usage=x509.KeyUsage(digital_signature=False, content_commitment=False, key_encipherment=False,
                                    data_encipherment=False, key_agreement=False, key_cert_sign=True,
                                    crl_sign=False, encipher_only=False, decipher_only=False))
save("signing.crt", certificates(signing), 0o644)
save("signing.key", private(signing_key))
"""
READ_STATE = r"""
import hashlib, json, sqlite3, stat
from pathlib import Path
data = Path('/data')
result = {'files': {name: hashlib.sha256((data / name).read_bytes()).hexdigest()
                    for name in ('tls.crt', 'tls.key', 'config.json', 'secret.key')},
          'modes': {name: stat.S_IMODE((data / name).stat().st_mode) for name in ('tls.crt', 'tls.key')},
          'backups': {}, 'key_lines': []}
backups = data / 'backups'
keys = [data / 'tls.key']
if backups.exists():
    result['backups_mode'] = stat.S_IMODE(backups.stat().st_mode)
    for path in sorted(backups.iterdir()):
        result['backups'][path.name] = {
            'mode': stat.S_IMODE(path.stat().st_mode),
            'files': {item.name: [stat.S_IMODE(item.stat().st_mode), hashlib.sha256(item.read_bytes()).hexdigest()]
                      for item in sorted(path.iterdir())}}
        keys.append(path / 'tls.key')
for path in keys:
    result['key_lines'].extend(line for line in path.read_text().splitlines() if line and '-----' not in line)
with sqlite3.connect('file:/data/db.sqlite3?mode=ro', uri=True) as connection:
    rows = connection.execute('SELECT username, password FROM auth_user ORDER BY username').fetchall()
result['account'] = hashlib.sha256(json.dumps(rows).encode()).hexdigest()
print(json.dumps(result))
"""
API_CLIENT = r"""
import http.cookiejar, json, ssl, sys, urllib.error, urllib.parse, urllib.request
from html.parser import HTMLParser

config = json.load(sys.stdin)
origin = config['origin']
context = ssl.create_default_context(cadata=config['trust'])
opener = urllib.request.build_opener(urllib.request.HTTPSHandler(context=context),
                                     urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar()))


class Token(HTMLParser):
    token = None

    def handle_starttag(self, tag, attributes):
        attributes = dict(attributes)
        if tag == 'input' and attributes.get('name') == 'csrfmiddlewaretoken':
            self.token = attributes['value']


def request(path, data=None):
    target = urllib.request.Request(origin + path, data=data, headers={'Origin': origin})
    try:
        response = opener.open(target, timeout=30)
    except urllib.error.HTTPError as error:
        response = error
    with response:
        headers = {name: response.headers.get(name) for name in ('Content-Type', 'Content-Disposition', 'Cache-Control')}
        return response.status, headers, response.read().decode('ascii', 'replace')


result = {'anonymous': [request(path)[0] for path in ('/api/certificate/', '/api/certificate/download/')]}
parser = Token()
parser.feed(request('/login/')[2])
form = {'username': config['username'], 'password': config['password'], 'csrfmiddlewaretoken': parser.token}
result['login'] = request('/login/', urllib.parse.urlencode(form).encode())[0]
for name, path in (('info', '/api/certificate/'), ('download', '/api/certificate/download/')):
    status, headers, body = request(path)
    result[name] = {'status': status, 'body': body, 'type': headers.get('Content-Type'),
                    'disposition': headers.get('Content-Disposition'), 'cache': headers.get('Cache-Control')}
print(json.dumps(result))
"""


def fingerprint(pem):
    first = pem[:pem.index("-----END CERTIFICATE-----") + len("-----END CERTIFICATE-----")]
    return hashlib.sha256(ssl.PEM_cert_to_DER_cert(first)).hexdigest().upper()


def colons(digest):
    return ":".join(digest[index:index + 2] for index in range(0, len(digest), 2))


def https_status(port, origin, trust):
    """GET /login/ trusting only the given PEM, its first certificate included (as a PC that registered it)."""
    parts = urlsplit(origin)
    context = ssl.create_default_context(cadata=trust)
    context.verify_flags |= ssl.VERIFY_X509_PARTIAL_CHAIN
    with socket.create_connection(("127.0.0.1", port), timeout=5) as connection:
        with context.wrap_socket(connection, server_hostname=parts.hostname) as secure:
            secure.sendall(f"GET /login/ HTTP/1.1\r\nHost: {parts.netloc}\r\nConnection: close\r\n\r\n".encode())
            response = http.client.HTTPResponse(secure)
            response.begin()
            return response.status


def refused_tls(port, origin, trust):
    try:
        https_status(port, origin, trust)
    except ssl.SSLCertVerificationError:
        return True
    return False


class Install:
    """The isolated install: CLI calls with a transcript, data volume state and gateway state."""

    def __init__(self, environment, gateway):
        self.environment = environment
        self.gateway = gateway
        self.volume = gateway + "-data"
        self.transcript = []

    def web(self, *arguments, code=0, stdin=subprocess.DEVNULL):
        result = subprocess.run([str(SOURCE / "raven"), "web", *arguments], env=self.environment, text=True,
                                capture_output=True, stdin=stdin, timeout=COMMAND_TIMEOUT)
        self.transcript.append(result.stdout + result.stderr)
        assert result.returncode == code, (f"raven web {' '.join(arguments)} exited {result.returncode}, "
                                           f"expected {code}:\n{result.stdout}{result.stderr}")
        return result.stdout, result.stderr

    def state(self):
        output = run(["docker", "run", "--rm", "--network", "none", *container_args(self.volume, readonly=True),
                      WEB_IMAGE, "python", "-c", READ_STATE], self.environment).stdout
        return json.loads(output)

    def tls(self):
        current = self.state()
        return {name: current["files"][name] for name in ("tls.crt", "tls.key")}, sorted(current["backups"])

    def container(self):
        current = inspect(self.environment, self.gateway)
        return current["Id"], current["State"]["StartedAt"]


def client(environment, script, payload, mounts=()):
    command = ["docker", "run", "--rm", "-i", "--network", "host", *CONTAINER_LIMITS]
    for source, target in mounts:
        command += ["--mount", f"type=bind,src={source},dst={target}"]
    result = run([*command, WEB_IMAGE, "python", *script], environment, input=json.dumps(payload).encode())
    return result.stdout.decode()


def live_client(environment, scratch, origin, password, trust, session_id, screenshot):
    output = client(environment, ["/live_client.py"],
                    {"origin": origin, "username": "owner", "password": password, "certificate": trust,
                     "session_id": session_id, "output": f"/output/{screenshot}"},
                    [(scratch, "/output"), (SOURCE / "web/tests/live_client.py", "/live_client.py,readonly")])
    result = json.loads(output)
    assert result["ok"] and result["session_id"] == session_id, output
    return output


def api_client(environment, origin, password, trust):
    output = client(environment, ["-c", API_CLIENT],
                    {"origin": origin, "username": "owner", "password": password, "trust": trust})
    result = json.loads(output)
    assert result["anonymous"] == [401, 401], result["anonymous"]
    assert result["login"] == 200, result["login"]
    for name in ("info", "download"):
        assert result[name]["status"] == 200 and result[name]["cache"] == "no-store", result[name]
    assert result["download"]["type"] == "application/x-x509-ca-cert", result["download"]
    assert result["download"]["disposition"] == 'attachment; filename="raven-localhost.crt"', result["download"]
    return output, json.loads(result["info"]["body"]), result["download"]["body"]


def systemctl(*arguments):
    return subprocess.run(["systemctl", "--user", *arguments], capture_output=True, text=True,
                          env={**os.environ, **MANAGER}, timeout=COMMAND_TIMEOUT)


def unit_state(unit):
    output = systemctl("show", unit, "--property=ActiveState", "--property=MainPID").stdout
    return dict(line.split("=", 1) for line in output.splitlines() if "=" in line)


def main():
    scratch = Path(tempfile.mkdtemp(prefix="raven-web-certificate-e2e-"))
    suffix = secrets.token_hex(4)
    instance = "cert-" + suffix
    environment = dict(os.environ)
    environment.pop("DISPLAY", None)
    for variable, directory in (("HOME", "home"), ("XDG_CONFIG_HOME", "config"),
                                ("XDG_STATE_HOME", "state"), ("XDG_RUNTIME_DIR", "runtime")):
        path = scratch / directory
        path.mkdir(mode=0o700)
        environment[variable] = str(path)
    environment.update(RAVEN_WEB_IMAGE=WEB_IMAGE, RAVEN_WEB_INSTANCE=instance, RAVEN_WEB_UNIT_DIR=str(UNIT_DIR))
    owner = Raven(SOURCE / "raven").user
    gateway = f"raven-{owner}-{instance}"
    unit = f"raven-{instance}.service"
    runtime = Path(environment["XDG_RUNTIME_DIR"]) / f"raven-web-{os.getuid()}-{instance}"
    keys = scratch / "keys"
    keys.mkdir(mode=0o700)
    port, next_port = allocate_ports(2)
    origin = f"https://localhost:{port}"
    password = secrets.token_urlsafe(32)
    previous_hostd = f"@raven-host-{os.getuid()}" in Path("/proc/net/unix").read_text()
    install = Install(environment, gateway)
    responses = []
    session_id = None
    try:
        print("Creating a disposable desktop and web install", flush=True)
        result = run([str(SOURCE / "raven"), "new", "-d", "-s", "cert-test-" + suffix,
                      "-g", "800x600", "-w", "openbox", "xterm"], environment)
        session_id = re.search(rb"raven: created session ([0-9]+)", result.stderr)[1].decode()
        desktop = f"raven-{owner}-{session_id}"
        desktop_state = inspect(environment, desktop)["Id"], inspect(environment, desktop)["State"]["StartedAt"]
        provision(environment, origin, password)
        install.web("start", "--port", str(port))
        run(["docker", "run", "--rm", "--network", "none", *CONTAINER_LIMITS,
             "--mount", f"type=bind,src={keys},dst=/out", WEB_IMAGE, "python", "-c", GENERATE], environment)

        print("certificate info", flush=True)
        initial = install.web("certificate")[0]
        output, _ = install.web("certificate", "info")
        assert "  Names     localhost\n" in output, output
        assert f"  SHA-256   {colons(fingerprint(initial))}\n" in output, output
        assert f"  Origin    {origin} is covered\n" in output, output
        assert re.search(r"  Expires   \d{4}-\d\d-\d\d \(36[45] days left\)\n", output), output
        assert "  Issuer    self-signed\n" in output and "\nTrust: " in output, output
        info_output = output
        before = install.state()
        identity = {name: before["files"][name] for name in ("config.json", "secret.key")}, before["account"]
        assert before["backups"] == {}, before["backups"]

        print("renew without a terminal or -y is refused", flush=True)
        container = install.container()
        _, error = install.web("certificate", "renew", code=1)
        assert "needs confirmation" in error, error
        assert install.tls() == ({"tls.crt": before["files"]["tls.crt"], "tls.key": before["files"]["tls.key"]}, [])
        assert install.container() == container

        print("renew -y --name ... --days 400", flush=True)
        output, error = install.web("certificate", "renew", "-y", "--name", "raven-cert.example.test",
                                    "--name", "127.0.0.1", "--days", "400")
        renew_output = output + error
        renewed = install.web("certificate")[0]
        assert fingerprint(renewed) != fingerprint(initial)
        assert f"  SHA-256   {colons(fingerprint(renewed))}\n" in output, output
        assert "Gateway restarted; " in output and "register the new certificate on each PC" in output, output
        assert re.search(r"\((399|400) days left\)", output), output
        assert "  Names     localhost, raven-cert.example.test, 127.0.0.1\n" in install.web("certificate", "info")[0]
        renewed_container = install.container()
        assert renewed_container[0] == container[0], "renew recreated the gateway instead of restarting it"
        assert renewed_container[1] != container[1], "renew did not restart the gateway"
        assert https_status(port, origin, renewed) == 200
        assert refused_tls(port, origin, initial), "the gateway still serves the old certificate"
        state = install.state()
        assert ({name: state["files"][name] for name in ("config.json", "secret.key")}, state["account"]) == identity
        assert state["modes"] == {"tls.crt": 0o600, "tls.key": 0o600}, state["modes"]
        assert state["backups_mode"] == 0o700 and len(state["backups"]) == 1, state["backups"]
        (name, backup), = state["backups"].items()
        assert re.fullmatch(r"tls-\d{8}T\d{6}Z", name) and backup["mode"] == 0o700, (name, backup["mode"])
        assert backup["files"] == {"tls.crt": [0o600, before["files"]["tls.crt"]],
                                   "tls.key": [0o600, before["files"]["tls.key"]]}, backup["files"]

        print("HTTPS login, WebSocket desktop and certificate API with the renewed certificate", flush=True)
        responses.append(live_client(environment, scratch, origin, password, renewed, session_id, "renewed.png"))
        output, info, download = api_client(environment, origin, password, renewed)
        responses.append(output)
        assert info["names"] == ["localhost", "raven-cert.example.test", "127.0.0.1"], info
        assert info["fingerprint"] == colons(fingerprint(renewed)) and info["origin_covered"] is True, info
        assert info["days_left"] in (399, 400), info
        assert download == renewed, "the downloaded certificate differs from raven web certificate"

        print("import a CA-issued chain (server certificate, then intermediate)", flush=True)
        container = install.container()
        output, error = install.web("certificate", "import", str(keys / "chain.crt"), str(keys / "chain.key"), "-y")
        import_output = output + error
        chain = (keys / "chain.crt").read_text()
        root = (keys / "root.crt").read_text()
        assert install.web("certificate")[0] == chain
        assert "Gateway restarted; " in output and f"  SHA-256   {colons(fingerprint(chain))}\n" in output, output
        assert install.container()[0] == container[0] and install.container()[1] != container[1]
        assert https_status(port, origin, chain) == 200 and https_status(port, origin, root) == 200
        assert "  Issuer    issued by a CA, chain of 2 certificates\n" in install.web("certificate", "info")[0]
        responses.append(live_client(environment, scratch, origin, password, root, session_id, "imported.png"))
        output, info, download = api_client(environment, origin, password, root)
        responses.append(output)
        assert info["names"] == ["localhost"] and info["fingerprint"] == colons(fingerprint(chain)), info
        assert download == chain
        state = install.state()
        assert ({name: state["files"][name] for name in ("config.json", "secret.key")}, state["account"]) == identity
        assert len(state["backups"]) == 2

        print("refused imports leave the files and the gateway alone", flush=True)
        files, backups = install.tls()
        container = install.container()
        refusals = (("chain.crt", "other.key", "KEY does not match the certificate"),
                    ("other.crt", "other.key", "CERT covers other.example.test, but not localhost"),
                    ("expired.crt", "expired.key", "CERT expired on"))
        refused_output = ""
        for certificate, key, message in refusals:
            output, error = install.web("certificate", "import", str(keys / certificate), str(keys / key), "-y",
                                        code=1)
            assert message in error, error
            refused_output += f"$ raven web certificate import {certificate} {key} -y\n{output}{error}"
            assert install.tls() == (files, backups), f"refused {certificate} changed tls files or backups"
            assert install.container() == container, f"refused {certificate} restarted the gateway"
        assert https_status(port, origin, chain) == 200

        print("a certificate TLS clients refuse is rolled back", flush=True)
        output, error = install.web("certificate", "import", str(keys / "signing.crt"), str(keys / "signing.key"),
                                    "-y", code=1)
        rollback_output = output + error
        assert "did not serve the new certificate" in error and "restored the previous certificate" in error, error
        assert install.tls()[0] == files, "rollback did not restore the previous pair"
        assert install.container()[0] == container[0]
        assert https_status(port, origin, chain) == 200
        assert install.web("certificate")[0] == chain

        print("configure --port keeps the certificate", flush=True)
        files, backups = install.tls()
        install.web("configure", "--port", str(next_port))
        install.web("stop")
        install.web("start")
        assert install.tls() == (files, backups), "a port change replaced the certificate"
        assert install.web("certificate")[0] == chain
        assert https_status(next_port, origin, chain) == 200

        current, supervised_check = chain, "skipped"
        if systemctl("show-environment").returncode == 0:
            print("renew while systemd supervises the broker", flush=True)
            install.web("enable")
            supervised = unit_state(unit)
            assert supervised["ActiveState"] == "active", supervised
            container = install.container()
            install.web("certificate", "renew", "-y")
            current = install.web("certificate")[0]
            assert unit_state(unit) == supervised, "renewal restarted or stopped the unit"
            assert install.container()[0] == container[0] and install.container()[1] != container[1]
            assert https_status(next_port, origin, current) == 200
            assert f"running, pid {supervised['MainPID']} (systemd)" in install.web("status")[0]
            assert "  ok    Certificate      localhost, valid until" in install.web("doctor")[0]
            install.web("disable")
            supervised_check = "checked"
        else:
            print("systemd user manager not reachable; skipped the supervised renewal", flush=True)

        print("status shows the certificate; key material appears nowhere", flush=True)
        status = install.web("status")[0]
        assert "\nCertificate\n" in status and f"  SHA-256   {colons(fingerprint(current))}\n" in status, status
        assert re.search(r"  Expires   \d{4}-\d\d-\d\d \(\d+ days left\)\n", status), status
        state = install.state()
        assert state["files"]["secret.key"] == identity[0]["secret.key"] and state["account"] == identity[1]
        assert inspect(environment, desktop)["Id"] == desktop_state[0], "the test desktop was recreated"
        assert inspect(environment, desktop)["State"]["StartedAt"] == desktop_state[1], "the desktop restarted"
        lines = set(state["key_lines"])
        for name in ("chain.key", "other.key", "expired.key", "signing.key"):
            lines.update(line for line in (keys / name).read_text().splitlines() if "-----" not in line)
        logs = run(["docker", "logs", gateway], environment)
        broker_log = (runtime / "broker.log").read_bytes() if (runtime / "broker.log").exists() else b""
        journal = subprocess.run(["journalctl", "--user", "-u", unit, "--no-pager"], capture_output=True,
                                 env={**os.environ, **MANAGER}, timeout=COMMAND_TIMEOUT).stdout
        sources = {"gateway log": logs.stdout + logs.stderr, "broker.log": broker_log, "journal": journal,
                   "CLI output": "".join(install.transcript).encode(), "API responses": "".join(responses).encode()}
        for source, text in sources.items():
            assert b"PRIVATE KEY" not in text, f"PRIVATE KEY appeared in the {source}"
            assert not any(line.encode() in text for line in lines), f"private key bytes appeared in the {source}"
        print("\n--- certificate info ---\n" + info_output.rstrip())
        print("\n--- renew -y --name raven-cert.example.test --name 127.0.0.1 --days 400 ---\n" + renew_output.rstrip())
        print("\n--- import chain.crt chain.key -y ---\n" + import_output.rstrip())
        print("\n--- refused imports ---\n" + refused_output.rstrip())
        print("\n--- import signing.crt signing.key -y (rolled back) ---\n" + rollback_output.rstrip())
        print(json.dumps({"certificate_e2e": "passed", "info": "checked", "confirmation": "checked",
                          "renew": "checked", "login_websocket_api": "checked", "import_chain": "checked",
                          "refused_imports": "checked", "rollback": "checked", "port_change": "checked",
                          "systemd": supervised_check,
                          "account_desktop_preserved": "checked", "key_material_absent": "checked"}))
        print(f"Screenshots: {scratch / 'renewed.png'}, {scratch / 'imported.png'}")
    finally:
        cleanup(environment, gateway, unit, session_id, previous_hostd)
        shutil.rmtree(keys, ignore_errors=True)


def cleanup(environment, gateway, unit, session_id, previous_hostd):
    subprocess.run([str(SOURCE / "raven"), "web", "disable"], env=environment, capture_output=True,
                   timeout=COMMAND_TIMEOUT)
    (UNIT_DIR / unit).unlink(missing_ok=True)
    systemctl("daemon-reload")
    systemctl("reset-failed", unit)
    commands = [[str(SOURCE / "raven"), "web", "stop"]]
    commands.extend(["docker", "container", "rm", "-f", name] for name in test_container_names(environment, gateway))
    commands.append(["docker", "volume", "rm", gateway + "-data"])
    if session_id is not None:
        commands.append([str(SOURCE / "raven"), "kill", "-t", session_id])
    if not previous_hostd:
        commands.append([str(SOURCE / "raven"), "hostd", "stop"])
    for command in commands:
        result = subprocess.run(command, env=environment, capture_output=True, timeout=COMMAND_TIMEOUT)
        if result.returncode and b"No such" not in result.stderr and b"no such" not in result.stderr:
            print("Cleanup needs inspection: " + " ".join(command), file=sys.stderr)


if __name__ == "__main__":
    try:
        main()
    except subprocess.CalledProcessError as error:
        print(error.stdout.decode(errors="replace"), file=sys.stderr)
        print(error.stderr.decode(errors="replace"), file=sys.stderr)
        sys.exit(1)
