"""BIMRI v5.2 build step 8: copy-under-lock for doctor, review, --history,
--audit.

Spine: design/DECISIONS.md D5.3 (D5.3.1 capture, D5.3.2 only a live full audit
publishes, D5.3.3 review and read-only doctor never reconcile), D1.7, D1.8,
D2.8 (verdict); detail from design/DESIGN.md section 05 (2.1-2.5 capture,
3.1-3.8 doctor, 4 review, 5 history/audit reads, 6 hold seam, 7 tests, local
decisions L3-L20), section 09 step 8 (drift on the copy path) and section 10
gate 7.

Process model (section 10.0): every behavioural assertion drives the engine
as a subprocess. Instrumentation stays out of the test process: the existing
``tests/crash_worker.py`` modes (``witness_crash_before_replace``,
``journal_hold_lock``) and a small probe written into the class workspace
(``PROBE_SOURCE``) that imports the engine by path, patches module attributes
only (the crash_worker rule) and then runs ``engine.main``. Probe modes:

- ``hold``: section 10.1.5 ``audit_copy_hold``. Wraps
  ``authority_storage_issues``; the first call whose ``paths.root`` is a
  ``.bimri-audit-copy-*`` directory writes ``<root>/.test-copy-captured``
  (copy path and captured head) and spins until ``<root>/.test-release-audit``
  exists (exit 9 after 120 s). The capture has finished and the live lock is
  free by then (section 05 2.1, 6).
- ``between``: section 05 6 ``audit_copy_between_phases``. Wraps
  ``complete_audit_copy``: writes ``<root>/.test-phase-a-done`` and spins
  until ``<root>/.test-phase-b-go`` before Phase B.
- ``budget1``: calls ``capture_audit_copy(paths, 1)`` whatever budget the
  command passes, so the reads without a ``--budget-ms`` flag (L4) can be
  driven into their deferral.
- ``phase_a``: in-process Phase A on ROOT under ``existing_store_lock`` with
  an open/link counter; writes a JSON report.
- ``crash_log_rewrite``: the process exits 110 at the first atomic rewrite of
  a run log, so a lifecycle ``close`` dies after its state write and leaves
  its transition marker.

The writes landed during a held audit are ``start``, ``propose`` + ``sync``
and ``journal`` (the legacy lane, present in every build); this checkout is
step 1 plus step 8, so the save lane of step 2 is not assumed.
"""

import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path


REPOSITORY = Path(__file__).resolve().parents[1]
ENGINE = REPOSITORY / "bimri-engine.py"
CRASH_WORKER = REPOSITORY / "tests" / "crash_worker.py"
TESTS = REPOSITORY / "tests"
if str(TESTS) not in sys.path:
    sys.path.insert(0, str(TESTS))
from test_bimri_engine import protected_tree_snapshot  # noqa: E402


RUN_RE = re.compile(r"(?:BIMRI RUN HANDLE: |=== BIMRI BRIEF )(R\d{6})")
PROPOSAL_RE = re.compile(r"\bR\d{6}-Q\d{3}\b")
COPY_PREFIX = ".bimri-audit-copy-"
WITNESS = Path(".bimri") / "audit-witness.json"
MANIFEST = Path(".bimri") / "audit-manifest.json"
GENERATIONS = Path(".bimri") / "audit-manifests"
VERDICT = Path(".bimri") / "audit-verdict.json"
DRIFT = Path(".bimri") / "audit-drift"
TRANSITION = Path(".bimri") / "audit-transition.json"
LINKED_ROOTS = (
    "proposals", "decisions", "conflicts", "resolutions", "revisions",
    "recovery", "settlements", "migrations",
)
PASSED_COPY_RE = re.compile(
    r"^BIMRI doctor: PASSED as of V(\d{6}) epoch (\d+) "
    r"\(copy captured in (\d+) ms, ([\d,]+) files\)$",
    re.MULTILINE,
)
PASSED_LIVE_RE = re.compile(
    r"^BIMRI doctor: PASSED \(live audit held the lock [\d.]+ s\)$",
    re.MULTILINE,
)
NO_CHECKPOINT_LINE = "BIMRI doctor: no valid checkpoint; auditing live under lock"
NEWEST_VERDICT_TAIL = (
    r"(?:last deep audit \S+ (?:PASSED|FAILED)(?: as of V\d{6})?"
    r"|no deep audit on record)\)"
)
SYNTHETIC_HISTORY_FILES = 50000
TAMPERED = "R000001-Q005"  # fixture.k03: no later write depends on it


PROBE_SOURCE = r'''
import builtins
import importlib.util
import io
import json
import os
import shutil
import sys
import time
from pathlib import Path

COPY_PREFIX = ".bimri-audit-copy-"
LINKED = ("proposals", "decisions", "conflicts", "resolutions", "revisions",
          "recovery", "settlements", "migrations", "log", "archive")


def spin(path, limit=120.0):
    deadline = time.monotonic() + limit
    while not path.exists():
        if time.monotonic() >= deadline:
            os._exit(9)
        time.sleep(0.05)


def copy_root_of(value):
    candidate = Path(str(value))
    for part in (candidate,) + tuple(candidate.parents):
        if part.name.startswith(COPY_PREFIX):
            return part
    return None


def main():
    engine_path, mode, root = sys.argv[1], sys.argv[2], Path(sys.argv[3]).resolve()
    command = sys.argv[4:]
    spec = importlib.util.spec_from_file_location("bimri_step8_probe", engine_path)
    engine = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(engine)

    if mode == "hold":
        original = engine.authority_storage_issues
        held = {"done": False}

        def held_storage_issues(paths, *args, **kwargs):
            copy_root = copy_root_of(paths.root)
            if copy_root is not None and not held["done"]:
                held["done"] = True
                state = json.loads(
                    (copy_root / ".bimri" / "state.json").read_text("utf-8")
                )
                (root / ".test-copy-captured").write_text(json.dumps({
                    "copy": str(copy_root),
                    "head_revision": state["head_revision"],
                    "audit_epoch": state.get("_audit_epoch"),
                }), encoding="utf-8")
                spin(root / ".test-release-audit")
            return original(paths, *args, **kwargs)

        engine.authority_storage_issues = held_storage_issues
    elif mode == "between":
        original = engine.complete_audit_copy

        def held_complete(*args, **kwargs):
            (root / ".test-phase-a-done").write_text("phase A done\n", encoding="utf-8")
            spin(root / ".test-phase-b-go")
            return original(*args, **kwargs)

        engine.complete_audit_copy = held_complete
    elif mode == "budget1":
        original = engine.capture_audit_copy

        def tiny_budget(paths, budget_ms=None, *args, **kwargs):
            return original(paths, 1, *args, **kwargs)

        engine.capture_audit_copy = tiny_budget
    elif mode == "crash_log_rewrite":
        original = engine.atomic_write_text
        logs = (root / ".bimri" / "log").resolve()

        def crash_on_log_rewrite(path, content):
            if Path(path).resolve().parent == logs:
                os._exit(110)
            return original(path, content)

        engine.atomic_write_text = crash_on_log_rewrite
    elif mode == "phase_a":
        out = Path(command[0])
        paths = engine.Paths(root)
        live_roots = [str((root / ".bimri" / name).resolve()) for name in LINKED]
        touched = []

        def under_live_history(value):
            try:
                text = str(Path(os.fsdecode(value)).resolve())
            except (TypeError, ValueError, OSError):
                return False
            return any(
                text == item or text.startswith(item + os.sep) for item in live_roots
            )

        real = {
            "open": builtins.open, "io_open": io.open, "os_open": os.open,
            "link": os.link, "copyfile": shutil.copyfile, "copy2": shutil.copy2,
        }

        def wrap(name, function):
            def wrapped(*args, **kwargs):
                for value in args[:2]:
                    if isinstance(value, (str, bytes, os.PathLike)) and under_live_history(value):
                        touched.append([name, os.fsdecode(value)])
                return function(*args, **kwargs)
            return wrapped

        with engine.existing_store_lock(paths):
            builtins.open = wrap("open", real["open"])
            io.open = wrap("io.open", real["io_open"])
            os.open = wrap("os.open", real["os_open"])
            os.link = wrap("os.link", real["link"])
            shutil.copyfile = wrap("copyfile", real["copyfile"])
            shutil.copy2 = wrap("copy2", real["copy2"])
            started = time.perf_counter()
            try:
                copy_paths, pending = engine.capture_audit_copy(
                    paths, engine.AUDIT_COPY_BUDGET_MS
                )
            finally:
                elapsed_ms = (time.perf_counter() - started) * 1000.0
                builtins.open = real["open"]
                io.open = real["io_open"]
                os.open = real["os_open"]
                os.link = real["link"]
                shutil.copyfile = real["copyfile"]
                shutil.copy2 = real["copy2"]
        receipt_ms = None
        if len(command) > 1 and command[1] == "complete":
            receipt = engine.complete_audit_copy(paths, copy_paths, pending)
            receipt_ms = (
                receipt["lock_held_ms"] if isinstance(receipt, dict)
                else getattr(receipt, "lock_held_ms")
            )
        shutil.rmtree(str(copy_paths.root), ignore_errors=True)
        out.write_text(json.dumps({
            "phase_a_ms": elapsed_ms,
            "budget_ms": engine.AUDIT_COPY_BUDGET_MS,
            "touched": touched,
            "receipt_lock_held_ms": receipt_ms,
            "copy_parent": str(Path(copy_paths.root).parent),
        }), encoding="utf-8")
        return 0
    else:
        raise SystemExit("unknown probe mode: " + mode)
    return engine.main(["--root", str(root)] + command)


if __name__ == "__main__":
    raise SystemExit(main())
'''


