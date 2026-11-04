"""Exercise a disposable Raven desktop through the real HTTPS gateway."""

import json
import os
from pathlib import Path
import pty
import re
import secrets
import select
import socket
import socketserver
import subprocess
import sys
import tempfile
import threading
import time

SOURCE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SOURCE))

from raven_web_host import Raven, VERIFY_VNC_PEER

WEB_IMAGE = os.environ.get("RAVEN_WEB_IMAGE", "raven-web:dev")
COMMAND_TIMEOUT = 180


class Tunnel(socketserver.ThreadingTCPServer):
    daemon_threads = True


class ForwardConnection(socketserver.BaseRequestHandler):
    def handle(self):
        with socket.create_connection(("127.0.0.1", self.server.destination_port)) as upstream:
            sockets = (self.request, upstream)
            while True:
                ready, _, _ = select.select(sockets, [], [], COMMAND_TIMEOUT)
                if not ready:
                    return
                for source in ready:
                    try:
                        data = source.recv(65536)
                    except ConnectionResetError:
                        return
                    if not data:
                        return
                    destination = upstream if source is self.request else self.request
                    try:
                        destination.sendall(data)
                    except (BrokenPipeError, ConnectionResetError):
                        return


def run(arguments, environment, **kwargs):
    return subprocess.run(arguments, env=environment, capture_output=True, check=True,
                          timeout=COMMAND_TIMEOUT, **kwargs)


def provision(environment, origin, password):
    master, slave = pty.openpty()
    process = subprocess.Popen([str(SOURCE / "raven"), "web", "init", "--username", "owner",
                                "--origin", origin], env=environment, stdin=slave,
                               stdout=slave, stderr=slave, start_new_session=True)
    os.close(slave)
    prompts = [b"Password: ", b"Confirm password: "]
    pending = b""
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
                pending += data
                if prompts and prompts[0] in pending:
                    os.write(master, password.encode() + b"\n")
                    prompts.pop(0)
                    pending = b""
            if process.poll() is not None:
                break
        if process.wait(timeout=5) != 0 or prompts:
            raise RuntimeError("Interactive web initialization failed")
    finally:
        os.close(master)
        if process.poll() is None:
            process.terminate()
            process.wait(timeout=10)


def verify_private_socket(environment, runtime):
    assert runtime.stat().st_mode & 0o777 == 0o700
    assert (runtime / "broker.sock").stat().st_mode & 0o777 == 0o600
    probe = (
        "import socket; connection = socket.socket(socket.AF_UNIX); "
        "\ntry: connection.connect('/broker/broker.sock')"
        "\nexcept PermissionError: raise SystemExit(0)"
        "\nraise SystemExit('Another UID could access the broker')"
    )
    run(["docker", "run", "--rm", "--network", "none", "--user", "65534:65534",
         "--mount", f"type=bind,src={runtime},dst=/broker,readonly",
         WEB_IMAGE, "python", "-c", probe], environment)


def verify_vnc_peer(environment, container, session_id):
    with Raven(SOURCE / "raven").connect({"id": session_id}) as connection:
        assert connection.recv(12) == b"RFB 003.008\n"
    with socket.socket() as fake_server:
        fake_server.bind(("127.0.0.1", 0))
        fake_server.listen()
        with socket.create_connection(fake_server.getsockname()) as client:
            peer, _ = fake_server.accept()
            with peer:
                result = subprocess.run(
                    ["docker", "exec", container, "python3", "-c", VERIFY_VNC_PEER,
                     str(fake_server.getsockname()[1]), str(client.getsockname()[1])],
                    env=environment, capture_output=True, timeout=10,
                )
                assert result.returncode == 1, "A foreign process passed VNC ownership verification"


