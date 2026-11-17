"""Owner-only bridge between the web container and the Raven CLI."""

import base64
import errno
import json
import os
from pathlib import Path
import pwd
import re
import secrets
import selectors
import signal
import socket
import socketserver
import stat
import struct
import subprocess
import sys
import threading
import time

MAX_REQUEST = 65536
COMMAND_TIMEOUT = 120
SOCKET_TIMEOUT = 10
MAX_CLIENTS = 24
SESSION_ID = re.compile(r"0|[1-9][0-9]{0,8}")
SESSION_NAME = re.compile(r"[A-Za-z][A-Za-z0-9_.-]{0,63}")
MAX_COMMAND_LENGTH = 4096
UCRED = struct.Struct("3i")

CHUNK_SIZE = 4 * 1024 * 1024
MAX_FILE_SIZE = 4 * 1024 ** 3
FREE_SPACE_MARGIN = 256 * 1024 * 1024
MAX_UPLOADS = 8
# Each download holds a broker connection for its whole length; keep most of MAX_CLIENTS for viewers.
MAX_DOWNLOADS = 4
UPLOAD_IDLE_SECONDS = 900
STALE_UPLOAD_SECONDS = 3600
MAX_ENTRIES = 2000
# Entries read from one folder, skipped ones included, so a folder of millions of names cannot stall a listing.
MAX_SCANNED = 5 * MAX_ENTRIES
MAX_PATH_LENGTH = 4096
MAX_PATH_DEPTH = 32
MAX_NAME_BYTES = 255
STAGING = ".raven-uploads"
UPLOAD_MODE = 0o660
DOWNLOAD_TIMEOUT = 120
PIECE = 64 * 1024
UPLOAD_TOKEN = re.compile(r"[A-Za-z0-9_-]{32}")
TEMPORARY_NAME = re.compile(r"[A-Za-z0-9_-]{32}\.part")
# Sessions can plant any symlink in ~/Downloads and this process has the host user's rights,
# so every component is opened relative to its parent without following symlinks.
DIRECTORY_FLAGS = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
FILE_ACTIONS = ("files_list", "upload_start", "upload_finish", "upload_cancel")
FILE_FAILURES = {
    "invalid_path": "Invalid path",
    "not_found": "No such file or folder",
    "not_a_directory": "Not a folder",
    "not_a_file": "Not a regular file",
    "file_exists": "A file with this name already exists",
    "too_large": "The file is too large",
    "no_space": "Not enough free space in Downloads",
    "permission_denied": "Permission denied",
    "upload_unknown": "The upload is unknown or expired",
    "offset_mismatch": "The upload is out of order; start it again",
    "busy": "Too many uploads in progress",
    "downloads_missing": "~/Downloads is missing or is not a folder",
    "staging_invalid": "~/Downloads/.raven-uploads is not this broker's private folder; remove it and upload again",
    "cross_device": "The upload staging folder and the target folder are on different file systems",
}
# Session states that refuse an action; the gateway answers these with 409.
SESSION_FAILURES = {
    "session_paused": "Session is paused; resume it first",
    "session_not_paused": "Session is not paused",
    "session_not_running": "Session is not running",
}
# Every session's CPU use is sampled once per listing, this far apart.
CPU_SAMPLE_SECONDS = 0.25
VIEWER_PROCESSES = frozenset({"xtigervncviewer", "xtightvncviewer"})
ERRNO_FAILURES = {
    errno.ENOENT: "not_found", errno.ENOTDIR: "not_a_directory", errno.ELOOP: "not_a_file",
    errno.EISDIR: "not_a_file", errno.ENXIO: "not_a_file", errno.EEXIST: "file_exists",
    errno.EACCES: "permission_denied", errno.EPERM: "permission_denied", errno.EROFS: "permission_denied",
    errno.ENOSPC: "no_space", errno.EDQUOT: "no_space", errno.ENAMETOOLONG: "invalid_path",
}

