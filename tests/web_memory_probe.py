"""Measure a bounded, disposable Raven gateway and desktop under viewer churn."""

import argparse
import json
import os
from pathlib import Path
import re
import secrets
import select
import socket
import subprocess
import tempfile
import time

from web_e2e import SOURCE, cleanup, provision, run
from raven_web_host import Raven

WEB_IMAGE = "sha256:446557f5d5d366d95c9a61a8b9c8fc89878d4dd917a5861854f25e0cb732022e"
PROBE_TIMEOUT = 660
WEB_ABORT_BYTES = 900 * 1024 ** 2
DESKTOP_ABORT_BYTES = 1536 * 1024 ** 2
BROKER_ABORT_KIB = 256 * 1024


def process_metrics(pid):
    path = Path("/proc") / str(pid)
    fields = {}
    for line in (path / "status").read_text().splitlines():
        key, _, value = line.partition(":")
        if key in ("VmRSS", "Threads"):
            fields[key] = int(value.split()[0])
    try:
        descriptors = len(list((path / "fd").iterdir()))
    except PermissionError:
        descriptors = None
    return {"pid": pid, "rss_kib": fields.get("VmRSS", 0), "threads": fields["Threads"], "fds": descriptors}


def container_metrics(environment, name):
    item = json.loads(run(["docker", "inspect", name], environment).stdout)[0]
    pids = run(["docker", "top", name, "-eo", "pid"], environment).stdout.decode().splitlines()[1:]
    processes = [process_metrics(int(pid.strip())) for pid in pids]
    group = Path("/proc") / str(item["State"]["Pid"]) / "cgroup"
    relative = next(line.split(":", 2)[2] for line in group.read_text().splitlines() if line.startswith("0::"))
    cgroup = Path("/sys/fs/cgroup") / relative.lstrip("/")
    events = dict(line.split() for line in (cgroup / "memory.events").read_text().splitlines())
    return {"rss_kib": sum(process["rss_kib"] for process in processes),
            "threads": sum(process["threads"] for process in processes),
            "fds": sum(process["fds"] or 0 for process in processes), "processes": processes,
            "memory_bytes": int((cgroup / "memory.current").read_text()),
            "oom_events": {key: int(value) for key, value in events.items()},
            "oom_killed": item["State"]["OOMKilled"],
            "memory_limit": item["HostConfig"]["Memory"], "cpu_limit": item["HostConfig"]["NanoCpus"]}


