"""Check the systemd-supervised web service on an isolated install: crash, simulated reboot, stale runtime,
start limit, stop and disable. Units go to the user manager's runtime directory so they never persist."""

import json
import os
from pathlib import Path
import secrets
import shutil
import signal
import subprocess
import tempfile
import time

from web_config_e2e import READY_TIMEOUT, allocate_ports, cleanup, cli, https_status, inspect, run
from web_e2e import COMMAND_TIMEOUT, SOURCE, WEB_IMAGE, provision
from raven_web_host import Raven

MANAGER = {"XDG_RUNTIME_DIR": f"/run/user/{os.getuid()}"}
UNIT_DIR = Path(f"/run/user/{os.getuid()}/systemd/user")


def systemctl(*arguments):
    return subprocess.run(["systemctl", "--user", *arguments], capture_output=True, text=True,
                          env={**os.environ, **MANAGER}, timeout=COMMAND_TIMEOUT)


def unit_state(unit):
    output = systemctl("show", unit, "--property=ActiveState", "--property=MainPID", "--property=NRestarts",
                       "--property=UnitFileState", "--property=Result").stdout
    return dict(line.split("=", 1) for line in output.splitlines() if "=" in line)


def wait_for(predicate, message, timeout=READY_TIMEOUT * 3):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        value = predicate()
        if value:
            return value
        time.sleep(0.3)
    raise AssertionError(message)


def exited(pid):
    """True once the process is gone or only a zombie waiting for an unrelated parent to reap it."""
    try:
        return Path(f"/proc/{pid}/status").read_text().split("State:", 1)[1].split()[0] == "Z"
    except FileNotFoundError:
        return True


def doctor(environment):
    result = subprocess.run([str(SOURCE / "raven"), "web", "doctor"], env=environment, capture_output=True,
                            text=True, timeout=COMMAND_TIMEOUT)
    return result.returncode, result.stdout


def desktops(owner):
    names = run(["docker", "ps", "-aq", "--filter", f"label=raven.user={owner}"], os.environ).stdout.split()
    return {name: inspect(os.environ, name.decode())["State"]["StartedAt"] for name in names}


