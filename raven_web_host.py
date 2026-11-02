"""Owner-only bridge between the web container and the Raven CLI."""

import base64
import json
import os
from pathlib import Path
import pwd
import re
import selectors
import signal
import socket
import socketserver
import stat
import struct
import subprocess
import sys
import threading

MAX_REQUEST = 65536
COMMAND_TIMEOUT = 120
SOCKET_TIMEOUT = 10
MAX_CLIENTS = 24
SESSION_ID = re.compile(r"0|[1-9][0-9]{0,8}")
SESSION_NAME = re.compile(r"[A-Za-z][A-Za-z0-9_.-]{0,63}")
MAX_COMMAND_LENGTH = 4096
UCRED = struct.Struct("3i")

# Run inside the session PID namespace before forwarding any RFB bytes.
VERIFY_VNC_PEER = """
import os
from pathlib import Path
import sys
import time

local_port, remote_port = (int(value) for value in sys.argv[1:])
deadline = time.monotonic() + 2
while time.monotonic() < deadline:
    inodes = set()
    for row in Path('/proc/net/tcp').read_text().splitlines()[1:]:
        fields = row.split()
        if (fields[1] == f'0100007F:{local_port:04X}'
                and fields[2] == f'0100007F:{remote_port:04X}' and fields[3] == '01'):
            inodes.add('socket:[' + fields[9] + ']')
    for process in Path('/proc').iterdir():
        if not process.name.isdigit():
            continue
        try:
            if (process / 'comm').read_text().strip() not in {'Xvnc', 'Xtigervnc'}:
                continue
            for descriptor in (process / 'fd').iterdir():
                if os.readlink(descriptor) in inodes:
                    sys.exit(0)
        except (FileNotFoundError, PermissionError):
            continue
    time.sleep(0.05)
sys.exit(1)
"""


class RequestError(Exception):
    pass


def private_directory(path):
    path = Path(path)
    try:
        path.mkdir(mode=0o700)
    except FileExistsError:
        pass
    info = path.lstat()
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid():
        raise RequestError("Runtime directory must be owned by the current user")
    if stat.S_IMODE(info.st_mode) != 0o700:
        raise RequestError("Runtime directory must have mode 0700")
    return path


def checked_text(request, key, maximum, default=None):
    value = request.get(key, default)
    if not isinstance(value, str) or len(value) > maximum or "\0" in value:
        raise RequestError("Invalid " + key)
    return value


def checked_name(request):
    name = checked_text(request, "name", 64)
    if not SESSION_NAME.fullmatch(name):
        raise RequestError("Invalid session name")
    return name


def checked_command(request, default=None):
    command = checked_text(request, "command", MAX_COMMAND_LENGTH, default)
    if not command.strip():
        raise RequestError("Command must not be empty")
    return command


