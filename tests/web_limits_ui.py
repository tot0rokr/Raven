"""Check session CPU/memory limits in the session list, the resource dialog and the new-session dialog with a fake API.

Run through `python3 tests/web_ui_check.py`, which renders index.html and passes it on stdin. No gateway, broker,
login or session is touched.
"""

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
PHONE = {"width": 412, "height": 915}
MIB = 1024 ** 2
GIB = 1024 ** 3
# What the fixture server resolves "max" and "N%" against: its total memory and all of its cores.
SERVER_MEMORY = 32 * GIB
SERVER_CPUS = 16
LIST = "GET /api/sessions/"
CREATE = "POST /api/sessions/"
NOTE = "실행 중인 세션에 바로 적용되며 앱은 그대로입니다. 메모리를 사용량보다 줄일 수는 없습니다."
PAUSED_NOTE = "일시정지한 세션은 상한을 늘리기만 할 수 있습니다. 줄이려면 이어하기를 누른 뒤 바꾸세요."
BELOW = "메모리를 지금 사용량보다 적게 줄일 수 없습니다."
INVALID = ("허용 범위를 벗어난 값입니다. 메모리는 0.5 GiB부터 서버 전체 메모리까지, "
           "CPU는 0.5코어부터 서버 코어 수까지 정할 수 있습니다.")
MESSAGES = {
    "session_paused": "일시정지한 세션은 상한을 줄일 수 없습니다. 늘리기만 하거나, 이어하기를 누른 뒤 줄여 주세요.",
    "csrf_token_invalid": "페이지가 만료되었습니다. 새로고침한 뒤 다시 시도해 주세요.",
    "broker_unavailable": "호스트 중계에 연결할 수 없습니다. 서버에서 raven web doctor로 확인해 주세요.",
    "network": "서버에 연결하지 못했습니다. 네트워크 연결을 확인해 주세요.",
    "memory_step": "메모리는 0.5 GiB 이상, 0.5 GiB 단위로 입력하세요.",
    "cpus_step": "CPU는 0.5코어 이상, 0.5코어 단위로 입력하세요.",
}
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
API_ROUTE = re.compile(r"/api/sessions/([0-9]+)/(open|limits)/")
SHARE = re.compile(r"([1-9][0-9]?|100)%")
SERVER_TOTALS = {"server_memory": SERVER_MEMORY, "server_cpus": SERVER_CPUS}

options = json.load(sys.stdin)
output = Path(options["output"])
output.mkdir(parents=True, exist_ok=True)


def fixture_sessions(totals):
    base = {"wm": "xmonad", "geometry": "1600x900", "command": "xterm"}
    rows = [
        ("41", "work", "running", 1288490189, 3.2, {"x11": 1, "web": 1}, {"memory": 2 * GIB, "cpus": 2}),
        ("42", "frozen", "paused", 2 * GIB, 0.0, {"x11": 0, "web": 0}, {"memory": 4 * GIB, "cpus": 1.5}),
        ("43", "stopped", "exited", None, None, {"x11": 0, "web": 0}, {"memory": 1 * GIB, "cpus": 1}),
        ("44", "open", "running", 512 * MIB, 0.2, {"x11": 0, "web": 2}, {"memory": None, "cpus": None}),
        ("45", "small", "running", 300 * MIB, 50.0, {"x11": 0, "web": 0}, {"memory": 512 * MIB, "cpus": 0.5}),
        ("46", "legacy", "paused", 1 * GIB, 0.0, {"x11": 0, "web": 0}, {"memory": None, "cpus": None}),
        ("47", "odd", "running", 400 * MIB, 1.0, {"x11": 0, "web": 0}, {"memory": 700 * MIB, "cpus": 1.25}),
    ]
    return [{**base, "id": sid, "name": name, "state": state, "display": str(100 + int(sid)),
             "generation": sid[-1] * 64, "memory": memory, "cpu": cpu, "viewers": viewers,
             "limits": {**limits, **(SERVER_TOTALS if totals else {})}}
            for sid, name, state, memory, cpu, viewers, limits in rows]


def key(method, sid, action):
    return f"{method} /api/sessions/{sid}/{action}/"


