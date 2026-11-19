"""Back up disposable sessions and restore them as new sessions, against the real image.

Uses a temporary HOME and XDG directories, so the user's sessions, ~/.config/raven, ~/Downloads and
~/.local/share/raven are never touched; every test session is killed at the end. Checks:
- a running session is frozen while its home is read and continues with the same start time and
  processes, and that live backup restores the same files; a paused session is backed up as it is
  and stays paused; --stop stops the session (from running and from paused), backs it up and starts
  it again (new StartedAt, running afterwards);
- a stopped session's backup is a 0600 file in a 0700 directory even under umask 000, holds exactly
  manifest.json and home.tar.gz, and its manifest names the session, image and home;
- the restored homes have the same Korean and space paths, contents, modes (exec bit, 0600, empty
  0700 directory, home root), symlinks and hard links as the source, every entry owned by 60000
  (a root-owned file too); a FIFO is left out of the backup with a message;
- restore takes the window manager, size, command and name from the backup, -s/-w/-g/CMD win, a
  name in use is refused, and the source session (container, start time, home) stays the same;
- damaged and crafted backups (sha256 mismatch, truncated file, extra outer member, ../x and
  absolute members, device and FIFO members, hard links out of the home, a member under a symlink,
  a newer format version) are refused without leaving a volume, container or state file;
- the user's other sessions keep their state and start time.
"""

import hashlib
import io
import json
import os
from pathlib import Path
import re
import secrets
import shutil
import socket
import stat
import subprocess
import sys
import tarfile
import tempfile

SOURCE = Path(__file__).resolve().parents[1]
RAVEN = str(SOURCE / "raven")
IMAGE = os.environ.get("RAVEN_IMAGE", "raven:latest")
USER = re.sub(r"[^A-Za-z0-9_.-]", "_", subprocess.run(["id", "-un"], capture_output=True, text=True).stdout.strip())
TIMEOUT = 180
FIXTURE = "m10 fixture"

MAKE_FIXTURE = r'''
set -e
cd ~ && mkdir -p "m10 fixture" && cd "m10 fixture"
mkdir -p "한글 폴더/빈 디렉터리" "빈 디렉터리"
chmod 700 "빈 디렉터리"
printf '안녕하세요, Raven\n' > "한글 폴더/공백 있는 파일.txt"
printf '#!/bin/sh\necho run\n' > run.sh && chmod 755 run.sh
printf 'secret\n' > "비밀 600" && chmod 600 "비밀 600"
head -c 1048576 /dev/urandom > random.bin
ln -s "한글 폴더/공백 있는 파일.txt" link
ln -s /etc/hostname abs-link
ln run.sh hard
mkfifo pipe
'''

# Prints the fixture tree of the home mounted at /home/raven: path -> [type, mode, uid, gid, content
# sha256 or link target, first path with the same inode]
TREE = r'''
import hashlib, json, os, stat
root, out, inodes = "/home/raven", {}, {}
for top, dirs, files in os.walk(os.path.join(root, "m10 fixture")):
    dirs.sort()
    for path in [top] + [os.path.join(top, n) for n in sorted(dirs + files)]:
        st = os.lstat(path)
        rel = os.path.relpath(path, root)
        kind = "l" if stat.S_ISLNK(st.st_mode) else "d" if stat.S_ISDIR(st.st_mode) else \
            "f" if stat.S_ISREG(st.st_mode) else "p" if stat.S_ISFIFO(st.st_mode) else "?"
        data = os.readlink(path) if kind == "l" else \
            hashlib.sha256(open(path, "rb").read()).hexdigest() if kind == "f" else ""
        same = inodes.setdefault(st.st_ino, rel) if kind == "f" else rel
        out[rel] = [kind, oct(stat.S_IMODE(st.st_mode)), st.st_uid, st.st_gid, data, same]
st = os.lstat(root)
out["."] = ["d", oct(stat.S_IMODE(st.st_mode)), st.st_uid, st.st_gid, "", "."]
print(json.dumps(out, sort_keys=True, ensure_ascii=False))
'''


