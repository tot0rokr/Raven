"""raven memory and CPU limits (new, restore, limit, recreate, ls) against fake docker, ss and nproc.

No real container is touched: the fake docker keeps sessions with their HostConfig limits in a
JSON file and logs every call, and RAVEN_TEST_PROC and RAVEN_TEST_CGROUP point the CLI at a
fake /proc (MemTotal, each session's cgroup) and cgroup tree (memory.current, cpu.stat).
"""

import json
import os
from pathlib import Path
import re
import subprocess
import tempfile
import unittest

SOURCE = Path(__file__).resolve().parents[1]
USER = re.sub(r"[^A-Za-z0-9_.\n-]", "_", subprocess.run(["id", "-un"], capture_output=True, text=True).stdout.strip())
MiB, GiB = 1024 ** 2, 1024 ** 3
# A server of 8 cores and MemTotal 16000000 kB: max memory is 15625 MiB (15.3GiB)
CORES = 8
MEMTOTAL_KB = 16000000
MAX_MEMORY = MEMTOTAL_KB * 1024 // MiB * MiB
TOTAL = MEMTOTAL_KB * 1024
NANO = 1_000_000_000


def share(part, whole):
    """part as a share of whole the way raven shows it: 25%, 12.5%."""
    text = f"{part * 100 / whole:.1f}"
    return (text[:-2] if text.endswith(".0") else text) + "%"


def memory_share(limit):
    return f"({share(limit, TOTAL)} of 15.3GiB)"


def cores_share(nano):
    return f"({share(nano, CORES * NANO)} of {CORES})"

FAKE_DOCKER = r'''#!/usr/bin/env python3
import json, os, re, sys
from decimal import Decimal
state_path, log_path = os.environ["FAKE_DOCKER_STATE"], os.environ["FAKE_DOCKER_LOG"]
args = sys.argv[1:]
with open(log_path, "a") as log:
    log.write(json.dumps(args) + "\n")
with open(state_path) as f:
    state = json.load(f)
sessions, prefix = state["sessions"], "raven-" + state["user"] + "-"

def session(name):
    return sessions.get(name[len(prefix):]) if name.startswith(prefix) else None

def save():
    with open(state_path, "w") as f:
        json.dump(state, f)

def option(name, default=None):
    return args[args.index(name) + 1] if name in args else default

def expand(template, sid, s):
    labels = {"raven.id": sid, "raven.display": s["display"], "raven.port": s["port"], "raven.cmd": "xterm",
              "raven.wm": "xmonad", "raven.geometry": "800x600", "raven.features": "", "raven.uid": "60000"}
    labels.update(s.get("labels", {}))
    template = re.sub(r'\{\{index \.Config\.Labels "([^"]+)"\}\}', lambda m: labels.get(m[1], ""), template)
    running = s["state"] in ("running", "paused")
    values = {".State.Running": "true" if running else "false", ".State.Pid": str(s.get("pid", 0) if running else 0),
              ".HostConfig.Memory": str(s["memory"]), ".HostConfig.MemorySwap": str(s.get("swap", s["memory"])),
              ".HostConfig.NanoCpus": str(s["nano"]),
              ".HostConfig.PidsLimit": "<nil>" if s["pids"] is None else str(s["pids"]),
              ".Image": "sha256:fake", ".Config.Image": "raven:fake", ".Id": "f" * 64}
    return re.sub(r"\{\{(\.[A-Za-z.]+)\}\}", lambda m: values[m[1]], template)

if args[:2] == ["ps", "-a"] and "--format" in args:
    for sid, s in sessions.items():
        if any(a.startswith("label=raven.user=") for a in args):
            print(sid, s["display"], s["port"], s["state"], "xterm")
        elif "label=raven.display" in args:
            print(s["display"])
    sys.exit(0)
if args[0] == "inspect":
    template = option("-f")
    names = [a for a in args[1:] if not a.startswith("-") and a != template]
    for name in names:
        s = session(name)
        if s is None:
            sys.exit(1)
        print(expand(template, name[len(prefix):], s))
    sys.exit(0)
if args[0] == "image":
    sys.exit(0)
if args[0] == "volume":
    sys.exit(1 if args[1] == "inspect" else 0)
if args[0] == "update":
    s = session(args[-1])
    if state.get("update_fails"):
        print("Error response from daemon: Cannot update container", file=sys.stderr)
        sys.exit(1)
    if not state.get("update_ignored"):
        if "--memory" in args:
            s["memory"], s["swap"] = int(option("--memory")), int(option("--memory-swap"))
        if "--cpus" in args:
            s["nano"] = int(Decimal(option("--cpus")) * 1000000000)
    save()
    sys.exit(0)
if args[0] == "create":
    labels = dict(a.split("=", 1) for a in args[1:] if "=" in a and a.startswith("raven."))
    sessions[labels["raven.id"]] = {
        "state": "created", "display": labels["raven.display"], "port": labels["raven.port"], "labels": labels,
        "memory": int(option("--memory")), "swap": int(option("--memory-swap")),
        "nano": int(Decimal(option("--cpus")) * 1000000000), "pids": int(option("--pids-limit"))}
    save()
    sys.exit(0)
if args[0] in ("exec", "start", "stop", "pause", "unpause", "rm", "run"):
    names = [a for a in args[1:] if a.startswith(prefix)]
    s = session(names[0]) if names else None
    if s is not None and s["state"] == "paused" and args[0] in ("exec", "start"):
        print("Error response from daemon: Container is paused", file=sys.stderr)
        sys.exit(1)
    if s is not None and args[0] in ("start", "unpause"):
        s["state"] = "running"
    if s is not None and args[0] == "stop":
        s["state"] = "exited"
    if s is not None and args[0] == "pause":
        s["state"] = "paused"
    if s is not None and args[0] == "rm":
        del sessions[names[0][len(prefix):]]
    save()
    sys.exit(0)
sys.exit(0)
'''

