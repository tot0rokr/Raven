import asyncio
import http.cookiejar
import json
import ssl
import struct
import sys
import urllib.error
import urllib.parse
import urllib.request
import zlib
from html.parser import HTMLParser
from pathlib import Path

from Crypto.Cipher import DES
from websockets.asyncio.client import connect
from websockets.exceptions import ConnectionClosed, InvalidStatus

stage = "configuration"
REQUEST_TIMEOUT = 30
MANAGEMENT_TIMEOUT = 130
CHECK_TIMEOUT = 170


class Tokens(HTMLParser):
    def handle_starttag(self, tag, attributes):
        attributes = dict(attributes)
        if tag == "meta" and attributes.get("name") == "csrf-token":
            self.token = attributes["content"]
        if tag == "input" and attributes.get("name") == "csrfmiddlewaretoken":
            self.token = attributes["value"]


class Stream:
    def __init__(self, websocket):
        self.websocket = websocket
        self.buffer = bytearray()

    async def read(self, length):
        while len(self.buffer) < length:
            data = await self.websocket.recv()
            assert isinstance(data, bytes), "RFB data must use binary WebSocket messages"
            self.buffer.extend(data)
        data = bytes(self.buffer[:length])
        del self.buffer[:length]
        return data


def save_png(path, width, height, pixels):
    def chunk(name, data):
        return struct.pack(">I", len(data)) + name + data + struct.pack(">I", zlib.crc32(name + data))

    rows = bytearray()
    for row in range(height):
        source = pixels[row * width * 4:(row + 1) * width * 4]
        rgb = bytearray(width * 3)
        rgb[0::3], rgb[1::3], rgb[2::3] = source[2::4], source[1::4], source[0::4]
        rows.extend(b"\0" + rgb)
    header = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
    Path(path).write_bytes(
        b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", header)
        + chunk(b"IDAT", zlib.compress(rows)) + chunk(b"IEND", b"")
    )


async def framebuffer(websocket, password, output):
    stream = Stream(websocket)
    assert await stream.read(12) == b"RFB 003.008\n", "Expected RFB 3.8"
    await websocket.send(b"RFB 003.008\n")
    count = (await stream.read(1))[0]
    assert count > 0 and 2 in await stream.read(count), "VncAuth is required"
    await websocket.send(b"\x02")
    key = bytes(int(f"{value:08b}"[::-1], 2) for value in password.encode("ascii")[:8].ljust(8, b"\0"))
    await websocket.send(DES.new(key, DES.MODE_ECB).encrypt(await stream.read(16)))
    assert await stream.read(4) == b"\0\0\0\0", "VNC authentication failed"
    await websocket.send(b"\x01")
    initialization = await stream.read(24)
    width, height = struct.unpack(">HH", initialization[:4])
    name_length = struct.unpack(">I", initialization[20:])[0]
    assert name_length <= 65536 and 0 < width * height <= 16000000, "Unexpected desktop dimensions"
    await stream.read(name_length)
    pixel_format = struct.pack(">BBBBHHHBBB3x", 32, 24, 0, 1, 255, 255, 255, 16, 8, 0)
    await websocket.send(b"\0\0\0\0" + pixel_format)
    await websocket.send(b"\x02\0" + struct.pack(">Hi", 1, 0))
    await websocket.send(b"\x05\0" + struct.pack(">HH", 0, 0))
    await websocket.send(b"\x03\0" + struct.pack(">HHHH", 0, 0, width, height))
    pixels = bytearray(width * height * 4)
    while True:
        kind = (await stream.read(1))[0]
        if kind == 2:
            continue
        if kind == 3:
            length = struct.unpack(">I", (await stream.read(7))[3:])[0]
            assert length <= 1048576, "Unexpected clipboard length"
            await stream.read(length)
            continue
        assert kind == 0, "Unexpected RFB server message"
        rectangles = struct.unpack(">H", (await stream.read(3))[1:])[0]
        for _ in range(rectangles):
            x, y, columns, rows, encoding = struct.unpack(">HHHHi", await stream.read(12))
            assert encoding == 0 and x + columns <= width and y + rows <= height, "Invalid raw rectangle"
            raw = await stream.read(columns * rows * 4)
            for row in range(rows):
                start = ((y + row) * width + x) * 4
                pixels[start:start + columns * 4] = raw[row * columns * 4:(row + 1) * columns * 4]
        if rectangles:
            break
    if output:
        save_png(output, width, height, pixels)
    return width, height


