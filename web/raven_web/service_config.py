import argparse
import base64
import binascii
from contextlib import closing
import datetime
import fcntl
import hashlib
import io
import ipaddress
import json
import os
import re
import secrets
import shutil
import sqlite3
import stat
import sys
import tarfile
import tempfile
from pathlib import Path

from cryptography import x509
from cryptography.exceptions import InvalidSignature, InvalidTag, UnsupportedAlgorithm
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import dsa, ec, ed448, ed25519, rsa
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.scrypt import Scrypt
from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID

from raven_web.config import parse_origin

CONFIG_KEYS = {"origin", "port", "listen", "ssh_target", "ssh_port"}
STATE_FILES = ("config.json", "tls.crt", "tls.key", "secret.key", "db.sqlite3")
REVISION = re.compile(r"[0-9a-f]{64}\Z")
SSH_NAME = re.compile(r"[A-Za-z0-9_][A-Za-z0-9_.-]*\Z")
DNS_LABEL = re.compile(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\Z")
NUMERIC_HOST = re.compile(r"(?:[0-9]+|0x[0-9a-f]*)\Z")
MAX_CONFIG_BYTES = 64 * 1024
# A certificate request carries a PEM chain and a key; real ones stay far below this.
MAX_TLS_BYTES = 256 * 1024
TLS_FILES = ("tls.crt", "tls.key")
BACKUPS = "backups"
BACKUP_NAME = re.compile(r"tls-([0-9]{8}T[0-9]{6}Z)(?:-([0-9]{1,3}))?\Z")
KEEP_BACKUPS = 5
DEFAULT_DAYS = 365
MAX_DAYS = 825
MAX_NAMES = 20
CLOCK_SKEW = datetime.timedelta(minutes=5)
MINIMUM_LIFETIME = datetime.timedelta(days=1)
WARNING_DAYS = 30
CERTIFICATE_ACTIONS = ("certificate-check", "certificate-apply", "certificate-restore")
PEM_BLOCK = re.compile(r"-----BEGIN ([A-Z0-9 ]+)-----(.*?)-----END \1-----", re.DOTALL)
ENCRYPTED_HEADER = re.compile(r"^Proc-Type:[ \t]*4,ENCRYPTED", re.MULTILINE)
CURVES = {"secp256r1": "EC P-256", "secp384r1": "EC P-384"}
# Encrypted settings backups (raven web backup and restore)
BACKUP_ACTIONS = ("backup", "restore-check", "restore")
BACKUP_MAGIC = b"RAVENWEB1\n"
BACKUP_FORMAT = "raven-web-backup"
BACKUP_VERSION = 1
SCRYPT = {"n": 32768, "r": 8, "p": 1}
SALT_BYTES = 16
NONCE_BYTES = 12
MAX_HEADER = 1024
MANIFEST = "manifest.json"
# The order of the archive members after manifest.json; db.sqlite3 is also the first file a restore replaces.
BACKUP_FILES = ("db.sqlite3", "secret.key", "config.json", "tls.crt", "tls.key")
DATABASE_SIDECARS = ("db.sqlite3-journal", "db.sqlite3-wal", "db.sqlite3-shm")
MAX_DATABASE = 16 * 1024 * 1024
MAX_MEMBER = {MANIFEST: 1024 * 1024, "db.sqlite3": MAX_DATABASE, "secret.key": 4096,
              "config.json": MAX_CONFIG_BYTES, "tls.crt": MAX_TLS_BYTES, "tls.key": MAX_TLS_BYTES}
MAX_BACKUP_BYTES = 24 * 1024 * 1024
# A restore request carries the encrypted file in base64 next to the passphrase.
MAX_BACKUP_REQUEST = MAX_BACKUP_BYTES * 4 // 3 + 64 * 1024
MIN_PASSPHRASE = 12
MAX_PASSPHRASE = 1024
SECRET_KEY = re.compile(rb"[A-Za-z0-9_-]{50,1024}\Z")
PRE_RESTORE = "pre-restore-"
UNDECRYPTABLE = "Cannot decrypt the backup: the passphrase is wrong or the file is damaged"


class ConfigurationError(ValueError):
    pass


def _read_file(path):
    try:
        descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
        with os.fdopen(descriptor, "rb") as source:
            return source.read()
    except FileNotFoundError as error:
        raise ConfigurationError(f"Missing {path.name}; initialize the Raven web installation first") from error


def _read_config(state):
    raw = _read_file(state / "config.json")
    return raw, _parse_config(raw)


def _parse_config(raw):
    if not raw or len(raw) > MAX_CONFIG_BYTES:
        raise ConfigurationError("config.json is empty or exceeds 64 KiB")
    try:
        config = json.loads(raw)
    except (ValueError, UnicodeDecodeError) as error:
        raise ConfigurationError("config.json is not valid JSON") from error
    if not isinstance(config, dict) or not isinstance(config.get("origin"), str):
        raise ConfigurationError("config.json must contain an origin string")
    return config


def _snapshot(state):
    raw, config = _read_config(state)
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


def _alternative_names(certificate):
    try:
        return certificate.extensions.get_extension_for_class(x509.SubjectAlternativeName).value
    except x509.ExtensionNotFound:
        return None


def _covers(hostname, certificate):
    """True when an exact DNS or IP SAN of the certificate names hostname; wildcards never match."""
    names = _alternative_names(certificate)
    if names is None:
        return False
    try:
        address = ipaddress.ip_address(hostname)
    except ValueError:
        return any("*" not in name and name.lower() == hostname.lower()
                   for name in names.get_values_for_type(x509.DNSName))
    return address in names.get_values_for_type(x509.IPAddress)


def _matches_certificate(hostname, pem):
    certificate = x509.load_pem_x509_certificate(pem.encode("ascii"))
    if _alternative_names(certificate) is None:
        raise ConfigurationError("The TLS certificate has no subject alternative names")
    if not _covers(hostname, certificate):
        raise ConfigurationError("The origin hostname must match an exact DNS or IP certificate SAN; "
                                 "wildcards are unsupported")


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


def _file_state(path):
    try:
        info = path.lstat()
    except FileNotFoundError:
        return None
    except OSError as error:
        return {"error": error.strerror}
    return {"uid": info.st_uid, "mode": stat.S_IMODE(info.st_mode), "regular": stat.S_ISREG(info.st_mode),
            "readable": os.access(path, os.R_OK)}


def _names(certificate):
    names = _alternative_names(certificate)
    if names is None:
        return []
    return [*names.get_values_for_type(x509.DNSName), *map(str, names.get_values_for_type(x509.IPAddress))]


def _pem_blocks(text, label):
    """Every complete PEM block of text as (type, block); refuse text without blocks or with a broken one."""
    blocks = [(match[1], match[0]) for match in PEM_BLOCK.finditer(text)]
    if not blocks or len(blocks) != text.count("-----BEGIN "):
        raise ConfigurationError(f"{label} is not a PEM file: expected complete -----BEGIN ...----- and "
                                 "-----END ...----- blocks")
    return blocks


def _chain(text, label="tls.crt"):
    """The certificates of a PEM file in their order; blocks of other types are skipped."""
    chain = []
    for index, (kind, block) in enumerate(_pem_blocks(text, label), 1):
        if kind == "CERTIFICATE":
            try:
                chain.append(x509.load_pem_x509_certificate(block.encode("ascii")))
            except ValueError:
                raise ConfigurationError(f"{label}: block {index} is not a valid X.509 certificate") from None
    if not chain:
        raise ConfigurationError(f"{label} holds no certificate")
    return chain


def _public_pem(chain):
    return b"".join(certificate.public_bytes(serialization.Encoding.PEM) for certificate in chain)


def _fingerprint(certificate):
    return certificate.fingerprint(hashes.SHA256()).hex(":").upper()


def _summary(chain):
    leaf = chain[0]
    return {"subject": leaf.subject.rfc4514_string(), "san": _names(leaf),
            "not_before": leaf.not_valid_before_utc.isoformat(), "not_after": leaf.not_valid_after_utc.isoformat(),
            "fingerprint": _fingerprint(leaf), "chain": len(chain), "self_signed": leaf.issuer == leaf.subject}


def certificate_info(pem, hostname, now=None):
    """The public facts the gateway shows about its certificate (the leaf of the chain in pem)."""
    leaf = _chain(pem)[0]
    now = now or datetime.datetime.now(datetime.timezone.utc)
    return {"names": _names(leaf), "not_before": leaf.not_valid_before_utc.isoformat(),
            "not_after": leaf.not_valid_after_utc.isoformat(), "days_left": (leaf.not_valid_after_utc - now).days,
            "fingerprint": _fingerprint(leaf), "origin_covered": _covers(hostname, leaf)}


def public_certificate(pem):
    """The certificate chain of pem encoded again, so nothing but certificates can leave the file."""
    return _public_pem(_chain(pem)).decode("ascii")


def _public_or_empty(data):
    """Only the certificates of a backed-up tls.crt, so a key pasted into it by hand never leaves the helper."""
    try:
        return _public_pem(_chain(data.decode("ascii"))).decode("ascii")
    except ValueError:
        return ""


def _moment(value):
    return value.strftime("%Y-%m-%d %H:%M UTC")


def _san_name(value, label="--name"):
    """A DNS hostname in lowercase or an IP address in canonical form, as a certificate SAN holds it."""
    if not isinstance(value, str) or not value or not value.isascii() or value != value.strip():
        raise ConfigurationError(f"{label} must be a DNS hostname or an IP address")
    text = value[1:-1] if value.startswith("[") and value.endswith("]") else value
    if "%" in text:
        raise ConfigurationError(f"{label} {value}: scoped IPv6 addresses are not supported")
    try:
        return ipaddress.ip_address(text).compressed
    except ValueError:
        pass
    name = text.lower()
    if "*" in name:
        raise ConfigurationError(f"{label} {value}: wildcard names are not supported; name each host exactly")
    labels = name.split(".")
    if len(name) > 253 or not all(DNS_LABEL.fullmatch(part) for part in labels) or NUMERIC_HOST.fullmatch(labels[-1]):
        raise ConfigurationError(f"{label} {value} is not a DNS hostname or an IP address")
    return name


def self_signed(names, days, now=None):
    """A new RSA 2048 key and a self-signed server certificate for names (the first is the common name)."""
    now = now or datetime.datetime.now(datetime.timezone.utc)
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    alternatives = []
    for name in names:
        try:
            alternatives.append(x509.IPAddress(ipaddress.ip_address(name)))
        except ValueError:
            alternatives.append(x509.DNSName(name))
    subject = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, names[0] if len(names[0]) <= 64 else "Raven web")])
    certificate = (
        x509.CertificateBuilder()
        .subject_name(subject)
        .issuer_name(subject)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - CLOCK_SKEW)
        .not_valid_after(now + datetime.timedelta(days=days))
        .add_extension(x509.SubjectAlternativeName(alternatives), critical=False)
        .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
        .sign(key, hashes.SHA256())
    )
    return (certificate.public_bytes(serialization.Encoding.PEM),
            key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                              serialization.NoEncryption()))


