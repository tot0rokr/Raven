"""Exercise management controls with rendered HTML and intercepted API fixtures."""

import json
from pathlib import Path
import sys
from urllib.parse import urlparse

from playwright.sync_api import expect, sync_playwright


ORIGIN = "https://raven.test"
CSRF_TOKEN = "ui-fixture"
CREATED_GENERATION = "b" * 64
SOURCE = Path("/source")
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
sessions = [{
    "id": "41", "name": "existing", "state": "running", "wm": "xmonad",
    "geometry": "1600x900", "display": "140", "generation": "a" * 64,
    "command": "printf '<img src=x onerror=window.uiInjected=true>'",
}]
mutations = []
browser_errors = []
unexpected_requests = []


def serve(route):
    request = route.request
    url = urlparse(request.url)
    if url.netloc != "raven.test":
        unexpected_requests.append(request.url)
        route.abort()
        return
    path = url.path
    if path == "/":
        route.fulfill(content_type="text/html", body=options["html"])
        return
    assets = {"/static/app.js": "text/javascript", "/static/style.css": "text/css"}
    if path in assets:
        asset = SOURCE / "web" / "static" / Path(path).name
        route.fulfill(content_type=assets[path], body=asset.read_text())
        return
    if path == "/static/novnc/core/rfb.js":
        route.fulfill(content_type="text/javascript", body=RFB_FIXTURE)
        return
    if path == "/favicon.ico":
        route.fulfill(status=204)
        return
    body = request.post_data_json if request.post_data else None
    if path.startswith("/api/") and request.method != "GET":
        assert request.headers.get("x-csrftoken") == CSRF_TOKEN
        if body is not None:
            assert request.headers.get("content-type") == "application/json"
        mutations.append({"method": request.method, "path": path, "body": body})
    if path == "/api/sessions/" and request.method == "GET":
        route.fulfill(json={"sessions": sessions})
    elif path == "/api/sessions/" and request.method == "POST":
        session = {"id": "42", "state": "running", "display": "141", "generation": CREATED_GENERATION, **body}
        sessions.append(session)
        route.fulfill(status=201, json={"session": session})
    elif path == "/api/sessions/42/open/" and request.method == "POST":
        route.fulfill(json={
            "name": sessions[-1]["name"], "password": "fixture-only",
            "websocket": "/ws/sessions/42/",
        })
    elif path == "/api/sessions/42/rename/" and request.method == "POST":
        sessions[-1]["name"] = body["name"]
        route.fulfill(json={"session": sessions[-1]})
    elif path == "/api/sessions/42/run/" and request.method == "POST":
        route.fulfill(status=202, json={"started": True})
    elif path == "/api/sessions/42/" and request.method == "DELETE":
        assert body == {"confirm": "42", "generation": CREATED_GENERATION}
        sessions.pop()
        route.fulfill(json={"deleted": "42"})
    else:
        unexpected_requests.append(f"{request.method} {request.url}")
        route.fulfill(status=404, json={"error": "Unexpected fixture request"})


def assert_no_overflow(page):
    assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
    for dialog in page.locator("dialog[open]").all():
        assert dialog.evaluate("node => node.scrollWidth <= node.clientWidth")


def settle_events(page):
    page.evaluate("() => new Promise(resolve => requestAnimationFrame(() => requestAnimationFrame(resolve)))")


