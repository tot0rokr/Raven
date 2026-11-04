"""Check the gateway's limits before running a disposable cgroup probe."""

import json
import os
from pathlib import Path
import runpy
import secrets
import subprocess
import sys

SOURCE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SOURCE))
EXPECTED_MEMORY = 1024 * 1024 * 1024
EXPECTED_CPUS = 1_000_000_000
EXPECTED_PIDS = 256
COMMAND_TIMEOUT = 60
PROBE = """
import json
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import threading
from django.contrib.auth.hashers import Argon2PasswordHasher
root = Path('/sys/fs/cgroup')
hasher = Argon2PasswordHasher()
attempts = 5
barrier = threading.Barrier(attempts)
def verify_budget(index):
    barrier.wait()
    return hasher.encode('resource-probe', hasher.salt()).startswith('argon2$argon2id$')
with ThreadPoolExecutor(max_workers=attempts) as workers:
    assert all(workers.map(verify_budget, range(attempts)))
observed = {name: (root / name).read_text().strip()
            for name in ('memory.max', 'memory.swap.max', 'cpu.max', 'pids.max', 'memory.peak')}
print(json.dumps(observed))
"""


def command(*args, check=True):
    return subprocess.run(["docker", *args], text=True, capture_output=True,
                          timeout=COMMAND_TIMEOUT, check=check)


def main():
    os.environ["RAVEN_WEB_INSTANCE"] = "limits-" + secrets.token_hex(4)
    manager = runpy.run_path(str(SOURCE / "raven-web"))
    name, volume = manager["NAME"], manager["VOLUME"]
    try:
        command("volume", "create", "--label", f"raven.web.uid={os.getuid()}", volume)
        command("create", "--name", name, "--network", "none",
                "--label", f"raven.web.uid={os.getuid()}", *manager["container_args"](),
                manager["IMAGE"], "python", "-c", PROBE)
        item = json.loads(command("inspect", name).stdout)[0]
        settings = item["HostConfig"]
        assert settings["Memory"] == EXPECTED_MEMORY, settings["Memory"]
        assert settings["MemorySwap"] == EXPECTED_MEMORY, settings["MemorySwap"]
        assert settings["NanoCpus"] == EXPECTED_CPUS, settings["NanoCpus"]
        assert settings["PidsLimit"] == EXPECTED_PIDS, settings["PidsLimit"]
        result = command("start", "--attach", name, check=False)
        if result.returncode:
            state = json.loads(command("inspect", name).stdout)[0]["State"]
            raise AssertionError({"probe_exit": result.returncode, "oom_killed": state["OOMKilled"],
                                  "stderr": result.stderr})
        observed = json.loads(result.stdout)
        assert observed["memory.max"] == str(EXPECTED_MEMORY), observed
        assert observed["memory.swap.max"] == "0", observed
        quota, period = map(int, observed["cpu.max"].split())
        assert quota == period, observed
        assert observed["pids.max"] == str(EXPECTED_PIDS), observed
        print(json.dumps({"resource_limits": "passed", "cgroup": observed}))
    finally:
        command("rm", "-f", name, check=False)
        command("volume", "rm", volume, check=False)


if __name__ == "__main__":
    main()
