import json
import socketserver
import tempfile
import threading
from pathlib import Path

from django.conf import settings
from django.contrib.auth import get_user_model
from django.test import Client, TransactionTestCase, override_settings

ORIGIN = "https://localhost:8443"
PASSWORD = "Several unique test words 987"
GENERATION = "a" * 64
MiB, GiB = 1024 ** 2, 1024 ** 3
SESSION = {
    "id": "0", "generation": GENERATION, "name": "desktop", "state": "running", "wm": "xmonad",
    "geometry": "1280x800", "command": "xterm", "display": "100",
    "limits": {"memory": 2 * GiB, "cpus": 2.0, "server_memory": 31 * GiB, "server_cpus": 16},
    "memory": 512 * MiB, "cpu": 3.5, "viewers": {"x11": 1, "web": 1},
}


class LimitsBrokerHandler(socketserver.StreamRequestHandler):
    """Changes one session's limits like the host broker, or sends the reply the test set."""

    def handle(self):
        request = json.loads(self.rfile.readline())
        self.server.calls.append(request)
        if self.server.reply is not None:
            self.wfile.write(json.dumps(self.server.reply).encode() + b"\n")
            return
        session = self.server.session
        action = request["action"]
        if action == "list":
            return self.send({"ok": True, "data": [session]})
        if action == "limits":
            if request.get("generation") != session["generation"]:
                return self.send({"ok": False, "error": "Session changed; refresh the session list"})
            # Like the CLI on a server of 31 GiB and 16 cores
            if "memory" in request:
                memory = {"max": "100%"}.get(request["memory"], request["memory"])
                session["limits"]["memory"] = int(31 * GiB * float(memory[:-1]) / 100) // MiB * MiB \
                    if isinstance(memory, str) else memory
            if "cpus" in request:
                cpus = {"max": "100%"}.get(request["cpus"], request["cpus"])
                session["limits"]["cpus"] = round(16 * float(cpus[:-1]) / 100, 2) if isinstance(cpus, str) \
                    else float(cpus)
            return self.send({"ok": True, "data": session})
        if action == "create":
            limits = {"memory": request.get("memory", 2 * GiB), "cpus": request.get("cpus", 2.0)}
            return self.send({"ok": True, "data": {**session, "id": "1", "name": request["name"], "limits": limits}})
        self.send({"ok": False})

    def send(self, reply):
        self.wfile.write(json.dumps(reply).encode() + b"\n")


class FakeBroker(socketserver.ThreadingUnixStreamServer):
    daemon_threads = True


