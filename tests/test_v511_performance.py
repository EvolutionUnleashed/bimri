"""Adversarial regressions for the v5.1.x witness and exact-current path.

These tests stay at the public command boundary except for narrow fault
injection in ``crash_worker.py``.  They are deliberately small-store tests:
the performance benchmark owns latency claims, while this file proves the
fixed-cost read boundary, write-time validation, locking, and durability.
"""

import hashlib
import importlib.util
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
RUN_RE = re.compile(r"=== BIMRI BRIEF (R\d{6})")
PROPOSAL_RE = re.compile(r"\bR\d{6}-Q\d{3}\b")
WITNESS_RELATIVE = Path(".bimri") / "audit-witness.json"
MANIFEST_RELATIVE = Path(".bimri") / "audit-manifest.json"
MANIFEST_GENERATIONS_RELATIVE = Path(".bimri") / "audit-manifests"
TRANSITION_RELATIVE = Path(".bimri") / "audit-transition.json"
DRIFT_RELATIVE = Path(".bimri") / "audit-drift"
STABLE_INVENTORY_ROOTS = (
    "proposals",
    "decisions",
    "conflicts",
    "resolutions",
    "revisions",
    "archive",
    "recovery",
)
AUTHORITY_RECORD_ROOTS = (
    "proposals",
    "decisions",
    "conflicts",
    "resolutions",
)
WITNESS_FIELDS = {
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
MANIFEST_FIELDS = {
    "manifest_schema",
    "created_at",
    "manifest",
    "manifest_hash",
    "manifest_count",
    "manifest_file_hash",
}
BLOCKED_FIELDS = {
    "blocked_schema",
    "created_at",
    "reasons",
    "prior_witness",
    "prior_witness_hash",
    "prior_manifest",
    "marker_hash",
}
TRANSITION_FIELDS = {
    "transition_schema",
    "created_at",
    "kind",
    "operation",
    "run_id",
    "scope",
    "prior_witness",
    "prior_witness_hash",
    "prior_manifest_hash",
    "epoch_before",
    "epoch_after",
    "pre_write_state",
    "pre_write_state_hash",
    "post_write_state_hash",
    "log_path",
    "pre_log_hash",
    "post_log_hash",
    "log_append",
    "transition_hash",
}


class V511WitnessAndFastPathTest(unittest.TestCase):
    maxDiff = None

    def setUp(self):
        self._temp = tempfile.TemporaryDirectory(prefix="bimri-v511-fast-")
        self.workspace = Path(self._temp.name)
        self.root = self.workspace / "store"

    def tearDown(self):
        self._temp.cleanup()

    def cli(
        self,
        *arguments,
        root=None,
        check=True,
        timeout=60,
        engine=None,
    ):
        target = Path(root or self.root)
        command = [
            sys.executable,
            str(engine or ENGINE),
            "--root",
            str(target),
            *map(str, arguments),
        ]
        result = subprocess.run(
            command,
            text=True,
            capture_output=True,
            timeout=timeout,
        )
        if check and result.returncode != 0:
            self.fail(
                f"command failed: {command!r}\n"
                f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}"
            )
        return result

    def worker(self, mode, *arguments, root=None, timeout=60, check=True):
        target = Path(root or self.root)
        command = [
            sys.executable,
            str(CRASH_WORKER),
            str(ENGINE),
            mode,
            str(target),
            *map(str, arguments),
        ]
        result = subprocess.run(
            command,
            text=True,
            capture_output=True,
            timeout=timeout,
        )
        if check and result.returncode != 0:
            self.fail(
                f"fault worker failed: {command!r}\n"
                f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}"
            )
        return result

    def start(self, actor="v511-test", root=None):
        result = self.cli("start", "--actor", actor, root=root)
        match = RUN_RE.search(result.stdout)
        self.assertIsNotNone(match, result.stdout)
        return match.group(1)

    def propose(
        self,
        run_id,
        key,
        text,
        *,
        root=None,
        new_subject=False,
        source="agent",
        trust="working",
        operation="set",
    ):
        arguments = [
            "propose",
            "--run",
            run_id,
            "--operation",
            operation,
            "--tier",
            "2",
            "--key",
            key,
            "--source",
            source,
            "--trust",
            trust,
        ]
        if operation == "set":
            arguments.extend(("--text", text))
        if new_subject:
            arguments.append("--new-subject")
        result = self.cli(*arguments, root=root)
        match = PROPOSAL_RE.search(result.stdout)
        self.assertIsNotNone(match, result.stdout)
        return match.group(0)

    def apply_set(
        self,
        run_id,
        key,
        text,
        *,
        root=None,
        new_subject=False,
        source="agent",
        trust="working",
    ):
        proposal = self.propose(
            run_id,
            key,
            text,
            root=root,
            new_subject=new_subject,
            source=source,
            trust=trust,
        )
        self.cli("sync", "--run", run_id, root=root)
        return proposal

    def state(self, root=None):
        return json.loads(
            Path(root or self.root)
            .joinpath(".bimri", "state.json")
            .read_text("utf-8")
        )

    def witness_path(self, root=None):
        return Path(root or self.root) / WITNESS_RELATIVE

    def witness(self, root=None):
        path = self.witness_path(root)
        self.assertTrue(path.is_file(), f"missing audit witness: {path}")
        return json.loads(path.read_text("utf-8"))

    def manifest_path(self, root=None):
        return Path(root or self.root) / MANIFEST_RELATIVE

    def manifest(self, root=None):
        path = self.manifest_path(root)
        self.assertTrue(path.is_file(), f"missing audit manifest: {path}")
        return json.loads(path.read_text("utf-8"))

    def manifest_generation_path(self, manifest_hash, root=None):
        return (
            Path(root or self.root)
            / MANIFEST_GENERATIONS_RELATIVE
            / f"{manifest_hash}.json"
        )

    def assert_manifest_generation(
        self, root, manifest_hash, expected_manifest=None
    ):
        path = self.manifest_generation_path(manifest_hash, root)
        self.assertTrue(path.is_file(), f"missing manifest generation: {path}")
        record = json.loads(path.read_text("utf-8"))
        self.assertEqual(set(record), MANIFEST_FIELDS)
        self.assertEqual(record["manifest_hash"], manifest_hash)
        self.assertEqual(
            record["manifest_hash"], self.audit_digest(record["manifest"])
        )
        self.assertEqual(record["manifest_count"], len(record["manifest"]))
        self.assertEqual(
            record["manifest_file_hash"],
            self.record_seal(record, "manifest_file_hash"),
        )
        if expected_manifest is not None:
            self.assertEqual(record["manifest"], expected_manifest)
        return record

    def assert_steady_manifest_generation_bound(self, root=None):
        root = Path(root or self.root)
        witness = self.witness(root)
        directory = root / MANIFEST_GENERATIONS_RELATIVE
        generations = sorted(directory.glob("*.json"))
        self.assertEqual(
            [path.name for path in generations],
            [f"{witness['manifest_hash']}.json"],
        )
        self.assert_manifest_generation(root, witness["manifest_hash"])
        alias = self.manifest(root)
        self.assertEqual(alias["manifest_hash"], witness["manifest_hash"])
        self.assertEqual(alias["manifest_count"], witness["manifest_count"])
        return witness

    def assert_doctor_rejects_external_drift(self, root):
        audited = self.cli(
            "doctor", "--read-only", root=root, check=False
        )
        self.assertNotEqual(audited.returncode, 0, audited.stdout)
        self.assertRegex(
            (audited.stdout + audited.stderr).lower(),
            r"authority|audit|witness|manifest|changed|unexpected|unsafe|missing",
        )
        return audited

    def assert_authority_write_rejects_external_drift(self, result):
        self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
        self.assertRegex(
            (result.stdout + result.stderr).lower(),
            r"authority|audit|witness|manifest|changed|unexpected|protected",
        )

    def assert_sealed_blocked_prior(self, root, expected_witness=None):
        root = Path(root)
        marker_path = root / ".bimri" / "audit-blocked.json"
        self.assertTrue(marker_path.is_file())
        marker = json.loads(marker_path.read_text("utf-8"))
        self.assertEqual(set(marker), BLOCKED_FIELDS)
        self.assertEqual(marker["blocked_schema"], 1)
        self.assertTrue(marker["reasons"])
        self.assertEqual(
            marker["marker_hash"], self.record_seal(marker, "marker_hash")
        )
        prior = marker["prior_witness"]
        self.assertIsInstance(prior, dict)
        self.assertEqual(set(prior), WITNESS_FIELDS)
        self.assertEqual(
            prior["witness_hash"], self.record_seal(prior, "witness_hash")
        )
        self.assertEqual(marker["prior_witness_hash"], prior["witness_hash"])
        prior_manifest = marker["prior_manifest"]
        self.assertIsInstance(prior_manifest, list)
        self.assertEqual(
            prior["manifest_hash"], self.audit_digest(prior_manifest)
        )
        self.assertEqual(prior["manifest_count"], len(prior_manifest))
        self.assert_manifest_generation(
            root, prior["manifest_hash"], prior_manifest
        )
        if expected_witness is not None:
            self.assertEqual(prior, json.loads(expected_witness.decode("utf-8")))
        return marker

    def drift_receipts(self, root=None):
        directory = Path(root or self.root) / DRIFT_RELATIVE
        if not directory.is_dir():
            return []
        entries = []
        for path in directory.glob("D*.json"):
            if not path.is_file():
                continue
            match = re.fullmatch(r"D(\d{6,})-[0-9a-f]{12}\.json", path.name)
            if match:
                entries.append((int(match.group(1)), path.name, path))
        return [entry[2] for entry in sorted(entries)]

    def drift_reasons(self, root=None):
        reasons = []
        for path in self.drift_receipts(root):
            record = json.loads(path.read_text("utf-8"))
            self.assertEqual(record["drift_schema"], 1)
            reasons.extend(str(reason) for reason in record["reasons"])
        return reasons

    def assert_drift_receipt(self, root=None, pattern=None, minimum=1):
        receipts = self.drift_receipts(root)
        self.assertGreaterEqual(len(receipts), minimum, receipts)
        if pattern is not None:
            self.assertRegex(
                " ".join(self.drift_reasons(root)).lower(), pattern
            )
        return receipts

    def assert_no_audit_block(self, root=None):
        self.assertFalse(
            Path(root or self.root)
            .joinpath(".bimri", "audit-blocked.json")
            .exists()
        )

    def assert_prior_witness_retained_but_invalid(self, root, expected_bytes):
        root = Path(root)
        path = self.witness_path(root)
        self.assertTrue(path.is_file())
        self.assertEqual(path.read_bytes(), expected_bytes)
        prior = json.loads(expected_bytes.decode("utf-8"))
        self.assertGreater(
            self.state(root).get("_audit_epoch", 0), prior["audit_epoch"]
        )
        return prior

    def assert_sealed_transition(
        self, root, expected_kind, expected_operation, expected_witness
    ):
        root = Path(root)
        path = root / TRANSITION_RELATIVE
        self.assertTrue(path.is_file(), f"missing audit transition: {path}")
        marker = json.loads(path.read_text("utf-8"))
        self.assertEqual(set(marker), TRANSITION_FIELDS)
        self.assertEqual(marker["transition_schema"], 1)
        self.assertEqual(marker["kind"], expected_kind)
        self.assertEqual(marker["operation"], expected_operation)
        self.assertEqual(
            marker["transition_hash"],
            self.record_seal(marker, "transition_hash"),
        )
        prior = marker["prior_witness"]
        self.assertEqual(set(prior), WITNESS_FIELDS)
        self.assertEqual(
            prior["witness_hash"], self.record_seal(prior, "witness_hash")
        )
        self.assertEqual(marker["prior_witness_hash"], prior["witness_hash"])
        self.assertEqual(marker["prior_manifest_hash"], prior["manifest_hash"])
        self.assertIsInstance(marker["pre_write_state"], dict)
        self.assertEqual(
            marker["pre_write_state_hash"],
            self.audit_digest(marker["pre_write_state"]),
        )
        self.assertEqual(
            prior, json.loads(expected_witness.decode("utf-8"))
        )
        self.assert_manifest_generation(root, prior["manifest_hash"])
        return marker

    @staticmethod
    def audit_digest(value):
        encoded = json.dumps(
            value, separators=(",", ":"), sort_keys=True
        ).encode("utf-8")
        return hashlib.sha256(encoded).hexdigest()

    @classmethod
    def record_seal(cls, record, seal_field):
        return cls.audit_digest({
            key: value for key, value in record.items() if key != seal_field
        })

    @staticmethod
    def authority_snapshot(root):
        root = Path(root)
        included = [root / "bimri.md", root / ".bimri" / "state.json"]
        for directory in (*STABLE_INVENTORY_ROOTS, "log"):
            included.extend(
                path
                for path in root.joinpath(".bimri", directory).rglob("*")
                if path.is_file() and not path.is_symlink()
            )
        return {
            path.relative_to(root).as_posix(): path.read_bytes()
            for path in included
            if path.is_file() and not path.is_symlink()
        }

    def clone_store(self, label):
        destination = self.workspace / label
        shutil.copytree(self.root, destination)
        return destination

    def seed_authority_graph(self):
        seed_run = self.start("witness-seed")
        seed_proposal = self.apply_set(
            seed_run,
            "witness.seed",
            "Stable current value used by witness tests.",
            new_subject=True,
        )

        candidate_run = self.start("witness-candidate")
        writer_run = self.start("witness-writer")
        candidate = self.propose(
            candidate_run,
            "witness.conflict",
            "Candidate value chosen by the later resolution.",
            new_subject=True,
        )
        writer = self.propose(
            writer_run,
            "witness.conflict",
            "Concurrent writer value.",
            new_subject=True,
        )
        self.cli("sync", "--run", writer_run)
        self.cli("sync", "--run", candidate_run)
        decision_path = (
            self.root / ".bimri" / "decisions" / f"{candidate}.json"
        )
        decision = json.loads(decision_path.read_text("utf-8"))
        self.assertEqual(decision["outcome"], "contested")
        conflict_id = decision["conflict_id"]
        self.cli(
            "resolve",
            conflict_id,
            "--choose",
            candidate,
            "--human-approved",
        )

        recalled = self.cli("get", "--key", "witness.seed")
        self.assertIn("witness.seed", recalled.stdout)
        self.witness()
        return {
            "proposals": self.root / ".bimri" / "proposals" / f"{seed_proposal}.json",
            "decisions": self.root / ".bimri" / "decisions" / f"{seed_proposal}.json",
            "conflicts": self.root / ".bimri" / "conflicts" / f"{conflict_id}.json",
            "resolutions": self.root / ".bimri" / "resolutions" / f"{conflict_id}.json",
        }

    def seed_cold_current(self, root):
        root = Path(root)
        create_run = self.start("cold-create", root=root)
        self.apply_set(
            create_run,
            "parity.cold",
            "Original cold-current value.",
            root=root,
            new_subject=True,
        )
        hot = root.joinpath("bimri.md").read_bytes()
        state_path = root / ".bimri" / "state.json"
        state = json.loads(state_path.read_text("utf-8"))
        state["hot_max_bytes"] = len(hot) + 32
        state_path.write_text(
            json.dumps(state, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        self.witness_path(root).unlink(missing_ok=True)
        update_run = self.start("cold-update", root=root)
        current = "Current cold value. " + "c" * 390
        self.apply_set(
            update_run,
            "parity.cold",
            current,
            root=root,
        )
        self.assertIn("parity.cold", self.state(root)["cold_current"])
        return current

    def test_witness_creation_schema_and_manifest_are_complete(self):
        self.seed_authority_graph()
        witness = self.witness()
        manifest_record = self.manifest()
        state = self.state()

        self.assertEqual(set(witness), WITNESS_FIELDS)
        self.assertEqual(set(manifest_record), MANIFEST_FIELDS)
        self.assertEqual(witness["witness_schema"], 1)
        self.assertEqual(manifest_record["manifest_schema"], 1)
        self.assertEqual(witness["head_revision"], state["head_revision"])
        self.assertEqual(witness["head_hash"], state["head_hash"])
        self.assertRegex(witness["state_hash"], r"^[0-9a-f]{64}$")
        self.assertRegex(witness["write_state_hash"], r"^[0-9a-f]{64}$")
        self.assertEqual(witness["audit_epoch"], state.get("_audit_epoch", 0))
        self.assertRegex(witness["run_authority_hash"], r"^[0-9a-f]{64}$")
        self.assertEqual(
            witness["proposal_runs"], sorted(set(witness["proposal_runs"]))
        )
        for run_id in witness["proposal_runs"]:
            self.assertRegex(run_id, r"^R\d{6}$")
        self.assertEqual(
            witness["witness_hash"],
            self.record_seal(witness, "witness_hash"),
        )

        rows = manifest_record["manifest"]
        self.assertEqual(manifest_record["manifest_count"], len(rows))
        self.assertEqual(witness["manifest_count"], len(rows))
        self.assertEqual(manifest_record["manifest_hash"], self.audit_digest(rows))
        self.assertEqual(witness["manifest_hash"], self.audit_digest(rows))
        self.assertEqual(
            manifest_record["manifest_file_hash"],
            self.record_seal(manifest_record, "manifest_file_hash"),
        )
        self.assert_manifest_generation(
            self.root, witness["manifest_hash"], rows
        )
        paths = [row["path"] for row in rows]
        self.assertEqual(paths, sorted(paths))
        self.assertEqual(len(paths), len(set(paths)))
        self.assertNotIn(".bimri/state.json", paths)
        self.assertFalse(any(path.startswith(".bimri/log/") for path in paths))
        self.assertNotIn(WITNESS_RELATIVE.as_posix(), paths)
        self.assertNotIn(MANIFEST_RELATIVE.as_posix(), paths)
        for row in rows:
            relative = row["path"]
            self.assertFalse(Path(relative).is_absolute(), relative)
            self.assertNotIn("\\", relative)
            path = self.root.joinpath(*Path(relative).parts)
            self.assertTrue(path.is_file(), relative)
            self.assertEqual(
                row["sha256"], hashlib.sha256(path.read_bytes()).hexdigest()
            )

    def test_witness_conflict_and_held_projections_cannot_author_live_truth(self):
        seed_run = self.start("projection-seed")
        self.apply_set(
            seed_run,
            "projection.anchor",
            "Current value used to establish a healthy witness.",
            new_subject=True,
        )

        candidate_run = self.start("projection-candidate")
        writer_run = self.start("projection-writer")
        candidate = self.propose(
            candidate_run,
            "projection.conflict",
            "Contested candidate retained for owner review.",
            new_subject=True,
        )
        self.propose(
            writer_run,
            "projection.conflict",
            "Accepted current value beside the open conflict.",
            new_subject=True,
        )
        self.cli("sync", "--run", writer_run)
        self.cli("sync", "--run", candidate_run)
        decision = json.loads(
            self.root.joinpath(
                ".bimri", "decisions", f"{candidate}.json"
            ).read_text("utf-8")
        )
        self.assertEqual(decision["outcome"], "contested")
        conflict_id = decision["conflict_id"]

        owner_run = self.start("projection-owner")
        self.apply_set(
            owner_run,
            "projection.held",
            "Owner-confirmed current value.",
            new_subject=True,
            source="user",
            trust="confirmed",
        )
        agent_run = self.start("projection-agent")
        held_proposal = self.apply_set(
            agent_run,
            "projection.held",
            "Unconfirmed candidate that remains held.",
        )
        held_decision = json.loads(
            self.root.joinpath(
                ".bimri", "decisions", f"{held_proposal}.json"
            ).read_text("utf-8")
        )
        self.assertEqual(held_decision["outcome"], "held")
        self.cli("get", "--key", "projection.anchor")
        baseline = self.witness()
        self.assertEqual(set(baseline), WITNESS_FIELDS)
        self.assertNotIn("open_conflicts", baseline)
        self.assertNotIn("held_records", baseline)
        baseline_status = self.cli("status")
        baseline_review = self.cli("review", conflict_id)
        self.assertIn("Open conflicts: 1", baseline_status.stdout)
        self.assertIn(conflict_id, baseline_review.stdout)

        tampered_root = self.clone_store("projection-tampered")
        tampered = self.witness(tampered_root)
        tampered["open_conflicts"] = []
        tampered["held_records"] = [{
            "location": "HELD",
            "key": "phantom.held",
            "id": "R999999-Q999",
            "detail": "Well-formed cache fiction with no authority record.",
            "reason": "confirmed-user-authority-required",
            "trust": "working",
            "source": "agent",
        }]
        tampered["witness_hash"] = self.record_seal(
            tampered, "witness_hash"
        )
        self.witness_path(tampered_root).write_text(
            json.dumps(tampered, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )

        status = self.cli("status", root=tampered_root)
        review = self.cli("review", conflict_id, root=tampered_root)
        self.assertIn("Open conflicts: 1", status.stdout)
        self.assertIn(conflict_id, review.stdout)
        held_read = self.cli(
            "get",
            "--key",
            "projection.held",
            "--history",
            root=tampered_root,
        )
        self.assertIn(held_proposal, held_read.stdout)
        phantom_read = self.cli(
            "get",
            "--key",
            "phantom.held",
            "--history",
            root=tampered_root,
            check=False,
        )
        self.assertEqual(phantom_read.returncode, 1)
        self.assertIn("no memory matched", phantom_read.stdout.lower())
        refreshed = self.witness(tampered_root)
        self.assertEqual(set(refreshed), WITNESS_FIELDS)
        self.assertNotIn("open_conflicts", refreshed)
        self.assertNotIn("held_records", refreshed)

    def test_file_shaped_empty_authority_directories_are_caught_by_doctor(self):
        self.seed_authority_graph()
        probes = (
            Path("decisions") / "R999999-Q999.json",
            Path("conflicts") / "C999999.json",
            Path("resolutions") / "C999999.json",
        )
        for relative in probes:
            with self.subTest(relative=relative.as_posix()):
                root = self.clone_store(
                    "file-shaped-" + relative.parent.name
                )
                witness_before = self.witness_path(root).read_bytes()
                root.joinpath(".bimri", relative).mkdir()

                recalled = self.cli(
                    "get",
                    "--key",
                    "witness.seed",
                    root=root,
                )

                self.assertIn("witness.seed", recalled.stdout)
                self.assertEqual(
                    self.witness_path(root).read_bytes(), witness_before
                )
                self.assert_doctor_rejects_external_drift(root)

    def test_harmless_external_edits_read_warm_and_doctor_rebaselines(self):
        records = self.seed_authority_graph()

        for directory in AUTHORITY_RECORD_ROOTS:
            with self.subTest(directory=directory):
                root = self.clone_store(f"edit-{directory}")
                path = root / records[directory].relative_to(self.root)
                witness_before = self.witness_path(root).read_bytes()
                path.write_bytes(path.read_bytes() + b"\n")

                recalled = self.cli("get", "--key", "witness.seed", root=root)

                self.assertIn("witness.seed", recalled.stdout)
                self.assertEqual(
                    self.witness_path(root).read_bytes(), witness_before
                )
                read_only = self.cli("doctor", "--read-only", root=root)
                self.assertIn("PASSED", read_only.stdout)
                self.assertRegex(
                    read_only.stdout.lower(), r"drift|differs"
                )
                self.assertEqual(
                    self.witness_path(root).read_bytes(), witness_before
                )
                repaired = self.cli("doctor", root=root)
                self.assertIn("BIMRI doctor: PASSED", repaired.stdout)
                self.assert_drift_receipt(
                    root, pattern=r"inventory|differs"
                )
                self.assert_no_audit_block(root)
                self.assertNotEqual(
                    self.witness_path(root).read_bytes(), witness_before
                )

    def test_external_additions_survive_and_are_receipted_on_rebaseline(self):
        self.seed_authority_graph()
        for directory in STABLE_INVENTORY_ROOTS:
            with self.subTest(directory=directory):
                root = self.clone_store(f"add-{directory}")
                witness_before = self.witness_path(root).read_bytes()
                added = root / ".bimri" / directory / "owner-extra.bin"
                added.write_bytes(f"extra inventory file in {directory}\n".encode())

                recalled = self.cli("get", "--key", "witness.seed", root=root)

                self.assertIn("witness.seed", recalled.stdout)
                self.assertEqual(
                    self.witness_path(root).read_bytes(), witness_before
                )
                repaired = self.cli("doctor", root=root)
                self.assertIn("BIMRI doctor: PASSED", repaired.stdout)
                # The unknown file is preserved (the manifest is not a
                # known-file allowlist), the divergence is evidenced, and
                # the store keeps working without any block state.
                self.assertTrue(added.is_file())
                self.assert_drift_receipt(root, pattern=r"owner-extra|added")
                self.assert_no_audit_block(root)
                after = self.cli("get", "--key", "witness.seed", root=root)
                self.assertIn("witness.seed", after.stdout)

    def test_external_deletions_are_deferred_to_doctor(self):
        records = self.seed_authority_graph()
        for directory in AUTHORITY_RECORD_ROOTS:
            with self.subTest(directory=directory):
                root = self.clone_store(f"delete-{directory}")
                witness_before = self.witness_path(root).read_bytes()
                target = root / records[directory].relative_to(self.root)
                target.unlink()

                recalled = self.cli(
                    "get",
                    "--key",
                    "witness.seed",
                    root=root,
                )

                self.assertIn("witness.seed", recalled.stdout)
                self.assertEqual(
                    self.witness_path(root).read_bytes(), witness_before
                )
                self.assert_doctor_rejects_external_drift(root)

    def test_external_symlink_replacements_are_deferred_to_doctor(self):
        records = self.seed_authority_graph()
        probe = self.workspace / "symlink-probe"
        probe.write_text("probe\n", encoding="utf-8")
        link = self.workspace / "symlink-capability"
        try:
            link.symlink_to(probe)
            link.unlink()
        except (NotImplementedError, OSError) as exc:
            self.skipTest(f"symbolic links are unavailable on this host: {exc}")

        for directory in AUTHORITY_RECORD_ROOTS:
            with self.subTest(directory=directory):
                root = self.clone_store(f"symlink-{directory}")
                witness_before = self.witness_path(root).read_bytes()
                target = root / records[directory].relative_to(self.root)
                external = self.workspace / f"external-{directory}.json"
                external.write_bytes(target.read_bytes())
                target.unlink()
                target.symlink_to(external)

                recalled = self.cli(
                    "get",
                    "--key",
                    "witness.seed",
                    root=root,
                )

                self.assertIn("witness.seed", recalled.stdout)
                self.assertEqual(
                    self.witness_path(root).read_bytes(), witness_before
                )
                self.assertEqual(
                    external.read_bytes(), records[directory].read_bytes()
                )
                self.assert_doctor_rejects_external_drift(root)

    def test_valid_checkpoint_writes_skip_global_graph_replay(self):
        self.seed_authority_graph()

        write_run = self.start("checkpoint-write")
        proposed = self.worker(
            "valid_checkpoint_forbid_graph_replay",
            "propose",
            "--run",
            write_run,
            "--operation",
            "set",
            "--tier",
            "2",
            "--key",
            "checkpoint.write",
            "--text",
            "A valid checkpoint uses the bounded write guard.",
            "--source",
            "agent",
            "--trust",
            "working",
            "--new-subject",
        )
        proposal_match = PROPOSAL_RE.search(proposed.stdout)
        self.assertIsNotNone(proposal_match, proposed.stdout)
        proposal_id = proposal_match.group(0)
        synced = self.worker(
            "valid_checkpoint_forbid_graph_replay",
            "sync",
            "--run",
            write_run,
        )
        self.assertIn("applied 1", synced.stdout)
        decision = json.loads(
            self.root.joinpath(
                ".bimri", "decisions", f"{proposal_id}.json"
            ).read_text("utf-8")
        )
        self.assertEqual(decision["outcome"], "accepted")

        candidate_run = self.start("checkpoint-resolution-candidate")
        writer_run = self.start("checkpoint-resolution-writer")
        candidate = self.propose(
            candidate_run,
            "checkpoint.resolve",
            "Candidate resolved through the bounded write guard.",
            new_subject=True,
        )
        self.propose(
            writer_run,
            "checkpoint.resolve",
            "Accepted writer value before the bounded resolution.",
            new_subject=True,
        )
        self.cli("sync", "--run", writer_run)
        self.cli("sync", "--run", candidate_run)
        candidate_decision = json.loads(
            self.root.joinpath(
                ".bimri", "decisions", f"{candidate}.json"
            ).read_text("utf-8")
        )
        conflict_id = candidate_decision["conflict_id"]
        resolved = self.worker(
            "valid_checkpoint_forbid_graph_replay",
            "resolve",
            conflict_id,
            "--choose",
            "current",
            "--human-approved",
        )
        self.assertIn(f"{conflict_id} resolved", resolved.stdout)
        resolution = json.loads(
            self.root.joinpath(
                ".bimri", "resolutions", f"{conflict_id}.json"
            ).read_text("utf-8")
        )
        self.assertEqual(resolution["status"], "resolved")

    def test_state_tamper_is_receipted_and_authoring_continues(self):
        self.seed_authority_graph()
        authoring_run = self.start("state-author-original")
        base_root = self.clone_store("state-authoring-base")

        def forge_actor(state):
            state["active_runs"][authoring_run]["actor"] = "forged-actor"

        def forge_run_date(state):
            state["run_dates"][authoring_run] = "2000-01-01"

        for label, mutation in (
            ("actor", forge_actor),
            ("run-date", forge_run_date),
        ):
            with self.subTest(label=label):
                root = self.workspace / f"state-authoring-{label}"
                shutil.copytree(base_root, root)
                witness_before = self.witness_path(root).read_bytes()
                state_path = root / ".bimri" / "state.json"
                state = json.loads(state_path.read_text("utf-8"))
                mutation(state)
                state_path.write_text(
                    json.dumps(state, indent=2, sort_keys=True) + "\n",
                    encoding="utf-8",
                )
                proposals_before = set(
                    root.joinpath(".bimri", "proposals").glob("*.json")
                )

                result = self.cli(
                    "propose",
                    "--run",
                    authoring_run,
                    "--operation",
                    "set",
                    "--tier",
                    "2",
                    "--key",
                    f"state.tamper.{label}",
                    "--text",
                    "Tampered authoring state is evidenced, never silent.",
                    "--source",
                    "agent",
                    "--trust",
                    "working",
                    "--new-subject",
                    root=root,
                    check=False,
                )

                # Cache-miss semantics: the divergent state forces a full
                # semantic audit and an evidence receipt, and the store
                # keeps working instead of latching shut.
                self.assertEqual(
                    result.returncode, 0, result.stdout + result.stderr
                )
                self.assertGreater(
                    len(set(
                        root.joinpath(".bimri", "proposals").glob("*.json")
                    )),
                    len(proposals_before),
                )
                self.assert_no_audit_block(root)
                self.assert_drift_receipt(
                    root, pattern=r"state|run facts"
                )
                self.assertNotEqual(
                    self.witness_path(root).read_bytes(), witness_before
                )

    def test_external_drift_is_receipted_and_authority_writes_proceed(self):
        self.seed_authority_graph()

        propose_root = self.clone_store("authority-write-propose")
        propose_run = next(iter(self.state(propose_root)["active_runs"]))
        propose_witness = self.witness_path(propose_root).read_bytes()
        propose_before = set(
            propose_root.joinpath(".bimri", "proposals").glob("*.json")
        )
        propose_root.joinpath(
            ".bimri", "archive", "outside-propose.bin"
        ).write_bytes(b"out-of-engine protected addition\n")
        proposed = self.cli(
            "propose",
            "--run",
            propose_run,
            "--operation",
            "set",
            "--tier",
            "2",
            "--key",
            "authority.write.propose",
            "--text",
            "This proposal must not be staged across external drift.",
            "--source",
            "agent",
            "--trust",
            "working",
            "--new-subject",
            root=propose_root,
            check=False,
        )
        self.assertEqual(
            proposed.returncode, 0, proposed.stdout + proposed.stderr
        )
        self.assertGreater(
            len(set(
                propose_root.joinpath(".bimri", "proposals").glob("*.json")
            )),
            len(propose_before),
        )
        self.assertNotEqual(
            self.witness_path(propose_root).read_bytes(), propose_witness
        )
        self.assert_no_audit_block(propose_root)
        self.assert_drift_receipt(
            propose_root, pattern=r"outside-propose|inventory"
        )
        after_read = self.cli(
            "get",
            "--key",
            "witness.seed",
            root=propose_root,
        )
        self.assertIn("witness.seed", after_read.stdout)

        sync_root = self.clone_store("authority-write-sync")
        sync_run = self.start("authority-write-sync", root=sync_root)
        sync_proposal = self.propose(
            sync_run,
            "authority.write.sync",
            "This decision must not commit across external drift.",
            root=sync_root,
            new_subject=True,
        )
        sync_witness = self.witness_path(sync_root).read_bytes()
        sync_root.joinpath(
            ".bimri", "archive", "outside-sync.bin"
        ).write_bytes(b"out-of-engine protected addition\n")
        synced = self.cli("sync", "--run", sync_run, root=sync_root)
        self.assertEqual(synced.returncode, 0)
        self.assertTrue(
            sync_root.joinpath(
                ".bimri", "decisions", f"{sync_proposal}.json"
            ).is_file()
        )
        self.assertNotEqual(
            self.witness_path(sync_root).read_bytes(), sync_witness
        )
        self.assert_no_audit_block(sync_root)
        self.assert_drift_receipt(
            sync_root, pattern=r"outside-sync|inventory"
        )

        resolve_root = self.clone_store("authority-write-resolve")
        candidate_run = self.start(
            "authority-write-candidate", root=resolve_root
        )
        writer_run = self.start("authority-write-writer", root=resolve_root)
        candidate = self.propose(
            candidate_run,
            "authority.write.resolve",
            "Candidate retained for the resolution preflight.",
            root=resolve_root,
            new_subject=True,
        )
        self.propose(
            writer_run,
            "authority.write.resolve",
            "Accepted writer value before owner resolution.",
            root=resolve_root,
            new_subject=True,
        )
        self.cli("sync", "--run", writer_run, root=resolve_root)
        self.cli("sync", "--run", candidate_run, root=resolve_root)
        candidate_decision = json.loads(
            resolve_root.joinpath(
                ".bimri", "decisions", f"{candidate}.json"
            ).read_text("utf-8")
        )
        conflict_id = candidate_decision["conflict_id"]
        resolve_witness = self.witness_path(resolve_root).read_bytes()
        resolve_root.joinpath(
            ".bimri", "archive", "outside-resolve.bin"
        ).write_bytes(b"out-of-engine protected addition\n")
        resolved = self.cli(
            "resolve",
            conflict_id,
            "--choose",
            "current",
            "--human-approved",
            root=resolve_root,
        )
        self.assertEqual(resolved.returncode, 0)
        self.assertTrue(
            resolve_root.joinpath(
                ".bimri", "resolutions", f"{conflict_id}.json"
            ).is_file()
        )
        self.assertNotEqual(
            self.witness_path(resolve_root).read_bytes(), resolve_witness
        )
        self.assert_no_audit_block(resolve_root)
        self.assert_drift_receipt(
            resolve_root, pattern=r"outside-resolve|inventory"
        )

    def test_byte_drift_is_receipted_without_restoration_ceremony(self):
        records = self.seed_authority_graph()
        target = records["decisions"]
        valid_bytes = target.read_bytes()
        witness_before = self.witness_path().read_bytes()
        target.write_bytes(valid_bytes + b"\n")
        run_id = next(iter(self.state()["active_runs"]))

        drifted_write = self.cli(
            "propose",
            "--run",
            run_id,
            "--operation",
            "set",
            "--tier",
            "2",
            "--key",
            "drift.doctor.probe",
            "--text",
            "Byte drift is evidenced while the store keeps working.",
            "--source",
            "agent",
            "--trust",
            "working",
            "--new-subject",
        )
        self.assertEqual(drifted_write.returncode, 0)
        self.assert_no_audit_block()
        self.assert_drift_receipt(pattern=r"inventory|differs")
        self.assertNotEqual(self.witness_path().read_bytes(), witness_before)

        audited = self.cli("doctor", "--read-only")
        self.assertIn("PASSED", audited.stdout)
        self.assertIn("unexplained-drift receipt", audited.stdout)

        repaired = self.cli("doctor")
        self.assertIn("BIMRI doctor: PASSED", repaired.stdout)
        self.assert_no_audit_block()
        recalled = self.cli("get", "--key", "witness.seed")
        self.assertIn("witness.seed", recalled.stdout)

    def test_witness_publication_crash_after_drift_heals_on_next_audit(self):
        self.seed_authority_graph()
        witness_before = self.witness_path().read_bytes()
        self.root.joinpath(
            ".bimri", "archive", "outside-crash-window.bin"
        ).write_bytes(b"out-of-engine protected addition\n")

        crashed = self.worker(
            "witness_crash_before_replace",
            "doctor",
            check=False,
        )

        self.assertEqual(
            crashed.returncode, 110, crashed.stdout + crashed.stderr
        )
        self.assertEqual(self.witness_path().read_bytes(), witness_before)
        self.assert_no_audit_block()
        # The receipt is a transactional precondition of publication, so it
        # is already durable even though the crash stopped the new witness.
        crash_receipts = self.assert_drift_receipt(
            pattern=r"outside-crash-window|inventory"
        )

        repaired = self.cli("doctor")
        self.assertIn("BIMRI doctor: PASSED", repaired.stdout)
        self.assertNotEqual(self.witness_path().read_bytes(), witness_before)
        checkpoint = self.witness()
        self.assertEqual(set(checkpoint), WITNESS_FIELDS)
        self.assertEqual(
            checkpoint["witness_hash"],
            self.record_seal(checkpoint, "witness_hash"),
        )
        # The retry deduplicates against the identical pre-crash receipt.
        self.assertEqual(len(self.drift_receipts()), len(crash_receipts))
        self.assert_no_audit_block()
        recalled = self.cli("get", "--key", "witness.seed")
        self.assertIn("witness.seed", recalled.stdout)

    def test_degraded_staging_and_quarantine_restore_without_baseline(self):
        records = self.seed_authority_graph()
        conflict_relative = records["conflicts"].relative_to(self.root)
        conflict_id = records["conflicts"].stem

        root = self.clone_store("degraded-restore")
        conflict_path = root / conflict_relative
        valid_conflict = conflict_path.read_bytes()
        corrupt_conflict = b"{blocked restore corruption"
        conflict_path.write_bytes(corrupt_conflict)
        run_id = next(iter(self.state(root)["active_runs"]))

        staged = self.cli(
            "propose",
            "--run",
            run_id,
            "--operation",
            "set",
            "--tier",
            "2",
            "--key",
            "degraded.restore.stage",
            "--text",
            "Degraded staging still works on a damaged authority store.",
            "--source",
            "agent",
            "--trust",
            "working",
            "--new-subject",
            root=root,
        )
        self.assertEqual(staged.returncode, 0)
        self.assert_no_audit_block(root)
        self.assert_drift_receipt(root, pattern=r"inventory|differs|state")

        gated = self.cli(
            "get", "--key", "witness.seed", root=root, check=False
        )
        self.assertEqual(gated.returncode, 2)
        self.assertIn(
            "recovery",
            (gated.stdout + gated.stderr).lower(),
        )

        quarantined = self.cli(
            "quarantine-authority",
            "--kind",
            "conflict",
            "--id",
            conflict_id,
            "--human-approved",
            root=root,
        )
        self.assertIn("exact bytes preserved", quarantined.stdout)
        stub = json.loads(conflict_path.read_text("utf-8"))
        self.assertEqual(stub["record_type"], "authority-quarantine")
        recovery_copy = root / stub["recovery_file"]
        self.assertEqual(recovery_copy.read_bytes(), corrupt_conflict)

        replacement = root / "reviewed-conflict.json"
        replacement.write_bytes(valid_conflict)
        restored = self.cli(
            "restore-authority",
            "--kind",
            "conflict",
            "--id",
            conflict_id,
            "--from",
            replacement,
            "--human-approved",
            root=root,
        )
        self.assertEqual(
            restored.returncode, 0, restored.stdout + restored.stderr
        )
        self.assertEqual(conflict_path.read_bytes(), valid_conflict)
        self.assert_no_audit_block(root)
        receipts = list(
            root.joinpath(".bimri", "recovery").glob(
                f"authority-restore-conflict-{conflict_id}-*.json"
            )
        )
        self.assertEqual(len(receipts), 1)
        recovered = self.cli("get", "--key", "witness.seed", root=root)
        self.assertIn("witness.seed", recovered.stdout)
        audited = self.cli("doctor", "--read-only", root=root)
        self.assertIn("PASSED", audited.stdout)

    def test_direct_quarantine_preserves_baseline_and_cannot_bless_other_drift(self):
        records = self.seed_authority_graph()
        conflict_path = records["conflicts"]
        conflict_id = conflict_path.stem
        valid_conflict = conflict_path.read_bytes()
        witness_before = self.witness_path().read_bytes()
        manifest_before = self.manifest_path().read_bytes()

        corrupt_conflict = b"{direct quarantine corruption"
        conflict_path.write_bytes(corrupt_conflict)
        unrelated = self.root.joinpath(
            ".bimri", "archive", "direct-quarantine-unrelated.bin"
        )
        unrelated.write_bytes(b"unrelated protected mismatch\n")

        # No doctor or authority write runs first. Quarantine itself must retain
        # the intact W0/M0 baseline before replacing the reviewed target.
        quarantined = self.cli(
            "quarantine-authority",
            "--kind",
            "conflict",
            "--id",
            conflict_id,
            "--human-approved",
        )

        self.assertIn("exact bytes preserved", quarantined.stdout)
        blocked_path = self.root / ".bimri" / "audit-blocked.json"
        self.assertTrue(blocked_path.is_file())
        self.assert_prior_witness_retained_but_invalid(
            self.root, witness_before
        )
        self.assertEqual(self.manifest_path().read_bytes(), manifest_before)
        self.assert_sealed_blocked_prior(self.root, witness_before)
        stub = json.loads(conflict_path.read_text("utf-8"))
        self.assertEqual(stub["record_type"], "authority-quarantine")
        self.assertEqual(
            self.root.joinpath(stub["recovery_file"]).read_bytes(),
            corrupt_conflict,
        )

        replacement = self.root / "reviewed-direct-conflict.json"
        replacement.write_bytes(valid_conflict)
        restored = self.cli(
            "restore-authority",
            "--kind",
            "conflict",
            "--id",
            conflict_id,
            "--from",
            replacement,
            "--human-approved",
            check=False,
        )

        self.assertEqual(restored.returncode, 2, restored.stdout + restored.stderr)
        self.assertIn(
            unrelated.name,
            restored.stdout + restored.stderr,
        )
        self.assertEqual(conflict_path.read_bytes(), valid_conflict)
        self.assertTrue(unrelated.is_file())
        self.assertTrue(blocked_path.is_file())
        self.assert_prior_witness_retained_but_invalid(
            self.root, witness_before
        )
        self.assert_sealed_blocked_prior(self.root, witness_before)

        gated = self.cli("get", "--key", "witness.seed", check=False)
        self.assertEqual(gated.returncode, 2)
        self.assertIn(
            "audit checkpoint blocked",
            (gated.stdout + gated.stderr).lower(),
        )
        audited = self.cli("doctor", "--read-only", check=False)
        self.assertEqual(audited.returncode, 1)
        self.assertRegex(
            (audited.stdout + audited.stderr).lower(),
            r"quarantine|blocked",
        )
        self.assertTrue(blocked_path.is_file())

        # The exit exists: remove the unrelated drift and the already-applied
        # restoration validates, clearing the owner-repair baseline.
        unrelated.unlink()
        retried = self.cli(
            "restore-authority",
            "--kind",
            "conflict",
            "--id",
            conflict_id,
            "--from",
            replacement,
            "--human-approved",
        )
        self.assertRegex(
            retried.stdout.lower(), r"already restored.*validated"
        )
        self.assertFalse(blocked_path.exists())
        healed = self.cli("get", "--key", "witness.seed")
        self.assertIn("witness.seed", healed.stdout)

    def test_blocked_restore_manifest_witness_crash_is_retryable(self):
        records = self.seed_authority_graph()
        conflict_path = records["conflicts"]
        conflict_id = conflict_path.stem
        valid_conflict = conflict_path.read_bytes()
        witness_before = self.witness_path().read_bytes()
        corrupt_conflict = b"{restore publication crash corruption"
        conflict_path.write_bytes(corrupt_conflict)
        blocked_path = self.root / ".bimri" / "audit-blocked.json"
        quarantined = self.cli(
            "quarantine-authority",
            "--kind",
            "conflict",
            "--id",
            conflict_id,
            "--human-approved",
        )
        self.assertIn("exact bytes preserved", quarantined.stdout)
        self.assertTrue(blocked_path.is_file())
        self.assert_prior_witness_retained_but_invalid(
            self.root, witness_before
        )
        self.assert_sealed_blocked_prior(self.root, witness_before)

        replacement = self.root / "reviewed-publication-crash-conflict.json"
        replacement.write_bytes(valid_conflict)
        crashed = self.worker(
            "blocked_restore_crash_between_manifest_and_witness",
            "restore-authority",
            "--kind",
            "conflict",
            "--id",
            conflict_id,
            "--from",
            replacement,
            "--human-approved",
            check=False,
        )

        self.assertEqual(crashed.returncode, 113, crashed.stdout + crashed.stderr)
        self.assertTrue(
            self.root.joinpath(".test-restore-manifest-published").is_file()
        )
        self.assertTrue(blocked_path.is_file())
        self.assert_prior_witness_retained_but_invalid(
            self.root, witness_before
        )
        self.assertEqual(conflict_path.read_bytes(), valid_conflict)
        receipts = list(
            self.root.joinpath(".bimri", "recovery").glob(
                f"authority-restore-conflict-{conflict_id}-*.json"
            )
        )
        self.assertEqual(len(receipts), 1)

        gated = self.cli("get", "--key", "witness.seed", check=False)
        self.assertEqual(gated.returncode, 2)
        self.assertIn(
            "audit checkpoint blocked",
            (gated.stdout + gated.stderr).lower(),
        )

        retried = self.cli(
            "restore-authority",
            "--kind",
            "conflict",
            "--id",
            conflict_id,
            "--from",
            replacement,
            "--human-approved",
        )
        self.assertRegex(
            retried.stdout.lower(), r"already restored.*validated"
        )
        self.assertFalse(blocked_path.exists())
        self.assertTrue(self.witness_path().is_file())
        audited = self.cli("doctor", "--read-only")
        self.assertIn("PASSED", audited.stdout)

    def test_corrupt_and_partial_witnesses_fall_back_and_rewrite(self):
        self.seed_authority_graph()
        for label, payload in (
            ("unparseable", b"{broken witness"),
            ("partial", b"{}\n"),
        ):
            with self.subTest(label=label):
                root = self.clone_store(f"witness-{label}")
                self.witness_path(root).write_bytes(payload)

                recalled = self.cli("get", "--key", "witness.seed", root=root)

                self.assertIn("witness.seed", recalled.stdout)
                repaired = self.witness(root)
                self.assertEqual(repaired["witness_schema"], 1)
                self.assertNotEqual(self.witness_path(root).read_bytes(), payload)

    def test_missing_or_invalid_checkpoint_write_requires_graph_replay(self):
        records = self.seed_authority_graph()
        for label, witness_bytes in (
            ("missing", None),
            ("invalid", b"{}\n"),
        ):
            with self.subTest(label=label):
                root = self.clone_store(f"checkpoint-{label}-write")
                if witness_bytes is None:
                    self.witness_path(root).unlink()
                else:
                    self.witness_path(root).write_bytes(witness_bytes)
                damaged = root / records["decisions"].relative_to(self.root)
                damaged.write_bytes(b"{broken authority decision")
                run_id = next(iter(self.state(root)["active_runs"]))
                proposals_before = set(
                    root.joinpath(".bimri", "proposals").glob("*.json")
                )

                result = self.worker(
                    "missing_checkpoint_require_graph_replay",
                    "propose",
                    "--run",
                    run_id,
                    "--operation",
                    "set",
                    "--tier",
                    "2",
                    "--key",
                    f"checkpoint.{label}.write",
                    "--text",
                    "Missing checkpoint must replay the authority graph.",
                    "--source",
                    "agent",
                    "--trust",
                    "working",
                    "--new-subject",
                    root=root,
                    check=False,
                )

                self.assertEqual(result.returncode, 2, result.stderr)
                self.assertNotIn(
                    "bypassed global graph replay", result.stderr
                )
                self.assertRegex(
                    (result.stdout + result.stderr).lower(),
                    r"authority|decision|json|recovery",
                )
                self.assertEqual(
                    set(root.joinpath(".bimri", "proposals").glob("*.json")),
                    proposals_before,
                )

    def test_engine_format_policy_and_head_witness_mismatches_reaudit(self):
        self.seed_authority_graph()
        baseline = self.witness()
        mutations = {
            "engine_version": "0.0.invalid-engine",
            "memory_format_version": "0.0.invalid-format",
            "policy_version": "invalid-policy-version",
            "head_revision": baseline["head_revision"] + 100,
            "head_hash": "0" * 64,
        }
        for field, invalid in mutations.items():
            with self.subTest(field=field):
                root = self.clone_store(f"mismatch-{field}")
                witness = self.witness(root)
                witness[field] = invalid
                self.witness_path(root).write_text(
                    json.dumps(witness, indent=2, sort_keys=True) + "\n",
                    encoding="utf-8",
                )

                recalled = self.cli("get", "--key", "witness.seed", root=root)

                self.assertIn("witness.seed", recalled.stdout)
                self.assertEqual(self.witness(root)[field], baseline[field])

    def test_legitimate_head_change_refreshes_head_binding(self):
        self.seed_authority_graph()
        before = self.witness()
        run_id = self.start("head-change")
        self.apply_set(
            run_id,
            "witness.seed",
            "Updated current value after a legitimate head change.",
        )

        recalled = self.cli("get", "--key", "witness.seed")
        after = self.witness()
        state = self.state()

        self.assertIn("Updated current value", recalled.stdout)
        self.assertGreater(after["head_revision"], before["head_revision"])
        self.assertNotEqual(after["head_hash"], before["head_hash"])
        self.assertEqual(after["head_revision"], state["head_revision"])
        self.assertEqual(after["head_hash"], state["head_hash"])

    def test_v511_accepts_v510_preflight_receipt_and_emits_v511_receipts(self):
        run_id = self.start("patch-compatibility-old")
        old_proposal = self.apply_set(
            run_id,
            "patch.compatibility-old",
            "Authority created by the prior patch engine remains readable.",
            new_subject=True,
        )
        old_path = (
            self.root / ".bimri" / "proposals" / f"{old_proposal}.json"
        )
        old_record = json.loads(old_path.read_text("utf-8"))
        self.assertEqual(old_record["bimri_version"], "5.1.0")
        self.assertEqual(
            old_record["preflight_receipt"]["engine_release"], "5.1.1"
        )
        self.witness_path().unlink(missing_ok=True)
        old_record["preflight_receipt"]["engine_release"] = "5.1.0"
        old_path.write_text(
            json.dumps(old_record, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )

        audited = self.cli("doctor", "--read-only")
        recalled = self.cli("get", "--key", "patch.compatibility-old")

        self.assertIn("BIMRI doctor (read-only): PASSED", audited.stdout)
        self.assertIn("patch.compatibility-old", recalled.stdout)
        self.assertEqual(self.state()["bimri_version"], "5.1.0")

        new_run = self.start("patch-compatibility-new")
        new_proposal = self.propose(
            new_run,
            "patch.compatibility-new",
            "New patch receipts identify the running engine release.",
            new_subject=True,
        )
        new_record = json.loads(
            self.root.joinpath(
                ".bimri", "proposals", f"{new_proposal}.json"
            ).read_text("utf-8")
        )
        self.assertEqual(new_record["bimri_version"], "5.1.0")
        self.assertEqual(
            new_record["preflight_receipt"]["engine_release"], "5.1.1"
        )

    def test_exact_current_parity_and_no_global_record_collection(self):
        self.seed_authority_graph()
        exact = self.cli("get", "--key", "witness.seed")
        historical = self.cli(
            "get", "--key", "witness.seed", "--history"
        )
        current_history_lines = [
            line
            for line in historical.stdout.splitlines()
            if line.startswith(("HOT\t", "COLD\t"))
        ]
        self.assertEqual(exact.stdout.splitlines(), current_history_lines)

        guarded = self.worker(
            "exact_recall_forbid_global_collection",
            "get",
            "--key",
            "witness.seed",
        )
        self.assertEqual(guarded.stdout, exact.stdout)

        cold_root = self.workspace / "cold-store"
        current = self.seed_cold_current(cold_root)
        cold_exact = self.cli("recall", "--key", "parity.cold", root=cold_root)
        cold_history = self.cli(
            "recall",
            "--key",
            "parity.cold",
            "--history",
            root=cold_root,
        )
        self.assertIn("COLD\tparity.cold", cold_exact.stdout)
        self.assertIn(current, cold_exact.stdout)
        cold_current_lines = [
            line
            for line in cold_history.stdout.splitlines()
            if line.startswith(("HOT\t", "COLD\t"))
        ]
        self.assertEqual(cold_exact.stdout.splitlines(), cold_current_lines)
        guarded_cold = self.worker(
            "exact_recall_forbid_global_collection",
            "recall",
            "--key",
            "parity.cold",
            root=cold_root,
        )
        self.assertEqual(guarded_cold.stdout, cold_exact.stdout)

    def test_start_and_no_proposal_close_crashes_reconcile_exactly(self):
        self.seed_authority_graph()
        active_before = set(self.state()["active_runs"])
        witness_before_start = self.witness_path().read_bytes()

        crashed_start = self.worker(
            "witness_crash_before_replace",
            "start",
            "--actor",
            "lifecycle-cutpoint-start",
            check=False,
        )

        self.assertEqual(
            crashed_start.returncode,
            110,
            crashed_start.stdout + crashed_start.stderr,
        )
        self.assertEqual(
            self.witness_path().read_bytes(), witness_before_start
        )
        state_after_start = self.state()
        new_runs = set(state_after_start["active_runs"]) - active_before
        self.assertEqual(len(new_runs), 1)
        run_id = new_runs.pop()
        transition = self.assert_sealed_transition(
            self.root, "lifecycle", "start", witness_before_start
        )
        self.assertEqual(transition["run_id"], run_id)

        recovered_start = self.cli("get", "--key", "witness.seed")
        self.assertIn("witness.seed", recovered_start.stdout)
        self.assertFalse(self.root.joinpath(TRANSITION_RELATIVE).exists())
        witness_after_start = self.witness_path().read_bytes()
        self.assertNotEqual(witness_after_start, witness_before_start)
        self.assert_steady_manifest_generation_bound()

        crashed_close = self.worker(
            "witness_crash_before_replace",
            "close",
            "--run",
            run_id,
            "--outcome",
            "success",
            "--summary",
            "No-proposal close interrupted before checkpoint publication.",
            check=False,
        )

        self.assertEqual(
            crashed_close.returncode,
            110,
            crashed_close.stdout + crashed_close.stderr,
        )
        self.assertNotIn(run_id, self.state()["active_runs"])
        self.assertEqual(
            self.witness_path().read_bytes(), witness_after_start
        )
        transition = self.assert_sealed_transition(
            self.root, "lifecycle", "close", witness_after_start
        )
        self.assertEqual(transition["run_id"], run_id)

        recovered_close = self.cli("get", "--key", "witness.seed")
        self.assertIn("witness.seed", recovered_close.stdout)
        self.assertFalse(self.root.joinpath(TRANSITION_RELATIVE).exists())
        self.assertNotEqual(
            self.witness_path().read_bytes(), witness_after_start
        )
        self.assert_steady_manifest_generation_bound()
        audited = self.cli("doctor", "--read-only")
        self.assertIn("PASSED", audited.stdout)

    def test_manifest_generations_reuse_content_and_remain_bounded(self):
        self.seed_authority_graph()
        initial = self.assert_steady_manifest_generation_bound()

        first_doctor = self.cli("doctor")
        self.assertIn("BIMRI doctor: PASSED", first_doctor.stdout)
        after_first = self.assert_steady_manifest_generation_bound()
        second_doctor = self.cli("doctor")
        self.assertIn("BIMRI doctor: PASSED", second_doctor.stdout)
        after_second = self.assert_steady_manifest_generation_bound()
        self.assertEqual(after_first["manifest_hash"], initial["manifest_hash"])
        self.assertEqual(after_second["manifest_hash"], initial["manifest_hash"])

        for number in range(3):
            run_id = self.start(f"manifest-generation-{number}")
            proposal = self.propose(
                run_id,
                f"manifest.generation.{number}",
                f"Authority generation {number}.",
                new_subject=True,
            )
            staged = self.assert_steady_manifest_generation_bound()
            self.assertNotEqual(staged["manifest_hash"], initial["manifest_hash"])
            self.cli("sync", "--run", run_id)
            decision = json.loads(
                self.root.joinpath(
                    ".bimri", "decisions", f"{proposal}.json"
                ).read_text("utf-8")
            )
            self.assertEqual(decision["outcome"], "accepted")
            self.assert_steady_manifest_generation_bound()

    def test_valid_witness_skips_full_audit_across_start_and_journals(self):
        self.seed_authority_graph()
        checkpoint_before = self.witness()
        checkpoint_before_bytes = self.witness_path().read_bytes()

        warm = self.worker(
            "valid_witness_forbid_full_audit",
            "get",
            "--key",
            "witness.seed",
        )
        self.assertIn("witness.seed", warm.stdout)

        started = self.worker(
            "valid_witness_forbid_full_audit",
            "start",
            "--actor",
            "witness-warm-lifecycle",
        )
        match = RUN_RE.search(started.stdout)
        self.assertIsNotNone(match, started.stdout)
        run_id = match.group(1)
        checkpoint_after_start = self.witness()
        checkpoint_after_start_bytes = self.witness_path().read_bytes()
        self.assertNotEqual(checkpoint_after_start_bytes, checkpoint_before_bytes)
        self.assertFalse(
            self.root.joinpath(TRANSITION_RELATIVE).exists()
        )
        for number in (1, 2):
            journaled = self.worker(
                "valid_witness_forbid_full_audit",
                "journal",
                "--run",
                run_id,
                "--importance",
                "3",
                "--text",
                f"Warm witnessed journal entry {number}.",
            )
            self.assertRegex(journaled.stdout, rf"{run_id}-E\d{{3}}")
            self.assertEqual(
                self.witness_path().read_bytes(), checkpoint_after_start_bytes
            )
        closed = self.worker(
            "valid_witness_forbid_full_audit",
            "close",
            "--run",
            run_id,
            "--outcome",
            "success",
            "--summary",
            "Lifecycle-only close stays on the compact checkpoint path.",
        )
        self.assertIn(f"run {run_id.lower()} closed", closed.stdout.lower())
        checkpoint_after = self.witness()
        for field in (
            "state_hash",
            "run_authority_hash",
            "manifest_hash",
            "manifest_count",
            "proposal_runs",
            "audit_epoch",
        ):
            self.assertEqual(
                checkpoint_after[field], checkpoint_before[field], field
            )
        self.assertNotEqual(
            self.witness_path().read_bytes(), checkpoint_after_start_bytes
        )
        self.assertEqual(
            checkpoint_after["audit_epoch"],
            self.state().get("_audit_epoch", 0),
        )
        self.assertFalse(self.root.joinpath(TRANSITION_RELATIVE).exists())

    def test_external_log_marker_is_deferred_to_doctor(self):
        self.seed_authority_graph()
        witness_before = self.witness_path().read_bytes()
        state = self.state()
        run_id = next(iter(state["active_runs"]))
        log = self.root / ".bimri" / "log" / f"{run_id}.md"
        with log.open("a", encoding="utf-8", newline="\n") as handle:
            handle.write(
                "\n[PROPOSE:R999999-Q001] [set] [K:forged.marker] "
                "[BASE:absent] Externally forged proposal marker.\n"
            )

        recalled = self.cli(
            "get", "--key", "witness.seed"
        )

        self.assertIn("witness.seed", recalled.stdout)
        self.assertEqual(
            self.witness_path().read_bytes(), witness_before
        )
        self.assert_doctor_rejects_external_drift(self.root)

    def test_unlogged_crash_window_invalidates_on_close_or_base_advance(self):
        seed_run = self.start("crash-window-seed")
        self.apply_set(
            seed_run,
            "crash-window.anchor",
            "Current value used to probe exact-read invalidation.",
            new_subject=True,
        )
        crash_run = self.start("crash-window-proposer")
        proposal = self.propose(
            crash_run,
            "crash-window.pending",
            "Proposal file created immediately before its log marker.",
            new_subject=True,
        )
        log = self.root / ".bimri" / "log" / f"{crash_run}.md"
        marker = f"[PROPOSE:{proposal}]"
        log.write_text(
            "\n".join(
                line
                for line in log.read_text("utf-8").splitlines()
                if marker not in line
            )
            + "\n",
            encoding="utf-8",
        )
        self.witness_path().unlink(missing_ok=True)

        established = self.cli("get", "--key", "crash-window.anchor")
        self.assertIn("crash-window.anchor", established.stdout)
        self.witness()

        closed_root = self.clone_store("crash-window-closed")
        closed_witness = self.witness_path(closed_root).read_bytes()
        closed_log = (
            closed_root / ".bimri" / "log" / f"{crash_run}.md"
        )
        with closed_log.open("a", encoding="utf-8", newline="\n") as handle:
            handle.write(f"[CLOSED:{crash_run} 2026-08-22T00:00:00Z]\n")
        closed_state_path = closed_root / ".bimri" / "state.json"
        closed_state = json.loads(closed_state_path.read_text("utf-8"))
        closed_state["active_runs"].pop(crash_run)
        closed_state_path.write_text(
            json.dumps(closed_state, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        closed_read = self.cli(
            "get",
            "--key",
            "crash-window.anchor",
            root=closed_root,
            check=False,
        )
        self.assertEqual(closed_read.returncode, 2)
        self.assertIn(
            f"decision {proposal} is missing",
            closed_read.stdout + closed_read.stderr,
        )
        self.assertEqual(
            self.witness_path(closed_root).read_bytes(), closed_witness
        )

        advanced_root = self.clone_store("crash-window-advanced")
        writer = self.start("crash-window-head-writer", root=advanced_root)
        self.apply_set(
            writer,
            "crash-window.unrelated",
            "Unrelated commit advances the accepted head.",
            root=advanced_root,
            new_subject=True,
        )
        advanced_witness = self.witness_path(advanced_root).read_bytes()
        state_path = advanced_root / ".bimri" / "state.json"
        state = json.loads(state_path.read_text("utf-8"))
        state["active_runs"][crash_run]["base_revision"] = state["head_revision"]
        state_path.write_text(
            json.dumps(state, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        advanced_read = self.cli(
            "get",
            "--key",
            "crash-window.anchor",
            root=advanced_root,
            check=False,
        )
        self.assertEqual(advanced_read.returncode, 2)
        self.assertIn(
            f"decision {proposal} is missing",
            advanced_read.stdout + advanced_read.stderr,
        )
        self.assertEqual(
            self.witness_path(advanced_root).read_bytes(), advanced_witness
        )

    def test_history_and_held_corpus_remains_retrievable_on_history_path(self):
        owner_run = self.start("parity-owner")
        self.apply_set(
            owner_run,
            "parity.held",
            "Owner-confirmed current value.",
            new_subject=True,
            source="user",
            trust="confirmed",
        )
        agent_run = self.start("parity-agent")
        self.apply_set(
            agent_run,
            "parity.held",
            "Unconfirmed agent candidate remains held.",
        )
        held_get = self.cli("get", "--key", "parity.held", "--history")
        held_recall = self.cli(
            "recall", "--key", "parity.held", "--history"
        )
        self.assertEqual(held_get.stdout, held_recall.stdout)
        self.assertIn("HOT\tparity.held", held_get.stdout)
        self.assertIn("HELD\tparity.held", held_get.stdout)

        history_run = self.start("parity-history")
        self.apply_set(
            history_run,
            "parity.history-only",
            "Value closed into immutable history.",
            new_subject=True,
        )
        close_proposal = self.propose(
            history_run,
            "parity.history-only",
            "",
            operation="close",
        )
        self.cli("sync", "--run", history_run)
        decision = json.loads(
            self.root.joinpath(
                ".bimri", "decisions", f"{close_proposal}.json"
            ).read_text("utf-8")
        )
        self.assertEqual(decision["outcome"], "accepted")
        exact_miss = self.cli(
            "get", "--key", "parity.history-only", check=False
        )
        history_hit = self.cli(
            "get", "--key", "parity.history-only", "--history"
        )
        self.assertEqual(exact_miss.returncode, 1)
        self.assertIn("HISTORY\tparity.history-only", history_hit.stdout)

    def test_warm_exact_defers_out_of_band_damage_but_head_damage_refuses(self):
        records = self.seed_authority_graph()
        authority_root = self.clone_store("damage-authority")
        witness_before = self.witness_path(authority_root).read_bytes()
        target = authority_root / records["proposals"].relative_to(self.root)
        target.write_bytes(b"{broken authority json")

        exact = self.cli(
            "get",
            "--key",
            "witness.seed",
            root=authority_root,
        )

        self.assertIn("witness.seed", exact.stdout)
        self.assertEqual(
            self.witness_path(authority_root).read_bytes(), witness_before
        )
        self.assert_doctor_rejects_external_drift(authority_root)

        head_root = self.clone_store("damage-head")
        state = self.state(head_root)
        head_path = head_root.joinpath(
            ".bimri", "revisions", f"V{state['head_revision']:06d}.md"
        )
        head_path.write_bytes(b"corrupted accepted head\n")
        head_read = self.cli(
            "get",
            "--key",
            "witness.seed",
            root=head_root,
            check=False,
        )

        self.assertEqual(head_read.returncode, 2)
        self.assertRegex(
            (head_read.stdout + head_read.stderr).lower(),
            r"head|hash|revision|memory",
        )

        state_root = self.clone_store("damage-state")
        state_path = state_root / ".bimri" / "state.json"
        state = json.loads(state_path.read_text("utf-8"))
        state["hot_max_bytes"] += 1
        state_path.write_text(
            json.dumps(state, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        state_read = self.cli(
            "get",
            "--key",
            "witness.seed",
            root=state_root,
        )
        # A grammar-valid state edit is a cache miss: the full audit accepts
        # the coherent store, the divergence is receipted, and the read
        # serves. Head-content damage above still refuses outright.
        self.assertIn("witness.seed", state_read.stdout)
        self.assert_drift_receipt(state_root, pattern=r"state")
        self.assert_no_audit_block(state_root)

    def test_kill_before_witness_replace_leaves_store_valid_and_recoverable(self):
        self.seed_authority_graph()
        state_before = self.root.joinpath(".bimri", "state.json").read_bytes()
        hot_before = self.root.joinpath("bimri.md").read_bytes()
        revisions_before = {
            path.name: path.read_bytes()
            for path in self.root.joinpath(".bimri", "revisions").glob("V*.md")
        }
        self.witness_path().unlink()

        crashed = self.worker(
            "witness_crash_before_replace",
            "get",
            "--key",
            "witness.seed",
            check=False,
        )

        self.assertEqual(crashed.returncode, 110, crashed.stdout + crashed.stderr)
        self.assertFalse(self.witness_path().exists())
        self.assertEqual(
            self.root.joinpath(".bimri", "state.json").read_bytes(), state_before
        )
        self.assertEqual(self.root.joinpath("bimri.md").read_bytes(), hot_before)
        self.assertEqual(
            {
                path.name: path.read_bytes()
                for path in self.root.joinpath(".bimri", "revisions").glob("V*.md")
            },
            revisions_before,
        )

        recovered = self.cli("get", "--key", "witness.seed")
        self.assertIn("witness.seed", recovered.stdout)
        self.assertEqual(self.witness()["witness_schema"], 1)

    def test_commit_crash_with_later_drift_recovers_and_receipts(self):
        seed_run = self.start("commit-cutpoint-seed")
        self.apply_set(
            seed_run,
            "commit-cutpoint.anchor",
            "Stable value before the interrupted authority commit.",
            new_subject=True,
        )
        commit_run = self.start("commit-cutpoint-writer")
        proposal = self.propose(
            commit_run,
            "commit-cutpoint.current",
            "Accepted value whose checkpoint publication is interrupted.",
            new_subject=True,
        )
        witness_before = self.witness_path().read_bytes()
        manifest_before = self.manifest_path().read_bytes()
        head_before = self.state()["head_revision"]

        crashed = self.worker(
            "witness_crash_before_replace",
            "sync",
            "--run",
            commit_run,
            check=False,
        )

        self.assertEqual(crashed.returncode, 110, crashed.stdout + crashed.stderr)
        self.assert_prior_witness_retained_but_invalid(
            self.root, witness_before
        )
        transition = self.assert_sealed_transition(
            self.root, "authority", "sync", witness_before
        )
        prior_manifest = json.loads(manifest_before.decode("utf-8"))
        self.assertEqual(
            transition["prior_manifest_hash"],
            prior_manifest["manifest_hash"],
        )
        state_after = self.state()
        self.assertGreater(state_after["head_revision"], head_before)
        decision = json.loads(
            self.root.joinpath(
                ".bimri", "decisions", f"{proposal}.json"
            ).read_text("utf-8")
        )
        self.assertEqual(decision["outcome"], "accepted")
        self.assertIn(
            "commit-cutpoint.current",
            self.root.joinpath("bimri.md").read_text("utf-8"),
        )

        unrelated = self.root.joinpath(
            ".bimri", "archive", "post-commit-crash-drift.bin"
        )
        unrelated.write_bytes(b"external drift after interrupted commit\n")
        recovered = self.cli("get", "--key", "commit-cutpoint.current")

        self.assertIn("commit-cutpoint.current", recovered.stdout)
        self.assertFalse(self.root.joinpath(TRANSITION_RELATIVE).exists())
        self.assert_no_audit_block()
        self.assert_drift_receipt(
            pattern=r"post-commit-crash-drift|inventory"
        )
        self.assertTrue(unrelated.is_file())
        audited = self.cli("doctor", "--read-only")
        self.assertIn("PASSED", audited.stdout)
        self.assertIn("unexplained-drift receipt", audited.stdout)

    def test_commit_crash_before_new_checkpoint_recovers_clean_scoped_delta(self):
        seed_run = self.start("clean-commit-cutpoint-seed")
        self.apply_set(
            seed_run,
            "clean-commit.anchor",
            "Stable value before a clean interrupted authority commit.",
            new_subject=True,
        )
        commit_run = self.start("clean-commit-cutpoint-writer")
        current_text = "Accepted clean value recovered from the scoped delta."
        proposal = self.propose(
            commit_run,
            "clean-commit.current",
            current_text,
            new_subject=True,
        )
        witness_before = self.witness_path().read_bytes()
        head_before = self.state()["head_revision"]

        crashed = self.worker(
            "witness_crash_before_replace",
            "sync",
            "--run",
            commit_run,
            check=False,
        )

        self.assertEqual(crashed.returncode, 110, crashed.stdout + crashed.stderr)
        self.assert_prior_witness_retained_but_invalid(
            self.root, witness_before
        )
        transition = self.assert_sealed_transition(
            self.root, "authority", "sync", witness_before
        )
        self.assertEqual(transition["run_id"], commit_run)
        self.assertGreater(self.state()["head_revision"], head_before)
        decision = json.loads(
            self.root.joinpath(
                ".bimri", "decisions", f"{proposal}.json"
            ).read_text("utf-8")
        )
        self.assertEqual(decision["outcome"], "accepted")

        recovered = self.cli("get", "--key", "clean-commit.current")

        self.assertIn("clean-commit.current", recovered.stdout)
        self.assertIn(current_text, recovered.stdout)
        self.assertFalse(self.root.joinpath(TRANSITION_RELATIVE).exists())
        self.assertFalse(
            self.root.joinpath(".bimri", "audit-blocked.json").exists()
        )
        self.assertNotEqual(self.witness_path().read_bytes(), witness_before)
        checkpoint = self.assert_steady_manifest_generation_bound()
        self.assertEqual(
            checkpoint["audit_epoch"], self.state().get("_audit_epoch", 0)
        )
        audited = self.cli("doctor", "--read-only")
        self.assertIn("PASSED", audited.stdout)

    def test_post_crash_proposal_injection_is_evidenced_not_latched(self):
        seed_run = self.start("frozen-scope-seed")
        self.apply_set(
            seed_run,
            "frozen-scope.anchor",
            "Stable current value before the scoped sync.",
            new_subject=True,
        )
        commit_run = self.start("frozen-scope-writer")
        scoped = self.propose(
            commit_run,
            "frozen-scope.accepted",
            "The only proposal present when sync begins.",
            new_subject=True,
        )
        log_path = self.root / ".bimri" / "log" / f"{commit_run}.md"
        log_before_extra = log_path.read_bytes()
        donor = self.clone_store("post-crash-proposal-donor")
        injected = self.propose(
            commit_run,
            "frozen-scope.injected",
            "Valid same-run proposal created outside the frozen transition.",
            root=donor,
            new_subject=True,
        )
        donor_log = donor / ".bimri" / "log" / f"{commit_run}.md"
        donor_log_bytes = donor_log.read_bytes()
        self.assertTrue(donor_log_bytes.startswith(log_before_extra))
        injected_log_append = donor_log_bytes[len(log_before_extra):]
        injected_bytes = donor.joinpath(
            ".bimri", "proposals", f"{injected}.json"
        ).read_bytes()
        witness_before = self.witness_path().read_bytes()

        crashed = self.worker(
            "witness_crash_before_replace",
            "sync",
            "--run",
            commit_run,
            check=False,
        )
        self.assertEqual(crashed.returncode, 110, crashed.stdout + crashed.stderr)
        transition = self.assert_sealed_transition(
            self.root, "authority", "sync", witness_before
        )
        self.assertIn(scoped, json.dumps(transition["scope"], sort_keys=True))
        self.assertNotIn(injected, json.dumps(transition["scope"], sort_keys=True))
        interrupted = self.clone_store("post-crash-proposal-base")

        for include_marker in (False, True):
            with self.subTest(include_marker=include_marker):
                label = "marker" if include_marker else "file-only"
                root = self.workspace / f"post-crash-proposal-{label}"
                shutil.copytree(interrupted, root)
                root.joinpath(
                    ".bimri", "proposals", f"{injected}.json"
                ).write_bytes(injected_bytes)
                if include_marker:
                    with root.joinpath(
                        ".bimri", "log", f"{commit_run}.md"
                    ).open("ab") as handle:
                        handle.write(injected_log_append)

                gated = self.cli(
                    "get",
                    "--key",
                    "frozen-scope.accepted",
                    root=root,
                    check=False,
                )

                # Both variants stay semantically refused, with evidence and
                # without a latch: the file-only injection lacks its log
                # lineage, and the with-marker injection carries a durably
                # logged proposal that no decision accounts for.
                self.assertEqual(
                    gated.returncode, 2, gated.stdout + gated.stderr
                )
                self.assertNotIn("frozen-scope.accepted", gated.stdout)
                self.assertRegex(
                    (gated.stdout + gated.stderr).lower(),
                    r"scope|transition|proposal|decision|authority|audit|log",
                )
                self.assertFalse(
                    root.joinpath(
                        ".bimri", "decisions", f"{injected}.json"
                    ).exists()
                )
                self.assert_no_audit_block(root)
                self.assert_drift_receipt(root)

    def test_unscoped_head_advance_is_evidenced_not_latched(self):
        seed_run = self.start("head-scope-seed")
        self.apply_set(
            seed_run,
            "head-scope.anchor",
            "Stable current value before the scoped head advance.",
            new_subject=True,
        )
        commit_run = self.start("head-scope-writer")
        proposal = self.propose(
            commit_run,
            "head-scope.accepted",
            "Accepted value inside the frozen sync effect.",
            new_subject=True,
        )
        witness_before = self.witness_path().read_bytes()

        crashed = self.worker(
            "witness_crash_before_replace",
            "sync",
            "--run",
            commit_run,
            check=False,
        )
        self.assertEqual(crashed.returncode, 110, crashed.stdout + crashed.stderr)
        self.assertEqual(
            json.loads(
                self.root.joinpath(
                    ".bimri", "decisions", f"{proposal}.json"
                ).read_text("utf-8")
            )["outcome"],
            "accepted",
        )
        self.assert_sealed_transition(
            self.root, "authority", "sync", witness_before
        )

        state_path = self.root / ".bimri" / "state.json"
        state = json.loads(state_path.read_text("utf-8"))
        scoped_head = state["head_revision"]
        scoped_head_path = self.root.joinpath(
            ".bimri", "revisions", f"V{scoped_head:06d}.md"
        )
        forged_head_bytes = scoped_head_path.read_bytes()
        forged_revision = scoped_head + 1
        self.root.joinpath(
            ".bimri", "revisions", f"V{forged_revision:06d}.md"
        ).write_bytes(forged_head_bytes)
        state["head_revision"] = forged_revision
        state["head_hash"] = hashlib.sha256(forged_head_bytes).hexdigest()
        state["last_revision_reason"] = (
            "unscoped syntactically valid post-crash head advance"
        )
        state_path.write_text(
            json.dumps(state, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )

        # A grammar-coherent hand advance is inside the documented local
        # boundary (a coherent rollback or advance is undetectable from the
        # store alone). v5.1.0 served it silently; the difference now is
        # the drift receipt naming the divergent state and added revision.
        recovered = self.cli("get", "--key", "head-scope.accepted")

        self.assertIn("head-scope.accepted", recovered.stdout)
        self.assert_no_audit_block()
        self.assert_drift_receipt(pattern=r"revision|state|head|added")
        audited = self.cli("doctor", "--read-only")
        self.assertIn("PASSED", audited.stdout)
        self.assertIn("unexplained-drift receipt", audited.stdout)

    def test_commit_recovery_completes_across_same_month_archive_append(self):
        stable_run = self.start("archive-scope-stable")
        self.apply_set(
            stable_run,
            "archive-scope.stable",
            "Stable current value used to probe archive recovery.",
            new_subject=True,
        )
        target_run = self.start("archive-scope-target")
        self.apply_set(
            target_run,
            "archive-scope.target",
            "Current value legitimately closed by the scoped sync.",
            new_subject=True,
        )
        close_run = self.start("archive-scope-closer")
        close_proposal = self.propose(
            close_run,
            "archive-scope.target",
            "",
            operation="close",
        )
        witness_before = self.witness_path().read_bytes()

        crashed = self.worker(
            "witness_crash_before_replace",
            "sync",
            "--run",
            close_run,
            check=False,
        )
        self.assertEqual(crashed.returncode, 110, crashed.stdout + crashed.stderr)
        self.assertEqual(
            json.loads(
                self.root.joinpath(
                    ".bimri", "decisions", f"{close_proposal}.json"
                ).read_text("utf-8")
            )["outcome"],
            "accepted",
        )
        self.assert_sealed_transition(
            self.root, "authority", "sync", witness_before
        )
        matching_archives = [
            path
            for path in self.root.joinpath(".bimri", "archive").glob("*.md")
            if f"[BY:{close_proposal}]" in path.read_text("utf-8")
        ]
        self.assertEqual(len(matching_archives), 1)
        archive_path = matching_archives[0]
        forged_append = (
            f"[ARCHIVED:2026-08-22] [BY:{close_proposal}] [closed] "
            "[T2][K:archive-scope.injected][I:3][S:agent][Q:working] "
            "Unrelated same-month archive append.\n"
        )
        with archive_path.open("a", encoding="utf-8", newline="\n") as handle:
            handle.write(forged_append)

        recovered = self.cli("get", "--key", "archive-scope.stable")

        self.assertIn("archive-scope.stable", recovered.stdout)
        # This forged row impersonates the interrupted operation's own
        # [BY:] stamp inside its crash window, which is the narrowed
        # documented boundary: append-shaped rows carrying the scoped stamp
        # are byte-provably indistinguishable from the operation's own
        # effect. Anything without the scoped stamp is drift evidence (see
        # test_unscoped_archive_edit_in_crash_window_is_receipted).
        self.assertIn(forged_append, archive_path.read_text("utf-8"))
        self.assert_no_audit_block()

        warmed = self.cli("get", "--key", "archive-scope.stable")
        self.assertIn("archive-scope.stable", warmed.stdout)
        self.assertFalse(self.root.joinpath(TRANSITION_RELATIVE).exists())
        audited = self.cli("doctor", "--read-only")
        self.assertIn("PASSED", audited.stdout)

    def test_commit_recovery_completes_across_unrelated_closed_run_fact(self):
        unrelated_run = self.start("run-fact-scope-unrelated")
        self.apply_set(
            unrelated_run,
            "run-fact-scope.anchor",
            "Accepted proposal keeps this unrelated run authority-bearing.",
            new_subject=True,
        )
        commit_run = self.start("run-fact-scope-writer")
        proposal = self.propose(
            commit_run,
            "run-fact-scope.accepted",
            "Accepted value inside the interrupted scoped sync.",
            new_subject=True,
        )
        witness_before = self.witness_path().read_bytes()

        crashed = self.worker(
            "witness_crash_before_replace",
            "sync",
            "--run",
            commit_run,
            check=False,
        )
        self.assertEqual(crashed.returncode, 110, crashed.stdout + crashed.stderr)
        self.assertEqual(
            json.loads(
                self.root.joinpath(
                    ".bimri", "decisions", f"{proposal}.json"
                ).read_text("utf-8")
            )["outcome"],
            "accepted",
        )
        self.assert_sealed_transition(
            self.root, "authority", "sync", witness_before
        )
        unrelated_log = self.root.joinpath(
            ".bimri", "log", f"{unrelated_run}.md"
        )
        with unrelated_log.open("a", encoding="utf-8", newline="\n") as handle:
            handle.write(
                f"\n[CLOSED:{unrelated_run} 2026-08-22T00:00:00Z]\n"
            )

        recovered = self.cli("get", "--key", "run-fact-scope.accepted")

        self.assertIn("run-fact-scope.accepted", recovered.stdout)
        self.assert_no_audit_block()
        warmed = self.cli("get", "--key", "run-fact-scope.accepted")
        self.assertIn("run-fact-scope.accepted", warmed.stdout)
        self.assertFalse(self.root.joinpath(TRANSITION_RELATIVE).exists())
        audited = self.cli("doctor", "--read-only")
        self.assertIn("PASSED", audited.stdout)

    def test_failed_resolution_discards_witness_and_requires_recovery(self):
        seed_run = self.start("failed-resolution-seed")
        self.apply_set(
            seed_run,
            "resolution.anchor",
            "Unrelated current value used to probe the recovery gate.",
            new_subject=True,
        )
        candidate_run = self.start("failed-resolution-candidate")
        writer_run = self.start("failed-resolution-writer")
        candidate = self.propose(
            candidate_run,
            "resolution.failure",
            "Candidate whose forced application will fail.",
            new_subject=True,
        )
        self.propose(
            writer_run,
            "resolution.failure",
            "Accepted writer value before the forced failure.",
            new_subject=True,
        )
        self.cli("sync", "--run", writer_run)
        self.cli("sync", "--run", candidate_run)
        decision = json.loads(
            self.root.joinpath(
                ".bimri", "decisions", f"{candidate}.json"
            ).read_text("utf-8")
        )
        self.assertEqual(decision["outcome"], "contested")
        conflict_id = decision["conflict_id"]
        self.cli("get", "--key", "resolution.anchor")
        witness_before = self.witness_path().read_bytes()

        failed = self.worker(
            "resolution_fail_during_force_apply",
            "resolve",
            conflict_id,
            "--choose",
            candidate,
            "--human-approved",
            check=False,
        )

        self.assertEqual(failed.returncode, 2, failed.stdout + failed.stderr)
        resolution = json.loads(
            self.root.joinpath(
                ".bimri", "resolutions", f"{conflict_id}.json"
            ).read_text("utf-8")
        )
        self.assertEqual(resolution["status"], "failed")
        self.assert_prior_witness_retained_but_invalid(
            self.root, witness_before
        )

        gated = self.cli(
            "get", "--key", "resolution.anchor", check=False
        )
        self.assertEqual(gated.returncode, 2)
        self.assertIn(
            "authority recovery",
            (gated.stdout + gated.stderr).lower(),
        )
        self.assert_prior_witness_retained_but_invalid(
            self.root, witness_before
        )

    def test_replayed_pre_failure_witness_cannot_bypass_failed_resolution(self):
        seed_run = self.start("replayed-witness-seed")
        self.apply_set(
            seed_run,
            "replayed-witness.anchor",
            "Unrelated current value used to probe stale verdict replay.",
            new_subject=True,
        )
        candidate_run = self.start("replayed-witness-candidate")
        writer_run = self.start("replayed-witness-writer")
        candidate = self.propose(
            candidate_run,
            "replayed-witness.conflict",
            "Candidate whose forced application will fail.",
            new_subject=True,
        )
        self.propose(
            writer_run,
            "replayed-witness.conflict",
            "Accepted writer value before the forced failure.",
            new_subject=True,
        )
        self.cli("sync", "--run", writer_run)
        self.cli("sync", "--run", candidate_run)
        decision = json.loads(
            self.root.joinpath(
                ".bimri", "decisions", f"{candidate}.json"
            ).read_text("utf-8")
        )
        self.assertEqual(decision["outcome"], "contested")
        conflict_id = decision["conflict_id"]
        self.cli("get", "--key", "replayed-witness.anchor")
        witness_before = self.witness_path().read_bytes()
        head_before = (
            self.state()["head_revision"],
            self.state()["head_hash"],
            self.root.joinpath("bimri.md").read_bytes(),
        )

        failed = self.worker(
            "resolution_fail_during_force_apply",
            "resolve",
            conflict_id,
            "--choose",
            candidate,
            "--human-approved",
            check=False,
        )

        self.assertEqual(failed.returncode, 2, failed.stdout + failed.stderr)
        resolution = json.loads(
            self.root.joinpath(
                ".bimri", "resolutions", f"{conflict_id}.json"
            ).read_text("utf-8")
        )
        self.assertEqual(resolution["status"], "failed")
        self.assert_prior_witness_retained_but_invalid(
            self.root, witness_before
        )
        self.assertEqual(
            (
                self.state()["head_revision"],
                self.state()["head_hash"],
                self.root.joinpath("bimri.md").read_bytes(),
            ),
            head_before,
        )

        # This is cache-only replay. Authority, state, and the accepted current
        # view remain exactly as the failed command left them.
        self.witness_path().unlink()
        self.witness_path().write_bytes(witness_before)
        gated = self.cli(
            "get", "--key", "replayed-witness.anchor", check=False
        )

        self.assertEqual(gated.returncode, 2, gated.stdout + gated.stderr)
        self.assertNotIn("replayed-witness.anchor", gated.stdout)
        self.assertRegex(
            (gated.stdout + gated.stderr).lower(),
            r"authority recovery|checkpoint|audit|unsettled|failed",
        )

    def test_two_eight_and_thirty_two_readers_wait_for_one_writer(self):
        self.seed_authority_graph()
        run_id = self.start("concurrent-writer")
        signal = self.root / ".test-writer-lock-held"
        release = self.root / ".test-release-writer-lock"

        for readers in (2, 8, 32):
            with self.subTest(readers=readers):
                signal.unlink(missing_ok=True)
                release.unlink(missing_ok=True)
                writer_command = [
                    sys.executable,
                    str(CRASH_WORKER),
                    str(ENGINE),
                    "journal_hold_lock",
                    str(self.root),
                    "journal",
                    "--run",
                    run_id,
                    "--importance",
                    "3",
                    "--text",
                    f"Concurrent writer beside {readers} exact readers.",
                ]
                writer = subprocess.Popen(
                    writer_command,
                    text=True,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                )
                deadline = time.monotonic() + 10
                while not signal.exists() and writer.poll() is None:
                    if time.monotonic() >= deadline:
                        writer.kill()
                        self.fail("writer never reached the held-lock boundary")
                    time.sleep(0.005)
                self.assertIsNone(writer.poll(), "writer exited before readers started")

                processes = []
                for _index in range(readers):
                    processes.append(
                        subprocess.Popen(
                            [
                                sys.executable,
                                str(ENGINE),
                                "--root",
                                str(self.root),
                                "get",
                                "--key",
                                "witness.seed",
                            ],
                            text=True,
                            stdout=subprocess.PIPE,
                            stderr=subprocess.PIPE,
                        )
                    )
                # Readers must actually wait: while the writer holds the
                # exclusive lock, no reader may complete.
                time.sleep(0.6)
                waiting = [process.poll() for process in processes]
                self.assertTrue(
                    all(code is None for code in waiting),
                    f"a reader finished while the writer held the lock: "
                    f"{waiting}",
                )
                release.write_text("release writer\n", encoding="utf-8")
                writer_stdout, writer_stderr = writer.communicate(timeout=30)
                self.assertEqual(
                    writer.returncode,
                    0,
                    writer_stdout + writer_stderr,
                )
                for process in processes:
                    stdout, stderr = process.communicate(timeout=30)
                    self.assertEqual(process.returncode, 0, stdout + stderr)
                    self.assertIn("witness.seed", stdout)

    def test_cold_archive_corruption_behind_cold_current_key_refuses(self):
        cold_root = self.workspace / "cold-corruption-store"
        self.seed_cold_current(cold_root)
        warm = self.cli("get", "--key", "parity.cold", root=cold_root)
        self.assertIn("parity.cold", warm.stdout)
        matching = [
            path
            for path in cold_root.joinpath(".bimri", "archive").glob("*.md")
            if "[K:parity.cold]" in path.read_text("utf-8")
        ]
        self.assertEqual(len(matching), 1)
        archive_path = matching[0]
        corrupted = archive_path.read_text("utf-8").replace(
            "[K:parity.cold]", "[K:parity.cold-x]"
        )
        archive_path.write_text(corrupted, encoding="utf-8")

        gated = self.cli(
            "get", "--key", "parity.cold", root=cold_root, check=False
        )
        self.assertNotEqual(gated.returncode, 0, gated.stdout)
        self.assertRegex(
            (gated.stdout + gated.stderr).lower(),
            r"archive|cold|binding|pointer|authority|audit",
        )

    def test_same_drifted_store_exact_stays_warm_history_audits_and_receipts(self):
        self.seed_authority_graph()
        witness_before = self.witness_path().read_bytes()
        stray = self.root.joinpath(".bimri", "archive", "drift-pair-probe.bin")
        stray.write_bytes(b"at-rest drift for the read-boundary pair\n")

        exact = self.cli("get", "--key", "witness.seed")
        self.assertIn("witness.seed", exact.stdout)
        self.assertEqual(self.witness_path().read_bytes(), witness_before)
        self.assertEqual(self.drift_receipts(), [])

        history = self.cli("recall", "--key", "witness.seed", "--history")
        self.assertIn("witness.seed", history.stdout)
        self.assert_drift_receipt(pattern=r"drift-pair-probe|inventory")
        self.assertNotEqual(self.witness_path().read_bytes(), witness_before)

        rewarmed = self.cli("get", "--key", "witness.seed")
        self.assertIn("witness.seed", rewarmed.stdout)

    def test_marker_path_obstruction_is_a_hard_error_never_false_health(self):
        self.seed_authority_graph()
        witness_before = self.witness_path().read_bytes()
        obstruction = self.root / ".bimri" / "audit-transition.json"
        obstruction.mkdir()

        doctored = self.cli("doctor", check=False)
        self.assertEqual(
            doctored.returncode, 2, doctored.stdout + doctored.stderr
        )
        self.assertIn(
            "obstruction", (doctored.stdout + doctored.stderr).lower()
        )
        self.assertTrue(obstruction.is_dir())
        self.assertEqual(self.witness_path().read_bytes(), witness_before)

        gated = self.cli("get", "--key", "witness.seed", check=False)
        self.assertEqual(gated.returncode, 2)

        obstruction.rmdir()
        repaired = self.cli("doctor")
        self.assertIn("BIMRI doctor: PASSED", repaired.stdout)
        recalled = self.cli("get", "--key", "witness.seed")
        self.assertIn("witness.seed", recalled.stdout)

    def test_unscoped_archive_edit_in_crash_window_is_receipted(self):
        stable_run = self.start("archive-drift-stable")
        self.apply_set(
            stable_run,
            "archive-drift.stable",
            "Stable value used to probe unscoped archive drift.",
            new_subject=True,
        )
        target_run = self.start("archive-drift-target")
        self.apply_set(
            target_run,
            "archive-drift.target",
            "Value legitimately closed by the interrupted sync.",
            new_subject=True,
        )
        close_run = self.start("archive-drift-closer")
        self.propose(
            close_run,
            "archive-drift.target",
            "",
            operation="close",
        )

        crashed = self.worker(
            "witness_crash_before_replace",
            "sync",
            "--run",
            close_run,
            check=False,
        )
        self.assertEqual(
            crashed.returncode, 110, crashed.stdout + crashed.stderr
        )
        archive_files = sorted(
            self.root.joinpath(".bimri", "archive").glob("*.md")
        )
        self.assertTrue(archive_files)
        foreign = (
            "[ARCHIVED:2026-08-22] [BY:R999999-Q999] [closed] "
            "[T2][K:archive-drift.foreign][I:3][S:agent][Q:working] "
            "Foreign row without a scoped stamp.\n"
        )
        with archive_files[0].open("a", encoding="utf-8", newline="\n") as f:
            f.write(foreign)

        recovered = self.cli("get", "--key", "archive-drift.stable")
        self.assertIn("archive-drift.stable", recovered.stdout)
        # The foreign row is preserved and evidenced, never silently
        # baselined as the operation's own effect.
        self.assertIn(foreign, archive_files[0].read_text("utf-8"))
        self.assert_no_audit_block()
        self.assert_drift_receipt(pattern=r"archive|inventory")
        # The recovery receipt is self-contained: the drifted month is
        # sealed with its hashes — changed with prior and current when the
        # month pre-existed, added with its current hash when the crash
        # window created it.
        record = json.loads(self.drift_receipts()[-1].read_text("utf-8"))
        self.assertIsNotNone(record["delta"])
        month_relative = archive_files[0].relative_to(self.root).as_posix()
        sealed = {
            item["path"]: item
            for section in ("changed", "added")
            for item in record["delta"][section]
        }
        self.assertIn(month_relative, sealed)
        self.assertRegex(sealed[month_relative]["sha256"], r"^[0-9a-f]{64}$")
        if "prior_sha256" in sealed[month_relative]:
            self.assertRegex(
                sealed[month_relative]["prior_sha256"], r"^[0-9a-f]{64}$"
            )

    def test_read_paths_refuse_unclaimed_legacy_roots(self):
        self.seed_authority_graph()
        legacy = self.root / "BIMRI-backup.md"
        legacy.write_text("# legacy rolling backup\n", encoding="utf-8")

        gated = self.cli("get", "--key", "witness.seed", check=False)
        self.assertEqual(gated.returncode, 2, gated.stdout + gated.stderr)
        self.assertIn(
            "unclaimed legacy", (gated.stdout + gated.stderr).lower()
        )
        audited = self.cli("doctor", "--read-only", check=False)
        self.assertNotEqual(audited.returncode, 0)
        self.assertIn(
            "unclaimed legacy", (audited.stdout + audited.stderr).lower()
        )

        legacy.unlink()
        recalled = self.cli("get", "--key", "witness.seed")
        self.assertIn("witness.seed", recalled.stdout)

    def test_drift_receipt_seals_every_diverging_path_with_hashes(self):
        records = self.seed_authority_graph()
        strays = []
        for index in range(5):
            stray = self.root / ".bimri" / "archive" / f"stray-{index}.bin"
            stray.write_bytes(f"stray {index}\n".encode())
            strays.append(stray)
        edited = records["decisions"]
        edited_relative = edited.relative_to(self.root).as_posix()
        prior_hash = hashlib.sha256(edited.read_bytes()).hexdigest()
        edited.write_bytes(edited.read_bytes() + b"\n")
        current_hash = hashlib.sha256(edited.read_bytes()).hexdigest()

        repaired = self.cli("doctor")
        self.assertIn("BIMRI doctor: PASSED", repaired.stdout)
        receipts = self.assert_drift_receipt()
        record = json.loads(receipts[-1].read_text("utf-8"))
        self.assertEqual(record["sequence"], 1)
        self.assertTrue(record["receipt_hash"])
        self.assertEqual(record["truncated"], {})
        added_paths = {item["path"] for item in record["delta"]["added"]}
        for stray in strays:
            self.assertIn(
                stray.relative_to(self.root).as_posix(), added_paths
            )
        for item in record["delta"]["added"]:
            self.assertRegex(item["sha256"], r"^[0-9a-f]{64}$")
        # Changed paths seal both sides, so the receipt stays meaningful
        # after the prior manifest generation is pruned.
        changed = {
            item["path"]: item for item in record["delta"]["changed"]
        }
        self.assertIn(edited_relative, changed)
        self.assertEqual(changed[edited_relative]["prior_sha256"], prior_hash)
        self.assertEqual(changed[edited_relative]["sha256"], current_hash)

    def test_obstructed_receipt_sink_blocks_rebaseline(self):
        self.seed_authority_graph()
        witness_before = self.witness_path().read_bytes()
        self.root.joinpath(".bimri", "archive", "sink-probe.bin").write_bytes(
            b"drift needing a receipt\n"
        )
        sink = self.root / ".bimri" / "audit-drift"
        self.assertFalse(sink.exists())
        sink.write_text("obstruction\n", encoding="utf-8")

        blocked = self.cli("doctor", check=False)
        self.assertEqual(
            blocked.returncode, 1, blocked.stdout + blocked.stderr
        )
        self.assertIn(
            "receipt", (blocked.stdout + blocked.stderr).lower()
        )
        self.assertEqual(self.witness_path().read_bytes(), witness_before)

        sink.unlink()
        repaired = self.cli("doctor")
        self.assertIn("BIMRI doctor: PASSED", repaired.stdout)
        self.assert_drift_receipt(pattern=r"sink-probe|inventory")
        self.assertNotEqual(self.witness_path().read_bytes(), witness_before)

    def test_tampered_receipt_is_reported_damaged_not_trusted(self):
        self.seed_authority_graph()
        self.root.joinpath(".bimri", "archive", "seal-probe.bin").write_bytes(
            b"drift for a receipt\n"
        )
        self.cli("doctor")
        receipt_path = self.drift_receipts()[-1]
        record = json.loads(receipt_path.read_text("utf-8"))
        record["reasons"] = ["forged reason the doctor must never trust"]
        record["receipt_hash"] = "0" * 64
        receipt_path.write_text(
            json.dumps(record, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )

        audited = self.cli("doctor", "--read-only")
        self.assertIn("PASSED", audited.stdout)
        self.assertIn("failed validation", audited.stdout)
        self.assertNotIn(
            "forged reason the doctor must never trust", audited.stdout
        )

    def test_truncated_receipt_pins_its_complete_delta_attachment(self):
        self.seed_authority_graph()
        archive = self.root / ".bimri" / "archive"
        for index in range(2001):
            archive.joinpath(f"mass-{index:04d}.bin").write_bytes(b"x\n")

        repaired = self.cli("doctor")
        self.assertIn("BIMRI doctor: PASSED", repaired.stdout)
        self.assertIn("truncated", repaired.stdout)
        record = json.loads(self.drift_receipts()[-1].read_text("utf-8"))
        self.assertEqual(record["truncated"], {"added": 1})
        self.assertEqual(len(record["delta"]["added"]), 2000)
        attachment = next(
            item for item in record["attachments"]
            if item["role"] == "complete-delta"
        )
        target = self.root.joinpath(*attachment["path"].split("/"))
        raw = target.read_bytes()
        self.assertEqual(len(raw), attachment["bytes"])
        self.assertEqual(
            hashlib.sha256(raw).hexdigest(), attachment["sha256"]
        )
        complete = json.loads(raw.decode("utf-8"))["complete_delta"]
        self.assertEqual(len(complete["added"]), 2001)
        # The attachment survives later publications and pruning while its
        # receipt is retained.
        probe_run = self.start("prune-probe")
        self.apply_set(
            probe_run, "prune.probe", "prune trigger", new_subject=True
        )
        self.assertTrue(target.is_file())
        # Deleting the pinned attachment turns the receipt into damaged
        # evidence, loudly, instead of a silent completeness claim.
        target.unlink()
        audited = self.cli("doctor", "--read-only")
        self.assertIn("PASSED", audited.stdout)
        self.assertIn("failed validation", audited.stdout)

    def test_invalid_precrash_receipt_is_not_reused_for_publication(self):
        self.seed_authority_graph()
        witness_before = self.witness_path().read_bytes()
        self.root.joinpath(".bimri", "archive", "dedup-probe.bin").write_bytes(
            b"drift before the crash\n"
        )
        crashed = self.worker(
            "witness_crash_before_replace", "doctor", check=False
        )
        self.assertEqual(
            crashed.returncode, 110, crashed.stdout + crashed.stderr
        )
        receipts = self.drift_receipts()
        self.assertEqual(len(receipts), 1)
        record = json.loads(receipts[0].read_text("utf-8"))
        record["receipt_hash"] = "0" * 64
        receipts[0].write_text(
            json.dumps(record, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )

        repaired = self.cli("doctor")
        self.assertIn("BIMRI doctor: PASSED", repaired.stdout)
        self.assertIn("failed validation", repaired.stdout)
        receipts = self.drift_receipts()
        self.assertEqual(len(receipts), 2)
        fresh = json.loads(receipts[-1].read_text("utf-8"))
        self.assertEqual(fresh["sequence"], 2)
        self.assertNotEqual(self.witness_path().read_bytes(), witness_before)

    def test_missing_prior_manifest_evidence_refuses_rebaseline(self):
        self.seed_authority_graph()
        witness_before = self.witness_path().read_bytes()
        self.manifest_path().unlink()
        for generation in self.root.joinpath(
            ".bimri", "audit-manifests"
        ).glob("*.json"):
            generation.unlink()
        self.root.joinpath(
            ".bimri", "archive", "null-delta-probe.bin"
        ).write_bytes(b"drift with no prior evidence\n")

        blocked = self.cli("doctor", check=False)
        self.assertEqual(
            blocked.returncode, 1, blocked.stdout + blocked.stderr
        )
        self.assertIn(
            "manifest evidence", (blocked.stdout + blocked.stderr).lower()
        )
        self.assertEqual(self.witness_path().read_bytes(), witness_before)

        # The documented exit: removing the derived witness rebuilds trust
        # from the full audit.
        self.witness_path().unlink()
        repaired = self.cli("doctor")
        self.assertIn("BIMRI doctor: PASSED", repaired.stdout)
        recalled = self.cli("get", "--key", "witness.seed")
        self.assertIn("witness.seed", recalled.stdout)

    def test_sequence_grammar_survives_the_millionth_receipt(self):
        self.seed_authority_graph()
        drift_dir = self.root / ".bimri" / "audit-drift"
        drift_dir.mkdir()
        drift_dir.joinpath("D999999-seed.json").write_text(
            "{}", encoding="utf-8"
        )
        self.root.joinpath(
            ".bimri", "archive", "million-probe.bin"
        ).write_bytes(b"drift at the sequence boundary\n")

        repaired = self.cli("doctor")
        self.assertIn("BIMRI doctor: PASSED", repaired.stdout)
        receipts = self.drift_receipts()
        self.assertEqual(len(receipts), 1)
        self.assertTrue(receipts[-1].name.startswith("D1000000-"))
        record = json.loads(receipts[-1].read_text("utf-8"))
        self.assertEqual(record["sequence"], 1000000)
        probe_run = self.start("million-prune-probe")
        self.apply_set(
            probe_run, "million.probe", "prune trigger", new_subject=True
        )
        self.assertTrue(receipts[-1].is_file())

    def test_missing_preserved_blob_marks_receipt_damaged(self):
        self.seed_authority_graph()
        marker = self.root / ".bimri" / "audit-transition.json"
        marker.write_text("{corrupt marker bytes", encoding="utf-8")
        healed = self.cli("start", "--actor", "marker-heal")
        self.assertEqual(healed.returncode, 0)
        record = json.loads(self.drift_receipts()[-1].read_text("utf-8"))
        blob_relative = next(
            item["path"] for item in record["attachments"]
            if item["role"] == "corrupt-transition-marker"
        )
        blob = self.root.joinpath(*blob_relative.split("/"))
        self.assertTrue(blob.is_file())
        blob.unlink()

        audited = self.cli("doctor", "--read-only")
        self.assertIn("PASSED", audited.stdout)
        self.assertIn("failed validation", audited.stdout)

    def test_interrupted_recovery_refuses_without_prior_evidence(self):
        stable_run = self.start("evidence-stable")
        self.apply_set(
            stable_run, "evidence.stable", "Stable value.", new_subject=True
        )
        close_run = self.start("evidence-closer")
        self.propose(close_run, "evidence.stable", "", operation="close")
        crashed = self.worker(
            "witness_crash_before_replace",
            "sync",
            "--run",
            close_run,
            check=False,
        )
        self.assertEqual(
            crashed.returncode, 110, crashed.stdout + crashed.stderr
        )
        months = sorted(self.root.joinpath(".bimri", "archive").glob("*.md"))
        with months[0].open("a", encoding="utf-8", newline="\n") as handle:
            handle.write(
                "[ARCHIVED:2026-08-22] [BY:R999999-Q999] [closed] "
                "[T2][K:evidence.foreign][I:3][S:agent][Q:working] Foreign.\n"
            )
        witness_before = self.witness_path().read_bytes()
        self.manifest_path().unlink()
        for generation in self.root.joinpath(
            ".bimri", "audit-manifests"
        ).glob("*.json"):
            generation.unlink()

        blocked = self.cli(
            "start", "--actor", "evidence-probe", check=False
        )
        self.assertEqual(
            blocked.returncode, 2, blocked.stdout + blocked.stderr
        )
        self.assertIn(
            "manifest evidence", (blocked.stdout + blocked.stderr).lower()
        )
        # Nothing was blindly retired: the marker and prior checkpoint
        # both survive the refusal.
        self.assertTrue(self.root.joinpath(TRANSITION_RELATIVE).exists())
        self.assertEqual(self.witness_path().read_bytes(), witness_before)

        # The documented exit resets the derived trust artifacts. The
        # evidence-invalid refusal clears; whatever the forged row itself
        # implies is the normal recovery story, not this condition.
        self.witness_path().unlink()
        self.root.joinpath(TRANSITION_RELATIVE).unlink()
        reset = self.cli("doctor", check=False)
        self.assertNotIn(
            "manifest evidence", (reset.stdout + reset.stderr).lower()
        )

    def test_marker_survives_until_its_evidence_is_durable(self):
        self.seed_authority_graph()
        marker = self.root / ".bimri" / "audit-transition.json"
        marker.write_text("{corrupt marker bytes", encoding="utf-8")
        sink = self.root / ".bimri" / "audit-drift"
        sink.write_text("obstruction\n", encoding="utf-8")

        blocked = self.cli("start", "--actor", "retire-order", check=False)
        self.assertEqual(
            blocked.returncode, 2, blocked.stdout + blocked.stderr
        )
        self.assertTrue(marker.is_file())

        sink.unlink()
        healed = self.cli("start", "--actor", "retire-order-2")
        self.assertEqual(healed.returncode, 0)
        self.assertFalse(marker.exists())
        record = json.loads(self.drift_receipts()[-1].read_text("utf-8"))
        self.assertTrue(any(
            item["role"] == "corrupt-transition-marker"
            for item in record["attachments"]
        ))

    def test_unknown_preflight_receipt_release_is_refused(self):
        self.seed_authority_graph()
        run_id = self.start("receipt-reject")
        proposal = self.propose(
            run_id,
            "receipt.reject.probe",
            "Reject-side coverage for unknown engine releases.",
            new_subject=True,
        )
        proposal_path = self.root.joinpath(
            ".bimri", "proposals", f"{proposal}.json"
        )
        record = json.loads(proposal_path.read_text("utf-8"))
        record["preflight_receipt"]["engine_release"] = "9.9.9"
        proposal_path.write_text(
            json.dumps(record, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )

        synced = self.cli("sync", "--run", run_id, check=False)
        self.assertNotEqual(synced.returncode, 0)
        self.assertRegex(
            (synced.stdout + synced.stderr).lower(),
            r"receipt|proposal|invalid|engine",
        )
        self.assertFalse(
            self.root.joinpath(
                ".bimri", "decisions", f"{proposal}.json"
            ).exists()
        )
        self.assert_no_audit_block()

    def test_console_output_is_utf8_under_an_ansi_code_page(self):
        # Memory text is UTF-8 on disk. A piped stdout on Windows defaulted to
        # the ANSI code page, so reading an entry that carried a character
        # outside it died in print() with exit 2. Forcing the legacy encoding
        # through the environment reproduces that host on every platform.
        run_id = self.start("utf8")
        text = "Tiếng Việt ✓ 日本語 → em dash — check"
        self.apply_set(run_id, "utf8.subject", text, new_subject=True)
        environment = dict(os.environ, PYTHONIOENCODING="cp1252", PYTHONUTF8="0")
        for arguments in (
            ("recall", "--key", "utf8.subject"),
            ("get", "--key", "utf8.subject"),
            ("recall", "--key", "utf8.subject", "--history"),
            ("recall", "--query", "em dash"),
        ):
            result = subprocess.run(
                [sys.executable, str(ENGINE), "--root", str(self.root), *arguments],
                capture_output=True,
                env=environment,
                timeout=120,
            )
            stdout = result.stdout.decode("utf-8", "replace")
            stderr = result.stderr.decode("utf-8", "replace")
            self.assertEqual(result.returncode, 0, stdout + stderr)
            self.assertIn(text, stdout, arguments)

    def test_missing_lock_read_refusal_names_the_repair(self):
        run_id = self.start("lock")
        self.apply_set(
            run_id,
            "lock.subject",
            "Value read after the lock file vanishes.",
            new_subject=True,
        )
        lock = self.root / ".bimri" / "engine.lock"
        lock.unlink()
        refused = self.cli("get", "--key", "lock.subject", check=False)
        self.assertNotEqual(refused.returncode, 0)
        self.assertIn(
            "recreates a missing lock file", refused.stderr + refused.stdout
        )
        self.cli("status")
        self.assertTrue(lock.is_file())
        served = self.cli("get", "--key", "lock.subject")
        self.assertIn("lock.subject", served.stdout)


class V511ReceiptUnitTest(unittest.TestCase):
    """Narrow unit-level checks on the receipt store itself.

    These import the engine directly — the same seam crash_worker.py uses —
    because fabricating hundreds of sealed receipts or distinct event
    bindings through the CLI would take minutes per case.
    """

    def setUp(self):
        spec = importlib.util.spec_from_file_location(
            "bimri_receipt_unit", ENGINE
        )
        self.engine = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.engine)
        self._temp = tempfile.TemporaryDirectory(
            prefix="bimri-receipt-unit-"
        )
        self.paths = self.engine.Paths(Path(self._temp.name))
        self.paths.bdir.mkdir()

    def tearDown(self):
        self._temp.cleanup()

    def test_prune_window_holds_across_the_millionth_sequence(self):
        drift = self.paths.audit_drift
        drift.mkdir()
        drift.joinpath("D999895-seed.json").write_text(
            "{}", encoding="utf-8"
        )
        for index in range(210):
            written = self.engine.write_audit_drift_receipt(
                self.paths, [f"unit drift {index}"]
            )
            self.assertIsNotNone(written, f"receipt {index} failed")
        receipts = self.engine.list_audit_drift_receipts(self.paths)
        self.assertEqual(len(receipts), self.engine.AUDIT_DRIFT_KEEP)
        sequences = [
            self.engine.parse_audit_drift_sequence(path.name)
            for path in receipts
        ]
        self.assertEqual(sequences, sorted(sequences))
        self.assertEqual(len(sequences), len(set(sequences)))
        # The window crossed the six-digit boundary: the newest receipts
        # carry seven-digit sequences and the pruned ones were the lowest
        # sequences, never the widest names.
        self.assertGreater(sequences[-1], 1000000)
        self.assertEqual(sequences[0], 999896 + 10)

    def test_dedup_is_bound_to_the_event_not_just_the_content(self):
        state_a = {
            "head_revision": 1, "head_hash": "a" * 64, "_audit_epoch": 0,
        }
        state_b = {
            "head_revision": 2, "head_hash": "b" * 64, "_audit_epoch": 0,
        }
        first = self.engine.write_audit_drift_receipt(
            self.paths, ["same drift"], state=state_a
        )
        self.assertIsNotNone(first)
        repeat = self.engine.write_audit_drift_receipt(
            self.paths, ["same drift"], state=state_a
        )
        self.assertEqual(repeat, first)
        other_event = self.engine.write_audit_drift_receipt(
            self.paths, ["same drift"], state=state_b
        )
        self.assertIsNotNone(other_event)
        self.assertNotEqual(other_event, first)


if __name__ == "__main__":
    unittest.main()
