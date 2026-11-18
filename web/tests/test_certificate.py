"""Certificate checks, renewal, import, backups and the gateway's certificate API."""

import datetime
import hashlib
import ipaddress
import json
import os
import stat
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec, ed25519, rsa
from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID
from django.conf import settings
from django.contrib.auth import get_user_model
from django.test import Client, TransactionTestCase, override_settings

from raven_web.bootstrap import initialize
from raven_web.service_config import (
    BACKUPS, KEEP_BACKUPS, ConfigurationError, certificate_info, execute, public_certificate,
)

ORIGIN = "https://localhost:8443"
NOW = datetime.datetime.now(datetime.timezone.utc)
DAY = datetime.timedelta(days=1)
PASSWORD = "Several unique test words 987"


def ec_key(curve=ec.SECP256R1):
    return ec.generate_private_key(curve())


def rsa_key(size=2048):
    return rsa.generate_private_key(public_exponent=65537, key_size=size)


def certificate(key, names=("localhost",), issuer=None, not_before=None, not_after=None, ca=False, usage=None,
                common_name=None):
    """A certificate for key; self-signed unless issuer is (issuer_certificate, issuer_key)."""
    subject = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, common_name or (names[0] if names else "raven"))])
    builder = (x509.CertificateBuilder().subject_name(subject)
               .issuer_name(issuer[0].subject if issuer else subject).public_key(key.public_key())
               .serial_number(x509.random_serial_number())
               .not_valid_before(not_before or NOW - DAY).not_valid_after(not_after or NOW + 200 * DAY)
               .add_extension(x509.BasicConstraints(ca=ca, path_length=None), critical=True))
    if names:
        alternatives = []
        for name in names:
            try:
                alternatives.append(x509.IPAddress(ipaddress.ip_address(name)))
            except ValueError:
                alternatives.append(x509.DNSName(name))
        builder = builder.add_extension(x509.SubjectAlternativeName(alternatives), critical=False)
    if usage is not None:
        builder = builder.add_extension(x509.ExtendedKeyUsage(usage), critical=False)
    signer = issuer[1] if issuer else key
    algorithm = None if isinstance(signer, ed25519.Ed25519PrivateKey) else hashes.SHA256()
    return builder.sign(signer, algorithm)


def pem(*certificates):
    return "".join(item.public_bytes(serialization.Encoding.PEM).decode() for item in certificates)


def key_pem(key, password=None, traditional=False):
    encryption = serialization.BestAvailableEncryption(password) if password else serialization.NoEncryption()
    form = serialization.PrivateFormat.TraditionalOpenSSL if traditional else serialization.PrivateFormat.PKCS8
    return key.private_bytes(serialization.Encoding.PEM, form, encryption).decode()


def key_body(text):
    """The base64 lines of a PEM key: none of them may appear in any output."""
    return [line for line in text.splitlines() if line and not line.startswith("-----") and ":" not in line]


class CertificateFixture(unittest.TestCase):
    origin = ORIGIN

    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="raven-certificate-test-")
        self.addCleanup(self.temporary.cleanup)
        self.state = Path(self.temporary.name)
        with patch.dict(os.environ, {"RAVEN_WEB_STATE": str(self.state)}):
            initialize(["--origin", self.origin])

    def files(self):
        return {path.name: (path.stat().st_ino, path.stat().st_mtime_ns, hashlib.sha256(path.read_bytes()).digest())
                for path in self.state.iterdir() if path.is_file()}

    def check(self, certificate_text, key_text, **extra):
        return execute("certificate-check", {"mode": "import", "certificate": certificate_text, "key": key_text,
                                             **extra}, self.state)

    def apply(self, payload):
        return execute("certificate-apply", payload, self.state)

    def refused(self, certificate_text, key_text, message, **extra):
        before = self.files()
        for action in ("certificate-check", "certificate-apply"):
            with self.subTest(action=action), self.assertRaises(ConfigurationError) as caught:
                execute(action, {"mode": "import", "certificate": certificate_text, "key": key_text, **extra},
                        self.state)
            self.assertIn(message, str(caught.exception))
            for line in key_body(key_text):
                self.assertNotIn(line, str(caught.exception))
        self.assertEqual(self.files(), before, "a refused import changed the data volume")
        self.assertFalse((self.state / BACKUPS).exists(), "a refused import made a backup")
        return str(caught.exception)


