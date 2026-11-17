import asyncio
import base64
import binascii
import json
import socket

from Crypto.Cipher import DES
from django.conf import settings

MAX_HEADER = 64 * 1024
MAX_FAILURE_MESSAGE = 200
BROKER_TIMEOUT = 120
CONNECT_TIMEOUT = 10
STREAM_PIECE = 64 * 1024
# File failures the host broker reports with a code; any other failure stays session_failed.
FILE_CODES = frozenset({
    "invalid_path", "not_found", "not_a_directory", "not_a_file", "file_exists", "too_large", "no_space",
    "permission_denied", "upload_unknown", "offset_mismatch", "busy", "downloads_missing", "staging_invalid",
    "cross_device",
})
# Session states the host broker refuses an action in; the gateway answers them with 409.
SESSION_CODES = frozenset({"session_paused", "session_not_paused", "session_not_running"})
CODES = FILE_CODES | SESSION_CODES


class BrokerError(Exception):
    """A broker failure; code is broker_unavailable, session_failed or one of CODES."""

    def __init__(self, message, code="broker_unavailable"):
        super().__init__(message)
        self.code = code


def _failure_message(message):
    # The host broker replies with static English messages; anything else is replaced.
    if (isinstance(message, str) and 0 < len(message) <= MAX_FAILURE_MESSAGE
            and message.isascii() and message.isprintable()):
        return message
    return "The session operation failed"


def _response(line):
    if len(line) > MAX_HEADER or not line.endswith(b"\n"):
        raise BrokerError("Invalid broker response")
    try:
        result = json.loads(line)
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise BrokerError("Invalid broker response") from error
    if not isinstance(result, dict):
        raise BrokerError("Invalid broker response")
    if result.get("ok") is False:
        code = result.get("code")
        raise BrokerError(_failure_message(result.get("error")),
                          code if isinstance(code, str) and code in CODES else "session_failed")
    if result.get("ok") is not True:
        raise BrokerError("Invalid broker response")
    return result.get("data")


def _header(action, arguments):
    header = json.dumps({"action": action, **arguments}).encode() + b"\n"
    if len(header) > MAX_HEADER:
        raise ValueError("The request is too large")
    return header


def request(action, **arguments):
    return send(action, b"", **arguments)


def send(action, payload, **arguments):
    """Send a header line followed by raw payload bytes and read the one-line reply."""
    header = _header(action, arguments)
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as connection:
            connection.settimeout(BROKER_TIMEOUT)
            connection.connect(settings.RAVEN_BROKER)
            connection.sendall(header)
            if payload:
                try:
                    connection.sendall(payload)
                except (BrokenPipeError, ConnectionResetError):
                    pass  # The broker refused the request before reading it all; its reply says why.
            with connection.makefile("rb") as response:
                return _response(response.readline(MAX_HEADER + 1))
    except OSError as error:
        raise BrokerError("The Raven broker is unavailable") from error


async def download(path):
    """Open a download; returns the stream and the size the broker will send after its header."""
    try:
        reader, writer = await asyncio.wait_for(
            asyncio.open_unix_connection(settings.RAVEN_BROKER, limit=MAX_HEADER),
            timeout=CONNECT_TIMEOUT,
        )
    except (OSError, TimeoutError) as error:
        raise BrokerError("The Raven broker is unavailable") from error
    try:
        writer.write(_header("download", {"path": path}))
        await asyncio.wait_for(writer.drain(), timeout=CONNECT_TIMEOUT)
        data = _response(await asyncio.wait_for(reader.readline(), timeout=BROKER_TIMEOUT))
        if not isinstance(data, dict) or type(data.get("size")) is not int or data["size"] < 0:
            raise BrokerError("Invalid broker response")
    except (OSError, TimeoutError, ValueError) as error:
        writer.close()
        raise BrokerError("The Raven broker is unavailable") from error
    except BaseException:
        writer.close()
        raise
    return reader, writer, data["size"]


async def stream(reader, writer, size):
    """Yield exactly size bytes, or stop early so the response ends short of its Content-Length."""
    try:
        while size:
            data = await asyncio.wait_for(reader.read(min(size, STREAM_PIECE)), timeout=BROKER_TIMEOUT)
            if not data:
                return
            size -= len(data)
            yield data
    except (OSError, TimeoutError):
        return
    finally:
        writer.close()


async def connect(session_id):
    try:
        reader, writer = await asyncio.wait_for(
            asyncio.open_unix_connection(settings.RAVEN_BROKER, limit=MAX_HEADER),
            timeout=CONNECT_TIMEOUT,
        )
    except (OSError, TimeoutError) as error:
        raise BrokerError("The Raven broker is unavailable") from error
    try:
        writer.write(json.dumps({"action": "connect", "id": session_id}).encode() + b"\n")
        await asyncio.wait_for(writer.drain(), timeout=CONNECT_TIMEOUT)
        header = await asyncio.wait_for(reader.readline(), timeout=CONNECT_TIMEOUT)
        _response(header)
    except (OSError, TimeoutError, ValueError, BrokerError) as error:
        writer.close()
        try:
            await writer.wait_closed()
        except OSError:
            pass
        # Keep a refusal by session state (a paused session) apart from a failed connection.
        code = error.code if isinstance(error, BrokerError) and error.code in SESSION_CODES else "broker_unavailable"
        raise BrokerError("The session connection failed", code) from error
    return reader, writer


def decode_password(encoded):
    try:
        encrypted = base64.b64decode(encoded, validate=True)
    except (ValueError, TypeError, binascii.Error) as error:
        raise BrokerError("Invalid VNC password file") from error
    if len(encrypted) != 8:
        raise BrokerError("Invalid VNC password file")
    # TigerVNC's d3des reverses each DES key byte's bit order.
    key = bytes(int(f"{value:08b}"[::-1], 2) for value in (23, 82, 107, 6, 35, 78, 88, 7))
    password = DES.new(key, DES.MODE_ECB).decrypt(encrypted).split(b"\0", 1)[0]
    try:
        return password.decode("ascii")
    except UnicodeDecodeError as error:
        raise BrokerError("Invalid VNC password file") from error
