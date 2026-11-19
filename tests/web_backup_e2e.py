"""Back up and restore the web settings of an isolated install through the real CLI and gateway (roadmap M10).

Backs up a running install, changes its password, certificate and saved settings, stops it, refuses a restore while
running, with a wrong passphrase, from a damaged file, from another instance and with a readable passphrase file
(the data volume unchanged each time), restores at a terminal, starts it and logs in with the old password while a
cookie from before the backup stays refused. Then removes the data volume and restores into a new one. Checks that
the backup file holds no plaintext key, secret or password hash, and that the passphrase reaches no output or log.

Uses its own instances (the second only to make another installation's backup), data volumes, runtime directory and
HOME/XDG directories, and no desktop session. Everything it made is removed at the end.
"""

import hashlib
import http.client
import json
import os
from pathlib import Path
import pty
import pwd
import re
import secrets
import select
import shutil
import ssl
import subprocess
import sys
import tempfile
import time
from urllib.parse import urlencode, urlsplit

from web_config_e2e import allocate_ports, cleanup, container_args
from web_e2e import COMMAND_TIMEOUT, SOURCE, WEB_IMAGE, provision, run
from web_logins_e2e import change_password

SESSION_COOKIE = "__Host-raven_session"
CSRF_COOKIE = "__Host-raven_csrf"
UNDECRYPTABLE = "Cannot decrypt the backup: the passphrase is wrong or the file is damaged"
READ_STATE = r"""
import hashlib, json, os, sqlite3, stat
from pathlib import Path
data = Path('/data')
files = {}
for root, directories, names in os.walk(data):
    for name in sorted(directories + names):
        path = Path(root) / name
        info = path.lstat()
        entry = [stat.S_IMODE(info.st_mode)]
        if stat.S_ISREG(info.st_mode):
            entry.append(hashlib.sha256(path.read_bytes()).hexdigest())
        files[str(path.relative_to(data))] = entry
connection = sqlite3.connect('file:/data/db.sqlite3?mode=ro', uri=True)
accounts = connection.execute('SELECT username, password FROM auth_user').fetchall()
sessions = connection.execute('SELECT COUNT(*) FROM django_session').fetchone()[0]
connection.close()
key_lines = [line for line in (data / 'tls.key').read_text().splitlines() if line and '-----' not in line]
print(json.dumps({'files': files, 'accounts': accounts, 'sessions': sessions, 'key_lines': key_lines,
                  'secret': (data / 'secret.key').read_text(), 'mode': stat.S_IMODE(data.stat().st_mode)}))
"""


def colons(digest):
    return ":".join(digest[index:index + 2] for index in range(0, len(digest), 2))


def fingerprint(pem):
    first = pem[:pem.index("-----END CERTIFICATE-----") + len("-----END CERTIFICATE-----")]
    return colons(hashlib.sha256(ssl.PEM_cert_to_DER_cert(first)).hexdigest().upper())


def https(origin, trust, method, path, headers=None, body=None):
    """One HTTPS request trusting only the given certificate, as a PC that registered it; status, cookies, body."""
    parts = urlsplit(origin)
    context = ssl.create_default_context(cadata=trust)
    context.verify_flags |= ssl.VERIFY_X509_PARTIAL_CHAIN
    connection = http.client.HTTPSConnection(parts.hostname, parts.port, context=context, timeout=30)
    try:
        connection.request(method, path, body=body, headers=headers or {})
        response = connection.getresponse()
        cookies = {}
        for header in response.msg.get_all("Set-Cookie") or []:
            name, _, rest = header.partition("=")
            cookies[name.strip()] = rest.split(";", 1)[0]
        return response.status, cookies, response.read().decode("utf-8", "replace")
    finally:
        connection.close()


