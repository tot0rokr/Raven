from argparse import Namespace
from contextlib import redirect_stdout
import io
import ipaddress
import json
import os
from pathlib import Path
import runpy
import shlex
import socket
import subprocess
import unittest
from unittest.mock import patch

from raven_web_host import RequestError

manager = runpy.run_path(str(Path(__file__).resolve().parents[1] / "raven-web"))


class DockerErrorTests(unittest.TestCase):
    def test_docker_failure_exposes_the_actual_diagnostic(self):
        message = "Bind for 127.0.0.1:9443 failed: port is already allocated"
        failure = subprocess.CalledProcessError(125, ["docker", "run"], stderr=message + "\n")
        with patch("subprocess.run", side_effect=failure), self.assertRaises(RequestError) as error:
            manager["docker"]("run", "example")
        self.assertEqual(str(error.exception), message)

    def test_interactive_failure_has_a_short_summary(self):
        failure = subprocess.CalledProcessError(1, ["docker", "run"])
        with patch("subprocess.run", side_effect=failure), self.assertRaises(RequestError) as error:
            manager["docker"]("run", "example", capture=False)
        self.assertEqual(str(error.exception), "Docker run failed (exit 1)")

    def test_optional_inspection_preserves_nonzero_result(self):
        result = subprocess.CompletedProcess(["docker", "inspect"], 1, "", "No such container")
        with patch("subprocess.run", return_value=result) as command:
            self.assertIs(manager["docker"]("container", "inspect", "missing", check=False), result)
        self.assertFalse(command.call_args.kwargs["check"])

    def test_absent_container_is_distinct_from_unavailable_daemon(self):
        absent = subprocess.CompletedProcess([], 1, "", "Error: No such container: missing")
        with patch("subprocess.run", return_value=absent):
            self.assertIsNone(manager["inspect_owned"]("container", "missing"))
        diagnostic = "Cannot connect to the Docker daemon at unix:///var/run/docker.sock"
        unavailable = subprocess.CompletedProcess([], 1, "", diagnostic)
        with patch("subprocess.run", return_value=unavailable), self.assertRaisesRegex(RequestError, diagnostic):
            manager["inspect_owned"]("container", "missing")

    def test_inspection_rejects_a_container_owned_by_another_uid(self):
        item = {"Config": {"Labels": {"raven.web.uid": str(os.getuid() + 1)}}}
        result = subprocess.CompletedProcess([], 0, json.dumps([item]), "")
        with patch("subprocess.run", return_value=result), self.assertRaisesRegex(RequestError, "owner label"):
            manager["inspect_owned"]("container", "foreign")


def gateway(port, listen="127.0.0.1", running=False):
    return {"Id": "a" * 64, "State": {"Running": running}, "Config": {"Env": []},
            "HostConfig": {"PortBindings": {"8443/tcp": [{"HostIp": listen, "HostPort": str(port)}]}}}


class ConfigurationTests(unittest.TestCase):
    def test_legacy_origin_uses_existing_container_binding(self):
        config = manager["desired_configuration"]({"origin": "https://localhost:9443"}, gateway(18443))
        self.assertEqual(config, {"listen": "127.0.0.1", "port": 18443, "origin": "https://localhost:9443"})

    def test_failed_modern_container_does_not_replace_unsaved_defaults(self):
        current = gateway(18443)
        current["Config"]["Env"] = ["RAVEN_WEB_ORIGIN=https://localhost:9443"]
        saved = {"origin": "https://localhost:9443"}
        self.assertEqual(manager["desired_configuration"](saved, current)["port"], 8443)
        current["State"]["Running"] = True
        self.assertEqual(manager["desired_configuration"](saved, current)["port"], 18443)
        self.assertEqual(manager["desired_configuration"]({**saved, "port": 19443}, current)["port"], 19443)

    def test_saved_settings_override_previous_binding_and_explicit_flags_override_saved(self):
        saved = {"origin": "https://localhost:9443", "listen": "127.0.0.1", "port": 18443,
                 "ssh_target": "owner@example.test", "ssh_port": 2222}
        self.assertEqual(manager["desired_configuration"](saved, gateway(8443)), saved)
        args = Namespace(listen=ipaddress.ip_address("::1"), port=19443, origin=None,
                         ssh_target=None, ssh_port=None)
        merged = manager["desired_configuration"](saved, gateway(8443), args)
        self.assertEqual(merged, {**saved, "listen": "::1", "port": 19443})
        self.assertEqual(saved["port"], 18443)

    def test_running_origin_uses_process_environment_or_legacy_configuration(self):
        current = gateway(18443, running=True)
        saved = {"origin": "https://localhost:9443"}
        self.assertEqual(manager["running_origin"](current, saved), "https://localhost:9443")
        current["Config"]["Env"] = ["RAVEN_WEB_ORIGIN=https://localhost:10443"]
        self.assertEqual(manager["running_origin"](current, saved), "https://localhost:10443")

    def test_multiple_published_ports_require_explicit_inspection(self):
        current = gateway(8443)
        current["HostConfig"]["PortBindings"]["8443/tcp"].append({"HostIp": "::1", "HostPort": "8443"})
        with self.assertRaisesRegex(RequestError, "exactly one"):
            manager["binding"](current)


