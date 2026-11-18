"""raven web certificate: confirmation, apply, restart, rollback and output, with a recorded fake Docker CLI."""

from argparse import Namespace
from contextlib import redirect_stderr, redirect_stdout
from datetime import datetime, timedelta, timezone
import fcntl
import io
import json
import os
from pathlib import Path
import runpy
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch

from raven_web_host import RequestError

manager = runpy.run_path(str(Path(__file__).resolve().parents[1] / "raven-web"))
GLOBALS = manager["change_certificate"].__globals__
NAME = GLOBALS["NAME"]
OWNED = {"raven.web.uid": str(os.getuid())}
NOW = datetime.now(timezone.utc)
OLD = "-----BEGIN CERTIFICATE-----\nT0xE\n-----END CERTIFICATE-----\n"
NEW = "-----BEGIN CERTIFICATE-----\nTkVX\n-----END CERTIFICATE-----\n"
KEY_LINE = "MIGHAgEAMBMGByqGSM49AgEGCCqGSM49AwEHBG0wawIBAQQgSecretKeyBytes0123"
KEY = f"-----BEGIN PRIVATE KEY-----\n{KEY_LINE}\n-----END PRIVATE KEY-----\n"
FINGERPRINT = ":".join(["AB"] * 32)
BACKUP = "backups/tls-20261118T101500Z"


def gateway(running=True, origin="https://localhost:9443", port=18443):
    return {"Id": "a" * 64, "Image": "sha256:" + "b" * 64, "Mounts": [],
            "State": {"Running": running, "Restarting": False, "Status": "running" if running else "exited",
                      "StartedAt": "2026-11-18T00:00:00Z", "ExitCode": 0},
            "Config": {"Env": [f"RAVEN_WEB_ORIGIN={origin}"], "Labels": OWNED, "Image": "raven-web:test"},
            "HostConfig": {"PortBindings": {"8443/tcp": [{"HostIp": "127.0.0.1", "HostPort": str(port)}]}}}


def summary(days=365, names=("localhost",)):
    return {"subject": "CN=localhost", "san": list(names), "fingerprint": FINGERPRINT, "chain": 1,
            "self_signed": True, "key_type": "RSA 2048",
            "not_before": (NOW - timedelta(minutes=5)).isoformat(),
            "not_after": (NOW + timedelta(days=days, hours=1)).isoformat()}


class FakeDocker:
    """Answer docker calls from the raven-web certificate flow and record them with their stdin."""

    def __init__(self, container=None, lock=None, **helpers):
        self.container = gateway() if container is None else container
        self.lock = lock
        self.calls = []
        self.inputs = []
        self.locked = []
        self.helpers = {
            "certificate-check": {"mode": "renew", "info": summary(), "warnings": [], "config": {}},
            "certificate-apply": {"mode": "renew", "info": summary(), "warnings": [],
                                  "config": {"origin": "https://localhost:9443"}, "certificate": NEW,
                                  "previous": OLD, "backup": BACKUP, "complete_backup": True},
            "certificate-restore": {"certificate": OLD, "restored": BACKUP},
            **helpers}

    def __call__(self, argv, **kwargs):
        if argv[0] != "docker":
            return subprocess.CompletedProcess(argv, 1, "", "unexpected command")
        self.calls.append(argv)
        self.inputs.append(kwargs.get("input"))
        if argv[1:3] == ["volume", "inspect"]:
            return subprocess.CompletedProcess(argv, 0, json.dumps([{"Labels": OWNED}]), "")
        if argv[1:3] == ["container", "inspect"]:
            if not self.container:
                return subprocess.CompletedProcess(argv, 1, "", "Error: No such container: x")
            return subprocess.CompletedProcess(argv, 0, json.dumps([self.container]), "")
        if argv[1:3] == ["container", "restart"]:
            return subprocess.CompletedProcess(argv, 0, NAME + "\n", "")
        if argv[1] == "run":
            action = argv[-1]
            if action == "certificate-apply" and self.lock:
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
        return [argv[-1] if argv[1] == "run" else " ".join(argv[1:3]) for argv in self.calls]


class CertificateFlowTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="raven-certificate-cli-")
        self.addCleanup(temporary.cleanup)
        self.scratch = Path(temporary.name)
        runtime = self.scratch / "runtime"
        self.wait = Mock(return_value=None)
        patcher = patch.dict(GLOBALS, {"RUNTIME": runtime, "ADDRESS": runtime / "broker.sock",
                                       "wait_for_https": self.wait})
        patcher.start()
        self.addCleanup(patcher.stop)
        self.lock = runtime / "manager.lock"

    def run_flow(self, docker, args, tty=False, answer="yes"):
        stdout, stderr = io.StringIO(), io.StringIO()
        prompts = []

        def ask(prompt):
            prompts.append(prompt)
            return answer

        stdin = Mock(isatty=Mock(return_value=tty))
        result = error = None
        with patch("subprocess.run", side_effect=docker), patch("sys.stdin", stdin), \
                patch("builtins.input", side_effect=ask), redirect_stdout(stdout), redirect_stderr(stderr):
            try:
                result = manager["change_certificate"](args)
            except RequestError as caught:
                error = caught
        self.prompts = prompts
        return result, error, stdout.getvalue(), stderr.getvalue()

    @staticmethod
    def renew(yes=False, names=(), days=365):
        return Namespace(certificate_action="renew", name=list(names), days=days, yes=yes)

    def test_non_interactive_replacement_without_yes_is_refused_before_any_docker_call(self):
        docker = FakeDocker()
        _, error, stdout, _ = self.run_flow(docker, self.renew())
        self.assertIn("needs confirmation", str(error))
        self.assertIn("add -y", str(error))
        self.assertEqual(docker.calls, [])
        self.wait.assert_not_called()

    def test_terminal_confirmation_requires_yes(self):
        for answer in ("no", "", "y"):
            with self.subTest(answer=answer):
                docker = FakeDocker()
                _, error, stdout, _ = self.run_flow(docker, self.renew(), tty=True, answer=answer)
                self.assertEqual(str(error), "Cancelled; the certificate is unchanged")
                self.assertNotIn("certificate-apply", docker.actions())
                self.assertNotIn("container restart", docker.actions())
                self.assertIn("register the new one on each PC", " ".join(stdout.split()))
                self.assertEqual(self.prompts, ["Type yes to replace it: "])

    def test_confirmed_renewal_restarts_the_gateway_in_place_and_verifies_https(self):
        docker = FakeDocker(lock=self.lock)
        code, error, stdout, stderr = self.run_flow(docker, self.renew(names=["raven.example.test"], days=400),
                                                    tty=True, answer=" YES ")
        self.assertIsNone(error, stderr)
        self.assertEqual(code, 0)
        self.assertEqual(docker.actions(), ["volume inspect", "container inspect", "certificate-check",
                                            "container inspect", "certificate-apply", "container restart"])
        request = json.loads(docker.inputs[docker.actions().index("certificate-apply")])
        self.assertEqual(request, {"mode": "renew", "names": ["raven.example.test"], "days": 400,
                                   "origins": ["https://localhost:9443"]})
        self.assertEqual(docker.locked, ["certificate-apply"], "apply must run under the manager lock")
        self.wait.assert_called_once_with({"listen": "127.0.0.1", "port": 18443, "origin": "https://localhost:9443"},
                                          NEW)
        restart = docker.calls[docker.actions().index("container restart")]
        self.assertEqual(restart[-1], NAME)
        self.assertIn(f"Previous certificate saved: {GLOBALS['VOLUME']}:/data/{BACKUP}", stdout)
        self.assertIn("Gateway restarted; https://localhost:9443 answers with the new certificate.", stdout)
        self.assertIn(f"  SHA-256   {FINGERPRINT}\n", stdout)
        self.assertIn("  Expires   ", stdout)
        self.assertIn("(365 days left)", stdout)
        flat = " ".join(stdout.split())
        self.assertIn("register the new certificate on each PC", flat)
        self.assertIn("trusting the certificate on Windows", flat)
        self.assertIn("The account, browser logins and desktops are unchanged.", flat)

    def test_stopped_gateway_only_gets_new_files(self):
        docker = FakeDocker(container=gateway(running=False))
        code, error, stdout, _ = self.run_flow(docker, self.renew(yes=True))
        self.assertEqual(code, 0)
        self.assertNotIn("container restart", docker.actions())
        self.wait.assert_not_called()
        self.assertIn("The gateway is not running; raven web start will serve the new certificate.", stdout)
        request = json.loads(docker.inputs[docker.actions().index("certificate-apply")])
        self.assertEqual(request["origins"], [])

    def test_failed_https_check_restores_the_previous_pair(self):
        self.wait.side_effect = [RequestError("Gateway HTTPS startup failed: handshake failure"), None]
        docker = FakeDocker()
        code, error, stdout, stderr = self.run_flow(docker, self.renew(yes=True))
        self.assertIsNone(error)
        self.assertEqual(code, 1)
        self.assertEqual(docker.actions()[-4:], ["certificate-apply", "container restart", "certificate-restore",
                                                 "container restart"])
        self.assertEqual(json.loads(docker.inputs[-2]), {"backup": "tls-20261118T101500Z"})
        self.assertEqual([call.args[1] for call in self.wait.call_args_list], [NEW, OLD])
        self.assertIn("did not serve the new certificate: Gateway HTTPS startup failed: handshake failure", stderr)
        self.assertIn("restored the previous certificate", stderr)
        self.assertNotIn("New certificate", stdout)

    def test_failed_restore_points_to_the_backup_and_doctor(self):
        self.wait.side_effect = RequestError("Gateway exited during startup")
        code, _, _, stderr = self.run_flow(FakeDocker(), self.renew(yes=True))
        self.assertEqual(code, 1)
        self.assertIn("restoring the previous certificate failed too", stderr)
        self.assertIn(f"{GLOBALS['VOLUME']}:/data/{BACKUP}; run raven web doctor", stderr)
        incomplete = FakeDocker()
        incomplete.helpers["certificate-apply"] = {**incomplete.helpers["certificate-apply"],
                                                   "complete_backup": False}
        self.wait.side_effect = RequestError("Gateway exited during startup")
        code, _, _, stderr = self.run_flow(incomplete, self.renew(yes=True))
        self.assertEqual(code, 1)
        self.assertNotIn("certificate-restore", incomplete.actions())
        self.assertIn("no complete previous pair", stderr)

    def test_import_sends_the_key_on_stdin_only_and_never_prints_it(self):
        certificate = self.scratch / "server.crt"
        key = self.scratch / "server.key"
        certificate.write_text(NEW)
        key.write_text(KEY)
        key.chmod(0o644)
        docker = FakeDocker()
        args = Namespace(certificate_action="import", certificate=certificate, key=key, yes=True)
        code, error, stdout, stderr = self.run_flow(docker, args)
        self.assertIsNone(error, stderr)
        self.assertEqual(code, 0)
        self.assertIn(f"KEY {key} is readable by other users (mode 0644); chmod 600", stderr)
        for argv in docker.calls:
            self.assertFalse(any(KEY_LINE in part or "PRIVATE KEY" in part for part in argv), argv)
        carriers = [text for text in docker.inputs if text and KEY_LINE in text]
        self.assertEqual(len(carriers), 2, "the key goes to the check and the apply helper on stdin")
        self.assertEqual(json.loads(carriers[0])["key"], KEY)
        for output in (stdout, stderr):
            self.assertNotIn(KEY_LINE, output)
            self.assertNotIn("PRIVATE KEY", output)

    def test_import_file_problems_are_named_without_content(self):
        binary = self.scratch / "der.key"
        binary.write_bytes(b"\x30\x82" + KEY_LINE.encode())
        certificate = self.scratch / "server.crt"
        certificate.write_text(NEW)
        cases = ((self.scratch / "missing.key", "Cannot read KEY"), (binary, "holds binary data"),
                 (self.scratch, "is not a regular file"))
        for path, message in cases:
            with self.subTest(message=message):
                args = Namespace(certificate_action="import", certificate=certificate, key=path, yes=True)
                _, error, _, _ = self.run_flow(FakeDocker(), args)
                self.assertIn(message, str(error))
                self.assertNotIn(KEY_LINE, str(error))

    def test_helper_refusal_reads_as_its_reason_and_is_redacted(self):
        reason = "Raven configuration: KEY does not match the certificate: their public keys differ\n" + KEY
        docker = FakeDocker(**{"certificate-check": reason})
        _, error, _, _ = self.run_flow(docker, self.renew(yes=True))
        self.assertTrue(str(error).startswith("KEY does not match the certificate"), str(error))
        self.assertNotIn(KEY_LINE, str(error))
        self.assertNotIn("certificate-apply", docker.actions())

    def test_days_are_checked_before_docker(self):
        docker = FakeDocker()
        _, error, _, _ = self.run_flow(docker, self.renew(yes=True, days=826))
        self.assertIn("--days must be from 1 to 825", str(error))
        self.assertEqual(docker.actions(), ["volume inspect"])