def login(origin, trust, password):
    """Log in through the form; return the status (302 on success) and the session cookie."""
    status, cookies, page = https(origin, trust, "GET", "/login/")
    assert status == 200, status
    token = re.search(r'name="csrfmiddlewaretoken" value="([^"]+)"', page)[1]
    form = urlencode({"username": "owner", "password": password, "csrfmiddlewaretoken": token})
    status, cookies, _ = https(origin, trust, "POST", "/login/",
                               {"Cookie": f"{CSRF_COOKIE}={cookies[CSRF_COOKIE]}", "Origin": origin,
                                "Content-Type": "application/x-www-form-urlencoded"}, form)
    return status, cookies.get(SESSION_COOKIE)


def signed_in(origin, trust, cookie):
    return https(origin, trust, "GET", "/api/certificate/", {"Cookie": f"{SESSION_COOKIE}={cookie}"})[0]


def interactive(environment, arguments, answers):
    """raven web ARGUMENTS at a terminal, answering each (prompt, reply) in turn; the exit code and screen text."""
    master, slave = pty.openpty()
    process = subprocess.Popen([str(SOURCE / "raven"), "web", *arguments], env={**environment, "COLUMNS": "160"},
                               stdin=slave, stdout=slave, stderr=slave, start_new_session=True)
    os.close(slave)
    output, pending = b"", list(answers)
    deadline = time.monotonic() + COMMAND_TIMEOUT
    try:
        while time.monotonic() < deadline:
            ready, _, _ = select.select([master], [], [], 0.2)
            if ready:
                try:
                    data = os.read(master, 65536)
                except OSError:
                    break
                if not data:
                    break
                output += data
                if pending and output.endswith(pending[0][0]):
                    os.write(master, pending.pop(0)[1] + b"\n")
            elif process.poll() is not None:
                break
        code = process.wait(timeout=10)
    finally:
        os.close(master)
        if process.poll() is None:
            process.kill()
    assert not pending, f"raven web {' '.join(arguments)} did not ask {pending[0][0]!r}:\n{output.decode()}"
    return code, output.decode(errors="replace").replace("\r\n", "\n")


class Install:
    """An isolated instance: CLI calls with a transcript, and its data volume read through a read-only helper."""

    def __init__(self, environment, gateway, transcript):
        self.environment = environment
        self.gateway = gateway
        self.volume = gateway + "-data"
        self.transcript = transcript

    def web(self, *arguments, code=0):
        result = subprocess.run([str(SOURCE / "raven"), "web", *arguments], env=self.environment, text=True,
                                capture_output=True, stdin=subprocess.DEVNULL, timeout=COMMAND_TIMEOUT)
        self.transcript.append(result.stdout + result.stderr)
        assert result.returncode == code, (f"raven web {' '.join(arguments)} exited {result.returncode}, "
                                           f"expected {code}:\n{result.stdout}{result.stderr}")
        return result.stdout, result.stderr

    def state(self):
        output = run(["docker", "run", "--rm", "--network", "none", *container_args(self.volume, readonly=True),
                      WEB_IMAGE, "python", "-c", READ_STATE], self.environment).stdout
        return json.loads(output)

    def files(self):
        return self.state()["files"]


def private_file(path, text, mode=0o600):
    path.write_text(text)
    path.chmod(mode)
    return path