class Raven:
    def __init__(self, cli):
        self.cli = str(Path(cli).resolve())
        self.user = re.sub(r"[^A-Za-z0-9_.-]", "_", pwd.getpwuid(os.getuid()).pw_name)
        state = os.environ.get("XDG_STATE_HOME", str(Path.home() / ".local/state"))
        self.state = Path(state) / "raven" / socket.gethostname()
        self.runtime = Path(os.environ.get("XDG_RUNTIME_DIR", "/tmp")) / f"raven-{os.getuid()}"
        self.lock = threading.Lock()
        self.environment = dict(os.environ)
        self.environment.pop("DISPLAY", None)

    def execute(self, args):
        result = subprocess.run(args, capture_output=True, env=self.environment, timeout=COMMAND_TIMEOUT)
        if result.returncode:
            raise RequestError("Raven operation failed; inspect the session with the CLI")
        return result

    def inspect(self, container):
        result = self.execute(["docker", "inspect", "--type", "container", container])
        item = json.loads(result.stdout)[0]
        labels = item["Config"].get("Labels") or {}
        sid = labels.get("raven.id", "")
        if (labels.get("raven.user") != self.user or not SESSION_ID.fullmatch(sid)
                or item["Name"] != f"/raven-{self.user}-{sid}"):
            raise RequestError("Session is not owned by this user")
        return item

    def session(self, request):
        sid = checked_text(request, "id", 9)
        if not SESSION_ID.fullmatch(sid):
            raise RequestError("Invalid session id")
        item = self.inspect(f"raven-{self.user}-{sid}")
        if request.get("action") in ("rename", "delete", "run"):
            if checked_text(request, "generation", 64) != item["Id"]:
                raise RequestError("Session changed; refresh the session list")
        return item

    def describe(self, item):
        labels = item["Config"]["Labels"]
        sid = labels["raven.id"]
        try:
            name = (self.state / f"{sid}.name").read_text().strip()
        except FileNotFoundError:
            name = ""
        return {"id": sid, "generation": item["Id"], "name": name, "state": item["State"]["Status"],
                "wm": labels.get("raven.wm") or "openbox",
                "geometry": labels["raven.geometry"], "command": labels["raven.cmd"],
                "display": labels["raven.display"]}

    def ensure_up(self, sid):
        self.execute([self.cli, "menu", "show", "-t", sid])

    def delete(self, item):
        sid = item["Config"]["Labels"]["raven.id"]
        self.execute(["docker", "rm", "-f", item["Id"]])
        # The home volume reserves this numeric ID until its metadata is removed.
        for path in (self.state / f"{sid}.name", self.state / f"{sid}.home",
                     self.runtime / f"{sid}.xauth", self.runtime / f"{sid}.log"):
            path.unlink(missing_ok=True)
        self.execute(["docker", "volume", "rm", f"raven-{self.user}-home-{sid}"])

    def create(self, request):
        name = checked_name(request)
        wm = checked_text(request, "wm", 16, "xmonad")
        if wm not in ("xmonad", "openbox"):
            raise RequestError("Invalid window manager")
        geometry = checked_text(request, "geometry", 16, "1600x900")
        match = re.fullmatch(r"([0-9]{3,4})x([0-9]{3,4})", geometry)
        if not match or not (320 <= int(match[1]) <= 7680 and 200 <= int(match[2]) <= 4320):
            raise RequestError("Invalid geometry")
        command = checked_command(request, "xterm")
        result = self.execute([self.cli, "new", "-d", "-s", name, "-g", geometry, "-w", wm, "--", command])
        matches = re.findall(rb"^raven: created session ([0-9]+)(?: |\n)", result.stderr, re.MULTILINE)
        if not matches:
            raise RequestError("Session creation did not return an id; inspect raven ls")
        return self.describe(self.session({"id": matches[-1].decode()}))

    def dispatch(self, request):
        action = request.get("action")
        if action == "list":
            result = self.execute(["docker", "ps", "-aq", "--filter", f"label=raven.user={self.user}"])
            sessions = [self.describe(self.inspect(cid)) for cid in result.stdout.decode().split()]
            return sorted(sessions, key=lambda session: int(session["id"]))
        if action == "create":
            return self.create(request)
        if action not in ("open", "rename", "delete", "run"):
            raise RequestError("Unknown operation")
        if action == "delete" and checked_text(request, "confirm", 9) != request.get("id"):
            raise RequestError("Confirm the exact session ID before deleting it")
        item = self.session(request)
        sid = item["Config"]["Labels"]["raven.id"]
        if action == "rename":
            self.execute([self.cli, "rename", "-t", sid, checked_name(request)])
            return self.describe(self.session(request))
        if action == "delete":
            self.delete(item)
            return None
        if action == "run":
            command = checked_command(request)
            self.ensure_up(sid)
            item = self.session(request)
            display = item["Config"]["Labels"]["raven.display"]
            self.execute(["docker", "exec", "-d", "-w", "/home/raven",
                          "-e", f"DISPLAY=:{display}", "-e", "XAUTHORITY=/home/raven/.Xauthority",
                          item["Id"], "sh", "-c", "umask 002; " + command])
            return None
        self.ensure_up(sid)
        item = self.session(request)
        password = self.execute(["docker", "exec", item["Id"], "cat", "/home/raven/.vnc/passwd"]).stdout
        if len(password) != 8:
            raise RequestError("Unsupported VNC password file")
        return {"id": sid, "name": self.describe(item)["name"],
                "password_file": base64.b64encode(password).decode()}

    def connect(self, request):
        item = self.session(request)
        labels = item["Config"]["Labels"]
        if not item["State"]["Running"]:
            raise RequestError("Session is not running")
        try:
            port, display = int(labels["raven.port"]), int(labels["raven.display"])
        except (KeyError, ValueError) as error:
            raise RequestError("Invalid session endpoint") from error
        if not 15900 <= port <= 65535 or port != 15800 + display:
            raise RequestError("Invalid session endpoint")
        upstream = socket.create_connection(("127.0.0.1", port), timeout=SOCKET_TIMEOUT)
        try:
            self.execute(["docker", "exec", item["Id"], "python3", "-c", VERIFY_VNC_PEER,
                          str(port), str(upstream.getsockname()[1])])
        except BaseException:
            upstream.close()
            raise
        return upstream