class PortTests(unittest.TestCase):
    def test_live_listener_blocks_start_unless_it_is_the_running_gateway_binding(self):
        with socket.socket() as listener:
            listener.bind(("127.0.0.1", 0))
            listener.listen()
            port = listener.getsockname()[1]
            config = {"listen": "127.0.0.1", "port": port}
            with self.assertRaisesRegex(RequestError, "Cannot bind"):
                manager["check_port"](config, None)
            with self.assertRaisesRegex(RequestError, "Cannot bind"):
                manager["check_port"](config, gateway(port))
            manager["check_port"](config, gateway(port, running=True))

    def test_probe_releases_an_available_port(self):
        with socket.socket() as reservation:
            reservation.bind(("127.0.0.1", 0))
            port = reservation.getsockname()[1]
        manager["check_port"]({"listen": "127.0.0.1", "port": port}, None)
        with socket.socket() as listener:
            listener.bind(("127.0.0.1", port))

    def test_ipv6_listener_blocks_the_same_ipv6_binding(self):
        with socket.socket(socket.AF_INET6) as listener:
            try:
                listener.bind(("::1", 0))
            except OSError as error:
                self.skipTest(str(error))
            listener.listen()
            with self.assertRaisesRegex(RequestError, "Cannot bind"):
                manager["check_port"]({"listen": "::1", "port": listener.getsockname()[1]}, None)


class ConnectionInstructionsTests(unittest.TestCase):
    def rows(self, config):
        return manager["connection_rows"](config)

    def test_tunnel_distinguishes_browser_server_and_ssh_ports(self):
        rows = self.rows({"origin": "https://localhost:9443", "listen": "0.0.0.0", "port": 18443,
                          "ssh_target": "owner@example.test", "ssh_port": 2222})
        self.assertEqual(rows[0][0], "Tunnel")
        self.assertFalse(rows[0][2], "a copyable command must not be wrapped")
        self.assertEqual(shlex.split(rows[0][1]), ["ssh", "-N", "-o", "ExitOnForwardFailure=yes", "-p", "2222",
                                                   "-L", "127.0.0.1:9443:127.0.0.1:18443", "owner@example.test"])
        self.assertEqual(rows[1][1], "run on your PC")

    def test_ipv6_tunnel_brackets_both_loopback_endpoints(self):
        rows = self.rows({"origin": "https://[::1]:9443", "listen": "::", "port": 18443})
        self.assertEqual(shlex.split(rows[0][1])[-2:], ["[::1]:9443:[::1]:18443", "USER@HOST"])
        self.assertIn("replacing USER@HOST", rows[1][1])

    def test_ipv6_ssh_target_omits_brackets_without_changing_tunnel_endpoints(self):
        rows = self.rows({"origin": "https://[::1]:9443", "listen": "::", "port": 18443,
                          "ssh_target": "owner@[::1]"})
        self.assertEqual(shlex.split(rows[0][1])[-2:], ["[::1]:9443:[::1]:18443", "owner@::1"])

    def test_public_browser_origin_reports_direct_access(self):
        rows = self.rows({"origin": "https://raven.example.test", "listen": "127.0.0.1", "port": 18443})
        self.assertEqual(rows, [("Access", "direct HTTPS; check DNS and network access for the browser URL", True)])


class TerminalTests(unittest.TestCase):
    def test_piped_output_is_plain_and_unwrapped(self):
        stream = io.StringIO()
        with redirect_stdout(stream):
            terminal = manager["Terminal"]()
            terminal.row("Label", "x " * 200, code="31")
        self.assertEqual(stream.getvalue(), "  Label     " + "x " * 200 + "\n")

    def test_interactive_output_wraps_prose_with_a_hanging_indent_but_not_commands(self):
        terminal = manager["Terminal"]()
        terminal.color, terminal.width = False, 40
        stream = io.StringIO()
        with redirect_stdout(stream):
            terminal.row("Label", "word " * 20)
            terminal.row("Command", "ssh " + "-o x " * 20, wrap=False)
        lines = stream.getvalue().splitlines()
        self.assertTrue(all(len(line) <= 42 for line in lines[:-1]), lines)
        self.assertTrue(all(line.startswith(" " * 12) for line in lines[1:-1]), lines)
        self.assertTrue(lines[-1].startswith("  Command   ssh -o x"))


if __name__ == "__main__":
    unittest.main()