def _key_type(key):
    """RSA 2048+ or EC P-256/P-384, the keys every browser accepts; anything else is refused."""
    if isinstance(key, rsa.RSAPrivateKey):
        if key.key_size < 2048:
            raise ConfigurationError(f"KEY is RSA {key.key_size}; use RSA 2048 or larger, or EC P-256/P-384")
        return f"RSA {key.key_size}"
    if isinstance(key, ec.EllipticCurvePrivateKey):
        if key.curve.name not in CURVES:
            raise ConfigurationError(f"KEY uses the EC curve {key.curve.name}; use EC P-256/P-384 or RSA 2048+")
        return CURVES[key.curve.name]
    kinds = ((ed25519.Ed25519PrivateKey, "Ed25519"), (ed448.Ed448PrivateKey, "Ed448"), (dsa.DSAPrivateKey, "DSA"))
    kind = next((name for cls, name in kinds if isinstance(key, cls)), "of an unsupported type")
    raise ConfigurationError(f"KEY is {kind}, which browsers do not accept for HTTPS; use RSA 2048+ or EC P-256/P-384")


ENCRYPTED_KEY = ("KEY is encrypted; the gateway starts unattended, so give it the key without a passphrase "
                 "(openssl pkey -in KEY -out KEY.plain)")


def _load_key(text):
    keys = [(kind, block) for kind, block in _pem_blocks(text, "KEY") if kind.endswith("PRIVATE KEY")]
    if len(keys) != 1:
        raise ConfigurationError(f"KEY must hold exactly one PEM private key (found {len(keys)})")
    kind, block = keys[0]
    if kind == "ENCRYPTED PRIVATE KEY" or ENCRYPTED_HEADER.search(block):
        raise ConfigurationError(ENCRYPTED_KEY)
    try:
        key = serialization.load_pem_private_key(block.encode("ascii"), password=None)
    except TypeError:
        raise ConfigurationError(ENCRYPTED_KEY) from None
    except (ValueError, UnsupportedAlgorithm):
        raise ConfigurationError("KEY is not a valid PEM private key") from None
    return key, _key_type(key)