class Handler(socketserver.StreamRequestHandler):
    def reply(self, **payload):
        self.wfile.write(json.dumps(payload).encode() + b"\n")
        self.wfile.flush()

    def handle(self):
        self.request.settimeout(SOCKET_TIMEOUT)
        credentials = self.request.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, UCRED.size)
        _, uid, _ = UCRED.unpack(credentials)
        if uid != os.getuid():
            self.reply(ok=False, error="Permission denied")
            return
        if not self.server.clients.acquire(blocking=False):
            self.reply(ok=False, error="Too many requests")
            return
        try:
            line = self.rfile.readline(MAX_REQUEST + 1)
            if len(line) > MAX_REQUEST or not line.endswith(b"\n"):
                raise RequestError("Invalid request size")
            request = json.loads(line)
            if not isinstance(request, dict):
                raise RequestError("Expected an object")
            action = request.get("action")
            if action == "ping":
                self.reply(ok=True, data={"raven": self.server.raven.cli, "pid": os.getpid()})
            elif action == "shutdown":
                self.reply(ok=True, data=None)
                self.server.shutdown()
            elif action == "connect":
                with self.server.raven.connect(request) as upstream:
                    self.reply(ok=True, data=None)
                    self.forward(upstream)
            else:
                with self.server.raven.lock:
                    data = self.server.raven.dispatch(request)
                self.reply(ok=True, data=data)
        except (RequestError, ValueError) as error:
            self.reply(ok=False, error=str(error) if isinstance(error, RequestError) else "Invalid request")
        except (OSError, subprocess.TimeoutExpired):
            try:
                self.reply(ok=False, error="Raven backend is unavailable")
            except OSError:
                pass
        finally:
            self.server.clients.release()

    def forward(self, upstream):
        with selectors.DefaultSelector() as selector:
            selector.register(self.request, selectors.EVENT_READ, upstream)
            selector.register(upstream, selectors.EVENT_READ, self.request)
            while True:
                for key, _ in selector.select():
                    data = key.fileobj.recv(MAX_REQUEST)
                    if not data:
                        return
                    key.data.sendall(data)


class Server(socketserver.ThreadingUnixStreamServer):
    daemon_threads = True

    def __init__(self, address, raven):
        self.raven = raven
        self.clients = threading.BoundedSemaphore(MAX_CLIENTS)
        super().__init__(address, Handler)


def main():
    cli, address = sys.argv[1:]
    os.umask(0o077)
    private_directory(Path(address).parent)
    with Server(address, Raven(cli)) as server:
        os.chmod(address, 0o600)
        signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))
        try:
            server.serve_forever()
        finally:
            Path(address).unlink(missing_ok=True)


if __name__ == "__main__":
    main()
