"""Regression tests for owner-authorised recovery of stranded legacy runs."""
import json
import shutil

from test_v520_step2 import V520Step2Base


class LegacyCleanupTest(V520Step2Base):
    def test_process_loss_during_owner_cleanup_never_applies_a_pending_change(self):
        run, pid = self.legacy_run("interrupted", "review.interrupted")
        other, other_pid = self.legacy_run("independent", "review.independent")
        args = ["recover-run", "--run", run, "--abandon", "--human-approved",
                "--summary", "Owner closed this thread."]
        cuts = []
        for number in range(1, 40):
            root = self.workspace / ("cut-" + str(number))
            shutil.copytree(self.root, root)
            result = self.worker("save_crash_after_write_" + str(number), *args, root=root)
            if result.returncode == 0:
                break
            self.assertEqual(result.returncode, 150, result.stdout + result.stderr)
            cuts.append((root / ".test-save-crash-point").read_text("utf-8"))
            self.cli("status", root=root)
            state_file = root / ".bimri" / "state.json"
            if run in json.loads(state_file.read_text("utf-8"))["active_runs"]:
                self.cli(*args, root=root)
            state = json.loads(state_file.read_text("utf-8"))
            self.assertIn(other, state["active_runs"], number)
            self.assertNotIn(run, state["active_runs"], number)
            self.assertEqual((root / "bimri.md").read_bytes(), (self.root / "bimri.md").read_bytes())
            decisions = root / ".bimri" / "decisions"
            self.assertFalse((decisions / (other_pid + ".json")).exists())
            self.assertEqual(json.loads((decisions / (pid + ".json")).read_text("utf-8"))["outcome"],
                             "abandoned")
        else:
            self.fail("cleanup exceeded its interruption-test boundary budget")
        self.assertTrue(any("decisions" in point for point in cuts), cuts)

    def test_cross_month_retry_of_a_legacy_close_uses_its_original_archive(self):
        root = self.root
        seed = self.save("seed-month", "review.month", self.key_token("review.month"),
                         "Original value.", new_subject=True, json_out=True)
        self.assertEqual(seed.returncode, 0, seed.stderr)
        run = self.start("month-retry")
        self.propose(run, "review.month", "Replacement.")
        interrupted = self.worker("proposal_crash_after_applying_decision", "close", "--run", run,
                                  "--outcome", "success", "--summary", "Interrupted.")
        self.assertNotEqual(interrupted.returncode, 0, interrupted.stdout)
        marker_file = root / ".bimri" / "audit-transition.json"
        marker = json.loads(marker_file.read_text(encoding="utf-8"))
        stamp = marker["created_at"][:7]
        before = set((root / ".bimri" / "audit-drift").glob("*.json"))
        result = self.worker("clock_plus_months_1", "sync", "--run", run)
        self.assertEqual(result.returncode, 0, result.stderr)
        archive = root / ".bimri" / "archive" / (stamp + ".md")
        self.assertIn("[BY:" + run + "-Q001]", archive.read_text(encoding="utf-8"))
        audit = self.worker("clock_plus_months_1", "doctor", "--live")
        self.assertEqual(audit.returncode, 0, audit.stdout + audit.stderr)
        self.assertEqual(before, set((root / ".bimri" / "audit-drift").glob("*.json")))

    def legacy_run(self, actor, key):
        run = self.start(actor)
        proposal = self.propose(run, key, "Pending owner correction.", new_subject=True)
        return run, proposal.stdout.strip().splitlines()[-1]

    def snapshot(self):
        return {p.relative_to(self.root): p.read_bytes() for p in self.root.rglob("*")
                if p.is_file() and p.name != "engine.lock"}

    def test_preview_and_refusal_leave_every_byte_intact(self):
        run, proposal = self.legacy_run("preview", "review.preview")
        command = ["recover-run", "--run", run, "--summary", "Reviewed.", "--abandon"]
        before = self.snapshot()
        self.assertIn(proposal, self.cli(*command, "--dry-run").stdout)
        self.assertIn("--human-approved", self.cli(*command, check=False).stderr)
        self.assertEqual(before, self.snapshot())

    def test_abandon_closes_only_selected_run_and_preserves_the_head(self):
        run, proposal = self.legacy_run("discard", "review.discard")
        other, other_proposal = self.legacy_run("keep", "review.keep")
        bdir = self.root / ".bimri"
        head = self.root.joinpath("bimri.md").read_bytes()
        self.cli("recover-run", "--run", run, "--summary", "No longer wanted.",
                 "--abandon", "--human-approved")
        state = json.loads((bdir / "state.json").read_text("utf-8"))
        self.assertNotIn(run, state["active_runs"])
        self.assertIn(other, state["active_runs"])
        self.assertFalse((bdir / "decisions" / (other_proposal + ".json")).exists())
        decision = json.loads((bdir / "decisions" / (proposal + ".json")).read_text("utf-8"))
        self.assertEqual(decision["outcome"], "abandoned")
        self.assertEqual(decision["summary"], "No longer wanted.")
        self.assertEqual(head, self.root.joinpath("bimri.md").read_bytes())
        self.assertIn("PASSED", self.cli("doctor", "--live").stdout)

    def test_cleanup_refuses_a_missing_legacy_proposal_without_closing_the_run(self):
        run, pid = self.legacy_run("missing-proposal", "review.missing")
        (self.bdir() / "proposals" / (pid + ".json")).unlink()
        before = self.snapshot()
        for flag in ("--dry-run", "--human-approved"):
            result = self.cli("recover-run", "--run", run, "--abandon", flag,
                              "--summary", "Review missing record.", check=False)
            self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
            self.assertIn(pid, result.stderr)
            self.assertEqual(self.snapshot(), before)

    def test_bulk_recovery_cannot_apply_pending_work(self):
        self.legacy_run("pending", "review.bulk")
        before = self.snapshot()
        result = self.cli("recover-run", "--all-orphans", "--summary", "review", check=False)
        self.assertEqual(2, result.returncode)
        self.assertEqual(before, self.snapshot())
