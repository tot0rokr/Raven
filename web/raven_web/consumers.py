import asyncio
from collections import Counter
import threading

from channels.db import database_sync_to_async
from channels.generic.websocket import AsyncWebsocketConsumer
from django.contrib.auth import HASH_SESSION_KEY, SESSION_KEY, get_user_model
from django.contrib.sessions.models import Session
from django.db import DatabaseError
from django.utils import timezone
from django.utils.crypto import constant_time_compare

from . import broker
from .logins import record_activity
from .validation import SESSION_ID

MAX_CONNECTIONS = 8
MAX_FRAME_SIZE = 1024 * 1024
STREAM_CHUNK_SIZE = 64 * 1024
SESSION_CHECK_SECONDS = 2
WRITE_TIMEOUT_SECONDS = 10
# The session is paused: the browser stops reconnecting until the user resumes it.
PAUSED_CLOSE_CODE = 4409
_connections = set()
_connections_lock = threading.Lock()


@database_sync_to_async
def session_valid(session_key, user_id):
    try:
        session = Session.objects.filter(session_key=session_key, expire_date__gt=timezone.now()).first()
        user = get_user_model().objects.filter(pk=user_id, is_active=True).first()
    except DatabaseError:
        return False
    if session is None or user is None:
        return False
    data = session.get_decoded()
    return str(data.get(SESSION_KEY)) == str(user_id) and constant_time_compare(
        data.get(HASH_SESSION_KEY, ""), user.get_session_auth_hash()
    )


def revoke_session(session_key):
    with _connections_lock:
        connections = [connection for connection in _connections if connection.session_key == session_key]
    for connection in connections:
        connection.loop.call_soon_threadsafe(connection.schedule_revocation)


def connection_counts():
    """Open desktop connections in this process, by login session key."""
    with _connections_lock:
        return Counter(connection.session_key for connection in _connections)


class DesktopConsumer(AsyncWebsocketConsumer):
    async def connect(self):
        self.tasks = []
        self.writer = None
        self.closing = False
        self.loop = asyncio.get_running_loop()
        self.session_key = self.scope["session"].session_key
        user = self.scope["user"]
        session_id = self.scope["url_route"]["kwargs"]["session_id"]
        if (
            not user.is_authenticated
            or not SESSION_ID.fullmatch(session_id)
            or not await session_valid(self.session_key, user.pk)
        ):
            await self.close(code=4401)
            return
        try:
            await database_sync_to_async(record_activity)(self.session_key)
        except DatabaseError:
            pass  # Last activity is bookkeeping: a busy database must not refuse the desktop.
        with _connections_lock:
            limited = len(_connections) >= MAX_CONNECTIONS
            if not limited:
                _connections.add(self)
        if limited:
            await self.close(code=4429)
            return
        paused = False
        try:
            self.reader, self.writer = await broker.connect(session_id)
        except broker.BrokerError as error:
            if error.code != "session_paused":
                await self.end(4502)
                return
            paused = True
        if self.closing or not await session_valid(self.session_key, user.pk):
            await self.cleanup()
            await self.close(code=4401)
            return
        try:
            await self.accept(subprotocol="binary" if "binary" in self.scope.get("subprotocols", []) else None)
        except BaseException:
            self.closing = True
            await self.cleanup()
            raise
        if paused:
            # A close before the handshake reaches the browser as 1006; after it, as this code.
            await self.end(PAUSED_CLOSE_CODE)
            return
        self.tasks = [asyncio.create_task(self.forward_screen()), asyncio.create_task(self.watch_session(user.pk))]

    def schedule_revocation(self):
        asyncio.create_task(self.end(4401))

    async def receive(self, text_data=None, bytes_data=None):
        if self.closing:
            return
        if bytes_data is None:
            await self.end(1003)
            return
        if len(bytes_data) > MAX_FRAME_SIZE:
            await self.end(1009)
            return
        try:
            self.writer.write(bytes_data)
            await asyncio.wait_for(self.writer.drain(), timeout=WRITE_TIMEOUT_SECONDS)
        except (OSError, TimeoutError):
            await self.end(1011)

    async def forward_screen(self):
        try:
            while data := await self.reader.read(STREAM_CHUNK_SIZE):
                await self.send(bytes_data=data)
            await self.end(1000)
        except OSError:
            await self.end(1011)

    async def watch_session(self, user_id):
        try:
            while True:
                await asyncio.sleep(SESSION_CHECK_SECONDS)
                if not await session_valid(self.session_key, user_id):
                    await self.end(4401)
                    return
        except DatabaseError:
            await self.end(1011)

    async def end(self, code):
        if self.closing:
            return
        self.closing = True
        await self.cleanup()
        await self.close(code=code)

    async def cleanup(self):
        with _connections_lock:
            _connections.discard(self)
        current = asyncio.current_task()
        cancelled = [task for task in self.tasks if task is not current]
        for task in cancelled:
            task.cancel()
        if self.writer is not None:
            self.writer.close()
            try:
                await self.writer.wait_closed()
            except OSError:
                pass
        await asyncio.gather(*cancelled, return_exceptions=True)

    async def disconnect(self, code):
        self.closing = True
        await self.cleanup()