class Desk:
    """One page with its own fake API state and request log; `totals` adds the server's memory and cores to limits."""

    def __init__(self, browser, viewport=None, totals=False):
        self.totals = totals
        self.sessions = fixture_sessions(totals)
        self.requests = []
        self.bodies = []
        self.failures = {}
        self.errors = []
        self.problems = []
        self.unexpected = []
        self.static_queries = {}
        self.context = browser.new_context(viewport=viewport or {"width": 1280, "height": 900})
        self.page = self.context.new_page()
        self.page.route("**/*", self.serve)
        self.page.on("pageerror", lambda error: self.errors.append(str(error)))
        self.page.on("console", lambda message: self.errors.append(message.text) if message.type == "error" else None)
        self.page.goto(ORIGIN, wait_until="commit")
        expect(self.page.locator("#session-count")).to_have_text(str(len(self.sessions)))
        self.dialog = self.page.locator("#limits-dialog")
        self.memory = self.page.locator("#limits-memory")
        self.cpus = self.page.locator("#limits-cpus")
        self.error = self.page.locator("#limits-error")
        self.save = self.page.locator("#limits-form button[type=submit]")

    def find(self, sid):
        return next((item for item in self.sessions if item["id"] == sid), None)

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
        elif request_key == CREATE:
            self.create(route, body)
        else:
            self.session_action(route, request_key, body)

    @staticmethod
    def resolve(value, server_total):
        if value == "max":
            return server_total
        if isinstance(value, str) and SHARE.fullmatch(value):
            share = server_total * int(value[:-1]) / 100
            return int(share) if type(server_total) is int and share == int(share) else share
        return value

    def check_limit_body(self, request_key, body, allowed):
        if not isinstance(body, dict) or body.keys() - allowed:
            self.problems.append(f"{request_key}: fields {body!r}")
            return
        memory, cpus = body.get("memory"), body.get("cpus")
        shares = lambda value: value == "max" or (isinstance(value, str) and SHARE.fullmatch(value))
        if "memory" in body and not shares(memory) and not (type(memory) is int and memory >= 512 * MIB
                                                             and memory % MIB == 0):
            self.problems.append(f"{request_key}: memory {memory!r}")
        if "cpus" in body and not shares(cpus) and not (type(cpus) in (int, float) and cpus >= 0.5):
            self.problems.append(f"{request_key}: cpus {cpus!r}")

    def create(self, route, body):
        self.check_limit_body(CREATE, body, {"name", "wm", "geometry", "command", "memory", "cpus"})
        sid = str(48 + sum(1 for item in self.sessions if int(item["id"]) >= 48))
        session = {"id": sid, "name": body["name"], "state": "running", "wm": body["wm"],
                   "geometry": body["geometry"], "command": body["command"], "display": str(100 + int(sid)),
                   "generation": "c" * 64, "memory": 200 * MIB, "cpu": 1.0, "viewers": {"x11": 0, "web": 0},
                   "limits": {"memory": self.resolve(body.get("memory", 2 * GIB), SERVER_MEMORY),
                              "cpus": self.resolve(body.get("cpus", 2), SERVER_CPUS),
                              **(SERVER_TOTALS if self.totals else {})}}
        self.sessions.append(session)
        route.fulfill(status=201, json={"session": session})

    def session_action(self, route, request_key, body):
        request = route.request
        match = API_ROUTE.fullmatch(urlparse(request.url).path)
        session = match and self.find(match.group(1))
        if not session or request.method != "POST":
            self.unexpected.append(request_key)
            route.fulfill(status=404, json={"error": "Unexpected fixture request"})
            return
        if match.group(2) == "open":
            route.fulfill(json={"name": session["name"], "password": "fixture-only",
                                "websocket": f"/ws/sessions/{session['id']}/"})
            return
        self.check_limit_body(request_key, body, {"generation", "memory", "cpus"})
        if body.get("generation") != session["generation"]:
            self.problems.append(f"{request_key}: generation {body!r}")
        limits = dict(session["limits"])
        if "memory" in body:
            limits["memory"] = self.resolve(body["memory"], SERVER_MEMORY)
        if "cpus" in body:
            limits["cpus"] = self.resolve(body["cpus"], SERVER_CPUS)
        session["limits"] = limits
        route.fulfill(json={"session": session})

    def fail(self, request_key, *responses):
        self.failures.setdefault(request_key, []).extend(responses)

    def card(self, name):
        return self.page.locator(".session-card").filter(has=self.page.locator(".session-name", has_text=name))

    def stats(self, name):
        return self.card(name).locator(".session-stats")

    def open_limits(self, name):
        self.card(name).locator("[data-action=limits]").click()
        expect(self.dialog).to_be_visible()
        expect(self.page.locator("#limits-session-name")).to_have_text(name)

    def choice(self, field, label):
        return self.page.locator(f"#limits-{field}").locator(
            "xpath=ancestor::div[contains(@class,'resource-field')]").locator(".choice", has_text=label)

    def settle(self):
        self.page.wait_for_timeout(300)

    def assert_no_overflow(self):
        assert self.page.evaluate("document.documentElement.scrollWidth <= innerWidth"), "the page scrolls sideways"
        assert self.page.evaluate("""() => [...document.querySelectorAll('.session-card, .session-actions')]
            .filter(node => node.getClientRects().length)
            .every(node => node.scrollWidth <= node.clientWidth)"""), "a card overflows"
        assert self.page.evaluate("""() => [...document.querySelectorAll('.session-card button')]
            .filter(node => node.getClientRects().length)
            .every(node => {
              const card = node.closest('.session-card').getBoundingClientRect();
              const box = node.getBoundingClientRect();
              return box.left >= card.left && box.right <= card.right;
            })"""), "a card button sticks out of its card"
        # Every visible control of an open dialog stays inside it and the dialog never scrolls sideways.
        assert self.page.evaluate("""() => [...document.querySelectorAll('dialog[open]')].every(dialog => {
              const frame = dialog.getBoundingClientRect();
              const inside = [...dialog.querySelectorAll('input, button, summary, .notice, .limits-note, .field-help')]
                .filter(node => node.getClientRects().length)
                .every(node => {
                  const box = node.getBoundingClientRect();
                  return box.left >= frame.left - 0.5 && box.right <= frame.right + 0.5;
                });
              return inside && dialog.scrollWidth <= dialog.clientWidth;
            })"""), "a dialog control sticks out"

    def shot(self, name):
        # Chosen buttons fade in over 120 ms; capture them settled.
        self.page.screenshot(path=str(output / name), animations="disabled")

    def close(self):
        assert not self.problems, self.problems
        assert not self.unexpected, self.unexpected
        # Chromium logs each fixture error status and aborted request; anything else is a page fault.
        script_errors = [error for error in self.errors if not error.startswith("Failed to load resource")]
        assert not script_errors, script_errors
        self.context.close()


