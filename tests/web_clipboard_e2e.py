"""Exchange clipboard text between browser viewers and the X clipboards of two disposable desktops through the real
gateway and Xvnc. The desktops use a test image that adds xclip on top of the session image."""

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
TEST_IMAGE = "raven:clipboard-test"
FIRST = "한글 클립보드 시험\n둘째 줄 abc 123\t탭"
SECOND = "세션에서 복사한 줄 — ünïcode ✓"


def x_command(container, *arguments, text=None):
    display = inspect(os.environ, container)["Config"]["Labels"]["raven.display"]
    return subprocess.run(["docker", "exec", "-i", "-u", "raven", "-e", f"DISPLAY=:{display}",
                           "-e", "XAUTHORITY=/home/raven/.Xauthority", container, *arguments],
                          input=text, capture_output=True, timeout=COMMAND_TIMEOUT)


def clipboard(container, selection="clipboard"):
    result = x_command(container, "xclip", "-o", "-selection", selection)
    return result.stdout.decode() if result.returncode == 0 else None


def main():
    scratch = Path(tempfile.mkdtemp(prefix="raven-web-clipboard-e2e-"))
    suffix = secrets.token_hex(4)
    instance = "clipboard-" + suffix
    environment = dict(os.environ)
    environment.pop("DISPLAY", None)
    for variable, directory in (("HOME", "home"), ("XDG_CONFIG_HOME", "config"),
                                ("XDG_STATE_HOME", "state"), ("XDG_RUNTIME_DIR", "runtime")):
        (scratch / directory).mkdir(mode=0o700)
        environment[variable] = str(scratch / directory)
    environment.update(RAVEN_WEB_IMAGE=WEB_IMAGE, RAVEN_WEB_INSTANCE=instance, RAVEN_IMAGE=TEST_IMAGE)
    with socket.socket() as reservation:
        reservation.bind(("127.0.0.1", 0))
        port = reservation.getsockname()[1]
    origin = f"https://localhost:{port}"
    password = secrets.token_urlsafe(24)
    owner = re.sub(r"[^A-Za-z0-9_.-]", "_", os.environ["USER"])
    gateway = f"raven-{owner}-{instance}"
    raven = str(SOURCE / "raven")
    previous_hostd = f"@raven-host-{os.getuid()}" in Path("/proc/net/unix").read_text()
    names = [f"clip1-{suffix}", f"clip2-{suffix}"]
    sessions = []
    browser = None
    try:
        subprocess.run(["docker", "build", "-q", "-t", TEST_IMAGE, "-"], check=True, capture_output=True,
                       timeout=COMMAND_TIMEOUT * 3, input=(
                           f"FROM {SESSION_IMAGE}\nUSER root\nRUN apt-get update && apt-get install -y "
                           "--no-install-recommends xclip && rm -rf /var/lib/apt/lists/*\nUSER raven\n").encode())
        for name in names:
            result = run([raven, "new", "-d", "-s", name, "-g", "800x600", "-w", "openbox", "xterm"], environment)
            sessions.append(re.search(rb"raven: created session ([0-9]+)", result.stderr)[1].decode())
        containers = [f"raven-{owner}-{session}" for session in sessions]
        provision(environment, origin, password)
        run([raven, "web", "start", "--port", str(port)], environment)
        browser = subprocess.Popen(
            ["docker", "run", "--rm", "-i", "--init", "--network", "host", "--memory", "1g", "--memory-swap", "1g",
             "--cpus", "1", "--pids-limit", "256", "--user", f"{os.getuid()}:{os.getgid()}",
             "--security-opt", "seccomp=unconfined", "--shm-size", "1g", "--env", "HOME=/tmp", "--read-only",
             "--tmpfs", "/tmp:rw,nosuid,nodev,size=512m", "--mount", f"type=bind,src={scratch},dst=/output",
             "--mount", f"type=bind,src={SOURCE / 'tests/web_clipboard_ui.py'},dst=/clipboard_ui.py,readonly",
             BROWSER_IMAGE, "python3", "/clipboard_ui.py"],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)

        def expect_step(name):
            line = browser.stdout.readline().strip()
            if line != name:
                browser.kill()
                raise AssertionError(f"Browser stopped before {name}: {line}\n{browser.stderr.read()[-3000:]}")

        def go():
            browser.stdin.write("go\n")
            browser.stdin.flush()

        browser.stdin.write(json.dumps({"origin": origin, "password": password, "sessions": names,
                                        "first": FIRST, "second": SECOND}) + "\n")
        browser.stdin.flush()
        expect_step("sent")
        assert clipboard(containers[0]) == FIRST, clipboard(containers[0])
        assert clipboard(containers[0], "primary") == FIRST
        assert clipboard(containers[1]) != FIRST, "Viewer A's text reached desktop 2"
        go()
        expect_step("copied")
        result = x_command(containers[1], "sh", "-c", "xclip -i -selection clipboard >/dev/null 2>&1",
                           text=SECOND.encode())
        assert result.returncode == 0, result.stderr
        go()
        expect_step("oversized")
        assert clipboard(containers[0]) == FIRST, "Oversized text replaced desktop 1's clipboard"
        go()
        summary = browser.stdout.readline().strip()
        assert browser.wait(timeout=COMMAND_TIMEOUT) == 0, browser.stderr.read()[-3000:]
        print(summary)
        logs = run(["docker", "logs", gateway], environment)
        for text in ("한글 클립보드", "세션에서 복사한", password):
            assert text.encode() not in logs.stdout + logs.stderr, "Clipboard text or the password reached the log"
        print(f"Screenshots: {scratch}")
    finally:
        if browser and browser.poll() is None:
            browser.kill()
        for session in sessions:
            subprocess.run([raven, "kill", "-t", session], env=environment, capture_output=True,
                           timeout=COMMAND_TIMEOUT)
        if not previous_hostd:
            subprocess.run([raven, "hostd", "stop"], env=environment, capture_output=True, timeout=COMMAND_TIMEOUT)
        cleanup(environment, gateway, f"raven-web:absent-{suffix}")


if __name__ == "__main__":
    main()
