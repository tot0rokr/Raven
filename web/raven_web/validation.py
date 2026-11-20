import math
import re

SESSION_ID = re.compile(r"(?:0|[1-9][0-9]{0,8})\Z")
SESSION_NAME = re.compile(r"[A-Za-z][A-Za-z0-9_.-]{0,63}\Z")
CONTAINER_ID = re.compile(r"[0-9a-f]{64}\Z")
MAX_COMMAND_LENGTH = 4096


def session_generation(value):
    if not isinstance(value, str) or not CONTAINER_ID.fullmatch(value):
        raise ValueError("Session generation is missing or invalid; refresh the session list")
    return value


def session_name(value):
    if not isinstance(value, str) or not SESSION_NAME.fullmatch(value):
        raise ValueError("Name must start with a letter and contain up to 64 letters, digits, _, . or -")
    return value


def session_command(value):
    if not isinstance(value, str) or not value.strip() or len(value) > MAX_COMMAND_LENGTH or "\0" in value:
        raise ValueError("Command must contain 1 to 4096 characters without NUL")
    return value


# Memory and CPU limits; the CLI checks them against the server's memory and cores.
MIN_MEMORY = 512 * 1024 ** 2
MAX_MEMORY = 1024 ** 5
MIN_CPUS = 0.5
MAX_CPUS = 1024
# A share of the server from 1% to 100%, up to two decimals; the CLI resolves it and applies the minimums.
SHARE = re.compile(r"(?:[1-9][0-9]?|100)(?:\.[0-9]{1,2})?%\Z")


class LimitRequestError(ValueError):
    code = "limit_invalid"


def server_share(value):
    return isinstance(value, str) and SHARE.match(value) is not None and 1 <= float(value[:-1]) <= 100


def session_memory(value):
    """Bytes, a whole number of MiB from 512 MiB; a share of the server like "25%"; or "max" for all of it."""
    if value == "max" or server_share(value):
        return value
    if type(value) is not int or not MIN_MEMORY <= value <= MAX_MEMORY or value % 1024 ** 2:
        raise LimitRequestError('Memory must be a whole number of MiB in bytes (at least 512 MiB), a share of the '
                                'server from "1%" to "100%", or "max"')
    return value


def session_cpus(value):
    """Cores from 0.5 with up to two decimals; a share of the server like "25%"; or "max" for every core."""
    if value == "max" or server_share(value):
        return value
    if type(value) not in (int, float) or not math.isfinite(value) or not MIN_CPUS <= value <= MAX_CPUS \
            or abs(value * 100 - round(value * 100)) > 1e-6:
        raise LimitRequestError('CPUs must be a number of cores from 0.5 with up to two decimals, a share of the '
                                'server from "1%" to "100%", or "max"')
    return value


def session_limits(data):
    """The memory and cpus of a request that has them; an empty dict when it has neither."""
    limits = {}
    if "memory" in data:
        limits["memory"] = session_memory(data["memory"])
    if "cpus" in data:
        limits["cpus"] = session_cpus(data["cpus"])
    return limits


def session_options(data):
    name = session_name(data.get("name"))
    wm = data.get("wm", "xmonad")
    if wm not in ("xmonad", "openbox"):
        raise ValueError("Window manager must be xmonad or openbox")
    geometry = data.get("geometry", "1600x900")
    match = re.fullmatch(r"([0-9]{3,4})x([0-9]{3,4})", geometry) if isinstance(geometry, str) else None
    if not match or not (320 <= int(match[1]) <= 7680 and 200 <= int(match[2]) <= 4320):
        raise ValueError("Geometry must be between 320x200 and 7680x4320")
    return {"name": name, "wm": wm, "geometry": geometry,
            "command": session_command(data.get("command", "xterm")), **session_limits(data)}


# File transfers stay inside the host's ~/Downloads; the host broker repeats every check.
CHUNK_SIZE = 4 * 1024 * 1024
MAX_FILE_SIZE = 4 * 1024 ** 3
MAX_PATH_LENGTH = 4096
MAX_PATH_DEPTH = 32
MAX_NAME_BYTES = 255
STAGING = ".raven-uploads"
UPLOAD_TOKEN = re.compile(r"[A-Za-z0-9_-]{32}\Z")
UPLOAD_OFFSET = re.compile(r"(?:0|[1-9][0-9]{0,9})\Z")


class FileRequestError(ValueError):
    def __init__(self, message, code="invalid_request"):
        super().__init__(message)
        self.code = code


def file_component(value):
    if not isinstance(value, str) or value in (".", "..") or "/" in value or "\0" in value:
        return False
    try:
        return 0 < len(value.encode()) <= MAX_NAME_BYTES
    except UnicodeEncodeError:
        return False


def file_path(value):
    """A path relative to Downloads; "" is Downloads itself."""
    if not isinstance(value, str) or len(value) > MAX_PATH_LENGTH:
        raise FileRequestError("Invalid path", "invalid_path")
    parts = value.split("/") if value else []
    if len(parts) > MAX_PATH_DEPTH or not all(map(file_component, parts)) or parts[:1] == [STAGING]:
        raise FileRequestError("Invalid path", "invalid_path")
    return value


def file_name(value, directory):
    if not file_component(value) or (directory == "" and value == STAGING):
        raise FileRequestError("Invalid file name", "invalid_path")
    return value


def upload_size(value):
    if type(value) is not int or value < 0:
        raise FileRequestError("File size must be a non-negative integer")
    if value > MAX_FILE_SIZE:
        raise FileRequestError("Files can be at most 4 GiB", "too_large")
    return value


def upload_offset(value):
    if not isinstance(value, str) or not UPLOAD_OFFSET.fullmatch(value) or int(value) > MAX_FILE_SIZE:
        raise FileRequestError("Invalid upload offset")
    return int(value)


def upload_token(value):
    if not isinstance(value, str) or not UPLOAD_TOKEN.fullmatch(value):
        raise FileRequestError("Invalid upload token")
    return value


def overwrite_flag(value):
    if not isinstance(value, bool):
        raise FileRequestError("overwrite must be true or false")
    return value
