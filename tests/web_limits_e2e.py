"""Memory and CPU limits through the real gateway and host broker of an isolated web instance (roadmap M13).

Logs in through the form and drives the JSON API with that login's cookies (no page selectors): creates a desktop
with limits, reads them in the session list with the server's totals, changes them while the desktop runs (same
container, same start time, new cgroup limits), refuses a memory limit below the desktop's use with 409 and the
minimum, takes a share of the server and max, refuses lowering a paused desktop, and maps out-of-range values to 400.
Runs on its own instance, account, runtime directory and HOME/XDG directories; the desktop it made and the instance
are removed at the end. The user's desktops are not opened or changed.
"""

import http.client
import json
import os
from pathlib import Path
import re
import secrets
import shutil
import socket
import ssl
import subprocess
import tempfile
import time
from urllib.parse import urlencode, urlsplit

from web_config_e2e import cleanup, inspect
from web_e2e import COMMAND_TIMEOUT, SOURCE, WEB_IMAGE, provision, run

SESSION_IMAGE = os.environ.get("RAVEN_IMAGE", "raven:latest")
SESSION_COOKIE = "__Host-raven_session"
CSRF_COOKIE = "__Host-raven_csrf"
MiB, GiB = 1024 ** 2, 1024 ** 3
HOG_SIZE = 700 * MiB
HOG = r'''
import os, sys, time
buffer = bytearray(b"\xa5") * int(sys.argv[1])
with open("/home/raven/Downloads/m13-web-hog", "w") as f:
    f.write(f"{os.getpid()}\n")
while True:
    time.sleep(1)
'''


class Api:
    """HTTPS to the test gateway with one login's cookies; the gateway's certificate is its own self-signed one."""

    def __init__(self, origin):
        self.origin = origin
        self.parts = urlsplit(origin)
        self.context = ssl.create_default_context()
        self.context.check_hostname = False
        self.context.verify_mode = ssl.CERT_NONE
        self.cookies = {}

    def call(self, method, path, body=None, headers=None, form=False):
        connection = http.client.HTTPSConnection(self.parts.hostname, self.parts.port, context=self.context,
                                                 timeout=COMMAND_TIMEOUT)
        headers = dict(headers or {})
        if self.cookies:
            headers["Cookie"] = "; ".join(f"{name}={value}" for name, value in self.cookies.items())
        if body is not None and not form:
            body = json.dumps(body)
            headers["Content-Type"] = "application/json"
        if method != "GET":
            headers.update({"Origin": self.origin, "X-CSRFToken": self.cookies.get(CSRF_COOKIE, "")})
        try:
            connection.request(method, path, body=body, headers=headers)
            response = connection.getresponse()
            for header in response.msg.get_all("Set-Cookie") or []:
                name, _, rest = header.partition("=")
                self.cookies[name.strip()] = rest.split(";", 1)[0]
            data = response.read().decode("utf-8", "replace")
        finally:
            connection.close()
        try:
            return response.status, json.loads(data)
        except ValueError:
            return response.status, data

    def login(self, password):
        status, page = self.call("GET", "/login/")
        assert status == 200, status
        token = re.search(r'name="csrfmiddlewaretoken" value="([^"]+)"', page)[1]
        status, _ = self.call("POST", "/login/", urlencode({"username": "owner", "password": password,
                                                            "csrfmiddlewaretoken": token}),
                              {"Content-Type": "application/x-www-form-urlencoded"}, form=True)
        assert status == 302 and SESSION_COOKIE in self.cookies, status
        # The page renews the CSRF cookie that API requests echo in X-CSRFToken
        assert self.call("GET", "/")[0] == 200

    def session(self, sid):
        status, data = self.call("GET", "/api/sessions/")
        assert status == 200, (status, data)
        return next(session for session in data["sessions"] if session["id"] == sid)

    def limits(self, sid, generation, **values):
        return self.call("POST", f"/api/sessions/{sid}/limits/", {"generation": generation, **values})


def host_limits(environment, container):
    item = inspect(environment, container)
    return {"id": item["Id"], "started": item["State"]["StartedAt"], "status": item["State"]["Status"],
            "memory": item["HostConfig"]["Memory"], "swap": item["HostConfig"]["MemorySwap"],
            "nano": item["HostConfig"]["NanoCpus"]}


def cgroup_file(environment, container, name):
    pid = inspect(environment, container)["State"]["Pid"]
    path = next(line[3:] for line in Path(f"/proc/{pid}/cgroup").read_text().splitlines() if line.startswith("0::"))
    return (Path("/sys/fs/cgroup" + path) / name).read_text().strip()


def wait(condition, what, seconds=60):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        value = condition()
        if value:
            return value
        time.sleep(0.2)
    raise AssertionError(f"timed out waiting for {what}")


