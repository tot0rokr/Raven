"""Host broker: memory and CPU limits in the session list, the limits action and limits on create."""

import json
from pathlib import Path
import socket
import subprocess
import threading
import unittest
from unittest.mock import patch

import raven_web_host
from raven_web_host import Files, RequestError, Server
from test_web_pause import BrokerFixture, GENERATION

MiB, GiB = 1024 ** 2, 1024 ** 3
# The broker host: MemTotal 32862272 kB and 16 cores
SERVER = {"server_memory": 32862272 * 1024, "server_cpus": 16}
BELOW_USAGE = (b"raven: session 0 uses 712MiB of memory now; a limit below 776MiB (its use + 64MiB) would make the"
               b" kernel kill its apps\n")


class LimitsFixture(BrokerFixture):
    def setUp(self):
        super().setUp()
        self.cli_result = (0, b"")
        (self.raven.proc / "meminfo").write_text("MemTotal:       32862272 kB\nMemFree:        1234 kB\n")
        patch.object(raven_web_host.os, "sched_getaffinity", return_value=set(range(16))).start()

    def add(self, sid, status, generation=None, pid=None, memory=2 * GiB, nano=2_000_000_000):
        item = super().add(sid, status, generation, pid)
        item["HostConfig"] = {"Memory": memory, "MemorySwap": memory, "NanoCpus": nano, "PidsLimit": 512}
        return item

    def process_result(self, arguments, **kwargs):
        if arguments[0] == self.raven.cli and arguments[1] in ("limit", "new"):
            status, stderr = self.cli_result
            if status == 0 and arguments[1] == "limit":
                host = self.items[arguments[3]]["HostConfig"]
                options = dict(zip(arguments[4::2], arguments[5::2]))
                # Like the CLI on this fake server: max and shares resolve against MemTotal and 16 cores
                if "-m" in options:
                    value = {"max": "100%"}.get(options["-m"], options["-m"])
                    host["Memory"] = (int(SERVER["server_memory"] * float(value[:-1]) / 100) // MiB * MiB
                                      if value.endswith("%") else
                                      int(value[:-1]) * (MiB if value.endswith("m") else 1024))
                if "-c" in options:
                    value = {"max": "100%"}.get(options["-c"], options["-c"])
                    cores = round(16 * float(value[:-1]) / 100, 2) if value.endswith("%") else float(value)
                    host["NanoCpus"] = round(cores * 1e9)
            if status == 0 and arguments[1] == "new":
                self.add("5", "running", "e" * 64)
                stderr = b"raven: created session 5 (test) with xmonad; memory limit 2GiB, 2 cores\n"
            return subprocess.CompletedProcess(arguments, status, b"", stderr)
        return super().process_result(arguments, **kwargs)

    def cli_calls(self, verb):
        return [arguments for arguments in self.commands() if arguments[0] == self.raven.cli and arguments[1] == verb]


class ListLimitTests(LimitsFixture):
    def test_list_reports_each_containers_limits(self):
        self.add("0", "running", memory=3 * GiB, nano=2_500_000_000)
        self.add("1", "exited", memory=0, nano=0)
        self.add("2", "paused", memory=1536 * MiB, nano=500_000_000)
        del self.add("3", "exited")["HostConfig"]
        sessions = {session["id"]: session for session in self.raven.dispatch({"action": "list"})}
        self.assertEqual(sessions["0"]["limits"], {"memory": 3 * GiB, "cpus": 2.5, **SERVER})
        self.assertEqual(sessions["1"]["limits"], {"memory": None, "cpus": None, **SERVER})
        self.assertEqual(sessions["2"]["limits"], {"memory": 1536 * MiB, "cpus": 0.5, **SERVER})
        self.assertEqual(sessions["3"]["limits"], {"memory": None, "cpus": None, **SERVER})
        json.dumps(sessions)
        self.assert_only_inspected()

    def test_unreadable_server_totals_are_null(self):
        self.add("0", "running", memory=3 * GiB, nano=2_500_000_000)
        for text in (None, "MemFree: 1 kB\n", "MemTotal: lots kB\n", "MemTotal: 5 MB\n"):
            with self.subTest(text=text):
                if text is None:
                    (self.raven.proc / "meminfo").unlink()
                else:
                    (self.raven.proc / "meminfo").write_text(text)
                session, = self.raven.dispatch({"action": "list"})
                self.assertEqual(session["limits"], {"memory": 3 * GiB, "cpus": 2.5, "server_memory": None,
                                                     "server_cpus": 16})


