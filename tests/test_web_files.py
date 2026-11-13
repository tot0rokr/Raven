import errno
import io
import json
import os
from pathlib import Path
import socket
import stat
import tempfile
import threading
import time
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import raven_web_host
from raven_web_host import (
    CHUNK_SIZE, FREE_SPACE_MARGIN, MAX_DOWNLOADS, MAX_FILE_SIZE, MAX_UPLOADS, STAGING, STALE_UPLOAD_SECONDS, UPLOAD_IDLE_SECONDS,
    FileError, Files, Raven, RequestError, Server,
)

SOURCE = Path(__file__).resolve().parents[1]


class FileFixture(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="raven-files-test-")
        self.addCleanup(self.temporary.cleanup)
        root = Path(self.temporary.name)
        self.home = root / "home"
        self.downloads = self.home / "Downloads"
        self.downloads.mkdir(parents=True)
        self.outside = root / "outside"
        self.outside.mkdir()
        (self.outside / "secret").write_text("host secret\n")
        self.files = Files(self.downloads)
        # The broker runs with umask 077; uploads must still come out group-readable.
        self.addCleanup(os.umask, os.umask(0o077))
        self.addCleanup(self.cancel_all)

    def cancel_all(self):
        for token in list(self.files.uploads):
            self.files.dispatch({"action": "upload_cancel", "token": token})

    def call(self, action, **arguments):
        return self.files.dispatch({"action": action, **arguments})

    def assert_code(self, code, action, **arguments):
        with self.assertRaises(FileError) as caught:
            if action == "download":
                self.files.open_download({"action": action, **arguments})
            else:
                self.call(action, **arguments)
        self.assertEqual(caught.exception.code, code, arguments)

    def start(self, name="file.bin", size=0, path="", overwrite=False):
        return self.call("upload_start", path=path, name=name, size=size, overwrite=overwrite)["token"]

    def chunk(self, token, offset, data):
        source = io.BytesIO(data)
        result = self.files.chunk({"action": "upload_chunk", "token": token, "offset": offset, "size": len(data)},
                                  source)
        self.assertEqual(source.tell(), len(data))
        return result

    def finish(self, token, overwrite=False):
        return self.call("upload_finish", token=token, overwrite=overwrite)

    def temporaries(self):
        staging = self.downloads / STAGING
        return sorted(path.name for path in staging.iterdir()) if staging.exists() else []