def stats_show_limits(desk):
    assert desk.static_queries == {"/static/app.js": "v=9", "/static/style.css": "v=9"}, desk.static_queries
    expected = {
        "work": "메모리 1.2 / 2 GiB · CPU 3% / 2코어 · 보는 중 X11 1 · 웹 1",
        "frozen": "메모리 2.0 / 4 GiB 유지 중 · CPU 상한 1.5코어 · 서버를 재부팅하면 사라집니다",
        "stopped": "메모리 상한 1 GiB · CPU 상한 1코어",
        "open": "메모리 512 MiB / 제한 없음 · CPU <1% / 제한 없음 · 보는 중 X11 0 · 웹 2",
        "small": "메모리 300 / 512 MiB · CPU 50% / 0.5코어 · 보는 중 X11 0 · 웹 0",
        "legacy": "메모리 1.0 GiB 유지 중 (제한 없음) · CPU 제한 없음 · 서버를 재부팅하면 사라집니다",
        "odd": "메모리 400 / 700 MiB · CPU 1% / 1.25코어 · 보는 중 X11 0 · 웹 0",
    }
    for name, text in expected.items():
        expect(desk.stats(name)).to_have_text(text)
        button = desk.card(name).locator("[data-action=limits]")
        expect(button).to_have_text("자원")
        expect(button).to_be_visible()
        expect(button).to_be_enabled()
        expect(button).to_have_class(re.compile(r"\bbutton-quiet\b"))
    expect(desk.stats("frozen")).to_have_class(re.compile(r"\bpaused\b"))
    desk.assert_no_overflow()
    desk.shot("limits-cards-desktop.png")

    # Usage that cannot be read leaves the limit; a list without limits keeps the earlier wording.
    desk.find("41")["memory"] = None
    desk.find("42")["memory"] = None
    desk.find("44")["cpu"] = None
    desk.find("45").pop("limits")
    desk.find("46").pop("limits")
    desk.page.locator("#refresh-sessions").click()
    expect(desk.stats("work")).to_have_text("메모리 상한 2 GiB · CPU 3% / 2코어 · 보는 중 X11 1 · 웹 1")
    expect(desk.stats("frozen")).to_have_text("메모리에 유지 중 (상한 4 GiB) · CPU 상한 1.5코어 · 서버를 재부팅하면 사라집니다")
    expect(desk.stats("open")).to_have_text("메모리 512 MiB / 제한 없음 · CPU 제한 없음 · 보는 중 X11 0 · 웹 2")
    expect(desk.stats("small")).to_have_text("메모리 300 MiB · CPU 50% · 보는 중 X11 0 · 웹 0")
    expect(desk.stats("legacy")).to_have_text("메모리 1.0 GiB 유지 중 · 서버를 재부팅하면 사라집니다")

    desk.page.set_viewport_size(PHONE)
    desk.assert_no_overflow()
    desk.shot("limits-cards-phone.png")
    assert desk.requests == [LIST, LIST], desk.requests
    return {"cards": len(desk.sessions), "layouts": ["1280", "412"]}


def expect_resource(desk, field, value, pressed, placeholder=""):
    """One resource field: its number, its placeholder and which quick choice reads as chosen."""
    target = desk.memory if field == "memory" else desk.cpus
    expect(target).to_have_value(value)
    expect(target).to_have_attribute("placeholder", placeholder)
    group = target.locator("xpath=ancestor::div[contains(@class,'resource-field')]")
    expect(group.locator(".choice[aria-pressed=true]")).to_have_count(len(pressed))
    for label in pressed:
        expect(group.locator(".choice[aria-pressed=true]", has_text=label)).to_have_count(1)


def dialog_opens_with_current_values(desk):
    desk.open_limits("work")
    expect(desk.page.locator("#limits-title")).to_have_text("자원 상한")
    expect(desk.page.locator("#limits-note")).to_have_text(NOTE)
    expect(desk.page.locator("#limits-paused")).to_be_hidden()
    expect(desk.error).to_be_hidden()
    expect_resource(desk, "memory", "2", ["2 GiB"])
    expect_resource(desk, "cpus", "2", [])
    expect(desk.memory).to_be_focused()
    expect(desk.page.locator("#limits-memory-help")).to_have_text("지금 1.2 GiB 사용 중 · 0.5 GiB 단위로 정합니다.", use_inner_text=True)
    expect(desk.page.locator("#limits-cpus-help")).to_have_text(
        "지금 CPU 3% 사용 중 · 0.5코어 단위로 정합니다. 1코어는 CPU 100%입니다.", use_inner_text=True)
    labels = desk.page.locator("#limits-dialog .choice").all_inner_texts()
    assert labels == ["1 GiB", "2 GiB", "4 GiB", "8 GiB", "25%", "50%", "75%", "최대", "25%", "50%", "75%", "최대"], labels
    # Without the server's totals there is no share line.
    expect(desk.page.locator("#limits-memory-current")).to_be_hidden()
    expect(desk.page.locator("#limits-cpus-current")).to_be_hidden()
    desk.assert_no_overflow()
    desk.shot("limits-dialog-desktop.png")
    desk.page.keyboard.press("Escape")
    expect(desk.dialog).to_be_hidden()

    desk.open_limits("open")
    expect_resource(desk, "memory", "", [], "제한 없음")
    expect_resource(desk, "cpus", "", [], "제한 없음")
    expect(desk.page.locator("#limits-memory-help")).to_have_text("지금 512 MiB 사용 중 · 0.5 GiB 단위로 정합니다.", use_inner_text=True)
    desk.page.locator("#limits-dialog [data-close-dialog]").click()
    expect(desk.dialog).to_be_hidden()

    desk.open_limits("frozen")
    expect(desk.page.locator("#limits-paused")).to_have_text(PAUSED_NOTE)
    expect_resource(desk, "memory", "4", ["4 GiB"])
    expect_resource(desk, "cpus", "1.5", [])
    expect(desk.page.locator("#limits-memory-usage")).to_have_text("지금 2.0 GiB 유지 중")
    expect(desk.page.locator("#limits-cpus-usage")).to_have_text("멈춰 있어 CPU를 쓰지 않습니다")
    desk.shot("limits-dialog-paused.png")
    desk.page.keyboard.press("Escape")

    desk.open_limits("stopped")
    expect(desk.page.locator("#limits-paused")).to_be_hidden()
    expect_resource(desk, "memory", "1", ["1 GiB"])
    expect(desk.page.locator("#limits-memory-usage")).to_have_text("꺼져 있어 사용량이 없습니다")
    expect(desk.page.locator("#limits-cpus-usage")).to_have_text("꺼져 있어 사용량이 없습니다")
    desk.page.keyboard.press("Escape")

    # Limits set off the 0.5 grid elsewhere (CLI) show as they are and are not sent when left alone.
    desk.open_limits("odd")
    expect_resource(desk, "memory", "0.68", [])
    expect_resource(desk, "cpus", "1.25", [])
    desk.save.click()
    expect(desk.dialog).to_be_hidden()
    # Saving with nothing changed sends nothing.
    desk.open_limits("work")
    desk.memory.press("Enter")
    expect(desk.dialog).to_be_hidden()
    desk.settle()

    desk.page.set_viewport_size(PHONE)
    desk.open_limits("work")
    desk.assert_no_overflow()
    desk.shot("limits-dialog-phone.png")
    chosen = desk.choice("memory", "최대")
    chosen.click()
    # The pointer still rests on the tapped choice; it must look chosen, like the 2 GiB choice did before.
    expect(chosen).to_have_css("background-color", "rgb(32, 48, 37)")
    desk.assert_no_overflow()
    desk.shot("limits-dialog-phone-max.png")
    desk.page.keyboard.press("Escape")
    expect(desk.dialog).to_be_hidden()
    assert desk.requests == [LIST], desk.requests
    return {"dialogs": 8, "requests": 0}