def _import_chain(text):
    kinds = [kind for kind, _ in _pem_blocks(text, "CERT")]
    if any(kind.endswith("PRIVATE KEY") for kind in kinds):
        raise ConfigurationError("CERT holds a private key; give the certificate chain and the key as separate files")
    others = sorted(set(kinds) - {"CERTIFICATE"})
    if others:
        raise ConfigurationError(f"CERT may hold only CERTIFICATE blocks, not {', '.join(others)}")
    if len(kinds) > 10:
        raise ConfigurationError("CERT holds more than 10 certificates")
    return _chain(text, "CERT")


def _spki(public_key):
    return public_key.public_bytes(serialization.Encoding.DER, serialization.PublicFormat.SubjectPublicKeyInfo)


def _lifetime_warnings(not_after, now):
    days = (not_after - now).days
    if days < WARNING_DAYS:
        return [f"The new certificate expires on {not_after:%Y-%m-%d} ({days} days left); replace it before then"]
    return []


def _check_pair(chain, key, required, now, minimum=MINIMUM_LIFETIME, dates=True):
    """Refuse a pair the gateway or browsers cannot use for the required (hostname, origin) pairs; return warnings.

    dates=False leaves the validity period to the caller (a restore puts back even an expired certificate)."""
    leaf = chain[0]
    try:
        public_keys = [_spki(certificate.public_key()) for certificate in chain]
    except (ValueError, UnsupportedAlgorithm):
        raise ConfigurationError("CERT uses an unsupported public key algorithm") from None
    own = _spki(key.public_key())
    if public_keys[0] != own:
        if own in public_keys[1:]:
            raise ConfigurationError(f"KEY belongs to certificate {public_keys.index(own) + 1} in CERT; list the "
                                     "server certificate first, then its issuers")
        raise ConfigurationError("KEY does not match the certificate: their public keys differ")
    for index in range(len(chain) - 1):
        try:
            chain[index].verify_directly_issued_by(chain[index + 1])
        except (ValueError, TypeError, InvalidSignature, UnsupportedAlgorithm):
            raise ConfigurationError(f"CERT is out of order: certificate {index + 1} is not issued by certificate "
                                     f"{index + 2}; list the server certificate first, then each issuer") from None
    try:
        usage = leaf.extensions.get_extension_for_class(x509.ExtendedKeyUsage).value
    except x509.ExtensionNotFound:
        pass
    else:
        if not {ExtendedKeyUsageOID.SERVER_AUTH, ExtendedKeyUsageOID.ANY_EXTENDED_KEY_USAGE} & set(usage):
            raise ConfigurationError("CERT is not for HTTPS servers: its extended key usage lacks serverAuth")
    not_before, not_after = leaf.not_valid_before_utc, leaf.not_valid_after_utc
    if dates and not_before > now + CLOCK_SKEW:
        raise ConfigurationError(f"CERT is not valid until {_moment(not_before)}; check its dates and the server clock")
    if dates and not_after <= now:
        raise ConfigurationError(f"CERT expired on {_moment(not_after)}")
    if dates and not_after <= now + minimum:
        raise ConfigurationError(f"CERT expires on {_moment(not_after)}, in less than a day")
    names = _names(leaf)
    for hostname, origin in required:
        if not _covers(hostname, leaf):
            listed = ", ".join(names) if names else "no names (no subject alternative names)"
            wildcard = "; wildcard names do not count" if any("*" in name for name in names) else ""
            raise ConfigurationError(f"CERT covers {listed}, but not {hostname}, the hostname of {origin}{wildcard}")
    if not dates:
        return []
    warnings = _lifetime_warnings(not_after, now)
    if not_before > now:
        warnings.append(f"The new certificate is valid only from {_moment(not_before)}, which is still ahead by this "
                        "server's clock; the HTTPS check after the restart fails until then (check the server clock)")
    return warnings


def _private_file(path, data):
    """Create path as a synced 0600 file; never replace or follow an existing entry."""
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    with os.fdopen(descriptor, "wb") as output:
        os.fchmod(output.fileno(), 0o600)
        output.write(data)
        output.flush()
        os.fsync(output.fileno())