def raven(environment, *arguments, check=True, umask=None):
    result = subprocess.run([RAVEN, *arguments], env=environment, capture_output=True, text=True,
                            stdin=subprocess.DEVNULL, timeout=TIMEOUT,
                            preexec_fn=(lambda: os.umask(umask)) if umask is not None else None)
    if check and result.returncode:
        raise AssertionError(f"raven {' '.join(arguments)} failed: {result.stderr}")
    return result


def docker(*arguments, check=True):
    return subprocess.run(["docker", *arguments], capture_output=True, text=True, check=check, timeout=TIMEOUT)


def container(sid):
    return f"raven-{USER}-{sid}"


def volume(sid):
    return f"raven-{USER}-home-{sid}"


def inspect(sid):
    return json.loads(docker("inspect", container(sid)).stdout)[0]


def tree(sid):
    result = docker("run", "--rm", "--network", "none", "--user", "0", "--mount",
                    f"type=volume,src={volume(sid)},dst=/home/raven,readonly,volume-nocopy",
                    "--entrypoint", "python3", IMAGE, "-c", TREE)
    return json.loads(result.stdout)


def created_id(result):
    return re.search(r"raven: restored session ([0-9]+)", result.stderr)[1]


def leftovers(statedir):
    """What a refused restore must not add: home volumes, session containers, state files."""
    volumes = docker("volume", "ls", "-q").stdout.split()
    containers = docker("ps", "-aq", "--filter", f"label=raven.user={USER}").stdout.split()
    files = sorted(p.name for p in statedir.glob("*")) if statedir.exists() else []
    return sorted(v for v in volumes if v.startswith(f"raven-{USER}-home-")), sorted(containers), files


def others():
    """State and start time of the sessions that are not the test's (the user's own)."""
    names = docker("ps", "-a", "--filter", f"label=raven.user={USER}", "--format", "{{.Names}}").stdout.split()
    return {n: docker("inspect", "-f", "{{.Id}} {{.State.Status}} {{.State.StartedAt}}", n).stdout.strip()
            for n in names}


def pack(members):
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w", format=tarfile.GNU_FORMAT) as archive:
        for name, data in members:
            entry = tarfile.TarInfo(name)
            entry.size, entry.mode = len(data), 0o600
            archive.addfile(entry, io.BytesIO(data))
    return buffer.getvalue()


def crafted_home(*extra):
    """A small home archive with a top directory, one file, and the given (name, type, link) members."""
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w:gz", format=tarfile.GNU_FORMAT) as archive:
        for name, kind, link in (("./", tarfile.DIRTYPE, ""), ("./a", tarfile.REGTYPE, "")) + extra:
            entry = tarfile.TarInfo(name)
            entry.type, entry.linkname, entry.uid, entry.gid = kind, link, 60000, 60000
            data = b"x" if kind == tarfile.REGTYPE else b""
            entry.size, entry.mode = len(data), 0o755 if kind == tarfile.DIRTYPE else 0o644
            archive.addfile(entry, io.BytesIO(data) if data else None)
    return buffer.getvalue(), 2 + len(extra)


