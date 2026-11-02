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
            "command": session_command(data.get("command", "xterm"))}