FAKE_SS = r'''#!/usr/bin/env python3
# The VNC port of a running session listens, the host relay runs, no viewer is connected.
import json, os, re, sys
args = " ".join(sys.argv[1:])
with open(os.environ["FAKE_DOCKER_STATE"]) as f:
    sessions = json.load(f)["sessions"]
if "-Hxlp" in args:
    print(f'u_str LISTEN 0 5 @raven-host-{os.getuid()} 1 * 0 users:(("python3",pid=1,fd=3))')
elif "-Hltn" in args:
    port = re.search(r"sport = :([0-9]+)", args)
    if port and any(s["port"] == port[1] and s["state"] == "running" for s in sessions.values()):
        print(f"LISTEN 0 5 127.0.0.1:{port[1]} 0.0.0.0:*")
'''

FAKE_NPROC = f"#!/bin/sh\necho {CORES}\n"


class LimitsFixture(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="raven-cli-limits-")
        self.addCleanup(self.temporary.cleanup)
        self.root = root = Path(self.temporary.name)
        bin_dir = root / "bin"
        bin_dir.mkdir()
        for name, text in (("docker", FAKE_DOCKER), ("ss", FAKE_SS), ("nproc", FAKE_NPROC)):
            (bin_dir / name).write_text(text)
            (bin_dir / name).chmod(0o755)
        self.state = root / "docker.json"
        self.log = root / "docker.log"
        self.proc = root / "proc"
        self.cgroup = root / "cgroup"
        for directory in ("home", "config", "state", "data", "runtime", "proc", "cgroup"):
            (root / directory).mkdir()
        (self.proc / "meminfo").write_text(f"MemTotal:       {MEMTOTAL_KB} kB\nMemFree:         1000 kB\n")
        self.environment = {
            "PATH": f"{bin_dir}:{os.environ['PATH']}", "HOME": str(root / "home"),
            "XDG_CONFIG_HOME": str(root / "config"), "XDG_STATE_HOME": str(root / "state"),
            "XDG_DATA_HOME": str(root / "data"), "XDG_RUNTIME_DIR": str(root / "runtime"),
            "FAKE_DOCKER_STATE": str(self.state), "FAKE_DOCKER_LOG": str(self.log),
            "RAVEN_IMAGE": "raven:fake", "RAVEN_CORE_IMAGE": "raven-core:fake",
            "RAVEN_TEST_PROC": str(self.proc), "RAVEN_TEST_CGROUP": str(self.cgroup),
        }
        self.statedir = root / "state" / "raven" / subprocess.run(["hostname"], capture_output=True,
                                                                   text=True).stdout.strip()
        self.sessions({})

    def sessions(self, specs, **extra):
        """specs: id -> (state, memory, nano, pids[, memory.current]); running and paused ones get a cgroup."""
        sessions = {}
        for sid, spec in specs.items():
            status, memory, nano, pids = spec[:4]
            pid = 1000 + int(sid)
            sessions[sid] = {"state": status, "display": str(100 + int(sid)), "port": str(15900 + int(sid)),
                             "memory": memory, "swap": memory, "nano": nano, "pids": pids, "pid": pid}
            group = self.cgroup / "raven" / sid
            group.mkdir(parents=True, exist_ok=True)
            (self.proc / str(pid)).mkdir(exist_ok=True)
            (self.proc / str(pid) / "cgroup").write_text(f"0::/raven/{sid}\n")
            (self.proc / str(pid) / "comm").write_text("docker-init\n")
            (group / "cgroup.procs").write_text(f"{pid}\n")
            (group / "cpu.stat").write_text("usage_usec 1000\n")
            if len(spec) > 4 and spec[4] is not None:
                (group / "memory.current").write_text(f"{spec[4]}\n")
            else:
                (group / "memory.current").unlink(missing_ok=True)
        self.state.write_text(json.dumps(dict({"user": USER, "sessions": sessions}, **extra)))
        self.log.write_text("")

    def update(self, **values):
        state = json.loads(self.state.read_text())
        state.update(values)
        self.state.write_text(json.dumps(state))

    def name(self, sid, name):
        self.statedir.mkdir(parents=True, exist_ok=True)
        (self.statedir / f"{sid}.name").write_text(name + "\n")

    def raven(self, *args, **environment):
        return subprocess.run([str(SOURCE / "raven"), *args], capture_output=True, text=True,
                              env=dict(self.environment, **environment), stdin=subprocess.DEVNULL, timeout=60)

    def calls(self, verb=None):
        calls = [json.loads(line) for line in self.log.read_text().splitlines()]
        return [c for c in calls if verb is None or c[0] == verb]

    def changes(self):
        """Docker calls that change a container."""
        return [c for c in self.calls() if c[0] in ("create", "update", "start", "stop", "rm", "pause", "unpause")]

    def session(self, sid):
        return json.loads(self.state.read_text())["sessions"][sid]

    def limits_of(self, call):
        """--memory, --memory-swap, --cpus and --pids-limit of a docker create call."""
        return tuple(call[call.index(flag) + 1] for flag in ("--memory", "--memory-swap", "--cpus", "--pids-limit"))