def save_sends_bytes_and_generation(desk):
    desk.open_limits("work")
    desk.choice("memory", "4 GiB").click()
    expect_resource(desk, "memory", "4", ["4 GiB"])
    desk.cpus.fill("3.5")
    desk.save.click()
    expect(desk.dialog).to_be_hidden()
    expect(desk.stats("work")).to_have_text("메모리 1.2 / 4 GiB · CPU 3% / 3.5코어 · 보는 중 X11 1 · 웹 1")
    expect(desk.card("work").locator("[data-action=limits]")).to_be_focused()
    expect(desk.page.locator("#loading-status")).to_have_text("")
    assert desk.requests == [LIST, key("POST", "41", "limits"), LIST], desk.requests
    assert desk.bodies == [(key("POST", "41", "limits"),
                            {"generation": "1" * 64, "memory": 4 * GIB, "cpus": 3.5})], desk.bodies

    # Only the changed field goes; a typed half GiB is sent as exact bytes.
    desk.open_limits("work")
    expect_resource(desk, "memory", "4", ["4 GiB"])
    expect_resource(desk, "cpus", "3.5", [])
    desk.memory.fill("1.5")
    expect_resource(desk, "memory", "1.5", [])
    desk.memory.press("Enter")
    expect(desk.dialog).to_be_hidden()
    expect(desk.stats("work")).to_have_text("메모리 1.2 / 1.5 GiB · CPU 3% / 3.5코어 · 보는 중 X11 1 · 웹 1")
    assert desk.bodies[-1] == (key("POST", "41", "limits"), {"generation": "1" * 64, "memory": 1610612736})

    # A session without limits gets its first one; the other field stays unlimited and unsent.
    desk.open_limits("open")
    desk.cpus.fill("1")
    desk.save.click()
    expect(desk.dialog).to_be_hidden()
    expect(desk.stats("open")).to_have_text("메모리 512 MiB / 제한 없음 · CPU <1% / 1코어 · 보는 중 X11 0 · 웹 2")
    assert desk.bodies[-1] == (key("POST", "44", "limits"), {"generation": "4" * 64, "cpus": 1})

    # Values off the 0.5 grid, below it or cleared are refused in the page and never sent.
    sent = len(desk.bodies)
    desk.open_limits("work")
    for field, value, message in (("memory", "0.7", "memory_step"), ("memory", "0", "memory_step"),
                                  ("memory", "", "memory_step"), ("cpus", "1.2", "cpus_step"),
                                  ("cpus", "0.25", "cpus_step")):
        desk.page.locator("#limits-form").evaluate("form => form.reset()")
        desk.page.evaluate("() => { for (const id of ['limits-memory', 'limits-cpus']) "
                           "document.getElementById(id).setCustomValidity(''); }")
        desk.memory.fill("1.5")
        desk.cpus.fill("3.5")
        target = desk.memory if field == "memory" else desk.cpus
        target.fill(value)
        desk.save.click()
        desk.settle()
        expect(desk.dialog).to_be_visible()
        assert target.evaluate("node => node.validationMessage") == MESSAGES[message], (field, value)
    assert len(desk.bodies) == sent, desk.bodies[sent:]
    desk.page.keyboard.press("Escape")
    expect(desk.dialog).to_be_hidden()

    # A paused session may grow.
    desk.open_limits("frozen")
    desk.choice("memory", "8 GiB").click()
    desk.save.click()
    expect(desk.dialog).to_be_hidden()
    expect(desk.stats("frozen")).to_have_text("메모리 2.0 / 8 GiB 유지 중 · CPU 상한 1.5코어 · 서버를 재부팅하면 사라집니다")
    assert desk.bodies[-1] == (key("POST", "42", "limits"), {"generation": "2" * 64, "memory": 8 * GIB})
    return {"saves": 4, "refused_in_page": 5}


