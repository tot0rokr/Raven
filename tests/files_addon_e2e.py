"""Drive PCManFM from the files addon in disposable xmonad and openbox sessions and check the host side of
~/Downloads. Uses a temporary HOME, so the user's real Downloads folder is never touched."""

import json
import os
from pathlib import Path
import re
import secrets
import shlex
import struct
import subprocess
import sys
import tempfile
import time
import zlib

SOURCE = Path(__file__).resolve().parents[1]
IMAGE = os.environ.get("RAVEN_IMAGE", "raven:latest")
TIMEOUT = 120
FOLDER = "한글 폴더 with space"


def run(arguments, environment, **kwargs):
    return subprocess.run(arguments, env=environment, capture_output=True, check=True, timeout=TIMEOUT, **kwargs)


class Desktop:
    def __init__(self, environment, session):
        self.environment = environment
        self.session = session
        self.container = f"raven-{os.environ['USER']}-{session}"
        labels = json.loads(run(["docker", "inspect", self.container], environment).stdout)[0]["Config"]["Labels"]
        self.display = labels["raven.display"]

    def x(self, *arguments, check=True):
        return subprocess.run(["docker", "exec", "-u", "raven", "-e", f"DISPLAY=:{self.display}",
                               "-e", "XAUTHORITY=/home/raven/.Xauthority", self.container, *arguments],
                              capture_output=True, text=True, check=check, timeout=TIMEOUT)

    def windows(self, pattern):
        result = self.x("xdotool", "search", "--onlyvisible", "--name", pattern, check=False)
        return result.stdout.split()

    def wait_window(self, pattern, gone=False):
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline:
            found = self.windows(pattern)
            if bool(found) != gone:
                return found
            time.sleep(0.3)
        raise AssertionError(f"Window {pattern!r} {'did not close' if gone else 'did not appear'}")

    def keys(self, window, *keys):
        self.x("xdotool", "windowactivate", "--sync", window)
        for key in keys:
            self.x("xdotool", "key", "--clearmodifiers", key)
            time.sleep(0.4)

    def type(self, window, text):
        self.x("xdotool", "windowactivate", "--sync", window)
        self.x("xdotool", "type", "--delay", "40", text)

    def screenshot(self, path):
        data = subprocess.run(["docker", "exec", "-u", "raven", "-e", f"DISPLAY=:{self.display}", self.container,
                               "xwd", "-root", "-silent"], capture_output=True, check=True, timeout=TIMEOUT).stdout
        header = struct.unpack(">25I", data[:100])
        width, height, step, bytes_per_line, colors = header[4], header[5], header[11] // 8, header[12], header[19]
        pixels = data[header[0] + colors * 12:]
        rows = bytearray()
        for y in range(height):
            rows.append(0)
            line = pixels[y * bytes_per_line:y * bytes_per_line + width * step]
            for x in range(width):
                pixel = int.from_bytes(line[x * step:(x + 1) * step], "little")
                rows += bytes(((pixel >> 16) & 255, (pixel >> 8) & 255, pixel & 255))

        def chunk(kind, body):
            return struct.pack(">I", len(body)) + kind + body + struct.pack(">I", zlib.crc32(kind + body))

        path.write_bytes(b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0))
                         + chunk(b"IDAT", zlib.compress(bytes(rows), 6)) + chunk(b"IEND", b""))


def wait_path(path, exists=True):
    deadline = time.monotonic() + 15
    while time.monotonic() < deadline:
        if path.exists() == exists:
            return
        time.sleep(0.2)
    raise AssertionError(f"{path} {'was not created' if exists else 'was not removed'}")


def select(desktop, window, prefix):
    """Select an item by typing its name; Escape closes the type-ahead box but keeps the selection."""
    desktop.type(window, prefix)
    desktop.keys(window, "Escape")


