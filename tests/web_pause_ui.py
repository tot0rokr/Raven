"""Check pause and resume in the session list and viewer with a fake API, a fake RFB module and a paused page clock.

Run through `python3 tests/web_ui_check.py`, which renders index.html and passes it on stdin. No gateway, broker,
login or session is touched.
"""

from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import re
import sys
import traceback
from urllib.parse import urlparse

from playwright.sync_api import Error as PlaywrightError, expect, sync_playwright

ORIGIN = "https://raven.test"
CSRF_TOKEN = "ui-fixture"
SOURCE = Path("/source")
# The page asks for the certificate when it loads; one far from expiry shows no notice.
CERTIFICATE = {"names": ["raven.test"], "not_before": "2025-12-01T00:00:00+00:00",
               "not_after": "2026-12-01T00:00:00+00:00", "days_left": 300,
               "fingerprint": ":".join(["5A"] * 32), "origin_covered": True}
IDLE_MS = 120_000
CLOCK_START = datetime(2026, 1, 1, tzinfo=timezone.utc)
PHONE = {"width": 412, "height": 915}
LIST = "GET /api/sessions/"
PAUSED_COLOR = "rgb(229, 207, 155)"
PAUSE_HELP = ("일시정지는 앱을 멈춘 채 메모리에 둡니다. RAM은 계속 차지하고, 서버를 재부팅하면 상태가 사라집니다. "
              "run --host로 띄운 호스트 앱과 네트워크 상대편 연결은 멈추거나 유지되지 않습니다.")
PAUSED_OFFER = "세션이 일시정지되었습니다. 이어하기를 누르면 다시 연결할 수 있습니다."
RECONNECT_NOTICE = "화면 연결이 끊어졌습니다. 자동으로 다시 연결합니다."
MESSAGES = {
    "session_paused": "세션이 일시정지되어 있습니다. 이어하기를 누른 뒤 다시 시도해 주세요.",
    "session_not_paused": "세션이 일시정지 상태가 아닙니다. 다른 곳에서 이미 이어했거나 중지했을 수 있습니다.",
    "session_not_running": "실행 중인 세션만 일시정지할 수 있습니다. 세션이 중지되었거나 다른 곳에서 바뀌었을 수 있습니다.",
    "csrf_token_invalid": "페이지가 만료되었습니다. 새로고침한 뒤 다시 시도해 주세요.",
    "broker_unavailable": "호스트 중계에 연결할 수 없습니다. 서버에서 raven web doctor로 확인해 주세요.",
    "network": "서버에 연결하지 못했습니다. 네트워크 연결을 확인해 주세요.",
}
RFB_FIXTURE = """
export default class RFB extends EventTarget {
  constructor(target, url, options) {
    super();
    this.url = url;
    this.password = options.credentials.password;
    this.closed = false;
    window.rfbProbe.instances.push(this);
    queueMicrotask(() => this.dispatchEvent(new Event('connect')));
  }
  drop() { this.dispatchEvent(new Event('disconnect')); }
  focus() {}
  disconnect() { this.closed = true; this.dispatchEvent(new Event('disconnect')); }
  sendCtrlAltDel() {}
}
"""
# The server closes every viewer before it answers a pause; this does the same to the page's own viewer.
DROP_ON_PAUSE = """() => {
  const original = window.fetch;
  window.fetch = (input, init) => {
    if (String(input).endsWith('/pause/')) window.rfbProbe.instances.at(-1).drop();
    return original(input, init);
  };
}"""
API_ROUTE = re.compile(r"/api/sessions/([0-9]+)/(open|pause|resume|run)/")

options = json.load(sys.stdin)
output = Path(options["output"])
output.mkdir(parents=True, exist_ok=True)