def broker_pid(runtime):
    with socket.socket(socket.AF_UNIX) as connection:
        connection.settimeout(5)
        connection.connect(str(runtime / "broker.sock"))
        connection.sendall(b'{"action":"ping"}\n')
        with connection.makefile("rb") as stream:
            reply = json.loads(stream.readline())
    assert reply["ok"]
    return reply["data"]["pid"]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--loops", type=int, choices=(0, 50, 100), default=100)
    args = parser.parse_args()
    scratch = Path(tempfile.mkdtemp(prefix="raven-memory-probe-"))
    suffix = secrets.token_hex(4)
    instance = "memory-" + suffix
    environment = dict(os.environ)
    environment.pop("DISPLAY", None)
    for variable, directory in (("HOME", "home"), ("XDG_CONFIG_HOME", "config"),
                                ("XDG_STATE_HOME", "state"), ("XDG_RUNTIME_DIR", "runtime")):
        path = scratch / directory
        path.mkdir(mode=0o700)
        environment[variable] = str(path)
    environment.update(RAVEN_WEB_IMAGE=WEB_IMAGE, RAVEN_WEB_INSTANCE=instance)
    owner = Raven(SOURCE / "raven").user
    gateway = f"raven-{owner}-{instance}"
    client_name = gateway + "-client"
    runtime = Path(environment["XDG_RUNTIME_DIR"]) / f"raven-web-{os.getuid()}-{instance}"
    with socket.socket() as reservation:
        reservation.bind(("127.0.0.1", 0))
        port = reservation.getsockname()[1]
    origin = f"https://localhost:{port}"
    session_id = None
    desktop_generation = None
    client = None
    samples = []
    artifact = scratch / "metrics.json"
    try:
        print(f"Memory probe artifacts: {scratch}", flush=True)
        result = run([str(SOURCE / "raven"), "new", "-d", "-s", "memory-test-" + suffix,
                      "-g", "800x600", "-w", "openbox", "xterm"], environment)
        match = re.search(rb"raven: created session ([0-9]+)", result.stderr)
        assert match, "The test desktop did not report an ID"
        session_id = match[1].decode()
        desktop = f"raven-{owner}-{session_id}"
        desktop_generation = json.loads(run(["docker", "inspect", desktop], environment).stdout)[0]["Id"]
        password = secrets.token_urlsafe(32)
        provision(environment, origin, password)
        certificate = run([str(SOURCE / "raven"), "web", "certificate"], environment).stdout.decode()
        run([str(SOURCE / "raven"), "web", "start", "--port", str(port)], environment)
        pid = broker_pid(runtime)

        def sample(event):
            record = {**event, "time": time.time(), "web": container_metrics(environment, gateway),
                      "desktop": container_metrics(environment, desktop), "broker": process_metrics(pid)}
            samples.append(record)
            artifact.write_text(json.dumps({"image": WEB_IMAGE, "samples": samples}, indent=2) + "\n")
            print(json.dumps({"event": event["event"], "connections": event.get("connections"),
                              "web_rss_kib": record["web"]["rss_kib"], "web_fds": record["web"]["fds"],
                              "broker_rss_kib": record["broker"]["rss_kib"], "broker_threads": record["broker"]["threads"],
                              "desktop_rss_kib": record["desktop"]["rss_kib"]}), flush=True)
            if event.get("errors"):
                print(json.dumps({"event": event["event"], "errors": event["errors"]}), flush=True)
            assert record["web"]["memory_limit"] == 1024 ** 3
            assert record["web"]["cpu_limit"] == 1_000_000_000
            assert record["desktop"]["memory_limit"] == 2 * 1024 ** 3
            assert record["web"]["memory_bytes"] < WEB_ABORT_BYTES, "Gateway reached the probe safety limit"
            assert record["desktop"]["memory_bytes"] < DESKTOP_ABORT_BYTES, "Desktop reached the probe safety limit"
            assert record["broker"]["rss_kib"] < BROKER_ABORT_KIB, "Broker reached the probe safety limit"
            assert not record["web"]["oom_killed"] and not record["desktop"]["oom_killed"]
            assert record["web"]["oom_events"]["oom_kill"] == record["desktop"]["oom_events"]["oom_kill"] == 0

        sample({"event": "baseline", "connections": 0})
        arguments = ["docker", "run", "--rm", "-i", "--name", client_name, "--network", "host",
                     "--memory", "512m", "--memory-swap", "512m", "--cpus", "1", "--pids-limit", "64",
                     "--user", f"{os.getuid()}:{os.getgid()}", "--read-only", "--cap-drop", "ALL",
                     "--security-opt", "no-new-privileges", "--mount", f"type=bind,src={SOURCE},dst=/source,readonly",
                     WEB_IMAGE, "python", "-u", "/source/tests/web_memory_client.py"]
        client = subprocess.Popen(arguments, env=environment, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                  stderr=subprocess.PIPE, text=True, bufsize=1)
        payload = {"origin": origin, "username": "owner", "password": password,
                   "certificate": certificate, "session_id": session_id, "loops": args.loops}
        client.stdin.write(json.dumps(payload) + "\n")
        client.stdin.flush()
        deadline = time.monotonic() + PROBE_TIMEOUT
        complete = False
        while time.monotonic() < deadline:
            ready, _, _ = select.select([client.stdout], [], [], min(30, max(0, deadline - time.monotonic())))
            if not ready:
                if client.poll() is not None:
                    break
                continue
            line = client.stdout.readline()
            if not line:
                break
            event = json.loads(line)
            if event["event"] == "error":
                sample(event)
                raise RuntimeError(f"Memory client failed: {event['type']}")
            if event["event"] == "complete":
                print(json.dumps(event), flush=True)
                complete = True
                break
            sample(event)
            client.stdin.write("continue\n")
            client.stdin.flush()
        assert complete, "The memory probe exceeded its deadline or stopped early"
        assert client.wait(timeout=10) == 0, "The memory client failed"
        print(f"Memory probe completed: {artifact}", flush=True)
    finally:
        logs = subprocess.run(["docker", "logs", "--tail", "200", gateway], env=environment,
                              capture_output=True, timeout=30)
        (scratch / "gateway.log").write_bytes(logs.stdout + logs.stderr)
        if client and client.poll() is None:
            subprocess.run(["docker", "container", "rm", "-f", client_name], env=environment,
                           capture_output=True, timeout=30)
            client.wait(timeout=10)
        safe_id = None
        if session_id is not None and desktop_generation is not None:
            current = subprocess.run(["docker", "inspect", f"raven-{owner}-{session_id}"],
                                     env=environment, capture_output=True, timeout=30)
            name = Path(environment["XDG_STATE_HOME"]) / "raven" / socket.gethostname() / f"{session_id}.name"
            if (current.returncode == 0 and json.loads(current.stdout)[0]["Id"] == desktop_generation
                    and name.exists() and name.read_text().strip() == "memory-test-" + suffix):
                safe_id = session_id
        cleanup(environment, gateway, safe_id, True, "memory-unused-" + suffix)


if __name__ == "__main__":
    try:
        main()
    except subprocess.CalledProcessError as error:
        print(error.stdout.decode(errors="replace"), flush=True)
        print(error.stderr.decode(errors="replace"), flush=True)
        raise SystemExit(1)
