"""Browser logins through the real gateway: two browsers view one disposable desktop, one ends the other's login,
the CLI lists and revokes logins, revoking all and a password change refuse every earlier cookie and WebSocket, and
no session key or cookie value reaches the gateway log, the CLI or an API response. Runs on its own web instance,
account and desktop under a temporary HOME."""

import json
import os
from pathlib import Path
import pty
import re
import secrets
import select
import socket
import subprocess
import tempfile
import time

from web_config_e2e import cleanup
from web_e2e import COMMAND_TIMEOUT, SOURCE, WEB_IMAGE, provision, run

BROWSER_IMAGE = os.environ.get("RAVEN_WEB_BROWSER_IMAGE", "headless-browser:1.63.0")
SESSION_IMAGE = os.environ.get("RAVEN_IMAGE", "raven:latest")
FIELDS = {"id", "browser", "os", "device", "created", "seen", "expires", "current", "connections"}
SESSION_COOKIE = "__Host-raven_session"


def change_password(environment, password):
    """raven web password at a terminal, answering both prompts."""
    master, slave = pty.openpty()
    process = subprocess.Popen([str(SOURCE / "raven"), "web", "password", "--username", "owner"], env=environment,
                               stdin=slave, stdout=slave, stderr=slave, start_new_session=True)
    os.close(slave)
    prompts = [b"Password: ", b"Confirm password: "]
    output = b""
    deadline = time.monotonic() + COMMAND_TIMEOUT
    try:
        while time.monotonic() < deadline and process.poll() is None:
            ready, _, _ = select.select([master], [], [], 0.2)
            if not ready:
                continue
            try:
                data = os.read(master, 65536)
            except OSError:
                break
            output += data
            if prompts and output.endswith(prompts[0]):
                os.write(master, password.encode() + b"\n")
                prompts.pop(0)
        assert process.wait(timeout=10) == 0 and not prompts, "raven web password failed"
    finally:
        os.close(master)
        if process.poll() is None:
            process.kill()
    return output.decode(errors="replace")


def table(output):
    """The rows of raven web logins: ID, browser, device and three local times, split on two or more spaces."""
    lines = output.splitlines()
    assert re.split(r"\s{2,}", lines[0]) == ["ID", "BROWSER", "DEVICE", "LOGGED IN", "LAST SEEN", "EXPIRES"], output
    return [re.split(r"\s{2,}", line) for line in lines[1:]]


