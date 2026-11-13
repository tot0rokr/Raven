"""Move files between a browser and ~/Downloads through the real gateway and host broker, and check them from inside a
disposable desktop that shares the folder. Everything runs under a temporary HOME, so the Downloads folder is a
scratch directory, not the user's."""

import hashlib
import json
import os
from pathlib import Path
import re
import secrets
import socket
import stat
import subprocess
import tempfile

from web_config_e2e import cleanup
from web_e2e import COMMAND_TIMEOUT, SOURCE, WEB_IMAGE, provision, run

BROWSER_IMAGE = os.environ.get("RAVEN_WEB_BROWSER_IMAGE", "headless-browser:1.63.0")
SESSION_IMAGE = os.environ.get("RAVEN_IMAGE", "raven:latest")
BIG_NAME = "한글 파일 ✓.bin"
EXPECTED_BOUNDARIES = {
    "passwd-link": (400, "not_a_file"), "root-link": (400, "not_a_directory"),
    "up-link": (400, "not_a_directory"), "through-link": (400, "not_a_directory"),
    "dotdot": (400, "invalid_path"), "absolute": (400, "invalid_path"),
    "private": (403, "permission_denied"), "fifo": (400, "not_a_file"), "directory": (400, "not_a_file"),
    "missing": (404, "not_found"), "anonymous": (401, "auth_required"), "anonymous-download": (401, "auth_required"),
    "no-csrf": (403, "csrf_token_invalid"), "too-large": (413, "too_large"),
}


def in_session(container, script, data=None, *arguments):
    # Like the session's own shells (entrypoint, raven run): files it makes stay group-writable.
    result = subprocess.run(["docker", "exec", "-i", "-u", "raven", "-w", "/home/raven/Downloads", container,
                             "sh", "-c", "umask 002; " + script, "sh", *arguments], input=data, capture_output=True,
                            timeout=COMMAND_TIMEOUT)
    assert result.returncode == 0, (script, result.stderr.decode()[-2000:])
    return result.stdout.decode()


def session_hash(container, name):
    return in_session(container, 'sha256sum -- "$1" | cut -d " " -f 1', None, name).strip()


