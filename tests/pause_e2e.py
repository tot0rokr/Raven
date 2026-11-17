"""Pause and resume a disposable xmonad session and check that its running state survives.

Uses a temporary HOME and XDG directories, so the user's sessions, ~/.config/raven and
~/Downloads are never touched; the test session is killed at the end. Checks:
- a python3 process keeps its PID and a 200 MiB RAM marker; its counter and the cgroup CPU time
  stop during a pause of more than 3 s while memory.current stays; the counter moves after resume;
- text typed into an xterm without Enter is submitted intact by an Enter after resume;
- pause ends the X11 viewer inside the session and drops a host TCP client of Xvnc (a web viewer);
- attach, run, shell, menu show, home sync -t and wallpaper -t refuse a paused session without
  starting or changing it; pause/resume in the wrong state fail; raven ls shows the paused state;
- after resume PCManFM and Microsoft Edge open windows and kime is not stopped; kill removes a
  paused session.
"""

import json
import os
from pathlib import Path
import re
import secrets
import shutil
import socket
import struct
import subprocess
import sys
import tempfile
import time

SOURCE = Path(__file__).resolve().parents[1]
RAVEN = str(SOURCE / "raven")
IMAGE = os.environ.get("RAVEN_IMAGE", "raven:latest")
TIMEOUT = 120
MARKER_SIZE = 200 * 1024 * 1024
TYPED = "kept across pause 42"
FIXED_KEY = bytes(int(f"{value:08b}"[::-1], 2) for value in (23, 82, 107, 6, 35, 78, 88, 7))

MARKER = r'''
import hashlib, os, signal, sys, time
token = sys.argv[1].encode()
out = "/home/raven/Downloads/m7"
size = int(sys.argv[2])
offsets = (0, size // 2, size - 64)
buffer = bytearray(b"\xa5") * size  # written, so every page is resident
for offset in offsets:
    buffer[offset:offset + len(token)] = token

def write(path, text):
    with open(path + ".tmp", "w") as f:
        f.write(text)
    os.replace(path + ".tmp", path)

def report(*_):
    found = " ".join(bytes(buffer[o:o + len(token)]).decode() for o in offsets)
    write(out + "-report", f"{os.getpid()} {found} {hashlib.sha256(buffer).hexdigest()}\n")

signal.signal(signal.SIGUSR1, report)
report()
count = 0
while True:
    count += 1
    write(out + "-counter", f"{count}\n")
    time.sleep(0.1)
'''


def raven(environment, *arguments, check=True, display=None):
    environment = dict(environment)
    if display:
        environment["DISPLAY"] = display
    result = subprocess.run([RAVEN, *arguments], env=environment, capture_output=True, text=True,
                            stdin=subprocess.DEVNULL, timeout=TIMEOUT)
    if check and result.returncode:
        raise AssertionError(f"raven {' '.join(arguments)} failed: {result.stderr}")
    return result


def wait(condition, what, seconds=30):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        value = condition()
        if value:
            return value
        time.sleep(0.2)
    raise AssertionError(f"timed out waiting for {what}")


def parse_size(text):
    match = re.fullmatch(r"([0-9.]+)(MiB|GiB)", text)
    assert match, text
    return float(match[1]) * (1024 ** 2 if match[2] == "MiB" else 1024 ** 3)