def main():
    scratch = Path(tempfile.mkdtemp(prefix="raven-web-logins-e2e-"))
    suffix = secrets.token_hex(4)
    instance = "logins-" + suffix
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
    new_password = secrets.token_urlsafe(24)
    owner = re.sub(r"[^A-Za-z0-9_.-]", "_", os.environ["USER"])
    gateway = f"raven-{owner}-{instance}"
    raven = str(SOURCE / "raven")
    previous_hostd = f"@raven-host-{os.getuid()}" in Path("/proc/net/unix").read_text()
    name = f"logins-{suffix}"
    session = None
    browser = None
    values = set()
    outputs = []

    def cli(*arguments):
        output = run([raven, "web", *arguments], environment).stdout.decode()
        outputs.append(output)
        return output

    try:
        result = run([raven, "new", "-d", "-s", name, "-g", "1280x720", "-w", "openbox", "xterm"], environment)
        session = re.search(rb"raven: created session ([0-9]+)", result.stderr)[1].decode()
        provision(environment, origin, password)
        run([raven, "web", "start", "--port", str(port)], environment)
        browser = subprocess.Popen(
            ["docker", "run", "--rm", "-i", "--init", "--network", "host", "--memory", "1536m",
             "--memory-swap", "1536m", "--cpus", "2", "--pids-limit", "512", "--user", f"{os.getuid()}:{os.getgid()}",
             "--security-opt", "seccomp=unconfined", "--shm-size", "1g", "--env", "HOME=/tmp", "--read-only",
             "--tmpfs", "/tmp:rw,nosuid,nodev,size=512m", "--mount", f"type=bind,src={scratch},dst=/output",
             "--mount", f"type=bind,src={SOURCE / 'tests/web_logins_e2e_browser.py'},dst=/logins.py,readonly",
             BROWSER_IMAGE, "python3", "/logins.py"],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)

        def step(expected):
            line = browser.stdout.readline().strip()
            try:
                result = json.loads(line)
            except ValueError:
                result = {}
            if result.get("step") != expected:
                browser.kill()
                raise AssertionError(f"Browser stopped before {expected}: {line[:2000]}\n"
                                     f"{browser.stderr.read()[-4000:]}")
            for jar in result.get("cookies", []):
                values.update(value for value in jar.values() if value)
            return result

        def go():
            browser.stdin.write("go\n")
            browser.stdin.flush()

        browser.stdin.write(json.dumps({"origin": origin, "password": password, "session": name,
                                        "id": session}) + "\n")
        browser.stdin.flush()

        listed = step("listed")
        a_list, b_list = listed["a"][1]["logins"], listed["b"][1]["logins"]
        assert listed["a"][0] == listed["b"][0] == 200, listed
        assert len(a_list) == 2 and all(set(login) == FIELDS for login in a_list), a_list
        a_id = next(login["id"] for login in a_list if login["current"])
        b_id = next(login["id"] for login in b_list if login["current"])
        assert a_id != b_id and {login["id"] for login in a_list} == {a_id, b_id}, (a_list, b_list)
        assert [login["connections"] for login in a_list] == [1, 1], a_list
        assert all((login["browser"], login["os"], login["device"]) == ("Chrome", "Linux", "desktop")
                   for login in a_list), a_list
        print("GET /api/logins/ as A:", listed["raw"], flush=True)
        go()

        revoked = step("revoked")
        assert revoked["b_id"] == b_id
        assert revoked["response"] == [200, {"revoked": 1, "current": False}], revoked
        assert 4401 in revoked["b"]["closes"], revoked
        assert revoked["b"]["api"] == [401, {"error": "Authentication required", "code": "auth_required"}], revoked
        assert revoked["a"]["closes"] == [] and revoked["a"]["status"] == "연결됨", revoked
        a_now = revoked["a"]["list"][1]["logins"]
        assert [(login["id"], login["current"], login["connections"]) for login in a_now] == [(a_id, True, 1)], a_now
        go()

        relogged = step("relogged")
        assert relogged["a_id"] == a_id and relogged["b_id"] not in (a_id, b_id), relogged
        b_id = relogged["b_id"]
        listing = cli("logins")
        print("raven web logins:\n" + listing, flush=True)
        rows = table(listing)
        assert sorted(row[0] for row in rows) == sorted([a_id, b_id]), rows
        assert all(row[1:3] == ["Chrome on Linux", "desktop"] and len(row) == 6 for row in rows), rows
        assert "Revoked 1 login." in cli("logins", "revoke", b_id)
        go()

        cli_revoked = step("cli_revoked")
        assert 4401 in cli_revoked["b"]["closes"], cli_revoked
        assert cli_revoked["b"]["api"][0] == 401, cli_revoked
        assert cli_revoked["a"] == {"closes": [], "status": "연결됨"}, cli_revoked
        assert [row[0] for row in table(cli("logins"))] == [a_id]
        go()

        revoked_all = step("revoked_all")
        assert revoked_all["control"]["api"] == [200, None], revoked_all
        assert revoked_all["control"]["websocket"]["opened"], revoked_all
        assert revoked_all["response"] == [200, {"revoked": 2, "current": True}], revoked_all
        assert not revoked_all["a_cookie_left"], "Revoking every login did not clear this browser's cookie"
        for who in ("a", "b"):
            assert 4401 in revoked_all["after"][who]["closes"], revoked_all
            assert revoked_all["after"][who]["api"][0] == 401, revoked_all
            assert revoked_all["replays"][who]["api"] == [401, "auth_required"], revoked_all
            assert not revoked_all["replays"][who]["websocket"]["opened"], revoked_all
        assert cli("logins") == "No browser logins.\n"
        go()

        before_password = step("before_password")
        change_password(environment, new_password)
        go()
        changed = step("password_changed")
        assert 4401 in changed["a"]["closes"] and changed["a"]["api"][0] == 401, changed
        assert changed["replay"]["api"] == [401, "auth_required"], changed
        assert not changed["replay"]["websocket"]["opened"], changed
        go()
        assert browser.wait(timeout=COMMAND_TIMEOUT) == 0, browser.stderr.read()[-4000:]

        # A's two logins and B's three: each session key travelled as a cookie value.
        sessions = {jar[SESSION_COOKIE] for result in (listed, relogged, revoked_all, before_password)
                    for jar in result["cookies"] if SESSION_COOKIE in jar}
        assert len(sessions) == 5, "The browsers did not report each login cookie"
        logs = run(["docker", "logs", gateway], environment)
        exposed = {"gateway log": (logs.stdout + logs.stderr).decode(errors="replace"),
                   "API responses": "\n".join(changed["responses"]), "CLI output": "\n".join(outputs)}
        for where, text in exposed.items():
            for value in values | {password, new_password}:
                assert value not in text, f"A session key, cookie or password reached the {where}"
        print(f"Logins e2e passed. Screenshots: {scratch}")
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
