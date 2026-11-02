"""Run management UI component checks with isolated Docker fixtures."""

import json
import os
from pathlib import Path
import subprocess
import tempfile

source = Path(__file__).resolve().parents[1]
output = Path(tempfile.mkdtemp(prefix="raven-web-ui-"))
web_image = os.environ.get("RAVEN_WEB_IMAGE", "raven-web:dev")
browser_image = os.environ.get("RAVEN_WEB_BROWSER_IMAGE", "headless-browser:1.63.0")
render = """
import os
os.environ['DJANGO_SETTINGS_MODULE'] = 'raven_web.test_settings'
import django
django.setup()
from django.template.loader import render_to_string
print(render_to_string('index.html', {'username': 'preview', 'csrf_token': 'ui-fixture'}))
"""
page = subprocess.run(
    ["docker", "run", "--rm", "--network", "none", "--read-only", "--user", f"{os.getuid()}:{os.getgid()}",
     "--tmpfs", "/tmp:rw,nosuid,nodev,size=64m", web_image, "python", "-c", render],
    capture_output=True, check=True, timeout=60,
)
result = subprocess.run(
    ["docker", "run", "--rm", "-i", "--init", "--network", "none", "--read-only",
     "--user", f"{os.getuid()}:{os.getgid()}", "--security-opt", "seccomp=unconfined",
     "--shm-size", "1g", "--env", "HOME=/tmp", "--tmpfs", "/tmp:rw,nosuid,nodev,size=512m",
     "--mount", f"type=bind,src={source},dst=/source,readonly",
     "--mount", f"type=bind,src={output},dst=/output", browser_image,
     "python3", "/source/tests/web_management_ui.py"],
    input=json.dumps({"html": page.stdout.decode(), "output": "/output"}).encode(), timeout=120,
)
print(f"UI artifacts: {output}")
result.check_returncode()
