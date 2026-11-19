"""M11 checkpoint ladder. Runs inside the lab container as root (see run_lab.py).

  python3 ladder.py phase1   rungs 1-4, extra checks, checkpoints left for the reboot
  python3 ladder.py phase2   after the lab restarted: restore those checkpoints, print the summary

Sessions are made with the repo's raven CLI (mounted read-only at /src/vdesk), run as the lab user
with its own HOME and XDG directories, so the containers get run_container's exact options: --init,
--network host (the lab's network namespace), --restart unless-stopped, memory/cpu/pids limits,
seccomp and shm-size labels, uid 60000, --group-add, the home volume and the Downloads bind mount.
CRIU options reach runc through /etc/criu/runc.conf, rewritten before each checkpoint and restore.
"""

import json
import os
from pathlib import Path
import re
import shutil
import socket
import struct
import subprocess
import sys
import time
import traceback

SOURCE = Path("/src/vdesk")
RAVEN = str(SOURCE / "raven")
STATE_DIR = Path("/var/lib/docker/raven-lab")
STATE = STATE_DIR / "state.json"
LOGS = STATE_DIR / "criu-logs"
CRIU_CONF = Path("/etc/criu/runc.conf")
CRIU_BASE = Path("/etc/criu/runc.conf.base")
CRIU_LOG = Path("/var/log/criu/criu.log")
LAB_HOME = Path(os.environ.get("LAB_RAVEN_HOME", "/labnfs"))
DOWNLOADS = LAB_HOME / "Downloads"
XDG = Path("/home/lab/xdg")
TIMEOUT = 300
FIXED_KEY = bytes(int(f"{value:08b}"[::-1], 2) for value in (23, 82, 107, 6, 35, 78, 88, 7))
MARKER_SIZE = 64 * 1024 * 1024
KEY_ENTER = 0xFF0D

MARKER = r'''
import hashlib, os, signal, sys, time
token, size, out = sys.argv[1].encode(), int(sys.argv[2]), sys.argv[3]
offsets = (0, size // 2, size - 64)
buffer = bytearray(b"\xa5") * size  # written, so every page is resident
for offset in offsets:
    buffer[offset:offset + len(token)] = token
count = 0

def report(*_):
    found = " ".join(bytes(buffer[o:o + len(token)]).decode() for o in offsets)
    with open(out + ".tmp", "w") as f:
        f.write(f"{os.getpid()} {count} {found} {hashlib.sha256(buffer).hexdigest()}\n")
    os.replace(out + ".tmp", out)

signal.signal(signal.SIGUSR1, report)
report()
while True:
    count += 1
    time.sleep(0.1)
'''

# Holds a file open and reports what it reads from it, to compare a restored process with the file
HOLDER = r'''
import os, signal, sys, time
f = open(sys.argv[1], "r")
out = sys.argv[2]

def report(*_):
    f.seek(0)
    with open(out + ".tmp", "w") as o:
        o.write(f"{os.getpid()} {os.fstat(f.fileno()).st_size} {f.read().strip()}\n")
    os.replace(out + ".tmp", out)

signal.signal(signal.SIGUSR1, report)
report()
while True:
    time.sleep(1)
'''

EDGE_PAGE = ('<!doctype html><title>m11:</title>'
             '<input autofocus oninput="document.title=\'m11:\'+this.value">')


# ---------------------------------------------------------------- helpers

def log(*parts):
    print("[ladder]", *parts, file=sys.stderr, flush=True)


def run(command, check=True, timeout=TIMEOUT, input=None, env=None):
    result = subprocess.run(command, capture_output=True, text=True, timeout=timeout, input=input, env=env)
    if check and result.returncode:
        raise RuntimeError(f"{' '.join(command)} failed ({result.returncode}): {result.stderr.strip()[-800:]}")
    return result


def docker(*arguments, check=True, timeout=TIMEOUT):
    return run(["docker", *arguments], check=check, timeout=timeout)


def wait(condition, what, seconds=60, interval=0.3):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        try:
            value = condition()
        except Exception:
            value = None
        if value:
            return value
        time.sleep(interval)
    raise RuntimeError(f"timed out waiting for {what}")


def lab_user_env():
    return {"PATH": "/usr/sbin:/usr/bin:/sbin:/bin", "HOME": str(LAB_HOME), "USER": "lab", "LANG": "C.UTF-8",
            "XDG_CONFIG_HOME": str(XDG / "config"), "XDG_STATE_HOME": str(XDG / "state"),
            "XDG_RUNTIME_DIR": str(XDG / "run"), "XDG_DATA_HOME": str(XDG / "data")}


def as_lab(*command, image=None, check=True):
    env = lab_user_env()
    if image:
        env["RAVEN_IMAGE"] = image
    return run(["runuser", "-u", "lab", "--", "env", "-i", *[f"{k}={v}" for k, v in env.items()], *command],
               check=check)


def raven(*arguments, image="raven:latest", check=True):
    return as_lab(RAVEN, *arguments, image=image, check=check)


def put_file(path, text):
    """Write a file in the NFS Downloads as the lab user (root is squashed on NFS)."""
    subprocess.run(["runuser", "-u", "lab", "--", "sh", "-c", 'umask 002; cat > "$1"', "sh", str(path)],
                   input=text, text=True, check=True, timeout=TIMEOUT)


def set_criu(options):
    text = CRIU_BASE.read_text() + "".join(f"{option}\n" for option in options)
    CRIU_CONF.write_text(text)


def criu_errors(limit=6):
    try:
        lines = CRIU_LOG.read_text(errors="replace").splitlines()
    except FileNotFoundError:
        return []
    return [line.strip() for line in lines if "Error" in line][:limit]


def keep_log(tag):
    LOGS.mkdir(parents=True, exist_ok=True)
    if CRIU_LOG.exists():
        shutil.copy(CRIU_LOG, LOGS / f"{tag}.log")


def tree_bytes(path):
    total = 0
    for root, _, files in os.walk(path):
        for name in files:
            try:
                total += os.lstat(os.path.join(root, name)).st_size
            except FileNotFoundError:
                pass
    return total


def container_id(name):
    return docker("inspect", "-f", "{{.Id}}", name).stdout.strip()


def checkpoint_path(name, cp):
    return Path(f"/var/lib/docker/containers/{container_id(name)}/checkpoints/{cp}")


def state_of(name):
    return json.loads(docker("inspect", name).stdout)[0]["State"]


