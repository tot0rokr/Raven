import hashlib
import json
import os
import stat
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from raven_web.bootstrap import initialize
from raven_web.service_config import ConfigurationError, execute


class ServiceConfigurationTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="raven-config-test-")
        self.state = Path(self.temporary.name)
        with patch.dict(os.environ, {"RAVEN_WEB_STATE": str(self.state)}):
            initialize(["--origin", "https://localhost:8443"])
        self.path = self.state / "config.json"
        self.original = self.path.read_bytes()
        self.config = {
            "origin": "https://localhost:9443", "listen": "127.0.0.1", "port": 10443,
            "ssh_target": "owner@desktop-host", "ssh_port": 22,
        }

    def tearDown(self):
        self.temporary.cleanup()

    def payload(self, config=None):
        return {
            "config": self.config.copy() if config is None else config,
            "revision": hashlib.sha256(self.path.read_bytes()).hexdigest(),
        }

    def files(self):
        return {path.name: (path.stat().st_ino, path.stat().st_size, path.stat().st_mtime_ns)
                for path in self.state.iterdir()}

    def test_read_legacy_and_validate_do_not_change_files(self):
        before = self.files()
        result = execute("read", state=self.state)
        self.assertEqual(result["config"], {"origin": "https://localhost:8443"})
        self.assertEqual(result["revision"], hashlib.sha256(self.original).hexdigest())
        self.assertEqual(result["certificate"], (self.state / "tls.crt").read_text())
        result = execute("validate", self.payload(), self.state)
        self.assertEqual(result["config"], self.config)
        self.assertIsNone(result["backup"])
        self.assertEqual(self.files(), before)

    def test_missing_or_incomplete_configuration_is_reported(self):
        with tempfile.TemporaryDirectory() as missing:
            with self.assertRaisesRegex(ConfigurationError, "Missing config.json"):
                execute("read", state=missing)
            Path(missing, "config.json").write_text('{"origin":"https://localhost:8443"}')
            with self.assertRaisesRegex(ConfigurationError, "Missing tls.crt"):
                execute("read", state=missing)
        for raw in (b"", b"{", b"[]", b"{}"):
            with self.subTest(raw=raw):
                self.path.write_bytes(raw)
                with self.assertRaises(ConfigurationError):
                    execute("read", state=self.state)

    def test_invalid_origin_and_certificate_hostname_are_rejected(self):
        invalid = (
            "http://localhost:9443", "https://owner:password@localhost:9443",
            "https://localhost/path", "https://localhost?query", "https://localhost#",
            "https://localhost:443", "https://localhost:0", "https://localhost:",
            "https://other-host:9443", "https://127.0.0.1:9443", None,
        )
        for origin in invalid:
            with self.subTest(origin=origin):
                with self.assertRaises(ConfigurationError):
                    execute("validate", self.payload({**self.config, "origin": origin}), self.state)
        self.assertEqual(self.path.read_bytes(), self.original)

    def test_ports_and_listen_address_are_validated(self):
        invalid = {
            "port": (True, "8443", 1023, 65536, None),
            "ssh_port": (False, "22", 0, 65536, None),
            "listen": (None, 0, "localhost", "127.0.0.1:8443", "fe80::1%eth0"),
        }
        for name, values in invalid.items():
            for value in values:
                with self.subTest(name=name, value=value):
                    with self.assertRaises(ConfigurationError):
                        execute("validate", self.payload({**self.config, name: value}), self.state)
        for port, ssh_port, listen in ((1024, 1, "0.0.0.0"), (65535, 65535, "::1")):
            result = execute("validate", self.payload({
                **self.config, "port": port, "ssh_port": ssh_port, "listen": listen,
            }), self.state)
            self.assertEqual(result["config"]["listen"], listen)

    def test_noncanonical_browser_origins_are_rejected_before_san_matching(self):
        invalid = (
            "https://localhost:08443", "https://[0:0:0:0:0:0:0:1]:9443",
            "https://[2001:0db8::1]:9443", "https://%6cocalhost:9443",
            "https://127.1:9443", "https://0x7f000001:9443", "https://0177.0.0.1:9443",
            "https://2130706433:9443", "https://example.123:9443", "https://example.0x7f:9443",
            "https://localhost.:9443", "https://bad_name:9443",
        )
        for origin in invalid:
            with self.subTest(origin=origin):
                with self.assertRaisesRegex(ConfigurationError, "canonical HTTPS origin"):
                    execute("validate", self.payload({**self.config, "origin": origin}), self.state)
        result = execute("validate", self.payload({**self.config, "origin": "https://localhost"}), self.state)
        self.assertEqual(result["config"]["origin"], "https://localhost")

    def test_safe_ssh_aliases_and_ipv6_are_accepted(self):
        for target in (None, "desktop-host", "owner@desktop-host", "user@[::1]", "2001:db8::1"):
            with self.subTest(target=target):
                result = execute("validate", self.payload({**self.config, "ssh_target": target}), self.state)
                self.assertEqual(result["config"]["ssh_target"], target)
        for target in ("", "-oProxyCommand=id", "host name", "user@-host", "u@@host", "a;id", "$(id)", "a`id`", "a\nb", "[::1];id", "fe80::1%eth0", 22):
            with self.subTest(target=target):
                with self.assertRaises(ConfigurationError):
                    execute("validate", self.payload({**self.config, "ssh_target": target}), self.state)

    def test_ip_origins_require_matching_ip_sans(self):
        for host in ("127.0.0.1", "[::1]", "[2001:db8::1]"):
            with self.subTest(host=host), tempfile.TemporaryDirectory() as directory:
                with patch.dict(os.environ, {"RAVEN_WEB_STATE": directory}):
                    initialize(["--origin", f"https://{host}:8443"])
                current = execute("read", state=directory)
                candidate = {**self.config, "origin": f"https://{host}:9443"}
                result = execute("validate", {"config": candidate, "revision": current["revision"]}, directory)
                self.assertEqual(result["config"], candidate)

    def test_unknown_existing_keys_must_be_preserved(self):
        current = {"origin": "https://localhost:8443", "future": {"enabled": True}}
        self.path.write_text(json.dumps(current))
        candidate = {**self.config, "future": current["future"]}
        result = execute("write", self.payload(candidate), self.state)
        self.assertEqual(result["config"]["future"], {"enabled": True})
        for invalid in (self.config, {**candidate, "future": False}, {**candidate, "typo": 1}):
            with self.assertRaises(ConfigurationError):
                execute("validate", self.payload(invalid), self.state)

    def test_write_preserves_verified_private_backup(self):
        result = execute("write", self.payload(), self.state)
        backup = Path(result["backup"])
        self.assertRegex(backup.name, r"^config\.json\.bak\.configure-v1\.\d{8}-\d{6}-[0-9a-f]+$")
        self.assertEqual(backup.read_bytes(), self.original)
        self.assertGreater(backup.stat().st_size, 0)
        self.assertEqual(stat.S_IMODE(backup.stat().st_mode), 0o600)
        self.assertEqual(stat.S_IMODE(self.path.stat().st_mode), 0o600)
        self.assertEqual(json.loads(self.path.read_bytes()), self.config)
        self.assertEqual(result["revision"], hashlib.sha256(self.path.read_bytes()).hexdigest())

    def test_stale_revision_cannot_overwrite_newer_configuration(self):
        stale = self.payload()
        execute("write", stale, self.state)
        before = self.files()
        for action in ("validate", "write"):
            with self.assertRaisesRegex(ConfigurationError, "Configuration changed"):
                execute(action, stale, self.state)
        self.assertEqual(self.files(), before)
        self.assertEqual(json.loads(self.path.read_bytes()), self.config)

    def test_unchanged_write_keeps_inode_contents_and_backup_count(self):
        result = execute("write", self.payload(), self.state)
        before = self.files()
        unchanged = execute("write", {"config": self.config, "revision": result["revision"]}, self.state)
        self.assertIsNone(unchanged["backup"])
        self.assertEqual(unchanged["revision"], result["revision"])
        self.assertEqual(self.files(), before)

    def test_failed_atomic_replace_leaves_original_and_verified_backup(self):
        with patch("os.replace", side_effect=OSError("replace failed")):
            with self.assertRaises(OSError):
                execute("write", self.payload(), self.state)
        self.assertEqual(self.path.read_bytes(), self.original)
        backups = list(self.state.glob("config.json.bak.configure-v1.*"))
        self.assertEqual(len(backups), 1)
        self.assertEqual(backups[0].read_bytes(), self.original)
        self.assertEqual(list(self.state.glob(".config.json.configure-v1.*")), [])

    def test_directory_sync_failure_reports_committed_configuration(self):
        original_fsync = os.fsync

        def sync(descriptor):
            if stat.S_ISDIR(os.fstat(descriptor).st_mode) and self.path.read_bytes() != self.original:
                raise OSError("directory sync failed")
            return original_fsync(descriptor)

        with patch("os.fsync", side_effect=sync):
            result = execute("write", self.payload(), self.state)
        self.assertIn("was replaced", result["warning"])
        self.assertEqual(json.loads(self.path.read_bytes()), self.config)
        self.assertEqual(Path(result["backup"]).read_bytes(), self.original)

    def test_cli_uses_json_and_does_not_import_django(self):
        environment = {**os.environ, "RAVEN_WEB_STATE": str(self.state)}
        result = subprocess.run(
            [sys.executable, "-m", "raven_web.service_config", "read"],
            env=environment, capture_output=True, text=True, check=True,
        )
        self.assertEqual(json.loads(result.stdout)["config"], {"origin": "https://localhost:8443"})
        subprocess.run([
            sys.executable, "-c",
            "import sys; import raven_web.service_config; assert 'django' not in sys.modules",
        ], env=environment, capture_output=True, text=True, check=True)
        result = subprocess.run(
            [sys.executable, "-m", "raven_web.service_config", "validate"],
            input=json.dumps({"config": self.config, "revision": "invalid"}),
            env=environment, capture_output=True, text=True,
        )
        self.assertEqual(result.returncode, 1)
        self.assertEqual(result.stdout, "")
        self.assertIn("revision", result.stderr)


if __name__ == "__main__":
    unittest.main()
