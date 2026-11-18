import io
import json
import socketserver
import tempfile
import threading
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import patch

from asgiref.sync import sync_to_async
from channels.testing import WebsocketCommunicator
from django.conf import settings
from django.contrib.auth import get_user_model
from django.contrib.auth import HASH_SESSION_KEY, SESSION_KEY
from django.contrib.sessions.backends.db import SessionStore
from django.contrib.sessions.models import Session
from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import Client, TransactionTestCase, override_settings
from django.utils import timezone

from raven_web import throttle, views
from raven_web.asgi import application
from raven_web.logins import LOGIN_KEY, classify, legacy_id

ORIGIN = "https://localhost:8443"
PASSWORD = "Several unique test words 987"
CHROME_WINDOWS = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) "
                  "Chrome/131.0.0.0 Safari/537.36")
FIREFOX_ANDROID = "Mozilla/5.0 (Android 14; Mobile; rv:133.0) Gecko/133.0 Firefox/133.0"
USER_AGENTS = (
    (CHROME_WINDOWS, ("Chrome", "Windows", "desktop")),
    (CHROME_WINDOWS + " Edg/131.0.0.0", ("Edge", "Windows", "desktop")),
    (CHROME_WINDOWS + " OPR/115.0.0.0", ("Opera", "Windows", "desktop")),
    ("Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:133.0) Gecko/20100101 Firefox/133.0",
     ("Firefox", "Windows", "desktop")),
    ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/18.1 "
     "Safari/605.1.15", ("Safari", "macOS", "desktop")),
    ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 "
     "Safari/537.36", ("Chrome", "macOS", "desktop")),
    ("Mozilla/5.0 (iPhone; CPU iPhone OS 18_1 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) "
     "Version/18.1 Mobile/15E148 Safari/604.1", ("Safari", "iOS", "mobile")),
    ("Mozilla/5.0 (iPhone; CPU iPhone OS 18_1 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) "
     "CriOS/131.0.6778.73 Mobile/15E148 Safari/604.1", ("Chrome", "iOS", "mobile")),
    ("Mozilla/5.0 (iPad; CPU OS 17_7 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) FxiOS/133.0 "
     "Mobile/15E148 Safari/605.1.15", ("Firefox", "iOS", "mobile")),
    ("Mozilla/5.0 (iPhone; CPU iPhone OS 18_1 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) "
     "EdgiOS/131.0.2903.68 Version/18.0 Mobile/15E148 Safari/604.1", ("Edge", "iOS", "mobile")),
    ("Mozilla/5.0 (Linux; Android 10; K) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Mobile "
     "Safari/537.36", ("Chrome", "Android", "mobile")),
    ("Mozilla/5.0 (Linux; Android 14; SM-S918B) AppleWebKit/537.36 (KHTML, like Gecko) SamsungBrowser/26.0 "
     "Chrome/122.0.0.0 Mobile Safari/537.36", ("Samsung Internet", "Android", "mobile")),
    ("Mozilla/5.0 (Linux; Android 10; K) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36 "
     "EdgA/131.0.0.0", ("Edge", "Android", "mobile")),
    (FIREFOX_ANDROID, ("Firefox", "Android", "mobile")),
    ("Mozilla/5.0 (X11; Linux x86_64; rv:133.0) Gecko/20100101 Firefox/133.0", ("Firefox", "Linux", "desktop")),
    ("Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) HeadlessChrome/131.0.0.0 "
     "Safari/537.36", ("Chrome", "Linux", "desktop")),
    ("Mozilla/5.0 (X11; CrOS x86_64 14541.0.0) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 "
     "Safari/537.36", ("Chrome", "ChromeOS", "desktop")),
    ("curl/8.5.0", (None, None, "desktop")),
    ("", (None, None, "desktop")),
)
FIELDS = {"id", "browser", "os", "device", "created", "seen", "expires", "current", "connections"}


class EchoBroker(socketserver.ThreadingUnixStreamServer):
    daemon_threads = True


class EchoHandler(socketserver.StreamRequestHandler):
    """A broker that only connects desktops: the greeting, then everything echoed back."""

    def handle(self):
        if json.loads(self.rfile.readline())["action"] != "connect":
            self.wfile.write(b'{"ok":false}\n')
            return
        self.wfile.write(b'{"ok":true}\nRFB 003.008\n')
        while data := self.connection.recv(65536):
            self.connection.sendall(data)


