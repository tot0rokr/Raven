"""Check the HTTPS certificate button, dialog and expiry notice with a fake API and a fixed page clock.

Run through `python3 tests/web_ui_check.py`, which renders index.html and passes it on stdin. No gateway, broker,
login or session is touched.
"""

from datetime import datetime, timedelta, timezone
from functools import partial
import hashlib
import json
from pathlib import Path
import re
import sys
import traceback
from urllib.parse import urlparse

from playwright.sync_api import Error as PlaywrightError, expect, sync_playwright

ORIGIN = "https://raven.test"
SOURCE = Path("/source")
# A fixed clock and a zone without daylight saving, so day counts and local dates are the same on every run.
NOW = datetime(2026, 11, 18, 3, 0, tzinfo=timezone.utc)
ZONE_ID = "Asia/Seoul"
ZONE = timezone(timedelta(hours=9))
DESKTOP = {"width": 1280, "height": 900}
PHONE = {"width": 412, "height": 915}
LIST = "GET /api/sessions/"
CERT = "GET /api/certificate/"
FINGERPRINT = hashlib.sha256(b"raven certificate fixture").hexdigest().upper()
FINGERPRINT = ":".join(FINGERPRINT[index:index + 2] for index in range(0, 64, 2))
NAMES = ["localhost", "raven.test", "10.20.30.40"]
LONG_NAMES = ["raven-workstation-with-a-very-long-hostname.department.example.internal", "raven.test",
              "2001:db8:85a3::8a2e:370:7334"]
UNAVAILABLE = "인증서 정보를 불러오지 못했습니다. 서버에서 raven web certificate info로 확인해 주세요."
RENEW = " 서버에서 raven web certificate renew로 갱신하고 PC의 신뢰 등록을 새로 하세요."
WARNING_COLOR = "rgb(236, 220, 180)"
ERROR_COLOR = "rgb(246, 192, 187)"
MUTED_COLOR = "rgb(162, 172, 182)"
GUIDE = (
    "raven web certificate info",
    "이 화면과 받은 파일만 믿으면 바꿔친 인증서를 믿게 될 수 있습니다",
    "certutil -user -addstore Root raven-raven.test.crt",
    "브라우저를 완전히 닫았다가 다시 엽니다",
    "Windows에서 인증서 신뢰",
    "raven web certificate renew",
    "PC마다 새 인증서를 받아 다시 등록하세요",
)
SESSIONS = [{"id": sid, "name": name, "state": "running", "wm": "xmonad", "geometry": "1600x900",
             "display": str(100 + int(sid)), "generation": sid[-1] * 64, "command": "xterm"}
            for sid, name in (("41", "work"), ("42", "build"))]
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

options = json.load(sys.stdin)
output = Path(options["output"])
output.mkdir(parents=True, exist_ok=True)


def certificate(days_left, not_after=None, **extra):
    """An API answer whose end time matches days_left the way the gateway counts it (whole days, rounded down)."""
    end = not_after or NOW + timedelta(days=days_left, hours=6)
    return {"names": NAMES, "not_before": (end - timedelta(days=365)).isoformat(), "not_after": end.isoformat(),
            "days_left": days_left, "fingerprint": FINGERPRINT, "origin_covered": True, **extra}


def local(text, date_only=False):
    shown = datetime.fromisoformat(text).astimezone(ZONE)
    return shown.strftime("%Y-%m-%d" if date_only else "%Y-%m-%d %H:%M")