async def run(config):
    global stage
    origin = config["origin"]
    context = ssl.create_default_context(cadata=config["certificate"])
    jar = http.cookiejar.CookieJar()
    opener = urllib.request.build_opener(
        urllib.request.HTTPSHandler(context=context), urllib.request.HTTPCookieProcessor(jar),
    )

    def request(path, data=None, headers=None, method=None, timeout=REQUEST_TIMEOUT):
        request_headers = {"Origin": origin, **(headers or {})}
        target = urllib.request.Request(origin + path, data=data, headers=request_headers, method=method)
        try:
            response = opener.open(target, timeout=timeout)
        except urllib.error.HTTPError as error:
            response = error
        with response:
            return response.status, response.read()

    def csrf(document):
        parser = Tokens()
        parser.feed(document.decode())
        return parser.token

    websocket_url = origin.replace("https://", "wss://", 1) + f'/ws/sessions/{config["session_id"]}/'

    async def denied(cookie="", request_origin=origin):
        try:
            async with connect(websocket_url, ssl=context, origin=request_origin,
                               additional_headers={"Cookie": cookie} if cookie else {}):
                raise AssertionError("Unauthorized WebSocket was accepted")
        except InvalidStatus as error:
            assert error.response.status_code == 403, "Expected denied WebSocket upgrade"

    stage = "anonymous access"
    assert request("/api/sessions/")[0] == 401, "Anonymous API access was accepted"
    await denied()
    stage = "wrong host"
    wrong_host = "127.0.0.1:" + origin.rsplit(":", 1)[1]
    status, page = request("/login/", headers={"Host": wrong_host})
    assert status == 400 and origin.encode() in page, "Wrong host page did not name the origin"
    assert wrong_host.encode() not in page, "Wrong host page echoed the request host"
    status, data = request("/api/sessions/", headers={"Host": wrong_host})
    assert status == 400 and json.loads(data)["code"] == "invalid_host", "Wrong API host was not rejected"
    stage = "login"
    status, page = request("/login/")
    assert status == 200, "Login page was unavailable"
    form = urllib.parse.urlencode({
        "username": config["username"], "password": config["password"], "csrfmiddlewaretoken": csrf(page),
    }).encode()
    status, page = request("/login/", form)
    assert status == 200, "Login failed"
    token = csrf(page)

    def management_request(path, payload, method="POST"):
        return request(path, json.dumps(payload).encode(), {
            "X-CSRFToken": token, "Content-Type": "application/json",
        }, method=method, timeout=MANAGEMENT_TIMEOUT)

    cookie_request = urllib.request.Request(origin + "/")
    jar.add_cookie_header(cookie_request)
    cookie = cookie_request.get_header("Cookie")
    assert cookie and "__Host-raven_session=" in cookie, "Session cookie was not issued"
    stage = "authenticated API"
    status, data = request("/api/sessions/")
    assert status == 200, "Session list failed"
    original = next((
        session for session in json.loads(data)["sessions"] if str(session["id"]) == config["session_id"]
    ), None)
    assert original is not None, "Test desktop was not listed"
    original_generation = original["generation"]

    def different_generation(generation):
        return ("0" if generation[0] != "0" else "1") + generation[1:]

    management_id = None
    if config.get("management_name"):
        stage = "create session"
        status, data = management_request("/api/sessions/", {
            "name": config["management_name"], "wm": "openbox", "geometry": "800x600", "command": "xterm",
        })
        assert status == 201, "Session creation failed"
        created = json.loads(data)["session"]
        management_id = str(created["id"])
        management_generation = created["generation"]
        assert management_id != config["session_id"], "Creation returned the original test session"
        assert created["geometry"] == "800x600" and created["wm"] == "openbox", "Session options were lost"
        stage = "rename session"
        renamed = config["management_name"] + "-renamed"
        status, data = management_request(f"/api/sessions/{management_id}/rename/", {
            "name": renamed, "generation": different_generation(management_generation),
        })
        assert status == 502, "Rename accepted a stale generation"
        assert json.loads(data) == {
            "error": "Session changed; refresh the session list", "code": "session_failed",
        }, "Stale rename did not report the host message"
        status, data = request("/api/sessions/")
        assert status == 200 and any(
            str(session["id"]) == management_id and session["name"] == config["management_name"]
            and session["generation"] == management_generation
            for session in json.loads(data)["sessions"]
        ), "Rejected rename changed the session"
        status, data = management_request(f"/api/sessions/{management_id}/rename/", {
            "name": renamed, "generation": management_generation,
        })
        assert status == 200, "Rename failed"
        updated = json.loads(data)["session"]
        assert updated["name"] == renamed and updated["generation"] == management_generation, "Rename changed the generation"
        status, data = request("/api/sessions/")
        assert status == 200 and any(
            str(session["id"]) == management_id and session["name"] == renamed
            for session in json.loads(data)["sessions"]
        ), "Renamed session was not listed"
        stage = "run container command"
        status, _ = management_request(f'/api/sessions/{config["session_id"]}/run/', {
            "command": "touch /tmp/raven-web-stale-command-check",
            "generation": different_generation(original_generation),
        })
        assert status == 502, "Run accepted a stale generation"
        status, data = management_request(f'/api/sessions/{config["session_id"]}/run/', {
            "command": "printf 'raven-web-command-ok\\n' > /tmp/raven-web-command-check",
            "generation": original_generation,
        })
        assert status == 202 and json.loads(data) == {"started": True}, "Container command was not accepted"
        stage = "delete confirmation"
        status, _ = management_request(
            f"/api/sessions/{management_id}/",
            {"confirm": management_id + "0", "generation": management_generation}, method="DELETE",
        )
        assert status == 400, "Incorrect delete confirmation was accepted"
        status, _ = management_request(
            f"/api/sessions/{management_id}/",
            {"confirm": management_id, "generation": different_generation(management_generation)}, method="DELETE",
        )
        assert status == 502, "Delete accepted a stale generation"
        status, data = request("/api/sessions/")
        assert status == 200 and any(
            str(session["id"]) == management_id and session["generation"] == management_generation
            for session in json.loads(data)["sessions"]
        ), "Rejected deletion changed the session list"
    stage = "open desktop"
    status, data = request(f'/api/sessions/{config["session_id"]}/open/', b"", {"X-CSRFToken": token})
    assert status == 200, "Desktop open failed"
    desktop = json.loads(data)
    stage = "cross-origin WebSocket"
    await denied(cookie, "https://attacker.example")
    stage = "authenticated RFB"
    async with connect(websocket_url, ssl=context, origin=origin, additional_headers={"Cookie": cookie},
                       subprotocols=["binary"], max_size=1048576) as websocket:
        width, height = await framebuffer(websocket, desktop["password"], config.get("output"))
        if management_id is not None:
            stage = "delete created session"
            status, data = management_request(
                f"/api/sessions/{management_id}/",
                {"confirm": management_id, "generation": management_generation}, method="DELETE",
            )
            assert status == 200 and json.loads(data) == {"deleted": management_id}, "Session deletion failed"
            status, data = request("/api/sessions/")
            assert status == 200, "Session list failed after deletion"
            remaining = {str(session["id"]) for session in json.loads(data)["sessions"]}
            assert management_id not in remaining and config["session_id"] in remaining, "Deletion changed the wrong session"
        stage = "logout revocation"
        assert request("/logout/", b"", {"X-CSRFToken": token})[0] == 200, "Logout failed"
        try:
            async with asyncio.timeout(4):
                while True:
                    await websocket.recv()
        except ConnectionClosed as closed:
            assert closed.rcvd and closed.rcvd.code == 4401, "Logout did not revoke the WebSocket"
    assert request("/api/sessions/", headers={"Cookie": cookie})[0] == 401, "Logged-out cookie remained valid"
    result = {"ok": True, "session_id": config["session_id"], "width": width, "height": height}
    if management_id is not None:
        result.update({
            "management_id": management_id,
            "management_checks": ["create", "rename", "run_accepted", "delete_confirmation", "stale_generation", "delete"],
        })
    return result


if __name__ == "__main__":
    try:
        result = asyncio.run(asyncio.wait_for(run(json.load(sys.stdin)), timeout=CHECK_TIMEOUT))
    except Exception as error:
        print(json.dumps({"ok": False, "stage": stage, "error": type(error).__name__}))
        sys.exit(1)
    print(json.dumps(result))
