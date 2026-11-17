"""Host broker: pause/resume, refusals of paused sessions and the resource fields of list."""

import copy
import json
from pathlib import Path
import socket
import subprocess
import tempfile
import threading
import unittest
from unittest.mock import patch

import raven_web_host
from raven_web_host import CPU_SAMPLE_SECONDS, Files, Raven, RequestError, Server, established_ports

SOURCE = Path(__file__).resolve().parents[1]
GENERATION = "a" * 64


def tcp_row(local, remote, state):
    return f"   0: {local} {remote} {state} 00000000:00000000 00:00000000 00000000 60000 0 1 1 0 20 4 30 10 -1"


def tcp_table(rows):
    return "  sl  local_address rem_address   st tx_queue rx_queue tr tm->when retrnsmt   uid  timeout inode\n" + \
        "\n".join(rows) + "\n"


class BrokerFixture(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="raven-pause-test-")
        self.addCleanup(self.temporary.cleanup)
        root = Path(self.temporary.name)
        self.raven = Raven(SOURCE / "raven")
        self.raven.state = root / "state"
        self.raven.state.mkdir()
        self.raven.runtime = root / "runtime"
        self.raven.runtime.mkdir()
        self.raven.proc = root / "proc"
        self.raven.cgroups = root / "cgroup"
        (self.raven.proc / "net").mkdir(parents=True)
        self.raven.cgroups.mkdir()
        self.items = {}
        self.process = patch("subprocess.run", side_effect=self.process_result).start()
        self.addCleanup(patch.stopall)

    def add(self, sid, status, generation=None, pid=None):
        generation = generation or f"{int(sid) + 10:064x}"
        item = {
            "Id": generation, "Name": f"/raven-{self.raven.user}-{sid}",
            "State": {"Status": status, "Running": status in ("running", "paused"),
                      "Paused": status == "paused", "Pid": pid or 0},
            "Config": {"Labels": {"raven.user": self.raven.user, "raven.id": sid,
                                   "raven.display": str(100 + int(sid)), "raven.port": str(15900 + int(sid)),
                                   "raven.geometry": "800x600", "raven.wm": "xmonad", "raven.cmd": "xterm"}},
        }
        self.items[sid] = item
        return item

    def process_result(self, arguments, **kwargs):
        if arguments[:2] == ["docker", "ps"]:
            output = "\n".join(item["Id"] for item in self.items.values()).encode()
            return subprocess.CompletedProcess(arguments, 0, output, b"")
        if arguments[:2] == ["docker", "inspect"]:
            target = arguments[-1]
            for sid, item in self.items.items():
                if target in (item["Id"], f"raven-{self.raven.user}-{sid}"):
                    return subprocess.CompletedProcess(arguments, 0, json.dumps([item]).encode(), b"")
            return subprocess.CompletedProcess(arguments, 1, b"[]", b"No such container")
        if arguments[0] == self.raven.cli and arguments[1] in ("pause", "resume"):
            item = self.items[arguments[-1]]
            item["State"]["Status"] = "paused" if arguments[1] == "pause" else "running"
            item["State"]["Paused"] = arguments[1] == "pause"
        return subprocess.CompletedProcess(arguments, 0, b"", b"")

    def commands(self):
        return [call.args[0] for call in self.process.call_args_list]

    def assert_only_inspected(self):
        self.assertTrue(all(arguments[:2] in (["docker", "inspect"], ["docker", "ps"])
                            for arguments in self.commands()), self.commands())

    def assert_code(self, code, request):
        with self.assertRaises(RequestError) as caught:
            self.raven.dispatch(request)
        self.assertEqual(caught.exception.code, code)
        return caught.exception


