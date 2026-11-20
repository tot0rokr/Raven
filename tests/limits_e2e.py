"""Memory and CPU limits of a disposable session, set at creation and changed while it runs (roadmap M13).

Uses a temporary HOME and XDG directories, so the user's ~/.config/raven and ~/Downloads are never
touched; the user's sessions are only read (by raven limit and raven ls). The test session is
killed at the end. Checks:
- new -m 1g -c 1 sets HostConfig and the cgroup (memory.max, memory.swap.max, cpu.max);
- limit -m 3g -c 2.5 changes them while the session runs: same container, same start time;
- with a process holding 700MiB, -m 600m is refused with the minimum and nothing changes, and a
  limit above the use is taken while the process keeps running without an OOM kill;
- -c 0.5 holds two busy loops at about half a core; raven ls shows use/limit;
- backup --stop and recreate -t keep the limits;
- a paused session refuses lowering memory or CPUs and takes raising them;
- -m max -c max set the server's memory (whole MiB) and cores, shown as "(max)"; -m 25% -c 50% set shares of
  them, shown with the share next to each limit;
- values out of range are refused with exit status 3.
"""

import json
import os
from pathlib import Path
import re
import secrets
import shutil
import subprocess
import sys
import tempfile
import time

SOURCE = Path(__file__).resolve().parents[1]
RAVEN = str(SOURCE / "raven")
IMAGE = os.environ.get("RAVEN_IMAGE", "raven:latest")
TIMEOUT = 180
MiB, GiB = 1024 ** 2, 1024 ** 3
HOG_SIZE = 700 * MiB

HOG = r'''
import os, sys, time
size = int(sys.argv[1])
buffer = bytearray(b"\xa5") * size  # written, so every page is resident
with open("/home/raven/Downloads/m13-hog", "w") as f:
    f.write(f"{os.getpid()}\n")
while True:
    time.sleep(1)
'''


def raven(environment, *arguments, check=True):
    result = subprocess.run([RAVEN, *arguments], env=environment, capture_output=True, text=True,
                            stdin=subprocess.DEVNULL, timeout=TIMEOUT)
    if check and result.returncode:
        raise AssertionError(f"raven {' '.join(arguments)} failed ({result.returncode}): {result.stderr}")
    return result


def wait(condition, what, seconds=60):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        value = condition()
        if value:
            return value
        time.sleep(0.2)
    raise AssertionError(f"timed out waiting for {what}")


class Session:
    def __init__(self, sid):
        self.sid = sid
        self.container = f"raven-{re.sub(r'[^A-Za-z0-9_.-]', '_', os.environ['USER'])}-{sid}"

    def inspect(self):
        result = subprocess.run(["docker", "inspect", self.container], capture_output=True, check=True,
                                timeout=TIMEOUT)
        return json.loads(result.stdout)[0]

    def limits(self):
        item = self.inspect()
        host = item["HostConfig"]
        return {"id": item["Id"], "started": item["State"]["StartedAt"], "status": item["State"]["Status"],
                "memory": host["Memory"], "swap": host["MemorySwap"], "nano": host["NanoCpus"],
                "pids": host["PidsLimit"]}

    def cgroup(self):
        pid = self.inspect()["State"]["Pid"]
        path = next(line[3:] for line in Path(f"/proc/{pid}/cgroup").read_text().splitlines()
                    if line.startswith("0::"))
        return Path("/sys/fs/cgroup" + path)

    def read(self, name):
        return (self.cgroup() / name).read_text().strip()

    def memory(self):
        return int(self.read("memory.current"))

    def cpu_usec(self):
        return int(re.search(r"^usage_usec ([0-9]+)$", self.read("cpu.stat"), re.M)[1])

    def oom_kills(self):
        return int(re.search(r"^oom_kill ([0-9]+)$", self.read("memory.events"), re.M)[1])

    def exec(self, *arguments):
        return subprocess.run(["docker", "exec", self.container, *arguments], capture_output=True, text=True,
                              timeout=TIMEOUT)

    def cpu_share(self, seconds=3.0):
        """Cores used over a few seconds, from the cgroup's cpu.stat."""
        before, started = self.cpu_usec(), time.monotonic()
        time.sleep(seconds)
        return (self.cpu_usec() - before) / ((time.monotonic() - started) * 1e6)


