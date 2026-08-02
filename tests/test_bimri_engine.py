"""Black-box regression tests for BIMRI v5.

The suite intentionally drives the command-line interface in separate Python
processes. This exercises the same parsing, file locking, atomic commits, and
root resolution that real agents use.
"""

import concurrent.futures
import datetime as dt
import hashlib
import json
import os
import re
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


REPOSITORY = Path(__file__).resolve().parents[1]
ENGINE = REPOSITORY / "bimri-engine.py"
CRASH_WORKER = REPOSITORY / "tests" / "crash_worker.py"
PROPOSAL_RE = re.compile(r"\bR\d{6}-Q\d{3}\b")


class BimriCliTest(unittest.TestCase):
    maxDiff = None

    def setUp(self):
        self._temp = tempfile.TemporaryDirectory(prefix="bimri-v5-test-")
        self.root = Path(self._temp.name)

    def tearDown(self):
        self._temp.cleanup()

    def cli(
        self,
        *arguments,
        root=None,
        cwd=None,
        input_text=None,
        check=True,
        timeout=30,
        engine=None,
    ):
        command = [
            sys.executable,
            str(engine or ENGINE),
            "--root",
            str(root or self.root),
        ]
        command.extend(str(argument) for argument in arguments)
        result = subprocess.run(
            command,
            cwd=str(cwd) if cwd else None,
            input=input_text,
            text=True,
            capture_output=True,
            timeout=timeout,
        )
        if check and result.returncode != 0:
            self.fail(
                "BIMRI command failed\n"
                f"command: {command!r}\n"
                f"return code: {result.returncode}\n"
                f"stdout:\n{result.stdout}\n"
                f"stderr:\n{result.stderr}"
            )
        return result

    def start(self, actor="test-agent", root=None):
        result = self.cli("start", "--actor", actor, root=root)
        match = re.search(r"=== BIMRI BRIEF (R\d{6})", result.stdout)
        self.assertIsNotNone(match, result.stdout)
        return match.group(1)

    def worker(
        self,
        mode,
        *arguments,
        root=None,
        engine_root=None,
        check=True,
        timeout=30,
    ):
        command = [
            sys.executable,
            str(CRASH_WORKER),
            str(ENGINE),
            mode,
            str(engine_root or root or self.root),
        ]
        command.extend(str(argument) for argument in arguments)
        result = subprocess.run(
            command,
            text=True,
            capture_output=True,
            timeout=timeout,
        )
        if check and result.returncode != 0:
            self.fail(
                "BIMRI fault worker failed\n"
                f"command: {command!r}\n"
                f"return code: {result.returncode}\n"
                f"stdout:\n{result.stdout}\n"
                f"stderr:\n{result.stderr}"
            )
        return result

    def propose(
        self,
        run_id,
        key,
        text,
        *,
        tier=2,
        source="user",
        trust="confirmed",
        extra=(),
        root=None,
    ):
        result = self.cli(
            "propose",
            "--run",
            run_id,
            "--tier",
            tier,
            "--key",
            key,
            "--text",
            text,
            "--source",
            source,
            "--trust",
            trust,
            *extra,
            root=root,
        )
        match = PROPOSAL_RE.search(result.stdout)
        self.assertIsNotNone(match, result.stdout)
        return match.group(0)

    def state(self, root=None):
        target = root or self.root
        return json.loads((target / ".bimri" / "state.json").read_text("utf-8"))

    def decision(self, proposal_id, root=None):
        target = root or self.root
        path = target / ".bimri" / "decisions" / f"{proposal_id}.json"
        return json.loads(path.read_text("utf-8"))

    def hot(self, root=None):
        return (root or self.root).joinpath("bimri.md").read_text("utf-8")

    def legacy_v3_bytes(self, claim="Preserve this legacy memory.", sessions=1):
        return (
            f"<!-- BIMRI v3.0 | Last Maintained: 2026-07-20 | Sessions: {sessions} -->\n"
            "# BIMRI: Memory File\n"
            "## Tier 1: Core Intelligence\n"
            f"- {claim}\n"
            "## Tier 2: Active Context\n"
            "## Tier 3: Pattern Recognition\n"
        ).encode("utf-8")

    def test_24_processes_start_with_unique_durable_run_handles(self):
        def launch(number):
            return self.cli(
                "start",
                "--actor",
                f"agent-{number:02d}",
                check=False,
                timeout=45,
            )

        with concurrent.futures.ThreadPoolExecutor(max_workers=24) as executor:
            results = list(executor.map(launch, range(24)))

        failures = [result for result in results if result.returncode != 0]
        self.assertEqual(
            failures,
            [],
            "\n\n".join(
                f"stdout:\n{item.stdout}\nstderr:\n{item.stderr}" for item in failures
            ),
        )
        run_ids = []
        for result in results:
            match = re.search(r"=== BIMRI BRIEF (R\d{6})", result.stdout)
            self.assertIsNotNone(match, result.stdout)
            run_ids.append(match.group(1))
        self.assertEqual(len(set(run_ids)), 24)
        self.assertEqual(set(run_ids), {f"R{number:06d}" for number in range(1, 25)})

        state = self.state()
        self.assertEqual(state["run_count"], 24)
        self.assertEqual(set(state["active_runs"]), set(run_ids))
        for run_id in run_ids:
            log = self.root / ".bimri" / "log" / f"{run_id}.md"
            self.assertTrue(log.is_file(), run_id)
            self.assertIn(f"# Run {run_id}", log.read_text("utf-8"))
        self.assertEqual(self.cli("doctor").returncode, 0)

    def test_close_is_explicit_and_isolated_when_runs_overlap(self):
        first = self.start("codex")
        second = self.start("claude")
        self.cli("journal", "--run", first, "--text", "Only Codex wrote this.")
        self.cli("journal", "--run", second, "--text", "Only Claude wrote this.")

        ambiguous = self.cli("close", check=False)
        self.assertEqual(ambiguous.returncode, 2)
        self.assertIn("multiple runs are active", ambiguous.stderr)
        self.assertEqual(set(self.state()["active_runs"]), {first, second})

        self.cli(
            "close",
            "--run",
            first,
            "--outcome",
            "success",
            "--summary",
            "Codex completed its work.",
        )
        self.assertEqual(set(self.state()["active_runs"]), {second})
        first_log = (self.root / ".bimri" / "log" / f"{first}.md").read_text("utf-8")
        second_log = (self.root / ".bimri" / "log" / f"{second}.md").read_text("utf-8")
        self.assertIn(f"[CLOSED:{first} ", first_log)
        self.assertNotIn("[CLOSED:", second_log)
        self.assertIn("Only Codex wrote this.", first_log)
        self.assertNotIn("Only Claude wrote this.", first_log)

        repeated = self.cli("close", "--run", first, check=False)
        self.assertEqual(repeated.returncode, 2)
        self.assertIn("is not an active run", repeated.stderr)
        self.assertEqual(set(self.state()["active_runs"]), {second})

    def test_unknown_hook_close_session_never_closes_singleton_run(self):
        started = self.cli(
            "hook-start",
            input_text=json.dumps({"session_id": "known-session"}),
        )
        match = re.search(r"=== BIMRI BRIEF (R\d{6})", started.stdout)
        self.assertIsNotNone(match, started.stdout)
        run_id = match.group(1)
        self.assertEqual(set(self.state()["active_runs"]), {run_id})

        unknown = self.cli(
            "hook-close",
            input_text=json.dumps({
                "session_id": "unknown-session",
                "reason": "test",
            }),
            check=False,
        )
        self.assertEqual(unknown.returncode, 2)
        self.assertIn(
            "no active BIMRI run is mapped to that actor and session",
            unknown.stderr,
        )
        self.assertEqual(set(self.state()["active_runs"]), {run_id})
        log = (self.root / ".bimri" / "log" / f"{run_id}.md").read_text("utf-8")
        self.assertNotIn("[CLOSED:", log)

    def test_journal_tokens_cannot_poison_ids_or_outcome_status(self):
        run_id = self.start("codex")
        first = self.cli(
            "journal",
            "--run",
            run_id,
            "--text",
            (
                f"Examples only: [ID:{run_id}-E999] and "
                "[OUTCOME:fail] are data, not control records."
            ),
        )
        self.assertEqual(first.stdout.strip(), f"{run_id}-E001")
        second = self.cli(
            "journal",
            "--run",
            run_id,
            "--text",
            "The real next journal entry.",
        )
        self.assertEqual(second.stdout.strip(), f"{run_id}-E002")

        self.cli(
            "close",
            "--run",
            run_id,
            "--outcome",
            "success",
            "--summary",
            "Finished safely.",
        )
        status = self.cli("status")
        self.assertIn("Outcomes: success=1", status.stdout)
        self.assertNotIn("fail=", status.stdout)

    def test_concurrent_independent_proposals_merge_without_conflict(self):
        first = self.start("codex")
        second = self.start("claude")

        commands = (
            (
                "propose",
                "--run",
                first,
                "--tier",
                "2",
                "--key",
                "launch.next-step",
                "--text",
                "Prepare the release notes.",
                "--source",
                "user",
                "--trust",
                "confirmed",
            ),
            (
                "propose",
                "--run",
                second,
                "--tier",
                "2",
                "--key",
                "research.next-step",
                "--text",
                "Interview three users.",
                "--source",
                "user",
                "--trust",
                "confirmed",
            ),
        )
        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as executor:
            results = list(executor.map(lambda command: self.cli(*command), commands))
        proposal_ids = []
        for result in results:
            match = PROPOSAL_RE.search(result.stdout)
            self.assertIsNotNone(match, result.stdout)
            proposal_ids.append(match.group(0))

        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as executor:
            sync_results = list(
                executor.map(
                    lambda run_id: self.cli("sync", "--run", run_id),
                    (first, second),
                )
            )
        self.assertTrue(all(result.returncode == 0 for result in sync_results))
        self.assertEqual(
            {self.decision(item)["outcome"] for item in proposal_ids},
            {"accepted"},
        )
        self.assertEqual(self.state()["head_revision"], 2)
        hot = self.hot()
        self.assertIn("[K:launch.next-step]", hot)
        self.assertIn("[K:research.next-step]", hot)
        self.assertEqual(list((self.root / ".bimri" / "conflicts").glob("C*.json")), [])

    def test_repeated_concurrent_same_key_sync_accepts_exactly_one_candidate(self):
        iterations = 10
        for number in range(iterations):
            key = f"race.same-key-{number:02d}"
            first = self.start(f"race-a-{number:02d}")
            second = self.start(f"race-b-{number:02d}")

            def submit(candidate):
                run_id, text = candidate
                return self.propose(run_id, key, text)

            with concurrent.futures.ThreadPoolExecutor(max_workers=2) as executor:
                proposal_ids = list(
                    executor.map(
                        submit,
                        (
                            (first, f"Candidate A for race {number}."),
                            (second, f"Candidate B for race {number}."),
                        ),
                    )
                )

            with concurrent.futures.ThreadPoolExecutor(max_workers=2) as executor:
                sync_results = list(
                    executor.map(
                        lambda run_id: self.cli("sync", "--run", run_id),
                        (first, second),
                    )
                )
            self.assertTrue(
                all(result.returncode == 0 for result in sync_results),
                "\n".join(
                    result.stdout + result.stderr for result in sync_results
                ),
            )

            decisions = [self.decision(item) for item in proposal_ids]
            self.assertEqual(
                sorted(item["outcome"] for item in decisions),
                ["accepted", "contested"],
                decisions,
            )
            contested = next(
                item for item in decisions if item["outcome"] == "contested"
            )
            conflict = json.loads(
                (
                    self.root
                    / ".bimri"
                    / "conflicts"
                    / f"{contested['conflict_id']}.json"
                ).read_text("utf-8")
            )
            self.assertEqual(conflict["type"], "stale-base")
            self.assertEqual(conflict["key"], key)
            self.assertEqual(
                conflict["proposal_ids"],
                [contested["proposal_id"]],
            )
            matching = [
                line for line in self.hot().splitlines() if f"[K:{key}]" in line
            ]
            self.assertEqual(len(matching), 1, matching)

        self.assertEqual(self.state()["head_revision"], iterations)

    def test_stale_same_key_retains_both_candidates_and_human_can_resolve(self):
        first = self.start("codex")
        second = self.start("claude")
        first_proposal = self.propose(
            first, "product.name", "The product is called Northstar."
        )
        second_proposal = self.propose(
            second, "product.name", "The product is called Wayfinder."
        )

        self.cli("sync", "--run", first)
        self.cli("sync", "--run", second)
        self.assertEqual(self.decision(first_proposal)["outcome"], "accepted")
        second_decision = self.decision(second_proposal)
        self.assertEqual(second_decision["outcome"], "contested")

        conflict_id = second_decision["conflict_id"]
        conflict_path = self.root / ".bimri" / "conflicts" / f"{conflict_id}.json"
        conflict = json.loads(conflict_path.read_text("utf-8"))
        self.assertEqual(conflict["type"], "stale-base")
        self.assertEqual(conflict["proposal_ids"], [second_proposal])
        self.assertIn("Northstar", conflict["current_line"])
        second_candidate = json.loads(
            (
                self.root
                / ".bimri"
                / "proposals"
                / f"{second_proposal}.json"
            ).read_text("utf-8")
        )
        self.assertEqual(second_candidate["text"], "The product is called Wayfinder.")

        self.cli("resolve", conflict_id, "--choose", second_proposal)
        resolution = json.loads(
            (
                self.root
                / ".bimri"
                / "resolutions"
                / f"{conflict_id}.json"
            ).read_text("utf-8")
        )
        self.assertEqual(resolution["choice"], second_proposal)
        self.assertIn("The product is called Wayfinder.", self.hot())
        self.assertNotIn("The product is called Northstar.", self.hot())
        self.assertIn("[T:confirmed] [SRC:user]", self.hot())
        status = self.cli("status")
        self.assertIn("Open conflicts: 0", status.stdout)

    def test_late_proposal_uses_run_start_revision_and_becomes_stale(self):
        earlier_run = self.start("codex")
        committing_run = self.start("claude")
        committed = self.propose(
            committing_run,
            "roadmap.priority",
            "Ship the search feature first.",
        )
        self.cli("sync", "--run", committing_run)
        self.assertEqual(self.decision(committed)["outcome"], "accepted")
        self.assertEqual(self.state()["head_revision"], 1)

        late = self.propose(
            earlier_run,
            "roadmap.priority",
            "Ship the reporting feature first.",
        )
        proposal = json.loads(
            (
                self.root / ".bimri" / "proposals" / f"{late}.json"
            ).read_text("utf-8")
        )
        self.assertEqual(proposal["base_revision"], 0)
        self.assertEqual(proposal["base_hash"], "absent")

        self.cli("sync", "--run", earlier_run)
        decision = self.decision(late)
        self.assertEqual(decision["outcome"], "contested")
        conflict = json.loads(
            (
                self.root
                / ".bimri"
                / "conflicts"
                / f"{decision['conflict_id']}.json"
            ).read_text("utf-8")
        )
        self.assertEqual(conflict["type"], "stale-base")
        self.assertIn("Ship the search feature first.", conflict["current_line"])
        self.assertIn("Ship the search feature first.", self.hot())
        self.assertNotIn("Ship the reporting feature first.", self.hot())

    def test_abrupt_crash_after_revision_create_recovers_without_reusing_snapshot(self):
        run_id = self.start("crash-before-state")
        proposal_id = self.propose(
            run_id,
            "crash.before-state",
            "The proposal survives an abrupt pre-state crash.",
        )

        crashed = self.worker(
            "crash_after_revision",
            "sync",
            "--run",
            run_id,
            check=False,
        )
        self.assertEqual(crashed.returncode, 91, crashed.stdout + crashed.stderr)
        self.assertEqual(self.state()["head_revision"], 0)
        orphan = self.root / ".bimri" / "revisions" / "V000001.md"
        self.assertTrue(orphan.is_file())
        self.assertIn("crash.before-state", orphan.read_text("utf-8"))
        self.assertEqual(self.decision(proposal_id)["outcome"], "applying")
        self.assertNotIn("[K:crash.before-state]", self.hot())

        recovered = self.cli("sync", "--run", run_id)
        self.assertIn("accepted 1, contested 0", recovered.stdout)
        final = self.decision(proposal_id)
        self.assertEqual(final["outcome"], "accepted")
        self.assertEqual(final["revision"], 2)
        self.assertEqual(self.state()["head_revision"], 2)
        self.assertIn("[K:crash.before-state]", self.hot())
        self.assertTrue(orphan.is_file())

        doctor = self.cli("doctor")
        self.assertIn(
            "unreferenced immutable revision V000001.md",
            doctor.stdout,
        )

    def test_abrupt_crash_after_state_commit_stays_conservative_after_later_update(self):
        crashed_run = self.start("crash-after-state")
        crashed_proposal = self.propose(
            crashed_run,
            "crash.same-key",
            "Value A landed before the child died.",
        )

        crashed = self.worker(
            "crash_after_view",
            "sync",
            "--run",
            crashed_run,
            check=False,
        )
        self.assertEqual(crashed.returncode, 92, crashed.stdout + crashed.stderr)
        landed_revision = self.state()["head_revision"]
        self.assertEqual(landed_revision, 1)
        applying = self.decision(crashed_proposal)
        self.assertEqual(applying["outcome"], "applying")
        self.assertIn("Value A landed before the child died.", self.hot())

        later_run = self.start("later-agent")
        later_proposal = self.propose(
            later_run,
            "crash.same-key",
            "Value B is the later committed update.",
        )
        self.cli("sync", "--run", later_run)
        self.assertEqual(self.decision(later_proposal)["outcome"], "accepted")
        self.assertEqual(self.state()["head_revision"], 2)
        self.assertIn("Value B is the later committed update.", self.hot())

        retry = self.cli("sync", "--run", crashed_run)
        self.assertIn("accepted 0, contested 1", retry.stdout)
        recovered = self.decision(crashed_proposal)
        self.assertEqual(recovered["outcome"], "contested")
        self.assertEqual(self.state()["head_revision"], 2)
        self.assertIn("Value B is the later committed update.", self.hot())
        self.assertNotIn("Value A landed before the child died.", self.hot())
        same_key_conflicts = [
            json.loads(path.read_text("utf-8"))
            for path in (self.root / ".bimri" / "conflicts").glob("C*.json")
            if '"key": "crash.same-key"' in path.read_text("utf-8")
        ]
        self.assertEqual(len(same_key_conflicts), 1)
        self.assertEqual(same_key_conflicts[0]["type"], "stale-base")
        self.assertEqual(
            same_key_conflicts[0]["proposal_ids"],
            [crashed_proposal],
        )

    def test_resolution_and_decision_finalization_are_crash_idempotent(self):
        first_candidate_run = self.start("candidate-one")
        second_candidate_run = self.start("candidate-two")
        committer_run = self.start("committer")
        committed = self.propose(
            committer_run,
            "shared.choice",
            "Keep the committed value.",
        )
        self.cli("sync", "--run", committer_run)
        self.assertEqual(self.decision(committed)["outcome"], "accepted")

        first_candidate = self.propose(
            first_candidate_run,
            "shared.choice",
            "Choose candidate one.",
        )
        self.cli("sync", "--run", first_candidate_run)
        second_candidate = self.propose(
            second_candidate_run,
            "shared.choice",
            "Choose candidate two.",
        )
        self.cli("sync", "--run", second_candidate_run)
        first_decision = self.decision(first_candidate)
        second_decision = self.decision(second_candidate)
        self.assertEqual(first_decision["outcome"], "contested")
        self.assertEqual(second_decision["outcome"], "contested")
        conflict_id = first_decision["conflict_id"]
        self.assertEqual(second_decision["conflict_id"], conflict_id)

        self.cli("resolve", conflict_id, "--choose", first_candidate)
        resolution_path = (
            self.root / ".bimri" / "resolutions" / f"{conflict_id}.json"
        )
        first_decision_path = (
            self.root / ".bimri" / "decisions" / f"{first_candidate}.json"
        )
        second_decision_path = (
            self.root / ".bimri" / "decisions" / f"{second_candidate}.json"
        )
        resolution = json.loads(resolution_path.read_text("utf-8"))
        self.assertEqual(resolution["status"], "resolved")
        self.assertEqual(resolution["choice"], first_candidate)
        finalized_first = json.loads(first_decision_path.read_text("utf-8"))
        finalized_second = json.loads(second_decision_path.read_text("utf-8"))
        self.assertEqual(finalized_first["outcome"], "accepted")
        self.assertEqual(finalized_second["outcome"], "noop")
        self.assertEqual(finalized_first["initial_outcome"], "contested")
        self.assertEqual(finalized_second["initial_outcome"], "contested")
        self.assertEqual(finalized_first["resolution_id"], conflict_id)
        self.assertEqual(finalized_second["resolution_id"], conflict_id)
        revision_after_normal_resolution = self.state()["head_revision"]
        stable_resolution = resolution_path.read_bytes()
        stable_first_decision = first_decision_path.read_bytes()
        stable_second_decision = second_decision_path.read_bytes()

        repeated = self.cli(
            "resolve",
            conflict_id,
            "--choose",
            first_candidate,
        )
        self.assertIn("already resolved", repeated.stdout)
        self.assertEqual(self.state()["head_revision"], revision_after_normal_resolution)
        self.assertEqual(resolution_path.read_bytes(), stable_resolution)
        self.assertEqual(first_decision_path.read_bytes(), stable_first_decision)
        self.assertEqual(second_decision_path.read_bytes(), stable_second_decision)

        stale_run = self.start("crash-candidate")
        crash_committer = self.start("crash-committer")
        crash_committed = self.propose(
            crash_committer,
            "crash.choice",
            "The pre-conflict value.",
        )
        self.cli("sync", "--run", crash_committer)
        self.assertEqual(self.decision(crash_committed)["outcome"], "accepted")
        crash_candidate = self.propose(
            stale_run,
            "crash.choice",
            "The chosen value already committed before the crash.",
        )
        self.cli("sync", "--run", stale_run)
        crash_decision = self.decision(crash_candidate)
        self.assertEqual(crash_decision["outcome"], "contested")
        crash_conflict = crash_decision["conflict_id"]
        self.cli("resolve", crash_conflict, "--choose", crash_candidate)
        crash_resolution_path = (
            self.root / ".bimri" / "resolutions" / f"{crash_conflict}.json"
        )
        crash_decision_path = (
            self.root / ".bimri" / "decisions" / f"{crash_candidate}.json"
        )
        resolved = json.loads(crash_resolution_path.read_text("utf-8"))
        revision_with_effect = self.state()["head_revision"]
        self.assertIn(
            "The chosen value already committed before the crash.",
            self.hot(),
        )

        applying = dict(resolved)
        applying["status"] = "applying"
        applying.pop("resolved_at", None)
        applying.pop("revision_after", None)
        crash_resolution_path.write_text(
            json.dumps(applying, indent=2, sort_keys=True) + "\n",
            "utf-8",
        )
        interrupted_decision = json.loads(crash_decision_path.read_text("utf-8"))
        interrupted_decision["outcome"] = "contested"
        for field in (
            "resolution_id",
            "resolution_choice",
            "resolved_at",
        ):
            interrupted_decision.pop(field, None)
        crash_decision_path.write_text(
            json.dumps(interrupted_decision, indent=2, sort_keys=True) + "\n",
            "utf-8",
        )

        recovered = self.cli(
            "resolve",
            crash_conflict,
            "--choose",
            crash_candidate,
        )
        self.assertIn(f"resolved with {crash_candidate}", recovered.stdout)
        self.assertEqual(self.state()["head_revision"], revision_with_effect)
        recovered_resolution = json.loads(
            crash_resolution_path.read_text("utf-8")
        )
        recovered_decision = json.loads(crash_decision_path.read_text("utf-8"))
        self.assertEqual(recovered_resolution["status"], "resolved")
        self.assertEqual(
            recovered_resolution["revision_after"],
            revision_with_effect,
        )
        self.assertEqual(recovered_decision["outcome"], "accepted")
        self.assertEqual(recovered_decision["resolution_id"], crash_conflict)
        stable_recovery = crash_resolution_path.read_bytes()
        self.cli("resolve", crash_conflict, "--choose", crash_candidate)
        self.assertEqual(self.state()["head_revision"], revision_with_effect)
        self.assertEqual(crash_resolution_path.read_bytes(), stable_recovery)

    def test_resolution_preflights_every_candidate_before_mutation(self):
        for damage in ("malformed-losing", "missing-chosen"):
            with self.subTest(damage=damage):
                root = self.root / damage
                first_run = self.start("first", root=root)
                second_run = self.start("second", root=root)
                first_id = self.propose(
                    first_run,
                    "resolve.preflight",
                    "First candidate.",
                    tier=1,
                    source="agent",
                    trust="working",
                    extra=("--kind", "decision"),
                    root=root,
                )
                second_id = self.propose(
                    second_run,
                    "resolve.preflight",
                    "Second candidate.",
                    tier=1,
                    source="agent",
                    trust="working",
                    extra=("--kind", "decision"),
                    root=root,
                )
                self.cli("sync", "--run", first_run, root=root)
                self.cli("sync", "--run", second_run, root=root)
                first_decision = self.decision(first_id, root=root)
                second_decision = self.decision(second_id, root=root)
                conflict_id = first_decision["conflict_id"]
                self.assertEqual(second_decision["conflict_id"], conflict_id)

                if damage == "malformed-losing":
                    damaged_id = second_id
                    chosen_id = first_id
                else:
                    damaged_id = first_id
                    chosen_id = first_id
                damaged_path = (
                    root
                    / ".bimri"
                    / "decisions"
                    / f"{damaged_id}.json"
                )
                if damage == "malformed-losing":
                    damaged_path.write_text("{}\n", "utf-8")
                else:
                    damaged_path.unlink()

                head_before = self.state(root=root)["head_revision"]
                hot_before = self.hot(root=root)
                untouched_id = (
                    first_id if damaged_id == second_id else second_id
                )
                untouched_before = self.decision(untouched_id, root=root)
                failed = self.cli(
                    "resolve",
                    conflict_id,
                    "--choose",
                    chosen_id,
                    root=root,
                    check=False,
                )
                self.assertEqual(failed.returncode, 2)
                self.assertIn("BIMRI ERROR:", failed.stderr)
                self.assertEqual(
                    self.state(root=root)["head_revision"], head_before
                )
                self.assertEqual(self.hot(root=root), hot_before)
                self.assertEqual(
                    self.decision(untouched_id, root=root), untouched_before
                )
                self.assertFalse(
                    (
                        root
                        / ".bimri"
                        / "resolutions"
                        / f"{conflict_id}.json"
                    ).exists()
                )

    def test_conflict_creation_crash_can_recreate_missing_decision(self):
        run_id = self.start("conflict-recovery")
        proposal_id = self.propose(
            run_id,
            "conflict.recovery",
            "An approval conflict survives a crash before its decision write.",
            tier=1,
            source="agent",
            trust="working",
            extra=("--kind", "decision"),
        )
        self.cli("sync", "--run", run_id)
        original = self.decision(proposal_id)
        self.assertEqual(original["outcome"], "contested")
        decision_path = (
            self.root
            / ".bimri"
            / "decisions"
            / f"{proposal_id}.json"
        )
        decision_path.unlink()

        recovered = self.cli("sync", "--run", run_id)
        self.assertIn("accepted 0, contested 1", recovered.stdout)
        recreated = self.decision(proposal_id)
        self.assertEqual(recreated["outcome"], "contested")
        self.assertEqual(recreated["conflict_id"], original["conflict_id"])
        self.assertIn("Open conflicts: 1", self.cli("status").stdout)
        self.assertIn("BIMRI doctor: PASSED", self.cli("doctor").stdout)

    def test_stale_close_resolution_archives_removed_current_value_after_retry(self):
        creator = self.start("creator")
        created = self.propose(
            creator,
            "stale.close",
            "Value A from the stale run base.",
        )
        self.cli("sync", "--run", creator)
        self.assertEqual(self.decision(created)["outcome"], "accepted")
        line_a = next(
            line for line in self.hot().splitlines() if "[K:stale.close]" in line
        )
        target_a = re.match(r"\[([^\]]+)\]", line_a).group(1)
        self.assertIn("Value A from the stale run base.", line_a)

        stale_closer = self.start("stale-closer")
        changer = self.start("changer")
        close_result = self.cli(
            "propose",
            "--run",
            stale_closer,
            "--operation",
            "close",
            "--key",
            "stale.close",
            "--target",
            target_a,
            "--source",
            "user",
            "--trust",
            "confirmed",
        )
        close_id = PROPOSAL_RE.search(close_result.stdout).group(0)

        changed = self.propose(
            changer,
            "stale.close",
            "Value B that the human ultimately removes.",
        )
        self.cli("sync", "--run", changer)
        self.assertEqual(self.decision(changed)["outcome"], "accepted")
        line_b = next(
            line for line in self.hot().splitlines() if "[K:stale.close]" in line
        )
        self.assertIn("Value B that the human ultimately removes.", line_b)
        self.assertNotIn("Value A from the stale run base.", line_b)

        self.cli("sync", "--run", stale_closer)
        contested = self.decision(close_id)
        self.assertEqual(contested["outcome"], "contested")
        conflict_id = contested["conflict_id"]
        conflict = json.loads(
            (
                self.root
                / ".bimri"
                / "conflicts"
                / f"{conflict_id}.json"
            ).read_text("utf-8")
        )
        self.assertEqual(conflict["type"], "stale-base")
        self.assertIn("Value B that the human ultimately removes.", conflict["current_line"])

        archive_path = (
            self.root
            / ".bimri"
            / "archive"
            / f"{dt.date.today():%Y-%m}.md"
        )
        external_archive = self.root / "external-archive-sentinel.md"
        external_content = "external archive sentinel\n"
        external_archive.write_text(external_content, "utf-8")
        try:
            archive_path.symlink_to(external_archive)
        except (NotImplementedError, OSError) as exc:
            self.skipTest(f"symbolic links unavailable: {exc}")

        failed = self.cli(
            "resolve",
            conflict_id,
            "--choose",
            close_id,
            check=False,
        )
        self.assertEqual(failed.returncode, 2)
        self.assertIn("monthly archive file cannot be a symbolic link", failed.stderr)
        self.assertEqual(external_archive.read_text("utf-8"), external_content)
        self.assertIn("[K:stale.close]", self.hot())
        self.assertIn("Value B that the human ultimately removes.", self.hot())
        self.assertEqual(self.state()["head_revision"], 2)

        resolution_path = (
            self.root / ".bimri" / "resolutions" / f"{conflict_id}.json"
        )
        failed_resolution = json.loads(resolution_path.read_text("utf-8"))
        self.assertEqual(failed_resolution["status"], "failed")
        self.assertIn(
            "Value B that the human ultimately removes.",
            failed_resolution["archived_raw"],
        )
        self.assertNotIn(
            "Value A from the stale run base.",
            failed_resolution["archived_raw"],
        )
        self.assertEqual(self.decision(close_id)["outcome"], "contested")

        revisions = self.root / ".bimri" / "revisions"
        revision_a = (revisions / "V000001.md").read_text("utf-8")
        revision_b = (revisions / "V000002.md").read_text("utf-8")
        self.assertIn("Value A from the stale run base.", revision_a)
        self.assertIn("Value B that the human ultimately removes.", revision_b)
        self.assertFalse((revisions / "V000003.md").exists())

        archive_path.unlink()
        recovered = self.cli(
            "resolve",
            conflict_id,
            "--choose",
            close_id,
        )
        self.assertIn(f"resolved with {close_id}", recovered.stdout)
        self.assertEqual(self.state()["head_revision"], 3)
        self.assertNotIn("[K:stale.close]", self.hot())
        revision_removed = (revisions / "V000003.md").read_text("utf-8")
        self.assertNotIn("[K:stale.close]", revision_removed)
        final_resolution = json.loads(resolution_path.read_text("utf-8"))
        final_decision = self.decision(close_id)
        self.assertEqual(final_resolution["status"], "resolved")
        self.assertEqual(final_resolution["revision_before"], 2)
        self.assertEqual(final_resolution["revision_after"], 3)
        self.assertEqual(final_decision["outcome"], "accepted")
        self.assertEqual(final_decision["initial_outcome"], "contested")
        self.assertEqual(final_decision["resolution_id"], conflict_id)

        archive_text = archive_path.read_text("utf-8")
        self.assertIn("Value B that the human ultimately removes.", archive_text)
        self.assertNotIn("Value A from the stale run base.", archive_text)
        self.assertEqual(archive_text.count(f"[BY:{close_id}]"), 1)
        stable_resolution = resolution_path.read_bytes()
        stable_decision = (
            self.root / ".bimri" / "decisions" / f"{close_id}.json"
        ).read_bytes()
        stable_archive = archive_path.read_bytes()

        repeated = self.cli(
            "resolve",
            conflict_id,
            "--choose",
            close_id,
        )
        self.assertIn("already resolved", repeated.stdout)
        self.assertEqual(self.state()["head_revision"], 3)
        self.assertEqual(resolution_path.read_bytes(), stable_resolution)
        self.assertEqual(
            (self.root / ".bimri" / "decisions" / f"{close_id}.json").read_bytes(),
            stable_decision,
        )
        self.assertEqual(archive_path.read_bytes(), stable_archive)
        self.assertIn("Open conflicts: 0", self.cli("status").stdout)

    def test_archive_idempotence_uses_exact_proposal_metadata(self):
        first_run = self.start("archive-decoy")
        future_close_id = "R000002-Q002"
        self.propose(
            first_run,
            "archive.decoy",
            f"Raw memory mentions [BY:{future_close_id}] as ordinary text.",
        )
        self.cli("sync", "--run", first_run)
        decoy_line = next(
            line
            for line in self.hot().splitlines()
            if "[K:archive.decoy]" in line
        )
        decoy_target = re.match(r"\[([^\]]+)\]", decoy_line).group(1)
        decoy_close = self.cli(
            "propose",
            "--run",
            first_run,
            "--operation",
            "close",
            "--key",
            "archive.decoy",
            "--target",
            decoy_target,
            "--source",
            "user",
            "--trust",
            "confirmed",
        )
        decoy_close_id = PROPOSAL_RE.search(decoy_close.stdout).group(0)
        self.cli("sync", "--run", first_run)

        second_run = self.start("archive-victim")
        self.assertEqual(second_run, "R000002")
        self.propose(
            second_run,
            "archive.victim",
            "This exact victim line must receive its own archive record.",
        )
        self.cli("sync", "--run", second_run)
        victim_line = next(
            line
            for line in self.hot().splitlines()
            if "[K:archive.victim]" in line
        )
        victim_target = re.match(r"\[([^\]]+)\]", victim_line).group(1)
        victim_close = self.cli(
            "propose",
            "--run",
            second_run,
            "--operation",
            "close",
            "--key",
            "archive.victim",
            "--target",
            victim_target,
            "--source",
            "user",
            "--trust",
            "confirmed",
        )
        victim_close_id = PROPOSAL_RE.search(victim_close.stdout).group(0)
        self.assertEqual(victim_close_id, future_close_id)
        self.cli("sync", "--run", second_run)

        archive = (
            self.root
            / ".bimri"
            / "archive"
            / f"{dt.date.today():%Y-%m}.md"
        ).read_text("utf-8")
        provenance = re.findall(
            r"^\[ARCHIVED:\d{4}-\d{2}-\d{2}\] "
            r"\[BY:(R\d{6}-Q\d{3})\]",
            archive,
            re.MULTILINE,
        )
        self.assertEqual(provenance.count(decoy_close_id), 1)
        self.assertEqual(provenance.count(victim_close_id), 1)
        self.assertIn(
            "This exact victim line must receive its own archive record.",
            archive,
        )
        self.assertNotIn("[K:archive.victim]", self.hot())

    def test_applying_decision_replay_recovers_close_and_touch_effects(self):
        creator = self.start("creator")
        created = self.propose(
            creator,
            "replay.close",
            "This item will be closed.",
        )
        self.cli("sync", "--run", creator)
        self.assertEqual(self.decision(created)["outcome"], "accepted")
        close_line = next(
            line for line in self.hot().splitlines() if "[K:replay.close]" in line
        )
        close_target = re.match(r"\[([^\]]+)\]", close_line).group(1)

        closer = self.start("closer")
        close_result = self.cli(
            "propose",
            "--run",
            closer,
            "--operation",
            "close",
            "--key",
            "replay.close",
            "--target",
            close_target,
            "--source",
            "user",
            "--trust",
            "confirmed",
        )
        close_id = PROPOSAL_RE.search(close_result.stdout).group(0)
        close_proposal = json.loads(
            (
                self.root / ".bimri" / "proposals" / f"{close_id}.json"
            ).read_text("utf-8")
        )
        self.cli("sync", "--run", closer)
        close_revision = self.state()["head_revision"]
        self.assertEqual(self.decision(close_id)["outcome"], "accepted")
        self.assertNotIn("[K:replay.close]", self.hot())
        close_decision_path = (
            self.root / ".bimri" / "decisions" / f"{close_id}.json"
        )
        close_decision_path.write_text(
            json.dumps({
                "bimri_version": "5.0",
                "proposal_id": close_id,
                "outcome": "applying",
                "recorded_at": self.decision(close_id)["recorded_at"],
                "base_hash": close_proposal["base_hash"],
                "revision_before": close_revision - 1,
            }, indent=2, sort_keys=True) + "\n",
            "utf-8",
        )

        close_replay = self.cli("sync", "--run", closer)
        self.assertIn("accepted 1, contested 0", close_replay.stdout)
        self.assertEqual(self.state()["head_revision"], close_revision)
        replayed_close = self.decision(close_id)
        self.assertEqual(replayed_close["outcome"], "accepted")
        self.assertTrue(replayed_close["recovered_from_intent"])
        archive_text = "\n".join(
            path.read_text("utf-8")
            for path in (self.root / ".bimri" / "archive").glob("*.md")
        )
        self.assertEqual(archive_text.count(f"[BY:{close_id}]"), 1)

        touch_creator = self.start("touch-creator")
        touch_created = self.propose(
            touch_creator,
            "replay.touch",
            "This item will be touched.",
        )
        self.cli("sync", "--run", touch_creator)
        self.assertEqual(self.decision(touch_created)["outcome"], "accepted")
        touch_line = next(
            line for line in self.hot().splitlines() if "[K:replay.touch]" in line
        )
        touch_target = re.match(r"\[([^\]]+)\]", touch_line).group(1)

        toucher = self.start("toucher")
        touch_result = self.cli(
            "propose",
            "--run",
            toucher,
            "--operation",
            "touch",
            "--key",
            "replay.touch",
            "--target",
            touch_target,
            "--source",
            "user",
            "--trust",
            "confirmed",
        )
        touch_id = PROPOSAL_RE.search(touch_result.stdout).group(0)
        touch_proposal = json.loads(
            (
                self.root / ".bimri" / "proposals" / f"{touch_id}.json"
            ).read_text("utf-8")
        )
        self.cli("sync", "--run", toucher)
        touch_revision = self.state()["head_revision"]
        applied_touch_line = next(
            line for line in self.hot().splitlines() if "[K:replay.touch]" in line
        )
        self.assertIn(f"[L:{toucher}]", applied_touch_line)
        touch_decision_path = (
            self.root / ".bimri" / "decisions" / f"{touch_id}.json"
        )
        touch_decision_path.write_text(
            json.dumps({
                "bimri_version": "5.0",
                "proposal_id": touch_id,
                "outcome": "applying",
                "recorded_at": self.decision(touch_id)["recorded_at"],
                "base_hash": touch_proposal["base_hash"],
                "revision_before": touch_revision - 1,
            }, indent=2, sort_keys=True) + "\n",
            "utf-8",
        )

        touch_replay = self.cli("sync", "--run", toucher)
        self.assertIn("accepted 1, contested 0", touch_replay.stdout)
        self.assertEqual(self.state()["head_revision"], touch_revision)
        replayed_touch = self.decision(touch_id)
        self.assertEqual(replayed_touch["outcome"], "accepted")
        self.assertTrue(replayed_touch["recovered_from_intent"])
        self.assertEqual(
            list((self.root / ".bimri" / "conflicts").glob("C*.json")),
            [],
        )

    def test_confirmation_provenance_and_approval_rules(self):
        run_id = self.start("codex")
        invalid = self.cli(
            "propose",
            "--run",
            run_id,
            "--tier",
            "2",
            "--key",
            "invalid.confirmation",
            "--text",
            "An agent asserted this.",
            "--source",
            "agent",
            "--trust",
            "confirmed",
            check=False,
        )
        self.assertEqual(invalid.returncode, 2)
        self.assertIn("only directly human-stated", invalid.stderr)

        invalid_external = self.cli(
            "propose",
            "--run",
            run_id,
            "--tier",
            "2",
            "--key",
            "external.confirmation",
            "--text",
            "A website asserted this.",
            "--source",
            "external",
            "--trust",
            "confirmed",
            check=False,
        )
        self.assertEqual(invalid_external.returncode, 2)
        self.assertIn("only directly human-stated", invalid_external.stderr)

        human_core = self.propose(
            run_id,
            "owner.preference",
            "The owner prefers concise status reports.",
            tier=1,
            source="user",
            trust="confirmed",
            extra=("--kind", "pref"),
        )
        self.cli("sync", "--run", run_id)
        self.assertEqual(self.decision(human_core)["outcome"], "accepted")
        self.assertIn("[T:confirmed] [SRC:user]", self.hot())

        second = self.start("agent")
        agent_core = self.propose(
            second,
            "architecture.assumption",
            "The service should use a queue.",
            tier=1,
            source="agent",
            trust="working",
            extra=("--kind", "decision"),
        )
        self.cli("sync", "--run", second)
        agent_decision = self.decision(agent_core)
        self.assertEqual(agent_decision["outcome"], "contested")
        conflict = json.loads(
            (
                self.root
                / ".bimri"
                / "conflicts"
                / f"{agent_decision['conflict_id']}.json"
            ).read_text("utf-8")
        )
        self.assertEqual(conflict["type"], "approval")

    def test_human_can_confirm_identical_working_memory(self):
        agent_run = self.start("agent")
        working = self.propose(
            agent_run,
            "release.channel",
            "Use the beta release channel.",
            source="agent",
            trust="working",
        )
        self.cli("sync", "--run", agent_run)
        self.assertEqual(self.decision(working)["outcome"], "accepted")
        self.assertIn("[T:working] [SRC:agent]", self.hot())

        human_run = self.start("codex")
        confirmed = self.propose(
            human_run,
            "release.channel",
            "Use the beta release channel.",
            source="user",
            trust="confirmed",
        )
        self.cli("sync", "--run", human_run)
        self.assertEqual(self.decision(confirmed)["outcome"], "accepted")
        matching_lines = [
            line for line in self.hot().splitlines() if "[K:release.channel]" in line
        ]
        self.assertEqual(len(matching_lines), 1)
        self.assertIn("[T:confirmed] [SRC:user]", matching_lines[0])

    def test_agent_change_to_confirmed_memory_requires_human(self):
        human_run = self.start("codex")
        initial = self.propose(
            human_run,
            "launch.date",
            "Launch on Monday.",
            source="user",
            trust="confirmed",
        )
        self.cli("sync", "--run", human_run)
        self.assertEqual(self.decision(initial)["outcome"], "accepted")

        agent_run = self.start("agent")
        changed = self.propose(
            agent_run,
            "launch.date",
            "Launch on Tuesday.",
            source="agent",
            trust="working",
        )
        self.cli("sync", "--run", agent_run)
        decision = self.decision(changed)
        self.assertEqual(decision["outcome"], "contested")
        conflict = json.loads(
            (
                self.root
                / ".bimri"
                / "conflicts"
                / f"{decision['conflict_id']}.json"
            ).read_text("utf-8")
        )
        self.assertEqual(conflict["type"], "confirmed-change")
        self.assertIn("Launch on Monday.", conflict["current_line"])

    def test_caps_and_oversize_inputs_fail_without_silent_truncation(self):
        self.cli("migrate")
        state_path = self.root / ".bimri" / "state.json"
        state = self.state()
        state["entry_max_chars"] = 50
        state["tier2_max"] = 1
        state["tier2_hard"] = 1
        state["hot_max_bytes"] = len(self.hot().encode("utf-8")) + 220
        state_path.write_text(json.dumps(state, indent=2, sort_keys=True) + "\n", "utf-8")

        first = self.start("codex")
        oversize = self.cli(
            "propose",
            "--run",
            first,
            "--tier",
            "2",
            "--key",
            "too.large",
            "--text",
            "x" * 51,
            "--source",
            "user",
            "--trust",
            "confirmed",
            check=False,
        )
        self.assertEqual(oversize.returncode, 2)
        self.assertIn("exceeds 50 characters", oversize.stderr)

        first_proposal = self.propose(first, "first.item", "First compact item.")
        self.cli("sync", "--run", first)
        self.assertEqual(self.decision(first_proposal)["outcome"], "accepted")
        second = self.start("claude")
        second_proposal = self.propose(second, "second.item", "Second compact item.")
        self.cli("sync", "--run", second)
        decision = self.decision(second_proposal)
        self.assertEqual(decision["outcome"], "contested")
        self.assertTrue(
            any("Tier 2 exceeds cap" in error for error in decision["errors"]),
            decision,
        )
        self.assertEqual(self.hot().count("[K:first.item]"), 1)
        self.assertNotIn("[K:second.item]", self.hot())

        state = self.state()
        state["tier2_max"] = 20
        state["tier2_hard"] = 26
        state["hot_max_bytes"] = len(self.hot().encode("utf-8")) + 10
        state_path.write_text(json.dumps(state, indent=2, sort_keys=True) + "\n", "utf-8")
        third = self.start("other")
        byte_cap = self.propose(third, "byte.cap", "This cannot fit.")
        self.cli("sync", "--run", third)
        byte_decision = self.decision(byte_cap)
        self.assertEqual(byte_decision["outcome"], "contested")
        self.assertTrue(
            any("hot memory exceeds byte cap" in error for error in byte_decision["errors"]),
            byte_decision,
        )

    def test_inherited_cap_overflow_can_be_reduced_three_to_two_to_one(self):
        bdir = self.root / ".bimri"
        log_dir = bdir / "log"
        log_dir.mkdir(parents=True)
        legacy_hot = """# BIMRI Memory

## Tier 1: Core Intelligence

## Tier 2: Active Context

[R1-E1] [I:3] [active] [F:R1] [L:R1] [legacy] First inherited item -> .bimri/log/R1.md
[R1-E2] [I:3] [active] [F:R1] [L:R1] [legacy] Second inherited item -> .bimri/log/R1.md
[R1-E3] [I:3] [active] [F:R1] [L:R1] [legacy] Third inherited item -> .bimri/log/R1.md

## Tier 3: Pattern Recognition

<!-- END BIMRI -->
"""
        (self.root / "bimri.md").write_text(legacy_hot, "utf-8")
        (bdir / "state.json").write_text(
            json.dumps({
                "bimri_version": "4.0",
                "project_id": "legacy-overflow",
                "run_count": 1,
                "current_run_id": "R1",
                "tier2_max": 1,
                "tier2_hard": 3,
            }, indent=2) + "\n",
            "utf-8",
        )
        (log_dir / "R1.md").write_text(
            "# Legacy run\n\n[CLOSED:R1 2026-07-26T00:00:00Z]\n",
            "utf-8",
        )
        self.cli("migrate")
        self.assertEqual(self.hot().count("[K:legacy.r1-e"), 3)

        run_id = self.start("codex")
        first_close = self.cli(
            "propose",
            "--run",
            run_id,
            "--operation",
            "close",
            "--key",
            "legacy.r1-e3",
            "--target",
            "R1-E3",
            "--source",
            "user",
            "--trust",
            "confirmed",
        )
        first_id = PROPOSAL_RE.search(first_close.stdout).group(0)
        self.cli("sync", "--run", run_id)
        self.assertEqual(self.decision(first_id)["outcome"], "accepted")
        self.assertEqual(self.hot().count("[K:legacy.r1-e"), 2)
        self.assertEqual(self.state()["head_revision"], 1)

        second_close = self.cli(
            "propose",
            "--run",
            run_id,
            "--operation",
            "close",
            "--key",
            "legacy.r1-e2",
            "--target",
            "R1-E2",
            "--source",
            "user",
            "--trust",
            "confirmed",
        )
        second_id = PROPOSAL_RE.search(second_close.stdout).group(0)
        self.cli("sync", "--run", run_id)
        self.assertEqual(self.decision(second_id)["outcome"], "accepted")
        self.assertEqual(self.hot().count("[K:legacy.r1-e"), 1)
        self.assertEqual(self.state()["head_revision"], 2)
        self.assertEqual(
            list((bdir / "conflicts").glob("C*.json")),
            [],
        )
        doctor = self.cli("doctor")
        self.assertIn("BIMRI doctor: PASSED", doctor.stdout)

    def test_malformed_inputs_and_path_traversal_are_rejected(self):
        run_id = self.start("codex")
        cases = (
            (
                "journal",
                "--run",
                "../../bimri-v5-escape-sentinel",
                "--text",
                "bad",
            ),
            (
                "journal",
                "--run",
                run_id,
                "--text",
                "first line\nsecond line",
            ),
            (
                "propose",
                "--run",
                run_id,
                "--tier",
                "2",
                "--key",
                "../bimri-v5-escape-sentinel",
                "--text",
                "bad",
            ),
            (
                "propose",
                "--run",
                run_id,
                "--tier",
                "2",
                "--key",
                "valid.key",
                "--target",
                "../../bimri-v5-escape-sentinel",
                "--text",
                "bad",
            ),
        )
        for arguments in cases:
            with self.subTest(arguments=arguments):
                result = self.cli(*arguments, check=False)
                self.assertNotEqual(result.returncode, 0)
                self.assertIn("BIMRI ERROR:", result.stderr)

        malformed = (
            self.root
            / ".bimri"
            / "proposals"
            / f"{run_id}-Q999.json"
        )
        malformed.write_text("{ definitely not JSON", "utf-8")
        before = self.state()["head_revision"]
        result = self.cli("sync", "--run", run_id, check=False)
        self.assertEqual(result.returncode, 2)
        self.assertIn("unreadable; BIMRI stopped without resetting", result.stderr)
        self.assertEqual(self.state()["head_revision"], before)
        self.assertEqual(malformed.read_text("utf-8"), "{ definitely not JSON")

    def test_forged_decision_and_resolution_fail_closed_when_consumed(self):
        decision_root = self.root / "forged-decision"
        decision_run = self.start("decision-agent", root=decision_root)
        proposal_id = self.propose(
            decision_run,
            "forged.decision",
            "This proposal must not inherit a forged acceptance.",
            root=decision_root,
        )
        decision_path = (
            decision_root
            / ".bimri"
            / "decisions"
            / f"{proposal_id}.json"
        )
        decision_path.write_text(
            json.dumps({
                "bimri_version": "5.0",
                "proposal_id": proposal_id,
                "outcome": "accepted",
                "recorded_at": "2026-07-27T00:00:00Z",
            }) + "\n",
            "utf-8",
        )
        for command in (
            ("sync", "--run", decision_run),
            ("doctor",),
        ):
            with self.subTest(forgery="decision", command=command):
                result = self.cli(*command, root=decision_root, check=False)
                self.assertNotEqual(result.returncode, 0)
                self.assertNotIn("Traceback", result.stderr)
                self.assertIn(
                    "decision revision",
                    result.stdout + result.stderr,
                )
        self.assertEqual(self.state(root=decision_root)["head_revision"], 0)
        self.assertNotIn("[K:forged.decision]", self.hot(root=decision_root))
        status = self.cli("status", root=decision_root)
        self.assertEqual(status.returncode, 0)
        self.assertEqual(self.state(root=decision_root)["head_revision"], 0)

        resolution_root = self.root / "forged-resolution"
        resolution_run = self.start("resolution-agent", root=resolution_root)
        contested_id = self.propose(
            resolution_run,
            "forged.resolution",
            "An agent-origin Tier 1 proposal needs approval.",
            tier=1,
            source="agent",
            trust="working",
            extra=("--kind", "decision"),
            root=resolution_root,
        )
        self.cli(
            "sync",
            "--run",
            resolution_run,
            root=resolution_root,
        )
        contested = self.decision(contested_id, root=resolution_root)
        conflict_id = contested["conflict_id"]
        (
            resolution_root
            / ".bimri"
            / "resolutions"
            / f"{conflict_id}.json"
        ).write_text("{}\n", "utf-8")
        for command in (
            ("sync", "--run", resolution_run),
            ("status",),
            ("doctor",),
        ):
            with self.subTest(forgery="resolution", command=command):
                result = self.cli(*command, root=resolution_root, check=False)
                self.assertNotEqual(result.returncode, 0)
                self.assertNotIn("Traceback", result.stderr)
                self.assertIn(
                    "resolution BIMRI version",
                    result.stdout + result.stderr,
                )
        self.assertNotIn(
            "[K:forged.resolution]",
            self.hot(root=resolution_root),
        )

    def test_semantically_forged_terminal_authority_fails_closed(self):
        for outcome in ("accepted", "noop"):
            with self.subTest(outcome=outcome):
                root = self.root / f"semantic-{outcome}"
                run_id = self.start(f"semantic-{outcome}", root=root)
                proposal_id = self.propose(
                    run_id,
                    f"semantic.{outcome}",
                    "A terminal record must be bound to its immutable effect.",
                    root=root,
                )
                decision = {
                    "bimri_version": "5.0",
                    "proposal_id": proposal_id,
                    "outcome": outcome,
                    "recorded_at": "2026-07-27T00:00:00Z",
                    "revision": 0,
                }
                if outcome == "noop":
                    decision["reason"] = "current memory already matches"
                (
                    root
                    / ".bimri"
                    / "decisions"
                    / f"{proposal_id}.json"
                ).write_text(
                    json.dumps(decision, indent=2, sort_keys=True) + "\n",
                    "utf-8",
                )

                for command in (
                    ("sync", "--run", run_id),
                    ("doctor",),
                ):
                    failed = self.cli(*command, root=root, check=False)
                    self.assertNotEqual(failed.returncode, 0)
                    self.assertIn(
                        "does not contain the proposal's recorded effect",
                        failed.stdout + failed.stderr,
                    )
                self.assertEqual(self.state(root=root)["head_revision"], 0)
                self.assertNotIn(
                    f"[K:semantic.{outcome}]",
                    self.hot(root=root),
                )

        resolved_root = self.root / "semantic-resolution"
        resolved_run = self.start("semantic-resolution", root=resolved_root)
        proposal_id = self.propose(
            resolved_run,
            "semantic.resolution",
            "A resolved candidate must exist in its recorded revision.",
            tier=1,
            source="agent",
            trust="working",
            extra=("--kind", "decision"),
            root=resolved_root,
        )
        self.cli("sync", "--run", resolved_run, root=resolved_root)
        decision = self.decision(proposal_id, root=resolved_root)
        conflict_id = decision["conflict_id"]
        conflict = json.loads(
            (
                resolved_root
                / ".bimri"
                / "conflicts"
                / f"{conflict_id}.json"
            ).read_text("utf-8")
        )
        head = self.state(root=resolved_root)["head_revision"]
        forged = {
            "bimri_version": "5.0",
            "conflict_id": conflict_id,
            "choice": proposal_id,
            "status": "resolved",
            "started_at": "2026-07-27T00:00:00Z",
            "resolved_at": "2026-07-27T00:00:01Z",
            "by": "user",
            "proposal_ids": conflict["proposal_ids"],
            "revision_before": head,
            "revision_after": head,
        }
        (
            resolved_root
            / ".bimri"
            / "resolutions"
            / f"{conflict_id}.json"
        ).write_text(
            json.dumps(forged, indent=2, sort_keys=True) + "\n",
            "utf-8",
        )
        for command in (
            ("sync", "--run", resolved_run),
            ("status",),
            ("doctor",),
        ):
            failed = self.cli(*command, root=resolved_root, check=False)
            self.assertNotEqual(failed.returncode, 0)
            self.assertIn(
                "does not contain the chosen proposal's effect",
                failed.stdout + failed.stderr,
            )
        self.assertEqual(
            self.state(root=resolved_root)["head_revision"], head
        )
        self.assertNotIn(
            "[K:semantic.resolution]",
            self.hot(root=resolved_root),
        )

    def test_path_traversal_proposal_id_cannot_touch_external_file(self):
        project = self.root / "traversal-project"
        sentinel = self.root / "outside-proposal-target.json"
        original = "external sentinel remains untouched\n"
        sentinel.write_text(original, "utf-8")
        run_id = self.start("codex", root=project)
        proposal_id = self.propose(
            run_id,
            "traversal.proposal-id",
            "A legitimate proposal body.",
            root=project,
        )
        path = (
            project / ".bimri" / "proposals" / f"{proposal_id}.json"
        )
        proposal = json.loads(path.read_text("utf-8"))
        proposal["proposal_id"] = "../../../outside-proposal-target"
        path.write_text(
            json.dumps(proposal, indent=2, sort_keys=True) + "\n",
            "utf-8",
        )

        result = self.cli(
            "sync",
            "--run",
            run_id,
            root=project,
            check=False,
        )
        self.assertEqual(result.returncode, 2)
        self.assertIn("proposal ID mismatch", result.stderr)
        self.assertNotIn("Traceback", result.stderr)
        self.assertEqual(sentinel.read_text("utf-8"), original)
        self.assertEqual(
            list((project / ".bimri" / "decisions").glob("*.json")),
            [],
        )
        self.assertEqual(self.state(root=project)["head_revision"], 0)

    def test_explicit_key_and_target_must_identify_the_same_entry(self):
        creator = self.start("codex")
        first = self.propose(creator, "entry.first", "The first value.")
        second = self.propose(creator, "entry.second", "The second value.")
        self.cli("sync", "--run", creator)
        self.assertEqual(self.decision(first)["outcome"], "accepted")
        self.assertEqual(self.decision(second)["outcome"], "accepted")

        second_line = next(
            line for line in self.hot().splitlines() if "[K:entry.second]" in line
        )
        second_id = re.match(r"\[([^\]]+)\]", second_line).group(1)
        run_id = self.start("claude")
        proposals_before = set(
            (self.root / ".bimri" / "proposals").glob("*.json")
        )
        mismatch = self.cli(
            "propose",
            "--run",
            run_id,
            "--tier",
            "2",
            "--key",
            "entry.first",
            "--target",
            second_id,
            "--text",
            "This must not replace either entry.",
            "--source",
            "user",
            "--trust",
            "confirmed",
            check=False,
        )
        self.assertEqual(mismatch.returncode, 2)
        self.assertIn("belongs to key entry.second, not entry.first", mismatch.stderr)
        self.assertEqual(
            set((self.root / ".bimri" / "proposals").glob("*.json")),
            proposals_before,
        )
        self.assertIn("The first value.", self.hot())
        self.assertIn("The second value.", self.hot())

    def test_more_than_twelve_unique_tags_are_rejected(self):
        run_id = self.start("codex")
        tags = ",".join(f"tag-{number}" for number in range(13))
        result = self.cli(
            "propose",
            "--run",
            run_id,
            "--tier",
            "2",
            "--key",
            "tag.limit",
            "--text",
            "This proposal has too many tags.",
            "--tags",
            tags,
            "--source",
            "user",
            "--trust",
            "confirmed",
            check=False,
        )
        self.assertEqual(result.returncode, 2)
        self.assertIn("at most 12 unique tags", result.stderr)
        self.assertEqual(
            list((self.root / ".bimri" / "proposals").glob("*.json")),
            [],
        )
        log = (
            self.root / ".bimri" / "log" / f"{run_id}.md"
        ).read_text("utf-8")
        self.assertNotIn("This proposal has too many tags.", log)

    def test_corrupt_state_fails_closed_and_is_not_replaced(self):
        self.cli("migrate")
        state_path = self.root / ".bimri" / "state.json"
        corrupt = "{ broken state; preserve me"
        state_path.write_text(corrupt, "utf-8")
        hot_before = self.hot()
        logs_before = list((self.root / ".bimri" / "log").glob("*.md"))

        result = self.cli("start", "--actor", "codex", check=False)
        self.assertEqual(result.returncode, 2)
        self.assertIn("state.json is unreadable", result.stderr)
        self.assertIn("stopped without resetting it", result.stderr)
        self.assertEqual(state_path.read_text("utf-8"), corrupt)
        self.assertEqual(self.hot(), hot_before)
        self.assertEqual(list((self.root / ".bimri" / "log").glob("*.md")), logs_before)

    def test_existing_state_never_authorizes_flat_legacy_reset(self):
        source = self.legacy_v3_bytes("State precedence must remain fail-closed.")
        cases = {
            "malformed": b"{ broken state; preserve me",
            "non-object": b"[]\n",
            "unknown-schema": b"{}\n",
            "unsupported-version": json.dumps({
                "bimri_version": "5.1",
            }).encode("utf-8") + b"\n",
        }
        for label, state_bytes in cases.items():
            with self.subTest(label=label):
                root = self.root / label
                bdir = root / ".bimri"
                bdir.mkdir(parents=True)
                (root / "BIMRI.md").write_bytes(source)
                state_path = bdir / "state.json"
                state_path.write_bytes(state_bytes)

                result = self.cli("migrate", root=root, check=False)

                self.assertEqual(result.returncode, 2)
                self.assertEqual(state_path.read_bytes(), state_bytes)
                self.assertEqual((root / "BIMRI.md").read_bytes(), source)
                self.assertEqual(list((bdir / "revisions").glob("V*.md")), [])
                self.assertFalse(
                    (bdir / "migrations" / "legacy-to-v5.json").exists()
                )

    def test_invalid_utf8_and_malformed_v4_run_count_exit_two_without_traceback(self):
        invalid_utf8_root = self.root / "invalid-utf8-v4"
        invalid_bdir = invalid_utf8_root / ".bimri"
        invalid_bdir.mkdir(parents=True)
        (invalid_bdir / "state.json").write_text(
            json.dumps({
                "bimri_version": "4.0",
                "project_id": "invalid-utf8",
                "run_count": 0,
                "current_run_id": "R000",
            }) + "\n",
            "utf-8",
        )
        (invalid_utf8_root / "bimri.md").write_bytes(
            b"# BIMRI Memory\n\xff\xfe"
        )
        invalid = self.cli(
            "migrate",
            root=invalid_utf8_root,
            check=False,
        )
        self.assertEqual(invalid.returncode, 2)
        self.assertIn("BIMRI ERROR:", invalid.stderr)
        self.assertNotIn("Traceback", invalid.stderr)

        malformed_count_root = self.root / "malformed-count-v4"
        malformed_bdir = malformed_count_root / ".bimri"
        malformed_bdir.mkdir(parents=True)
        legacy_hot = """# BIMRI Memory

## Tier 1: Core Intelligence

## Tier 2: Active Context

## Tier 3: Pattern Recognition

<!-- END BIMRI -->
"""
        (malformed_count_root / "bimri.md").write_text(
            legacy_hot,
            "utf-8",
        )
        original_state = json.dumps({
            "bimri_version": "4.0",
            "project_id": "malformed-count",
            "run_count": "seven",
            "current_run_id": "R000",
        }, indent=2) + "\n"
        state_path = malformed_bdir / "state.json"
        state_path.write_text(original_state, "utf-8")
        malformed = self.cli(
            "migrate",
            root=malformed_count_root,
            check=False,
        )
        self.assertEqual(malformed.returncode, 2)
        self.assertIn(
            "v4 state run_count must be a non-negative integer",
            malformed.stderr,
        )
        self.assertNotIn("Traceback", malformed.stderr)
        self.assertEqual(state_path.read_text("utf-8"), original_state)
        self.assertEqual(
            list((malformed_bdir / "revisions").glob("V*.md")),
            [],
        )

    def test_v4_state_without_hot_memory_fails_closed(self):
        bdir = self.root / ".bimri"
        bdir.mkdir()
        legacy_state = {
            "bimri_version": "4.0",
            "project_id": "missing-hot",
            "run_count": 7,
            "current_run_id": "R000",
        }
        state_path = bdir / "state.json"
        original_state = json.dumps(legacy_state, indent=2) + "\n"
        state_path.write_text(original_state, "utf-8")

        result = self.cli("migrate", check=False)
        self.assertEqual(result.returncode, 2)
        self.assertIn("v4 state exists but bimri.md is missing", result.stderr)
        self.assertIn("stopped without creating replacement memory", result.stderr)
        self.assertFalse((self.root / "bimri.md").exists())
        self.assertEqual(state_path.read_text("utf-8"), original_state)
        self.assertEqual(
            list((bdir / "revisions").glob("V*.md")),
            [],
        )
        self.assertFalse((bdir / "migrations" / "v4-to-v5.json").exists())

    def test_unrelated_hot_markdown_cannot_initialize_as_memory(self):
        unrelated = (
            "# Project Notes\n\n"
            "This is an ordinary Markdown file with no BIMRI tier headings.\n"
        )
        hot_path = self.root / "bimri.md"
        hot_path.write_text(unrelated, "utf-8")

        result = self.cli("doctor", check=False)
        self.assertEqual(result.returncode, 2)
        self.assertIn("initial hot memory is invalid", result.stderr)
        self.assertIn("exactly one ordered Tier 1", result.stderr)
        self.assertEqual(hot_path.read_text("utf-8"), unrelated)
        self.assertFalse((self.root / ".bimri" / "state.json").exists())
        self.assertEqual(
            list((self.root / ".bimri" / "revisions").glob("V*.md")),
            [],
        )

    def test_manual_hot_edit_is_preserved_restored_and_raised_once(self):
        self.cli("migrate")
        canonical = (
            self.root / ".bimri" / "revisions" / "V000000.md"
        ).read_text("utf-8")
        first_manual = canonical.replace(
            "<!-- END BIMRI -->",
            "A human typed the first unstructured note.\n\n<!-- END BIMRI -->",
        )
        second_manual = canonical.replace(
            "<!-- END BIMRI -->",
            "A human typed a different second note.\n\n<!-- END BIMRI -->",
        )

        (self.root / "bimri.md").write_text("", "utf-8")
        first = self.cli("doctor")
        self.assertIn("BIMRI doctor: PASSED", first.stdout)
        self.assertIn("BIMRI NOTICE:", first.stderr)
        self.assertIn("direct edit to bimri.md was preserved", first.stderr)
        self.assertEqual(self.hot(), canonical)

        (self.root / "bimri.md").write_text(first_manual, "utf-8")
        second = self.cli("status")
        self.assertIn("BIMRI NOTICE:", second.stderr)
        self.assertEqual(self.hot(), canonical)

        (self.root / "bimri.md").write_text(second_manual, "utf-8")
        third = self.cli("doctor")
        self.assertIn("BIMRI NOTICE:", third.stderr)
        self.assertEqual(self.hot(), canonical)

        recovery_files = sorted(
            (self.root / ".bimri" / "recovery").glob("manual-hot-*.md")
        )
        conflicts = list((self.root / ".bimri" / "conflicts").glob("C*.json"))
        self.assertEqual(len(recovery_files), 3)
        self.assertEqual(
            {path.read_text("utf-8") for path in recovery_files},
            {"", first_manual, second_manual},
        )
        self.assertEqual(len(conflicts), 1)
        conflict = json.loads(conflicts[0].read_text("utf-8"))
        self.assertEqual(conflict["type"], "manual-edit")
        self.assertEqual(conflict["key"], "manual.bimri")
        recorded = conflict["extra"]["recovery_files"]
        self.assertEqual(
            set(recorded),
            {
                str(path.relative_to(self.root))
                for path in recovery_files
            },
        )
        self.assertIn(conflict["extra"]["recovery_file"], recorded)

        stable = self.cli("doctor")
        self.assertIn("BIMRI doctor: PASSED", stable.stdout)
        self.assertNotIn("BIMRI NOTICE:", stable.stderr)
        self.assertEqual(
            len(
                list(
                    (self.root / ".bimri" / "recovery").glob(
                        "manual-hot-*.md"
                    )
                )
            ),
            3,
        )

    def test_manual_hot_byte_edits_are_preserved_exactly(self):
        self.cli("migrate")
        hot_path = self.root / "bimri.md"
        revision_path = (
            self.root / ".bimri" / "revisions" / "V000000.md"
        )
        canonical = revision_path.read_bytes()
        edits = (
            canonical.replace(b"\n", b"\r\n"),
            b"\xff\xfeinvalid UTF-8 direct edit\r\n",
        )

        for edit in edits:
            hot_path.write_bytes(edit)
            result = self.cli("status")
            self.assertEqual(result.returncode, 0)
            self.assertIn("BIMRI NOTICE:", result.stderr)
            self.assertEqual(hot_path.read_bytes(), canonical)

        recovery_files = sorted(
            (self.root / ".bimri" / "recovery").glob("manual-hot-*")
        )
        self.assertEqual(len(recovery_files), 2)
        self.assertEqual(
            {path.read_bytes() for path in recovery_files},
            set(edits),
        )
        conflicts = list(
            (self.root / ".bimri" / "conflicts").glob("C*.json")
        )
        self.assertEqual(len(conflicts), 1)
        conflict = json.loads(conflicts[0].read_text("utf-8"))
        self.assertEqual(
            set(conflict["extra"]["recovery_files"]),
            {
                str(path.relative_to(self.root))
                for path in recovery_files
            },
        )

        revision_path.write_bytes(canonical.replace(b"\n", b"\r\n"))
        corrupted = self.cli("status", check=False)
        self.assertEqual(corrupted.returncode, 2)
        self.assertIn(
            "state head hash does not match the head revision",
            corrupted.stderr,
        )

    def test_v4_migration_preserves_memory_and_is_idempotent(self):
        bdir = self.root / ".bimri"
        log_dir = bdir / "log"
        log_dir.mkdir(parents=True)
        legacy_hot = """# BIMRI Memory

## Tier 1: Core Intelligence

[R3-E1] [fact] [legacy] Legacy fact -> .bimri/log/R3.md

## Tier 2: Active Context

[R3-E2] [I:4] [active] [F:R3] [L:R3] [legacy] Legacy task -> .bimri/log/R3.md

## Tier 3: Pattern Recognition

[P1] [emerging] [obs:2] [ev:R3-E1,R3-E2] Legacy pattern | Falsify: contrary evidence

<!-- END BIMRI -->
"""
        (self.root / "bimri.md").write_text(legacy_hot, "utf-8")
        legacy_state = {
            "bimri_version": "4.0",
            "project_id": "legacy-project",
            "run_count": 3,
            "current_run_id": "R3",
            "cadence_class": "interactive",
            "tier1_max": 12,
            "tier2_max": 20,
            "tier2_hard": 26,
            "tier3_max": 8,
        }
        (bdir / "state.json").write_text(
            json.dumps(legacy_state, indent=2) + "\n", "utf-8"
        )
        (log_dir / "R3.md").write_text(
            "# Legacy run R3\n\n[CLOSED:R3 2026-07-26T00:00:00Z]\n",
            "utf-8",
        )

        first = self.cli("migrate")
        self.assertIn("complete at v5.0", first.stdout)
        state = self.state()
        self.assertEqual(state["bimri_version"], "5.0")
        self.assertEqual(state["project_id"], "legacy-project")
        self.assertEqual(state["run_count"], 3)
        converted = self.hot()
        self.assertIn("[K:legacy.r3-e1]", converted)
        self.assertIn("[T:working] [SRC:legacy]", converted)
        self.assertIn("[K:legacy.r3-e2]", converted)
        self.assertIn("[K:legacy.pattern-p1]", converted)
        self.assertEqual((bdir / "revisions" / "V000000.md").read_text("utf-8"), converted)
        marker_path = bdir / "migrations" / "v4-to-v5.json"
        marker_before = marker_path.read_bytes()
        backups_before = sorted(path.name for path in (bdir / "backups").iterdir())
        revisions_before = sorted(path.name for path in (bdir / "revisions").iterdir())

        second = self.cli("migrate")
        self.assertIn("complete at v5.0", second.stdout)
        self.assertEqual(marker_path.read_bytes(), marker_before)
        self.assertEqual(
            sorted(path.name for path in (bdir / "backups").iterdir()),
            backups_before,
        )
        self.assertEqual(
            sorted(path.name for path in (bdir / "revisions").iterdir()),
            revisions_before,
        )
        next_run = self.start("codex")
        self.assertEqual(next_run, "R000004")
        self.assertTrue((log_dir / "R3.md").exists())

    def test_tier3_reserved_delimiter_is_rejected_and_migrated_p1_updates(self):
        delimiter_root = self.root / "delimiter-project"
        run_id = self.start("codex", root=delimiter_root)
        reserved = self.cli(
            "propose",
            "--run",
            run_id,
            "--tier",
            "3",
            "--key",
            "pattern.invalid",
            "--text",
            "Hypothesis | Falsify: text smuggled into the hypothesis",
            "--confidence",
            "emerging",
            "--observations",
            "2",
            "--falsifier",
            "A legitimate falsification condition.",
            "--source",
            "user",
            "--trust",
            "confirmed",
            root=delimiter_root,
            check=False,
        )
        self.assertEqual(reserved.returncode, 2)
        self.assertIn("reserved delimiter", reserved.stderr)
        self.assertEqual(
            list((delimiter_root / ".bimri" / "proposals").glob("*.json")),
            [],
        )
        self.assertEqual(self.state(root=delimiter_root)["pattern_count"], 0)

        legacy_root = self.root / "legacy-pattern-project"
        bdir = legacy_root / ".bimri"
        log_dir = bdir / "log"
        log_dir.mkdir(parents=True)
        legacy_hot = """# BIMRI Memory

## Tier 1: Core Intelligence

[R1-E1] [fact] [legacy] First observation -> .bimri/log/R1.md
[R1-E2] [fact] [legacy] Second observation -> .bimri/log/R1.md

## Tier 2: Active Context

## Tier 3: Pattern Recognition

[P1] [emerging] [obs:2] [ev:R1-E1,R1-E2] Original hypothesis | Falsify: original condition

<!-- END BIMRI -->
"""
        (legacy_root / "bimri.md").write_text(legacy_hot, "utf-8")
        (bdir / "state.json").write_text(
            json.dumps({
                "bimri_version": "4.0",
                "project_id": "legacy-pattern",
                "run_count": 1,
                "current_run_id": "R1",
            }, indent=2) + "\n",
            "utf-8",
        )
        (log_dir / "R1.md").write_text(
            "# Legacy run\n\n[CLOSED:R1 2026-07-26T00:00:00Z]\n",
            "utf-8",
        )
        self.cli("migrate", root=legacy_root)

        update_run = self.start("codex", root=legacy_root)
        update = self.cli(
            "propose",
            "--run",
            update_run,
            "--tier",
            "3",
            "--key",
            "legacy.pattern-p1",
            "--target",
            "P1",
            "--text",
            "Updated hypothesis",
            "--confidence",
            "developing",
            "--observations",
            "3",
            "--evidence",
            "R1-E1,R1-E2",
            "--falsifier",
            "New contrary evidence appears.",
            "--source",
            "user",
            "--trust",
            "confirmed",
            root=legacy_root,
        )
        proposal_id = PROPOSAL_RE.search(update.stdout).group(0)
        self.cli("sync", "--run", update_run, root=legacy_root)
        self.assertEqual(
            self.decision(proposal_id, root=legacy_root)["outcome"],
            "accepted",
        )
        pattern_lines = [
            line
            for line in self.hot(root=legacy_root).splitlines()
            if "[K:legacy.pattern-p1]" in line
        ]
        self.assertEqual(len(pattern_lines), 1)
        self.assertTrue(pattern_lines[0].startswith("[P1] "), pattern_lines[0])
        self.assertIn("[developing] [obs:3]", pattern_lines[0])
        self.assertIn("Updated hypothesis", pattern_lines[0])

    def test_install_preserves_project_files_and_engine_root_is_portable(self):
        target = self.root / "target-project"
        elsewhere = self.root / "unrelated-working-directory"
        target.mkdir()
        elsewhere.mkdir()
        original_agents = "# Existing project rules\n\nKeep this sentence.\n"
        original_claude = "# Existing Claude rules\n\nKeep Claude's sentence.\n"
        original_protocol = "locally customized old protocol\n"
        original_license = "This is the target project's own license.\n"
        legacy_keep = "Unrelated historical material stays here.\n"
        (target / "AGENTS.md").write_text(original_agents, "utf-8")
        (target / "CLAUDE.md").write_text(original_claude, "utf-8")
        (target / "BIMRI-PROTOCOL.md").write_text(original_protocol, "utf-8")
        (target / "LICENSE").write_text(original_license, "utf-8")
        (target / "legacy").mkdir()
        (target / "legacy" / "keep.txt").write_text(legacy_keep, "utf-8")

        install = subprocess.run(
            [sys.executable, str(ENGINE), "install", "--target", str(target)],
            cwd=str(elsewhere),
            text=True,
            capture_output=True,
            timeout=30,
        )
        self.assertEqual(install.returncode, 0, install.stdout + install.stderr)
        self.assertIn("Doctor passed.", install.stdout)
        agents = (target / "AGENTS.md").read_text("utf-8")
        claude = (target / "CLAUDE.md").read_text("utf-8")
        self.assertIn("Keep this sentence.", agents)
        self.assertIn("Keep Claude's sentence.", claude)
        self.assertEqual((target / "LICENSE").read_text("utf-8"), original_license)
        self.assertEqual(
            (target / "BIMRI-LICENSE").read_bytes(),
            (REPOSITORY / "LICENSE").read_bytes(),
        )
        self.assertEqual(
            (target / "legacy" / "keep.txt").read_text("utf-8"), legacy_keep
        )
        for relative in (
            "legacy/README.md",
            "legacy/v1/BIMRI-global-instructions.md",
            "legacy/v3/BIMRI-global-instructions.md",
            "legacy/v3/BIMRI-global-instructions-v3.md",
            "legacy/v3/README.md",
        ):
            self.assertEqual(
                (target / relative).read_bytes(),
                (REPOSITORY / relative).read_bytes(),
            )
        self.assertEqual(agents.count("<!-- BIMRI:START -->"), 1)
        self.assertEqual(agents.count("<!-- BIMRI:END -->"), 1)
        self.assertEqual(claude.count("<!-- BIMRI:START -->"), 1)
        expected_agent_block = (
            REPOSITORY / "BIMRI-AGENT-BLOCK.md"
        ).read_text("utf-8").strip()
        installed_agent_block = agents.split(
            "<!-- BIMRI:START -->", 1
        )[1].split("<!-- BIMRI:END -->", 1)[0].strip()
        self.assertEqual(installed_agent_block, expected_agent_block)
        installed_claude_block = claude.split(
            "<!-- BIMRI:START -->", 1
        )[1].split("<!-- BIMRI:END -->", 1)[0].strip()
        self.assertIn("@AGENTS.md", installed_claude_block)
        self.assertIn(
            "Use BIMRI-PROTOCOL.md for the full memory protocol.",
            installed_claude_block,
        )
        backups = list(
            (target / ".bimri" / "install-backups").glob("*/BIMRI-PROTOCOL.md")
        )
        self.assertEqual(len(backups), 1)
        self.assertEqual(backups[0].read_text("utf-8"), original_protocol)

        second_install = subprocess.run(
            [sys.executable, str(ENGINE), "install", "--target", str(target)],
            cwd=str(elsewhere),
            text=True,
            capture_output=True,
            timeout=30,
        )
        self.assertEqual(
            second_install.returncode,
            0,
            second_install.stdout + second_install.stderr,
        )
        self.assertEqual(
            (target / "AGENTS.md").read_text("utf-8").count("<!-- BIMRI:START -->"),
            1,
        )
        self.assertEqual(
            (target / "CLAUDE.md").read_text("utf-8").count("<!-- BIMRI:START -->"),
            1,
        )

        installed_engine = target / "bimri-engine.py"
        portable_start = subprocess.run(
            [sys.executable, str(installed_engine), "start", "--actor", "portable"],
            cwd=str(elsewhere),
            text=True,
            capture_output=True,
            timeout=30,
        )
        self.assertEqual(
            portable_start.returncode,
            0,
            portable_start.stdout + portable_start.stderr,
        )
        self.assertRegex(portable_start.stdout, r"=== BIMRI BRIEF R\d{6}")
        self.assertTrue((target / ".bimri" / "state.json").is_file())
        self.assertFalse((elsewhere / ".bimri").exists())
        self.assertFalse((elsewhere / "bimri.md").exists())

    def test_concurrent_installs_serialize_and_leave_one_coherent_adapter(self):
        target = self.root / "concurrent-install"
        target.mkdir()
        (target / "AGENTS.md").write_text(
            "# Existing rules\n\nKeep this content.\n",
            "utf-8",
        )

        def install(_number):
            return subprocess.run(
                [
                    sys.executable,
                    str(ENGINE),
                    "install",
                    "--target",
                    str(target),
                ],
                text=True,
                capture_output=True,
                timeout=60,
            )

        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as executor:
            results = list(executor.map(install, range(2)))
        self.assertEqual(
            [result.returncode for result in results],
            [0, 0],
            "\n\n".join(
                result.stdout + result.stderr for result in results
            ),
        )
        agents = (target / "AGENTS.md").read_text("utf-8")
        self.assertIn("Keep this content.", agents)
        self.assertEqual(agents.count("<!-- BIMRI:START -->"), 1)
        self.assertEqual(agents.count("<!-- BIMRI:END -->"), 1)
        block = (REPOSITORY / "BIMRI-AGENT-BLOCK.md").read_text(
            "utf-8"
        ).strip()
        self.assertEqual(
            agents.split("<!-- BIMRI:START -->", 1)[1].split(
                "<!-- BIMRI:END -->", 1
            )[0].strip(),
            block,
        )
        manifests = sorted(
            (target / ".bimri" / "install-backups").glob(
                "*/install-manifest.json"
            )
        )
        self.assertEqual(len(manifests), 2)
        self.assertEqual(
            {
                json.loads(path.read_text("utf-8"))["status"]
                for path in manifests
            },
            {"installed"},
        )
        self.assertIn("BIMRI doctor: PASSED", self.cli(
            "doctor",
            root=target,
        ).stdout)

    def test_install_refuses_active_v4_writer_before_replacing_files(self):
        target = self.root / "active-v4-install"
        bdir = target / ".bimri"
        log_dir = bdir / "log"
        log_dir.mkdir(parents=True)
        hot = """# BIMRI Memory

## Tier 1: Core Intelligence

## Tier 2: Active Context

## Tier 3: Pattern Recognition

<!-- END BIMRI -->
"""
        state = {
            "bimri_version": "4.0",
            "project_id": "active-v4",
            "run_count": 1,
            "current_run_id": "R1",
            "last_started_at": "2026-07-27T00:00:00Z",
        }
        (target / "bimri.md").write_text(hot, "utf-8")
        state_path = bdir / "state.json"
        state_bytes = (json.dumps(state, indent=2) + "\n").encode("utf-8")
        state_path.write_bytes(state_bytes)
        (log_dir / "R1.md").write_text(
            "# Active v4 run without a close marker\n",
            "utf-8",
        )
        agent_sentinel = "# Existing active-v4 rules\n"
        (target / "AGENTS.md").write_text(agent_sentinel, "utf-8")

        result = subprocess.run(
            [
                sys.executable,
                str(ENGINE),
                "install",
                "--target",
                str(target),
            ],
            text=True,
            capture_output=True,
            timeout=30,
        )
        self.assertEqual(result.returncode, 2)
        self.assertIn("v4 run R1 appears active", result.stderr)
        self.assertIn("does not participate in the v5 lock", result.stderr)
        self.assertEqual(state_path.read_bytes(), state_bytes)
        self.assertEqual((target / "bimri.md").read_text("utf-8"), hot)
        self.assertEqual(
            (target / "AGENTS.md").read_text("utf-8"),
            agent_sentinel,
        )
        self.assertFalse((target / "bimri-engine.py").exists())
        self.assertEqual(
            list((bdir / "install-backups").glob("*")),
            [],
        )

    def test_install_refuses_ambiguous_v4_current_run_without_log(self):
        target = self.root / "ambiguous-v4-install"
        bdir = target / ".bimri"
        bdir.mkdir(parents=True)
        hot = """# BIMRI Memory

## Tier 1: Core Intelligence

## Tier 2: Active Context

## Tier 3: Pattern Recognition

<!-- END BIMRI -->
"""
        state = {
            "bimri_version": "4.0",
            "project_id": "ambiguous-v4",
            "run_count": 1,
            "current_run_id": "R1",
        }
        (target / "bimri.md").write_text(hot, "utf-8")
        state_path = bdir / "state.json"
        state_bytes = (json.dumps(state, indent=2) + "\n").encode("utf-8")
        state_path.write_bytes(state_bytes)
        agent_sentinel = "# Existing ambiguous-v4 rules\n"
        (target / "AGENTS.md").write_text(agent_sentinel, "utf-8")

        result = subprocess.run(
            [
                sys.executable,
                str(ENGINE),
                "install",
                "--target",
                str(target),
            ],
            text=True,
            capture_output=True,
            timeout=30,
        )
        self.assertEqual(result.returncode, 2)
        self.assertIn("v4 run R1 appears active", result.stderr)
        self.assertEqual(state_path.read_bytes(), state_bytes)
        self.assertEqual((target / "bimri.md").read_text("utf-8"), hot)
        self.assertEqual(
            (target / "AGENTS.md").read_text("utf-8"),
            agent_sentinel,
        )
        self.assertFalse((target / "bimri-engine.py").exists())
        self.assertFalse((bdir / "install-backups").exists())

    def test_failed_post_migration_selfcheck_rolls_back_coherently(self):
        target = self.root / "rollback-v4-install"
        bdir = target / ".bimri"
        log_dir = bdir / "log"
        log_dir.mkdir(parents=True)
        legacy_hot = """# BIMRI Memory

## Tier 1: Core Intelligence

[R1-E1] [fact] [legacy] Preserve this v4 fact -> .bimri/log/R1.md

## Tier 2: Active Context

## Tier 3: Pattern Recognition

<!-- END BIMRI -->
"""
        legacy_state = {
            "bimri_version": "4.0",
            "project_id": "rollback-v4",
            "run_count": 1,
            "current_run_id": "R1",
        }
        legacy_log = (
            "# Legacy run R1\n\n"
            "[OUTCOME:success] Already closed.\n"
            "[CLOSED:R1 2026-07-26T00:00:00Z]\n"
        )
        state_bytes = (
            json.dumps(legacy_state, indent=2) + "\n"
        ).encode("utf-8")
        (target / "bimri.md").write_text(legacy_hot, "utf-8")
        (bdir / "state.json").write_bytes(state_bytes)
        (log_dir / "R1.md").write_text(legacy_log, "utf-8")
        original_files = {
            "AGENTS.md": "# Existing AGENTS rules\n",
            "CLAUDE.md": "# Existing CLAUDE rules\n",
            "BIMRI-PROTOCOL.md": "custom v4 protocol\n",
        }
        for name, content in original_files.items():
            (target / name).write_text(content, "utf-8")
        legacy_keep = "Keep this unrelated legacy file.\n"
        (target / "legacy").mkdir()
        (target / "legacy" / "keep.txt").write_text(legacy_keep, "utf-8")

        result = self.worker(
            "install_selfcheck",
            "install",
            "--target",
            target,
            engine_root=REPOSITORY,
            check=False,
            timeout=60,
        )
        self.assertEqual(result.returncode, 2)
        self.assertIn("target files were rolled back", result.stderr)
        self.assertIn("forced post-migration self-check failure", result.stderr)
        self.assertIn(".bimri/install-backups/", result.stderr)
        self.assertEqual((target / "bimri.md").read_text("utf-8"), legacy_hot)
        self.assertEqual((bdir / "state.json").read_bytes(), state_bytes)
        self.assertEqual((log_dir / "R1.md").read_text("utf-8"), legacy_log)
        for name, content in original_files.items():
            self.assertEqual((target / name).read_text("utf-8"), content)
        self.assertEqual(
            (target / "legacy" / "keep.txt").read_text("utf-8"), legacy_keep
        )
        self.assertFalse((target / "legacy" / "v1").exists())
        self.assertFalse((target / "legacy" / "v3").exists())
        self.assertFalse((target / "BIMRI-LICENSE").exists())
        self.assertFalse((target / "bimri-engine.py").exists())
        self.assertEqual(list((bdir / "revisions").glob("V*.md")), [])
        self.assertEqual(
            list((bdir / "migrations").glob("*.json")),
            [],
        )
        manifests = list(
            (bdir / "install-backups").glob("*/install-manifest.json")
        )
        self.assertEqual(len(manifests), 1)
        manifest = json.loads(manifests[0].read_text("utf-8"))
        self.assertEqual(manifest["status"], "rolled-back")

    def test_hot_view_and_install_backup_symlinks_fail_before_mutation(self):
        hot_project = self.root / "hot-symlink-project"
        hot_project.mkdir()
        external_hot = self.root / "external-hot.md"
        external_content = "outside sentinel\n"
        external_hot.write_text(external_content, "utf-8")
        try:
            (hot_project / "bimri.md").symlink_to(external_hot)
        except (NotImplementedError, OSError) as exc:
            self.skipTest(f"symbolic links unavailable: {exc}")

        hot_result = self.cli("migrate", root=hot_project, check=False)
        self.assertEqual(hot_result.returncode, 2)
        self.assertIn("bimri.md cannot be a symbolic link", hot_result.stderr)
        self.assertTrue((hot_project / "bimri.md").is_symlink())
        self.assertEqual(external_hot.read_text("utf-8"), external_content)
        self.assertFalse((hot_project / ".bimri" / "state.json").exists())
        self.assertEqual(
            list((hot_project / ".bimri" / "revisions").glob("V*.md")),
            [],
        )

        install_project = self.root / "install-symlink-project"
        bdir = install_project / ".bimri"
        bdir.mkdir(parents=True)
        external_backups = self.root / "external-install-backups"
        external_backups.mkdir()
        (bdir / "install-backups").symlink_to(
            external_backups,
            target_is_directory=True,
        )
        protocol_path = install_project / "BIMRI-PROTOCOL.md"
        agents_path = install_project / "AGENTS.md"
        protocol_sentinel = "custom protocol sentinel\n"
        agents_sentinel = "custom agent sentinel\n"
        protocol_path.write_text(protocol_sentinel, "utf-8")
        agents_path.write_text(agents_sentinel, "utf-8")

        install_result = self.cli(
            "install",
            "--target",
            install_project,
            check=False,
        )
        self.assertEqual(install_result.returncode, 2)
        self.assertIn(
            "target .bimri/install-backups cannot be a symbolic link",
            install_result.stderr,
        )
        self.assertEqual(protocol_path.read_text("utf-8"), protocol_sentinel)
        self.assertEqual(agents_path.read_text("utf-8"), agents_sentinel)
        self.assertEqual(list(external_backups.iterdir()), [])
        self.assertFalse((install_project / "bimri-engine.py").exists())
        self.assertTrue(os.path.islink(bdir / "install-backups"))

    def test_install_rejects_redirected_legacy_directory(self):
        target = self.root / "redirected-legacy-target"
        external = self.root / "external-legacy-directory"
        target.mkdir()
        external.mkdir()
        sentinel = external / "keep.txt"
        sentinel.write_text("outside content must remain untouched\n", "utf-8")
        try:
            (target / "legacy").symlink_to(external, target_is_directory=True)
        except (NotImplementedError, OSError) as exc:
            self.skipTest(f"symbolic links unavailable: {exc}")

        result = self.cli("install", "--target", target, check=False)

        self.assertEqual(result.returncode, 2)
        self.assertIn("redirected directory: legacy", result.stderr)
        self.assertEqual(
            sentinel.read_text("utf-8"),
            "outside content must remain untouched\n",
        )
        self.assertEqual({path.name for path in external.iterdir()}, {"keep.txt"})
        self.assertFalse((target / "bimri-engine.py").exists())
        self.assertFalse((target / "BIMRI-LICENSE").exists())

    def test_doctor_detects_malformed_memory_and_pointer_escape(self):
        run_id = self.start("codex")
        proposal_id = self.propose(run_id, "safe.pointer", "A valid value.")
        self.cli("sync", "--run", run_id)
        self.assertEqual(self.decision(proposal_id)["outcome"], "accepted")

        state_path = self.root / ".bimri" / "state.json"
        state = self.state()
        revision_path = (
            self.root
            / ".bimri"
            / "revisions"
            / f"V{state['head_revision']:06d}.md"
        )
        escaped = revision_path.read_text("utf-8").replace(
            f".bimri/log/{run_id}.md", "../../outside.md"
        )
        revision_path.write_text(escaped, "utf-8")
        (self.root / "bimri.md").write_text(escaped, "utf-8")
        state["head_hash"] = hashlib.sha256(escaped.encode("utf-8")).hexdigest()
        state_path.write_text(json.dumps(state, indent=2, sort_keys=True) + "\n", "utf-8")

        pointer_result = self.cli("doctor", check=False)
        self.assertEqual(pointer_result.returncode, 1)
        self.assertIn("pointer escapes the BIMRI project", pointer_result.stdout)

        malformed = escaped.replace(
            "## Tier 2: Active Context",
            "## Tier 2: Active Context\nthis is malformed shared memory",
        )
        revision_path.write_text(malformed, "utf-8")
        (self.root / "bimri.md").write_text(malformed, "utf-8")
        state["head_hash"] = hashlib.sha256(malformed.encode("utf-8")).hexdigest()
        state_path.write_text(json.dumps(state, indent=2, sort_keys=True) + "\n", "utf-8")
        malformed_result = self.cli("doctor", check=False)
        self.assertEqual(malformed_result.returncode, 1)
        self.assertIn("malformed Tier 2 entry", malformed_result.stdout)

    def test_doctor_reports_applying_orphan_revision_and_temp_litter(self):
        self.cli("migrate")
        run_id = self.start("doctor-warnings")
        proposal_id = self.propose(
            run_id,
            "doctor.applying",
            "Leave a valid applying intent for doctor to report.",
        )
        proposal_path = (
            self.root / ".bimri" / "proposals" / f"{proposal_id}.json"
        )
        proposal = json.loads(proposal_path.read_text("utf-8"))
        decision_path = (
            self.root / ".bimri" / "decisions" / f"{proposal_id}.json"
        )
        decision_path.write_text(
            json.dumps({
                "bimri_version": "5.0",
                "proposal_id": proposal_id,
                "outcome": "applying",
                "recorded_at": "2020-01-01T00:00:00Z",
                "base_hash": proposal["base_hash"],
                "revision_before": 0,
            }, indent=2, sort_keys=True) + "\n",
            "utf-8",
        )
        revision_zero = (
            self.root / ".bimri" / "revisions" / "V000000.md"
        ).read_text("utf-8")
        (
            self.root / ".bimri" / "revisions" / "V000001.md"
        ).write_text(revision_zero, "utf-8")
        root_temp = self.root / ".bimri-tmp-crash-leftover"
        nested_temp = (
            self.root
            / ".bimri"
            / "proposals"
            / ".bimri-new-crash-leftover"
        )
        root_temp.write_text("temporary data\n", "utf-8")
        nested_temp.write_text("temporary data\n", "utf-8")

        doctor = self.cli("doctor")
        self.assertIn("BIMRI doctor: PASSED", doctor.stdout)
        self.assertIn(
            f"unfinished applying decision {proposal_id}",
            doctor.stdout,
        )
        self.assertIn(
            "unreferenced immutable revision V000001.md",
            doctor.stdout,
        )
        self.assertIn(
            "abandoned temporary file .bimri-tmp-crash-leftover",
            doctor.stdout,
        )
        self.assertIn(
            "abandoned temporary file "
            f"{nested_temp.relative_to(self.root)}",
            doctor.stdout,
        )

    def test_view_replace_permission_exhaustion_warns_after_durable_accept(self):
        run_id = self.start("windows-view")
        proposal_id = self.propose(
            run_id,
            "view.retry",
            "The durable revision must survive a locked generated view.",
        )
        hot_before = self.hot()

        result = self.worker(
            "view_permission",
            "sync",
            "--run",
            run_id,
            timeout=45,
        )
        self.assertIn("accepted 1, contested 0", result.stdout)
        self.assertIn(
            "BIMRI WARNING: bimri.md could not be refreshed",
            result.stderr,
        )
        self.assertIn(
            "accepted revision and state are durable",
            result.stderr,
        )
        decision = self.decision(proposal_id)
        self.assertEqual(decision["outcome"], "accepted")
        self.assertEqual(self.state()["head_revision"], 1)
        revision = (
            self.root / ".bimri" / "revisions" / "V000001.md"
        ).read_text("utf-8")
        self.assertIn("[K:view.retry]", revision)
        self.assertEqual(self.hot(), hot_before)
        self.assertNotIn("[K:view.retry]", self.hot())

        healed = self.cli("status")
        self.assertEqual(healed.returncode, 0)
        self.assertIn("[K:view.retry]", self.hot())
        self.assertEqual(
            self.hot(),
            revision,
        )

    def test_generic_post_commit_view_failure_warns_and_self_heals(self):
        run_id = self.start("generic-view")
        proposal_id = self.propose(
            run_id,
            "view.generic",
            "A generic final-view error cannot undo durable authority.",
        )
        hot_before = self.hot()

        result = self.worker(
            "view_oserror",
            "sync",
            "--run",
            run_id,
            timeout=30,
        )
        self.assertIn("accepted 1, contested 0", result.stdout)
        self.assertIn(
            "BIMRI WARNING: bimri.md could not be refreshed",
            result.stderr,
        )
        self.assertIn("forced generic generated-view failure", result.stderr)
        self.assertEqual(self.decision(proposal_id)["outcome"], "accepted")
        self.assertEqual(self.state()["head_revision"], 1)
        self.assertEqual(self.hot(), hot_before)

        healed = self.cli("status")
        self.assertEqual(healed.returncode, 0)
        self.assertIn("[K:view.generic]", self.hot())

    def test_post_commit_index_failures_warn_without_undoing_operations(self):
        start_root = self.root / "index-start"
        started = self.worker(
            "index_failure",
            "start",
            "--actor",
            "index-start",
            root=start_root,
        )
        start_match = re.search(
            r"=== BIMRI BRIEF (R\d{6})",
            started.stdout,
        )
        self.assertIsNotNone(start_match, started.stdout)
        start_run = start_match.group(1)
        self.assertIn("durable operation succeeded", started.stderr)
        self.assertIn("forced index failure", started.stderr)
        self.assertIn(
            start_run,
            self.state(root=start_root)["active_runs"],
        )

        sync_root = self.root / "index-sync"
        sync_run = self.start("index-sync", root=sync_root)
        sync_proposal = self.propose(
            sync_run,
            "index.sync",
            "Sync remains committed when indexing fails.",
            root=sync_root,
        )
        synced = self.worker(
            "index_failure",
            "sync",
            "--run",
            sync_run,
            root=sync_root,
        )
        self.assertIn("durable operation succeeded", synced.stderr)
        self.assertEqual(
            self.decision(sync_proposal, root=sync_root)["outcome"],
            "accepted",
        )
        self.assertIn("[K:index.sync]", self.hot(root=sync_root))

        close_root = self.root / "index-close"
        close_run = self.start("index-close", root=close_root)
        closed = self.worker(
            "index_failure",
            "close",
            "--run",
            close_run,
            "--outcome",
            "success",
            "--summary",
            "Close remains committed when indexing fails.",
            root=close_root,
        )
        self.assertIn("durable operation succeeded", closed.stderr)
        self.assertNotIn(
            close_run,
            self.state(root=close_root)["active_runs"],
        )
        close_log = (
            close_root / ".bimri" / "log" / f"{close_run}.md"
        ).read_text("utf-8")
        self.assertIn(f"[CLOSED:{close_run} ", close_log)

        resolve_root = self.root / "index-resolve"
        resolve_run = self.start("index-resolve", root=resolve_root)
        candidate = self.propose(
            resolve_run,
            "index.resolve",
            "Human approval should survive an index failure.",
            tier=1,
            source="agent",
            trust="working",
            extra=("--kind", "decision"),
            root=resolve_root,
        )
        self.cli(
            "sync",
            "--run",
            resolve_run,
            root=resolve_root,
        )
        contested = self.decision(candidate, root=resolve_root)
        self.assertEqual(contested["outcome"], "contested")
        resolved = self.worker(
            "index_failure",
            "resolve",
            contested["conflict_id"],
            "--choose",
            candidate,
            root=resolve_root,
        )
        self.assertIn("durable operation succeeded", resolved.stderr)
        self.assertEqual(
            self.decision(candidate, root=resolve_root)["outcome"],
            "accepted",
        )
        self.assertIn("[K:index.resolve]", self.hot(root=resolve_root))

    def test_index_and_doctor_are_deterministic(self):
        run_id = self.start("codex")
        first = self.propose(run_id, "zeta.item", "Zeta comes second alphabetically.")
        second = self.propose(run_id, "alpha.item", "Alpha comes first alphabetically.")
        self.cli("sync", "--run", run_id)
        self.assertEqual(self.decision(first)["outcome"], "accepted")
        self.assertEqual(self.decision(second)["outcome"], "accepted")

        first_index = self.cli("index")
        index_path = self.root / ".bimri" / "index.tsv"
        content_one = index_path.read_bytes()
        state_one = (self.root / ".bimri" / "state.json").read_bytes()
        revisions_one = sorted(
            path.read_bytes()
            for path in (self.root / ".bimri" / "revisions").glob("V*.md")
        )
        second_index = self.cli("index")
        content_two = index_path.read_bytes()
        self.assertEqual(first_index.stdout, second_index.stdout)
        self.assertEqual(content_one, content_two)

        rows = index_path.read_text("utf-8").splitlines()
        self.assertEqual(rows[0].split("\t"), [
            "id", "key", "loc", "trust", "source", "status", "file", "headline"
        ])
        self.assertTrue(all(len(row.split("\t")) == 8 for row in rows))
        ids = [row.split("\t")[0] for row in rows[1:]]
        self.assertEqual(ids, sorted(ids))

        first_doctor = self.cli("doctor")
        doctor_index_one = index_path.read_bytes()
        second_doctor = self.cli("doctor")
        self.assertEqual(first_doctor.stdout, second_doctor.stdout)
        self.assertIn("BIMRI doctor: PASSED", first_doctor.stdout)
        self.assertEqual(doctor_index_one, index_path.read_bytes())
        self.assertEqual(state_one, (self.root / ".bimri" / "state.json").read_bytes())
        self.assertEqual(
            revisions_one,
            sorted(
                path.read_bytes()
                for path in (self.root / ".bimri" / "revisions").glob("V*.md")
            ),
        )

    def test_v3_uppercase_migration_preserves_active_backup_and_authority_map(self):
        active = (
            "<!-- BIMRI v3.0 | Last Maintained: 2026-07-20 | Sessions: 12 | Token Est: ~900 -->\r\n"
            "<!-- Target: under 12,000 tokens | Hard Ceiling: 15,000 tokens -->\r\n\r\n"
            "# BIMRI: Memory File\r\n\r\n"
            "## Tier 1: Core Intelligence\r\n"
            "- The workspace publishes a weekly research brief.\r\n\r\n"
            "## Tier 2: Active Context\r\n"
            "[ID:T2-20260720-01] [IMP:4] [CREATED:2026-07-20] [SESSION:11] "
            "[LAST_USED:2026-07-21] [LAST_USED_SESSION:12] [TAGS:launch,research] [W:4.0]\r\n"
            "Verify the launch research before publication.\r\n\r\n"
            "## Tier 3: Pattern Recognition\r\n"
            "[PATTERN] [CONFIDENCE:DEVELOPING] [OBSERVATIONS:4] [TAGS:workflow]\r\n"
            "Research quality improves when a second agent checks sources.\r\n"
        ).encode("utf-8")
        rolling = (
            "<!-- BIMRI v3.0 | Last Maintained: 2026-07-19 | Sessions: 11 | Token Est: ~400 -->\n\n"
            "# BIMRI: Memory File\n\n"
            "## Tier 1: Core Intelligence\n"
            "- The workspace publishes a weekly research brief.\n\n"
            "## Tier 2: Active Context\n\n"
            "## Tier 3: Pattern Recognition\n"
        ).encode("utf-8")
        (self.root / "BIMRI.md").write_bytes(active)
        (self.root / "BIMRI-backup.md").write_bytes(rolling)

        first = self.cli("migrate")
        self.assertIn("complete at v5.0", first.stdout)
        self.assertNotIn("BIMRI.md", {path.name for path in self.root.iterdir()})
        self.assertNotIn("BIMRI-backup.md", {path.name for path in self.root.iterdir()})
        self.assertTrue((self.root / "bimri.md").exists())
        converted = self.hot()
        self.assertIn("[K:legacy.v3.t1-0001]", converted)
        self.assertIn("[K:legacy.v3.t2-20260720-01]", converted)
        pattern_line = next(
            line for line in converted.splitlines()
            if "[K:legacy.v3.pattern-0001]" in line
        )
        self.assertIn("[watch] [T:working] [SRC:legacy]", pattern_line)
        self.assertNotIn("[P", pattern_line)
        self.assertEqual(self.state()["run_count"], 12)

        marker_path = self.root / ".bimri" / "migrations" / "legacy-to-v5.json"
        marker = json.loads(marker_path.read_text("utf-8"))
        self.assertEqual(marker["migration"], "legacy-to-v5")
        self.assertEqual(marker["source_version"], 3)
        self.assertEqual(marker["source_hot_hash"], hashlib.sha256(active).hexdigest())
        self.assertEqual(len(marker["assets"]), 2)
        expected = {"BIMRI.md": active, "BIMRI-backup.md": rolling}
        for asset in marker["assets"]:
            original = expected[asset["source_path"]]
            self.assertEqual(asset["byte_length"], len(original))
            self.assertEqual(asset["sha256"], hashlib.sha256(original).hexdigest())
            self.assertEqual(
                (self.root / asset["backup_path"]).read_bytes(), original
            )
        mapped = {item["source_id"]: item for item in marker["id_map"]}
        self.assertEqual(mapped["T2-20260720-01"]["target_key"], "legacy.v3.t2-20260720-01")
        self.assertEqual(mapped["PATTERN-0001"]["target_tier"], 2)
        marker_before = marker_path.read_bytes()
        backup_before = {
            path.name: path.read_bytes()
            for path in (self.root / ".bimri" / "backups").iterdir()
        }
        self.cli("migrate")
        self.assertEqual(marker_path.read_bytes(), marker_before)
        self.assertEqual(
            {
                path.name: path.read_bytes()
                for path in (self.root / ".bimri" / "backups").iterdir()
            },
            backup_before,
        )
        run_id = self.start("codex")
        proposal = self.propose(
            run_id, "post.migration", "A later v5 revision must remain canonical."
        )
        self.cli("sync", "--run", run_id)
        self.assertEqual(self.decision(proposal)["outcome"], "accepted")
        self.cli("status")
        self.assertIn("[K:post.migration]", self.hot())
        self.assertEqual(self.state()["head_revision"], 1)

    def test_legacy_marker_rejects_traversal_and_semantic_forgery(self):
        traversal_root = self.root / "marker-traversal"
        traversal_root.mkdir()
        source = self.legacy_v3_bytes("The backup must remain inside its authority folder.")
        (traversal_root / "BIMRI.md").write_bytes(source)
        self.cli("migrate", root=traversal_root)
        marker_path = (
            traversal_root / ".bimri" / "migrations" / "legacy-to-v5.json"
        )
        marker = json.loads(marker_path.read_text("utf-8"))
        active = next(
            item for item in marker["assets"] if item["role"] == "active memory"
        )
        real_backup = traversal_root / active["backup_path"]
        escaped = traversal_root / "escape.bin"
        escaped.write_bytes(real_backup.read_bytes())
        real_backup.unlink()
        active["backup_path"] = ".bimri/backups/../../escape.bin"
        marker_path.write_text(json.dumps(marker, indent=2) + "\n", "utf-8")

        traversal = self.cli("doctor", root=traversal_root, check=False)
        self.assertEqual(traversal.returncode, 2)
        self.assertIn("deterministic direct .bimri/backups", traversal.stderr)

        semantic_root = self.root / "marker-semantic-forgery"
        semantic_root.mkdir()
        original = self.legacy_v3_bytes("The original source determines V000000.")
        (semantic_root / "BIMRI.md").write_bytes(original)
        self.cli("migrate", root=semantic_root)
        semantic_marker_path = (
            semantic_root / ".bimri" / "migrations" / "legacy-to-v5.json"
        )
        semantic_marker = json.loads(semantic_marker_path.read_text("utf-8"))
        semantic_asset = next(
            item for item in semantic_marker["assets"]
            if item["role"] == "active memory"
        )
        forged = self.legacy_v3_bytes("A forged source must not inherit old authority.")
        forged_hash = hashlib.sha256(forged).hexdigest()
        forged_name = f"legacy-{semantic_asset['source_path']}-{forged_hash}.bin"
        forged_path = semantic_root / ".bimri" / "backups" / forged_name
        forged_path.write_bytes(forged)
        semantic_asset["sha256"] = forged_hash
        semantic_asset["byte_length"] = len(forged)
        semantic_asset["backup_path"] = f".bimri/backups/{forged_name}"
        semantic_marker["source_hot_hash"] = forged_hash
        semantic_marker_path.write_text(
            json.dumps(semantic_marker, indent=2) + "\n", "utf-8"
        )

        semantic = self.cli("doctor", root=semantic_root, check=False)
        self.assertEqual(semantic.returncode, 2)
        self.assertIn("not the deterministic conversion", semantic.stderr)

    def test_legacy_marker_and_state_must_claim_each_other(self):
        missing_root = self.root / "missing-legacy-marker"
        missing_root.mkdir()
        (missing_root / "BIMRI.md").write_bytes(self.legacy_v3_bytes())
        self.cli("migrate", root=missing_root)
        marker_path = (
            missing_root / ".bimri" / "migrations" / "legacy-to-v5.json"
        )
        marker_path.unlink()
        missing = self.cli("doctor", root=missing_root, check=False)
        self.assertEqual(missing.returncode, 2)
        self.assertIn("durable migration marker is missing", missing.stderr)

        invalid_root = self.root / "invalid-legacy-state-claim"
        invalid_root.mkdir()
        (invalid_root / "BIMRI.md").write_bytes(self.legacy_v3_bytes())
        self.cli("migrate", root=invalid_root)
        state_path = invalid_root / ".bimri" / "state.json"
        state = json.loads(state_path.read_text("utf-8"))
        state["legacy_migration"] = "legacy-to-v6"
        state_path.write_text(json.dumps(state, indent=2) + "\n", "utf-8")
        invalid = self.cli("doctor", root=invalid_root, check=False)
        self.assertEqual(invalid.returncode, 2)
        self.assertIn("legacy_migration has an unsupported value", invalid.stderr)

    def test_legacy_migration_rejects_unrelated_history_without_state(self):
        source = self.legacy_v3_bytes("Existing revisions must never be orphaned.")
        (self.root / "BIMRI.md").write_bytes(source)
        revisions = self.root / ".bimri" / "revisions"
        revisions.mkdir(parents=True)
        unrelated = b"pre-existing revision authority\n"
        (revisions / "V000001.md").write_bytes(unrelated)

        result = self.cli("migrate", check=False)

        self.assertEqual(result.returncode, 2)
        self.assertIn("unrelated BIMRI history exists", result.stderr)
        self.assertEqual((self.root / "BIMRI.md").read_bytes(), source)
        self.assertEqual((revisions / "V000001.md").read_bytes(), unrelated)
        self.assertFalse((revisions / "V000000.md").exists())
        self.assertFalse((self.root / ".bimri" / "state.json").exists())

    def test_legacy_pointer_delimiter_rejects_unicode_whitespace(self):
        source = self.legacy_v3_bytes(
            "Keep source\u00a0->\u00a0destination as ordinary text."
        )
        (self.root / "BIMRI.md").write_bytes(source)

        result = self.cli("migrate", check=False)

        self.assertEqual(result.returncode, 2)
        self.assertIn("reserved v5 pointer delimiter", result.stderr)
        self.assertEqual((self.root / "BIMRI.md").read_bytes(), source)
        self.assertFalse((self.root / ".bimri").exists())

    def test_damaged_retry_marker_cannot_retire_legacy_sources(self):
        source = self.legacy_v3_bytes("Retry validation must precede retirement.")
        (self.root / "BIMRI.md").write_bytes(source)
        crashed = self.worker(
            "legacy_crash_before_state", "migrate", root=self.root, check=False
        )
        self.assertEqual(crashed.returncode, 93)
        marker_path = self.root / ".bimri" / "migrations" / "legacy-to-v5.json"
        marker = json.loads(marker_path.read_text("utf-8"))
        marker.pop("completed_at")
        marker_path.write_text(json.dumps(marker, indent=2) + "\n", "utf-8")

        result = self.cli("migrate", check=False)

        self.assertEqual(result.returncode, 2)
        self.assertIn("invalid completion time", result.stderr)
        self.assertEqual((self.root / "BIMRI.md").read_bytes(), source)
        self.assertFalse((self.root / ".bimri" / "state.json").exists())

    def test_v1_lowercase_and_v2_legacy_metadata_migrate_deterministically(self):
        for version in (1, 2):
            with self.subTest(version=version):
                root = self.root / f"v{version}-lowercase"
                root.mkdir()
                active = (
                    f"<!-- BIMRI v{version}.0 | Last Maintained: 2026-03-15 | Sessions: 3 | Token Est: ~300 -->\n\n"
                    "# BIMRI: Memory File\n\n"
                    "## Tier 1: Core Intelligence\n"
                    "- Keep answers concise.\n\n"
                    "## Tier 2: Active Context\n"
                    "[IMPORTANCE:4] [TIMESTAMP:2026-03-15] [TAGS:launch] [WEIGHT:4.0]\n"
                    "Prepare the launch checklist.\n\n"
                    "## Tier 3: Pattern Recognition\n"
                    "[PATTERN] [CONFIDENCE:EMERGING] [OBSERVATIONS:2] [TAGS:review]\n"
                    "The owner reviews launch claims before publishing.\n"
                ).encode("utf-8")
                rolling = (
                    f"<!-- BIMRI v{version}.0 | Last Maintained: 2026-03-14 | Sessions: 2 -->\n"
                    "# BIMRI: Memory File\n"
                    "## Tier 1: Core Intelligence\n"
                    "## Tier 2: Active Context\n"
                    "## Tier 3: Pattern Recognition\n"
                ).encode("utf-8")
                (root / "bimri.md").write_bytes(active)
                (root / "bimri-backup.md").write_bytes(rolling)
                self.cli("migrate", root=root)
                hot = self.hot(root=root)
                self.assertIn(f"[K:legacy.v{version}.t1-0001]", hot)
                self.assertIn(f"[K:legacy.v{version}.t2-legacy-0001]", hot)
                self.assertIn(f"[K:legacy.v{version}.pattern-0001]", hot)
                self.assertFalse((root / "bimri-backup.md").exists())
                marker = json.loads(
                    (root / ".bimri" / "migrations" / "legacy-to-v5.json").read_text("utf-8")
                )
                self.assertEqual(marker["source_version"], version)
                self.assertEqual(self.state(root=root)["run_count"], 3)

    def test_identical_dual_case_migrates_and_differing_dual_case_fails_closed(self):
        identical_root = self.root / "identical-dual"
        identical_root.mkdir()
        source = (
            "<!-- BIMRI v3.0 | Last Maintained: 2026-07-20 | Sessions: 1 -->\n"
            "# BIMRI: Memory File\n"
            "## Tier 1: Core Intelligence\n"
            "- Preserve this shared claim.\n"
            "## Tier 2: Active Context\n"
            "## Tier 3: Pattern Recognition\n"
        ).encode("utf-8")
        (identical_root / "BIMRI.md").write_bytes(source)
        (identical_root / "bimri.md").write_bytes(source)
        identical_source_names = {
            path.name for path in identical_root.iterdir()
            if path.name in {"BIMRI.md", "bimri.md"}
        }
        self.cli("migrate", root=identical_root)
        self.assertNotIn(
            "BIMRI.md", {path.name for path in identical_root.iterdir()}
        )
        marker = json.loads(
            (identical_root / ".bimri" / "migrations" / "legacy-to-v5.json").read_text("utf-8")
        )
        self.assertEqual(
            {asset["source_path"] for asset in marker["assets"]},
            identical_source_names,
        )

        differing_root = self.root / "differing-dual"
        differing_root.mkdir()
        upper = source
        lower = source.replace(b"shared claim", b"different claim")
        (differing_root / "BIMRI.md").write_bytes(upper)
        (differing_root / "bimri.md").write_bytes(lower)
        differing_names = {
            path.name for path in differing_root.iterdir()
            if path.name in {"BIMRI.md", "bimri.md"}
        }
        if len(differing_names) < 2:
            return
        result = self.cli("migrate", root=differing_root, check=False)
        self.assertEqual(result.returncode, 2)
        self.assertIn("case variants of BIMRI.md differ", result.stderr)
        self.assertEqual((differing_root / "BIMRI.md").read_bytes(), upper)
        self.assertEqual((differing_root / "bimri.md").read_bytes(), lower)
        self.assertFalse((differing_root / ".bimri").exists())

    def test_mixed_case_legacy_names_are_discovered_and_canonicalized(self):
        active = (
            "<!-- BIMRI v3.0 | Last Maintained: 2026-07-20 | Sessions: 1 -->\n"
            "# BIMRI: Memory File\n"
            "## Tier 1: Core Intelligence\n"
            "- Mixed-case source names remain portable.\n"
            "## Tier 2: Active Context\n"
            "## Tier 3: Pattern Recognition\n"
        ).encode("utf-8")
        rolling = active.replace(b"Sessions: 1", b"Sessions: 0")
        (self.root / "Bimri.md").write_bytes(active)
        (self.root / "Bimri-backup.md").write_bytes(rolling)
        self.cli("migrate")
        marker = json.loads(
            (self.root / ".bimri" / "migrations" / "legacy-to-v5.json").read_text("utf-8")
        )
        self.assertEqual(
            {asset["source_path"] for asset in marker["assets"]},
            {"Bimri.md", "Bimri-backup.md"},
        )
        root_names = {path.name for path in self.root.iterdir()}
        self.assertNotIn("Bimri.md", root_names)
        self.assertNotIn("Bimri-backup.md", root_names)
        self.assertIn("bimri.md", root_names)
        for asset in marker["assets"]:
            expected = active if asset["role"] == "active memory" else rolling
            self.assertEqual((self.root / asset["backup_path"]).read_bytes(), expected)

    def test_backup_only_invalid_utf8_and_unparseable_legacy_fail_without_mutation(self):
        valid = (
            "<!-- BIMRI v3.0 | Last Maintained: 2026-07-20 | Sessions: 1 -->\n"
            "# BIMRI: Memory File\n"
            "## Tier 1: Core Intelligence\n"
            "## Tier 2: Active Context\n"
            "## Tier 3: Pattern Recognition\n"
        ).encode("utf-8")
        backup_root = self.root / "backup-only"
        backup_root.mkdir()
        (backup_root / "BIMRI-backup.md").write_bytes(valid)
        backup_result = self.cli("migrate", root=backup_root, check=False)
        self.assertEqual(backup_result.returncode, 2)
        self.assertIn("backup exists without active memory", backup_result.stderr)
        self.assertFalse((backup_root / ".bimri").exists())

        utf8_root = self.root / "invalid-utf8-legacy"
        utf8_root.mkdir()
        invalid_bytes = b"<!-- BIMRI v3.0 -->\n\xff\xfe"
        (utf8_root / "BIMRI.md").write_bytes(invalid_bytes)
        utf8_result = self.cli("migrate", root=utf8_root, check=False)
        self.assertEqual(utf8_result.returncode, 2)
        self.assertIn("not valid UTF-8", utf8_result.stderr)
        self.assertEqual((utf8_root / "BIMRI.md").read_bytes(), invalid_bytes)
        self.assertFalse((utf8_root / ".bimri").exists())

        malformed_root = self.root / "malformed-legacy"
        malformed_root.mkdir()
        malformed = valid.replace(
            b"## Tier 3: Pattern Recognition\n",
            b"This Tier 2 line has no recognized metadata.\n## Tier 3: Pattern Recognition\n",
        )
        (malformed_root / "BIMRI.md").write_bytes(malformed)
        malformed_result = self.cli("migrate", root=malformed_root, check=False)
        self.assertEqual(malformed_result.returncode, 2)
        self.assertIn("not a recognized v1-v3 Tier 2 metadata line", malformed_result.stderr)
        self.assertEqual((malformed_root / "BIMRI.md").read_bytes(), malformed)
        self.assertFalse((malformed_root / ".bimri").exists())

        pointer_root = self.root / "reserved-pointer-legacy"
        pointer_root.mkdir()
        pointer = valid.replace(
            b"## Tier 2: Active Context\n",
            b"- Keep the literal source -> destination instruction.\n"
            b"## Tier 2: Active Context\n",
        )
        (pointer_root / "BIMRI.md").write_bytes(pointer)
        pointer_result = self.cli("migrate", root=pointer_root, check=False)
        self.assertEqual(pointer_result.returncode, 2)
        self.assertIn("reserved v5 pointer delimiter", pointer_result.stderr)
        self.assertEqual((pointer_root / "BIMRI.md").read_bytes(), pointer)
        self.assertFalse((pointer_root / ".bimri").exists())

    def test_legacy_import_may_inherit_tier_and_hot_byte_overflow(self):
        lines = []
        for number in range(1, 31):
            lines.extend([
                f"[ID:T2-20260720-{number:02d}] [IMP:3] [CREATED:2026-07-20] "
                f"[SESSION:1] [LAST_USED:2026-07-20] [LAST_USED_SESSION:1] "
                f"[TAGS:legacy] [W:3.0]",
                f"Inherited active claim number {number} with deliberately retained detail "
                f"that makes the generated view exceed its normal byte ceiling. " + ("x" * 370),
            ])
        source = (
            "<!-- BIMRI v3.0 | Last Maintained: 2026-07-20 | Sessions: 1 -->\n"
            "# BIMRI: Memory File\n"
            "## Tier 1: Core Intelligence\n"
            "## Tier 2: Active Context\n"
            + "\n".join(lines)
            + "\n## Tier 3: Pattern Recognition\n"
        )
        (self.root / "BIMRI.md").write_text(source, "utf-8")
        self.cli("migrate")
        self.assertEqual(self.hot().count("[K:legacy.v3.t2-20260720-"), 30)
        self.assertGreater(len(self.hot().encode("utf-8")), self.state()["hot_max_bytes"])
        doctor = self.cli("doctor")
        self.assertIn("BIMRI doctor: PASSED", doctor.stdout)

    def test_legacy_migration_retries_before_state_and_before_retirement(self):
        source = (
            "<!-- BIMRI v3.0 | Last Maintained: 2026-07-20 | Sessions: 2 -->\n"
            "# BIMRI: Memory File\n"
            "## Tier 1: Core Intelligence\n"
            "- Crash recovery must preserve this claim.\n"
            "## Tier 2: Active Context\n"
            "## Tier 3: Pattern Recognition\n"
        ).encode("utf-8")
        before_state = self.root / "crash-before-state"
        before_state.mkdir()
        (before_state / "BIMRI.md").write_bytes(source)
        crashed = self.worker(
            "legacy_crash_before_state", "migrate", root=before_state,
            check=False,
        )
        self.assertEqual(crashed.returncode, 93)
        self.assertFalse((before_state / ".bimri" / "state.json").exists())
        marker_path = before_state / ".bimri" / "migrations" / "legacy-to-v5.json"
        marker_before = marker_path.read_bytes()
        revision_before = (
            before_state / ".bimri" / "revisions" / "V000000.md"
        ).read_bytes()
        backups_before = {
            path.name: path.read_bytes()
            for path in (before_state / ".bimri" / "backups").iterdir()
        }
        self.cli("migrate", root=before_state)
        self.assertEqual(marker_path.read_bytes(), marker_before)
        self.assertEqual(
            (before_state / ".bimri" / "revisions" / "V000000.md").read_bytes(),
            revision_before,
        )
        self.assertEqual(
            {
                path.name: path.read_bytes()
                for path in (before_state / ".bimri" / "backups").iterdir()
            },
            backups_before,
        )

        before_retire = self.root / "crash-before-retire"
        before_retire.mkdir()
        (before_retire / "BIMRI.md").write_bytes(source)
        crashed = self.worker(
            "legacy_crash_before_retire", "migrate", root=before_retire,
            check=False,
        )
        self.assertEqual(crashed.returncode, 94)
        self.assertTrue((before_retire / ".bimri" / "state.json").exists())
        self.assertIn("BIMRI.md", {path.name for path in before_retire.iterdir()})
        self.cli("migrate", root=before_retire)
        self.assertNotIn("BIMRI.md", {path.name for path in before_retire.iterdir()})
        self.assertTrue((before_retire / "bimri.md").exists())

    def test_failed_install_rolls_back_every_legacy_case_file(self):
        target = self.root / "legacy-install-rollback"
        target.mkdir()
        active = (
            "<!-- BIMRI v3.0 | Last Maintained: 2026-07-20 | Sessions: 1 -->\n"
            "# BIMRI: Memory File\n"
            "## Tier 1: Core Intelligence\n"
            "- Installer rollback must restore this source.\n"
            "## Tier 2: Active Context\n"
            "## Tier 3: Pattern Recognition\n"
        ).encode("utf-8")
        rolling = active.replace(b"Sessions: 1", b"Sessions: 0")
        (target / "BIMRI.md").write_bytes(active)
        (target / "BIMRI-backup.md").write_bytes(rolling)
        result = self.worker(
            "install_selfcheck", "install", "--target", str(target),
            root=target, check=False,
        )
        self.assertEqual(result.returncode, 2)
        self.assertIn("rolled back", result.stderr)
        self.assertEqual((target / "BIMRI.md").read_bytes(), active)
        self.assertEqual((target / "BIMRI-backup.md").read_bytes(), rolling)
        self.assertNotIn(
            "bimri.md", {path.name for path in target.iterdir()}
        )
        self.assertFalse((target / ".bimri" / "state.json").exists())
        self.assertEqual(
            list((target / ".bimri" / "migrations").glob("*.json")), []
        )
        self.assertFalse((target / "legacy").exists())
        self.assertFalse((target / "BIMRI-LICENSE").exists())

    def test_late_legacy_writer_is_detected_before_any_source_retirement(self):
        source = (
            "<!-- BIMRI v3.0 | Last Maintained: 2026-07-20 | Sessions: 1 -->\n"
            "# BIMRI: Memory File\n"
            "## Tier 1: Core Intelligence\n"
            "- Preserve this claim before retiring the legacy file.\n"
            "## Tier 2: Active Context\n"
            "## Tier 3: Pattern Recognition\n"
        ).encode("utf-8")
        (self.root / "BIMRI.md").write_bytes(source)
        result = self.worker(
            "legacy_source_change_before_retire", "migrate",
            root=self.root, check=False,
        )
        self.assertEqual(result.returncode, 2)
        self.assertIn("changed before retirement", result.stderr)
        changed = (self.root / "BIMRI.md").read_bytes()
        self.assertTrue(changed.startswith(source))
        marker = json.loads(
            (self.root / ".bimri" / "migrations" / "legacy-to-v5.json").read_text("utf-8")
        )
        active_asset = next(
            asset for asset in marker["assets"]
            if asset["role"] == "active memory"
        )
        self.assertEqual(
            (self.root / active_asset["backup_path"]).read_bytes(), source
        )
        (self.root / "BIMRI.md").write_bytes(source)
        self.cli("migrate")
        self.assertNotIn("BIMRI.md", {path.name for path in self.root.iterdir()})
        self.assertTrue((self.root / "bimri.md").exists())


if __name__ == "__main__":
    unittest.main()
