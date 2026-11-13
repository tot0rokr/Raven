"""Verify persistent gateway settings using an isolated account and Docker volume."""

from contextlib import ExitStack
import http.client
import json
import os
from pathlib import Path
import secrets
import socket
import ssl
import subprocess
import sys
import tempfile
import time
from urllib.parse import urlsplit

from web_e2e import COMMAND_TIMEOUT, SOURCE, WEB_IMAGE, provision, run
from raven_web_host import Raven


OWNER_LABEL = f"raven.web.uid={os.getuid()}"
READY_TIMEOUT = 20
READ_STATE = """
import base64, hashlib, json, sqlite3
from pathlib import Path
data = Path('/data')
config = (data / 'config.json').read_bytes()
identity = {name: hashlib.sha256((data / name).read_bytes()).hexdigest()
            for name in ('tls.crt', 'tls.key', 'secret.key')}
with sqlite3.connect('file:/data/db.sqlite3?mode=ro', uri=True) as connection:
    rows = connection.execute('SELECT username, password FROM auth_user ORDER BY username').fetchall()
identity['account'] = hashlib.sha256(json.dumps(rows).encode()).hexdigest()
print(json.dumps({'config': json.loads(config), 'config_bytes': base64.b64encode(config).decode(),
                  'identity': identity}))
"""


def allocate_ports(count):
    with ExitStack() as stack:
        sockets = [stack.enter_context(socket.socket()) for _ in range(count)]
        for connection in sockets:
            connection.bind(("127.0.0.1", 0))
        return [connection.getsockname()[1] for connection in sockets]


def container_args(volume, readonly=False):
    return ["--user", f"{os.getuid()}:{os.getgid()}", "--read-only", "--cap-drop", "ALL",
            "--security-opt", "no-new-privileges", "--pids-limit", "256",
            "--tmpfs", "/tmp:rw,nosuid,nodev,size=64m", "--mount",
            f"type=volume,src={volume},dst=/data" + (",readonly" if readonly else "")]


def state(environment, volume):
    result = run(["docker", "run", "--rm", "--network", "none", *container_args(volume, readonly=True),
                  WEB_IMAGE, "python", "-c", READ_STATE], environment)
    return json.loads(result.stdout)


def inspect(environment, name):
    return json.loads(run(["docker", "container", "inspect", name], environment).stdout)[0]


def cli(environment, *arguments):
    return run([str(SOURCE / "raven"), "web", *arguments], environment).stdout.decode()


def fails(environment, *arguments):
    try:
        cli(environment, *arguments)
    except subprocess.CalledProcessError as error:
        return error.stderr.decode()
    raise AssertionError(f"Command unexpectedly succeeded: raven web {' '.join(arguments)}")


def https_status(port, origin, certificate):
    host = urlsplit(origin)
    context = ssl.create_default_context(cadata=certificate)
    with socket.create_connection(("127.0.0.1", port), timeout=2) as connection:
        with context.wrap_socket(connection, server_hostname=host.hostname) as secure:
            secure.sendall(f"GET /login/ HTTP/1.1\r\nHost: {host.netloc}\r\nConnection: close\r\n\r\n".encode())
            response = http.client.HTTPResponse(secure)
            response.begin()
            return response.status


def wait_for_https(port, origin, certificate):
    deadline = time.monotonic() + READY_TIMEOUT
    while time.monotonic() < deadline:
        try:
            if https_status(port, origin, certificate) == 200:
                return
        except OSError:
            pass
        time.sleep(0.1)
    raise AssertionError("The disposable legacy gateway did not become ready")


def shutdown_broker(runtime):
    address = runtime / "broker.sock"
    with socket.socket(socket.AF_UNIX) as connection:
        connection.settimeout(5)
        connection.connect(str(address))
        connection.sendall(b'{"action":"shutdown"}\n')
        with connection.makefile("rb") as stream:
            assert json.loads(stream.readline())["ok"]
    deadline = time.monotonic() + READY_TIMEOUT
    while address.exists() and time.monotonic() < deadline:
        time.sleep(0.1)
    assert not address.exists(), "The test broker did not stop"


def test_container_names(environment, gateway):
    names = run(["docker", "container", "ls", "-a", "--filter", f"label={OWNER_LABEL}",
                 "--format", "{{.Names}}"], environment).stdout.decode().splitlines()
    return [name for name in names if name in (gateway, gateway + "-image")
            or name.startswith(gateway + "-failed-") or name.startswith(gateway + "-previous-")]