def fixture_sessions():
    base = {"wm": "xmonad", "geometry": "1600x900", "command": "xterm"}
    rows = [
        ("41", "work", "running", 1288490189, 3.2, {"x11": 1, "web": 1}),
        ("42", "frozen", "paused", 2147483648, 0.0, {"x11": 0, "web": 0}),
        ("43", "stopped", "exited", None, None, {"x11": 0, "web": 0}),
        ("44", "quiet", "running", None, None, None),
        ("45", "busy", "running", 536870912, 0.2, {"x11": 0, "web": 2}),
    ]
    return [{**base, "id": sid, "name": name, "state": state, "display": str(100 + int(sid)),
             "generation": sid[-1] * 64, "memory": memory, "cpu": cpu, "viewers": viewers}
            for sid, name, state, memory, cpu, viewers in rows]


def key(method, sid, action):
    return f"{method} /api/sessions/{sid}/{action}/"


class Desk:
    """One page with its own fake API state, paused clock and request log."""

    def __init__(self, browser, viewport=None):
        self.sessions = fixture_sessions()
        self.requests = []
        self.bodies = []
        self.failures = {}
        self.hold = set()
        self.held = []
        self.passwords = 0
        self.errors = []
        self.problems = []
        self.unexpected = []
        self.static_queries = {}
        self.context = browser.new_context(viewport=viewport or {"width": 1280, "height": 900})
        self.page = self.context.new_page()
        self.page.clock.install(time=CLOCK_START)
        self.page.add_init_script("window.rfbProbe = {instances: []};")
        self.page.route("**/*", self.serve)
        self.page.on("pageerror", lambda error: self.errors.append(str(error)))
        self.page.on("console", lambda message: self.errors.append(message.text) if message.type == "error" else None)
        self.page.goto(ORIGIN, wait_until="commit")
        expect(self.page.locator("#session-count")).to_have_text(str(len(self.sessions)))
        self.page.clock.pause_at(CLOCK_START + timedelta(minutes=1))
        self.status = self.page.locator("#connection-status")
        self.viewer_notice = self.page.locator("#viewer-notice")
        self.offer = self.page.locator("#viewer-paused")
        self.retry = self.page.locator("#reconnect-viewer")
        self.pause_viewer = self.page.locator("#pause-viewer")
        self.resume_viewer = self.page.locator("#resume-viewer")
        self.info = self.page.locator("#workspace-info")
        self.workspace_notice = self.page.locator("#workspace-notice")

    def find(self, sid):
        return next((item for item in self.sessions if item["id"] == sid), None)

    def set_state(self, sid, state):
        self.find(sid)["state"] = state

    def serve(self, route):
        request = route.request
        url = urlparse(request.url)
        if url.netloc != "raven.test":
            self.unexpected.append(request.url)
            route.abort()
            return
        path = url.path
        if path == "/":
            route.fulfill(content_type="text/html", body=options["html"])
        elif path in ("/static/app.js", "/static/style.css"):
            self.static_queries[path] = url.query
            body = (SOURCE / "web/static" / Path(path).name).read_text()
            route.fulfill(content_type="text/javascript" if path.endswith(".js") else "text/css", body=body)
        elif path == "/static/novnc-1.7.0-raven1/core/rfb.js":
            route.fulfill(content_type="text/javascript", body=RFB_FIXTURE)
        elif path == "/favicon.ico":
            route.fulfill(status=204)
        elif path == "/api/certificate/" and request.method == "GET":
            route.fulfill(json=CERTIFICATE)
        elif path == "/login/" and request.method == "GET":
            self.requests.append("GET /login/")
            route.fulfill(content_type="text/html", body="<!doctype html><title>login fixture</title>")
        elif path.startswith("/api/"):
            self.api(route, f"{request.method} {path}")
        else:
            self.unexpected.append(f"{request.method} {path}")
            route.fulfill(status=404, body="")

    def api(self, route, request_key):
        request = route.request
        self.requests.append(request_key)
        body = request.post_data_json if request.post_data else None
        if request.method != "GET":
            if request.headers.get("x-csrftoken") != CSRF_TOKEN:
                self.problems.append(f"{request_key}: CSRF header {request.headers.get('x-csrftoken')!r}")
            if body is not None and request.headers.get("content-type") != "application/json":
                self.problems.append(f"{request_key}: content type {request.headers.get('content-type')!r}")
            self.bodies.append((request_key, body))
        if request_key in self.hold:
            self.hold.discard(request_key)
            self.held.append((route, request_key, body))
            return
        self.respond(route, request_key, body)

    def release(self):
        """Answer the requests held back with `hold`."""
        held, self.held = self.held, []
        for route, request_key, body in held:
            self.respond(route, request_key, body)

    def respond(self, route, request_key, body):
        request = route.request
        queued = self.failures.get(request_key)
        if queued:
            failure = queued.pop(0)
            if failure == "abort":
                route.abort("internetdisconnected")
            else:
                route.fulfill(status=failure[0], json=failure[1])
            return
        if request_key == LIST:
            route.fulfill(json={"sessions": self.sessions})
            return
        match = API_ROUTE.fullmatch(urlparse(request.url).path)
        session = match and self.find(match.group(1))
        if not session or request.method != "POST":
            self.unexpected.append(request_key)
            route.fulfill(status=404, json={"error": "Unexpected fixture request"})
            return
        action = match.group(2)
        if action in ("pause", "resume", "run") and (body or {}).get("generation") != session["generation"]:
            self.problems.append(f"{request_key}: generation {body!r}")
        if action == "open":
            if session["state"] == "paused":
                route.fulfill(status=409, json={"error": "Session is paused", "code": "session_paused"})
                return
            self.passwords += 1
            route.fulfill(json={"name": session["name"], "password": f"fixture-{self.passwords}",
                                "websocket": f"/ws/sessions/{session['id']}/"})
        elif action == "run":
            if session["state"] == "paused":
                route.fulfill(status=409, json={"error": "Session is paused", "code": "session_paused"})
                return
            route.fulfill(status=202, json={"started": True})
        elif action == "pause":
            if body != {"generation": session["generation"]}:
                self.problems.append(f"{request_key}: body {body!r}")
            if session["state"] == "paused":
                route.fulfill(status=409, json={"error": "Session is paused", "code": "session_paused"})
            elif session["state"] != "running":
                route.fulfill(status=409, json={"error": "Session is not running", "code": "session_not_running"})
            else:
                session.update(state="paused", cpu=0.0, viewers={"x11": 0, "web": 0})
                route.fulfill(json={"session": self.describe(session)})
        else:
            if body != {"generation": session["generation"]}:
                self.problems.append(f"{request_key}: body {body!r}")
            if session["state"] != "paused":
                route.fulfill(status=409, json={"error": "Session is not paused", "code": "session_not_paused"})
            else:
                session.update(state="running", cpu=1.0)
                route.fulfill(json={"session": self.describe(session)})

    @staticmethod
    def describe(session):
        return {name: session[name] for name in
                ("id", "generation", "name", "state", "wm", "geometry", "command", "display")}

    def fail(self, request_key, *responses):
        self.failures.setdefault(request_key, []).extend(responses)

    def card(self, name):
        return self.page.locator(".session-card").filter(has=self.page.locator(".session-name", has_text=name))

    def rfb(self, expression):
        return self.page.evaluate(f"window.rfbProbe.{expression}")

    def settle(self):
        self.page.wait_for_timeout(300)

    def open(self, name):
        self.card(name).locator(".session-open").click()
        expect(self.status).to_have_text("연결됨")
        expect(self.pause_viewer).to_be_visible()
        expect(self.pause_viewer).to_be_enabled()

    def assert_idle(self):
        """Nothing may reach the API while the clock runs well past the longest reconnect delay."""
        before = list(self.requests)
        self.page.clock.run_for(IDLE_MS)
        self.settle()
        assert self.requests == before, self.requests[len(before):]

    def assert_no_overflow(self):
        assert self.page.evaluate("document.documentElement.scrollWidth <= innerWidth"), "the page scrolls sideways"
        assert self.page.evaluate("""() => [...document.querySelectorAll(
            '.session-card, .session-actions, .pause-help, #workspace-info, .viewer-toolbar, .viewer-paused')]
            .filter(node => node.getClientRects().length)
            .every(node => node.scrollWidth <= node.clientWidth)"""), "a panel overflows"
        assert self.page.evaluate("""() => [...document.querySelectorAll('.session-card button')]
            .filter(node => node.getClientRects().length)
            .every(node => {
              const card = node.closest('.session-card').getBoundingClientRect();
              const box = node.getBoundingClientRect();
              return box.left >= card.left && box.right <= card.right;
            })"""), "a card button sticks out of its card"

    def shot(self, name):
        self.page.screenshot(path=str(output / name))

    def close(self):
        assert not self.problems, self.problems
        assert not self.unexpected, self.unexpected
        # Chromium logs each fixture error status and aborted request; anything else is a page fault.
        script_errors = [error for error in self.errors if not error.startswith("Failed to load resource")]
        assert not script_errors, script_errors
        self.context.close()