# Run inside the session PID namespace before forwarding any RFB bytes.
VERIFY_VNC_PEER = """
import os
from pathlib import Path
import sys
import time

local_port, remote_port = (int(value) for value in sys.argv[1:])
deadline = time.monotonic() + 2
while time.monotonic() < deadline:
    inodes = set()
    for row in Path('/proc/net/tcp').read_text().splitlines()[1:]:
        fields = row.split()
        if (fields[1] == f'0100007F:{local_port:04X}'
                and fields[2] == f'0100007F:{remote_port:04X}' and fields[3] == '01'):
            inodes.add('socket:[' + fields[9] + ']')
    for process in Path('/proc').iterdir():
        if not process.name.isdigit():
            continue
        try:
            if (process / 'comm').read_text().strip() not in {'Xvnc', 'Xtigervnc'}:
                continue
            for descriptor in (process / 'fd').iterdir():
                if os.readlink(descriptor) in inodes:
                    sys.exit(0)
        except (FileNotFoundError, PermissionError):
            continue
    time.sleep(0.05)
sys.exit(1)
"""


class RequestError(Exception):
    """A refused request; a code, when set, lets the gateway map it to an HTTP status."""

    def __init__(self, message, code=None):
        super().__init__(message)
        self.code = code


class FileError(RequestError):
    """A file transfer failure the gateway maps to an HTTP status by its code."""

    def __init__(self, code, message=None):
        super().__init__(message or FILE_FAILURES[code], code)


def session_failure(code, message=None):
    return RequestError(message or SESSION_FAILURES[code], code)


def established_ports(tables):
    """Count established TCP connections by local port, from /proc/net/tcp-style tables."""
    counts = {}
    for table in tables:
        for row in table.splitlines()[1:]:
            fields = row.split()
            if len(fields) > 3 and fields[3] == "01":
                port = int(fields[1].rsplit(":", 1)[1], 16)
                counts[port] = counts.get(port, 0) + 1
    return counts


def file_failure(error, directory=False):
    code = ERRNO_FAILURES.get(error.errno)
    if code is None:
        return RequestError("The file operation failed")
    if directory and code == "not_a_file":
        code = "not_a_directory"
    return FileError(code)


def refuse_paused(item):
    if item["State"].get("Status") == "paused":
        raise session_failure("session_paused")


def cpu_usage(group):
    """Microseconds of CPU time the cgroup has used, or None."""
    try:
        for line in (group / "cpu.stat").read_text().splitlines():
            name, _, value = line.partition(" ")
            if name == "usage_usec":
                return int(value)
    except (OSError, ValueError):
        pass
    return None


def memory_usage(group):
    try:
        return int((group / "memory.current").read_text())
    except (OSError, ValueError):
        return None


def private_directory(path):
    path = Path(path)
    try:
        path.mkdir(mode=0o700)
    except FileExistsError:
        pass
    info = path.lstat()
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid():
        raise RequestError("Runtime directory must be owned by the current user")
    if stat.S_IMODE(info.st_mode) != 0o700:
        raise RequestError("Runtime directory must have mode 0700")
    return path


def checked_text(request, key, maximum, default=None):
    value = request.get(key, default)
    if not isinstance(value, str) or len(value) > maximum or "\0" in value:
        raise RequestError("Invalid " + key)
    return value


def checked_name(request):
    name = checked_text(request, "name", 64)
    if not SESSION_NAME.fullmatch(name):
        raise RequestError("Invalid session name")
    return name


def checked_command(request, default=None):
    command = checked_text(request, "command", MAX_COMMAND_LENGTH, default)
    if not command.strip():
        raise RequestError("Command must not be empty")
    return command


def file_component(value):
    try:
        size = len(value.encode())
    except UnicodeEncodeError:
        return False
    return 0 < size <= MAX_NAME_BYTES and value not in (".", "..") and "/" not in value and "\0" not in value


def checked_path(request):
    value = request.get("path", "")
    if not isinstance(value, str) or len(value) > MAX_PATH_LENGTH:
        raise FileError("invalid_path")
    parts = value.split("/") if value else []
    if len(parts) > MAX_PATH_DEPTH or not all(map(file_component, parts)) or parts[:1] == [STAGING]:
        raise FileError("invalid_path")
    return parts


def checked_file_name(request, parts):
    name = request.get("name")
    if not isinstance(name, str) or not file_component(name) or (not parts and name == STAGING):
        raise FileError("invalid_path")
    return name


def checked_flag(request, key):
    value = request.get(key, False)
    if not isinstance(value, bool):
        raise RequestError("Invalid " + key)
    return value


def checked_token(request):
    token = request.get("token")
    if not isinstance(token, str) or not UPLOAD_TOKEN.fullmatch(token):
        raise FileError("upload_unknown")
    return token


def checked_size(request, key, maximum):
    value = request.get(key)
    if type(value) is not int or not 0 <= value <= maximum:
        raise RequestError("Invalid " + key)
    return value


