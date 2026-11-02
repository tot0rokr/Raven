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
    "http": http_application,
    "websocket": WebSocketBoundary(AuthMiddlewareStack(URLRouter([
        re_path(r"^ws/sessions/(?P<session_id>[^/]+)/$", DesktopConsumer.as_asgi()),
    ]))),
    "lifespan": lifespan,
})
