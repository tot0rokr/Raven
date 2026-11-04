import asyncio
import json
import socketserver
import tempfile
import threading
from pathlib import Path
from unittest.mock import patch

from asgiref.sync import sync_to_async
from asgiref.testing import ApplicationCommunicator
from channels.testing import WebsocketCommunicator
from django.conf import settings
from django.contrib.auth import get_user_model
from django.contrib.auth.password_validation import validate_password
from django.contrib.sessions.models import Session
from django.core.exceptions import ValidationError
from django.test import Client, TransactionTestCase, override_settings
from django.utils import timezone
from uvicorn.protocols.utils import ClientDisconnected

from raven_web.asgi import application
from raven_web.broker import BrokerError, decode_password
from raven_web import throttle
from raven_web.consumers import MAX_CONNECTIONS, _connections

ORIGIN = "https://localhost:8443"
PASSWORD = "Several unique test words 987"
SESSION = {
    "id": "0",
    "generation": "a" * 64,
    "name": "desktop",
    "state": "running",
    "wm": "xmonad",
    "geometry": "1280x800",
    "command": "xterm",
    "display": 20,
}
MANAGEMENT_REQUESTS = (
    ("post", "/api/sessions/", {"name": "web-test"}),
    ("post", "/api/sessions/0/rename/", {"name": "renamed", "generation": SESSION["generation"]}),
    ("post", "/api/sessions/0/run/", {"command": "true", "generation": SESSION["generation"]}),
    ("delete", "/api/sessions/0/", {"confirm": "0", "generation": SESSION["generation"]}),
)


class BrokerHandler(socketserver.StreamRequestHandler):
    def handle(self):
        request = json.loads(self.rfile.readline())
        self.server.calls.append(request)
        action = request["action"]
        if action in ("rename", "run", "delete"):
            session = self.server.sessions.get(request["id"])
            if session is None or request.get("generation") != session["generation"]:
                self.wfile.write(b'{"ok":false}\n')
                return
        if action == "list":
            data = list(self.server.sessions.values())
        elif action == "create":
            session_id = str(self.server.next_id)
            self.server.next_id += 1
            data = {
                **SESSION, "id": session_id, "name": request["name"],
                "generation": f"{self.server.next_id:064x}",
                "wm": request.get("wm", "xmonad"),
                "geometry": request.get("geometry", "1600x900"),
                "command": request.get("command", "xterm"),
            }
            self.server.sessions[session_id] = data
        elif action == "rename":
            data = self.server.sessions[request["id"]]
            data["name"] = request["name"]
        elif action == "run":
            self.server.started.append(request.copy())
            data = None
        elif action == "delete":
            if request.get("confirm") != request["id"]:
                self.wfile.write(b'{"ok":false}\n')
                return
            del self.server.sessions[request["id"]]
            data = None
        elif action == "open":
            data = {"id": SESSION["id"], "name": SESSION["name"], "password_file": "29g8/XJ6FFg="}
        elif action == "connect":
            self.wfile.write(b'{"ok":true}\nRFB 003.008\n')
            while data := self.connection.recv(65536):
                self.connection.sendall(data)
            return
        else:
            self.wfile.write(b'{"ok":false}\n')
            return
        self.wfile.write(json.dumps({"ok": True, "data": data}).encode() + b"\n")


class FakeBroker(socketserver.ThreadingUnixStreamServer):
    daemon_threads = True