class CertificateOutputTests(unittest.TestCase):
    def render(self, function, *arguments):
        stream = io.StringIO()
        with redirect_stdout(stream):
            function(*arguments)
        return stream.getvalue()

    def test_status_section_levels(self):
        cases = ((365, None, "(365 days left)"), (10, "Certificate expires in 10 days -> raven web certificate renew",
                                                 "(10 days left)"),
                 (-3, "Certificate expired on", "(expired)"))
        for days, headline, expires in cases:
            with self.subTest(days=days):
                info = summary(days=days)
                if days < 0:
                    info["not_before"] = (NOW - timedelta(days=400)).isoformat()
                data = {"certificate_info": {**info, "origin_covered": True}, "config": {"origin": "https://x"}}
                output = self.render(manager["certificate_section"], manager["Terminal"](), data)
                lines = output.splitlines()
                self.assertEqual(lines[1].startswith("Certificate"), True, output)
                if headline:
                    self.assertIn(headline.split(" ", 1)[1], lines[1])
                else:
                    self.assertEqual(lines[1], "Certificate")
                self.assertIn("  Names     localhost\n", output)
                self.assertIn("  Expires   " + datetime.fromisoformat(info["not_after"]).strftime("%Y-%m-%d"), output)
                self.assertIn(expires, output)
                self.assertIn(f"  SHA-256   {FINGERPRINT}\n", output)
                self.assertNotIn("Origin", output)
        data = {"certificate_info": {**summary(), "origin_covered": False}, "config": {"origin": "https://x.test"}}
        output = self.render(manager["certificate_section"], manager["Terminal"](), data)
        self.assertIn("  Origin    https://x.test is not covered -> raven web certificate renew --name HOST", output)
        output = self.render(manager["certificate_section"], manager["Terminal"](), {"certificate_error": "broken"})
        self.assertIn("Certificate unavailable: broken", output)

    def test_info_shows_names_validity_fingerprint_origin_and_trust(self):
        inspection = {"certificate_info": {**summary(names=("localhost", "10.0.0.7")), "origin_covered": True},
                      "config": {"origin": "https://localhost:9443"}}
        docker = FakeDocker(inspect=inspection)
        with patch("subprocess.run", side_effect=docker):
            output = self.render(manager["show_certificate"])
        self.assertEqual(docker.actions(), ["volume inspect", "inspect"])
        run = docker.calls[-1]
        self.assertTrue(any(part.endswith(",readonly") for part in run), "info must mount the volume read-only")
        self.assertIn("  Names     localhost, 10.0.0.7\n", output)
        self.assertRegex(output, r"  Valid     \d{4}-\d\d-\d\d \d\d:\d\d to \d{4}-\d\d-\d\d \d\d:\d\d UTC\n")
        self.assertIn("(365 days left)\n", output)
        self.assertIn(f"  SHA-256   {FINGERPRINT}\n", output)
        self.assertIn("  Origin    https://localhost:9443 is covered\n", output)
        self.assertIn("  Issuer    self-signed\n", output)
        trust = [line for line in output.splitlines() if line.startswith("Trust:")]
        self.assertEqual(len(trust), 1)
        self.assertIn("raven web certificate", trust[0])
        self.assertIn("trusting the certificate on Windows", trust[0])

    def test_doctor_points_to_renewal(self):
        info = {**summary(days=5)}
        report = manager["Report"]()
        output = self.render(manager["check_certificate"], report, {"certificate_info": info})
        self.assertIn("  warn  Certificate      localhost, expires on", output)
        self.assertIn("-> raven web certificate renew", output)
        self.assertNotIn("M8", output)

    def test_arguments(self):
        cases = ((["certificate"], {"certificate_action": None}),
                 (["certificate", "info"], {"certificate_action": "info"}),
                 (["certificate", "renew", "--name", "a.test", "--name", "10.0.0.7", "--days", "30", "-y"],
                  {"certificate_action": "renew", "name": ["a.test", "10.0.0.7"], "days": 30, "yes": True}),
                 (["certificate", "renew"], {"name": [], "days": 365, "yes": False}),
                 (["certificate", "import", "c.pem", "k.pem"],
                  {"certificate_action": "import", "certificate": Path("c.pem"), "key": Path("k.pem"), "yes": False}))
        for argv, expected in cases:
            with self.subTest(argv=argv), patch.object(sys, "argv", ["raven-web", *argv]):
                args = manager["arguments"]()
                self.assertEqual({key: getattr(args, key) for key in expected}, expected)

    def test_https_checks_trust_a_chain_without_its_root(self):
        with patch("ssl.create_default_context") as create:
            create.return_value = Mock(verify_flags=0)
            result = manager["trusting"](NEW)
        create.assert_called_once_with(cadata=NEW)
        self.assertTrue(result.verify_flags & GLOBALS["ssl"].VERIFY_X509_PARTIAL_CHAIN)


if __name__ == "__main__":
    unittest.main()
