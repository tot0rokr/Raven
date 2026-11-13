"""Check automatic viewer reconnect with a fake RFB module, fake APIs and a paused page clock."""

from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import sys
import traceback
from urllib.parse import urlparse

from playwright.sync_api import Error as PlaywrightError, expect, sync_playwright

ORIGIN = "https://raven.test"
CSRF_TOKEN = "ui-fixture"
SOURCE = Path("/source")
GENERATION = "a" * 64
DELAYS_MS = (1000, 2000, 4000, 8000, 15000, 30000, 30000, 30000, 30000, 30000)
MAX_ATTEMPTS = len(DELAYS_MS)
IDLE_MS = 120_000
SETTLE_MS = 300
CLOCK_START = datetime(2026, 1, 1, tzinfo=timezone.utc)
LIST = "GET /api/sessions/"
OPEN_41 = "POST /api/sessions/41/open/"
RECONNECT_NOTICE = "화면 연결이 끊어졌습니다. 자동으로 다시 연결합니다."
BROKER_NOTICE = "호스트 중계에 연결할 수 없습니다. 서버에서 raven web doctor로 확인해 주세요."
DELETED_NOTICE = "세션이 삭제되었거나 다시 만들어졌습니다. 세션 목록에서 다시 선택해 주세요."
STOPPED_NOTICE = "세션이 중지되어 자동으로 다시 연결하지 않습니다. 다시 연결하면 세션을 시작합니다."
EXHAUSTED_NOTICE = "화면에 다시 연결하지 못했습니다. 네트워크와 서버 상태를 확인한 뒤 다시 연결해 주세요."
RFB_FIXTURE = """
export default class RFB extends EventTarget {
  constructor(target, url, options) {
    super();
    this.url = url;
    this.password = options.credentials.password;
    this.closed = false;
    window.rfbProbe.instances.push(this);
    const outcome = window.rfbProbe.outcomes.shift() || 'connect';
    queueMicrotask(() => this.finish(outcome));
  }
  finish(outcome) {
    if (outcome === 'connect') this.dispatchEvent(new Event('connect'));
    if (outcome === 'securityfailure') this.dispatchEvent(new CustomEvent('securityfailure', {detail: {status: 1}}));
    if (outcome === 'credentialsrequired') this.dispatchEvent(new Event('credentialsrequired'));
    if (outcome === 'close' || outcome === 'securityfailure') this.dispatchEvent(new Event('disconnect'));
  }
  drop() { this.dispatchEvent(new Event('disconnect')); }
  focus() {}
  disconnect() { this.closed = true; this.dispatchEvent(new Event('disconnect')); }
  sendCtrlAltDel() {}
}
"""

options = json.load(sys.stdin)
output = Path(options["output"])
output.mkdir(parents=True, exist_ok=True)


def session(sid, name):
    return {"id": sid, "name": name, "state": "running", "wm": "xmonad", "geometry": "1600x900",
            "display": str(100 + int(sid)), "generation": GENERATION, "command": "xterm"}