class NewLimitTests(LimitsFixture):
    def test_new_takes_the_options_then_the_environment_then_the_defaults(self):
        quarter = TOTAL // 4 // MiB * MiB
        cases = ((("-m", "1.5g", "-c", "2.5"), {}, (str(3 * GiB // 2), "2.5"),
                  f"memory limit 1.5GiB {memory_share(3 * GiB // 2)}, 2.5 cores {cores_share(2.5 * NANO)}"),
                 ((), {}, (str(2 * GiB), "2"), f"memory limit 2GiB {memory_share(2 * GiB)}, 2 cores (25% of 8)"),
                 ((), {"RAVEN_MEMORY": "3G", "RAVEN_CPUS": "1"}, (str(3 * GiB), "1"),
                  f"memory limit 3GiB {memory_share(3 * GiB)}, 1 core (12.5% of 8)"),
                 (("-m", "768m"), {"RAVEN_MEMORY": "3g", "RAVEN_CPUS": "0.5"}, (str(768 * MiB), "0.5"),
                  f"memory limit 768MiB {memory_share(768 * MiB)}, 0.5 cores (6.2% of 8)"),
                 (("-m", "max", "-c", "max"), {}, (str(MAX_MEMORY), str(CORES)),
                  f"memory limit 15.3GiB (max), {CORES} cores (max)"),
                 ((), {"RAVEN_MEMORY": "max", "RAVEN_CPUS": "max"}, (str(MAX_MEMORY), str(CORES)),
                  f"memory limit 15.3GiB (max), {CORES} cores (max)"),
                 (("-m", "1048576k", "-c", "0.75"), {}, (str(GiB), "0.75"),
                  f"memory limit 1GiB {memory_share(GiB)}, 0.75 cores {cores_share(0.75 * NANO)}"),
                 # Shares of the server: memory in whole MiB, CPUs to two decimals; 100% is max
                 (("-m", "25%", "-c", "50%"), {}, (str(quarter), "4"),
                  f"memory limit 3.8GiB (25% of 15.3GiB), 4 cores (50% of 8)"),
                 (("-m", "12.5%", "-c", "33%"), {}, (str(TOTAL // 8 // MiB * MiB), "2.64"),
                  f"memory limit 1.9GiB (12.5% of 15.3GiB), 2.64 cores (33% of 8)"),
                 ((), {"RAVEN_MEMORY": "100%", "RAVEN_CPUS": "100%"}, (str(MAX_MEMORY), str(CORES)),
                  f"memory limit 15.3GiB (max), {CORES} cores (max)"),
                 (("-c", "6.25%"), {"RAVEN_MEMORY": "4%"}, (str(TOTAL * 4 // 100 // MiB * MiB), "0.5"),
                  f"memory limit 625MiB (4% of 15.3GiB), 0.5 cores (6.2% of 8)"))
        for options, environment, (memory, cpus), summary in cases:
            with self.subTest(options=options, environment=environment):
                self.sessions({})
                result = self.raven("new", "-d", *options, "xterm", **environment)
                self.assertEqual(result.returncode, 0, result.stderr)
                create, = self.calls("create")
                self.assertEqual(self.limits_of(create), (memory, memory, cpus, "512"))
                self.assertIn(f"raven: created session 0 with xmonad; {summary}\n", result.stderr)
                self.assertEqual(self.session("0")["state"], "running")

    def test_bad_values_are_refused_before_anything_is_made(self):
        cases = (
            (("-m", "2048"), {}, "memory must look like 512m, 1.5g, 4g, 25% or max: '2048'"),
            (("-m", "1.5"), {}, "memory must look like 512m, 1.5g, 4g, 25% or max: '1.5'"),
            (("-m", "2gb"), {}, "memory must look like 512m, 1.5g, 4g, 25% or max: '2gb'"),
            (("-m", "-1g"), {}, "memory must look like 512m, 1.5g, 4g, 25% or max: '-1g'"),
            (("-m", ""), {}, "memory must look like 512m, 1.5g, 4g, 25% or max: ''"),
            (("-m", "511m"), {}, "memory must be at least 512MiB: '511m'"),
            (("-m", "0.4g"), {}, "memory must be at least 512MiB: '0.4g'"),
            (("-m", "15.3g"), {}, "memory can be at most the server's memory, 15.3GiB: '15.3g'"),
            (("-m", "99999999999999999999g"), {}, "memory can be at most the server's memory, 15.3GiB: "
                                                   "'99999999999999999999g'"),
            (("-c", "0.25"), {}, "CPUs must be at least 0.5: '0.25'"),
            (("-c", "0"), {}, "CPUs must be at least 0.5: '0'"),
            (("-c", "1.234"), {}, "CPUs must be a number of cores like 1, 1.5 or 2.25, a share like 25%, or max: '1.234'"),
            (("-c", ".5"), {}, "CPUs must be a number of cores like 1, 1.5 or 2.25, a share like 25%, or max: '.5'"),
            (("-c", "two"), {}, "CPUs must be a number of cores like 1, 1.5 or 2.25, a share like 25%, or max: 'two'"),
            (("-c", "8.01"), {}, f"CPUs can be at most the server's {CORES} cores: '8.01'"),
            (("-m", "0.5%"), {}, "memory must be a share from 1% to 100% of the server: '0.5%'"),
            (("-m", "101%"), {}, "memory must be a share from 1% to 100% of the server: '101%'"),
            (("-m", "12.345%"), {}, "memory must look like 512m, 1.5g, 4g, 25% or max: '12.345%'"),
            (("-m", "%"), {}, "memory must look like 512m, 1.5g, 4g, 25% or max: '%'"),
            (("-m", "2%"), {}, "memory 2% of the server's 15.3GiB is 312MiB, below the minimum 512MiB: '2%'"),
            (("-c", "5%"), {}, f"CPUs 5% of the server's {CORES} cores is 0.4, below the minimum 0.5: '5%'"),
            (("-c", "100.01%"), {}, "CPUs must be a share from 1% to 100% of the server: '100.01%'"),
            (("-c", "-5%"), {}, "CPUs must be a number of cores like 1, 1.5 or 2.25, a share like 25%, or max: '-5%'"),
            ((), {"RAVEN_CPUS": "1%"}, f"RAVEN_CPUS 1% of the server's {CORES} cores is 0.08, below the minimum"
                                       " 0.5: '1%'"),
            ((), {"RAVEN_MEMORY": "lots"}, "RAVEN_MEMORY must look like 512m, 1.5g, 4g, 25% or max: 'lots'"),
            ((), {"RAVEN_CPUS": "64"}, f"RAVEN_CPUS can be at most the server's {CORES} cores: '64'"),
        )
        for options, environment, message in cases:
            with self.subTest(options=options, environment=environment):
                self.log.write_text("")
                result = self.raven("new", "-d", *options, "xterm", **environment)
                self.assertEqual((result.returncode, result.stderr), (3, f"raven: {message}\n"))
                self.assertEqual(self.changes(), [])
                self.assertEqual(self.calls(), [])
        # The highest values are allowed
        result = self.raven("new", "-d", "-m", f"{MEMTOTAL_KB}k", "-c", str(CORES), "xterm")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.limits_of(self.calls("create")[0]),
                         (str(MEMTOTAL_KB * 1024), str(MEMTOTAL_KB * 1024), str(CORES), "512"))
        self.assertIn(f"memory limit 15.3GiB (max), {CORES} cores (max)", result.stderr)


class LimitCommandTests(LimitsFixture):
    def test_limit_shows_one_session(self):
        self.sessions({"0": ("running", 2 * GiB, 2_000_000_000, 512, 300 * MiB),
                       "1": ("paused", MAX_MEMORY, CORES * 1_000_000_000, 512, 1536 * MiB),
                       "2": ("exited", 0, 0, None)})
        self.name("0", "work")
        result = self.raven("limit", "work")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout, "session 0 (work): running\n"
                                        f"memory  used 300MiB, limit 2GiB {memory_share(2 * GiB)}\n"
                                        "cpu     used 0% (one core = 100%), limit 2 cores (25% of 8)\n"
                                        "pids    limit 512\n")
        result = self.raven("limit", "-t", "1")
        self.assertEqual(result.stdout, "session 1: paused\n"
                                        "memory  used 1.5GiB, limit 15.3GiB (max)\n"
                                        f"cpu     used 0% (paused), limit {CORES} cores (max)\n"
                                        "pids    limit 512\n")
        result = self.raven("limit", "2")
        self.assertEqual(result.stdout, "session 2: exited\n"
                                        "memory  used -, limit none\n"
                                        "cpu     used -, limit none\n"
                                        "pids    limit none\n")
        self.assertEqual(self.changes(), [])

    def test_limit_changes_a_running_session_without_restarting_it(self):
        self.sessions({"0": ("running", 1 * GiB, 1_000_000_000, 512, 300 * MiB)})
        self.name("0", "work")
        result = self.raven("limit", "-t", "0", "-m", "3g", "-c", "2.5")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.changes(), [["update", "--memory", str(3 * GiB), "--memory-swap", str(3 * GiB),
                                           "--cpus", "2.5", f"raven-{USER}-0"]])
        self.assertEqual(result.stderr, "raven: session 0 (work): memory limit 1GiB -> 3GiB, cpus 1 -> 2.5;"
                                        " applied now, its apps keep running\n")
        self.assertIn(f"memory  used 300MiB, limit 3GiB {memory_share(3 * GiB)}\n", result.stdout)
        self.assertIn(f"limit 2.5 cores {cores_share(2.5 * NANO)}\n", result.stdout)
        self.assertEqual((self.session("0")["memory"], self.session("0")["swap"], self.session("0")["nano"]),
                         (3 * GiB, 3 * GiB, 2_500_000_000))
        # One limit alone leaves the other; max is the whole server
        self.log.write_text("")
        result = self.raven("limit", "work", "-c", "max")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.changes(), [["update", "--cpus", str(CORES), f"raven-{USER}-0"]])
        self.assertIn(f"cpus 2.5 -> {CORES} (max)", result.stderr)
        self.log.write_text("")
        result = self.raven("limit", "0", "-m", "max")
        self.assertEqual(self.changes(), [["update", "--memory", str(MAX_MEMORY), "--memory-swap", str(MAX_MEMORY),
                                           f"raven-{USER}-0"]])
        self.assertIn("memory  used 300MiB, limit 15.3GiB (max)\n", result.stdout)
        # A share of the server
        self.log.write_text("")
        result = self.raven("limit", "0", "-m", "50%", "-c", "25%")
        self.assertEqual(result.returncode, 0, result.stderr)
        half = TOTAL // 2 // MiB * MiB
        self.assertEqual(self.changes(), [["update", "--memory", str(half), "--memory-swap", str(half),
                                           "--cpus", "2", f"raven-{USER}-0"]])
        self.assertIn("memory  used 300MiB, limit 7.6GiB (50% of 15.3GiB)\n", result.stdout)
        self.assertIn("limit 2 cores (25% of 8)\n", result.stdout)
        self.assertIn(f"memory limit 15.3GiB (max) -> 7.6GiB, cpus {CORES} (max) -> 2;", result.stderr)

    def test_memory_below_the_use_is_refused_with_the_minimum(self):
        self.sessions({"0": ("running", 2 * GiB, 2_000_000_000, 512, 700 * MiB)})
        for value in ("600m", "763m", "512m"):
            with self.subTest(value=value):
                result = self.raven("limit", "0", "-m", value, "-c", "1")
                self.assertEqual((result.returncode, result.stderr),
                                 (4, "raven: session 0 uses 700MiB of memory now; a limit below 764MiB (its use"
                                     " + 64MiB) would make the kernel kill its apps\n"))
                self.assertEqual(self.changes(), [])
        result = self.raven("limit", "0", "-m", "764m")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.session("0")["memory"], 764 * MiB)
        # Raising a limit that is already close to the use is allowed
        self.sessions({"0": ("running", 512 * MiB, 2_000_000_000, 512, 500 * MiB)})
        result = self.raven("limit", "0", "-m", "520m")
        self.assertEqual(result.returncode, 0, result.stderr)
        # A session from before limits has none: any limit is lower
        self.sessions({"0": ("running", 0, 0, None, 700 * MiB)})
        result = self.raven("limit", "0", "-m", "600m")
        self.assertEqual(result.returncode, 4)
        self.assertIn("memory limit none -> 1GiB", self.raven("limit", "0", "-m", "1g").stderr)
        # Unknown use of a running session: nothing is changed
        self.sessions({"0": ("running", 2 * GiB, 2_000_000_000, 512, None)})
        result = self.raven("limit", "0", "-m", "1g")
        self.assertEqual((result.returncode, result.stderr),
                         (1, "raven: cannot read the memory use of session 0; its limits were not changed\n"))
        self.assertEqual(self.changes(), [])

    def test_a_stopped_session_takes_any_limit_for_its_next_start(self):
        self.sessions({"0": ("exited", 4 * GiB, 4_000_000_000, 512)})
        result = self.raven("limit", "0", "-m", "512m", "-c", "0.5")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stderr, "raven: session 0: memory limit 4GiB -> 512MiB, cpus 4 -> 0.5;"
                                        " it gets them when it starts\n")
        self.assertEqual([c[0] for c in self.changes()], ["update"])

    def test_a_paused_session_can_only_grow(self):
        self.sessions({"1": ("paused", 2 * GiB, 2_000_000_000, 512, 1800 * MiB)})
        message = "raven: session 1 is paused; its limits can only be raised until it is resumed: raven resume 1\n"
        for options in (("-m", "1g"), ("-c", "1"), ("-m", "4g", "-c", "1.5"), ("-m", "1g", "-c", "4")):
            with self.subTest(options=options):
                result = self.raven("limit", "1", *options)
                self.assertEqual((result.returncode, result.stderr), (5, message))
                self.assertEqual(self.changes(), [])
        result = self.raven("limit", "1", "-m", "4g", "-c", "2")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.changes(), [["update", "--memory", str(4 * GiB), "--memory-swap", str(4 * GiB),
                                           "--cpus", "2", f"raven-{USER}-1"]])
        self.assertEqual(self.session("1")["state"], "paused")

    def test_a_change_docker_did_not_make_is_reported(self):
        self.sessions({"0": ("running", 2 * GiB, 2_000_000_000, 512, 300 * MiB)}, update_ignored=True)
        result = self.raven("limit", "0", "-m", "3g")
        self.assertEqual((result.returncode, result.stderr),
                         (1, f"raven: session 0: the limits did not change as asked (now memory {2 * GiB}, "
                             f"swap {2 * GiB}, NanoCpus 2000000000)\n"))
        self.update(update_ignored=False, update_fails=True)
        result = self.raven("limit", "0", "-c", "3")
        self.assertEqual(result.returncode, 1)
        self.assertIn("raven: session 0: docker update failed; its limits were not changed\n", result.stderr)

    def test_usage_errors(self):
        self.sessions({"0": ("running", 2 * GiB, 2_000_000_000, 512, 300 * MiB)})
        usage = "raven: usage: raven limit [[-t] TARGET [-m SIZE] [-c CPUS]]\n"
        for args, message in ((("-m", "1g"), "raven: name the session to change: raven limit [-t] TARGET"
                                             " [-m SIZE] [-c CPUS]\n"),
                              (("0", "1"), usage), (("-x", "0"), usage), (("0", "-m"), "raven: -m needs an argument\n"),
                              (("nosuch", "-m", "1g"), "raven: no session named 'nosuch'\n")):
            with self.subTest(args=args):
                result = self.raven("limit", *args)
                self.assertEqual((result.returncode, result.stderr), (1, message))
        self.assertEqual(self.changes(), [])

    def test_limit_lists_every_session_and_warns_about_overcommit(self):
        self.sessions({"0": ("running", 2 * GiB, 2_000_000_000, 512, 242 * MiB),
                       "1": ("paused", MAX_MEMORY, CORES * 1_000_000_000, 512, 1536 * MiB),
                       "2": ("exited", 1536 * MiB, 500_000_000, 256)})
        self.name("0", "work")
        result = self.raven("limit")
        self.assertEqual(result.returncode, 0, result.stderr)
        lines = result.stdout.splitlines()
        self.assertEqual(lines[0].split(), ["ID", "NAME", "STATE", "MEM", "MEM-LIMIT", "CPU", "CPU-LIMIT", "PIDS"])
        self.assertEqual(lines[1].split(), ["0", "work", "running", "242MiB", "2GiB", "(13.1%", "of", "15.3GiB)",
                                            "0%", "2", "(25%", "of", "8)", "512"])
        self.assertEqual(lines[2].split(), ["1", "-", "paused", "1.5GiB", "15.3GiB", "(max)", "0%", str(CORES),
                                            "(max)", "512"])
        self.assertEqual(lines[3].split(), ["2", "-", "exited", "-", "1.5GiB", "(9.8%", "of", "15.3GiB)", "-", "0.5",
                                            "(6.2%", "of", "8)", "256"])
        # The columns line up
        for line in lines[1:4]:
            self.assertEqual(line.index(line.split()[4]), lines[0].index("MEM-LIMIT"), line)
        self.assertEqual(lines[4], "memory limits add up to 18.8GiB; the server has 15.3GiB")
        self.assertEqual(lines[5], "warning: sessions may use more memory together than the server has;"
                                   " if they do, the kernel kills apps")
        self.assertEqual(len(lines), 6)
        self.sessions({"0": ("running", 2 * GiB, 2_000_000_000, 512, 242 * MiB),
                       "1": ("exited", 4 * GiB, 1_000_000_000, 512)})
        lines = self.raven("limit").stdout.splitlines()
        self.assertEqual(lines[-1], "memory limits add up to 6GiB; the server has 15.3GiB")
        # A session without a limit can take the whole server
        self.sessions({"0": ("running", 0, 0, None, 242 * MiB)})
        lines = self.raven("limit").stdout.splitlines()
        self.assertEqual(lines[1].split(), ["0", "work", "running", "242MiB", "none", "0%", "none", "none"])
        self.assertEqual(lines[2], "memory limits add up to 15.3GiB (a session without a limit counts as the"
                                   " whole server); the server has 15.3GiB")
        self.assertEqual(len(lines), 3)
        self.assertEqual(self.changes(), [])
        self.sessions({})
        self.assertEqual(self.raven("limit").stdout, "no sessions\n")

    def test_ls_shows_use_and_limits(self):
        self.sessions({"0": ("running", 2 * GiB, 2_000_000_000, 512, 242 * MiB),
                       "1": ("paused", 3 * GiB // 2, 2_500_000_000, 512, 1288 * MiB),
                       "2": ("exited", 8 * GiB, 500_000_000, 512),
                       "3": ("running", 0, 0, None, 3 * GiB)})
        result = self.raven("ls")
        self.assertEqual(result.returncode, 0, result.stderr)
        lines = result.stdout.splitlines()
        self.assertEqual(lines[0].split(), ["ID", "NAME", "STATE", "WM", "DISPLAY", "PORT", "X11", "WEB", "CPU",
                                            "MEM", "CMD"])
        rows = {line.split()[0]: line.split() for line in lines[1:5]}
        self.assertEqual(rows["0"][8:10], ["0%/2", "242MiB/2GiB"])
        self.assertEqual(rows["1"][8:10], ["0%/2.5", "1.3GiB/1.5GiB"])
        self.assertEqual(rows["2"][8:10], ["-/0.5", "-/8GiB"])
        self.assertEqual(rows["3"][8:10], ["0%/none", "3.0GiB/none"])
        # The columns line up for the common widths
        self.assertEqual(lines[1].index("242MiB/2GiB"), lines[0].index("MEM"))
        self.assertEqual(self.changes(), [])


class RecreateLimitTests(LimitsFixture):
    def test_recreate_keeps_the_limits_of_the_old_container(self):
        self.sessions({"0": ("running", 3 * GiB, 2_500_000_000, 256, 300 * MiB),
                       "1": ("exited", 0, 0, None),
                       "2": ("exited", 1536 * MiB, 0, -1)})
        result = self.raven("recreate", "-t", "0")
        self.assertEqual(result.returncode, 0, result.stderr)
        create, = self.calls("create")
        self.assertEqual(self.limits_of(create), (str(3 * GiB), str(3 * GiB), "2.5", "256"))
        self.assertNotIn("update", [c[0] for c in self.calls()])
        # Sessions from before limits get the defaults (RAVEN_MEMORY and RAVEN_CPUS when set)
        self.log.write_text("")
        result = self.raven("recreate", "-a", RAVEN_CPUS="1.5")
        self.assertEqual(result.returncode, 0, result.stderr)
        by_id = {next(a for a in c if a.startswith("raven.id="))[len("raven.id="):]: c for c in self.calls("create")}
        self.assertEqual(self.limits_of(by_id["0"]), (str(3 * GiB), str(3 * GiB), "2.5", "256"))
        self.assertEqual(self.limits_of(by_id["1"]), (str(2 * GiB), str(2 * GiB), "1.5", "512"))
        self.assertEqual(self.limits_of(by_id["2"]), (str(1536 * MiB), str(1536 * MiB), "1.5", "512"))

    def test_bad_defaults_stop_recreate_before_any_session(self):
        self.sessions({"0": ("running", 3 * GiB, 2_500_000_000, 256, 300 * MiB)})
        result = self.raven("recreate", "-a", RAVEN_MEMORY="huge")
        self.assertEqual((result.returncode, result.stderr),
                         (3, "raven: RAVEN_MEMORY must look like 512m, 1.5g, 4g, 25% or max: 'huge'\n"))
        self.assertEqual(self.changes(), [])


if __name__ == "__main__":
    unittest.main()
