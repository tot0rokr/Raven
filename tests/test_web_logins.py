"""raven web logins: listing and revoking browser logins through a helper container, with a recorded fake Docker."""

from contextlib import redirect_stderr, redirect_stdout
import fcntl
import io
import json
import os
from pathlib import Path
import runpy
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from raven_web_host import RequestError

manager = runpy.run_path(str(Path(__file__).resolve().parents[1] / "raven-web"))
GLOBALS = manager["main"].__globals__
VOLUME = GLOBALS["VOLUME"]
IMAGE = GLOBALS["IMAGE"]
OWNED = {"raven.web.uid": str(os.getuid())}
CHROME = {"id": "0123456789abcdef", "browser": "Chrome", "os": "Windows", "device": "desktop",
          "created": "2026-11-18T01:00:00+00:00", "seen": "2026-11-18T02:30:00+00:00",
          "expires": "2026-11-18T09:00:00+00:00"}
PHONE = {"id": "fedcba9876543210", "browser": None, "os": "Android", "device": "mobile",
         "created": "2026-11-18T00:10:00+00:00", "seen": "2026-11-18T00:10:00+00:00",
         "expires": "2026-11-18T08:10:00+00:00"}
LEGACY = {"id": "00112233aabbccdd", "browser": None, "os": None, "device": None, "created": None, "seen": None,
          "expires": "2026-11-18T07:00:00+00:00"}


class FakeDocker:
    """Answer the docker calls of raven web logins and record them; manage.py's reply comes from reply."""

    def __init__(self, reply=None, failure=None, lock=None):
        self.reply = reply
        self.failure = failure
        self.lock = lock
        self.calls = []
        self.locked = []

    def __call__(self, argv, **kwargs):
        self.calls.append(argv)
        if argv[1:3] == ["volume", "inspect"]:
            return subprocess.CompletedProcess(argv, 0, json.dumps([{"Labels": OWNED}]), "")
        if argv[1:3] == ["container", "inspect"]:
            return subprocess.CompletedProcess(argv, 1, "", "Error: No such container: x")
        if argv[1] == "run":
            if self.lock:
                with open(self.lock) as lock:
                    try:
                        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    except BlockingIOError:
                        self.locked.append(argv[-1])
            if self.failure:
                raise subprocess.CalledProcessError(1, argv, "", self.failure)
            return subprocess.CompletedProcess(argv, 0, json.dumps(self.reply), "")
        raise AssertionError(f"unexpected docker call {argv}")

    def runs(self):
        return [argv for argv in self.calls if argv[1] == "run"]


class LoginsCommandTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="raven-logins-cli-")
        self.addCleanup(temporary.cleanup)
        runtime = Path(temporary.name) / "runtime"
        patcher = patch.dict(GLOBALS, {"RUNTIME": runtime, "ADDRESS": runtime / "broker.sock"})
        patcher.start()
        self.addCleanup(patcher.stop)
        self.lock = runtime / "manager.lock"

    def raven_web(self, docker, *arguments):
        stdout, stderr = io.StringIO(), io.StringIO()
        error = None
        with patch("subprocess.run", side_effect=docker), patch("sys.argv", ["raven-web", *arguments]), \
                redirect_stdout(stdout), redirect_stderr(stderr):
            try:
                manager["main"]()
            except RequestError as caught:
                error = caught
        return error, stdout.getvalue()

    def assert_helper(self, argv, writable, *command):
        """A networkless, unprivileged helper on the data volume that runs manage.py logins."""
        self.assertEqual(argv[:7], ["docker", "run", "--rm", "--pull", "never", "--network", "none"])
        mount = f"type=volume,src={VOLUME},dst=/data"
        self.assertIn(mount if writable else mount + ",readonly", argv)
        for option in ("--read-only", "--cap-drop", "no-new-privileges"):
            self.assertIn(option, argv)
        self.assertEqual(argv[argv.index(IMAGE):], [IMAGE, "python", "manage.py", "logins", *command])

    def test_list_reads_the_volume_and_prints_a_table_in_local_time(self):
        docker = FakeDocker({"logins": [CHROME, PHONE, LEGACY]})
        error, stdout = self.raven_web(docker, "logins")
        self.assertIsNone(error)
        [run] = docker.runs()
        self.assert_helper(run, False, "list")
        lines = stdout.splitlines()
        self.assertEqual(lines[0].split(), ["ID", "BROWSER", "DEVICE", "LOGGED", "IN", "LAST", "SEEN", "EXPIRES"])
        local = manager["local_time"]
        self.assertEqual(lines[1].split("  ")[0], CHROME["id"])
        self.assertIn("Chrome on Windows", lines[1])
        self.assertIn("desktop", lines[1])
        for key in ("created", "seen", "expires"):
            self.assertIn(local(CHROME[key]), lines[1])
        self.assertIn("unknown browser on Android", lines[2])
        self.assertIn("mobile", lines[2])
        legacy = lines[3].split()
        self.assertEqual(legacy[:5], [LEGACY["id"], "unknown", "-", "-", "-"])
        self.assertEqual(" ".join(legacy[5:]), local(LEGACY["expires"]))
        columns = [line.index(header) for line, header in ((lines[0], "BROWSER"), (lines[1], "Chrome"),
                                                           (lines[2], "unknown"), (lines[3], "unknown"))]
        self.assertEqual(len(set(columns)), 1, "the browser column is aligned")

    def test_list_without_logins_says_so(self):
        error, stdout = self.raven_web(FakeDocker({"logins": []}), "logins")
        self.assertIsNone(error)
        self.assertEqual(stdout, "No browser logins.\n")

    def test_revoking_one_login_mounts_the_volume_writable_under_the_manager_lock(self):
        docker = FakeDocker({"revoked": 1}, lock=self.lock)
        error, stdout = self.raven_web(docker, "logins", "revoke", CHROME["id"])
        self.assertIsNone(error)
        [run] = docker.runs()
        self.assert_helper(run, True, "revoke", CHROME["id"])
        self.assertEqual(docker.locked, [CHROME["id"]])
        self.assertIn("Revoked 1 login.", stdout)

    def test_revoking_all_logins(self):
        docker = FakeDocker({"revoked": 3})
        error, stdout = self.raven_web(docker, "logins", "revoke", "--all")
        self.assertIsNone(error)
        [run] = docker.runs()
        self.assert_helper(run, True, "revoke", "--all")
        self.assertIn("Revoked 3 logins.", stdout)

    def test_invalid_revocations_are_refused_before_docker(self):
        cases = ((("logins", "revoke"), "Give one login ID or --all"),
                 (("logins", "revoke", CHROME["id"], "--all"), "Give one login ID or --all"),
                 (("logins", "revoke", "0123456789ABCDEF"), "16 hexadecimal characters"),
                 (("logins", "revoke", "abc"), "16 hexadecimal characters"))
        for arguments, message in cases:
            with self.subTest(arguments=arguments):
                docker = FakeDocker({"revoked": 1})
                error, _ = self.raven_web(docker, *arguments)
                self.assertIn(message, str(error))
                self.assertEqual(docker.runs(), [])

    def test_helper_failures_read_as_the_reason(self):
        docker = FakeDocker(failure="CommandError: No login has ID 0123456789abcdef; raven web logins lists them\n")
        error, _ = self.raven_web(docker, "logins", "revoke", CHROME["id"])
        self.assertEqual(str(error), "No login has ID 0123456789abcdef; raven web logins lists them")
        docker = FakeDocker(failure="Unknown command: 'logins'\nType 'manage.py help' for usage.\n")
        error, _ = self.raven_web(docker, "logins")
        self.assertEqual(str(error), f"{IMAGE} has no login management; run raven web build, then raven web stop "
                                     "and raven web start")

    def test_password_change_still_runs_in_a_writable_helper(self):
        docker = FakeDocker()
        error, _ = self.raven_web(docker, "password", "--username", "owner")
        self.assertIsNone(error)
        [run] = docker.runs()
        self.assertIn(f"type=volume,src={VOLUME},dst=/data", run)
        self.assertEqual(run[-5:], ["python", "manage.py", "change-password", "--username", "owner"])


if __name__ == "__main__":
    unittest.main()