class PathBoundaryTests(FileFixture):
    def test_listing_types_order_and_hidden_staging(self):
        (self.downloads / "b-dir").mkdir()
        (self.downloads / "A-dir").mkdir()
        (self.downloads / "a.txt").write_bytes(b"abc")
        (self.downloads / "링크").symlink_to(self.outside)
        os.mkfifo(self.downloads / "pipe")
        self.call("upload_cancel", token=self.start())
        result = self.call("files_list", path="")
        self.assertEqual(result["path"], "")
        self.assertEqual([entry["name"] for entry in result["entries"]], ["A-dir", "b-dir", "a.txt", "pipe", "링크"])
        types = {entry["name"]: entry["type"] for entry in result["entries"]}
        self.assertEqual(types, {"A-dir": "dir", "b-dir": "dir", "a.txt": "file", "pipe": "other", "링크": "other"})
        entry = next(entry for entry in result["entries"] if entry["name"] == "a.txt")
        self.assertEqual(entry["size"], 3)
        self.assertIsInstance(entry["mtime"], int)
        self.assertEqual((result["truncated"], result["skipped"]), (False, 0))
        self.assertTrue((self.downloads / STAGING).is_dir())

    def test_listing_a_subfolder_shows_a_staging_named_entry(self):
        (self.downloads / "sub" / STAGING).mkdir(parents=True)
        result = self.call("files_list", path="sub")
        self.assertEqual([entry["name"] for entry in result["entries"]], [STAGING])
        self.assertEqual(result["path"], "sub")

    def test_listing_skips_names_that_are_not_utf8(self):
        (self.downloads / "ok.txt").write_text("ok")
        descriptor = os.open(os.fsencode(self.downloads) + b"/\xff\xfe.bin", os.O_WRONLY | os.O_CREAT, 0o600)
        os.close(descriptor)
        result = self.call("files_list", path="")
        self.assertEqual([entry["name"] for entry in result["entries"]], ["ok.txt"])
        self.assertEqual(result["skipped"], 1)

    def test_listing_is_bounded(self):
        for index in range(5):
            (self.downloads / f"f{index}").write_text("x")
        with patch.object(raven_web_host, "MAX_ENTRIES", 3):
            result = self.call("files_list", path="")
        self.assertEqual(len(result["entries"]), 3)
        self.assertTrue(result["truncated"])

    def test_listing_scan_is_bounded_even_by_skipped_names(self):
        for index in range(4):
            os.close(os.open(os.fsencode(self.downloads) + b"/\xff%d" % index, os.O_WRONLY | os.O_CREAT, 0o600))
        for index in range(2):
            (self.downloads / f"ok{index}").write_text("x")
        with patch.object(raven_web_host, "MAX_SCANNED", 3):
            result = self.call("files_list", path="")
        self.assertTrue(result["truncated"])
        self.assertLessEqual(len(result["entries"]) + result["skipped"], 3)
        with patch.object(raven_web_host, "MAX_SCANNED", 6):
            result = self.call("files_list", path="")
        self.assertFalse(result["truncated"])
        self.assertEqual((len(result["entries"]), result["skipped"]), (2, 4))

    def test_symlinked_downloads_is_refused(self):
        self.downloads.rmdir()
        self.downloads.symlink_to(self.outside)
        self.assert_code("downloads_missing", "files_list", path="")
        self.assert_code("downloads_missing", "download", path="secret")
        self.assert_code("downloads_missing", "upload_start", path="", name="new", size=1, overwrite=True)
        self.assertEqual(sorted(path.name for path in self.outside.iterdir()), ["secret"])

    def test_symlinked_middle_folder_is_never_followed(self):
        (self.downloads / "real").mkdir()
        (self.downloads / "real" / "mid").symlink_to(self.outside)
        self.assert_code("not_a_directory", "files_list", path="real/mid")
        self.assert_code("not_a_directory", "download", path="real/mid/secret")
        self.assert_code("not_a_directory", "upload_start", path="real/mid", name="new", size=1, overwrite=True)
        (self.downloads / "up").symlink_to("..")
        self.assert_code("not_a_directory", "files_list", path="up")
        self.assert_code("not_a_directory", "download", path="up/Downloads/real")
        self.assertEqual(sorted(path.name for path in self.outside.iterdir()), ["secret"])

    def test_symlinked_file_is_never_followed(self):
        (self.downloads / "leaf").symlink_to(self.outside / "secret")
        self.assert_code("not_a_file", "download", path="leaf")
        self.assert_code("not_a_directory", "files_list", path="leaf")
        self.assert_code("file_exists", "upload_start", path="", name="leaf", size=3, overwrite=False)
        # Overwriting replaces the link itself, never the file it points to.
        token = self.start("leaf", 3, overwrite=True)
        self.chunk(token, 0, b"new")
        self.finish(token, overwrite=True)
        self.assertFalse((self.downloads / "leaf").is_symlink())
        self.assertEqual((self.downloads / "leaf").read_bytes(), b"new")
        self.assertEqual((self.outside / "secret").read_text(), "host secret\n")

    def test_invalid_paths_are_refused(self):
        (self.downloads / "a").mkdir()
        for path in ("..", "../x", "a/../a", ".", "./a", "/etc", "a//a", "a/", "a\0", STAGING, f"{STAGING}/x",
                     "\ud800", "x" * 256, "/".join(["a"] * 33), "a/" * 2100 + "a", 5, None, ["a"]):
            with self.subTest(path=path):
                self.assert_code("invalid_path", "files_list", path=path)
                self.assert_code("invalid_path", "download", path=path)
                self.assert_code("invalid_path", "upload_start", path=path, name="x", size=1, overwrite=False)
        self.assertEqual(self.temporaries(), [])

    def test_invalid_names_are_refused(self):
        for name in ("", ".", "..", "a/b", "a\0b", "가" * 86, "\udcff", None, 1, STAGING):
            with self.subTest(name=name):
                self.assert_code("invalid_path", "upload_start", path="", name=name, size=1, overwrite=False)
        (self.downloads / "sub").mkdir()
        for name, path in (("가" * 85, ""), (STAGING, "sub")):
            with self.subTest(name=name, path=path):
                token = self.start(name, 1, path=path)
                self.chunk(token, 0, b"x")
                self.finish(token)
                self.assertEqual((self.downloads / path / name).read_bytes(), b"x")

    def test_downloads_missing_is_not_created(self):
        missing = Files(self.home / "missing")
        for action, arguments in (("files_list", {"path": ""}),
                                  ("upload_start", {"path": "", "name": "x", "size": 1, "overwrite": False})):
            with self.subTest(action=action), self.assertRaises(FileError) as caught:
                missing.dispatch({"action": action, **arguments})
            self.assertEqual(caught.exception.code, "downloads_missing")
        self.assertFalse((self.home / "missing").exists())
        (self.home / "plain").write_text("not a folder")
        with self.assertRaises(FileError) as caught:
            Files(self.home / "plain").dispatch({"action": "files_list", "path": ""})
        self.assertEqual(caught.exception.code, "downloads_missing")

    def test_default_downloads_is_the_broker_home(self):
        with patch.dict(os.environ, {"HOME": str(self.home)}):
            self.assertEqual(Files().downloads, self.downloads)

    def test_downloads_refuse_everything_but_regular_files(self):
        (self.downloads / "folder").mkdir()
        os.mkfifo(self.downloads / "fifo")
        listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.addCleanup(listener.close)
        listener.bind(str(self.downloads / "socket"))
        for path in ("fifo", "folder", "socket", ""):
            with self.subTest(path=path):
                self.assert_code("not_a_file", "download", path=path)
        self.assert_code("not_found", "download", path="missing")
        self.assert_code("not_found", "files_list", path="missing")
        (self.downloads / "plain").write_text("x")
        self.assert_code("not_a_directory", "files_list", path="plain")
        self.assert_code("not_a_directory", "download", path="plain/x")

    @unittest.skipIf(os.geteuid() == 0, "root reads every file")
    def test_unreadable_file_is_permission_denied(self):
        private = self.downloads / "private.bin"
        private.write_text("secret")
        private.chmod(0)
        self.assert_code("permission_denied", "download", path="private.bin")

    def test_open_download_returns_the_regular_file(self):
        (self.downloads / "sub").mkdir()
        (self.downloads / "sub" / "한글.txt").write_bytes(b"12345")
        name, descriptor, size = self.files.open_download({"path": "sub/한글.txt"})
        os.close(descriptor)
        self.assertEqual((name, size), ("한글.txt", 5))