class Desk:
    """One page with its own fake API, fixed clock and request log.

    `answers` are used in order for the certificate requests: a dict is the JSON answer, (status, body) an error,
    "abort" a network failure and "hold" a request left unanswered until `release`. Afterwards `certificate`
    answers every request.
    """

    def __init__(self, browser, viewport, answer, answers=()):
        self.certificate = answer
        self.answers = list(answers)
        self.requests = []
        self.held = []
        self.errors = []
        self.unexpected = []
        self.static_queries = {}
        self.context = browser.new_context(viewport=viewport, timezone_id=ZONE_ID, accept_downloads=True)
        self.page = self.context.new_page()
        self.page.clock.install(time=NOW)
        self.page.route("**/*", self.serve)
        self.page.on("pageerror", lambda error: self.errors.append(str(error)))
        self.page.on("console", lambda message: self.errors.append(message.text) if message.type == "error" else None)
        self.page.goto(ORIGIN, wait_until="commit")
        expect(self.page.locator("#session-count")).to_have_text(str(len(SESSIONS)))
        expect(self.page.locator("#loading-status")).to_have_text("")
        self.dialog = self.page.locator("#certificate-dialog")
        self.button = self.page.locator("#certificate-open")
        self.notice = self.page.locator("#certificate-notice")
        self.status = self.page.locator("#certificate-status")
        self.details = self.page.locator("#certificate-details")
        self.link = self.page.locator("#certificate-download")

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
        elif key == "GET /login/":
            self.requests.append(key)
            route.fulfill(content_type="text/html", body="<!doctype html><title>login fixture</title>")
        elif key == LIST:
            self.requests.append(key)
            route.fulfill(json={"sessions": SESSIONS})
        elif key == "POST /api/sessions/41/open/":
            self.requests.append(key)
            route.fulfill(json={"name": "work", "password": "fixture-only", "websocket": "/ws/sessions/41/"})
        elif key == CERT:
            self.requests.append(key)
            self.answer(route)
        else:
            self.unexpected.append(key)
            route.fulfill(status=404, json={"error": "Unexpected fixture request"})

    def answer(self, route):
        reply = self.answers.pop(0) if self.answers else self.certificate
        if reply == "hold":
            self.held.append(route)
        elif reply == "abort":
            route.abort("internetdisconnected")
        elif isinstance(reply, tuple):
            route.fulfill(status=reply[0], json=reply[1])
        else:
            route.fulfill(json=reply)

    def release(self, answer):
        """Answer the held certificate requests with `answer`."""
        held, self.held = self.held, []
        for route in held:
            route.fulfill(json=answer)

    def count(self, key):
        return self.requests.count(key)

    def close_with_escape(self):
        self.page.keyboard.press("Escape")
        expect(self.dialog).to_be_hidden()

    def assert_no_overflow(self):
        assert self.page.evaluate("document.documentElement.scrollWidth <= innerWidth"), "the page scrolls sideways"
        assert self.page.evaluate("""() => [...document.querySelectorAll(
            '.app-header, .account-controls, #certificate-notice, dialog[open], dialog[open] .certificate-facts dd,'
            + ' dialog[open] .certificate-download, dialog[open] .certificate-guide')]
            .filter(node => node.getClientRects().length)
            .every(node => node.scrollWidth <= node.clientWidth)"""), "a panel overflows"
        assert self.page.evaluate("""() => [...document.querySelectorAll('.app-header *')]
            .filter(node => node.getClientRects().length)
            .every(node => node.getBoundingClientRect().right <= innerWidth)"""), "a header control is cut off"
        assert self.page.evaluate("""() => {
            const dialog = document.querySelector('dialog[open]');
            if (!dialog) return true;
            const box = dialog.getBoundingClientRect();
            return [...dialog.querySelectorAll('*')].filter(node => node.getClientRects().length).every(node => {
              const rect = node.getBoundingClientRect();
              return rect.left >= box.left - 0.5 && rect.right <= box.right + 0.5;
            });
        }"""), "something in the dialog sticks out"

    def shot(self, name):
        self.page.screenshot(path=str(output / name))

    def close(self):
        assert not self.unexpected, self.unexpected
        # Chromium logs each fixture error status and aborted request; anything else is a page fault.
        script_errors = [error for error in self.errors if not error.startswith("Failed to load resource")]
        assert not script_errors, script_errors
        self.context.close()


def wait_for_certificate_requests(desk, count):
    deadline = 50
    while desk.count(CERT) < count and deadline:
        desk.page.wait_for_timeout(100)
        deadline -= 1
    assert desk.count(CERT) == count, desk.requests


def open_dialog(desk, via=None):
    before = desk.count(CERT)
    (via or desk.button).click()
    expect(desk.dialog).to_be_visible()
    wait_for_certificate_requests(desk, before + 1)


