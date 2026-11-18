import json
import re
from functools import wraps
from urllib.parse import quote

from django.conf import settings
from django.contrib.auth import authenticate, login, logout
from django.contrib.auth.decorators import login_required
from django.contrib.sessions.backends.db import SessionStore
from django.core.exceptions import RequestDataTooBig
from django.http import HttpResponse, JsonResponse, StreamingHttpResponse
from django.middleware import csrf
from django.shortcuts import redirect, render
from django.views.decorators.csrf import ensure_csrf_cookie
from django.views.decorators.debug import sensitive_post_parameters
from django.views.decorators.http import require_http_methods

from . import broker, logins
from .consumers import connection_counts, revoke_session
from .service_config import certificate_info, public_certificate
from .throttle import login_retry_after
from .validation import (
    CHUNK_SIZE, SESSION_ID, UPLOAD_TOKEN, FileRequestError, file_name, file_path, overwrite_flag, session_command,
    session_generation, session_name, session_options, upload_offset, upload_size,
)

# Django formats the rejected Origin or Referer into these reasons, so match only the fixed prefix.
ORIGIN_FAILURES = tuple(reason.split("%s", 1)[0] for reason in (
    csrf.REASON_BAD_ORIGIN, csrf.REASON_NO_REFERER, csrf.REASON_BAD_REFERER,
    csrf.REASON_MALFORMED_REFERER, csrf.REASON_INSECURE_REFERER,
))

# HTTP status for each file failure code; other broker failures stay 502.
# 409 marks server-side state the user has to fix: a missing Downloads or a bad staging folder.
FILE_ERROR_STATUS = {
    "invalid_path": 400, "not_found": 404, "upload_unknown": 404, "not_a_directory": 400, "not_a_file": 400,
    "file_exists": 409, "offset_mismatch": 409, "too_large": 413, "no_space": 507, "permission_denied": 403,
    "busy": 429, "downloads_missing": 409, "staging_invalid": 409, "cross_device": 409,
}
# A session in the wrong state for the action (paused, not paused, not running) is a 409 as well.
BROKER_ERROR_STATUS = {
    **FILE_ERROR_STATUS, "session_paused": 409, "session_not_paused": 409, "session_not_running": 409,
}
CONTENT_LENGTH = re.compile(r"[0-9]{1,10}\Z")


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
    logins.start(request)
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
            token = kwargs.get("token")
            if token is not None and not UPLOAD_TOKEN.fullmatch(token):
                return error_response("invalid_request", "Invalid upload token", 400)
            try:
                return view(request, *args, **kwargs)
            except FileRequestError as error:
                return error_response(error.code, str(error), FILE_ERROR_STATUS.get(error.code, 400))
            except (ValueError, UnicodeDecodeError, RequestDataTooBig) as error:
                return error_response("invalid_request", str(error), 400)
            except broker.BrokerError as error:
                return error_response(error.code, str(error), BROKER_ERROR_STATUS.get(error.code, 502))
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


@api_view(["POST"])
def pause_session(request, session_id):
    generation = session_generation(json_object(request, {"generation"}).get("generation"))
    return JsonResponse({"session": broker.request("pause", id=session_id, generation=generation)})


@api_view(["POST"])
def resume_session(request, session_id):
    generation = session_generation(json_object(request, {"generation"}).get("generation"))
    return JsonResponse({"session": broker.request("resume", id=session_id, generation=generation)})


@api_view(["DELETE"])
def delete_session(request, session_id):
    data = json_object(request, {"confirm", "generation"})
    if data.get("confirm") != session_id:
        raise ValueError("Confirm the exact session ID before deleting it")
    generation = session_generation(data.get("generation"))
    broker.request("delete", id=session_id, confirm=session_id, generation=generation)
    return JsonResponse({"deleted": session_id})


def broker_object(result, keys):
    if not isinstance(result, dict):
        raise broker.BrokerError("Invalid broker response")
    return {key: result.get(key) for key in keys}


@api_view(["GET"])
def files(request):
    path = file_path(request.GET.get("path", ""))
    return JsonResponse(broker_object(broker.request("files_list", path=path),
                                      ("path", "entries", "truncated", "skipped")))


@api_view(["POST"])
def start_upload(request):
    data = json_object(request, {"path", "name", "size", "overwrite"})
    path = file_path(data.get("path", ""))
    name = file_name(data.get("name"), path)
    size = upload_size(data.get("size"))
    overwrite = overwrite_flag(data.get("overwrite", False))
    token = broker_object(broker.request("upload_start", path=path, name=name, size=size, overwrite=overwrite),
                          ("token",))["token"]
    if not isinstance(token, str) or not UPLOAD_TOKEN.fullmatch(token):
        raise broker.BrokerError("Invalid broker response")
    return JsonResponse({"token": token, "chunk_size": CHUNK_SIZE}, status=201)


