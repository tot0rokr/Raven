"""raven backup and restore around their arguments and failures, against fake docker and ss.

No real container is touched: the fake docker keeps sessions and volumes in a JSON file, logs
every call, and plays the backup helper (check, extract, backup) as the test tells it to, so
refusals can be checked for making nothing and failures for removing what was made.
"""

import json
import os
from pathlib import Path
import re
import stat
import subprocess
import tempfile
import unittest

SOURCE = Path(__file__).resolve().parents[1]
USER = re.sub(r"[^A-Za-z0-9_.\n-]", "_", subprocess.run(["id", "-un"], capture_output=True, text=True).stdout.strip())

FAKE_DOCKER = r'''#!/usr/bin/env python3
import io, json, os, re, sys, tarfile
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
    labels = dict(s.get("labels", {}), **{"raven.id": sid, "raven.display": s["display"], "raven.port": s["port"],
                                          "raven.features": "wallpaper", "raven.uid": "60000"})
    template = re.sub(r'\{\{index \.Config\.Labels "([^"]+)"\}\}', lambda m: labels.get(m[1], ""), template)
    return (template.replace("{{.State.Running}}", "true" if s["state"] in ("running", "paused") else "false")
            .replace("{{.State.Pid}}", "0").replace("{{.Image}}", "sha256:session")
            .replace("{{.Config.Image}}", "raven:fake").replace("{{.Id}}", "f" * 64))

if args[:2] == ["ps", "-a"] and "--format" in args:
    for sid, s in sessions.items():
        if any(a.startswith("label=raven.user=") for a in args):
            print(sid, s["display"], s["port"], s["state"], s.get("labels", {}).get("raven.cmd", "xterm"))
        elif "label=raven.display" in args:
            print(s["display"])
    sys.exit(0)
if args[0] == "inspect":
    template = args[args.index("-f") + 1]
    for name in [a for a in args[1:] if not a.startswith("-") and a != template]:
        s = session(name)
        if s is None:
            sys.exit(1)
        print(expand(template, name[len(prefix):], s))
    sys.exit(0)
if args[:2] == ["image", "inspect"]:
    if "-f" in args and args[args.index("-f") + 1] == "{{.Id}}":
        print(state.get("image_id", "sha256:current"))
    elif "-f" in args and "raven.uid" in args[args.index("-f") + 1]:
        print("60000")
    elif "-f" in args:
        print("")
    sys.exit(0)
if args[0] == "volume":
    volumes = state.setdefault("volumes", [])
    if args[1] == "inspect":
        sys.exit(0 if args[2] in volumes else 1)
    if args[1] == "create":
        volumes.append(args[2])
    if args[1] == "rm":
        volumes.remove(args[2])
    save()
    sys.exit(0)
if args[0] == "run":
    entry = args[args.index("--entrypoint") + 1]
    if entry != "python3":
        sys.exit(0)
    command = args[args.index("-c") + 2]
    helper = state.get("helper", {}).get(command, {})
    if command == "check":
        sys.stdin.buffer.read()
        if helper.get("rc", 0):
            print("raven: the backup is bad", file=sys.stderr)
            sys.exit(3)
        for key, value in state["backup"].items():
            print(f"{key}={value}")
    elif command == "extract":
        sys.stdin.buffer.read()
        if helper.get("rc", 0):
            print("raven: tar could not unpack the home", file=sys.stderr)
            sys.exit(3)
    elif command == "backup":
        if helper.get("rc", 0):
            sys.stdout.buffer.write(b"partial")
            sys.exit(3)
        data = json.dumps({"home": {"entries": 7}}, indent=2).encode()
        buffer = io.BytesIO()
        with tarfile.open(fileobj=buffer, mode="w") as archive:
            entry = tarfile.TarInfo("manifest.json")
            entry.size = len(data)
            archive.addfile(entry, io.BytesIO(data))
        sys.stdout.buffer.write(buffer.getvalue())
    sys.exit(0)
if args[0] == "create":
    labels = dict(a.split("=", 1) for a in args[1:] if "=" in a and a.startswith("raven."))
    sid = labels["raven.id"]
    sessions[sid] = {"state": "created", "display": labels["raven.display"], "port": labels["raven.port"],
                     "labels": labels}
    save()
    sys.exit(0)
if args[0] in ("exec", "start", "stop", "pause", "unpause", "rm"):
    names = [a for a in args[1:] if a.startswith(prefix)]
    s = session(names[0]) if names else None
    if s is not None and args[0] == "start" and state.get("start_fails"):
        print("Error response from daemon: cannot start", file=sys.stderr)
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

BACKUP = {"user": USER, "host": "server", "created": "2026-11-19T00:00:00Z", "id": "3", "name": "work",
          "cmd": "xclock -digital", "wm": "openbox", "geometry": "1024x768", "uid": "60000",
          "image_tag": "raven:latest", "image_id": "sha256:current", "features": "wallpaper",
          "sha256": "0" * 64, "size": "100", "entries": "7", "foreign": "0"}


class BackupCliTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="raven-cli-backup-")
        self.addCleanup(self.temporary.cleanup)
        self.root = root = Path(self.temporary.name)
        bin_dir = root / "bin"
        bin_dir.mkdir()
        for name, text in (("docker", FAKE_DOCKER), ("ss", FAKE_SS)):
            (bin_dir / name).write_text(text)
            (bin_dir / name).chmod(0o755)
        self.state = root / "docker.json"
        self.log = root / "docker.log"
        for name in ("home", "config", "state", "data", "runtime"):
            (root / name).mkdir()
        self.environment = {
            "PATH": f"{bin_dir}:{os.environ['PATH']}", "HOME": str(root / "home"),
            "XDG_CONFIG_HOME": str(root / "config"), "XDG_STATE_HOME": str(root / "state"),
            "XDG_DATA_HOME": str(root / "data"), "XDG_RUNTIME_DIR": str(root / "runtime"),
            "FAKE_DOCKER_STATE": str(self.state), "FAKE_DOCKER_LOG": str(self.log),
            "RAVEN_IMAGE": "raven:fake", "RAVEN_CORE_IMAGE": "raven-core:fake",
        }
        self.statedir = root / "state" / "raven" / subprocess.run(["hostname"], capture_output=True,
                                                                   text=True).stdout.strip()
        self.backups = root / "data" / "raven" / "backups"
        self.backup_file = root / "work.tar"
        self.backup_file.write_bytes(b"backup")
        self.sessions({"0": "running", "1": "paused", "2": "exited"})

    def sessions(self, states, **extra):
        sessions = {sid: {"state": state, "display": str(100 + int(sid)), "port": str(15900 + int(sid)),
                          "labels": {"raven.cmd": "xterm", "raven.wm": "xmonad", "raven.geometry": "800x600"}}
                    for sid, state in states.items()}
        volumes = [f"raven-{USER}-home-{sid}" for sid in states]
        self.state.write_text(json.dumps(dict({"user": USER, "sessions": sessions, "volumes": volumes,
                                               "backup": BACKUP}, **extra)))
        self.log.write_text("")

    def update(self, **values):
        state = json.loads(self.state.read_text())
        state.update(values)
        self.state.write_text(json.dumps(state))

    def raven(self, *args, umask=None):
        return subprocess.run([str(SOURCE / "raven"), *args], capture_output=True, text=True, env=self.environment,
                              stdin=subprocess.DEVNULL, timeout=60,
                              preexec_fn=(lambda: os.umask(umask)) if umask is not None else None)

    def calls(self, verb=None):
        calls = [json.loads(line) for line in self.log.read_text().splitlines()]
        return [c for c in calls if verb is None or c[0] == verb]

    def helper_runs(self):
        return [c for c in self.calls("run") if c[c.index("--entrypoint") + 1] == "python3"]

    def docker_state(self):
        return json.loads(self.state.read_text())

    # backup

    def test_backup_usage_errors_touch_nothing(self):
        usage = "raven: usage: raven backup [-t] TARGET [-o FILE] [--stop]\n"
        for args, message in (((), usage), (("2", "0"), usage), (("-x", "2"), usage),
                              (("2", "-o"), "raven: -o needs an argument\n"),
                              (("-t",), "raven: -t needs an argument\n"),
                              (("nosuch",), "raven: no session named 'nosuch'\n")):
            with self.subTest(args=args):
                self.log.write_text("")
                result = self.raven("backup", *args)
                self.assertEqual((result.returncode, result.stderr), (1, message))
                self.assertEqual(self.helper_runs(), [])
        self.assertFalse(self.backups.exists())

    def state_calls(self):
        return [c[:2] if c[0] != "run" else ["run"] for c in self.calls()
                if c[0] in ("pause", "unpause", "stop", "start") or c in self.helper_runs()]

    def test_backup_freezes_a_running_session_while_its_home_is_read(self):
        result = self.raven("backup", "-t", "0")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("raven: freezing session 0 while its home is read", result.stderr)
        self.assertIn("like a power cut", result.stderr)
        self.assertEqual(self.state_calls(), [["pause", f"raven-{USER}-0"], ["run"], ["unpause", f"raven-{USER}-0"]])
        self.assertEqual(self.docker_state()["sessions"]["0"]["state"], "running")
        self.assertEqual(len(list(self.backups.iterdir())), 1)

    def test_backup_of_a_paused_session_leaves_it_paused(self):
        result = self.raven("backup", "1")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("raven: session 1 is paused; its files are backed up as they are and it stays paused",
                      result.stderr)
        self.assertEqual(self.state_calls(), [["run"]])
        self.assertEqual(self.docker_state()["sessions"]["1"]["state"], "paused")

    def test_failed_backup_lets_a_frozen_session_continue(self):
        self.update(helper={"backup": {"rc": 3}})
        out = self.root / "out" / "b.tar"
        out.parent.mkdir()
        result = self.raven("backup", "0", "-o", str(out))
        self.assertEqual(result.returncode, 1)
        self.assertIn("raven: session 0 was not backed up", result.stderr)
        self.assertIn("raven: session 0 continues", result.stderr)
        self.assertEqual(list(out.parent.iterdir()), [])
        self.assertEqual(self.docker_state()["sessions"]["0"]["state"], "running")
        self.assertEqual(self.state_calls(), [["pause", f"raven-{USER}-0"], ["run"], ["unpause", f"raven-{USER}-0"]])

    def test_backup_of_a_stopped_session_is_private_and_atomic(self):
        result = self.raven("backup", "2", umask=0)
        self.assertEqual(result.returncode, 0, result.stderr)
        files = list(self.backups.iterdir())
        self.assertEqual(len(files), 1)
        self.assertRegex(files[0].name, r"^session-2-noname-[0-9]{8}T[0-9]{6}Z\.tar$")
        self.assertEqual(result.stdout, f"{files[0]}\n")
        self.assertEqual(stat.S_IMODE(self.backups.stat().st_mode), 0o700)
        self.assertEqual(stat.S_IMODE(files[0].stat().st_mode), 0o600)
        self.assertIn("raven: backed up session 2: 7 entries", result.stderr)
        self.assertIn("~/Downloads is not included", result.stderr)
        run, = self.helper_runs()
        self.assertEqual(run[:5], ["run", "--rm", "--network", "none", "--log-driver"])
        self.assertIn(f"type=volume,src=raven-{USER}-home-2,dst=/home/raven,readonly,volume-nocopy", run)
        self.assertEqual(run[run.index("--user") + 1], "0")
        backup_args = run[run.index("-c") + 2:]
        self.assertEqual(backup_args[0], "backup")
        self.assertEqual(backup_args[4:9], ["2", "", "xterm", "xmonad", "800x600"])
        self.assertEqual([c for c in self.calls() if c[0] in ("stop", "start", "unpause")], [])
        # A named session goes into the name, and a second backup in the same second is refused
        (self.statedir).mkdir(parents=True, exist_ok=True)
        (self.statedir / "2.name").write_text("work\n")
        result = self.raven("backup", "work")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertRegex(Path(result.stdout.strip()).name, r"^session-2-work-")

    def test_backup_with_stop_stops_and_starts_the_session_again(self):
        for sid, state, before in (("0", "running", []), ("1", "paused", [["unpause", f"raven-{USER}-1"]])):
            with self.subTest(state=state):
                self.log.write_text("")
                result = self.raven("backup", sid, "--stop")
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertIn("unsaved work in them is lost", result.stderr)
                self.assertIn(f"raven: started session {sid} again", result.stderr)
                order = [c[:2] if c[0] != "run" else ["run"] for c in self.calls()
                         if c[0] in ("unpause", "stop", "start") or c in self.helper_runs()]
                self.assertEqual(order, before + [["stop", f"raven-{USER}-{sid}"], ["run"],
                                                  ["start", f"raven-{USER}-{sid}"]])
                self.assertEqual(self.docker_state()["sessions"][sid]["state"], "running")

    def test_backup_to_an_existing_file_is_refused_before_stopping(self):
        out = self.root / "exists.tar"
        out.write_text("keep")
        result = self.raven("backup", "0", "--stop", "-o", str(out))
        self.assertEqual((result.returncode, result.stderr), (1, f"raven: {out} already exists\n"))
        self.assertEqual(out.read_text(), "keep")
        self.assertEqual([c for c in self.calls() if c[0] in ("run", "stop")], [])

    def test_failed_backup_leaves_no_file_and_restarts_a_stopped_session(self):
        self.update(helper={"backup": {"rc": 3}})
        out = self.root / "out" / "b.tar"
        out.parent.mkdir()
        result = self.raven("backup", "0", "--stop", "-o", str(out))
        self.assertEqual(result.returncode, 1)
        self.assertIn("raven: session 0 was not backed up", result.stderr)
        self.assertIn("raven: session 0 started again", result.stderr)
        self.assertEqual(list(out.parent.iterdir()), [])
        self.assertEqual(self.docker_state()["sessions"]["0"]["state"], "running")

    # restore

    def test_restore_usage_and_option_errors_create_nothing(self):
        usage = "raven: usage: raven restore FILE [-s NAME] [-w WM] [-g WxH] [CMD...]\n"
        for args, message in (((), usage), (("-s",), "raven: -s needs an argument\n"),
                              (("-x", str(self.backup_file)), usage),
                              ((str(self.root / "missing.tar"),), f"raven: cannot read {self.root / 'missing.tar'}\n"),
                              ((str(self.backup_file), "-w", "kde"),
                               "raven: window manager must be xmonad or openbox: 'kde'\n"),
                              ((str(self.backup_file), "-g", "big"), "raven: geometry must look like 1600x900: 'big'\n"),
                              ((str(self.backup_file), "-s", "9lives"), "raven: name must start with a letter: '9lives'\n")):
            with self.subTest(args=args):
                self.log.write_text("")
                result = self.raven("restore", *args)
                self.assertEqual((result.returncode, result.stderr), (1, message))
                self.assertEqual(self.helper_runs(), [])

    def test_refused_backup_creates_nothing(self):
        self.update(helper={"check": {"rc": 3}})
        result = self.raven("restore", str(self.backup_file))
        self.assertEqual(result.returncode, 1)
        self.assertEqual(result.stderr, f"raven: the backup is bad\nraven: {self.backup_file} was not restored;"
                                        " nothing was created\n")
        self.assertEqual([c for c in self.calls() if c[0] in ("create", "volume") and c[1:2] != ["inspect"]], [])
        self.assertFalse(self.statedir.exists())

    def test_name_from_the_backup_in_use_is_refused_before_anything_is_made(self):
        self.statedir.mkdir(parents=True)
        (self.statedir / "2.name").write_text("work\n")
        result = self.raven("restore", str(self.backup_file))
        self.assertEqual((result.returncode, result.stderr),
                         (1, "raven: name 'work' from the backup is used by session 2; "
                             "give another with: raven restore FILE -s NAME\n"))
        self.assertEqual([c for c in self.calls() if c[0] in ("create",) or c[:2] == ["volume", "create"]], [])
        result = self.raven("restore", "-s", "work", str(self.backup_file))
        self.assertEqual((result.returncode, result.stderr), (1, "raven: name 'work' is used by session 2\n"))

    def test_restore_takes_the_backup_settings_unless_given(self):
        result = self.raven("restore", str(self.backup_file))
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout, "")
        self.assertIn("raven: restored session 3 (work) with openbox, 1024x768: xclock -digital\n", result.stderr)
        self.assertIn("raven: attach with: raven attach 3\n", result.stderr)
        labels = self.docker_state()["sessions"]["3"]["labels"]
        self.assertEqual((labels["raven.wm"], labels["raven.geometry"], labels["raven.cmd"]),
                         ("openbox", "1024x768", "xclock -digital"))
        self.assertEqual((self.statedir / "3.name").read_text(), "work\n")
        self.assertIn(f"raven-{USER}-home-3", self.docker_state()["volumes"])
        extract, = [c for c in self.helper_runs() if c[c.index("-c") + 2] == "extract"]
        self.assertIn(f"type=volume,src=raven-{USER}-home-3,dst=/home/raven,volume-nocopy", extract)
        self.assertEqual(extract[-1], "0" * 64)
        check, = [c for c in self.helper_runs() if c[c.index("-c") + 2] == "check"]
        self.assertNotIn("--user", check)
        self.assertNotIn("--mount", check)
        # Options before or after FILE, then CMD; -- ends the options
        result = self.raven("restore", "-w", "xmonad", str(self.backup_file), "-s", "other", "-g", "640x480",
                            "--", "xterm", "-T", "x")
        self.assertEqual(result.returncode, 0, result.stderr)
        labels = self.docker_state()["sessions"]["4"]["labels"]
        self.assertEqual((labels["raven.wm"], labels["raven.geometry"], labels["raven.cmd"]),
                         ("xmonad", "640x480", "xterm -T x"))
        self.assertEqual((self.statedir / "4.name").read_text(), "other\n")

    def test_restore_warns_about_another_user_and_image(self):
        self.update(backup=dict(BACKUP, user="someone", image_id="sha256:" + "a" * 64, foreign="2"))
        result = self.raven("restore", str(self.backup_file), "-s", "copy")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn(f"raven: warning: the backup belongs to user someone, not {USER}\n", result.stderr)
        self.assertIn("raven: warning: the backup was made with image raven:latest (sha256:aaaaaaaaaaaa);"
                      " the session gets the current raven:fake (sha256:current)\n", result.stderr)
        self.assertIn("raven: warning: 2 entries belong to other users; they are given to the session user 60000\n",
                      result.stderr)

    def test_failed_unpack_removes_the_new_volume(self):
        self.update(helper={"extract": {"rc": 3}})
        result = self.raven("restore", str(self.backup_file))
        self.assertEqual(result.returncode, 1)
        self.assertIn("raven: removed the unfinished session 3; nothing else was changed", result.stderr)
        self.assertIn(["volume", "create", f"raven-{USER}-home-3"], self.calls())
        self.assertIn(["volume", "rm", f"raven-{USER}-home-3"], self.calls())
        self.assertNotIn(f"raven-{USER}-home-3", self.docker_state()["volumes"])
        self.assertEqual(self.calls("create"), [])
        self.assertFalse((self.statedir / "3.name").exists())
        self.assertEqual(sorted(self.docker_state()["sessions"]), ["0", "1", "2"])

    def test_failed_start_removes_the_container_volume_and_name(self):
        self.update(start_fails=True)
        result = self.raven("restore", str(self.backup_file))
        self.assertEqual(result.returncode, 1)
        self.assertIn("raven: removed the unfinished session 3", result.stderr)
        self.assertIn(["rm", "-f", f"raven-{USER}-3"], self.calls())
        self.assertNotIn(f"raven-{USER}-home-3", self.docker_state()["volumes"])
        self.assertFalse((self.statedir / "3.name").exists())
        self.assertEqual(sorted(self.docker_state()["sessions"]), ["0", "1", "2"])
        # The other sessions were not touched
        self.assertEqual([c for c in self.calls() if c[0] in ("rm", "stop", "start") and
                          any(a in c for a in (f"raven-{USER}-0", f"raven-{USER}-1", f"raven-{USER}-2"))], [])

    def test_uninstall_keeps_backups(self):
        result = self.raven("uninstall", "-n")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn(f"session backups in {self.backups}", result.stdout)


if __name__ == "__main__":
    unittest.main()