def main():
    scratch = Path(tempfile.mkdtemp(prefix="raven-web-e2e-"))
    suffix = secrets.token_hex(4)
    instance = "test-" + suffix
    environment = dict(os.environ)
    environment.pop("DISPLAY", None)
    for variable, directory in (("HOME", "home"), ("XDG_CONFIG_HOME", "config"),
                                ("XDG_STATE_HOME", "state"), ("XDG_RUNTIME_DIR", "runtime")):
        path = scratch / directory
        path.mkdir(mode=0o700)
        environment[variable] = str(path)
    environment["RAVEN_WEB_IMAGE"] = WEB_IMAGE
    environment["RAVEN_WEB_INSTANCE"] = instance
    with socket.socket() as reservation:
        reservation.bind(("127.0.0.1", 0))
        port = reservation.getsockname()[1]
    tunnel = Tunnel(("127.0.0.1", 0), ForwardConnection)
    tunnel.destination_port = port
    threading.Thread(target=tunnel.serve_forever, daemon=True).start()
    origin = f"https://localhost:{tunnel.server_address[1]}"
    password = secrets.token_urlsafe(32)
    owner = Raven(SOURCE / "raven").user
    gateway = f"raven-{owner}-{instance}"
    runtime = Path(environment["XDG_RUNTIME_DIR"]) / f"raven-web-{os.getuid()}-{instance}"
    previous_hostd = f"@raven-host-{os.getuid()}" in Path("/proc/net/unix").read_text()
    session_id = None
    try:
        print("Creating a disposable desktop", flush=True)
        result = run([str(SOURCE / "raven"), "new", "-d", "-s", "web-test-" + suffix,
                      "-g", "800x600", "-w", "openbox", "xterm"], environment)
        match = re.search(rb"raven: created session ([0-9]+)", result.stderr)
        if not match:
            raise RuntimeError("The desktop did not report its session ID")
        session_id = match[1].decode()
        print("Provisioning a random test account", flush=True)
        provision(environment, origin, password)
        certificate = run([str(SOURCE / "raven"), "web", "certificate"], environment).stdout.decode()
        run([str(SOURCE / "raven"), "web", "start", "--port", str(port)], environment)
        gateway_id = run(["docker", "inspect", "--format", "{{.Id}}", gateway], environment).stdout
        run(["docker", "update", "--memory", "1536m", "--memory-swap", "1536m", "--cpus", "2", gateway], environment)
        run([str(SOURCE / "raven"), "web", "start"], environment)
        gateway_state = json.loads(run(["docker", "inspect", gateway], environment).stdout)[0]
        assert gateway_state["HostConfig"]["Memory"] == 1024 ** 3
        assert gateway_state["HostConfig"]["MemorySwap"] == 1024 ** 3
        assert gateway_state["HostConfig"]["NanoCpus"] == 1_000_000_000
        assert gateway_state["Id"].encode() == gateway_id.strip()
        desktop_id = run(["docker", "inspect", "--format", "{{.Id}}",
                          f"raven-{owner}-{session_id}"], environment).stdout
        desktop = json.loads(run(["docker", "inspect", f"raven-{owner}-{session_id}"], environment).stdout)[0]
        resources = desktop["HostConfig"]
        assert resources["Memory"] == 2 * 1024 ** 3
        assert resources["MemorySwap"] == resources["Memory"]
        assert resources["NanoCpus"] == 2_000_000_000
        assert resources["PidsLimit"] == 512
        run([str(SOURCE / "raven"), "web", "stop"], environment)
        run([str(SOURCE / "raven"), "web", "start"], environment)
        assert run([str(SOURCE / "raven"), "web", "certificate"], environment).stdout.decode() == certificate
        assert run(["docker", "inspect", "--format", "{{.Id}}",
                    f"raven-{owner}-{session_id}"], environment).stdout == desktop_id
        status = run([str(SOURCE / "raven"), "web", "status"], environment).stdout.decode()
        assert f"127.0.0.1:{tunnel.server_address[1]}:127.0.0.1:{port}" in status
        verify_private_socket(environment, runtime)
        verify_vnc_peer(environment, f"raven-{owner}-{session_id}", session_id)
        print("Checking HTTPS login, WSS and framebuffer through a TCP tunnel with distinct ports", flush=True)
        payload = {"origin": origin, "username": "owner", "password": password,
                   "certificate": certificate, "session_id": session_id, "output": "/output/desktop.png",
                   "management_name": "web-manage-" + suffix}
        client = run(["docker", "run", "--rm", "-i", "--network", "host",
                      "--memory", "512m", "--memory-swap", "512m", "--cpus", "1", "--pids-limit", "128",
                      "--user", f"{os.getuid()}:{os.getgid()}", "--read-only", "--cap-drop", "ALL",
                      "--security-opt", "no-new-privileges", "--mount", f"type=bind,src={scratch},dst=/output",
                      "--mount", f"type=bind,src={SOURCE / 'web/tests/live_client.py'},dst=/live_client.py,readonly",
                      WEB_IMAGE, "python", "/live_client.py"], environment, input=json.dumps(payload).encode())
        print(client.stdout.decode().strip())
        marker = run(["docker", "exec", f"raven-{owner}-{session_id}",
                      "cat", "/tmp/raven-web-command-check"], environment).stdout
        assert marker == b"raven-web-command-ok\n", "Web command did not execute in the test container"
        run(["docker", "exec", f"raven-{owner}-{session_id}", "test", "!", "-e",
             "/tmp/raven-web-stale-command-check"], environment)
        browser_image = environment.get("RAVEN_WEB_BROWSER_IMAGE")
        if browser_image:
            browser = run(["docker", "run", "--rm", "-i", "--init", "--network", "host",
                           "--user", f"{os.getuid()}:{os.getgid()}", "--security-opt", "seccomp=unconfined",
                           "--shm-size", "1g", "--env", "HOME=/tmp", "--read-only",
                           "--tmpfs", "/tmp:rw,nosuid,nodev,size=512m",
                           "--mount", f"type=bind,src={scratch},dst=/output",
                           "--mount", f"type=bind,src={SOURCE / 'tests/web_login_ui.py'},dst=/login_ui.py,readonly",
                           browser_image, "python3", "/login_ui.py"], environment,
                          input=json.dumps({"origin": origin, "output": "/output"}).encode())
            print(browser.stdout.decode().strip())
        print(f"Other-UID broker access and foreign VNC process: rejected\nScreenshot: {scratch / 'desktop.png'}")
    finally:
        cleanup(environment, gateway, session_id, previous_hostd, "web-manage-" + suffix)
        tunnel.shutdown()
        tunnel.server_close()


def cleanup(environment, gateway, session_id, previous_hostd, management_name):
    state = Path(environment["XDG_STATE_HOME"]) / "raven" / socket.gethostname()
    for path in state.glob("*.name"):
        if path.read_text().strip() in (management_name, management_name + "-renamed"):
            run([str(SOURCE / "raven"), "kill", "-t", path.stem], environment)
    commands = [[str(SOURCE / "raven"), "web", "stop"],
                ["docker", "container", "rm", "-f", gateway],
                ["docker", "volume", "rm", gateway + "-data"]]
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