class Session:
    def __init__(self, environment, sid):
        self.environment = environment
        self.sid = sid
        self.container = f"raven-{re.sub(r'[^A-Za-z0-9_.-]', '_', os.environ['USER'])}-{sid}"
        labels = self.inspect()["Config"]["Labels"]
        self.display, self.port = labels["raven.display"], int(labels["raven.port"])

    def inspect(self):
        result = subprocess.run(["docker", "inspect", self.container], capture_output=True, check=True,
                                timeout=TIMEOUT)
        return json.loads(result.stdout)[0]

    def state(self):
        return self.inspect()["State"]

    def x(self, *arguments, check=True):
        return subprocess.run(["docker", "exec", "-e", f"DISPLAY=:{self.display}",
                               "-e", "XAUTHORITY=/home/raven/.Xauthority", self.container, *arguments],
                              capture_output=True, text=True, check=check, timeout=TIMEOUT)

    def cgroup(self):
        pid = self.state()["Pid"]
        path = next(line[3:] for line in Path(f"/proc/{pid}/cgroup").read_text().splitlines()
                    if line.startswith("0::"))
        return Path("/sys/fs/cgroup" + path)

    def processes(self):
        """(host pid, comm, state, cmdline) of every process in the session, read from the host."""
        found = []
        for pid in (self.cgroup() / "cgroup.procs").read_text().split():
            try:
                comm = Path(f"/proc/{pid}/comm").read_text().strip()
                state = Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()[0]
                cmdline = Path(f"/proc/{pid}/cmdline").read_bytes().replace(b"\0", b" ").decode()
            except (FileNotFoundError, ProcessLookupError):
                continue
            found.append((int(pid), comm, state, cmdline))
        return found

    def cpu_usec(self):
        return int(re.search(r"^usage_usec ([0-9]+)$", (self.cgroup() / "cpu.stat").read_text(), re.M)[1])

    def memory(self):
        return int((self.cgroup() / "memory.current").read_text())

    def vnc_clients(self):
        result = subprocess.run(["ss", "-Htn", "state", "established", f"( sport = :{self.port} )"],
                                capture_output=True, text=True, check=True)
        return len(result.stdout.splitlines())

    def windows(self, *query):
        return self.x("xdotool", "search", "--onlyvisible", *query, check=False).stdout.split()

    def ls_row(self):
        result = raven(self.environment, "ls")
        lines = result.stdout.splitlines()
        assert lines[0].split() == ["ID", "NAME", "STATE", "WM", "DISPLAY", "PORT", "X11", "WEB", "CPU", "MEM",
                                    "CMD"], lines[0]
        row = next(line.split() for line in lines[1:] if line.split()[:1] == [self.sid])
        return dict(zip(("id", "name", "state", "wm", "display", "port", "x11", "web", "cpu", "mem"), row)), lines


def rfb_client(port, password_file):
    """Connect like the web broker does: a host TCP client that completes the VNC handshake."""
    connection = socket.create_connection(("127.0.0.1", port), timeout=10)

    def exact(size):
        data = b""
        while len(data) < size:
            piece = connection.recv(size - len(data))
            if not piece:
                raise AssertionError("Xvnc closed the handshake")
            data += piece
        return data

    assert exact(12) == b"RFB 003.008\n"
    connection.sendall(b"RFB 003.008\n")
    types = exact(exact(1)[0])
    try:
        import warnings
        warnings.filterwarnings("ignore")
        from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
    except ImportError:
        return connection, "handshake pending (no python3 cryptography for VNC auth)"
    assert 2 in types, types

    def des(key, data, decrypt=False):
        cipher = Cipher(algorithms.TripleDES(key), modes.ECB())
        operation = cipher.decryptor() if decrypt else cipher.encryptor()
        return operation.update(data) + operation.finalize()

    password = des(FIXED_KEY, password_file, decrypt=True).split(b"\0", 1)[0]
    key = bytes(int(f"{value:08b}"[::-1], 2) for value in password.ljust(8, b"\0")[:8])
    connection.sendall(b"\x02")
    connection.sendall(des(key, exact(16)))
    assert struct.unpack(">I", exact(4))[0] == 0, "VNC authentication failed"
    connection.sendall(b"\x01")
    exact(20)
    exact(struct.unpack(">I", exact(4))[0])
    # Like noVNC: without the desktop size encodings Xvnc closes the client when the screen size changes.
    connection.sendall(struct.pack(">BxHiii", 2, 3, 0, -223, -308))
    return connection, "authenticated"


def closed(connection, seconds=3):
    connection.settimeout(seconds)
    try:
        while connection.recv(65536):
            pass
        return True
    except socket.timeout:
        return False
    except OSError:
        return True


def broker_call(address, request):
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as connection:
        connection.settimeout(TIMEOUT)
        connection.connect(address)
        connection.sendall(json.dumps(request).encode() + b"\n")
        with connection.makefile("rb") as response:
            return json.loads(response.readline())


