import json
import socketserver
import tempfile
import threading
from pathlib import Path

from asgiref.sync import sync_to_async
from channels.testing import WebsocketCommunicator
from django.conf import settings
from django.contrib.auth import get_user_model
from django.test import Client, TransactionTestCase, override_settings

from raven_web import broker
from raven_web.asgi import application
from raven_web.consumers import PAUSED_CLOSE_CODE, _connections

ORIGIN = "https://localhost:8443"
PASSWORD = "Several unique test words 987"
GENERATION = "a" * 64
SESSION = {
    "id": "0", "generation": GENERATION, "name": "desktop", "state": "running", "wm": "xmonad",
    "geometry": "1280x800", "command": "xterm", "display": "100",
    "memory": 512 * 1024 ** 2, "cpu": 3.5, "viewers": {"x11": 1, "web": 1},
}
STATE_REFUSALS = {
    "session_paused": "Session is paused; resume it first",
    "session_not_paused": "Session is not paused",
    "session_not_running": "Session is not running",
}


class PauseBrokerHandler(socketserver.StreamRequestHandler):
    """Keeps one session's state the way the host broker does and refuses actions by it."""

    def handle(self):
        request = json.loads(self.rfile.readline())
        self.server.calls.append(request)
        if self.server.reply is not None:
            self.wfile.write(self.server.reply)
            return
        session = self.server.session
        action = request["action"]
        if action in ("pause", "resume", "run") and request.get("generation") != session["generation"]:
            return self.send({"ok": False, "error": "Session changed; refresh the session list"})
        if action == "list":
            return self.send({"ok": True, "data": [session]})
        if action == "pause":
            if session["state"] == "paused":
                return self.refuse("session_paused", "Session is already paused")
            if session["state"] != "running":
                return self.refuse("session_not_running")
            session.update(state="paused", cpu=0.0, viewers={"x11": 0, "web": 0})
            return self.send({"ok": True, "data": session})
        if action == "resume":
            if session["state"] != "paused":
                return self.refuse("session_not_paused")
            session.update(state="running")
            return self.send({"ok": True, "data": session})
        if session["state"] == "paused" and action in ("open", "run", "connect"):
            return self.refuse("session_paused")
        if action == "open":
            return self.send({"ok": True, "data": {"id": "0", "name": "desktop", "password_file": "29g8/XJ6FFg="}})
        if action == "run":
            return self.send({"ok": True, "data": None})
        if action == "connect":
            self.wfile.write(b'{"ok":true}\nRFB 003.008\n')
            while data := self.connection.recv(65536):
                self.connection.sendall(data)
            return
        self.send({"ok": False})

    def send(self, reply):
        self.wfile.write(json.dumps(reply).encode() + b"\n")

    def refuse(self, code, message=None):
        self.send({"ok": False, "error": message or STATE_REFUSALS[code], "code": code})


class FakeBroker(socketserver.ThreadingUnixStreamServer):
    daemon_threads = True


