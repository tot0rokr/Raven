import asyncio
import base64
import binascii
import json
import socket

from Crypto.Cipher import DES
from django.conf import settings

MAX_HEADER = 64 * 1024
BROKER_TIMEOUT = 120
CONNECT_TIMEOUT = 10


class BrokerError(Exception):
    pass


def _response(line):
    if len(line) > MAX_HEADER or not line.endswith(b"\n"):
        raise BrokerError("Invalid broker response")
    try:
        result = json.loads(line)
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise BrokerError("Invalid broker response") from error
    if not isinstance(result, dict) or result.get("ok") is not True:
        raise BrokerError("The session operation failed")
    return result.get("data")


def request(action, **arguments):
    payload = json.dumps({"action": action, **arguments}).encode() + b"\n"
    if len(payload) > MAX_HEADER:
        raise BrokerError("The request is too large")
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
