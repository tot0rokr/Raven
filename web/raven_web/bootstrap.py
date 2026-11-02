import argparse
import datetime
import ipaddress
import json
import os
import secrets
from pathlib import Path

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import NameOID

from .config import parse_origin


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
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    hostname = origin.hostname
    try:
        alternative_name = x509.IPAddress(ipaddress.ip_address(hostname))
    except ValueError:
        alternative_name = x509.DNSName(hostname)
    subject = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, hostname)])
    now = datetime.datetime.now(datetime.timezone.utc)
    certificate = (
        x509.CertificateBuilder()
        .subject_name(subject)
        .issuer_name(subject)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - datetime.timedelta(minutes=5))
        .not_valid_after(now + datetime.timedelta(days=365))
        .add_extension(x509.SubjectAlternativeName([alternative_name]), critical=False)
        .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
        .sign(key, hashes.SHA256())
    )
    _create_private(
        key_file,
        key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        ),
    )
    _create_private(certificate_file, certificate.public_bytes(serialization.Encoding.PEM))


def _create_private(path, data):
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "wb") as output:
        output.write(data)