def expect_card(desk, name, state, stats, actions):
    """Check one card's state label, stats line and which actions it offers."""
    card = desk.card(name)
    expect(card.locator(".session-state")).to_have_text(state)
    if stats is None:
        expect(card.locator(".session-stats")).to_be_hidden()
    else:
        expect(card.locator(".session-stats")).to_have_text(stats)
    controls = {"open": ".session-open", "resume": ".session-resume", "pause": ".session-pause"}
    for action, selector in controls.items():
        if action in actions:
            expect(card.locator(selector)).to_be_visible()
            expect(card.locator(selector)).to_be_enabled()
        else:
            expect(card.locator(selector)).to_be_hidden()
    run = card.locator("[data-action=run]")
    if "run" in actions:
        expect(run).to_be_enabled()
    else:
        expect(run).to_be_disabled()
        expect(run).to_have_attribute("title", re.compile("이어하기"))
    expect(card.locator("[data-action=rename]")).to_be_enabled()
    expect(card.locator("[data-action=delete]")).to_be_enabled()


def cards_show_state_and_stats(desk):
    assert desk.static_queries == {"/static/app.js": "v=7", "/static/style.css": "v=7"}, desk.static_queries
    expect_card(desk, "work", "실행 중", "메모리 1.2 GiB · CPU 3% · 보는 중 X11 1 · 웹 1", {"open", "pause", "run"})
    expect_card(desk, "frozen", "일시정지", "메모리 2.0 GiB 유지 중 · 서버를 재부팅하면 사라집니다", {"resume"})
    expect_card(desk, "stopped", "중지됨", None, {"open", "run"})
    expect_card(desk, "quiet", "실행 중", None, {"open", "pause", "run"})
    expect_card(desk, "busy", "실행 중", "메모리 512 MiB · CPU <1% · 보는 중 X11 0 · 웹 2", {"open", "pause", "run"})
    paused = desk.card("frozen").locator(".session-state")
    expect(paused).to_have_class(re.compile(r"\bpaused\b"))
    assert paused.evaluate("node => getComputedStyle(node).color") == PAUSED_COLOR
    running = desk.card("work").locator(".session-state").evaluate("node => getComputedStyle(node).color")
    stopped = desk.card("stopped").locator(".session-state").evaluate("node => getComputedStyle(node).color")
    error_color = desk.page.evaluate("""() => {
      const probe = document.createElement('p');
      probe.className = 'notice';
      document.body.append(probe);
      const color = getComputedStyle(probe).color;
      probe.remove();
      return color;
    }""")
    assert len({PAUSED_COLOR, running, stopped, error_color}) == 4, (running, stopped, error_color)
    expect(desk.card("frozen").locator(".session-stats")).to_have_class(re.compile(r"\bpaused\b"))
    expect(desk.page.locator(".pause-help:visible")).to_have_count(0)
    desk.assert_no_overflow()
    desk.shot("pause-cards-desktop.png")
    desk.page.set_viewport_size({"width": 820, "height": 900})
    desk.assert_no_overflow()
    desk.shot("pause-cards-tablet.png")

    desk.find("42")["memory"] = None
    desk.find("41").update(memory=None, viewers={"x11": 2})
    desk.page.locator("#refresh-sessions").click()
    expect_card(desk, "frozen", "일시정지", "메모리에 유지 중 · 서버를 재부팅하면 사라집니다", {"resume"})
    expect_card(desk, "work", "실행 중", "CPU 3% · 보는 중 X11 2", {"open", "pause", "run"})

    desk.page.set_viewport_size(PHONE)
    desk.assert_no_overflow()
    desk.shot("pause-cards-phone.png")
    assert desk.requests == [LIST, LIST], desk.requests
    return {"cards": len(desk.sessions), "layouts": ["1280", "820", "412"]}