def entry_type(mode):
    if stat.S_ISDIR(mode):
        return "dir"
    return "file" if stat.S_ISREG(mode) else "other"


def discard(source, size):
    while size:
        data = source.read(min(size, PIECE))
        if not data:
            return
        size -= len(data)


def copy_chunk(source, descriptor, offset, size):
    while size:
        data = source.read(min(size, PIECE))
        if not data:
            raise RequestError("The chunk ended early")
        view = memoryview(data)
        while view:
            written = os.pwrite(descriptor, view, offset)
            offset += written
            view = view[written:]
        size -= len(data)


class Upload:
    def __init__(self, token, parts, name, size, staging, descriptor):
        self.token = token
        self.parts = parts
        self.name = name
        self.size = size
        self.temporary = token + ".part"
        self.staging = staging
        self.descriptor = descriptor
        self.received = 0
        self.touched = time.monotonic()
        self.lock = threading.Lock()
        self.closed = False

    def close(self, remove):
        if self.closed:
            return
        self.closed = True
        try:
            if remove:
                try:
                    os.unlink(self.temporary, dir_fd=self.staging)
                except FileNotFoundError:
                    pass
        finally:
            try:
                os.close(self.descriptor)
            finally:
                os.close(self.staging)


class Files:
    """Transfers within ~/Downloads, which every session shares as /home/raven/Downloads."""

    def __init__(self, downloads=None):
        self.downloads = Path(downloads) if downloads is not None else Path.home() / "Downloads"
        self.uid = os.getuid()
        self.uploads = {}
        self.lock = threading.Lock()

    def dispatch(self, request):
        action = request.get("action")
        try:
            if action == "files_list":
                return self.listing(request)
            if action == "upload_start":
                return self.start(request)
            if action == "upload_finish":
                return self.finish(request)
            if action == "upload_cancel":
                return self.cancel(request)
        except OSError as error:
            raise file_failure(error) from error
        raise RequestError("Unknown operation")

    def open_root(self):
        # Raven creates ~/Downloads with g+rwxs when it makes a session; the broker never does.
        try:
            return os.open(self.downloads, DIRECTORY_FLAGS)
        except (FileNotFoundError, NotADirectoryError) as error:
            raise FileError("downloads_missing") from error
        except OSError as error:
            if error.errno == errno.ELOOP:
                raise FileError("downloads_missing") from error
            raise file_failure(error, directory=True) from error

    def open_directory(self, parts):
        descriptor = self.open_root()
        try:
            for part in parts:
                child = os.open(part, DIRECTORY_FLAGS, dir_fd=descriptor)
                os.close(descriptor)
                descriptor = child
        except OSError as error:
            os.close(descriptor)
            raise file_failure(error, directory=True) from error
        return descriptor

    def open_staging(self, root, create=True):
        if create:
            try:
                os.mkdir(STAGING, 0o700, dir_fd=root)
            except FileExistsError:
                pass
        try:
            staging = os.open(STAGING, DIRECTORY_FLAGS, dir_fd=root)
        except NotADirectoryError as error:
            raise FileError("staging_invalid") from error
        info = os.fstat(staging)
        # Downloads is setgid, so a new staging folder inherits that bit; only the permission bits matter.
        if info.st_uid != self.uid or stat.S_IMODE(info.st_mode) & 0o777 != 0o700:
            os.close(staging)
            raise FileError("staging_invalid")
        return staging

    def remove_stale(self, staging):
        """Best effort: a leftover that cannot be removed (a folder, ESTALE on NFS) must not block uploads."""
        with self.lock:
            active = {upload.temporary for upload in self.uploads.values()}
        limit = time.time() - STALE_UPLOAD_SECONDS
        try:
            with os.scandir(staging) as entries:
                for entry in entries:
                    if entry.name in active or not TEMPORARY_NAME.fullmatch(entry.name):
                        continue
                    try:
                        if entry.stat(follow_symlinks=False).st_mtime < limit:
                            os.unlink(entry.name, dir_fd=staging)
                    except OSError:
                        pass
        except OSError:
            pass

    def clean_staging(self):
        """Remove temporary files that an earlier broker left behind."""
        try:
            root = self.open_root()
            try:
                staging = self.open_staging(root, create=False)
            finally:
                os.close(root)
            try:
                self.remove_stale(staging)
            finally:
                os.close(staging)
        except (RequestError, OSError):
            pass

    def expire(self):
        # Called with self.lock held; an upload busy with a chunk is never expired under it.
        limit = time.monotonic() - UPLOAD_IDLE_SECONDS
        for token, upload in list(self.uploads.items()):
            if upload.touched < limit and upload.lock.acquire(blocking=False):
                try:
                    del self.uploads[token]
                    upload.close(remove=True)
                except OSError:
                    pass  # Its descriptors are closed; remove_stale() retries the file.
                finally:
                    upload.lock.release()

    def claim(self, token):
        with self.lock:
            self.expire()
            upload = self.uploads.get(token)
        if upload is None:
            raise FileError("upload_unknown")
        upload.lock.acquire()
        if upload.closed:
            upload.lock.release()
            raise FileError("upload_unknown")
        upload.touched = time.monotonic()
        return upload

    def listing(self, request):
        parts = checked_path(request)
        directory = self.open_directory(parts)
        entries, skipped, truncated = [], 0, False
        try:
            with os.scandir(directory) as found:
                for scanned, entry in enumerate(found, 1):
                    if scanned > MAX_SCANNED:
                        truncated = True
                        break
                    if not parts and entry.name == STAGING:
                        continue
                    if not file_component(entry.name):
                        skipped += 1
                        continue
                    if len(entries) == MAX_ENTRIES:
                        truncated = True
                        break
                    try:
                        info = entry.stat(follow_symlinks=False)
                    except FileNotFoundError:
                        continue
                    kind = entry_type(info.st_mode)
                    entries.append({"name": entry.name, "type": kind,
                                    "size": info.st_size if kind == "file" else 0, "mtime": int(info.st_mtime)})
        finally:
            os.close(directory)
        entries.sort(key=lambda entry: (entry["type"] != "dir", entry["name"]))
        return {"path": "/".join(parts), "entries": entries, "truncated": truncated, "skipped": skipped}

    def start(self, request):
        parts = checked_path(request)
        name = checked_file_name(request, parts)
        size = request.get("size")
        if type(size) is not int or size < 0:
            raise RequestError("Invalid size")
        if size > MAX_FILE_SIZE:
            raise FileError("too_large")
        overwrite = checked_flag(request, "overwrite")
        target = self.open_directory(parts)
        try:
            try:
                existing = os.stat(name, dir_fd=target, follow_symlinks=False)
            except FileNotFoundError:
                existing = None
            if existing is not None and stat.S_ISDIR(existing.st_mode):
                raise FileError("not_a_file")
            if existing is not None and not overwrite:
                raise FileError("file_exists")
            if not os.access(".", os.W_OK | os.X_OK, dir_fd=target):
                raise FileError("permission_denied")
            # Fail early, before touching the staging folder; admitted again below with the insertion.
            with self.lock:
                self.admit(target, size)
            root = self.open_root()
            try:
                staging = self.open_staging(root)
            finally:
                os.close(root)
            try:
                self.remove_stale(staging)
                token = secrets.token_urlsafe(24)
                descriptor = os.open(token + ".part",
                                     os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC,
                                     UPLOAD_MODE, dir_fd=staging)
            except BaseException:
                os.close(staging)
                raise
            upload = Upload(token, parts, name, size, staging, descriptor)
            try:
                # The broker's umask is 077; sessions read and write uploads through the shared group.
                os.fchmod(descriptor, UPLOAD_MODE)
                # Checking and inserting under one lock keeps two starts from both taking the last free space.
                with self.lock:
                    self.admit(target, size)
                    self.uploads[token] = upload
            except BaseException:
                try:
                    upload.close(remove=True)
                except OSError:
                    pass
                raise
        finally:
            os.close(target)
        return {"token": token}

    def admit(self, target, size):
        """Refuse an upload of size that would exceed MAX_UPLOADS or the free space; call with self.lock held."""
        self.expire()
        if len(self.uploads) >= MAX_UPLOADS:
            raise FileError("busy")
        reserved = sum(upload.size - upload.received for upload in self.uploads.values())
        space = os.statvfs(target)
        if space.f_bavail * space.f_frsize < size + reserved + FREE_SPACE_MARGIN:
            raise FileError("no_space")

    def chunk(self, request, source):
        size = checked_size(request, "size", CHUNK_SIZE)
        try:
            offset = checked_size(request, "offset", MAX_FILE_SIZE)
            upload = self.claim(checked_token(request))
        except RequestError:
            discard(source, size)
            raise
        try:
            if offset != upload.received:
                discard(source, size)
                raise FileError("offset_mismatch")
            if offset + size > upload.size:
                discard(source, size)
                raise FileError("too_large", "The chunk goes past the declared file size")
            try:
                copy_chunk(source, upload.descriptor, offset, size)
            except BaseException:
                try:
                    os.ftruncate(upload.descriptor, upload.received)
                except OSError:
                    pass
                raise
            upload.received += size
            upload.touched = time.monotonic()
            return {"received": upload.received}
        except OSError as error:
            if isinstance(error, TimeoutError):
                raise
            raise file_failure(error) from error
        finally:
            upload.lock.release()

    def finish(self, request):
        overwrite = checked_flag(request, "overwrite")
        upload = self.claim(checked_token(request))
        try:
            if upload.received != upload.size:
                raise FileError("offset_mismatch", "The upload is incomplete")
            os.fsync(upload.descriptor)
            target = self.open_directory(upload.parts)
            try:
                self.place(upload, target, overwrite)
            finally:
                os.close(target)
            info = os.fstat(upload.descriptor)
            with self.lock:
                self.uploads.pop(upload.token, None)
            try:
                upload.close(remove=False)
            except OSError:
                pass  # The data was fsynced and the file is in place.
        finally:
            upload.lock.release()
        return {"name": upload.name, "size": info.st_size, "mtime": int(info.st_mtime)}

    def place(self, upload, target, overwrite):
        try:
            if stat.S_ISDIR(os.stat(upload.name, dir_fd=target, follow_symlinks=False).st_mode):
                raise FileError("not_a_file")
        except FileNotFoundError:
            pass
        try:
            if overwrite:
                os.replace(upload.temporary, upload.name, src_dir_fd=upload.staging, dst_dir_fd=target)
                return
            # link() fails on an existing name instead of replacing it, unlike rename().
            os.link(upload.temporary, upload.name, src_dir_fd=upload.staging, dst_dir_fd=target,
                    follow_symlinks=False)
        except OSError as error:
            if error.errno == errno.EXDEV:
                raise FileError("cross_device") from error
            raise file_failure(error) from error
        try:
            os.unlink(upload.temporary, dir_fd=upload.staging)
        except OSError:
            pass  # remove_stale() deletes it later

    def cancel(self, request):
        token = checked_token(request)
        with self.lock:
            upload = self.uploads.pop(token, None)
        if upload is not None:
            with upload.lock:
                upload.close(remove=True)
        return None

    def open_download(self, request):
        parts = checked_path(request)
        if not parts:
            raise FileError("not_a_file")
        parent = self.open_directory(parts[:-1])
        try:
            # O_NONBLOCK keeps a FIFO from blocking the open; fstat then rejects it.
            descriptor = os.open(parts[-1], os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC,
                                 dir_fd=parent)
        except OSError as error:
            raise file_failure(error) from error
        finally:
            os.close(parent)
        info = os.fstat(descriptor)
        if not stat.S_ISREG(info.st_mode):
            os.close(descriptor)
            raise FileError("not_a_file")
        return parts[-1], descriptor, info.st_size