class LimitsActionTests(LimitsFixture):
    def limits(self, **values):
        return self.raven.dispatch({"action": "limits", "id": "0", "generation": GENERATION, **values})

    def test_limits_run_the_cli_and_return_the_changed_session(self):
        self.add("0", "running", GENERATION)
        session = self.limits(memory=3 * GiB, cpus=2.5)
        self.assertEqual(self.cli_calls("limit"), [[self.raven.cli, "limit", "-t", "0", "-m", "3072m", "-c", "2.5"]])
        self.assertEqual((session["id"], session["generation"]), ("0", GENERATION))
        self.assertEqual(session["limits"], {"memory": 3 * GiB, "cpus": 2.5, **SERVER})
        for values, options in (({"memory": 1536 * MiB}, ["-m", "1536m"]),
                                ({"memory": 1536 * MiB + 1024}, ["-m", "1572865k"]),
                                ({"cpus": 2}, ["-c", "2"]), ({"cpus": 0.75}, ["-c", "0.75"]),
                                ({"cpus": 1.1}, ["-c", "1.1"]), ({"cpus": 2.0}, ["-c", "2"]),
                                ({"cpus": 0.29}, ["-c", "0.29"]),
                                ({"memory": "max", "cpus": "max"}, ["-m", "max", "-c", "max"]),
                                ({"memory": "25%", "cpus": "12.5%"}, ["-m", "25%", "-c", "12.5%"]),
                                ({"memory": "100%", "cpus": "1%"}, ["-m", "100%", "-c", "1%"]),
                                ({"cpus": "99.99%"}, ["-c", "99.99%"]),
                                ({"memory": None, "cpus": 1}, ["-c", "1"])):
            with self.subTest(values=values):
                self.process.reset_mock()
                self.limits(**values)
                self.assertEqual(self.cli_calls("limit"), [[self.raven.cli, "limit", "-t", "0", *options]])

    def test_invalid_values_never_run_the_cli(self):
        self.add("0", "running", GENERATION)
        for values in ({}, {"memory": None, "cpus": None}, {"memory": True}, {"memory": "2g"}, {"memory": 1.5 * GiB},
                       {"memory": 0}, {"memory": -GiB}, {"memory": GiB + 1}, {"memory": 1 << 61}, {"memory": "MAX"},
                       {"cpus": "2"}, {"cpus": True}, {"cpus": float("nan")}, {"cpus": float("inf")}, {"cpus": 0},
                       {"cpus": -1}, {"cpus": 1.234}, {"cpus": 10 ** 6}, {"cpus": [2]},
                       {"memory": "0.5%"}, {"memory": "101%"}, {"memory": "100.5%"}, {"memory": "25 %"},
                       {"memory": "25%%"}, {"memory": "%"}, {"memory": "-5%"}, {"memory": "05%"},
                       {"cpus": "12.345%"}, {"cpus": "0%"}, {"cpus": "50%\n"}, {"cpus": "x%"}):
            with self.subTest(values=values):
                error = self.assert_code("limit_invalid", {"action": "limits", "id": "0", "generation": GENERATION,
                                                           **values})
                self.assertTrue(str(error))
        self.assert_only_inspected()

    def test_limits_require_the_current_generation(self):
        self.add("0", "running", GENERATION)
        for generation in ("b" * 64, None, 1):
            with self.subTest(generation=generation), self.assertRaisesRegex(RequestError, "Session changed|Invalid"):
                self.raven.dispatch({"action": "limits", "id": "0", "generation": generation, "memory": 3 * GiB})
        self.add("0", "running", GENERATION)["Config"]["Labels"]["raven.user"] = "another-user"
        with self.assertRaisesRegex(RequestError, "not owned"):
            self.limits(memory=3 * GiB)
        self.assert_only_inspected()

    def test_cli_refusals_become_codes(self):
        self.add("0", "running", GENERATION)
        cases = (
            ((3, b"raven: memory must be at least 512MiB: '100m'\n"), "limit_invalid", "Memory must be at least 512MiB"),
            ((3, b"raven: memory can be at most the server's memory, 31.3GiB: '64g'\n"), "limit_invalid",
             "Memory can be at most the server's memory, 31.3GiB"),
            ((3, b"raven: cpus can be at most the server's 16 cores: '32'\n"), "limit_invalid",
             "Cpus can be at most the server's 16 cores"),
            ((3, b"\xff\n"), "limit_invalid", "Invalid memory or CPU limit"),
            ((4, BELOW_USAGE), "limit_below_usage",
             "The memory limit must be at least 776MiB; the session uses 712MiB now"),
            ((4, b"raven: something else\n"), "limit_below_usage",
             "The memory limit is below what the session uses now"),
            ((5, b"raven: session 0 is paused; its limits can only be raised until it is resumed: raven resume 0\n"),
             "session_paused", "Session is paused; its limits can only be raised until it is resumed"),
        )
        for result, code, message in cases:
            with self.subTest(result=result):
                self.cli_result = result
                error = self.assert_code(code, {"action": "limits", "id": "0", "generation": GENERATION,
                                                "memory": 600 * MiB})
                self.assertEqual(str(error), message)
        for status in (1, 2, 127):
            with self.subTest(status=status):
                self.cli_result = (status, b"raven: cannot read the memory use of session 0\n")
                error = self.assert_code(None, {"action": "limits", "id": "0", "generation": GENERATION,
                                                "memory": 600 * MiB})
                self.assertEqual(str(error), "Raven operation failed; inspect the session with the CLI")