def main():
    scratch = Path(tempfile.mkdtemp(prefix="raven-web-files-e2e-"))
    suffix = secrets.token_hex(4)
    instance = "files-" + suffix
    environment = dict(os.environ)
    environment.pop("DISPLAY", None)
    for variable, directory in (("HOME", "home"), ("XDG_CONFIG_HOME", "config"),
                                ("XDG_STATE_HOME", "state"), ("XDG_RUNTIME_DIR", "runtime")):
        (scratch / directory).mkdir(mode=0o700)
        environment[variable] = str(scratch / directory)
    # The broker socket path must fit AF_UNIX's limit; the scratch path may be long.
    environment["XDG_RUNTIME_DIR"] = tempfile.mkdtemp(prefix="rt-")
    environment.update(RAVEN_WEB_IMAGE=WEB_IMAGE, RAVEN_WEB_INSTANCE=instance, RAVEN_IMAGE=SESSION_IMAGE)
    downloads = scratch / "home/Downloads"
    with socket.socket() as reservation:
        reservation.bind(("127.0.0.1", 0))
        port = reservation.getsockname()[1]
    origin = f"https://localhost:{port}"
    password = secrets.token_urlsafe(24)
    owner = re.sub(r"[^A-Za-z0-9_.-]", "_", os.environ["USER"])
    gateway = f"raven-{owner}-{instance}"
    raven = str(SOURCE / "raven")
    previous_hostd = f"@raven-host-{os.getuid()}" in Path("/proc/net/unix").read_text()
    session = None
    browser = None
    try:
        result = run([raven, "new", "-d", "-s", f"files-{suffix}", "-g", "800x600", "-w", "openbox", "xterm"],
                     environment)
        session = re.search(rb"raven: created session ([0-9]+)", result.stderr)[1].decode()
        container = f"raven-{owner}-{session}"
        in_session(container, 'mkdir "sub dir"')
        provision(environment, origin, password)
        run([raven, "web", "start", "--port", str(port)], environment)
        browser = subprocess.Popen(
            ["docker", "run", "--rm", "-i", "--init", "--network", "host", "--memory", "1g", "--memory-swap", "1g",
             "--cpus", "1", "--pids-limit", "256", "--user", f"{os.getuid()}:{os.getgid()}",
             "--security-opt", "seccomp=unconfined", "--shm-size", "1g", "--env", "HOME=/tmp", "--read-only",
             "--tmpfs", "/tmp:rw,nosuid,nodev,size=512m", "--mount", f"type=bind,src={scratch},dst=/output",
             "--mount", f"type=bind,src={SOURCE / 'tests/web_files_e2e_browser.py'},dst=/files.py,readonly",
             BROWSER_IMAGE, "python3", "/files.py"],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)

        def step(name):
            line = browser.stdout.readline().strip()
            try:
                result = json.loads(line)
            except ValueError:
                result = {}
            if result.get("step") != name:
                browser.kill()
                raise AssertionError(f"Browser stopped before {name}: {line}\n{browser.stderr.read()[-4000:]}")
            return result

        def go():
            browser.stdin.write("go\n")
            browser.stdin.flush()

        browser.stdin.write(json.dumps({"origin": origin, "password": password, "big_name": BIG_NAME}) + "\n")
        browser.stdin.flush()

        uploaded = step("uploaded")
        assert session_hash(container, BIG_NAME) == uploaded["big"]
        assert session_hash(container, "sub dir/작은.txt") == uploaded["small"]
        info = (downloads / BIG_NAME).stat()
        assert stat.S_IMODE(info.st_mode) == 0o660 and info.st_uid == os.getuid(), oct(info.st_mode)
        assert info.st_gid == downloads.stat().st_gid, "The upload did not take the shared folder's group"
        in_session(container, 'test -w "$1" && test -r "sub dir/작은.txt"', None, BIG_NAME)
        data = os.urandom(5 * 1024 * 1024 + 7)
        in_session(container, "cat > from-session.bin", data)
        go()

        downloaded = step("downloaded")
        assert downloaded["session"] == hashlib.sha256(data).hexdigest()
        go()

        step("cancelled")
        staging = downloads / ".raven-uploads"
        assert not list(staging.iterdir()), list(staging.iterdir())
        assert not (downloads / "cancelled.bin").exists()
        go()

        raced = step("raced")
        assert session_hash(container, BIG_NAME) == raced["replacement"]
        in_session(container, "printf theirs > race.txt")
        go()
        step("overwritten")
        assert (downloads / "race.txt").read_bytes() == b"mine"
        assert not list(staging.iterdir()), list(staging.iterdir())
        in_session(container, "ln -s /etc/passwd passwd-link && ln -s / root-link && ln -s .. up-link"
                              " && printf secret > private.bin && chmod 600 private.bin && mkfifo fifo")
        go()

        shown = step("ui")
        assert session_hash(container, "화면에서 올림.bin") == shown["picked"]
        assert shown["clicked"] == hashlib.sha256(data).hexdigest()
        go()

        boundaries = step("boundaries")
        results = {name: tuple(value) for name, value in boundaries["results"].items()}
        assert results == EXPECTED_BOUNDARIES, {name: (value, EXPECTED_BOUNDARIES.get(name))
                                                for name, value in results.items()
                                                if value != EXPECTED_BOUNDARIES.get(name)}
        assert set(boundaries["types"].values()) == {"other"}, boundaries["types"]
        go()
        assert browser.wait(timeout=COMMAND_TIMEOUT) == 0, browser.stderr.read()[-4000:]
        logs = run(["docker", "logs", gateway], environment)
        assert password.encode() not in logs.stdout + logs.stderr, "The password reached the gateway log"
        print(f"Files e2e passed. Scratch: {scratch}")
    finally:
        if browser and browser.poll() is None:
            browser.kill()
        if session is not None:
            subprocess.run([raven, "kill", "-t", session], env=environment, capture_output=True,
                           timeout=COMMAND_TIMEOUT)
        if not previous_hostd:
            subprocess.run([raven, "hostd", "stop"], env=environment, capture_output=True, timeout=COMMAND_TIMEOUT)
        cleanup(environment, gateway, f"raven-web:absent-{suffix}")


if __name__ == "__main__":
    main()
