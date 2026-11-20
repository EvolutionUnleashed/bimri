"""Owner-visible handoffs from one process to another on a disposable project."""
import json

from test_v520_step2 import V520Step2Base


class HandoffTest(V520Step2Base):
    def test_new_agent_finds_the_correction_and_abstains_on_unknowns(self):
        key = "booking.cancellation"
        for hours in (48, 24):
            result = self.save("booking-" + str(hours), key, self.key_token(key),
                               f"Guests may cancel a booking up to {hours} hours before arrival.",
                               new_subject=hours == 48, extra=["--tags", "deadline,cancellation",
                                                              "--source", "user", "--trust", "confirmed"])
            self.assertEqual(0, result.returncode, result.stdout + result.stderr)
        def query(words):
            result = self.cli("recall", "--query", words, "--current", "--json", check=False)
            self.assertIn(result.returncode, (0, 1), result.stderr)
            return json.loads(result.stdout)["matches"]
        for words in ("What is the cancellation deadline?", "Can guests cancel a booking?"):
            rows = query(words)
            self.assertEqual(len(rows), 1)
            self.assertIn("24 hours", rows[0]["text"])
            self.assertEqual(rows[0]["source"], "user")
            self.assertEqual(rows[0]["trust"], "confirmed")
        for words in ("48 hours", "cancellation fee", "no booking", "cancellation fee"):
            self.assertEqual(query(words), [], words)
        history = self.cli("recall", "--key", key, "--history", "--json")
        self.assertTrue(any("48 hours" in r["text"] and r["location"] == "HISTORY"
                            for r in json.loads(history.stdout)["matches"]))

    def test_core_review_identifies_verbatim_policy_without_writing(self):
        key = "policy.confirmation"
        rule = "Confirm the delivery address before sending a replacement."
        result = self.save("owner-policy", key, self.key_token(key), rule, new_subject=True,
                           extra=["--tier", "1", "--source", "user", "--trust", "confirmed"])
        self.assertEqual(0, result.returncode, result.stdout + result.stderr)
        (self.root / "POLICY.md").write_text("# Support\n\n" + rule + "\n", encoding="utf-8")
        def snapshot():
            return {p: p.read_bytes() for p in self.bdir().rglob("*")
                    if p.is_file() and p.name != "engine.lock"}
        before = snapshot()
        out = self.cli("maintain", "--review-core", "--compare", "POLICY.md")
        row = next(r for r in json.loads(out.stdout)["core_entries"] if r["key"] == key)
        self.assertEqual(row["also_in"], ["POLICY.md"])
        self.assertGreater(row["entry_bytes"], len(rule))
        self.assertEqual(before, snapshot())
