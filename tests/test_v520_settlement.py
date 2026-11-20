"""Held-candidate settlement preserves evidence and the owner's authority."""
import json
import shutil

from test_v520_step2 import AGENT_REWRITE, OWNER_KEY, V520Step2Base


class SettlementTest(V520Step2Base):
    def test_compacted_retry_keeps_original_observation_after_another_correction(self):
        pid = self.stage_held()
        original_token = self.key_token(OWNER_KEY)
        args = self.save_arguments("first-owner", OWNER_KEY, original_token, AGENT_REWRITE,
                                   extra=["--source", "user", "--trust", "confirmed",
                                          "--settles", pid], json_out=True)
        first = json.loads(self.cli(*args).stdout)
        later = self.save("second-owner", OWNER_KEY, first["token"], "A subsequent correction.",
                          extra=["--source", "user", "--trust", "confirmed"], json_out=True)
        self.assertEqual(later.returncode, 0, later.stderr)
        self.assertNotEqual(json.loads(later.stdout)["token"], first["token"])
        moved = self.worker("clock_plus_days_8", "maintain")
        self.assertEqual(moved.returncode, 0, moved.stderr)
        retry = self.worker("clock_plus_days_8", *args)
        self.assertEqual(retry.returncode, 0, retry.stderr)
        replayed = json.loads(retry.stdout)
        for field in ("token", "revision", "settled", "cooled", "decision", "result", "proposal"):
            self.assertEqual(replayed[field], first[field], field)

    def stage_held(self):
        result = self.save("held", OWNER_KEY, self.key_token(OWNER_KEY), AGENT_REWRITE,
                           json_out=True)
        self.assertEqual(result.returncode, 1, result.stderr)
        return json.loads(result.stdout)["proposal"]

    def test_dismiss_requires_owner_and_preserves_history(self):
        pid = self.stage_held()
        decision = self.bdir() / "decisions" / (pid + ".json")
        original = decision.read_bytes()
        args = ["dismiss-held", pid, "--summary", "Owner declined this change."]
        self.assertEqual(self.cli(*args, check=False).returncode, 2)
        self.cli(*args, "--human-approved")
        self.assertIn("already settled", self.cli(*args, "--human-approved").stdout)
        self.assertEqual(decision.read_bytes(), original)
        self.assertNotIn("held " + pid, self.cli("status").stdout)
        history = self.cli("recall", "--key", OWNER_KEY, "--history", "--json")
        row = next(r for r in json.loads(history.stdout)["matches"] if r["id"] == pid)
        self.assertEqual(row["location"], "HISTORY")
        self.assertEqual(row["reason"], "settled by owner-dismissed")
        self.assertIn("PASSED", self.cli("doctor", "--live").stdout)

    def test_confirmed_adoption_and_idempotent_retry(self):
        pid = self.stage_held()
        before = self.key_token(OWNER_KEY)
        args = ["--source", "user", "--trust", "confirmed", "--settles", pid]
        for expected_replay in (False, True):
            result = self.save("owner-adopts", OWNER_KEY, before, AGENT_REWRITE,
                               extra=args, json_out=True)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            answer = json.loads(result.stdout)
            self.assertEqual(answer["result"], "accepted")
            self.assertEqual(answer["settled"], [pid])
            self.assertEqual(answer["replayed"], expected_replay)
        self.assertNotIn("held " + pid, self.cli("status").stdout)
        self.assertIn("PASSED", self.cli("doctor", "--live").stdout)

    def test_empty_settlement_target_cannot_silently_become_an_ordinary_save(self):
        key = "review.empty-target"
        token = self.key_token(key)
        before = self.state()["head_revision"]
        for malformed in ("", ",", "R000001-Q001,"):
            result = self.save("empty-settlement", key, token, "Must remain pending.",
                               new_subject=True, extra=["--settles", malformed], json_out=True)
            self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
            self.assertIn("--settles", result.stderr)
            self.assertEqual(self.state()["head_revision"], before)
        self.assertEqual(self.key_token(key), token)

    def test_unconfirmed_or_different_key_cannot_settle_owner_hold(self):
        pid = self.stage_held()
        for key, new, extra in [(OWNER_KEY, False, []),
                                ("review.unrelated", True, ["--source", "user", "--trust", "confirmed"])]:
            result = self.save("try-settle", key, self.key_token(key), "Different rule.",
                               new_subject=new, extra=["--settles", pid] + extra, json_out=True)
            self.assertEqual(result.returncode, 2, result.stdout)
            self.assertFalse((self.bdir() / "settlements" / (pid + ".json")).exists())
        self.assertIn("held " + pid, self.cli("status").stdout)

    def test_interrupted_settlement_recovers_original_evidence_exactly_once(self):
        pid = self.stage_held()
        before = (self.bdir() / "decisions" / (pid + ".json")).read_bytes()
        adoption = self.save_arguments(
            "owner-crash", OWNER_KEY, self.key_token(OWNER_KEY), AGENT_REWRITE,
            extra=["--source", "user", "--trust", "confirmed", "--settles", pid], json_out=True)
        dismissal = ["dismiss-held", pid, "--summary", "Owner declined.", "--human-approved"]
        for name, command in [("adopt", adoption), ("dismiss", dismissal)]:
            saw_settlement = False
            for n in range(1, 50):
                root = self.workspace / (name + str(n))
                shutil.copytree(self.root, root)
                result = self.worker("save_crash_after_write_" + str(n), *command, root=root)
                if result.returncode == 0:
                    break
                self.assertEqual(result.returncode, 150, result.stdout + result.stderr)
                point = (root / ".test-save-crash-point").read_text("utf-8")
                saw_settlement |= "settlements" in point
                self.cli("status", root=root)
                self.cli(*command, root=root)
                files = list((root / ".bimri" / "settlements").glob("*.json"))
                self.assertEqual([file.stem for file in files], [pid], (name, n))
                self.assertEqual((root / ".bimri" / "decisions" / (pid + ".json")).read_bytes(), before)
                self.assertIn("PASSED", self.cli("doctor", "--live", root=root).stdout)
            else:
                self.fail("settlement did not finish within the interruption budget")
            self.assertTrue(saw_settlement, name)

    def test_damaged_settlement_is_reported_by_full_diagnostic(self):
        pid = self.stage_held()
        self.cli("dismiss-held", pid, "--summary", "Owner declined.", "--human-approved")
        file = self.bdir() / "settlements" / (pid + ".json")
        evidence = json.loads(file.read_text(encoding="utf-8"))
        evidence["summary"] = "Changed after the fact."
        file.write_text(json.dumps(evidence), encoding="utf-8")
        result = self.cli("doctor", "--live", check=False)
        self.assertNotEqual(result.returncode, 0, result.stdout)
        self.assertIn(pid, result.stdout + result.stderr)

    def test_adoption_identical_to_current_value_still_settles_the_hold(self):
        pid = self.stage_held()
        unchanged = self.current_text(OWNER_KEY)
        head = self.state()["head_revision"]
        result = self.save("settle-current", OWNER_KEY, self.key_token(OWNER_KEY), unchanged,
                           extra=["--source", "user", "--trust", "confirmed", "--settles", pid],
                           json_out=True)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        answer = json.loads(result.stdout)
        self.assertEqual(answer["settled"], [pid])
        self.assertEqual(answer["result"], "unchanged")
        self.assertEqual(self.state()["head_revision"], head)