def max_choice_sends_max(desk):
    desk.open_limits("work")
    desk.choice("memory", "최대").click()
    expect_resource(desk, "memory", "", ["최대"], "최대")
    field = desk.page.locator("#limits-dialog .resource-field").first
    expect(field).to_have_class(re.compile(r"\bis-share\b"))
    expect(field.locator(".unit")).to_be_hidden()
    expect(desk.page.locator("#limits-memory-help")).to_have_text("지금 1.2 GiB 사용 중 · 서버의 전체 메모리까지 씁니다.", use_inner_text=True)
    desk.choice("cpus", "최대").click()
    expect_resource(desk, "cpus", "", ["최대"], "최대")
    expect(desk.page.locator("#limits-cpus-help")).to_have_text("지금 CPU 3% 사용 중 · 서버의 모든 코어를 씁니다.", use_inner_text=True)
    desk.shot("limits-dialog-max.png")
    desk.save.click()
    expect(desk.dialog).to_be_hidden()
    # The list reports what "max" became, in numbers.
    expect(desk.stats("work")).to_have_text("메모리 1.2 / 32 GiB · CPU 3% / 16코어 · 보는 중 X11 1 · 웹 1")
    assert desk.bodies == [(key("POST", "41", "limits"),
                            {"generation": "1" * 64, "memory": "max", "cpus": "max"})], desk.bodies

    # Typing or choosing a number after "최대" leaves it.
    desk.open_limits("work")
    expect_resource(desk, "memory", "32", [])
    desk.choice("memory", "최대").click()
    desk.memory.fill("6")
    expect_resource(desk, "memory", "6", [])
    expect(field).not_to_have_class(re.compile(r"\bis-share\b"))
    desk.choice("cpus", "최대").click()
    desk.choice("cpus", "최대").click()
    expect_resource(desk, "cpus", "", ["최대"], "최대")
    desk.save.click()
    expect(desk.dialog).to_be_hidden()
    assert desk.bodies[-1] == (key("POST", "41", "limits"), {"generation": "1" * 64, "memory": 6 * GIB, "cpus": "max"})

    desk.open_limits("open")
    desk.choice("memory", "최대").click()
    desk.choice("memory", "2 GiB").click()
    expect_resource(desk, "memory", "2", ["2 GiB"], "제한 없음")
    desk.choice("memory", "최대").click()
    desk.save.click()
    expect(desk.dialog).to_be_hidden()
    assert desk.bodies[-1] == (key("POST", "44", "limits"), {"generation": "4" * 64, "memory": "max"})
    return {"max_requests": 3}


def errors_show_korean_messages(desk):
    path = key("POST", "41", "limits")
    desk.open_limits("work")
    cases = (
        # The minimum named in the refusal, rounded up, with the next value the 0.5 GiB field accepts.
        ((409, {"error": "Memory 1g is below the current usage 1.2GiB plus 64MiB; the minimum is 1600MiB",
                "code": "limit_below_usage"}),
         f"{BELOW} 최소 1.57 GiB가 필요합니다. 2 GiB 이상으로 정해 주세요.", "memory"),
        # The broker's wording (raven_web_host.limit_refusal).
        ((409, {"error": "The memory limit must be at least 1300MiB; the session uses 1.2GiB now",
                "code": "limit_below_usage"}),
         f"{BELOW} 최소 1.27 GiB가 필요합니다. 1.5 GiB 이상으로 정해 주세요.", "memory"),
        ((409, {"error": "Memory limit must be at least 2684354560 bytes", "code": "limit_below_usage"}),
         f"{BELOW} 2.5 GiB 이상으로 정해 주세요.", "memory"),
        # Without a number in the refusal the page uses the usage it showed plus the 64 MiB headroom.
        ((409, {"error": "The memory limit is below what the session uses now", "code": "limit_below_usage"}),
         f"{BELOW} 최소 1.27 GiB가 필요합니다. 1.5 GiB 이상으로 정해 주세요.", "memory"),
        ((409, {"error": "Below usage", "code": "limit_below_usage", "minimum": 2 * GIB}),
         f"{BELOW} 2 GiB 이상으로 정해 주세요.", "memory"),
        ((400, {"error": "Memory must be between 512MiB and 32GiB", "code": "limit_invalid"}),
         f"{INVALID} (Memory must be between 512MiB and 32GiB)", "memory"),
        ((409, {"error": "Session is paused; lowering limits needs a running session", "code": "session_paused"}),
         MESSAGES["session_paused"], "memory"),
        ((403, {"error": "The page expired.", "code": "csrf_token_invalid"}), MESSAGES["csrf_token_invalid"], "memory"),
        ((502, {"error": "The Raven broker is unavailable", "code": "broker_unavailable"}),
         MESSAGES["broker_unavailable"], "memory"),
        ("abort", MESSAGES["network"], "memory"),
    )
    desk.memory.fill("1")
    for response, message, focused in cases:
        desk.fail(path, response)
        desk.save.click()
        expect(desk.error).to_have_text(message)
        expect(desk.dialog).to_be_visible()
        expect(desk.save).to_be_enabled()
        expect(desk.memory if focused == "memory" else desk.cpus).to_be_focused()
        if message.endswith("최소 1.57 GiB가 필요합니다. 2 GiB 이상으로 정해 주세요."):
            desk.shot("limits-error-below-usage.png")
    bodies = [body for request_key, body in desk.bodies if request_key == path]
    assert bodies == [{"generation": "1" * 64, "memory": GIB}] * len(cases), bodies
    desk.assert_no_overflow()

    # A CPU-only change refused by the server puts the cursor back on the CPU field.
    desk.memory.fill("2")
    desk.cpus.fill("40")
    desk.fail(path, (400, {"error": "CPUs must be between 0.5 and 16", "code": "limit_invalid"}))
    desk.save.click()
    expect(desk.error).to_have_text(f"{INVALID} (CPUs must be between 0.5 and 16)")
    expect(desk.cpus).to_be_focused()
    assert desk.bodies[-1] == (path, {"generation": "1" * 64, "cpus": 40})

    # A session recreated underneath: the reason shows and the list behind the dialog is reloaded.
    before = len(desk.requests)
    desk.fail(path, (502, {"error": "Session changed; refresh the session list", "code": "session_failed"}))
    desk.save.click()
    expect(desk.error).to_have_text("세션 작업에 실패했습니다: Session changed; refresh the session list")
    expect(desk.page.locator("#loading-status")).to_have_text("")
    assert desk.requests[before:] == [path, LIST], desk.requests[before:]

    # The error clears on the next try, and a good answer closes the dialog.
    desk.cpus.fill("4")
    desk.save.click()
    expect(desk.dialog).to_be_hidden()
    expect(desk.stats("work")).to_have_text("메모리 1.2 / 2 GiB · CPU 3% / 4코어 · 보는 중 X11 1 · 웹 1")
    desk.open_limits("work")
    expect(desk.error).to_be_hidden()
    desk.page.keyboard.press("Escape")

    desk.page.set_viewport_size(PHONE)
    desk.open_limits("work")
    desk.memory.fill("1")
    desk.fail(path, (409, {"error": "the minimum is 1600MiB", "code": "limit_below_usage"}))
    desk.save.click()
    expect(desk.error).to_have_text(f"{BELOW} 최소 1.57 GiB가 필요합니다. 2 GiB 이상으로 정해 주세요.")
    desk.assert_no_overflow()
    desk.shot("limits-error-phone.png")
    return {"messages": len(cases) + 2}