def bad_backups(good):
    outer = tarfile.open(fileobj=io.BytesIO(good))
    manifest_data = outer.extractfile("manifest.json").read()
    manifest = json.loads(manifest_data)
    home = outer.extractfile("home.tar.gz").read()

    def with_home(members, **change):
        data, entries = crafted_home(*members)
        m = json.loads(manifest_data)
        m["home"].update(sha256=hashlib.sha256(data).hexdigest(), size=len(data), entries=entries)
        m.update(change)
        return pack([("manifest.json", json.dumps(m).encode()), ("home.tar.gz", data)])

    flipped = bytearray(good)
    flipped[good.index(home) + len(home) // 2] ^= 0xFF
    yield "sha256 mismatch", bytes(flipped), "does not match the sha256 in the manifest"
    yield "truncated", good[:good.index(home) + len(home) // 2], "truncated"
    yield "extra outer member", pack([("manifest.json", manifest_data), ("home.tar.gz", home), ("extra", b"x")]), \
        "members besides manifest.json and home.tar.gz"
    yield "../x member", with_home([("./../x", tarfile.REGTYPE, "")]), "points outside the home"
    yield "absolute member", with_home([("/tmp/x", tarfile.REGTYPE, "")]), "points outside the home"
    yield "device member", with_home([("./dev", tarfile.CHRTYPE, "")]), "is a character device"
    yield "FIFO member", with_home([("./fifo", tarfile.FIFOTYPE, "")]), "is a FIFO"
    yield "hard link out of the home", with_home([("./h", tarfile.LNKTYPE, "../../etc/passwd")]), \
        "Removing leading `../../' from hard link targets"
    yield "absolute hard link", with_home([("./h", tarfile.LNKTYPE, "/etc/passwd")]), \
        "Removing leading `/' from hard link targets"
    yield "member under a symlink", with_home([("./l", tarfile.SYMTYPE, "/tmp"), ("./l/x", tarfile.REGTYPE, "")]), \
        "lies under the symbolic link"
    yield "newer format", with_home([], version=2), "version 2 is not supported"
    yield "bad session name", with_home([], session=dict(manifest["session"], name="../x")), \
        "session name '../x' must start with a letter"
    # A line break in the command label would add a fake row to raven's session list
    yield "line break in the command", \
        with_home([], session=dict(manifest["session"], cmd="xterm\n7 107 15907 running xterm")), \
        "session.cmd must be text without control characters"
    assert manifest["format"] == "raven-session-backup"


def main():
    scratch = Path(tempfile.mkdtemp(prefix="raven-backup-e2e-"))
    environment = dict(os.environ)
    environment.pop("DISPLAY", None)
    for variable, directory in (("HOME", "home"), ("XDG_CONFIG_HOME", "config"), ("XDG_STATE_HOME", "state"),
                                ("XDG_DATA_HOME", "data"), ("XDG_RUNTIME_DIR", "runtime")):
        (scratch / directory).mkdir(mode=0o700)
        environment[variable] = str(scratch / directory)
    environment["RAVEN_IMAGE"] = IMAGE
    previous_hostd = f"@raven-host-{os.getuid()}" in Path("/proc/net/unix").read_text()
    statedir = scratch / "state" / "raven" / socket.gethostname()
    backups = scratch / "data" / "raven" / "backups"
    name = f"m10e2e-{secrets.token_hex(3)}"
    before_others = others()
    sids = []
    report = {}
    try:
        result = raven(environment, "new", "-d", "-s", name, "-w", "openbox", "-g", "1024x768", "xclock -digital")
        sid = re.search(r"raven: created session ([0-9]+)", result.stderr)[1]
        sids.append(sid)
        docker("exec", "-u", "raven", container(sid), "sh", "-c", MAKE_FIXTURE)
        docker("exec", "-u", "0", container(sid), "sh", "-c", f'touch "/home/raven/{FIXTURE}/root owned"')

        # A running session is frozen while its home is read and continues with the same processes
        started = inspect(sid)["State"]["StartedAt"]
        clock = docker("exec", container(sid), "pgrep", "-x", "xclock").stdout.strip()
        live_backup = scratch / "live.tar"
        result = raven(environment, "backup", name, "-o", str(live_backup))
        state = inspect(sid)["State"]
        assert state["Status"] == "running" and state["StartedAt"] == started, state
        assert clock and docker("exec", container(sid), "pgrep", "-x", "xclock").stdout.strip() == clock
        assert f"freezing session {sid}" in result.stderr and "power cut" in result.stderr, result.stderr
        assert result.stdout == f"{live_backup}\n" and stat.S_IMODE(live_backup.stat().st_mode) == 0o600
        report["live backup"] = result.stderr.strip()
        # A paused session is backed up as it is and stays paused
        raven(environment, "pause", name)
        out = scratch / "paused.tar"
        result = raven(environment, "backup", "-t", name, "-o", str(out))
        state = inspect(sid)["State"]
        assert state["Status"] == "paused" and state["StartedAt"] == started, state
        assert "it stays paused" in result.stderr, result.stderr
        report["paused backup"] = result.stderr.strip()
        assert not backups.exists() or not list(backups.iterdir()), list(backups.iterdir())

        # --stop from paused, then from running: stopped, backed up, started again
        for label in ("paused", "running"):
            out = scratch / f"stop-{label}.tar"
            result = raven(environment, "backup", name, "--stop", "-o", str(out))
            state = inspect(sid)["State"]
            assert state["Status"] == "running" and state["StartedAt"] != started, (label, state)
            assert "unsaved work in them is lost" in result.stderr and "started session" in result.stderr, \
                result.stderr
            assert result.stdout == f"{out}\n" and stat.S_IMODE(out.stat().st_mode) == 0o600, result.stdout
            started = state["StartedAt"]
            report[f"--stop from {label}"] = result.stderr.strip()

        # A stopped session is backed up as it is; the file is private whatever the umask
        docker("stop", container(sid))
        result = raven(environment, "backup", name, umask=0)
        good_path = Path(result.stdout.strip())
        assert good_path.parent == backups, good_path
        assert re.fullmatch(rf"session-{sid}-{name}-[0-9]{{8}}T[0-9]{{6}}Z\.tar", good_path.name), good_path
        assert stat.S_IMODE(backups.stat().st_mode) == 0o700, oct(backups.stat().st_mode)
        assert stat.S_IMODE(good_path.stat().st_mode) == 0o600, oct(good_path.stat().st_mode)
        assert not [p for p in backups.iterdir() if p != good_path], list(backups.iterdir())
        assert "left out 1 special file" in result.stderr and "pipe" in result.stderr, result.stderr
        assert "~/Downloads is not included" in result.stderr, result.stderr
        report["backup"] = {"stdout": result.stdout.strip(), "stderr": result.stderr.strip()}
        with tarfile.open(good_path) as outer:
            assert [(m.name, m.isreg()) for m in outer.getmembers()] == \
                [("manifest.json", True), ("home.tar.gz", True)], outer.getnames()
            manifest = json.loads(outer.extractfile("manifest.json").read())
        image_id = docker("image", "inspect", "-f", "{{.Id}}", IMAGE).stdout.strip()
        assert manifest["format"] == "raven-session-backup" and manifest["version"] == 1, manifest
        assert manifest["session"] == {"id": sid, "name": name, "cmd": "xclock -digital", "wm": "openbox",
                                       "geometry": "1024x768", "uid": "60000"}, manifest["session"]
        assert manifest["image"]["id"] == image_id and manifest["user"] == USER, manifest
        assert manifest["downloads"] == "not included" and manifest["home"]["file"] == "home.tar.gz", manifest
        report["manifest"] = manifest
        source_tree = tree(sid)
        source_state = inspect(sid)
        # The FIFO is left out; every entry belongs to the session user, modes stay
        expected = {path: entry[:2] + [60000, 60000] + entry[4:] for path, entry in source_tree.items()
                    if path != f"{FIXTURE}/pipe"}
        assert source_tree[f"{FIXTURE}/root owned"][2] == 0, source_tree[f"{FIXTURE}/root owned"]
        assert source_tree[f"{FIXTURE}/hard"][5] == source_tree[f"{FIXTURE}/run.sh"][5], source_tree
        assert source_tree[f"{FIXTURE}/빈 디렉터리"][1] == "0o700", source_tree
        assert source_tree[f"{FIXTURE}/비밀 600"][1] == "0o600", source_tree

        # The live backup restores the same fixture files
        result = raven(environment, "restore", str(live_backup), "-s", f"{name}-live")
        sids.append(created_id(result))
        fixture = lambda entries: {path: entry for path, entry in entries.items() if path.startswith(FIXTURE)}
        assert fixture(tree(sids[-1])) == fixture(expected), (fixture(tree(sids[-1])), fixture(expected))
        report["live restore"] = result.stderr.strip()

        # The backup's name is in use: refused before anything is made
        made = leftovers(statedir)
        result = raven(environment, "restore", str(good_path), check=False)
        assert result.returncode == 1 and f"name '{name}' from the backup is used by session {sid}" in result.stderr, \
            result.stderr
        assert leftovers(statedir) == made

        # Restore with the backup's settings under another name
        copy = f"{name}-copy"
        result = raven(environment, "restore", str(good_path), "-s", copy)
        report["restore"] = result.stderr.strip()
        sids.append(created_id(result))
        restored = inspect(sids[-1])
        labels = restored["Config"]["Labels"]
        assert (labels["raven.wm"], labels["raven.geometry"], labels["raven.cmd"]) == \
            ("openbox", "1024x768", "xclock -digital"), labels
        assert (statedir / f"{sids[-1]}.name").read_text() == f"{copy}\n"
        assert restored["State"]["Status"] == "running", restored["State"]
        assert f"attach with: raven attach {sids[-1]}" in result.stderr, result.stderr
        assert "1 entries belong to other users" in result.stderr, result.stderr
        assert tree(sids[-1]) == expected, (tree(sids[-1]), expected)

        # Options win; the backup's own name once the source no longer has it
        raven(environment, "rename", name, f"{name}-old")
        result = raven(environment, "restore", str(good_path), "-w", "xmonad", "-g", "800x600", "xterm", "-T", "m10")
        sids.append(created_id(result))
        labels = inspect(sids[-1])["Config"]["Labels"]
        assert (labels["raven.wm"], labels["raven.geometry"], labels["raven.cmd"]) == \
            ("xmonad", "800x600", "xterm -T m10"), labels
        assert (statedir / f"{sids[-1]}.name").read_text() == f"{name}\n"
        assert tree(sids[-1]) == expected
        listing = raven(environment, "ls").stdout
        assert re.search(rf"^{sids[-1]} +{re.escape(name)} +running +xmonad ", listing, re.M), listing

        # The source session is as it was
        after = inspect(sid)
        assert (after["Id"], after["State"]["StartedAt"], after["State"]["Status"]) == \
            (source_state["Id"], source_state["State"]["StartedAt"], "exited"), after["State"]
        assert tree(sid) == source_tree

        # Damaged and crafted backups are refused and leave nothing behind
        made = leftovers(statedir)
        report["refused"] = {}
        for i, (case, data, message) in enumerate(bad_backups(good_path.read_bytes())):
            bad = scratch / f"bad-{i}.tar"
            bad.write_bytes(data)
            result = raven(environment, "restore", str(bad), "-s", f"bad{i}", check=False)
            assert result.returncode == 1 and message in result.stderr \
                and "nothing was created" in result.stderr, (case, result.stderr)
            assert leftovers(statedir) == made, (case, leftovers(statedir), made)
            report["refused"][case] = result.stderr.strip().splitlines()[0]

        for sid_ in reversed(sids):
            raven(environment, "kill", "-t", sid_)
        sids.clear()
        assert others() == before_others, (others(), before_others)
        report["session_backup_e2e"] = "passed"
        print(json.dumps(report, indent=2, ensure_ascii=False))
    finally:
        for sid_ in sids:
            raven(environment, "kill", "-t", sid_, check=False)
        if not previous_hostd:
            raven(environment, "hostd", "stop", check=False)
        shutil.rmtree(scratch, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(main())
