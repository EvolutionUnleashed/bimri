"""Report caching must preserve audit results and never reach authoring."""

import contextlib
import importlib.util
import io
import json
import re
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock


ENGINE = Path(__file__).resolve().parents[1] / "bimri-engine.py"


class DiagnosticPerformanceTest(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="bimri-report-")
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        spec = importlib.util.spec_from_file_location("bimri_reports", ENGINE)
        self.engine = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.engine)
        self.paths = self.engine.Paths(self.root)

    def cli(self, *arguments):
        result = subprocess.run(
            [sys.executable, str(ENGINE), "--root", str(self.root), *arguments],
            capture_output=True, text=True, encoding="utf-8", timeout=60,
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        return result.stdout

    def start(self, actor):
        return re.search(r"BIMRI RUN HANDLE: (R\d+)", self.cli(
            "start", "--open-run", "--actor", actor
        )).group(1)

    def propose(self, run, text):
        output = self.cli(
            "propose", "--run", run, "--tier", "2", "--new-subject",
            "--key", "report.subject", "--source", "user",
            "--trust", "confirmed", "--text", text,
        )
        return re.search(r"R\d+-Q\d+", output).group(0)

    def contested_store(self):
        first = self.start("report-first")
        second = self.start("report-second")
        self.propose(first, "First value.")
        candidate = self.propose(second, "Second value.")
        self.cli("sync", "--run", first)
        self.cli("sync", "--run", second)
        return first, candidate

    def report(self, command, cached=True):
        output = io.StringIO()
        scope = self.engine.diagnostic_read_scope if cached else (
            lambda paths: contextlib.nullcontext()
        )
        with mock.patch.object(self.engine, "diagnostic_read_scope", scope):
            with contextlib.redirect_stdout(output):
                if command == "review":
                    self.engine.cmd_review(self.paths, show_all=True)
                else:
                    self.engine.cmd_status(self.paths)
        self.assertIsNone(self.paths.diagnostic_read_cache)
        return output.getvalue()

    def test_cached_reports_equal_uncached_full_audit(self):
        self.contested_store()
        # v5.2 status no longer audits (D5.1), so the cache comparison is
        # review's alone; status must leave the checkpoint bytes untouched.
        witness = self.root / ".bimri" / "audit-witness.json"
        for command in ("review",):
            with self.subTest(command=command):
                witness_before = witness.read_bytes()
                uncached = self.report(command, cached=False)
                cached = self.report(command)
                self.assertEqual(cached, uncached)
                # v5.2 (build step 8): review audits a captured copy
                # inside diagnostic_read_scope and never publishes.
                self.assertEqual(witness.read_bytes(), witness_before)
        witness_bytes = self.paths.audit_witness.read_bytes()
        self.assertEqual(self.report("status"), self.report("status"))
        self.assertEqual(self.paths.audit_witness.read_bytes(), witness_bytes)

    def test_historical_revisions_validated_once_per_report(self):
        self.contested_store()
        validate = self.engine.validate_hot_content
        with mock.patch.object(self.engine, "validate_hot_content", wraps=validate) as calls:
            self.report("review", cached=False)
            uncached = calls.call_count
        with mock.patch.object(self.engine, "validate_hot_content", wraps=validate) as calls:
            self.report("review")
            cached = calls.call_count
        self.assertLess(cached, uncached / 2)

    def test_cold_historical_satisfaction_matches_uncached_report(self):
        from tests import test_v510_lifecycle

        # Reuse the lifecycle suite's pressure-cooled fixture rather than
        # synthesizing archive or decision records for this regression.
        fixture = test_v510_lifecycle.V510LifecycleTest()
        fixture.root = self.root
        key = "report.cold-subject"
        fixture.seed_cold_current(self.root, key=key)
        first, _ = fixture.start("cold-report-first")
        second, _ = fixture.start("cold-report-second")
        candidate_text = "Candidate cold value. " + "c" * 300
        fixture.propose_set(first, key, "Other cold value. " + "a" * 300)
        fixture.propose_set(second, key, candidate_text)
        fixture.cli("sync", "--run", first)
        fixture.cli("sync", "--run", second)
        satisfying, _ = fixture.start("cold-report-satisfying")
        fixture.apply_set(satisfying, key, candidate_text)
        self.assertIn(key, fixture.cold_current())
        # Satisfied historical candidates are review's distinction only;
        # v5.2 status counts the facts generation's held list (D5.1).
        for command in ("review",):
            with self.subTest(command=command):
                uncached = self.report(command, cached=False)
                cached = self.report(command)
                self.assertIn("Satisfied historical candidates: 1", cached)
                self.assertEqual(cached, uncached)

    def test_report_cache_does_not_hide_later_authority_corruption(self):
        first, proposal_id = self.contested_store()
        self.report("review")
        path = self.paths.proposals / (proposal_id + ".json")
        proposal = json.loads(path.read_text(encoding="utf-8"))
        proposal["base_hash"] = "0" * 64
        # Intentional adversarial corruption in this disposable test store.
        path.write_text(json.dumps(proposal), encoding="utf-8")
        self.assertIn("AUTHORITY RECOVERY NEEDED", self.report("review"))
        # v5.2 (build step 3, D4.5): a checkpoint-scoped write re-validates
        # the records it depends on, so the write that must refuse is the
        # one whose run holds the corrupted proposal (the other run's sync
        # depends on none of it; the deep audit, as review shows, does).
        corrupted_run = proposal_id.split("-Q", 1)[0]
        result = subprocess.run(
            [sys.executable, str(ENGINE), "--root", str(self.root),
             "sync", "--run", corrupted_run],
            capture_output=True, text=True, encoding="utf-8", timeout=60,
        )
        self.assertNotEqual(result.returncode, 0)
        del first

    def test_cached_entries_cannot_be_changed_by_the_reader(self):
        self.contested_store()
        with self.engine.existing_store_lock(self.paths):
            state = self.engine.load_current_state_read_only(self.paths)
            with self.engine.diagnostic_read_scope(self.paths):
                entries = self.engine.authority_revision_entries(
                    self.paths, state, state["head_revision"], "first read"
                )
                entries[0]["text"] = "Reader annotation must not become truth."
                reread = self.engine.authority_revision_entries(
                    self.paths, state, state["head_revision"], "second read"
                )
                self.assertNotEqual(entries[0]["text"], reread[0]["text"])

    def test_authority_write_is_refused_in_diagnostic_scope(self):
        self.start("report-write-guard")
        with self.engine.existing_store_lock(self.paths):
            state = self.engine.load_current_state_read_only(self.paths)
            with self.assertRaisesRegex(self.engine.BimriError, "authority writes"):
                with self.engine.diagnostic_read_scope(self.paths):
                    self.engine.begin_authority_write(self.paths, state)
            # v5.2 (build step 3): the checkpoint-scoped write boundary
            # refuses a diagnostic scope before it reads any evidence.
            with self.assertRaisesRegex(self.engine.BimriError, "authority writes"):
                with self.engine.diagnostic_read_scope(self.paths):
                    self.engine.begin_scoped_authority_write(
                        self.paths, state, "propose"
                    )
        self.assertIsNone(self.paths.diagnostic_read_cache)


if __name__ == "__main__":
    unittest.main()
