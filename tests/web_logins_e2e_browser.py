"""Browser side of web_logins_e2e.py: two browsers, A and B, log in, view the same desktop and end logins through the
login API (not the page's buttons), one step per host "go". Prints one JSON line per step; the cookie values in it
are for the host's leak checks."""

import json
import sys
import time

from playwright.sync_api import expect, sync_playwright

options = json.loads(sys.stdin.readline())
origin = options["origin"]
SESSION_COOKIE = "__Host-raven_session"
CLOSES = "raven-e2e-closes"
# Keep each desktop WebSocket's close code in sessionStorage, which survives the move to /login/ after a revocation.
# A plain function keeps the native socket object, whose prototype noVNC inspects.
RECORD_CLOSES = """
(() => {
  const Native = window.WebSocket;
  function Recorded(...args) {
    const socket = new Native(...args);
    socket.addEventListener("close", (event) => {
      const closes = JSON.parse(sessionStorage.getItem("%s") || "[]");
      closes.push(event.code);
      sessionStorage.setItem("%s", JSON.stringify(closes));
    });
    return socket;
  }
  Recorded.prototype = Native.prototype;
  Object.assign(Recorded, {CONNECTING: 0, OPEN: 1, CLOSING: 2, CLOSED: 3});
  window.WebSocket = Recorded;
})();
""" % (CLOSES, CLOSES)
PROBE = """
(url) => new Promise((resolve) => {
  const socket = new WebSocket(url, ["binary"]);
  let opened = false;
  socket.onopen = () => { opened = true; socket.close(); };
  socket.onclose = (event) => resolve({opened, code: event.code});
})
"""
REVOKED_WAIT_MS = 20000


def emit(step, **values):
    print(json.dumps({"step": step, **values}, ensure_ascii=False), flush=True)


def wait():
    if sys.stdin.readline().strip() != "go":
        raise SystemExit("host stopped")


responses = []


def call(page, method, path, body=None):
    """The login API with this browser's cookies; a change carries the page's CSRF token, Origin and Referer."""
    headers = {}
    if method == "POST":
        token = page.locator('meta[name="csrf-token"]').get_attribute("content")
        headers = {"X-CSRFToken": token, "Origin": origin, "Referer": origin + "/"}
    if body is not None:
        headers["Content-Type"] = "application/json"
    response = page.request.fetch(origin + path, method=method, headers=headers,
                                  data=None if body is None else json.dumps(body))
    responses.append(response.text())
    return response.status, response.json()


def login(page):
    page.goto(origin + "/login/", wait_until="networkidle")
    page.get_by_label("아이디").fill("owner")
    page.get_by_label("패스워드").fill(options["password"])
    page.locator("button[type=submit]").click()
    page.wait_for_url(origin + "/")
    page.evaluate(f"sessionStorage.removeItem('{CLOSES}')")


def view(page):
    page.locator(".session-card", has_text=options["session"]).locator(".session-open").click()
    expect(page.locator("#connection-status")).to_have_text("연결됨", timeout=30000)


def closes(page):
    return json.loads(page.evaluate(f"sessionStorage.getItem('{CLOSES}') || '[]'"))


def cookies(context):
    return {cookie["name"]: cookie["value"] for cookie in context.cookies()}


def current_id(page):
    status, data = call(page, "GET", "/api/logins/")
    assert status == 200, (status, data)
    return next(login["id"] for login in data["logins"] if login["current"])


def left_for_login(page):
    """Wait until the page went to the login screen the way it does after a refused API request."""
    page.wait_for_url(origin + "/login/", timeout=REVOKED_WAIT_MS)
    return {"closes": closes(page), "api": list(call(page, "GET", "/api/logins/"))}


with sync_playwright() as playwright:
    browser = playwright.chromium.launch(chromium_sandbox=True)

    def replay(session):
        """An earlier login cookie, used from a fresh browser: the API, then a desktop WebSocket."""
        api = playwright.request.new_context(ignore_https_errors=True,
                                             extra_http_headers={"Cookie": f"{SESSION_COOKIE}={session}"})
        response = api.get(origin + "/api/logins/")
        responses.append(response.text())
        result = {"api": [response.status, response.json().get("code")]}
        api.dispose()
        context = browser.new_context(ignore_https_errors=True)
        page = context.new_page()
        page.goto(origin + "/login/", wait_until="networkidle")
        # The login page's response drops an unknown session cookie, so set it again right before connecting.
        context.add_cookies([{"name": SESSION_COOKIE, "value": session, "url": origin + "/", "secure": True,
                              "httpOnly": True, "sameSite": "Strict"}])
        result["websocket"] = page.evaluate(PROBE, origin.replace("https://", "wss://") +
                                            f"/ws/sessions/{options['id']}/")
        context.close()
        return result

    pages = {}
    contexts = {}
    for name in ("a", "b"):
        contexts[name] = browser.new_context(ignore_https_errors=True, viewport={"width": 1366, "height": 860})
        contexts[name].add_init_script(RECORD_CLOSES)
        pages[name] = contexts[name].new_page()
    a, b = pages["a"], pages["b"]

    # Two browsers view the same desktop.
    for page in (a, b):
        login(page)
        view(page)
    listed = {name: call(page, "GET", "/api/logins/") for name, page in pages.items()}
    emit("listed", a=listed["a"], b=listed["b"], raw=responses[0],
         cookies=[cookies(contexts["a"]), cookies(contexts["b"])])
    wait()

    # A ends B's login: only B's viewer closes, B's next request is refused and B lands on the login screen.
    b_id = current_id(b)
    revoked = call(a, "POST", f"/api/logins/{b_id}/revoke/")
    b_after = left_for_login(b)
    time.sleep(3)
    a_after = {"closes": closes(a), "status": a.locator("#connection-status").inner_text(),
               "list": call(a, "GET", "/api/logins/")}
    b.screenshot(path="/output/b-revoked.png")
    a.screenshot(path="/output/a-still-viewing.png")
    emit("revoked", b_id=b_id, response=revoked, b=b_after, a=a_after)
    wait()

    # B logs in again; the host lists and revokes it with the CLI.
    login(b)
    view(b)
    emit("relogged", b_id=current_id(b), a_id=current_id(a), cookies=[cookies(contexts["b"])])
    wait()
    b_after = left_for_login(b)
    emit("cli_revoked", b=b_after, a={"closes": closes(a), "status": a.locator("#connection-status").inner_text()})
    wait()

    # A ends every login, its own included; neither earlier cookie works again.
    login(b)
    view(b)
    saved = {name: cookies(contexts[name])[SESSION_COOKIE] for name in ("a", "b")}
    control = replay(saved["a"])
    a.evaluate(f"sessionStorage.removeItem('{CLOSES}')")
    revoked = call(a, "POST", "/api/logins/revoke/", {"scope": "all"})
    a_cookie_left = SESSION_COOKIE in cookies(contexts["a"])
    after = {name: left_for_login(page) for name, page in pages.items()}
    emit("revoked_all", response=revoked, a_cookie_left=a_cookie_left, after=after, control=control,
         replays={name: replay(value) for name, value in saved.items()},
         cookies=[{SESSION_COOKIE: value} for value in saved.values()])
    wait()

    # After a password change on the server, the login of the moment and its viewer end too.
    login(a)
    view(a)
    saved = cookies(contexts["a"])
    emit("before_password", cookies=[saved])
    wait()
    after = left_for_login(a)
    emit("password_changed", a=after, replay=replay(saved[SESSION_COOKIE]), responses=responses)
    wait()
    browser.close()
