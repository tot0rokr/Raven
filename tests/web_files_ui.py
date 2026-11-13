"""Check the web file panel with the real template, JavaScript and CSS against an in-memory file API.

Run on the host: `python3 tests/web_files_ui.py`. It renders index.html with the web image (the template comes from
this checkout), then runs itself with --browser in the headless-browser image. That half serves the page and a fake
file API from 127.0.0.1 inside the container (no network) and drives Chromium in desktop and phone contexts. No
gateway, broker, login or session is touched.
"""

import hashlib
import json
import os
from pathlib import Path
import re
import secrets
import subprocess
import sys
import tempfile
import threading
import time

ROOT = Path(__file__).resolve().parents[1]
CSRF_TOKEN = "ui-fixture"
CHUNK = 4 * 1024 * 1024
INTRO = "서버의 ~/Downloads 폴더입니다. 모든 세션의 Downloads와 같은 폴더이고, 세션 안에서는 /home/raven/Downloads로 보입니다."
RENDER = """
import os
os.environ['DJANGO_SETTINGS_MODULE'] = 'raven_web.test_settings'
import django
django.setup()
from django.template.loader import render_to_string
print(render_to_string('index.html', {'username': 'preview', 'csrf_token': 'ui-fixture'}))
"""


def host():
    output = Path(tempfile.mkdtemp(prefix="raven-web-files-ui-"))
    web_image = os.environ.get("RAVEN_WEB_IMAGE", "raven-web:dev")
    browser_image = os.environ.get("RAVEN_WEB_BROWSER_IMAGE", "headless-browser:1.63.0")
    limits = ["--memory", "1g", "--memory-swap", "1g", "--cpus", "1", "--pids-limit", "256"]
    # The web image only renders; the template is this checkout's, not the one baked into the image.
    page = subprocess.run(
        ["docker", "run", "--rm", "--network", "none", "--read-only", "--user", f"{os.getuid()}:{os.getgid()}",
         *limits, "--tmpfs", "/tmp:rw,nosuid,nodev,size=64m",
         "--mount", f"type=bind,src={ROOT / 'web' / 'templates'},dst=/app/templates,readonly",
         web_image, "python", "-c", RENDER],
        capture_output=True, check=True, timeout=60,
    )
    print(f"UI artifacts: {output}", flush=True)
    subprocess.run(
        ["docker", "run", "--rm", "-i", "--init", "--network", "none", "--read-only", *limits,
         "--user", f"{os.getuid()}:{os.getgid()}", "--security-opt", "seccomp=unconfined",
         "--shm-size", "1g", "--env", "HOME=/tmp", "--tmpfs", "/tmp:rw,nosuid,nodev,size=512m",
         "--mount", f"type=bind,src={ROOT},dst=/source,readonly",
         "--mount", f"type=bind,src={output},dst=/output", browser_image,
         "python3", "/source/tests/web_files_ui.py", "--browser"],
        input=json.dumps({"html": page.stdout.decode(), "output": "/output"}).encode(), timeout=600, check=True,
    )
    print(f"UI artifacts: {output}", flush=True)


# ---- Browser half (inside the headless-browser image) ----

RFB_FIXTURE = """
export default class RFB extends EventTarget {
  constructor() {
    super();
    queueMicrotask(() => this.dispatchEvent(new Event('connect')));
  }
  focus() {}
  disconnect() { this.dispatchEvent(new Event('disconnect')); }
  sendCtrlAltDel() {}
}
"""
CSP = ("default-src 'none'; script-src 'self'; style-src 'self' 'unsafe-inline'; connect-src 'self'; "
       "img-src 'self' data: blob:; font-src 'self'; worker-src 'self' blob:; base-uri 'none'; "
       "frame-ancestors 'none'; form-action 'self'; object-src 'none'")
TOKEN = re.compile(r"[A-Za-z0-9_-]{32}")
UPLOAD_ROUTE = re.compile(r"/api/files/uploads/([^/]+)/(finish/)?")
MTIME = 1_763_000_000.0
SESSION = {"id": "41", "name": "files", "state": "running", "wm": "xmonad", "geometry": "1600x900",
           "display": "141", "generation": "a" * 64, "command": "xterm"}


