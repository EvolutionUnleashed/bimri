"""Windows replacement-contention regressions and portable error-policy checks.

The native cases hold a real destination metadata handle. Release is triggered
by an observed failed native replace, never by a timing guess. The policy cases
substitute only the OS error/platform boundary; file publication stays real.
"""

import contextlib
import ctypes
import errno
import importlib.util
import io
import os
import subprocess
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest import mock


ENGINE = Path(__file__).resolve().parents[1] / "bimri-engine.py"


def load_engine(path=None):
    spec = importlib.util.spec_from_file_location(
        "bimri_windows_replace_test", path or ENGINE
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class OsView:
    """Override the engine's OS boundary without changing pathlib's platform."""

    def __init__(self, platform_name, replace):
        self.name = platform_name
        self.replace = replace

    def __getattr__(self, name):
        return getattr(os, name)


class WindowsMetadataHandle:
    """The CreateFileW parameters used by CPython 3.8.10's Windows lstat."""

    def __init__(self, path):
        from ctypes import wintypes

        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        create = kernel.CreateFileW
        create.argtypes = [
            wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD,
            wintypes.LPVOID, wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE,
        ]
        create.restype = wintypes.HANDLE
        self._close = kernel.CloseHandle
        self._close.argtypes = [wintypes.HANDLE]
        self._close.restype = wintypes.BOOL
        self.handle = create(
            str(path),
            0x80,        # FILE_READ_ATTRIBUTES
            0,           # no file sharing
            None,
            3,           # OPEN_EXISTING
            0x02200000,  # BACKUP_SEMANTICS | OPEN_REPARSE_POINT
            None,
        )
        if self.handle == wintypes.HANDLE(-1).value:
            raise ctypes.WinError(ctypes.get_last_error())

    def close(self):
        if self.handle is not None:
            if not self._close(self.handle):
                raise ctypes.WinError(ctypes.get_last_error())
            self.handle = None


def windows_error(code):
    error = PermissionError(errno.EACCES, "test Windows replacement refusal")
    error.winerror = code
    return error


def run_start_with_blocked_state(engine_path, root):
    """Subprocess worker: introduce contention only at state replacement."""
    engine = load_engine(engine_path)
    state = Path(root).resolve() / ".bimri" / "state.json"
    original_replace = os.replace
    held = None
    failures = []

    def replace(source, destination):
        nonlocal held
        if Path(destination) == state:
            if held is None:
                held = WindowsMetadataHandle(state)
            try:
                return original_replace(source, destination)
            except OSError as exc:
                failures.append(getattr(exc, "winerror", None))
                raise
        return original_replace(source, destination)

    engine.os = OsView(os.name, replace)
    try:
        result = engine.main([
            "--root", str(root), "start", "--actor", "blocked-state-writer",
        ])
    finally:
        if held is not None:
            held.close()
        print("NATIVE_STATE_REPLACE_FAILURES=" + str(len(failures)), file=sys.stderr)
    return result


class WindowsAtomicReplaceTest(unittest.TestCase):
    def setUp(self):
        self._temp = tempfile.TemporaryDirectory(prefix="bimri-replace-test-")
        self.addCleanup(self._temp.cleanup)
        self.root = Path(self._temp.name)
        self.target = self.root / "state.json"
        self.original = b'{"before": "unchanged"}\n'
        self.target.write_bytes(self.original)
        self.engine = load_engine()

    def write(self, kind):
        if kind == "text":
            content = '{"after": "caf\u00e9"}\n'
            self.engine.atomic_write_text(self.target, content)
            return content.encode("utf-8")
        content = b"\x00exact backup\r\n\xff\x80\n"
        self.engine.atomic_write_bytes(self.target, content)
        return content

    def assert_no_temporary_files(self):
        self.assertEqual(list(self.root.glob(".bimri-tmp-*")), [])

    def native_release_after_failure(self, kind):
        held = WindowsMetadataHandle(self.target)
        failures = []
        replacements = []
        original_replace = os.replace

        def replace(source, destination):
            replacements.append((str(source), str(destination)))
            try:
                return original_replace(source, destination)
            except OSError as exc:
                failures.append(getattr(exc, "winerror", None))
                # Release only once Windows has actually rejected replacement.
                held.close()
                raise

        try:
            with mock.patch.object(self.engine, "os", OsView("nt", replace)):
                expected = self.write(kind)
        finally:
            held.close()
        self.assertEqual(failures, [5])
        self.assertGreaterEqual(len(replacements), 2)
        self.assertEqual(len(set(replacements)), 1, "retry changed source or target")
        self.assertEqual(self.target.read_bytes(), expected)
        self.assert_no_temporary_files()

    @unittest.skipUnless(os.name == "nt", "requires real Windows file handles")
    def test_text_write_succeeds_after_real_destination_handle_release(self):
        self.native_release_after_failure("text")

    @unittest.skipUnless(os.name == "nt", "requires real Windows file handles")
    def test_bytes_write_succeeds_after_real_destination_handle_release(self):
        self.native_release_after_failure("bytes")

    def native_persistent_handle(self, kind):
        held = WindowsMetadataHandle(self.target)
        failures = []
        original_replace = os.replace
        original_sleep = self.engine.time.sleep

        def replace(source, destination):
            try:
                return original_replace(source, destination)
            except OSError as exc:
                failures.append(exc)
                raise

        try:
            with mock.patch.object(self.engine, "os", OsView("nt", replace)):
                with mock.patch.object(
                    self.engine.time, "sleep", wraps=original_sleep
                ) as sleep:
                    with self.assertRaises(PermissionError) as caught:
                        self.write(kind)
        finally:
            held.close()
        self.assertEqual(len(failures), 7)
        self.assertIs(caught.exception, failures[-1])
        self.assertTrue(all(error.winerror == 5 for error in failures))
        self.assertEqual(sleep.call_count, 6)
        self.assertAlmostEqual(sum(call.args[0] for call in sleep.call_args_list), 0.63)
        self.assertEqual(self.target.read_bytes(), self.original)
        self.assert_no_temporary_files()

    @unittest.skipUnless(os.name == "nt", "requires real Windows file handles")
    def test_text_write_exhaustion_preserves_original_and_cleans_temp(self):
        self.native_persistent_handle("text")

    @unittest.skipUnless(os.name == "nt", "requires real Windows file handles")
    def test_bytes_write_exhaustion_preserves_original_and_cleans_temp(self):
        self.native_persistent_handle("bytes")

    def test_both_windows_error_classes_retry_the_same_completed_file(self):
        for code in (5, 32):
            for kind in ("text", "bytes"):
                with self.subTest(winerror=code, writer=kind):
                    attempts = []
                    original_replace = os.replace

                    def replace(source, destination):
                        attempts.append((str(source), str(destination)))
                        if len(attempts) == 1:
                            raise windows_error(code)
                        return original_replace(source, destination)

                    with mock.patch.object(self.engine, "os", OsView("nt", replace)):
                        with mock.patch.object(self.engine.time, "sleep") as sleep:
                            expected = self.write(kind)
                    self.assertEqual(len(attempts), 2)
                    self.assertEqual(attempts[0], attempts[1])
                    self.assertEqual(sleep.call_count, 1)
                    self.assertGreater(sleep.call_args.args[0], 0)
                    self.assertEqual(self.target.read_bytes(), expected)
                    self.assert_no_temporary_files()

    def test_first_attempt_success_does_not_sleep(self):
        for kind in ("text", "bytes"):
            with self.subTest(writer=kind):
                replace = mock.Mock(wraps=os.replace)
                with mock.patch.object(self.engine, "os", OsView("nt", replace)):
                    with mock.patch.object(self.engine.time, "sleep") as sleep:
                        expected = self.write(kind)
                self.assertEqual(replace.call_count, 1)
                sleep.assert_not_called()
                self.assertEqual(self.target.read_bytes(), expected)

    def test_unrelated_windows_and_posix_errors_propagate_without_sleep(self):
        disk_full = OSError(errno.ENOSPC, "disk full")
        disk_full.winerror = 112
        missing = FileNotFoundError(errno.ENOENT, "missing temporary file")
        missing.winerror = 2
        io_error = OSError(errno.EIO, "I/O device error")
        io_error.winerror = 1117
        cases = [
            ("nt", disk_full),
            ("nt", missing),
            ("nt", io_error),
            ("nt", PermissionError(errno.EACCES, "no Windows error attribute")),
            ("posix", PermissionError(errno.EACCES, "POSIX permission failure")),
            ("posix", windows_error(5)),
        ]
        for platform_name, error in cases:
            for kind in ("text", "bytes"):
                with self.subTest(platform=platform_name, error=repr(error), writer=kind):
                    replace = mock.Mock(side_effect=error)
                    with mock.patch.object(
                        self.engine, "os", OsView(platform_name, replace)
                    ):
                        with mock.patch.object(self.engine.time, "sleep") as sleep:
                            with self.assertRaises(OSError) as caught:
                                self.write(kind)
                    self.assertIs(caught.exception, error)
                    self.assertEqual(replace.call_count, 1)
                    sleep.assert_not_called()
                    self.assertEqual(self.target.read_bytes(), self.original)
                    self.assert_no_temporary_files()

    def test_generated_view_keeps_bounded_retries_and_existing_warning_contract(self):
        paths = types.SimpleNamespace(hot=self.target)
        for warn_only in (False, True):
            with self.subTest(warn_only=warn_only):
                replace = mock.Mock(side_effect=windows_error(5))
                stderr = io.StringIO()
                with mock.patch.object(self.engine, "os", OsView("nt", replace)):
                    with mock.patch.object(self.engine.time, "sleep") as sleep:
                        with contextlib.redirect_stderr(stderr):
                            if warn_only:
                                result = self.engine.write_generated_view(
                                    paths, "new generated view\n", warn_only=True
                                )
                                self.assertFalse(result)
                            else:
                                with self.assertRaises(self.engine.BimriError):
                                    self.engine.write_generated_view(
                                        paths, "new generated view\n"
                                    )
                self.assertEqual(replace.call_count, 35)
                self.assertAlmostEqual(
                    sum(call.args[0] for call in sleep.call_args_list), 3.90
                )
                if warn_only:
                    self.assertIn("BIMRI WARNING:", stderr.getvalue())
                    self.assertIn("could not be refreshed", stderr.getvalue())
                self.assertEqual(self.target.read_bytes(), self.original)
                self.assert_no_temporary_files()

    @unittest.skipUnless(os.name == "nt", "requires real Windows file handles")
    def test_state_replacement_exhaustion_reaches_cli_failure(self):
        store = self.root / "store"
        seed = subprocess.run(
            [sys.executable, str(ENGINE), "--root", str(store), "start", "--actor", "seed"],
            capture_output=True, text=True, encoding="utf-8", timeout=60,
        )
        self.assertEqual(seed.returncode, 0, seed.stdout + seed.stderr)
        state = store / ".bimri" / "state.json"
        before = state.read_bytes()
        worker = (
            "import runpy, sys; "
            "helpers = runpy.run_path(sys.argv[1], run_name='replace_test_worker'); "
            "sys.exit(helpers['run_start_with_blocked_state'](sys.argv[2], sys.argv[3]))"
        )
        result = subprocess.run(
            [sys.executable, "-c", worker, str(Path(__file__).resolve()), str(ENGINE), str(store)],
            capture_output=True, text=True, encoding="utf-8", timeout=60,
        )
        self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
        self.assertIn("BIMRI ERROR: filesystem operation failed:", result.stderr)
        self.assertIn("state.json", result.stderr)
        self.assertIn("NATIVE_STATE_REPLACE_FAILURES=7", result.stderr)
        self.assertNotIn("BIMRI RUN HANDLE:", result.stdout)
        self.assertEqual(state.read_bytes(), before)
        self.assertEqual(list(state.parent.glob(".bimri-tmp-*")), [])


if __name__ == "__main__":
    unittest.main()
