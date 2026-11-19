"""Encrypted web settings backups: format, decryption failures, manifest checks, installation checks and restore."""

import base64
from contextlib import closing
import datetime
import hashlib
import io
import json
import os
import sqlite3
import stat
import subprocess
import sys
import tarfile
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import NameOID

from raven_web import service_config
from raven_web.bootstrap import initialize
from raven_web.service_config import (
    BACKUP_FILES, BACKUP_MAGIC, MANIFEST, UNDECRYPTABLE, ConfigurationError, execute,
)

USER, INSTANCE = "owner-host", "web"
PASSPHRASE = "correct horse battery staple"
PASSWORD_HASH = "argon2$argon2id$v=19$m=102400,t=2,p=8$c2FsdHNhbHQ$backup-must-encrypt-this-hash"
SESSION_KEY = "k" * 24 + "sessionkeymarker"
SESSION_DATA = "session-data-marker-" * 8
NOW = datetime.datetime.now(datetime.timezone.utc)
DAY = datetime.timedelta(days=1)


def write_database(path, accounts=("owner",), sessions=2, session_table=True):
    with closing(sqlite3.connect(path)) as connection, connection:
        connection.execute("CREATE TABLE auth_user (id INTEGER PRIMARY KEY, username TEXT, password TEXT, "
                           "is_active BOOL)")
        if session_table:
            connection.execute("CREATE TABLE django_session (session_key TEXT PRIMARY KEY, session_data TEXT, "
                               "expire_date TEXT)")
            connection.executemany("INSERT INTO django_session VALUES (?, ?, ?)",
                                   [(f"{SESSION_KEY}{index}", SESSION_DATA, "2099-01-01") for index in range(sessions)])
        connection.executemany("INSERT INTO auth_user (username, password, is_active) VALUES (?, ?, 1)",
                               [(name, PASSWORD_HASH) for name in accounts])


def database_bytes(**options):
    with tempfile.TemporaryDirectory() as scratch:
        path = Path(scratch) / "db.sqlite3"
        write_database(path, **options)
        return path.read_bytes()


def rows(path, query):
    with closing(sqlite3.connect(f"file:{path}?mode=ro", uri=True)) as connection:
        return connection.execute(query).fetchall()


def pair(hostname="localhost", not_before=None, not_after=None):
    key = ec.generate_private_key(ec.SECP256R1())
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, hostname)])
    certificate = (
        x509.CertificateBuilder().subject_name(name).issuer_name(name).public_key(key.public_key())
        .serial_number(x509.random_serial_number()).not_valid_before(not_before or NOW - DAY)
        .not_valid_after(not_after or NOW + 200 * DAY)
        .add_extension(x509.SubjectAlternativeName([x509.DNSName(hostname)]), critical=False)
        .sign(key, hashes.SHA256()))
    return (certificate.public_bytes(serialization.Encoding.PEM),
            key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                              serialization.NoEncryption()))


def key_lines(pem):
    return [line for line in pem.splitlines() if line and not line.startswith(b"-----")]


def tar(members):
    """An archive of (name, data, type) members, as a hand-made backup would hold."""
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w", format=tarfile.USTAR_FORMAT) as archive:
        for name, data, kind in members:
            info = tarfile.TarInfo(name)
            info.type = kind
            if kind == tarfile.SYMTYPE:
                info.linkname = "/etc/passwd"
                archive.addfile(info)
            else:
                info.size = len(data)
                archive.addfile(info, io.BytesIO(data))
    return buffer.getvalue()


class BackupTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="raven-backup-test-")
        self.addCleanup(temporary.cleanup)
        root = Path(temporary.name)
        self.state, self.target = root / "state", root / "target"
        self.state.mkdir(mode=0o700)
        self.target.mkdir(mode=0o700)
        with patch.dict(os.environ, {"RAVEN_WEB_STATE": str(self.state)}):
            initialize(["--origin", "https://localhost:8443"])
        write_database(self.state / "db.sqlite3")

    def backup(self, passphrase=PASSPHRASE, user=USER, instance=INSTANCE, state=None):
        result = execute("backup", {"passphrase": passphrase, "user": user, "instance": instance},
                         self.state if state is None else state)
        return base64.b64decode(result["data"]), result

    def restore(self, data, passphrase=PASSPHRASE, user=USER, instance=INSTANCE, state=None, action="restore"):
        payload = {"passphrase": passphrase, "user": user, "instance": instance,
                   "backup": base64.b64encode(data).decode()}
        return execute(action, payload, self.target if state is None else state)

    @staticmethod
    def volume(state):
        """Every entry below state with its mode and, for files, its SHA-256."""
        found = {}
        for path in sorted(state.rglob("*")):
            info = path.lstat()
            digest = hashlib.sha256(path.read_bytes()).hexdigest() if stat.S_ISREG(info.st_mode) else None
            found[str(path.relative_to(state))] = (stat.S_IMODE(info.st_mode), digest)
        return found

    def contents(self, data):
        """The members of a backup, decrypted with the test passphrase."""
        return service_config._unpack(service_config._decrypt(data, PASSPHRASE))

    def reencrypt(self, contents, members=None):
        if members is None:
            members = [(name, contents[name], tarfile.REGTYPE) for name in (MANIFEST, *BACKUP_FILES)]
        return service_config._encrypt(tar(members), PASSPHRASE)

    def assert_refused(self, data, message, state=None, **options):
        state = self.target if state is None else state
        before = self.volume(state)
        with self.assertRaises(ConfigurationError) as error:
            self.restore(data, state=state, **options)
        self.assertIn(message, str(error.exception))
        self.assertEqual(self.volume(state), before, "a refused restore changed the data volume")
        return str(error.exception)

    def test_format_and_round_trip_into_an_empty_volume(self):
        data, result = self.backup()
        magic, header, ciphertext = data.split(b"\n", 2)
        self.assertEqual(magic + b"\n", BACKUP_MAGIC)
        header = json.loads(header)
        self.assertEqual(list(header), ["kdf", "n", "r", "p", "salt", "nonce"])
        self.assertEqual((header["kdf"], header["n"], header["r"], header["p"]), ("scrypt", 32768, 8, 1))
        self.assertEqual((len(base64.b64decode(header["salt"])), len(base64.b64decode(header["nonce"]))), (16, 12))
        self.assertGreater(len(ciphertext), 16)
        key = (self.state / "tls.key").read_bytes()
        secret = (self.state / "secret.key").read_bytes()
        for plain in (b"PRIVATE KEY", secret, PASSWORD_HASH.encode(), b"localhost:8443", *key_lines(key)):
            self.assertNotIn(plain, data)
        summary = result["summary"]
        self.assertEqual((summary["account"], summary["origin"]), ("owner", "https://localhost:8443"))
        self.assertEqual(summary["names"], ["localhost"])
        self.assertRegex(summary["fingerprint"], r"^([0-9A-F]{2}:){31}[0-9A-F]{2}$")
        self.assertNotIn(PASSPHRASE, json.dumps(result))
        self.assertEqual(result["warnings"], [])

        contents = self.contents(data)
        manifest = json.loads(contents[MANIFEST])
        self.assertEqual({key: manifest[key] for key in ("format", "version", "user", "instance", "origin",
                                                          "fingerprint", "account")},
                         {"format": "raven-web-backup", "version": 1, "user": USER, "instance": INSTANCE,
                          "origin": "https://localhost:8443", "fingerprint": summary["fingerprint"],
                          "account": "owner"})
        self.assertEqual(manifest["files"], {name: hashlib.sha256(contents[name]).hexdigest()
                                             for name in BACKUP_FILES})
        with tarfile.open(fileobj=io.BytesIO(service_config._decrypt(data, PASSPHRASE))) as archive:
            self.assertEqual([(member.name, member.mode, member.isreg()) for member in archive.getmembers()],
                             [(name, 0o600, True) for name in (MANIFEST, *BACKUP_FILES)])

        check = self.restore(data, action="restore-check")
        self.assertIsNone(check["current"])
        self.assertEqual(check["backup"]["fingerprint"], summary["fingerprint"])
        self.assertEqual((check["backup"]["user"], check["backup"]["instance"]), (USER, INSTANCE))
        self.assertEqual(self.volume(self.target), {}, "restore-check wrote to the volume")
        result = self.restore(data)
        self.assertIsNone(result["saved"], "nothing existed to keep")
        for name in ("secret.key", "config.json", "tls.crt", "tls.key"):
            self.assertEqual((self.target / name).read_bytes(), (self.state / name).read_bytes(), name)
        for name in BACKUP_FILES:
            self.assertEqual(stat.S_IMODE((self.target / name).stat().st_mode), 0o600, name)
        self.assertEqual(rows(self.target / "db.sqlite3", "SELECT username, password FROM auth_user"),
                         [("owner", PASSWORD_HASH)])
        self.assertEqual(sorted(self.volume(self.target)), sorted(BACKUP_FILES), "temporary files were left")
        current = execute("restore-check", {"passphrase": PASSPHRASE, "user": USER, "instance": INSTANCE,
                                            "backup": base64.b64encode(data).decode()}, self.state)["current"]
        self.assertEqual(current, {"origin": "https://localhost:8443", "fingerprint": summary["fingerprint"]})

    def test_logins_are_neither_backed_up_nor_restored(self):
        self.assertEqual(rows(self.state / "db.sqlite3", "SELECT COUNT(*) FROM django_session"), [(2,)])
        data, _ = self.backup()
        database = self.contents(data)["db.sqlite3"]
        for marker in (SESSION_KEY, SESSION_DATA[:20]):
            self.assertNotIn(marker.encode(), database, "a deleted login left its bytes in the backup")
        self.assertEqual(rows(self.state / "db.sqlite3", "SELECT COUNT(*) FROM django_session"), [(2,)],
                         "the backup changed the live database")
        # A hand-made backup that still holds logins: the restore removes them all the same.
        contents = self.contents(data)
        contents["db.sqlite3"] = (self.state / "db.sqlite3").read_bytes()
        manifest = json.loads(contents[MANIFEST])
        manifest["files"]["db.sqlite3"] = hashlib.sha256(contents["db.sqlite3"]).hexdigest()
        contents[MANIFEST] = json.dumps(manifest).encode()
        self.restore(self.reencrypt(contents), state=self.state)
        self.assertEqual(rows(self.state / "db.sqlite3", "SELECT COUNT(*) FROM django_session"), [(0,)])
        self.assertNotIn(SESSION_KEY.encode(), (self.state / "db.sqlite3").read_bytes())
        self.assertEqual(rows(self.state / "db.sqlite3", "SELECT password FROM auth_user"), [(PASSWORD_HASH,)])

    def test_wrong_passphrase_and_damage_give_one_message(self):
        data, _ = self.backup()
        magic, header, ciphertext = data.split(b"\n", 2)
        middle = len(data) - len(ciphertext) // 2

        def flipped(position):
            return data[:position] + bytes([data[position] ^ 1]) + data[position + 1:]

        weaker = json.loads(header)
        weaker["n"] = 16384
        other_salt = json.loads(header)
        other_salt["salt"] = base64.b64encode(b"\0" * 16).decode()
        cases = {"wrong passphrase": (data, "another long passphrase"),
                 "flipped ciphertext byte": (flipped(middle), PASSPHRASE),
                 "flipped tag byte": (flipped(len(data) - 1), PASSPHRASE),
                 "flipped header byte": (flipped(len(magic) + 5), PASSPHRASE),
                 "truncated": (data[:-100], PASSPHRASE),
                 "appended byte": (data + b"\0", PASSPHRASE),
                 "no ciphertext": (magic + b"\n" + header + b"\n", PASSPHRASE),
                 "no header line": (magic + b"\n" + b"x" * 2000, PASSPHRASE),
                 "weaker parameters": (b"\n".join([magic, json.dumps(weaker).encode(), ciphertext]), PASSPHRASE),
                 "other salt": (b"\n".join([magic, json.dumps(other_salt).encode(), ciphertext]), PASSPHRASE)}
        for case, (damaged, passphrase) in cases.items():
            with self.subTest(case):
                message = self.assert_refused(damaged, "Cannot decrypt", state=self.state, passphrase=passphrase)
                self.assertEqual(message, UNDECRYPTABLE)
                self.assertNotIn(passphrase, message)
        self.assert_refused(b"RAVENWEB2\n" + data[len(BACKUP_MAGIC):], "unsupported format version")
        self.assert_refused(b"\x1f\x8b" + data, "not a Raven web settings backup")

    def test_manifest_and_file_hashes_are_checked(self):
        data, _ = self.backup()
        contents = self.contents(data)
        manifest = json.loads(contents[MANIFEST])

        def with_manifest(**changes):
            return {**contents, MANIFEST: json.dumps({**manifest, **changes}).encode()}

        changed_config = {**contents, "config.json": b'{"origin": "https://localhost:8443", "port": 9443}'}
        cases = (
            (self.reencrypt(changed_config), "config.json in the backup does not match the SHA-256"),
            (self.reencrypt(with_manifest(format="raven-session-backup")), "does not describe a Raven web backup"),
            (self.reencrypt(with_manifest(version=2)), "format version 2"),
            (self.reencrypt(with_manifest(files={**manifest["files"], "tls.key": "0" * 64})),
             "tls.key in the backup does not match the SHA-256"),
            (self.reencrypt(with_manifest(files={"db.sqlite3": manifest["files"]["db.sqlite3"]})),
             "must list the SHA-256"),
            (self.reencrypt(with_manifest(account="intruder")), "its manifest names another account"),
            (self.reencrypt(with_manifest(created="yesterday")), "valid created time"),
            (self.reencrypt({**contents, MANIFEST: b"not json"}), "not valid JSON"),
            (self.reencrypt(contents, [(name, contents[name], tarfile.REGTYPE)
                                       for name in (MANIFEST, *reversed(BACKUP_FILES))]), "must hold exactly"),
            (self.reencrypt(contents, [*((name, contents[name], tarfile.REGTYPE) for name in (MANIFEST, *BACKUP_FILES)),
                                       ("../escape", b"x", tarfile.REGTYPE)]), "must hold exactly"),
            (self.reencrypt(contents, [*((name, contents[name], tarfile.REGTYPE) for name in (MANIFEST, *BACKUP_FILES[:-1])),
                                       ("tls.key", b"", tarfile.SYMTYPE)]), "as regular files"),
            (service_config._encrypt(b"not a tar archive" * 64, PASSPHRASE), "archive inside the backup is damaged"),
        )
        for damaged, message in cases:
            with self.subTest(message):
                self.assert_refused(damaged, message, state=self.state)

    def test_another_host_user_or_instance_is_refused(self):
        data, _ = self.backup(user="alice", instance="web")
        message = self.assert_refused(data, "another installation", state=self.state)
        self.assertIn("host user alice, instance web", message)
        data, _ = self.backup(instance="cert-test")
        self.assert_refused(data, "another installation", state=self.state)
        self.assert_refused(data, "another installation", state=self.state, action="restore-check")

    def test_database_must_be_intact_and_hold_exactly_one_account(self):
        data, _ = self.backup()
        contents = self.contents(data)

        def with_database(database):
            manifest = json.loads(contents[MANIFEST])
            manifest["files"]["db.sqlite3"] = hashlib.sha256(database).hexdigest()
            return self.reencrypt({**contents, "db.sqlite3": database, MANIFEST: json.dumps(manifest).encode()})

        good = contents["db.sqlite3"]
        page = int.from_bytes(good[16:18], "big")
        damaged = good[:page] + bytes(len(good) - page)
        cases = ((damaged, "db.sqlite3"),
                 (b"SQLite format 3\0" + b"\xff" * 4080, "not a usable SQLite database"),
                 (database_bytes(accounts=("owner", "second")), "holds 2 accounts; Raven web needs exactly one"),
                 (database_bytes(accounts=()), "holds 0 accounts"),
                 (database_bytes(session_table=False), "not a Raven web account database"))
        for database, message in cases:
            with self.subTest(message):
                self.assert_refused(with_database(database), message, state=self.state)
        with closing(sqlite3.connect(self.state / "db.sqlite3")) as connection, connection:
            connection.execute("INSERT INTO auth_user (username, password, is_active) VALUES ('second', 'x', 1)")
        with self.assertRaisesRegex(ConfigurationError, "holds 2 accounts"):
            self.backup()

    def test_settings_certificate_and_secret_are_checked(self):
        data, _ = self.backup()
        contents = self.contents(data)

        def with_files(manifest_changes=None, **files):
            manifest = {**json.loads(contents[MANIFEST]), **(manifest_changes or {})}
            changed = {**contents, **{name.replace("_", "."): value for name, value in files.items()}}
            for name in BACKUP_FILES:
                manifest["files"][name] = hashlib.sha256(changed[name]).hexdigest()
            changed[MANIFEST] = json.dumps(manifest).encode()
            return self.reencrypt(changed)

        other_certificate, other_key = pair()
        foreign_certificate, foreign_key = pair("other.example.test")
        cases = (
            (with_files(tls_key=other_key), "KEY does not match the certificate"),
            (with_files(tls_crt=foreign_certificate, tls_key=foreign_key), "The origin hostname must match"),
            (with_files(config_json=b'{"origin": "https://localhost:8443", "port": 80}'), "port must be"),
            (with_files(config_json=b'{"origin": "http://localhost"}'), "origin must be"),
            (with_files(config_json=b"[]"), "must contain an origin"),
            (with_files(secret_key=b"short"), "secret.key is not a Raven web secret key"),
            (with_files(tls_key=b"-----BEGIN PRIVATE KEY-----\nAAAA\n-----END PRIVATE KEY-----\n"),
             "tls.key: KEY is not a valid PEM private key"),
        )
        for damaged, message in cases:
            with self.subTest(message):
                text = self.assert_refused(damaged, message, state=self.state)
                self.assertTrue(text.startswith("The backup cannot be restored: "), text)
                self.assertNotIn("PRIVATE KEY", text)
        config = (b'{"listen": "::1", "origin": "https://localhost:9443", "port": 19443, '
                  b'"ssh_target": "owner@example.test"}')
        fingerprint = x509.load_pem_x509_certificate(other_certificate).fingerprint(hashes.SHA256()).hex(":").upper()
        self.assert_refused(with_files(tls_crt=other_certificate, tls_key=other_key, config_json=config),
                            "its manifest names another origin", state=self.state)
        self.assert_refused(with_files({"origin": "https://localhost:9443"}, tls_crt=other_certificate,
                                       tls_key=other_key, config_json=config),
                            "its manifest names another fingerprint", state=self.state)
        accepted = with_files({"origin": "https://localhost:9443", "fingerprint": fingerprint},
                              tls_crt=other_certificate, tls_key=other_key, config_json=config)
        check = self.restore(accepted, state=self.state, action="restore-check")["backup"]
        self.assertEqual((check["origin"], check["listen"], check["port"], check["ssh_target"], check["ssh_port"],
                          check["fingerprint"]),
                         ("https://localhost:9443", "::1", 19443, "owner@example.test", None, fingerprint))

    def test_expired_certificate_is_restored_with_a_warning(self):
        certificate, key = pair(not_before=NOW - 30 * DAY, not_after=NOW - DAY)
        (self.state / "tls.crt").write_bytes(certificate)
        (self.state / "tls.key").write_bytes(key)
        data, result = self.backup()
        self.assertIn("The certificate expired on", result["warnings"][0])
        result = self.restore(data)
        self.assertIn("raven web certificate renew before raven web start", result["warnings"][0])
        self.assertEqual((self.target / "tls.crt").read_bytes(), certificate)

    def test_short_passphrase_is_refused_for_a_backup(self):
        with self.assertRaisesRegex(ConfigurationError, "12 to 1024 characters"):
            self.backup(passphrase="short")

    def test_restore_keeps_the_current_files_and_puts_them_back_after_a_failure(self):
        data, _ = self.backup()
        certificate, key = pair()
        (self.state / "tls.crt").write_bytes(certificate)
        (self.state / "tls.key").write_bytes(key)
        (self.state / "config.json").write_text('{"origin": "https://localhost:8443", "port": 9443}')
        (self.state / "db.sqlite3-journal").write_bytes(b"stale journal")
        for path in self.state.iterdir():
            path.chmod(0o600)
        before = self.volume(self.state)
        current = {name: (self.state / name).read_bytes() for name in (*BACKUP_FILES, "db.sqlite3-journal")}
        replace = os.replace
        calls = []

        def failing(source, target):
            calls.append(target)
            if len(calls) == 3:
                raise OSError(28, "No space left on device")
            return replace(source, target)

        with patch.object(service_config.os, "replace", side_effect=failing):
            with self.assertRaises(ConfigurationError) as error:
                self.restore(data, state=self.state)
        self.assertIn("Restoring failed (No space left on device); the previous files are back in place",
                      str(error.exception))
        after = self.volume(self.state)
        saved = [name for name in after if name.startswith("backups/pre-restore-") and "/" not in name[8:]]
        self.assertEqual(len(saved), 1, after)
        self.assertRegex(saved[0], r"^backups/pre-restore-\d{8}T\d{6}Z$")
        self.assertEqual({name: value for name, value in after.items() if not name.startswith("backups")},
                         {name: value for name, value in before.items() if not name.startswith("backups")})
        for name, data_before in current.items():
            self.assertEqual((self.state / name).read_bytes(), data_before, name)
            self.assertEqual(after[f"{saved[0]}/{name}"], (0o600, hashlib.sha256(data_before).hexdigest()))
        self.assertEqual((after["backups"][0], after[saved[0]][0]), (0o700, 0o700))

        result = self.restore(data, state=self.state)
        self.assertRegex(result["saved"], r"^backups/pre-restore-\d{8}T\d{6}Z(-2)?$")
        self.assertFalse((self.state / "db.sqlite3-journal").exists(), "a stale journal stayed beside the database")
        restored = self.contents(data)
        for name in ("secret.key", "config.json", "tls.crt", "tls.key"):
            self.assertEqual((self.state / name).read_bytes(), restored[name], name)
        self.assertEqual((self.state / result["saved"] / "tls.key").read_bytes(), key)

    def test_failure_before_any_replacement_changes_nothing(self):
        data, _ = self.backup()
        (self.state / "config.json").write_text('{"origin": "https://localhost:8443", "port": 9443}')
        before = {name: (self.state / name).read_bytes() for name in BACKUP_FILES}
        original = service_config._temporary
        calls = []

        def failing(state, name, data):
            calls.append(name)
            if len(calls) == 2:
                raise OSError(5, "Input/output error")
            return original(state, name, data)

        with patch.object(service_config, "_temporary", side_effect=failing):
            with self.assertRaisesRegex(ConfigurationError, r"Input/output error\); the previous files are back"):
                self.restore(data, state=self.state)
        self.assertEqual({name: (self.state / name).read_bytes() for name in BACKUP_FILES}, before)
        self.assertEqual([path.name for path in self.state.iterdir() if path.name.startswith(".")], [],
                         "temporary files were left")

    def test_backup_copies_only_committed_data_while_a_writer_holds_the_database(self):
        writer = sqlite3.connect(self.state / "db.sqlite3", isolation_level=None)
        self.addCleanup(writer.close)
        writer.execute("BEGIN IMMEDIATE")
        writer.execute("UPDATE auth_user SET password = 'uncommitted-change'")
        data, _ = self.backup()
        writer.execute("COMMIT")
        restored = self.contents(data)["db.sqlite3"]
        self.assertIn(PASSWORD_HASH.encode(), restored)
        self.assertNotIn(b"uncommitted-change", restored)

    def test_command_line_keeps_the_passphrase_and_secrets_out_of_its_output(self):
        environment = {**os.environ, "RAVEN_WEB_STATE": str(self.state)}

        def run(action, payload):
            return subprocess.run([sys.executable, "-m", "raven_web.service_config", action],
                                  input=json.dumps(payload), env=environment, capture_output=True, text=True)

        made = run("backup", {"passphrase": PASSPHRASE, "user": USER, "instance": INSTANCE})
        self.assertEqual(made.returncode, 0, made.stderr)
        data = base64.b64decode(json.loads(made.stdout)["data"])
        wrong = run("restore-check", {"passphrase": "wrong passphrase 123", "user": USER, "instance": INSTANCE,
                                      "backup": base64.b64encode(data).decode()})
        self.assertEqual((wrong.returncode, wrong.stdout), (1, ""))
        self.assertEqual(wrong.stderr, f"Raven configuration: {UNDECRYPTABLE}\n")
        broken = run("restore", {"passphrase": PASSPHRASE, "user": USER, "instance": INSTANCE, "backup": 5})
        self.assertEqual(broken.returncode, 1)
        self.assertIn("Expected the backup file in base64", broken.stderr)
        key = (self.state / "tls.key").read_bytes().decode()
        for output in (made.stdout, made.stderr, wrong.stderr, broken.stderr):
            for value in (PASSPHRASE, "wrong passphrase 123", "PRIVATE KEY", PASSWORD_HASH,
                          (self.state / "secret.key").read_text(), *map(bytes.decode, key_lines(key.encode()))):
                self.assertNotIn(value, output)


if __name__ == "__main__":
    unittest.main()