class Raven:
    def __init__(self, cli):
        self.cli = str(Path(cli).resolve())
        self.user = re.sub(r"[^A-Za-z0-9_.-]", "_", pwd.getpwuid(os.getuid()).pw_name)
        state = os.environ.get("XDG_STATE_HOME", str(Path.home() / ".local/state"))
        self.state = Path(state) / "raven" / socket.gethostname()
        self.runtime = Path(os.environ.get("XDG_RUNTIME_DIR", "/tmp")) / f"raven-{os.getuid()}"
        self.lock = threading.Lock()
        self.proc = Path("/proc")
        self.cgroups = Path("/sys/fs/cgroup")
        self.environment = dict(os.environ)
        self.environment.pop("DISPLAY", None)

    def execute(self, args):
        result = subprocess.run(args, capture_output=True, env=self.environment, timeout=COMMAND_TIMEOUT)
        if result.returncode:
            raise RequestError("Raven operation failed; inspect the session with the CLI")
        return result

    def inspect(self, container):
        result = self.execute(["docker", "inspect", "--type", "container", container])
        item = json.loads(result.stdout)[0]
        labels = item["Config"].get("Labels") or {}
        sid = labels.get("raven.id", "")
        if (labels.get("raven.user") != self.user or not SESSION_ID.fullmatch(sid)
                or item["Name"] != f"/raven-{self.user}-{sid}"):
            raise RequestError("Session is not owned by this user")
        return item

    def session(self, request):
        sid = checked_text(request, "id", 9)
        if not SESSION_ID.fullmatch(sid):
            raise RequestError("Invalid session id")
        item = self.inspect(f"raven-{self.user}-{sid}")
        if request.get("action") in ("rename", "delete", "run", "pause", "resume"):
            if checked_text(request, "generation", 64) != item["Id"]:
                raise RequestError("Session changed; refresh the session list")
        return item

    def describe(self, item, usage=None):
        labels = item["Config"]["Labels"]
        sid = labels["raven.id"]
        try:
            name = (self.state / f"{sid}.name").read_text().strip()
        except FileNotFoundError:
            name = ""
        if usage is None:
            usage = self.usage([item])[item["Id"]]
        return {"id": sid, "generation": item["Id"], "name": name, "state": item["State"]["Status"],
                "wm": labels.get("raven.wm") or "openbox",
                "geometry": labels["raven.geometry"], "command": labels["raven.cmd"],
                "display": labels["raven.display"], **usage}

    def cgroup(self, item):
        """The container's cgroup v2 directory, found through its init process; None when unknown."""
        state = item["State"]
        pid = state.get("Pid")
        if not state.get("Running") or type(pid) is not int or pid <= 0:
            return None
        try:
            lines = (self.proc / str(pid) / "cgroup").read_text().splitlines()
        except OSError:
            return None
        path = next((line[3:] for line in lines if line.startswith("0::")), "")
        parts = path.split("/")
        # The init process may have exited and its PID been reused: the path must name this container.
        if not path.startswith("/") or ".." in parts or item["Id"] not in path:
            return None
        return self.cgroups.joinpath(*parts[1:])

    def x11_viewers(self, group):
        """VNC viewer processes inside the session: the X11 viewers that raven attach starts."""
        try:
            pids = (group / "cgroup.procs").read_text().split()
        except OSError:
            return None
        count = 0
        for pid in pids:
            try:
                if (self.proc / pid / "comm").read_text().strip() in VIEWER_PROCESSES:
                    count += 1
            except OSError:
                continue  # It exited meanwhile.
        return count

    def connections(self):
        """Established TCP connections by local port; sessions share the host network."""
        tables = []
        for name in ("tcp", "tcp6"):
            try:
                tables.append((self.proc / "net" / name).read_text())
            except FileNotFoundError:
                if name == "tcp":
                    return None
            except OSError:
                return None
        try:
            return established_ports(tables)
        except (ValueError, IndexError):
            return None

    def usage(self, items):
        """Memory, CPU and viewers of each session by container ID, without docker exec.

        A value that cannot be read is None; it never fails the listing. Paused sessions keep
        their memory, use no CPU and have no viewers, as pause disconnected them.
        """
        groups = {item["Id"]: self.cgroup(item) for item in items}
        running = [item["Id"] for item in items
                   if item["State"].get("Status") == "running" and groups[item["Id"]] is not None]
        before, after, elapsed = {}, {}, 0
        if running:
            started = time.monotonic()
            before = {cid: cpu_usage(groups[cid]) for cid in running}
            time.sleep(CPU_SAMPLE_SECONDS)
            elapsed = time.monotonic() - started
            after = {cid: cpu_usage(groups[cid]) for cid in running}
        connections = self.connections() if running else None
        result = {}
        for item in items:
            cid, status, group = item["Id"], item["State"].get("Status"), groups[item["Id"]]
            memory = cpu = None
            viewers = {"x11": 0, "web": 0}
            if group is not None and status in ("running", "paused"):
                memory = memory_usage(group)
            if status == "paused":
                cpu = 0.0
            elif status == "running":
                viewers = None
                if before.get(cid) is not None and after.get(cid) is not None and elapsed > 0:
                    cpu = round(max(after[cid] - before[cid], 0) / (elapsed * 1e6) * 100, 1)
                x11 = self.x11_viewers(group) if group is not None else None
                try:
                    port = int(item["Config"]["Labels"]["raven.port"])
                except (KeyError, ValueError):
                    port = None
                if x11 is not None and connections is not None and port is not None:
                    # The other connections to the VNC port are web viewers through this broker.
                    viewers = {"x11": x11, "web": max(connections.get(port, 0) - x11, 0)}
            result[cid] = {"memory": memory, "cpu": cpu, "viewers": viewers}
        return result

    def ensure_up(self, sid):
        self.execute([self.cli, "menu", "show", "-t", sid])

    def delete(self, item):
        sid = item["Config"]["Labels"]["raven.id"]
        self.execute(["docker", "rm", "-f", item["Id"]])
        # The home volume reserves this numeric ID until its metadata is removed.
        for path in (self.state / f"{sid}.name", self.state / f"{sid}.home",
                     self.runtime / f"{sid}.xauth", self.runtime / f"{sid}.log"):
            path.unlink(missing_ok=True)
        self.execute(["docker", "volume", "rm", f"raven-{self.user}-home-{sid}"])

    def create(self, request):
        name = checked_name(request)
        wm = checked_text(request, "wm", 16, "xmonad")
        if wm not in ("xmonad", "openbox"):
            raise RequestError("Invalid window manager")
        geometry = checked_text(request, "geometry", 16, "1600x900")
        match = re.fullmatch(r"([0-9]{3,4})x([0-9]{3,4})", geometry)
        if not match or not (320 <= int(match[1]) <= 7680 and 200 <= int(match[2]) <= 4320):
            raise RequestError("Invalid geometry")
        command = checked_command(request, "xterm")
        result = self.execute([self.cli, "new", "-d", "-s", name, "-g", geometry, "-w", wm, "--", command])
        matches = re.findall(rb"^raven: created session ([0-9]+)(?: |\n)", result.stderr, re.MULTILINE)
        if not matches:
            raise RequestError("Session creation did not return an id; inspect raven ls")
        return self.describe(self.session({"id": matches[-1].decode()}))

    def dispatch(self, request):
        action = request.get("action")
        if action == "list":
            result = self.execute(["docker", "ps", "-aq", "--filter", f"label=raven.user={self.user}"])
            items = [self.inspect(cid) for cid in result.stdout.decode().split()]
            usage = self.usage(items)
            sessions = [self.describe(item, usage[item["Id"]]) for item in items]
            return sorted(sessions, key=lambda session: int(session["id"]))
        if action == "create":
            return self.create(request)
        if action not in ("open", "rename", "delete", "run", "pause", "resume"):
            raise RequestError("Unknown operation")
        if action == "delete" and checked_text(request, "confirm", 9) != request.get("id"):
            raise RequestError("Confirm the exact session ID before deleting it")
        item = self.session(request)
        sid = item["Config"]["Labels"]["raven.id"]
        if action == "rename":
            self.execute([self.cli, "rename", "-t", sid, checked_name(request)])
            return self.describe(self.session(request))
        if action == "delete":
            self.delete(item)
            return None
        if action == "pause":
            status = item["State"]["Status"]
            if status == "paused":
                raise session_failure("session_paused", "Session is already paused")
            if status != "running":
                raise session_failure("session_not_running")
            self.execute([self.cli, "pause", "-t", sid])
            return self.describe(self.session(request))
        if action == "resume":
            if item["State"]["Status"] != "paused":
                raise session_failure("session_not_paused")
            self.execute([self.cli, "resume", "-t", sid])
            return self.describe(self.session(request))
        if action == "run":
            command = checked_command(request)
            refuse_paused(item)
            self.ensure_up(sid)
            item = self.session(request)
            refuse_paused(item)
            display = item["Config"]["Labels"]["raven.display"]
            self.execute(["docker", "exec", "-d", "-w", "/home/raven",
                          "-e", f"DISPLAY=:{display}", "-e", "XAUTHORITY=/home/raven/.Xauthority",
                          item["Id"], "sh", "-c", "umask 002; " + command])
            return None
        # open: a paused session would only fail in ensure_up, with a less useful message.
        refuse_paused(item)
        self.ensure_up(sid)
        item = self.session(request)
        refuse_paused(item)
        password = self.execute(["docker", "exec", item["Id"], "cat", "/home/raven/.vnc/passwd"]).stdout
        if len(password) != 8:
            raise RequestError("Unsupported VNC password file")
        return {"id": sid, "name": self.describe(item)["name"],
                "password_file": base64.b64encode(password).decode()}

    def connect(self, request):
        item = self.session(request)
        labels = item["Config"]["Labels"]
        # Running stays true while paused; Xvnc would accept the TCP connection and never answer.
        refuse_paused(item)
        if not item["State"]["Running"]:
            raise session_failure("session_not_running")
        try:
            port, display = int(labels["raven.port"]), int(labels["raven.display"])
        except (KeyError, ValueError) as error:
            raise RequestError("Invalid session endpoint") from error
        if not 15900 <= port <= 65535 or port != 15800 + display:
            raise RequestError("Invalid session endpoint")
        upstream = socket.create_connection(("127.0.0.1", port), timeout=SOCKET_TIMEOUT)
        try:
            self.execute(["docker", "exec", item["Id"], "python3", "-c", VERIFY_VNC_PEER,
                          str(port), str(upstream.getsockname()[1])])
        except BaseException:
            upstream.close()
            raise
        return upstream