def cgroup_memory(name):
    try:
        pid = state_of(name)["Pid"]
        path = next(line[3:] for line in Path(f"/proc/{pid}/cgroup").read_text().splitlines()
                    if line.startswith("0::"))
        return int(Path(f"/sys/fs/cgroup{path}/memory.current").read_text())
    except Exception:
        return None


def checkpoint(name, cp, options, tag, leave_running=False, checkpoint_dir=None):
    set_criu(options)
    CRIU_LOG.unlink(missing_ok=True)
    arguments = ["checkpoint", "create"]
    if leave_running:
        arguments.append("--leave-running")
    if checkpoint_dir:
        arguments += ["--checkpoint-dir", checkpoint_dir]
    memory = cgroup_memory(name)
    started = time.monotonic()
    result = docker(*arguments, name, cp, check=False)
    seconds = round(time.monotonic() - started, 2)
    keep_log(f"{tag}-dump")
    entry = {"step": f"{tag} dump", "options": options, "ok": result.returncode == 0, "seconds": seconds,
             "memory_before": memory}
    if result.returncode == 0:
        path = Path(checkpoint_dir) / cp if checkpoint_dir else checkpoint_path(name, cp)
        entry["bytes"] = tree_bytes(path)
    else:
        entry["error"] = result.stderr.strip()[-400:]
        entry["criu_errors"] = criu_errors()
    log(tag, "dump", options, "ok" if entry["ok"] else "FAILED", seconds, "s", entry.get("bytes", ""),
        entry.get("criu_errors", ""))
    return entry


def restore(name, cp, options, tag):
    set_criu(options)
    CRIU_LOG.unlink(missing_ok=True)
    started = time.monotonic()
    result = docker("start", "--checkpoint", cp, name, check=False)
    seconds = round(time.monotonic() - started, 2)
    keep_log(f"{tag}-restore")
    entry = {"step": f"{tag} restore", "options": options, "ok": result.returncode == 0, "seconds": seconds}
    if result.returncode:
        entry["error"] = result.stderr.strip()[-400:]
        entry["criu_errors"] = criu_errors()
    log(tag, "restore", options, "ok" if entry["ok"] else "FAILED", seconds, "s", entry.get("error", ""),
        entry.get("criu_errors", ""))
    return entry


def guarded(results, key, function, *arguments):
    """Run one check; an exception becomes a recorded failure instead of ending the ladder."""
    try:
        results[key] = function(*arguments)
    except Exception as error:  # the ladder records failures and goes on
        results[key] = {"ok": False, "exception": f"{type(error).__name__}: {error}",
                        "trace": traceback.format_exc()[-1500:]}
        log(key, "EXCEPTION", error)
    return results[key]


def ensure_running(session, candidates, tag):
    """Bring a session back after a step left it stopped, from the newest checkpoint that restores."""
    for cp, options in candidates:
        if state_of(session.container)["Running"]:
            return True
        restore(session.container, cp, options, f"{tag}-recover-{cp}")
    return state_of(session.container)["Running"]


# ---------------------------------------------------------------- VNC client (like the web broker)

class Rfb:
    def __init__(self, port, password_file):
        self.connection = socket.create_connection(("127.0.0.1", port), timeout=15)
        assert self.exact(12) == b"RFB 003.008\n"
        self.connection.sendall(b"RFB 003.008\n")
        types = self.exact(self.exact(1)[0])
        assert 2 in types, types
        import warnings
        warnings.filterwarnings("ignore")
        from cryptography.hazmat.primitives.ciphers import Cipher, modes
        try:
            from cryptography.hazmat.decrepit.ciphers.algorithms import TripleDES
        except ImportError:  # python3-cryptography 41 in Ubuntu 24.04
            from cryptography.hazmat.primitives.ciphers.algorithms import TripleDES

        def des(key, data, decrypt=False):
            cipher = Cipher(TripleDES(key), modes.ECB())
            operation = cipher.decryptor() if decrypt else cipher.encryptor()
            return operation.update(data) + operation.finalize()

        password = des(FIXED_KEY, password_file, decrypt=True).split(b"\0", 1)[0]
        key = bytes(int(f"{value:08b}"[::-1], 2) for value in password.ljust(8, b"\0")[:8])
        self.connection.sendall(b"\x02")
        self.connection.sendall(des(key, self.exact(16)))
        assert struct.unpack(">I", self.exact(4))[0] == 0, "VNC authentication failed"
        self.connection.sendall(b"\x01")
        init = self.exact(20)
        self.width, self.height = struct.unpack(">HH", init[:4])
        self.name = self.exact(struct.unpack(">I", self.exact(4))[0]).decode(errors="replace")
        self.connection.sendall(struct.pack(">BxHiii", 2, 3, 0, -223, -308))

    def exact(self, size):
        data = b""
        while len(data) < size:
            piece = self.connection.recv(size - len(data))
            if not piece:
                raise RuntimeError("Xvnc closed the connection")
            data += piece
        return data

    def frame(self):
        """Ask for the whole screen and wait for the first FramebufferUpdate message."""
        self.connection.sendall(struct.pack(">BBHHHH", 3, 0, 0, 0, self.width, self.height))
        while True:
            kind = self.exact(1)[0]
            if kind == 0:
                return True
            if kind == 2:
                continue
            if kind == 3:
                self.exact(3)
                self.exact(struct.unpack(">I", self.exact(4))[0])
                continue
            raise RuntimeError(f"unexpected RFB message {kind}")

    def key(self, keysym):
        self.connection.sendall(struct.pack(">BBxxI", 4, 1, keysym) + struct.pack(">BBxxI", 4, 0, keysym))
        time.sleep(0.03)

    def type(self, text):
        for character in text:
            self.key(ord(character))

    def click(self, x, y):
        for mask in (0, 1, 0):
            self.connection.sendall(struct.pack(">BBHH", 5, mask, x, y))
            time.sleep(0.1)

    def close(self):
        self.connection.close()


# ---------------------------------------------------------------- sessions

