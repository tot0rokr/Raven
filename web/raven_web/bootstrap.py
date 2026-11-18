import argparse
import json
import os
import secrets
from pathlib import Path

from .config import parse_origin
from .service_config import DEFAULT_DAYS, self_signed


def initialize_directory(arguments):
    parser = argparse.ArgumentParser()
    parser.add_argument("--uid", required=True, type=int)
    parser.add_argument("--gid", required=True, type=int)
    options = parser.parse_args(arguments)
    if options.uid <= 0 or options.gid < 0:
        parser.error("A non-root UID and a nonnegative GID are required")
    state = Path(os.environ.get("RAVEN_WEB_STATE", "/data"))
    state.mkdir(mode=0o700, parents=True, exist_ok=True)
    if any(state.iterdir()) and state.stat().st_uid != options.uid:
        raise ValueError("Refusing to change ownership of an existing installation")
    state.chmod(0o700)
    os.chown(state, options.uid, options.gid)


def initialize(arguments):
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument("--origin", required=True)
    options, _ = parser.parse_known_args(arguments)
    origin = parse_origin(options.origin)
    state = Path(os.environ.get("RAVEN_WEB_STATE", "/data"))
    state.mkdir(mode=0o700, parents=True, exist_ok=True)
    state.chmod(0o700)
    config_file = state / "config.json"
    if config_file.exists():
        config = json.loads(config_file.read_text())
        if config["origin"] != options.origin:
            raise ValueError("The origin differs from this installation's configured origin")
    else:
        _create_private(config_file, json.dumps({"origin": options.origin}).encode())
    if not (state / "secret.key").exists():
        _create_private(state / "secret.key", secrets.token_urlsafe(64).encode())
    key_file = state / "tls.key"
    certificate_file = state / "tls.crt"
    if key_file.exists() and certificate_file.exists():
        return
    if key_file.exists() or certificate_file.exists():
        raise ValueError("Incomplete TLS configuration; restore the matching certificate and key")
    certificate, key = self_signed([origin.hostname], DEFAULT_DAYS)
    _create_private(key_file, key)
    _create_private(certificate_file, certificate)


def _create_private(path, data):
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "wb") as output:
        output.write(data)
