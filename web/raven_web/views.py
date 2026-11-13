import json
from functools import wraps

from django.conf import settings
from django.contrib.auth import authenticate, login, logout
from django.contrib.auth.decorators import login_required
from django.core.exceptions import RequestDataTooBig
from django.http import JsonResponse
from django.middleware import csrf
from django.shortcuts import redirect, render
from django.views.decorators.csrf import ensure_csrf_cookie
from django.views.decorators.debug import sensitive_post_parameters
from django.views.decorators.http import require_http_methods

from . import broker
from .consumers import revoke_session
from .throttle import login_retry_after
from .validation import SESSION_ID, session_command, session_generation, session_name, session_options

# Django formats the rejected Origin or Referer into these reasons, so match only the fixed prefix.
ORIGIN_FAILURES = tuple(reason.split("%s", 1)[0] for reason in (
    csrf.REASON_BAD_ORIGIN, csrf.REASON_NO_REFERER, csrf.REASON_BAD_REFERER,
    csrf.REASON_MALFORMED_REFERER, csrf.REASON_INSECURE_REFERER,
))


def error_response(code, message, status, **kwargs):
    return JsonResponse({"error": message, "code": code}, status=status, **kwargs)


def csrf_failure_code(reason):
    if reason == csrf.REASON_NO_CSRF_COOKIE:
        return "csrf_cookie_missing"
    if reason.startswith(ORIGIN_FAILURES):
        return "csrf_origin_mismatch"
    return "csrf_token_invalid"


def csrf_failure(request, reason=""):
    # Never echo the reason: it contains the request's Origin or Referer.
    code = csrf_failure_code(reason)
    api = request.path.startswith("/api/")
    if code == "csrf_cookie_missing":
        message = f"The security cookie is missing. Allow cookies for this site and open {settings.ORIGIN} exactly."
    elif code == "csrf_origin_mismatch":
        sender = "request" if api else "form"
        message = f"The {sender} came from a different address. Open {settings.ORIGIN} and try again."
    elif api:
        message = "The page expired. Reload it and try again."
    else:
        message = "The form expired. Please try again."
    if api:
        return error_response(code, message, 403)
    return render(request, "login.html", {"error": message}, status=403)


@sensitive_post_parameters("password")
@require_http_methods(["GET", "POST"])
@ensure_csrf_cookie
def login_view(request):
    if request.user.is_authenticated:
        return redirect("/")
    if request.method == "GET":
        return render(request, "login.html")
    retry_after = login_retry_after()
    if retry_after:
        response = render(request, "login.html", {
            "error": f"Too many login attempts. Try again in {retry_after} seconds.",
        }, status=429)
        response["Retry-After"] = str(retry_after)
        return response
    username = request.POST.get("username", "")
    password = request.POST.get("password", "")
    if len(username) > 150 or len(password) > 1024:
        user = None
    else:
        user = authenticate(request, username=username, password=password)
    if user is None:
        return render(request, "login.html", {"error": "Incorrect username or password."}, status=401)
    login(request, user)
    return redirect("/")


@require_http_methods(["POST"])
def logout_view(request):
    key = request.session.session_key
    logout(request)
    if key:
        revoke_session(key)
    return redirect("/login/")


@login_required
@require_http_methods(["GET"])
@ensure_csrf_cookie
def index(request):
    return render(request, "index.html", {"username": request.user.username})


def api_view(methods):
    def decorator(view):
        @wraps(view)
        def wrapped(request, *args, **kwargs):
            if not request.user.is_authenticated:
                return error_response("auth_required", "Authentication required", 401)
            if request.method not in methods:
                return error_response("method_not_allowed", "Method not allowed", 405,
                                      headers={"Allow": ", ".join(methods)})
            session_id = kwargs.get("session_id")
            if session_id is not None and not SESSION_ID.fullmatch(session_id):
                return error_response("invalid_request", "Invalid session ID", 400)
            try:
                return view(request, *args, **kwargs)
            except (ValueError, UnicodeDecodeError, RequestDataTooBig) as error:
                return error_response("invalid_request", str(error), 400)
            except broker.BrokerError as error:
                return error_response(error.code, str(error), 502)
        return wrapped
    return decorator


def json_object(request, allowed):
    if request.content_type != "application/json":
        raise ValueError("Content-Type must be application/json")
    try:
        data = json.loads(request.body)
    except (ValueError, UnicodeDecodeError) as error:
        raise ValueError("Invalid JSON body") from error
    if not isinstance(data, dict) or data.keys() - allowed:
        raise ValueError("Invalid request fields")
    return data


@api_view(["GET", "POST"])
def sessions(request):
    if request.method == "GET":
        return JsonResponse({"sessions": broker.request("list")})
    options = session_options(json_object(request, {"name", "wm", "geometry", "command"}))
    return JsonResponse({"session": broker.request("create", **options)}, status=201)


@api_view(["POST"])
def open_session(request, session_id):
    result = broker.request("open", id=session_id)
    return JsonResponse({
        "name": result["name"],
        "password": broker.decode_password(result["password_file"]),
        "websocket": f"/ws/sessions/{session_id}/",
    })


@api_view(["POST"])
def rename_session(request, session_id):
    data = json_object(request, {"name", "generation"})
    name = session_name(data.get("name"))
    generation = session_generation(data.get("generation"))
    return JsonResponse({"session": broker.request("rename", id=session_id, name=name, generation=generation)})


@api_view(["POST"])
def run_session(request, session_id):
    data = json_object(request, {"command", "generation"})
    command = session_command(data.get("command"))
    generation = session_generation(data.get("generation"))
    broker.request("run", id=session_id, command=command, generation=generation)
    return JsonResponse({"started": True}, status=202)


@api_view(["DELETE"])
def delete_session(request, session_id):
    data = json_object(request, {"confirm", "generation"})
    if data.get("confirm") != session_id:
        raise ValueError("Confirm the exact session ID before deleting it")
    generation = session_generation(data.get("generation"))
    broker.request("delete", id=session_id, confirm=session_id, generation=generation)
    return JsonResponse({"deleted": session_id})