class SecurityTests(TransactionTestCase):
    def setUp(self):
        throttle._attempts = 0
        throttle._blocked_until = 0.0
        self.user = get_user_model().objects.create_user(username="owner", password=PASSWORD)
        self.temporary = tempfile.TemporaryDirectory(prefix="raven-broker-test-")
        self.socket_path = str(Path(self.temporary.name) / "broker.sock")
        self.server = FakeBroker(self.socket_path, BrokerHandler)
        self.server.calls = []
        self.server.started = []
        self.server.sessions = {SESSION["id"]: SESSION.copy()}
        self.server.next_id = 1
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

    def management_request(self, method, path, payload, csrf=True, **headers):
        if csrf:
            headers["HTTP_X_CSRFTOKEN"] = self.csrf
        return getattr(self.client, method)(
            path, data=json.dumps(payload), content_type="application/json", secure=True, **headers,
        )

    def test_anonymous_requests_never_reach_broker(self):
        response = self.client.get("/api/sessions/", secure=True)
        self.assertEqual(response.status_code, 401)
        response = self.client.post("/api/sessions/0/open/", secure=True, HTTP_X_CSRFTOKEN=self.csrf)
        self.assertEqual(response.status_code, 401)
        self.assertEqual(self.server.calls, [])

    def test_csrf_and_cross_origin_requests_are_rejected(self):
        self.authenticate()
        response = self.client.post("/api/sessions/0/open/", secure=True)
        self.assertEqual(response.status_code, 403)
        response = self.client.post(
            "/api/sessions/0/open/", secure=True,
            HTTP_X_CSRFTOKEN=self.csrf, HTTP_ORIGIN="https://attacker.example",
        )
        self.assertEqual(response.status_code, 403)
        self.assertEqual(self.server.calls, [])

    def test_invalid_session_id_never_reaches_broker(self):
        self.authenticate()
        response = self.client.post("/api/sessions/--help/open/", secure=True, HTTP_X_CSRFTOKEN=self.csrf)
        self.assertEqual(response.status_code, 400)
        self.assertEqual(self.server.calls, [])

    def test_authenticated_owner_lists_and_opens_desktop(self):
        self.authenticate()
        response = self.client.get("/api/sessions/", secure=True)
        self.assertEqual(response.json(), {"sessions": [SESSION]})
        response = self.client.post("/api/sessions/0/open/", secure=True, HTTP_X_CSRFTOKEN=self.csrf)
        self.assertEqual(response.json(), {
            "name": "desktop", "password": "password", "websocket": "/ws/sessions/0/",
        })
        self.assertEqual(response["Cache-Control"], "no-store")
        self.assertEqual(response["X-Frame-Options"], "DENY")

    def test_login_rotates_session_and_sets_secure_cookie(self):
        old_session = self.client.session.session_key
        response = self.client.post("/login/", {
            "username": "owner", "password": PASSWORD, "csrfmiddlewaretoken": self.csrf,
        }, secure=True)
        self.assertEqual(response.status_code, 302)
        cookie = response.cookies[settings.SESSION_COOKIE_NAME]
        self.assertTrue(cookie["secure"])
        self.assertTrue(cookie["httponly"])
        self.assertEqual(cookie["samesite"], "Strict")
        self.assertNotEqual(old_session, cookie.value)
        self.assertTrue(self.user.password.startswith("argon2$argon2id$"))

    def test_login_form_preserves_same_origin_referrer(self):
        response = self.client.get("/login/", secure=True)
        self.assertEqual(response["Referrer-Policy"], "same-origin")
        del self.client.defaults["HTTP_ORIGIN"]
        response = self.client.post("/login/", {
            "username": "owner", "password": PASSWORD, "csrfmiddlewaretoken": self.csrf,
        }, secure=True, HTTP_REFERER=ORIGIN + "/login/")
        self.assertEqual(response.status_code, 302)

    def test_opaque_login_origin_is_still_rejected(self):
        response = self.client.post("/login/", {
            "username": "owner", "password": PASSWORD, "csrfmiddlewaretoken": self.csrf,
        }, secure=True, HTTP_ORIGIN="null")
        self.assertEqual(response.status_code, 403)

    def test_authenticated_owner_manages_sessions(self):
        self.authenticate()
        fields = {"name": "web-test", "wm": "openbox", "geometry": "800x600", "command": "xterm"}
        response = self.management_request("post", "/api/sessions/", fields)
        self.assertEqual(response.status_code, 201)
        created = response.json()["session"]
        session_id = created["id"]
        generation = created["generation"]
        self.assertNotEqual(session_id, SESSION["id"])
        self.assertNotEqual(generation, SESSION["generation"])
        for key, value in fields.items():
            self.assertEqual(created[key], value)
        response = self.management_request(
            "post", f"/api/sessions/{session_id}/rename/", {"name": "renamed", "generation": generation},
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["session"]["name"], "renamed")
        self.assertEqual(response.json()["session"]["generation"], generation)
        response = self.management_request(
            "post", f"/api/sessions/{session_id}/run/", {"command": "printf hello", "generation": generation},
        )
        self.assertEqual(response.status_code, 202)
        self.assertEqual(response.json(), {"started": True})
        self.assertIn({"action": "run", "id": session_id, "command": "printf hello", "generation": generation}, self.server.started)
        response = self.management_request(
            "delete", f"/api/sessions/{session_id}/", {"confirm": session_id, "generation": generation},
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {"deleted": session_id})
        self.assertIn({"action": "delete", "id": session_id, "confirm": session_id, "generation": generation}, self.server.calls)
        self.assertEqual(self.client.get("/api/sessions/", secure=True).json(), {"sessions": [SESSION]})

    def test_create_defaults_and_supported_screen_bounds(self):
        self.authenticate()
        response = self.management_request("post", "/api/sessions/", {"name": "Defaults"})
        self.assertEqual(response.status_code, 201)
        created = response.json()["session"]
        self.assertEqual((created["wm"], created["geometry"], created["command"]), ("xmonad", "1600x900", "xterm"))
        for geometry in ("320x200", "7680x4320"):
            with self.subTest(geometry=geometry):
                response = self.management_request(
                    "post", "/api/sessions/", {"name": "Screen" + geometry.replace("x", "-"), "geometry": geometry},
                )
                self.assertEqual(response.status_code, 201)
                self.assertEqual(response.json()["session"]["geometry"], geometry)

    def test_anonymous_management_requests_never_reach_broker(self):
        for method, path, payload in MANAGEMENT_REQUESTS:
            with self.subTest(path=path, method=method):
                response = self.management_request(method, path, payload)
                self.assertEqual(response.status_code, 401)
        self.assertEqual(self.server.calls, [])

    def test_management_requires_csrf_and_same_origin(self):
        self.authenticate()
        for method, path, payload in MANAGEMENT_REQUESTS:
            with self.subTest(path=path, method=method):
                response = self.management_request(method, path, payload, csrf=False)
                self.assertEqual(response.status_code, 403)
                response = self.management_request(
                    method, path, payload, HTTP_ORIGIN="https://attacker.example",
                )
                self.assertEqual(response.status_code, 403)
        self.assertEqual(self.server.calls, [])

    def test_management_rejects_invalid_ids_and_unknown_fields(self):
        self.authenticate()
        for method, path, payload in MANAGEMENT_REQUESTS:
            with self.subTest(path=path, method=method):
                response = self.management_request(method, path, {**payload, "host": True})
                self.assertEqual(response.status_code, 400)
                if "/0/" in path:
                    for session_id in ("--help", "01", "1000000000"):
                        response = self.management_request(method, path.replace("/0/", f"/{session_id}/"), payload)
                        self.assertEqual(response.status_code, 400)
        self.assertEqual(self.server.calls, [])

    def test_management_accepts_only_json_objects(self):
        self.authenticate()
        for method, path, payload in MANAGEMENT_REQUESTS:
            with self.subTest(path=path, method=method):
                response = getattr(self.client, method)(
                    path, data=json.dumps(payload), content_type="text/plain", secure=True,
                    HTTP_X_CSRFTOKEN=self.csrf,
                )
                self.assertEqual(response.status_code, 400)
                response = getattr(self.client, method)(
                    path, data="{", content_type="application/json", secure=True,
                    HTTP_X_CSRFTOKEN=self.csrf,
                )
                self.assertEqual(response.status_code, 400)
                for value in ([], "text", None, 1):
                    response = self.management_request(method, path, value)
                    self.assertEqual(response.status_code, 400)
        self.assertEqual(self.server.calls, [])

    def test_invalid_creation_and_rename_fields_never_reach_broker(self):
        self.authenticate()
        invalid_names = ("", "1desktop", "-desktop", "../desktop", "한글", "x" * 65, "a b", True)
        for name in invalid_names:
            for path in ("/api/sessions/", "/api/sessions/0/rename/"):
                with self.subTest(name=name, path=path):
                    payload = {"name": name}
                    if path.endswith("/rename/"):
                        payload["generation"] = SESSION["generation"]
                    response = self.management_request("post", path, payload)
                    self.assertEqual(response.status_code, 400)
        invalid_fields = (
            {}, {"name": "Desktop", "wm": "other"}, {"name": "Desktop", "wm": None},
            *({"name": "Desktop", "geometry": value} for value in (
                "319x200", "320x199", "7681x4320", "7680x4321", "800X600", None,
            )),
        )
        for fields in invalid_fields:
            with self.subTest(fields=fields):
                response = self.management_request("post", "/api/sessions/", fields)
                self.assertEqual(response.status_code, 400)
        self.assertEqual(self.server.calls, [])

    def test_invalid_commands_and_delete_confirmation_never_reach_broker(self):
        self.authenticate()
        for action in ("run", "rename"):
            response = self.management_request("post", f"/api/sessions/0/{action}/", {"generation": SESSION["generation"]})
            self.assertEqual(response.status_code, 400)
        for command in ("", " \t\n", "x" * 4097, "x\0y", ["xterm"], None):
            response = self.management_request("post", "/api/sessions/0/run/", {"command": command, "generation": SESSION["generation"]})
            self.assertEqual(response.status_code, 400)
            response = self.management_request("post", "/api/sessions/", {"name": "Desktop", "command": command})
            self.assertEqual(response.status_code, 400)
        for payload in ({}, {"confirm": "1"}, {"confirm": 0}, {"confirm": "00"}):
            response = self.management_request("delete", "/api/sessions/0/", {**payload, "generation": SESSION["generation"]})
            self.assertEqual(response.status_code, 400)
        self.assertEqual(self.server.calls, [])

    def test_missing_or_malformed_generation_never_reaches_broker(self):
        self.authenticate()
        for method, path, payload in MANAGEMENT_REQUESTS[1:]:
            with self.subTest(path=path):
                without_generation = {key: value for key, value in payload.items() if key != "generation"}
                response = self.management_request(method, path, without_generation)
                self.assertEqual(response.status_code, 400)
                for generation in (None, 0, "", "a" * 63, "a" * 65, "A" * 64, "g" * 64):
                    response = self.management_request(method, path, {**payload, "generation": generation})
                    self.assertEqual(response.status_code, 400)
        self.assertEqual(self.server.calls, [])

    def test_stale_generation_cannot_mutate_reused_session_id(self):
        self.authenticate()
        replacement = {**SESSION, "generation": "b" * 64}
        self.server.sessions[SESSION["id"]] = replacement.copy()
        for method, path, payload in MANAGEMENT_REQUESTS[1:]:
            with self.subTest(path=path):
                response = self.management_request(method, path, payload)
                self.assertEqual(response.status_code, 502)
                self.assertEqual(self.server.sessions, {SESSION["id"]: replacement})
        self.assertEqual(self.server.started, [])

    def test_unconfigured_host_is_rejected(self):
        response = self.client.get("/api/sessions/", secure=True, HTTP_HOST="localhost:9999")
        self.assertEqual(response.status_code, 400)
        self.assertEqual(self.server.calls, [])

    def test_vnc_password_file_is_validated(self):
        self.assertEqual(decode_password("29g8/XJ6FFg="), "password")
        for value in ("!", "eA=="):
            with self.assertRaises(BrokerError):
                decode_password(value)

    def test_eight_character_password_is_accepted_and_seven_is_rejected(self):
        password = "Rvn#8xQ!"
        validate_password(password, self.user)
        with self.assertRaises(ValidationError) as rejected:
            validate_password(password[:-1], self.user)
        self.assertIn("password_too_short", [error.code for error in rejected.exception.error_list])
        self.user.set_password(password)
        self.user.save(update_fields=["password"])
        response = self.client.post("/login/", {
            "username": "owner", "password": password, "csrfmiddlewaretoken": self.csrf,
        }, secure=True)
        self.assertEqual(response.status_code, 302)

    def test_every_five_login_attempts_require_a_full_minute_cooldown(self):
        data = {"username": "owner", "password": "incorrect", "csrfmiddlewaretoken": self.csrf}
        with patch("time.monotonic") as clock:
            for now in (0, 10, 20, 30, 40):
                clock.return_value = now
                self.assertEqual(self.client.post("/login/", data, secure=True).status_code, 401)
            for now, remaining in ((40.1, "60"), (60, "40"), (99.9, "1")):
                clock.return_value = now
                response = self.client.post("/login/", data, secure=True)
                self.assertEqual(response.status_code, 429)
                self.assertEqual(response["Retry-After"], remaining)
            clock.return_value = 100
            for _ in range(5):
                self.assertEqual(self.client.post("/login/", data, secure=True).status_code, 401)
            response = self.client.post("/login/", data, secure=True)
            self.assertEqual(response.status_code, 429)
            self.assertEqual(response["Retry-After"], "60")
            clock.return_value = 160
            self.assertEqual(self.client.post("/login/", data, secure=True).status_code, 401)

    def websocket(self, path="/ws/sessions/0/", origin=ORIGIN):
        headers = [(b"host", b"localhost:8443"), (b"origin", origin.encode())]
        cookie = self.client.cookies.get(settings.SESSION_COOKIE_NAME)
        if cookie:
            headers.append((b"cookie", f"{settings.SESSION_COOKIE_NAME}={cookie.value}".encode()))
        connection = WebsocketCommunicator(application, path, headers=headers, subprotocols=["binary"])
        connection.scope["scheme"] = "wss"
        return connection

    async def connected_desktop(self):
        await sync_to_async(self.authenticate)()
        connection = self.websocket()
        accepted, protocol = await connection.connect()
        self.assertTrue(accepted)
        self.assertEqual(protocol, "binary")
        self.assertEqual(await connection.receive_from(), b"RFB 003.008\n")
        return connection

    async def test_websocket_requires_login_and_exact_origin(self):
        connection = self.websocket()
        self.assertFalse((await connection.connect())[0])
        await connection.disconnect()
        await sync_to_async(self.authenticate)()
        for path, origin in (
            ("/ws/sessions/0/", "https://attacker.example"),
            ("/ws/sessions/--help/", ORIGIN),
            ("/ws/sessions/0/?password=secret", ORIGIN),
        ):
            connection = self.websocket(path, origin)
            self.assertFalse((await connection.connect())[0])
            await connection.disconnect()
        self.assertEqual(self.server.calls, [])

    async def test_server_refuses_startup_without_an_account(self):
        await sync_to_async(get_user_model().objects.all().delete)()
        server = ApplicationCommunicator(application, {"type": "lifespan"})
        await server.send_input({"type": "lifespan.startup"})
        response = await server.receive_output()
        self.assertEqual(response["type"], "lifespan.startup.failed")
        await server.wait()

    async def test_authenticated_websocket_forwards_binary_data(self):
        connection = await self.connected_desktop()
        await connection.send_to(bytes_data=b"client input")
        self.assertEqual(await connection.receive_from(), b"client input")
        await connection.disconnect()

    async def test_disconnect_during_accept_releases_the_connection_slot(self):
        await sync_to_async(self.authenticate)()
        cookie = self.client.cookies[settings.SESSION_COOKIE_NAME].value
        try:
            for _ in range(MAX_CONNECTIONS):
                incoming = asyncio.Queue()
                incoming.put_nowait({"type": "websocket.connect"})
                scope = {
                    "type": "websocket", "scheme": "wss", "path": "/ws/sessions/0/",
                    "query_string": b"", "subprotocols": ["binary"],
                    "headers": [(b"host", b"localhost:8443"), (b"origin", ORIGIN.encode()),
                                (b"cookie", f"{settings.SESSION_COOKIE_NAME}={cookie}".encode())],
                }

                async def send(message):
                    if message["type"] == "websocket.accept":
                        raise ClientDisconnected("The client disconnected during upgrade")

                with self.assertRaises(ClientDisconnected):
                    await asyncio.wait_for(application(scope, incoming.get, send), timeout=5)
                self.assertEqual(len(_connections), 0)
            connection = self.websocket()
            self.assertTrue((await connection.connect())[0])
            self.assertEqual(await connection.receive_from(), b"RFB 003.008\n")
            await connection.disconnect()
        finally:
            await asyncio.gather(*(connection.cleanup() for connection in list(_connections)))

    async def test_logout_closes_existing_websocket(self):
        connection = await self.connected_desktop()
        response = await sync_to_async(self.client.post)(
            "/logout/", secure=True, HTTP_X_CSRFTOKEN=self.csrf,
        )
        self.assertEqual(response.status_code, 302)
        closed = await connection.receive_output(timeout=1)
        self.assertEqual(closed, {"type": "websocket.close", "code": 4401})
        await connection.disconnect()
        connection = self.websocket()
        self.assertFalse((await connection.connect())[0])
        await connection.disconnect()

    async def test_expired_session_closes_existing_websocket(self):
        connection = await self.connected_desktop()
        await sync_to_async(Session.objects.update)(expire_date=timezone.now())
        closed = await connection.receive_output(timeout=3)
        self.assertEqual(closed["code"], 4401)
        await connection.disconnect()

    async def test_password_change_closes_existing_websocket(self):
        connection = await self.connected_desktop()
        self.user.set_unusable_password()
        await sync_to_async(self.user.save)(update_fields=["password"])
        closed = await connection.receive_output(timeout=3)
        self.assertEqual(closed["code"], 4401)
        await connection.disconnect()

    async def test_oversized_websocket_message_is_rejected(self):
        connection = await self.connected_desktop()
        await connection.send_to(bytes_data=b"x" * (1024 * 1024 + 1))
        closed = await connection.receive_output()
        self.assertEqual(closed["code"], 1009)
        await connection.disconnect()

    async def test_simultaneous_desktop_connections_are_bounded(self):
        await sync_to_async(self.authenticate)()
        connections = []
        try:
            for _ in range(8):
                connection = self.websocket()
                self.assertTrue((await connection.connect())[0])
                self.assertEqual(await connection.receive_from(), b"RFB 003.008\n")
                connections.append(connection)
            rejected = self.websocket()
            self.assertEqual(await rejected.connect(), (False, 4429))
            await rejected.disconnect()
        finally:
            for connection in connections:
                await connection.disconnect()
