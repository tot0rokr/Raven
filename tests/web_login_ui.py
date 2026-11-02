"""Inspect the public login page without entering any credentials."""

import json
from pathlib import Path
import sys

from playwright.sync_api import sync_playwright

options = json.load(sys.stdin)
errors = []
with sync_playwright() as playwright:
    browser = playwright.chromium.launch(chromium_sandbox=True)
    context = browser.new_context(ignore_https_errors=True, viewport={"width": 1280, "height": 900})
    page = context.new_page()
    page.on("pageerror", lambda error: errors.append(str(error)))
    response = page.goto(options["origin"] + "/login/", wait_until="networkidle")
    assert response.status == 200
    assert page.get_by_label("아이디").is_visible()
    assert page.get_by_label("패스워드").is_visible()
    assert page.evaluate("async () => typeof (await import('/static/novnc/core/rfb.js')).default") == "function"
    output = Path(options["output"])
    page.screenshot(path=str(output / "login-desktop.png"))
    page.set_viewport_size({"width": 390, "height": 844})
    assert page.evaluate("document.documentElement.scrollWidth <= window.innerWidth")
    page.screenshot(path=str(output / "login-mobile.png"))
    with page.expect_response(lambda result: result.request.method == "POST" and result.url.endswith("/login/")) as submitted:
        page.locator("form").evaluate("form => { form.noValidate = true; form.requestSubmit(); }")
    submission = submitted.value
    headers = submission.request.all_headers()
    observation = {
        "status": submission.status,
        "origin": headers.get("origin"),
        "csrf_cookie_present": "__Host-raven_csrf=" in headers.get("cookie", ""),
        "referrer_policy": response.headers.get("referrer-policy"),
    }
    print(json.dumps({"empty_login_form": observation}), flush=True)
    assert observation["csrf_cookie_present"], "Browser did not send the CSRF cookie"
    assert observation["origin"] == options["origin"], "Browser form lost its origin"
    assert submission.status == 401, "Empty credentials must reach authentication, not fail CSRF"
    assert not errors, errors
    print(json.dumps({"login_ui": "passed", "novnc_module": "loaded", "browser_errors": len(errors)}))
    browser.close()
