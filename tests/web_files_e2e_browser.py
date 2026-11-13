"""Browser side of web_files_e2e.py: log in, then drive the file API with the browser's cookies, one step per host
"go". Prints one JSON line per step."""

import hashlib
import json
import os
import sys
from urllib.parse import quote, unquote

from playwright.sync_api import sync_playwright

CHUNK = 4 * 1024 * 1024
options = json.loads(sys.stdin.readline())
origin = options["origin"]


def emit(step, **values):
    print(json.dumps({"step": step, **values}, ensure_ascii=False), flush=True)


def wait():
    if sys.stdin.readline().strip() != "go":
        raise SystemExit("host stopped")


with sync_playwright() as playwright:
    browser = playwright.chromium.launch(chromium_sandbox=True)
    context = browser.new_context(ignore_https_errors=True, accept_downloads=True,
                                  viewport={"width": 1366, "height": 860})
    page = context.new_page()
    page.goto(origin + "/login/", wait_until="networkidle")
    page.get_by_label("아이디").fill("owner")
    page.get_by_label("패스워드").fill(options["password"])
    page.locator("button[type=submit]").click()
    page.wait_for_url(origin + "/")
    token = page.locator('meta[name="csrf-token"]').get_attribute("content")
    unsafe = {"X-CSRFToken": token, "Origin": origin, "Referer": origin + "/"}
    api = page.request

    def status(response):
        try:
            return response.status, response.json().get("code")
        except Exception:  # noqa: BLE001 - a non-JSON body is part of what is reported
            return response.status, None

    def start(path, name, size, overwrite=False):
        return api.post(origin + "/api/files/uploads/", headers={**unsafe, "Content-Type": "application/json"},
                        data=json.dumps({"path": path, "name": name, "size": size, "overwrite": overwrite}))

    def send(upload, data, offset=0):
        response = None
        while offset < len(data):
            piece = data[offset:offset + CHUNK]
            response = api.put(f"{origin}/api/files/uploads/{upload}/?offset={offset}", data=piece,
                               headers={**unsafe, "Content-Type": "application/octet-stream"})
            assert response.status == 200, (response.status, response.text())
            offset += len(piece)
        return response

    def finish(upload, overwrite=False):
        return api.post(f"{origin}/api/files/uploads/{upload}/finish/",
                        headers={**unsafe, "Content-Type": "application/json"},
                        data=json.dumps({"overwrite": overwrite}))

    def upload(path, name, data, overwrite=False):
        response = start(path, name, len(data), overwrite)
        assert response.status == 201, (response.status, response.text())
        created = response.json()
        assert created["chunk_size"] == CHUNK, created
        send(created["token"], data)
        response = finish(created["token"], overwrite)
        assert response.status == 200, (response.status, response.text())
        return response.json()["file"]

    def listing(path=""):
        return api.get(origin + "/api/files/?path=" + quote(path))

    def download(path):
        return api.get(origin + "/api/files/download/?path=" + quote(path))

    # Uploads: several chunks with a Korean name, and a small file into a folder the session made.
    big = os.urandom(2 * CHUNK + 12345)
    small = "세션 폴더로 올린 작은 파일\n".encode()
    first = upload("", options["big_name"], big)
    assert first["size"] == len(big), first
    upload("sub dir", "작은.txt", small)
    names = {entry["name"]: entry for entry in listing().json()["entries"]}
    assert names[options["big_name"]]["type"] == "file" and names["sub dir"]["type"] == "dir", names
    assert ".raven-uploads" not in names, "The staging folder is listed"
    emit("uploaded", big=hashlib.sha256(big).hexdigest(), small=hashlib.sha256(small).hexdigest())
    wait()

    # Downloads: a file the session wrote, and the Korean name's Content-Disposition.
    response = download("from-session.bin")
    assert response.status == 200, status(response)
    session_file = hashlib.sha256(response.body()).hexdigest()
    response = download(options["big_name"])
    assert response.status == 200 and hashlib.sha256(response.body()).digest() == hashlib.sha256(big).digest()
    disposition = response.headers["content-disposition"]
    encoded = disposition.split("filename*=UTF-8''", 1)[1].split(";", 1)[0]
    assert unquote(encoded) == options["big_name"], disposition
    assert response.headers["content-length"] == str(len(big)), response.headers
    emit("downloaded", session=session_file)
    wait()

    # Cancel after one chunk: the host checks that neither the temporary nor the final file remains.
    response = start("", "cancelled.bin", 3 * CHUNK)
    cancelled = response.json()["token"]
    send(cancelled, os.urandom(CHUNK))
    response = api.delete(f"{origin}/api/files/uploads/{cancelled}/", headers=unsafe)
    assert response.status == 200, status(response)
    response = api.put(f"{origin}/api/files/uploads/{cancelled}/?offset={CHUNK}", data=b"x",
                       headers={**unsafe, "Content-Type": "application/octet-stream"})
    assert status(response) == (404, "upload_unknown"), status(response)
    emit("cancelled")
    wait()

    # Overwrite: refused without consent at start, then replaced with consent.
    response = start("", options["big_name"], 10)
    assert status(response) == (409, "file_exists"), status(response)
    replacement = b"replaced!\n"
    upload("", options["big_name"], replacement, overwrite=True)
    # A file that appears between start and finish is not replaced silently either.
    response = start("", "race.txt", 4)
    racing = response.json()["token"]
    send(racing, b"mine")
    emit("raced", replacement=hashlib.sha256(replacement).hexdigest())
    wait()
    response = finish(racing)
    assert status(response) == (409, "file_exists"), status(response)
    response = finish(racing, overwrite=True)
    assert response.status == 200, status(response)
    response = api.put(f"{origin}/api/files/uploads/{'A' * 32}/?offset=0", data=b"x",
                       headers={**unsafe, "Content-Type": "application/octet-stream"})
    assert status(response) == (404, "upload_unknown"), status(response)
    emit("overwritten")
    wait()

    # The real dialog: upload with the file picker, then download the session's file with the link.
    picked = os.urandom(CHUNK + 4321)
    with open("/tmp/화면에서 올림.bin", "wb") as source:
        source.write(picked)
    page.locator("#files-open").click()
    page.locator("#files-input").set_input_files("/tmp/화면에서 올림.bin")
    page.locator('#files-upload-list .upload-row[data-state="done"]').wait_for(timeout=60000)
    page.locator("#files-list a[download]", has_text="from-session.bin").wait_for()
    page.screenshot(path="/output/files-dialog.png")
    with page.expect_download() as waiting:
        page.locator("#files-list a[download]", has_text="from-session.bin").click()
    saved = waiting.value.path()
    with open(saved, "rb") as received:
        clicked = hashlib.sha256(received.read()).hexdigest()
    page.keyboard.press("Escape")
    emit("ui", picked=hashlib.sha256(picked).hexdigest(), clicked=clicked)
    wait()

    # Boundaries: symlinks the session planted, traversal, unreadable and special files, auth and CSRF.
    results = {
        "passwd-link": status(download("passwd-link")),
        "root-link": status(listing("root-link")),
        "up-link": status(listing("up-link")),
        "through-link": status(download("root-link/etc/passwd")),
        "dotdot": status(download("../x")),
        "absolute": status(download("/etc/passwd")),
        "private": status(download("private.bin")),
        "fifo": status(download("fifo")),
        "directory": status(download("sub dir")),
        "missing": status(listing("no such dir")),
    }
    types = {entry["name"]: entry["type"] for entry in listing().json()["entries"]}
    stranger = browser.new_context(ignore_https_errors=True)
    results["anonymous"] = status(stranger.request.get(origin + "/api/files/?path="))
    results["anonymous-download"] = status(stranger.request.get(origin + "/api/files/download/?path=race.txt"))
    stranger.close()
    # Same origin, no token: the token check itself refuses it.
    response = api.post(origin + "/api/files/uploads/", headers={"Content-Type": "application/json",
                                                                 "Origin": origin, "Referer": origin + "/"},
                        data=json.dumps({"path": "", "name": "x", "size": 1, "overwrite": False}))
    results["no-csrf"] = status(response)
    response = start("", "huge.bin", 5 * 1024 ** 3)
    results["too-large"] = status(response)
    emit("boundaries", results=results, types={name: types.get(name) for name in ("passwd-link", "root-link", "fifo")})
    wait()
    browser.close()