class Handler(socketserver.StreamRequestHandler):
    streaming = False

    def reply(self, **payload):
        if self.streaming:
            return  # Raw download bytes are on the wire; a JSON line would read as file content.
        self.wfile.write(json.dumps(payload).encode() + b"\n")
        self.wfile.flush()

    def handle(self):
        self.request.settimeout(SOCKET_TIMEOUT)
        credentials = self.request.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, UCRED.size)
        _, uid, _ = UCRED.unpack(credentials)
        if uid != os.getuid():
            self.reply(ok=False, error="Permission denied")
            return
        if not self.server.clients.acquire(blocking=False):
            self.reply(ok=False, error="Too many requests")
            return
        try:
            line = self.rfile.readline(MAX_REQUEST + 1)
            if len(line) > MAX_REQUEST or not line.endswith(b"\n"):
                raise RequestError("Invalid request size")
            request = json.loads(line)
            if not isinstance(request, dict):
                raise RequestError("Expected an object")
            action = request.get("action")
            if action == "ping":
                self.reply(ok=True, data={"raven": self.server.raven.cli, "pid": os.getpid()})
            elif action == "shutdown":
                self.reply(ok=True, data=None)
                self.server.shutdown()
            elif action == "connect":
                with self.server.raven.connect(request) as upstream:
                    self.reply(ok=True, data=None)
                    self.forward(upstream)
            # File transfers never take raven.lock, so a long stream does not hold up session management.
            elif action == "download":
                self.download(request)
            elif action == "upload_chunk":
                self.reply(ok=True, data=self.server.files.chunk(request, self.rfile))
            elif action in FILE_ACTIONS:
                self.reply(ok=True, data=self.server.files.dispatch(request))
            else:
                with self.server.raven.lock:
                    data = self.server.raven.dispatch(request)
                self.reply(ok=True, data=data)
        except RequestError as error:
            if error.code is None:
                self.reply(ok=False, error=str(error))
            else:
                self.reply(ok=False, error=str(error), code=error.code)
        except ValueError:
            self.reply(ok=False, error="Invalid request")
        except (OSError, subprocess.TimeoutExpired):
            try:
                self.reply(ok=False, error="Raven backend is unavailable")
            except OSError:
                pass
        finally:
            self.server.clients.release()

    def download(self, request):
        if not self.server.downloads.acquire(blocking=False):
            raise FileError("busy", "Too many downloads in progress")
        try:
            self.send_file(request)
        finally:
            self.server.downloads.release()

    def send_file(self, request):
        name, descriptor, size = self.server.files.open_download(request)
        with open(descriptor, "rb") as source:
            self.reply(ok=True, data={"name": name, "size": size})
            # A failure from here on ends the body short of size, which the gateway passes on as a failed download.
            self.streaming = True
            if size:
                self.request.settimeout(DOWNLOAD_TIMEOUT)
                self.request.sendfile(source, 0, size)

    def forward(self, upstream):
        with selectors.DefaultSelector() as selector:
            selector.register(self.request, selectors.EVENT_READ, upstream)
            selector.register(upstream, selectors.EVENT_READ, self.request)
            while True:
                for key, _ in selector.select():
                    data = key.fileobj.recv(MAX_REQUEST)
                    if not data:
                        return
                    key.data.sendall(data)


class Server(socketserver.ThreadingUnixStreamServer):
    daemon_threads = True

    def __init__(self, address, raven, files=None):
        self.raven = raven
        self.files = files if files is not None else Files()
        self.clients = threading.BoundedSemaphore(MAX_CLIENTS)
        self.downloads = threading.BoundedSemaphore(MAX_DOWNLOADS)
        super().__init__(address, Handler)


def main():
    cli, address = sys.argv[1:]
    os.umask(0o077)
    private_directory(Path(address).parent)
    files = Files()
    files.clean_staging()
    with Server(address, Raven(cli), files) as server:
        os.chmod(address, 0o600)
        signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))
        try:
            server.serve_forever()
        finally:
            Path(address).unlink(missing_ok=True)


if __name__ == "__main__":
    main()
