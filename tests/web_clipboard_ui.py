"""Browser half of web_clipboard_e2e.py. Viewer A shows desktop 1 and viewer B desktop 2; the host side reads and
sets each desktop's X clipboard with xclip between the steps announced on stdout."""

import json
import sys

from playwright.sync_api import expect, sync_playwright

options = json.loads(sys.stdin.readline())
origin = options["origin"]
FIRST, SECOND = options["first"], options["second"]
WAIT = 20000


def step(name):
    print(name, flush=True)
    assert sys.stdin.readline().strip() == "go", "The host side failed"


def log_in(context):
    page = context.new_page()
    page.goto(origin + "/login/", wait_until="networkidle")
    page.get_by_label("아이디").fill("owner")
    page.get_by_label("패스워드").fill(options["password"])
    page.locator("button[type=submit]").click()
    page.wait_for_selector(".session-open", timeout=WAIT)
    return page


def connected(page):
    # The page's CSP forbids string evaluation, so wait with locator assertions instead of wait_for_function.
    expect(page.locator("#connection-status")).to_have_text("연결됨", timeout=WAIT)


def open_viewer(page, name):
    page.locator(".session-card", has_text=name).locator(".session-open").click()
    connected(page)
    page.locator("#clipboard-toggle").click()
    expect(page.locator("#clipboard-panel")).to_be_visible()


results = {}
with sync_playwright() as playwright:
    browser = playwright.chromium.launch(chromium_sandbox=True)
    context = browser.new_context(ignore_https_errors=True, viewport={"width": 1280, "height": 900})
    context.grant_permissions(["clipboard-read", "clipboard-write"], origin=origin)
    first = log_in(context)
    second = context.new_page()
    second.goto(origin + "/", wait_until="networkidle")
    second.wait_for_selector(".session-open", timeout=WAIT)
    open_viewer(first, options["sessions"][0])
    open_viewer(second, options["sessions"][1])

    first.locator("#clipboard-outgoing").fill(FIRST)
    first.locator("#clipboard-send").click()
    expect(first.locator("#clipboard-status")).to_contain_text("세션 클립보드에 넣었습니다")
    step("sent")
    results["pc_to_session"] = "passed"

    step("copied")
    expect(second.locator("#clipboard-incoming")).to_have_value(SECOND, timeout=WAIT)
    expect(second.locator("#clipboard-copy")).to_be_enabled()
    first.wait_for_timeout(1500)
    assert first.locator("#clipboard-incoming").input_value() != SECOND, "Desktop 2's clipboard reached viewer A"
    results["session_to_pc"] = "passed"

    second.locator("#clipboard-copy").click()
    assert second.evaluate("navigator.clipboard.readText()") == SECOND
    second.evaluate("text => navigator.clipboard.writeText(text)", FIRST + " (PC)")
    second.locator("#clipboard-read").click()
    expect(second.locator("#clipboard-outgoing")).to_have_value(FIRST + " (PC)", timeout=WAIT)
    results["pc_clipboard"] = "passed"

    first.locator("#clipboard-outgoing").fill("가" * 90000)
    first.locator("#clipboard-send").click()
    expect(first.locator("#clipboard-status")).to_contain_text("텍스트가 너무 깁니다 (264 KiB)")
    step("oversized")
    results["size_limit"] = "passed"

    second.locator("#close-viewer").click()
    second.locator(".session-card", has_text=options["sessions"][1]).locator(".session-open").click()
    connected(second)
    expect(second.locator("#clipboard-panel")).to_be_hidden()
    assert second.locator("#clipboard-incoming").input_value() == ""
    assert second.locator("#clipboard-outgoing").input_value() == ""
    results["cleared_on_close"] = "passed"
    first.screenshot(path="/output/clipboard-panel.png")

    denied = browser.new_context(ignore_https_errors=True, viewport={"width": 390, "height": 844})
    page = log_in(denied)
    open_viewer(page, options["sessions"][0])
    page.locator("#clipboard-read").click()
    expect(page.locator("#clipboard-status")).to_contain_text("'PC → 세션' 칸에 직접 붙여넣으세요", timeout=WAIT)
    assert page.evaluate("document.documentElement.scrollWidth <= window.innerWidth")
    page.screenshot(path="/output/clipboard-mobile.png")
    results["permission_denied"] = "passed"
print(json.dumps({"clipboard_ui": results}, ensure_ascii=False), flush=True)