def pause_from_card(desk):
    card = desk.card("work")
    card.locator(".session-pause").click()
    expect_card(desk, "work", "일시정지", "메모리 1.2 GiB 유지 중 · 서버를 재부팅하면 사라집니다", {"resume"})
    expect(desk.page.locator("#loading-status")).to_have_text("")
    assert desk.requests == [LIST, key("POST", "41", "pause"), LIST], desk.requests
    assert desk.bodies == [(key("POST", "41", "pause"), {"generation": "1" * 64})], desk.bodies
    help_box = card.locator(".pause-help")
    expect(help_box).to_be_visible()
    expect(help_box.locator("p")).to_have_text(PAUSE_HELP)
    expect(card.locator(".session-resume")).to_be_focused()
    expect(desk.workspace_notice).to_be_hidden()
    expect(desk.info).to_be_hidden()
    expect(desk.page.locator(".pause-help:visible")).to_have_count(1)
    desk.assert_no_overflow()
    desk.shot("pause-help-desktop.png")
    desk.page.set_viewport_size(PHONE)
    desk.assert_no_overflow()
    card.scroll_into_view_if_needed()
    desk.shot("pause-help-phone.png")

    help_box.locator(".pause-help-dismiss").click()
    expect(help_box).to_be_hidden()
    expect(card.locator(".session-resume")).to_be_focused()
    assert desk.page.evaluate("localStorage.getItem('raven.pause-help-seen')") == "1"
    # The explanation is shown once per browser, not once per session.
    desk.card("busy").locator(".session-pause").click()
    expect_card(desk, "busy", "일시정지", "메모리 512 MiB 유지 중 · 서버를 재부팅하면 사라집니다", {"resume"})
    expect(desk.page.locator(".pause-help:visible")).to_have_count(0)
    desk.page.locator("#refresh-sessions").click()
    expect(desk.page.locator("#loading-status")).to_have_text("")
    expect(desk.page.locator(".pause-help:visible")).to_have_count(0)
    desk.assert_idle()
    assert desk.requests == [LIST, key("POST", "41", "pause"), LIST, key("POST", "45", "pause"), LIST, LIST]
    return {"pauses": 2, "help_shown": 1}


