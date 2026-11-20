"""BIMRI v5.2 build step 1: versions, writer gate, witness schema 2, facts
generation, audit verdict, scoped publication primitives, scoped recovery.

Spine: design/DECISIONS.md D0.1-D0.6, D2.2, D2.3, D2.4 (scope keys), D2.8,
D3.4.3, D4.4 (note_protected_write), build order section 9 step 1; detail from
design/DESIGN.md sections 02 (2.1, 2.4, 2.5, 2.6, 2.9, 2.13), 04 (4.4-4.7),
09 (step 1, local decision 1) and 10 (gate 9).

Process model (section 10.0): behaviour is driven through the public command
boundary in separate processes. Two kinds of instrumentation stay out of the
test process:

- ``tests/crash_worker.py`` modes that already exist
  (``witness_crash_before_replace``, ``lifecycle_crash_before_log``,
  ``protected_mutation_monitor``, ``valid_witness_forbid_full_audit``);
- a small probe written into each test's temp workspace (``PROBE_SOURCE``)
  that imports the engine by path, counts every sha256 digest the engine
  computes (``hashes``) or records every ``state.json`` / marker write
  (``statewrites``), then runs ``engine.main``. It patches module
  attributes only, the same rule as crash_worker.

The three primitive tests (scoped publication, scope-key grammar, witness and
state-version validators) import the engine in-process, as section 09 step 1
says ("unit-tested in-process only").

The 5.1.2 comparison engine is main at 2c936ec, materialised with
``git archive`` exactly as tests/test_v510_lifecycle.py:272 does, so its own
install path has every source file.
"""

import datetime as dt
import hashlib
import importlib.util
import io
import json
import os
import re
import shutil
import stat
import subprocess
import sys
import tarfile
import tempfile
import unittest
import uuid
from pathlib import Path, PurePosixPath


REPOSITORY = Path(__file__).resolve().parents[1]
ENGINE = REPOSITORY / "bimri-engine.py"
CRASH_WORKER = REPOSITORY / "tests" / "crash_worker.py"
PRIOR_RELEASE_COMMIT = "2c936ec"
RUN_RE = re.compile(r"BIMRI RUN HANDLE: (R\d{6})")
PROPOSAL_RE = re.compile(r"\bR\d{6}-Q\d{3}\b")
HASH_RE = re.compile(r"^[0-9a-f]{64}$")
PRIOR_EVIDENCE_INVALID_PREFIX = "prior audit manifest evidence is invalid"
STATE_REFUSAL = "unsupported BIMRI state version: 5.2.1"
READ_ONLY_REFUSAL = "read-only audit requires memory format v5.1.0; found 5.2.1."
WITNESSED_ROOTS = (
    "proposals",
    "decisions",
    "conflicts",
    "resolutions",
    "revisions",
    "archive",
    "recovery",
    "settlements",
)
WITNESS_FIELDS_V1 = {
    "witness_schema",
    "engine_version",
    "memory_format_version",
    "policy_version",
    "created_at",
    "head_revision",
    "head_hash",
    "state_hash",
    "write_state_hash",
    "audit_epoch",
    "run_authority_hash",
    "manifest_hash",
    "manifest_count",
    "proposal_runs",
    "witness_hash",
}
WITNESS_FIELDS_V2 = WITNESS_FIELDS_V1 | {
    "facts_hash",
    "audited_at",
    "audit_origin",
    "scoped_publications",
}
FACTS_FIELDS = {
    "facts_schema",
    "created_at",
    "manifest_hash",
    "run_facts",
    "run_authority_hash",
    "unsettled",
    "applying_resolutions",
    "open_conflicts",
    "held",
    "settled",
    "writers_by_key",
    "watermarks",
    "facts_hash",
}
RUN_FACT_FIELDS = {
    "proposal_id", "run_id", "file_present", "marker_present", "closed",
}
UNSETTLED_FIELDS = {
    "applying_decisions", "applying_resolutions", "failed_resolutions",
}
WATERMARK_FIELDS = {"proposal", "pattern", "conflict", "run"}
VERDICT_FIELDS = {
    "verdict_schema",
    "created_at",
    "source",
    "status",
    "head_revision",
    "head_hash",
    "audit_epoch",
    "manifest_hash",
    "issue_count",
    "issues",
    "copy_capture_ms",
    "verdict_hash",
}
PROTECTED_EXCLUSIONS = {
    ".bimri/engine.lock",
    ".bimri/runtime.local.json",
    ".bimri/hooks.claude.local.json",
}

# Out-of-process probe. It never changes engine behaviour: it only observes
# digests (mode "hashes") or the content of state/marker replacements (mode
# "statewrites") and then runs the ordinary command.
PROBE_SOURCE = r'''
import hashlib
import importlib.util
import json
import sys
from pathlib import Path


def main():
    engine_path, mode, out_path = sys.argv[1], sys.argv[2], sys.argv[3]
    argv = sys.argv[4:]
    spec = importlib.util.spec_from_file_location(
        "bimri_v520_probe_target", engine_path
    )
    engine = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(engine)
    records = []
    if mode == "hashes":
        real = hashlib

        class CountingHash(object):
            def __init__(self, inner):
                self._inner = inner

            def update(self, data):
                self._inner.update(data)

            def copy(self):
                return CountingHash(self._inner.copy())

            def hexdigest(self):
                value = self._inner.hexdigest()
                records.append(value)
                return value

            def digest(self):
                records.append(self._inner.hexdigest())
                return self._inner.digest()

            def __getattr__(self, name):
                return getattr(self._inner, name)

        class CountingHashlib(object):
            def __getattr__(self, name):
                return getattr(real, name)

            def sha256(self, data=b"", **kwargs):
                inner = real.sha256(**kwargs)
                if data:
                    inner.update(data)
                return CountingHash(inner)

        engine.hashlib = CountingHashlib()
    elif mode == "statewrites":
        def record(path, payload):
            candidate = Path(path)
            if (
                candidate.parent.name == ".bimri"
                and candidate.name in ("state.json", "audit-transition.json")
            ):
                if isinstance(payload, bytes):
                    payload = payload.decode("utf-8")
                try:
                    content = json.loads(payload)
                except ValueError:
                    content = None
                records.append({"file": candidate.name, "content": content})

        original_text = engine.atomic_write_text
        original_bytes = engine.atomic_write_bytes

        def text_writer(path, content):
            record(path, content)
            return original_text(path, content)

        def bytes_writer(path, content):
            record(path, content)
            return original_bytes(path, content)

        engine.atomic_write_text = text_writer
        engine.atomic_write_bytes = bytes_writer
    else:
        raise SystemExit("unknown probe mode: " + mode)
    code = 2
    try:
        code = engine.main(argv)
    finally:
        Path(out_path).write_text(json.dumps(records), encoding="utf-8")
        sys.stdout.flush()
        sys.stderr.flush()
    sys.exit(code if isinstance(code, int) else 0)


if __name__ == "__main__":
    main()
'''


def protected_tree_snapshot(root):
    """Copy of tests/test_bimri_engine.py:62 (kept local so this file has no
    import-order dependency on another test module)."""
    root = Path(root)
    records = {}

    def record(path):
        metadata = path.lstat()
        mode = metadata.st_mode
        reparse = bool(
            getattr(metadata, "st_file_attributes", 0)
            & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x0400)
        )
        if stat.S_ISLNK(mode) or reparse:
            target = os.readlink(path)
            return {"type": "symlink", "target": target}
        if stat.S_ISDIR(mode):
            return {"type": "directory"}
        if stat.S_ISREG(mode):
            content = path.read_bytes()
            return {
                "type": "file",
                "bytes": len(content),
                "sha256": hashlib.sha256(content).hexdigest(),
            }
        return {"type": "other", "mode": stat.S_IFMT(mode)}

    hot = root / "bimri.md"
    records["bimri.md"] = (
        record(hot) if hot.exists() or hot.is_symlink() else {"type": "missing"}
    )

    def visit(directory):
        for entry in sorted(os.scandir(directory), key=lambda item: item.name):
            path = Path(entry.path)
            relative = path.relative_to(root).as_posix()
            if relative in PROTECTED_EXCLUSIONS:
                continue
            value = record(path)
            records[relative] = value
            if value["type"] == "directory":
                visit(path)

    visit(root / ".bimri")
    return records


def without_prefixes(snapshot, prefixes):
    return {
        relative: value
        for relative, value in snapshot.items()
        if not relative.startswith(tuple(prefixes))
    }


def audit_digest(value):
    encoded = json.dumps(value, separators=(",", ":"), sort_keys=True).encode(
        "utf-8"
    )
    return hashlib.sha256(encoded).hexdigest()


def record_seal(record, seal_field):
    return audit_digest(
        {key: value for key, value in record.items() if key != seal_field}
    )


def revision_path(number):
    return f".bimri/revisions/V{number:06d}.md"


