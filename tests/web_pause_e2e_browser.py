"""Browser side of web_pause_e2e.py: pause and resume a desktop from the real page, one step per host "go"."""

import json
import sys

from playwright.sync_api import expect, sync_playwright

options = json.loads(sys.stdin.readline())
origin = options["origin"]


def emit(step, **values):
    print(json.dumps({"step": step, **values}, ensure_ascii=False), flush=True)


def wait():
    if sys.stdin.readline().strip() != "go":
        raise SystemExit("host stopped")


with sync_playwright() as playwright:
    browser = playwright.chromium.launch(chromium_sandbox=True)
    context = browser.new_context(ignore_https_errors=True, viewport={"width": 1366, "height": 860})
    page = context.new_page()
    page.goto(origin + "/login/", wait_until="networkidle")
    page.get_by_label("아이디").fill("owner")
    page.get_by_label("패스워드").fill(options["password"])
    page.locator("button[type=submit]").click()
    page.wait_for_url(origin + "/")
    token = page.locator('meta[name="csrf-token"]').get_attribute("content")
    card = page.locator(".session-card", has_text=options["session"])
    status = page.locator("#connection-status")

    # Pause from the viewer toolbar: the viewer closes and the card offers to resume.
    card.locator(".session-open").click()
    expect(status).to_have_text("연결됨", timeout=30000)
    page.locator("#pause-viewer").click()
    expect(card.locator(".session-resume")).to_be_visible(timeout=30000)
    expect(page.locator("#viewer")).to_be_hidden()
    page.screenshot(path="/output/paused-list.png")
    emit("paused", stats=card.locator(".session-stats").inner_text())
    wait()

    # A paused desktop refuses to open; resuming from the card brings back 연결.
    response = page.request.post(f"{origin}/api/sessions/{options['id']}/open/",
                                 headers={"X-CSRFToken": token, "Origin": origin, "Referer": origin + "/"})
    refused = [response.status, response.json().get("code")]
    card.locator(".session-resume").click()
    expect(card.locator(".session-open")).to_be_visible(timeout=30000)
    emit("resumed", refused=refused)
    wait()

    # Paused from elsewhere (the CLI) while connected: no automatic reconnect, an offer instead.
    card.locator(".session-open").click()
    expect(status).to_have_text("연결됨", timeout=30000)
    emit("connected")
    wait()
    expect(page.locator("#viewer-paused")).to_be_visible(timeout=60000)
    page.screenshot(path="/output/paused-viewer.png")
    emit("offered", status=status.inner_text())
    wait()
    page.locator("#resume-viewer").click()
    expect(status).to_have_text("연결됨", timeout=30000)
    expect(page.locator("#viewer-paused")).to_be_hidden()
    emit("reconnected")
    wait()
    browser.close()