def login_expiry_during_save(desk):
    desk.open_limits("work")
    desk.memory.fill("3")
    desk.fail(key("POST", "41", "limits"), (401, {"error": "Authentication required", "code": "auth_required"}))
    desk.save.click()
    desk.page.wait_for_url(ORIGIN + "/login/")
    assert desk.requests == [LIST, key("POST", "41", "limits"), "GET /login/"], desk.requests
    return {"redirect": "/login/"}


def create(desk, name):
    """Submit the new-session dialog and come back to the list; return the body it sent."""
    desk.page.locator("#session-name").fill(name)
    desk.page.locator("#create-form button[type=submit]").click()
    expect(desk.page.locator("#connection-status")).to_have_text("연결됨")
    desk.page.locator("#close-viewer").click()
    expect(desk.page.locator("#workspace")).to_be_visible()
    expect(desk.page.locator("#loading-status")).to_have_text("")
    request_key, body = [item for item in desk.bodies if item[0] == CREATE][-1]
    return body


def create_sends_limits_only_when_changed(desk):
    details = desk.page.locator("#create-resources")
    summary = desk.page.locator("#create-resources-summary")
    memory = desk.page.locator("#create-memory")
    cpus = desk.page.locator("#create-cpus")
    group = lambda target: target.locator("xpath=ancestor::div[contains(@class,'resource-field')]")
    base = {"wm": "xmonad", "geometry": "1600x900", "command": "xterm"}

    desk.page.locator("#new-session").click()
    expect(desk.page.locator("#create-dialog")).to_be_visible()
    expect(details).not_to_have_attribute("open", "")
    expect(summary).to_have_text("메모리 2 GiB · CPU 2코어")
    expect(memory).to_be_hidden()
    assert create(desk, "plain") == {"name": "plain", **base}
    expect(desk.stats("plain")).to_have_text("메모리 200 MiB / 2 GiB · CPU 1% / 2코어 · 보는 중 X11 0 · 웹 0")

    desk.page.locator("#new-session").click()
    details.locator("summary").click()
    expect(memory).to_be_visible()
    expect(memory).to_have_value("2")
    expect(cpus).to_have_value("2")
    expect(group(memory).locator(".choice[aria-pressed=true]")).to_have_text("2 GiB")
    desk.assert_no_overflow()
    desk.shot("limits-create-desktop.png")
    group(memory).locator(".choice", has_text="4 GiB").click()
    expect(summary).to_have_text("메모리 4 GiB · CPU 2코어")
    assert create(desk, "bigger") == {"name": "bigger", **base, "memory": 4 * GIB}

    # Every opening starts folded, at the defaults.
    desk.page.locator("#new-session").click()
    expect(details).not_to_have_attribute("open", "")
    expect(summary).to_have_text("메모리 2 GiB · CPU 2코어")
    details.locator("summary").click()
    expect(memory).to_have_value("2")
    group(memory).locator(".choice", has_text="최대").click()
    group(cpus).locator(".choice", has_text="최대").click()
    expect(summary).to_have_text("메모리 최대 · CPU 최대")
    expect(memory).to_have_attribute("placeholder", "최대")
    desk.shot("limits-create-max.png")
    assert create(desk, "everything") == {"name": "everything", **base, "memory": "max", "cpus": "max"}
    expect(desk.stats("everything")).to_have_text("메모리 200 MiB / 32 GiB · CPU 1% / 16코어 · 보는 중 X11 0 · 웹 0")

    # A value moved and moved back is not sent; a typed CPU count is.
    desk.page.locator("#new-session").click()
    details.locator("summary").click()
    group(memory).locator(".choice", has_text="8 GiB").click()
    group(memory).locator(".choice", has_text="2 GiB").click()
    cpus.fill("1.5")
    expect(summary).to_have_text("메모리 2 GiB · CPU 1.5코어")
    assert create(desk, "lighter") == {"name": "lighter", **base, "cpus": 1.5}

    # A bad value in the folded section unfolds it instead of sending.
    sent = len(desk.bodies)
    desk.page.locator("#new-session").click()
    desk.page.locator("#session-name").fill("refused")
    details.locator("summary").click()
    memory.fill("0.7")
    details.locator("summary").click()
    expect(memory).to_be_hidden()
    desk.page.locator("#create-form button[type=submit]").click()
    expect(details).to_have_attribute("open", "")
    assert not memory.evaluate("node => node.checkValidity()")
    desk.settle()
    assert len(desk.bodies) == sent, desk.bodies[sent:]

    # A limit the server refuses shows in Korean with the server's bounds and the section open.
    memory.fill("128")
    details.locator("summary").click()
    desk.fail(CREATE, (400, {"error": "Memory must be between 512MiB and 32GiB", "code": "limit_invalid"}))
    desk.page.locator("#create-form button[type=submit]").click()
    expect(desk.page.locator("#create-error")).to_have_text(f"{INVALID} (Memory must be between 512MiB and 32GiB)")
    expect(details).to_have_attribute("open", "")
    assert desk.bodies[-1] == (CREATE, {"name": "refused", **base, "memory": 128 * GIB})

    desk.page.set_viewport_size(PHONE)
    desk.assert_no_overflow()
    desk.page.locator("#create-dialog").evaluate("node => node.scrollTop = node.scrollHeight")
    desk.shot("limits-create-phone.png")
    desk.page.keyboard.press("Escape")
    expect(desk.page.locator("#create-dialog")).to_be_hidden()
    return {"creates": 4, "refused": 2}