def resume_from_card(desk):
    card = desk.card("frozen")
    card.locator(".session-resume").click()
    expect_card(desk, "frozen", "실행 중", "메모리 2.0 GiB · CPU 1% · 보는 중 X11 0 · 웹 0", {"open", "pause", "run"})
    expect(card.locator(".session-open")).to_be_focused()
    expect(desk.workspace_notice).to_be_hidden()
    expect(desk.page.locator("#viewer")).to_be_hidden()
    assert desk.requests == [LIST, key("POST", "42", "resume"), LIST], desk.requests
    assert desk.bodies == [(key("POST", "42", "resume"), {"generation": "2" * 64})], desk.bodies
    desk.assert_idle()
    return {"open_calls": 0}


def pause_from_viewer(desk):
    desk.open("work")
    desk.assert_no_overflow()
    desk.shot("pause-viewer-toolbar.png")
    desk.page.evaluate(DROP_ON_PAUSE)
    desk.hold.add(key("POST", "41", "pause"))
    desk.pause_viewer.click()
    # The server has already closed the viewer; the page waits for the pause instead of reconnecting.
    expect(desk.status).to_have_text("일시정지하는 중…")
    expect(desk.viewer_notice).to_be_hidden()
    expect(desk.retry).to_be_hidden()
    expect(desk.pause_viewer).to_be_disabled()
    desk.page.clock.run_for(30_000)
    desk.settle()
    assert desk.requests == [LIST, key("POST", "41", "open"), key("POST", "41", "pause")], desk.requests
    desk.release()
    expect(desk.page.locator("#workspace")).to_be_visible()
    expect(desk.page.locator("#viewer")).to_be_hidden()
    expect(desk.info).to_have_text("work 세션을 일시정지했습니다. 이어하기를 누르면 멈춘 그대로 이어집니다.")
    assert desk.info.evaluate("node => getComputedStyle(node).color") != desk.workspace_notice.evaluate(
        "node => getComputedStyle(node).color")
    expect(desk.workspace_notice).to_be_hidden()
    card = desk.card("work")
    expect_card(desk, "work", "일시정지", "메모리 1.2 GiB 유지 중 · 서버를 재부팅하면 사라집니다", {"resume"})
    expect(card.locator(".pause-help")).to_be_visible()
    expect(card.locator(".session-resume")).to_be_focused()
    desk.assert_no_overflow()
    desk.shot("pause-viewer-back-to-list.png")
    # The viewer dropped by the pause must not come back on its own.
    desk.assert_idle()
    assert desk.requests == [LIST, key("POST", "41", "open"), key("POST", "41", "pause"), LIST], desk.requests
    assert desk.rfb("instances.length") == 1
    expect(desk.status).to_have_text("연결 종료")
    return {"reconnects": 0}