class PauseActionTests(BrokerFixture):
    def test_pause_and_resume_run_the_cli_and_return_the_session(self):
        item = self.add("0", "running", GENERATION)
        session = self.raven.dispatch({"action": "pause", "id": "0", "generation": GENERATION})
        self.assertIn([self.raven.cli, "pause", "-t", "0"], self.commands())
        self.assertEqual((session["state"], session["generation"]), ("paused", item["Id"]))
        self.assertEqual(session["cpu"], 0.0)
        self.assertEqual(session["viewers"], {"x11": 0, "web": 0})
        session = self.raven.dispatch({"action": "resume", "id": "0", "generation": GENERATION})
        self.assertIn([self.raven.cli, "resume", "-t", "0"], self.commands())
        self.assertEqual(session["state"], "running")

    def test_state_refusals_carry_codes_and_never_run_the_cli(self):
        self.add("0", "paused", GENERATION)
        error = self.assert_code("session_paused", {"action": "pause", "id": "0", "generation": GENERATION})
        self.assertEqual(str(error), "Session is already paused")
        self.add("0", "running", GENERATION)
        self.assert_code("session_not_paused", {"action": "resume", "id": "0", "generation": GENERATION})
        for status in ("exited", "created", "restarting"):
            with self.subTest(status=status):
                self.add("0", status, GENERATION)
                self.assert_code("session_not_running", {"action": "pause", "id": "0", "generation": GENERATION})
                self.assert_code("session_not_paused", {"action": "resume", "id": "0", "generation": GENERATION})
        self.assert_only_inspected()

    def test_pause_and_resume_require_the_current_generation(self):
        self.add("0", "running", GENERATION)
        for action in ("pause", "resume"):
            for generation in ("b" * 64, None, 1, "a" * 65):
                with self.subTest(action=action, generation=generation), self.assertRaises(RequestError) as caught:
                    self.raven.dispatch({"action": action, "id": "0", "generation": generation})
                self.assertIsNone(caught.exception.code)
        self.assert_only_inspected()

    def test_pause_rejects_another_owners_session_and_bad_ids(self):
        self.add("0", "running", GENERATION)["Config"]["Labels"]["raven.user"] = "another-user"
        for action in ("pause", "resume"):
            with self.subTest(action=action), self.assertRaisesRegex(RequestError, "not owned"):
                self.raven.dispatch({"action": action, "id": "0", "generation": GENERATION})
            for sid in ("--host", "00", "0; id", 0):
                with self.subTest(action=action, sid=sid), self.assertRaises(RequestError):
                    self.raven.dispatch({"action": action, "id": sid, "generation": GENERATION})
        self.assert_only_inspected()

    def test_a_generation_change_during_pause_is_reported(self):
        self.add("0", "running", GENERATION)
        original = self.process.side_effect

        def replace(arguments, **kwargs):
            result = original(arguments, **kwargs)
            if arguments[:2] == [self.raven.cli, "pause"]:
                self.items["0"]["Id"] = "b" * 64
            return result

        self.process.side_effect = replace
        with self.assertRaisesRegex(RequestError, "Session changed"):
            self.raven.dispatch({"action": "pause", "id": "0", "generation": GENERATION})


class PausedRefusalTests(BrokerFixture):
    def test_open_and_run_refuse_a_paused_session_before_starting_anything(self):
        self.add("0", "paused", GENERATION)
        for request in ({"action": "open", "id": "0"},
                        {"action": "run", "id": "0", "command": "xterm", "generation": GENERATION}):
            with self.subTest(action=request["action"]):
                self.assert_code("session_paused", request)
        self.assert_only_inspected()

    def test_open_refuses_a_session_paused_while_it_was_brought_up(self):
        self.add("0", "running", GENERATION)
        original = self.process.side_effect

        def pause_during_startup(arguments, **kwargs):
            if arguments[:2] == [self.raven.cli, "menu"]:
                self.items["0"]["State"]["Status"] = "paused"
            return original(arguments, **kwargs)

        self.process.side_effect = pause_during_startup
        for request in ({"action": "open", "id": "0"},
                        {"action": "run", "id": "0", "command": "xterm", "generation": GENERATION}):
            with self.subTest(action=request["action"]):
                self.items["0"]["State"]["Status"] = "running"
                self.assert_code("session_paused", request)
        self.assertFalse(any(arguments[:2] == ["docker", "exec"] for arguments in self.commands()))

    def test_connect_refuses_paused_and_stopped_sessions_without_a_connection(self):
        with patch("socket.create_connection") as connection:
            self.add("0", "paused", GENERATION)
            with self.assertRaises(RequestError) as caught:
                self.raven.connect({"action": "connect", "id": "0"})
            self.assertEqual(caught.exception.code, "session_paused")
            self.add("0", "exited", GENERATION)
            with self.assertRaises(RequestError) as caught:
                self.raven.connect({"action": "connect", "id": "0"})
            self.assertEqual(caught.exception.code, "session_not_running")
            connection.assert_not_called()
        self.assert_only_inspected()

    def test_rename_and_delete_still_work_while_paused(self):
        self.add("0", "paused", GENERATION)
        session = self.raven.dispatch({"action": "rename", "id": "0", "name": "kept", "generation": GENERATION})
        self.assertEqual(session["state"], "paused")
        self.raven.dispatch({"action": "delete", "id": "0", "confirm": "0", "generation": GENERATION})
        self.assertIn(["docker", "rm", "-f", GENERATION], self.commands())