def ls_row(environment, sid):
    lines = raven(environment, "ls").stdout.splitlines()
    header = lines[0].split()
    assert header == ["ID", "NAME", "STATE", "WM", "DISPLAY", "PORT", "X11", "WEB", "CPU", "MEM", "CMD"], header
    row = next(line.split() for line in lines[1:] if line.split()[:1] == [sid])
    return dict(zip([h.lower() for h in header], row)), next(line for line in lines[1:] if line.split()[:1] == [sid])


def refused(environment, sid, *options, status):
    result = raven(environment, "limit", "-t", sid, *options, check=False)
    assert result.returncode == status, (options, result.returncode, result.stderr)
    return result.stderr.strip()


def main():
    scratch = Path(tempfile.mkdtemp(prefix="raven-limits-e2e-"))
    environment = dict(os.environ)
    environment.pop("DISPLAY", None)
    for variable in ("RAVEN_MEMORY", "RAVEN_CPUS"):
        environment.pop(variable, None)
    for variable, directory in (("HOME", "home"), ("XDG_CONFIG_HOME", "config"), ("XDG_STATE_HOME", "state"),
                                ("XDG_RUNTIME_DIR", "runtime"), ("XDG_DATA_HOME", "data")):
        (scratch / directory).mkdir(mode=0o700)
        environment[variable] = str(scratch / directory)
    environment["RAVEN_IMAGE"] = IMAGE
    previous_hostd = f"@raven-host-{os.getuid()}" in Path("/proc/net/unix").read_text()
    downloads = scratch / "home" / "Downloads"
    name = f"m13e2e-{secrets.token_hex(3)}"
    total = int(re.search(r"^MemTotal:\s+([0-9]+) kB", Path("/proc/meminfo").read_text(), re.M)[1]) * 1024
    cores = int(subprocess.run(["nproc"], capture_output=True, text=True, check=True,
                               env={"PATH": os.environ["PATH"]}).stdout)
    sid = None
    report = {}
    try:
        # Created with limits
        result = raven(environment, "new", "-d", "-s", name, "-w", "xmonad", "-g", "1024x768", "-m", "1g", "-c", "1",
                       "xterm")
        sid = re.search(r"raven: created session ([0-9]+)", result.stderr)[1]
        assert re.search(rf"; memory limit 1GiB \([0-9.]+% of [0-9.]+GiB\), 1 core \([0-9.]+% of {cores}\)\n$",
                         result.stderr), result.stderr
        session = Session(sid)
        first = session.limits()
        assert (first["memory"], first["swap"], first["nano"], first["pids"]) == (GiB, GiB, 10 ** 9, 512), first
        assert (session.read("memory.max"), session.read("memory.swap.max"), session.read("cpu.max")) == \
            (str(GiB), "0", "100000 100000"), (session.read("memory.max"), session.read("cpu.max"))
        report["new"] = result.stderr.strip().splitlines()[-1]

        # Changed while running: the same container keeps running
        result = raven(environment, "limit", "-t", sid, "-m", "3g", "-c", "2.5")
        report["limit_change"] = {"stderr": result.stderr.strip(), "stdout": result.stdout.splitlines()}
        assert result.stderr == f"raven: session {sid} ({name}): memory limit 1GiB -> 3GiB, cpus 1 -> 2.5;" \
                                " applied now, its apps keep running\n", result.stderr
        now = session.limits()
        assert (now["id"], now["started"], now["status"]) == (first["id"], first["started"], "running"), now
        assert (now["memory"], now["swap"], now["nano"], now["pids"]) == (3 * GiB, 3 * GiB, 2_500_000_000, 512), now
        assert (session.read("memory.max"), session.read("memory.swap.max"), session.read("cpu.max")) == \
            (str(3 * GiB), "0", "250000 100000")
        assert re.search(r", limit 3GiB \([0-9.]+% of [0-9.]+GiB\)$", result.stdout.splitlines()[1]), result.stdout
        assert result.stdout.splitlines()[2].endswith(f", limit 2.5 cores ({2.5 * 100 / cores:.1f}% of {cores})"
                                                      .replace(".0%", "%")), result.stdout
        report["limit_show"] = raven(environment, "limit", sid).stdout.splitlines()
        report["limit_all"] = raven(environment, "limit").stdout.splitlines()
        assert any(line.split()[:3] == [sid, name, "running"] for line in report["limit_all"]), report["limit_all"]

        # Not below what the session uses
        (downloads / "m13-hog.py").write_text(HOG)
        raven(environment, "run", "-t", sid, f"python3 /home/raven/Downloads/m13-hog.py {HOG_SIZE}")
        hog = wait(lambda: (downloads / "m13-hog").exists() and (downloads / "m13-hog").read_text().strip(),
                   "the memory hog")
        wait(lambda: session.memory() >= HOG_SIZE, "the hog's memory")
        kills = session.oom_kills()
        message = refused(environment, sid, "-m", "600m", status=4)
        report["below_use_refusal"] = message
        match = re.fullmatch(rf"raven: session {sid} uses ([0-9.]+)(MiB|GiB) of memory now; a limit below "
                             r"([0-9]+)MiB \(its use \+ 64MiB\) would make the kernel kill its apps", message)
        assert match, message
        minimum = int(match[3])
        assert minimum >= HOG_SIZE // MiB + 64, minimum
        assert session.limits()["memory"] == 3 * GiB and session.read("memory.max") == str(3 * GiB)
        allowed = "1g" if minimum <= 1024 - 32 else f"{minimum + 32}m"
        result = raven(environment, "limit", "-t", sid, "-m", allowed)
        allowed_bytes = GiB if allowed == "1g" else (minimum + 32) * MiB
        assert session.limits()["memory"] == allowed_bytes and session.read("memory.max") == str(allowed_bytes)
        assert session.exec("kill", "-0", hog).returncode == 0, "the hog did not survive the lower limit"
        assert session.oom_kills() == kills, "an app was killed for memory"
        report["below_use_allowed"] = {"value": allowed, "memory_now": session.memory()}
        row, line = ls_row(environment, sid)
        assert re.fullmatch(r"[0-9.]+(MiB|GiB)/" + ("1GiB" if allowed == "1g" else r"[0-9.]+(MiB|GiB)"),
                            row["mem"]), line
        assert re.fullmatch(r"[0-9]+%/2\.5", row["cpu"]), line
        report["ls_after_memory"] = line
        session.exec("kill", hog)

        # Two busy loops are held at about half a core
        for _ in range(2):
            raven(environment, "run", "-t", sid, "exec sh -c 'while :; do :; done'")
        wait(lambda: session.cpu_share(1.0) > 1.5, "two busy loops on 2.5 cores", 30)
        raven(environment, "limit", "-t", sid, "-c", "0.5")
        assert session.read("cpu.max") == "50000 100000", session.read("cpu.max")
        time.sleep(1)
        share = session.cpu_share(4.0)
        report["cpu_share_at_0.5"] = round(share, 3)
        assert 0.4 <= share <= 0.6, share
        row, line = ls_row(environment, sid)
        report["ls_at_0.5"] = line
        percent = int(re.fullmatch(r"([0-9]+)%/0\.5", row["cpu"])[1])
        assert 35 <= percent <= 65, line
        session.exec("pkill", "-f", "while :; do :; done")

        # backup --stop starts the same container again: Docker keeps its limits
        before = session.limits()
        raven(environment, "backup", "-t", sid, "--stop", "-o", str(scratch / "m13.tar"))
        after = session.limits()
        assert after["id"] == before["id"] and after["status"] == "running", (before, after)
        assert (after["memory"], after["nano"], after["pids"]) == (allowed_bytes, 500_000_000, 512), after
        assert session.read("cpu.max") == "50000 100000"

        # recreate makes a new container with the same limits
        raven(environment, "recreate", "-t", sid)
        after = session.limits()
        assert after["id"] != before["id"], "recreate kept the container"
        assert (after["memory"], after["swap"], after["nano"], after["pids"]) == \
            (allowed_bytes, allowed_bytes, 500_000_000, 512), after
        assert (session.read("memory.max"), session.read("cpu.max")) == (str(allowed_bytes), "50000 100000")
        report["recreate"] = after

        # A paused session only grows
        raven(environment, "limit", "-t", sid, "-c", "1")
        raven(environment, "pause", "-t", sid)
        paused = session.limits()
        report["paused_refusals"] = [refused(environment, sid, "-m", "768m", status=5),
                                     refused(environment, sid, "-c", "0.5", status=5)]
        assert report["paused_refusals"][0] == f"raven: session {sid} is paused; its limits can only be raised until" \
                                               f" it is resumed: raven resume {sid}", report["paused_refusals"]
        assert session.limits() == paused
        result = raven(environment, "limit", "-t", sid, "-m", "2g", "-c", "2")
        now = session.limits()
        assert (now["status"], now["memory"], now["nano"], now["started"]) == \
            ("paused", 2 * GiB, 2 * 10 ** 9, paused["started"]), now
        assert (session.read("memory.max"), session.read("cpu.max")) == (str(2 * GiB), "200000 100000")
        report["paused_raise"] = result.stdout.splitlines()
        raven(environment, "resume", "-t", sid)

        # max is the server's memory in whole MiB and every core
        result = raven(environment, "limit", "-t", sid, "-m", "max", "-c", "max")
        now = session.limits()
        assert (now["memory"], now["nano"]) == (total // MiB * MiB, cores * 10 ** 9), now
        assert session.read("memory.max") == str(total // MiB * MiB)
        assert " (max)\n" in result.stdout and f"limit {cores} cores (max)\n" in result.stdout, result.stdout
        report["max"] = result.stdout.splitlines()
        report["ls_at_max"] = ls_row(environment, sid)[1]

        # Shares of the server: memory in whole MiB, CPUs to two decimals
        result = raven(environment, "limit", "-t", sid, "-m", "25%", "-c", "50%")
        now = session.limits()
        assert (now["memory"], now["nano"]) == (total // 4 // MiB * MiB, round(cores * 0.5, 2) * 10 ** 9), now
        assert re.search(r"^memory  used [0-9.]+(MiB|GiB), limit [0-9.]+GiB \(25% of [0-9.]+GiB\)$", result.stdout,
                         re.M), result.stdout
        assert re.search(rf"^cpu     used .*, limit [0-9.]+ cores? \(50% of {cores}\)$", result.stdout, re.M), \
            result.stdout
        report["share"] = {"stderr": result.stderr.strip(), "stdout": result.stdout.splitlines()}
        message = refused(environment, sid, "-c", "1%", status=3)
        assert message.startswith(f"raven: CPUs 1% of the server's {cores} cores is "), message
        report["share_refusal"] = message
        report["limit_all_shares"] = [line for line in raven(environment, "limit").stdout.splitlines()
                                      if line.split()[:1] in (["ID"], [sid]) or not line[:1].isdigit()]

        # Out of range: nothing changes
        before = session.limits()
        report["range_refusals"] = [refused(environment, sid, "-m", "100m", status=3),
                                    refused(environment, sid, "-c", str(cores + 1), status=3),
                                    refused(environment, sid, "-m", f"{total // MiB + 1}m", status=3)]
        assert session.limits() == before

        logs = subprocess.run(["docker", "logs", session.container], capture_output=True, text=True, timeout=TIMEOUT)
        for text in ("Zygote process exited", "Fatal server error"):
            assert text not in logs.stdout + logs.stderr, text

        raven(environment, "kill", "-t", sid)
        assert subprocess.run(["docker", "inspect", session.container], capture_output=True).returncode != 0
        sid = None
        report["limits_e2e"] = "passed"
        print(json.dumps(report, indent=2, ensure_ascii=False))
    finally:
        if sid is not None:
            raven(environment, "kill", "-t", sid, check=False)
        if not previous_hostd:
            raven(environment, "hostd", "stop", check=False)
        shutil.rmtree(scratch, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(main())