class LimitsApiTests(TransactionTestCase):
    def setUp(self):
        self.user = get_user_model().objects.create_user(username="owner", password=PASSWORD)
        self.temporary = tempfile.TemporaryDirectory(prefix="raven-limits-broker-")
        self.socket_path = str(Path(self.temporary.name) / "broker.sock")
        self.server = FakeBroker(self.socket_path, LimitsBrokerHandler)
        self.server.calls = []
        self.server.reply = None
        self.server.session = json.loads(json.dumps(SESSION))
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.config = override_settings(RAVEN_BROKER=self.socket_path)
        self.config.enable()
        self.client = Client(enforce_csrf_checks=True, HTTP_HOST="localhost:8443", HTTP_ORIGIN=ORIGIN)
        self.client.get("/login/", secure=True)
        self.csrf = self.client.cookies[settings.CSRF_COOKIE_NAME].value

    def tearDown(self):
        self.config.disable()
        self.server.shutdown()
        self.server.server_close()
        self.thread.join()
        self.temporary.cleanup()

    def authenticate(self):
        self.client.force_login(self.user)
        self.client.get("/", secure=True)
        self.csrf = self.client.cookies[settings.CSRF_COOKIE_NAME].value

    def post(self, path, payload, csrf=True, raw=None, **headers):
        if csrf:
            headers["HTTP_X_CSRFTOKEN"] = self.csrf
        return self.client.post(path, data=raw if raw is not None else json.dumps(payload),
                                content_type="application/json", secure=True, **headers)

    def assert_api_error(self, response, status, code, message=None):
        self.assertEqual(response.status_code, status, response.content)
        self.assertEqual(set(response.json()), {"error", "code"})
        self.assertEqual(response.json()["code"], code)
        if message is not None:
            self.assertEqual(response.json()["error"], message)

    def test_anonymous_and_cross_site_requests_never_reach_the_broker(self):
        payload = {"generation": GENERATION, "memory": 4 * GiB}
        self.assert_api_error(self.post("/api/sessions/0/limits/", payload), 401, "auth_required")
        self.authenticate()
        self.assert_api_error(self.post("/api/sessions/0/limits/", payload, csrf=False), 403, "csrf_token_invalid")
        self.assert_api_error(self.post("/api/sessions/0/limits/", payload, HTTP_ORIGIN="https://attacker.example"),
                              403, "csrf_origin_mismatch")
        self.assertEqual(self.server.calls, [])

    def test_invalid_requests_never_reach_the_broker(self):
        self.authenticate()
        path = "/api/sessions/0/limits/"
        response = self.client.get(path, secure=True)
        self.assert_api_error(response, 405, "method_not_allowed")
        self.assertEqual(response["Allow"], "POST")
        for payload in ({}, {"memory": 4 * GiB}, {"generation": GENERATION}, {"generation": "A" * 64, "cpus": 2},
                        {"generation": GENERATION, "cpus": 2, "pids": 100}, [], "text"):
            with self.subTest(payload=payload):
                self.assert_api_error(self.post(path, payload), 400, "invalid_request")
        for session_id in ("--help", "01", "1000000000"):
            self.assert_api_error(self.post(f"/api/sessions/{session_id}/limits/",
                                            {"generation": GENERATION, "cpus": 2}), 400, "invalid_request")
        for memory in (True, None, "4g", "MAX", "", 4.0 * GiB, 512 * MiB - 1, 511 * MiB, GiB + 1024,
                       1024 ** 5 + MiB, -GiB, [GiB], "0.5%", "101%", "100.01%", "25 %", "25%%", "%", "-5%", "05%",
                       "12.345%", "25%\n", "２5%"):
            with self.subTest(memory=memory):
                self.assert_api_error(self.post(path, {"generation": GENERATION, "memory": memory}), 400,
                                      "limit_invalid")
        for cpus in (True, None, "2", "Max", 0, 0.49, 1.234, 1025, -1, {"cores": 2}, "0%", "1000%", "x%", "50"):
            with self.subTest(cpus=cpus):
                self.assert_api_error(self.post(path, {"generation": GENERATION, "cpus": cpus}), 400, "limit_invalid")
        for constant in ("NaN", "Infinity"):
            with self.subTest(constant=constant):
                raw = '{"generation": "%s", "cpus": %s}' % (GENERATION, constant)
                self.assert_api_error(self.post(path, None, raw=raw), 400, "limit_invalid")
        self.assertEqual(self.server.calls, [])

    def test_limits_are_passed_to_the_broker_and_return_the_session(self):
        self.authenticate()
        response = self.post("/api/sessions/0/limits/", {"generation": GENERATION, "memory": 3 * GiB, "cpus": 2.5})
        self.assertEqual(response.status_code, 200, response.content)
        self.assertEqual(response.json()["session"]["limits"], {"memory": 3 * GiB, "cpus": 2.5,
                                                                "server_memory": 31 * GiB, "server_cpus": 16})
        self.assertEqual(self.server.calls[-1], {"action": "limits", "id": "0", "generation": GENERATION,
                                                 "memory": 3 * GiB, "cpus": 2.5})
        for payload in ({"memory": 512 * MiB}, {"cpus": 0.5}, {"cpus": 3}, {"memory": "max"}, {"cpus": "max"},
                        {"memory": 1024 ** 5, "cpus": 1024}, {"memory": "max", "cpus": "max"},
                        {"memory": "25%", "cpus": "12.5%"}, {"memory": "100%"}, {"cpus": "1%"},
                        {"memory": "99.99%"}):
            with self.subTest(payload=payload):
                response = self.post("/api/sessions/0/limits/", {"generation": GENERATION, **payload})
                self.assertEqual(response.status_code, 200, response.content)
                self.assertEqual(self.server.calls[-1], {"action": "limits", "id": "0", "generation": GENERATION,
                                                         **payload})
        limits = self.client.get("/api/sessions/", secure=True).json()["sessions"][0]["limits"]
        self.assertEqual(limits, {"memory": int(31 * GiB * 0.9999) // MiB * MiB, "cpus": 0.16,
                                  "server_memory": 31 * GiB, "server_cpus": 16})

    def test_broker_refusals_map_to_their_statuses(self):
        self.authenticate()
        cases = (
            ("limit_invalid", "Memory can be at most the server's memory, 31.3GiB", 400),
            ("limit_below_usage", "The memory limit must be at least 776MiB; the session uses 712MiB now", 409),
            ("session_paused", "Session is paused; its limits can only be raised until it is resumed", 409),
        )
        for code, message, status in cases:
            with self.subTest(code=code):
                self.server.reply = {"ok": False, "error": message, "code": code}
                response = self.post("/api/sessions/0/limits/", {"generation": GENERATION, "memory": 600 * MiB})
                self.assert_api_error(response, status, code, message)
        for code in ("limit_too_low", "", None, ["limit_invalid"]):
            with self.subTest(code=code):
                self.server.reply = {"ok": False, "error": "Refused", "code": code}
                response = self.post("/api/sessions/0/limits/", {"generation": GENERATION, "cpus": 1})
                self.assert_api_error(response, 502, "session_failed", "Refused")
        self.server.reply = None
        response = self.post("/api/sessions/0/limits/", {"generation": "b" * 64, "cpus": 1})
        self.assert_api_error(response, 502, "session_failed", "Session changed; refresh the session list")

    def test_creation_takes_optional_limits(self):
        self.authenticate()
        response = self.post("/api/sessions/", {"name": "Plain"})
        self.assertEqual(response.status_code, 201, response.content)
        self.assertEqual(self.server.calls[-1], {"action": "create", "name": "Plain", "wm": "xmonad",
                                                 "geometry": "1600x900", "command": "xterm"})
        response = self.post("/api/sessions/", {"name": "Big", "memory": 4 * GiB, "cpus": 1.5})
        self.assertEqual(response.status_code, 201, response.content)
        self.assertEqual(response.json()["session"]["limits"], {"memory": 4 * GiB, "cpus": 1.5})
        self.assertEqual(self.server.calls[-1], {"action": "create", "name": "Big", "wm": "xmonad",
                                                 "geometry": "1600x900", "command": "xterm",
                                                 "memory": 4 * GiB, "cpus": 1.5})
        response = self.post("/api/sessions/", {"name": "Most", "memory": "max", "cpus": "max"})
        self.assertEqual(response.status_code, 201, response.content)
        self.assertEqual((self.server.calls[-1]["memory"], self.server.calls[-1]["cpus"]), ("max", "max"))
        response = self.post("/api/sessions/", {"name": "Half", "memory": "50%", "cpus": "25%"})
        self.assertEqual(response.status_code, 201, response.content)
        self.assertEqual((self.server.calls[-1]["memory"], self.server.calls[-1]["cpus"]), ("50%", "25%"))
        calls = len(self.server.calls)
        for payload in ({"memory": "4g"}, {"memory": 100 * MiB}, {"cpus": 0.25}, {"cpus": None}, {"memory": "0%"},
                        {"cpus": "150%"}):
            with self.subTest(payload=payload):
                self.assert_api_error(self.post("/api/sessions/", {"name": "Bad", **payload}), 400, "limit_invalid")
        self.assertEqual(len(self.server.calls), calls)
        self.server.reply = {"ok": False, "error": "RAVEN_MEMORY must look like 512m, 1.5g, 4g, 25% or max",
                             "code": "limit_invalid"}
        self.assert_api_error(self.post("/api/sessions/", {"name": "Env"}), 400, "limit_invalid")