def stored(session_key):
    return Session.objects.get(session_key=session_key)


def login_data(session_key):
    return stored(session_key).get_decoded()[LOGIN_KEY]


class LoginTests(TransactionTestCase):
    def setUp(self):
        throttle._attempts = 0
        throttle._blocked_until = 0.0
        self.user = get_user_model().objects.create_user(username="owner", password=PASSWORD)
        self.temporary = tempfile.TemporaryDirectory(prefix="raven-logins-broker-")
        socket_path = str(Path(self.temporary.name) / "broker.sock")
        self.server = EchoBroker(socket_path, EchoHandler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.config = override_settings(RAVEN_BROKER=socket_path)
        self.config.enable()
        self.revocations = patch.object(views, "revoke_session", wraps=views.revoke_session)
        self.revoke_session = self.revocations.start()

    def tearDown(self):
        self.revocations.stop()
        self.config.disable()
        self.server.shutdown()
        self.server.server_close()
        self.thread.join()
        self.temporary.cleanup()

    def browser(self, user_agent=CHROME_WINDOWS):
        client = Client(enforce_csrf_checks=True, HTTP_HOST="localhost:8443", HTTP_ORIGIN=ORIGIN,
                        HTTP_USER_AGENT=user_agent)
        client.get("/login/", secure=True)
        client.csrf = client.cookies[settings.CSRF_COOKIE_NAME].value
        return client

    def login(self, user_agent=CHROME_WINDOWS):
        client = self.browser(user_agent)
        response = client.post("/login/", {"username": "owner", "password": PASSWORD,
                                           "csrfmiddlewaretoken": client.csrf}, secure=True)
        self.assertEqual(response.status_code, 302)
        client.key = client.cookies[settings.SESSION_COOKIE_NAME].value
        client.csrf = client.cookies[settings.CSRF_COOKIE_NAME].value
        client.login_id = login_data(client.key)["id"]
        return client

    def legacy_login(self):
        """A login from before login metadata existed: force_login stores none."""
        client = self.browser()
        client.force_login(self.user)
        client.key = client.cookies[settings.SESSION_COOKIE_NAME].value
        client.login_id = legacy_id(client.key)
        return client

    @staticmethod
    def logins(client):
        response = client.get("/api/logins/", secure=True)
        assert response.status_code == 200, response.content
        return response.json()["logins"]

    @staticmethod
    def post(client, path, payload=None, csrf=True, **headers):
        if csrf:
            headers["HTTP_X_CSRFTOKEN"] = client.csrf
        if payload is None:
            return client.post(path, secure=True, **headers)
        return client.post(path, data=json.dumps(payload), content_type="application/json", secure=True, **headers)

    def assert_api_error(self, response, status, code):
        self.assertEqual(response.status_code, status, response.content)
        self.assertEqual(set(response.json()), {"error", "code"})
        self.assertEqual(response.json()["code"], code)

    def assert_logged_out(self, response):
        cookie = response.cookies[settings.SESSION_COOKIE_NAME]
        self.assertEqual(cookie.value, "")
        self.assertEqual(cookie["max-age"], 0)
        self.assertTrue(cookie["secure"])

    def test_user_agent_families(self):
        for user_agent, expected in USER_AGENTS:
            with self.subTest(user_agent=user_agent):
                self.assertEqual(classify(user_agent), expected)

    def test_login_records_only_coarse_metadata_and_an_absolute_expiry(self):
        before = timezone.now()
        client = self.login(FIREFOX_ANDROID)
        row = stored(client.key)
        data = row.get_decoded()
        login = data[LOGIN_KEY]
        self.assertEqual(set(login), {"id", "created", "seen", "browser", "os", "device"})
        self.assertRegex(login["id"], r"\A[0-9a-f]{16}\Z")
        self.assertEqual((login["browser"], login["os"], login["device"]), ("Firefox", "Android", "mobile"))
        self.assertEqual(login["created"], login["seen"])
        created = datetime.fromisoformat(login["created"])
        self.assertLessEqual(abs(created - before), timedelta(seconds=2))
        self.assertEqual(created.utcoffset(), timedelta(0))
        self.assertLessEqual(abs(row.expire_date - before - timedelta(hours=8)), timedelta(seconds=2))
        self.assertEqual(datetime.fromisoformat(data["_session_expiry"]), row.expire_date)
        text = json.dumps(data)
        for fingerprint in ("Mozilla", "133.0", "rv:", "127.0.0.1"):
            self.assertNotIn(fingerprint, text)
        self.assertNotEqual(self.login(FIREFOX_ANDROID).key, client.key)
        self.assertNotEqual(login_data(self.login().key)["id"], login["id"])

    def test_activity_keeps_the_absolute_expiry(self):
        client = self.login()
        expiry = stored(client.key).expire_date
        seen = datetime.fromisoformat(login_data(client.key)["seen"])
        later = seen + timedelta(hours=7)
        with patch("django.utils.timezone.now", return_value=later):
            self.logins(client)
        row = stored(client.key)
        self.assertEqual(row.get_decoded()[LOGIN_KEY]["seen"], later.isoformat(timespec="seconds"))
        self.assertEqual(row.expire_date, expiry)
        # Any later save through Django's session store keeps the login's expiry too.
        session = SessionStore(client.key)
        session["unrelated"] = True
        session.save()
        self.assertEqual(stored(client.key).expire_date, expiry)
        with patch("django.utils.timezone.now", return_value=expiry + timedelta(seconds=1)):
            self.assert_api_error(client.get("/api/logins/", secure=True), 401, "auth_required")

    def test_activity_is_recorded_at_most_once_a_minute(self):
        client = self.login()
        start = datetime.fromisoformat(login_data(client.key)["seen"])
        for offset, expected in ((30, 0), (61, 61), (61 + 59, 61), (61 + 60, 121)):
            with self.subTest(offset=offset), patch("django.utils.timezone.now",
                                                    return_value=start + timedelta(seconds=offset)):
                before = stored(client.key).session_data
                self.logins(client)
                seen = login_data(client.key)["seen"]
                self.assertEqual(seen, (start + timedelta(seconds=expected)).isoformat(timespec="seconds"))
                if offset == expected:
                    self.assertNotEqual(stored(client.key).session_data, before)
                else:
                    self.assertEqual(stored(client.key).session_data, before)

    def test_list_shows_own_unexpired_logins_without_secrets(self):
        first = self.login(CHROME_WINDOWS)
        second = self.login(FIREFOX_ANDROID)
        other = get_user_model().objects.create_user(username="other", password=PASSWORD)
        foreign = SessionStore()
        foreign.update({SESSION_KEY: str(other.pk), HASH_SESSION_KEY: other.get_session_auth_hash()})
        foreign.save()
        stale = SessionStore()
        stale.update({SESSION_KEY: str(self.user.pk), HASH_SESSION_KEY: "stale"})
        stale.save()
        expired = SessionStore()
        expired.update({SESSION_KEY: str(self.user.pk), HASH_SESSION_KEY: self.user.get_session_auth_hash()})
        expired.set_expiry(timezone.now() - timedelta(seconds=1))
        expired.save()
        anonymous = SessionStore()
        anonymous["note"] = "no login"
        anonymous.save()
        response = first.get("/api/logins/", secure=True)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response["Cache-Control"], "no-store")
        found = response.json()["logins"]
        self.assertEqual(len(found), 2)
        for login in found:
            self.assertEqual(set(login), FIELDS)
            self.assertEqual(login["connections"], 0)
        mine = {login["id"]: login for login in found}
        self.assertTrue(mine[first.login_id]["current"])
        self.assertFalse(mine[second.login_id]["current"])
        self.assertEqual(mine[second.login_id]["browser"], "Firefox")
        self.assertEqual(mine[second.login_id]["device"], "mobile")
        self.assertEqual(mine[first.login_id]["expires"],
                         stored(first.key).expire_date.isoformat(timespec="seconds"))
        secrets = [first.key, second.key, first.csrf, second.csrf, foreign.session_key, stale.session_key]
        secrets += [client.cookies[name].value for client in (first, second)
                    for name in (settings.SESSION_COOKIE_NAME, settings.CSRF_COOKIE_NAME)]
        for secret in secrets:
            self.assertNotIn(secret.encode(), response.content)
        self.assertFalse(Session.objects.filter(session_key=expired.session_key).exists(),
                         "listing clears expired login rows")
        self.assertTrue(Session.objects.filter(session_key=anonymous.session_key).exists())

    def test_list_orders_by_last_activity(self):
        first = self.login()
        second = self.login(FIREFOX_ANDROID)
        seen = datetime.fromisoformat(login_data(first.key)["seen"])
        with patch("django.utils.timezone.now", return_value=seen + timedelta(minutes=5)):
            self.logins(first)
        # Within the minute since its login, the second login's own request records nothing.
        self.assertEqual([login["id"] for login in self.logins(second)], [first.login_id, second.login_id])

    def test_anonymous_requests_are_refused(self):
        client = self.browser()
        self.assert_api_error(client.get("/api/logins/", secure=True), 401, "auth_required")
        self.assert_api_error(self.post(client, "/api/logins/revoke/", {"scope": "all"}), 401, "auth_required")
        self.assert_api_error(self.post(client, f"/api/logins/{'0' * 16}/revoke/"), 401, "auth_required")

    def test_revoking_another_login_ends_only_that_login(self):
        current = self.login()
        other = self.login(FIREFOX_ANDROID)
        response = self.post(current, f"/api/logins/{other.login_id}/revoke/")
        self.assertEqual(response.status_code, 200, response.content)
        self.assertEqual(response.json(), {"revoked": 1, "current": False})
        self.assertNotIn(settings.SESSION_COOKIE_NAME, response.cookies)
        self.revoke_session.assert_called_once_with(other.key)
        self.assertFalse(Session.objects.filter(session_key=other.key).exists())
        self.assert_api_error(other.get("/api/logins/", secure=True), 401, "auth_required")
        self.assertEqual([login["id"] for login in self.logins(current)], [current.login_id])

    def test_revoking_the_current_login_logs_the_response_out(self):
        current = self.login()
        other = self.login(FIREFOX_ANDROID)
        response = self.post(current, f"/api/logins/{current.login_id}/revoke/")
        self.assertEqual(response.json(), {"revoked": 1, "current": True})
        self.assert_logged_out(response)
        self.revoke_session.assert_called_once_with(current.key)
        self.assertFalse(Session.objects.filter(session_key=current.key).exists())
        self.assert_api_error(current.get("/api/logins/", secure=True), 401, "auth_required")
        self.assertEqual(len(self.logins(other)), 1)

    def test_revoking_other_logins_keeps_the_current_one(self):
        current = self.login()
        others = [self.login(FIREFOX_ANDROID), self.legacy_login()]
        response = self.post(current, "/api/logins/revoke/", {"scope": "others"})
        self.assertEqual(response.json(), {"revoked": 2, "current": False})
        self.assertNotIn(settings.SESSION_COOKIE_NAME, response.cookies)
        self.assertEqual(sorted(call.args[0] for call in self.revoke_session.call_args_list),
                         sorted(client.key for client in others))
        self.assertEqual([login["id"] for login in self.logins(current)], [current.login_id])
        response = self.post(current, "/api/logins/revoke/", {"scope": "others"})
        self.assertEqual(response.json(), {"revoked": 0, "current": False})

    def test_revoking_all_logins_includes_the_current_one(self):
        clients = [self.login(), self.login(FIREFOX_ANDROID), self.legacy_login()]
        response = self.post(clients[0], "/api/logins/revoke/", {"scope": "all"})
        self.assertEqual(response.json(), {"revoked": 3, "current": True})
        self.assert_logged_out(response)
        self.assertEqual(sorted(call.args[0] for call in self.revoke_session.call_args_list),
                         sorted(client.key for client in clients))
        for client in clients:
            self.assert_api_error(client.get("/api/logins/", secure=True), 401, "auth_required")
        self.assertFalse(Session.objects.exists())

    def test_revocation_requires_csrf_and_the_same_origin(self):
        current = self.login()
        other = self.login(FIREFOX_ANDROID)
        target = f"/api/logins/{other.login_id}/revoke/"
        self.assert_api_error(self.post(current, target, csrf=False), 403, "csrf_token_invalid")
        self.assert_api_error(self.post(current, target, HTTP_ORIGIN="https://attacker.example"),
                              403, "csrf_origin_mismatch")
        self.assert_api_error(self.post(current, "/api/logins/revoke/", {"scope": "all"}, csrf=False),
                              403, "csrf_token_invalid")
        self.revoke_session.assert_not_called()
        self.assertEqual(len(self.logins(other)), 2)

    def test_unknown_or_malformed_logins_and_bad_requests_are_refused(self):
        current = self.login()
        other = self.login(FIREFOX_ANDROID)
        for login_id in ("0" * 16, "ABCDEF0123456789", "short", other.key[:16], other.key):
            with self.subTest(login_id=login_id):
                response = self.post(current, f"/api/logins/{login_id}/revoke/")
                self.assert_api_error(response, 404, "login_not_found")
        for payload in ({"scope": "everything"}, {}, {"scope": "all", "id": "x"}, ["all"]):
            with self.subTest(payload=payload):
                response = self.post(current, "/api/logins/revoke/", payload)
                self.assert_api_error(response, 400, "invalid_request")
        response = self.post(current, "/api/logins/revoke/")
        self.assert_api_error(response, 400, "invalid_request")
        response = current.get(f"/api/logins/{other.login_id}/revoke/", secure=True)
        self.assert_api_error(response, 405, "method_not_allowed")
        self.assertEqual(response["Allow"], "POST")
        response = self.post(current, "/api/logins/", {})
        self.assert_api_error(response, 405, "method_not_allowed")
        self.revoke_session.assert_not_called()
        self.assertEqual(len(self.logins(current)), 2)

    def test_a_login_without_metadata_is_listed_and_revocable_by_a_derived_id(self):
        current = self.login()
        legacy = self.legacy_login()
        response = current.get("/api/logins/", secure=True)
        self.assertNotIn(legacy.key.encode(), response.content)
        entry = next(login for login in response.json()["logins"] if not login["current"])
        self.assertEqual(entry["id"], legacy_id(legacy.key))
        self.assertRegex(entry["id"], r"\A[0-9a-f]{16}\Z")
        self.assertNotIn(entry["id"], legacy.key)
        self.assertEqual({key: entry[key] for key in ("browser", "os", "device", "created", "seen")},
                         dict.fromkeys(("browser", "os", "device", "created", "seen")))
        self.assertEqual(entry["expires"], stored(legacy.key).expire_date.isoformat(timespec="seconds"))
        # A legacy login's own requests do not invent metadata or move its expiry.
        expiry = stored(legacy.key).expire_date
        with patch("django.utils.timezone.now", return_value=timezone.now() + timedelta(minutes=5)):
            found = self.logins(legacy)
        self.assertEqual([login["id"] for login in found if login["current"]], [legacy.login_id])
        self.assertNotIn(LOGIN_KEY, stored(legacy.key).get_decoded())
        self.assertEqual(stored(legacy.key).expire_date, expiry)
        response = self.post(current, f"/api/logins/{entry['id']}/revoke/")
        self.assertEqual(response.json(), {"revoked": 1, "current": False})
        self.revoke_session.assert_called_once_with(legacy.key)
        self.assert_api_error(legacy.get("/api/logins/", secure=True), 401, "auth_required")

    def websocket(self, client):
        headers = [(b"host", b"localhost:8443"), (b"origin", ORIGIN.encode()),
                   (b"cookie", f"{settings.SESSION_COOKIE_NAME}={client.key}".encode())]
        connection = WebsocketCommunicator(application, "/ws/sessions/0/", headers=headers, subprotocols=["binary"])
        connection.scope["scheme"] = "wss"
        return connection

    async def desktop(self, client):
        connection = self.websocket(client)
        self.assertTrue((await connection.connect())[0])
        self.assertEqual(await connection.receive_from(), b"RFB 003.008\n")
        return connection

    async def test_revocation_closes_only_the_revoked_logins_desktop_connections(self):
        current = await sync_to_async(self.login)()
        other = await sync_to_async(self.login)(FIREFOX_ANDROID)
        mine = await self.desktop(current)
        theirs = [await self.desktop(other), await self.desktop(other)]
        try:
            found = await sync_to_async(self.logins)(current)
            counts = {login["id"]: login["connections"] for login in found}
            self.assertEqual(counts, {current.login_id: 1, other.login_id: 2})
            response = await sync_to_async(self.post)(current, f"/api/logins/{other.login_id}/revoke/")
            self.assertEqual(response.json(), {"revoked": 1, "current": False})
            for connection in theirs:
                self.assertEqual(await connection.receive_output(timeout=1), {"type": "websocket.close",
                                                                             "code": 4401})
            await mine.send_to(bytes_data=b"still here")
            self.assertEqual(await mine.receive_from(), b"still here")
            refused = self.websocket(other)
            self.assertFalse((await refused.connect())[0])
            await refused.disconnect()
            found = await sync_to_async(self.logins)(current)
            self.assertEqual([(login["current"], login["connections"]) for login in found], [(True, 1)])
        finally:
            for connection in (mine, *theirs):
                await connection.disconnect()

    async def test_revoking_all_closes_the_current_logins_desktop_too(self):
        current = await sync_to_async(self.login)()
        mine = await self.desktop(current)
        try:
            response = await sync_to_async(self.post)(current, "/api/logins/revoke/", {"scope": "all"})
            self.assertEqual(response.json(), {"revoked": 1, "current": True})
            self.assertEqual(await mine.receive_output(timeout=1), {"type": "websocket.close", "code": 4401})
        finally:
            await mine.disconnect()

    async def test_desktop_connection_records_activity(self):
        client = await sync_to_async(self.login)()
        seen = datetime.fromisoformat((await sync_to_async(login_data)(client.key))["seen"])
        expiry = (await sync_to_async(stored)(client.key)).expire_date
        later = seen + timedelta(minutes=2)
        with patch("django.utils.timezone.now", return_value=later):
            connection = await self.desktop(client)
        await connection.disconnect()
        self.assertEqual((await sync_to_async(login_data)(client.key))["seen"], later.isoformat(timespec="seconds"))
        self.assertEqual((await sync_to_async(stored)(client.key)).expire_date, expiry)

    async def test_command_line_revocation_closes_desktop_connections_by_the_session_check(self):
        client = await sync_to_async(self.login)()
        connection = await self.desktop(client)
        try:
            await sync_to_async(call_command)("logins", "revoke", client.login_id, stdout=io.StringIO())
            closed = await connection.receive_output(timeout=4)
            self.assertEqual(closed["code"], 4401)
        finally:
            await connection.disconnect()

    def command(self, *arguments):
        output = io.StringIO()
        call_command("logins", *arguments, stdout=output)
        return json.loads(output.getvalue())

    def test_management_command_lists_and_revokes_logins(self):
        first = self.login()
        second = self.login(FIREFOX_ANDROID)
        legacy = self.legacy_login()
        listed = self.command()["logins"]
        self.assertEqual(self.command("list"), {"logins": listed})
        self.assertEqual({login["id"] for login in listed}, {client.login_id for client in (first, second, legacy)})
        for login in listed:
            self.assertEqual(set(login), FIELDS - {"current", "connections"})
        text = json.dumps(listed)
        for client in (first, second, legacy):
            self.assertNotIn(client.key, text)
        self.assertEqual(self.command("revoke", second.login_id), {"revoked": 1})
        self.assert_api_error(second.get("/api/logins/", secure=True), 401, "auth_required")
        self.assertEqual(len(self.logins(first)), 2)
        with self.assertRaisesMessage(CommandError, "No login has ID"):
            self.command("revoke", second.login_id)
        for arguments in (("revoke",), ("revoke", first.login_id, "--all"), ("list", "--all"),
                          ("revoke", "not-an-id")):
            with self.subTest(arguments=arguments), self.assertRaises(CommandError):
                self.command(*arguments)
        self.assertEqual(self.command("revoke", "--all"), {"revoked": 2})
        self.assertEqual(self.command(), {"logins": []})
        self.assert_api_error(first.get("/api/logins/", secure=True), 401, "auth_required")