def main():
    scratch = Path(tempfile.mkdtemp(prefix="raven-web-backup-e2e-"))
    suffix = secrets.token_hex(4)
    instance, other_instance = "backup-" + suffix, "backupx-" + suffix
    environment = dict(os.environ)
    environment.pop("DISPLAY", None)
    for variable, directory in (("HOME", "home"), ("XDG_CONFIG_HOME", "config"), ("XDG_STATE_HOME", "state"),
                                ("XDG_DATA_HOME", "data")):
        (scratch / directory).mkdir(mode=0o700)
        environment[variable] = str(scratch / directory)
    # The broker socket path must fit AF_UNIX's limit; the scratch path may be long.
    runtime_root = tempfile.mkdtemp(prefix="rt-")
    environment.update(XDG_RUNTIME_DIR=runtime_root, RAVEN_WEB_IMAGE=WEB_IMAGE, RAVEN_WEB_INSTANCE=instance,
                       NO_COLOR="1")
    other_environment = {**environment, "RAVEN_WEB_INSTANCE": other_instance}
    owner = re.sub(r"[^A-Za-z0-9_.-]", "_", pwd.getpwuid(os.getuid()).pw_name)
    gateway, other_gateway = f"raven-{owner}-{instance}", f"raven-{owner}-{other_instance}"
    port, next_port = allocate_ports(2)
    origin = f"https://localhost:{port}"
    password, new_password, other_password = (secrets.token_urlsafe(24) for _ in range(3))
    passphrase, wrong = "backup " + secrets.token_urlsafe(18), "not the one " + secrets.token_urlsafe(18)
    keys = scratch / "keys"
    keys.mkdir(mode=0o700)
    good = private_file(keys / "passphrase", passphrase + "\n")
    bad = private_file(keys / "wrong", wrong + "\n")
    loose = private_file(keys / "loose", passphrase + "\n", 0o644)
    transcript = []
    install = Install(environment, gateway, transcript)
    other = Install(other_environment, other_gateway, transcript)
    logs = []
    report = {}
    try:
        print("Creating a disposable web install and logging in", flush=True)
        provision(environment, origin, password)
        install.web("start", "--port", str(port))
        trust = install.web("certificate")[0]
        status, old_cookie = login(origin, trust, password)
        assert status == 302 and old_cookie, status
        assert signed_in(origin, trust, old_cookie) == 200
        before = install.state()
        assert before["sessions"] >= 1, before["sessions"]

        print("backup while the gateway runs", flush=True)
        output, error = install.web("backup", "--passphrase-file", str(good))
        report["backup"] = output + error
        path = Path(re.search(r"^  File      (\S+)$", output, re.MULTILINE)[1])
        directory = scratch / "data/raven/backups"
        assert path.parent == directory and re.fullmatch(rf"web-{instance}-\d{{8}}T\d{{6}}Z\.ravenweb", path.name), path
        assert path.stat().st_mode & 0o777 == 0o600 and directory.stat().st_mode & 0o777 == 0o700
        assert [item.name for item in directory.iterdir()] == [path.name], "a temporary file was left"
        assert f"  SHA-256   {fingerprint(trust)}\n" in output and f"  Browser   {origin}\n" in output, output
        data = path.read_bytes()
        magic, header, ciphertext = data.split(b"\n", 2)
        report["header"] = (magic + b"\n" + header).decode()
        header = json.loads(header)
        assert magic == b"RAVENWEB1" and list(header) == ["kdf", "n", "r", "p", "salt", "nonce"], header
        assert (header["kdf"], header["n"], header["r"], header["p"]) == ("scrypt", 32768, 8, 1), header
        plain = [b"PRIVATE KEY", b"BEGIN", before["secret"].encode(), before["accounts"][0][1].encode(),
                 passphrase.encode(), old_cookie.encode(), origin.encode(),
                 *(line.encode() for line in before["key_lines"])]
        assert not any(value in data for value in plain), "the backup file holds a plaintext secret"
        _, error = install.web("backup", "--passphrase-file", str(good), "-o", str(path), code=1)
        assert "already exists" in error, error
        _, error = install.web("backup", "--passphrase-file", str(loose), code=1)
        assert "has mode 0644; other users must not read it" in error, error
        assert [item.name for item in directory.iterdir()] == [path.name]

        print("change the password, renew the certificate and change the saved settings", flush=True)
        change_password(environment, new_password)
        install.web("certificate", "renew", "-y", "--name", "127.0.0.1")
        install.web("configure", "--origin", f"https://127.0.0.1:{port}", "--port", str(next_port),
                     "--ssh-target", "owner@backup.example.test")
        changed = install.state()
        for name in ("tls.crt", "tls.key", "config.json"):
            assert changed["files"][name] != before["files"][name], f"{name} did not change"
        assert changed["accounts"] != before["accounts"], "the password did not change"

        print("restore while the gateway runs is refused", flush=True)
        refusals = {}
        _, error = install.web("restore", str(path), "--passphrase-file", str(good), "-y", code=1)
        assert "The web service is running; stop it before restoring: raven web stop" in error, error
        refusals["running"] = error
        assert install.files() == changed["files"], "a refused restore changed the data volume"
        logs.append(run(["docker", "logs", gateway], environment))
        install.web("stop")
        changed = install.state()

        print("wrong passphrase, damaged files, another instance's backup and a readable passphrase file", flush=True)
        _, error = install.web("restore", str(path), "--passphrase-file", str(bad), "-y", code=1)
        assert error == f"raven web: {UNDECRYPTABLE}\n", error
        refusals["wrong passphrase"] = error
        for name, damaged in (("flipped byte", data[:len(data) // 2] + bytes([data[len(data) // 2] ^ 1])
                               + data[len(data) // 2 + 1:]), ("truncated", data[:-64])):
            copy = scratch / f"{name.replace(' ', '-')}.ravenweb"
            copy.write_bytes(damaged)
            _, error = install.web("restore", str(copy), "--passphrase-file", str(good), "-y", code=1)
            assert error == f"raven web: {UNDECRYPTABLE}\n", error
            refusals[name] = error
        provision(other_environment, "https://localhost:8443", other_password)
        other_file = scratch / "other.ravenweb"
        other.web("backup", "--passphrase-file", str(good), "-o", str(other_file))
        _, error = install.web("restore", str(other_file), "--passphrase-file", str(good), "-y", code=1)
        assert (f"The backup is from another installation (host user {pwd.getpwuid(os.getuid()).pw_name}, "
                f"instance {other_instance})") in error, error
        refusals["other instance"] = error
        _, error = install.web("restore", str(path), "--passphrase-file", str(loose), "-y", code=1)
        assert "has mode 0644" in error, error
        refusals["readable passphrase file"] = error
        _, error = install.web("restore", str(path), "--passphrase-file", str(good), code=1)
        assert "Restoring needs confirmation: run it in a terminal, or add -y" in error, error
        assert install.files() == changed["files"], "a refused restore changed the data volume"
        report["refusals"] = refusals

        print("restore at a terminal, confirming with yes", flush=True)
        code, screen = interactive(environment, ["restore", str(path), "--passphrase-file", str(good)],
                                   [(b"Type yes to restore: ", b"yes")])
        report["restore"] = screen
        transcript.append(screen)
        assert code == 0, screen
        flat = " ".join(screen.split())
        for text in (f"Restore the web settings of {gateway} from {path}",
                     f"Browser {origin} (now https://127.0.0.1:{port})", f"Listen 127.0.0.1:{port}",
                     f"SHA-256 {fingerprint(trust)}", "Account owner (the password it had then)",
                     "every login ends, and cookies from before the backup stay refused",
                     "The certificate differs from the current one", "Type yes to restore: yes",
                     "every browser login has ended", "Next: raven web start"):
            assert text in flat, f"{text!r} missing:\n{screen}"
        saved = re.search(rf"Previous files saved: {install.volume}:/data/(backups/pre-restore-\d{{8}}T\d{{6}}Z)",
                          screen)[1]
        restored = install.state()
        for name in ("tls.crt", "tls.key", "secret.key", "config.json"):
            assert restored["files"][name] == before["files"][name], f"{name} is not the backup's"
        assert restored["accounts"] == before["accounts"] and restored["sessions"] == 0, restored["sessions"]
        assert restored["files"]["backups"][0] == 0o700 and restored["files"][saved][0] == 0o700
        for name in ("db.sqlite3", "secret.key", "config.json", "tls.crt", "tls.key"):
            assert restored["files"][f"{saved}/{name}"] == [0o600, changed["files"][name][1]], name
        assert install.web("logins")[0] == "No browser logins.\n"

        print("start: the old password logs in, the cookie from before the backup is refused", flush=True)
        output, _ = install.web("start")
        assert f"HTTPS port 127.0.0.1:{port}" in output and f"Browser URL: {origin}" in output, output
        served = install.web("certificate")[0]
        assert fingerprint(served) == fingerprint(trust), "the certificate is not the backup's"
        info = install.web("certificate", "info")[0]
        assert f"  SHA-256   {fingerprint(trust)}\n" in info and f"  Origin    {origin} is covered\n" in info, info
        assert signed_in(origin, served, old_cookie) == 401, "a cookie from before the backup was accepted"
        assert login(origin, served, new_password)[0] == 401, "the changed password still logs in"
        status, cookie = login(origin, served, password)
        assert status == 302 and signed_in(origin, served, cookie) == 200, status
        assert f"  Browser   {origin}\n" in install.web("status")[0]

        print("restore into a removed data volume", flush=True)
        install.web("stop")
        logs.append(run(["docker", "logs", gateway], environment))
        run(["docker", "container", "rm", gateway], environment)
        run(["docker", "volume", "rm", install.volume], environment)
        output, error = install.web("restore", str(path), "--passphrase-file", str(good), "-y")
        report["recreate"] = output + error
        assert f"Created the data volume {install.volume}, as raven web init does" in output, output
        assert "Previous files saved" not in output, output
        recreated = install.state()
        assert recreated["mode"] == 0o700 and "backups" not in recreated["files"], recreated["files"]
        for name in ("tls.crt", "tls.key", "secret.key", "config.json"):
            assert recreated["files"][name] == before["files"][name], name
        install.web("start")
        status, cookie = login(origin, served, password)
        assert status == 302 and signed_in(origin, served, cookie) == 200, status
        logs.append(run(["docker", "logs", gateway], environment))

        print("the passphrase reaches no output or log", flush=True)
        runtime = Path(runtime_root) / f"raven-web-{os.getuid()}-{instance}"
        broker_log = (runtime / "broker.log").read_bytes() if (runtime / "broker.log").exists() else b""
        sources = {"CLI output": "".join(transcript).encode(), "broker.log": broker_log,
                   "gateway logs": b"".join(result.stdout + result.stderr for result in logs)}
        for where, text in sources.items():
            for value in (passphrase, wrong):
                assert value.encode() not in text, f"a passphrase reached the {where}"
            assert b"PRIVATE KEY" not in text and before["secret"].encode() not in text, where
        print("\n--- backup ---\n" + report["backup"].rstrip())
        print("\n--- encrypted file, first two lines ---\n" + report["header"])
        print("\n--- restore at a terminal ---\n" + report["restore"].rstrip())
        print("\n--- restore -y into a removed data volume ---\n" + report["recreate"].rstrip())
        for name, text in report["refusals"].items():
            print(f"\n--- refused: {name} ---\n" + text.rstrip())
        print(json.dumps({"backup_e2e": "passed", "backup_running": "checked", "file_private": "checked",
                          "plaintext_absent": "checked", "restore_while_running": "refused",
                          "wrong_passphrase": "refused", "damaged": "refused", "other_instance": "refused",
                          "volume_unchanged_on_refusal": "checked", "restore": "checked",
                          "pre_restore_copy": "checked", "old_password": "checked", "old_cookie": "refused",
                          "fingerprint_origin": "checked", "missing_volume": "checked",
                          "passphrase_absent": "checked"}))
    finally:
        cleanup(environment, gateway, f"raven-web:absent-{suffix}")
        cleanup(other_environment, other_gateway, f"raven-web:absent-{suffix}")
        shutil.rmtree(scratch, ignore_errors=True)
        shutil.rmtree(runtime_root, ignore_errors=True)


if __name__ == "__main__":
    try:
        main()
    except subprocess.CalledProcessError as error:
        print((error.stdout or b"").decode(errors="replace"), file=sys.stderr)
        print((error.stderr or b"").decode(errors="replace"), file=sys.stderr)
        sys.exit(1)