class Desk:
    """One page with its own fake API state, paused clock and request log."""

    def __init__(self, browser):
        self.sessions = [session("41", "reconnect"), session("43", "other")]
        self.requests = []
        self.failures = {}
        self.passwords = 0
        self.errors = []
        self.unexpected = []
        self.context = browser.new_context(viewport={"width": 1280, "height": 900})
        self.page = self.context.new_page()
        self.page.clock.install(time=CLOCK_START)
        self.page.add_init_script("window.rfbProbe = {instances: [], outcomes: []};")
        self.page.route("**/*", self.serve)
        self.page.on("pageerror", lambda error: self.errors.append(str(error)))
        self.page.on("console", lambda message: self.errors.append(message.text) if message.type == "error" else None)
        self.page.goto(ORIGIN, wait_until="commit")
        expect(self.page.locator("#session-count")).to_have_text("2")
        self.page.clock.pause_at(CLOCK_START + timedelta(minutes=1))
        self.status = self.page.locator("#connection-status")
        self.notice = self.page.locator("#viewer-notice")
        self.retry = self.page.locator("#reconnect-viewer")

    def serve(self, route):
        request = route.request
        url = urlparse(request.url)
        if url.netloc != "raven.test":
            self.unexpected.append(request.url)
            route.abort()
            return
        path = url.path
        key = f"{request.method} {path}"
        if path == "/":
            route.fulfill(content_type="text/html", body=options["html"])
        elif path in ("/static/app.js", "/static/style.css"):
            body = (SOURCE / "web/static" / Path(path).name).read_text()
            route.fulfill(content_type="text/javascript" if path.endswith(".js") else "text/css", body=body)
        elif path == "/static/novnc-1.7.0-raven1/core/rfb.js":
            route.fulfill(content_type="text/javascript", body=RFB_FIXTURE)
        elif path == "/favicon.ico":
            route.fulfill(status=204)
        elif key == "GET /login/":
            self.requests.append(key)
            route.fulfill(content_type="text/html", body="<!doctype html><title>login fixture</title>")
        elif path.startswith("/api/"):
            self.api(route, key)
        else:
            self.unexpected.append(key)
            route.fulfill(status=404, body="")

    def api(self, route, key):
        request = route.request
        self.requests.append(key)
        if request.method != "GET":
            assert request.headers.get("x-csrftoken") == CSRF_TOKEN, key
        queued = self.failures.get(key)
        if queued:
            failure = queued.pop(0)
            if failure == "abort":
                route.abort("internetdisconnected")
            else:
                route.fulfill(status=failure[0], json=failure[1])
            return
        if key == LIST:
            route.fulfill(json={"sessions": self.sessions})
            return
        sid = key.split("/")[3]
        if key == f"POST /api/sessions/{sid}/open/" and any(item["id"] == sid for item in self.sessions):
            self.passwords += 1
            route.fulfill(json={"name": sid, "password": f"fixture-{self.passwords}",
                                "websocket": f"/ws/sessions/{sid}/"})
            return
        self.unexpected.append(key)
        route.fulfill(status=404, json={"error": "Unexpected fixture request"})

    def fail(self, key, *responses):
        self.failures.setdefault(key, []).extend(responses)

    def count(self, key):
        return self.requests.count(key)

    def settle(self):
        self.page.wait_for_timeout(SETTLE_MS)

    def rfb(self, expression):
        return self.page.evaluate(f"window.rfbProbe.{expression}")

    def open(self, name="reconnect"):
        self.page.locator(".session-card").filter(has_text=name).locator(".session-open").click()
        expect(self.status).to_have_text("연결됨")
        expect(self.retry).to_be_hidden()

    def drop(self):
        self.rfb("instances.at(-1).drop()")

    def waiting(self, number, delay_ms):
        return f"다시 연결 중 ({number}/{MAX_ATTEMPTS}, {delay_ms // 1000}초 후)"

    def run_attempt(self, number, delay_ms):
        """Check the countdown, then that the attempt starts exactly when its delay ends."""
        expect(self.status).to_have_text(self.waiting(number, delay_ms))
        expect(self.retry).to_be_visible()
        before = list(self.requests)
        if delay_ms > 1000:
            self.page.clock.run_for(delay_ms - 1000)
            expect(self.status).to_have_text(self.waiting(number, 1000))
        self.page.clock.run_for(999)
        self.settle()
        assert self.requests == before, (number, self.requests[len(before):])
        with self.page.expect_request(lambda request: urlparse(request.url).path == "/api/sessions/"):
            self.page.clock.run_for(1)

    def assert_idle(self):
        """Nothing may reach the API while the clock runs well past the longest delay."""
        before = list(self.requests)
        self.page.clock.run_for(IDLE_MS)
        self.settle()
        assert self.requests == before, self.requests[len(before):]

    def close(self):
        assert not self.unexpected, self.unexpected
        # Chromium logs each fixture error status and aborted request; anything else is a page fault.
        script_errors = [error for error in self.errors if not error.startswith("Failed to load resource")]
        assert not script_errors, script_errors
        self.context.close()


def reconnects_with_fresh_credentials(desk):
    desk.open()
    assert desk.rfb("instances[0].password") == "fixture-1"
    desk.drop()
    expect(desk.notice).to_have_text(RECONNECT_NOTICE)
    desk.page.screenshot(path=str(output / "reconnect-waiting.png"))
    desk.page.set_viewport_size({"width": 390, "height": 844})
    assert desk.page.evaluate("""() => [...document.querySelectorAll('#viewer, .viewer-toolbar')]
        .every(node => node.scrollWidth <= node.clientWidth)""")
    desk.page.screenshot(path=str(output / "reconnect-waiting-mobile.png"))
    desk.run_attempt(1, 1000)
    expect(desk.status).to_have_text("연결됨")
    expect(desk.notice).to_be_hidden()
    expect(desk.retry).to_be_hidden()
    assert desk.rfb("instances.length") == 2
    assert desk.rfb("instances[1].password") == "fixture-2"
    assert desk.rfb("instances[1].url") == "wss://raven.test/ws/sessions/41/"
    desk.drop()
    desk.run_attempt(1, 1000)
    expect(desk.status).to_have_text("연결됨")
    assert desk.rfb("instances[2].password") == "fixture-3"
    assert desk.requests == [LIST, OPEN_41, LIST, OPEN_41, LIST, OPEN_41], desk.requests
    return {"attempts_after_success": 1, "passwords": desk.passwords}