def audit_digest(value):
    encoded = json.dumps(value, separators=(",", ":"), sort_keys=True).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def record_seal(record, seal_field):
    return audit_digest({k: v for k, v in record.items() if k != seal_field})


class CopyUnderLockTest(unittest.TestCase):
    maxDiff = None

    # ---- fixture ---------------------------------------------------------

    @classmethod
    def setUpClass(cls):
        cls._class_temp = tempfile.TemporaryDirectory(prefix="bimri-v520-s8-")
        base = Path(cls._class_temp.name)
        cls.probe_path = base / "step8_probe.py"
        cls.probe_path.write_text(PROBE_SOURCE, encoding="utf-8")
        helped = subprocess.run(
            [sys.executable, str(ENGINE), "start", "--help"],
            capture_output=True, text=True, encoding="utf-8", timeout=60,
        )
        cls.open_run = ["--open-run"] if "--open-run" in helped.stdout else []
        cls.template = base / "template"
        cls._build_template(cls.template)

    @classmethod
    def tearDownClass(cls):
        cls._class_temp.cleanup()

    @classmethod
    def _run(cls, root, *arguments, timeout=120):
        result = subprocess.run(
            [sys.executable, str(ENGINE), "--root", str(root)]
            + [str(a) for a in arguments],
            capture_output=True, text=True, encoding="utf-8", timeout=timeout,
        )
        if result.returncode != 0:
            raise AssertionError(
                "fixture command failed: %r\n%s\n%s"
                % (arguments, result.stdout, result.stderr)
            )
        return result

    @classmethod
    def _build_template(cls, root):
        # Section 10.2 F1/F2 shape, scaled down: a confirmed T1 subject, 20 T2
        # subjects, all accepted, a published witness and a live verdict.
        started = cls._run(root, "start", *cls.open_run, "--actor", "fixture")
        run_id = RUN_RE.search(started.stdout).group(1)
        cls._run(
            root, "propose", "--run", run_id, "--operation", "set",
            "--tier", "1", "--key", "fixture.rule", "--source", "user",
            "--trust", "confirmed", "--text", "Owner rule for the copy tests.",
            "--new-subject",
        )
        for index in range(20):
            cls._run(
                root, "propose", "--run", run_id, "--operation", "set",
                "--tier", "2", "--key", "fixture.k%02d" % index,
                "--source", "agent", "--trust", "working",
                "--text", "Fixture value number %d." % index, "--new-subject",
            )
        cls._run(root, "sync", "--run", run_id)
        cls._run(root, "get", "--key", "fixture.k00")

    def setUp(self):
        self._temp = tempfile.TemporaryDirectory(prefix="bimri-v520-s8-t-")
        self.workspace = Path(self._temp.name)
        self.root = self.workspace / "store"
        shutil.copytree(str(self.template), str(self.root))
        self._processes = []
        self.assertTrue((self.root / WITNESS).is_file())

    def tearDown(self):
        for process in self._processes:
            if process.poll() is None:
                for name in (".test-release-audit", ".test-phase-b-go",
                             ".test-release-writer-lock"):
                    try:
                        (self.root / name).write_text("release\n", encoding="utf-8")
                    except OSError:
                        pass
                try:
                    process.communicate(timeout=30)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.communicate()
        self._temp.cleanup()

    # ---- helpers -----------------------------------------------------------

    def cli(self, *arguments, root=None, check=True, timeout=120):
        command = [sys.executable, str(ENGINE), "--root", str(root or self.root)]
        command.extend(str(a) for a in arguments)
        result = subprocess.run(
            command, capture_output=True, text=True, encoding="utf-8",
            timeout=timeout,
        )
        if check and result.returncode != 0:
            self.fail(
                "command failed: %r\nstdout:\n%s\nstderr:\n%s"
                % (command, result.stdout, result.stderr)
            )
        return result

    def timed(self, *arguments):
        started = time.perf_counter()
        result = self.cli(*arguments, check=False, timeout=60)
        return result, (time.perf_counter() - started) * 1000.0

    def probe(self, mode, *arguments, check=False, timeout=180):
        result = subprocess.run(
            [sys.executable, str(self.probe_path), str(ENGINE), mode,
             str(self.root)] + [str(a) for a in arguments],
            capture_output=True, text=True, encoding="utf-8", timeout=timeout,
        )
        if check and result.returncode != 0:
            self.fail("probe failed: %s\n%s" % (result.stdout, result.stderr))
        return result

    def popen_probe(self, mode, *arguments):
        process = subprocess.Popen(
            [sys.executable, str(self.probe_path), str(ENGINE), mode,
             str(self.root)] + [str(a) for a in arguments],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            text=True, encoding="utf-8",
        )
        self._processes.append(process)
        return process

    def wait_for(self, name, process, timeout=30.0):
        path = self.root / name
        deadline = time.monotonic() + timeout
        while not path.exists():
            if process.poll() is not None:
                out, err = process.communicate()
                self.fail(
                    "process ended before %s (rc %s)\n%s\n%s"
                    % (name, process.returncode, out, err)
                )
            if time.monotonic() >= deadline:
                self.fail("timed out waiting for " + name)
            time.sleep(0.05)
        return path

    def release(self, name, process, timeout=120):
        (self.root / name).write_text("release\n", encoding="utf-8")
        out, err = process.communicate(timeout=timeout)
        return process.returncode, out, err

    def hold_doctor(self, *doctor_arguments):
        process = self.popen_probe("hold", "doctor", *doctor_arguments)
        signal = self.wait_for(".test-copy-captured", process)
        captured = None
        deadline = time.monotonic() + 10
        while captured is None:
            try:
                captured = json.loads(signal.read_text("utf-8"))
            except ValueError:
                if time.monotonic() >= deadline:
                    raise
                time.sleep(0.05)
        return process, captured

    def start(self, actor):
        result = self.cli("start", *self.open_run, "--actor", actor)
        match = RUN_RE.search(result.stdout)
        self.assertIsNotNone(match, result.stdout)
        return match.group(1)

    def propose(self, run_id, key, text, check=True):
        result = self.cli(
            "propose", "--run", run_id, "--operation", "set", "--tier", "2",
            "--key", key, "--source", "agent", "--trust", "working",
            "--text", text, "--new-subject", check=check,
        )
        return result

    def state(self):
        return json.loads((self.root / ".bimri" / "state.json").read_text("utf-8"))

    def read_bytes(self, relative):
        path = self.root / relative
        return path.read_bytes() if path.is_file() else None

    def witness(self):
        return json.loads((self.root / WITNESS).read_text("utf-8"))

    def verdict(self):
        return json.loads((self.root / VERDICT).read_text("utf-8"))

    def generations(self):
        directory = self.root / GENERATIONS
        if not directory.is_dir():
            return {}
        return {p.name: p.read_bytes() for p in sorted(directory.iterdir()) if p.is_file()}

    def evidence(self):
        return {
            "witness": self.read_bytes(WITNESS),
            "manifest": self.read_bytes(MANIFEST),
            "generations": self.generations(),
        }

    def copy_dirs(self):
        return sorted(
            p.name for p in self.root.iterdir() if p.name.startswith(COPY_PREFIX)
        )

    def drift_receipts(self):
        directory = self.root / DRIFT
        if not directory.is_dir():
            return []
        return sorted(p for p in directory.glob("D*.json") if p.is_file())

    def drift_reasons(self, receipts):
        reasons = []
        for path in receipts:
            reasons.extend(
                str(r) for r in json.loads(path.read_text("utf-8"))["reasons"]
            )
        return " ".join(reasons)

    def replace_bytes(self, path, data):
        # Every writer under a linked root replaces the directory entry
        # (section 05 2.2); an in-place write would reach the copy's hard link.
        temporary = path.with_name(path.name + ".test-replace")
        temporary.write_bytes(data)
        os.replace(str(temporary), str(path))

    def tamper_decision_outcome(self, proposal_id=TAMPERED):
        path = self.root / ".bimri" / "decisions" / (proposal_id + ".json")
        original = path.read_bytes()
        self.assertIn(b'"accepted"', original)
        path.write_bytes(original.replace(b'"accepted"', b'"rejected"', 1))
        return path, original

    def plant_unconfirmable_reference(self):
        # authority_reference_issues reports a run log naming another run's
        # proposal; it has no per-record form, so it is never confirmable.
        log = self.root / ".bimri" / "log" / "R000001.md"
        with open(str(log), "a", encoding="utf-8") as handle:
            handle.write("[PROPOSE:R000099-Q001] planted reference\n")
        return log

    def decision_finding(self, proposal_id=TAMPERED):
        return "decision %s (.bimri/decisions/%s.json)" % (proposal_id, proposal_id)

    # ---- gate 7 -------------------------------------------------------------

    def test_starts_and_saves_proceed_during_a_slow_copied_audit(self):
        # Gate 7, section 05 6, section 10 gate 7: with the copied audit held
        # after capture, 8 starts, 4 saves, 4 gets and 1 status each finish
        # rc 0 within 3 s; after release doctor is PASSED as of the captured
        # head and the later saves are not in the verdict's head.
        process, captured = self.hold_doctor()
        head = captured["head_revision"]
        slow = []
        for index in range(8):
            result, ms = self.timed("start", *self.open_run, "--actor", "gate7-%d" % index)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            slow.append(("start", ms))
        writer = RUN_RE.search(result.stdout).group(1)
        for index in range(4):
            result, ms = self.timed(
                "propose", "--run", writer, "--operation", "set", "--tier", "2",
                "--key", "gate7.k%d" % index, "--source", "agent",
                "--trust", "working", "--text", "Held audit write %d." % index,
                "--new-subject",
            )
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            slow.append(("propose", ms))
            result, ms = self.timed("sync", "--run", writer)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            slow.append(("sync", ms))
        for index in range(4):
            result, ms = self.timed("get", "--key", "gate7.k%d" % index)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            slow.append(("get", ms))
        result, ms = self.timed("status")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        slow.append(("status", ms))
        self.assertEqual([item for item in slow if item[1] > 3000.0], [])
        self.assertEqual(self.state()["head_revision"], head + 4)

        rc, out, err = self.release(".test-release-audit", process)
        self.assertEqual(rc, 0, out + err)
        first = out.splitlines()[0]
        match = PASSED_COPY_RE.match(first)
        self.assertIsNotNone(match, out)
        self.assertEqual(int(match.group(1)), head)
        verdict = self.verdict()
        self.assertEqual((verdict["source"], verdict["status"]), ("copy", "passed"))
        self.assertEqual(verdict["head_revision"], head)
        self.assertLess(verdict["head_revision"], self.state()["head_revision"])
        self.assertEqual(self.copy_dirs(), [])

    def test_index_is_not_rewritten_when_the_store_moved_during_the_audit(self):
        # Section 05 3.4 step 3: the live index is written only when state and
        # hot bytes still equal the receipt; otherwise the named warning.
        process, _captured = self.hold_doctor()
        writer = self.start("index-moved")
        self.propose(writer, "moved.key", "Lands during the held audit.")
        self.cli("sync", "--run", writer)
        index_before = self.read_bytes(Path(".bimri") / "index.tsv")
        rc, out, err = self.release(".test-release-audit", process)
        self.assertEqual(rc, 0, out + err)
        self.assertIn("index not rewritten: the store changed during the audit", out)
        self.assertEqual(self.read_bytes(Path(".bimri") / "index.tsv"), index_before)

    # ---- positive copy result ------------------------------------------------

    def test_copy_positive_never_touches_live_witness(self):
        # D5.3.2, section 05 3.4: a passing copy audit writes a copy verdict and
        # leaves the live witness, manifest alias and generations byte-equal.
        before = self.evidence()
        doctor = self.cli("doctor")
        self.assertIsNotNone(PASSED_COPY_RE.search(doctor.stdout), doctor.stdout)
        self.assertEqual(self.evidence(), before)
        verdict = self.verdict()
        self.assertEqual((verdict["source"], verdict["status"]), ("copy", "passed"))
        self.assertIsInstance(verdict["copy_capture_ms"], int)
        self.assertEqual(verdict["head_revision"], self.state()["head_revision"])
        self.assertEqual(self.copy_dirs(), [])

    def test_copy_positive_rewrites_the_live_index(self):
        # Section 05 3.4 step 3: a quiet store gets its index from the copy.
        index = self.root / ".bimri" / "index.tsv"
        if index.exists():
            index.unlink()
        self.cli("doctor")
        self.assertTrue(index.is_file())
        self.assertIn("fixture.k00", index.read_text("utf-8"))

    def test_review_and_read_only_doctor_leave_witness_and_verdict_alone(self):
        # D5.3.3, section 10 gate 7: review and doctor --read-only change
        # neither the witness nor the verdict after a copy doctor wrote one.
        self.cli("doctor")
        before = (self.read_bytes(WITNESS), self.read_bytes(VERDICT), self.generations())
        self.cli("review")
        read_only = self.cli("doctor", "--read-only")
        self.assertIn("BIMRI doctor (read-only): PASSED", read_only.stdout)
        self.assertEqual(
            (self.read_bytes(WITNESS), self.read_bytes(VERDICT), self.generations()),
            before,
        )

    def test_read_only_doctor_passed_line_carries_the_capture_suffix(self):
        # Section 05 3.7, 3.8: the read-only PASSED line names the capture.
        result = self.cli("doctor", "--read-only")
        self.assertRegex(
            result.stdout,
            r"(?m)^BIMRI doctor \(read-only\): PASSED as of V\d{6} epoch \d+ "
            r"\(copy captured in \d+ ms, [\d,]+ files\)$",
        )

    # ---- negative copy result ------------------------------------------------

    def test_copy_negative_condemns_only_when_live_bytes_equal(self):
        # D5.3.2, section 05 3.5 item 2: an unchanged store makes the copy's
        # finding a live finding: FAILED naming the file, exit 1, epoch
        # advanced, witness bytes retained, verdict failed from the copy.
        self.tamper_decision_outcome()
        witness_before = self.read_bytes(WITNESS)
        witness_epoch = json.loads(witness_before.decode("utf-8"))["audit_epoch"]
        doctor = self.cli("doctor", check=False)
        self.assertEqual(doctor.returncode, 1, doctor.stdout + doctor.stderr)
        self.assertIn("BIMRI doctor: FAILED", doctor.stdout)
        self.assertIn(self.decision_finding(), doctor.stdout)
        self.assertEqual(self.read_bytes(WITNESS), witness_before)
        self.assertGreater(self.state()["_audit_epoch"], witness_epoch)
        verdict = self.verdict()
        self.assertEqual((verdict["source"], verdict["status"]), ("copy", "failed"))
        self.assertIn(self.decision_finding(), " ".join(verdict["issues"]))
        self.assertEqual(self.copy_dirs(), [])

    def test_condemned_copy_result_is_a_recorded_recovery_condition(self):
        # D5.2 with D5.3.2: after the copy condemnation start shows
        # AUTHORITY RECOVERY NEEDED and status exits 1.
        self.tamper_decision_outcome()
        self.assertEqual(self.cli("doctor", check=False).returncode, 1)
        started = self.cli("start", *self.open_run, "--actor", "after", check=False)
        self.assertIn("AUTHORITY RECOVERY NEEDED", started.stdout + started.stderr)
        self.assertEqual(self.cli("status", check=False).returncode, 1)

    def test_copy_negative_is_confirmed_per_record_when_the_store_moved(self):
        # Section 05 3.5 item 3 (L18): a write lands during the held audit, so
        # live differs from the capture; the per-record finding is re-validated
        # on live bytes, reproduced, and condemns with a failed verdict.
        busy = self.start("busy-writer")
        self.tamper_decision_outcome()
        witness_epoch = self.witness()["audit_epoch"]
        process, _captured = self.hold_doctor()
        self.cli("journal", "--run", busy, "--text", "Lands during the held audit.")
        rc, out, err = self.release(".test-release-audit", process)
        self.assertEqual(rc, 1, out + err)
        self.assertIn("BIMRI doctor: FAILED", out)
        self.assertIn(self.decision_finding(), out)
        self.assertNotIn("further findings not confirmed", out)
        self.assertGreater(self.state()["_audit_epoch"], witness_epoch)
        verdict = self.verdict()
        self.assertEqual((verdict["source"], verdict["status"]), ("copy", "failed"))
        self.assertIn(self.decision_finding(), " ".join(verdict["issues"]))

    def test_copy_negative_is_superseded_when_the_record_was_repaired(self):
        # Section 05 3.5 item 3, 3.8: the tampered decision is restored and a
        # write lands during the hold; nothing reproduces on live, so
        # SUPERSEDED, exit 3, no verdict written, epoch unchanged.
        busy = self.start("busy-repair")
        path, original = self.tamper_decision_outcome()
        verdict_before = self.read_bytes(VERDICT)
        epoch_before = self.state()["_audit_epoch"]
        process, _captured = self.hold_doctor()
        self.replace_bytes(path, original)
        self.cli("journal", "--run", busy, "--text", "Repair landed during the audit.")
        rc, out, err = self.release(".test-release-audit", process)
        self.assertEqual(rc, 3, out + err)
        self.assertIn(
            "BIMRI doctor: SUPERSEDED (the store changed during the audit; "
            "1 findings not confirmed; run doctor again, or doctor --live to "
            "audit under the lock)",
            out,
        )
        self.assertEqual(self.read_bytes(VERDICT), verdict_before)
        self.assertEqual(self.state()["_audit_epoch"], epoch_before)
        self.assertEqual(self.copy_dirs(), [])

    def test_busy_store_confirms_findings_per_record(self):
        # Section 05 3.5 item 3 (L18), BRIEF item D: two writes during the
        # hold; the decision finding is confirmed and alone in the verdict,
        # the unconfirmable reference finding is counted, never recorded.
        busy = self.start("busy-two")
        self.tamper_decision_outcome()
        self.plant_unconfirmable_reference()
        witness_epoch = self.witness()["audit_epoch"]
        process, _captured = self.hold_doctor()
        self.cli("journal", "--run", busy, "--text", "First busy write.")
        self.cli("journal", "--run", busy, "--text", "Second busy write.")
        rc, out, err = self.release(".test-release-audit", process)
        self.assertEqual(rc, 1, out + err)
        self.assertIn(self.decision_finding(), out)
        unconfirmed = re.search(
            r"(?m)^  - (\d+) further findings not confirmed: the store changed "
            r"during the audit; run doctor again$",
            out,
        )
        self.assertIsNotNone(unconfirmed, out)
        self.assertGreaterEqual(int(unconfirmed.group(1)), 1)
        verdict = self.verdict()
        self.assertEqual(verdict["status"], "failed")
        issues = " ".join(verdict["issues"])
        self.assertIn(self.decision_finding(), issues)
        self.assertNotIn("references another run's proposal", issues)
        self.assertGreater(self.state()["_audit_epoch"], witness_epoch)
        started = self.cli("start", *self.open_run, "--actor", "after-busy", check=False)
        self.assertIn("AUTHORITY RECOVERY NEEDED", started.stdout + started.stderr)

    def test_confirmed_finding_under_a_crashed_writers_marker_records_nothing(self):
        # Section 05 3.5 (re-check loads live state the cheap way and never
        # reconciles): a close that crashed during the held audit leaves its
        # lifecycle marker. The confirmed finding is printed, but nothing is
        # condemned or recorded, so the next start still finishes the close
        # (an epoch advance here would make the marker unreconcilable).
        crashed = self.start("crashed-close")
        self.tamper_decision_outcome()
        epoch_before = self.state()["_audit_epoch"]
        verdict_before = self.read_bytes(VERDICT)
        process, _captured = self.hold_doctor()
        close = self.probe(
            "crash_log_rewrite", "close", "--run", crashed,
            "--outcome", "success", "--summary", "Close that crashed.",
        )
        self.assertEqual(close.returncode, 110, close.stdout + close.stderr)
        marker = json.loads((self.root / TRANSITION).read_text("utf-8"))
        self.assertEqual((marker["kind"], marker["run_id"]), ("lifecycle", crashed))
        rc, out, err = self.release(".test-release-audit", process)
        self.assertEqual(rc, 1, out + err)
        self.assertIn("BIMRI doctor: FAILED", out)
        self.assertIn(self.decision_finding(), out)
        self.assertIn(
            "  - nothing was recorded: an operation (close %s) was left "
            "unfinished during the audit; the next writable command reconciles "
            "the unfinished operation; run doctor again" % crashed,
            out,
        )
        self.assertEqual(self.state()["_audit_epoch"], epoch_before)
        self.assertEqual(self.read_bytes(VERDICT), verdict_before)
        self.assertTrue((self.root / TRANSITION).is_file())
        self.assertEqual(self.copy_dirs(), [])
        receipts_before = self.drift_receipts()
        self.start("after-crash")
        self.assertFalse((self.root / TRANSITION).exists())
        log = (self.root / ".bimri" / "log" / (crashed + ".md")).read_text("utf-8")
        self.assertIn("[CLOSED:%s " % crashed, log)
        self.assertNotIn(crashed, self.state()["active_runs"])
        added = [p for p in self.drift_receipts() if p not in receipts_before]
        self.assertNotIn(
            "does not match its intended state/log bytes",
            self.drift_reasons(added),
        )
        # The damage is still there; the next doctor records it.
        doctor = self.cli("doctor", check=False)
        self.assertEqual(doctor.returncode, 1, doctor.stdout + doctor.stderr)
        self.assertIn(self.decision_finding(), " ".join(self.verdict()["issues"]))

    def test_quiet_store_records_unconfirmable_findings_too(self):
        # Section 05 3.5 item 2: with no write during the audit, every finding
        # (per-record and reference) lands in the failed verdict.
        self.tamper_decision_outcome()
        self.plant_unconfirmable_reference()
        doctor = self.cli("doctor", check=False)
        self.assertEqual(doctor.returncode, 1, doctor.stdout + doctor.stderr)
        self.assertNotIn("further findings not confirmed", doctor.stdout)
        issues = " ".join(self.verdict()["issues"])
        self.assertIn(self.decision_finding(), issues)
        self.assertIn("references another run's proposal", issues)

    def test_copy_evidence_finding_never_condemns(self):
        # Section 05 3.5 items 1-2 (L19): a missing facts generation fails the
        # copy audit with the prior-evidence line; verdict failed, the epoch
        # does not move (the live audit's condemnation filter).
        witness = self.witness()
        facts = self.root / GENERATIONS / ("facts-%s.json" % witness["facts_hash"])
        self.assertTrue(facts.is_file())
        facts.unlink()
        epoch_before = self.state()["_audit_epoch"]
        doctor = self.cli("doctor", check=False)
        self.assertEqual(doctor.returncode, 1, doctor.stdout + doctor.stderr)
        self.assertIn("BIMRI doctor: FAILED", doctor.stdout)
        self.assertIn("prior audit manifest evidence is invalid", doctor.stdout)
        verdict = self.verdict()
        self.assertEqual((verdict["source"], verdict["status"]), ("copy", "failed"))
        self.assertEqual(self.state()["_audit_epoch"], epoch_before)

    def test_evidence_finding_is_superseded_when_the_witness_is_resealed(self):
        # Section 05 3.5 item 1 (L19): the witness bytes are part of the
        # re-check, so a reseal during the hold makes the evidence finding
        # unconfirmed: SUPERSEDED, exit 3, epoch unchanged.
        witness = self.witness()
        (self.root / GENERATIONS / ("facts-%s.json" % witness["facts_hash"])).unlink()
        epoch_before = self.state()["_audit_epoch"]
        verdict_before = self.read_bytes(VERDICT)
        process, _captured = self.hold_doctor()
        resealed = dict(witness)
        resealed["created_at"] = "2026-10-15T00:00:01Z"
        resealed["witness_hash"] = record_seal(resealed, "witness_hash")
        (self.root / WITNESS).write_text(
            json.dumps(resealed, indent=2, sort_keys=True) + "\n", encoding="utf-8"
        )
        rc, out, err = self.release(".test-release-audit", process)
        self.assertEqual(rc, 3, out + err)
        self.assertIn("BIMRI doctor: SUPERSEDED (the store changed during the audit;", out)
        self.assertEqual(self.state()["_audit_epoch"], epoch_before)
        self.assertEqual(self.read_bytes(VERDICT), verdict_before)

    # ---- drift on the copy path ------------------------------------------

    def drift_one_proposal(self):
        proposal = self.root / ".bimri" / "proposals" / "R000001-Q002.json"
        proposal.write_bytes(proposal.read_bytes() + b"\n")
        return "proposals/R000001-Q002.json"

    def test_copy_doctor_receipts_benign_drift_without_rebaseline(self):
        # Section 09 step 8 drift rule, 04 4.9 item 10: harmless drift passes,
        # one live receipt names the path with the copy reason, the warning
        # names doctor --live, witness/manifest/facts bytes are untouched.
        relative = self.drift_one_proposal()
        receipts_before = self.drift_receipts()
        before = self.evidence()
        doctor = self.cli("doctor")
        self.assertIsNotNone(PASSED_COPY_RE.search(doctor.stdout), doctor.stdout)
        self.assertRegex(
            doctor.stdout,
            r"checkpoint drift: [^\n]*" + re.escape(relative)
            + r"[^\n]*; run doctor --live to rebaseline",
        )
        self.assertEqual(self.evidence(), before)
        receipts = self.drift_receipts()
        self.assertEqual(len(receipts), len(receipts_before) + 1)
        new = [p for p in receipts if p not in receipts_before]
        reasons = self.drift_reasons(new)
        self.assertIn("copy audit found checkpoint drift", reasons)
        self.assertIn(relative, reasons)
        verdict = self.verdict()
        self.assertEqual((verdict["source"], verdict["status"]), ("copy", "passed"))

    def test_second_copy_doctor_adds_no_drift_receipt(self):
        # Section 09 step 8: the receipt is skipped when the newest one already
        # carries the same delta against the same witness_hash.
        self.drift_one_proposal()
        self.cli("doctor")
        receipts = self.drift_receipts()
        again = self.cli("doctor")
        self.assertIn("checkpoint drift:", again.stdout)
        self.assertEqual(self.drift_receipts(), receipts)

    def test_read_only_doctor_reports_drift_and_writes_nothing(self):
        # Section 10 gate 7 drift test, section 05 3.7: read-only names the
        # drift and leaves the protected tree byte-equal.
        self.drift_one_proposal()
        before = protected_tree_snapshot(self.root)
        read_only = self.cli("doctor", "--read-only")
        self.assertIn("checkpoint drift", read_only.stdout)
        self.assertEqual(protected_tree_snapshot(self.root), before)
        self.assertEqual(self.copy_dirs(), [])

    def test_doctor_live_rebaselines_copy_drift(self):
        # Section 09 step 8: only doctor --live adopts the drifted bytes, with
        # its own inventory receipt and a changed witness; the warning is gone.
        self.drift_one_proposal()
        self.cli("doctor")
        receipts = self.drift_receipts()
        witness_before = self.read_bytes(WITNESS)
        live = self.cli("doctor", "--live")
        self.assertIsNotNone(PASSED_LIVE_RE.search(live.stdout), live.stdout)
        after = self.drift_receipts()
        self.assertEqual(len(after), len(receipts) + 1)
        self.assertRegex(
            self.drift_reasons([p for p in after if p not in receipts]).lower(),
            r"inventory|differs",
        )
        self.assertNotEqual(self.read_bytes(WITNESS), witness_before)
        self.assertEqual(self.verdict()["source"], "live")
        quiet = self.cli("doctor")
        self.assertNotIn("run doctor --live to rebaseline", quiet.stdout)

    # ---- markers: review and read-only doctor never reconcile --------------

    def leave_authority_marker(self):
        run_id = self.start("marker")
        self.propose(run_id, "marker.key", "Value whose sync crashed.")
        crashed = subprocess.run(
            [sys.executable, str(CRASH_WORKER), str(ENGINE),
             "witness_crash_before_replace", str(self.root), "sync", "--run", run_id],
            capture_output=True, text=True, timeout=120,
        )
        self.assertEqual(crashed.returncode, 110, crashed.stdout + crashed.stderr)
        marker = json.loads((self.root / TRANSITION).read_text("utf-8"))
        return marker

    def test_read_only_doctor_and_review_leave_a_save_marker_alone(self):
        # Section 05 3.7, 4 (L20), D5.3.3: with an unfinished operation's
        # marker, doctor --read-only reports the transition first and review
        # its unfinished-operation line first; neither changes a byte.
        marker = self.leave_authority_marker()
        before = protected_tree_snapshot(self.root)
        read_only = self.cli("doctor", "--read-only", check=False)
        self.assertIn("BIMRI doctor (read-only): PASSED", read_only.stdout)
        bullets = [l for l in read_only.stdout.splitlines() if l.startswith("  - ")]
        self.assertTrue(bullets, read_only.stdout)
        self.assertTrue(
            bullets[0].startswith(
                "  - audit transition is incomplete; the next writable command "
                "reconciles it: %s %s" % (marker["operation"], marker["run_id"])
            ),
            read_only.stdout,
        )
        review = self.cli("review", check=False)
        self.assertEqual(
            review.stdout.splitlines()[0],
            "BIMRI: an engine authority operation is unfinished (%s %s); the next "
            "writable command finishes it" % (marker["operation"], marker["run_id"]),
        )
        self.assertEqual(protected_tree_snapshot(self.root), before)
        self.assertTrue((self.root / TRANSITION).is_file())
        self.assertEqual(self.copy_dirs(), [])

    def test_status_finishes_the_marker_review_left_alone(self):
        # D3.4.1 with L20: the next load_or_initialize caller finishes it.
        self.leave_authority_marker()
        self.cli("review", check=False)
        self.cli("doctor", "--read-only", check=False)
        self.cli("status", check=False)
        self.assertFalse((self.root / TRANSITION).exists())

    # ---- capture: two phases, budget, deferral --------------------------------

    def phase_a_report(self, complete=False):
        report = self.workspace / "phase-a.json"
        arguments = [report] + (["complete"] if complete else [])
        result = self.probe("phase_a", *arguments, timeout=600)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        return json.loads(report.read_text("utf-8"))

    def test_capture_lock_time_does_not_grow_with_history(self):
        # Section 05 2.2-2.3 (L17): Phase A stays under the shipped budget,
        # opens and links nothing under the linked roots, log/ or archive/,
        # on the fixture and on a 50,000-file synthetic history.
        report = self.phase_a_report(complete=True)
        self.assertEqual(report["touched"], [])
        self.assertLess(report["phase_a_ms"], report["budget_ms"])
        self.assertLess(report["receipt_lock_held_ms"], report["budget_ms"])
        self.assertEqual(Path(report["copy_parent"]), self.root.resolve())
        proposals = self.root / ".bimri" / "proposals"
        payload = b"{}\n"
        for index in range(SYNTHETIC_HISTORY_FILES):
            with open(str(proposals / ("synthetic-%05d.bin" % index)), "wb") as handle:
                handle.write(payload)
        grown = self.phase_a_report(complete=False)
        self.assertEqual(grown["touched"], [])
        self.assertLess(grown["phase_a_ms"], grown["budget_ms"])
        self.assertEqual(self.copy_dirs(), [])

    def between_phases(self, mutate):
        witness_before = self.read_bytes(WITNESS)
        verdict_before = self.read_bytes(VERDICT)
        process = self.popen_probe("between", "doctor")
        self.wait_for(".test-phase-a-done", process)
        mutate()
        rc, out, err = self.release(".test-phase-b-go", process)
        self.assertEqual(rc, 3, out + err)
        self.assertRegex(
            out.splitlines()[0],
            r"^BIMRI doctor: DEFERRED \(store changed during capture after [\d,]+ "
            r"files; " + NEWEST_VERDICT_TAIL + r"$",
        )
        self.assertEqual(self.copy_dirs(), [])
        self.assertEqual(self.read_bytes(WITNESS), witness_before)
        self.assertEqual(self.read_bytes(VERDICT), verdict_before)

    def test_capture_detects_a_file_replaced_between_phases(self):
        # Section 05 2.2: an inventoried decision atomically replaced after
        # Phase A fails the size/mtime check: DEFERRED, exit 3, nothing kept.
        decision = self.root / ".bimri" / "decisions" / "R000001-Q003.json"

        def replace():
            body = json.loads(decision.read_text("utf-8"))
            temporary = decision.with_name("replacement.tmp")
            temporary.write_text(json.dumps(body, indent=4, sort_keys=True), "utf-8")
            os.replace(str(temporary), str(decision))

        self.between_phases(replace)

    def test_capture_detects_a_log_truncated_between_phases(self):
        # Section 05 2.2: a log shorter than its recorded length is a change.
        log = self.root / ".bimri" / "log" / "R000001.md"

        def truncate():
            content = log.read_bytes()
            self.assertGreater(len(content), 1)
            log.write_bytes(content[: len(content) // 2])

        self.between_phases(truncate)

    def redirect_name(self, path, outside_bytes):
        # Swap a live name for a redirection whose target is outside the
        # store: a file symlink off Windows (bytes that were never live), a
        # junction to a directory on Windows (file symlinks need privilege).
        path.unlink()
        if os.name == "nt":
            import _winapi
            outside = self.workspace / "outside-directory"
            outside.mkdir(exist_ok=True)
            _winapi.CreateJunction(str(outside), str(path))
        else:
            outside = self.workspace / ("outside-" + path.name)
            outside.write_bytes(outside_bytes)
            os.symlink(str(outside), str(path))
        return outside

    def test_capture_never_reads_a_log_redirected_between_phases(self):
        # Section 05 2.2: Phase B opens an appended file only when its name is
        # still the plain file it lstat-ed (O_NOFOLLOW where available); a log
        # swapped for a redirection after Phase A is a change, never read.
        log = self.root / ".bimri" / "log" / "R000001.md"

        def redirect():
            content = log.read_bytes()
            self.redirect_name(log, content + b"[OUTSIDE] never live\n")

        self.between_phases(redirect)

    @unittest.skipIf(os.name == "nt", "file symlinks need privilege on Windows")
    def test_capture_never_links_through_a_record_redirected_between_phases(self):
        # Section 05 2.2: the live decision is damaged; after Phase A its name
        # becomes a symlink to outside bytes that are valid and carry the
        # recorded size and mtime_ns. Phase B links without following and
        # re-reads the live name, so the copy never audits bytes that were
        # never live (which would pass): DEFERRED, nothing recorded.
        path, original = self.tamper_decision_outcome()
        self.assertEqual(len(original), len(path.read_bytes()))
        recorded = os.stat(str(path))

        def redirect():
            outside = self.redirect_name(path, original)
            os.utime(
                str(outside), ns=(recorded.st_atime_ns, recorded.st_mtime_ns)
            )

        self.between_phases(redirect)

    def test_copy_budget_exhaustion_defers_with_exit_3(self):
        # D5.3.1, section 05 2.3, 3.8: --budget-ms 1 defers with the newest
        # verdict named, removes the partial copy, frees the lock and leaves
        # witness and verdict bytes unchanged.
        witness_before = self.read_bytes(WITNESS)
        verdict_before = self.read_bytes(VERDICT)
        started = time.perf_counter()
        doctor = self.cli("doctor", "--budget-ms", "1", check=False)
        self.assertLess(time.perf_counter() - started, 5.0 + 3.0)
        self.assertEqual(doctor.returncode, 3, doctor.stdout + doctor.stderr)
        self.assertRegex(
            doctor.stdout.splitlines()[0],
            r"^BIMRI doctor: DEFERRED \(copy budget 1 ms exceeded after [\d,]+ "
            r"files; " + NEWEST_VERDICT_TAIL + r"$",
        )
        self.assertEqual(self.copy_dirs(), [])
        self.assertEqual(self.read_bytes(WITNESS), witness_before)
        self.assertEqual(self.read_bytes(VERDICT), verdict_before)
        got, ms = self.timed("get", "--key", "fixture.k00")
        self.assertEqual(got.returncode, 0, got.stdout + got.stderr)
        self.assertLess(ms, 3000.0)

    def test_review_budget_exhaustion_defers_with_exit_3(self):
        # D1.8, L4: review takes --budget-ms and defers with exit 3.
        review = self.cli("review", "--budget-ms", "1", check=False)
        self.assertEqual(review.returncode, 3, review.stdout + review.stderr)
        self.assertIn("BIMRI review: DEFERRED (copy budget 1 ms exceeded after", review.stdout)
        self.assertEqual(self.copy_dirs(), [])

    def test_read_only_doctor_budget_exhaustion_defers_with_exit_3(self):
        # Section 05 3.7: the read-only deferral line, exit 3, nothing written.
        before = protected_tree_snapshot(self.root)
        result = self.cli("doctor", "--read-only", "--budget-ms", "1", check=False)
        self.assertEqual(result.returncode, 3, result.stdout + result.stderr)
        self.assertIn("BIMRI doctor (read-only): DEFERRED (", result.stdout)
        self.assertEqual(protected_tree_snapshot(self.root), before)
        self.assertEqual(self.copy_dirs(), [])

    def test_history_read_deferral_goes_to_stderr_with_empty_stdout(self):
        # Section 05 5 (L7): a deferred --history capture prints the DEFERRED
        # line on stderr, nothing on stdout, exit 3.
        for arguments in (
            ("get", "--key", "fixture.k00", "--history"),
            ("search", "--query", "fixture", "--audit"),
        ):
            with self.subTest(arguments=arguments):
                result = self.probe("budget1", *arguments)
                self.assertEqual(result.returncode, 3, result.stdout + result.stderr)
                self.assertEqual(result.stdout, "")
                self.assertRegex(
                    result.stderr,
                    r"BIMRI: DEFERRED \(copy budget 1 ms exceeded after [\d,]+ "
                    r"files; retry when the store is quiet\)",
                )
                self.assertEqual(self.copy_dirs(), [])

    def test_budget_ms_must_be_a_positive_integer_up_to_600000(self):
        # Section 05 2.3: N outside 1..600000 is refused, exit 2, no write.
        before = protected_tree_snapshot(self.root)
        for value in ("0", "-5", "600001", "fast"):
            with self.subTest(value=value):
                result = self.cli("doctor", "--budget-ms", value, check=False)
                self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
        self.assertEqual(protected_tree_snapshot(self.root), before)
        self.assertEqual(self.copy_dirs(), [])

    def test_doctor_live_with_read_only_is_an_argparse_error(self):
        # Section 09 step 8: doctor --live --read-only is a usage error.
        before = protected_tree_snapshot(self.root)
        result = self.cli("doctor", "--live", "--read-only", check=False)
        self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
        self.assertIn("usage:", result.stderr)
        self.assertEqual(protected_tree_snapshot(self.root), before)

    # ---- leftovers and refusals ------------------------------------------

    def test_doctor_removes_a_leftover_audit_copy_before_capture(self):
        # Section 05 2.3, D5.3.1, Decisions 26: a killed audit's copy is
        # removed by the next doctor; a sibling without the exact prefix stays.
        leftover = self.root / (COPY_PREFIX + "deadbeef")
        (leftover / ".bimri").mkdir(parents=True)
        (leftover / ".bimri" / "state.json").write_text("{}", encoding="utf-8")
        keep = self.root / ("keep" + COPY_PREFIX + "00000000")
        keep.mkdir()
        doctor = self.cli("doctor")
        self.assertIsNotNone(PASSED_COPY_RE.search(doctor.stdout), doctor.stdout)
        self.assertFalse(leftover.exists())
        self.assertTrue(keep.is_dir())

    def test_review_removes_a_leftover_audit_copy_before_capture(self):
        # Section 05 2.3: review also clears a killed audit's copy.
        leftover = self.root / (COPY_PREFIX + "0badf00d")
        leftover.mkdir()
        self.cli("review")
        self.assertFalse(leftover.exists())

    @unittest.skipIf(os.name == "nt", "symlinks need privilege on Windows")
    def test_doctor_refuses_redirected_root_entry_without_copy(self):
        # Section 05 2.2, 2.5 (L3): a redirected entry in a linked root is a
        # live finding under the lock: FAILED, exit 1, no copy directory.
        target = self.workspace / "outside.json"
        target.write_text("{}", encoding="utf-8")
        os.symlink(str(target), str(self.root / ".bimri" / "decisions" / "R000001-Q099.json"))
        doctor = self.cli("doctor", check=False)
        self.assertEqual(doctor.returncode, 1, doctor.stdout + doctor.stderr)
        self.assertIn("BIMRI doctor: FAILED", doctor.stdout)
        self.assertIn("audit witness refused redirected path", doctor.stdout)
        self.assertEqual(self.copy_dirs(), [])

    def test_redirected_entry_outside_witness_roots_does_not_stop_the_copy(self):
        # Section 05 2.2 / L3 scope the capture's refusal to witness roots and
        # run logs. A redirected directory in backups/ (a junction on Windows,
        # a directory symlink elsewhere) is passed over by every live reader:
        # doctor --live passes, so the copied doctor, review and --history
        # read as usual, and nothing behind the redirection is touched.
        outside = self.workspace / "outside-directory"
        outside.mkdir()
        (outside / "kept.json").write_text("{}", encoding="utf-8")
        name = self.root / ".bimri" / "backups" / "redirected"
        self.assertTrue(name.parent.is_dir())
        if os.name == "nt":
            import _winapi
            _winapi.CreateJunction(str(outside), str(name))
        else:
            os.symlink(str(outside), str(name))
        doctor = self.cli("doctor", check=False)
        self.assertEqual(doctor.returncode, 0, doctor.stdout + doctor.stderr)
        self.assertIsNotNone(PASSED_COPY_RE.search(doctor.stdout), doctor.stdout)
        self.assertEqual(self.verdict()["source"], "copy")
        review = self.cli("review", check=False)
        self.assertEqual(review.returncode, 0, review.stdout + review.stderr)
        history = self.cli("get", "--key", "fixture.k00", "--history", check=False)
        self.assertEqual(history.returncode, 0, history.stdout + history.stderr)
        self.assertIn("fixture.k00", history.stdout)
        read_only = self.cli("doctor", "--read-only", check=False)
        self.assertEqual(read_only.returncode, 0, read_only.stdout + read_only.stderr)
        live = self.cli("doctor", "--live", check=False)
        self.assertEqual(live.returncode, 0, live.stdout + live.stderr)
        self.assertEqual(self.copy_dirs(), [])
        self.assertTrue((outside / "kept.json").is_file())

    @unittest.skipIf(os.name == "nt", "file symlinks need privilege on Windows")
    def test_redirected_verdict_is_mirrored_and_never_written_through(self):
        # A symlinked verdict is derived and outside the witness roots: the
        # live audit neither reads nor replaces it (load_audit_verdict,
        # write_audit_verdict). The capture mirrors the link instead of
        # refusing, so doctor passes on the copy, review and --history read
        # as usual, and the link and its target are left as they were.
        self.cli("doctor")
        verdict = self.root / VERDICT
        outside = self.workspace / "outside-verdict.json"
        outside.write_bytes(verdict.read_bytes())
        verdict.unlink()
        os.symlink(str(outside), str(verdict))
        target_before = outside.read_bytes()
        doctor = self.cli("doctor", check=False)
        self.assertEqual(doctor.returncode, 0, doctor.stdout + doctor.stderr)
        self.assertIsNotNone(PASSED_COPY_RE.search(doctor.stdout), doctor.stdout)
        review = self.cli("review", check=False)
        self.assertEqual(review.returncode, 0, review.stdout + review.stderr)
        history = self.cli("get", "--key", "fixture.k00", "--history", check=False)
        self.assertEqual(history.returncode, 0, history.stdout + history.stderr)
        self.assertTrue(verdict.is_symlink())
        self.assertEqual(outside.read_bytes(), target_before)
        self.assertEqual(self.copy_dirs(), [])

    @unittest.skipIf(os.name == "nt", "file symlinks need privilege on Windows")
    def test_redirected_index_reads_on_the_copy_as_it_does_live(self):
        # doctor --read-only reports a symlinked index exactly as the live
        # read-only audit does (doctor_errors), not as a capture refusal;
        # review never reads the index and reads as usual.
        index = self.root / ".bimri" / "index.tsv"
        outside = self.workspace / "outside-index.tsv"
        outside.write_bytes(index.read_bytes() if index.is_file() else b"")
        if index.exists():
            index.unlink()
        os.symlink(str(outside), str(index))
        read_only = self.cli("doctor", "--read-only", check=False)
        self.assertEqual(read_only.returncode, 1, read_only.stdout + read_only.stderr)
        self.assertIn(
            "  - index is a symbolic link and was not read.", read_only.stdout
        )
        self.assertNotIn("audit copy refused", read_only.stdout)
        review = self.cli("review", check=False)
        self.assertEqual(review.returncode, 0, review.stdout + review.stderr)
        self.assertTrue(index.is_symlink())
        self.assertEqual(self.copy_dirs(), [])

    def test_doctor_read_only_writes_nothing_and_busy_is_exit_2(self):
        # Section 05 3.7 (L6): read-only writes nothing; on a held lock it is
        # BIMRI ERROR on stderr, exit 2, not a FAILED line.
        before = protected_tree_snapshot(self.root)
        self.cli("doctor", "--read-only")
        self.assertEqual(protected_tree_snapshot(self.root), before)
        writer = self.start("lock-holder")
        process = subprocess.Popen(
            [sys.executable, str(CRASH_WORKER), str(ENGINE), "journal_hold_lock",
             str(self.root), "journal", "--run", writer, "--text", "Holding."],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
        )
        self._processes.append(process)
        self.wait_for(".test-writer-lock-held", process)
        busy = self.cli("doctor", "--read-only", check=False, timeout=60)
        self.release(".test-release-writer-lock", process)
        self.assertEqual(busy.returncode, 2, busy.stdout + busy.stderr)
        self.assertIn("BIMRI ERROR:", busy.stderr)
        self.assertIn("busy", busy.stderr)
        self.assertNotIn("FAILED", busy.stdout)

    # ---- seeding: the live path -----------------------------------------------

    def test_doctor_live_seeds_when_no_checkpoint(self):
        # D5.3.2, section 05 3.1-3.2: without a valid checkpoint doctor audits
        # live, says so first, publishes a schema-2 witness with its facts and
        # a source: live verdict.
        (self.root / WITNESS).unlink()
        doctor = self.cli("doctor")
        lines = doctor.stdout.splitlines()
        self.assertEqual(lines[0], NO_CHECKPOINT_LINE)
        self.assertIsNotNone(PASSED_LIVE_RE.match(lines[1]), doctor.stdout)
        witness = self.witness()
        self.assertEqual(witness["witness_schema"], 2)
        self.assertTrue(
            (self.root / GENERATIONS / ("facts-%s.json" % witness["facts_hash"])).is_file()
        )
        verdict = self.verdict()
        self.assertEqual((verdict["source"], verdict["status"]), ("live", "passed"))
        self.assertIsNone(verdict["copy_capture_ms"])

    def test_doctor_after_seeding_takes_the_copy_path(self):
        # D5.3.2: once seeded, the next default doctor audits a copy.
        (self.root / WITNESS).unlink()
        self.cli("doctor")
        second = self.cli("doctor")
        self.assertNotIn(NO_CHECKPOINT_LINE, second.stdout)
        self.assertIsNotNone(PASSED_COPY_RE.search(second.stdout), second.stdout)
        self.assertEqual(self.verdict()["source"], "copy")

    def test_doctor_live_republishes_on_a_warm_store(self):
        # Section 05 3.2: --live is today's in-lock full audit; it republishes
        # the witness and writes a live verdict without the seeding line.
        witness_before = self.read_bytes(WITNESS)
        live = self.cli("doctor", "--live")
        self.assertNotIn(NO_CHECKPOINT_LINE, live.stdout)
        self.assertIsNotNone(PASSED_LIVE_RE.search(live.stdout), live.stdout)
        self.assertNotEqual(self.read_bytes(WITNESS), witness_before)
        self.assertEqual(self.verdict()["source"], "live")

    # ---- reads never publish --------------------------------------------------

    def assert_read_publishes_nothing(self, *arguments):
        before = (self.read_bytes(WITNESS), self.read_bytes(VERDICT), self.generations())
        result = self.cli(*arguments)
        self.assertEqual(
            (self.read_bytes(WITNESS), self.read_bytes(VERDICT), self.generations()),
            before,
        )
        self.assertEqual(self.copy_dirs(), [])
        return result

    def test_history_read_never_publishes(self):
        # D5.3.3, section 05 5 (L13): get --history audits a copy; no witness,
        # manifest generation or verdict changes.
        result = self.assert_read_publishes_nothing(
            "get", "--key", "fixture.k00", "--history"
        )
        self.assertIn("fixture.k00", result.stdout)

    def test_recall_query_history_never_publishes(self):
        # D5.3.3, section 05 5: recall --query --history is a copy read.
        self.assert_read_publishes_nothing("recall", "--query", "fixture", "--history")

    def test_search_audit_never_publishes(self):
        # D5.3.3, section 05 5 (L14): search --audit is a copy read.
        self.assert_read_publishes_nothing("search", "--query", "fixture", "--audit")

    def test_history_audit_is_accepted_and_identical_to_history(self):
        # Section 05 5 (L14): --history --audit equals --history.
        plain = self.cli("get", "--key", "fixture.k00", "--history")
        audited = self.cli("get", "--key", "fixture.k00", "--history", "--audit")
        self.assertEqual(audited.stdout, plain.stdout)

    def test_review_never_publishes(self):
        # D1.8, D5.3.3, section 05 4 (L13): review audits a copy and writes no
        # witness, generation or verdict.
        self.assert_read_publishes_nothing("review")

    def test_search_audit_without_witness_reports_and_publishes_nothing(self):
        # Section 05 7 (v511:2821 re-aim): with no witness, search --audit
        # still does not publish one.
        (self.root / WITNESS).unlink()
        generations = self.generations()
        self.cli("search", "--query", "fixture", "--audit")
        self.assertFalse((self.root / WITNESS).exists())
        self.assertEqual(self.generations(), generations)

    # ---- gate 7: the held doctor writes no witness -----------------------------

    def test_held_copy_doctor_leaves_the_writers_witness_alone(self):
        # Gate 7, D5.3.2: while the copy audit is held the writers publish the
        # live witness; releasing the doctor changes none of those bytes and
        # writes no manifest generation.
        process, captured = self.hold_doctor()
        writer = self.start("gate7-witness")
        self.propose(writer, "gate7.witness", "Lands while the copy audit is held.")
        self.cli("sync", "--run", writer)
        after_writes = self.evidence()
        self.assertEqual(self.state()["head_revision"], captured["head_revision"] + 1)
        rc, out, err = self.release(".test-release-audit", process)
        self.assertEqual(rc, 0, out + err)
        self.assertIsNotNone(PASSED_COPY_RE.search(out), out)
        self.assertEqual(self.evidence(), after_writes)

    # ---- drift on the copy path: the moved-store and sink rules -----------

    def test_copy_drift_moved_during_the_audit_writes_no_receipt(self):
        # Section 04 4.9 item 10, section 10 gate 7 drift variant: the drifted
        # path is replaced again during the hold, so its live bytes no longer
        # equal the copy's: PASSED, no receipt, the moved-store warning.
        self.drift_one_proposal()
        proposal = self.root / ".bimri" / "proposals" / "R000001-Q002.json"
        receipts_before = self.drift_receipts()
        witness_before = self.read_bytes(WITNESS)
        process, _captured = self.hold_doctor()
        self.replace_bytes(proposal, proposal.read_bytes() + b"\n")
        rc, out, err = self.release(".test-release-audit", process)
        self.assertEqual(rc, 0, out + err)
        self.assertIsNotNone(PASSED_COPY_RE.search(out), out)
        self.assertIn(
            "checkpoint drift seen on the copy was not recorded: the checkpoint "
            "moved during the audit; run doctor again",
            out,
        )
        self.assertEqual(self.drift_receipts(), receipts_before)
        self.assertEqual(self.read_bytes(WITNESS), witness_before)

    def test_unrecordable_copy_drift_receipt_is_a_warning_with_exit_0(self):
        # Section 09 step 8, 04 4.9 item 10: an item-10 receipt that cannot be
        # recorded (the receipt sink obstructed after capture) is the warning
        # line with exit 0, not the live path's rc 1 refusal; witness untouched.
        self.drift_one_proposal()
        sink = self.root / DRIFT
        self.assertFalse(sink.exists())
        before = self.evidence()
        process, _captured = self.hold_doctor()
        sink.write_text("obstruction\n", encoding="utf-8")
        rc, out, err = self.release(".test-release-audit", process)
        self.assertEqual(rc, 0, out + err)
        self.assertIsNotNone(PASSED_COPY_RE.search(out), out)
        self.assertIn("checkpoint drift could not be recorded:", out)
        self.assertEqual(self.evidence(), before)
        self.assertTrue(sink.is_file())

    # ---- copy reads report, never condemn ----------------------------------

    def test_review_on_a_damaged_store_exits_1_and_condemns_nothing(self):
        # Section 05 4 (L13): review reports authority issues as of capture,
        # exit 1, and changes no verdict, witness or epoch (no live re-check).
        self.tamper_decision_outcome()
        before = protected_tree_snapshot(self.root)
        review = self.cli("review", check=False)
        self.assertEqual(review.returncode, 1, review.stdout + review.stderr)
        self.assertIn(TAMPERED, review.stdout + review.stderr)
        self.assertEqual(protected_tree_snapshot(self.root), before)
        self.assertEqual(self.copy_dirs(), [])

    def test_read_only_doctor_on_a_damaged_store_fails_and_writes_nothing(self):
        # Section 05 3.7: doctor --read-only prints FAILED, exit 1, never
        # condemns and never writes a verdict.
        self.tamper_decision_outcome()
        before = protected_tree_snapshot(self.root)
        result = self.cli("doctor", "--read-only", check=False)
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertIn("BIMRI doctor (read-only): FAILED", result.stdout)
        self.assertIn(self.decision_finding(), result.stdout)
        self.assertEqual(protected_tree_snapshot(self.root), before)
        self.assertEqual(self.copy_dirs(), [])

    def test_history_read_on_a_damaged_store_stops_without_condemning(self):
        # Section 05 5 (L13, D5.3.3): a negative copy finding stops the read
        # with the recall error, exit 2, and the live store is not condemned.
        self.tamper_decision_outcome()
        before = protected_tree_snapshot(self.root)
        result = self.cli("get", "--key", "fixture.k00", "--history", check=False)
        self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
        self.assertIn(
            "recall stopped because authority recovery is required", result.stderr
        )
        self.assertEqual(protected_tree_snapshot(self.root), before)
        self.assertEqual(self.copy_dirs(), [])

    def test_recall_query_audit_never_publishes(self):
        # Section 05 5 (L14): recall --query --audit is a copy read.
        self.assert_read_publishes_nothing("recall", "--query", "fixture", "--audit")

    def test_exact_get_audit_never_publishes(self):
        # Section 05 5 (L14): get --key K --audit forces the full check on a
        # copy and publishes nothing.
        result = self.assert_read_publishes_nothing(
            "get", "--key", "fixture.k00", "--audit"
        )
        self.assertIn("fixture.k00", result.stdout)

    # ---- condemned store: the live path is automatic -------------------------

    def test_doctor_after_a_copy_condemnation_audits_live(self):
        # Section 05 3.1 (D5.3.2): a condemned witness is no valid checkpoint,
        # so the next doctor says so first and takes the live path.
        self.tamper_decision_outcome()
        self.assertEqual(self.cli("doctor", check=False).returncode, 1)
        again = self.cli("doctor", check=False)
        self.assertEqual(again.returncode, 1, again.stdout + again.stderr)
        self.assertEqual(again.stdout.splitlines()[0], NO_CHECKPOINT_LINE)
        self.assertEqual(self.verdict()["source"], "live")
        self.assertEqual(self.copy_dirs(), [])


if __name__ == "__main__":
    unittest.main()