def expect_details(desk, answer):
    """The dialog shows every field of `answer`, with the guide and the download link."""
    expect(desk.details).to_be_visible()
    expect(desk.status).to_be_hidden()
    expect(desk.page.locator("#certificate-names .certificate-name")).to_have_text(answer["names"])
    validity = desk.page.locator("#certificate-validity")
    expect(validity).to_have_text(f"{local(answer['not_before'])} – {local(answer['not_after'])}")
    assert validity.locator("time").evaluate_all("nodes => nodes.map(node => node.dateTime)") == [
        answer["not_before"], answer["not_after"]]
    fingerprint = desk.page.locator("#certificate-fingerprint")
    assert fingerprint.text_content() == answer["fingerprint"]
    assert "monospace" in fingerprint.evaluate("node => getComputedStyle(node).fontFamily")
    assert fingerprint.locator("wbr").count() == answer["fingerprint"].count(":")
    expect(desk.page.locator("#certificate-origin code")).to_have_text(ORIGIN)
    expect(desk.link).to_be_visible()
    expect(desk.link).to_have_text("인증서 받기")
    assert desk.link.get_attribute("href") == "/api/certificate/download/"
    assert desk.link.get_attribute("download") == ""
    assert desk.link.evaluate("node => node.href") == ORIGIN + "/api/certificate/download/"
    guide = desk.page.locator(".certificate-guide")
    for text in GUIDE:
        expect(guide).to_contain_text(text)


def expect_days(desk, text, level):
    days = desk.page.locator("#certificate-days")
    expect(days).to_have_text(text)
    expect(days).to_have_attribute("data-level", level)


def dialog_shows_every_field(desk):
    assert desk.static_queries == {"/static/app.js": "v=8", "/static/style.css": "v=8"}, desk.static_queries
    wait_for_certificate_requests(desk, 1)
    assert sorted(desk.requests) == sorted([LIST, CERT]), desk.requests
    expect(desk.notice).to_be_hidden()
    expect(desk.button).to_have_text("인증서")
    expect(desk.page.locator(".account-controls #certificate-open")).to_be_visible()
    expect(desk.dialog).to_be_hidden()
    desk.shot("certificate-header-desktop.png")

    open_dialog(desk)
    answer = desk.certificate
    expect_details(desk, answer)
    expect_days(desk, "200일 남음", "ok")
    expect(desk.page.locator("#certificate-origin")).to_have_text(f"{ORIGIN} 인증서 이름에 들어 있습니다.")
    # The first focus goes to the close button; Tab reaches the download link next.
    expect(desk.page.locator("#certificate-dialog [data-close-dialog]")).to_be_focused()
    desk.page.keyboard.press("Tab")
    expect(desk.link).to_be_focused()
    desk.assert_no_overflow()
    desk.shot("certificate-dialog-desktop.png")

    with desk.page.expect_download() as download_info:
        desk.link.click()
    download = download_info.value
    # Chromium fetches an <a download> outside page routing (and the container has no network), so this checks
    # that the link starts a download of the right URL; the file name and body come from the gateway.
    assert download.url == ORIGIN + "/api/certificate/download/", download.url

    desk.close_with_escape()
    expect(desk.button).to_be_focused()
    # Keyboard only: Enter opens the dialog, Escape closes it.
    desk.page.keyboard.press("Enter")
    expect(desk.dialog).to_be_visible()
    wait_for_certificate_requests(desk, 3)
    expect_details(desk, answer)
    desk.close_with_escape()
    expect(desk.button).to_be_focused()
    desk.page.keyboard.press("Enter")
    expect(desk.dialog).to_be_visible()
    desk.page.locator("#certificate-dialog [data-close-dialog]").click()
    expect(desk.dialog).to_be_hidden()
    assert desk.count(LIST) == 1 and desk.count(CERT) == 4, desk.requests
    return {"fields": 4, "download": download.url, "keyboard": "Enter/Tab/Escape"}