class CreateLimitTests(LimitsFixture):
    def create(self, **values):
        return self.raven.dispatch({"action": "create", "name": "test", **values})

    def test_create_passes_limits_to_raven_new(self):
        self.create()
        self.assertEqual(self.cli_calls("new")[-1], [self.raven.cli, "new", "-d", "-s", "test", "-g", "1600x900",
                                                     "-w", "xmonad", "--", "xterm"])
        session = self.create(memory=GiB, cpus=1.5, command="--host xterm")
        self.assertEqual(self.cli_calls("new")[-1], [self.raven.cli, "new", "-d", "-s", "test", "-g", "1600x900",
                                                     "-w", "xmonad", "-m", "1024m", "-c", "1.5", "--", "--host xterm"])
        self.assertEqual(session["limits"], {"memory": 2 * GiB, "cpus": 2.0, **SERVER})
        self.create(memory="max", cpus="max")
        self.assertEqual(self.cli_calls("new")[-1][9:13], ["-m", "max", "-c", "max"])
        self.create(memory="50%", cpus="25%")
        self.assertEqual(self.cli_calls("new")[-1][9:13], ["-m", "50%", "-c", "25%"])

    def test_create_refuses_bad_limits(self):
        for values in ({"memory": "lots"}, {"cpus": 0.125}, {"memory": True}, {"memory": "200%"}):
            with self.subTest(values=values):
                self.assert_code("limit_invalid", {"action": "create", "name": "test", **values})
        self.assertEqual(self.commands(), [])
        self.cli_result = (3, b"raven: RAVEN_MEMORY must look like 512m, 1.5g, 4g, 25% or max: 'lots'\n")
        error = self.assert_code("limit_invalid", {"action": "create", "name": "test"})
        self.assertEqual(str(error), "RAVEN_MEMORY must look like 512m, 1.5g, 4g, 25% or max")
        self.cli_result = (1, b"raven: name 'test' is used by session 2\n")
        self.assert_code(None, {"action": "create", "name": "test", "memory": GiB})


class LimitReplyTests(LimitsFixture):
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

    def test_replies_carry_the_limit_codes(self):
        self.add("0", "running", GENERATION)
        request = {"action": "limits", "id": "0", "generation": GENERATION, "memory": 600 * MiB}
        self.cli_result = (4, BELOW_USAGE)
        self.assertEqual(self.exchange(request), {
            "ok": False, "code": "limit_below_usage",
            "error": "The memory limit must be at least 776MiB; the session uses 712MiB now"})
        self.assertEqual(self.exchange(dict(request, memory="1g")),
                         {"ok": False, "code": "limit_invalid", "error": 'Memory must be a number of bytes, a share like "25%" or "max"'})
        self.cli_result = (0, b"")
        reply = self.exchange(dict(request, memory=3 * GiB, cpus="max"))
        self.assertTrue(reply["ok"], reply)
        self.assertEqual(reply["data"]["limits"]["memory"], 3 * GiB)


if __name__ == "__main__":
    unittest.main()
