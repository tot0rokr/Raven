"""Check the login devices button and dialog with a fake API, a fake RFB module and a fixed page clock.

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
# A fixed clock and a zone without daylight saving, so local times and "N분 전" are the same on every run.
NOW = datetime(2026, 11, 18, 3, 0, tzinfo=timezone.utc)
# The page clock stops here once the page has loaded; the relative times count from it.
SHOWN = NOW + timedelta(minutes=1)
ZONE_ID = "Asia/Seoul"
ZONE = timezone(timedelta(hours=9))
DESKTOP = {"width": 1280, "height": 900}
PHONE = {"width": 412, "height": 915}
NARROW = {"width": 360, "height": 780}
LIST = "GET /api/sessions/"
CERT = "GET /api/certificate/"
LOGINS = "GET /api/logins/"
BULK = "POST /api/logins/revoke/"
OPEN = "POST /api/sessions/41/open/"
UPLOAD = "POST /api/files/uploads/"
LOGIN_PAGE = "GET /login/"
# The page asks for the certificate when it loads; one far from expiry shows no notice.
CERTIFICATE = {"names": ["raven.test"], "not_before": "2025-12-01T00:00:00+00:00",
               "not_after": "2027-06-01T00:00:00+00:00", "days_left": 500,
               "fingerprint": ":".join(["5A"] * 32), "origin_covered": True}
SESSIONS = [{"id": "41", "name": "work", "state": "running", "wm": "xmonad", "geometry": "1600x900",
             "display": "141", "generation": "1" * 64, "command": "xterm"}]
INTRO = ("이 계정으로 로그인한 브라우저입니다. 해제하면 그 브라우저의 화면 연결이 바로 끊기고, "
         "다시 쓰려면 로그인해야 합니다.")
PRIVACY = ("접속 주소는 SSH 터널 뒤라 알 수 없어 남기지 않고, 브라우저와 OS 종류만 기록합니다. "
           "로그인이 끝나면 함께 지웁니다.")
LEAVE = "열린 화면 연결이 끊기고 로그인 화면으로 이동합니다."
LEAVING = "이 기기의 로그인을 해제했습니다. 로그인 화면으로 이동합니다."
MESSAGES = {
    "login_not_found": "이미 해제되었거나 만료된 로그인입니다. 목록을 다시 불러왔습니다.",
    "network": "서버에 연결하지 못했습니다. 네트워크 연결을 확인해 주세요.",
    "csrf_token_invalid": "페이지가 만료되었습니다. 새로고침한 뒤 다시 시도해 주세요.",
    "unknown": "요청을 처리하지 못했습니다. 다시 시도해 주세요.",
    "list_network": "로그인 목록을 불러오지 못했습니다. 네트워크 연결을 확인해 주세요.",
    "list_unavailable": "로그인 목록을 불러오지 못했습니다. 다시 시도해 주세요.",
}
CURRENT_GREEN = "rgb(182, 216, 188)"
CONFIRM_RED = "rgb(246, 192, 187)"
RFB_FIXTURE = """
export default class RFB extends EventTarget {
  constructor(target, url, options) {
    super();
    this.url = url;
    this.closed = false;
    window.rfbProbe.instances.push(this);
    queueMicrotask(() => this.dispatchEvent(new Event('connect')));
  }
  focus() {}
  disconnect() { this.closed = true; this.dispatchEvent(new Event('disconnect')); }
  sendCtrlAltDel() {}
  clipboardPasteFrom(text) { window.rfbProbe.pasted.push(text); }
}
"""
# A beforeunload listener that would stop the page from leaving answers a synthetic event with preventDefault.
UNLOAD_GUARDED = """() => {
  const event = new Event('beforeunload', {cancelable: true});
  dispatchEvent(event);
  return event.defaultPrevented;
}"""
# Playwright waits for a navigation in progress before it reads the page, so the page reports its own state when it
# starts to leave. Registered after the app's listeners, it also sees whether an unload guard stopped the leaving.
WATCH_LEAVING = """() => addEventListener('beforeunload', (event) => {
  const value = (id) => document.getElementById(id);
  console.info('raven-leaving ' + JSON.stringify({
    status: value('logins-status').textContent,
    dialog_open: value('logins-dialog').open,
    controls_locked: [...document.querySelectorAll('#logins-dialog button:not([data-close-dialog])')]
      .filter(node => node.getClientRects().length).every(node => node.disabled),
    viewers_closed: window.rfbProbe.instances.map(instance => instance.closed),
    connection: value('connection-status').textContent,
    clipboard_out: value('clipboard-outgoing').value,
    clipboard_in: value('clipboard-incoming').value,
    clipboard_hidden: value('clipboard-panel').hidden,
    clipboard_copy_disabled: value('clipboard-copy').disabled,
    desktop_children: value('desktop').childElementCount,
    viewer_notice_hidden: value('viewer-notice').hidden,
    reconnect_hidden: value('reconnect-viewer').hidden,
    unload_guarded: event.defaultPrevented,
  }));
})"""
REVOKE_ROUTE = re.compile(r"/api/logins/([^/]+)/revoke/")

options = json.load(sys.stdin)
output = Path(options["output"])
output.mkdir(parents=True, exist_ok=True)


def stamp(moment, zulu=False):
    text = moment.astimezone(timezone.utc).isoformat()
    return text.replace("+00:00", "Z") if zulu else text


def local(text):
    return datetime.fromisoformat(text.replace("Z", "+00:00")).astimezone(ZONE).strftime("%Y-%m-%d %H:%M")


def login(lid, browser, os_name, device, created, seen, current, connections, zulu=False):
    return {"id": lid, "browser": browser, "os": os_name, "device": device, "created": stamp(created, zulu),
            "seen": stamp(seen, zulu), "expires": stamp(created + timedelta(hours=8), zulu),
            "current": current, "connections": connections}


def fixture_logins():
    """Logins in the server's order (latest activity first); this browser's is the second."""
    return [
        login("fx-linux_1", "Firefox", "Linux", "desktop", NOW - timedelta(hours=5),
              SHOWN - timedelta(seconds=40), False, 2),
        login("this-chrome", "Chrome", "Windows", "desktop", NOW - timedelta(hours=2),
              SHOWN - timedelta(minutes=4), True, 1, zulu=True),
        login("galaxy-tab", "Samsung Internet", "Android", "mobile", NOW - timedelta(hours=7),
              SHOWN - timedelta(hours=2, minutes=10), False, 0),
        login("older-login", None, None, None, NOW - timedelta(hours=7, minutes=30),
              NOW - timedelta(hours=7, minutes=30), False, 0),
    ]


# What each row shows, in screen order: this browser first, then the server's order.
SHOWN_ROWS = [
    ("this-chrome", "Chrome · Windows", "데스크톱 · 화면 연결 1개", "4분 전"),
    ("fx-linux_1", "Firefox · Linux", "데스크톱 · 화면 연결 2개", "방금 전"),
    ("galaxy-tab", "Samsung Internet · Android", "모바일 · 화면 연결 0개", "2시간 전"),
    ("older-login", "알 수 없는 브라우저", "화면 연결 0개", "7시간 전"),
]


def revoke_key(lid):
    return f"POST /api/logins/{lid}/revoke/"


class Desk:
    """One page with its own fake API state, fixed clock and request log.

    `fail(key, ...)` queues answers for a request: "abort" is a network failure, (status, body) an answer. A key in
    `hold` is left unanswered until `release`. With `hold_login`, the login page is held the same way, so the page
    being left can still be inspected.
    """

    def __init__(self, browser, viewport):
        self.logins = fixture_logins()
        self.requests = []
        self.mutations = []
        self.failures = {}
        self.hold = set()
        self.held = []
        self.hold_login = False
        self.held_login = []
        self.dialogs = []
        self.leaving = []
        self.errors = []
        self.problems = []
        self.unexpected = []
        self.static_queries = {}
        self.context = browser.new_context(viewport=viewport, timezone_id=ZONE_ID)
        self.page = self.context.new_page()
        self.page.clock.install(time=NOW)
        self.page.add_init_script("window.rfbProbe = {instances: [], pasted: []};")
        self.page.route("**/*", self.serve)
        self.page.on("pageerror", lambda error: self.errors.append(str(error)))
        self.page.on("console", self.on_console)
        self.page.on("dialog", self.on_dialog)
        self.page.goto(ORIGIN, wait_until="commit")
        expect(self.page.locator("#session-count")).to_have_text(str(len(SESSIONS)))
        expect(self.page.locator("#loading-status")).to_have_text("")
        self.wait_for(CERT, 1)
        self.page.clock.pause_at(SHOWN)
        self.button = self.page.locator("#logins-open")
        self.dialog = self.page.locator("#logins-dialog")
        self.rows = self.page.locator("#logins-list .login-row")
        self.status = self.page.locator("#logins-status")
        self.error = self.page.locator("#logins-error")
        self.others = self.page.locator("#logins-revoke-others")
        self.all = self.page.locator("#logins-revoke-all")
        self.actions = self.page.locator("#logins-actions")
        self.confirm = self.page.locator("#logins-confirm")
        self.confirm_text = self.page.locator("#logins-confirm-text")
        self.cancel = self.page.locator("#logins-confirm-cancel")
        self.go = self.page.locator("#logins-confirm-go")
        self.refresh = self.page.locator("#logins-refresh")
        self.close_button = self.page.locator("#logins-dialog [data-close-dialog]")

    def on_console(self, message):
        if message.type == "error":
            self.errors.append(message.text)
        elif message.text.startswith("raven-leaving "):
            self.leaving.append(json.loads(message.text.removeprefix("raven-leaving ")))

    def on_dialog(self, dialog):
        # Staying is the answer that would show an unload guard left in place: the page would not reach /login/.
        self.dialogs.append(dialog.type)
        dialog.dismiss()

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
            self.static_queries[path] = url.query
            body = (SOURCE / "web/static" / Path(path).name).read_text()
            route.fulfill(content_type="text/javascript" if path.endswith(".js") else "text/css", body=body)
        elif path == "/static/novnc-1.7.0-raven1/core/rfb.js":
            route.fulfill(content_type="text/javascript", body=RFB_FIXTURE)
        elif path == "/favicon.ico":
            route.fulfill(status=204)
        elif key == LOGIN_PAGE:
            self.requests.append(key)
            if self.hold_login:
                self.held_login.append(route)
            else:
                route.fulfill(content_type="text/html", body="<!doctype html><title>login fixture</title>")
        elif path.startswith("/api/"):
            self.api(route, key)
        else:
            self.unexpected.append(key)
            route.fulfill(status=404, body="")

    def api(self, route, key):
        request = route.request
        self.requests.append(key)
        body = request.post_data_json if request.post_data else None
        if request.method != "GET":
            if request.headers.get("x-csrftoken") != CSRF_TOKEN:
                self.problems.append(f"{key}: CSRF header {request.headers.get('x-csrftoken')!r}")
            if body is not None and request.headers.get("content-type") != "application/json":
                self.problems.append(f"{key}: content type {request.headers.get('content-type')!r}")
            self.mutations.append((key, body))
        if key in self.hold:
            self.hold.discard(key)
            self.held.append((route, key, body))
            return
        self.respond(route, key, body)

    def release(self):
        held, self.held = self.held, []
        for route, key, body in held:
            self.respond(route, key, body)

    def release_login(self):
        held, self.held_login = self.held_login, []
        for route in held:
            route.fulfill(content_type="text/html", body="<!doctype html><title>login fixture</title>")

    def respond(self, route, key, body):
        queued = self.failures.get(key)
        if queued:
            failure = queued.pop(0)
            if failure == "abort":
                route.abort("internetdisconnected")
            else:
                route.fulfill(status=failure[0], json=failure[1])
            return
        match = REVOKE_ROUTE.fullmatch(urlparse(route.request.url).path)
        if key == LIST:
            route.fulfill(json={"sessions": SESSIONS})
        elif key == CERT:
            route.fulfill(json=CERTIFICATE)
        elif key == OPEN:
            route.fulfill(json={"name": "work", "password": "fixture-only", "websocket": "/ws/sessions/41/"})
        elif key == LOGINS:
            route.fulfill(json={"logins": self.logins})
        elif key == BULK:
            scope = (body or {}).get("scope")
            if body != {"scope": scope} or scope not in ("others", "all"):
                self.problems.append(f"{key}: body {body!r}")
            kept = [item for item in self.logins if scope == "others" and item["current"]]
            revoked = len(self.logins) - len(kept)
            self.logins = kept
            route.fulfill(json={"revoked": revoked, "current": scope == "all"})
        elif match and route.request.method == "POST":
            if body is not None:
                self.problems.append(f"{key}: body {body!r}")
            target = self.find(match.group(1))
            if not target:
                route.fulfill(status=404, json={"error": "Login not found", "code": "login_not_found"})
                return
            self.logins.remove(target)
            route.fulfill(json={"revoked": 1, "current": target["current"]})
        else:
            self.unexpected.append(key)
            route.fulfill(status=404, json={"error": "Unexpected fixture request"})

    def revokes(self):
        return [(key, body) for key, body in self.mutations if key.startswith("POST /api/logins/")]

    def confirm_and_leave(self):
        """Confirm a revoke that includes this browser and hold the login page; return the page's leaving state.

        The page clock runs in real time from here, past the first reconnect delay, so a reconnect the page should
        not make would reach the fake API before the login page is answered.
        """
        self.page.evaluate(WATCH_LEAVING)
        self.hold_login = True
        self.page.clock.resume()
        before = len(self.requests)
        self.go.click()
        self.wait_for(LOGIN_PAGE, 1)
        self.page.wait_for_timeout(2500)
        for _ in range(20):
            if self.leaving:
                break
            self.page.wait_for_timeout(100)
        assert len(self.leaving) == 1, self.leaving
        requests = self.requests[before:]
        self.release_login()
        self.page.wait_for_url(ORIGIN + "/login/")
        expect(self.page).to_have_title("login fixture")
        return self.leaving[0], requests

    def find(self, lid):
        return next((item for item in self.logins if item["id"] == lid), None)

    def fail(self, key, *answers):
        self.failures.setdefault(key, []).extend(answers)

    def count(self, key):
        return self.requests.count(key)

    def wait_for(self, key, count):
        for _ in range(50):
            if self.count(key) >= count:
                break
            self.page.wait_for_timeout(100)
        assert self.count(key) == count, (key, count, self.requests)

    def row(self, lid):
        return self.page.locator(f'#logins-list .login-row[data-login="{lid}"]')

    def open_dialog(self, keyboard=False):
        before = self.count(LOGINS)
        if keyboard:
            self.page.keyboard.press("Enter")
        else:
            self.button.click()
        expect(self.dialog).to_be_visible()
        self.wait_for(LOGINS, before + 1)
        expect(self.rows).to_have_count(len(self.logins))

    def close_with_escape(self):
        self.page.keyboard.press("Escape")
        expect(self.dialog).to_be_hidden()

    def assert_no_overflow(self):
        assert self.page.evaluate("document.documentElement.scrollWidth <= innerWidth"), "the page scrolls sideways"
        assert self.page.evaluate("""() => [...document.querySelectorAll(
            '.app-header, .account-controls, dialog[open], dialog[open] .logins-list, dialog[open] .login-row,'
            + ' dialog[open] .login-summary, dialog[open] .login-facts, dialog[open] .logins-actions,'
            + ' dialog[open] .logins-confirm')]
            .filter(node => node.getClientRects().length)
            .every(node => node.scrollWidth <= node.clientWidth)"""), "a panel overflows"
        assert self.page.evaluate("""() => [...document.querySelectorAll('.app-header *')]
            .filter(node => node.getClientRects().length)
            .every(node => {
              const box = node.getBoundingClientRect();
              return box.left >= 0 && box.right <= innerWidth;
            })"""), "a header control is cut off"
        assert self.page.evaluate("""() => {
            const dialog = document.querySelector('dialog[open]');
            if (!dialog) return true;
            const box = dialog.getBoundingClientRect();
            return [...dialog.querySelectorAll('*')].filter(node => node.getClientRects().length).every(node => {
              const rect = node.getBoundingClientRect();
              return rect.left >= box.left - 0.5 && rect.right <= box.right + 0.5;
            });
        }"""), "something in the dialog sticks out"
        assert self.page.evaluate("""() => [...document.querySelectorAll('dialog[open] .login-revoke')]
            .filter(node => node.getClientRects().length)
            .every(node => {
              const row = node.closest('.login-row').getBoundingClientRect();
              const box = node.getBoundingClientRect();
              return box.left >= row.left && box.right <= row.right;
            })"""), "a revoke button sticks out of its row"

    def shot(self, name):
        self.page.screenshot(path=str(output / name))

    def close(self):
        assert not self.problems, self.problems
        assert not self.unexpected, self.unexpected
        assert not self.dialogs, self.dialogs
        # Chromium logs each fixture error status and aborted request; anything else is a page fault.
        script_errors = [error for error in self.errors if not error.startswith("Failed to load resource")]
        assert not script_errors, script_errors
        # A request still held (the upload start the page left behind) is answered so no route stays pending.
        for route, _key, _body in self.held:
            try:
                route.abort()
            except PlaywrightError:
                pass
        self.held = []
        self.context.close()


def expect_rows(desk, rows):
    """The dialog lists `rows` (ids from SHOWN_ROWS) in that order with every field."""
    expected = [row for lid in rows for row in SHOWN_ROWS if row[0] == lid]
    expect(desk.rows).to_have_count(len(expected))
    assert desk.rows.evaluate_all("nodes => nodes.map(node => node.dataset.login)") == list(rows)
    expect(desk.rows.locator(".login-name")).to_have_text([row[1] for row in expected])
    expect(desk.rows.locator(".login-meta")).to_have_text([row[2] for row in expected])
    expect(desk.rows.locator(".login-seen")).to_have_text([row[3] for row in expected])
    for index, (lid, _name, _meta, seen) in enumerate(expected):
        data = desk.find(lid)
        row = desk.rows.nth(index)
        badge = row.locator(".login-current")
        if data["current"]:
            expect(badge).to_be_visible()
            expect(badge).to_have_text("이 기기")
        else:
            expect(badge).to_be_hidden()
        expect(row.locator(".login-facts dt")).to_have_text(["로그인", "마지막 활동", "만료"])
        expect(row.locator(".login-seen")).to_have_text(seen)
        expect(row.locator(".login-created")).to_have_text(local(data["created"]))
        expect(row.locator(".login-expires")).to_have_text(local(data["expires"]))
        times = row.locator("time").evaluate_all("nodes => nodes.map(node => node.dateTime)")
        assert times == [data["created"], data["seen"], data["expires"]], times
        # The relative activity time keeps the exact local time on hover.
        expect(row.locator(".login-seen time")).to_have_attribute("title", local(data["seen"]))
        revoke = row.locator(".login-revoke")
        expect(revoke).to_have_text("해제")
        expect(revoke).to_be_enabled()
    expect(desk.page.locator("#logins-count")).to_have_text(str(len(expected)))


def expect_ready(desk):
    """No request in flight, no confirmation: every control usable."""
    expect(desk.confirm).to_be_hidden()
    expect(desk.actions).to_be_visible()
    expect(desk.refresh).to_be_enabled()
    expect(desk.all).to_be_enabled()
    for button in desk.rows.locator(".login-revoke").all():
        expect(button).to_be_enabled()


def expect_locked(desk):
    expect(desk.refresh).to_be_disabled()
    for button in desk.rows.locator(".login-revoke").all():
        expect(button).to_be_disabled()


def list_shows_every_field(desk):
    assert desk.static_queries == {"/static/app.js": "v=9", "/static/style.css": "v=9"}, desk.static_queries
    # The list loads when the dialog opens, never with the page.
    assert sorted(desk.requests) == sorted([LIST, CERT]), desk.requests
    expect(desk.button).to_have_text("로그인 기기")
    expect(desk.button).to_have_attribute("aria-haspopup", "dialog")
    expect(desk.page.locator(".account-controls #logins-open")).to_be_visible()
    expect(desk.dialog).to_be_hidden()
    desk.assert_no_overflow()
    desk.shot("logins-header-desktop.png")

    desk.open_dialog()
    expect_rows(desk, ["this-chrome", "fx-linux_1", "galaxy-tab", "older-login"])
    expect(desk.page.locator("#logins-title")).to_have_text("로그인 기기 4")
    expect(desk.page.locator("#logins-intro")).to_have_text(INTRO)
    expect(desk.page.locator(".logins-privacy")).to_have_text(PRIVACY)
    expect(desk.status).to_be_hidden()
    expect(desk.error).to_be_hidden()
    expect(desk.others).to_have_text("다른 기기 모두 해제")
    expect(desk.all).to_have_text("모두 해제(이 기기 포함)")
    expect_ready(desk)
    expect(desk.others).to_be_enabled()
    expect(desk.page.locator(".login-current:visible")).to_have_count(1)
    badge = desk.row("this-chrome").locator(".login-current")
    assert badge.evaluate("node => getComputedStyle(node).color") == CURRENT_GREEN
    expect(desk.row("older-login").locator(".login-name")).to_have_class(re.compile(r"\bunknown\b"))
    expect(desk.row("this-chrome").locator(".login-revoke")).to_have_attribute("aria-label", "이 기기의 로그인 해제")
    expect(desk.row("fx-linux_1").locator(".login-revoke")).to_have_attribute(
        "aria-label", "Firefox · Linux 로그인 해제")
    # Opening never lands on a revoke button.
    expect(desk.close_button).to_be_focused()
    desk.assert_no_overflow()
    desk.shot("logins-dialog-desktop.png")

    desk.close_with_escape()
    expect(desk.button).to_be_focused()
    # Every opening asks again: a login made since then shows up.
    desk.logins.insert(0, login("new-edge", "Edge", "Windows", "desktop", SHOWN - timedelta(seconds=20),
                                SHOWN - timedelta(seconds=20), False, 0))
    desk.open_dialog(keyboard=True)
    expect(desk.rows).to_have_count(5)
    assert desk.rows.evaluate_all("nodes => nodes.map(node => node.dataset.login)") == [
        "this-chrome", "new-edge", "fx-linux_1", "galaxy-tab", "older-login"]
    expect(desk.row("new-edge").locator(".login-name")).to_have_text("Edge · Windows")
    expect(desk.page.locator("#logins-count")).to_have_text("5")
    desk.logins.pop(0)
    desk.refresh.click()
    desk.wait_for(LOGINS, 3)
    expect_rows(desk, ["this-chrome", "fx-linux_1", "galaxy-tab", "older-login"])
    desk.close_button.click()
    expect(desk.dialog).to_be_hidden()
    assert desk.count(LOGINS) == 3 and not desk.mutations, desk.requests
    return {"rows": 4, "refreshes": 3, "keyboard": "Enter/Escape"}


def revoke_one_other(desk):
    desk.open_dialog()
    desk.hold.add(revoke_key("fx-linux_1"))
    desk.row("fx-linux_1").locator(".login-revoke").click()
    # Another browser needs no confirmation; while the request runs nothing else can start.
    expect(desk.row("fx-linux_1").locator(".login-revoke")).to_have_text("해제하는 중…")
    expect_locked(desk)
    expect(desk.others).to_be_disabled()
    expect(desk.all).to_be_disabled()
    expect(desk.confirm).to_be_hidden()
    desk.release()
    expect(desk.status).to_have_text("로그인 1개를 해제했습니다.")
    expect_rows(desk, ["this-chrome", "galaxy-tab", "older-login"])
    expect_ready(desk)
    expect(desk.error).to_be_hidden()
    # The row that had focus is gone; focus stays in the dialog on a harmless control.
    expect(desk.close_button).to_be_focused()
    assert desk.mutations == [(revoke_key("fx-linux_1"), None)], desk.mutations
    assert desk.requests[-3:] == [LOGINS, revoke_key("fx-linux_1"), LOGINS], desk.requests
    desk.assert_no_overflow()
    desk.shot("logins-revoked-one.png")
    assert desk.count(LOGIN_PAGE) == 0
    expect(desk.page).to_have_url(ORIGIN + "/")
    return {"revoked": "fx-linux_1", "confirmations": 0}


def revoke_others(desk):
    desk.open_dialog()
    desk.others.click()
    expect(desk.status).to_have_text("로그인 3개를 해제했습니다.")
    expect_rows(desk, ["this-chrome"])
    expect(desk.others).to_be_disabled()
    expect(desk.all).to_be_enabled()
    expect(desk.confirm).to_be_hidden()
    expect(desk.close_button).to_be_focused()
    assert desk.mutations == [(BULK, {"scope": "others"})], desk.mutations
    assert desk.requests[-3:] == [LOGINS, BULK, LOGINS], desk.requests
    desk.assert_no_overflow()
    desk.shot("logins-revoked-others.png")
    # With only this browser left, "all" still asks first and names one login.
    desk.all.click()
    expect(desk.confirm_text).to_have_text(f"이 기기의 로그인을 해제할까요? {LEAVE}")
    desk.cancel.click()
    expect(desk.all).to_be_focused()
    assert desk.count(LOGIN_PAGE) == 0 and len(desk.mutations) == 1
    return {"revoked": 3, "scope": "others"}


def revoke_all_from_viewer(desk):
    desk.page.locator(".session-card .session-open").click()
    expect(desk.page.locator("#connection-status")).to_have_text("연결됨")
    desk.page.locator("#clipboard-toggle").click()
    desk.page.locator("#clipboard-outgoing").fill("세션에 보낼 비밀 메모")
    desk.page.evaluate("""() => window.rfbProbe.instances[0].dispatchEvent(
        new CustomEvent('clipboard', {detail: {text: '세션에서 복사한 텍스트'}}))""")
    expect(desk.page.locator("#clipboard-incoming")).to_have_value("세션에서 복사한 텍스트")
    # The viewer covers the header, so press the button the way a script would; the dialog opens above the viewer.
    before = desk.count(LOGINS)
    desk.page.evaluate("document.getElementById('logins-open').click()")
    expect(desk.dialog).to_be_visible()
    desk.wait_for(LOGINS, before + 1)
    expect(desk.rows).to_have_count(4)

    desk.all.click()
    expect(desk.confirm).to_be_visible()
    expect(desk.confirm_text).to_have_text(f"이 기기를 포함해 로그인 4개를 모두 해제할까요? {LEAVE}")
    assert desk.confirm.evaluate("node => getComputedStyle(node).color") == CONFIRM_RED
    expect(desk.go).to_have_text("해제하고 로그아웃")
    expect(desk.cancel).to_be_focused()
    expect(desk.actions).to_be_hidden()
    expect_locked(desk)
    desk.assert_no_overflow()
    desk.shot("logins-confirm-all-desktop.png")
    desk.cancel.click()
    expect_ready(desk)
    expect(desk.all).to_be_focused()
    assert not desk.revokes(), desk.revokes()

    desk.all.click()
    expect(desk.confirm).to_be_visible()
    state, requests = desk.confirm_and_leave()
    # The page being left: it closed its own viewer, emptied the clipboard panel and made no reconnect.
    assert requests == [BULK, LOGIN_PAGE], requests
    assert desk.revokes() == [(BULK, {"scope": "all"})], desk.revokes()
    assert state == {
        "status": LEAVING, "dialog_open": True, "controls_locked": True, "viewers_closed": [True],
        "connection": "연결 종료", "clipboard_out": "", "clipboard_in": "", "clipboard_hidden": True,
        "clipboard_copy_disabled": True, "desktop_children": 0, "viewer_notice_hidden": True,
        "reconnect_hidden": True, "unload_guarded": False,
    }, state
    assert desk.requests.count(OPEN) == 1, desk.requests
    return {"scope": "all", "confirmations": 2, "reconnects": 0}


def revoke_this_row_during_upload(desk):
    # An upload whose start never answers keeps the page's unload guard up.
    desk.hold.add(UPLOAD)
    desk.page.locator("#files-input").set_input_files(
        files=[{"name": "큰 파일.bin", "mimeType": "application/octet-stream", "buffer": b"x" * 2048}])
    desk.wait_for(UPLOAD, 1)
    assert desk.page.evaluate(UNLOAD_GUARDED), "an upload in progress should guard the page"

    desk.open_dialog()
    desk.row("this-chrome").locator(".login-revoke").click()
    expect(desk.confirm_text).to_have_text(f"이 기기의 로그인을 해제할까요? {LEAVE} 올리는 중인 파일도 끊깁니다.")
    expect(desk.cancel).to_be_focused()
    # Escape closes the dialog and drops the confirmation; opening again starts clean.
    desk.close_with_escape()
    expect(desk.button).to_be_focused()
    desk.open_dialog()
    expect_ready(desk)
    assert [key for key, _body in desk.mutations] == [UPLOAD], desk.mutations
    assert desk.page.evaluate(UNLOAD_GUARDED)

    desk.row("this-chrome").locator(".login-revoke").click()
    expect(desk.confirm).to_be_visible()
    state, requests = desk.confirm_and_leave()
    assert requests == [revoke_key("this-chrome"), LOGIN_PAGE], requests
    assert desk.revokes() == [(revoke_key("this-chrome"), None)], desk.revokes()
    assert state["status"] == LEAVING and state["controls_locked"], state
    assert not state["unload_guarded"], "the guard must come down before leaving for /login/"
    return {"revoked": "this-chrome", "unload_guard": "removed"}


def errors_show_korean_messages(desk):
    desk.open_dialog()
    # Another browser revoked this login after the list loaded.
    desk.logins.remove(desk.find("galaxy-tab"))
    desk.row("galaxy-tab").locator(".login-revoke").click()
    expect(desk.status).to_have_text(MESSAGES["login_not_found"])
    expect(desk.error).to_be_hidden()
    expect_rows(desk, ["this-chrome", "fx-linux_1", "older-login"])
    expect_ready(desk)

    # A revoke that never reached the server leaves the list and its button as they were.
    desk.fail(revoke_key("fx-linux_1"), "abort")
    desk.row("fx-linux_1").locator(".login-revoke").click()
    expect(desk.error).to_have_text(MESSAGES["network"])
    expect(desk.status).to_be_hidden()
    expect_rows(desk, ["this-chrome", "fx-linux_1", "older-login"])
    expect(desk.row("fx-linux_1").locator(".login-revoke")).to_be_focused()
    expect_ready(desk)
    desk.assert_no_overflow()
    desk.shot("logins-error-network.png")

    desk.fail(BULK, (403, {"error": "The page expired. Reload it and try again.", "code": "csrf_token_invalid"}),
              (500, {}))
    desk.others.click()
    expect(desk.error).to_have_text(MESSAGES["csrf_token_invalid"])
    expect(desk.others).to_be_focused()
    desk.others.click()
    expect(desk.error).to_have_text(MESSAGES["unknown"])
    expect_rows(desk, ["this-chrome", "fx-linux_1", "older-login"])
    # A failed request for this browser stays on the page with the reason.
    desk.fail(revoke_key("this-chrome"), "abort")
    desk.row("this-chrome").locator(".login-revoke").click()
    desk.go.click()
    expect(desk.error).to_have_text(MESSAGES["network"])
    expect(desk.row("this-chrome").locator(".login-revoke")).to_be_focused()
    expect_ready(desk)

    # The list itself fails: no stale rows, nothing to revoke, a Korean reason.
    for failure, message in (("abort", "list_network"), ((200, {"logins": "nope"}), "list_unavailable"),
                             ((200, {"logins": [{"browser": "Chrome"}]}), "list_unavailable"),
                             ((500, {"error": "Internal error", "code": "internal_error"}), "list_unavailable"),
                             ((404, {}), "list_unavailable")):
        desk.fail(LOGINS, failure)
        desk.refresh.click()
        expect(desk.error).to_have_text(MESSAGES[message])
        expect(desk.rows).to_have_count(0)
        expect(desk.page.locator("#logins-list")).to_be_hidden()
        expect(desk.page.locator("#logins-count")).to_have_text("")
        expect(desk.others).to_be_disabled()
        expect(desk.all).to_be_disabled()
        expect(desk.refresh).to_be_enabled()
    desk.assert_no_overflow()
    desk.shot("logins-error-list.png")
    desk.refresh.click()
    expect(desk.error).to_be_hidden()
    expect_rows(desk, ["this-chrome", "fx-linux_1", "older-login"])
    # An error does not outlive the dialog.
    desk.fail(revoke_key("fx-linux_1"), "abort")
    desk.row("fx-linux_1").locator(".login-revoke").click()
    expect(desk.error).to_have_text(MESSAGES["network"])
    desk.close_with_escape()
    desk.open_dialog()
    expect(desk.error).to_be_hidden()
    expect(desk.status).to_be_hidden()
    assert desk.count(LOGIN_PAGE) == 0
    expect(desk.page).to_have_url(ORIGIN + "/")
    return {"messages": 8}


def phone_layout(desk):
    header = desk.page.locator(".app-header")
    controls = ["#logins-open", "#certificate-open", "#logout-form button", ".account-name"]
    desk.assert_no_overflow()
    boxes = {control: desk.page.locator(control).bounding_box() for control in controls}
    for control, box in boxes.items():
        assert box["x"] >= 0 and box["x"] + box["width"] <= PHONE["width"], (control, box)
    middles = {round(box["y"] + box["height"] / 2) for box in boxes.values()}
    assert max(middles) - min(middles) <= 1, f"the header controls are not on one line: {boxes}"
    assert header.bounding_box()["height"] == 65, header.bounding_box()
    name = desk.page.locator(".account-name")
    expect(name).to_have_text("preview")
    assert name.evaluate("node => node.scrollWidth <= node.clientWidth"), "the user name is cut off"
    for control in controls[:3]:
        target = desk.page.locator(control)
        assert target.evaluate("node => node.getBoundingClientRect().height") >= 32, control
    desk.shot("logins-header-phone.png")
    # A long user name gives way to the buttons.
    desk.page.evaluate("document.querySelector('.account-name').textContent = 'raven-operator-with-a-long-name'")
    desk.assert_no_overflow()
    assert name.evaluate("node => node.scrollWidth > node.clientWidth && getComputedStyle(node).textOverflow"
                         " === 'ellipsis'"), "a long user name should end in an ellipsis"
    desk.shot("logins-header-phone-long-name.png")
    desk.page.evaluate("document.querySelector('.account-name').textContent = 'preview'")

    desk.open_dialog()
    expect_rows(desk, ["this-chrome", "fx-linux_1", "galaxy-tab", "older-login"])
    box = desk.dialog.bounding_box()
    assert box["x"] >= 16 and box["x"] + box["width"] <= PHONE["width"] - 16, box
    # Each fact gets its own line on a phone; the revoke button stays beside its row.
    facts = desk.row("this-chrome").locator(".login-facts > div")
    tops = [item.bounding_box()["y"] for item in facts.all()]
    assert tops == sorted(tops) and len(set(round(top) for top in tops)) == 3, tops
    summary = desk.row("this-chrome").locator(".login-summary").bounding_box()
    revoke = desk.row("this-chrome").locator(".login-revoke").bounding_box()
    assert revoke["x"] >= summary["x"] + summary["width"], (summary, revoke)
    desk.assert_no_overflow()
    desk.shot("logins-dialog-phone.png")
    desk.page.locator(".logins-privacy").scroll_into_view_if_needed()
    desk.assert_no_overflow()
    desk.shot("logins-dialog-phone-footer.png")
    desk.all.click()
    expect(desk.confirm).to_be_visible()
    desk.confirm.scroll_into_view_if_needed()
    desk.assert_no_overflow()
    desk.shot("logins-confirm-phone.png")
    desk.cancel.click()
    desk.close_with_escape()

    desk.page.set_viewport_size(NARROW)
    desk.assert_no_overflow()
    desk.shot("logins-header-narrow.png")
    desk.open_dialog()
    desk.assert_no_overflow()
    desk.shot("logins-dialog-narrow.png")
    desk.close_with_escape()
    assert not desk.mutations, desk.mutations
    return {"widths": [PHONE["width"], NARROW["width"]], "header_height": 65}


def login_expiry_goes_to_login(desk):
    desk.fail(LOGINS, (401, {"error": "Authentication required", "code": "auth_required"}))
    desk.button.click()
    desk.page.wait_for_url(ORIGIN + "/login/")
    assert desk.requests[-2:] == [LOGINS, LOGIN_PAGE], desk.requests
    return {"redirect": "/login/"}


SCENARIOS = (
    (list_shows_every_field, DESKTOP),
    (revoke_one_other, DESKTOP),
    (revoke_others, DESKTOP),
    (revoke_all_from_viewer, DESKTOP),
    (revoke_this_row_during_upload, DESKTOP),
    (errors_show_korean_messages, DESKTOP),
    (phone_layout, PHONE),
    (login_expiry_goes_to_login, DESKTOP),
)

failed = []
with sync_playwright() as playwright:
    browser = playwright.chromium.launch(chromium_sandbox=True)
    for scenario, viewport in SCENARIOS:
        desk = None
        try:
            desk = Desk(browser, viewport)
            detail = scenario(desk)
            desk.close()
            print(json.dumps({"scenario": scenario.__name__, "result": "passed", **detail}, ensure_ascii=False),
                  flush=True)
        except Exception as error:
            failed.append(scenario.__name__)
            if desk:
                try:
                    desk.page.screenshot(path=str(output / f"failed-logins-{scenario.__name__}.png"))
                except PlaywrightError:
                    pass
            print(json.dumps({"scenario": scenario.__name__, "result": "failed",
                              "error": f"{type(error).__name__}: {str(error)[:300]}"}, ensure_ascii=False), flush=True)
            traceback.print_exc()
            if desk:
                desk.context.close()
    browser.close()

print(json.dumps({"logins_ui": "failed" if failed else "passed", "scenarios": len(SCENARIOS), "failed": failed}))
if failed:
    raise SystemExit(1)