def phone_layout(desk):
    expect(desk.notice).to_be_hidden()
    desk.assert_no_overflow()
    for control in ("#certificate-open", "#logout-form button"):
        box = desk.page.locator(control).bounding_box()
        assert box["x"] >= 0 and box["x"] + box["width"] <= PHONE["width"], (control, box)
    desk.shot("certificate-header-phone.png")

    open_dialog(desk)
    answer = desk.certificate
    expect_details(desk, answer)
    expect_days(desk, "200일 남음", "ok")
    uncovered = desk.page.locator("#certificate-origin .certificate-uncovered")
    expect(uncovered).to_have_text("설정된 주소의 이름이 인증서에 없습니다. 서버에서 raven web doctor로 확인해 주세요.")
    assert uncovered.evaluate("node => getComputedStyle(node).color") == ERROR_COLOR
    box = desk.dialog.bounding_box()
    assert box["x"] >= 16 and box["x"] + box["width"] <= PHONE["width"] - 16, box
    fingerprint = desk.page.locator("#certificate-fingerprint")
    assert fingerprint.evaluate("node => node.scrollWidth <= node.clientWidth"), "the fingerprint overflows"
    lines = fingerprint.evaluate(
        "node => Math.round(node.getBoundingClientRect().height / parseFloat(getComputedStyle(node).lineHeight))")
    assert lines >= 3, lines
    # Lines break between bytes, never inside one.
    assert fingerprint.evaluate("""node => [...node.childNodes]
        .filter(child => child.nodeType === Node.TEXT_NODE && child.data !== ':')
        .every(child => {
          const range = document.createRange();
          range.selectNodeContents(child);
          return range.getClientRects().length === 1;
        })"""), "a byte of the fingerprint is split across lines"
    desk.assert_no_overflow()
    desk.shot("certificate-dialog-phone.png")
    desk.page.locator(".certificate-guide li").last.scroll_into_view_if_needed()
    desk.assert_no_overflow()
    desk.shot("certificate-dialog-phone-guide.png")
    desk.close_with_escape()
    return {"width": PHONE["width"], "fingerprint_lines": lines}


def expiry_notice(desk, lead, level, days_text):
    """A certificate near or past its end shows a notice above the workspace; one with 30+ days does not."""
    wait_for_certificate_requests(desk, 1)
    if level == "ok":
        desk.page.wait_for_timeout(300)
        expect(desk.notice).to_be_hidden()
    else:
        expect(desk.notice).to_be_visible()
        expect(desk.page.locator("#certificate-notice-text")).to_have_text(lead + RENEW)
        expect(desk.page.locator("#certificate-notice-text code")).to_have_text("raven web certificate renew")
        expected = {"warn": ("notice-warning", WARNING_COLOR), "expired": ("notice-error", ERROR_COLOR)}[level]
        expect(desk.notice).to_have_class(re.compile(rf"\b{expected[0]}\b"))
        assert desk.notice.evaluate("node => getComputedStyle(node).color") == expected[1]
        heading = desk.page.locator(".workspace-heading").bounding_box()
        assert desk.notice.bounding_box()["y"] < heading["y"], "the notice is not above the workspace"
        # The session list and its controls stay usable under the notice.
        expect(desk.page.locator(".session-card")).to_have_count(len(SESSIONS))
        expect(desk.page.locator("#refresh-sessions")).to_be_enabled()
        desk.assert_no_overflow()
        desk.shot(f"certificate-{desk.name}-desktop.png")
        desk.page.set_viewport_size(PHONE)
        desk.assert_no_overflow()
        desk.shot(f"certificate-{desk.name}-phone.png")
    open_dialog(desk, desk.page.locator("#certificate-notice-open") if level != "ok" else None)
    expect_details(desk, desk.certificate)
    expect_days(desk, days_text, level)
    desk.assert_no_overflow()
    desk.close_with_escape()
    return {"days_left": desk.certificate["days_left"], "notice": level != "ok"}


def failures_stay_quiet(desk):
    # The first answer never comes: the list and its controls work without it.
    expect(desk.page.locator(".session-card")).to_have_count(len(SESSIONS))
    expect(desk.page.locator("#refresh-sessions")).to_be_enabled()
    expect(desk.page.locator("#workspace-notice")).to_be_hidden()
    expect(desk.notice).to_be_hidden()
    assert len(desk.held) == 1, desk.held

    # A server error: a quiet line in the dialog, with the download and the guide still there.
    open_dialog(desk)
    expect(desk.status).to_have_text(UNAVAILABLE)
    assert desk.status.evaluate("node => getComputedStyle(node).color") == MUTED_COLOR
    expect(desk.details).to_be_hidden()
    expect(desk.link).to_be_visible()
    expect(desk.page.locator(".certificate-guide")).to_contain_text("raven web certificate info")
    desk.assert_no_overflow()
    desk.shot("certificate-dialog-failed.png")
    # The page-load answer that arrives late is older than the failed one and changes nothing.
    desk.release(certificate(10))
    desk.page.wait_for_timeout(300)
    expect(desk.notice).to_be_hidden()
    expect(desk.status).to_have_text(UNAVAILABLE)
    desk.close_with_escape()

    for failure in ("network", "malformed", "missing endpoint"):
        open_dialog(desk)
        expect(desk.status).to_have_text(UNAVAILABLE)
        expect(desk.details).to_be_hidden()
        desk.close_with_escape()

    open_dialog(desk)
    expect_details(desk, desk.certificate)
    expect_days(desk, "200일 남음", "ok")
    desk.close_with_escape()
    # A later failure drops the details instead of showing them as current.
    open_dialog(desk)
    expect(desk.status).to_have_text(UNAVAILABLE)
    expect(desk.details).to_be_hidden()
    desk.close_with_escape()

    expect(desk.page.locator("#workspace-notice")).to_be_hidden()
    expect(desk.notice).to_be_hidden()
    desk.page.locator("#refresh-sessions").click()
    expect(desk.page.locator("#loading-status")).to_have_text("")
    desk.page.locator(".session-card").filter(has_text="work").locator(".session-open").click()
    expect(desk.page.locator("#connection-status")).to_have_text("연결됨")
    desk.page.locator("#close-viewer").click()
    expect(desk.page.locator("#workspace")).to_be_visible()
    assert desk.count(CERT) == 7 and desk.count(LIST) == 3, desk.requests
    return {"failed_answers": 5, "late_answer_ignored": True, "page_usable": True}