def _sync_directory(path):
    descriptor = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _install_pair(state, certificate, key, directory):
    """Replace tls.key, then tls.crt, each by renaming a synced 0600 temporary file; return a warning or None."""
    temporaries = {}
    try:
        for name, data in (("tls.key", key), ("tls.crt", certificate)):
            descriptor, temporary = tempfile.mkstemp(prefix=f".{name}.", dir=state)
            temporaries[name] = Path(temporary)
            with os.fdopen(descriptor, "wb") as output:
                os.fchmod(output.fileno(), 0o600)
                output.write(data)
                output.flush()
                os.fsync(output.fileno())
        for name in ("tls.key", "tls.crt"):
            os.replace(temporaries[name], state / name)
            del temporaries[name]
    finally:
        for temporary in temporaries.values():
            temporary.unlink(missing_ok=True)
    try:
        os.fsync(directory)
    except OSError:
        return "The certificate files were replaced, but the directory fsync failed; verify storage"
    return None


def _backups(root):
    entries = []
    for path in root.iterdir():
        match = BACKUP_NAME.fullmatch(path.name)
        if match and path.is_dir() and not path.is_symlink():
            entries.append(((match[1], int(match[2] or 1)), path))
    return [path for _, path in sorted(entries)]


def _backup_directory(state, stamp, kind):
    """A new private backups/<stamp>[-N]/ directory (0700) in the data volume."""
    root = state / BACKUPS
    try:
        os.mkdir(root, 0o700)
    except FileExistsError:
        if not stat.S_ISDIR(root.lstat().st_mode):
            raise ConfigurationError(f"{BACKUPS} in the data volume is not a directory") from None
    os.chmod(root, 0o700)
    for suffix in ("", *(f"-{number}" for number in range(2, 1000))):
        target = root / (stamp + suffix)
        try:
            os.mkdir(target, 0o700)
            break
        except FileExistsError:
            continue
    else:
        raise ConfigurationError(f"Too many {kind} backups within one second")
    os.chmod(target, 0o700)
    return root, target


def _backup(state, now):
    """Copy the current pair to backups/tls-<UTC>/ (0700, files 0600); keep the newest KEEP_BACKUPS of them."""
    root, target = _backup_directory(state, now.strftime("tls-%Y%m%dT%H%M%SZ"), "certificate")
    saved = []
    for name in TLS_FILES:
        if not (state / name).exists():
            continue
        data = _read_file(state / name)
        _private_file(target / name, data)
        if _read_file(target / name) != data:
            raise ConfigurationError("Backup verification failed; the certificate was not replaced")
        saved.append(name)
    _sync_directory(target)
    _sync_directory(root)
    # Never the new one, even when a clock set back makes it sort before older backups.
    older = [path for path in _backups(root) if path != target]
    for path in older[:max(len(older) - (KEEP_BACKUPS - 1), 0)]:
        shutil.rmtree(path)
    return target, saved


def _strings(value, name):
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value) or len(value) > MAX_NAMES:
        raise ConfigurationError(f"{name} must be a list of at most {MAX_NAMES} strings")
    return value


def _certificate_request(state, payload, directory=None):
    """Check (directory None) or apply a renew or import request; the private key is never part of the result."""
    if not isinstance(payload, dict) or payload.get("mode") not in ("renew", "import"):
        raise ConfigurationError("Expected a renew or import request")
    mode = payload["mode"]
    allowed = {"mode", "origins", *(("names", "days") if mode == "renew" else ("certificate", "key"))}
    if set(payload) - allowed:
        raise ConfigurationError("Unrecognized certificate request fields")
    _, config = _read_config(state)
    required = []
    for origin in [config["origin"], *_strings(payload.get("origins", []), "origins")]:
        try:
            hostname = _san_name(parse_origin(origin).hostname, "origin")
        except (ValueError, UnicodeError):
            raise ConfigurationError(f"{origin} is not a valid HTTPS origin") from None
        if hostname not in (known for known, _ in required):
            required.append((hostname, origin))
    now = datetime.datetime.now(datetime.timezone.utc)
    if mode == "renew":
        days = payload.get("days", DEFAULT_DAYS)
        if type(days) is not int or not 1 <= days <= MAX_DAYS:
            raise ConfigurationError(f"--days must be a whole number from 1 to {MAX_DAYS}")
        names = [hostname for hostname, _ in required]
        for value in _strings(payload.get("names", []), "names"):
            name = _san_name(value)
            if name not in names:
                names.append(name)
        if len(names) > MAX_NAMES:
            raise ConfigurationError(f"A certificate holds at most {MAX_NAMES} names")
        if directory is None:
            not_after = now + datetime.timedelta(days=days)
            return {"mode": mode, "info": {"san": names, "not_after": not_after.isoformat(), "days": days,
                                           "key_type": "RSA 2048", "self_signed": True, "chain": 1},
                    "warnings": _lifetime_warnings(not_after, now), "config": config}
        certificate_bytes, key_bytes = self_signed(names, days, now)
        chain = [x509.load_pem_x509_certificate(certificate_bytes)]
        key = serialization.load_pem_private_key(key_bytes, password=None)
        key_type = _key_type(key)
        minimum = datetime.timedelta(0)
    else:
        for label, value in (("CERT", payload.get("certificate")), ("KEY", payload.get("key"))):
            if not isinstance(value, str) or not value.isascii():
                raise ConfigurationError(f"{label} is not a PEM file: it holds non-ASCII bytes")
        chain = _import_chain(payload["certificate"])
        key, key_type = _load_key(payload["key"])
        certificate_bytes = _public_pem(chain)
        key_bytes = key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                                      serialization.NoEncryption())
        minimum = MINIMUM_LIFETIME
    warnings = _check_pair(chain, key, required, now, minimum)
    info = {**_summary(chain), "key_type": key_type}
    if directory is None:
        return {"mode": mode, "info": info, "warnings": warnings, "config": config}
    backup, saved = _backup(state, now)
    try:
        warning = _install_pair(state, certificate_bytes, key_bytes, directory)
    except OSError as error:
        problem = error.strerror or type(error).__name__
        if saved != list(TLS_FILES):
            raise ConfigurationError(f"Writing the new certificate failed ({problem})") from None
        try:
            _install_pair(state, *(_read_file(backup / name) for name in TLS_FILES), directory)
        except OSError:
            raise ConfigurationError(f"Writing the new certificate failed ({problem}), and so did restoring "
                                     f"{BACKUPS}/{backup.name}; copy it back to tls.crt and tls.key") from None
        raise ConfigurationError(f"Writing the new certificate failed ({problem}); "
                                 "the previous certificate is back in place") from None
    if warning:
        warnings.append(warning)
    previous = _public_or_empty(_read_file(backup / "tls.crt")) if "tls.crt" in saved else None
    return {"mode": mode, "info": info, "warnings": warnings, "config": config,
            "certificate": certificate_bytes.decode("ascii"), "previous": previous,
            "backup": f"{BACKUPS}/{backup.name}", "complete_backup": saved == list(TLS_FILES)}


