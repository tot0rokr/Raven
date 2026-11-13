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


class BrokerError(Exception):
    """A broker failure; code is broker_unavailable or session_failed."""

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
        raise BrokerError(_failure_message(result.get("error")), "session_failed")
    if result.get("ok") is not True:
        raise BrokerError("Invalid broker response")
    return result.get("data")


def request(action, **arguments):
    payload = json.dumps({"action": action, **arguments}).encode() + b"\n"
    if len(payload) > MAX_HEADER:
        raise ValueError("The request is too large")
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as connection:
            connection.settimeout(BROKER_TIMEOUT)
            connection.connect(settings.RAVEN_BROKER)
            connection.sendall(payload)
            with connection.makefile("rb") as response:
                return _response(response.readline(MAX_HEADER + 1))
    except OSError as error:
        raise BrokerError("The Raven broker is unavailable") from error


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
        raise BrokerError("The session connection failed") from error
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
