import argparse
import datetime
import fcntl
import hashlib
import ipaddress
import json
import os
import re
import secrets
import sys
import tempfile
from pathlib import Path

from cryptography import x509

from raven_web.config import parse_origin

CONFIG_KEYS = {"origin", "port", "listen", "ssh_target", "ssh_port"}
REVISION = re.compile(r"[0-9a-f]{64}\Z")
SSH_NAME = re.compile(r"[A-Za-z0-9_][A-Za-z0-9_.-]*\Z")
DNS_LABEL = re.compile(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\Z")
NUMERIC_HOST = re.compile(r"(?:[0-9]+|0x[0-9a-f]*)\Z")
MAX_CONFIG_BYTES = 64 * 1024


class ConfigurationError(ValueError):
    pass


def _read_file(path):
    try:
        descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
        with os.fdopen(descriptor, "rb") as source:
            return source.read()
    except FileNotFoundError as error:
        raise ConfigurationError(f"Missing {path.name}; initialize the Raven web installation first") from error


def _snapshot(state):
    raw = _read_file(state / "config.json")
    if not raw or len(raw) > MAX_CONFIG_BYTES:
        raise ConfigurationError("config.json is empty or exceeds 64 KiB")
    try:
        config = json.loads(raw)
    except (ValueError, UnicodeDecodeError) as error:
        raise ConfigurationError("config.json is not valid JSON") from error
    if not isinstance(config, dict) or not isinstance(config.get("origin"), str):
        raise ConfigurationError("config.json must contain an origin string")
    certificate_bytes = _read_file(state / "tls.crt")
    try:
        certificate = certificate_bytes.decode("ascii")
        x509.load_pem_x509_certificate(certificate.encode("ascii"))
    except (ValueError, UnicodeDecodeError) as error:
        raise ConfigurationError("tls.crt is not a valid PEM certificate") from error
    return raw, {"config": config, "revision": hashlib.sha256(raw).hexdigest(), "certificate": certificate}


def _origin(value):
    if not isinstance(value, str):
        raise ConfigurationError("origin must be an HTTPS origin string")
    try:
        parsed = parse_origin(value)
    except (ValueError, UnicodeError) as error:
        raise ConfigurationError("origin must be an HTTPS origin without credentials, path or query") from error
    canonical_error = "Use a canonical HTTPS origin with a DNS hostname or canonical IP address; omit default port 443"
    hostname = parsed.hostname
    if "%" in hostname:
        raise ConfigurationError(canonical_error)
    try:
        address = ipaddress.ip_address(hostname)
    except ValueError:
        labels = hostname.split(".")
        if len(hostname) > 253 or not all(DNS_LABEL.fullmatch(label) for label in labels) or NUMERIC_HOST.fullmatch(labels[-1]):
            raise ConfigurationError(canonical_error)
        host = hostname
    else:
        host = f"[{address.compressed}]" if address.version == 6 else str(address)
    port = parsed.port
    canonical = "https://" + host + (f":{port}" if port is not None and port != 443 else "")
    if port == 0 or value != canonical:
        raise ConfigurationError(canonical_error)
    return parsed


def _port(value, minimum, name):
    if type(value) is not int or not minimum <= value <= 65535:
        raise ConfigurationError(f"{name} must be an integer between {minimum} and 65535")


def _ssh_target(value):
    if value is None:
        return
    if not isinstance(value, str) or value.count("@") > 1:
        raise ConfigurationError("ssh_target must be a safe [user@]host or SSH alias")
    host = value
    if "@" in value:
        user, host = value.split("@")
        if not SSH_NAME.fullmatch(user):
            raise ConfigurationError("ssh_target contains an invalid username")
    if "%" in host:
        raise ConfigurationError("Scoped IPv6 SSH targets are not supported")
    if ":" in host:
        try:
            ipaddress.IPv6Address(host[1:-1] if host.startswith("[") and host.endswith("]") else host)
        except ValueError as error:
            raise ConfigurationError("ssh_target contains an invalid IPv6 address") from error
    elif not SSH_NAME.fullmatch(host):
        raise ConfigurationError("ssh_target must not contain options, whitespace or shell syntax")


def _matches_certificate(hostname, pem):
    certificate = x509.load_pem_x509_certificate(pem.encode("ascii"))
    try:
        names = certificate.extensions.get_extension_for_class(x509.SubjectAlternativeName).value
    except x509.ExtensionNotFound as error:
        raise ConfigurationError("The TLS certificate has no subject alternative names") from error
    try:
        address = ipaddress.ip_address(hostname)
    except ValueError:
        addresses = names.get_values_for_type(x509.DNSName)
        if any("*" not in name and name.lower() == hostname.lower() for name in addresses):
            return
    else:
        if address in names.get_values_for_type(x509.IPAddress):
            return
    raise ConfigurationError("The origin hostname must match an exact DNS or IP certificate SAN; wildcards are unsupported")


def _validate(payload, snapshot):
    if not isinstance(payload, dict) or set(payload) != {"config", "revision"}:
        raise ConfigurationError("Expected an object containing config and revision")
    revision = payload["revision"]
    if not isinstance(revision, str) or not REVISION.fullmatch(revision):
        raise ConfigurationError("revision must be a lowercase SHA-256 digest")
    if revision != snapshot["revision"]:
        raise ConfigurationError("Configuration changed; read it again before retrying")
    config = payload["config"]
    if not isinstance(config, dict) or not {"origin", "listen", "port"} <= config.keys():
        raise ConfigurationError("The complete config must include origin, listen and port")
    current = snapshot["config"]
    if set(config) - CONFIG_KEYS - set(current):
        raise ConfigurationError("Unrecognized new configuration keys")
    for key in current.keys() - CONFIG_KEYS:
        if key not in config or config[key] != current[key]:
            raise ConfigurationError("Existing unrecognized configuration keys must be preserved")
    origin = _origin(config["origin"])
    _port(config["port"], 1024, "port")
    listen = config["listen"]
    if not isinstance(listen, str) or "%" in listen:
        raise ConfigurationError("listen must be an IPv4 or IPv6 address without a scope ID")
    try:
        ipaddress.ip_address(listen)
    except ValueError as error:
        raise ConfigurationError("listen must be an IPv4 or IPv6 address") from error
    if "ssh_target" in config:
        _ssh_target(config["ssh_target"])
    if "ssh_port" in config:
        _port(config["ssh_port"], 1, "ssh_port")
    _matches_certificate(origin.hostname, snapshot["certificate"])
    try:
        encoded = (json.dumps(config, indent=2, sort_keys=True, allow_nan=False) + "\n").encode()
    except ValueError as error:
        raise ConfigurationError("Configuration values must be valid JSON") from error
    if len(encoded) > MAX_CONFIG_BYTES:
        raise ConfigurationError("Configuration exceeds 64 KiB")
    return config, encoded


def _write(state, raw, encoded, revision, directory):
    timestamp = datetime.datetime.now(datetime.timezone.utc).strftime("%Y%m%d-%H%M%S")
    backup = state / f"config.json.bak.configure-v1.{timestamp}-{secrets.token_hex(4)}"
    descriptor = os.open(backup, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "wb") as output:
        os.fchmod(output.fileno(), 0o600)
        output.write(raw)
        output.flush()
        os.fsync(output.fileno())
    if not backup.stat().st_size or _read_file(backup) != raw:
        raise ConfigurationError("Backup verification failed; the original configuration was not replaced")
    os.fsync(directory)
    descriptor, name = tempfile.mkstemp(prefix=".config.json.configure-v1.", dir=state)
    temporary = Path(name)
    try:
        with os.fdopen(descriptor, "wb") as output:
            output.write(encoded)
            output.flush()
            os.fsync(output.fileno())
        if hashlib.sha256(_read_file(state / "config.json")).hexdigest() != revision:
            raise ConfigurationError("Configuration changed; the original configuration was not replaced")
        os.replace(temporary, state / "config.json")
        temporary = None
        warning = None
        try:
            os.fsync(directory)
        except OSError:
            warning = "Configuration was replaced, but the directory fsync failed; verify storage before restarting"
    finally:
        if temporary is not None:
            temporary.unlink()
    return str(backup), warning


def execute(action, payload=None, state=None):
    state = Path(state if state is not None else os.environ.get("RAVEN_WEB_STATE", "/data"))
    if action in ("read", "validate"):
        _, snapshot = _snapshot(state)
        if action == "validate":
            config, _ = _validate(payload, snapshot)
            return {**snapshot, "config": config, "backup": None}
        return snapshot
    if action != "write":
        raise ConfigurationError("Expected read, validate or write")
    directory = os.open(state, os.O_RDONLY | os.O_DIRECTORY)
    try:
        fcntl.flock(directory, fcntl.LOCK_EX)
        raw, snapshot = _snapshot(state)
        config, encoded = _validate(payload, snapshot)
        if config == snapshot["config"]:
            return {**snapshot, "backup": None}
        backup, warning = _write(state, raw, encoded, snapshot["revision"], directory)
        result = {"config": config, "revision": hashlib.sha256(encoded).hexdigest(),
                  "certificate": snapshot["certificate"], "backup": backup}
        if warning is not None:
            result["warning"] = warning
        return result
    finally:
        os.close(directory)


def main():
    parser = argparse.ArgumentParser(description="Read, validate or atomically update Raven web configuration")
    parser.add_argument("action", choices=("read", "validate", "write"))
    arguments = parser.parse_args()
    try:
        payload = None
        if arguments.action != "read":
            data = sys.stdin.read(MAX_CONFIG_BYTES + 1)
            if len(data) > MAX_CONFIG_BYTES:
                raise ConfigurationError("Configuration request exceeds 64 KiB")
            payload = json.loads(data)
        print(json.dumps(execute(arguments.action, payload), allow_nan=False))
    except (OSError, ValueError) as error:
        print(f"Raven configuration: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
