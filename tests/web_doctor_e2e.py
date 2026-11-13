"""Reproduce web service failures in an isolated install and check raven web doctor's diagnosis."""

import base64
import json
import os
from pathlib import Path
import secrets
import signal
import socket
import subprocess
import tempfile
import time

from web_config_e2e import (OWNER_LABEL, READY_TIMEOUT, allocate_ports, cleanup, cli, container_args, inspect,
                            shutdown_broker, test_container_names)
from web_e2e import COMMAND_TIMEOUT, SOURCE, WEB_IMAGE, provision, run
from raven_web_host import Raven

SNAPSHOT = """
import hashlib, json
from pathlib import Path
entries = {}
for path in sorted(Path('/data').iterdir()):
    info = path.lstat()
    entries[path.name] = [info.st_mode, info.st_size, info.st_mtime_ns, hashlib.sha256(path.read_bytes()).hexdigest()]
print(json.dumps(entries))
"""
EXPIRE_CERTIFICATE = """
import datetime, os, sys
from pathlib import Path
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import NameOID
data = Path('/data')
for name in ('tls.crt', 'tls.key'):
    os.replace(data / name, data / (name + '.doctor-test'))
key = ec.generate_private_key(ec.SECP256R1())
now = datetime.datetime.now(datetime.timezone.utc)
name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, sys.argv[1])])
certificate = (x509.CertificateBuilder().subject_name(name).issuer_name(name).public_key(key.public_key())
               .serial_number(x509.random_serial_number()).not_valid_before(now - datetime.timedelta(days=30))
               .not_valid_after(now - datetime.timedelta(days=1))
               .add_extension(x509.SubjectAlternativeName([x509.DNSName(sys.argv[1])]), critical=False)
               .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
               .sign(key, hashes.SHA256()))
descriptor = os.open(data / 'tls.key', os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
with os.fdopen(descriptor, 'wb') as output:
    output.write(key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                                   serialization.NoEncryption()))
(data / 'tls.crt').write_bytes(certificate.public_bytes(serialization.Encoding.PEM))
"""
RESTORE_CERTIFICATE = """
import os
from pathlib import Path
for name in ('tls.crt', 'tls.key'):
    os.replace(Path('/data') / (name + '.doctor-test'), Path('/data') / name)
"""
WRITE_CONFIG = "import base64, sys; open('/data/config.json', 'wb').write(base64.b64decode(sys.argv[1]))"


class Doctor:
    """Run raven web doctor and require identical installation state before and after."""

    def __init__(self, environment, gateway, volume, runtime, forbidden):
        self.environment = environment
        self.gateway = gateway
        self.volume = volume
        self.runtime = runtime
        self.forbidden = forbidden

    def data(self, script, *arguments, readonly=True):
        return run(["docker", "run", "--rm", "--network", "none", *container_args(self.volume, readonly=readonly),
                    WEB_IMAGE, "python", "-c", script, *arguments], self.environment).stdout.decode()

    def snapshot(self):
        containers = {}
        for name in test_container_names(self.environment, self.gateway):
            state = inspect(self.environment, name)
            containers[name] = [state["Id"], state["State"]["Status"], state["State"]["StartedAt"]]
        runtime = None
        if self.runtime.exists():
            runtime = [self.runtime.stat().st_mode, sorted(
                (path.name, path.lstat().st_mode, path.lstat().st_size, path.lstat().st_mtime_ns)
                for path in self.runtime.iterdir())]
        return {"data": json.loads(self.data(SNAPSHOT)), "containers": containers, "runtime": runtime}

    def run(self, returncode, expected, **variables):
        before = self.snapshot()
        result = subprocess.run([str(SOURCE / "raven"), "web", "doctor"], env={**self.environment, **variables},
                                capture_output=True, text=True, timeout=COMMAND_TIMEOUT)
        output = result.stdout + result.stderr
        assert self.snapshot() == before, "doctor changed the installation:\n" + output
        assert result.returncode == returncode, f"exit {result.returncode}:\n{output}"
        for text in expected:
            assert text in output, f"missing {text!r}:\n{output}"
        for secret in self.forbidden:
            assert secret not in output, "doctor printed a secret:\n" + output
        return output


def wait_for_port(port):
    deadline = time.monotonic() + READY_TIMEOUT
    while time.monotonic() < deadline:
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=1):
                time.sleep(1)
                return
        except OSError:
            time.sleep(0.2)
    raise AssertionError(f"Nothing listens on {port}")