def _restore(state, payload, directory):
    """Put the pair of one backups/tls-<UTC>/ directory back in place."""
    name = payload.get("backup") if isinstance(payload, dict) and set(payload) == {"backup"} else None
    if not isinstance(name, str) or not BACKUP_NAME.fullmatch(name):
        raise ConfigurationError("Expected the name of a tls-<UTC> backup")
    source = state / BACKUPS / name
    try:
        certificate, key = (_read_file(source / item) for item in TLS_FILES)
    except ConfigurationError:
        raise ConfigurationError(f"{BACKUPS}/{name} does not hold both tls.crt and tls.key") from None
    warning = _install_pair(state, certificate, key, directory)
    result = {"certificate": _public_or_empty(certificate), "restored": f"{BACKUPS}/{name}"}
    if warning:
        result["warning"] = warning
    return result


def _sha256(data):
    return hashlib.sha256(data).hexdigest()


def _name_field(payload, key):
    value = payload[key]
    if not isinstance(value, str) or not 0 < len(value) <= 256 or not value.isprintable():
        raise ConfigurationError(f"Expected a printable {key} name")
    return value


def _backup_request(payload, fields, minimum):
    """The passphrase, host user and instance of a backup or restore request; the passphrase is never echoed."""
    if not isinstance(payload, dict) or set(payload) != set(fields):
        raise ConfigurationError(f"Expected an object containing {', '.join(fields)}")
    passphrase = payload["passphrase"]
    if not isinstance(passphrase, str) or not minimum <= len(passphrase) <= MAX_PASSPHRASE:
        raise ConfigurationError(f"The passphrase must have {MIN_PASSPHRASE} to {MAX_PASSPHRASE} characters")
    return passphrase, _name_field(payload, "user"), _name_field(payload, "instance")


def _derive(passphrase, salt):
    return Scrypt(salt=salt, length=32, **SCRYPT).derive(passphrase.encode("utf-8"))


def _encrypt(plaintext, passphrase):
    """RAVENWEB1, a JSON header line, then AES-256-GCM over plaintext with both lines as associated data."""
    salt, nonce = secrets.token_bytes(SALT_BYTES), secrets.token_bytes(NONCE_BYTES)
    header = {"kdf": "scrypt", **SCRYPT, "salt": base64.b64encode(salt).decode("ascii"),
              "nonce": base64.b64encode(nonce).decode("ascii")}
    associated = BACKUP_MAGIC + json.dumps(header).encode("ascii") + b"\n"
    return associated + AESGCM(_derive(passphrase, salt)).encrypt(nonce, plaintext, associated)


def _decrypt(data, passphrase):
    """The plaintext of an encrypted backup; a wrong passphrase and any damage give the same error."""
    if not data.startswith(BACKUP_MAGIC):
        if data.startswith(BACKUP_MAGIC[:8]):
            raise ConfigurationError("The file is a Raven web backup of an unsupported format version")
        raise ConfigurationError("The file is not a Raven web settings backup (raven web backup makes one)")
    end = data.find(b"\n", len(BACKUP_MAGIC), len(BACKUP_MAGIC) + MAX_HEADER)
    try:
        if end < 0:
            raise ValueError("no header line")
        header = json.loads(data[len(BACKUP_MAGIC):end])
        if (not isinstance(header, dict) or set(header) != {"kdf", *SCRYPT, "salt", "nonce"}
                or header["kdf"] != "scrypt"
                or any(type(header[name]) is not int or header[name] != value for name, value in SCRYPT.items())
                or not isinstance(header["salt"], str) or not isinstance(header["nonce"], str)):
            raise ValueError("unexpected header")
        salt = base64.b64decode(header["salt"], validate=True)
        nonce = base64.b64decode(header["nonce"], validate=True)
        if len(salt) != SALT_BYTES or len(nonce) != NONCE_BYTES:
            raise ValueError("unexpected salt or nonce")
        return AESGCM(_derive(passphrase, salt)).decrypt(nonce, data[end + 1:], data[:end + 1])
    except (ValueError, TypeError, InvalidTag, binascii.Error):
        raise ConfigurationError(UNDECRYPTABLE) from None


def _archive(manifest, files, moment):
    """An uncompressed tar of manifest.json and the state files, in a fixed order, as owner-only regular files."""
    buffer = io.BytesIO()
    members = ((MANIFEST, (json.dumps(manifest, indent=2, sort_keys=True) + "\n").encode()),
               *((name, files[name]) for name in BACKUP_FILES))
    with tarfile.open(fileobj=buffer, mode="w", format=tarfile.USTAR_FORMAT) as archive:
        for name, data in members:
            info = tarfile.TarInfo(name)
            info.size, info.mode, info.mtime = len(data), 0o600, int(moment.timestamp())
            archive.addfile(info, io.BytesIO(data))
    return buffer.getvalue()


def _unpack(plaintext):
    """The members of a decrypted backup: exactly manifest.json and the state files, as regular files."""
    names = [MANIFEST, *BACKUP_FILES]
    try:
        with tarfile.open(fileobj=io.BytesIO(plaintext), mode="r:") as archive:
            members = archive.getmembers()
            if [member.name for member in members] != names or not all(member.isreg() for member in members):
                raise ConfigurationError(f"The backup must hold exactly {', '.join(names)} as regular files")
            contents = {}
            for member in members:
                if member.size > MAX_MEMBER[member.name]:
                    raise ConfigurationError(f"{member.name} in the backup is too large")
                contents[member.name] = archive.extractfile(member).read()
    except (tarfile.TarError, EOFError):
        raise ConfigurationError("The archive inside the backup is damaged") from None
    return contents


