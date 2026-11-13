import inspect
import json
import socketserver
import tempfile
import threading
from pathlib import Path
from urllib.parse import quote

from asgiref.sync import sync_to_async
from asgiref.testing import ApplicationCommunicator
from django.conf import settings
from django.contrib.auth import get_user_model
from django.test import Client, TransactionTestCase, override_settings

from raven_web import broker, views
from raven_web.asgi import MAX_BODY, application
from raven_web.validation import CHUNK_SIZE

ORIGIN = "https://localhost:8443"
PASSWORD = "Several unique test words 987"
TOKEN = "Abc_-" + "x" * 27
KOREAN = "한글 문서.txt"
CONTENT = bytes(range(256)) * 1024
LISTING = {"path": "a/b", "entries": [{"name": "c", "type": "file", "size": 1, "mtime": 0}],
           "truncated": False, "skipped": 0}


class FileBrokerHandler(socketserver.StreamRequestHandler):
    def handle(self):
        request = json.loads(self.rfile.readline())
        payload = self.rfile.read(request["size"]) if request["action"] == "upload_chunk" else b""
        self.server.calls.append(request)
        self.server.payloads.append(payload)
        reply = self.server.replies.get(request["action"])
        if reply is None and request["action"] == "download":
            reply = FileBrokerHandler.download
        if callable(reply):
            reply(self, request)
            return
        if reply is None:
            reply = {"ok": True, "data": self.default(request)}
        self.wfile.write(json.dumps(reply).encode() + b"\n")

    def download(self, request):
        # Hold back everything after the first part until the test saw that part reach the client.
        self.wfile.write(json.dumps({"ok": True, "data": {"name": "x", "size": len(CONTENT)}}).encode() + b"\n")
        self.wfile.write(CONTENT[:65536])
        self.wfile.flush()
        self.server.release.wait(10)
        self.wfile.write(CONTENT[65536:])

    def default(self, request):
        action = request["action"]
        if action == "files_list":
            return {**LISTING, "path": request["path"]}
        if action == "upload_start":
            return {"token": TOKEN}
        if action == "upload_chunk":
            return {"received": request["offset"] + request["size"]}
        if action == "upload_finish":
            return {"name": "x.bin", "size": 3, "mtime": 1700000000}
        return None


class FakeBroker(socketserver.ThreadingUnixStreamServer):
    daemon_threads = True