def main():
    scratch = Path(tempfile.mkdtemp(prefix="raven-web-limits-e2e-"))
    suffix = secrets.token_hex(4)
    instance = "limits-" + suffix
    environment = dict(os.environ)
    environment.pop("DISPLAY", None)
    for variable in ("RAVEN_MEMORY", "RAVEN_CPUS"):
        environment.pop(variable, None)
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
    total = int(re.search(r"^MemTotal:\s+([0-9]+) kB", Path("/proc/meminfo").read_text(), re.M)[1]) * 1024
    cores = len(os.sched_getaffinity(0))
    name = f"limits-{suffix}"
    sid = None
    report = {}
    try:
        provision(environment, origin, password)
        run([raven, "web", "start", "--port", str(port)], environment)
        api = Api(origin)
        wait(lambda: _ready(api), "the gateway", 60)
        api.login(password)

        # Created with limits from the API
        status, data = api.call("POST", "/api/sessions/", {"name": name, "geometry": "1024x768", "command": "xterm",
                                                           "memory": GiB, "cpus": 1})
        assert status == 201, (status, data)
        sid = data["session"]["id"]
        container = f"raven-{owner}-{sid}"
        assert data["session"]["limits"] == {"memory": GiB, "cpus": 1.0, "server_memory": total,
                                             "server_cpus": cores}, data["session"]
        first = host_limits(environment, container)
        assert (first["memory"], first["swap"], first["nano"]) == (GiB, GiB, 10 ** 9), first
        listed = api.session(sid)
        report["listed_limits"] = listed["limits"]
        assert listed["limits"] == data["session"]["limits"], listed
        generation = listed["generation"]

        # Changed while the desktop runs
        status, data = api.limits(sid, generation, memory=3 * GiB, cpus=2.5)
        assert status == 200, (status, data)
        assert data["session"]["limits"]["memory"] == 3 * GiB and data["session"]["limits"]["cpus"] == 2.5, data
        now = host_limits(environment, container)
        assert (now["id"], now["started"], now["status"]) == (first["id"], first["started"], "running"), now
        assert (now["memory"], now["swap"], now["nano"]) == (3 * GiB, 3 * GiB, 2_500_000_000), now
        assert (cgroup_file(environment, container, "memory.max"), cgroup_file(environment, container, "cpu.max")) \
            == (str(3 * GiB), "250000 100000")
        report["changed"] = data["session"]["limits"]

        # Below the desktop's use: 409 with the minimum, nothing changed
        downloads = scratch / "home" / "Downloads"
        (downloads / "m13-web-hog.py").write_text(HOG)
        run([raven, "run", "-t", sid, f"python3 /home/raven/Downloads/m13-web-hog.py {HOG_SIZE}"], environment)
        wait(lambda: (downloads / "m13-web-hog").exists(), "the memory hog")
        wait(lambda: int(cgroup_file(environment, container, "memory.current")) >= HOG_SIZE, "the hog's memory")
        status, data = api.limits(sid, generation, memory=512 * MiB)
        report["below_use"] = [status, data]
        assert status == 409 and data["code"] == "limit_below_usage", (status, data)
        assert re.fullmatch(r"The memory limit must be at least [0-9]+MiB; the session uses [0-9.]+(MiB|GiB) now",
                            data["error"]), data
        assert host_limits(environment, container)["memory"] == 3 * GiB

        # Shares of the server and max; out of range values
        status, data = api.limits(sid, generation, memory="25%", cpus="50%")
        assert status == 200, (status, data)
        now = host_limits(environment, container)
        assert (now["memory"], now["nano"]) == (total // 4 // MiB * MiB, round(cores / 2, 2) * 10 ** 9), now
        report["share"] = data["session"]["limits"]
        status, data = api.limits(sid, generation, memory="max", cpus="max")
        assert status == 200, (status, data)
        now = host_limits(environment, container)
        assert (now["memory"], now["nano"], now["id"]) == (total // MiB * MiB, cores * 10 ** 9, first["id"]), now
        status, data = api.limits(sid, generation, cpus=0.25)
        assert (status, data["code"]) == (400, "limit_invalid"), (status, data)
        status, data = api.limits(sid, generation, cpus=cores + 1)
        report["out_of_range"] = [status, data]
        assert (status, data) == (400, {"code": "limit_invalid",
                                        "error": f"CPUs can be at most the server's {cores} cores"}), (status, data)
        status, data = api.limits(sid, "0" * 64, cpus=2)
        assert (status, data["code"]) == (502, "session_failed"), (status, data)

        # A paused desktop only grows
        status, data = api.limits(sid, generation, memory=2 * GiB, cpus=1)
        assert status == 200, (status, data)
        run([raven, "pause", "-t", sid], environment)
        status, data = api.limits(sid, generation, memory=1536 * MiB)
        report["paused_lower"] = [status, data]
        assert (status, data["code"]) == (409, "session_paused"), (status, data)
        status, data = api.limits(sid, generation, memory=3 * GiB, cpus=2)
        assert status == 200 and data["session"]["state"] == "paused", (status, data)
        now = host_limits(environment, container)
        assert (now["memory"], now["nano"], now["status"], now["started"]) == \
            (3 * GiB, 2 * 10 ** 9, "paused", first["started"]), now
        run([raven, "resume", "-t", sid], environment)
        listed = api.session(sid)
        assert listed["limits"]["memory"] == 3 * GiB and listed["limits"]["cpus"] == 2.0, listed

        run([raven, "kill", "-t", sid], environment)
        sid = None
        report["web_limits_e2e"] = "passed"
        print(json.dumps(report, indent=2, ensure_ascii=False))
    finally:
        if sid is not None:
            subprocess.run([raven, "kill", "-t", sid], env=environment, capture_output=True, timeout=COMMAND_TIMEOUT)
        if not previous_hostd:
            subprocess.run([raven, "hostd", "stop"], env=environment, capture_output=True, timeout=COMMAND_TIMEOUT)
        cleanup(environment, gateway, f"raven-web:absent-{suffix}")
        shutil.rmtree(scratch, ignore_errors=True)
        shutil.rmtree(environment["XDG_RUNTIME_DIR"], ignore_errors=True)


def _ready(api):
    try:
        return api.call("GET", "/login/")[0] == 200
    except OSError:
        return False


if __name__ == "__main__":
    main()