def backs_off_then_stops_at_the_limit(desk):
    desk.open()
    desk.drop()
    desk.fail(LIST, "abort")
    desk.fail(OPEN_41, (502, {"error": "The Raven broker is unavailable", "code": "broker_unavailable"}))
    for number, delay_ms in enumerate(DELAYS_MS, start=1):
        if number == 3:
            desk.rfb("outcomes.push('close')")
        if number >= 4:
            desk.fail(LIST, "abort")
        desk.run_attempt(number, delay_ms)
        if number == 1:
            expect(desk.status).to_have_text(desk.waiting(2, DELAYS_MS[1]))
            expect(desk.notice).to_have_text(RECONNECT_NOTICE)
        if number == 2:
            expect(desk.status).to_have_text(desk.waiting(3, DELAYS_MS[2]))
            expect(desk.notice).to_have_text(BROKER_NOTICE)
    expect(desk.status).to_have_text("연결 실패")
    expect(desk.notice).to_have_text(EXHAUSTED_NOTICE)
    expect(desk.retry).to_be_visible()
    assert desk.count(LIST) == 1 + MAX_ATTEMPTS
    assert desk.count(OPEN_41) == 3
    assert desk.rfb("instances.length") == 2
    desk.assert_idle()
    desk.page.screenshot(path=str(output / "reconnect-exhausted.png"))
    with desk.page.expect_request(ORIGIN + "/api/sessions/41/open/"):
        desk.retry.click()
    expect(desk.status).to_have_text("연결됨")
    desk.drop()
    expect(desk.status).to_have_text(desk.waiting(1, DELAYS_MS[0]))
    return {"delays_ms": list(DELAYS_MS), "attempts": MAX_ATTEMPTS}


def stops_when_login_expires(desk):
    desk.open()
    desk.drop()
    desk.fail(LIST, (401, {"error": "Authentication required", "code": "auth_required"}))
    desk.run_attempt(1, 1000)
    desk.page.wait_for_url(ORIGIN + "/login/")
    assert desk.requests == [LIST, OPEN_41, LIST, "GET /login/"], desk.requests
    return {"redirect": "/login/"}


def stops_when_the_session_is_deleted(desk):
    desk.open()
    desk.drop()
    desk.sessions.pop(0)
    desk.run_attempt(1, 1000)
    expect(desk.notice).to_have_text(DELETED_NOTICE)
    expect(desk.status).to_have_text("연결 종료")
    expect(desk.retry).to_be_hidden()
    desk.assert_idle()
    assert desk.count(OPEN_41) == 1
    return {"open_calls": 1}


def stops_when_the_session_is_recreated(desk):
    desk.open()
    desk.drop()
    desk.sessions[0] = {**desk.sessions[0], "generation": "c" * 64}
    desk.run_attempt(1, 1000)
    expect(desk.notice).to_have_text(DELETED_NOTICE)
    expect(desk.retry).to_be_hidden()
    desk.assert_idle()
    assert desk.count(OPEN_41) == 1
    return {"open_calls": 1}


def does_not_start_a_stopped_session(desk):
    desk.open()
    desk.drop()
    desk.sessions[0] = {**desk.sessions[0], "state": "exited"}
    desk.run_attempt(1, 1000)
    expect(desk.notice).to_have_text(STOPPED_NOTICE)
    expect(desk.status).to_have_text("연결 종료")
    expect(desk.retry).to_be_visible()
    desk.assert_idle()
    assert desk.count(OPEN_41) == 1
    desk.page.screenshot(path=str(output / "reconnect-stopped.png"))
    return {"open_calls": 1}


def user_close_cancels_reconnect(desk):
    desk.open()
    desk.drop()
    expect(desk.status).to_have_text(desk.waiting(1, 1000))
    desk.page.locator("#close-viewer").click()
    expect(desk.page.locator("#workspace")).to_be_visible()
    expect(desk.page.locator("#loading-status")).to_have_text("")
    desk.assert_idle()
    desk.open("other")
    desk.page.locator("#close-viewer").click()
    expect(desk.page.locator("#loading-status")).to_have_text("")
    desk.assert_idle()
    assert desk.requests == [LIST, OPEN_41, LIST, "POST /api/sessions/43/open/", LIST], desk.requests
    assert desk.page.evaluate("window.rfbProbe.instances.map(instance => instance.closed)") == [False, True]
    return {"open_calls": 2}


