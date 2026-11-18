from django.conf import settings
from django.core.exceptions import DisallowedHost
from django.db import DatabaseError
from django.http import HttpResponse, JsonResponse
from django.template.loader import render_to_string

from .logins import activity_due, record_activity


def exact_host(request):
    try:
        return request.get_host() == settings.ORIGIN_PARTS.netloc
    except DisallowedHost:
        return False


class ExactHostMiddleware:
    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        if exact_host(request):
            return self.get_response(request)
        if request.path.startswith(("/api/", "/ws/")):
            return JsonResponse({"error": "Invalid host", "code": "invalid_host"}, status=400)
        # A browser opened another address; name the configured one without echoing the request's Host.
        return HttpResponse(render_to_string("wrong_host.html", {"origin": settings.ORIGIN}), status=400)


class LoginActivityMiddleware:
    """Record a logged-in request as its login's last activity, at most once per ACTIVITY_INTERVAL."""

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        response = self.get_response(request)
        # After the view: a logout or revocation in this request leaves nothing to record.
        if request.user.is_authenticated and activity_due(request.session):
            try:
                record_activity(request.session.session_key)
            except DatabaseError:
                pass  # Last activity is bookkeeping: a busy database must not fail the request.
        return response


class SecurityHeadersMiddleware:
    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        response = self.get_response(request)
        response["Content-Security-Policy"] = (
            "default-src 'none'; script-src 'self'; style-src 'self' 'unsafe-inline'; "
            f"connect-src 'self' wss://{settings.ORIGIN_PARTS.netloc}; "
            "img-src 'self' data: blob:; font-src 'self'; worker-src 'self' blob:; "
            "base-uri 'none'; frame-ancestors 'none'; form-action 'self'; object-src 'none'"
        )
        response["X-Frame-Options"] = "DENY"
        response["Permissions-Policy"] = "camera=(), microphone=(), geolocation=()"
        response["Cross-Origin-Opener-Policy"] = "same-origin"
        response["Cross-Origin-Resource-Policy"] = "same-origin"
        if not request.path.startswith(settings.STATIC_URL):
            response["Cache-Control"] = "no-store"
        return response