def cleanup(environment, gateway, failure_image):
    commands = [[str(SOURCE / "raven"), "web", "stop"]]
    commands.extend(["docker", "container", "rm", "-f", name]
                    for name in test_container_names(environment, gateway))
    commands.extend([["docker", "volume", "rm", gateway + "-data"],
                     ["docker", "image", "rm", failure_image]])
    for command in commands:
        result = subprocess.run(command, env=environment, capture_output=True, timeout=COMMAND_TIMEOUT)
        if result.returncode and b"No such" not in result.stderr and b"no such" not in result.stderr:
            print("Cleanup needs inspection: " + " ".join(command), file=sys.stderr)


def main():
    scratch = Path(tempfile.mkdtemp(prefix="raven-web-config-e2e-"))
    suffix = secrets.token_hex(4)
    instance = "config-" + suffix
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
    runtime = Path(environment["XDG_RUNTIME_DIR"]) / f"raven-web-{os.getuid()}-{instance}"
    failure_image = f"raven-web:failure-{suffix}"
    browser_port, server_port, next_browser_port, next_server_port = allocate_ports(4)
    origin = f"https://localhost:{browser_port}"
    next_origin = f"https://localhost:{next_browser_port}"
    try:
        print("Checking read-only status before initialization", flush=True)
        output = cli(environment, "status")
        assert "Raven web: not initialized" in output and "raven web init --username NAME" in output
        assert not runtime.exists(), "Status created runtime files before initialization"

        print("Provisioning a disposable account and legacy gateway", flush=True)
        provision(environment, origin, secrets.token_urlsafe(32))
        initial = state(environment, volume)
        assert initial["config"] == {"origin": origin}
        certificate = cli(environment, "certificate")
        run(["docker", "run", "-d", "--name", gateway, "--init", "--restart", "unless-stopped",
             "--label", OWNER_LABEL, "--network", "bridge",
             "--publish", f"127.0.0.1:{server_port}:8443", *container_args(volume),
             "--mount", f"type=bind,src={runtime},dst=/run/raven,readonly",
             "--env", "RAVEN_WEB_BROKER=/run/raven/broker.sock", WEB_IMAGE], environment)
        wait_for_https(server_port, origin, certificate)
        legacy_id = inspect(environment, gateway)["Id"]
        assert not any(value.startswith("RAVEN_WEB_ORIGIN=")
                       for value in inspect(environment, gateway)["Config"]["Env"])
        output = cli(environment, "status")
        assert "Raven web: running, broker stopped" in output and "  Broker    stopped" in output
        assert f"  Listen    127.0.0.1:{server_port} -> container 8443" in output
        assert "(not saved yet; raven web start saves it)" in output
        assert state(environment, volume)["config_bytes"] == initial["config_bytes"]

        print("Adopting a legacy binding without explicit flags", flush=True)
        cli(environment, "start")
        assert inspect(environment, gateway)["Id"] == legacy_id
        adopted = state(environment, volume)
        assert adopted["config"]["port"] == server_port
        assert adopted["config"]["origin"] == origin
        assert "  Broker    running, pid" in cli(environment, "status")
        alternate_runtime = scratch / "alternate-runtime"
        output = cli({**environment, "XDG_RUNTIME_DIR": str(alternate_runtime)}, "status")
        assert "Raven web: running\n" in output and "  Broker    running, pid" in output
        assert f"            {runtime / 'broker.log'}\n" in output
        assert not alternate_runtime.exists(), "Status created files instead of using the actual runtime mount"

        print("Checking legacy origin changes require stopping the gateway", flush=True)
        diagnostic = fails(environment, "configure", "--port", str(next_server_port), "--origin", next_origin)
        assert "Stop the legacy gateway before changing its origin" in diagnostic
        assert state(environment, volume)["config_bytes"] == adopted["config_bytes"]
        assert https_status(server_port, origin, certificate) == 200
        cli(environment, "stop")
        cli(environment, "start")
        assert inspect(environment, gateway)["Id"] != legacy_id
        assert f"RAVEN_WEB_ORIGIN={origin}" in inspect(environment, gateway)["Config"]["Env"]

        print("Checking active versus pending origin and persistent restart settings", flush=True)
        cli(environment, "configure", "--port", str(next_server_port), "--origin", next_origin,
            "--ssh-target", "owner@gateway.example.test", "--ssh-port", "2222")
        pending = state(environment, volume)
        assert pending["config"]["port"] == next_server_port
        assert pending["config"]["origin"] == next_origin
        output = cli(environment, "status")
        assert f"  Listen    127.0.0.1:{server_port} -> container 8443" in output
        assert f"  Browser   {origin}\n" in output
        assert f"  Running   127.0.0.1:{server_port}, {origin}" in output
        assert f"  Saved     127.0.0.1:{next_server_port}, {next_origin}" in output
        assert "Settings    pending -> raven web stop, then raven web start" in output
        assert f"127.0.0.1:{browser_port}:127.0.0.1:{server_port}" in output
        assert "-p 2222" in output and "owner@gateway.example.test" in output
        assert https_status(server_port, origin, certificate) == 200
        assert https_status(server_port, next_origin, certificate) == 400
        run(["docker", "container", "restart", "--time", "10", gateway], environment)
        wait_for_https(server_port, origin, certificate)
        assert https_status(server_port, next_origin, certificate) == 400
        output = cli(environment, "status")
        assert f"  Browser   {origin}\n" in output
        assert f"  Saved     127.0.0.1:{next_server_port}, {next_origin}" in output
        assert "Settings    pending" in output
        assert state(environment, volume)["config_bytes"] == pending["config_bytes"]
        assert "different settings" in fails(environment, "start")
        assert state(environment, volume)["config_bytes"] == pending["config_bytes"]
        cli(environment, "stop")
        cli(environment, "start")
        assert https_status(next_server_port, next_origin, certificate) == 200
        assert "Settings    same as running" in cli(environment, "status")
        cli(environment, "stop")
        cli(environment, "start")
        assert https_status(next_server_port, next_origin, certificate) == 200
        assert state(environment, volume)["identity"] == initial["identity"]

        print("Checking occupied ports and certificate mismatch preserve saved bytes", flush=True)
        stable = state(environment, volume)
        with socket.socket() as occupied:
            occupied.bind(("127.0.0.1", 0))
            occupied.listen()
            busy_port = occupied.getsockname()[1]
            assert "Cannot bind" in fails(environment, "configure", "--port", str(busy_port))
            assert state(environment, volume)["config_bytes"] == stable["config_bytes"]
            cli(environment, "stop")
            assert "Cannot bind" in fails(environment, "start", "--port", str(busy_port))
            assert state(environment, volume)["config_bytes"] == stable["config_bytes"]
        cli(environment, "start")
        diagnostic = fails(environment, "configure", "--origin", "https://not-in-certificate.example.test")
        assert "certificate" in diagnostic.lower() or "san" in diagnostic.lower(), diagnostic
        assert state(environment, volume)["config_bytes"] == stable["config_bytes"]

        print("Checking broker-down reporting and startup rollback", flush=True)
        shutdown_broker(runtime)
        output = cli(environment, "status")
        assert "Raven web: running, broker stopped" in output and "  Gateway   running since" in output
        cli(environment, "start")
        assert "  Broker    running, pid" in cli(environment, "status")
        cli(environment, "stop")
        previous_id = inspect(environment, gateway)["Id"]
        before_failure = state(environment, volume)
        builder = gateway + "-image"
        run(["docker", "create", "--name", builder, "--label", OWNER_LABEL, WEB_IMAGE], environment)
        run(["docker", "commit", "--change", 'CMD ["python", "-c", "raise SystemExit(23)"]',
             builder, failure_image], environment)
        run(["docker", "container", "rm", builder], environment)
        diagnostic = fails({**environment, "RAVEN_WEB_IMAGE": failure_image}, "start")
        assert "Previous stopped gateway restored" in diagnostic, diagnostic
        assert inspect(environment, gateway)["Id"] == previous_id
        assert not inspect(environment, gateway)["State"]["Running"]
        assert state(environment, volume)["config_bytes"] == before_failure["config_bytes"]
        failed = [name for name in test_container_names(environment, gateway) if "-failed-" in name]
        assert failed, "Failed gateway was not retained for inspection"
        assert all(not inspect(environment, name)["State"]["Running"] for name in failed)
        cli(environment, "start")
        assert https_status(next_server_port, next_origin, certificate) == 200
        assert state(environment, volume)["identity"] == initial["identity"]
        print(json.dumps({"config_e2e": "passed", "legacy_adoption": "checked",
                          "active_pending_origin": "checked", "restart_persistence": "checked",
                          "failure_rollback": "checked", "identity_preserved": "checked"}))
        print(f"Test artifacts: {scratch}")
    finally:
        cleanup(environment, gateway, failure_image)


if __name__ == "__main__":
    try:
        main()
    except subprocess.CalledProcessError as error:
        print(error.stdout.decode(errors="replace"), file=sys.stderr)
        print(error.stderr.decode(errors="replace"), file=sys.stderr)
        sys.exit(1)
