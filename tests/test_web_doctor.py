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


UNIT_ABSENT = "LoadState=not-found\nUnitFileState=\nActiveState=inactive\nResult=success\nNRestarts=0\nMainPID=0\n"


class FakeDocker:
    """Answer docker calls from a table and record them; systemd reports an absent unit unless told otherwise."""

    def __init__(self, unit=UNIT_ABSENT, linger="no", **responses):
        self.calls = []
        self.unit = unit
        self.linger = linger
        self.responses = responses

    def __call__(self, argv, **kwargs):
        if argv[0] == "systemctl":
            return subprocess.CompletedProcess(argv, 0, self.unit, "")
        if argv[0] == "loginctl":
            return subprocess.CompletedProcess(argv, 0, self.linger + "\n", "")
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
        self.assertIn("  fail  Docker           cannot use the daemon\n", output)
        self.assertIn("-> add this account to the docker group or fix DOCKER_HOST", output)
        self.assertIn("docker: " + denied, output)
        self.assertIn("  skip  Gateway          needs Docker", output)
        self.assertIn("  fail  Broker           stopped", output)
        rows = [line.split()[1] for line in output.splitlines() if line[:6].strip() in ("ok", "warn", "fail", "skip")]
        self.assertEqual(rows, [item.split()[0] for item in GLOBALS["DOCTOR_ITEMS"]])
        self.assertTrue(output.rstrip().endswith('2 failed, 8 skipped. Fix the "->" items from the top.'))
        self.assertNotIn("\033[", output)
        self.assertEqual([verb(argv) for argv in docker.calls], ["version"])
        _, output = run_doctor(FakeDocker(version=FileNotFoundError()))
        self.assertIn("docker CLI is not installed", output)

    def test_uninitialized_install_is_reported_with_read_only_docker_calls(self):
        docker = FakeDocker(version=(0, "29.1.3\n", ""), container_inspect=(1, "", "Error: No such container: x"),
                            image_inspect=(0, "sha256:" + "a" * 64 + "\n", ""),
                            volume_inspect=(1, "", "Error: no such volume: x"))
        failed, output = run_doctor(docker)
        self.assertTrue(failed)
        self.assertIn("is not created\n" + " " * 25 + "-> raven web start", output)
        self.assertIn("does not exist\n" + " " * 25 + "-> raven web init --username NAME", output)
        self.assertIn("  skip  HTTPS            the gateway is not running", output)
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
        self.assertIn("  skip  Account          needs a readable data volume", output)
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
                report = manager["Report"]()
                stream = io.StringIO()
                with redirect_stdout(stream):
                    manager["check_certificate"](report, {"certificate_info": info})
                output = stream.getvalue()
                self.assertTrue(output.startswith(f"  {level:<5} Certificate      localhost, "), output)
                self.assertIn(message, output)
                self.assertEqual(report.notes, [("SHA-256", "AB:CD")])
        info = {"not_before": (now + timedelta(days=1)).isoformat(), "not_after": (now + timedelta(days=9)).isoformat(),
                "san": ["localhost"], "fingerprint": "AB:CD"}
        _, output, failed = call(manager["check_certificate"], {"certificate_info": info})
        self.assertTrue(failed)
        self.assertIn("check the server clock", output)

    def test_settings_report_pending_changes_and_busy_ports(self):
        data = {"config": {"origin": "https://localhost:8443", "listen": "127.0.0.1", "port": 9555}}
        settings, output, failed = call(manager["check_settings"], data, gateway())
        self.assertFalse(failed)
        self.assertIn("  warn  Settings         saved settings are not applied yet", output)
        self.assertIn("running 127.0.0.1:9444 for https://localhost:8443", output)
        self.assertIn("saved   127.0.0.1:9555 for https://localhost:8443", output)
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
        self.assertIn("Broker           stopped (no ", output)
        self.runtime.mkdir(mode=0o755)
        self.runtime.chmod(0o755)
        _, output, _ = call(check)
        self.assertIn("must be a directory owned by uid", output)
        self.runtime.chmod(0o700)
        _, output, _ = call(check)
        self.assertIn("Broker           stopped (no socket)", output)
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
            self.assertIn("  warn  Broker           pid 4321 runs /elsewhere/raven, not this checkout", output)
            server.raven = str(GLOBALS["SOURCE"] / "raven")
            _, output, _ = call(check)
            self.assertIn("  ok    Broker           pid 4321", output)
        finally:
            server.shutdown()
            server.server_close()
            thread.join()

    def test_autostart_levels(self):
        def unit(file_state, active, result="success", restarts=0):
            return (f"LoadState=loaded\nUnitFileState={file_state}\nActiveState={active}\nResult={result}\n"
                    f"NRestarts={restarts}\nMainPID=0\n")

        name = GLOBALS["UNIT"]
        cases = ((UNIT_ABSENT, "no", "  ok    Autostart        off"),
                 (unit("enabled", "active"), "yes", f"  ok    Autostart        {name} enabled and running"),
                 (unit("enabled-runtime", "active"), "no", "  warn  Autostart        enabled, but linger is off"),
                 (unit("enabled", "failed", "start-limit-hit", 5), "yes",
                  f"  fail  Autostart        {name} failed (start-limit-hit) after 5 restarts"),
                 (unit("enabled", "inactive"), "yes", f"  warn  Autostart        {name} is enabled but inactive"))
        for state, lingering, expected in cases:
            with self.subTest(expected=expected):
                with patch("subprocess.run", side_effect=FakeDocker(unit=state, linger=lingering)):
                    _, output, _ = call(manager["check_autostart"])
                self.assertIn(expected, output)


class AutostartTests(unittest.TestCase):
    def test_unit_runs_serve_with_the_cli_environment_quoted_for_systemd(self):
        environment = {"HOME": "/home/u", "PATH": "/usr/bin", "XDG_RUNTIME_DIR": "/tmp/r 1",
                       "RAVEN_WEB_INSTANCE": "web", "RAVEN_X": 'a"b%c\\d', "DISPLAY": ":0", "XDG_SESSION_ID": "3"}
        with patch.dict(os.environ, environment, clear=True):
            text = manager["unit_text"]()
        self.assertIn('Environment="RAVEN_X=a\\"b%%c\\\\d"\n', text)
        self.assertIn('Environment="XDG_RUNTIME_DIR=/tmp/r 1"\n', text)
        self.assertNotIn("DISPLAY", text)
        self.assertNotIn("XDG_SESSION_ID", text)
        self.assertIn('/raven-web" "serve"\n', text)
        for line in ("Type=exec", "Restart=on-failure", "StartLimitBurst=5", "WantedBy=default.target"):
            self.assertIn(line + "\n", text)

    def test_serve_waits_for_a_late_docker_daemon(self):
        results = iter([subprocess.CompletedProcess([], 1, "", "Cannot connect to the Docker daemon"),
                        subprocess.CompletedProcess([], 0, "29.1.3\n", "")])
        with patch("subprocess.run", side_effect=lambda *args, **kwargs: next(results)), \
                patch("time.sleep") as sleep:
            manager["wait_for_docker"]()
        sleep.assert_called_once()


if __name__ == "__main__":
    unittest.main()