def _manifest(raw):
    try:
        manifest = json.loads(raw)
    except (ValueError, UnicodeDecodeError):
        raise ConfigurationError("manifest.json in the backup is not valid JSON") from None
    if not isinstance(manifest, dict) or manifest.get("format") != BACKUP_FORMAT:
        raise ConfigurationError("manifest.json does not describe a Raven web backup")
    if type(manifest.get("version")) is not int or manifest["version"] != BACKUP_VERSION:
        raise ConfigurationError(f"The backup has format version {manifest.get('version')!r}; this Raven reads "
                                 f"version {BACKUP_VERSION}")
    for key in ("created", "user", "instance", "origin", "fingerprint", "account"):
        if not isinstance(manifest.get(key), str) or not manifest[key].isprintable():
            raise ConfigurationError(f"manifest.json lacks a valid {key}")
    try:
        datetime.datetime.fromisoformat(manifest["created"])
    except ValueError:
        raise ConfigurationError("manifest.json lacks a valid created time") from None
    files = manifest.get("files")
    if (not isinstance(files, dict) or set(files) != set(BACKUP_FILES)
            or not all(isinstance(value, str) and REVISION.fullmatch(value) for value in files.values())):
        raise ConfigurationError(f"manifest.json must list the SHA-256 of {', '.join(BACKUP_FILES)}")
    return manifest


def _copy_database(source, target):
    """A consistent copy of the live account database, even while the gateway writes it (SQLite backup API)."""
    try:
        info = source.lstat()
    except FileNotFoundError:
        raise ConfigurationError("Missing db.sqlite3; initialize the Raven web installation first") from None
    if not stat.S_ISREG(info.st_mode):
        raise ConfigurationError("db.sqlite3 is not a regular file")
    try:
        with closing(sqlite3.connect(f"file:{source}?mode=ro", uri=True, timeout=10)) as live, \
                closing(sqlite3.connect(target)) as copy:
            live.backup(copy)
    except sqlite3.Error as error:
        raise ConfigurationError(f"Cannot copy the account database ({error})") from None