def percent_choices_send_shares(desk):
    # No server totals here: the shares go as strings and the help names only the share.
    desk.open_limits("work")
    desk.choice("memory", "25%").click()
    expect_resource(desk, "memory", "", ["25%"], "25%")
    expect(desk.page.locator("#limits-memory-help")).to_have_text(
        "지금 1.2 GiB 사용 중 · 서버 메모리의 25%까지 씁니다.", use_inner_text=True)
    desk.choice("cpus", "50%").click()
    expect_resource(desk, "cpus", "", ["50%"], "50%")
    expect(desk.page.locator("#limits-cpus-help")).to_have_text(
        "지금 CPU 3% 사용 중 · 서버 코어의 50%까지 씁니다.", use_inner_text=True)
    # Another share replaces the first; a size choice leaves shares.
    desk.choice("memory", "75%").click()
    expect_resource(desk, "memory", "", ["75%"], "75%")
    desk.shot("limits-dialog-shares.png")
    desk.save.click()
    expect(desk.dialog).to_be_hidden()
    # The list reports what the shares became, in numbers.
    expect(desk.stats("work")).to_have_text("메모리 1.2 / 24 GiB · CPU 3% / 8코어 · 보는 중 X11 1 · 웹 1")
    assert desk.bodies == [(key("POST", "41", "limits"),
                            {"generation": "1" * 64, "memory": "75%", "cpus": "50%"})], desk.bodies

    desk.open_limits("open")
    desk.choice("memory", "50%").click()
    desk.choice("memory", "4 GiB").click()
    expect_resource(desk, "memory", "4", ["4 GiB"], "제한 없음")
    desk.choice("cpus", "25%").click()
    desk.save.click()
    expect(desk.dialog).to_be_hidden()
    expect(desk.stats("open")).to_have_text("메모리 512 MiB / 4 GiB · CPU <1% / 4코어 · 보는 중 X11 0 · 웹 2")
    assert desk.bodies[-1] == (key("POST", "44", "limits"), {"generation": "4" * 64, "memory": 4 * GIB, "cpus": "25%"})

    desk.page.locator("#new-session").click()
    desk.page.locator("#create-resources summary").click()
    create_memory = desk.page.locator("#create-memory")
    labels = desk.page.locator("#create-resources .choice").all_inner_texts()
    assert labels == ["1 GiB", "2 GiB", "4 GiB", "8 GiB", "25%", "50%", "75%", "최대", "25%", "50%", "75%", "최대"], labels
    group = lambda kind: desk.page.locator(f"#create-{kind}").locator(
        "xpath=ancestor::div[contains(@class,'resource-field')]")
    group("memory").locator(".choice", has_text="50%").click()
    group("cpus").locator(".choice", has_text="75%").click()
    expect(create_memory).to_have_attribute("placeholder", "50%")
    expect(desk.page.locator("#create-resources-summary")).to_have_text("메모리 50% · CPU 75%")
    expect(desk.page.locator("#create-memory-help")).to_have_text("서버 메모리의 50%까지 씁니다.", use_inner_text=True)
    assert create(desk, "shares") == {"name": "shares", "wm": "xmonad", "geometry": "1600x900", "command": "xterm",
                                      "memory": "50%", "cpus": "75%"}
    expect(desk.stats("shares")).to_have_text("메모리 200 MiB / 16 GiB · CPU 1% / 12코어 · 보는 중 X11 0 · 웹 0")
    return {"share_requests": 3}


