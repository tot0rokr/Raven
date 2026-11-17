"""Pause and resume a disposable desktop from a real browser through the real gateway and host broker: the viewer's
pause button, the paused card, a refused open, resuming from the card, and a CLI pause seen by a connected viewer."""

import json
import os
from pathlib import Path
import re
import secrets
import socket
import subprocess
import tempfile

from web_config_e2e import cleanup, inspect
from web_e2e import COMMAND_TIMEOUT, SOURCE, WEB_IMAGE, provision, run

BROWSER_IMAGE = os.environ.get("RAVEN_WEB_BROWSER_IMAGE", "headless-browser:1.63.0")
SESSION_IMAGE = os.environ.get("RAVEN_IMAGE", "raven:latest")


def main():
    scratch = Path(tempfile.mkdtemp(prefix="raven-web-pause-e2e-"))
    suffix = secrets.token_hex(4)
    instance = "pause-" + suffix
    environment = dict(os.environ)
    environment.pop("DISPLAY", None)
    for variable, directory in (("HOME", "home"), ("XDG_CONFIG_HOME", "config"), ("XDG_STATE_HOME", "state")):
        (scratch / directory).mkdir(mode=0o700)
        environment[variable] = str(scratch / directory)
    # The broker socket path must fit AF_UNIX's limit; the scratch path may be long.
    environment["XDG_RUNTIME_DIR"] = tempfile.mkdtemp(prefix="rt-")
    environment.update(RAVEN_WEB_IMAGE=WEB_IMAGE, RAVEN_WEB_INSTANCE=instance, RAVEN_IMAGE=SESSION_IMAGE)
    with socket.socket() as reservation:
        reservation.bind(("127.0.0.1", 0))
        port = reservation.getsockname()[1]
    origin = f"https://localhost:{port}"
    password = secrets.token_urlsafe(24)
    owner = re.sub(r"[^A-Za-z0-9_.-]", "_", os.environ["USER"])
    gateway = f"raven-{owner}-{instance}"
    raven = str(SOURCE / "raven")
    previous_hostd = f"@raven-host-{os.getuid()}" in Path("/proc/net/unix").read_text()
    name = f"pause-{suffix}"
    session = None
    browser = None

    def paused():
        return inspect(environment, f"raven-{owner}-{session}")["State"]["Paused"]

    try:
        result = run([raven, "new", "-d", "-s", name, "-g", "1280x720", "-w", "xmonad", "xterm"], environment)
        session = re.search(rb"raven: created session ([0-9]+)", result.stderr)[1].decode()
        started = inspect(environment, f"raven-{owner}-{session}")["State"]["StartedAt"]
        provision(environment, origin, password)
        run([raven, "web", "start", "--port", str(port)], environment)
        browser = subprocess.Popen(
            ["docker", "run", "--rm", "-i", "--init", "--network", "host", "--memory", "1g", "--memory-swap", "1g",
             "--cpus", "1", "--pids-limit", "256", "--user", f"{os.getuid()}:{os.getgid()}",
             "--security-opt", "seccomp=unconfined", "--shm-size", "1g", "--env", "HOME=/tmp", "--read-only",
             "--tmpfs", "/tmp:rw,nosuid,nodev,size=512m", "--mount", f"type=bind,src={scratch},dst=/output",
             "--mount", f"type=bind,src={SOURCE / 'tests/web_pause_e2e_browser.py'},dst=/pause.py,readonly",
             BROWSER_IMAGE, "python3", "/pause.py"],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)

        def step(expected):
            line = browser.stdout.readline().strip()
            try:
                result = json.loads(line)
            except ValueError:
                result = {}
            if result.get("step") != expected:
                browser.kill()
                raise AssertionError(f"Browser stopped before {expected}: {line}\n{browser.stderr.read()[-4000:]}")
            return result

        def go():
            browser.stdin.write("go\n")
            browser.stdin.flush()

        browser.stdin.write(json.dumps({"origin": origin, "password": password, "session": name,
                                        "id": session}) + "\n")
        browser.stdin.flush()
        shown = step("paused")
        assert paused(), "The viewer's pause button did not pause the container"
        assert "유지 중" in shown["stats"], shown
        go()
        resumed = step("resumed")
        assert resumed["refused"] == [409, "session_paused"], resumed
        assert not paused(), "Resume from the card did not unpause the container"
        go()
        step("connected")
        run([raven, "pause", "-t", session], environment)
        go()
        offered = step("offered")
        assert paused(), offered
        go()
        step("reconnected")
        assert not paused()
        assert inspect(environment, f"raven-{owner}-{session}")["State"]["StartedAt"] == started, \
            "The desktop was restarted instead of resumed"
        go()
        assert browser.wait(timeout=COMMAND_TIMEOUT) == 0, browser.stderr.read()[-4000:]
        print(f"Pause e2e passed. Screenshots: {scratch}")
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