class Session:
    def __init__(self, sid, image):
        self.sid, self.image = sid, image
        self.container = f"raven-lab-{sid}"
        labels = json.loads(docker("inspect", self.container).stdout)[0]["Config"]["Labels"]
        self.display, self.port = labels["raven.display"], int(labels["raven.port"])

    @classmethod
    def new(cls, name, wm, command, image):
        result = raven("new", "-d", "-s", name, "-w", wm, "-g", "1280x800", command, image=image)
        sid = re.search(r"created session ([0-9]+)", result.stderr)[1]
        session = cls(sid, image)
        wait(lambda: session.windows(), "the session's X server")
        return session

    def x(self, *arguments, check=True):
        return docker("exec", "-e", f"DISPLAY=:{self.display}", "-e", "XAUTHORITY=/home/raven/.Xauthority",
                      "-w", "/home/raven", self.container, *arguments, check=check)

    def spawn(self, command):
        """Start an app in the session's init tree, as the menu does: its parent exits, so it is
        reparented to the container's init. Apps started by docker exec (raven run) stay outside
        that tree and are not part of a checkpoint."""
        self.x("sh", "-c", f'umask 002; setsid sh -c {shell_quote(command)} </dev/null >/dev/null 2>&1 &')

    def pids(self, pattern):
        return self.x("pgrep", "-f", pattern, check=False).stdout.split()

    def windows(self):
        out = self.x("xwininfo", "-root", "-children").stdout
        return sorted([list(found) for found in
                       re.findall(r"^\s+(0x[0-9a-f]+) (\".*?\"|\(has no name\))", out, re.M)])

    def window(self, name):
        out = self.x("xwininfo", "-root", "-tree").stdout
        match = re.search(r"^\s+(0x[0-9a-f]+) \"" + re.escape(name) + r"\"", out, re.M)
        return match and match[1]

    def geometry(self, window):
        out = self.x("xwininfo", "-id", window).stdout
        get = lambda label: int(re.search(label + r":\s+(-?[0-9]+)", out)[1])
        return (get("Absolute upper-left X") + get("Width") // 2, get("Absolute upper-left Y") + get("Height") // 2)

    def rfb(self):
        password = subprocess.run(["docker", "exec", self.container, "cat", "/home/raven/.vnc/passwd"],
                                  capture_output=True, check=True, timeout=TIMEOUT).stdout
        return Rfb(self.port, password)

    def type_into(self, window, text, enter=False):
        client = self.rfb()
        try:
            client.click(*self.geometry(window))
            time.sleep(0.4)
            client.type(text)
            if enter:
                client.key(KEY_ENTER)
            time.sleep(0.5)
        finally:
            client.close()

    def marker(self, tag):
        """A python3 process in the init tree holding a 64 MiB RAM marker."""
        token = f"{tag}-{os.urandom(4).hex()}"
        self.spawn(f"exec python3 /home/raven/Downloads/m11-marker.py {token} {MARKER_SIZE} /tmp/{tag}.report")
        report = wait(lambda: self.report(tag), f"marker {tag}")
        return token, report

    def report(self, tag, refresh=False):
        if refresh:
            pid = self.x("cat", f"/tmp/{tag}.report").stdout.split()[0]
            self.x("rm", "-f", f"/tmp/{tag}.report")
            self.x("kill", "-USR1", pid)
            wait(lambda: self.x("test", "-s", f"/tmp/{tag}.report", check=False).returncode == 0, f"report {tag}")
        result = self.x("cat", f"/tmp/{tag}.report", check=False)
        return result.stdout.split() if result.returncode == 0 and result.stdout.strip() else None

    def processes(self):
        out = docker("top", self.container, "-o", "pid,args").stdout.splitlines()[1:]
        return [line.split(None, 1) for line in out]

    def apparmor(self):
        pid = state_of(self.container)["Pid"]
        return Path(f"/proc/{pid}/attr/current").read_text().strip() if pid else None

    def kill(self):
        raven("kill", self.sid, image=self.image, check=False)


def shell_quote(text):
    return "'" + text.replace("'", "'\"'\"'") + "'"


def stop_panel(session):
    """tint2 listens to kernel uevents; a pending uevent makes CRIU refuse the dump."""
    session.x("pkill", "-x", "tint2", check=False)
    wait(lambda: not session.pids("^tint2"), "tint2 to stop", 10)


def start_panel(session):
    session.spawn("exec raven-panel")


def prepare_desktop(session, tag):
    """State that has to survive: RAM marker, a line in the tty buffer, a line held by readline,
    a process started with raven run (docker exec, outside the init tree)."""
    state = {}
    token, report = session.marker(f"{tag}-mark")
    state["marker"] = {"token": token, "report": report}
    tty_window = wait(lambda: session.window("m11tty"), "the start command xterm")
    session.spawn(f"exec xterm -geometry 70x8+20+560 -T m11bash -e bash --norc --noprofile -i")
    bash_window = wait(lambda: session.window("m11bash"), "the bash xterm")
    time.sleep(1.5)
    tty_text = f"tty line {tag} 42"
    bash_text = f"echo bash line {tag} 43 > /home/raven/Downloads/m11-{tag}-bash"
    session.type_into(tty_window, tty_text)
    session.type_into(bash_window, bash_text)
    state["typed"] = {"tty": tty_text, "bash": bash_text, "tty_window": tty_window, "bash_window": bash_window}
    # Control: the same typing path with Enter and no checkpoint reaches the program
    session.spawn(f"exec xterm -geometry 70x8+20+300 -T m11ctl -e sh -c 'read line; printf %s \"$line\" > /home/raven/Downloads/m11-{tag}-ctl'")
    control_window = wait(lambda: session.window("m11ctl"), "the control xterm")
    time.sleep(1.5)
    session.type_into(control_window, f"control {tag}", enter=True)
    control = DOWNLOADS / f"m11-{tag}-ctl"
    state["typing_control_ok"] = wait(lambda: control.exists() and control.read_text() == f"control {tag}",
                                      "the control line", 10)
    raven("run", "-t", session.sid, "sleep 777777", image=session.image)
    wait(lambda: session.pids("sleep 777777"), "the raven run process")
    state["windows"] = session.windows()
    client = session.rfb()
    state["vnc_before"] = {"size": [client.width, client.height], "frame": client.frame()}
    client.close()
    return state


def before_dump(session, tag, state):
    """Record the windows and a fresh marker report just before a dump."""
    state["windows"] = session.windows()
    state["marker"]["report"] = session.report(f"{tag}-mark", refresh=True)


def verify_desktop(session, tag, state):
    """After a restore: same windows, VNC handshake and a frame, marker intact and still counting,
    typed lines submitted by Enter, raven run process gone, new X clients and D-Bus work."""
    checks = {}
    client = session.rfb()
    checks["vnc_handshake"] = True
    checks["vnc_frame"] = client.frame()
    client.close()
    windows = session.windows()
    checks["same_windows"] = windows == state["windows"]
    if not checks["same_windows"]:
        checks["windows_before"], checks["windows_after"] = state["windows"], windows
    marker = state["marker"]
    before = marker["report"]
    after = session.report(f"{tag}-mark", refresh=True)
    checks["marker_same_pid"] = after[0] == before[0]
    checks["marker_token"] = after[2:5] == [marker["token"]] * 3
    checks["marker_sha_same"] = after[5] == before[5]
    checks["marker_count"] = [int(before[1]), int(after[1])]
    checks["marker_kept_counting"] = int(after[1]) >= int(before[1])
    typed = state["typed"]
    got_tty = DOWNLOADS / f"m11-{tag}-tty"
    got_bash = DOWNLOADS / f"m11-{tag}-bash"
    session.type_into(typed["tty_window"], "", enter=True)
    session.type_into(typed["bash_window"], "", enter=True)
    time.sleep(1)
    checks["tty_line"] = got_tty.read_text() if got_tty.exists() else None
    checks["tty_line_ok"] = checks["tty_line"] == typed["tty"]
    checks["bash_line"] = got_bash.read_text().strip() if got_bash.exists() else None
    checks["bash_line_ok"] = checks["bash_line"] == f"bash line {tag} 43"
    checks["raven_run_process_lost"] = not session.pids("sleep 777777")
    checks["new_x_client"] = session.x("xwininfo", "-root", check=False).returncode == 0
    bus = session.x("dbus-send", "--session", "--print-reply", "--dest=org.freedesktop.DBus", "/",
                    "org.freedesktop.DBus.ListNames", check=False)
    checks["dbus"] = bus.returncode == 0
    checks["apparmor"] = session.apparmor()
    checks["typing_control_ok"] = state.get("typing_control_ok")
    checks["ok"] = all(checks[k] for k in ("vnc_frame", "same_windows", "marker_same_pid", "marker_token",
                                             "marker_sha_same", "marker_kept_counting", "bash_line_ok",
                                             "new_x_client", "dbus"))
    return checks


# ---------------------------------------------------------------- rungs

def versions():
    info = json.loads(docker("info", "--format", "{{json .}}").stdout)
    release = dict(line.split("=", 1) for line in Path("/etc/os-release").read_text().splitlines() if "=" in line)
    criu_check = run(["criu", "check", "--all"], check=False)
    return {
        "lab_os": release.get("PRETTY_NAME", "").strip('"'),
        "kernel": os.uname().release,
        "docker": info["ServerVersion"], "experimental": info["ExperimentalBuild"],
        "storage": info["Driver"], "cgroup": f"v{info['CgroupVersion']} {info['CgroupDriver']}",
        "security": info["SecurityOptions"],
        "containerd": run(["containerd", "--version"]).stdout.strip(),
        "runc": run(["runc", "--version"]).stdout.splitlines()[0],
        "criu": run(["criu", "--version"]).stdout.split()[1],
        "criu_check_all": (criu_check.stdout + criu_check.stderr).strip().splitlines()[-4:],
        "dpkg": run(["dpkg-query", "-W", "docker.io", "containerd", "runc"]).stdout.split("\n")[:3],
    }


def raven_like_options(name, volume):
    return ["--name", name, "--init", "--restart", "unless-stopped", "--network", "host",
            "--memory", "2g", "--memory-swap", "2g", "--cpus", "2", "--pids-limit", "512",
            "--security-opt", "seccomp=unconfined", "--shm-size", "1024m", "--group-add", str(os.environ["LAB_GID"]),
            "-v", f"{volume}:/home/raven", "-v", f"{DOWNLOADS}:/home/raven/Downloads",
            "--entrypoint", "python3", "raven:latest", "/home/raven/Downloads/m11-marker.py"]


def tiny(name, network, token, extra=()):
    docker("rm", "-f", name, check=False)
    arguments = ["run", "-d", "--name", name, *extra]
    if network:
        arguments += ["--network", network]
    docker(*arguments, "-v", f"{DOWNLOADS}:/m:ro", "--entrypoint", "python3", "raven:latest",
           "/m/m11-marker.py", token, str(MARKER_SIZE), "/tmp/report")


def tiny_report(name):
    """Ask the marker for a report with docker exec: docker kill -s would mark the container
    as manually stopped and hide the restart policy."""
    # The report file lives in the container's writable layer, which a new container lacks
    pid = docker("exec", name, "pgrep", "-f", "m11-marker.py").stdout.split()[0]
    docker("exec", name, "rm", "-f", "/tmp/report")
    docker("exec", name, "kill", "-USR1", pid)
    wait(lambda: docker("exec", name, "test", "-s", "/tmp/report", check=False).returncode == 0, "report")
    return docker("exec", name, "cat", "/tmp/report").stdout.split()


def rung1():
    result = {"rung": 1, "name": "작은 메모리 프로세스 (python3, 64 MiB marker)", "attempts": [], "checks": {}}
    attempts, checks = result["attempts"], result["checks"]

    # Plain containers: host, default bridge, none
    for network in ("host", "bridge", "none"):
        name = f"m11-r1-{network}"
        token = f"r1{network}"
        tiny(name, network, token)
        wait(lambda: docker("exec", name, "test", "-s", "/tmp/report", check=False).returncode == 0, "marker")
        time.sleep(1)
        before = tiny_report(name)
        attempts.append(checkpoint(name, "c1", [], f"r1-{network}"))
        entry = restore(name, "c1", [], f"r1-{network}")
        attempts.append(entry)
        if entry["ok"]:
            time.sleep(1)
            after = tiny_report(name)
            checks[f"{network}_same_pid_token_sha"] = after[0] == before[0] and after[2:] == before[2:]
            checks[f"{network}_count"] = [int(before[1]), int(after[1])]
        docker("rm", "-f", name, check=False)

    # Raven's run_container options (named volume, Downloads bind, --init, limits, unless-stopped)
    name, volume = "m11-r1-raven", "m11-r1-vol"
    docker("rm", "-f", name, check=False)
    docker("volume", "rm", volume, check=False)
    docker("run", "-d", *raven_like_options(name, volume), "r1raven", str(MARKER_SIZE), "/tmp/report")
    wait(lambda: docker("exec", name, "test", "-s", "/tmp/report", check=False).returncode == 0, "marker")
    time.sleep(1)
    before = tiny_report(name)

    # --leave-running keeps the container going and leaves an older state behind
    attempts.append(checkpoint(name, "lr", [], "r1-raven-leave-running", leave_running=True))
    checks["leave_running_still_running"] = state_of(name)["Running"]
    time.sleep(1)

    # Plain checkpoint with --restart unless-stopped: the restart policy starts a fresh process
    attempts.append(checkpoint(name, "c1", [], "r1-raven-unless-stopped"))
    time.sleep(3)
    state = state_of(name)
    checks["unless_stopped_restarted_after_checkpoint"] = {
        "running": state["Running"], "restart_count": json.loads(docker("inspect", name).stdout)[0]["RestartCount"]}
    if state["Running"]:
        fresh = tiny_report(name)
        checks["unless_stopped_fresh_process_count"] = int(fresh[1])
    docker("update", "--restart", "no", name)
    docker("stop", "-t", "2", name)

    # Restore the --leave-running checkpoint: mount-v2 first, then the compat mount engine
    attempts.append(restore(name, "lr", [], "r1-raven-lr"))
    entry = restore(name, "lr", ["mntns-compat-mode"], "r1-raven-lr-compat") if not attempts[-1]["ok"] else None
    if entry:
        attempts.append(entry)
    if state_of(name)["Running"]:
        time.sleep(1)
        after = tiny_report(name)
        checks["restored_older_state"] = {"pid_same": after[0] == before[0], "sha_same": after[5] == before[5],
                                          "count_at_start": int(before[1]), "count_after": int(after[1])}

    # Checkpoint with restart policy "no": the container stays stopped
    attempts.append(checkpoint(name, "c2", [], "r1-raven-restart-no"))
    time.sleep(3)
    checks["restart_no_stays_stopped"] = not state_of(name)["Running"]
    attempts.append(restore(name, "c2", ["mntns-compat-mode"], "r1-raven-c2"))

    # A new container from a checkpoint: --checkpoint-dir on restore, then copying the images
    attempts.append(checkpoint(name, "ext", [], "r1-raven-dir", checkpoint_dir="/var/lib/docker/raven-lab/cp"))
    docker("rm", "-f", name)
    docker("create", *raven_like_options(name + "-new", volume)[0:], "r1raven", str(MARKER_SIZE), "/tmp/report")
    docker("update", "--restart", "no", name + "-new")
    custom = docker("start", "--checkpoint-dir", "/var/lib/docker/raven-lab/cp", "--checkpoint", "ext",
                    name + "-new", check=False)
    checks["custom_checkpoint_dir_restore"] = custom.stderr.strip() or "ok"
    target = Path(f"/var/lib/docker/containers/{container_id(name + '-new')}/checkpoints/ext")
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copytree("/var/lib/docker/raven-lab/cp/ext", target)
    entry = restore(name + "-new", "ext", ["mntns-compat-mode"], "r1-raven-new-container")
    attempts.append(entry)
    if entry["ok"]:
        time.sleep(1)
        after = tiny_report(name + "-new")
        checks["new_container_same_process"] = after[0] == before[0] and after[5] == before[5]
        checks["new_container_count"] = int(after[1])
    docker("rm", "-f", name + "-new", check=False)
    docker("volume", "rm", volume, check=False)

    result["ok"] = bool(checks.get("host_same_pid_token_sha")) and bool(checks.get("new_container_same_process"))
    return result


def rung2(wm):
    tag = f"r2{wm[0]}"
    result = {"rung": 2, "name": f"Xvnc + {wm} + D-Bus (raven-core:latest)", "attempts": [], "checks": {}}
    attempts = result["attempts"]
    session = Session.new(tag, wm, f"xterm -geometry 70x8+20+40 -T m11tty -e sh -c 'read line; printf %s \"$line\" > "
                          f"/home/raven/Downloads/m11-{tag}-tty'", "raven-core:latest")
    result["session"] = session.container
    state = prepare_desktop(session, tag)
    docker("update", "--restart", "no", session.container)
    result["processes"] = [p[1][:80] for p in session.processes()]
    entry = checkpoint(session.container, "c1", [], f"{tag}-panel")
    attempts.append(entry)
    if entry["ok"]:
        # The panel did not block this time; take it down anyway so later steps match
        attempts.append(restore(session.container, "c1", ["mntns-compat-mode"], f"{tag}-panel-undo"))
    stop_panel(session)
    before_dump(session, tag, state)
    entry = checkpoint(session.container, "c2", [], tag)
    attempts.append(entry)
    if not entry["ok"]:
        result["ok"] = False
        return result, session, state
    result["checks"]["stays_stopped"] = not state_of(session.container)["Running"]
    result["memory_before"] = entry["memory_before"]
    entry = restore(session.container, "c2", [], f"{tag}-mountv2")
    attempts.append(entry)
    if not entry["ok"]:
        entry = restore(session.container, "c2", ["mntns-compat-mode"], tag)
        attempts.append(entry)
    if not entry["ok"]:
        result["ok"] = False
        return result, session, state
    time.sleep(1)
    result["checks"].update(verify_desktop(session, tag, state))
    start_panel(session)
    result["checks"]["panel_restarted"] = bool(wait(lambda: session.pids("^tint2"), "tint2", 15))
    result["ok"] = result["checks"]["ok"]
    return result, session, state


def extras(session, tag, state):
    """Open VNC TCP connection, external X client (as run --host), port and display conflicts,
    open files changed while checkpointed, NFS root squash, and restore into a new container."""
    out = {}
    stop_panel(session)
    compat = ["mntns-compat-mode"]
    good = [("c2", compat)]  # checkpoints known to restore, newest last

    def back():
        return ensure_running(session, list(reversed(good)), tag)

    def tcp():
        client = session.rfb()
        client.frame()
        found = {"base": checkpoint(session.container, "tcp1", [], f"{tag}-tcp")}
        if found["base"]["ok"]:
            found["restore"] = restore(session.container, "tcp1", compat, f"{tag}-tcp")
            client.close()
            return found
        found["tcp_established"] = checkpoint(session.container, "tcp2", ["tcp-established"], f"{tag}-tcp-est")
        if found["tcp_established"]["ok"]:
            found["restore"] = restore(session.container, "tcp2", ["tcp-established", *compat], f"{tag}-tcp-est")
            if found["restore"]["ok"]:
                try:
                    found["viewer_survived"] = client.frame()
                except Exception as error:
                    found["viewer_survived"] = f"{type(error).__name__}: {error}"
            else:
                found["restore_tcp_close"] = restore(session.container, "tcp2", ["tcp-close", *compat],
                                                     f"{tag}-tcp-close")
        client.close()
        return found

    def external_x_client():
        # A process outside the container (as run --host is) connected to Xvnc's abstract socket
        holder = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        holder.connect(f"\0/tmp/.X11-unix/X{session.display}")
        time.sleep(0.5)
        found = {"base": checkpoint(session.container, "ext1", [], f"{tag}-extx")}
        if found["base"]["ok"]:
            found["restore"] = restore(session.container, "ext1", compat, f"{tag}-extx")
        else:
            found["ext_unix_sk"] = checkpoint(session.container, "ext2", ["ext-unix-sk"], f"{tag}-extx-sk")
            if found["ext_unix_sk"]["ok"]:
                found["restore"] = restore(session.container, "ext2", ["ext-unix-sk", *compat], f"{tag}-extx-sk")
        holder.close()
        return found

    def conflicts():
        found = {"dump": checkpoint(session.container, "busy", [], f"{tag}-busy")}
        if not found["dump"]["ok"]:
            return found
        good.append(("busy", compat))
        port = socket.socket()
        port.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        port.bind(("127.0.0.1", session.port))
        port.listen()
        found["vnc_port_taken"] = restore(session.container, "busy", compat, f"{tag}-busy-port")
        port.close()
        if not state_of(session.container)["Running"]:
            x = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            x.bind(f"\0/tmp/.X11-unix/X{session.display}")
            x.listen()
            found["x_abstract_socket_taken"] = restore(session.container, "busy", compat, f"{tag}-busy-x")
            x.close()
        if not state_of(session.container)["Running"]:
            found["free_again"] = restore(session.container, "busy", compat, f"{tag}-busy-free")
        return found

    def holder(path, name):
        inside = "/home/raven/Downloads/" + path.name
        session.spawn(f"exec python3 /home/raven/Downloads/m11-holder.py {inside} /tmp/{name}.report")
        return wait(lambda: session.x("cat", f"/tmp/{name}.report", check=False).stdout.split(), name)

    def holder_report(pid, name):
        session.x("rm", "-f", f"/tmp/{name}.report")
        session.x("kill", "-USR1", pid)
        return wait(lambda: session.x("cat", f"/tmp/{name}.report", check=False).stdout.split(), name)

    def files():
        found = {}
        held = DOWNLOADS / f"m11-{tag}-held.txt"
        put_file(held, "original content 1234\n")
        before = holder(held, f"{tag}-held")
        found["dump"] = checkpoint(session.container, "files", [], f"{tag}-files")
        if not found["dump"]["ok"]:
            return found
        put_file(held, "original content 1234\nadded while checkpointed\n")
        found["size_changed"] = restore(session.container, "files", compat, f"{tag}-files-size")
        if state_of(session.container)["Running"]:
            docker("stop", "-t", "2", session.container)
        put_file(held, "CHANGED! content 1234\n")
        found["same_size_changed"] = restore(session.container, "files", compat, f"{tag}-files-same")
        if found["same_size_changed"]["ok"]:
            good.append(("files", compat))
            after = holder_report(before[0], f"{tag}-held")
            found["restored_process_same_pid"] = after[0] == before[0]
            found["restored_process_reads"] = " ".join(after[2:])
        session.x("pkill", "-f", "m11-holder.py", check=False)
        return found

    def nfs_root_squash():
        # Downloads is NFS on the server; CRIU runs as root and reopens files on restore.
        # A file the session can read only through its group is not readable by squashed root.
        found = {}
        private = DOWNLOADS / f"m11-{tag}-private.txt"
        put_file(private, "group-only file\n")
        as_lab("chmod", "640", str(private))
        probe = run(["cat", str(private)], check=False)
        found["root_can_read"] = probe.returncode == 0
        found["root_read_error"] = probe.stderr.strip()
        before = holder(private, f"{tag}-private")
        found["dump"] = checkpoint(session.container, "nfs", [], f"{tag}-nfs")
        if found["dump"]["ok"]:
            found["restore"] = restore(session.container, "nfs", compat, f"{tag}-nfs")
            if found["restore"]["ok"]:
                good.append(("nfs", compat))
                found["restored_process_reads"] = " ".join(holder_report(before[0], f"{tag}-private")[2:])
        session.x("pkill", "-f", "m11-holder.py", check=False)
        return found

    def new_container():
        found = {"dump": checkpoint(session.container, "move", [], f"{tag}-move",
                                    checkpoint_dir="/var/lib/docker/raven-lab/cp")}
        if not found["dump"]["ok"]:
            return found
        info = json.loads(docker("inspect", session.container).stdout)[0]
        clone = session.container + "-new"
        docker("rm", "-f", clone, check=False)
        host = info["HostConfig"]
        arguments = ["create", "--name", clone, "--init", "--network", "host", "--memory", str(host["Memory"]),
                     "--memory-swap", str(host["MemorySwap"]), "--cpus", str(host["NanoCpus"] / 1e9),
                     "--pids-limit", str(host["PidsLimit"]), "--shm-size", str(host["ShmSize"])]
        for option in host.get("SecurityOpt") or []:
            arguments += ["--security-opt", option]
        for group in host.get("GroupAdd") or []:
            arguments += ["--group-add", group]
        for variable in info["Config"]["Env"]:
            arguments += ["-e", variable]
        for mount in info["Mounts"]:
            source = mount["Name"] if mount["Type"] == "volume" else mount["Source"]
            arguments += ["-v", f"{source}:{mount['Destination']}"]
        docker(*arguments, info["Config"]["Image"])
        # The original goes away and its rootfs (/tmp: X and D-Bus sockets, lock files) with it
        docker("rm", "-f", session.container)
        target = Path(f"/var/lib/docker/containers/{container_id(clone)}/checkpoints/move")
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copytree("/var/lib/docker/raven-lab/cp/move", target)
        found["restore_new_container"] = restore(clone, "move", compat, f"{tag}-move")
        if not found["restore_new_container"]["ok"]:
            # Second try: the socket directories the old rootfs had (X, D-Bus), made in the new one
            run(["sh", "-c", "set -e; d=$(mktemp -d); mkdir -m 1777 $d/.X11-unix; mkdir -m 700 $d/raven-bus; "
                 "chown 60000:60000 $d/raven-bus; tar -C $d -cf - .X11-unix raven-bus | docker cp - \"$1\":/tmp; "
                 "rm -rf $d", "sh", clone])
            found["restore_new_container_with_dirs"] = restore(clone, "move", compat, f"{tag}-move-dirs")
            if found["restore_new_container_with_dirs"]["ok"]:
                probe = docker("exec", "-e", f"DISPLAY=:{session.display}", "-e", "XAUTHORITY=/home/raven/.Xauthority",
                               clone, "xwininfo", "-root", "-children", check=False)
                found["new_container_x_works"] = probe.returncode == 0
                found["new_container_windows"] = len(re.findall(r"^\s+0x", probe.stdout, re.M))
        docker("rm", "-f", clone, check=False)
        return found

    for key, function in (("tcp_viewer_connected", tcp), ("external_x_client", external_x_client),
                          ("port_and_display_conflict", conflicts), ("open_file_changed", files),
                          ("nfs_root_squash", nfs_root_squash)):
        guarded(out, key, function)
        if not back():
            out["stopped_after"] = key
            log("extras: session could not be brought back after", key)
            return out
    guarded(out, "restore_into_new_container", new_container)
    return out


def rung3():
    tag = "r3"
    result = {"rung": 3, "name": "PCManFM + kime (raven:latest)", "attempts": [], "checks": {}}
    attempts, checks = result["attempts"], result["checks"]
    session = Session.new(tag, "xmonad", f"xterm -geometry 70x8+20+40 -T m11tty -e sh -c 'read line; printf %s \"$line\" > "
                          f"/home/raven/Downloads/m11-{tag}-tty'", "raven:latest")
    result["session"] = session.container
    wait(lambda: session.pids("^kime"), "kime")
    session.spawn("exec pcmanfm /home/raven/Downloads")
    wait(lambda: session.window("Downloads"), "the PCManFM window")
    state = prepare_desktop(session, tag)
    docker("update", "--restart", "no", session.container)
    stop_panel(session)
    before_dump(session, tag, state)
    kime_before = sorted(session.pids("^kime"))
    pcmanfm_before = session.pids("^pcmanfm")
    for options in ([], ["file-locks"]):
        entry = checkpoint(session.container, f"c{len(options)}", options, f"{tag}-{'-'.join(options) or 'base'}")
        attempts.append(entry)
        if entry["ok"]:
            break
    if not entry["ok"]:
        result["ok"] = False
        return result, session, state
    result["memory_before"] = entry["memory_before"]
    cp = f"c{len(options)}"
    entry = restore(session.container, cp, [*options, "mntns-compat-mode"], tag)
    attempts.append(entry)
    if not entry["ok"]:
        result["ok"] = False
        return result, session, state
    time.sleep(1)
    checks.update(verify_desktop(session, tag, state))
    checks["kime_same_pids"] = sorted(session.pids("^kime")) == kime_before
    checks["xim_server"] = "@server=kime" in session.x("xprop", "-root", "XIM_SERVERS", check=False).stdout
    checks["pcmanfm_same_pid"] = session.pids("^pcmanfm") == pcmanfm_before
    before = len(session.x("xdotool", "search", "--onlyvisible", "--class", "pcmanfm", check=False).stdout.split())
    window = session.window("Downloads")
    session.x("xdotool", "windowactivate", "--sync", str(int(window, 16)), check=False)
    session.x("xdotool", "key", "--clearmodifiers", "ctrl+n", check=False)
    after = wait(lambda: (lambda n: n if n > before else None)(
        len(session.x("xdotool", "search", "--onlyvisible", "--class", "pcmanfm", check=False).stdout.split())),
        "a new PCManFM window", 15)
    checks["pcmanfm_responds"] = [before, after]
    start_panel(session)
    result["ok"] = checks["ok"] and checks["kime_same_pids"] and checks["xim_server"] and checks["pcmanfm_same_pid"]
    return result, session, state


def rung4():
    tag = "r4"
    result = {"rung": 4, "name": "Microsoft Edge + kime (raven:latest)", "attempts": [], "checks": {}}
    attempts, checks = result["attempts"], result["checks"]
    session = Session.new(tag, "xmonad", "xterm", "raven:latest")
    result["session"] = session.container
    put_file(DOWNLOADS / "m11-edge.html", EDGE_PAGE)
    docker("update", "--restart", "no", session.container)
    stop_panel(session)

    def edge(extra):
        session.spawn(f"exec microsoft-edge {extra} file:///home/raven/Downloads/m11-edge.html")
        window = wait(lambda: session.x("xdotool", "search", "--onlyvisible", "--name", "^m11:", check=False)
                      .stdout.split(), "the Edge window", 60)[0]
        time.sleep(3)
        session.x("xdotool", "windowactivate", "--sync", window, check=False)
        session.x("xdotool", "type", "--delay", "60", "abc", check=False)
        return window, wait(lambda: (lambda n: n if "m11:abc" in n else None)(
            session.x("xdotool", "getwindowname", window).stdout.strip()), "typed title", 15)

    window, title = edge("")
    checks["title_before"] = title
    checks["processes"] = len([p for p in session.processes() if "msedge" in p[1]])
    for options in (["file-locks"], ["file-locks", "tcp-established", "ext-unix-sk"]):
        entry = checkpoint(session.container, "e" + str(len(options)), options, f"{tag}-sandbox-{len(options)}")
        attempts.append(entry)
        if entry["ok"]:
            break
    result["ok"] = False
    if entry["ok"]:
        entry = restore(session.container, "e" + str(len(options)), [*options, "mntns-compat-mode"], tag)
        attempts.append(entry)
        result["ok"] = entry["ok"]
        return result, session

    # Reference only, not a supported setup (the project never runs Edge with --no-sandbox):
    # whether anything besides the sandbox's namespaces stops the dump.
    session.x("pkill", "-f", "/opt/microsoft/msedge/", check=False)
    wait(lambda: not session.pids("/opt/microsoft/msedge/"), "Edge to exit", 20)
    reference = {"note": "--no-sandbox: 참고용, 지원 구성이 아님", "attempts": []}
    window, title = edge("--no-sandbox")
    session.x("pkill", "-x", "tint2", check=False)
    reference["processes"] = len([p for p in session.processes() if "msedge" in p[1]])
    tried = []
    for options in (["file-locks"], ["file-locks", "tcp-established"],
                    ["file-locks", "tcp-established", "ext-unix-sk"]):
        entry = checkpoint(session.container, "n" + str(len(options)), options, f"{tag}-nosandbox-{len(options)}")
        reference["attempts"].append(entry)
        tried = options
        if entry["ok"]:
            break
    if entry["ok"]:
        reference["checkpoint_bytes"] = entry["bytes"]
        entry = restore(session.container, "n" + str(len(tried)), [*tried, "mntns-compat-mode"], f"{tag}-nosandbox")
        reference["attempts"].append(entry)
        if entry["ok"]:
            time.sleep(3)
            session.x("xdotool", "windowactivate", "--sync", window, check=False)
            session.x("xdotool", "type", "--delay", "60", "def", check=False)
            time.sleep(1)
            reference["title_after_typing"] = session.x("xdotool", "getwindowname", window, check=False).stdout.strip()
            reference["renderer_kept_state"] = "m11:abcdef" in reference["title_after_typing"]
    result["no_sandbox_reference"] = reference
    return result, session


# ---------------------------------------------------------------- phases

def setup():
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    (STATE_DIR / "cp").mkdir(exist_ok=True)
    for directory in ("config", "state", "run", "data"):
        (XDG / directory).mkdir(parents=True, exist_ok=True)
    run(["chown", "-R", "lab:", str(XDG)])
    os.chmod(XDG / "run", 0o700)
    put_file(DOWNLOADS / "m11-marker.py", MARKER)
    put_file(DOWNLOADS / "m11-holder.py", HOLDER)


def save(data):
    STATE.write_text(json.dumps(data, indent=1, default=str))


def phase1():
    setup()
    data = {"versions": versions(), "rungs": [], "extras": {}, "reboot": {}}
    save(data)
    climbing = True

    rung = guarded({}, "r1", rung1)
    data["rungs"].append(rung)
    save(data)
    climbing = rung.get("ok", False)

    keep = {}
    if climbing:
        for wm in ("xmonad", "openbox"):
            try:
                result, session, state = rung2(wm)
            except Exception as error:
                result, session = {"rung": 2, "name": f"Xvnc + {wm}", "ok": False,
                                   "exception": f"{error}", "trace": traceback.format_exc()[-1500:]}, None
            data["rungs"].append(result)
            save(data)
            if result.get("ok") and wm == "xmonad":
                keep["r2"] = (session, state)
            elif session:
                session.kill()
        climbing = all(r.get("ok") for r in data["rungs"] if r.get("rung") == 2)

    if "r2" in keep:
        session, state = keep["r2"]
        data["extras"] = guarded({}, "extras", extras, session, "r2x", state)
        save(data)
        session.kill()
        docker("volume", "rm", "-f", f"raven-lab-home-{session.sid}", check=False)

    if climbing:
        try:
            result, session, state = rung3()
        except Exception as error:
            result, session = {"rung": 3, "ok": False, "exception": f"{error}",
                               "trace": traceback.format_exc()[-1500:]}, None
        data["rungs"].append(result)
        save(data)
        climbing = result.get("ok", False)
        if session:
            session.kill()

    if climbing:
        try:
            result, session = rung4()[:2]
        except Exception as error:
            result, session = {"rung": 4, "ok": False, "exception": f"{error}",
                               "trace": traceback.format_exc()[-1500:]}, None
        data["rungs"].append(result)
        save(data)
        if session:
            session.kill()

    # Checkpoints left on disk across the reboot
    boot = data["reboot"]
    try:
        session = Session.new("boot", "xmonad", "xterm -geometry 70x8+20+40 -T m11tty -e sh -c 'read line; printf %s \"$line\" > "
                              "/home/raven/Downloads/m11-boot-tty'", "raven:latest")
        session.spawn("exec pcmanfm /home/raven/Downloads")
        wait(lambda: session.window("Downloads"), "the PCManFM window")
        state = prepare_desktop(session, "boot")
        docker("update", "--restart", "no", session.container)
        stop_panel(session)
        before_dump(session, "boot", state)
        boot["session"] = {"sid": session.sid, "container": session.container, "image": session.image}
        boot["state"] = state
        boot["dump"] = checkpoint(session.container, "boot", ["file-locks"], "boot")
        # A second session keeps --restart unless-stopped through the checkpoint
        other = Session.new("bootfresh", "xmonad", "xterm", "raven-core:latest")
        stop_panel(other)
        boot["unless_stopped"] = {"container": other.container,
                                  "dump": checkpoint(other.container, "boot", [], "boot-unless")}
        time.sleep(3)
        boot["unless_stopped"]["running_after_checkpoint"] = state_of(other.container)["Running"]
    except Exception as error:
        boot["exception"] = f"{type(error).__name__}: {error}"
        boot["trace"] = traceback.format_exc()[-1500:]
    save(data)
    log("phase1 done")


def phase2():
    data = json.loads(STATE.read_text())
    boot = data["reboot"]
    if "session" in boot and boot.get("dump", {}).get("ok"):
        try:
            session = Session(boot["session"]["sid"], boot["session"]["image"])
            info = json.loads(docker("inspect", session.container).stdout)[0]
            boot["after_boot_state"] = info["State"]["Status"]
            other = boot["unless_stopped"]["container"]
            boot["unless_stopped"]["after_boot_state"] = state_of(other)["Status"]
            boot["checkpoints_after_boot"] = docker("checkpoint", "ls", session.container).stdout.split()[2:]
            boot["raven_ls_after_boot"] = raven("ls").stdout.splitlines()
            entry = restore(session.container, "boot", ["file-locks", "mntns-compat-mode"], "boot")
            boot["restore"] = entry
            if entry["ok"]:
                time.sleep(1)
                checks = verify_desktop(session, "boot", boot["state"])
                checks["pcmanfm_window"] = bool(session.window("Downloads"))
                checks["kime"] = bool(session.pids("^kime"))
                boot["checks"] = checks
                start_panel(session)
                boot["raven_ls_after_restore"] = raven("ls").stdout.splitlines()
        except Exception as error:
            boot["exception"] = f"{type(error).__name__}: {error}"
            boot["trace"] = traceback.format_exc()[-1500:]
    save(data)
    print(json.dumps(data, indent=1, default=str))


if __name__ == "__main__":
    {"phase1": phase1, "phase2": phase2}[sys.argv[1]]()