def share_of_server_with_totals(desk):
    current = {kind: desk.page.locator(f"#limits-{kind}-current") for kind in ("memory", "cpus")}
    help_line = {kind: desk.page.locator(f"#limits-{kind}-help") for kind in ("memory", "cpus")}
    # The cards keep their wording; the share shows only in the dialog.
    expect(desk.stats("work")).to_have_text("메모리 1.2 / 2 GiB · CPU 3% / 2코어 · 보는 중 X11 1 · 웹 1")
    desk.open_limits("work")
    expect(current["memory"]).to_have_text("지금 상한 2 GiB · 서버의 6%")
    expect(current["cpus"]).to_have_text("지금 상한 2코어 · 서버의 13%")
    desk.assert_no_overflow()
    desk.shot("limits-dialog-totals.png")
    for field, label, text in (
            ("memory", "25%", "지금 1.2 GiB 사용 중 · 서버 메모리의 25%(8 GiB)까지 씁니다."),
            ("memory", "최대", "지금 1.2 GiB 사용 중 · 서버의 전체 메모리(32 GiB)까지 씁니다."),
            ("cpus", "75%", "지금 CPU 3% 사용 중 · 서버 코어의 75%(12코어)까지 씁니다."),
            ("cpus", "최대", "지금 CPU 3% 사용 중 · 서버의 모든 코어(16코어)를 씁니다.")):
        desk.choice(field, label).click()
        expect(help_line[field]).to_have_text(text, use_inner_text=True)
    # The share line is about the limit now, not the one being chosen.
    expect(current["memory"]).to_have_text("지금 상한 2 GiB · 서버의 6%")
    desk.page.keyboard.press("Escape")
    expect(desk.dialog).to_be_hidden()
    for name, memory, cpus in (("open", "지금 제한 없음", "지금 제한 없음"),
                               ("small", "지금 상한 512 MiB · 서버의 2%", "지금 상한 0.5코어 · 서버의 3%"),
                               ("frozen", "지금 상한 4 GiB · 서버의 13%", "지금 상한 1.5코어 · 서버의 9%"),
                               ("odd", "지금 상한 700 MiB · 서버의 2%", "지금 상한 1.25코어 · 서버의 8%")):
        desk.open_limits(name)
        expect(current["memory"]).to_have_text(memory)
        expect(current["cpus"]).to_have_text(cpus)
        desk.page.keyboard.press("Escape")
        expect(desk.dialog).to_be_hidden()

    desk.page.set_viewport_size(PHONE)
    desk.open_limits("work")
    desk.choice("memory", "50%").click()
    desk.assert_no_overflow()
    desk.shot("limits-dialog-totals-phone.png")
    desk.page.keyboard.press("Escape")
    assert desk.requests == [LIST], desk.requests
    return {"dialogs": 6}


def upper_bound_with_totals(desk):
    desk.open_limits("work")
    for memory, cpus, field, message in (
            ("33", "2", "memory", "메모리는 서버 전체(32 GiB)보다 크게 정할 수 없습니다. 서버 전체를 쓰려면 최대를 고르세요."),
            ("32", "16.5", "cpus", "CPU는 서버 코어 수(16코어)보다 크게 정할 수 없습니다. 모든 코어를 쓰려면 최대를 고르세요.")):
        desk.memory.fill(memory)
        desk.cpus.fill(cpus)
        desk.save.click()
        desk.settle()
        expect(desk.dialog).to_be_visible()
        target = desk.memory if field == "memory" else desk.cpus
        assert target.evaluate("node => node.validationMessage") == message, field
    assert desk.bodies == [], desk.bodies
    # The whole server is still allowed.
    desk.cpus.fill("16")
    desk.save.click()
    expect(desk.dialog).to_be_hidden()
    assert desk.bodies == [(key("POST", "41", "limits"),
                            {"generation": "1" * 64, "memory": 32 * GIB, "cpus": 16})], desk.bodies

    details = desk.page.locator("#create-resources")
    desk.page.locator("#new-session").click()
    desk.page.locator("#session-name").fill("too-big")
    details.locator("summary").click()
    desk.page.locator("#create-memory").fill("40")
    details.locator("summary").click()
    desk.page.locator("#create-form button[type=submit]").click()
    expect(details).to_have_attribute("open", "")
    assert desk.page.locator("#create-memory").evaluate("node => node.validationMessage") == (
        "메모리는 서버 전체(32 GiB)보다 크게 정할 수 없습니다. 서버 전체를 쓰려면 최대를 고르세요.")
    desk.page.locator("#create-memory").fill("8")
    desk.page.locator("#create-cpus").fill("17")
    desk.page.locator("#create-form button[type=submit]").click()
    desk.settle()
    assert desk.page.locator("#create-cpus").evaluate("node => node.validationMessage") == (
        "CPU는 서버 코어 수(16코어)보다 크게 정할 수 없습니다. 모든 코어를 쓰려면 최대를 고르세요.")
    assert [body for request_key, body in desk.bodies if request_key == CREATE] == []
    desk.page.locator("#create-cpus").fill("16")
    assert create(desk, "too-big") == {"name": "too-big", "wm": "xmonad", "geometry": "1600x900", "command": "xterm",
                                       "memory": 8 * GIB, "cpus": 16}
    return {"refused_in_page": 4}


def upper_bound_without_totals(desk):
    # An older gateway does not tell the server's totals; the server alone judges the upper bound.
    desk.open_limits("work")
    desk.memory.fill("40")
    desk.cpus.fill("17")
    desk.save.click()
    expect(desk.dialog).to_be_hidden()
    assert desk.bodies == [(key("POST", "41", "limits"),
                            {"generation": "1" * 64, "memory": 40 * GIB, "cpus": 17})], desk.bodies
    desk.page.locator("#new-session").click()
    desk.page.locator("#create-resources summary").click()
    desk.page.locator("#create-cpus").fill("17")
    assert create(desk, "no-totals") == {"name": "no-totals", "wm": "xmonad", "geometry": "1600x900",
                                         "command": "xterm", "cpus": 17}
    return {"sent": 2}


SCENARIOS = (
    (stats_show_limits, None, False),
    (dialog_opens_with_current_values, None, False),
    (save_sends_bytes_and_generation, None, False),
    (max_choice_sends_max, None, False),
    (errors_show_korean_messages, None, False),
    (login_expiry_during_save, None, False),
    (create_sends_limits_only_when_changed, None, False),
    (percent_choices_send_shares, None, False),
    (share_of_server_with_totals, None, True),
    (upper_bound_with_totals, None, True),
    (upper_bound_without_totals, None, False),
)

failed = []
with sync_playwright() as playwright:
    browser = playwright.chromium.launch(chromium_sandbox=True)
    for scenario, viewport, totals in SCENARIOS:
        desk = Desk(browser, viewport, totals)
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

print(json.dumps({"limits_ui": "failed" if failed else "passed", "scenarios": len(SCENARIOS), "failed": failed}))
if failed:
    raise SystemExit(1)