class PauseApiTests(TransactionTestCase):
    def setUp(self):
        self.user = get_user_model().objects.create_user(username="owner", password=PASSWORD)
        self.temporary = tempfile.TemporaryDirectory(prefix="raven-pause-broker-")
        self.socket_path = str(Path(self.temporary.name) / "broker.sock")
        self.server = FakeBroker(self.socket_path, PauseBrokerHandler)
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

    def post(self, path, payload, csrf=True, **headers):
        if csrf:
            headers["HTTP_X_CSRFTOKEN"] = self.csrf
        return self.client.post(path, data=json.dumps(payload), content_type="application/json", secure=True,
                                **headers)

    def assert_api_error(self, response, status, code, message=None):
        self.assertEqual(response.status_code, status, response.content)
        self.assertEqual(set(response.json()), {"error", "code"})
        self.assertEqual(response.json()["code"], code)
        if message is not None:
            self.assertEqual(response.json()["error"], message)

    def test_anonymous_and_cross_site_requests_never_reach_the_broker(self):
        for action in ("pause", "resume"):
            with self.subTest(action=action):
                response = self.post(f"/api/sessions/0/{action}/", {"generation": GENERATION})
                self.assert_api_error(response, 401, "auth_required")
        self.authenticate()
        for action in ("pause", "resume"):
            with self.subTest(action=action):
                response = self.post(f"/api/sessions/0/{action}/", {"generation": GENERATION}, csrf=False)
                self.assert_api_error(response, 403, "csrf_token_invalid")
                response = self.post(f"/api/sessions/0/{action}/", {"generation": GENERATION},
                                     HTTP_ORIGIN="https://attacker.example")
                self.assert_api_error(response, 403, "csrf_origin_mismatch")
        self.assertEqual(self.server.calls, [])

    def test_invalid_requests_never_reach_the_broker(self):
        self.authenticate()
        for action in ("pause", "resume"):
            path = f"/api/sessions/0/{action}/"
            with self.subTest(action=action):
                response = self.client.get(path, secure=True)
                self.assert_api_error(response, 405, "method_not_allowed")
                self.assertEqual(response["Allow"], "POST")
                for payload in ({}, {"generation": None}, {"generation": "a" * 63}, {"generation": "A" * 64},
                                {"generation": GENERATION, "force": True}, [], "text"):
                    response = self.post(path, payload)
                    self.assert_api_error(response, 400, "invalid_request")
                response = self.client.post(path, data="{", content_type="application/json", secure=True,
                                            HTTP_X_CSRFTOKEN=self.csrf)
                self.assert_api_error(response, 400, "invalid_request")
                response = self.client.post(path, data=json.dumps({"generation": GENERATION}),
                                            content_type="text/plain", secure=True, HTTP_X_CSRFTOKEN=self.csrf)
                self.assert_api_error(response, 400, "invalid_request")
                for session_id in ("--help", "01", "1000000000"):
                    response = self.post(f"/api/sessions/{session_id}/{action}/", {"generation": GENERATION})
                    self.assert_api_error(response, 400, "invalid_request")
        self.assertEqual(self.server.calls, [])

    def test_pause_and_resume_return_the_session(self):
        self.authenticate()
        response = self.post("/api/sessions/0/pause/", {"generation": GENERATION})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["session"]["state"], "paused")
        self.assertEqual(response.json()["session"]["memory"], SESSION["memory"])
        self.assertEqual(self.server.calls[-1], {"action": "pause", "id": "0", "generation": GENERATION})
        response = self.post("/api/sessions/0/resume/", {"generation": GENERATION})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["session"]["state"], "running")
        self.assertEqual(self.server.calls[-1], {"action": "resume", "id": "0", "generation": GENERATION})

    def test_list_passes_the_resource_fields_through(self):
        self.authenticate()
        response = self.client.get("/api/sessions/", secure=True)
        self.assertEqual(response.json(), {"sessions": [SESSION]})
        self.server.session.update(memory=None, cpu=None, viewers=None)
        session = self.client.get("/api/sessions/", secure=True).json()["sessions"][0]
        self.assertEqual((session["memory"], session["cpu"], session["viewers"]), (None, None, None))

    def test_state_refusals_are_conflicts_with_their_codes(self):
        self.authenticate()
        self.assertEqual(self.post("/api/sessions/0/pause/", {"generation": GENERATION}).status_code, 200)
        response = self.post("/api/sessions/0/pause/", {"generation": GENERATION})
        self.assert_api_error(response, 409, "session_paused", "Session is already paused")
        response = self.client.post("/api/sessions/0/open/", secure=True, HTTP_X_CSRFTOKEN=self.csrf)
        self.assert_api_error(response, 409, "session_paused", STATE_REFUSALS["session_paused"])
        response = self.post("/api/sessions/0/run/", {"command": "xterm", "generation": GENERATION})
        self.assert_api_error(response, 409, "session_paused", STATE_REFUSALS["session_paused"])
        self.assertEqual(self.post("/api/sessions/0/resume/", {"generation": GENERATION}).status_code, 200)
        response = self.post("/api/sessions/0/resume/", {"generation": GENERATION})
        self.assert_api_error(response, 409, "session_not_paused", STATE_REFUSALS["session_not_paused"])
        self.server.session["state"] = "exited"
        response = self.post("/api/sessions/0/pause/", {"generation": GENERATION})
        self.assert_api_error(response, 409, "session_not_running", STATE_REFUSALS["session_not_running"])

    def test_stale_generation_and_unknown_codes_stay_session_failed(self):
        self.authenticate()
        for action in ("pause", "resume"):
            with self.subTest(action=action):
                response = self.post(f"/api/sessions/0/{action}/", {"generation": "b" * 64})
                self.assert_api_error(response, 502, "session_failed", "Session changed; refresh the session list")
        self.assertEqual(self.server.session["state"], "running")
        for code in ("session_running", "auth_required", "", None, ["session_paused"]):
            with self.subTest(code=code):
                self.server.reply = json.dumps({"ok": False, "error": "Refused", "code": code}).encode() + b"\n"
                response = self.post("/api/sessions/0/pause/", {"generation": GENERATION})
                self.assert_api_error(response, 502, "session_failed", "Refused")

    def websocket(self):
        cookie = self.client.cookies[settings.SESSION_COOKIE_NAME].value
        connection = WebsocketCommunicator(application, "/ws/sessions/0/", subprotocols=["binary"], headers=[
            (b"host", b"localhost:8443"), (b"origin", ORIGIN.encode()),
            (b"cookie", f"{settings.SESSION_COOKIE_NAME}={cookie}".encode()),
        ])
        connection.scope["scheme"] = "wss"
        return connection

    async def test_websocket_to_a_paused_session_closes_with_its_own_code(self):
        await sync_to_async(self.authenticate)()
        self.server.session["state"] = "paused"
        connection = self.websocket()
        # Accepted first: a close before the handshake would reach the browser only as 1006.
        self.assertEqual(await connection.connect(), (True, "binary"))
        self.assertEqual(await connection.receive_output(timeout=3), {"type": "websocket.close", "code": 4409})
        self.assertEqual(PAUSED_CLOSE_CODE, 4409)
        await connection.disconnect()
        self.assertEqual(len(_connections), 0)
        self.server.session["state"] = "running"
        connection = self.websocket()
        self.assertTrue((await connection.connect())[0])
        self.assertEqual(await connection.receive_from(), b"RFB 003.008\n")
        await connection.disconnect()

    async def test_other_broker_refusals_keep_the_generic_close_code(self):
        await sync_to_async(self.authenticate)()
        for code in ("session_not_running", "session_failed", None):
            with self.subTest(code=code):
                reply = {"ok": False, "error": "Refused"}
                if code is not None:
                    reply["code"] = code
                self.server.reply = json.dumps(reply).encode() + b"\n"
                connection = self.websocket()
                self.assertEqual(await connection.connect(), (False, 4502))
                await connection.disconnect()
        self.assertEqual(len(_connections), 0)

    async def test_broker_connect_keeps_only_session_state_codes(self):
        for reply, code in (
            ({"ok": False, "error": "x", "code": "session_paused"}, "session_paused"),
            ({"ok": False, "error": "x", "code": "session_not_running"}, "session_not_running"),
            ({"ok": False, "error": "x", "code": "not_found"}, "broker_unavailable"),
            ({"ok": False, "error": "x"}, "broker_unavailable"),
        ):
            with self.subTest(reply=reply):
                self.server.reply = json.dumps(reply).encode() + b"\n"
                with self.assertRaises(broker.BrokerError) as caught:
                    await broker.connect("0")
                self.assertEqual(caught.exception.code, code)
                self.assertEqual(str(caught.exception), "The session connection failed")
