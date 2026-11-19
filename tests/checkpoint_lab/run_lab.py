"""M11: can a Raven desktop session be checkpointed to disk (Docker checkpoint, CRIU) and restored?

Runs the whole ladder in an isolated, privileged Docker-in-Docker lab and prints a JSON summary.
The server's Docker daemon, its configuration and its packages are not changed: the lab runs its own
dockerd (experimental: true) with CRIU inside. Nothing touches the user's sessions, volumes,
~/.config/raven or ~/Downloads.

  python3 tests/checkpoint_lab/run_lab.py [--keep] [--no-apparmor] [--rebuild] [--out DIR]

Created on the server, and removed at the end unless --keep:
- container raven-lab-m11-<random> (label raven.lab=m11; privileged; 6g memory, 4 CPUs, 4096 pids;
  no published ports; /run on tmpfs, so a lab restart clears it like a reboot)
- volume raven-lab-m11-<random>-docker for the lab's /var/lib/docker (label raven.lab=m11)
- directory ~/.cache/raven-lab-m11-<random>/ (stands in for ~/Downloads, which is NFS on the server)
Kept: the image raven-lab-m11:latest (tests/checkpoint_lab/Dockerfile). raven:latest and
raven-core:latest are copied into the lab with docker save | docker load.

Steps: build the lab image, start the lab, load the images, phase 1 (rungs 1-4 and extra checks,
leaving checkpoints on disk), restart the lab as a reboot, phase 2 (restore after the reboot).
"""

import argparse
import json
import os
from pathlib import Path
import secrets
import shutil
import subprocess
import sys
import tempfile
import time

HERE = Path(__file__).resolve().parent
SOURCE = HERE.parents[1]
IMAGE = "raven-lab-m11:latest"
LADDER = "/src/vdesk/tests/checkpoint_lab/ladder.py"


def run(command, check=True, timeout=600, **options):
    result = subprocess.run(command, capture_output=True, text=True, timeout=timeout, **options)
    if check and result.returncode:
        raise RuntimeError(f"{' '.join(command)} failed: {result.stderr.strip()[-1000:]}")
    return result


def wait_dockerd(lab, seconds=90):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if run(["docker", "exec", lab, "docker", "info"], check=False, timeout=30).returncode == 0:
            return
        time.sleep(1)
    raise RuntimeError("the lab's dockerd did not come up; see: docker logs " + lab)


def results(ladder):
    """One row per rung: the last dump and restore that worked, or the first error."""
    rows = []
    for rung in ladder.get("rungs", []):
        attempts = rung.get("attempts", [])
        dump = next((a for a in reversed(attempts) if a["step"].endswith("dump") and a["ok"]), None)
        back = next((a for a in reversed(attempts) if a["step"].endswith("restore") and a["ok"]), None)
        failed = next((a for a in attempts if not a["ok"]), None)
        rows.append({
            "rung": rung.get("rung"), "name": rung.get("name"), "ok": rung.get("ok", False),
            "bytes": dump and dump.get("bytes"), "dump_seconds": dump and dump["seconds"],
            "restore_seconds": back and back["seconds"], "dump_options": dump and dump["options"],
            "restore_options": back and back["options"],
            "first_failure": failed and {"step": failed["step"],
                                         "error": (failed.get("criu_errors") or [failed.get("error")])[0]},
            "exception": rung.get("exception"),
        })
    boot = ladder.get("reboot", {})
    rows.append({"rung": "reboot", "name": "restore after the lab restarted",
                 "ok": bool(boot.get("checks", {}).get("ok")), "bytes": boot.get("dump", {}).get("bytes"),
                 "dump_seconds": boot.get("dump", {}).get("seconds"),
                 "restore_seconds": boot.get("restore", {}).get("seconds"),
                 "dump_options": boot.get("dump", {}).get("options"),
                 "restore_options": boot.get("restore", {}).get("options"), "exception": boot.get("exception")})
    return rows


