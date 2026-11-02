import json
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from raven_web_host import Raven, RequestError


class HostBoundaryTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="raven-host-test-")
        self.addCleanup(self.temporary.cleanup)
        self.raven = Raven(Path(__file__).resolve().parents[1] / "raven")
        self.raven.state = Path(self.temporary.name)
        self.raven.runtime = self.raven.state / "runtime"
        self.raven.runtime.mkdir()
        self.item = {
            "Id": "a" * 64, "Name": f"/raven-{self.raven.user}-0",
            "State": {"Status": "running", "Running": True},
            "Config": {"Labels": {"raven.user": self.raven.user, "raven.id": "0",
                                   "raven.display": "100", "raven.port": "15900",
                                   "raven.geometry": "800x600", "raven.wm": "openbox", "raven.cmd": "xterm"}},
        }
        self.process = patch("subprocess.run", side_effect=self.process_result).start()
        self.addCleanup(patch.stopall)

    def process_result(self, arguments, **kwargs):
        if arguments[:2] == ["docker", "inspect"]:
            output = json.dumps([self.item]).encode()
            return subprocess.CompletedProcess(arguments, 0, output, b"")
        return subprocess.CompletedProcess(arguments, 0, b"", b"raven: created session 0 (test) with openbox\n")

    def test_management_rejects_another_owners_session(self):
        self.item["Config"]["Labels"]["raven.user"] = "another-user"
        for action in ("open", "rename", "delete", "run"):
            with self.subTest(action=action), self.assertRaises(RequestError):
                self.raven.dispatch({"action": action, "id": "0", "name": "test", "command": "xterm",
                                     "confirm": "0", "generation": self.item["Id"]})
        self.assertTrue(all(call.args[0][:2] == ["docker", "inspect"] for call in self.process.call_args_list))

    def test_management_rejects_invalid_ids_before_running_any_process(self):
        for sid in ("--host", "../0", "00", "0; id", 0, None):
            with self.subTest(sid=sid), self.assertRaises(RequestError):
                self.raven.dispatch({"action": "run", "id": sid, "command": "xterm"})
        self.process.assert_not_called()

    def test_deletion_requires_the_exact_confirmation(self):
        for confirmation in (None, "1", 0):
            with self.subTest(confirmation=confirmation), self.assertRaises(RequestError):
                self.raven.dispatch({"action": "delete", "id": "0", "confirm": confirmation})
        self.process.assert_not_called()

    def test_commands_are_only_interpreted_inside_the_owned_container(self):
        for command in ("@Terminal", "--host xterm", "printf ok; echo $(id)"):
            self.raven.dispatch({"action": "run", "id": "0", "command": command, "generation": self.item["Id"]})
            arguments = self.process.call_args.args[0]
            self.assertEqual(arguments[:3], ["docker", "exec", "-d"])
            self.assertEqual(arguments[-4:], [self.item["Id"], "sh", "-c", "umask 002; " + command])
            self.assertNotIn("shell", self.process.call_args.kwargs)
        cli_calls = [call.args[0] for call in self.process.call_args_list if call.args[0][0] == self.raven.cli]
        self.assertTrue(all(arguments[1:3] == ["menu", "show"] for arguments in cli_calls))

    def test_creation_does_not_interpret_the_command_as_host_options(self):
        command = "--host sh -c 'echo not-a-host-command'"
        self.raven.dispatch({"action": "create", "name": "test", "command": command})
        create_call = self.process.call_args_list[0].args[0]
        self.assertEqual(create_call[:3], [self.raven.cli, "new", "-d"])
        self.assertEqual(create_call[-2:], ["--", command])

    def test_invalid_creation_fields_never_start_a_process(self):
        for changes in ({"name": "bad;name"}, {"geometry": "9999x9999"}, {"wm": []},
                        {"command": "\0"}, {"command": "  "}):
            with self.subTest(changes=changes), self.assertRaises(RequestError):
                self.raven.dispatch({"action": "create", "name": "test", **changes})
        self.process.assert_not_called()

    def test_reused_numeric_id_rejects_stale_management_requests(self):
        for action in ("rename", "run", "delete"):
            with self.subTest(action=action), self.assertRaisesRegex(RequestError, "Session changed"):
                self.raven.dispatch({"action": action, "id": "0", "name": "test", "command": "xterm",
                                     "confirm": "0", "generation": "b" * 64})
        self.assertTrue(all(call.args[0][:2] == ["docker", "inspect"] for call in self.process.call_args_list))

    def test_run_rechecks_generation_after_startup(self):
        inspections = 0

        def replace_during_startup(arguments, **kwargs):
            nonlocal inspections
            if arguments[:2] == ["docker", "inspect"]:
                inspections += 1
                if inspections == 2:
                    self.item["Id"] = "b" * 64
            return self.process_result(arguments, **kwargs)

        self.process.side_effect = replace_during_startup
        with self.assertRaisesRegex(RequestError, "Session changed"):
            self.raven.dispatch({"action": "run", "id": "0", "command": "xterm", "generation": "a" * 64})
        self.assertFalse(any(call.args[0][:2] == ["docker", "exec"] for call in self.process.call_args_list))

    def test_delete_pins_container_and_releases_volume_after_metadata(self):
        paths = [self.raven.state / "0.name", self.raven.state / "0.home",
                 self.raven.runtime / "0.xauth", self.raven.runtime / "0.log"]
        for path in paths:
            path.write_text("test metadata")

        def check_volume_barrier(arguments, **kwargs):
            if arguments[:3] == ["docker", "volume", "rm"]:
                self.assertTrue(all(not path.exists() for path in paths))
            return self.process_result(arguments, **kwargs)

        self.process.side_effect = check_volume_barrier
        self.raven.dispatch({"action": "delete", "id": "0", "confirm": "0", "generation": self.item["Id"]})
        operations = [call.args[0] for call in self.process.call_args_list]
        self.assertEqual(operations[1], ["docker", "rm", "-f", self.item["Id"]])
        self.assertEqual(operations[2], ["docker", "volume", "rm", f"raven-{self.raven.user}-home-0"])


if __name__ == "__main__":
    unittest.main()