class FileApiTests(TransactionTestCase):
    def setUp(self):
        self.user = get_user_model().objects.create_user(username="owner", password=PASSWORD)
        self.temporary = tempfile.TemporaryDirectory(prefix="raven-files-broker-")
        self.socket_path = str(Path(self.temporary.name) / "broker.sock")
        self.server = FakeBroker(self.socket_path, FileBrokerHandler)
        self.server.calls = []
        self.server.payloads = []
        self.server.replies = {}
        self.server.release = threading.Event()
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.config = override_settings(RAVEN_BROKER=self.socket_path)
        self.config.enable()
        self.client = Client(enforce_csrf_checks=True, HTTP_HOST="localhost:8443", HTTP_ORIGIN=ORIGIN)
        self.client.get("/login/", secure=True)
        self.csrf = self.client.cookies[settings.CSRF_COOKIE_NAME].value

    def tearDown(self):
        self.server.release.set()
        self.config.disable()
        self.server.shutdown()
        self.server.server_close()
        self.thread.join()
        self.temporary.cleanup()

    def authenticate(self):
        self.client.force_login(self.user)
        self.client.get("/", secure=True)
        self.csrf = self.client.cookies[settings.CSRF_COOKIE_NAME].value

    def json_request(self, method, path, payload, csrf=True, **headers):
        if csrf:
            headers["HTTP_X_CSRFTOKEN"] = self.csrf
        return getattr(self.client, method)(path, data=json.dumps(payload), content_type="application/json",
                                            secure=True, **headers)

    def put_chunk(self, token=TOKEN, offset="0", data=b"abc", csrf=True, content_type="application/octet-stream",
                  **headers):
        if csrf:
            headers["HTTP_X_CSRFTOKEN"] = self.csrf
        query = "" if offset is None else f"?offset={offset}"
        return self.client.put(f"/api/files/uploads/{token}/{query}", data=data, content_type=content_type,
                               secure=True, **headers)

    def assert_api_error(self, response, status, code):
        self.assertEqual(response.status_code, status, response.content)
        self.assertEqual(response.json()["code"], code)
        self.assertEqual(set(response.json()), {"error", "code"})

    def changes(self, csrf=True, **headers):
        """Every state-changing file request, sent the way the browser sends it."""
        return (
            self.json_request("post", "/api/files/uploads/",
                              {"path": "", "name": "x", "size": 1, "overwrite": False}, csrf, **headers),
            self.put_chunk(csrf=csrf, **headers),
            self.json_request("post", f"/api/files/uploads/{TOKEN}/finish/", {"overwrite": False}, csrf, **headers),
            self.client.delete(f"/api/files/uploads/{TOKEN}/", secure=True,
                               **({"HTTP_X_CSRFTOKEN": self.csrf} if csrf else {}), **headers),
        )

    def test_anonymous_file_requests_never_reach_broker(self):
        responses = (
            self.client.get("/api/files/?path=", secure=True),
            self.client.get("/api/files/download/?path=x", secure=True),
            *self.changes(),
        )
        for response in responses:
            with self.subTest(path=response.request["PATH_INFO"]):
                self.assert_api_error(response, 401, "auth_required")
        self.assertEqual(self.server.calls, [])

    def test_file_changes_require_csrf_and_same_origin(self):
        self.authenticate()
        for response in self.changes(csrf=False):
            with self.subTest(path=response.request["PATH_INFO"], method=response.request["REQUEST_METHOD"]):
                self.assert_api_error(response, 403, "csrf_token_invalid")
        for response in self.changes(HTTP_ORIGIN="https://attacker.example"):
            with self.subTest(path=response.request["PATH_INFO"], method=response.request["REQUEST_METHOD"]):
                self.assert_api_error(response, 403, "csrf_origin_mismatch")
        self.assertEqual(self.server.calls, [])

    def test_file_endpoints_allow_only_their_methods(self):
        self.authenticate()
        cases = (
            ("post", "/api/files/", "GET"),
            ("get", "/api/files/uploads/", "POST"),
            ("get", f"/api/files/uploads/{TOKEN}/", "PUT, DELETE"),
            ("post", f"/api/files/uploads/{TOKEN}/", "PUT, DELETE"),
            ("get", f"/api/files/uploads/{TOKEN}/finish/", "POST"),
            ("delete", f"/api/files/uploads/{TOKEN}/finish/", "POST"),
            ("post", "/api/files/download/?path=x", "GET"),
            ("put", "/api/files/download/?path=x", "GET"),
        )
        for method, path, allowed in cases:
            with self.subTest(method=method, path=path):
                response = getattr(self.client, method)(path, secure=True, HTTP_X_CSRFTOKEN=self.csrf)
                self.assert_api_error(response, 405, "method_not_allowed")
                self.assertEqual(response["Allow"], allowed)
        self.assertEqual(self.server.calls, [])

    def test_upload_tokens_are_checked_before_the_broker(self):
        self.authenticate()
        for token in ("x" * 31, "x" * 33, "x" * 31 + "%21", "x" * 31 + ".", "%2E%2E"):
            with self.subTest(token=token):
                self.assert_api_error(self.put_chunk(token=token), 400, "invalid_request")
                response = self.json_request("post", f"/api/files/uploads/{token}/finish/", {"overwrite": False})
                self.assert_api_error(response, 400, "invalid_request")
                response = self.client.delete(f"/api/files/uploads/{token}/", secure=True,
                                              HTTP_X_CSRFTOKEN=self.csrf)
                self.assert_api_error(response, 400, "invalid_request")
        self.assertEqual(self.server.calls, [])

    def test_chunk_bodies_are_bounded_and_raw(self):
        self.authenticate()
        self.assert_api_error(self.put_chunk(data=b""), 411, "invalid_request")
        self.assert_api_error(self.put_chunk(data=b"x" * (CHUNK_SIZE + 1)), 413, "too_large")
        self.assert_api_error(self.put_chunk(content_type="application/json"), 400, "invalid_request")
        for offset in (None, "", "-1", "01", "1.0", "x", "4294967297", "1" * 11):
            with self.subTest(offset=offset):
                self.assert_api_error(self.put_chunk(offset=offset), 400, "invalid_request")
        self.assertEqual(self.server.calls, [])
        data = bytes(range(256)) * (CHUNK_SIZE // 256)
        response = self.put_chunk(offset="4194304", data=data)
        self.assertEqual(response.status_code, 200, response.content)
        self.assertEqual(response.json(), {"received": 2 * CHUNK_SIZE})
        self.assertEqual(self.server.calls, [{"action": "upload_chunk", "token": TOKEN, "offset": CHUNK_SIZE,
                                              "size": CHUNK_SIZE}])
        self.assertEqual(self.server.payloads, [data])

    def test_paths_names_and_sizes_are_checked_before_the_broker(self):
        self.authenticate()
        for path in ("..", "../x", "a/../b", "/etc", "a//b", "a/", "a%00b", ".raven-uploads", ".raven-uploads/x",
                     "x" * 256, "/".join(["a"] * 33)):
            with self.subTest(path=path):
                self.assert_api_error(self.client.get(f"/api/files/?path={path}", secure=True), 400, "invalid_path")
                response = self.client.get(f"/api/files/download/?path={path}", secure=True)
                self.assert_api_error(response, 400, "invalid_path")
        self.assert_api_error(self.client.get("/api/files/download/?path=", secure=True), 400, "not_a_file")
        start = "/api/files/uploads/"
        cases = (
            ({"path": "../x", "name": "x", "size": 1}, 400, "invalid_path"),
            ({"path": "", "name": ".raven-uploads", "size": 1}, 400, "invalid_path"),
            ({"path": "", "name": "가" * 86, "size": 1}, 400, "invalid_path"),
            ({"path": "", "name": "a/b", "size": 1}, 400, "invalid_path"),
            ({"path": "", "name": "", "size": 1}, 400, "invalid_path"),
            ({"path": "", "name": None, "size": 1}, 400, "invalid_path"),
            ({"path": "", "name": "x", "size": 4 * 1024 ** 3 + 1}, 413, "too_large"),
            ({"path": "", "name": "x", "size": -1}, 400, "invalid_request"),
            ({"path": "", "name": "x", "size": True}, 400, "invalid_request"),
            ({"path": "", "name": "x", "size": 1.5}, 400, "invalid_request"),
            ({"path": "", "name": "x", "size": "1"}, 400, "invalid_request"),
            ({"path": "", "name": "x"}, 400, "invalid_request"),
            ({"path": "", "name": "x", "size": 1, "overwrite": "yes"}, 400, "invalid_request"),
            ({"path": "", "name": "x", "size": 1, "host": True}, 400, "invalid_request"),
        )
        for payload, status, code in cases:
            with self.subTest(payload=payload):
                self.assert_api_error(self.json_request("post", start, payload), status, code)
        response = self.json_request("post", f"/api/files/uploads/{TOKEN}/finish/", {"overwrite": 1})
        self.assert_api_error(response, 400, "invalid_request")
        self.assertEqual(self.server.calls, [])
        response = self.json_request("post", start, {"path": "sub", "name": ".raven-uploads", "size": 0})
        self.assertEqual(response.status_code, 201)

    def test_upload_flow_reaches_the_broker(self):
        self.authenticate()
        response = self.json_request("post", "/api/files/uploads/",
                                     {"path": "a b/c", "name": KOREAN, "size": 3, "overwrite": True})
        self.assertEqual(response.status_code, 201)
        self.assertEqual(response.json(), {"token": TOKEN, "chunk_size": CHUNK_SIZE})
        response = self.json_request("post", f"/api/files/uploads/{TOKEN}/finish/", {"overwrite": True})
        self.assertEqual(response.json(), {"file": {"name": "x.bin", "size": 3, "mtime": 1700000000}})
        response = self.client.delete(f"/api/files/uploads/{TOKEN}/", secure=True, HTTP_X_CSRFTOKEN=self.csrf)
        self.assertEqual(response.json(), {"cancelled": True})
        response = self.client.get(f"/api/files/?path={quote('a/b')}", secure=True)
        self.assertEqual(response.json(), LISTING)
        self.assertEqual(self.client.get("/api/files/", secure=True).json()["path"], "")
        self.assertEqual(self.server.calls, [
            {"action": "upload_start", "path": "a b/c", "name": KOREAN, "size": 3, "overwrite": True},
            {"action": "upload_finish", "token": TOKEN, "overwrite": True},
            {"action": "upload_cancel", "token": TOKEN},
            {"action": "files_list", "path": "a/b"},
            {"action": "files_list", "path": ""},
        ])

    def test_malformed_broker_data_is_not_passed_on(self):
        self.authenticate()
        self.server.replies["upload_start"] = {"ok": True, "data": {"token": "../x"}}
        response = self.json_request("post", "/api/files/uploads/", {"path": "", "name": "x", "size": 1})
        self.assert_api_error(response, 502, "broker_unavailable")
        self.server.replies["files_list"] = {"ok": True, "data": ["not", "an", "object"]}
        self.assert_api_error(self.client.get("/api/files/", secure=True), 502, "broker_unavailable")

    def test_broker_codes_map_to_http_statuses(self):
        self.assertEqual(set(views.FILE_ERROR_STATUS), broker.FILE_CODES)
        self.authenticate()
        expected = {
            "invalid_path": 400, "not_found": 404, "upload_unknown": 404, "not_a_directory": 400,
            "not_a_file": 400, "file_exists": 409, "offset_mismatch": 409, "too_large": 413, "no_space": 507,
            "permission_denied": 403, "busy": 429, "downloads_missing": 409, "staging_invalid": 409,
            "cross_device": 409,
            "session_failed": 502, "not_a_code": 502,
        }
        for code, status in expected.items():
            with self.subTest(code=code):
                reply = {"ok": False, "error": "Static broker message", "code": code}
                for action in ("files_list", "upload_chunk", "download"):
                    self.server.replies[action] = reply
                self.server.release.set()
                responses = (
                    self.client.get("/api/files/?path=a", secure=True),
                    self.put_chunk(),
                    self.client.get("/api/files/download/?path=a", secure=True),
                )
                wanted = code if status != 502 else "session_failed"
                for response in responses:
                    self.assertEqual(response.status_code, status)
                    self.assertEqual(response.json(), {"error": "Static broker message", "code": wanted})
        self.server.replies["files_list"] = {"ok": False, "error": "Line\nbreak", "code": "not_found"}
        response = self.client.get("/api/files/?path=a", secure=True)
        self.assertEqual(response.json(), {"error": "The session operation failed", "code": "not_found"})

    def test_content_disposition_keeps_korean_names(self):
        self.assertEqual(
            views.attachment(KOREAN),
            "attachment; filename=\"__ __.txt\"; filename*=UTF-8''%ED%95%9C%EA%B8%80%20%EB%AC%B8%EC%84%9C.txt",
        )
        self.assertEqual(views.attachment('a"b\\c\r\nd;e.txt'),
                         "attachment; filename=\"a_b_c__d;e.txt\"; filename*=UTF-8''a%22b%5Cc%0D%0Ad%3Be.txt")
        self.assertEqual(views.attachment("plain.tar.gz"),
                         "attachment; filename=\"plain.tar.gz\"; filename*=UTF-8''plain.tar.gz")

    def test_download_is_an_async_stream(self):
        self.assertTrue(inspect.iscoroutinefunction(views.download))
        self.assertTrue(inspect.isasyncgenfunction(broker.stream))

    def scope(self, method, path, query=b"", headers=()):
        cookies = "; ".join(f"{name}={morsel.value}" for name, morsel in self.client.cookies.items())
        return {
            "type": "http", "asgi": {"version": "3.0"}, "http_version": "1.1", "method": method,
            "scheme": "https", "path": path, "raw_path": path.encode(), "query_string": query, "root_path": "",
            "headers": [(b"host", b"localhost:8443"), (b"cookie", cookies.encode()), *headers],
            "client": ("127.0.0.1", 50000), "server": ("localhost", 8443),
        }

    async def receive_body(self, communicator):
        body = b""
        while True:
            message = await communicator.receive_output(timeout=5)
            self.assertEqual(message["type"], "http.response.body")
            body += message.get("body", b"")
            if not message.get("more_body", False):
                return body

    async def test_download_streams_through_the_asgi_stack(self):
        await sync_to_async(self.authenticate)()
        path = f"폴더/{KOREAN}"
        communicator = ApplicationCommunicator(
            application, self.scope("GET", "/api/files/download/", b"path=" + quote(path).encode()))
        await communicator.send_input({"type": "http.request", "body": b"", "more_body": False})
        start = await communicator.receive_output(timeout=5)
        self.assertEqual(start["status"], 200)
        headers = dict(start["headers"])
        self.assertEqual(headers[b"Content-Length"], str(len(CONTENT)).encode())
        self.assertEqual(headers[b"Content-Type"], b"application/octet-stream")
        self.assertEqual(headers[b"X-Content-Type-Options"], b"nosniff")
        disposition = headers[b"Content-Disposition"].decode()
        self.assertTrue(disposition.startswith('attachment; filename="'))
        self.assertEqual(disposition.split("filename*=UTF-8''", 1)[1], quote(KOREAN, safe=""))
        # The broker holds back the rest until the first part reached the client, so nothing was buffered.
        first = await communicator.receive_output(timeout=5)
        self.assertTrue(first["more_body"])
        self.assertTrue(first["body"])
        self.server.release.set()
        body = first["body"] + await self.receive_body(communicator)
        self.assertEqual(body, CONTENT)
        await communicator.wait(timeout=5)
        self.assertEqual(self.server.calls, [{"action": "download", "path": path}])

    async def test_download_ends_short_when_the_broker_stops(self):
        await sync_to_async(self.authenticate)()

        def truncated(handler, request):
            handler.wfile.write(json.dumps({"ok": True, "data": {"name": "x", "size": 100}}).encode() + b"\n")
            handler.wfile.write(b"z" * 40)

        self.server.replies["download"] = truncated
        communicator = ApplicationCommunicator(application, self.scope("GET", "/api/files/download/", b"path=x"))
        await communicator.send_input({"type": "http.request", "body": b"", "more_body": False})
        start = await communicator.receive_output(timeout=5)
        self.assertEqual(dict(start["headers"])[b"Content-Length"], b"100")
        self.assertEqual(await self.receive_body(communicator), b"z" * 40)
        await communicator.wait(timeout=5)

    async def test_download_errors_and_anonymous_requests_through_the_asgi_stack(self):
        communicator = ApplicationCommunicator(application, self.scope("GET", "/api/files/download/", b"path=x"))
        await communicator.send_input({"type": "http.request", "body": b"", "more_body": False})
        start = await communicator.receive_output(timeout=5)
        self.assertEqual(start["status"], 401)
        self.assertEqual(json.loads(await self.receive_body(communicator))["code"], "auth_required")
        await communicator.wait(timeout=5)
        self.assertEqual(self.server.calls, [])
        await sync_to_async(self.authenticate)()
        self.server.replies["download"] = {"ok": False, "error": "Not a regular file", "code": "not_a_file"}
        communicator = ApplicationCommunicator(application, self.scope("GET", "/api/files/download/", b"path=x"))
        await communicator.send_input({"type": "http.request", "body": b"", "more_body": False})
        start = await communicator.receive_output(timeout=5)
        self.assertEqual(start["status"], 400)
        self.assertEqual(json.loads(await self.receive_body(communicator)),
                         {"error": "Not a regular file", "code": "not_a_file"})
        await communicator.wait(timeout=5)

    async def test_chunk_upload_through_the_asgi_stack(self):
        await sync_to_async(self.authenticate)()
        data = b"\0\xff" * (CHUNK_SIZE // 2)
        communicator = ApplicationCommunicator(application, self.scope(
            "PUT", f"/api/files/uploads/{TOKEN}/", b"offset=0", headers=[
                (b"origin", ORIGIN.encode()), (b"x-csrftoken", self.csrf.encode()),
                (b"content-type", b"application/octet-stream"), (b"content-length", str(len(data)).encode()),
            ]))
        for index in range(0, len(data), 65536):
            await communicator.send_input({"type": "http.request", "body": data[index:index + 65536],
                                           "more_body": index + 65536 < len(data)})
        start = await communicator.receive_output(timeout=10)
        body = await self.receive_body(communicator)
        self.assertEqual(start["status"], 200, body)
        self.assertEqual(json.loads(body), {"received": CHUNK_SIZE})
        self.assertEqual(self.server.payloads, [data])
        await communicator.wait(timeout=5)

    async def test_large_or_unsized_bodies_are_refused_before_django_reads_them(self):
        chunk = f"/api/files/uploads/{TOKEN}/"
        over_chunk = [(b"content-length", str(settings.FILE_UPLOAD_MAX_MEMORY_SIZE + 1).encode())]
        over_form = [(b"content-length", str(MAX_BODY + 1).encode())]
        for method, path, headers, status, code in (
            ("PUT", chunk, over_chunk, 413, "too_large"),
            ("PUT", chunk, [(b"content-length", b"99999999999")], 400, "invalid_request"),
            ("PUT", chunk, [(b"content-length", b"1"), (b"content-length", b"1")], 400, "invalid_request"),
            ("PUT", chunk, [(b"transfer-encoding", b"chunked")], 411, "invalid_request"),
            ("POST", "/login/", [(b"transfer-encoding", b"chunked")], 411, "invalid_request"),
            # Only the chunk route gets the large limit; every other body stays small.
            ("POST", chunk, over_form, 413, "too_large"),
            ("PUT", chunk + "finish/", over_form, 413, "too_large"),
            ("PUT", "/api/files/uploads/short/", over_form, 413, "too_large"),
            ("POST", f"/api/files/uploads/{TOKEN}/finish/", over_form, 413, "too_large"),
            ("POST", "/api/files/uploads/", over_form, 413, "too_large"),
            ("POST", "/api/sessions/", over_form, 413, "too_large"),
            ("POST", "/login/", over_form, 413, "too_large"),
        ):
            with self.subTest(method=method, path=path, headers=headers):
                communicator = ApplicationCommunicator(
                    application, self.scope(method, path, b"offset=0", headers=headers))
                start = await communicator.receive_output(timeout=5)
                self.assertEqual(start["status"], status)
                self.assertEqual(json.loads(await self.receive_body(communicator))["code"], code)
                await communicator.wait(timeout=5)
        self.assertEqual(self.server.calls, [])

    async def test_small_bodies_pass_the_limit(self):
        await sync_to_async(self.authenticate)()
        body = json.dumps({"path": "", "name": "x" * (MAX_BODY - 50), "size": 1}).encode()
        body += b" " * (MAX_BODY - len(body))
        communicator = ApplicationCommunicator(application, self.scope("POST", "/api/files/uploads/", headers=[
            (b"origin", ORIGIN.encode()), (b"x-csrftoken", self.csrf.encode()),
            (b"content-type", b"application/json"), (b"content-length", str(len(body)).encode()),
        ]))
        await communicator.send_input({"type": "http.request", "body": body, "more_body": False})
        start = await communicator.receive_output(timeout=5)
        # Django's own 16 KiB JSON limit answers, not the 64 KiB guard.
        self.assertEqual(start["status"], 400)
        self.assertEqual(json.loads(await self.receive_body(communicator))["code"], "invalid_request")
        await communicator.wait(timeout=5)
        self.assertEqual(self.server.calls, [])