class ListUsageTests(BrokerFixture):
    def cgroup(self, sid, pid, memory=None, usage=None, processes=()):
        item = self.items[sid]
        path = f"system.slice/docker-{item['Id']}.scope"
        (self.raven.proc / str(pid)).mkdir()
        (self.raven.proc / str(pid) / "cgroup").write_text(f"0::/{path}\n")
        group = self.raven.cgroups / path
        group.mkdir(parents=True)
        if memory is not None:
            (group / "memory.current").write_text(f"{memory}\n")
        if usage is not None:
            (group / "cpu.stat").write_text(f"usage_usec {usage}\nuser_usec 1\nsystem_usec 2\n")
        pids = [pid]
        for child, name in processes:
            (self.raven.proc / str(child)).mkdir()
            (self.raven.proc / str(child) / "comm").write_text(name + "\n")
            pids.append(child)
        (self.raven.proc / str(pid) / "comm").write_text("docker-init\n")
        (group / "cgroup.procs").write_text("".join(f"{value}\n" for value in pids))
        return group

    def connections(self, ipv4=(), ipv6=()):
        (self.raven.proc / "net" / "tcp").write_text(tcp_table(ipv4))
        (self.raven.proc / "net" / "tcp6").write_text(tcp_table(ipv6))

    def listing(self, usage_after=None, clock=(100.0, 100.25)):
        """List sessions; usage_after maps a cgroup to the cpu.stat usage it reports after the sample sleep."""

        def sleep(seconds):
            self.assertEqual(seconds, CPU_SAMPLE_SECONDS)
            for group, usage in (usage_after or {}).items():
                (group / "cpu.stat").write_text(f"usage_usec {usage}\n")

        with patch.object(raven_web_host.time, "sleep", side_effect=sleep) as slept, \
                patch.object(raven_web_host.time, "monotonic", side_effect=list(clock)):
            sessions = self.raven.dispatch({"action": "list"})
        return {session["id"]: session for session in sessions}, slept

    def test_list_reports_memory_cpu_and_viewers_from_cgroups_and_proc(self):
        self.add("0", "running", pid=500)
        self.add("1", "paused", pid=600)
        self.add("2", "exited")
        running = self.cgroup("0", 500, memory=300 * 1024 ** 2, usage=1_000_000,
                              processes=((501, "Xvnc"), (502, "xtigervncviewer"), (503, "xterm")))
        self.cgroup("1", 600, memory=1536 * 1024 ** 2, usage=7_000_000,
                    processes=((601, "xtightvncviewer"),))
        self.connections(
            ipv4=[tcp_row("0100007F:3E1C", "0100007F:A000", "01"),  # 15900, the X11 viewer
                  tcp_row("0100007F:3E1C", "0100007F:A001", "01"),  # 15900, a web viewer
                  tcp_row("0100007F:3E1C", "0100007F:A002", "06"),  # closing: not counted
                  tcp_row("0100007F:A000", "0100007F:3E1C", "01"),  # client side: other port
                  tcp_row("00000000:3E1C", "00000000:0000", "0A")],  # listening
            ipv6=[tcp_row("00000000000000000000000001000000:3E1C",
                          "00000000000000000000000001000000:B000", "01")],  # 15900, another web viewer
        )
        sessions, slept = self.listing({running: 1_050_000})
        self.assertEqual(slept.call_count, 1)
        self.assertEqual(sessions["0"]["memory"], 300 * 1024 ** 2)
        self.assertEqual(sessions["0"]["cpu"], 20.0)
        self.assertEqual(sessions["0"]["viewers"], {"x11": 1, "web": 2})
        self.assertEqual(sessions["1"]["state"], "paused")
        self.assertEqual(sessions["1"]["memory"], 1536 * 1024 ** 2)
        self.assertEqual(sessions["1"]["cpu"], 0.0)
        self.assertEqual(sessions["1"]["viewers"], {"x11": 0, "web": 0})
        self.assertEqual((sessions["2"]["memory"], sessions["2"]["cpu"]), (None, None))
        self.assertEqual(sessions["2"]["viewers"], {"x11": 0, "web": 0})
        self.assert_only_inspected()

    def test_read_failures_become_null_without_failing_the_list(self):
        self.add("0", "running", pid=500)
        self.add("1", "running", pid=600)
        self.add("2", "running", pid=700)
        self.add("3", "paused", pid=800)
        self.cgroup("0", 500, memory=None, usage=None, processes=((501, "xtigervncviewer"),))
        group = self.cgroup("1", 600, memory=10, usage=5)
        (group / "cgroup.procs").unlink()
        # PID 700 was reused by a process outside this container.
        (self.raven.proc / "700").mkdir()
        (self.raven.proc / "700" / "cgroup").write_text("0::/user.slice/other.scope\n")
        # Session 3 is paused and its init process is missing from /proc.
        self.connections()
        sessions, _ = self.listing({group: 30})
        self.assertEqual(sessions["0"]["memory"], None)
        self.assertEqual(sessions["0"]["cpu"], None)
        self.assertEqual(sessions["0"]["viewers"], {"x11": 1, "web": 0})
        self.assertEqual(sessions["1"]["memory"], 10)
        self.assertEqual(sessions["1"]["cpu"], 0.0)
        self.assertEqual(sessions["1"]["viewers"], None)
        self.assertEqual((sessions["2"]["memory"], sessions["2"]["cpu"], sessions["2"]["viewers"]),
                         (None, None, None))
        self.assertEqual((sessions["3"]["memory"], sessions["3"]["cpu"], sessions["3"]["viewers"]),
                         (None, 0.0, {"x11": 0, "web": 0}))

    def test_unreadable_connection_table_only_drops_the_viewer_counts(self):
        self.add("0", "running", pid=500)
        group = self.cgroup("0", 500, memory=1, usage=0)
        sessions, _ = self.listing({group: 25_000})
        self.assertEqual((sessions["0"]["memory"], sessions["0"]["cpu"], sessions["0"]["viewers"]), (1, 10.0, None))
        (self.raven.proc / "net" / "tcp").write_text(tcp_table([tcp_row("0100007F:ZZZZ", "0100007F:1234", "01")]))
        sessions, _ = self.listing({group: 25_000})
        self.assertEqual(sessions["0"]["viewers"], None)

    def test_no_cpu_sample_without_a_running_session(self):
        self.add("0", "paused", pid=500)
        self.add("1", "exited")
        self.cgroup("0", 500, memory=42, usage=9)
        sessions, slept = self.listing(clock=())
        slept.assert_not_called()
        self.assertEqual((sessions["0"]["memory"], sessions["0"]["cpu"]), (42, 0.0))

    def test_cgroup_paths_outside_the_cgroup_root_are_ignored(self):
        self.add("0", "running", pid=500)
        (self.raven.proc / "500").mkdir()
        (self.raven.proc / "500" / "cgroup").write_text(
            f"0::/system.slice/../../docker-{self.items['0']['Id']}.scope\n")
        sessions, slept = self.listing(clock=())
        slept.assert_not_called()
        self.assertEqual(sessions["0"]["memory"], None)

    def test_established_ports_parses_ipv4_and_ipv6(self):
        tables = [tcp_table([tcp_row("0100007F:3E1F", "0100007F:1234", "01"),
                             tcp_row("0100007F:3E1F", "0100007F:1235", "01")]),
                  tcp_table([tcp_row("00000000000000000000000001000000:3E1F",
                                     "00000000000000000000000001000000:1236", "01")])]
        self.assertEqual(established_ports(tables), {15903: 3})