def main():
    scratch = Path(tempfile.mkdtemp(prefix="raven-web-autostart-e2e-"))
    instance = "autostart-" + secrets.token_hex(4)
    environment = dict(os.environ)
    environment.pop("DISPLAY", None)
    for variable, directory in (("HOME", "home"), ("XDG_CONFIG_HOME", "config"),
                                ("XDG_STATE_HOME", "state"), ("XDG_RUNTIME_DIR", "runtime")):
        path = scratch / directory
        path.mkdir(mode=0o700)
        environment[variable] = str(path)
    environment.update(RAVEN_WEB_IMAGE=WEB_IMAGE, RAVEN_WEB_INSTANCE=instance, RAVEN_WEB_UNIT_DIR=str(UNIT_DIR))
    owner = Raven(SOURCE / "raven").user
    gateway = f"raven-{owner}-{instance}"
    unit = f"raven-{instance}.service"
    runtime = Path(environment["XDG_RUNTIME_DIR"]) / f"raven-web-{os.getuid()}-{instance}"
    browser_port, server_port = allocate_ports(2)
    origin = f"https://localhost:{browser_port}"
    sessions_before = desktops(owner)
    try:
        provision(environment, origin, secrets.token_urlsafe(32))
        cli(environment, "start", "--port", str(server_port))
        certificate = cli(environment, "certificate")
        detached = json.loads(subprocess.run(
            ["python3", "-c", "import json,socket,sys; s=socket.socket(socket.AF_UNIX); s.connect(sys.argv[1]); "
             "s.sendall(b'{\"action\":\"ping\"}\\n'); print(s.makefile().readline())", str(runtime / "broker.sock")],
            capture_output=True, text=True, check=True).stdout)["data"]["pid"]

        print("Enabling the unit replaces the detached broker", flush=True)
        output = cli(environment, "enable")
        assert f"Autostart enabled: {unit}" in output, output
        assert (UNIT_DIR / unit).is_file()
        state = unit_state(unit)
        assert state["ActiveState"] == "active" and state["UnitFileState"].startswith("enabled"), state
        assert state["MainPID"] != str(detached)
        wait_for(lambda: exited(detached), "The detached broker is still running")
        status = cli(environment, "status")
        assert f"running, pid {state['MainPID']} (systemd)" in status, status
        assert "  Autostart enabled" in status, status
        code, report = doctor(environment)
        assert code == 0 and "Gateway->broker  reachable" in report, report

        print("A killed broker is restarted by systemd", flush=True)
        first = int(state["MainPID"])
        os.kill(first, signal.SIGKILL)
        wait_for(lambda: unit_state(unit)["MainPID"] not in ("0", str(first))
                 and unit_state(unit)["ActiveState"] == "active", "systemd did not restart the broker")
        assert int(unit_state(unit)["NRestarts"]) >= 1
        wait_for(lambda: doctor(environment)[0] == 0, "doctor did not recover after the restart")

        print("A simulated reboot loses the runtime directory; starting the unit recreates the gateway", flush=True)
        systemctl("stop", unit)
        run(["docker", "container", "stop", "--time", "10", gateway], environment)
        shutil.rmtree(runtime)
        start = subprocess.run(["docker", "container", "start", gateway], capture_output=True, text=True)
        assert start.returncode != 0 and not inspect(environment, gateway)["State"]["Running"], start
        assert systemctl("start", unit).returncode == 0
        wait_for(lambda: unit_state(unit)["ActiveState"] == "active" and (runtime / "broker.sock").exists(),
                 "the unit did not come back")
        wait_for(lambda: https_status(server_port, origin, certificate) == 200, "HTTPS did not come back")
        code, report = doctor(environment)
        assert code == 0 and "Gateway->broker  reachable" in report, report

        print("A replaced runtime directory under a running gateway is detected", flush=True)
        systemctl("stop", unit)
        old_gateway = inspect(environment, gateway)["Id"]
        runtime.rename(scratch / "replaced-runtime")
        assert systemctl("start", unit).returncode == 0
        wait_for(lambda: unit_state(unit)["ActiveState"] == "active" and (runtime / "broker.sock").exists(),
                 "the unit did not come back")
        assert inspect(environment, gateway)["Id"] != old_gateway, "The stale gateway was not recreated"
        code, report = doctor(environment)
        assert code == 0 and "Gateway->broker  reachable" in report, report

        print("Repeated crashes stop at the start limit; raven web start recovers", flush=True)
        for _ in range(8):
            current = unit_state(unit)
            if current["ActiveState"] == "failed":
                break
            if current["MainPID"] != "0":
                os.kill(int(current["MainPID"]), signal.SIGKILL)
            time.sleep(4)
        assert unit_state(unit)["ActiveState"] == "failed", unit_state(unit)
        code, report = doctor(environment)
        assert code == 1 and f"fail  Autostart        {unit} failed" in report, report
        cli(environment, "start")
        assert unit_state(unit)["ActiveState"] == "active"
        code, report = doctor(environment)
        assert code == 0, report

        print("stop stops the unit; start brings it back", flush=True)
        output = cli(environment, "stop")
        assert "Autostart is still enabled" in output, output
        assert unit_state(unit)["ActiveState"] == "inactive"
        assert not inspect(environment, gateway)["State"]["Running"]
        cli(environment, "start")
        assert unit_state(unit)["ActiveState"] == "active"
        assert https_status(server_port, origin, certificate) == 200

        print("disable removes the unit and keeps the gateway serving", flush=True)
        output = cli(environment, "disable")
        assert "keeps running with a broker outside systemd" in output, output
        assert not (UNIT_DIR / unit).exists()
        assert unit_state(unit).get("UnitFileState", "") == ""
        code, report = doctor(environment)
        assert code == 0 and "ok    Autostart        off" in report and "Gateway->broker  reachable" in report, report
        assert desktops(owner) == sessions_before, "A desktop session was started, stopped or restarted"
        print(json.dumps({"autostart_e2e": "passed", "enable": "checked", "crash_restart": "checked",
                          "simulated_reboot": "checked", "stale_runtime": "checked", "start_limit": "checked",
                          "stop_start": "checked", "disable": "checked", "desktops_untouched": "checked"}))
    finally:
        subprocess.run([str(SOURCE / "raven"), "web", "disable"], env=environment, capture_output=True,
                       timeout=COMMAND_TIMEOUT)
        (UNIT_DIR / unit).unlink(missing_ok=True)
        systemctl("daemon-reload")
        systemctl("reset-failed", unit)
        cleanup(environment, gateway, f"raven-web:absent-{instance}")


if __name__ == "__main__":
    main()
