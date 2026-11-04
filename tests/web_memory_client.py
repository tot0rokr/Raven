"""Exercise bounded authenticated viewer churn without browser automation."""

import asyncio
from contextlib import AsyncExitStack
import http.cookiejar
import json
import ssl
import struct
import sys
import time
import urllib.parse
import urllib.request

sys.path.insert(0, "/source/web/tests")
from live_client import Tokens, framebuffer
from websockets.asyncio.client import connect

REQUEST_TIMEOUT = 30
CONNECTION_TIMEOUT = 30
PROBE_TIMEOUT = 600
CONCURRENT_VIEWERS = 8
SLOW_SECONDS = 15


def error_details(error):
    if isinstance(error, BaseExceptionGroup):
        return [detail for child in error.exceptions for detail in error_details(child)]
    details = {"type": type(error).__name__, "message": str(error)[:300]}
    response = getattr(error, "response", None)
    if response is not None:
        details["http_status"] = response.status_code
    return [details]


async def checkpoint(event, **values):
    print(json.dumps({"event": event, **values}), flush=True)
    reply = await asyncio.wait_for(asyncio.to_thread(sys.stdin.readline), timeout=REQUEST_TIMEOUT)
    if reply.strip() != "continue":
        raise RuntimeError("The host monitor stopped the probe")


async def probe(config):
    origin = config["origin"]
    context = ssl.create_default_context(cadata=config["certificate"])
    jar = http.cookiejar.CookieJar()
    opener = urllib.request.build_opener(urllib.request.HTTPSHandler(context=context),
                                         urllib.request.HTTPCookieProcessor(jar))

    def request(path, body=None, headers=None):
        target = urllib.request.Request(origin + path, data=body, headers={"Origin": origin, **(headers or {})})
        with opener.open(target, timeout=REQUEST_TIMEOUT) as response:
            return response.read()

    def token(document):
        parser = Tokens()
        parser.feed(document.decode())
        return parser.token

    page = request("/login/")
    page = request("/login/", urllib.parse.urlencode({
        "username": config["username"], "password": config["password"], "csrfmiddlewaretoken": token(page),
    }).encode())
    csrf = token(page)
    cookie_request = urllib.request.Request(origin + "/")
    jar.add_cookie_header(cookie_request)
    cookie = cookie_request.get_header("Cookie")
    assert cookie and "__Host-raven_session=" in cookie
    desktop = json.loads(request(f'/api/sessions/{config["session_id"]}/open/', b"", {"X-CSRFToken": csrf}))
    url = origin.replace("https://", "wss://", 1) + f'/ws/sessions/{config["session_id"]}/'

    def connection():
        return connect(url, ssl=context, origin=origin, additional_headers={"Cookie": cookie},
                       subprotocols=["binary"], max_size=1048576, max_queue=1,
                       open_timeout=CONNECTION_TIMEOUT, close_timeout=2, ping_interval=None)

    await checkpoint("authenticated", connections=0)
    start = time.monotonic()
    for index in range(config["loops"]):
        async with asyncio.timeout(CONNECTION_TIMEOUT):
            async with connection() as websocket:
                assert await framebuffer(websocket, desktop["password"], None) == (800, 600)
        if (index + 1) % 10 == 0:
            await checkpoint("sequential", connections=index + 1, elapsed=round(time.monotonic() - start, 3))

    ready = 0
    all_ready = asyncio.Event()
    release = asyncio.Event()

    async def viewer():
        nonlocal ready
        async with connection() as websocket:
            assert await framebuffer(websocket, desktop["password"], None) == (800, 600)
            ready += 1
            if ready == CONCURRENT_VIEWERS:
                all_ready.set()
            await release.wait()

    concurrent_success = False
    try:
        async with asyncio.TaskGroup() as group:
            for _ in range(CONCURRENT_VIEWERS):
                group.create_task(viewer())
            await asyncio.wait_for(all_ready.wait(), timeout=CONNECTION_TIMEOUT)
            await checkpoint("concurrent", connections=CONCURRENT_VIEWERS)
            await asyncio.sleep(3)
            release.set()
        concurrent_success = True
    except ExceptionGroup as error:
        await checkpoint("concurrent_error", ready=ready, errors=error_details(error))
    if not concurrent_success:
        async with AsyncExitStack() as viewers:
            for _ in range(CONCURRENT_VIEWERS):
                websocket = await viewers.enter_async_context(connection())
                assert await framebuffer(websocket, desktop["password"], None) == (800, 600)
            await checkpoint("concurrent_ramped", connections=CONCURRENT_VIEWERS)
            await asyncio.sleep(3)
        concurrent_success = True
    await checkpoint("after_concurrent", connections=0)

    async with connection() as websocket:
        width, height = await framebuffer(websocket, desktop["password"], None)
        update = b"\x03\0" + struct.pack(">HHHH", 0, 0, width, height)
        await checkpoint("slow_start", connections=1)
        start = time.monotonic()
        next_sample = 5
        requests = 0
        while time.monotonic() - start < SLOW_SECONDS:
            await websocket.send(update)
            requests += 1
            await asyncio.sleep(0.05)
            elapsed = time.monotonic() - start
            if elapsed >= next_sample:
                await checkpoint("slow_consumer", seconds=round(elapsed, 3), requests=requests)
                next_sample += 5
    await checkpoint("after_slow", connections=0)
    await asyncio.sleep(5)
    await checkpoint("settled", connections=0)
    print(json.dumps({"event": "complete", "sequential": config["loops"], "concurrent": CONCURRENT_VIEWERS,
                      "concurrent_success": concurrent_success, "slow_seconds": SLOW_SECONDS}), flush=True)


if __name__ == "__main__":
    try:
        asyncio.run(asyncio.wait_for(probe(json.loads(sys.stdin.readline())), timeout=PROBE_TIMEOUT))
    except Exception as error:
        print(json.dumps({"event": "error", "type": type(error).__name__, "errors": error_details(error)}), flush=True)
        raise SystemExit(1)