class V520Step1Test(unittest.TestCase):
    maxDiff = None

    @classmethod
    def setUpClass(cls):
        cls._class_temp = tempfile.TemporaryDirectory(prefix="bimri-v520-s1-")
        cls.class_workspace = Path(cls._class_temp.name)
        cls._prior_releases = {}
        cls._help_cache = {}

    @classmethod
    def tearDownClass(cls):
        cls._class_temp.cleanup()

    def setUp(self):
        self._temp = tempfile.TemporaryDirectory(prefix="bimri-v520-s1-")
        self.workspace = Path(self._temp.name)
        self.root = self.workspace / "store"

    def tearDown(self):
        self._temp.cleanup()

    # ---- process helpers (test_v512_defects.py:59-160 recipe) -------------

    def cli(
        self, *arguments, root=None, engine=None, input_text=None,
        check=True, timeout=180,
    ):
        command = [
            sys.executable,
            str(engine or ENGINE),
            "--root",
            str(root or self.root),
            *map(str, arguments),
        ]
        result = subprocess.run(
            command,
            text=True,
            capture_output=True,
            input=input_text,
            timeout=timeout,
            encoding="utf-8",
        )
        if check and result.returncode != 0:
            self.fail(
                f"command failed: {command!r}\n"
                f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}"
            )
        return result

    def help_text(self, engine, *command):
        key = (str(engine), command)
        if key not in self._help_cache:
            result = subprocess.run(
                [sys.executable, str(engine), *command, "--help"],
                text=True, capture_output=True, timeout=60, encoding="utf-8",
            )
            type(self)._help_cache[key] = result.stdout + result.stderr
        return self._help_cache[key]

    def start_arguments(self, engine=None):
        # From build step 4 a plain start opens nothing (D1.1); the legacy
        # lane these tests drive needs --open-run on any engine that has it.
        engine = engine or ENGINE
        arguments = ["start"]
        if "--open-run" in self.help_text(engine, "start"):
            arguments.append("--open-run")
        return arguments

    def live_doctor_arguments(self):
        # From build step 8 doctor audits a copy by default (D1.7); the live
        # in-lock full audit stays reachable with --live.
        arguments = ["doctor"]
        if "--live" in self.help_text(ENGINE, "doctor"):
            arguments.append("--live")
        return arguments

    def start(self, actor, root=None, engine=None):
        result = self.cli(
            *self.start_arguments(engine), "--actor", actor,
            root=root, engine=engine,
        )
        match = RUN_RE.search(result.stdout)
        self.assertIsNotNone(match, result.stdout)
        return match.group(1)

    def propose(
        self, run_id, key, text, root=None, engine=None, new_subject=True,
        source="agent", trust="working", check=True,
    ):
        arguments = [
            "propose", "--run", run_id, "--tier", "2", "--key", key,
            "--text", text, "--source", source, "--trust", trust,
        ]
        if new_subject:
            arguments.append("--new-subject")
        result = self.cli(*arguments, root=root, engine=engine, check=check)
        if result.returncode != 0:
            return result
        match = PROPOSAL_RE.search(result.stdout)
        self.assertIsNotNone(match, result.stdout)
        return match.group(0)

    def apply_set(self, actor, key, text, root=None, engine=None):
        run_id = self.start(actor, root=root, engine=engine)
        proposal = self.propose(run_id, key, text, root=root, engine=engine)
        self.cli("sync", "--run", run_id, root=root, engine=engine)
        return run_id, proposal

    def worker(
        self, mode, *arguments, root=None, engine=None, check=True,
        timeout=180,
    ):
        command = [
            sys.executable,
            str(CRASH_WORKER),
            str(engine or ENGINE),
            mode,
            str(root or self.root),
            *map(str, arguments),
        ]
        result = subprocess.run(
            command, text=True, capture_output=True, timeout=timeout,
            encoding="utf-8",
        )
        if check and result.returncode != 0:
            self.fail(
                f"fault worker failed: {command!r}\n"
                f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}"
            )
        return result

    def probe(self, mode, *arguments, root=None, engine=None, timeout=180):
        script = self.workspace / "v520_probe.py"
        if not script.is_file():
            script.write_text(PROBE_SOURCE, encoding="utf-8")
        out = self.workspace / f"probe-{uuid.uuid4().hex}.json"
        command = [
            sys.executable,
            str(script),
            str(engine or ENGINE),
            mode,
            str(out),
            "--root",
            str(root or self.root),
            *map(str, arguments),
        ]
        result = subprocess.run(
            command, text=True, capture_output=True, timeout=timeout,
            encoding="utf-8",
        )
        self.assertTrue(out.is_file(), result.stdout + result.stderr)
        return result, json.loads(out.read_text("utf-8"))

    def install(self, target, *flags, engine=None, check=True):
        result = subprocess.run(
            [sys.executable, str(engine or ENGINE), "install", "--target",
             str(target), *flags],
            text=True, capture_output=True, timeout=240, encoding="utf-8",
        )
        if check and result.returncode != 0:
            self.fail(
                f"install failed for {target}\n"
                f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}"
            )
        return result

    def receipts(self, target):
        return [
            json.loads(path.read_text("utf-8"))
            for path in sorted(
                Path(target).joinpath(".bimri-update-backups").glob(
                    "*/install-manifest.json"
                )
            )
        ]

    def load_engine(self, label):
        name = f"bimri_v520_s1_{label}_{uuid.uuid4().hex[:8]}"
        spec = importlib.util.spec_from_file_location(name, ENGINE)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module

    # ---- the 5.1.2 comparison engine (main at 2c936ec) --------------------

    def prior_engine(self, release="5.1.2", commit=PRIOR_RELEASE_COMMIT):
        cls = type(self)
        if commit not in cls._prior_releases:
            archived = subprocess.run(
                ["git", "-c", f"safe.directory={REPOSITORY.as_posix()}",
                 "archive", "--format=tar", commit],
                cwd=str(REPOSITORY), capture_output=True, timeout=60,
            )
            self.assertEqual(
                archived.returncode, 0,
                f"the checkout must contain release commit "
                f"{commit}: {archived.stderr!r}",
            )
            destination = cls.class_workspace / ("release-" + release)
            destination.mkdir()
            package_root = destination.resolve()
            with tarfile.open(
                fileobj=io.BytesIO(archived.stdout), mode="r:"
            ) as archive:
                for member in archive.getmembers():
                    target = (destination / member.name).resolve()
                    self.assertTrue(
                        target == package_root or package_root in target.parents,
                        member.name,
                    )
                archive.extractall(str(destination))
            engine = destination / "bimri-engine.py"
            self.assertIn(
                f'ENGINE_VERSION = "{release}"'.encode("ascii"), engine.read_bytes()
            )
            cls._prior_releases[commit] = engine
        return cls._prior_releases[commit]

    def save_under_520(self, old, key):
        missing = self.cli("get", "--key", key, "--json", engine=old, check=False)
        self.assertEqual(missing.returncode, 1, missing.stdout + missing.stderr)
        return ["save", "--request", key, "--key", key, "--new-subject", "--tier",
                "2", "--text", "Original 5.2.0 statement.", "--observed",
                json.loads(missing.stdout)["token"], "--json"]

    def test_521_installs_over_520_and_preserves_completed_request_identity(self):
        old = self.prior_engine("5.2.0", "414a4d7ff820f6f4eb0f8a8c604157e42792a1c1")
        self.root.mkdir()
        self.install(self.root, engine=old)
        args = self.save_under_520(old, "compat.completed")
        original = json.loads(self.cli(*args, engine=old).stdout)
        self.install(self.root, "--quiescent", engine=old)
        before = protected_tree_snapshot(self.root)
        for _ in range(2):
            self.install(self.root, "--quiescent")
            self.assertEqual(before, protected_tree_snapshot(self.root))
        self.assertEqual(self.state()["bimri_version"], "5.2.0")
        self.cli("start", "--json")
        replay = json.loads(self.cli(*args).stdout)
        self.assertTrue(replay["replayed"])
        for field in ("token", "proposal", "run", "revision", "decision"):
            self.assertEqual(replay[field], original[field])
        self.assertEqual(self.state()["bimri_version"], "5.2.0")
        args = self.save_under_520(old, "compat.fence")
        saved = json.loads(self.cli(*args).stdout)
        self.assertEqual(saved["result"], "accepted")
        self.assertEqual(self.state()["bimri_version"], "5.2.1")
        fenced = protected_tree_snapshot(self.root)
        for cmd in (["start", "--open-run", "--actor", "previous"], args):
            refused = self.cli(*cmd, engine=old, check=False)
            self.assertNotEqual(refused.returncode, 0)
            self.assertIn("5.2.1", refused.stderr + refused.stdout)
            self.assertEqual(fenced, protected_tree_snapshot(self.root))

    def test_521_fences_520_before_a_newly_admitted_ticket_can_be_stranded(self):
        old = self.prior_engine("5.2.0", "414a4d7ff820f6f4eb0f8a8c604157e42792a1c1")
        self.root.mkdir()
        self.install(self.root, engine=old)
        args = self.save_under_520(old, "compat.admission")
        stopped = subprocess.run(
            [sys.executable, str(REPOSITORY / "tests" / "crash_worker.py"),
             str(ENGINE), "save_crash_after_ticket", str(self.root)] + args,
            capture_output=True, text=True, encoding="utf-8", timeout=60)
        self.assertEqual(stopped.returncode, 121, stopped.stdout + stopped.stderr)
        self.assertEqual(self.state()["bimri_version"], "5.2.1")
        before = protected_tree_snapshot(self.root)
        refused = self.cli("start", "--open-run", "--actor", "older", engine=old, check=False)
        self.assertEqual(refused.returncode, 2)
        self.assertIn("5.2.1", refused.stderr)
        self.assertEqual(before, protected_tree_snapshot(self.root))
        self.assertEqual(json.loads(self.cli(*args).stdout)["result"], "accepted")
        self.assertTrue(json.loads(self.cli(*args).stdout)["replayed"])

    def test_521_completes_interrupted_520_save_without_reallocating(self):
        old = self.prior_engine("5.2.0", "414a4d7ff820f6f4eb0f8a8c604157e42792a1c1")
        for mode, expected_exit in (("save_crash_after_epoch", 123),
                                    ("save_crash_after_proposal", 124)):
            with self.subTest(mode=mode):
                self.root = self.workspace / mode
                self.root.mkdir()
                self.install(self.root, engine=old)
                args = self.save_under_520(old, "compat.interrupted")
                stopped = subprocess.run(
                    [sys.executable, str(REPOSITORY / "tests" / "crash_worker.py"),
                     str(old), mode, str(self.root)] + args,
                    capture_output=True, text=True, encoding="utf-8", timeout=60)
                self.assertEqual(stopped.returncode, expected_exit, stopped.stdout + stopped.stderr)
                ticket = json.loads((self.root / ".bimri" / "saves" / "open" /
                                     "compat.interrupted.json").read_text("utf-8"))
                result = json.loads(self.cli(*args).stdout)
                self.assertEqual(result["result"], "accepted")
                self.assertEqual(result["proposal"], ticket["proposal_id"])
                self.assertEqual(result["run"], ticket["run_id"])
                self.assertTrue(json.loads(self.cli(*args).stdout)["replayed"])
                self.assertIn("PASSED", self.cli("doctor", "--read-only").stdout)
                self.assertEqual(1, len(list((self.root / ".bimri" / "proposals").glob("*.json"))))

    def test_521_republishes_exact_frozen_520_witness_before_current_policy_audit(self):
        old = self.prior_engine("5.2.0", "414a4d7ff820f6f4eb0f8a8c604157e42792a1c1")
        self.root.mkdir()
        self.install(self.root, engine=old)
        args = self.save_under_520(old, "compat.frozen")
        stopped = subprocess.run(
            [sys.executable, str(REPOSITORY / "tests" / "crash_worker.py"),
             str(old), "witness_crash_before_replace", str(self.root)] + args,
            capture_output=True, text=True, encoding="utf-8", timeout=60)
        self.assertEqual(stopped.returncode, 110, stopped.stdout + stopped.stderr)
        frozen = json.loads(self.marker_path().read_bytes())["scope"]["completion"]["witness"]
        self.assertEqual(frozen["engine_version"], "5.2.0")
        head = self.state()["head_revision"]
        drift = self.root / ".bimri" / "audit-drift"
        original_receipts = list(drift.glob("*.json"))
        result = json.loads(self.cli(*args).stdout)
        self.assertEqual(result["result"], "accepted")
        self.assertTrue(result["replayed"])
        self.assertFalse(self.marker_path().exists())
        self.assertEqual(self.state()["head_revision"], head)
        self.assertEqual(list(drift.glob("*.json")), original_receipts)
        self.assertIn("PASSED", self.cli("doctor", "--read-only").stdout)

    def test_two_upgrades_and_ten_new_saves_keep_old_staging_untouched(self):
        self.seed_prior_store()
        prior = self.prior_engine()
        run_id = self.start("legacy-pending", engine=prior)
        pid = self.propose(run_id, "upgrade.pending", "Pending older decision.", engine=prior)
        active_count = len(self.state()["active_runs"])
        audit = subprocess.run(
            [sys.executable, str(REPOSITORY / "tests" / "rehearse_update.py"),
             "--store", str(self.root), "--prior-engine", str(prior)],
            capture_output=True, text=True, encoding="utf-8", timeout=300)
        self.assertEqual(audit.returncode, 0, audit.stdout + audit.stderr)
        result = json.loads(audit.stdout)
        self.assertEqual(result["legacy_runs_preserved_through_ten_saves"], active_count)
        self.assertEqual(result["prior_engine_refusal"], STATE_REFUSAL)
        self.assertEqual([r["protected_write_attempts"] for r in result["receipts"]], [0, 0])
        self.assertFalse((self.root / ".bimri" / "decisions" / (pid + ".json")).exists())

    def seed_prior_store(self, root=None):
        """A store written only by 5.1.2: state 5.1.0, schema-1 witness."""
        root = Path(root or self.root)
        old = self.prior_engine()
        run_id, proposal = self.apply_set(
            "prior-engine", "upgrade.seed", "Preserved prior value.",
            root=root, engine=old,
        )
        self.assertEqual(self.state(root)["bimri_version"], "5.1.0")
        witness = self.witness(root)
        self.assertEqual(witness["witness_schema"], 1)
        self.assertEqual(witness["engine_version"], "5.1.2")
        return run_id, proposal

    # ---- store readers ---------------------------------------------------

    def state(self, root=None):
        return json.loads(
            Path(root or self.root).joinpath(".bimri", "state.json")
            .read_text("utf-8")
        )

    def state_bytes(self, root=None):
        return Path(root or self.root).joinpath(".bimri", "state.json").read_bytes()

    def witness_path(self, root=None):
        return Path(root or self.root) / ".bimri" / "audit-witness.json"

    def witness(self, root=None):
        path = self.witness_path(root)
        self.assertTrue(path.is_file(), f"missing audit witness: {path}")
        return json.loads(path.read_text("utf-8"))

    def marker_path(self, root=None):
        return Path(root or self.root) / ".bimri" / "audit-transition.json"

    def verdict_path(self, root=None):
        return Path(root or self.root) / ".bimri" / "audit-verdict.json"

    def manifests_dir(self, root=None):
        return Path(root or self.root) / ".bimri" / "audit-manifests"

    def facts_path(self, facts_hash, root=None):
        return self.manifests_dir(root) / f"facts-{facts_hash}.json"

    def facts_files(self, root=None):
        return sorted(
            path.name for path in self.manifests_dir(root).glob("facts-*.json")
            if HASH_RE.fullmatch(path.name[len("facts-"):-len(".json")])
        )

    def manifest_generation_files(self, root=None):
        return sorted(
            path.name for path in self.manifests_dir(root).glob("*.json")
            if HASH_RE.fullmatch(path.stem)
        )

    def decision(self, proposal_id, root=None):
        return json.loads(
            Path(root or self.root).joinpath(
                ".bimri", "decisions", f"{proposal_id}.json"
            ).read_text("utf-8")
        )

    def proposal(self, proposal_id, root=None):
        return json.loads(
            Path(root or self.root).joinpath(
                ".bimri", "proposals", f"{proposal_id}.json"
            ).read_text("utf-8")
        )

    def drift_reasons(self, root=None):
        directory = Path(root or self.root) / ".bimri" / "audit-drift"
        reasons = []
        if not directory.is_dir():
            return reasons
        for path in sorted(directory.glob("D*.json")):
            record = json.loads(path.read_text("utf-8"))
            reasons.extend(str(reason) for reason in record.get("reasons", []))
        return reasons

    def drift_receipts(self, root=None):
        directory = Path(root or self.root) / ".bimri" / "audit-drift"
        if not directory.is_dir():
            return []
        return sorted(directory.glob("D*.json"))

    def witnessed_file_digests(self, root=None):
        root = Path(root or self.root)
        digests = {}
        for name in WITNESSED_ROOTS:
            directory = root / ".bimri" / name
            if not directory.is_dir():
                continue
            for path in sorted(directory.iterdir()):
                if path.is_file() and not path.is_symlink():
                    digest = hashlib.sha256(path.read_bytes()).hexdigest()
                    digests.setdefault(digest, []).append(
                        path.relative_to(root).as_posix()
                    )
        return digests

    def outside_memory_files(self, root=None):
        """Install targets: every file under root outside .bimri and backups."""
        root = Path(root or self.root)
        files = {}
        for path in sorted(root.rglob("*")):
            relative = path.relative_to(root).as_posix()
            if relative.split("/", 1)[0] in {".bimri", ".bimri-update-backups"}:
                continue
            if path.is_file():
                files[relative] = hashlib.sha256(path.read_bytes()).hexdigest()
        return files

    # ---- record assertions -------------------------------------------------

    def assert_current_witness(self, witness, origin=None):
        """D2.3 / DESIGN 2.5: the nineteen-field schema-2 verdict."""
        self.assertEqual(set(witness), WITNESS_FIELDS_V2)
        self.assertEqual(witness["witness_schema"], 2)
        self.assertEqual(witness["engine_version"], "5.2.1")
        self.assertEqual(witness["memory_format_version"], "5.1.0")
        self.assertEqual(witness["policy_version"], "5.2.1-authority-1")
        self.assertEqual(witness["witness_hash"], record_seal(witness, "witness_hash"))
        self.assertRegex(witness["facts_hash"], HASH_RE)
        self.assertIn(witness["audit_origin"], {"full", "scoped", "lifecycle"})
        self.assertIsInstance(witness["scoped_publications"], int)
        self.assertGreaterEqual(witness["scoped_publications"], 0)
        self.assertLessEqual(witness["audited_at"], witness["created_at"])
        if origin is not None:
            self.assertEqual(witness["audit_origin"], origin)
        if witness["audit_origin"] == "full":
            self.assertEqual(witness["scoped_publications"], 0)
            self.assertEqual(witness["audited_at"], witness["created_at"])

    def assert_facts_generation(self, witness, root=None):
        """D2.2 / DESIGN 2.4: exact field set, seal, run_authority_hash triple."""
        path = self.facts_path(witness["facts_hash"], root)
        self.assertTrue(path.is_file(), f"missing facts generation: {path}")
        record = json.loads(path.read_text("utf-8"))
        self.assertEqual(set(record), FACTS_FIELDS)
        self.assertEqual(record["facts_schema"], 1)
        self.assertEqual(record["facts_hash"], witness["facts_hash"])
        self.assertEqual(record["facts_hash"], record_seal(record, "facts_hash"))
        self.assertEqual(record["manifest_hash"], witness["manifest_hash"])
        self.assertEqual(
            record["run_authority_hash"], audit_digest(record["run_facts"])
        )
        self.assertEqual(
            record["run_authority_hash"], witness["run_authority_hash"]
        )
        ids = [row["proposal_id"] for row in record["run_facts"]]
        self.assertEqual(ids, sorted(ids))
        for row in record["run_facts"]:
            self.assertEqual(set(row), RUN_FACT_FIELDS)
        self.assertEqual(set(record["unsettled"]), UNSETTLED_FIELDS)
        for values in record["unsettled"].values():
            self.assertEqual(values, sorted(set(values)))
        self.assertEqual(record["held"], sorted(set(record["held"])))
        self.assertIsInstance(record["settled"], dict)
        self.assertIsInstance(record["writers_by_key"], dict)
        self.assertIsInstance(record["open_conflicts"], list)
        self.assertIsInstance(record["applying_resolutions"], list)
        self.assertEqual(set(record["watermarks"]), WATERMARK_FIELDS)
        dt.datetime.strptime(record["created_at"], "%Y-%m-%dT%H:%M:%SZ")
        return record

    def assert_verdict(self, status, root=None):
        """D2.8 / DESIGN 2.9: twelve fields, sealed, live source."""
        path = self.verdict_path(root)
        self.assertTrue(path.is_file(), f"missing audit verdict: {path}")
        verdict = json.loads(path.read_text("utf-8"))
        self.assertEqual(set(verdict), VERDICT_FIELDS)
        self.assertEqual(verdict["verdict_schema"], 1)
        self.assertEqual(verdict["source"], "live")
        self.assertIsNone(verdict["copy_capture_ms"])
        self.assertEqual(verdict["status"], status)
        self.assertEqual(verdict["verdict_hash"], record_seal(verdict, "verdict_hash"))
        self.assertIsInstance(verdict["issues"], list)
        self.assertLessEqual(len(verdict["issues"]), 20)
        self.assertGreaterEqual(verdict["issue_count"], len(verdict["issues"]))
        for issue in verdict["issues"]:
            self.assertIsInstance(issue, str)
        state = self.state(root)
        self.assertEqual(verdict["head_revision"], state["head_revision"])
        self.assertEqual(verdict["head_hash"], state["head_hash"])
        epoch = state.get("_audit_epoch", 0)
        if status == "passed":
            self.assertEqual(verdict["audit_epoch"], epoch)
        else:
            # A failing audit may condemn the checkpoint (epoch + 1,
            # condemn_audit_checkpoint) after or before the verdict is
            # written; the audited tree's epoch is one of the two.
            self.assertIn(verdict["audit_epoch"], {epoch, epoch - 1})
        if verdict["manifest_hash"] is not None:
            self.assertRegex(verdict["manifest_hash"], HASH_RE)
        dt.datetime.strptime(verdict["created_at"], "%Y-%m-%dT%H:%M:%SZ")
        if status == "passed":
            self.assertEqual(verdict["issue_count"], 0)
            self.assertEqual(verdict["issues"], [])
        else:
            self.assertGreaterEqual(verdict["issue_count"], 1)
            self.assertTrue(verdict["issues"])
        return verdict

    def assert_scope_grammar(self, scope):
        """D2.4 / DESIGN 2.6: declared_paths and dependency_hashes shapes."""
        declared = scope["declared_paths"]
        self.assertIsInstance(declared, list)
        self.assertEqual(declared, sorted(set(declared)))
        dependencies = scope["dependency_hashes"]
        self.assertIsInstance(dependencies, dict)
        for path in list(declared) + list(dependencies):
            pure = PurePosixPath(path)
            self.assertFalse(pure.is_absolute(), path)
            self.assertNotIn("..", pure.parts, path)
            self.assertEqual(len(pure.parts), 3, path)
            self.assertEqual(pure.parts[0], ".bimri", path)
            self.assertIn(pure.parts[1], WITNESSED_ROOTS, path)
        for value in dependencies.values():
            self.assertTrue(value == "absent" or HASH_RE.fullmatch(value), value)

    # =======================================================================
    # Constants and validators (in-process; section 09 step 1 "(a)-(d)")
    # =======================================================================

    def test_version_constants_move_and_artifact_format_stays(self):
        # D0 JSON block and D0.1: engine, policy, state and schema numbers move;
        # MEMORY_FORMAT_VERSION and COMPATIBLE_ARTIFACT_VERSIONS do not, so
        # every artifact switch keeps 5.1.0 (risk: "the version cascade").
        engine = self.load_engine("constants")
        self.assertEqual(engine.ENGINE_VERSION, "5.2.1")
        self.assertEqual(engine.V5_1_2_ENGINE_VERSION, "5.1.2")
        self.assertEqual(engine.V5_1_1_ENGINE_VERSION, "5.1.1")
        self.assertEqual(engine.V5_1_0_ENGINE_VERSION, "5.1.0")
        self.assertEqual(engine.STATE_FORMAT_VERSION, "5.2.1")
        self.assertEqual(set(engine.READABLE_STATE_VERSIONS), {"5.1.0", "5.2.0", "5.2.1"})
        self.assertEqual(engine.AUTHORITY_POLICY_VERSION, "5.2.1-authority-1")
        self.assertEqual(engine.AUDIT_WITNESS_SCHEMA, 2)
        self.assertEqual(engine.AUDIT_FACTS_SCHEMA, 1)
        self.assertEqual(engine.SAVE_TICKET_SCHEMA, 1)
        self.assertEqual(engine.SETTLEMENT_SCHEMA, 1)
        self.assertEqual(engine.AUDIT_VERDICT_SCHEMA, 1)
        self.assertEqual(engine.AUDIT_TRANSITION_SCHEMA, 1)
        self.assertEqual(engine.MEMORY_FORMAT_VERSION, "5.1.0")
        self.assertEqual(engine.HOT_FORMAT_VERSION, "5.0.2")
        self.assertEqual(
            set(engine.COMPATIBLE_ARTIFACT_VERSIONS),
            {"5.0", "5.0.1", "5.0.2", "5.1.0"},
        )
        # D0.3 / DESIGN 2.1 item 3 (L9): a new store is born at 5.2.0.
        self.assertEqual(engine.fresh_state()["bimri_version"], "5.2.1")
        self.assertEqual(engine.DEFAULT_STATE["bimri_version"], "5.2.1")
        # D0.3: validate_state reads both versions by default and nothing else.
        for version in ("5.1.0", "5.2.0", "5.2.1"):
            with self.subTest(version=version):
                state = engine.fresh_state()
                state["bimri_version"] = version
                engine.validate_state(state)
        state = engine.fresh_state()
        state["bimri_version"] = "5.3.0"
        with self.assertRaises(engine.BimriError) as raised:
            engine.validate_state(state)
        self.assertIn("unsupported BIMRI state version: 5.3.0", str(raised.exception))

    def test_code_update_receipt_identity_accepts_each_supported_release_and_state_pair(self):
        # Each receipt pins its producer and the readable state it updated.
        engine = self.load_engine("receipts")
        accepted = (
            ("5.0.3", "5.0.2"),
            ("5.1.0", "5.1.0"),
            ("5.1.1", "5.1.0"),
            ("5.1.2", "5.1.0"),
            ("5.2.0", "5.1.0"),
            ("5.2.0", "5.2.0"),
            ("5.2.1", "5.1.0"), ("5.2.1", "5.2.0"), ("5.2.1", "5.2.1"),
        )
        for release, memory in accepted:
            with self.subTest(release=release, memory=memory):
                self.assertEqual(
                    engine.code_update_receipt_identity(
                        {"engine_release": release, "memory_format": memory}
                    ),
                    (release, memory),
                )
        for release, memory in (
            ("5.2.2", "5.1.0"), ("5.2.0", "5.0.2"), ("5.1.2", "5.2.0"),
            ("9.9.9", "5.1.0"),
        ):
            with self.subTest(release=release, memory=memory):
                with self.assertRaises(engine.BimriError):
                    engine.code_update_receipt_identity(
                        {"engine_release": release, "memory_format": memory}
                    )

    def test_state_whitelist_admits_only_the_forward_flip(self):
        # D0.2 / DESIGN 2.1 item 2: authority_transition_state_issues pops
        # bimri_version only for the delta 5.1.0 -> 5.2.0, for every authority
        # operation; any other version delta stays an out-of-scope change
        # (DESIGN 2.13 item 1, last clause).
        engine = self.load_engine("whitelist")
        before = engine.fresh_state()
        before["bimri_version"] = "5.1.0"
        flipped = dict(before, bimri_version="5.2.1")
        for operation in (
            "propose", "sync", "close-authority-run", "resolve",
            "recover-interrupted-authority", "authority-recovery",
            "recover-manual-hot-edit",
        ):
            with self.subTest(operation=operation):
                marker = {
                    "operation": operation,
                    "run_id": None,
                    "pre_write_state": engine.audit_witness_write_state_payload(
                        before
                    ),
                }
                self.assertEqual(
                    engine.authority_transition_state_issues(marker, flipped), []
                )
        reverse = {
            "operation": "sync",
            "run_id": None,
            "pre_write_state": engine.audit_witness_write_state_payload(flipped),
        }
        self.assertTrue(engine.authority_transition_state_issues(reverse, before))
        marker = {
            "operation": "propose",
            "run_id": None,
            "pre_write_state": engine.audit_witness_write_state_payload(before),
        }
        self.assertTrue(
            engine.authority_transition_state_issues(
                marker, dict(before, bimri_version="5.3.0")
            )
        )

    def test_marker_scope_keys_are_validated_when_present(self):
        # D2.4 / DESIGN 2.6: validate_frozen_authority_transition_scope keeps
        # the 5.1.2 shape valid (no new keys) and validates request_id,
        # settles, declared_paths and dependency_hashes when present.
        engine = self.load_engine("scope")
        base = {"proposal_ids": [], "proposal_hashes": {}, "completion": None}
        engine.validate_frozen_authority_transition_scope(dict(base))
        good = dict(
            base,
            request_id="sess1-turn7-a1b2",
            settles=["R000001-Q001", "R000002-Q001"],
            declared_paths=[
                ".bimri/archive/2026-10.md",
                ".bimri/decisions/R000003-Q001.json",
                ".bimri/revisions/V000004.md",
            ],
            dependency_hashes={
                ".bimri/proposals/R000003-Q001.json": "a" * 64,
                ".bimri/revisions/V000005.md": "absent",
            },
        )
        engine.validate_frozen_authority_transition_scope(good)
        bad_cases = {
            "request id grammar": dict(good, request_id="bad id!"),
            "request id leading dash": dict(good, request_id="-leading"),
            "settles unsorted": dict(
                good, settles=["R000002-Q001", "R000001-Q001"]
            ),
            "settles duplicate": dict(
                good, settles=["R000001-Q001", "R000001-Q001"]
            ),
            "settles not proposal ids": dict(good, settles=["C000001"]),
            "declared unsorted": dict(
                good,
                declared_paths=[
                    ".bimri/revisions/V000004.md",
                    ".bimri/decisions/R000003-Q001.json",
                ],
            ),
            "declared dot-dot": dict(
                good, declared_paths=[".bimri/revisions/../state.json"]
            ),
            "declared outside witnessed roots": dict(
                good, declared_paths=[".bimri/log/R000003.md"]
            ),
            "declared absolute": dict(
                good, declared_paths=["/etc/.bimri/revisions/V000004.md"]
            ),
            "dependency short hash": dict(
                good,
                dependency_hashes={".bimri/revisions/V000004.md": "abc"},
            ),
            "dependency outside roots": dict(
                good, dependency_hashes={".bimri/state.json": "a" * 64}
            ),
        }
        for label, scope in bad_cases.items():
            with self.subTest(label):
                with self.assertRaises(engine.BimriError):
                    engine.validate_frozen_authority_transition_scope(scope)

    def test_witness_schema_two_is_the_only_current_verdict(self):
        # D0.5 / DESIGN 2.1 item 6 and 2.5: schema 2 with the current triple is
        # the only current verdict; schema 1 is prior-policy evidence only for
        # ("5.1.1","5.1.1-authority-1") and ("5.1.2","5.1.2-authority-1").
        self.apply_set("witness-shape", "witness.shape", "Shape probe.")
        current = self.witness()
        self.assert_current_witness(current)
        engine = self.load_engine("witness")
        self.assertIsNotNone(engine.validate_sealed_audit_witness_record(dict(current)))

        def schema_one(release, policy):
            record = {k: v for k, v in current.items() if k in WITNESS_FIELDS_V1}
            record.update(
                witness_schema=1, engine_version=release, policy_version=policy
            )
            record.pop("witness_hash")
            record["witness_hash"] = record_seal(record, "witness_hash")
            return record

        def resealed(record):
            record = dict(record)
            record.pop("witness_hash")
            record["witness_hash"] = record_seal(record, "witness_hash")
            return record

        for release, policy in (
            ("5.1.2", "5.1.2-authority-1"), ("5.1.1", "5.1.1-authority-1"),
        ):
            with self.subTest(prior=release):
                record = schema_one(release, policy)
                self.assertIsNone(engine.validate_sealed_audit_witness_record(record))
                self.assertIsNotNone(
                    engine.validate_sealed_audit_witness_record(
                        record, allow_prior_policy=True
                    )
                )
        prior = resealed(dict(
            current, engine_version="5.2.0", policy_version="5.2.0-authority-1"
        ))
        self.assertIsNone(engine.validate_sealed_audit_witness_record(prior))
        self.assertIsNotNone(engine.validate_sealed_audit_witness_record(
            prior, allow_prior_policy=True
        ))
        refused = {
            "schema 1 with prior schema 2 policy": schema_one("5.2.0", "5.2.0-authority-1"),
            "schema 1 mixed pair": schema_one("5.1.2", "5.1.1-authority-1"),
            "schema 1 unknown release": schema_one("9.9.9", "5.1.2-authority-1"),
            "schema 1 current pair": schema_one("5.2.1", "5.2.1-authority-1"),
            "schema 2 prior pair": resealed(dict(
                current, engine_version="5.1.2", policy_version="5.1.2-authority-1"
            )),
            "schema 2 artifact format bump": resealed(
                dict(current, memory_format_version="5.2.0")
            ),
            "schema 2 missing facts_hash": resealed(
                {k: v for k, v in current.items() if k != "facts_hash"}
            ),
            "schema 2 unknown origin": resealed(dict(current, audit_origin="copy")),
            "schema 2 full origin with scoped count": resealed(
                dict(current, audit_origin="full", scoped_publications=1)
            ),
            "schema 2 negative scoped count": resealed(
                dict(current, audit_origin="scoped", scoped_publications=-1)
            ),
            "schema 2 broken seal": dict(current, witness_hash="0" * 64),
        }
        for label, record in refused.items():
            with self.subTest(label):
                self.assertIsNone(
                    engine.validate_sealed_audit_witness_record(
                        record, allow_prior_policy=True
                    )
                )

    # =======================================================================
    # Facts generation (D2.2) and verdict (D2.8)
    # =======================================================================

    def test_facts_generation_round_trips_full_audit(self):
        # D2.2 / DESIGN 2.4 and 4.6: a full audit publishes a sealed facts
        # generation whose run_authority_hash equals the witness's and whose
        # run_facts are exactly audit_witness_run_facts; pruning keeps every
        # referenced facts file, removes an orphan, and leaves non-matching
        # names alone (DESIGN 2.13 item 6).
        run_id, proposal = self.apply_set(
            "facts-writer", "facts.subject", "Facts round trip value."
        )
        self.cli(*self.live_doctor_arguments())
        witness = self.witness()
        self.assert_current_witness(witness, origin="full")
        facts = self.assert_facts_generation(witness)

        engine = self.load_engine("facts")
        state = engine.fresh_state()
        state.update(self.state())
        self.assertEqual(
            facts["run_facts"],
            engine.audit_witness_run_facts(engine.Paths(self.root), state),
        )
        self.assertEqual(
            facts["writers_by_key"].get("facts.subject"),
            {
                "proposal_id": proposal,
                "revision": self.decision(proposal)["revision"],
                "run_id": run_id,
            },
        )
        self.assertEqual(
            facts["unsettled"],
            {"applying_decisions": [], "applying_resolutions": [],
             "failed_resolutions": []},
        )
        self.assertEqual(facts["held"], [])
        self.assertEqual(facts["settled"], {})
        self.assertEqual(facts["open_conflicts"], [])
        self.assertEqual(facts["applying_resolutions"], [])
        self.assertEqual(facts["watermarks"]["proposal"].get(run_id), 1)
        for name in ("pattern", "conflict", "run"):
            self.assertIsInstance(facts["watermarks"][name], int, name)
        self.assertGreaterEqual(facts["watermarks"]["run"], int(run_id[1:]))
        self.assertEqual(
            facts["watermarks"]["conflict"], self.state()["conflict_count"]
        )

        # Prune: plant an orphan facts generation and two names prune must
        # never touch, then publish again through an ordinary write.
        directory = self.manifests_dir()
        orphan = directory / ("facts-" + "0" * 64 + ".json")
        orphan.write_text("{}\n", encoding="utf-8")
        untouched = {
            directory / "facts-notahash.json": b"not a generation\n",
            directory / "notes.txt": b"operator notes\n",
        }
        for path, content in untouched.items():
            path.write_bytes(content)
        old_facts = self.facts_path(witness["facts_hash"])

        self.apply_set("facts-second", "facts.second", "Second publication.")
        republished = self.witness()
        self.assert_current_witness(republished)
        self.assert_facts_generation(republished)
        self.assertFalse(orphan.exists(), "orphan facts generation survived")
        for path, content in untouched.items():
            self.assertEqual(path.read_bytes(), content, path.name)
        self.assertEqual(
            self.facts_files(), [f"facts-{republished['facts_hash']}.json"]
        )
        self.assertEqual(
            self.manifest_generation_files(),
            [f"{republished['manifest_hash']}.json"],
        )
        if republished["facts_hash"] != witness["facts_hash"]:
            self.assertFalse(old_facts.exists(), "unreferenced facts survived")

    def test_missing_facts_evidence_refuses_rebaseline_like_missing_manifest(self):
        # D2.2 load_audit_facts_evidence / DESIGN 2.4 last bullet and 2.12:
        # missing facts behind a sealed witness is damaged evidence, the same
        # PRIOR_EVIDENCE_INVALID_PREFIX refusal as missing manifest evidence
        # (test_v511_performance.py:3106): the write takes the full audit,
        # the drift receipt names the refusal, nothing is rebaselined blind.
        self.apply_set("evidence-seed", "evidence.seed", "Stable value.")
        self.cli(*self.live_doctor_arguments())
        writer = self.start("evidence-writer")
        witness = self.witness()
        self.assert_current_witness(witness)
        witness_before = self.witness_path().read_bytes()
        state_before = self.state()
        self.facts_path(witness["facts_hash"]).unlink()
        facts_before = self.facts_files()

        refused = self.propose(
            writer, "evidence.next", "Must not land.", check=False
        )
        self.assertEqual(refused.returncode, 2, refused.stdout + refused.stderr)
        self.assertIn(PRIOR_EVIDENCE_INVALID_PREFIX, refused.stdout + refused.stderr)
        self.assertEqual(self.witness_path().read_bytes(), witness_before)
        self.assertEqual(self.facts_files(), facts_before)
        self.assertEqual(
            list(self.root.joinpath(".bimri", "proposals").glob(f"{writer}-Q*.json")),
            [],
        )
        self.assertEqual(self.state()["head_revision"], state_before["head_revision"])
        self.assertEqual(self.state()["head_hash"], state_before["head_hash"])
        reasons = " | ".join(self.drift_reasons())
        self.assertIn(PRIOR_EVIDENCE_INVALID_PREFIX, reasons)
        self.assertIn("facts", reasons.lower())

        # Same as the manifest case: the live full audit refuses too and
        # keeps the prior checkpoint bytes.
        audited = self.cli(*self.live_doctor_arguments(), check=False)
        self.assertEqual(audited.returncode, 1, audited.stdout + audited.stderr)
        self.assertIn(PRIOR_EVIDENCE_INVALID_PREFIX, audited.stdout + audited.stderr)
        self.assertEqual(self.witness_path().read_bytes(), witness_before)

        # The documented exit: remove the derived witness, the full audit
        # rebuilds trust and publishes a schema-2 witness with facts.
        self.witness_path().unlink()
        repaired = self.cli(*self.live_doctor_arguments())
        self.assertIn("PASSED", repaired.stdout)
        rebuilt = self.witness()
        self.assert_current_witness(rebuilt, origin="full")
        self.assert_facts_generation(rebuilt)
        self.propose(writer, "evidence.next", "Lands after the rebuild.")

    def test_verdict_written_on_pass_and_fail_and_never_by_updater(self):
        # D2.8 / DESIGN 2.9: every live full audit writes a sealed verdict,
        # pass and fail; read-only audits and the code-only updater write
        # none (gate 9: protected_mutation_monitor clean through
        # install --quiescent, tests/test_bimri_engine.py:8486 pattern).
        _run, proposal = self.apply_set("verdict", "verdict.key", "Verdict probe.")
        self.cli(*self.live_doctor_arguments())
        passed = self.assert_verdict("passed")
        self.assertEqual(passed["manifest_hash"], self.witness()["manifest_hash"])

        decision = self.root / ".bimri" / "decisions" / f"{proposal}.json"
        decision.write_bytes(b"{damaged decision bytes")
        failed_run = self.cli(*self.live_doctor_arguments(), check=False)
        self.assertEqual(failed_run.returncode, 1, failed_run.stdout + failed_run.stderr)
        failed = self.assert_verdict("failed")
        self.assertNotEqual(failed["verdict_hash"], passed["verdict_hash"])

        # A second, healthy store for the read-only and updater halves.
        other = self.workspace / "updater-store"
        self.apply_set("verdict-updater", "verdict.other", "Updater probe.", root=other)
        self.cli(*self.live_doctor_arguments(), root=other)
        self.assert_verdict("passed", root=other)
        self.verdict_path(other).unlink()

        read_only = self.cli("doctor", "--read-only", root=other)
        self.assertIn("PASSED", read_only.stdout)
        self.assertFalse(self.verdict_path(other).exists())

        protected = protected_tree_snapshot(other)
        installed = self.worker(
            "protected_mutation_monitor",
            "install", "--target", other, "--quiescent",
            root=REPOSITORY,
        )
        self.assertIn("Memory preservation: PASSED", installed.stdout)
        self.assertNotIn("protected mutation monitor", installed.stderr)
        self.assertEqual(protected_tree_snapshot(other), protected)
        self.assertFalse(self.verdict_path(other).exists())

    # =======================================================================
    # Scoped publication primitives (D4.4; in-process per step 1)
    # =======================================================================

    def seed_scoped_store(self):
        """Seeded store with a valid checkpoint; runs before the in-process
        lock is taken (a CLI call under that lock would be BIMRI is busy)."""
        run_id, _proposal = self.apply_set(
            "scoped-seed", "scoped.seed", "Seed for scoped publication."
        )
        return run_id

    def open_scoped_transition(self, engine, declared, run_id):
        """Under the caller's engine_lock: W0/M0/F0 loaded, an authority
        marker carrying declared_paths, an empty observed set bound."""
        paths = engine.Paths(self.root)
        state = engine.load_or_initialize(paths)
        w0 = engine.load_valid_audit_witness(paths, state)
        self.assertIsNotNone(w0, "seed store must carry a valid checkpoint")
        m0 = engine.load_audit_manifest_evidence(paths, w0)
        f0 = engine.load_audit_facts_evidence(paths, w0)
        witness_bytes = self.witness_path().read_bytes()
        engine.begin_authority_checkpoint_transition(
            paths, state, "propose", run_id=run_id,
            scope={
                "proposal_ids": [],
                "proposal_hashes": {},
                "declared_paths": [declared],
                "dependency_hashes": {},
            },
        )
        marker = engine.load_audit_transition(paths)
        self.assertIsNotNone(marker)
        self.assertIn(declared, marker["scope"].get("declared_paths", []))
        observed = set()
        paths.scoped_write_set = observed
        if hasattr(engine, "_SCOPED_WRITE_SET"):
            # DESIGN 4.4 local decision L4: the primitives record through a
            # module global bound to paths.scoped_write_set.
            engine._SCOPED_WRITE_SET = observed
        return paths, state, w0, m0, f0, witness_bytes, observed

    def release_scoped_recording(self, engine, paths):
        paths.scoped_write_set = None
        if hasattr(engine, "_SCOPED_WRITE_SET"):
            engine._SCOPED_WRITE_SET = None

    def test_scoped_publication_rejects_undeclared_observed_write(self):
        # D4.4 / DESIGN 4.5 P1: the observed write set (recorded by
        # note_protected_write inside the write primitives) must lie within
        # marker.scope.declared_paths + {state.json, bimri.md}; otherwise
        # BimriError, nothing published, the marker stays, W0 bytes unchanged.
        engine = self.load_engine("scoped_reject")
        run_id = self.seed_scoped_store()
        declared = ".bimri/recovery/declared-probe.bin"
        undeclared = ".bimri/recovery/undeclared-probe.bin"
        with engine.engine_lock(engine.Paths(self.root)):
            paths, state, _w0, m0, f0, witness_bytes, observed = (
                self.open_scoped_transition(engine, declared, run_id)
            )
            generations_before = sorted(
                path.name for path in self.manifests_dir().iterdir()
            )
            try:
                engine.exclusive_write_bytes(
                    self.root.joinpath(*undeclared.split("/")),
                    b"written outside the declaration\n",
                )
                self.assertIn(undeclared, observed)
                with self.assertRaises(engine.BimriError) as raised:
                    engine.publish_scoped_checkpoint(paths, state, m0, f0, {})
            finally:
                self.release_scoped_recording(engine, paths)
        message = str(raised.exception)
        self.assertIn("undeclared", message)
        self.assertIn(undeclared, message)
        self.assertTrue(self.marker_path().is_file(), "marker must stay")
        self.assertEqual(self.witness_path().read_bytes(), witness_bytes)
        self.assertEqual(
            sorted(path.name for path in self.manifests_dir().iterdir()),
            generations_before,
        )

    def test_scoped_publication_publishes_declared_write_from_prior_inventory(self):
        # D4.4 / DESIGN 4.5 P2-P6 and the 2.5 producer table (control for the
        # rejection test): M1 = M0 with only the observed declared row
        # rehashed, F1 = F0 + delta, W1 schema 2 with audit_origin "scoped",
        # audited_at carried, scoped_publications + 1; marker cleared; the
        # new checkpoint serves a warm read with no full audit.
        engine = self.load_engine("scoped_publish")
        run_id = self.seed_scoped_store()
        declared = ".bimri/recovery/declared-probe.bin"
        content = b"declared scoped write\n"
        with engine.engine_lock(engine.Paths(self.root)):
            paths, state, w0, m0, f0, _bytes, observed = (
                self.open_scoped_transition(engine, declared, run_id)
            )
            try:
                engine.exclusive_write_bytes(
                    self.root.joinpath(*declared.split("/")), content
                )
                self.assertIn(declared, observed)
                published = engine.publish_scoped_checkpoint(
                    paths, state, m0, f0, {}
                )
            finally:
                self.release_scoped_recording(engine, paths)
        self.assertIsNot(published, False)
        self.assertFalse(self.marker_path().exists(), "B15 clears the marker")
        w1 = self.witness()
        self.assert_current_witness(w1, origin="scoped")
        self.assertEqual(w1["audited_at"], w0["audited_at"])
        self.assertEqual(w1["scoped_publications"], w0["scoped_publications"] + 1)
        self.assertEqual(w1["run_authority_hash"], w0["run_authority_hash"])
        expected_rows = sorted(
            [row for row in m0 if row["path"] != declared]
            + [{"path": declared,
                "sha256": hashlib.sha256(content).hexdigest()}],
            key=lambda row: row["path"],
        )
        generation = json.loads(
            (self.manifests_dir() / f"{w1['manifest_hash']}.json").read_text("utf-8")
        )
        self.assertEqual(generation["manifest"], expected_rows)
        self.assertEqual(w1["manifest_hash"], audit_digest(expected_rows))
        f1 = self.assert_facts_generation(w1)
        self.assertEqual(f1["run_facts"], f0["run_facts"])
        warm = self.worker(
            "valid_witness_forbid_full_audit", "get", "--key", "scoped.seed"
        )
        self.assertIn("Seed for scoped publication.", warm.stdout)

    # =======================================================================
    # Scoped recovery (D3.4.3)
    # =======================================================================

    def seed_decoys(self, root=None, engine=None):
        """History no sync of another run depends on."""
        decoy_run = self.start("decoy-a", root=root, engine=engine)
        first = self.propose(decoy_run, "decoy.alpha", "Decoy alpha.",
                             root=root, engine=engine)
        second = self.propose(decoy_run, "decoy.beta", "Decoy beta.",
                              root=root, engine=engine)
        self.cli("sync", "--run", decoy_run, root=root, engine=engine)
        _run, third = self.apply_set(
            "decoy-b", "decoy.gamma", "Decoy gamma.", root=root, engine=engine
        )
        return [first, second, third]

    def test_scoped_recovery_after_legacy_sync_crash_rehashes_only_declared_paths(self):
        # D3.4.3 / DESIGN 4.7: a 5.2.0 authority marker carries declared_paths
        # and dependency_hashes (D2.4); recovery builds the manifest as M0
        # with only declared + dependency paths + revisions V(head0+1..head1)
        # (+ the archive month) rehashed. A counting sha256 wrapper proves no
        # other witnessed file was hashed. A marker written by 5.1.2 (no
        # declared_paths) still takes the full replay.
        decoys = self.seed_decoys()
        target_run = self.start("scoped-target")
        target = self.propose(target_run, "scoped.target", "Recovered by scope.")
        crashed = self.worker(
            "witness_crash_before_replace", "sync", "--run", target_run,
            check=False,
        )
        self.assertEqual(crashed.returncode, 110, crashed.stdout + crashed.stderr)
        marker = json.loads(self.marker_path().read_text("utf-8"))
        self.assertEqual(marker["kind"], "authority")
        scope = marker["scope"]
        self.assertIn("declared_paths", scope)
        self.assertIn("dependency_hashes", scope)
        self.assert_scope_grammar(scope)
        head0 = marker["prior_witness"]["head_revision"]
        head1 = self.state()["head_revision"]
        self.assertGreater(head1, head0)
        self.assertIn(f".bimri/decisions/{target}.json", scope["declared_paths"])
        self.assertIn(revision_path(head1), scope["declared_paths"])
        self.assertEqual(scope["proposal_ids"], [target])
        # The crash is after the completion seal: recovery republishes the
        # writer's own sealed W1, never a verdict stamped at recovery time.
        completion = scope.get("completion")
        self.assertIsNotNone(completion, "the crash follows the completion seal")
        sealed = completion["witness"]
        digests = self.witnessed_file_digests()
        receipts_before = self.drift_receipts()

        result, recorded = self.probe("hashes", "get", "--key", "scoped.target")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("Recovered by scope.", result.stdout)
        self.assertFalse(self.marker_path().exists(), "recovery must clear M")
        recovered = self.witness()
        self.assert_current_witness(recovered)
        # Scoped recovery is not a full audit: audit_origin, audited_at and
        # scoped_publications are the sealed completion's, byte for byte.
        self.assertEqual(recovered, sealed)
        for field in ("audit_origin", "audited_at", "scoped_publications"):
            self.assertEqual(recovered[field], sealed[field], field)
        self.assertEqual(self.drift_receipts(), receipts_before)

        hashed = set()
        for digest in set(recorded):
            hashed.update(digests.get(digest, []))
        months = {
            dt.date.today().strftime("%Y-%m"),
            dt.datetime.now(dt.timezone.utc).strftime("%Y-%m"),
        }
        allowed = (
            set(scope["declared_paths"])
            | set(scope["dependency_hashes"])
            | {revision_path(n) for n in range(head0 + 1, head1 + 1)}
            | {f".bimri/archive/{month}.md" for month in months}
            # The frozen proposal hashes are re-read by the completion seal
            # (seal_authority_transition_completion); they are in scope.
            | {f".bimri/proposals/{q}.json" for q in scope["proposal_ids"]}
        )
        self.assertEqual(sorted(hashed - allowed), [])
        self.assertIn(revision_path(head1), hashed)
        unrelated = {f".bimri/proposals/{q}.json" for q in decoys}
        unrelated |= {f".bimri/decisions/{q}.json" for q in decoys}
        unrelated |= {revision_path(n) for n in range(0, head0)}
        self.assertEqual(sorted(hashed & unrelated), [])

        # Control: a 5.1.2-made marker (no declared_paths) takes the full
        # replay, which hashes the unrelated history too.
        prior_root = self.workspace / "prior-marker"
        old = self.prior_engine()
        prior_decoys = self.seed_decoys(root=prior_root, engine=old)
        prior_run = self.start("prior-target", root=prior_root, engine=old)
        self.propose(prior_run, "scoped.prior", "Recovered by full replay.",
                     root=prior_root, engine=old)
        crashed = self.worker(
            "witness_crash_before_replace", "sync", "--run", prior_run,
            root=prior_root, engine=old, check=False,
        )
        self.assertEqual(crashed.returncode, 110, crashed.stdout + crashed.stderr)
        prior_marker = json.loads(self.marker_path(prior_root).read_text("utf-8"))
        self.assertNotIn("declared_paths", prior_marker["scope"])
        self.assertEqual(prior_marker["prior_witness"]["engine_version"], "5.1.2")
        prior_digests = self.witnessed_file_digests(prior_root)
        result, recorded = self.probe(
            "hashes", "get", "--key", "scoped.prior", root=prior_root
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("Recovered by full replay.", result.stdout)
        self.assertFalse(self.marker_path(prior_root).exists())
        prior_hashed = set()
        for digest in set(recorded):
            prior_hashed.update(prior_digests.get(digest, []))
        self.assertIn(f".bimri/proposals/{prior_decoys[0]}.json", prior_hashed)
        self.assertIn(f".bimri/decisions/{prior_decoys[0]}.json", prior_hashed)

    # =======================================================================
    # Writer gate (D0.2, D0.3, D0.6)
    # =======================================================================

    def test_state_version_flips_on_first_authority_write_not_on_read(self):
        # D0.2 / DESIGN 2.1 items 1-2: reads and lifecycle commands leave a
        # 5.1.0 store at 5.1.0; the first authority write sets 5.2.0 in the
        # same save_state that bumps _audit_epoch (the marker's
        # pre_write_state still says 5.1.0); a second writer keeps 5.2.0;
        # artifacts stay 5.1.0 with receipt engine_release 5.2.0 (D0.1, D0.4).
        # install --quiescent on a 5.1.0 store is byte-identical (gate 9).
        self.seed_prior_store()

        def assert_unflipped(label):
            self.assertEqual(self.state()["bimri_version"], "5.1.0", label)

        self.cli("get", "--key", "upgrade.seed")
        assert_unflipped("get")
        self.assert_current_witness(self.witness())  # D0.5 one-time miss
        lifecycle = self.start("lifecycle-only")
        assert_unflipped("start")
        self.cli("journal", "--run", lifecycle, "--text", "Lifecycle note.")
        assert_unflipped("journal")
        self.cli("close", "--run", lifecycle, "--outcome", "success",
                 "--summary", "Lifecycle only.")
        assert_unflipped("lifecycle close")
        session = json.dumps({"session_id": "v520-flip-hook"})
        self.cli("hook-start", input_text=session)
        assert_unflipped("hook-start")
        self.cli("hook-close", input_text=session)
        assert_unflipped("hook-close")
        self.cli("get", "--key", "upgrade.seed")
        assert_unflipped("second get")

        writer = self.start("first-writer")
        assert_unflipped("writer start")
        epoch_before = self.state().get("_audit_epoch", 0)
        result, records = self.probe(
            "statewrites", "propose", "--run", writer, "--tier", "2",
            "--new-subject", "--key", "flip.first",
            "--text", "First authority write.",
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        proposal = PROPOSAL_RE.search(result.stdout).group(0)
        states = [r["content"] for r in records if r["file"] == "state.json"]
        markers = [
            r["content"] for r in records
            if r["file"] == "audit-transition.json"
            and isinstance(r["content"], dict)
            and r["content"].get("kind") == "authority"
        ]
        self.assertTrue(states, "the authority write must save state")
        bumped = []
        for content in states:
            epoch = content.get("_audit_epoch", 0)
            if epoch == epoch_before:
                self.assertEqual(content["bimri_version"], "5.1.0", content)
            else:
                self.assertGreater(epoch, epoch_before)
                self.assertEqual(content["bimri_version"], "5.2.1", content)
                bumped.append(content)
        self.assertTrue(bumped, "the epoch bump carries the version flip")
        self.assertTrue(markers, "an authority marker was written")
        self.assertEqual(markers[0]["pre_write_state"]["bimri_version"], "5.1.0")
        self.assertEqual(markers[0]["epoch_before"], epoch_before)
        self.assertEqual(self.state()["bimri_version"], "5.2.1")
        record = self.proposal(proposal)
        self.assertEqual(record["bimri_version"], "5.1.0")
        self.assertEqual(record["preflight_receipt"]["engine_release"], "5.2.1")

        self.cli("sync", "--run", writer)
        self.assertEqual(self.state()["bimri_version"], "5.2.1")
        self.assertEqual(self.decision(proposal)["bimri_version"], "5.1.0")
        self.assert_current_witness(self.witness())
        self.assertFalse(self.marker_path().exists())

        # Gate 9 pre-check: a code-only update of a 5.1.0 store never flips.
        other = self.workspace / "install-store"
        self.seed_prior_store(other)
        before = protected_tree_snapshot(other)
        state_bytes = self.state_bytes(other)
        installed = self.install(other, "--quiescent")
        self.assertIn("Existing authority store v5.1.0 verified", installed.stdout)
        self.assertIn("Memory preservation: PASSED", installed.stdout)
        self.assertEqual(protected_tree_snapshot(other), before)
        self.assertEqual(self.state_bytes(other), state_bytes)
        self.assertIn(
            ("5.2.1", "5.1.0"),
            [(r["engine_release"], r["memory_format"]) for r in self.receipts(other)],
        )

    def test_5_1_2_engine_refuses_flipped_store_before_mutation(self):
        # D0.6 / DESIGN 2.1 item 4: a store 5.2.0 has only read, started or
        # audited stays readable by 5.1.2, which re-proves the schema-2
        # witness as a miss; after the first authority write 5.1.2 start
        # refuses with `unsupported BIMRI state version: 5.2.0` (rc 2) before
        # any run, log or state write.
        old = self.prior_engine()
        self.seed_prior_store()
        self.cli("get", "--key", "upgrade.seed")
        self.start("read-only-chat")
        self.cli(*self.live_doctor_arguments())
        self.assertEqual(self.state()["bimri_version"], "5.1.0")
        self.assert_current_witness(self.witness())
        self.assertTrue(self.facts_files())

        read = self.cli("get", "--key", "upgrade.seed", engine=old)
        self.assertIn("Preserved prior value.", read.stdout)
        self.start("rollback-chat", engine=old)
        reproved = self.witness()
        self.assertEqual(reproved["witness_schema"], 1)
        self.assertEqual(reproved["engine_version"], "5.1.2")
        self.assertIn(
            "PASSED", self.cli("doctor", "--read-only", engine=old).stdout
        )

        self.apply_set("flip-writer", "flip.key", "One-way write.")
        self.assertEqual(self.state()["bimri_version"], "5.2.1")
        before = protected_tree_snapshot(self.root)
        logs_before = sorted(
            path.name for path in self.root.joinpath(".bimri", "log").iterdir()
        )
        refused = self.cli("start", "--actor", "too-old", engine=old, check=False)
        self.assertEqual(refused.returncode, 2, refused.stdout + refused.stderr)
        self.assertIn(STATE_REFUSAL, refused.stderr)
        self.assertIsNone(RUN_RE.search(refused.stdout))
        self.assertEqual(protected_tree_snapshot(self.root), before)
        self.assertEqual(
            sorted(path.name for path in self.root.joinpath(".bimri", "log").iterdir()),
            logs_before,
        )

    def test_old_engine_refuses_after_first_authority_write(self):
        # Section 10 gate 9 (CROSSREF; legacy propose/sync stands in for the
        # step-2 save as the first authority write). Install 5.2.0 over a
        # 5.1.0 store; 5.1.2 still reads and starts; after one 5.2.0
        # authority write every 5.1.2 command refuses with an unfiltered
        # snapshot equal before/after; a 5.2.0 code-only update of the flipped
        # store reports v5.2.0 and receipts ("5.2.0","5.2.0"); 5.1.2 install
        # over it refuses and rolls the install files back (DESIGN 2.1 item 4,
        # section 08 R8.5).
        old = self.prior_engine()
        self.seed_prior_store()
        first = self.install(self.root, "--quiescent")
        self.assertIn("Existing authority store v5.1.0 verified", first.stdout)
        self.assertEqual(self.state()["bimri_version"], "5.1.0")
        self.assertIn(
            "Preserved prior value.",
            self.cli("get", "--key", "upgrade.seed", engine=old).stdout,
        )
        self.start("still-rollbackable", engine=old)
        self.assertEqual(self.state()["bimri_version"], "5.1.0")

        self.apply_set("one-way", "oneway.key", "First 5.2.1 authority write.")
        self.assertEqual(self.state()["bimri_version"], "5.2.1")
        memory = protected_tree_snapshot(self.root)
        second = self.install(self.root, "--quiescent")
        self.assertIn("Existing authority store v5.2.1 verified", second.stdout)
        self.assertIn("Memory preservation: PASSED", second.stdout)
        self.assertEqual(protected_tree_snapshot(self.root), memory)
        self.assertEqual(self.state()["bimri_version"], "5.2.1")
        self.assertIn(
            ("5.2.1", "5.2.1"),
            [(r["engine_release"], r["memory_format"]) for r in self.receipts(self.root)],
        )

        # Observed 5.1.2 (2c936ec) behaviour on a 5.2.0 state: the writable
        # lane refuses at load_or_initialize l.2824-2827; the read-only lane
        # at load_current_state_read_only l.13244 (doctor --read-only reports
        # it as a FAILED audit, rc 1).
        expectations = (
            (("start", "--actor", "too-old"), 2, STATE_REFUSAL, "stderr"),
            (("status",), 2, STATE_REFUSAL, "stderr"),
            (("doctor",), 2, STATE_REFUSAL, "stderr"),
            (("get", "--key", "oneway.key"), 2, READ_ONLY_REFUSAL, "stderr"),
            (("doctor", "--read-only"), 1, READ_ONLY_REFUSAL, "stdout"),
        )
        for arguments, code, message, stream in expectations:
            with self.subTest(command=arguments[0:2]):
                before = protected_tree_snapshot(self.root)
                result = self.cli(*arguments, engine=old, check=False)
                self.assertEqual(result.returncode, code, result.stdout + result.stderr)
                self.assertIn(message, getattr(result, stream))
                self.assertEqual(protected_tree_snapshot(self.root), before)
        with self.subTest(command="hook-close"):
            before = protected_tree_snapshot(self.root)
            result = self.cli(
                "hook-close", engine=old, check=False,
                input_text=json.dumps({"session_id": "too-old"}),
            )
            self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
            self.assertIn(STATE_REFUSAL, result.stderr)
            self.assertEqual(protected_tree_snapshot(self.root), before)

        with self.subTest(command="install"):
            before = without_prefixes(
                protected_tree_snapshot(self.root), (".bimri/install-backups",)
            )
            installed_files = self.outside_memory_files()
            engine_bytes = self.root.joinpath("bimri-engine.py").read_bytes()
            self.assertIn(b'ENGINE_VERSION = "5.2.1"', engine_bytes)
            result = self.install(self.root, "--quiescent", engine=old, check=False)
            self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
            self.assertIn(STATE_REFUSAL, result.stderr)
            self.assertEqual(
                without_prefixes(
                    protected_tree_snapshot(self.root), (".bimri/install-backups",)
                ),
                before,
            )
            self.assertEqual(self.root.joinpath("bimri-engine.py").read_bytes(), engine_bytes)
            self.assertEqual(self.outside_memory_files(), installed_files)

    def test_writer_gate_flips_on_authority_writes_without_a_checkpoint(self):
        # D0.2 / D0.6 (step-1 fix-up). An authority write that takes no new
        # checkpoint transition still moves a 5.1.0 state to 5.2.0 before its
        # first effect: a retry under a retained transition marker
        # (enter_authority_write_after_audit) and degraded proposal staging
        # (begin_authority_write). Both lanes start from a failed resolution
        # written by 5.1.2: state 5.1.0, its resolve marker retained, W0 kept
        # but unreadable. Without the flip 5.1.2 kept opening the store after
        # a 5.2.0 authority write, and read the degraded proposal's v5.2.0
        # preflight receipt as authority damage.
        old = self.prior_engine()

        def failed_resolution(root):
            seed_run = self.start("gate-seed", root=root, engine=old)
            self.propose(
                seed_run, "gate.anchor", "Anchor value.", root=root, engine=old
            )
            self.cli("sync", "--run", seed_run, root=root, engine=old)
            candidate_run = self.start("gate-candidate", root=root, engine=old)
            writer_run = self.start("gate-writer", root=root, engine=old)
            spare_run = self.start("gate-spare", root=root, engine=old)
            candidate = self.propose(
                candidate_run, "gate.conflict", "Candidate value.",
                root=root, engine=old,
            )
            self.propose(
                writer_run, "gate.conflict", "Accepted writer value.",
                root=root, engine=old,
            )
            self.cli("sync", "--run", writer_run, root=root, engine=old)
            self.cli("sync", "--run", candidate_run, root=root, engine=old)
            decision = self.decision(candidate, root)
            self.assertEqual(decision["outcome"], "contested")
            conflict_id = decision["conflict_id"]
            self.cli("get", "--key", "gate.anchor", root=root, engine=old)
            failed = self.worker(
                "resolution_fail_during_force_apply", "resolve", conflict_id,
                "--choose", candidate, "--human-approved",
                root=root, engine=old, check=False,
            )
            self.assertEqual(failed.returncode, 2, failed.stdout + failed.stderr)
            self.assertEqual(resolution(root, conflict_id)["status"], "failed")
            self.assertEqual(self.state(root)["bimri_version"], "5.1.0")
            marker = json.loads(self.marker_path(root).read_text("utf-8"))
            self.assertEqual(marker["operation"], "resolve")
            self.assertEqual(marker["pre_write_state"]["bimri_version"], "5.1.0")
            return conflict_id, candidate, spare_run

        def resolution(root, conflict_id):
            return json.loads(
                Path(root).joinpath(
                    ".bimri", "resolutions", f"{conflict_id}.json"
                ).read_text("utf-8")
            )

        def assert_old_engine_refuses(root):
            before = protected_tree_snapshot(root)
            refused = self.cli(
                "start", "--actor", "too-old", root=root, engine=old,
                check=False,
            )
            self.assertEqual(refused.returncode, 2, refused.stdout + refused.stderr)
            self.assertIn(STATE_REFUSAL, refused.stderr)
            self.assertIsNone(RUN_RE.search(refused.stdout))
            self.assertEqual(protected_tree_snapshot(root), before)

        with self.subTest(lane="resolve retry under a retained marker"):
            root = self.workspace / "gate-retry"
            conflict_id, candidate, _spare = failed_resolution(root)
            retried = self.cli(
                "resolve", conflict_id, "--choose", candidate,
                "--human-approved", root=root,
            )
            self.assertIn(f"{conflict_id} resolved with {candidate}", retried.stdout)
            self.assertEqual(resolution(root, conflict_id)["status"], "resolved")
            self.assertEqual(self.state(root)["bimri_version"], "5.2.1")
            self.assertFalse(self.marker_path(root).exists())
            assert_old_engine_refuses(root)

        with self.subTest(lane="degraded propose"):
            root = self.workspace / "gate-degraded"
            conflict_id, candidate, spare_run = failed_resolution(root)
            staged = self.propose(
                spare_run, "gate.degraded", "Staged while recovery is pending.",
                root=root,
            )
            self.assertEqual(
                self.proposal(staged, root)["preflight_receipt"]["engine_release"],
                "5.2.1",
            )
            self.assertEqual(self.state(root)["bimri_version"], "5.2.1")
            assert_old_engine_refuses(root)
            # The flip is inside the retained 5.1.2 marker's whitelisted
            # delta: 5.2.0 still finishes the retry and the staged sync.
            self.cli(
                "resolve", conflict_id, "--choose", candidate,
                "--human-approved", root=root,
            )
            self.assertEqual(resolution(root, conflict_id)["status"], "resolved")
            self.cli("sync", "--run", spare_run, root=root)
            self.assertEqual(self.decision(staged, root)["outcome"], "accepted")
            self.assertEqual(self.state(root)["bimri_version"], "5.2.1")
            self.assert_current_witness(self.witness(root))

    def test_rollback_stranded_lifecycle_run_is_closable(self):
        # Section 10 gate 9 (CROSSREF; D0.6 revised): 5.2.0 start killed after
        # its state write and before the log create; 5.1.2 retires the
        # schema-2 marker as corrupt and leaves the run active with no log;
        # 5.2.0 recover-run --abandon writes the exact initial log bytes the
        # marker recorded, closes the run and pops it from active_runs and
        # session_runs. The same run WITH a proposal file stays a hard error.
        # Needs recover-run --abandon (build step 5); skipped until it exists.
        recover_help = self.help_text(ENGINE, "recover-run")
        if "--abandon" not in recover_help:
            self.skipTest("recover-run --abandon lands in build step 5")
        approval = ["--human-approved"] if "--human-approved" in recover_help else []
        old = self.prior_engine()
        self.seed_prior_store()

        def strand(actor):
            # A schema-2 checkpoint first, so the 5.2.0 start writes its
            # lifecycle marker (the case 5.1.2 cannot read). Doctor reports an
            # earlier stranded run as "active run R has no log" (rc 1) but its
            # passing governance audit still publishes the checkpoint.
            self.cli(*self.live_doctor_arguments(), check=False)
            self.assertEqual(self.witness()["witness_schema"], 2)
            self.assertEqual(self.state()["bimri_version"], "5.1.0")
            crashed = self.worker(
                "lifecycle_crash_before_log",
                *self.start_arguments(), "--actor", actor, check=False,
            )
            self.assertEqual(crashed.returncode, 110, crashed.stdout + crashed.stderr)
            marker = json.loads(self.marker_path().read_text("utf-8"))
            self.assertEqual(marker["kind"], "lifecycle")
            run_id = marker["run_id"]
            log = self.root / marker["log_path"]
            self.assertFalse(log.exists())
            self.assertIn(run_id, self.state()["active_runs"])
            receipts = len(self.drift_receipts())
            self.start(f"rollback-{actor}", engine=old)
            self.assertFalse(self.marker_path().exists())
            self.assertGreater(len(self.drift_receipts()), receipts)
            self.assertIn(run_id, self.state()["active_runs"])
            self.assertFalse(log.exists())
            return run_id, log, marker["log_append"]

        # Both strandings happen while the store is still 5.1.0: after the
        # first abandon (an authority write, D0.2) 5.1.2 refuses the store.
        run_id, log, intended = strand("stranded")
        damaged_run, damaged_log, _intended = strand("stranded-with-proposal")
        self.cli(
            "recover-run", "--run", run_id, "--abandon", *approval,
            "--summary", "Owner closed the stranded rollback run.",
        )
        text = log.read_text("utf-8")
        self.assertTrue(text.startswith(intended), text)
        self.assertIn(f"[CLOSED:{run_id} ", text)
        state = self.state()
        self.assertNotIn(run_id, state["active_runs"])
        self.assertNotIn(run_id, state.get("session_runs", {}).values())

        self.assertEqual(self.state()["bimri_version"], "5.2.1")
        proposal = self.root / ".bimri" / "proposals" / f"{damaged_run}-Q001.json"
        proposal.write_text("{}\n", encoding="utf-8")
        refused = self.cli(
            "recover-run", "--run", damaged_run, "--abandon", *approval,
            "--summary", "Must refuse.", check=False,
        )
        self.assertEqual(refused.returncode, 2, refused.stdout + refused.stderr)
        self.assertFalse(damaged_log.exists())
        self.assertIn(damaged_run, self.state()["active_runs"])


if __name__ == "__main__":
    unittest.main()