class ReplyCodeTests(BrokerFixture):
    def setUp(self):
        super().setUp()
        self.address = str(Path(self.temporary.name) / "broker.sock")
        self.server = Server(self.address, self.raven, Files(Path(self.temporary.name) / "Downloads"))
        thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(thread.join)
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)

    def exchange(self, request):
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as connection:
            connection.settimeout(10)
            connection.connect(self.address)
            connection.sendall(json.dumps(request).encode() + b"\n")
            with connection.makefile("rb") as response:
                return json.loads(response.readline())

    def test_replies_carry_session_codes_and_plain_failures_none(self):
        self.add("0", "paused", GENERATION)
        self.assertEqual(self.exchange({"action": "open", "id": "0"}),
                         {"ok": False, "error": "Session is paused; resume it first", "code": "session_paused"})
        self.assertEqual(self.exchange({"action": "connect", "id": "0"}),
                         {"ok": False, "error": "Session is paused; resume it first", "code": "session_paused"})
        self.assertEqual(self.exchange({"action": "resume", "id": "0", "generation": "b" * 64}),
                         {"ok": False, "error": "Session changed; refresh the session list"})
        self.add("0", "running", GENERATION)
        self.assertEqual(self.exchange({"action": "resume", "id": "0", "generation": GENERATION}),
                         {"ok": False, "error": "Session is not paused", "code": "session_not_paused"})
        self.add("0", "exited", GENERATION)
        self.assertEqual(self.exchange({"action": "pause", "id": "0", "generation": GENERATION}),
                         {"ok": False, "error": "Session is not running", "code": "session_not_running"})


if __name__ == "__main__":
    unittest.main()