def broker_pid(runtime):
    with socket.socket(socket.AF_UNIX) as connection:
        connection.settimeout(5)
        connection.connect(str(runtime / "broker.sock"))
        connection.sendall(b'{"action":"ping"}\n')
        with connection.makefile("rb") as stream:
            return json.loads(stream.readline())["data"]["pid"]


def main():
    scratch = Path(tempfile.mkdtemp(prefix="raven-web-doctor-e2e-"))
    suffix = secrets.token_hex(4)
    instance = "doctor-" + suffix
    foreign_instance = "doctor-foreign-" + suffix
    environment = dict(os.environ)
    environment.pop("DISPLAY", None)
    for variable, directory in (("HOME", "home"), ("XDG_CONFIG_HOME", "config"),
                                ("XDG_STATE_HOME", "state"), ("XDG_RUNTIME_DIR", "runtime")):
        path = scratch / directory
        path.mkdir(mode=0o700)
        environment[variable] = str(path)
    environment["RAVEN_WEB_IMAGE"] = WEB_IMAGE
    environment["RAVEN_WEB_INSTANCE"] = instance
    owner = Raven(SOURCE / "raven").user
    gateway = f"raven-{owner}-{instance}"
    volume = gateway + "-data"
    foreign_volume = f"raven-{owner}-{foreign_instance}-data"
    runtime = Path(environment["XDG_RUNTIME_DIR"]) / f"raven-web-{os.getuid()}-{instance}"
    browser_port, server_port, next_browser_port, next_server_port = allocate_ports(4)
    origin = f"https://localhost:{browser_port}"
    password = secrets.token_urlsafe(32)
    try:
        provision(environment, origin, password)
        cli(environment, "start", "--port", str(server_port))
        doctor = Doctor(environment, gateway, volume, runtime, [password, "PRIVATE KEY", "__Host-raven"])
        doctor.forbidden.append(doctor.data("print(open('/data/secret.key').read().strip())").strip())

        print("Checking a healthy installation", flush=True)
        output = doctor.run(0, [
            "ok    docker ", f"ok    gateway {gateway} running since", f"ok    image {WEB_IMAGE}",
            f"ok    data volume {volume}: owned by uid {os.getuid()}, mode 0700, files present",
            "ok    account: initialized", "ok    certificate for localhost: valid until",
            f"ok    settings: 127.0.0.1:{server_port} for {origin}", "ok    broker: pid",
            "ok    gateway -> broker: reachable",
            f"ok    HTTPS: 127.0.0.1:{server_port} answers for localhost:{browser_port} with tls.crt",
            f"-L {browser_port}:127.0.0.1:{server_port}"])
        assert all(line.startswith(("ok ", "info ")) for line in output.splitlines()), output
        assert "raven web doctor" in cli(environment, "status")

        print("Checking pending, conflicting and invalid settings", flush=True)
        next_origin = f"https://localhost:{next_browser_port}"
        cli(environment, "configure", "--port", str(next_server_port), "--origin", next_origin)
        doctor.run(0, [f"warn  settings: the gateway runs 127.0.0.1:{server_port} for {origin}, but the next "
                       f"start uses 127.0.0.1:{next_server_port} for {next_origin}",
                       f"ok    HTTPS: 127.0.0.1:{server_port} answers for localhost:{browser_port}"])
        cli(environment, "stop")
        with socket.socket() as occupied:
            occupied.bind(("127.0.0.1", next_server_port))
            occupied.listen()
            doctor.run(1, [f"fail  gateway {gateway}: exited",
                           f"fail  settings: the next start cannot listen (Cannot bind 127.0.0.1:{next_server_port}",
                           "fail  broker: stopped", "skip  gateway -> broker and HTTPS: the gateway is not running"])
        cli(environment, "start")
        original = doctor.data("import base64; print(base64.b64encode(open('/data/config.json', 'rb').read()).decode())")
        foreign = json.dumps({**json.loads(base64.b64decode(original)), "origin": "https://elsewhere.example.test"})
        doctor.data(WRITE_CONFIG, base64.b64encode(foreign.encode()).decode(), readonly=False)
        doctor.run(1, ["fail  settings: The origin hostname must match an exact DNS or IP certificate SAN",
                       "skip  HTTPS: needs readable settings and tls.crt"])
        doctor.data(WRITE_CONFIG, original.strip(), readonly=False)
        doctor.run(0, [f"ok    settings: 127.0.0.1:{next_server_port} for {next_origin}"])

        print("Checking broker failures", flush=True)
        shutdown_broker(runtime)
        doctor.run(1, ["fail  broker: stopped (no socket); run: raven web start",
                       "skip  gateway -> broker: needs a running broker",
                       f"ok    HTTPS: 127.0.0.1:{next_server_port}"])
        cli(environment, "start")
        pid = broker_pid(runtime)
        os.kill(pid, signal.SIGKILL)
        deadline = time.monotonic() + READY_TIMEOUT
        while Path(f"/proc/{pid}").exists() and time.monotonic() < deadline:
            time.sleep(0.1)
        doctor.run(1, ["fail  broker: stopped (a stale socket is left behind); run: raven web start"])
        cli(environment, "start")
        shutdown_broker(runtime)
        runtime.rename(scratch / "replaced-runtime")
        cli(environment, "start")
        doctor.run(1, ["ok    broker: pid", "fail  gateway -> broker: the gateway cannot reach the broker "
                       "through /run/raven; run: raven web stop, then raven web start"])
        cli(environment, "stop")
        cli(environment, "start")
        doctor.run(0, ["ok    gateway -> broker: reachable"])

        print("Checking an expired certificate", flush=True)
        run(["docker", "container", "stop", "--time", "10", gateway], environment)
        doctor.data(EXPIRE_CERTIFICATE, "localhost", readonly=False)
        run(["docker", "container", "start", gateway], environment)
        wait_for_port(next_server_port)
        doctor.run(1, ["fail  certificate for localhost: expired on",
                       "fail  HTTPS: the served certificate has expired"])
        run(["docker", "container", "stop", "--time", "10", gateway], environment)
        doctor.data(RESTORE_CERTIFICATE, readonly=False)
        run(["docker", "container", "start", gateway], environment)
        wait_for_port(next_server_port)
        doctor.run(0, ["ok    certificate for localhost: valid until",
                       f"ok    HTTPS: 127.0.0.1:{next_server_port} answers for localhost:{next_browser_port}"])

        print("Checking permission errors", flush=True)
        runtime.chmod(0o755)
        try:
            doctor.run(1, [f"fail  broker: {runtime} must be a directory owned by uid {os.getuid()} with mode 0700 "
                           f"(found uid {os.getuid()}, mode 0755)"])
        finally:
            runtime.chmod(0o700)
        doctor.run(1, ["fail  docker: cannot use the daemon; start the Docker daemon or fix DOCKER_HOST",
                       "skip  gateway, image, data volume"], DOCKER_HOST="unix:///nonexistent")
        run(["docker", "volume", "create", "--label", OWNER_LABEL, foreign_volume], environment)
        run(["docker", "run", "--rm", "--network", "none", "--user", "0", "--read-only", "--cap-drop", "ALL",
             "--cap-add", "CHOWN", "--cap-add", "DAC_OVERRIDE", "--security-opt", "no-new-privileges",
             "--mount", f"type=volume,src={foreign_volume},dst=/data", WEB_IMAGE,
             "python", "manage.py", "init-data", "--uid", "4242", "--gid", "4242"], environment)
        result = subprocess.run([str(SOURCE / "raven"), "web", "doctor"], capture_output=True, text=True,
                                env={**environment, "RAVEN_WEB_INSTANCE": foreign_instance}, timeout=COMMAND_TIMEOUT)
        assert result.returncode == 1, result.stdout
        for text in (f"fail  data volume {foreign_volume}: /data belongs to uid 4242, but the gateway runs as "
                     f"uid {os.getuid()}; restore ownership: docker run", "chown -R",
                     "skip  account, certificate and settings"):
            assert text in result.stdout, f"missing {text!r}:\n{result.stdout}"

        print("Checking logs for credentials", flush=True)
        logs = run(["docker", "logs", gateway], environment)
        broker_log = (runtime / "broker.log").read_bytes()
        for secret in doctor.forbidden[:1] + doctor.forbidden[-1:]:
            for text in (logs.stdout, logs.stderr, broker_log):
                assert secret.encode() not in text, "A credential appeared in the gateway or broker log"
        print(json.dumps({"doctor_e2e": "passed", "healthy": "checked", "settings": "checked",
                          "broker": "checked", "expired_certificate": "checked", "permissions": "checked",
                          "state_unchanged": "checked", "credentials_absent": "checked"}))
    finally:
        cleanup(environment, gateway, f"raven-web:absent-{suffix}")
        subprocess.run(["docker", "volume", "rm", foreign_volume], env=environment, capture_output=True,
                       timeout=COMMAND_TIMEOUT)


if __name__ == "__main__":
    main()