with sync_playwright() as playwright:
    browser = playwright.chromium.launch(chromium_sandbox=True)
    context = browser.new_context(viewport={"width": 1280, "height": 900})
    page = context.new_page()
    page.route("**/*", serve)
    page.on("pageerror", lambda error: browser_errors.append(str(error)))
    page.on("console", lambda message: browser_errors.append(message.text) if message.type == "error" else None)
    page.goto(ORIGIN, wait_until="networkidle")
    expect(page.locator("#session-count")).to_have_text("1")
    expect(page.locator(".session-command img")).to_have_count(0)
    assert page.evaluate("window.uiInjected !== true")
    assert_no_overflow(page)
    page.screenshot(path=str(output / "management-desktop.png"))

    page.locator("#new-session").click()
    expect(page.locator("#create-dialog")).to_be_visible()
    expect(page.locator("#session-name")).to_be_focused()
    page.locator("#session-name").fill("ui-created")
    page.locator("#session-wm").select_option("openbox")
    page.locator("#session-geometry").select_option("1280x720")
    page.locator("#session-command").fill("  xterm  ")
    assert_no_overflow(page)
    page.screenshot(path=str(output / "management-create.png"))
    page.locator("#create-form button[type=submit]").click()
    expect(page.locator("#viewer")).to_be_visible()
    expect(page.locator("#connection-status")).to_have_text("연결됨")
    expect(page.locator("#viewer-title")).to_have_text("ui-created")
    page.locator("#close-viewer").click()
    expect(page.locator("#workspace")).to_be_visible()
    expect(page.locator("#session-count")).to_have_text("2")

    card = page.locator(".session-card").filter(has_text="ui-created")
    card.locator("[data-action=rename]").click()
    expect(page.locator("#rename-name")).to_have_value("ui-created")
    page.locator("#rename-name").fill("ui-renamed")
    page.locator("#rename-form button[type=submit]").click()
    expect(page.locator("#rename-dialog")).not_to_be_visible()
    card = page.locator(".session-card").filter(has_text="ui-renamed")
    expect(card).to_have_count(1)
    card.locator("[data-action=run]").click()
    page.locator("#run-command").fill("  printf 'ui marker'  ")
    page.locator("#run-form button[type=submit]").click()
    expect(page.locator("#run-dialog")).not_to_be_visible()

    page.set_viewport_size({"width": 390, "height": 844})
    assert_no_overflow(page)
    page.screenshot(path=str(output / "management-mobile.png"))
    card.locator("[data-action=delete]").click()
    expect(page.locator("#delete-dialog [data-close-dialog]")).to_be_focused()
    expect(page.locator("#delete-session-name")).to_have_text("ui-renamed")
    expect(page.locator("#delete-description")).to_contain_text("홈 볼륨")
    assert_no_overflow(page)
    page.screenshot(path=str(output / "management-delete-mobile.png"))
    before_delete = len(mutations)
    page.locator("#delete-name").fill("wrong-name")
    page.locator("#confirm-delete").click()
    assert not page.locator("#delete-name").evaluate("node => node.checkValidity()")
    settle_events(page)
    assert len(mutations) == before_delete
    page.locator("#delete-name").fill("ui-renamed")
    page.locator("#delete-name").press("Enter")
    settle_events(page)
    expect(page.locator("#delete-dialog")).to_be_visible()
    assert len(mutations) == before_delete
    page.locator("#confirm-delete").click()
    expect(page.locator("#delete-dialog")).not_to_be_visible()
    expect(page.locator("#session-count")).to_have_text("1")
    expect(page.locator(".session-card").filter(has_text="ui-renamed")).to_have_count(0)
    assert_no_overflow(page)
    assert mutations == [
        {"method": "POST", "path": "/api/sessions/", "body": {
            "name": "ui-created", "wm": "openbox", "geometry": "1280x720", "command": "xterm",
        }},
        {"method": "POST", "path": "/api/sessions/42/open/", "body": None},
        {"method": "POST", "path": "/api/sessions/42/rename/", "body": {
            "name": "ui-renamed", "generation": CREATED_GENERATION,
        }},
        {"method": "POST", "path": "/api/sessions/42/run/", "body": {
            "command": "printf 'ui marker'", "generation": CREATED_GENERATION,
        }},
        {"method": "DELETE", "path": "/api/sessions/42/", "body": {
            "confirm": "42", "generation": CREATED_GENERATION,
        }},
    ], mutations
    assert not browser_errors, browser_errors
    assert not unexpected_requests, unexpected_requests
    print(json.dumps({
        "management_ui": "passed", "mutations": len(mutations), "csrf": "checked",
        "delete_confirmation": "checked", "desktop_mobile_layout": "checked",
        "browser_errors": len(browser_errors),
    }))
    browser.close()