class FakeFiles:
    """The DESIGN.md gateway API over an in-memory Downloads tree, with hooks the checks steer."""

    def __init__(self):
        self.lock = threading.Lock()
        self.nodes = {"": {"type": "dir"}}
        self.uploads = {}
        self.log = []
        self.problems = []
        self.failures = {}
        self.static_queries = {}
        self.conflict_on_chunk = set()
        self.hold = False
        self.held = threading.Event()
        self.release = threading.Event()

    def add(self, path, kind="file", data=b"", **extra):
        self.nodes[path] = {"type": kind, "data": data, "mtime": MTIME, **extra}

    def entries(self, path):
        prefix = f"{path}/" if path else ""
        found = []
        for key, node in self.nodes.items():
            if key and key.startswith(prefix) and "/" not in key[len(prefix):]:
                size = len(node.get("data", b"")) if node["type"] == "file" else 4096
                found.append({"name": key[len(prefix):], "type": node["type"], "size": size, "mtime": node["mtime"]})
        return sorted(found, key=lambda entry: (entry["type"] != "dir", entry["name"]))

    def events(self, kind):
        with self.lock:
            return [entry for entry in self.log if entry[0] == kind]


def make_handler(state, html):
    from http.server import BaseHTTPRequestHandler
    from urllib.parse import parse_qs, quote, urlsplit

    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, *_args):
            pass

        def reply(self, status, payload=None, body=None, content_type="application/json", headers=()):
            data = json.dumps(payload).encode() if body is None else body
            try:
                self.send_response(status)
                self.send_header("Content-Type", content_type)
                self.send_header("Content-Length", str(len(data)))
                self.send_header("Cache-Control", "no-store")
                for name, value in headers:
                    self.send_header(name, value)
                self.end_headers()
                self.wfile.write(data)
            except (BrokenPipeError, ConnectionResetError):
                self.close_connection = True

        def error(self, status, code, message="fixture error"):
            self.reply(status, {"error": message, "code": code})

        def do_GET(self):
            self.route("GET")

        def do_POST(self):
            self.route("POST")

        def do_PUT(self):
            self.route("PUT")

        def do_DELETE(self):
            self.route("DELETE")

        def route(self, method):
            url = urlsplit(self.path)
            query = parse_qs(url.query, keep_blank_values=True)
            length = int(self.headers.get("Content-Length") or 0)
            if length > CHUNK + 1024:
                state.problems.append(f"{method} {url.path} sent {length} bytes")
                self.close_connection = True
                self.error(413, "too_large")
                return
            body = self.rfile.read(length) if length else b""
            path = url.path
            if path == "/":
                self.reply(200, body=html.encode(), content_type="text/html; charset=utf-8",
                           headers=[("Content-Security-Policy", CSP)])
                return
            if path in ("/static/app.js", "/static/style.css"):
                state.static_queries[path] = url.query
                kind = "text/javascript" if path.endswith(".js") else "text/css"
                self.reply(200, body=(Path("/source/web/static") / Path(path).name).read_bytes(), content_type=kind)
                return
            if path == "/static/novnc-1.7.0-raven1/core/rfb.js":
                self.reply(200, body=RFB_FIXTURE.encode(), content_type="text/javascript")
                return
            if path == "/favicon.ico":
                self.reply(204, body=b"", content_type="image/x-icon")
                return
            if method != "GET" and self.headers.get("X-CSRFToken") != CSRF_TOKEN:
                state.problems.append(f"{method} {path} without the CSRF header")
                self.error(403, "csrf_token_invalid")
                return
            upload = UPLOAD_ROUTE.fullmatch(path)
            key = f"{method} {UPLOAD_ROUTE.sub(lambda m: '/api/files/uploads/*/' + (m.group(2) or ''), path)}"
            queued = state.failures.get(key)
            if queued:
                status, payload = queued.pop(0)
                with state.lock:
                    state.log.append(("failure", key, status))
                self.reply(status, payload)
                return
            if key == "GET /api/sessions/":
                self.reply(200, {"sessions": [SESSION]})
            elif key == "POST /api/sessions/41/open/":
                self.reply(200, {"name": "files", "password": "fixture-only", "websocket": "/ws/sessions/41/"})
            elif key == "GET /api/files/":
                self.list_files(query.get("path", [""])[0])
            elif key == "POST /api/files/uploads/":
                self.start_upload(json.loads(body))
            elif upload and not TOKEN.fullmatch(upload.group(1)):
                state.problems.append(f"bad token in {path}")
                self.error(400, "invalid_request")
            elif key == "PUT /api/files/uploads/*/":
                self.chunk(upload.group(1), query, body)
            elif key == "POST /api/files/uploads/*/finish/":
                self.finish_upload(upload.group(1), json.loads(body))
            elif key == "DELETE /api/files/uploads/*/":
                with state.lock:
                    dropped = state.uploads.pop(upload.group(1), None)
                    state.log.append(("cancel", upload.group(1), dropped and dropped["name"]))
                self.reply(200, {"cancelled": True})
            elif key == "GET /api/files/download/":
                self.download(query.get("path", [""])[0])
            else:
                state.problems.append(f"unexpected request {method} {self.path}")
                self.error(404, "not_found", "Unexpected fixture request")

        def list_files(self, path):
            with state.lock:
                state.log.append(("list", path))
                node = state.nodes.get(path)
                if node is None:
                    self.error(404, "not_found")
                elif node["type"] != "dir":
                    self.error(400, "not_a_directory")
                else:
                    self.reply(200, {"path": path, "entries": state.entries(path),
                                     "truncated": node.get("truncated", False), "skipped": node.get("skipped", 0)})

        def start_upload(self, request):
            if not (isinstance(request.get("overwrite"), bool) and isinstance(request.get("size"), int)
                    and isinstance(request.get("path"), str) and isinstance(request.get("name"), str)):
                state.problems.append(f"bad start body {request}")
            path, name = request["path"], request["name"]
            target = f"{path}/{name}" if path else name
            with state.lock:
                state.log.append(("start", name, request["overwrite"], path))
                directory = state.nodes.get(path)
                existing = state.nodes.get(target)
                if directory is None:
                    self.error(404, "not_found")
                elif directory["type"] != "dir":
                    self.error(400, "not_a_directory")
                elif not name or "/" in name or name in (".", ".."):
                    self.error(400, "invalid_path")
                elif existing and existing["type"] != "file":
                    self.error(400, "not_a_file")
                elif existing and not request["overwrite"]:
                    self.error(409, "file_exists", "A file with that name exists")
                else:
                    token = secrets.token_urlsafe(24)
                    state.uploads[token] = {"name": name, "target": target, "size": request["size"],
                                            "data": bytearray()}
                    self.reply(201, {"token": token, "chunk_size": CHUNK})

        def chunk(self, token, query, body):
            if self.headers.get("Content-Type") != "application/octet-stream":
                state.problems.append(f"chunk content type {self.headers.get('Content-Type')}")
            offset = int(query["offset"][0])
            with state.lock:
                state.log.append(("chunk", token, offset, len(body)))
                upload = state.uploads.get(token)
                if upload and upload["name"] in state.conflict_on_chunk:
                    # A session saves a file with the same name while the upload is running.
                    state.conflict_on_chunk.discard(upload["name"])
                    state.add(upload["target"], data=b"made in a session\n")
            if state.hold:
                state.held.set()
                state.release.wait(30)
            with state.lock:
                upload = state.uploads.get(token)
                if upload is None:
                    self.error(404, "upload_unknown")
                elif offset != len(upload["data"]):
                    self.error(409, "offset_mismatch")
                elif offset + len(body) > upload["size"] or len(body) > CHUNK:
                    self.error(400, "invalid_request")
                else:
                    upload["data"] += body
                    self.reply(200, {"received": len(upload["data"])})

        def finish_upload(self, token, request):
            with state.lock:
                upload = state.uploads.get(token)
                state.log.append(("finish", upload and upload["name"], request.get("overwrite")))
                existing = upload and state.nodes.get(upload["target"])
                if upload is None:
                    self.error(404, "upload_unknown")
                elif len(upload["data"]) != upload["size"]:
                    self.error(409, "offset_mismatch")
                elif existing and existing["type"] != "file":
                    self.error(400, "not_a_file")
                elif existing and request.get("overwrite") is not True:
                    self.error(409, "file_exists", "A file with that name exists")
                else:
                    del state.uploads[token]
                    state.add(upload["target"], data=bytes(upload["data"]))
                    self.reply(200, {"file": {"name": upload["name"], "size": upload["size"], "mtime": MTIME}})

        def download(self, path):
            with state.lock:
                state.log.append(("download", path))
                node = state.nodes.get(path)
            if node is None or node["type"] != "file":
                self.error(404, "not_found")
                return
            name = path.rsplit("/", 1)[-1]
            fallback = "".join(c if 32 <= ord(c) < 127 and c not in '"\\' else "_" for c in name)
            self.reply(200, body=node["data"], content_type="application/octet-stream", headers=[
                ("Content-Disposition", f"attachment; filename=\"{fallback}\"; filename*=UTF-8''{quote(name, safe='')}"),
                ("X-Content-Type-Options", "nosniff"),
            ])

    return Handler