def pause_from_viewer_on_phone(desk):
    desk.open("busy")
    desk.assert_no_overflow()
    desk.shot("pause-viewer-toolbar-phone.png")
    desk.pause_viewer.click()
    expect(desk.page.locator("#workspace")).to_be_visible()
    expect(desk.info).to_have_text("busy 세션을 일시정지했습니다. 이어하기를 누르면 멈춘 그대로 이어집니다.")
    expect(desk.card("busy").locator(".pause-help")).to_be_visible()
    desk.assert_no_overflow()
    desk.shot("pause-viewer-back-to-list-phone.png")
    desk.assert_idle()
    assert desk.rfb("instances[0].closed")
    assert desk.requests == [LIST, key("POST", "45", "open"), key("POST", "45", "pause"), LIST], desk.requests
    # Opening a session clears the notice.
    desk.open("work")
    expect(desk.info).to_be_hidden()
    return {"reconnects": 0}


def expect_offer(desk):
    expect(desk.offer).to_be_visible()
    expect(desk.offer.locator("p")).to_have_text(PAUSED_OFFER)
    expect(desk.resume_viewer).to_be_visible()
    expect(desk.resume_viewer).to_be_enabled()
    expect(desk.status).to_have_text("일시정지됨")
    expect(desk.status).to_have_class(re.compile(r"\bpaused\b"))
    expect(desk.viewer_notice).to_be_hidden()
    expect(desk.retry).to_be_hidden()
    expect(desk.pause_viewer).to_be_hidden()


def resume_and_connect(desk, sid, instances):
    desk.resume_viewer.click()
    expect(desk.status).to_have_text("연결됨")
    expect(desk.offer).to_be_hidden()
    expect(desk.viewer_notice).to_be_hidden()
    expect(desk.pause_viewer).to_be_visible()
    expect(desk.pause_viewer).to_be_enabled()
    assert desk.rfb("instances.length") == instances
    assert desk.rfb("instances.at(-1).url") == f"wss://raven.test/ws/sessions/{sid}/"
    assert desk.rfb("instances.at(-1).password") == f"fixture-{desk.passwords}"