@api_view(["PUT", "DELETE"])
def upload(request, token):
    if request.method == "DELETE":
        broker.request("upload_cancel", token=token)
        return JsonResponse({"cancelled": True})
    # Django has already read the body (the ASGI guard caps it); read() avoids request.body's 16 KiB form limit.
    length = request.META.get("CONTENT_LENGTH", "")
    if not CONTENT_LENGTH.fullmatch(length):
        return error_response("invalid_request", "Content-Length is required", 411)
    if int(length) > CHUNK_SIZE:
        return error_response("too_large", "An upload chunk can hold at most 4 MiB", 413)
    if request.content_type != "application/octet-stream":
        raise ValueError("Content-Type must be application/octet-stream")
    offset = upload_offset(request.GET.get("offset"))
    data = request.read()
    if len(data) != int(length):
        raise ValueError("The body does not match its Content-Length")
    received = broker_object(broker.send("upload_chunk", data, token=token, offset=offset, size=len(data)),
                             ("received",))["received"]
    if type(received) is not int:
        raise broker.BrokerError("Invalid broker response")
    return JsonResponse({"received": received})


@api_view(["POST"])
def finish_upload(request, token):
    data = json_object(request, {"overwrite"})
    overwrite = overwrite_flag(data.get("overwrite", False))
    result = broker.request("upload_finish", token=token, overwrite=overwrite)
    return JsonResponse({"file": broker_object(result, ("name", "size", "mtime"))})


def attachment(name):
    fallback = "".join(c if " " <= c <= "~" and c not in '"\\' else "_" for c in name)
    return f"attachment; filename=\"{fallback}\"; filename*=UTF-8''{quote(name, safe='')}"


async def download(request):
    # Async on purpose: under ASGI, Django 5.2 collects a sync iterator into memory before sending it.
    user = await request.auser()
    if not user.is_authenticated:
        return error_response("auth_required", "Authentication required", 401)
    if request.method != "GET":
        return error_response("method_not_allowed", "Method not allowed", 405, headers={"Allow": "GET"})
    try:
        path = file_path(request.GET.get("path", ""))
        if not path:
            raise FileRequestError("Choose a file to download", "not_a_file")
        reader, writer, size = await broker.download(path)
    except FileRequestError as error:
        return error_response(error.code, str(error), FILE_ERROR_STATUS.get(error.code, 400))
    except broker.BrokerError as error:
        return error_response(error.code, str(error), FILE_ERROR_STATUS.get(error.code, 502))
    response = StreamingHttpResponse(broker.stream(reader, writer, size), content_type="application/octet-stream")
    response["Content-Length"] = str(size)
    response["Content-Disposition"] = attachment(path.rsplit("/", 1)[-1])
    response["X-Content-Type-Options"] = "nosniff"
    return response


def served_certificate():
    """The chain in /data/tls.crt, the file uvicorn loaded at startup; raises OSError or ValueError."""
    return (settings.STATE_DIR / "tls.crt").read_bytes().decode("ascii")


def certificate_unavailable():
    return error_response("certificate_unavailable", "The HTTPS certificate cannot be read; run raven web doctor "
                          "on the server", 500)


@api_view(["GET"])
def certificate(request):
    try:
        info = certificate_info(served_certificate(), settings.ORIGIN_PARTS.hostname)
    except (OSError, ValueError):
        return certificate_unavailable()
    return JsonResponse(info)


@api_view(["GET"])
def certificate_download(request):
    # Only re-encoded certificates leave: a private key appended to tls.crt by hand is never served.
    try:
        chain = public_certificate(served_certificate())
    except (OSError, ValueError):
        return certificate_unavailable()
    hostname = re.sub(r"[^A-Za-z0-9.-]", "_", settings.ORIGIN_PARTS.hostname)
    response = HttpResponse(chain.encode("ascii"), content_type="application/x-x509-ca-cert")
    response["Content-Disposition"] = f'attachment; filename="raven-{hostname}.crt"'
    return response


@api_view(["GET"])
def login_list(request):
    # Session keys never leave this view; entries carry only the login ID.
    SessionStore.clear_expired()
    current = request.session.session_key
    counts = connection_counts()
    return JsonResponse({"logins": [
        {**entry, "current": key == current, "connections": counts[key]}
        for key, entry in logins.account_logins(request.user)
    ]})


def revoked(request, session_keys):
    """End these logins and their open desktop connections; log this response out when one is the current login."""
    current = request.session.session_key in session_keys
    count = logins.revoke(session_keys)
    for key in session_keys:
        revoke_session(key)
    if current:
        logout(request)
    return JsonResponse({"revoked": count, "current": current})


@api_view(["POST"])
def revoke_login(request, login_id):
    keys = []
    if logins.LOGIN_ID.fullmatch(login_id):
        keys = [key for key, entry in logins.account_logins(request.user) if entry["id"] == login_id]
    if not keys:
        return error_response("login_not_found", "That login has already ended; refresh the list", 404)
    return revoked(request, keys)


@api_view(["POST"])
def revoke_logins(request):
    scope = json_object(request, {"scope"}).get("scope")
    if scope not in ("others", "all"):
        raise ValueError('scope must be "others" or "all"')
    current = request.session.session_key
    return revoked(request, [key for key, _ in logins.account_logins(request.user)
                             if scope == "all" or key != current])
