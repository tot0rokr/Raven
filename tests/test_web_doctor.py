"""Unit tests for raven web doctor with a recorded fake Docker CLI."""

from contextlib import redirect_stdout
from datetime import datetime, timedelta, timezone
import io
import json
import os
from pathlib import Path
import runpy
import socket
import socketserver
import subprocess
import tempfile
import threading
import unittest
from unittest.mock import patch

manager = runpy.run_path(str(Path(__file__).resolve().parents[1] / "raven-web"))
GLOBALS = manager["doctor"].__globals__
READ_ONLY = {"version", "container inspect", "volume inspect", "image inspect", "run", "exec"}
OWNED = {"Labels": {"raven.web.uid": str(os.getuid())}}


def verb(argv):
    return argv[1] if argv[1] in ("version", "run", "exec") else " ".join(argv[1:3])


class FakeDocker:
    def __init__(self, **responses):
        self.calls = []
        self.responses = responses

    def __call__(self, argv, **kwargs):
        self.calls.append(argv)
        response = self.responses[verb(argv).replace(" ", "_")]
        if isinstance(response, BaseException):
            raise response
        returncode, stdout, stderr = response
        return subprocess.CompletedProcess(argv, returncode, stdout, stderr)


def run_doctor(docker):
    stream = io.StringIO()
    with patch("subprocess.run", side_effect=docker), redirect_stdout(stream):
        failed = manager["doctor"]()
    return failed, stream.getvalue()


def call(function, *arguments):
    stream = io.StringIO()
    report = manager["Report"]()
    with redirect_stdout(stream):
        result = function(report, *arguments)
    return result, stream.getvalue(), report.failed


def gateway(port=9444, origin="https://localhost:8443", running=True):
    return {"State": {"Running": running, "Status": "running" if running else "exited"},
            "HostConfig": {"PortBindings": {"8443/tcp": [{"HostIp": "127.0.0.1", "HostPort": str(port)}]}},
            "Config": {"Env": [f"RAVEN_WEB_ORIGIN={origin}"]}}


class BrokerHandler(socketserver.StreamRequestHandler):
    def handle(self):
        self.rfile.readline()
        self.wfile.write(json.dumps({"ok": True, "data": {"raven": self.server.raven, "pid": 4321}}).encode() + b"\n")


class DoctorTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="raven-doctor-test-")
        runtime = Path(self.temporary.name) / "runtime"
        self.paths = patch.dict(GLOBALS, {"RUNTIME": runtime, "ADDRESS": runtime / "broker.sock"})
        self.paths.start()
        self.runtime = runtime

    def tearDown(self):
        self.paths.stop()
        self.temporary.cleanup()

    def test_redaction_hides_private_keys_cookies_and_passwords(self):
        text = ("password=hunter2 Cookie: __Host-raven_session=abc123; token: xyz\n"
                "-----BEGIN PRIVATE KEY-----\nMIIEvQ\n-----END PRIVATE KEY-----")
        redacted = manager["redact"](text)
        for secret in ("hunter2", "abc123", "xyz", "MIIEvQ"):
            self.assertNotIn(secret, redacted)
        self.assertIn("[private key redacted]", redacted)

    def test_unreachable_docker_is_explained_and_skips_dependent_checks(self):
        denied = "permission denied while trying to connect to the Docker daemon socket"
        docker = FakeDocker(version=(1, "", denied))
        failed, output = run_doctor(docker)
        self.assertTrue(failed)
        self.assertIn("fail  docker: cannot use the daemon; add this account to the docker group", output)
        self.assertIn("      " + denied, output)
        self.assertIn("skip  gateway, image, data volume", output)
        self.assertIn("fail  broker: stopped", output)
        self.assertEqual([verb(argv) for argv in docker.calls], ["version"])
        _, output = run_doctor(FakeDocker(version=FileNotFoundError()))
        self.assertIn("docker CLI is not installed", output)

    def test_uninitialized_install_is_reported_with_read_only_docker_calls(self):
        docker = FakeDocker(version=(0, "29.1.3\n", ""), container_inspect=(1, "", "Error: No such container: x"),
                            image_inspect=(0, "sha256:" + "a" * 64 + "\n", ""),
                            volume_inspect=(1, "", "Error: no such volume: x"))
        failed, output = run_doctor(docker)
        self.assertTrue(failed)
        self.assertIn("not created; run: raven web start", output)
        self.assertIn("not found; run: raven web init --username NAME", output)
        self.assertIn("skip  gateway -> broker and HTTPS: the gateway is not running", output)
        self.assertLessEqual({verb(argv) for argv in docker.calls}, READ_ONLY)

    def test_foreign_data_owner_prints_a_root_repair_command_without_running_it(self):
        inspection = {"directory": {"uid": 4242, "gid": 4242, "mode": 0o700},
                      "files": dict.fromkeys(("config.json", "tls.crt"), {"error": "Permission denied"})}
        docker = FakeDocker(version=(0, "29.1.3\n", ""), container_inspect=(1, "", "No such container"),
                            image_inspect=(0, "sha256:" + "a" * 64 + "\n", ""),
                            volume_inspect=(0, json.dumps([OWNED]), ""), run=(0, json.dumps(inspection), ""))
        failed, output = run_doctor(docker)
        self.assertTrue(failed)
        self.assertIn("/data belongs to uid 4242, but the gateway runs as uid", output)
        self.assertIn("chown -R", output)
        self.assertIn("skip  account, certificate and settings", output)
        runs = [argv for argv in docker.calls if verb(argv) == "run"]
        self.assertEqual(len(runs), 1)
        self.assertIn("none", runs[0][runs[0].index("--network") + 1])
        self.assertTrue(any(argument.endswith(",readonly") for argument in runs[0]))
        self.assertNotIn("chown", runs[0])

    def test_certificate_expiry_levels(self):
        now = datetime.now(timezone.utc)
        cases = ((now - timedelta(days=1), "fail", "expired on"), (now + timedelta(days=10), "warn", "days left"),
                 (now + timedelta(days=200), "ok", "valid until"))
        for not_after, level, message in cases:
            with self.subTest(level=level):
                info = {"not_before": (now - timedelta(days=2)).isoformat(), "not_after": not_after.isoformat(),
                        "san": ["localhost"], "fingerprint": "AB:CD"}
                _, output, _ = call(manager["check_certificate"], {"certificate_info": info})
                self.assertTrue(output.startswith(f"{level:<5} certificate for localhost: "), output)
                self.assertIn(message, output)
                self.assertIn("info  certificate SHA-256 AB:CD", output)
        info = {"not_before": (now + timedelta(days=1)).isoformat(), "not_after": (now + timedelta(days=9)).isoformat(),
                "san": ["localhost"], "fingerprint": "AB:CD"}
        _, output, failed = call(manager["check_certificate"], {"certificate_info": info})
        self.assertTrue(failed)
        self.assertIn("check the server clock", output)

    def test_settings_report_pending_changes_and_busy_ports(self):
        data = {"config": {"origin": "https://localhost:8443", "listen": "127.0.0.1", "port": 9555}}
        settings, output, failed = call(manager["check_settings"], data, gateway())
        self.assertFalse(failed)
        self.assertIn("warn  settings: the gateway runs 127.0.0.1:9444", output)
        self.assertIn("next start uses 127.0.0.1:9555", output)
        self.assertEqual(settings["port"], 9444)
        with socket.socket() as listener:
            listener.bind(("127.0.0.1", 0))
            listener.listen()
            port = listener.getsockname()[1]
            busy = {"config": {**data["config"], "port": port}}
            _, output, failed = call(manager["check_settings"], busy, gateway(running=False))
        self.assertTrue(failed)
        self.assertIn("cannot listen (Cannot bind", output)

    def test_broker_socket_states(self):
        check = manager["check_broker"]
        _, output, _ = call(check)
        self.assertIn("broker: stopped (no ", output)
        self.runtime.mkdir(mode=0o755)
        self.runtime.chmod(0o755)
        _, output, _ = call(check)
        self.assertIn("must be a directory owned by uid", output)
        self.runtime.chmod(0o700)
        _, output, _ = call(check)
        self.assertIn("broker: stopped (no socket)", output)
        address = str(self.runtime / "broker.sock")
        with socket.socket(socket.AF_UNIX) as stale:
            stale.bind(address)
        os.chmod(address, 0o600)
        _, output, _ = call(check)
        self.assertIn("stale socket", output)
        os.unlink(address)
        server = socketserver.UnixStreamServer(address, BrokerHandler)
        os.chmod(address, 0o600)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            server.raven = "/elsewhere/raven"
            _, output, failed = call(check)
            self.assertFalse(failed)
            self.assertIn("warn  broker: pid 4321 runs /elsewhere/raven, not this checkout", output)
            server.raven = str(GLOBALS["SOURCE"] / "raven")
            _, output, _ = call(check)
            self.assertIn("ok    broker: pid 4321", output)
        finally:
            server.shutdown()
            server.server_close()
            thread.join()


if __name__ == "__main__":
    unittest.main()
