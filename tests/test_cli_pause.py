"""The raven CLI around paused sessions, against fake docker and ss commands.

No real container is touched: the fake docker keeps the sessions in a JSON file and logs
every call, so commands that go through every session (recreate -a, menu sync, home sync,
wallpaper) can be checked for leaving paused sessions alone.
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

FAKE_DOCKER = r'''#!/usr/bin/env python3
import json, os, re, sys
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

def expand(template, sid, s):
    labels = {"raven.id": sid, "raven.display": s["display"], "raven.port": s["port"], "raven.cmd": "xterm",
              "raven.wm": "xmonad", "raven.geometry": "800x600", "raven.features": "wallpaper"}
    template = re.sub(r'\{\{index \.Config\.Labels "([^"]+)"\}\}', lambda m: labels.get(m[1], ""), template)
    return (template.replace("{{.State.Running}}", "true" if s["state"] in ("running", "paused") else "false")
            .replace("{{.State.Pid}}", "0").replace("{{.Image}}", "sha256:fake").replace("{{.Id}}", "f" * 64)
            .replace("{{.HostConfig.Memory}}", "2147483648").replace("{{.HostConfig.MemorySwap}}", "2147483648")
            .replace("{{.HostConfig.NanoCpus}}", "2000000000").replace("{{.HostConfig.PidsLimit}}", "512"))

if args[:2] == ["ps", "-a"] and "--format" in args:
    if any(a.startswith("label=raven.user=") for a in args):
        for sid, s in sessions.items():
            print(sid, s["display"], s["port"], s["state"], "xterm")
    sys.exit(0)
if args[0] == "inspect":
    template = args[args.index("-f") + 1]
    names = [a for a in args[1:] if not a.startswith("-") and a != template]
    for name in names:
        s = session(name)
        if s is None:
            sys.exit(1)
        print(expand(template, name[len(prefix):], s))
    sys.exit(0)
if args[0] == "image":
    sys.exit(0)
if args[0] in ("exec", "start", "stop", "pause", "unpause", "rm", "create"):
    names = [a for a in args[1:] if a.startswith(prefix)]
    s = session(names[0]) if names else None
    if s is not None and s["state"] == "paused" and args[0] in ("exec", "start"):
        print("Error response from daemon: Container is paused, unpause the container before exec", file=sys.stderr)
        sys.exit(1)
    if s is not None and args[0] == "pause":
        s["state"] = "paused"
    if s is not None and args[0] == "unpause":
        s["state"] = "running"
    if s is not None and args[0] in ("start", "create"):
        s["state"] = "running"
    if s is not None and args[0] == "stop":
        s["state"] = "exited"
    save()
    sys.exit(0)
sys.exit(0)
'''

FAKE_SS = r'''#!/bin/sh
# Ports always listen, the host relay always runs, no viewer is connected.
case "$*" in
    *-Hxlp*) echo "u_str LISTEN 0 5 @raven-host-$(id -u) 1 * 0 users:((\"python3\",pid=1,fd=3))" ;;
    *-Hltn*) echo "LISTEN 0 5 127.0.0.1:15900 0.0.0.0:*" ;;
esac
exit 0
'''


class PausedSessionCliTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="raven-cli-pause-")
        self.addCleanup(self.temporary.cleanup)
        root = Path(self.temporary.name)
        bin_dir = root / "bin"
        bin_dir.mkdir()
        for name, text in (("docker", FAKE_DOCKER), ("ss", FAKE_SS)):
            (bin_dir / name).write_text(text)
            (bin_dir / name).chmod(0o755)
        self.state = root / "docker.json"
        self.log = root / "docker.log"
        for name in ("home", "config", "state", "runtime"):
            (root / name).mkdir()
        self.environment = {
            "PATH": f"{bin_dir}:{os.environ['PATH']}", "HOME": str(root / "home"),
            "XDG_CONFIG_HOME": str(root / "config"), "XDG_STATE_HOME": str(root / "state"),
            "XDG_RUNTIME_DIR": str(root / "runtime"), "FAKE_DOCKER_STATE": str(self.state),
            "FAKE_DOCKER_LOG": str(self.log), "RAVEN_IMAGE": "raven:fake", "RAVEN_CORE_IMAGE": "raven-core:fake",
        }
        self.sessions({"0": "running", "1": "paused", "2": "exited"})

    def sessions(self, states):
        sessions = {sid: {"state": state, "display": str(100 + int(sid)), "port": str(15900 + int(sid))}
                    for sid, state in states.items()}
        self.state.write_text(json.dumps({"user": USER, "sessions": sessions}))
        self.log.write_text("")

    def raven(self, *args, display=None):
        environment = dict(self.environment)
        if display:
            environment["DISPLAY"] = display
        return subprocess.run([str(SOURCE / "raven"), *args], capture_output=True, text=True,
                              env=environment, stdin=subprocess.DEVNULL, timeout=60)

    def calls(self):
        return [json.loads(line) for line in self.log.read_text().splitlines()]

    def changes(self, sid):
        """Docker calls that act on session sid's container, other than reading it."""
        name = f"raven-{USER}-{sid}"
        return [call for call in self.calls() if call[0] not in ("ps", "inspect") and name in call]

    def state_of(self, sid):
        return json.loads(self.state.read_text())["sessions"][sid]["state"]

    def test_commands_that_need_the_session_up_refuse_a_paused_one(self):
        hint = "raven: session 1 is paused; resume it first: raven resume 1\n"
        for args, display in ((("attach", "1"), ":59999"), (("run", "-t", "1", "xterm"), None),
                              (("run", "-t", "1", "--host", "xterm"), None), (("shell", "-t", "1"), None),
                              (("menu", "show", "-t", "1"), None), (("home", "sync", "-t", "1"), None),
                              (("wallpaper", "color", "-t", "1", "#24303c"), None)):
            with self.subTest(args=args):
                self.log.write_text("")
                result = self.raven(*args, display=display)
                self.assertEqual(result.returncode, 1, result.stderr)
                self.assertEqual(result.stderr, hint)
                self.assertEqual(self.changes("1"), [])
                self.assertEqual(self.state_of("1"), "paused")

    def test_all_session_commands_leave_paused_sessions_alone(self):
        config = Path(self.environment["XDG_CONFIG_HOME"]) / "raven"
        (config / "home").mkdir(parents=True)
        (config / "home" / ".profile-test").write_text("x\n")
        (config / "menu.conf").write_text("Terminal = xterm\n")
        for args in (("menu", "sync"), ("home", "sync"), ("wallpaper", "color", "#24303c")):
            with self.subTest(args=args):
                self.log.write_text("")
                result = self.raven(*args)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(self.changes("1"), [])
                self.assertTrue(self.changes("0"), "the running session was not updated")
        self.assertEqual(self.state_of("1"), "paused")

    def test_recreate_all_skips_paused_sessions_and_says_so(self):
        result = self.raven("recreate", "-a")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("raven: session 1 is paused; skipped (resume it, or recreate -t 1)\n", result.stderr)
        self.assertIn("raven: recreated session 0\n", result.stderr)
        self.assertIn("raven: recreated session 2\n", result.stderr)
        self.assertNotIn("recreated session 1", result.stderr)
        self.assertEqual(self.changes("1"), [])
        self.assertEqual(self.state_of("1"), "paused")
        self.assertIn(["stop", f"raven-{USER}-0"], self.calls())

    def test_explicit_recreate_and_kill_still_act_on_a_paused_session(self):
        result = self.raven("recreate", "-t", "1")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn(["stop", f"raven-{USER}-1"], self.changes("1"))
        create, = [call for call in self.changes("1") if call[0] == "create"]
        self.assertEqual([create[create.index(flag) + 1] for flag in ("--memory", "--memory-swap", "--cpus",
                                                                       "--pids-limit")],
                         ["2147483648", "2147483648", "2", "512"])
        self.sessions({"1": "paused"})
        result = self.raven("kill", "1")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn(["rm", "-f", f"raven-{USER}-1"], self.changes("1"))

    def test_pause_and_resume_refuse_the_wrong_state(self):
        for args, message in ((("pause", "1"), "raven: session 1 is already paused\n"),
                              (("pause", "-t", "2"), "raven: session 2 is not running\n"),
                              (("resume", "0"), "raven: session 0 is not paused\n"),
                              (("resume", "2"), "raven: session 2 is not paused\n"),
                              (("pause",), "raven: usage: raven pause [-t] TARGET\n"),
                              (("resume",), "raven: usage: raven resume [-t] TARGET\n")):
            with self.subTest(args=args):
                self.log.write_text("")
                result = self.raven(*args)
                self.assertEqual((result.returncode, result.stderr), (1, message))
                self.assertFalse([call for call in self.calls() if call[0] in ("pause", "unpause", "exec")])

    def test_pause_disconnects_viewers_before_freezing_and_resume_unfreezes(self):
        result = self.raven("pause", "0")
        self.assertEqual(result.returncode, 0, result.stderr)
        changes = self.changes("0")
        self.assertEqual(changes[0][0], "exec")
        self.assertIn("vncconfig", changes[0][-1])
        self.assertIn("pkill", changes[0][-1])
        self.assertEqual(changes[1], ["pause", f"raven-{USER}-0"])
        self.assertIn("raven: resume with: raven resume 0\n", result.stderr)
        self.assertIn("a server reboot loses the paused state", result.stderr)
        self.assertIn("run --host", result.stderr)
        self.assertEqual(self.state_of("0"), "paused")
        result = self.raven("ls")
        self.assertEqual(result.returncode, 0, result.stderr)
        rows = {line.split()[0]: line.split() for line in result.stdout.splitlines()[1:] if line[:1].isdigit()}
        self.assertEqual(result.stdout.splitlines()[0].split(),
                         ["ID", "NAME", "STATE", "WM", "DISPLAY", "PORT", "X11", "WEB", "CPU", "MEM", "CMD"])
        # No cgroup in the fake, so the paused session's memory use is unknown
        self.assertEqual(rows["0"][2:3] + rows["0"][6:10], ["paused", "-", "-", "0%/2", "?/2GiB"])
        self.assertEqual(rows["2"][6:10], ["-", "-", "-/2", "-/2GiB"])
        self.assertEqual(result.stdout.splitlines()[-1],
                         "paused sessions keep their memory until resumed; a server reboot loses them")
        self.log.write_text("")
        result = self.raven("resume", "-t", "0")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.changes("0"), [["unpause", f"raven-{USER}-0"]])
        self.assertIn("attach again: raven attach 0", result.stderr)


if __name__ == "__main__":
    unittest.main()