def reconnect_stops_when_paused(desk):
    desk.open("work")
    desk.set_state("41", "paused")
    desk.rfb("instances[0].drop()")
    expect(desk.viewer_notice).to_have_text(RECONNECT_NOTICE)
    with desk.page.expect_request(lambda request: urlparse(request.url).path == "/api/sessions/"):
        desk.page.clock.run_for(1000)
    expect_offer(desk)
    desk.assert_idle()
    desk.shot("reconnect-paused.png")
    desk.page.set_viewport_size(PHONE)
    desk.assert_no_overflow()
    desk.shot("reconnect-paused-phone.png")
    resume_and_connect(desk, "41", 2)
    assert desk.requests == [LIST, key("POST", "41", "open"), LIST, key("POST", "41", "resume"),
                             key("POST", "41", "open")], desk.requests
    assert desk.find("41")["state"] == "running"
    return {"auto_reconnects_after_pause": 0}


def open_paused_offers_resume(desk):
    # The card still says running; someone paused the session since the list was loaded.
    desk.set_state("41", "paused")
    desk.card("work").locator(".session-open").click()
    expect_offer(desk)
    desk.assert_idle()
    resume_and_connect(desk, "41", 1)
    assert desk.requests == [LIST, key("POST", "41", "open"), key("POST", "41", "resume"),
                             key("POST", "41", "open")], desk.requests
    # The list may say running while the open that follows meets a session paused in between.
    desk.rfb("instances[0].drop()")
    desk.fail(key("POST", "41", "open"), (409, {"error": "Session is paused", "code": "session_paused"}))
    with desk.page.expect_request(lambda request: urlparse(request.url).path == "/api/sessions/41/open/"):
        desk.page.clock.run_for(1000)
    expect_offer(desk)
    desk.assert_idle()
    return {"open_refusals": 2}


def errors_show_korean_messages(desk):
    # Pause on a card whose session stopped since the list loaded.
    desk.set_state("41", "exited")
    desk.card("work").locator(".session-pause").click()
    expect(desk.workspace_notice).to_have_text(MESSAGES["session_not_running"])
    expect_card(desk, "work", "중지됨", None, {"open", "run"})
    # Resume on a card whose session someone else resumed.
    desk.set_state("42", "running")
    desk.card("frozen").locator(".session-resume").click()
    expect(desk.workspace_notice).to_have_text(MESSAGES["session_not_paused"])
    expect(desk.card("frozen").locator(".session-state")).to_have_text("실행 중")
    # A session someone else already paused is where the pause wanted it: no error.
    desk.set_state("45", "paused")
    desk.card("busy").locator(".session-pause").click()
    expect(desk.card("busy").locator(".session-state")).to_have_text("일시정지")
    expect(desk.workspace_notice).to_be_hidden()
    # A command for a session paused since the list loaded.
    desk.set_state("44", "paused")
    desk.card("quiet").locator("[data-action=run]").click()
    desk.page.locator("#run-command").fill("xclock")
    desk.page.locator("#run-form button[type=submit]").click()
    expect(desk.page.locator("#run-error")).to_have_text(MESSAGES["session_paused"])
    desk.page.locator("#run-dialog [data-close-dialog]").click()
    # A pause that never reached the server leaves the card as it was.
    desk.set_state("44", "running")
    desk.page.locator("#refresh-sessions").click()
    expect(desk.card("quiet").locator(".session-state")).to_have_text("실행 중")
    desk.fail(key("POST", "44", "pause"), "abort")
    desk.card("quiet").locator(".session-pause").click()
    expect(desk.workspace_notice).to_have_text(MESSAGES["network"])
    expect(desk.card("quiet").locator(".session-pause")).to_have_text("일시정지")
    expect(desk.card("quiet").locator(".session-pause")).to_be_focused()
    expect_card(desk, "quiet", "실행 중", None, {"open", "pause", "run"})
    desk.fail(key("POST", "44", "pause"), (502, {"error": "The Raven broker is unavailable",
                                                 "code": "broker_unavailable"}))
    desk.card("quiet").locator(".session-pause").click()
    expect(desk.workspace_notice).to_have_text(MESSAGES["broker_unavailable"])
    expect_card(desk, "quiet", "실행 중", None, {"open", "pause", "run"})
    desk.assert_no_overflow()
    desk.shot("pause-error-card.png")

    # Viewer pause refused while the viewer stays connected.
    desk.open("quiet")
    desk.fail(key("POST", "44", "pause"), (403, {"error": "The page expired.", "code": "csrf_token_invalid"}))
    desk.pause_viewer.click()
    expect(desk.viewer_notice).to_have_text(MESSAGES["csrf_token_invalid"])
    expect(desk.status).to_have_text("연결됨")
    expect(desk.pause_viewer).to_be_enabled()
    expect(desk.page.locator("#viewer")).to_be_visible()
    # Viewer pause refused after the server already dropped the viewer: reconnect as before.
    desk.page.evaluate(DROP_ON_PAUSE)
    desk.fail(key("POST", "44", "pause"), (502, {"error": "The Raven broker is unavailable",
                                                 "code": "broker_unavailable"}))
    desk.pause_viewer.click()
    expect(desk.viewer_notice).to_have_text(MESSAGES["broker_unavailable"])
    expect(desk.status).to_have_text("다시 연결 중 (1/10, 1초 후)")
    with desk.page.expect_request(lambda request: urlparse(request.url).path == "/api/sessions/44/open/"):
        desk.page.clock.run_for(1000)
    expect(desk.status).to_have_text("연결됨")
    # Viewer pause of a session someone else already paused shows the offer.
    desk.set_state("44", "paused")
    desk.pause_viewer.click()
    expect_offer(desk)
    # Resume refused by the broker keeps the offer.
    desk.fail(key("POST", "44", "resume"), (502, {"error": "The Raven broker is unavailable",
                                                  "code": "broker_unavailable"}))
    desk.resume_viewer.click()
    expect(desk.viewer_notice).to_have_text(MESSAGES["broker_unavailable"])
    expect(desk.offer).to_be_visible()
    expect(desk.status).to_have_text("일시정지됨")
    desk.shot("pause-error-resume.png")
    # Resume of a session that is no longer paused ends the connection with the reason and a manual retry.
    desk.set_state("44", "running")
    desk.resume_viewer.click()
    expect(desk.viewer_notice).to_have_text(MESSAGES["session_not_paused"])
    expect(desk.status).to_have_text("연결 종료")
    expect(desk.retry).to_be_visible()
    expect(desk.offer).to_be_hidden()
    desk.assert_idle()
    return {"messages": 7}