class UploadTests(FileFixture):
    def test_upload_lands_with_group_access_and_no_temporary(self):
        data = os.urandom(150000)
        token = self.start("한글 파일.bin", len(data))
        self.assertRegex(token, r"\A[A-Za-z0-9_-]{32}\Z")
        self.assertEqual(self.temporaries(), [token + ".part"])
        self.assertEqual(stat.S_IMODE((self.downloads / STAGING / (token + ".part")).stat().st_mode), 0o660)
        self.assertEqual(self.chunk(token, 0, data[:100000]), {"received": 100000})
        self.assertEqual(self.chunk(token, 100000, data[100000:]), {"received": len(data)})
        result = self.finish(token)
        target = self.downloads / "한글 파일.bin"
        info = target.stat()
        self.assertEqual(result, {"name": "한글 파일.bin", "size": len(data), "mtime": int(info.st_mtime)})
        self.assertEqual(target.read_bytes(), data)
        self.assertEqual(stat.S_IMODE(info.st_mode), 0o660)
        self.assertEqual(info.st_uid, os.getuid())
        self.assertEqual(self.temporaries(), [])
        self.assertEqual(self.files.uploads, {})
        staging = (self.downloads / STAGING).stat()
        self.assertEqual((stat.S_IMODE(staging.st_mode), staging.st_uid), (0o700, os.getuid()))

    def test_empty_file_upload(self):
        self.finish(self.start("empty", 0))
        self.assertEqual((self.downloads / "empty").read_bytes(), b"")

    def test_chunks_must_arrive_in_order_and_within_the_declared_size(self):
        token = self.start("ordered", 10)
        self.assert_code("offset_mismatch", "upload_finish", token=token, overwrite=False)
        for offset, data, code in ((5, b"x", "offset_mismatch"), (0, b"x" * 11, "too_large")):
            with self.subTest(offset=offset, size=len(data)), self.assertRaises(FileError) as caught:
                self.chunk(token, offset, data)
            self.assertEqual(caught.exception.code, code)
        self.chunk(token, 0, b"abcdef")
        with self.assertRaises(FileError) as caught:
            self.chunk(token, 6, b"ghijk")
        self.assertEqual(caught.exception.code, "too_large")
        with self.assertRaises(FileError) as caught:
            self.chunk(token, 0, b"abcd")
        self.assertEqual(caught.exception.code, "offset_mismatch")
        self.chunk(token, 6, b"ghij")
        self.finish(token)
        self.assertEqual((self.downloads / "ordered").read_bytes(), b"abcdefghij")

    def test_malformed_chunks_are_refused(self):
        token = self.start("x", 10)
        for request in ({"size": CHUNK_SIZE + 1, "offset": 0}, {"size": -1, "offset": 0}, {"size": "1", "offset": 0},
                        {"size": 1, "offset": -1}, {"size": 1, "offset": True}, {"size": True, "offset": 0}):
            with self.subTest(request=request), self.assertRaises(RequestError):
                self.files.chunk({"token": token, **request}, io.BytesIO(b"x"))
        with self.assertRaises(FileError) as caught:
            self.chunk("A" * 32, 0, b"x")
        self.assertEqual(caught.exception.code, "upload_unknown")
        with self.assertRaises(FileError) as caught:
            self.files.chunk({"token": "../" + "A" * 29, "offset": 0, "size": 1}, io.BytesIO(b"x"))
        self.assertEqual(caught.exception.code, "upload_unknown")
        with self.assertRaises(RequestError):
            self.files.chunk({"token": token, "offset": 0, "size": 4}, io.BytesIO(b"ab"))
        self.assertEqual(os.path.getsize(self.downloads / STAGING / (token + ".part")), 0)
        self.chunk(token, 0, b"0123456789")

    def test_declared_size_is_limited(self):
        self.assert_code("too_large", "upload_start", path="", name="big", size=MAX_FILE_SIZE + 1, overwrite=False)
        for size in (-1, True, 1.5, "1", None):
            with self.subTest(size=size), self.assertRaises(RequestError) as caught:
                self.call("upload_start", path="", name="x", size=size, overwrite=False)
            self.assertNotIsInstance(caught.exception, FileError)
        with self.assertRaises(RequestError):
            self.call("upload_start", path="", name="x", size=1, overwrite="yes")
        self.assertEqual(self.temporaries(), [])

    def test_open_uploads_are_bounded(self):
        tokens = [self.start(f"f{index}", 1) for index in range(MAX_UPLOADS)]
        self.assert_code("busy", "upload_start", path="", name="extra", size=1, overwrite=False)
        self.call("upload_cancel", token=tokens.pop())
        tokens.append(self.start("extra", 1))
        self.assertEqual(len(self.temporaries()), MAX_UPLOADS)

    def test_idle_uploads_expire(self):
        token = self.start("idle", 2)
        self.chunk(token, 0, b"a")
        self.files.uploads[token].touched -= UPLOAD_IDLE_SECONDS + 1
        with self.assertRaises(FileError) as caught:
            self.chunk(token, 1, b"b")
        self.assertEqual(caught.exception.code, "upload_unknown")
        self.assertEqual(self.files.uploads, {})
        self.assertEqual(self.temporaries(), [])
        self.assertFalse((self.downloads / "idle").exists())

    def test_a_failing_close_still_releases_both_descriptors(self):
        token = self.start("x", 1)
        upload = self.files.uploads[token]
        closed = []
        close = os.close

        def failing_close(descriptor):
            closed.append(descriptor)
            close(descriptor)
            if descriptor == upload.descriptor:
                raise OSError(errno.EDQUOT, "Disk quota exceeded")

        with patch("raven_web_host.os.close", side_effect=failing_close), self.assertRaises(OSError):
            upload.close(remove=True)
        self.assertEqual(closed, [upload.descriptor, upload.staging])
        self.assertEqual(self.temporaries(), [])

    def test_expiry_survives_a_failing_close(self):
        tokens = [self.start(f"idle{index}", 1) for index in range(3)]
        for token in tokens:
            self.files.uploads[token].touched -= UPLOAD_IDLE_SECONDS + 1
        first = self.files.uploads[tokens[0]]
        close = os.close
        failed = []

        def failing_close(descriptor):
            close(descriptor)
            # Only once: the kernel reuses the descriptor number for the next open.
            if descriptor == first.descriptor and not failed:
                failed.append(descriptor)
                raise OSError(errno.EIO, "Input/output error")

        with patch("raven_web_host.os.close", side_effect=failing_close):
            fresh = self.start("fresh", 1)
        self.assertEqual(list(self.files.uploads), [fresh])
        self.assertEqual(self.temporaries(), [fresh + ".part"])

    def test_concurrent_starts_cannot_both_take_the_last_free_space(self):
        entered, gate = threading.Event(), threading.Event()
        remove_stale = self.files.remove_stale
        outcome = {}

        def hold_the_first(staging):
            if not entered.is_set():
                entered.set()
                gate.wait(10)
            remove_stale(staging)

        def first():
            try:
                outcome["first"] = self.start("first", 100)
            except FileError as error:
                outcome["first"] = error.code

        free = SimpleNamespace(f_bavail=FREE_SPACE_MARGIN + 150, f_frsize=1)
        with patch("raven_web_host.os.statvfs", return_value=free), \
                patch.object(self.files, "remove_stale", side_effect=hold_the_first):
            thread = threading.Thread(target=first)
            thread.start()
            self.assertTrue(entered.wait(10))
            second = self.start("second", 100)
            gate.set()
            thread.join(10)
        self.assertEqual(outcome["first"], "no_space")
        self.assertEqual(list(self.files.uploads), [second])
        self.assertEqual(self.temporaries(), [second + ".part"])

    def test_free_space_is_checked_with_open_uploads_reserved(self):
        with patch("raven_web_host.os.statvfs", return_value=SimpleNamespace(f_bavail=10, f_frsize=4096)):
            self.assert_code("no_space", "upload_start", path="", name="x", size=1, overwrite=False)
        self.assertFalse((self.downloads / STAGING).exists())
        free = SimpleNamespace(f_bavail=FREE_SPACE_MARGIN + 150, f_frsize=1)
        with patch("raven_web_host.os.statvfs", return_value=free):
            self.start("first", 100)
            self.assert_code("no_space", "upload_start", path="", name="second", size=100, overwrite=False)
            self.start("second", 50)

    def test_start_refuses_existing_names_unless_overwriting(self):
        (self.downloads / "taken").write_text("old")
        (self.downloads / "folder").mkdir()
        self.assert_code("file_exists", "upload_start", path="", name="taken", size=1, overwrite=False)
        self.assert_code("not_a_file", "upload_start", path="", name="folder", size=1, overwrite=True)
        token = self.start("taken", 3, overwrite=True)
        self.chunk(token, 0, b"new")
        self.finish(token, overwrite=True)
        self.assertEqual((self.downloads / "taken").read_bytes(), b"new")

    def test_finish_keeps_the_upload_when_the_name_was_taken_meanwhile(self):
        token = self.start("race.txt", 4)
        self.chunk(token, 0, b"mine")
        (self.downloads / "race.txt").write_text("theirs")
        self.assert_code("file_exists", "upload_finish", token=token, overwrite=False)
        self.assertEqual((self.downloads / "race.txt").read_text(), "theirs")
        self.assertEqual(self.temporaries(), [token + ".part"])
        result = self.finish(token, overwrite=True)
        self.assertEqual(result["size"], 4)
        self.assertEqual((self.downloads / "race.txt").read_bytes(), b"mine")
        self.assertEqual(stat.S_IMODE((self.downloads / "race.txt").stat().st_mode), 0o660)
        self.assertEqual(self.temporaries(), [])
        self.assert_code("upload_unknown", "upload_finish", token=token, overwrite=True)

    def test_finish_never_replaces_a_folder(self):
        token = self.start("later", 1)
        self.chunk(token, 0, b"x")
        (self.downloads / "later").mkdir()
        self.assert_code("not_a_file", "upload_finish", token=token, overwrite=True)
        self.assertTrue((self.downloads / "later").is_dir())

    def test_cancel_removes_the_temporary(self):
        token = self.start("cancelled", 4)
        self.chunk(token, 0, b"ab")
        self.assertIsNone(self.call("upload_cancel", token=token))
        self.assertEqual(self.temporaries(), [])
        self.assertEqual(self.files.uploads, {})
        self.assertIsNone(self.call("upload_cancel", token=token))
        self.assertIsNone(self.call("upload_cancel", token="B" * 32))
        with self.assertRaises(FileError) as caught:
            self.chunk(token, 2, b"cd")
        self.assertEqual(caught.exception.code, "upload_unknown")
        self.assertFalse((self.downloads / "cancelled").exists())

    def test_staging_owned_by_someone_else_is_refused(self):
        self.files.uid = os.getuid() + 1
        self.assert_code("staging_invalid", "upload_start", path="", name="x", size=1, overwrite=False)
        self.assertEqual(self.temporaries(), [])

    def test_staging_with_a_wrong_mode_is_refused(self):
        (self.downloads / STAGING).mkdir()
        (self.downloads / STAGING).chmod(0o750)
        self.assert_code("staging_invalid", "upload_start", path="", name="x", size=1, overwrite=False)
        self.assertEqual(self.temporaries(), [])

    def test_staging_replaced_by_a_symlink_or_file_is_refused(self):
        (self.downloads / STAGING).symlink_to(self.outside)
        self.assert_code("staging_invalid", "upload_start", path="", name="x", size=1, overwrite=False)
        self.assertEqual(sorted(path.name for path in self.outside.iterdir()), ["secret"])
        (self.downloads / STAGING).unlink()
        (self.downloads / STAGING).write_text("planted")
        self.assert_code("staging_invalid", "upload_start", path="", name="x", size=1, overwrite=False)

    def test_setgid_downloads_still_gives_a_private_staging(self):
        self.downloads.chmod(0o2770)
        if not self.downloads.stat().st_mode & stat.S_ISGID:
            self.skipTest("this file system drops the setgid bit")
        token = self.start("x", 1)
        info = (self.downloads / STAGING).stat()
        self.assertEqual(stat.S_IMODE(info.st_mode) & 0o777, 0o700)
        self.chunk(token, 0, b"x")
        self.finish(token)

    def test_stale_temporaries_are_removed(self):
        self.call("upload_cancel", token=self.start())
        staging = self.downloads / STAGING
        old = time.time() - STALE_UPLOAD_SECONDS - 60
        for name in ("C" * 32 + ".part", "keep.txt"):
            (staging / name).write_text("left over")
            os.utime(staging / name, (old, old))
        (staging / ("D" * 32 + ".part")).write_text("recent")
        active = self.start("active", 1)
        os.utime(staging / (active + ".part"), (old, old))
        self.start("another", 1)
        self.assertNotIn("C" * 32 + ".part", self.temporaries())
        self.assertIn("D" * 32 + ".part", self.temporaries())
        self.assertIn("keep.txt", self.temporaries())
        self.assertIn(active + ".part", self.temporaries())

    def test_stale_cleanup_skips_what_it_cannot_remove(self):
        self.call("upload_cancel", token=self.start())
        staging = self.downloads / STAGING
        old = time.time() - STALE_UPLOAD_SECONDS - 60
        folder = staging / ("F" * 32 + ".part")
        folder.mkdir()
        stale = staging / ("G" * 32 + ".part")
        stale.write_text("left over")
        for path in (folder, stale):
            os.utime(path, (old, old))
        self.start("after a folder", 1)
        self.assertTrue(folder.is_dir())
        self.assertFalse(stale.exists())
        with patch("raven_web_host.os.scandir", side_effect=OSError(errno.ESTALE, "Stale file handle")):
            self.start("after a stale handle", 1)

    def test_cross_device_placement_has_its_own_code(self):
        token = self.start("x", 1)
        self.chunk(token, 0, b"x")
        exdev = OSError(errno.EXDEV, "Invalid cross-device link")
        with patch("raven_web_host.os.link", side_effect=exdev):
            self.assert_code("cross_device", "upload_finish", token=token, overwrite=False)
        with patch("raven_web_host.os.replace", side_effect=exdev):
            self.assert_code("cross_device", "upload_finish", token=token, overwrite=True)
        self.finish(token)
        self.assertEqual((self.downloads / "x").read_bytes(), b"x")

    def test_startup_cleanup_never_creates_the_staging_folder(self):
        self.files.clean_staging()
        self.assertFalse((self.downloads / STAGING).exists())
        Files(self.home / "missing").clean_staging()
        (self.downloads / STAGING).mkdir(mode=0o700)
        old = time.time() - STALE_UPLOAD_SECONDS - 60
        stale = self.downloads / STAGING / ("E" * 32 + ".part")
        stale.write_text("left over")
        os.utime(stale, (old, old))
        Files(self.downloads).clean_staging()
        self.assertFalse(stale.exists())