def logout_cancels_reconnect(desk):
    desk.open()
    desk.drop()
    expect(desk.status).to_have_text(desk.waiting(1, 1000))
    # The viewer covers the header, so submit the form directly. Cancel its navigation to keep the page
    # alive while the clock runs; the app's own submit listener still runs.
    desk.page.evaluate("""() => {
      const form = document.querySelector('#logout-form');
      form.addEventListener('submit', event => event.preventDefault());
      form.requestSubmit();
    }""")
    expect(desk.status).to_have_text("연결 종료")
    desk.assert_idle()
    assert desk.requests == [LIST, OPEN_41], desk.requests
    return {"open_calls": 1}


def pagehide_cancels_reconnect(desk):
    desk.open()
    desk.drop()
    expect(desk.status).to_have_text(desk.waiting(1, 1000))
    desk.page.evaluate("dispatchEvent(new PageTransitionEvent('pagehide'))")
    expect(desk.status).to_have_text("연결 종료")
    desk.assert_idle()
    assert desk.requests == [LIST, OPEN_41], desk.requests
    return {"open_calls": 1}


def online_retries_immediately(desk):
    desk.open()
    before = list(desk.requests)
    desk.page.evaluate("dispatchEvent(new Event('online'))")
    desk.settle()
    assert desk.requests == before, "online must not reconnect a live connection"
    desk.drop()
    desk.fail(LIST, "abort")
    desk.run_attempt(1, 1000)
    expect(desk.status).to_have_text(desk.waiting(2, 2000))
    with desk.page.expect_request(ORIGIN + "/api/sessions/41/open/"):
        desk.page.evaluate("dispatchEvent(new Event('online'))")
    expect(desk.status).to_have_text("연결됨")
    desk.assert_idle()
    assert desk.requests == [LIST, OPEN_41, LIST, LIST, OPEN_41], desk.requests
    return {"open_calls": 2}


def authentication_failures_do_not_reconnect(desk):
    desk.open()
    desk.drop()
    desk.rfb("outcomes.push('securityfailure')")
    desk.run_attempt(1, 1000)
    expect(desk.notice).to_have_text("데스크톱 인증에 실패했습니다. 다시 연결해 주세요.")
    expect(desk.status).to_have_text("연결 종료")
    expect(desk.retry).to_be_visible()
    desk.assert_idle()
    desk.page.locator("#close-viewer").click()
    expect(desk.page.locator("#loading-status")).to_have_text("")
    desk.rfb("outcomes.push('credentialsrequired')")
    desk.page.locator(".session-card").filter(has_text="reconnect").locator(".session-open").click()
    expect(desk.notice).to_have_text("데스크톱 인증 정보를 확인하지 못했습니다. 다시 연결해 주세요.")
    expect(desk.status).to_have_text("연결 종료")
    assert desk.rfb("instances.at(-1).closed")
    desk.assert_idle()
    assert desk.count(OPEN_41) == 3
    return {"open_calls": 3}


def client_errors_stop_reconnect(desk):
    desk.open()
    desk.drop()
    desk.fail(OPEN_41, (403, {"error": "The page expired. Reload it and try again.", "code": "csrf_token_invalid"}))
    desk.run_attempt(1, 1000)
    expect(desk.notice).to_have_text("페이지가 만료되었습니다. 새로고침한 뒤 다시 시도해 주세요.")
    expect(desk.status).to_have_text("연결 실패")
    desk.assert_idle()
    assert desk.count(OPEN_41) == 2
    return {"open_calls": 2}


SCENARIOS = (
    reconnects_with_fresh_credentials,
    backs_off_then_stops_at_the_limit,
    stops_when_login_expires,
    stops_when_the_session_is_deleted,
    stops_when_the_session_is_recreated,
    does_not_start_a_stopped_session,
    user_close_cancels_reconnect,
    logout_cancels_reconnect,
    pagehide_cancels_reconnect,
    online_retries_immediately,
    authentication_failures_do_not_reconnect,
    client_errors_stop_reconnect,
)

failed = []
with sync_playwright() as playwright:
    browser = playwright.chromium.launch(chromium_sandbox=True)
    for scenario in SCENARIOS:
        desk = Desk(browser)
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

print(json.dumps({"reconnect_ui": "failed" if failed else "passed", "scenarios": len(SCENARIOS), "failed": failed}))
if failed:
    raise SystemExit(1)
