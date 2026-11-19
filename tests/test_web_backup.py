"""raven web backup and restore: passphrase handling, files, refusals, confirmation and helper wiring, with a
recorded fake Docker CLI."""

from argparse import Namespace
import base64
from contextlib import redirect_stderr, redirect_stdout
from datetime import datetime, timedelta, timezone
import fcntl
import io
import json
import os
from pathlib import Path
import pwd
import runpy
import stat
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch

from raven_web_host import RequestError

manager = runpy.run_path(str(Path(__file__).resolve().parents[1] / "raven-web"))
GLOBALS = manager["backup"].__globals__
NAME, VOLUME, INSTANCE = GLOBALS["NAME"], GLOBALS["VOLUME"], GLOBALS["INSTANCE"]
HOST_USER = pwd.getpwuid(os.getuid()).pw_name
OWNED = {"raven.web.uid": str(os.getuid())}
PASSPHRASE = "tidy lantern 4 orbit"
ENCRYPTED = b'RAVENWEB1\n{"kdf": "scrypt"}\n\x00\x01ciphertext'
FINGERPRINT = ":".join(["AB"] * 32)
OTHER_FINGERPRINT = ":".join(["CD"] * 32)
NOW = datetime.now(timezone.utc)
UNDECRYPTABLE = "Cannot decrypt the backup: the passphrase is wrong or the file is damaged"
SUMMARY = {"created": "2026-11-19T01:15:00+00:00", "user": HOST_USER, "instance": INSTANCE, "account": "owner",
           "origin": "https://localhost:9443", "fingerprint": FINGERPRINT, "names": ["localhost"],
           "not_before": (NOW - timedelta(days=1)).isoformat(), "not_after": (NOW + timedelta(days=200)).isoformat(),
           "key_type": "RSA 2048", "listen": "127.0.0.1", "port": 18443, "ssh_target": "owner@example.test",
           "ssh_port": 2222}
SAVED = "backups/pre-restore-20261119T101600Z"


def gateway(running):
    return {"Id": "a" * 64, "Mounts": [], "State": {"Running": running, "Restarting": False},
            "Config": {"Env": [], "Labels": OWNED},
            "HostConfig": {"PortBindings": {"8443/tcp": [{"HostIp": "127.0.0.1", "HostPort": "18443"}]}}}


class FakeDocker:
    """Answer the docker and systemctl calls of backup and restore; record them with their stdin."""

    def __init__(self, volume=True, container=None, unit="inactive", lock=None, **helpers):
        self.volume = volume
        self.container = container
        self.unit = unit
        self.lock = lock
        self.calls, self.inputs, self.locked = [], [], []
        self.helpers = {
            "backup": {"data": base64.b64encode(ENCRYPTED).decode(), "warnings": [],
                       "summary": {**SUMMARY, "origin": "https://localhost:8443"}},
            "restore-check": {"backup": SUMMARY, "current": {"origin": "https://127.0.0.1:9443",
                                                             "fingerprint": OTHER_FINGERPRINT}, "warnings": []},
            "restore": {"backup": SUMMARY, "warnings": [], "saved": SAVED},
            **helpers}

    def __call__(self, argv, **kwargs):
        if argv[0] == "systemctl":
            return subprocess.CompletedProcess(argv, 0, f"LoadState=loaded\nActiveState={self.unit}\n", "")
        if argv[0] != "docker":
            return subprocess.CompletedProcess(argv, 1, "", "unexpected command")
        self.calls.append(argv)
        self.inputs.append(kwargs.get("input"))
        if argv[1:3] == ["volume", "inspect"]:
            if not self.volume:
                return subprocess.CompletedProcess(argv, 1, "", "Error response from daemon: no such volume")
            return subprocess.CompletedProcess(argv, 0, json.dumps([{"Labels": OWNED}]), "")
        if argv[1:3] == ["volume", "create"]:
            self.volume = True
            return subprocess.CompletedProcess(argv, 0, VOLUME + "\n", "")
        if argv[1:3] == ["volume", "rm"]:
            self.volume = False
            return subprocess.CompletedProcess(argv, 0, VOLUME + "\n", "")
        if argv[1:3] == ["container", "inspect"]:
            if not self.container:
                return subprocess.CompletedProcess(argv, 1, "", "Error: No such container: x")
            return subprocess.CompletedProcess(argv, 0, json.dumps([self.container]), "")
        if argv[1] == "run":
            if "init-data" in argv:
                return subprocess.CompletedProcess(argv, 0, "", "")
            action = argv[-1]
            if self.lock and action in ("backup", "restore"):
                with open(self.lock) as lock:
                    try:
                        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    except BlockingIOError:
                        self.locked.append(action)
            response = self.helpers[action]
            if isinstance(response, str):
                raise subprocess.CalledProcessError(1, argv, "", response)
            return subprocess.CompletedProcess(argv, 0, json.dumps(response), "")
        raise AssertionError(f"unexpected docker call {argv}")

    def actions(self):
        names = []
        for argv in self.calls:
            if argv[1] == "run":
                names.append("init-data" if "init-data" in argv else argv[-1])
            else:
                names.append(" ".join(argv[1:3]))
        return names

    def run(self, action):
        return self.calls[self.actions().index(action)], self.inputs[self.actions().index(action)]


class BackupCommandTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="raven-backup-cli-")
        self.addCleanup(temporary.cleanup)
        self.scratch = Path(temporary.name)
        runtime = self.scratch / "runtime"
        self.data_home = self.scratch / "data"
        patcher = patch.dict(GLOBALS, {"RUNTIME": runtime, "ADDRESS": runtime / "broker.sock"})
        patcher.start()
        self.addCleanup(patcher.stop)
        environment = patch.dict(os.environ, {"XDG_DATA_HOME": str(self.data_home), "HOME": str(self.scratch)})
        environment.start()
        self.addCleanup(environment.stop)
        self.lock = runtime / "manager.lock"
        self.secret = self.passphrase_file(PASSPHRASE + "\n")

    def passphrase_file(self, text, mode=0o600, name="passphrase"):
        path = self.scratch / name
        path.write_bytes(text.encode() if isinstance(text, str) else text)
        path.chmod(mode)
        return path

    def run_command(self, function, docker, args, tty=False, answers=("yes",), secrets=()):
        stdout, stderr = io.StringIO(), io.StringIO()
        self.prompts, self.hidden = [], []
        answers, secrets = list(answers), list(secrets)

        def ask(prompt):
            self.prompts.append(prompt)
            return answers.pop(0)

        def hidden(prompt):
            self.hidden.append(prompt)
            return secrets.pop(0)

        stdin = Mock(isatty=Mock(return_value=tty))
        error = None
        with patch("subprocess.run", side_effect=docker), patch("sys.stdin", stdin), \
                patch("builtins.input", side_effect=ask), patch("getpass.getpass", side_effect=hidden), \
                redirect_stdout(stdout), redirect_stderr(stderr):
            try:
                function(args)
            except RequestError as caught:
                error = caught
        self.output = stdout.getvalue() + stderr.getvalue()
        return error, stdout.getvalue(), stderr.getvalue()

    def backup(self, docker, output=None, passphrase_file="default", **options):
        file = self.secret if passphrase_file == "default" else passphrase_file
        return self.run_command(manager["backup"], docker, Namespace(output=output, passphrase_file=file), **options)

    def restore(self, docker, path=None, yes=False, passphrase_file="default", **options):
        if path is None:
            path = self.scratch / "web.ravenweb"
            if not path.exists():
                path.write_bytes(ENCRYPTED)
        file = self.secret if passphrase_file == "default" else passphrase_file
        return self.run_command(manager["restore"], docker, Namespace(file=path, yes=yes, passphrase_file=file),
                                **options)

    def assert_secret_kept(self, docker):
        for argv in docker.calls:
            self.assertFalse(any(PASSPHRASE in part for part in argv), argv)
        self.assertNotIn(PASSPHRASE, self.output)

    def test_backup_writes_the_encrypted_file_privately_under_the_data_home(self):
        docker = FakeDocker(lock=self.lock)
        error, stdout, stderr = self.backup(docker)
        self.assertIsNone(error, stderr)
        self.assertEqual(docker.actions(), ["volume inspect", "backup"])
        self.assertEqual(docker.locked, ["backup"], "the backup must run under the manager lock")
        argv, payload = docker.run("backup")
        self.assertEqual(json.loads(payload), {"passphrase": PASSPHRASE, "user": HOST_USER, "instance": INSTANCE})
        self.assertIn(f"type=volume,src={VOLUME},dst=/data,readonly", argv)
        self.assertEqual(argv[argv.index("--network") + 1], "none")
        self.assert_secret_kept(docker)
        directory = self.data_home / "raven/backups"
        files = list(directory.iterdir())
        self.assertEqual(len(files), 1, files)
        self.assertRegex(files[0].name, rf"^web-{INSTANCE}-\d{{8}}T\d{{6}}Z\.ravenweb$")
        self.assertEqual(files[0].read_bytes(), ENCRYPTED)
        self.assertEqual(stat.S_IMODE(files[0].stat().st_mode), 0o600)
        self.assertEqual(stat.S_IMODE(directory.stat().st_mode), 0o700)
        self.assertIn(f"Web settings backup of {VOLUME}\n", stdout)
        self.assertIn(f"  File      {files[0]}\n", stdout)
        self.assertIn(f"  Size      {len(ENCRYPTED)} bytes, mode 0600, encrypted\n", stdout)
        self.assertIn(f"  SHA-256   {FINGERPRINT}\n", stdout)
        self.assertIn("Browser logins are not included.", stdout)
        self.assertIn(f"Restore: raven web stop, then raven web restore {files[0]}", stdout)
        error, _, _ = self.backup(FakeDocker())
        self.assertIsNone(error)
        names = sorted(path.name for path in directory.iterdir())
        self.assertEqual(len(names), 2, "a second backup within a second must not replace the first")
        self.assertRegex(names[-1], rf"^web-{INSTANCE}-\d{{8}}T\d{{6}}Z(-2)?\.ravenweb$")

    def test_backup_to_a_chosen_file_never_replaces_one(self):
        target = self.scratch / "chosen.ravenweb"
        error, _, _ = self.backup(FakeDocker(), output=target)
        self.assertIsNone(error)
        self.assertEqual((target.read_bytes(), stat.S_IMODE(target.stat().st_mode)), (ENCRYPTED, 0o600))
        docker = FakeDocker()
        error, _, _ = self.backup(docker, output=target)
        self.assertIn("already exists; choose another -o FILE", str(error))
        self.assertEqual(docker.calls, [])
        self.assertEqual([path.name for path in self.scratch.iterdir() if path.name.endswith(".tmp")], [])

    def test_passphrase_file_must_be_private_and_hold_one_long_line(self):
        cases = ((self.passphrase_file(PASSPHRASE, 0o644, "shared"), "has mode 0644; other users must not read it"),
                 (self.passphrase_file(PASSPHRASE, 0o640, "group"), "has mode 0640"),
                 (self.passphrase_file("eleven char\n", name="short"), "at least 12 characters"),
                 (self.passphrase_file(PASSPHRASE + "\nsecond line\n", name="lines"), "on a single line"),
                 (self.passphrase_file(b"\xff" * 20, name="binary"), "is not UTF-8 text"),
                 (self.scratch / "missing", "Cannot read --passphrase-file"),
                 (self.scratch, "is not a regular file"))
        for path, message in cases:
            with self.subTest(message):
                docker = FakeDocker()
                error, _, _ = self.backup(docker, passphrase_file=path)
                self.assertIn(message, str(error))
                self.assertNotIn(PASSPHRASE, str(error))
                self.assertNotIn("backup", docker.actions())
        self.assertFalse((self.data_home / "raven/backups").exists() and
                         any((self.data_home / "raven/backups").iterdir()))
        crlf = self.passphrase_file(PASSPHRASE + "\r\n", name="crlf")
        docker = FakeDocker()
        error, _, _ = self.backup(docker, passphrase_file=crlf)
        self.assertIsNone(error)
        self.assertEqual(json.loads(docker.run("backup")[1])["passphrase"], PASSPHRASE)

    def test_terminal_asks_twice_for_a_backup(self):
        docker = FakeDocker()
        error, _, _ = self.backup(docker, passphrase_file=None, tty=True, secrets=[PASSPHRASE, PASSPHRASE])
        self.assertIsNone(error)
        self.assertEqual(self.hidden, ["Passphrase for the new backup: ", "Repeat the passphrase: "])
        self.assertEqual(json.loads(docker.run("backup")[1])["passphrase"], PASSPHRASE)
        self.assert_secret_kept(docker)
        docker = FakeDocker()
        error, _, _ = self.backup(docker, passphrase_file=None, tty=True, secrets=[PASSPHRASE, PASSPHRASE + "x"])
        self.assertEqual(str(error), "The passphrases do not match; nothing was written")
        self.assertNotIn("backup", docker.actions())
        error, _, _ = self.backup(FakeDocker(), passphrase_file=None, tty=True, secrets=["too short"])
        self.assertEqual(str(error), "Choose a passphrase of at least 12 characters")
        self.assertEqual(self.hidden, ["Passphrase for the new backup: "], "a short passphrase is refused at once")

    def test_backup_without_a_terminal_needs_a_passphrase_file(self):
        docker = FakeDocker()
        error, _, _ = self.backup(docker, passphrase_file=None)
        self.assertIn("run it in a terminal, or give --passphrase-file FILE", str(error))
        self.assertNotIn("backup", docker.actions())

    def test_backup_needs_an_initialized_install(self):
        docker = FakeDocker(volume=False)
        error, _, _ = self.backup(docker)
        self.assertEqual(str(error), "Run raven web init --username NAME first")
        self.assertEqual(docker.actions(), ["volume inspect"])

    def test_restore_refuses_while_the_gateway_or_its_unit_runs(self):
        for docker, message in ((FakeDocker(container=gateway(True)), "The web service is running"),
                                (FakeDocker(container=gateway(False), unit="active"),
                                 f"raven-{INSTANCE}.service is running")):
            with self.subTest(message):
                error, _, _ = self.restore(docker, yes=True)
                self.assertIn(message, str(error))
                self.assertIn("before restoring: raven web stop", str(error))
                self.assertFalse(any(argv[1] == "run" for argv in docker.calls), docker.actions())

    def test_restore_without_a_terminal_needs_yes_before_anything_else(self):
        docker = FakeDocker()
        error, _, _ = self.restore(docker)
        self.assertEqual(str(error), "Restoring needs confirmation: run it in a terminal, or add -y")
        self.assertEqual(docker.calls, [])

    def test_restore_shows_the_scope_asks_once_and_runs_under_the_lock(self):
        docker = FakeDocker(container=gateway(False), lock=self.lock)
        error, stdout, stderr = self.restore(docker, passphrase_file=None, tty=True, secrets=[PASSPHRASE])
        self.assertIsNone(error, stderr)
        self.assertEqual(self.hidden, ["Backup passphrase: "])
        self.assertEqual(self.prompts, ["Type yes to restore: "])
        self.assertEqual(docker.actions(), ["container inspect", "volume inspect", "restore-check",
                                            "container inspect", "volume inspect", "restore"])
        self.assertEqual(docker.locked, ["restore"])
        check, check_input = docker.run("restore-check")
        apply, apply_input = docker.run("restore")
        self.assertIn(f"type=volume,src={VOLUME},dst=/data,readonly", check)
        self.assertIn(f"type=volume,src={VOLUME},dst=/data", apply)
        self.assertEqual(json.loads(check_input), {"passphrase": PASSPHRASE, "user": HOST_USER, "instance": INSTANCE,
                                                   "backup": base64.b64encode(ENCRYPTED).decode()})
        self.assertEqual(apply_input, check_input)
        self.assert_secret_kept(docker)
        flat = " ".join(stdout.split())
        self.assertIn(f"Restore the web settings of {NAME} from ", stdout)
        self.assertIn(f"  Backup    {manager['local_time'](SUMMARY['created'])}, host user {HOST_USER}, instance "
                      f"{INSTANCE}\n", stdout)
        self.assertIn("  Account   owner (the password it had then)\n", stdout)
        self.assertIn("  Browser   https://localhost:9443 (now https://127.0.0.1:9443)\n", stdout)
        self.assertIn("  Listen    127.0.0.1:18443\n", stdout)
        self.assertIn("  SSH       owner@example.test, port 2222\n", stdout)
        self.assertIn(f"  SHA-256   {FINGERPRINT}\n", stdout)
        self.assertIn("(199 days left)", stdout)
        for text in ("replaces the account and its password, the HTTPS certificate and private key, secret.key, and "
                     "the saved settings (browser origin, listen address and port, SSH target)",
                     "every login ends, and cookies from before the backup stay refused",
                     f"The current files stay in {VOLUME}:/data/backups/pre-restore-<UTC time>/.",
                     "The certificate differs from the current one: register it on each PC again",
                     f"Previous files saved: {VOLUME}:/data/{SAVED}",
                     "every browser login has ended", "Next: raven web start",
                     "Then register the restored certificate on each PC"):
            self.assertIn(text, flat)
        self.assertLess(stdout.index("SHA-256"), stdout.index("Previous files saved"))

    def test_declined_restore_changes_nothing(self):
        for answer in ("no", "", "y"):
            with self.subTest(answer=answer):
                docker = FakeDocker()
                error, _, _ = self.restore(docker, tty=True, answers=[answer])
                self.assertEqual(str(error), "Cancelled; nothing was restored")
                self.assertNotIn("restore", docker.actions())

    def test_same_certificate_needs_no_new_trust(self):
        docker = FakeDocker(**{"restore-check": {"backup": SUMMARY, "warnings": [],
                                                 "current": {"origin": SUMMARY["origin"],
                                                             "fingerprint": FINGERPRINT}}})
        error, stdout, _ = self.restore(docker, yes=True)
        self.assertIsNone(error)
        flat = " ".join(stdout.split())
        self.assertIn("The certificate is the one in use now; PCs that trust it need nothing new.", flat)
        self.assertNotIn("Then register", flat)
        self.assertIn("  Browser   https://localhost:9443\n", stdout)

    def test_missing_volume_is_created_and_removed_again_when_the_restore_fails(self):
        docker = FakeDocker(volume=False, restore="Raven configuration: Restoring failed (No space left on device); "
                                                  "the previous files are back in place")
        error, stdout, stderr = self.restore(docker, yes=True)
        self.assertEqual(str(error), "Restoring failed (No space left on device); the previous files are back in place")
        self.assertEqual(docker.actions(), ["container inspect", "volume inspect", "restore-check", "container inspect",
                                            "volume inspect", "volume create", "init-data", "restore", "volume rm"])
        check, _ = docker.run("restore-check")
        self.assertFalse(any("type=volume" in part for part in check), "the check must not create the volume")
        self.assertIn(f"The data volume {VOLUME} does not exist; it is created first", " ".join(stdout.split()))
        self.assertIn(f"removed the data volume {VOLUME} created for this restore", stderr)
        create = docker.calls[docker.actions().index("volume create")]
        self.assertEqual(create[-3:], ["--label", f"raven.web.uid={os.getuid()}", VOLUME])

        docker = FakeDocker(volume=False, restore={"backup": SUMMARY, "warnings": [], "saved": None})
        error, stdout, _ = self.restore(docker, yes=True)
        self.assertIsNone(error)
        self.assertEqual(docker.actions()[-3:], ["volume create", "init-data", "restore"])
        self.assertIn(f"Created the data volume {VOLUME}, as raven web init does", stdout)
        self.assertNotIn("Previous files saved", stdout)

    def test_helper_refusals_read_as_their_reason(self):
        for reason in (UNDECRYPTABLE, "The backup is from another installation (host user alice, instance web), not "
                                      f"from host user {HOST_USER}, instance {INSTANCE}; restore it there"):
            with self.subTest(reason):
                docker = FakeDocker(**{"restore-check": "Raven configuration: " + reason + "\n"})
                error, stdout, _ = self.restore(docker, yes=True)
                self.assertEqual(str(error), reason)
                self.assertNotIn("restore", docker.actions())
                self.assertEqual(stdout, "")
                self.assert_secret_kept(docker)

    def test_warnings_are_shown_before_the_confirmation(self):
        warning = "The certificate expired on 2026-01-01 00:00 UTC; run raven web certificate renew before raven web start"
        docker = FakeDocker(**{"restore-check": {"backup": SUMMARY, "current": None, "warnings": [warning]}})
        error, _, stderr = self.restore(docker, tty=True, answers=["no"])
        self.assertEqual(stderr, f"raven web: warning: {warning}\n")

    def test_restore_file_problems_are_named(self):
        large = self.scratch / "large.ravenweb"
        with open(large, "wb") as output:
            output.truncate(GLOBALS["MAX_BACKUP_FILE"] + 1)
        for path, message in ((self.scratch / "missing.ravenweb", "Cannot read"), (self.scratch, "not a regular file"),
                              (large, "is larger than 24 MiB")):
            with self.subTest(message):
                docker = FakeDocker()
                error, _, _ = self.restore(docker, path=path, yes=True)
                self.assertIn(message, str(error))
                self.assertFalse(any(argv[1] == "run" for argv in docker.calls))

    def test_arguments(self):
        cases = ((["backup"], {"action": "backup", "output": None, "passphrase_file": None}),
                 (["backup", "-o", "x.ravenweb", "--passphrase-file", "p"],
                  {"output": Path("x.ravenweb"), "passphrase_file": Path("p")}),
                 (["restore", "x.ravenweb"], {"action": "restore", "file": Path("x.ravenweb"), "yes": False,
                                              "passphrase_file": None}),
                 (["restore", "x.ravenweb", "-y", "--passphrase-file", "p"],
                  {"file": Path("x.ravenweb"), "yes": True, "passphrase_file": Path("p")}))
        for argv, expected in cases:
            with self.subTest(argv=argv), patch.object(sys, "argv", ["raven-web", *argv]):
                args = manager["arguments"]()
                self.assertEqual({key: getattr(args, key) for key in expected}, expected)


if __name__ == "__main__":
    unittest.main()