def login_expiry_during_pause(desk):
    desk.fail(key("POST", "41", "pause"), (401, {"error": "Authentication required", "code": "auth_required"}))
    desk.card("work").locator(".session-pause").click()
    desk.page.wait_for_url(ORIGIN + "/login/")
    assert desk.requests == [LIST, key("POST", "41", "pause"), "GET /login/"], desk.requests
    return {"redirect": "/login/"}


SCENARIOS = (
    (cards_show_state_and_stats, None),
    (pause_from_card, None),
    (resume_from_card, None),
    (pause_from_viewer, None),
    (pause_from_viewer_on_phone, PHONE),
    (reconnect_stops_when_paused, None),
    (open_paused_offers_resume, None),
    (errors_show_korean_messages, None),
    (login_expiry_during_pause, None),
)

failed = []
with sync_playwright() as playwright:
    browser = playwright.chromium.launch(chromium_sandbox=True)
    for scenario, viewport in SCENARIOS:
        desk = Desk(browser, viewport)
        try:
            detail = scenario(desk)
            desk.close()
            print(json.dumps({"scenario": scenario.__name__, "result": "passed", **detail}), flush=True)
        except Exception as error:
            failed.append(scenario.__name__)
            try:
                desk.page.screenshot(path=str(output / f"failed-{scenario.__name__}.png"))
            except PlaywrightError:
                pass
            print(json.dumps({"scenario": scenario.__name__, "result": "failed",
                              "error": f"{type(error).__name__}: {str(error)[:300]}"}, ensure_ascii=False), flush=True)
            traceback.print_exc()
            desk.context.close()
    browser.close()

print(json.dumps({"pause_ui": "failed" if failed else "passed", "scenarios": len(SCENARIOS), "failed": failed}))
if failed:
    raise SystemExit(1)