def _clean_database(path):
    """Check a private copy of the account database and remove every browser login from it; return the account."""
    try:
        with closing(sqlite3.connect(path, isolation_level=None)) as connection:
            connection.execute("PRAGMA temp_store=MEMORY")
            if connection.execute("PRAGMA integrity_check").fetchall() != [("ok",)]:
                raise ConfigurationError("db.sqlite3 fails the SQLite integrity check")
            tables = {row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
            if not {"auth_user", "django_session"} <= tables:
                raise ConfigurationError("db.sqlite3 is not a Raven web account database")
            accounts = connection.execute("SELECT username FROM auth_user").fetchall()
            if len(accounts) != 1:
                raise ConfigurationError(f"db.sqlite3 holds {len(accounts)} accounts; Raven web needs exactly one")
            # Logins are never carried over: their cookies must stay refused. VACUUM drops the deleted rows' bytes.
            connection.execute("DELETE FROM django_session")
            connection.execute("VACUUM")
    except sqlite3.Error as error:
        raise ConfigurationError(f"db.sqlite3 is not a usable SQLite database ({error})") from None
    return accounts[0][0]


def _check_contents(files, now):
    """Refuse state files the gateway cannot run with; return their public summary and warnings."""
    config = _parse_config(files["config.json"])
    try:
        certificate, key_text = files["tls.crt"].decode("ascii"), files["tls.key"].decode("ascii")
    except UnicodeDecodeError:
        raise ConfigurationError("tls.crt and tls.key must be PEM text") from None
    # The rules of raven web configure, with the defaults raven web start would add to a never-started install.
    revision = _sha256(files["config.json"])
    _validate({"config": {"listen": "127.0.0.1", "port": 8443, **config}, "revision": revision},
              {"config": config, "revision": revision, "certificate": certificate})
    chain = _chain(certificate)
    try:
        key, key_type = _load_key(key_text)
    except ConfigurationError as error:
        raise ConfigurationError(f"tls.key: {error}") from None
    origin = _origin(config["origin"])
    _check_pair(chain, key, [(origin.hostname, config["origin"])], now, dates=False)
    if not SECRET_KEY.fullmatch(files["secret.key"]):
        raise ConfigurationError("secret.key is not a Raven web secret key")
    leaf = chain[0]
    not_before, not_after = leaf.not_valid_before_utc, leaf.not_valid_after_utc
    warnings = []
    if not_after <= now:
        warnings.append(f"The certificate expired on {_moment(not_after)}; run raven web certificate renew "
                        "before raven web start")
    elif not_before > now + CLOCK_SKEW:
        warnings.append(f"The certificate is not valid until {_moment(not_before)}; check the server clock")
    elif (not_after - now).days < WARNING_DAYS:
        warnings.append(f"The certificate expires on {not_after:%Y-%m-%d} ({(not_after - now).days} days left); "
                        "renew it soon")
    summary = {"origin": config["origin"], "fingerprint": _fingerprint(leaf), "names": _names(leaf),
               "not_before": not_before.isoformat(), "not_after": not_after.isoformat(), "key_type": key_type,
               **{key: config.get(key) for key in ("listen", "port", "ssh_target", "ssh_port")}}
    return summary, warnings


def _create_backup(state, payload):
    """Snapshot, check and encrypt the state files; only the encrypted bytes and public facts leave the helper."""
    passphrase, user, instance = _backup_request(payload, ("passphrase", "user", "instance"), MIN_PASSPHRASE)
    now = datetime.datetime.now(datetime.timezone.utc)
    directory = os.open(state, os.O_RDONLY | os.O_DIRECTORY)
    try:
        # Writers in other helpers hold this lock exclusively; the gateway itself only writes db.sqlite3.
        fcntl.flock(directory, fcntl.LOCK_SH)
        with tempfile.TemporaryDirectory(prefix="raven-backup-") as scratch:
            database = Path(scratch) / "db.sqlite3"
            _copy_database(state / "db.sqlite3", database)
            account = _clean_database(database)
            files = {"db.sqlite3": database.read_bytes()}
        for name in BACKUP_FILES[1:]:
            files[name] = _read_file(state / name)
    finally:
        os.close(directory)
    for name, data in files.items():
        if len(data) > MAX_MEMBER[name]:
            raise ConfigurationError(f"{name} is larger than a backup may hold")
    try:
        summary, warnings = _check_contents(files, now)
    except ConfigurationError as error:
        raise ConfigurationError(f"The data volume holds settings Raven cannot restore: {error}") from None
    manifest = {"format": BACKUP_FORMAT, "version": BACKUP_VERSION, "created": now.isoformat(timespec="seconds"),
                "user": user, "instance": instance, "origin": summary["origin"],
                "fingerprint": summary["fingerprint"], "account": account,
                "files": {name: _sha256(files[name]) for name in BACKUP_FILES}}
    data = _encrypt(_archive(manifest, files, now), passphrase)
    if len(data) > MAX_BACKUP_BYTES:
        raise ConfigurationError(f"The backup would exceed {MAX_BACKUP_BYTES // (1024 * 1024)} MiB")
    return {"data": base64.b64encode(data).decode("ascii"), "warnings": warnings,
            "summary": {**summary, "created": manifest["created"], "account": account}}


def _prepare_restore(payload, now):
    """Decrypt and check a backup completely; return its manifest, the files to install and their summary."""
    passphrase, user, instance = _backup_request(payload, ("passphrase", "user", "instance", "backup"), 1)
    encoded = payload["backup"]
    try:
        data = base64.b64decode(encoded, validate=True) if isinstance(encoded, str) else None
    except binascii.Error:
        data = None
    if data is None or len(data) > MAX_BACKUP_BYTES:
        raise ConfigurationError("Expected the backup file in base64, at most "
                                 f"{MAX_BACKUP_BYTES // (1024 * 1024)} MiB")
    contents = _unpack(_decrypt(data, passphrase))
    manifest = _manifest(contents.pop(MANIFEST))
    for name in BACKUP_FILES:
        if _sha256(contents[name]) != manifest["files"][name]:
            raise ConfigurationError(f"{name} in the backup does not match the SHA-256 in its manifest")
    if (manifest["user"], manifest["instance"]) != (user, instance):
        raise ConfigurationError(f"The backup is from another installation (host user {manifest['user']}, "
                                 f"instance {manifest['instance']}), not from host user {user}, instance "
                                 f"{instance}; restore it there")
    with tempfile.TemporaryDirectory(prefix="raven-restore-") as scratch:
        database = Path(scratch) / "db.sqlite3"
        _private_file(database, contents["db.sqlite3"])
        try:
            account = _clean_database(database)
        except ConfigurationError as error:
            raise ConfigurationError(f"The backup cannot be restored: {error}") from None
        contents["db.sqlite3"] = database.read_bytes()
    try:
        summary, warnings = _check_contents(contents, now)
    except ConfigurationError as error:
        raise ConfigurationError(f"The backup cannot be restored: {error}") from None
    for key, value in (("account", account), ("origin", summary["origin"]), ("fingerprint", summary["fingerprint"])):
        if manifest[key] != value:
            raise ConfigurationError(f"The backup cannot be restored: its manifest names another {key}")
    summary = {**summary, "created": manifest["created"], "user": manifest["user"],
               "instance": manifest["instance"], "account": account}
    return contents, summary, warnings


def _current(state):
    """The origin and certificate the data volume holds now, or None when it holds no settings at all."""
    if not state.is_dir() or not any((state / name).exists() for name in BACKUP_FILES):
        return None
    current = {"origin": None, "fingerprint": None}
    try:
        current["origin"] = _read_config(state)[1]["origin"]
    except (OSError, ValueError):
        pass
    try:
        current["fingerprint"] = _fingerprint(_chain(_read_file(state / "tls.crt").decode("ascii"))[0])
    except (OSError, ValueError):
        pass
    return current


def _check_restore(state, payload):
    _, summary, warnings = _prepare_restore(payload, datetime.datetime.now(datetime.timezone.utc))
    return {"backup": summary, "current": _current(state), "warnings": warnings}


def _temporary(state, name, data):
    descriptor, temporary = tempfile.mkstemp(prefix=f".{name}.restore.", dir=state)
    try:
        with os.fdopen(descriptor, "wb") as output:
            os.fchmod(output.fileno(), 0o600)
            output.write(data)
            output.flush()
            os.fsync(output.fileno())
    except BaseException:
        os.unlink(temporary)
        raise
    return Path(temporary)


def _save_current(state, now):
    """Copy the state files that exist to backups/pre-restore-<UTC>/ (0700, files 0600); return it and their names."""
    present = [name for name in (*BACKUP_FILES, *DATABASE_SIDECARS) if os.path.lexists(state / name)]
    if not present:
        return None, []
    _, target = _backup_directory(state, now.strftime(PRE_RESTORE + "%Y%m%dT%H%M%SZ"), "pre-restore")
    for name in present:
        data = _read_file(state / name)
        _private_file(target / name, data)
        if _read_file(target / name) != data:
            raise ConfigurationError("Verifying the copy of the current files failed")
    _sync_directory(target)
    _sync_directory(target.parent)
    return target, present


def _put_back(state, saved, present, directory):
    for name in (*BACKUP_FILES, *DATABASE_SIDECARS):
        if name in present:
            os.replace(_temporary(state, name, _read_file(saved / name)), state / name)
        else:
            (state / name).unlink(missing_ok=True)
    os.fsync(directory)


def _problem(error):
    return str(error) if isinstance(error, ConfigurationError) else (
        getattr(error, "strerror", None) or type(error).__name__)


def _restore_backup(state, payload, directory):
    """Check the backup, keep the current files, then replace them one by one; any failure puts them back."""
    now = datetime.datetime.now(datetime.timezone.utc)
    contents, summary, warnings = _prepare_restore(payload, now)
    try:
        saved, present = _save_current(state, now)
    except (OSError, ValueError) as error:
        raise ConfigurationError(f"Saving the current files failed ({_problem(error)}); nothing was restored") from None
    temporaries = {}
    try:
        for name in BACKUP_FILES:
            temporaries[name] = _temporary(state, name, contents[name])
        # A journal left next to the old database would be rolled into the restored one.
        for name in DATABASE_SIDECARS:
            (state / name).unlink(missing_ok=True)
        for name in BACKUP_FILES:
            os.replace(temporaries[name], state / name)
            del temporaries[name]
        os.fsync(directory)
        for name in BACKUP_FILES:
            if _read_file(state / name) != contents[name]:
                raise ConfigurationError(f"{name} reads back differently")
    except Exception as error:
        for temporary in temporaries.values():
            temporary.unlink(missing_ok=True)
        kept = f"{BACKUPS}/{saved.name}" if saved else None
        try:
            if saved:
                _put_back(state, saved, present, directory)
            else:
                for name in (*BACKUP_FILES, *DATABASE_SIDECARS):
                    (state / name).unlink(missing_ok=True)
        except Exception:
            where = f"copy the files back from {kept}" if kept else "remove the data volume and restore again"
            raise ConfigurationError(f"Restoring failed ({_problem(error)}), and so did putting the previous files "
                                     f"back; {where}") from None
        raise ConfigurationError(f"Restoring failed ({_problem(error)}); the previous files are back in place"
                                 + (f" (a copy stays in {kept})" if kept else "")) from None
    return {"backup": summary, "warnings": warnings, "saved": f"{BACKUPS}/{saved.name}" if saved else None}


def _accounts(path):
    with closing(sqlite3.connect(f"file:{path}?mode=ro", uri=True)) as connection:
        total, active = connection.execute("SELECT COUNT(*), COALESCE(SUM(is_active), 0) FROM auth_user").fetchone()
    return {"total": total, "active": active}


def _inspect(state):
    """Describe the state volume for diagnostics without reading private keys or password hashes."""
    info = state.stat()
    result = {"directory": {"uid": info.st_uid, "gid": info.st_gid, "mode": stat.S_IMODE(info.st_mode)},
              "files": {name: _file_state(state / name) for name in STATE_FILES}}
    leaf = None
    try:
        chain = _chain(_read_file(state / "tls.crt").decode("ascii"))
        result["certificate_info"] = _summary(chain)
        result["certificate"] = _public_pem(chain).decode("ascii")
        leaf = chain[0]
    except (OSError, ValueError) as error:
        result["certificate_error"] = str(error)
    try:
        _, snapshot = _snapshot(state)
        result["config"] = snapshot["config"]
        hostname = _origin(snapshot["config"]["origin"]).hostname
        if leaf is not None:
            result["certificate_info"]["origin_covered"] = _covers(hostname, leaf)
        _matches_certificate(hostname, snapshot["certificate"])
    except (OSError, ValueError) as error:
        result["config_error"] = str(error)
    try:
        result["accounts"] = _accounts(state / "db.sqlite3")
    except (OSError, sqlite3.Error) as error:
        result["accounts_error"] = str(error)
    return result


def execute(action, payload=None, state=None):
    state = Path(state if state is not None else os.environ.get("RAVEN_WEB_STATE", "/data"))
    if action == "inspect":
        return _inspect(state)
    if action in ("read", "validate"):
        _, snapshot = _snapshot(state)
        if action == "validate":
            config, _ = _validate(payload, snapshot)
            return {**snapshot, "config": config, "backup": None}
        return snapshot
    if action == "certificate-check":
        return _certificate_request(state, payload)
    if action == "backup":
        return _create_backup(state, payload)
    if action == "restore-check":
        return _check_restore(state, payload)
    if action not in ("write", "certificate-apply", "certificate-restore", "restore"):
        raise ConfigurationError("Expected read, validate, write, inspect, a certificate or a backup action")
    directory = os.open(state, os.O_RDONLY | os.O_DIRECTORY)
    try:
        fcntl.flock(directory, fcntl.LOCK_EX)
        if action == "certificate-apply":
            return _certificate_request(state, payload, directory)
        if action == "certificate-restore":
            return _restore(state, payload, directory)
        if action == "restore":
            return _restore_backup(state, payload, directory)
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
    parser = argparse.ArgumentParser(description="Read, validate, inspect or atomically update Raven web configuration "
                                     "and its HTTPS certificate; back up and restore it encrypted")
    parser.add_argument("action", choices=("read", "validate", "write", "inspect", *CERTIFICATE_ACTIONS,
                                           *BACKUP_ACTIONS))
    arguments = parser.parse_args()
    try:
        payload = None
        if arguments.action not in ("read", "inspect"):
            limit = (MAX_TLS_BYTES if arguments.action in CERTIFICATE_ACTIONS
                     else MAX_BACKUP_REQUEST if arguments.action in BACKUP_ACTIONS else MAX_CONFIG_BYTES)
            data = sys.stdin.read(limit + 1)
            if len(data) > limit:
                raise ConfigurationError(f"The request exceeds {limit // 1024} KiB")
            payload = json.loads(data)
        print(json.dumps(execute(arguments.action, payload), allow_nan=False))
    except (OSError, ValueError) as error:
        print(f"Raven configuration: {error}", file=sys.stderr)
        return 1
    except Exception as error:
        if arguments.action not in (*CERTIFICATE_ACTIONS, *BACKUP_ACTIONS):
            raise
        # A traceback could quote the request: a private key, or a backup passphrase.
        print(f"Raven configuration: unexpected {type(error).__name__}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