class ValidationTests(CertificateFixture):
    def test_supported_keys_are_accepted_and_described(self):
        for name, key in (("EC P-256", ec_key()), ("EC P-384", ec_key(ec.SECP384R1)), ("RSA 2048", rsa_key()),
                          ("RSA 3072", rsa_key(3072))):
            with self.subTest(name):
                result = self.check(pem(certificate(key)), key_pem(key))
                self.assertEqual(result["info"]["key_type"], name)
                self.assertEqual(result["info"]["san"], ["localhost"])
                self.assertEqual(result["warnings"], [])
                self.assertNotIn("PRIVATE KEY", json.dumps(result))
        key = rsa_key()
        result = self.check(pem(certificate(key)), key_pem(key, traditional=True))
        self.assertEqual(result["info"]["key_type"], "RSA 2048")

    def test_mismatched_key_is_refused(self):
        self.refused(pem(certificate(ec_key())), key_pem(ec_key()), "KEY does not match the certificate")

    def test_other_hostname_is_refused_with_the_names_it_covers(self):
        key = ec_key()
        message = self.refused(pem(certificate(key, ("other.example.test", "10.0.0.1"))), key_pem(key),
                               "CERT covers other.example.test, 10.0.0.1, but not localhost")
        self.assertIn(ORIGIN, message)
        self.refused(pem(certificate(key, ())), key_pem(key), "no subject alternative names")

    def test_wildcards_do_not_cover_the_origin(self):
        state = Path(self.temporary.name)
        config = json.loads((state / "config.json").read_text())
        (state / "config.json").write_text(json.dumps({**config, "origin": "https://raven.example.test"}))
        key = ec_key()
        self.refused(pem(certificate(key, ("*.example.test",))), key_pem(key), "wildcard names do not count")
        result = self.check(pem(certificate(key, ("*.example.test", "raven.example.test"))), key_pem(key))
        self.assertEqual(result["info"]["san"], ["*.example.test", "raven.example.test"])

    def test_validity_window(self):
        key = ec_key()
        self.refused(pem(certificate(key, not_before=NOW - 30 * DAY, not_after=NOW - DAY)), key_pem(key),
                     "CERT expired on")
        self.refused(pem(certificate(key, not_before=NOW + DAY)), key_pem(key), "CERT is not valid until")
        self.refused(pem(certificate(key, not_after=NOW + DAY / 2)), key_pem(key), "in less than a day")
        early = NOW + datetime.timedelta(minutes=4)
        result = self.check(pem(certificate(key, not_before=early)), key_pem(key))
        self.assertEqual(len(result["warnings"]), 1)
        self.assertIn("still ahead by this server's clock", result["warnings"][0])
        result = self.check(pem(certificate(key, not_after=NOW + 10 * DAY + datetime.timedelta(hours=1))),
                            key_pem(key))
        self.assertEqual(len(result["warnings"]), 1)
        self.assertIn("(10 days left)", result["warnings"][0])

    def test_encrypted_and_unsupported_keys_are_refused(self):
        for key, text in ((ec_key(), None), (rsa_key(), "traditional")):
            with self.subTest(text):
                encrypted = key_pem(key, b"passphrase", traditional=bool(text))
                self.refused(pem(certificate(key)), encrypted, "KEY is encrypted")
        weak = rsa_key(1024)
        self.refused(pem(certificate(weak)), key_pem(weak), "KEY is RSA 1024")
        edwards = ed25519.Ed25519PrivateKey.generate()
        self.refused(pem(certificate(edwards)), key_pem(edwards), "KEY is Ed25519")
        curve = ec_key(ec.SECP521R1)
        self.refused(pem(certificate(curve)), key_pem(curve), "EC curve secp521r1")

    def test_ip_origins_need_ip_sans(self):
        config = json.loads((self.state / "config.json").read_text())
        (self.state / "config.json").write_text(json.dumps({**config, "origin": "https://127.0.0.1:8443"}))
        key = ec_key()
        result = self.check(pem(certificate(key, ("127.0.0.1",))), key_pem(key))
        self.assertEqual(result["info"]["san"], ["127.0.0.1"])
        self.refused(pem(certificate(key, ("localhost",), common_name="127.0.0.1")), key_pem(key),
                     "but not 127.0.0.1")
        (self.state / "config.json").write_text(json.dumps({**config, "origin": "https://[::1]:8443"}))
        self.check(pem(certificate(key, ("::1",))), key_pem(key))

    def test_chain_must_start_with_the_server_certificate(self):
        root_key, intermediate_key, leaf_key = ec_key(), ec_key(), ec_key()
        root = certificate(root_key, (), ca=True, common_name="Raven test root")
        intermediate = certificate(intermediate_key, (), issuer=(root, root_key), ca=True,
                                   common_name="Raven test intermediate")
        leaf = certificate(leaf_key, issuer=(intermediate, intermediate_key),
                           usage=[ExtendedKeyUsageOID.SERVER_AUTH])
        result = self.check(pem(leaf, intermediate), key_pem(leaf_key))
        self.assertEqual(result["info"]["chain"], 2)
        self.assertFalse(result["info"]["self_signed"])
        self.check(pem(leaf, intermediate, root), key_pem(leaf_key))
        self.refused(pem(intermediate, leaf), key_pem(leaf_key), "KEY belongs to certificate 2 in CERT")
        self.refused(pem(leaf, root), key_pem(leaf_key), "certificate 1 is not issued by certificate 2")
        self.refused(pem(leaf, root, intermediate), key_pem(leaf_key), "CERT is out of order")

    def test_client_only_certificates_are_refused(self):
        key = ec_key()
        self.refused(pem(certificate(key, usage=[ExtendedKeyUsageOID.CLIENT_AUTH])), key_pem(key),
                     "lacks serverAuth")

    def test_broken_pem_and_keys_in_the_certificate_file_are_refused(self):
        key = ec_key()
        good = pem(certificate(key))
        self.refused("not a certificate", key_pem(key), "CERT is not a PEM file")
        self.refused(good[:-30], key_pem(key), "CERT is not a PEM file")
        broken = good.replace(good.splitlines()[3], "A" * 64)
        self.refused(broken, key_pem(key), "CERT: block 1 is not a valid X.509 certificate")
        self.refused(good + key_pem(key), key_pem(key), "CERT holds a private key")
        self.refused(good, "garbage", "KEY is not a PEM file")
        self.refused(good, good, "KEY must hold exactly one PEM private key (found 0)")
        self.refused(good, key_pem(key) + key_pem(key), "found 2")
        text = key_pem(key)
        damaged = text.replace(key_body(text)[-1] + "\n", "")
        self.refused(good, damaged, "KEY is not a valid PEM private key")
        self.refused(good, key_pem(key).replace("A", "Ä", 1), "non-ASCII")

    def test_key_after_a_certificate_in_the_key_file_is_accepted(self):
        key = ec_key()
        result = self.check(pem(certificate(key)), pem(certificate(key)) + key_pem(key))
        self.assertEqual(result["info"]["key_type"], "EC P-256")

    def test_running_origin_must_stay_covered(self):
        key = ec_key()
        self.refused(pem(certificate(key)), key_pem(key), "but not old.example.test, the hostname of "
                     "https://old.example.test:9443", origins=["https://old.example.test:9443"])
        self.check(pem(certificate(key, ("localhost", "old.example.test"))), key_pem(key),
                   origins=["https://old.example.test:9443"])

    def test_renew_request_names_and_days(self):
        result = execute("certificate-check", {"mode": "renew", "names": ["Raven.Example.TEST", "localhost",
                                                                          "[::1]", "10.0.0.7", "raven.example.test"],
                                               "days": 400}, self.state)
        self.assertEqual(result["info"]["san"], ["localhost", "raven.example.test", "::1", "10.0.0.7"])
        self.assertEqual(result["warnings"], [])
        result = execute("certificate-check", {"mode": "renew", "days": 10}, self.state)
        self.assertIn("(10 days left)", result["warnings"][0])
        invalid = {"names": (["*.example.test"], ["bad_name"], ["127.1"], ["fe80::1%eth0"], [""], [" localhost"],
                             ["a" * 64 + ".test"], "localhost", [1]),
                   "days": (0, 826, "365", True, 1.5)}
        for field, values in invalid.items():
            for value in values:
                with self.subTest(field=field, value=value), self.assertRaises(ConfigurationError):
                    execute("certificate-check", {"mode": "renew", field: value}, self.state)
        for payload in ({"mode": "renew", "key": "x"}, {"mode": "other"}, [], {"mode": "import", "names": []}):
            with self.subTest(payload=payload), self.assertRaises(ConfigurationError):
                execute("certificate-check", payload, self.state)


