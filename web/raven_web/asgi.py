import json
import os
import re

os.umask(0o077)
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "raven_web.settings")

from django.core.asgi import get_asgi_application

http_application = get_asgi_application()

from channels.auth import AuthMiddlewareStack
from channels.db import database_sync_to_async
from channels.routing import ProtocolTypeRouter, URLRouter
from django.conf import settings
from django.contrib.auth import get_user_model
from django.db import DatabaseError
from django.urls import re_path

from .consumers import DesktopConsumer

MAX_BODY = 64 * 1024
UPLOAD_CHUNK_PATH = re.compile(r"/api/files/uploads/[A-Za-z0-9_-]{32}/")


class WebSocketBoundary:
    def __init__(self, application):
        self.application = application

    async def __call__(self, scope, receive, send):
        origins = [value for name, value in scope.get("headers", []) if name == b"origin"]
        hosts = [value for name, value in scope.get("headers", []) if name == b"host"]
        if (
            scope.get("scheme") != "wss"
            or origins != [settings.ORIGIN.encode("ascii")]
            or hosts != [settings.ORIGIN_PARTS.netloc.encode("ascii")]
            or scope.get("query_string")
            or not re.fullmatch(r"/ws/sessions/(?:0|[1-9][0-9]{0,8})/", scope["path"])
        ):
            await send({"type": "websocket.close", "code": 4403})
            return
        await self.application(scope, receive, send)


class RequestBodyLimit:
    """Refuse large or unsized bodies before Django reads them: it spools the whole body before any view runs.

    Only an upload chunk may carry up to FILE_UPLOAD_MAX_MEMORY_SIZE; every other body is a small form or JSON.
    """

    def __init__(self, application):
        self.application = application

    async def __call__(self, scope, receive, send):
        headers = scope.get("headers", [])
        lengths = [value for name, value in headers if name == b"content-length"]
        if any(name == b"transfer-encoding" for name, _ in headers):
            await self.refuse(send, 411, "invalid_request", "Content-Length is required")
        elif len(lengths) > 1 or (lengths and not re.fullmatch(rb"[0-9]{1,10}", lengths[0])):
            await self.refuse(send, 400, "invalid_request", "Invalid Content-Length")
        elif lengths and int(lengths[0]) > self.limit(scope):
            await self.refuse(send, 413, "too_large", "The request body is too large")
        else:
            await self.application(scope, receive, send)

    @staticmethod
    def limit(scope):
        if scope.get("method") == "PUT" and UPLOAD_CHUNK_PATH.fullmatch(scope.get("path", "")):
            return settings.FILE_UPLOAD_MAX_MEMORY_SIZE
        return MAX_BODY

    @staticmethod
    async def refuse(send, status, code, message):
        await send({"type": "http.response.start", "status": status, "headers": [
            (b"content-type", b"application/json"), (b"cache-control", b"no-store"),
            (b"x-content-type-options", b"nosniff"), (b"connection", b"close"),
        ]})
        await send({"type": "http.response.body", "body": json.dumps({"error": message, "code": code}).encode()})


@database_sync_to_async
def account_initialized():
    users = get_user_model().objects.all()
    return users.count() == 1 and users.filter(is_active=True).exists()


async def lifespan(scope, receive, send):
    await receive()
    try:
        initialized = await account_initialized()
    except DatabaseError:
        initialized = False
    if not initialized:
        await send({"type": "lifespan.startup.failed", "message": "Run raven web init to initialize the single account."})
        return
    await send({"type": "lifespan.startup.complete"})
    await receive()
    await send({"type": "lifespan.shutdown.complete"})


application = ProtocolTypeRouter({
    "http": RequestBodyLimit(http_application),
    "websocket": WebSocketBoundary(AuthMiddlewareStack(URLRouter([
        re_path(r"^ws/sessions/(?P<session_id>[^/]+)/$", DesktopConsumer.as_asgi()),
    ]))),
    "lifespan": lifespan,
})