class BrokerProtocolTests(FileFixture):
    def setUp(self):
        super().setUp()
        self.address = str(Path(self.temporary.name) / "broker.sock")
        self.raven = Raven(SOURCE / "raven")
        self.server = Server(self.address, self.raven, self.files)
        thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(thread.join)
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)

    def exchange(self, request, payload=b""):
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as connection:
            connection.settimeout(10)
            connection.connect(self.address)
            connection.sendall(json.dumps(request).encode() + b"\n" + payload)
            with connection.makefile("rb") as response:
                header = json.loads(response.readline())
                return header, response.read()

    def test_chunks_follow_the_header_and_refusals_drain_the_payload(self):
        header, _ = self.exchange({"action": "upload_start", "path": "", "name": "wire.bin",
                                   "size": CHUNK_SIZE + 3, "overwrite": False})
        token = header["data"]["token"]
        data = os.urandom(CHUNK_SIZE)
        header, rest = self.exchange({"action": "upload_chunk", "token": token, "offset": 7, "size": CHUNK_SIZE}, data)
        self.assertEqual(header, {"ok": False, "error": "The upload is out of order; start it again",
                                  "code": "offset_mismatch"})
        self.assertEqual(rest, b"")
        header, _ = self.exchange({"action": "upload_chunk", "token": token, "offset": 0, "size": CHUNK_SIZE}, data)
        self.assertEqual(header, {"ok": True, "data": {"received": CHUNK_SIZE}})
        header, _ = self.exchange({"action": "upload_chunk", "token": token, "offset": CHUNK_SIZE, "size": 3}, b"xyz")
        self.assertEqual(header["data"], {"received": CHUNK_SIZE + 3})
        header, _ = self.exchange({"action": "upload_finish", "token": token, "overwrite": False})
        self.assertEqual(header["data"]["size"], CHUNK_SIZE + 3)
        self.assertEqual((self.downloads / "wire.bin").read_bytes(), data + b"xyz")

    def test_download_sends_the_header_then_exactly_size_bytes(self):
        data = os.urandom(3 * 65536 + 17)
        (self.downloads / "다운로드.bin").write_bytes(data)
        header, body = self.exchange({"action": "download", "path": "다운로드.bin"})
        self.assertEqual(header, {"ok": True, "data": {"name": "다운로드.bin", "size": len(data)}})
        self.assertEqual(body, data)
        (self.downloads / "empty").write_bytes(b"")
        self.assertEqual(self.exchange({"action": "download", "path": "empty"}),
                         ({"ok": True, "data": {"name": "empty", "size": 0}}, b""))

    def test_downloads_beyond_the_cap_are_refused_and_free_their_slot(self):
        (self.downloads / "file").write_bytes(b"data")
        for _ in range(MAX_DOWNLOADS):
            self.assertTrue(self.server.downloads.acquire(blocking=False))
        try:
            self.assertEqual(self.exchange({"action": "download", "path": "file"}),
                             ({"ok": False, "error": "Too many downloads in progress", "code": "busy"}, b""))
        finally:
            for _ in range(MAX_DOWNLOADS):
                self.server.downloads.release()
        for _ in range(MAX_DOWNLOADS + 1):
            self.assertEqual(self.exchange({"action": "download", "path": "file"})[1], b"data")

    def test_download_ends_early_when_the_file_shrinks(self):
        (self.downloads / "shrinking").write_bytes(b"x" * 1000)
        opened = self.files.open_download

        def larger(request):
            name, descriptor, size = opened(request)
            return name, descriptor, size + 500

        with patch.object(self.files, "open_download", side_effect=larger):
            header, body = self.exchange({"action": "download", "path": "shrinking"})
        self.assertEqual(header["data"]["size"], 1500)
        self.assertEqual(body, b"x" * 1000)

    def test_download_failure_after_the_header_only_shortens_the_body(self):
        (self.downloads / "broken").write_bytes(b"y" * 1000)

        def fail_midway(connection, source, offset, count):
            connection.sendall(source.read(10))
            raise OSError(5, "Input/output error")

        with patch.object(socket.socket, "sendfile", autospec=True, side_effect=fail_midway):
            header, body = self.exchange({"action": "download", "path": "broken"})
        self.assertEqual(header["data"]["size"], 1000)
        self.assertEqual(body, b"y" * 10)

    def test_failures_carry_codes_and_old_failures_do_not(self):
        (self.downloads / "leaf").symlink_to(self.outside / "secret")
        header, body = self.exchange({"action": "download", "path": "leaf"})
        self.assertEqual((header, body), ({"ok": False, "error": "Not a regular file", "code": "not_a_file"}, b""))
        header, _ = self.exchange({"action": "files_list", "path": "../"})
        self.assertEqual(header["code"], "invalid_path")
        header, _ = self.exchange({"action": "no_such_action"})
        self.assertEqual(header, {"ok": False, "error": "Unknown operation"})

    def test_a_planted_staging_folder_names_the_fix(self):
        (self.downloads / STAGING).symlink_to(self.outside)
        header, _ = self.exchange({"action": "upload_start", "path": "", "name": "x", "size": 1, "overwrite": False})
        self.assertEqual(header["code"], "staging_invalid")
        self.assertIn("~/Downloads/.raven-uploads", header["error"])
        self.assertIn("remove it", header["error"])

    def test_file_actions_do_not_wait_for_the_session_lock(self):
        (self.downloads / "a").write_text("a")
        with self.raven.lock:
            header, _ = self.exchange({"action": "files_list", "path": ""})
            self.assertEqual([entry["name"] for entry in header["data"]["entries"]], ["a"])
            header, body = self.exchange({"action": "download", "path": "a"})
            self.assertEqual(body, b"a")
            header, _ = self.exchange({"action": "upload_start", "path": "", "name": "b", "size": 0,
                                       "overwrite": False})
            header, _ = self.exchange({"action": "upload_finish", "token": header["data"]["token"],
                                       "overwrite": False})
            self.assertTrue(header["ok"])


if __name__ == "__main__":
    unittest.main()