def browser():
    from http.server import ThreadingHTTPServer
    from urllib.parse import parse_qs, urlsplit

    from playwright.sync_api import expect, sync_playwright

    options = json.load(sys.stdin)
    output = Path(options["output"])
    work = Path(tempfile.mkdtemp(prefix="files-ui-"))
    state = FakeFiles()
    state.add("프로젝트", "dir")
    state.add("프로젝트/sub", "dir")
    state.add("프로젝트/sub/deep.txt", data=b"deep\n")
    state.add("프로젝트/notes.md", data=b"# notes\n")
    state.add("many", "dir", truncated=True, skipped=2)
    state.add("many/0001.txt", data=b"1")
    state.add("기존.txt", data=b"old contents\n")
    state.add("screenshot 2026-11-13.png", data=bytes(150 * 1024))
    state.add("loop", "other")
    state.add("아주-긴-파일-이름-" * 6 + ".tar.gz", data=b"x" * 3_500_000)
    state.add("a" * 120 + ".log", data=b"log\n")
    root_count = len(state.entries(""))

    server = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(state, options["html"]))
    server.daemon_threads = True
    threading.Thread(target=server.serve_forever, daemon=True).start()
    origin = f"http://127.0.0.1:{server.server_address[1]}"
    expect.set_options(timeout=15000)
    errors = []
    results = {}

    def local(name, data):
        folder = work / secrets.token_hex(4)
        folder.mkdir()
        target = folder / name
        target.write_bytes(data)
        return str(target)

    def until(page, predicate, what, seconds=15):
        # Playwright's sync API only dispatches page events (dialogs) while a call into it is waiting.
        deadline = time.monotonic() + seconds
        while not predicate():
            assert time.monotonic() < deadline, f"timed out waiting for {what}"
            page.wait_for_timeout(50)

    def stored(path):
        with state.lock:
            node = state.nodes.get(path)
            return node and bytes(node["data"])

    def choose(page, *paths, tap=False):
        with page.expect_file_chooser() as chooser:
            if tap:
                page.locator("#files-upload").tap()
            else:
                page.locator("#files-upload").click()
        chooser.value.set_files(list(paths))

    def no_overflow(page):
        assert page.evaluate("document.documentElement.scrollWidth <= window.innerWidth"), "page overflows"
        assert page.evaluate("""() => [...document.querySelectorAll(
            'dialog[open], dialog[open] .files-browser, dialog[open] .upload-list, .viewer-toolbar')]
            .every(node => node.scrollWidth <= node.clientWidth)"""), "a panel overflows"
        assert page.evaluate("""() => [...document.querySelectorAll('dialog[open] .file-name, dialog[open] .upload-name')]
            .every(node => node.getBoundingClientRect().right <= node.closest('dialog').getBoundingClientRect().right)""")

    def watch(page):
        page.on("pageerror", lambda error: errors.append(str(error)))
        page.on("console", lambda message: errors.append(message.text) if message.type == "error" else None)

    def row(page, name):
        return page.locator(".upload-row").filter(has=page.locator(".upload-name", has_text=re.compile(f"^{re.escape(name)}$")))

    def done(page, name):
        expect(row(page, name)).to_have_attribute("data-state", "done", timeout=60000)
        expect(row(page, name).locator(".upload-percent")).to_have_text("100%")
        expect(row(page, name).locator(".upload-status")).to_have_text("완료")
        assert row(page, name).locator("progress").evaluate("node => node.value === node.max")

    with sync_playwright() as playwright:
        chromium = playwright.chromium.launch(chromium_sandbox=True)

        # ---- Desktop ----
        context = chromium.new_context(viewport={"width": 1366, "height": 860}, accept_downloads=True)
        page = context.new_page()
        watch(page)
        page.goto(origin + "/", wait_until="networkidle")
        expect(page.locator("#session-count")).to_have_text("1")
        assert state.static_queries == {"/static/app.js": "v=5", "/static/style.css": "v=5"}, state.static_queries
        dialog = page.locator("#files-dialog")
        rows = page.locator("#files-list .file-row")
        crumbs = page.locator("#files-breadcrumb li")

        page.locator("#files-open").click()
        expect(dialog).to_be_visible()
        expect(page.locator("#files-intro")).to_have_text(INTRO)
        expect(page.locator("#files-upload")).to_be_focused()
        expect(rows).to_have_count(root_count)
        expect(crumbs).to_have_text(["Downloads"])
        expect(page.locator("#files-target")).to_have_text("Downloads")
        expect(page.locator("#files-uploads")).to_be_hidden()
        expect(rows.nth(0)).to_have_class(re.compile(r"\bfile-dir\b"))
        expect(rows.nth(0).locator(".file-name")).to_have_text("many")
        shot = rows.filter(has_text="screenshot 2026-11-13.png")
        expect(shot.locator(".file-size")).to_have_text("150 KiB")
        expect(rows.filter(has_text="아주-긴").locator(".file-size")).to_have_text("3.3 MiB")
        expect(shot.locator(".file-time")).to_have_text(re.compile(r"^\d{4}-\d\d-\d\d \d\d:\d\d$"))
        link = page.locator("#files-list a.file-name", has_text="기존.txt")
        href = urlsplit(link.get_attribute("href"))
        assert href.path == "/api/files/download/" and parse_qs(href.query) == {"path": ["기존.txt"]}, href
        assert link.get_attribute("download") == "기존.txt"
        other = rows.filter(has_text="loop")
        expect(other).to_have_class(re.compile(r"\bfile-other\b"))
        assert other.locator(".file-name").evaluate("node => node.tagName") == "SPAN"
        assert other.locator(".file-name").evaluate("node => getComputedStyle(node).color") == "rgb(119, 130, 140)"
        before = len(state.log)
        other.locator(".file-name").click()
        page.wait_for_timeout(200)
        assert len(state.log) == before, state.log[before:]
        no_overflow(page)
        page.screenshot(path=str(output / "files-desktop.png"))
        results["listing"] = "passed"

        with page.expect_download() as info:
            link.click()
        download = info.value
        assert download.suggested_filename == "기존.txt", download.suggested_filename
        assert Path(download.path()).read_bytes() == b"old contents\n"
        assert ("download", "기존.txt") in state.log
        results["download"] = "passed"

        rows.filter(has_text="many").locator(".file-name").click()
        expect(crumbs).to_have_text(["Downloads", "many"])
        expect(page.locator("#files-note")).to_have_text(
            "항목이 너무 많아 1개만 보입니다. 이름순으로 고른 것이 아닐 수 있으니 전체는 세션의 파일 관리자에서 확인해 주세요."
            " 이름을 읽을 수 없는 항목 2개는 목록에서 뺐습니다.")
        crumbs.nth(0).locator("button").click()
        expect(crumbs).to_have_text(["Downloads"])
        expect(page.locator("#files-note")).to_be_hidden()
        rows.filter(has_text="프로젝트").locator(".file-name").click()
        expect(crumbs).to_have_text(["Downloads", "프로젝트"])
        rows.filter(has_text="sub").locator(".file-name").click()
        expect(crumbs).to_have_text(["Downloads", "프로젝트", "sub"])
        expect(page.locator("#files-breadcrumb [aria-current=location]")).to_have_text("sub")
        expect(page.locator("#files-target")).to_have_text("Downloads/프로젝트/sub")
        sub_link = page.locator("#files-list a.file-name", has_text="deep.txt")
        assert parse_qs(urlsplit(sub_link.get_attribute("href")).query) == {"path": ["프로젝트/sub/deep.txt"]}
        crumbs.nth(1).locator("button").click()
        expect(crumbs).to_have_text(["Downloads", "프로젝트"])
        expect(page.locator("#files-target")).to_have_text("Downloads/프로젝트")
        results["breadcrumb"] = "passed"

        # Two files at once into 프로젝트: a three-chunk file with a Korean name, then a small one.
        big_name = "보고서 최종 (1).bin"
        big = os.urandom(2 * CHUNK + 123_457)
        small = "작은 파일 메모.txt".encode() * 100
        choose(page, local(big_name, big), local("작은 파일.txt", small))
        done(page, big_name)
        done(page, "작은 파일.txt")
        assert hashlib.sha256(stored(f"프로젝트/{big_name}")).hexdigest() == hashlib.sha256(big).hexdigest()
        assert stored("프로젝트/작은 파일.txt") == small
        starts = [entry for entry in state.events("start") if entry[1] in (big_name, "작은 파일.txt")]
        assert starts == [("start", big_name, False, "프로젝트"), ("start", "작은 파일.txt", False, "프로젝트")], starts
        with state.lock:
            order = [entry for entry in state.log if entry[0] in ("start", "finish") and entry[1] in (big_name, "작은 파일.txt")]
        assert [entry[:2] for entry in order] == [("start", big_name), ("finish", big_name),
                                                  ("start", "작은 파일.txt"), ("finish", "작은 파일.txt")], order
        chunks = [entry[2:] for entry in state.events("chunk")]
        assert chunks[:3] == [(0, CHUNK), (CHUNK, CHUNK), (2 * CHUNK, 123_457)], chunks
        expect(row(page, big_name).locator(".upload-destination")).to_have_text("Downloads/프로젝트에 올립니다")
        expect(page.locator("#files-list a.file-name", has_text=big_name)).to_be_visible()
        expect(page.locator("#files-list .file-row", has_text=big_name).locator(".file-size")).to_have_text("8.1 MiB")
        assert not state.uploads, state.uploads
        results["multi_chunk_upload"] = "passed"

        # Cancel in the middle: hold the first chunk's response, check progress, the unload guard, then cancel.
        crumbs.nth(0).locator("button").click()
        expect(crumbs).to_have_text(["Downloads"])
        state.hold = True
        state.held.clear()
        state.release.clear()
        choose(page, local("취소할 파일.bin", os.urandom(9 * 1024 * 1024)))
        cancel_row = row(page, "취소할 파일.bin")
        until(page, state.held.is_set, "the held chunk")
        expect(cancel_row).to_have_attribute("data-state", "uploading")
        expect(cancel_row.locator(".upload-percent")).to_have_text("44%")
        expect(cancel_row.locator(".upload-status")).to_have_text("올리는 중 · 4.0 MiB / 9.0 MiB")
        no_overflow(page)
        page.screenshot(path=str(output / "files-uploading.png"))
        dialogs = []
        page.once("dialog", lambda prompt: (dialogs.append(prompt.type), prompt.dismiss()))
        page.close(run_before_unload=True)
        until(page, lambda: dialogs, "the beforeunload prompt", 5)
        assert dialogs == ["beforeunload"] and not page.is_closed(), dialogs
        token = next(entry[1] for entry in state.events("chunk") if entry[2] == 0 and entry[1] in state.uploads)
        cancel_row.locator(".upload-cancel").click()
        expect(cancel_row).to_have_attribute("data-state", "cancelled")
        expect(cancel_row.locator(".upload-status")).to_have_text("취소함")
        expect(cancel_row.locator(".upload-cancel")).to_be_hidden()
        until(page, lambda: ("cancel", token, "취소할 파일.bin") in state.log, "the DELETE")
        state.hold = False
        state.release.set()
        page.wait_for_timeout(300)
        assert stored("취소할 파일.bin") is None
        assert not state.uploads, state.uploads
        assert [entry for entry in state.events("chunk") if entry[1] == token] == [("chunk", token, 0, CHUNK)]
        results["cancel"] = "passed"

        # Same name at start: overwrite, then skip.
        choose(page, local("기존.txt", b"new contents\n"))
        existing_row = row(page, "기존.txt")
        expect(existing_row).to_have_attribute("data-state", "conflict")
        expect(existing_row.locator(".upload-status")).to_have_text("같은 이름이 있습니다")
        expect(existing_row.locator(".upload-overwrite")).to_be_visible()
        expect(existing_row.locator(".upload-skip")).to_be_visible()
        expect(existing_row.locator(".upload-cancel")).to_be_hidden()
        expect(existing_row.locator(".upload-overwrite")).to_be_in_viewport()
        page.screenshot(path=str(output / "files-conflict.png"))
        existing_row.locator(".upload-overwrite").click()
        done(page, "기존.txt")
        assert stored("기존.txt") == b"new contents\n"
        assert [entry for entry in state.events("start") if entry[1] == "기존.txt"] == [
            ("start", "기존.txt", False, ""), ("start", "기존.txt", True, "")]
        assert ("finish", "기존.txt", True) in state.log
        page.locator("#files-clear-uploads").click()
        expect(page.locator("#files-uploads")).to_be_hidden()
        expect(page.locator("#files-upload")).to_be_focused()
        cancels = len(state.events("cancel"))
        choose(page, local("기존.txt", b"third\n"))
        expect(row(page, "기존.txt")).to_have_attribute("data-state", "conflict")
        row(page, "기존.txt").locator(".upload-skip").click()
        expect(row(page, "기존.txt")).to_have_attribute("data-state", "skipped")
        expect(row(page, "기존.txt").locator(".upload-status")).to_have_text("건너뜀")
        page.wait_for_timeout(200)
        assert stored("기존.txt") == b"new contents\n"
        assert len(state.events("cancel")) == cancels and not state.uploads
        results["overwrite_at_start"] = "passed"

        # Same name appears while uploading (finish answers file_exists): overwrite, then skip.
        state.conflict_on_chunk.update({"경합.bin", "경합2.bin"})
        race = os.urandom(5000)
        choose(page, local("경합.bin", race))
        expect(row(page, "경합.bin")).to_have_attribute("data-state", "conflict")
        assert stored("경합.bin") == b"made in a session\n"
        row(page, "경합.bin").locator(".upload-overwrite").click()
        done(page, "경합.bin")
        assert stored("경합.bin") == race
        assert [entry for entry in state.events("finish") if entry[1] == "경합.bin"] == [
            ("finish", "경합.bin", False), ("finish", "경합.bin", True)]
        assert not [entry for entry in state.events("start") if entry[1] == "경합.bin" and entry[2]]
        choose(page, local("경합2.bin", os.urandom(5000)))
        expect(row(page, "경합2.bin")).to_have_attribute("data-state", "conflict")
        row(page, "경합2.bin").locator(".upload-skip").click()
        expect(row(page, "경합2.bin")).to_have_attribute("data-state", "skipped")
        until(page, lambda: any(entry[2] == "경합2.bin" for entry in state.events("cancel")), "the DELETE after skip")
        assert stored("경합2.bin") == b"made in a session\n" and not state.uploads
        results["overwrite_at_finish"] = "passed"

        # Korean error messages and retry.
        state.failures["POST /api/files/uploads/"] = [(507, {"error": "No space left", "code": "no_space"})]
        choose(page, local("재시도.txt", b"retry\n"))
        failed = row(page, "재시도.txt")
        expect(failed).to_have_attribute("data-state", "failed")
        expect(failed.locator(".upload-status")).to_have_text(
            "서버의 디스크 공간이 부족합니다. 필요 없는 파일을 지운 뒤 다시 시도해 주세요.")
        expect(page.locator("#files-clear-uploads")).to_be_visible()
        failed.locator(".upload-retry").click()
        done(page, "재시도.txt")
        assert stored("재시도.txt") == b"retry\n"
        state.failures["PUT /api/files/uploads/*/"] = [(409, {"error": "Offset mismatch", "code": "offset_mismatch"})]
        choose(page, local("순서.txt", b"order\n"))
        expect(row(page, "순서.txt")).to_have_attribute("data-state", "failed")
        expect(row(page, "순서.txt").locator(".upload-status")).to_have_text("업로드 순서가 어긋났습니다. 다시 올려 주세요.")
        expect(row(page, "순서.txt").locator(".upload-percent")).to_have_text("0%")
        until(page, lambda: any(entry[2] == "순서.txt" for entry in state.events("cancel")), "the DELETE after a failed chunk")
        state.failures["GET /api/files/"] = [(404, {"error": "Not found", "code": "not_found"}),
                                             (409, {"error": "Downloads is missing", "code": "downloads_missing"})]
        page.locator("#files-refresh").click()
        expect(page.locator("#files-error")).to_have_text(
            "파일이나 폴더를 찾지 못했습니다. 다른 곳에서 옮기거나 지웠을 수 있으니 목록을 새로고침해 주세요.")
        expect(rows).to_have_count(0)
        page.locator("#files-refresh").click()
        expect(page.locator("#files-error")).to_contain_text("서버에 ~/Downloads 폴더가 없습니다.")
        page.locator("#files-refresh").click()
        expect(page.locator("#files-error")).to_be_hidden()
        expect(rows.filter(has_text="재시도.txt")).to_have_count(1)
        assert not state.uploads, state.uploads
        results["errors"] = "passed"

        # The viewer's button opens the same dialog; closing it does not stop an upload.
        page.locator("#files-dialog [data-close-dialog]").click()
        expect(dialog).to_be_hidden()
        page.locator(".session-open").click()
        expect(page.locator("#connection-status")).to_have_text("연결됨")
        page.locator("#viewer-files-open").click()
        expect(dialog).to_be_visible()
        state.hold = True
        state.held.clear()
        state.release.clear()
        background = os.urandom(CHUNK + 10)
        choose(page, local("닫은 뒤 계속.bin", background))
        until(page, state.held.is_set, "the held chunk")
        upload_rows = page.locator(".upload-row").count()
        page.keyboard.press("Escape")
        expect(dialog).to_be_hidden()
        state.hold = False
        state.release.set()
        until(page, lambda: stored("닫은 뒤 계속.bin") == background, "the upload with the dialog closed")
        expect(page.locator("#viewer")).to_be_visible()
        page.locator("#viewer-files-open").click()
        expect(dialog).to_be_visible()
        done(page, "닫은 뒤 계속.bin")
        expect(page.locator(".upload-row")).to_have_count(upload_rows)
        expect(row(page, "재시도.txt")).to_have_attribute("data-state", "done")
        expect(rows.filter(has_text="닫은 뒤 계속.bin")).to_have_count(1)
        no_overflow(page)
        page.screenshot(path=str(output / "files-viewer-desktop.png"))
        page.locator("#files-dialog [data-close-dialog]").click()
        expect(page.locator("#connection-status")).to_have_text("연결됨")
        results["viewer_and_background_upload"] = "passed"

        dialogs.clear()
        page.on("dialog", lambda prompt: (dialogs.append(prompt.type), prompt.accept()))
        with page.expect_event("close", timeout=5000):
            page.close(run_before_unload=True)
        assert dialogs == [], dialogs
        results["unload_guard"] = "passed"
        context.close()

        # ---- Phone ----
        phone = chromium.new_context(viewport={"width": 412, "height": 780}, device_scale_factor=2,
                                     is_mobile=True, has_touch=True)
        page = phone.new_page()
        watch(page)
        page.goto(origin + "/", wait_until="networkidle")
        expect(page.locator("#session-count")).to_have_text("1")
        no_overflow(page)
        page.locator("#files-open").tap()
        dialog = page.locator("#files-dialog")
        rows = page.locator("#files-list .file-row")
        expect(dialog).to_be_visible()
        expect(rows.filter(has_text="기존.txt")).to_have_count(1)
        box = dialog.bounding_box()
        assert 360 <= box["width"] <= 380 and box["x"] >= 16, box
        file_row = rows.filter(has_text="screenshot 2026-11-13.png")
        name_box = file_row.locator(".file-name").bounding_box()
        meta_box = file_row.locator(".file-meta").bounding_box()
        assert meta_box["y"] >= name_box["y"] + name_box["height"] - 1, (name_box, meta_box)
        assert file_row.locator(".file-meta").evaluate("node => parseFloat(getComputedStyle(node).fontSize)") <= 11
        long_row = rows.filter(has_text="a" * 40)
        assert long_row.locator(".file-name").evaluate("node => node.scrollWidth <= node.clientWidth")
        assert long_row.locator(".file-name").bounding_box()["height"] > 40
        no_overflow(page)
        page.screenshot(path=str(output / "files-phone.png"))
        rows.filter(has_text="프로젝트").locator(".file-name").tap()
        expect(page.locator("#files-breadcrumb li")).to_have_text(["Downloads", "프로젝트"])
        phone_name = "휴대폰에서 올린 아주 긴 이름의 사진 파일 2026-11-13 오후 3시 42분 (편집본).jpg"
        phone_data = os.urandom(CHUNK + 4096)
        choose(page, local(phone_name, phone_data), tap=True)
        done(page, phone_name)
        assert stored(f"프로젝트/{phone_name}") == phone_data
        choose(page, local("notes.md", b"# phone\n"), tap=True)
        expect(row(page, "notes.md")).to_have_attribute("data-state", "conflict")
        expect(row(page, "notes.md").locator(".upload-skip")).to_be_in_viewport()
        no_overflow(page)
        page.screenshot(path=str(output / "files-phone-upload.png"))
        row(page, "notes.md").locator(".upload-skip").tap()
        expect(row(page, "notes.md")).to_have_attribute("data-state", "skipped")
        assert stored("프로젝트/notes.md") == b"# notes\n"
        phone_link = page.locator("#files-list a.file-name", has_text=phone_name)
        assert parse_qs(urlsplit(phone_link.get_attribute("href")).query) == {"path": [f"프로젝트/{phone_name}"]}
        page.locator("#files-dialog [data-close-dialog]").tap()
        expect(dialog).to_be_hidden()
        page.locator(".session-open").tap()
        expect(page.locator("#connection-status")).to_have_text("연결됨")
        expect(page.locator("#viewer-files-open")).to_be_visible()
        no_overflow(page)
        page.screenshot(path=str(output / "files-phone-viewer.png"))
        page.locator("#viewer-files-open").tap()
        expect(dialog).to_be_visible()
        expect(page.locator(".upload-row")).to_have_count(2)
        no_overflow(page)
        page.screenshot(path=str(output / "files-phone-viewer-dialog.png"))
        results["phone"] = "passed"
        phone.close()
        chromium.close()

    server.shutdown()
    assert not state.problems, state.problems
    # Chromium logs every fixture error status as a console error; anything else is a page fault.
    script_errors = [error for error in errors if not error.startswith("Failed to load resource")]
    assert not script_errors, script_errors
    print(json.dumps({"files_ui": results, "browser_errors": len(script_errors)}, ensure_ascii=False), flush=True)


if __name__ == "__main__":
    browser() if sys.argv[1:] == ["--browser"] else host()