class ApplyTests(CertificateFixture):
    def pair(self):
        return (self.state / "tls.crt").read_bytes(), (self.state / "tls.key").read_bytes()

    def test_renew_replaces_the_pair_and_keeps_a_private_backup(self):
        before = self.pair()
        identity = {name: (self.state / name).read_bytes() for name in ("config.json", "secret.key")}
        result = self.apply({"mode": "renew", "names": ["raven.example.test", "10.0.0.7"], "days": 30})
        certificate_bytes, key_bytes = self.pair()
        self.assertNotEqual(certificate_bytes, before[0])
        self.assertNotEqual(key_bytes, before[1])
        self.assertEqual(result["certificate"], certificate_bytes.decode())
        self.assertEqual(result["previous"], before[0].decode())
        self.assertNotIn("PRIVATE KEY", json.dumps(result))
        for line in key_body(key_bytes.decode()):
            self.assertNotIn(line, json.dumps(result))
        leaf = x509.load_pem_x509_certificate(certificate_bytes)
        self.assertEqual(result["info"]["san"], ["localhost", "raven.example.test", "10.0.0.7"])
        self.assertEqual(result["info"]["fingerprint"], leaf.fingerprint(hashes.SHA256()).hex(":").upper())
        self.assertEqual((leaf.not_valid_after_utc - leaf.not_valid_before_utc).days, 30)
        self.assertEqual(result["info"]["key_type"], "RSA 2048")
        public = serialization.load_pem_private_key(key_bytes, None).public_key()
        self.assertEqual(public.public_numbers(), leaf.public_key().public_numbers())
        for name in ("tls.crt", "tls.key"):
            self.assertEqual(stat.S_IMODE((self.state / name).stat().st_mode), 0o600)
        backup = self.state / result["backup"]
        self.assertRegex(backup.name, r"\Atls-\d{8}T\d{6}Z\Z")
        self.assertEqual(stat.S_IMODE((self.state / BACKUPS).stat().st_mode), 0o700)
        self.assertEqual(stat.S_IMODE(backup.stat().st_mode), 0o700)
        self.assertEqual(((backup / "tls.crt").read_bytes(), (backup / "tls.key").read_bytes()), before)
        for name in ("tls.crt", "tls.key"):
            self.assertEqual(stat.S_IMODE((backup / name).stat().st_mode), 0o600)
        self.assertTrue(result["complete_backup"])
        self.assertEqual({name: (self.state / name).read_bytes() for name in identity}, identity)
        self.assertEqual([path.name for path in self.state.iterdir() if path.name.startswith(".")], [])

    def test_import_writes_only_the_reencoded_chain_and_key(self):
        key = rsa_key()
        text = "issuer note\n" + pem(certificate(key, ("localhost", "raven.example.test")))
        result = self.apply({"mode": "import", "certificate": text, "key": key_pem(key, traditional=True)})
        certificate_bytes, key_bytes = self.pair()
        self.assertTrue(certificate_bytes.startswith(b"-----BEGIN CERTIFICATE-----"))
        self.assertNotIn(b"issuer note", certificate_bytes)
        self.assertTrue(key_bytes.startswith(b"-----BEGIN PRIVATE KEY-----"))
        self.assertEqual(serialization.load_pem_private_key(key_bytes, None).private_numbers(), key.private_numbers())
        self.assertEqual(result["info"]["san"], ["localhost", "raven.example.test"])
        self.assertNotIn("PRIVATE KEY", json.dumps(result))

    def test_only_the_newest_backups_are_kept(self):
        created = []
        moments = [NOW + datetime.timedelta(seconds=index) for index in range(KEEP_BACKUPS + 2)]
        for moment in moments:
            with patch("raven_web.service_config.datetime") as clock:
                clock.datetime.now.return_value = moment
                clock.timedelta = datetime.timedelta
                clock.timezone = datetime.timezone
                created.append(self.apply({"mode": "renew"})["backup"])
        kept = sorted(path.name for path in (self.state / BACKUPS).iterdir())
        self.assertEqual(len(kept), KEEP_BACKUPS)
        self.assertEqual(kept, sorted(name.split("/")[1] for name in created[-KEEP_BACKUPS:]))
        (self.state / BACKUPS / "unrelated").mkdir()
        with patch("raven_web.service_config.datetime") as clock:
            clock.datetime.now.return_value = NOW - datetime.timedelta(days=1)
            clock.timedelta = datetime.timedelta
            clock.timezone = datetime.timezone
            set_back = self.apply({"mode": "renew"})["backup"]
        self.assertTrue((self.state / set_back).is_dir(), "a backup made with the clock set back was pruned")
        self.assertEqual(len([path for path in (self.state / BACKUPS).iterdir() if path.name != "unrelated"]),
                         KEEP_BACKUPS)
        with patch("raven_web.service_config.datetime") as clock:
            clock.datetime.now.return_value = moments[-1]
            clock.timedelta = datetime.timedelta
            clock.timezone = datetime.timezone
            same_second = self.apply({"mode": "renew"})["backup"]
        self.assertTrue(same_second.endswith("-2"), same_second)
        self.assertTrue((self.state / BACKUPS / "unrelated").is_dir())

    def test_restore_puts_the_backup_back(self):
        before = self.pair()
        result = self.apply({"mode": "renew"})
        restored = execute("certificate-restore", {"backup": result["backup"].split("/")[1]}, self.state)
        self.assertEqual(self.pair(), before)
        self.assertEqual(restored["certificate"], before[0].decode())
        self.assertNotIn("PRIVATE KEY", json.dumps(restored))
        for name in ("tls.crt", "tls.key"):
            self.assertEqual(stat.S_IMODE((self.state / name).stat().st_mode), 0o600)
        for payload in ({"backup": "../tls.key"}, {"backup": "tls-1"}, {"backup": result["backup"]}, {}):
            with self.subTest(payload=payload), self.assertRaises(ConfigurationError):
                execute("certificate-restore", payload, self.state)
        with self.assertRaisesRegex(ConfigurationError, "does not hold both"):
            execute("certificate-restore", {"backup": "tls-20000101T000000Z"}, self.state)

    def test_failed_write_leaves_the_previous_pair(self):
        before = self.pair()
        original = os.replace
        calls = []

        def replace(source, target):
            calls.append(Path(target).name)
            if len(calls) == 2:
                raise OSError(28, "No space left on device")
            return original(source, target)

        with patch("os.replace", side_effect=replace), \
                self.assertRaisesRegex(ConfigurationError, "No space left on device.*previous certificate is back"):
            self.apply({"mode": "renew"})
        self.assertEqual(self.pair(), before)
        self.assertEqual([path.name for path in self.state.iterdir() if path.name.startswith(".")], [])

    def test_results_carry_only_certificates_from_a_hand_edited_file(self):
        key = ec_key()
        leaf = certificate(key)
        (self.state / "tls.crt").write_text(pem(leaf) + key_pem(key))
        (self.state / "tls.key").write_text(key_pem(key))
        result = self.apply({"mode": "renew"})
        self.assertEqual(result["previous"], pem(leaf))
        restored = execute("certificate-restore", {"backup": result["backup"].split("/")[1]}, self.state)
        self.assertEqual(restored["certificate"], pem(leaf))
        self.assertNotIn("PRIVATE KEY", json.dumps([result, restored]))

    def test_renew_repairs_a_missing_certificate(self):
        (self.state / "tls.crt").unlink()
        result = self.apply({"mode": "renew"})
        self.assertFalse(result["complete_backup"])
        self.assertIsNone(result["previous"])
        self.assertTrue((self.state / "tls.crt").exists())

    def test_cli_keeps_the_key_out_of_its_output(self):
        key = ec_key()
        environment = {**os.environ, "RAVEN_WEB_STATE": str(self.state)}
        request = {"mode": "import", "certificate": pem(certificate(key)), "key": key_pem(key)}
        result = subprocess.run([sys.executable, "-m", "raven_web.service_config", "certificate-apply"],
                                input=json.dumps(request), env=environment, capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        refused = subprocess.run([sys.executable, "-m", "raven_web.service_config", "certificate-check"],
                                 input=json.dumps({**request, "key": key_pem(ec_key())}), env=environment,
                                 capture_output=True, text=True)
        self.assertEqual(refused.returncode, 1)
        self.assertEqual(refused.stdout, "")
        self.assertIn("KEY does not match", refused.stderr)
        broken = subprocess.run([sys.executable, "-m", "raven_web.service_config", "certificate-check"],
                                input=json.dumps({**request, "unexpected": key_pem(key)}), env=environment,
                                capture_output=True, text=True)
        self.assertEqual(broken.returncode, 1)
        for output in (result.stdout, result.stderr, refused.stderr, broken.stderr):
            self.assertNotIn("PRIVATE KEY", output)
            for line in key_body(key_pem(key)):
                self.assertNotIn(line, output)

    def test_inspect_reports_chain_issuer_and_origin_coverage(self):
        result = execute("inspect", state=self.state)
        self.assertEqual(result["certificate_info"]["chain"], 1)
        self.assertTrue(result["certificate_info"]["self_signed"])
        self.assertTrue(result["certificate_info"]["origin_covered"])
        key = ec_key()
        (self.state / "tls.crt").write_text(pem(certificate(key, ("other.example.test",))) + key_pem(key))
        output = json.dumps(execute("inspect", state=self.state))
        self.assertIn('"origin_covered": false', output)
        self.assertNotIn("PRIVATE KEY", output)


class GatewayCertificateTests(TransactionTestCase):
    def setUp(self):
        self.user = get_user_model().objects.create_user(username="owner", password=PASSWORD)
        self.temporary = tempfile.TemporaryDirectory(prefix="raven-certificate-api-")
        self.addCleanup(self.temporary.cleanup)
        self.state = Path(self.temporary.name)
        self.key = ec_key()
        self.leaf = certificate(self.key, ("localhost", "10.0.0.7"), not_after=NOW + 20 * DAY)
        (self.state / "tls.crt").write_text(pem(self.leaf))
        self.settings = override_settings(STATE_DIR=self.state)
        self.settings.enable()
        self.addCleanup(self.settings.disable)
        self.client = Client(HTTP_HOST="localhost:8443")

    def test_login_is_required(self):
        for path in ("/api/certificate/", "/api/certificate/download/"):
            with self.subTest(path=path):
                response = self.client.get(path, secure=True)
                self.assertEqual(response.status_code, 401)
                self.assertEqual(response.json()["code"], "auth_required")

    def test_info_describes_the_served_leaf(self):
        self.client.force_login(self.user)
        response = self.client.get("/api/certificate/", secure=True)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response["Cache-Control"], "no-store")
        body = response.json()
        self.assertEqual(set(body), {"names", "not_before", "not_after", "days_left", "fingerprint",
                                     "origin_covered"})
        self.assertEqual(body["names"], ["localhost", "10.0.0.7"])
        self.assertEqual(body["fingerprint"], self.leaf.fingerprint(hashes.SHA256()).hex(":").upper())
        self.assertEqual(datetime.datetime.fromisoformat(body["not_after"]), self.leaf.not_valid_after_utc)
        self.assertEqual(datetime.datetime.fromisoformat(body["not_before"]).utcoffset(), datetime.timedelta(0))
        self.assertIn(body["days_left"], (19, 20))
        self.assertIs(body["origin_covered"], True)
        (self.state / "tls.crt").write_text(pem(certificate(self.key, ("other.example.test",))))
        self.assertIs(self.client.get("/api/certificate/", secure=True).json()["origin_covered"], False)
        response = self.client.post("/api/certificate/", secure=True)
        self.assertEqual(response.status_code, 405)

    def test_download_serves_only_the_public_chain(self):
        self.client.force_login(self.user)
        root_key = ec_key()
        root = certificate(root_key, (), ca=True, common_name="Raven test root")
        (self.state / "tls.crt").write_text(pem(self.leaf, root) + key_pem(self.key))
        response = self.client.get("/api/certificate/download/", secure=True)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response["Content-Type"], "application/x-x509-ca-cert")
        self.assertEqual(response["Content-Disposition"], 'attachment; filename="raven-localhost.crt"')
        self.assertEqual(response["Cache-Control"], "no-store")
        self.assertEqual(response["X-Content-Type-Options"], "nosniff")
        self.assertEqual(response.content.decode(), pem(self.leaf, root))
        self.assertNotIn(b"PRIVATE KEY", response.content)
        for line in key_body(key_pem(self.key)):
            self.assertNotIn(line.encode(), response.content)
        info = self.client.get("/api/certificate/", secure=True)
        self.assertNotIn(b"PRIVATE KEY", info.content)

    def test_unreadable_certificate_is_reported(self):
        self.client.force_login(self.user)
        (self.state / "tls.crt").write_text("broken")
        for path in ("/api/certificate/", "/api/certificate/download/"):
            with self.subTest(path=path):
                response = self.client.get(path, secure=True)
                self.assertEqual(response.status_code, 500)
                self.assertEqual(response.json()["code"], "certificate_unavailable")

    def test_helpers_match_the_running_settings(self):
        info = certificate_info(pem(self.leaf), settings.ORIGIN_PARTS.hostname)
        self.assertTrue(info["origin_covered"])
        self.assertEqual(public_certificate(pem(self.leaf) + key_pem(self.key)), pem(self.leaf))
