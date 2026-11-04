"""Run noVNC module-loading regressions with image assets and mocked HTTP APIs."""

import argparse
import json
import os
from pathlib import Path
import secrets
import subprocess
import tempfile

SOURCE = Path(__file__).resolve().parents[1]
RENDER = """
import django, json
from django.conf import settings
settings.configure(INSTALLED_APPS=['django.contrib.staticfiles'], STATIC_URL='/static/',
                   TEMPLATES=[{'BACKEND': 'django.template.backends.django.DjangoTemplates',
                               'DIRS': ['/source/web/templates']}])
django.setup()
from django.template.loader import render_to_string
print(json.dumps({'html': render_to_string('index.html', {'username': 'preview', 'csrf_token': 'ui-fixture'})}))
"""


def run(arguments, **kwargs):
    return subprocess.run(arguments, capture_output=True, check=True, timeout=120, **kwargs)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--baseline", action="store_true")
    parser.add_argument("--loader-timeout-only", action="store_true")
    parser.add_argument("--image", default=os.environ.get("RAVEN_WEB_IMAGE", "raven-web:latest"))
    parser.add_argument("--browser-image", default="headless-browser:1.63.0")
    args = parser.parse_args()
    if args.baseline and args.loader_timeout_only:
        parser.error("--baseline and --loader-timeout-only cannot be combined")
    scratch = Path(tempfile.mkdtemp(prefix="raven-module-loading-"))
    fixture = "raven-module-fixture-" + secrets.token_hex(4)
    identity = f"{os.getuid()}:{os.getgid()}"
    limits = ["--memory", "1g", "--memory-swap", "1g", "--cpus", "1", "--pids-limit", "128"]
    run(["docker", "create", "--name", fixture, "--network", "none", *limits, "--user", identity,
         "--read-only", "--cap-drop", "ALL", "--security-opt", "no-new-privileges", args.image, "true"])
    try:
        run(["docker", "cp", f"{fixture}:/app/static", str(scratch / "static")])
    finally:
        run(["docker", "container", "rm", fixture])
    vendor_name = "novnc" if (scratch / "static/novnc").is_dir() else "novnc-1.7.0-raven1"
    rendered = run(["docker", "run", "--rm", "--network", "none", *limits, "--user", identity,
                    "--read-only", "--cap-drop", "ALL", "--security-opt", "no-new-privileges",
                    "--mount", f"type=bind,src={SOURCE},dst=/source,readonly", args.image, "python", "-c", RENDER])
    options = {**json.loads(rendered.stdout), "baseline": args.baseline,
               "loader_timeout_only": args.loader_timeout_only,
               "vendor": "/fixture/static/" + vendor_name, "output": "/output"}
    print(f"Module-loading fixture artifacts: {scratch}", flush=True)
    browser = run(["docker", "run", "--rm", "-i", "--init", "--network", "none", *limits,
                   "--user", identity, "--read-only", "--cap-drop", "ALL",
                   "--security-opt", "seccomp=unconfined", "--shm-size", "256m", "--env", "HOME=/tmp",
                   "--tmpfs", "/tmp:rw,nosuid,nodev,size=256m",
                   "--mount", f"type=bind,src={SOURCE},dst=/source,readonly",
                   "--mount", f"type=bind,src={scratch},dst=/fixture,readonly",
                   "--mount", f"type=bind,src={scratch},dst=/output",
                   args.browser_image, "python3", "/source/tests/web_module_loading_browser.py"],
                  input=json.dumps(options).encode())
    print(browser.stdout.decode(), end="")


if __name__ == "__main__":
    try:
        main()
    except subprocess.CalledProcessError as error:
        print(error.stdout.decode(errors="replace"), end="")
        print(error.stderr.decode(errors="replace"), end="")
        raise SystemExit(1)