def login_expiry_goes_to_login(desk):
    wait_for_certificate_requests(desk, 1)
    desk.button.click()
    desk.page.wait_for_url(ORIGIN + "/login/")
    assert desk.requests[-1] == "GET /login/", desk.requests
    return {"redirect": "/login/"}


def notice_case(days_left, lead, level, days_text, not_after=None):
    answer = certificate(days_left, not_after)
    name = f"notice_{'expired_' if level == 'expired' else ''}{days_left}".replace("-", "minus_")
    lead = lead.format(end=local(answer["not_after"]), date=local(answer["not_after"], date_only=True))
    return name, partial(expiry_notice, lead=lead, level=level, days_text=days_text), DESKTOP, answer, ()


SCENARIOS = (
    ("dialog_shows_every_field", dialog_shows_every_field, DESKTOP, certificate(200), ()),
    ("phone_layout", phone_layout, PHONE, certificate(200, names=LONG_NAMES, origin_covered=False), ()),
    notice_case(29, "HTTPS 인증서가 29일 뒤({date}) 만료됩니다.", "warn", "29일 남음"),
    notice_case(1, "HTTPS 인증서가 1일 뒤({date}) 만료됩니다.", "warn", "1일 남음"),
    notice_case(0, "HTTPS 인증서가 하루 안에({end}) 만료됩니다.", "warn", "하루 안에 만료"),
    notice_case(0, "HTTPS 인증서가 {date}에 만료되었습니다.", "expired", "만료됨", NOW - timedelta(hours=2)),
    notice_case(-3, "HTTPS 인증서가 {date}에 만료되었습니다.", "expired", "만료됨"),
    notice_case(30, "", "ok", "30일 남음"),
    notice_case(365, "", "ok", "365일 남음"),
    ("failures_stay_quiet", failures_stay_quiet, DESKTOP, certificate(200), (
        "hold", (500, {}), "abort", {"names": "raven.test"},
        (404, {"error": "Unexpected", "code": "not_found"}), certificate(200),
        (502, {"error": "Bad gateway"}),
    )),
    ("login_expiry_goes_to_login", login_expiry_goes_to_login, DESKTOP, certificate(200), (
        certificate(200), (401, {"error": "Authentication required", "code": "auth_required"}),
    )),
)

failed = []
with sync_playwright() as playwright:
    browser = playwright.chromium.launch(chromium_sandbox=True)
    for name, scenario, viewport, answer, answers in SCENARIOS:
        desk = None
        try:
            desk = Desk(browser, viewport, answer, answers)
            desk.name = name
            detail = scenario(desk)
            desk.close()
            print(json.dumps({"scenario": name, "result": "passed", **detail}, ensure_ascii=False), flush=True)
        except Exception as error:
            failed.append(name)
            if desk:
                try:
                    desk.page.screenshot(path=str(output / f"failed-certificate-{name}.png"))
                except PlaywrightError:
                    pass
            print(json.dumps({"scenario": name, "result": "failed",
                              "error": f"{type(error).__name__}: {str(error)[:300]}"}, ensure_ascii=False), flush=True)
            traceback.print_exc()
            if desk:
                desk.context.close()
    browser.close()

print(json.dumps({"certificate_ui": "failed" if failed else "passed", "scenarios": len(SCENARIOS), "failed": failed}))
if failed:
    raise SystemExit(1)