def main():
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--keep", action="store_true", help="leave the lab container and volume for debugging")
    parser.add_argument("--no-apparmor", action="store_true", help="run inner containers without AppArmor")
    parser.add_argument("--rebuild", action="store_true", help="rebuild the lab image")
    parser.add_argument("--out", help="directory for the summary and CRIU logs (default: a new temp dir)")
    arguments = parser.parse_args()

    name = f"raven-lab-m11-{secrets.token_hex(3)}"
    volume = f"{name}-docker"
    nfs = Path.home() / ".cache" / name
    out = Path(arguments.out or tempfile.mkdtemp(prefix=f"{name}-"))
    out.mkdir(parents=True, exist_ok=True)
    summary = {"lab": name, "out": str(out), "host": {}, "timings": {}}
    created = []
    try:
        summary["host"] = {
            "kernel": os.uname().release,
            "docker": run(["docker", "version", "-f", "{{.Server.Version}}"]).stdout.strip(),
            "raven_image": run(["docker", "image", "inspect", "-f", "{{.Id}} {{.Created}}", "raven:latest"]).stdout.strip(),
            "raven_core_image": run(["docker", "image", "inspect", "-f", "{{.Id}}", "raven-core:latest"]).stdout.strip(),
            "repo": run(["git", "-C", str(SOURCE), "rev-parse", "--short", "HEAD"]).stdout.strip(),
        }
        if arguments.rebuild or run(["docker", "image", "inspect", IMAGE], check=False).returncode:
            started = time.monotonic()
            with open(out / "build.log", "w") as build_log:
                subprocess.run(["docker", "build", "-t", IMAGE, str(HERE)], stdout=build_log, stderr=build_log,
                               check=True, timeout=1800)
            summary["timings"]["build_seconds"] = round(time.monotonic() - started, 1)
        summary["host"]["lab_image"] = run(["docker", "image", "inspect", "-f", "{{.Id}}", IMAGE]).stdout.strip()

        nfs.mkdir(parents=True)
        os.chmod(nfs, 0o755)
        (nfs / "Downloads").mkdir()
        os.chmod(nfs / "Downloads", 0o2775)
        created.append(("dir", nfs))
        run(["docker", "volume", "create", "--label", "raven.lab=m11", volume])
        created.append(("volume", volume))
        run(["docker", "run", "-d", "--name", name, "--label", "raven.lab=m11", "--privileged", "--init",
             "--memory", "6g", "--memory-swap", "6g", "--cpus", "4", "--pids-limit", "4096",
             "--tmpfs", "/run:mode=755,exec",
             "-e", f"LAB_APPARMOR={0 if arguments.no_apparmor else 1}",
             "-e", f"LAB_UID={os.getuid()}", "-e", f"LAB_GID={os.getgid()}", "-e", "LAB_RAVEN_HOME=/labnfs",
             "-v", f"{volume}:/var/lib/docker", "-v", f"{SOURCE}:/src/vdesk:ro", "-v", f"{nfs}:/labnfs",
             IMAGE])
        created.append(("container", name))
        wait_dockerd(name)
        summary["lab_log_start"] = run(["docker", "logs", name], check=False).stderr.splitlines()[:3]

        started = time.monotonic()
        save = subprocess.Popen(["docker", "save", "raven:latest", "raven-core:latest"], stdout=subprocess.PIPE)
        run(["docker", "exec", "-i", name, "docker", "load"], stdin=save.stdout, timeout=1800)
        save.stdout.close()
        if save.wait():
            raise RuntimeError("docker save failed")
        summary["timings"]["image_load_seconds"] = round(time.monotonic() - started, 1)

        started = time.monotonic()
        with open(out / "phase1.log", "w") as phase_log:
            subprocess.run(["docker", "exec", name, "python3", LADDER, "phase1"], stdout=phase_log,
                           stderr=phase_log, check=True, timeout=3600)
        summary["timings"]["phase1_seconds"] = round(time.monotonic() - started, 1)

        # Reboot: every process in the lab ends, /run (tmpfs) is cleared, /var/lib/docker stays
        started = time.monotonic()
        run(["docker", "restart", "-t", "60", name])
        wait_dockerd(name)
        summary["timings"]["reboot_seconds"] = round(time.monotonic() - started, 1)

        started = time.monotonic()
        with open(out / "phase2.log", "w") as phase_log:
            phase2 = subprocess.run(["docker", "exec", name, "python3", LADDER, "phase2"], stdout=subprocess.PIPE,
                                    stderr=phase_log, text=True, check=True, timeout=1800)
        summary["timings"]["phase2_seconds"] = round(time.monotonic() - started, 1)
        summary["ladder"] = json.loads(phase2.stdout)
        summary["results"] = results(summary["ladder"])
    except Exception as error:
        summary["error"] = f"{type(error).__name__}: {error}"
        if any(kind == "container" for kind, _ in created):
            state = run(["docker", "exec", name, "cat", "/var/lib/docker/raven-lab/state.json"], check=False)
            if state.returncode == 0:
                summary["ladder"] = json.loads(state.stdout)
    finally:
        if any(kind == "container" for kind, _ in created):
            run(["docker", "cp", f"{name}:/var/lib/docker/raven-lab/criu-logs", str(out / "criu-logs")], check=False)
            (out / "lab.log").write_text(run(["docker", "logs", name], check=False).stderr)
        summary["cleanup"] = {}
        if not arguments.keep:
            for kind, item in reversed(created):
                if kind == "container":
                    result = run(["docker", "rm", "-f", "-v", item], check=False)
                elif kind == "volume":
                    result = run(["docker", "volume", "rm", item], check=False)
                else:
                    # Files the session user (60000) made are group-writable in the setgid directory
                    shutil.rmtree(item, ignore_errors=True)
                    result = subprocess.CompletedProcess([], 0 if not Path(item).exists() else 1, "", "")
                summary["cleanup"][f"{kind} {item}"] = "removed" if result.returncode == 0 else result.stderr.strip()
            left = run(["docker", "ps", "-a", "-q", "--filter", "label=raven.lab=m11"]).stdout.split()
            left += run(["docker", "volume", "ls", "-q", "--filter", "label=raven.lab=m11"]).stdout.split()
            summary["cleanup"]["left_with_label"] = left
        else:
            summary["cleanup"]["kept"] = [f"{kind} {item}" for kind, item in created]
        summary["cleanup"]["kept_image"] = IMAGE
        (out / "summary.json").write_text(json.dumps(summary, indent=1, default=str))
    print(json.dumps(summary, indent=1, default=str))
    return 1 if "error" in summary else 0


if __name__ == "__main__":
    sys.exit(main())
