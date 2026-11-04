import asyncio
import gc
import json
import os
import threading
import traceback
from pathlib import Path

from asgiref.sync import sync_to_async
from django.conf import settings
from uvicorn.protocols.utils import ClientDisconnected

from raven_web.asgi import application
from raven_web.consumers import MAX_CONNECTIONS, _connections, _connections_lock
from tests.test_security import ORIGIN, SecurityTests

PROBE_ITERATIONS = 8
PROBE_TIMEOUT_SECONDS = 5


def counts():
    descriptors = list(Path("/proc/self/fd").iterdir())
    sockets = 0
    for descriptor in descriptors:
        try:
            sockets += os.readlink(descriptor).startswith("socket:")
        except FileNotFoundError:
            pass
    status = Path("/proc/self/status").read_text().splitlines()
    rss_kib = int(next(line for line in status if line.startswith("VmRSS:")).split()[1])
    with _connections_lock:
        registered = list(_connections)
    return {
        "registered": len(registered),
        "open_broker_writers": sum(connection.writer is not None and not connection.writer.is_closing()
                                   for connection in registered),
        "buffered_vnc_bytes": sum(len(connection.reader._buffer) for connection in registered
                                  if hasattr(connection, "reader")),
        "fds": len(descriptors), "sockets": sockets, "rss_kib": rss_kib,
        "threads": threading.active_count(), "async_tasks": len(asyncio.all_tasks()),
    }


class AcceptDisconnectProbe(SecurityTests):
    async def test_aborted_accept_does_not_retain_connection(self):
        await sync_to_async(self.authenticate)()
        self.assertEqual(len(_connections), 0, "Run this probe in its own isolated test process")
        cookie = self.client.cookies[settings.SESSION_COOKIE_NAME].value
        baseline = counts()
        baseline_threads = set(threading.enumerate())
        print("ACCEPT_DISCONNECT_BASELINE " + json.dumps(baseline), flush=True)
        observations = []
        first_failure = None
        control_result = None
        retained = 0
        try:
            for iteration in range(min(PROBE_ITERATIONS, MAX_CONNECTIONS)):
                scope = {
                    "type": "websocket", "scheme": "wss", "path": "/ws/sessions/0/",
                    "query_string": b"", "subprotocols": ["binary"],
                    "headers": [(b"host", b"localhost:8443"), (b"origin", ORIGIN.encode()),
                                (b"cookie", f"{settings.SESSION_COOKIE_NAME}={cookie}".encode())],
                }
                incoming = asyncio.Queue()
                incoming.put_nowait({"type": "websocket.connect"})

                async def send(message):
                    if message["type"] == "websocket.accept":
                        raise ClientDisconnected("Probe transport closed before accepting the WebSocket")
                    if message["type"] == "websocket.close":
                        incoming.put_nowait({"type": "websocket.disconnect", "code": message["code"]})

                try:
                    await asyncio.wait_for(application(scope, incoming.get, send), PROBE_TIMEOUT_SECONDS)
                except ClientDisconnected:
                    if first_failure is None:
                        first_failure = traceback.format_exc()
                gc.collect()
                observation = {"iteration": iteration + 1, **counts()}
                observations.append(observation)
                print("ACCEPT_DISCONNECT_ITERATION " + json.dumps(observation), flush=True)
            retained = counts()["registered"]
            connection = self.websocket()
            accepted, detail = await connection.connect(timeout=PROBE_TIMEOUT_SECONDS)
            control_result = {"accepted": accepted, "detail": detail}
            await connection.disconnect()
            print("ACCEPT_DISCONNECT_NEXT_CONNECTION " + json.dumps(control_result), flush=True)
            if first_failure is not None:
                print("ACCEPT_DISCONNECT_FIRST_EXCEPTION\n" + first_failure, flush=True)
        finally:
            with _connections_lock:
                leftovers = list(_connections)
            await asyncio.wait_for(
                asyncio.gather(*(connection.cleanup() for connection in leftovers)), PROBE_TIMEOUT_SECONDS,
            )
            for thread in set(threading.enumerate()) - baseline_threads:
                thread.join(timeout=1)
            gc.collect()
            print("ACCEPT_DISCONNECT_AFTER_CLEANUP " + json.dumps(counts()), flush=True)
        self.assertEqual(retained, 0, json.dumps({
            "problem": "Disconnected transports left registered consumers and open broker sockets",
            "baseline": baseline, "after_errors": observations[-1], "next_connection": control_result,
        }))