def check(desktop, downloads, scratch, wm):
    menu = run([str(SOURCE / "raven"), "menu", "show", "-t", desktop.session], desktop.environment).stdout.decode()
    assert "Files\tpcmanfm /home/raven\n" in menu and "Downloads\tpcmanfm /home/raven/Downloads\n" in menu, menu
    assert "File Manager PCManFM" not in menu and "Desktop Preferences" not in menu, menu
    assert "Downloads" in desktop.x("cat", "/home/raven/.config/gtk-3.0/bookmarks").stdout

    # raven run hands its arguments to sh -c in the session, like ssh, so quote the path for that shell.
    run([str(SOURCE / "raven"), "run", "-t", desktop.session,
         "pcmanfm " + shlex.quote(f"/home/raven/Downloads/{FOLDER}")], desktop.environment)
    window = desktop.wait_window(FOLDER)[0]
    pid = desktop.x("xdotool", "getwindowpid", window).stdout.strip()
    umask = desktop.x("sh", "-c", f"grep Umask /proc/{pid}/status").stdout.split()[1]
    assert umask == "0002", umask

    folder = downloads / FOLDER
    desktop.keys(window, "ctrl+shift+n")
    dialog = desktop.wait_window("^Creating New Folder$")[0]
    desktop.keys(dialog, "ctrl+a")
    desktop.type(dialog, f"new {wm} dir")
    desktop.keys(dialog, "Return")
    created = folder / f"new {wm} dir"
    wait_path(created)
    info = created.stat()
    assert info.st_uid == 60000 and info.st_gid == os.getgid(), (info.st_uid, info.st_gid)
    assert info.st_mode & 0o2070 == 0o2070, oct(info.st_mode)

    select(desktop, window, f"new {wm}")
    desktop.keys(window, "F2")
    dialog = desktop.wait_window("^Rename File$")[0]
    desktop.keys(dialog, "ctrl+a")
    desktop.type(dialog, f"renamed {wm} dir")
    desktop.keys(dialog, "Return")
    renamed = folder / f"renamed {wm} dir"
    wait_path(renamed)
    wait_path(created, exists=False)

    select(desktop, window, f"renamed {wm}")
    desktop.keys(window, "Delete")
    confirm = desktop.wait_window("^Confirm$")[0]
    desktop.screenshot(scratch / f"{wm}-confirm-delete.png")
    desktop.keys(confirm, "Escape")
    desktop.wait_window("^Confirm$", gone=True)
    assert renamed.exists(), "Cancelling the confirmation still deleted the folder"
    desktop.keys(window, "Delete")
    confirm = desktop.wait_window("^Confirm$")[0]
    desktop.keys(confirm, "Return")
    wait_path(renamed, exists=False)
    assert not any(path.name.startswith(".Trash") for path in downloads.iterdir()), list(downloads.iterdir())

    desktop.x("xdg-open", "/home/raven/Downloads")
    desktop.wait_window("^Downloads$")
    desktop.screenshot(scratch / f"{wm}-files.png")


def main():
    scratch = Path(tempfile.mkdtemp(prefix="raven-files-e2e-"))
    environment = dict(os.environ)
    environment.pop("DISPLAY", None)
    for variable, directory in (("HOME", "home"), ("XDG_CONFIG_HOME", "config"),
                                ("XDG_STATE_HOME", "state"), ("XDG_RUNTIME_DIR", "runtime")):
        (scratch / directory).mkdir(mode=0o700)
        environment[variable] = str(scratch / directory)
    environment["RAVEN_IMAGE"] = IMAGE
    previous_hostd = f"@raven-host-{os.getuid()}" in Path("/proc/net/unix").read_text()
    downloads = scratch / "home" / "Downloads"
    suffix = secrets.token_hex(3)
    sessions = []
    try:
        for wm in ("xmonad", "openbox"):
            result = run([str(SOURCE / "raven"), "new", "-d", "-s", f"files-{wm}-{suffix}", "-w", wm,
                          "-g", "1280x800", "xterm"], environment)
            sessions.append(re.search(rb"raven: created session ([0-9]+)", result.stderr)[1].decode())
            (downloads / FOLDER).mkdir(mode=0o2775, exist_ok=True)
            (downloads / FOLDER).chmod(0o2775)
            (downloads / FOLDER / "보고서 draft.txt").write_text("raven\n")
            check(Desktop(environment, sessions[-1]), downloads, scratch, wm)
            print(f"{wm}: passed", flush=True)
        print(json.dumps({"files_addon": "passed", "screenshots": str(scratch)}))
    finally:
        for session in sessions:
            subprocess.run([str(SOURCE / "raven"), "kill", "-t", session], env=environment, capture_output=True,
                           timeout=TIMEOUT)
        if not previous_hostd:
            subprocess.run([str(SOURCE / "raven"), "hostd", "stop"], env=environment, capture_output=True,
                           timeout=TIMEOUT)


if __name__ == "__main__":
    sys.exit(main())