def broker_stream(address, sid):
    """Open a desktop stream through the broker, as the gateway does; returns it after the RFB banner."""
    connection = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    connection.settimeout(30)
    connection.connect(address)
    connection.sendall(json.dumps({"action": "connect", "id": sid}).encode() + b"\n")
    data = b""
    while b"\n" not in data or len(data.split(b"\n", 1)[1]) < 12:
        piece = connection.recv(4096)
        assert piece, f"the broker closed the stream: {data!r}"
        data += piece
    header, rest = data.split(b"\n", 1)
    assert json.loads(header) == {"ok": True, "data": None}, header
    assert rest[:12] == b"RFB 003.008\n", rest
    return connection


def check_broker(environment, session, scratch):
    """The host broker of the web gateway, run privately on a scratch socket against the test session."""
    runtime = scratch / "broker"
    runtime.mkdir(mode=0o700)
    address = str(runtime / "broker.sock")
    process = subprocess.Popen([sys.executable, str(SOURCE / "raven_web_host.py"), RAVEN, address], env=environment,
                               stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    sid = session.sid

    def listed():
        reply = broker_call(address, {"action": "list"})
        assert reply["ok"], reply
        return next(item for item in reply["data"] if item["id"] == sid)

    def refused(request, code):
        reply = broker_call(address, request)
        assert reply.get("ok") is False and reply.get("code") == code, (request, reply)

    try:
        wait(lambda: Path(address).exists(), "the broker socket", 10)
        item = listed()
        assert item["state"] == "running" and item["memory"] >= MARKER_SIZE, item
        assert isinstance(item["cpu"], float) and item["viewers"] == {"x11": 0, "web": 0}, item
        stream = broker_stream(address, sid)
        item = listed()
        assert item["viewers"] == {"x11": 0, "web": 1}, item
        generation = item["generation"]
        reply = broker_call(address, {"action": "pause", "id": sid, "generation": generation})
        assert reply["ok"], reply
        paused = reply["data"]
        assert (paused["state"], paused["cpu"], paused["viewers"]) == ("paused", 0.0, {"x11": 0, "web": 0}), paused
        assert paused["memory"] >= MARKER_SIZE, paused
        assert closed(stream), "the web stream survived the pause"
        stream.close()
        for request in ({"action": "open", "id": sid}, {"action": "connect", "id": sid},
                        {"action": "run", "id": sid, "command": "true", "generation": generation},
                        {"action": "pause", "id": sid, "generation": generation}):
            refused(request, "session_paused")
        reply = broker_call(address, {"action": "resume", "id": sid, "generation": "0" * 64})
        assert reply == {"ok": False, "error": "Session changed; refresh the session list"}, reply
        assert session.state()["Status"] == "paused"
        reply = broker_call(address, {"action": "resume", "id": sid, "generation": generation})
        assert reply["ok"] and reply["data"]["state"] == "running", reply
        refused({"action": "resume", "id": sid, "generation": generation}, "session_not_paused")
        broker_stream(address, sid).close()
        return {"paused": paused, "resumed": reply["data"]}
    finally:
        process.terminate()
        process.wait(timeout=TIMEOUT)


def main():
    scratch = Path(tempfile.mkdtemp(prefix="raven-pause-e2e-"))
    environment = dict(os.environ)
    environment.pop("DISPLAY", None)
    for variable, directory in (("HOME", "home"), ("XDG_CONFIG_HOME", "config"),
                                ("XDG_STATE_HOME", "state"), ("XDG_RUNTIME_DIR", "runtime")):
        (scratch / directory).mkdir(mode=0o700)
        environment[variable] = str(scratch / directory)
    environment["RAVEN_IMAGE"] = IMAGE
    previous_hostd = f"@raven-host-{os.getuid()}" in Path("/proc/net/unix").read_text()
    downloads = scratch / "home" / "Downloads"
    name = f"m7e2e-{secrets.token_hex(3)}"
    sid = None
    report = {}
    try:
        result = raven(environment, "new", "-d", "-s", name, "-w", "xmonad", "-g", "1280x800", "xterm")
        sid = re.search(r"raven: created session ([0-9]+)", result.stderr)[1]
        session = Session(environment, sid)
        statedir = Path(environment["XDG_STATE_HOME"]) / "raven" / socket.gethostname()

        # The running state that has to survive
        token = "m7-" + secrets.token_hex(8)
        (downloads / "m7-marker.py").write_text(MARKER)
        raven(environment, "run", "-t", sid, f"python3 /home/raven/Downloads/m7-marker.py {token} {MARKER_SIZE}")
        before = wait(lambda: (downloads / "m7-report").exists() and (downloads / "m7-report").read_text(),
                      "the marker process", 60).split()
        assert before[1:4] == [token] * 3, before
        marker = next(process for process in session.processes()
                      if process[1] == "python3" and "m7-marker.py" in process[3])
        rss = int(re.search(r"^VmRSS:\s+([0-9]+) kB", Path(f"/proc/{marker[0]}/status").read_text(), re.M)[1])
        assert rss * 1024 >= MARKER_SIZE, rss

        raven(environment, "run", "-t", sid,
              "xterm -T m7input -e sh -c 'read line; printf %s \"$line\" > /home/raven/Downloads/m7-got'")
        terminal = wait(lambda: session.windows("--name", "^m7input$"), "the input xterm")[0]
        session.x("xdotool", "windowactivate", "--sync", terminal)
        session.x("xdotool", "type", "--delay", "40", TYPED)

        # An X11 viewer inside the session (as raven attach runs it) and a host TCP client (as the broker)
        subprocess.run(["docker", "exec", "-d", "-e", f"DISPLAY=:{session.display}",
                        "-e", "XAUTHORITY=/home/raven/.Xauthority", session.container, "xtigervncviewer",
                        "-Shared=1", "-RemoteResize=0", "-passwd", "/home/raven/.vnc/passwd",
                        f"localhost::{session.port}"], check=True, timeout=TIMEOUT)
        wait(lambda: session.vnc_clients() >= 1, "the X11 viewer to connect")
        password = subprocess.run(["docker", "exec", session.container, "cat", "/home/raven/.vnc/passwd"],
                                  capture_output=True, check=True, timeout=TIMEOUT).stdout
        web, web_kind = rfb_client(session.port, password)
        wait(lambda: session.vnc_clients() >= 2, "the web-style client to connect")
        time.sleep(1)
        assert session.vnc_clients() == 2, session.vnc_clients()
        row, _ = session.ls_row()
        assert (row["state"], row["x11"], row["web"]) == ("running", "1", "1"), row
        assert parse_size(row["mem"]) >= MARKER_SIZE, row
        report["running_ls"] = row

        config_home = Path(environment["XDG_CONFIG_HOME"]) / "raven" / "home"
        config_home.mkdir(parents=True)
        (config_home / ".m7-home-test").write_text("pushed only to running sessions\n")
        started_at = session.state()["StartedAt"]

        # Pause
        result = raven(environment, "pause", "-t", sid)
        report["pause_stderr"] = result.stderr
        for text in (f"paused session {sid} ({name})", "until resumed", "a server reboot loses the paused state",
                     "run --host", f"raven resume {sid}"):
            assert text in result.stderr, (text, result.stderr)
        state = session.state()
        assert state["Status"] == "paused" and state["Paused"], state
        assert "frozen 1" in (session.cgroup() / "cgroup.events").read_text()
        assert closed(web), "the host TCP client of Xvnc was not disconnected"
        assert session.vnc_clients() == 0, session.vnc_clients()
        assert not [p for p in session.processes() if p[1] in ("xtigervncviewer", "xtightvncviewer")], \
            session.processes()
        counter = (downloads / "m7-counter").read_text()
        usage, memory = session.cpu_usec(), session.memory()
        time.sleep(3.5)
        assert (downloads / "m7-counter").read_text() == counter, "the counter moved during the pause"
        assert session.cpu_usec() == usage, (usage, session.cpu_usec())
        assert memory >= MARKER_SIZE and session.memory() >= MARKER_SIZE, (memory, session.memory())
        rss = int(re.search(r"^VmRSS:\s+([0-9]+) kB", Path(f"/proc/{marker[0]}/status").read_text(), re.M)[1])
        assert rss * 1024 >= MARKER_SIZE, rss
        report["paused_memory"] = session.memory()
        row, lines = session.ls_row()
        assert (row["state"], row["x11"], row["web"], row["cpu"]) == ("paused", "-", "-", "0%"), row
        assert parse_size(row["mem"]) >= MARKER_SIZE, row
        assert lines[-1] == "paused sessions keep their memory until resumed; a server reboot loses them", lines
        report["paused_ls"] = lines

        # Nothing may start, exec into or change the paused session
        hint = f"session {sid} is paused; resume it first: raven resume {sid}"
        assert not Path("/tmp/.X11-unix/X59999").exists()
        for arguments, display in ((("attach", sid), ":59999"), (("run", "-t", sid, "xterm"), None),
                                   (("run", "-t", sid, "--host", "true"), None), (("shell", "-t", sid), None),
                                   (("menu", "show", "-t", sid), None), (("home", "sync", "-t", sid), None),
                                   (("wallpaper", "color", "-t", sid, "#24303c"), None)):
            result = raven(environment, *arguments, check=False, display=display)
            assert result.returncode == 1 and hint in result.stderr, (arguments, result.returncode, result.stderr)
        result = raven(environment, "pause", sid, check=False)
        assert result.returncode == 1 and f"session {sid} is already paused" in result.stderr, result.stderr
        state = session.state()
        assert state["Status"] == "paused" and state["StartedAt"] == started_at, state
        assert not (statedir / f"{sid}.home").exists(), "home files were pushed into the paused session"
        assert (downloads / "m7-counter").read_text() == counter
        assert not (downloads / "m7-got").exists()

        # Resume
        result = raven(environment, "resume", sid)
        assert f"raven attach {sid}" in result.stderr, result.stderr
        assert session.state()["Status"] == "running"
        result = raven(environment, "resume", sid, check=False)
        assert result.returncode == 1 and f"session {sid} is not paused" in result.stderr, result.stderr
        wait(lambda: int((downloads / "m7-counter").read_text() or 0) > int(counter), "the counter to move", 10)
        (downloads / "m7-report").unlink()
        session.x("kill", "-USR1", before[0])
        after = wait(lambda: (downloads / "m7-report").exists() and (downloads / "m7-report").read_text(),
                     "the marker report").split()
        assert after == before, (before, after)
        assert any(process[0] == marker[0] for process in session.processes()), "the marker process changed"
        session.x("xdotool", "windowactivate", "--sync", terminal)
        session.x("xdotool", "key", "Return")
        got = wait(lambda: (downloads / "m7-got").exists() and (downloads / "m7-got").read_text(), "the xterm input")
        assert got == TYPED, got
        row, _ = session.ls_row()
        assert row["state"] == "running" and row["cpu"].endswith("%") and row["cpu"] != "?", row
        report["resumed_ls"] = row

        # Apps after resume
        raven(environment, "run", "-t", sid, "pcmanfm /home/raven")
        wait(lambda: session.windows("--class", "pcmanfm"), "a PCManFM window", 60)
        raven(environment, "run", "-t", sid, "microsoft-edge")
        wait(lambda: session.windows("--class", "microsoft-edge"), "a Microsoft Edge window", 90)
        kime = [process for process in session.processes() if process[1].startswith("kime")]
        assert kime and all(process[2] not in ("T", "t") for process in kime), kime
        report["kime"] = [(process[1], process[2]) for process in kime]
        report["broker"] = check_broker(environment, session, scratch)

        # A paused session can still be deleted
        raven(environment, "pause", "-t", sid)
        raven(environment, "kill", "-t", sid)
        gone = subprocess.run(["docker", "inspect", session.container], capture_output=True)
        assert gone.returncode != 0, "the container is still there"
        volume = subprocess.run(["docker", "volume", "inspect", f"{session.container.rsplit('-', 1)[0]}-home-{sid}"],
                                capture_output=True)
        assert volume.returncode != 0, "the home volume is still there"
        sid = None
        report.update(pause_e2e="passed", web_client=web_kind)
        print(json.dumps(report, indent=2))
    finally:
        if sid is not None:
            raven(environment, "kill", "-t", sid, check=False)
        if not previous_hostd:
            raven(environment, "hostd", "stop", check=False)
        shutil.rmtree(scratch, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(main())
