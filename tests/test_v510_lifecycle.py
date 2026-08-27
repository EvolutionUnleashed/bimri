"""Black-box lifecycle regressions for the BIMRI v5.1.x release.

The tests exercise the command-line boundary in separate processes so that
parsing, locking, atomic writes, migration, and rendered retrieval are covered
as they are in normal use.
"""

import concurrent.futures
import datetime as dt
import hashlib
import importlib.util
import io
import json
import os
import re
import shutil
import subprocess
import sys
import tarfile
import tempfile
import unittest
from pathlib import Path


REPOSITORY = Path(__file__).resolve().parents[1]
ENGINE = REPOSITORY / "bimri-engine.py"
CRASH_WORKER = REPOSITORY / "tests" / "crash_worker.py"
POPULATED_FIXTURE = REPOSITORY / "tests" / "fixtures" / "v5.0.2-populated"
RUN_RE = re.compile(r"=== BIMRI BRIEF (R\d{6})")


class V510LifecycleTest(unittest.TestCase):
    maxDiff = None

    def setUp(self):
        self._temp = tempfile.TemporaryDirectory(prefix="bimri-v510-")
        self.root = Path(self._temp.name)

    def tearDown(self):
        self._temp.cleanup()

    def cli(self, *arguments, root=None, check=True, timeout=60, engine=None):
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
            timeout=timeout,
        )
        if check and result.returncode != 0:
            self.fail(
                f"command failed: {command!r}\n"
                f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}"
            )
        return result

    def start(self, actor="lifecycle", root=None):
        result = self.cli("start", "--actor", actor, root=root)
        match = RUN_RE.search(result.stdout)
        self.assertIsNotNone(match, result.stdout)
        return match.group(1), result

    def propose_set(
        self,
        run_id,
        key,
        text,
        *,
        tier=2,
        new_subject=False,
        importance=None,
        kind=None,
        status=None,
        tags=None,
        source="user",
        trust="confirmed",
        root=None,
        check=True,
    ):
        arguments = [
            "propose",
            "--run",
            run_id,
            "--operation",
            "set",
            "--tier",
            str(tier),
            "--key",
            key,
            "--text",
            text,
            "--source",
            source,
            "--trust",
            trust,
        ]
        if kind is not None or (new_subject and tier == 1):
            arguments.extend(("--kind", kind or "fact"))
        if importance is not None:
            arguments.extend(("--importance", str(importance)))
        if status is not None:
            arguments.extend(("--status", status))
        if tags is not None:
            arguments.extend(("--tags", tags))
        if new_subject:
            arguments.append("--new-subject")
        return self.cli(*arguments, root=root, check=check)

    def apply_set(self, run_id, key, text, **kwargs):
        root = kwargs.get("root")
        proposed = self.propose_set(run_id, key, text, **kwargs)
        synced = self.cli("sync", "--run", run_id, root=root)
        return proposed, synced

    def state(self, root=None):
        return json.loads(
            Path(root or self.root)
            .joinpath(".bimri", "state.json")
            .read_text("utf-8")
        )

    def update_state(self, root=None, **updates):
        target = Path(root or self.root).joinpath(".bimri", "state.json")
        state = json.loads(target.read_text("utf-8"))
        state.update(updates)
        target.write_text(
            json.dumps(state, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        return state

    def hot_text(self, root=None):
        return Path(root or self.root).joinpath("bimri.md").read_text("utf-8")

    def cold_current(self, root=None):
        value = self.state(root).get("cold_current", {})
        self.assertIsInstance(value, dict)
        return value

    def current_count(self, key, root=None):
        marker = f"[K:{key}]"
        hot_count = sum(
            marker in line
            for line in self.hot_text(root).splitlines()
            if line.startswith("[")
        )
        cold_count = int(key in self.cold_current(root))
        return hot_count + cold_count

    def conflict_files(self, root=None):
        return sorted(
            Path(root or self.root).joinpath(".bimri", "conflicts").glob("C*.json")
        )

    def proposal_records(self, root=None):
        directory = Path(root or self.root).joinpath(".bimri", "proposals")
        return {
            path.stem: json.loads(path.read_text("utf-8"))
            for path in sorted(directory.glob("R*-Q*.json"))
        }

    def decision(self, proposal_id, root=None):
        path = Path(root or self.root).joinpath(
            ".bimri", "decisions", f"{proposal_id}.json"
        )
        self.assertTrue(path.is_file(), f"missing decision for {proposal_id}")
        return json.loads(path.read_text("utf-8"))

    def held_proposal(self, key, text, root=None):
        matches = [
            record
            for record in self.proposal_records(root).values()
            if record.get("key") == key and record.get("text") == text
        ]
        self.assertEqual(len(matches), 1, matches)
        proposal = matches[0]
        decision = self.decision(proposal["proposal_id"], root)
        self.assertEqual(decision.get("outcome"), "held")
        self.assertEqual(decision.get("reason"), proposal.get("hold_reason"))
        return proposal, decision

    def apply_close(self, run_id, key, *, root=None):
        proposed = self.cli(
            "propose",
            "--run",
            run_id,
            "--operation",
            "close",
            "--key",
            key,
            "--source",
            "user",
            "--trust",
            "confirmed",
            root=root,
        )
        synced = self.cli("sync", "--run", run_id, root=root)
        return proposed, synced

    def apply_touch(self, run_id, key, *, root=None):
        proposed = self.cli(
            "propose",
            "--run",
            run_id,
            "--operation",
            "touch",
            "--key",
            key,
            "--source",
            "user",
            "--trust",
            "confirmed",
            root=root,
        )
        synced = self.cli("sync", "--run", run_id, root=root)
        return proposed, synced

    def seed_cold_current(self, root, key="corruption.cold-subject"):
        create_run, _ = self.start("cold-fixture-create", root=root)
        old_text = "Original cold fixture value."
        self.apply_set(
            create_run,
            key,
            old_text,
            root=root,
            new_subject=True,
            importance=3,
        )
        current_bytes = len(self.hot_text(root).encode("utf-8"))
        self.update_state(root=root, hot_max_bytes=current_bytes + 32)
        update_run, _ = self.start("cold-fixture-update", root=root)
        new_text = "Current cold fixture value. " + "c" * 390
        self.apply_set(update_run, key, new_text, root=root)
        self.assertIn(key, self.cold_current(root))
        return old_text, new_text

    @staticmethod
    def protected_snapshot(root):
        """Record pre-existing memory bytes without following links."""
        root = Path(root)
        snapshot = {}
        candidates = [root / "bimri.md"]
        candidates.extend(
            sorted(
                root.joinpath(".bimri").rglob("*"),
                key=lambda item: item.as_posix(),
            )
        )
        for path in candidates:
            relative = path.relative_to(root).as_posix()
            if path.is_symlink():
                snapshot[relative] = ("symlink", os.readlink(path))
            elif path.is_dir():
                snapshot[relative] = ("directory", None)
            elif path.is_file():
                content = path.read_bytes()
                snapshot[relative] = (
                    "file",
                    len(content),
                    hashlib.sha256(content).hexdigest(),
                )
            else:
                snapshot[relative] = ("missing", None)
        return snapshot

    def materialize_release(self, commit, destination):
        archived = subprocess.run(
            [
                "git",
                "-c",
                f"safe.directory={REPOSITORY.as_posix()}",
                "archive",
                "--format=tar",
                commit,
            ],
            cwd=str(REPOSITORY),
            capture_output=True,
            timeout=30,
        )
        self.assertEqual(
            archived.returncode,
            0,
            f"the checkout must contain release commit {commit}",
        )
        destination = Path(destination)
        destination.mkdir()
        with tarfile.open(fileobj=io.BytesIO(archived.stdout), mode="r:") as archive:
            package_root = destination.resolve()
            for member in archive.getmembers():
                target = (destination / member.name).resolve()
                self.assertTrue(
                    target == package_root or package_root in target.parents,
                    member.name,
                )
            archive.extractall(destination)
        return destination

    def test_direct_user_confirmed_tier1_create_and_update(self):
        run_id, _ = self.start("tier-one-owner")
        key = "core.operating-principle"
        original = "Customer evidence decides the next operating priority."
        replacement = "Verified customer evidence decides the next operating priority."

        self.apply_set(
            run_id,
            key,
            original,
            tier=1,
            new_subject=True,
        )
        created = self.cli("recall", "--key", key)
        self.assertIn("HOT", created.stdout)
        self.assertIn(original, created.stdout)
        hot = self.hot_text()
        self.assertIn(f"[K:{key}]", hot)
        self.assertIn("[T:confirmed] [SRC:user]", hot)

        self.apply_set(run_id, key, replacement, tier=1)
        current = self.cli("recall", "--key", key)
        history = self.cli(
            "recall", "--key", key, "--history", "--limit", "20"
        )

        self.assertIn(replacement, current.stdout)
        self.assertNotIn(original, current.stdout)
        self.assertIn("HISTORY", history.stdout)
        self.assertIn(original, history.stdout)
        self.assertIn(replacement, history.stdout)
        self.assertEqual(self.current_count(key), 1)
        self.assertEqual(self.state()["bimri_version"], "5.1.0")

    def test_exact_key_updates_are_count_neutral_and_history_is_recoverable(self):
        create_run, _ = self.start("stable-key-create")
        key = "product.release-state"
        generations = [
            "Release candidate is awaiting acceptance checks.",
            "Release candidate passed acceptance checks.",
            "Release is available to the internal group.",
        ]

        self.apply_set(
            create_run,
            key,
            generations[0],
            new_subject=True,
            importance=5,
            status="watch",
            tags="release,verified",
        )
        self.assertEqual(self.current_count(key), 1)
        created_line = next(
            line for line in self.hot_text().splitlines() if f"[K:{key}]" in line
        )
        created_id = created_line.split("]", 1)[0].lstrip("[")
        self.assertIn("[I:5] [watch]", created_line)
        self.assertIn("[release,verified]", created_line)
        self.assertIn(f"[F:{create_run}] [L:{create_run}]", created_line)

        update_runs = []
        for number, generation in enumerate(generations[1:], 1):
            update_run, _ = self.start(f"stable-key-update-{number}")
            update_runs.append(update_run)
            self.apply_set(update_run, key, generation)
            self.assertEqual(self.current_count(key), 1)

        current_line = next(
            line for line in self.hot_text().splitlines() if f"[K:{key}]" in line
        )
        current_id = current_line.split("]", 1)[0].lstrip("[")
        self.assertNotEqual(current_id, created_id)
        self.assertIn("[I:5] [watch]", current_line)
        self.assertIn("[release,verified]", current_line)
        self.assertIn(f"[F:{create_run}] [L:{update_runs[-1]}]", current_line)

        current = self.cli("recall", "--key", key)
        history = self.cli(
            "recall", "--key", key, "--history", "--limit", "20"
        )
        self.assertIn(generations[-1], current.stdout)
        self.assertNotIn(generations[0], current.stdout)
        for generation in generations:
            self.assertIn(generation, history.stdout)

        archive_bytes = b"\n".join(
            path.read_bytes()
            for path in sorted(self.root.joinpath(".bimri", "archive").glob("*.md"))
        )
        self.assertIn(generations[0].encode("utf-8"), archive_bytes)
        self.assertIn(b"replaced", archive_bytes.lower())

    def test_absent_update_requires_explicit_new_subject_admission(self):
        run_id, _ = self.start("admission")
        key = "delivery.next-milestone"
        held_text = "Prepare the release notes."

        missing = self.propose_set(
            run_id,
            key,
            held_text,
        )
        self.assertEqual(missing.returncode, 0)
        self.cli("sync", "--run", run_id)
        proposal, _decision = self.held_proposal(key, held_text)
        self.assertFalse(proposal["new_subject"])
        self.assertEqual(proposal["hold_reason"], "classification-required")
        self.assertEqual(self.current_count(key), 0)
        self.assertEqual(self.conflict_files(), [])
        pull = self.cli(
            "recall", "--query", "prepare the release notes", "--limit", "5"
        )
        self.assertIn("HELD", pull.stdout)
        self.assertIn(held_text, pull.stdout)
        status = self.cli("status")
        self.assertIn("held", status.stdout.lower())

        _later, started = self.start("admission-later")
        self.assertNotIn(proposal["proposal_id"], started.stdout + started.stderr)
        self.assertNotIn("PENDING REVIEW", (started.stdout + started.stderr).upper())

        self.apply_set(
            run_id,
            key,
            held_text,
            new_subject=True,
        )
        duplicate = self.propose_set(
            run_id,
            key,
            "Create a second current subject by mistake.",
            new_subject=True,
            check=False,
        )
        self.assertNotEqual(duplicate.returncode, 0)
        self.assertEqual(self.current_count(key), 1)
        current = self.cli("recall", "--key", key)
        self.assertIn("Prepare the release notes.", current.stdout)
        self.assertNotIn("second current", current.stdout.lower())

    def test_count_overflow_is_accepted_and_byte_pressure_cools_safely(self):
        run_id, _ = self.start("capacity")
        anchor_key = "archive.lookup-anchor"
        anchor_text = (
            "Orbital checksum handoff evidence remains retrievable by task wording. "
            + "a" * 340
        )
        self.apply_set(
            run_id,
            anchor_key,
            anchor_text,
            new_subject=True,
            importance=1,
        )

        for number in range(1, 40):
            self.apply_set(
                run_id,
                f"capacity.subject-{number:03d}",
                f"Working subject {number:03d}. " + chr(97 + number % 20) * 360,
                new_subject=True,
                importance=3,
            )

        overflow_key = "capacity.subject-040"
        proposed, synced = self.apply_set(
            run_id,
            overflow_key,
            "Forty-first current Tier 2 subject is accepted. " + "z" * 350,
            new_subject=True,
            importance=4,
        )
        self.assertEqual(proposed.returncode, 0)
        self.assertEqual(synced.returncode, 0)
        self.assertEqual(
            sum(self.current_count(f"capacity.subject-{number:03d}") for number in range(1, 41))
            + self.current_count(anchor_key),
            41,
        )

        for number in range(41, 94):
            self.apply_set(
                run_id,
                f"capacity.subject-{number:03d}",
                f"Pressure subject {number:03d}. " + chr(97 + number % 20) * 380,
                new_subject=True,
                importance=4,
            )

        priority_key = "delivery.critical-approval"
        priority_text = "Critical deployment approval is ready for final verification."
        proposed, synced = self.apply_set(
            run_id,
            priority_key,
            priority_text,
            new_subject=True,
            importance=5,
        )
        self.assertEqual(proposed.returncode, 0)
        self.assertEqual(synced.returncode, 0)

        cold = self.cold_current()
        self.assertTrue(cold, "byte pressure did not cool any current subject")
        self.assertIn(anchor_key, cold)
        self.assertNotIn(priority_key, cold)
        self.assertEqual(self.current_count(priority_key), 1)
        self.assertEqual(
            len(self.hot_text().encode("utf-8")) <= self.state()["hot_max_bytes"],
            True,
        )

        exact = self.cli("recall", "--key", anchor_key)
        task_query = self.cli(
            "recall",
            "--query",
            "handoff evidence orbital",
            "--limit",
            "5",
        )
        self.assertIn("COLD", exact.stdout)
        self.assertIn(anchor_key, exact.stdout)
        self.assertIn("Orbital checksum handoff", exact.stdout)
        self.assertIn(anchor_key, task_query.stdout)
        self.assertIn("Orbital checksum handoff", task_query.stdout)

        updated_anchor = "Orbital checksum handoff evidence was independently verified."
        self.apply_set(
            run_id,
            anchor_key,
            updated_anchor,
            importance=5,
        )
        self.assertEqual(self.current_count(anchor_key), 1)
        updated = self.cli(
            "recall", "--key", anchor_key, "--history", "--limit", "20"
        )
        self.assertIn(updated_anchor, updated.stdout)
        self.assertIn(anchor_text, updated.stdout)

        closing_key = next(iter(self.cold_current()))
        self.apply_close(run_id, closing_key)
        self.assertEqual(self.current_count(closing_key), 0)
        closed_history = self.cli(
            "recall", "--key", closing_key, "--history", "--limit", "20"
        )
        self.assertIn("HISTORY", closed_history.stdout)
        self.assertIn(closing_key, closed_history.stdout)
        self.assertEqual(self.conflict_files(), [])

        self.root.joinpath(".bimri", "index.tsv").unlink(missing_ok=True)
        rebuilt = self.cli("index")
        self.assertIn("index rebuilt", rebuilt.stdout.lower())
        rebuilt_exact = self.cli(
            "recall", "--key", anchor_key, "--history", "--limit", "20"
        )
        rebuilt_query = self.cli(
            "recall", "--query", "evidence orbital checksum", "--limit", "5"
        )
        self.assertIn(updated_anchor, rebuilt_exact.stdout)
        self.assertIn(anchor_text, rebuilt_exact.stdout)
        self.assertIn(anchor_key, rebuilt_query.stdout)

    def test_all_protected_pressure_holds_intent_without_losing_it(self):
        run_id, _ = self.start("protected-pressure")
        state = self.state()
        ceiling = state["hot_max_bytes"]
        initial_bytes = len(self.hot_text().encode("utf-8"))

        def protected_text(number):
            prefix = f"Protected owner memory {number:03d}. "
            return prefix + "p" * (470 - len(prefix))

        self.apply_set(
            run_id,
            "protected.subject-001",
            protected_text(1),
            tier=1,
            new_subject=True,
            importance=5,
        )
        first_bytes = len(self.hot_text().encode("utf-8"))
        entry_growth = first_bytes - initial_bytes
        self.assertGreater(entry_growth, 0)

        number = 2
        while len(self.hot_text().encode("utf-8")) + entry_growth <= ceiling:
            self.assertLess(number, 150, "Tier 1 pressure fixture did not converge")
            self.apply_set(
                run_id,
                f"protected.subject-{number:03d}",
                protected_text(number),
                tier=1,
                new_subject=True,
                importance=5,
            )
            number += 1

        incoming_key = "protected.subject-999"
        incoming_text = protected_text(999)
        proposed = self.propose_set(
            run_id,
            incoming_key,
            incoming_text,
            tier=1,
            new_subject=True,
            importance=5,
        )
        self.assertEqual(proposed.returncode, 0)
        synced = self.cli("sync", "--run", run_id)
        self.assertIn("held candidates 1", synced.stdout.lower())
        proposal, decision = self.held_proposal(incoming_key, incoming_text)
        self.assertEqual(proposal["hold_reason"], "capacity-residency-required")
        self.assertEqual(decision["reason"], "capacity-residency-required")

        self.assertEqual(self.current_count(incoming_key), 0)
        self.assertLessEqual(len(self.hot_text().encode("utf-8")), ceiling)
        self.assertEqual(self.cold_current(), {})
        self.assertEqual(self.conflict_files(), [])
        doctor = self.cli("doctor", "--read-only")
        self.assertIn("PASSED", doctor.stdout)
        exact = self.cli("recall", "--key", incoming_key, "--history")
        query = self.cli(
            "recall", "--query", "owner memory 999", "--limit", "5"
        )
        self.assertIn("HELD", exact.stdout)
        self.assertIn(incoming_text, exact.stdout)
        self.assertIn(incoming_key, query.stdout)
        self.assertIn("Held candidates: 1", self.cli("status").stdout)

        _later, started = self.start("protected-pressure-later")
        output = started.stdout + started.stderr
        self.assertNotIn(proposal["proposal_id"], output)
        self.assertNotIn("PENDING REVIEW", output.upper())
        self.assertNotIn("MEMORY CONFLICT", output.upper())

    def test_pending_runs_do_not_reserve_or_protect_hot_residency(self):
        seed_run, _ = self.start("pending-residency-seed")
        initial_bytes = len(self.hot_text().encode("utf-8"))
        ceiling = initial_bytes + 5200
        self.update_state(hot_max_bytes=ceiling)

        def subject_text(number, marker="current"):
            prefix = f"Pending residency {marker} {number:03d}. "
            return prefix + "q" * (430 - len(prefix))

        keys = []
        first_key = "pending.subject-001"
        self.apply_set(
            seed_run,
            first_key,
            subject_text(1),
            new_subject=True,
            importance=1,
        )
        keys.append(first_key)
        first_bytes = len(self.hot_text().encode("utf-8"))
        entry_growth = first_bytes - initial_bytes
        number = 2
        while len(self.hot_text().encode("utf-8")) + entry_growth <= ceiling:
            key = f"pending.subject-{number:03d}"
            self.apply_set(
                seed_run,
                key,
                subject_text(number),
                new_subject=True,
                importance=1,
            )
            keys.append(key)
            number += 1
        self.assertGreaterEqual(len(keys), 4)
        self.assertEqual(self.cold_current(), {})

        active_pending = []
        orphan_pending = []
        orphan_runs = []
        for number, key in enumerate(keys, 1):
            active_run, _ = self.start(f"active-pending-{number:03d}")
            active_result = self.propose_set(
                active_run,
                key,
                subject_text(number, "active-candidate"),
            )
            active_pending.append(
                re.search(r"R\d{6}-Q\d{3}", active_result.stdout).group(0)
            )

            orphan_run, _ = self.start(f"orphan-pending-{number:03d}")
            orphan_result = self.propose_set(
                orphan_run,
                key,
                subject_text(number, "orphan-candidate"),
            )
            orphan_pending.append(
                re.search(r"R\d{6}-Q\d{3}", orphan_result.stdout).group(0)
            )
            orphan_runs.append(orphan_run)

        state_path = self.root / ".bimri" / "state.json"
        state = json.loads(state_path.read_text("utf-8"))
        for orphan_run in orphan_runs:
            state["active_runs"].pop(orphan_run)
        state_path.write_text(
            json.dumps(state, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )

        incoming_run, _ = self.start("pending-residency-incoming")
        incoming_key = "pending.subject-999"
        incoming_text = subject_text(999, "important-incoming")
        self.apply_set(
            incoming_run,
            incoming_key,
            incoming_text,
            new_subject=True,
            importance=5,
        )
        incoming_records = [
            record
            for record in self.proposal_records().values()
            if record.get("key") == incoming_key
        ]
        self.assertEqual(len(incoming_records), 1)
        incoming_decision = self.decision(incoming_records[0]["proposal_id"])
        self.assertEqual(incoming_decision["outcome"], "accepted")
        self.assertIsNone(incoming_records[0].get("hold_reason"))
        self.assertEqual(self.current_count(incoming_key), 1)
        self.assertTrue(self.cold_current())
        self.assertEqual(self.conflict_files(), [])
        for proposal_id in active_pending + orphan_pending:
            self.assertFalse(
                self.root.joinpath(
                    ".bimri", "decisions", f"{proposal_id}.json"
                ).exists(),
                proposal_id,
            )

    def test_apply_time_pressure_terminally_holds_a_preflighted_intent_once(self):
        candidate_run, _ = self.start("delayed-capacity-candidate")
        initial_bytes = len(self.hot_text().encode("utf-8"))
        ceiling = initial_bytes + 4500
        self.update_state(hot_max_bytes=ceiling)

        def protected_text(number):
            prefix = f"Delayed protected memory {number:03d}. "
            return prefix + "d" * (470 - len(prefix))

        candidate_key = "delayed.subject-999"
        candidate_text = protected_text(999)
        proposed = self.propose_set(
            candidate_run,
            candidate_key,
            candidate_text,
            tier=1,
            new_subject=True,
            importance=5,
        )
        candidate_id = re.search(r"R\d{6}-Q\d{3}", proposed.stdout).group(0)
        candidate_record = self.proposal_records()[candidate_id]
        self.assertIsNone(candidate_record.get("hold_reason"))
        self.assertFalse(
            self.root.joinpath(
                ".bimri", "decisions", f"{candidate_id}.json"
            ).exists()
        )

        filler_run, _ = self.start("delayed-capacity-filler")
        baseline_bytes = len(self.hot_text().encode("utf-8"))
        self.apply_set(
            filler_run,
            "delayed.subject-001",
            "g",
            tier=1,
            new_subject=True,
            importance=5,
        )
        first_bytes = len(self.hot_text().encode("utf-8"))
        self.assertGreater(first_bytes, baseline_bytes)
        self.apply_set(
            filler_run,
            "delayed.subject-002",
            "g",
            tier=1,
            new_subject=True,
            importance=5,
        )
        second_bytes = len(self.hot_text().encode("utf-8"))
        small_growth = second_bytes - first_bytes
        self.assertGreater(small_growth, 0)
        self.apply_set(
            filler_run,
            "delayed.subject-003",
            protected_text(3),
            tier=1,
            new_subject=True,
            importance=5,
        )
        third_bytes = len(self.hot_text().encode("utf-8"))
        large_growth = third_bytes - second_bytes
        self.assertGreater(large_growth, small_growth)

        number = 4
        while len(self.hot_text().encode("utf-8")) + large_growth <= ceiling:
            self.apply_set(
                filler_run,
                f"delayed.subject-{number:03d}",
                protected_text(number),
                tier=1,
                new_subject=True,
                importance=5,
            )
            number += 1
        while len(self.hot_text().encode("utf-8")) + small_growth <= ceiling:
            self.apply_set(
                filler_run,
                f"delayed.subject-{number:03d}",
                "g",
                tier=1,
                new_subject=True,
                importance=5,
            )
            number += 1

        revision_before_sync = self.state()["head_revision"]
        first_sync = self.cli("sync", "--run", candidate_run)
        self.assertIn("held candidates 1", first_sync.stdout.lower())
        decision_path = self.root.joinpath(
            ".bimri", "decisions", f"{candidate_id}.json"
        )
        decision_bytes = decision_path.read_bytes()
        decision = json.loads(decision_bytes.decode("utf-8"))
        self.assertEqual(decision["outcome"], "held")
        self.assertEqual(decision["reason"], "capacity-residency-required")
        self.assertEqual(decision["revision"], revision_before_sync)
        self.assertEqual(self.current_count(candidate_key), 0)
        self.assertEqual(self.conflict_files(), [])

        second_sync = self.cli("sync", "--run", candidate_run)
        self.assertIn("held candidates 0", second_sync.stdout.lower())
        self.assertEqual(decision_path.read_bytes(), decision_bytes)
        self.assertEqual(self.state()["head_revision"], revision_before_sync)
        self.assertEqual(self.current_count(candidate_key), 0)
        self.assertEqual(self.conflict_files(), [])

    def test_same_key_update_becomes_cold_when_it_is_the_only_tier2_victim(self):
        create_run, _ = self.start("cold-update-create")
        key = "delivery.only-tier2-subject"
        old_text = "Original current delivery state."
        self.apply_set(
            create_run,
            key,
            old_text,
            new_subject=True,
            importance=4,
            status="active",
            tags="delivery",
        )
        current_bytes = len(self.hot_text().encode("utf-8"))
        ceiling = current_bytes + 32
        self.update_state(hot_max_bytes=ceiling)

        update_run, _ = self.start("cold-update-replace")
        new_text = "Latest current delivery state. " + "u" * 390
        self.apply_set(update_run, key, new_text)

        records = [
            record
            for record in self.proposal_records().values()
            if record.get("key") == key and record.get("text") == new_text
        ]
        self.assertEqual(len(records), 1)
        decision = self.decision(records[0]["proposal_id"])
        self.assertEqual(decision["outcome"], "accepted")
        self.assertIsNone(records[0].get("hold_reason"))
        self.assertEqual(self.current_count(key), 1)
        self.assertIn(key, self.cold_current())
        self.assertNotIn(f"[K:{key}]", self.hot_text())
        self.assertLessEqual(len(self.hot_text().encode("utf-8")), ceiling)
        self.assertEqual(self.conflict_files(), [])

        current = self.cli("recall", "--key", key)
        history = self.cli(
            "recall", "--key", key, "--history", "--limit", "20"
        )
        self.assertIn("COLD", current.stdout)
        self.assertIn(new_text, current.stdout)
        self.assertNotIn(old_text, current.stdout)
        self.assertIn(old_text, history.stdout)
        self.assertIn(new_text, history.stdout)

    def test_cooling_retry_across_month_boundary_reuses_archive_date(self):
        create_run, _ = self.start("month-rollover-create")
        key = "archive.month-rollover"
        old_text = "Current value before month-boundary cooling."
        self.apply_set(
            create_run,
            key,
            old_text,
            new_subject=True,
            importance=1,
        )
        head_before = self.state()["head_revision"]
        current_bytes = len(self.hot_text().encode("utf-8"))
        self.update_state(hot_max_bytes=current_bytes + 32)

        update_run, _ = self.start("month-rollover-update")
        new_text = "Current value cooled at the August boundary. " + "m" * 390
        proposed = self.propose_set(update_run, key, new_text)
        proposal_id = re.search(
            r"R\d{6}-Q\d{3}", proposed.stdout
        ).group(0)

        crashed = subprocess.run(
            [
                sys.executable,
                str(CRASH_WORKER),
                str(ENGINE),
                "cooling_crash_after_archive_august",
                str(self.root),
                "sync",
                "--run",
                update_run,
            ],
            text=True,
            capture_output=True,
            timeout=120,
        )
        self.assertEqual(crashed.returncode, 109, crashed.stdout + crashed.stderr)
        self.assertEqual(self.state()["head_revision"], head_before)
        self.assertEqual(self.decision(proposal_id)["outcome"], "applying")
        self.assertNotIn(key, self.cold_current())
        self.assertIn(old_text, self.cli("recall", "--key", key).stdout)

        august_archive = self.root / ".bimri" / "archive" / "2026-08.md"
        september_archive = self.root / ".bimri" / "archive" / "2026-09.md"
        self.assertTrue(august_archive.is_file())
        self.assertFalse(september_archive.exists())
        august_before_retry = august_archive.read_bytes()
        cooled_lines = [
            line
            for line in august_before_retry.decode("utf-8").splitlines()
            if f"[BY:{proposal_id}] [cooled]" in line
        ]
        self.assertEqual(len(cooled_lines), 1)
        self.assertTrue(cooled_lines[0].startswith("[ARCHIVED:2026-08-31]"))
        self.assertIn(new_text, cooled_lines[0])

        retried = subprocess.run(
            [
                sys.executable,
                str(CRASH_WORKER),
                str(ENGINE),
                "cooling_retry_september",
                str(self.root),
                "sync",
                "--run",
                update_run,
            ],
            text=True,
            capture_output=True,
            timeout=120,
        )
        self.assertEqual(retried.returncode, 0, retried.stdout + retried.stderr)
        self.assertEqual(self.decision(proposal_id)["outcome"], "accepted")
        self.assertEqual(self.state()["head_revision"], head_before + 1)
        cold = self.cold_current()[key]
        self.assertEqual(cold["archived_by"], proposal_id)
        self.assertEqual(cold["archived_on"], "2026-08-31")
        self.assertIn(new_text, cold["raw_line"])
        self.assertNotIn(f"[K:{key}]", self.hot_text())
        self.assertEqual(august_archive.read_bytes(), august_before_retry)
        self.assertFalse(september_archive.exists())

        repeated = self.cli("sync", "--run", update_run)
        self.assertIn("held candidates 0", repeated.stdout.lower())
        self.assertEqual(august_archive.read_bytes(), august_before_retry)
        self.assertFalse(september_archive.exists())
        self.assertEqual(self.conflict_files(), [])
        self.assertIn("PASSED", self.cli("doctor", "--read-only").stdout)
        self.start("month-rollover-restart")
        recalled = self.cli("recall", "--key", key)
        self.assertIn("COLD", recalled.stdout)
        self.assertIn(new_text, recalled.stdout)

    def test_cold_only_touch_refreshes_then_safely_self_cools(self):
        key = "delivery.cold-touch-subject"
        _old_text, current_text = self.seed_cold_current(self.root, key=key)
        before = dict(self.cold_current()[key])
        self.assertNotIn(f"[K:{key}]", self.hot_text())

        touch_run, _ = self.start("cold-touch")
        proposed, synced = self.apply_touch(touch_run, key)
        self.assertEqual(proposed.returncode, 0)
        self.assertEqual(synced.returncode, 0)
        proposal_id = re.search(r"R\d{6}-Q\d{3}", proposed.stdout).group(0)
        decision = self.decision(proposal_id)
        self.assertEqual(decision["outcome"], "accepted")

        after = self.cold_current()[key]
        self.assertEqual(after["entry_id"], before["entry_id"])
        self.assertNotEqual(after["raw_line"], before["raw_line"])
        self.assertIn(f"[L:{touch_run}]", after["raw_line"])
        self.assertIn("[T:confirmed]", after["raw_line"])
        self.assertIn("[SRC:user]", after["raw_line"])
        self.assertIn("[I:3]", after["raw_line"])
        self.assertIn("[active]", after["raw_line"])
        self.assertIn(current_text, after["raw_line"])
        self.assertEqual(self.current_count(key), 1)
        self.assertNotIn(f"[K:{key}]", self.hot_text())
        self.assertEqual(self.conflict_files(), [])

        doctor = self.cli("doctor", "--read-only")
        self.assertIn("PASSED", doctor.stdout)
        _later, restarted = self.start("cold-touch-restart")
        self.assertNotIn("PENDING REVIEW", restarted.stdout.upper())
        self.assertEqual(self.cold_current()[key]["raw_line"], after["raw_line"])

    def test_cold_current_compatible_writes_resolve_as_noops(self):
        redundant_root = self.root / "redundant-set"
        redundant_key = "cold.noop-redundant-set"
        _old_text, current_text = self.seed_cold_current(
            redundant_root, key=redundant_key
        )
        cold_before = dict(self.cold_current(redundant_root)[redundant_key])
        redundant_run, _ = self.start(
            "cold-redundant-set", root=redundant_root
        )
        redundant = self.propose_set(
            redundant_run, redundant_key, current_text, root=redundant_root
        )
        redundant_id = re.search(
            r"R\d{6}-Q\d{3}", redundant.stdout
        ).group(0)
        self.cli("sync", "--run", redundant_run, root=redundant_root)
        self.assertEqual(
            self.decision(redundant_id, redundant_root)["outcome"], "noop"
        )
        self.assertEqual(
            self.cold_current(redundant_root)[redundant_key], cold_before
        )
        self.assertEqual(self.conflict_files(redundant_root), [])
        self.assertIn(
            "PASSED",
            self.cli("doctor", "--read-only", root=redundant_root).stdout,
        )
        self.start("cold-redundant-restart", root=redundant_root)
        later_run, _ = self.start(
            "cold-redundant-later", root=redundant_root
        )
        later_text = "Later cold generation remains valid. " + "v" * 300
        self.apply_set(
            later_run,
            redundant_key,
            later_text,
            root=redundant_root,
        )
        self.assertIn(
            later_text,
            self.cli(
                "recall", "--key", redundant_key, root=redundant_root
            ).stdout,
        )
        self.assertIn(
            "PASSED",
            self.cli("doctor", "--read-only", root=redundant_root).stdout,
        )

        set_root = self.root / "compatible-sets"
        set_key = "cold.noop-compatible-sets"
        self.seed_cold_current(set_root, key=set_key)
        set_left, _ = self.start("cold-set-left", root=set_root)
        set_right, _ = self.start("cold-set-right", root=set_root)
        shared_text = "Concurrent compatible cold value. " + "s" * 300
        left_result = self.propose_set(
            set_left, set_key, shared_text, root=set_root
        )
        right_result = self.propose_set(
            set_right, set_key, shared_text, root=set_root
        )
        left_id = re.search(r"R\d{6}-Q\d{3}", left_result.stdout).group(0)
        right_id = re.search(r"R\d{6}-Q\d{3}", right_result.stdout).group(0)
        self.cli("sync", "--run", set_left, root=set_root)
        self.cli("sync", "--run", set_right, root=set_root)
        self.assertEqual(self.decision(left_id, set_root)["outcome"], "accepted")
        self.assertEqual(self.decision(right_id, set_root)["outcome"], "noop")
        self.assertEqual(self.current_count(set_key, set_root), 1)
        self.assertIn(set_key, self.cold_current(set_root))
        self.assertIn(
            shared_text,
            self.cli("recall", "--key", set_key, root=set_root).stdout,
        )
        self.assertEqual(self.conflict_files(set_root), [])
        self.assertIn(
            "PASSED", self.cli("doctor", "--read-only", root=set_root).stdout
        )
        self.start("cold-set-restart", root=set_root)

        touch_root = self.root / "compatible-touches"
        touch_key = "cold.noop-compatible-touches"
        self.seed_cold_current(touch_root, key=touch_key)
        touch_left, _ = self.start("cold-touch-left", root=touch_root)
        touch_right, _ = self.start("cold-touch-right", root=touch_root)

        def stage_touch(run_id):
            result = self.cli(
                "propose",
                "--run",
                run_id,
                "--operation",
                "touch",
                "--key",
                touch_key,
                "--source",
                "user",
                "--trust",
                "confirmed",
                root=touch_root,
            )
            return re.search(r"R\d{6}-Q\d{3}", result.stdout).group(0)

        touch_left_id = stage_touch(touch_left)
        touch_right_id = stage_touch(touch_right)
        self.cli("sync", "--run", touch_left, root=touch_root)
        cold_after_first = dict(self.cold_current(touch_root)[touch_key])
        self.cli("sync", "--run", touch_right, root=touch_root)
        self.assertEqual(
            self.decision(touch_left_id, touch_root)["outcome"], "accepted"
        )
        self.assertEqual(
            self.decision(touch_right_id, touch_root)["outcome"], "noop"
        )
        self.assertEqual(
            self.cold_current(touch_root)[touch_key], cold_after_first
        )
        self.assertIn(f"[L:{touch_left}]", cold_after_first["raw_line"])
        self.assertEqual(self.current_count(touch_key, touch_root), 1)
        self.assertEqual(self.conflict_files(touch_root), [])
        self.assertIn(
            "PASSED",
            self.cli("doctor", "--read-only", root=touch_root).stdout,
        )
        self.start("cold-touch-noop-restart", root=touch_root)

    def test_historical_cold_base_survives_unrelated_head_advance(self):
        for operation in ("set", "touch", "close"):
            with self.subTest(operation=operation):
                root = self.root / f"historical-cold-{operation}"
                key = f"a.target-{operation}"
                _old_text, current_text = self.seed_cold_current(root, key=key)
                base_revision = self.state(root)["head_revision"]
                cold_before = dict(self.cold_current(root)[key])
                actor_run, _ = self.start(
                    f"historical-cold-{operation}-actor", root=root
                )
                unrelated_run, _ = self.start(
                    f"historical-cold-{operation}-unrelated", root=root
                )
                self.apply_set(
                    unrelated_run,
                    f"z.advance-{operation}",
                    f"Unrelated advance before historical cold {operation}.",
                    tier=1,
                    root=root,
                    new_subject=True,
                    importance=5,
                )
                head_after_unrelated = self.state(root)["head_revision"]
                self.assertGreater(head_after_unrelated, base_revision)
                self.assertEqual(
                    self.state(root)["active_runs"][actor_run]["base_revision"],
                    base_revision,
                )
                self.assertEqual(self.cold_current(root)[key], cold_before)

                if operation == "set":
                    proposed = self.propose_set(
                        actor_run, key, current_text, root=root
                    )
                    synced = self.cli("sync", "--run", actor_run, root=root)
                elif operation == "touch":
                    proposed, synced = self.apply_touch(
                        actor_run, key, root=root
                    )
                else:
                    proposed, synced = self.apply_close(
                        actor_run, key, root=root
                    )
                self.assertEqual(proposed.returncode, 0)
                self.assertEqual(synced.returncode, 0)
                proposal_id = re.search(
                    r"R\d{6}-Q\d{3}", proposed.stdout
                ).group(0)
                proposal = self.proposal_records(root)[proposal_id]
                self.assertEqual(proposal["base_revision"], head_after_unrelated)
                self.assertEqual(proposal["base_storage"], "cold")
                self.assertEqual(
                    proposal["base_hash"],
                    hashlib.sha256(
                        cold_before["raw_line"].encode("utf-8")
                    ).hexdigest(),
                )
                self.assertEqual(
                    proposal["base_archive_proposal_id"],
                    cold_before["archived_by"],
                )
                decision = self.decision(proposal_id, root)
                self.assertEqual(
                    decision["outcome"],
                    "noop" if operation == "set" else "accepted",
                )
                if operation == "set":
                    self.assertEqual(self.cold_current(root)[key], cold_before)
                elif operation == "touch":
                    self.assertIn(key, self.cold_current(root))
                    self.assertIn(
                        f"[L:{actor_run}]",
                        self.cold_current(root)[key]["raw_line"],
                    )
                else:
                    self.assertEqual(self.current_count(key, root), 0)
                self.assertEqual(self.conflict_files(root), [])
                self.assertIn(
                    "PASSED",
                    self.cli("doctor", "--read-only", root=root).stdout,
                )

        for operation in ("set", "touch", "close"):
            with self.subTest(origin="genesis", operation=operation):
                root = self.root / f"genesis-cold-{operation}"
                self.cli("migrate", root=root)
                key = f"a.genesis-{operation}"
                genesis_text = (
                    f"Inherited genesis value for historical cold {operation}. "
                    + "g" * 330
                )
                state_path = root.joinpath(".bimri", "state.json")
                genesis_state = self.state(root)
                revision_path = root.joinpath(
                    ".bimri",
                    "revisions",
                    f"V{genesis_state['head_revision']:06d}.md",
                )
                genesis_line = (
                    f"[R0-E1] [K:{key}] [I:3] [active] [T:working] "
                    f"[SRC:legacy] [F:R0] [L:R0] [] {genesis_text}"
                )
                tier2_comment = (
                    "<!-- Current work, risks and next actions. "
                    "Soft target: state.json. -->"
                )
                genesis_content = revision_path.read_text("utf-8").replace(
                    tier2_comment,
                    tier2_comment + "\n" + genesis_line,
                )
                self.assertIn(genesis_line, genesis_content)
                genesis_bytes = genesis_content.encode("utf-8")
                revision_path.write_bytes(genesis_bytes)
                root.joinpath("bimri.md").write_bytes(genesis_bytes)
                genesis_state["head_hash"] = hashlib.sha256(
                    genesis_bytes
                ).hexdigest()
                state_path.write_text(
                    json.dumps(genesis_state, indent=2, sort_keys=True) + "\n",
                    encoding="utf-8",
                )

                cooler_run, _ = self.start(
                    f"genesis-cold-{operation}-cooler", root=root
                )
                current_bytes = len(self.hot_text(root).encode("utf-8"))
                self.update_state(
                    root=root, hot_max_bytes=current_bytes + 32
                )
                self.apply_set(
                    cooler_run,
                    f"z.genesis-cool-{operation}",
                    "Protected unrelated cooling write. " + "p" * 160,
                    tier=1,
                    root=root,
                    new_subject=True,
                    importance=5,
                )
                self.assertIn(key, self.cold_current(root))
                inherited_cold = dict(self.cold_current(root)[key])
                cooled_revision = self.state(root)["head_revision"]
                self.assertEqual(inherited_cold["raw_line"], genesis_line)
                proposal_records = self.proposal_records(root)
                same_key_decisions = [
                    self.decision(proposal_id, root)
                    for proposal_id, record in proposal_records.items()
                    if record["key"] == key
                ]
                self.assertEqual(same_key_decisions, [])

                if operation == "set":
                    orphan_root = self.root / "genesis-orphan-archive"
                    shutil.copytree(root, orphan_root)
                    orphan_root.joinpath(
                        ".bimri",
                        "decisions",
                        f"{inherited_cold['archived_by']}.json",
                    ).unlink()
                    spec = importlib.util.spec_from_file_location(
                        "bimri_orphan_archive_probe", ENGINE
                    )
                    self.assertIsNotNone(spec)
                    self.assertIsNotNone(spec.loader)
                    module = importlib.util.module_from_spec(spec)
                    spec.loader.exec_module(module)
                    self.assertIsNone(
                        module.inherited_cold_current_at_revision(
                            module.Paths(orphan_root),
                            self.state(orphan_root),
                            key,
                            cooled_revision,
                        )
                    )

                actor_run, _ = self.start(
                    f"genesis-cold-{operation}-actor", root=root
                )
                advance_run, _ = self.start(
                    f"genesis-cold-{operation}-advance", root=root
                )
                self.apply_set(
                    advance_run,
                    f"z.genesis-advance-{operation}",
                    f"Second unrelated advance before genesis {operation}.",
                    tier=1,
                    root=root,
                    new_subject=True,
                    importance=5,
                )
                self.assertEqual(
                    self.state(root)["active_runs"][actor_run]["base_revision"],
                    cooled_revision,
                )

                if operation == "set":
                    proposed = self.propose_set(
                        actor_run,
                        key,
                        "Updated inherited genesis value.",
                        source="user",
                        trust="confirmed",
                        root=root,
                    )
                    synced = self.cli("sync", "--run", actor_run, root=root)
                elif operation == "touch":
                    proposed, synced = self.apply_touch(
                        actor_run, key, root=root
                    )
                else:
                    proposed, synced = self.apply_close(
                        actor_run, key, root=root
                    )
                self.assertEqual(proposed.returncode, 0)
                self.assertEqual(synced.returncode, 0)
                proposal_id = re.search(
                    r"R\d{6}-Q\d{3}", proposed.stdout
                ).group(0)
                proposal = self.proposal_records(root)[proposal_id]
                self.assertEqual(proposal["base_storage"], "cold")
                self.assertEqual(
                    proposal["base_hash"],
                    hashlib.sha256(genesis_line.encode("utf-8")).hexdigest(),
                )
                self.assertEqual(
                    proposal["base_archive_proposal_id"],
                    inherited_cold["archived_by"],
                )
                self.assertEqual(
                    self.decision(proposal_id, root)["outcome"], "accepted"
                )
                if operation == "close":
                    self.assertEqual(self.current_count(key, root), 0)
                else:
                    self.assertEqual(self.current_count(key, root), 1)
                self.assertEqual(self.conflict_files(root), [])
                self.assertIn(
                    "PASSED",
                    self.cli("doctor", "--read-only", root=root).stdout,
                )

        changed_root = self.root / "historical-cold-same-key-change"
        changed_key = "a.target-changed"
        _old_text, stale_text = self.seed_cold_current(
            changed_root, key=changed_key
        )
        stale_run, _ = self.start("historical-cold-stale", root=changed_root)
        writer_run, _ = self.start("historical-cold-writer", root=changed_root)
        advanced_text = "Actual same-key advance. " + "n" * 300
        self.apply_set(
            writer_run,
            changed_key,
            advanced_text,
            root=changed_root,
        )
        stale = self.propose_set(
            stale_run,
            changed_key,
            stale_text,
            root=changed_root,
            check=False,
        )
        self.assertNotEqual(stale.returncode, 0)
        self.assertIn(f"sync {stale_run}", stale.stderr)
        self.assertIn(advanced_text, self.cli(
            "recall", "--key", changed_key, root=changed_root
        ).stdout)
        self.assertEqual(
            list(
                changed_root.joinpath(".bimri", "proposals").glob(
                    f"{stale_run}-Q*.json"
                )
            ),
            [],
        )
        self.assertEqual(self.conflict_files(changed_root), [])

    def test_applying_proposal_crash_recovers_across_cold_residency(self):
        for operation in ("set", "touch", "close"):
            with self.subTest(operation=operation):
                case_root = self.root / f"cold-{operation}"
                key = f"crash.cold-{operation}"
                _old_text, current_text = self.seed_cold_current(
                    case_root, key=key
                )
                operation_run, _ = self.start(
                    f"cold-{operation}-applying", root=case_root
                )
                if operation == "set":
                    expected_text = (
                        "Updated cold value after applying-decision crash. "
                        + "u" * 260
                    )
                    staged = self.propose_set(
                        operation_run,
                        key,
                        expected_text,
                        root=case_root,
                    )
                else:
                    arguments = [
                        "propose",
                        "--run",
                        operation_run,
                        "--operation",
                        operation,
                        "--key",
                        key,
                        "--source",
                        "user",
                        "--trust",
                        "confirmed",
                    ]
                    staged = self.cli(*arguments, root=case_root)
                proposal_id = re.search(
                    r"R\d{6}-Q\d{3}", staged.stdout
                ).group(0)
                proposal = self.proposal_records(case_root)[proposal_id]
                self.assertEqual(proposal["base_storage"], "cold")

                crashed = subprocess.run(
                    [
                        sys.executable,
                        str(CRASH_WORKER),
                        str(ENGINE),
                        "proposal_crash_after_applying_decision",
                        str(case_root),
                        "sync",
                        "--run",
                        operation_run,
                    ],
                    text=True,
                    capture_output=True,
                    timeout=120,
                )
                self.assertEqual(
                    crashed.returncode, 108, crashed.stdout + crashed.stderr
                )
                self.assertEqual(
                    self.decision(proposal_id, case_root)["outcome"],
                    "applying",
                )
                self.assertIn(key, self.cold_current(case_root))
                self.assertIn(
                    current_text,
                    self.cli(
                        "recall", "--key", key, root=case_root
                    ).stdout,
                )
                doctor = self.cli(
                    "doctor", "--read-only", root=case_root, check=False
                )
                self.assertEqual(
                    doctor.returncode, 0, doctor.stdout + doctor.stderr
                )
                self.start(
                    f"cold-{operation}-applying-restart", root=case_root
                )

                retried = self.cli(
                    "sync", "--run", operation_run, root=case_root
                )
                self.assertIn("applied 1", retried.stdout)
                self.assertEqual(
                    self.decision(proposal_id, case_root)["outcome"],
                    "accepted",
                )
                if operation == "set":
                    current = self.cli(
                        "recall", "--key", key, root=case_root
                    )
                    self.assertIn(expected_text, current.stdout)
                    self.assertIn(key, self.cold_current(case_root))
                elif operation == "touch":
                    raw = self.cold_current(case_root)[key]["raw_line"]
                    self.assertIn(f"[L:{operation_run}]", raw)
                    self.assertIn(current_text, raw)
                else:
                    self.assertEqual(self.current_count(key, case_root), 0)
                    history = self.cli(
                        "recall",
                        "--key",
                        key,
                        "--history",
                        "--limit",
                        "20",
                        root=case_root,
                    )
                    self.assertIn(current_text, history.stdout)
                self.assertEqual(self.conflict_files(case_root), [])
                self.assertIn(
                    "PASSED",
                    self.cli(
                        "doctor", "--read-only", root=case_root
                    ).stdout,
                )

        cooled_root = self.root / "hot-base-cooled-before-sync"
        key = "crash.hot-base-cooled"
        seed_run, _ = self.start("hot-base-seed", root=cooled_root)
        original_text = "Hot observed base that will cool before sync."
        self.apply_set(
            seed_run,
            key,
            original_text,
            root=cooled_root,
            new_subject=True,
            importance=1,
        )
        staged_run, _ = self.start("hot-base-staged", root=cooled_root)
        updated_text = "Update staged while its unchanged base was hot."
        staged = self.propose_set(
            staged_run, key, updated_text, root=cooled_root
        )
        staged_id = re.search(r"R\d{6}-Q\d{3}", staged.stdout).group(0)
        staged_record = self.proposal_records(cooled_root)[staged_id]
        self.assertEqual(staged_record["base_storage"], "hot")

        current_bytes = len(self.hot_text(cooled_root).encode("utf-8"))
        self.update_state(
            root=cooled_root, hot_max_bytes=current_bytes + 32
        )
        pressure_run, _ = self.start("hot-base-pressure", root=cooled_root)
        self.apply_set(
            pressure_run,
            "crash.hot-base-pressure",
            "Unrelated pressure cools the unchanged base. " + "p" * 80,
            root=cooled_root,
            new_subject=True,
            importance=5,
        )
        self.assertIn(key, self.cold_current(cooled_root))
        self.assertIn(
            original_text,
            self.cold_current(cooled_root)[key]["raw_line"],
        )

        crashed = subprocess.run(
            [
                sys.executable,
                str(CRASH_WORKER),
                str(ENGINE),
                "proposal_crash_after_applying_decision",
                str(cooled_root),
                "sync",
                "--run",
                staged_run,
            ],
            text=True,
            capture_output=True,
            timeout=120,
        )
        self.assertEqual(crashed.returncode, 108, crashed.stdout + crashed.stderr)
        self.assertEqual(
            self.decision(staged_id, cooled_root)["outcome"], "applying"
        )
        doctor = self.cli(
            "doctor", "--read-only", root=cooled_root, check=False
        )
        self.assertEqual(doctor.returncode, 0, doctor.stdout + doctor.stderr)
        self.start("hot-base-cooled-restart", root=cooled_root)
        self.cli("sync", "--run", staged_run, root=cooled_root)
        self.assertEqual(
            self.decision(staged_id, cooled_root)["outcome"], "accepted"
        )
        self.assertIn(
            updated_text,
            self.cli("recall", "--key", key, root=cooled_root).stdout,
        )
        self.assertEqual(self.current_count(key, cooled_root), 1)
        self.assertEqual(self.conflict_files(cooled_root), [])
        self.assertIn(
            "PASSED",
            self.cli("doctor", "--read-only", root=cooled_root).stdout,
        )

    def test_inherited_hot_overflow_allows_monotonic_tier1_repair(self):
        seed_run, _ = self.start("inherited-overflow-seed")
        sizes = []
        keys = []
        for number in range(1, 4):
            key = f"inherited.protected-{number:03d}"
            keys.append(key)
            self.apply_set(
                seed_run,
                key,
                f"Inherited protected memory {number:03d}. " + "p" * 430,
                tier=1,
                new_subject=True,
                importance=5,
            )
            sizes.append(len(self.hot_text().encode("utf-8")))

        ceiling = (sizes[0] + sizes[1]) // 2
        self.assertLess(sizes[0], ceiling)
        self.assertLess(ceiling, sizes[1])
        self.assertLess(ceiling, sizes[2])
        self.update_state(hot_max_bytes=ceiling)

        first_run, started = self.start("inherited-overflow-repair-one")
        self.assertIn(first_run, started.stdout)
        first_proposed, first_synced = self.apply_close(first_run, keys[2])
        self.assertEqual(first_synced.returncode, 0)
        first_id = re.search(
            r"R\d{6}-Q\d{3}", first_proposed.stdout
        ).group(0)
        self.assertEqual(self.decision(first_id)["outcome"], "accepted")
        self.assertEqual(self.current_count(keys[2]), 0)
        self.assertGreater(len(self.hot_text().encode("utf-8")), ceiling)

        second_run, _ = self.start("inherited-overflow-repair-two")
        second_proposed, second_synced = self.apply_close(second_run, keys[1])
        self.assertEqual(second_synced.returncode, 0)
        second_id = re.search(
            r"R\d{6}-Q\d{3}", second_proposed.stdout
        ).group(0)
        self.assertEqual(self.decision(second_id)["outcome"], "accepted")
        self.assertEqual(self.current_count(keys[1]), 0)
        self.assertLessEqual(len(self.hot_text().encode("utf-8")), ceiling)
        self.assertEqual(self.current_count(keys[0]), 1)
        self.assertEqual(self.conflict_files(), [])
        self.assertIn("Held candidates: 0", self.cli("status").stdout)
        self.assertIn("PASSED", self.cli("doctor", "--read-only").stdout)

    def test_cold_authority_corruption_fails_before_start_mutation(self):
        cases = ("hot-cold-overlap", "missing-cooled-evidence")
        for case in cases:
            with self.subTest(case=case):
                case_root = self.root / case
                key = f"corruption.{case}"
                self.seed_cold_current(case_root, key=key)
                state_path = case_root / ".bimri" / "state.json"
                state = json.loads(state_path.read_text("utf-8"))
                cold = state["cold_current"][key]
                if case == "hot-cold-overlap":
                    head_path = case_root.joinpath(
                        ".bimri",
                        "revisions",
                        f"V{state['head_revision']:06d}.md",
                    )
                    content = head_path.read_text("utf-8")
                    content = content.replace(
                        "\n## Tier 3: Pattern Recognition",
                        f"\n{cold['raw_line']}\n\n## Tier 3: Pattern Recognition",
                        1,
                    )
                    head_path.write_text(content, encoding="utf-8")
                    case_root.joinpath("bimri.md").write_text(
                        content, encoding="utf-8"
                    )
                    state["head_hash"] = hashlib.sha256(
                        content.encode("utf-8")
                    ).hexdigest()
                    expected = "both hot and cold-current"
                else:
                    cold["archived_by"] = "R999999-Q999"
                    expected = "not bound to exactly one immutable cooled archive"
                state_path.write_text(
                    json.dumps(state, indent=2, sort_keys=True) + "\n",
                    encoding="utf-8",
                )

                before = self.protected_snapshot(case_root)
                started = self.cli(
                    "start",
                    "--actor",
                    "corruption-probe",
                    root=case_root,
                    check=False,
                )
                self.assertNotEqual(started.returncode, 0)
                self.assertIn(
                    expected, (started.stdout + started.stderr).lower()
                )
                self.assertEqual(self.protected_snapshot(case_root), before)

                doctor = self.cli(
                    "doctor", "--read-only", root=case_root, check=False
                )
                self.assertNotEqual(doctor.returncode, 0)
                self.assertIn(expected, (doctor.stdout + doctor.stderr).lower())
                self.assertEqual(self.protected_snapshot(case_root), before)

    def test_run_count_does_not_age_a_current_subject(self):
        run_id, _ = self.start("age-baseline")
        older_run_key = "zeta.run-neutral-subject"
        older_text = "Run count alone must not make this current subject older. " + "r" * 300
        self.apply_set(
            run_id,
            older_run_key,
            older_text,
            new_subject=True,
            importance=1,
        )

        for number in range(100):
            self.start(f"unrelated-{number:03d}")

        same_day_key = "alpha.same-day-peer"
        same_day_text = "This same-day peer differs only by its stable key. " + "s" * 300
        self.apply_set(
            run_id,
            same_day_key,
            same_day_text,
            new_subject=True,
            importance=1,
        )

        for number in range(120):
            self.apply_set(
                run_id,
                f"pressure.priority-{number:03d}",
                f"High-value pressure subject {number:03d}. " + "p" * 390,
                new_subject=True,
                importance=5,
            )
            if {older_run_key, same_day_key} & set(self.cold_current()):
                break
        else:
            self.fail("pressure never selected either comparable eviction candidate")

        self.assertGreaterEqual(self.state()["run_count"], 101)
        self.assertIn(same_day_key, self.cold_current())
        self.assertNotIn(older_run_key, self.cold_current())
        older = self.cli("recall", "--key", older_run_key)
        peer = self.cli("recall", "--key", same_day_key)
        self.assertIn("HOT", older.stdout)
        self.assertIn(older_text, older.stdout)
        self.assertIn("COLD", peer.stdout)
        self.assertIn(same_day_text, peer.stdout)

    def test_wall_clock_decay_can_cool_old_high_importance_before_recent_low(self):
        old_run, _ = self.start("old-high-importance")
        old_key = "retention.old-important"
        old_text = "Old high-importance context. " + "o" * 300
        self.apply_set(
            old_run,
            old_key,
            old_text,
            new_subject=True,
            importance=5,
        )

        recent_run, _ = self.start("recent-low-importance")
        recent_key = "retention.recent-low"
        recent_text = "Recent low-importance context. " + "r" * 300
        self.apply_set(
            recent_run,
            recent_key,
            recent_text,
            new_subject=True,
            importance=1,
        )

        state_path = self.root / ".bimri" / "state.json"
        state = json.loads(state_path.read_text("utf-8"))
        state["run_dates"][old_run] = (
            dt.date.today() - dt.timedelta(days=365)
        ).isoformat()
        state_path.write_text(
            json.dumps(state, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        current_bytes = len(self.hot_text().encode("utf-8"))
        self.update_state(hot_max_bytes=current_bytes + 32)

        incoming_run, _ = self.start("retention-pressure")
        incoming_key = "retention.new-important"
        self.apply_set(
            incoming_run,
            incoming_key,
            "New important pressure context. " + "n" * 80,
            new_subject=True,
            importance=5,
        )

        cold = self.cold_current()
        self.assertIn(old_key, cold)
        self.assertNotIn(recent_key, cold)
        self.assertNotIn(incoming_key, cold)
        self.assertIn("COLD", self.cli("recall", "--key", old_key).stdout)
        self.assertIn("HOT", self.cli("recall", "--key", recent_key).stdout)
        self.assertEqual(self.conflict_files(), [])

    def test_referenced_run_dates_survive_500_run_save_restart_and_pressure(self):
        old_run, _ = self.start("retention-old-referenced")
        old_key = "retention.referenced-old"
        old_text = "Old referenced high-importance context. " + "o" * 300
        self.apply_set(
            old_run,
            old_key,
            old_text,
            new_subject=True,
            importance=5,
        )
        old_date = (dt.date.today() - dt.timedelta(days=365)).isoformat()
        state_path = self.root.joinpath(".bimri", "state.json")
        state = self.state()
        state["run_dates"][old_run] = old_date
        for number in range(2, 502):
            state["run_dates"][f"R{number:06d}"] = dt.date.today().isoformat()
        state["run_count"] = 501
        state_path.write_text(
            json.dumps(state, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        self.assertGreater(len(self.state()["run_dates"]), 500)

        recent_run, _ = self.start("retention-recent-referenced")
        self.assertEqual(recent_run, "R000502")
        self.assertEqual(self.state()["run_dates"][old_run], old_date)
        recent_key = "retention.referenced-recent"
        recent_text = "Recent low-importance context. " + "r" * 300
        self.apply_set(
            recent_run,
            recent_key,
            recent_text,
            new_subject=True,
            importance=1,
        )
        old_line = next(
            line for line in self.hot_text().splitlines()
            if f"[K:{old_key}]" in line
        )
        self.assertIn(f"[F:{old_run}]", old_line)
        self.assertIn(f"[L:{old_run}]", old_line)

        for number in range(8):
            self.start(f"retention-noise-{number:02d}")
        after_noise = self.state()
        self.assertEqual(after_noise["run_dates"][old_run], old_date)
        self.assertEqual(
            after_noise["run_dates"][recent_run], dt.date.today().isoformat()
        )
        self.assertIn(old_text, self.cli("recall", "--key", old_key).stdout)

        pressure_run, _ = self.start("retention-referenced-pressure")
        current_bytes = len(self.hot_text().encode("utf-8"))
        self.update_state(hot_max_bytes=current_bytes + 32)
        incoming_key = "retention.referenced-incoming"
        self.apply_set(
            pressure_run,
            incoming_key,
            "New important pressure context. " + "n" * 80,
            new_subject=True,
            importance=5,
        )
        cold = self.cold_current()
        self.assertIn(old_key, cold)
        self.assertNotIn(recent_key, cold)
        self.assertNotIn(incoming_key, cold)
        self.assertIn(f"[F:{old_run}]", cold[old_key]["raw_line"])
        self.assertIn(f"[L:{old_run}]", cold[old_key]["raw_line"])
        self.assertEqual(self.state()["run_dates"][old_run], old_date)

        for number in range(8, 16):
            self.start(f"retention-noise-{number:02d}")
        restarted = self.state()
        self.assertEqual(restarted["run_dates"][old_run], old_date)
        self.assertIn(old_key, self.cold_current())
        self.assertIn("COLD", self.cli("recall", "--key", old_key).stdout)
        self.assertIn("HOT", self.cli("recall", "--key", recent_key).stdout)
        self.assertIn("PASSED", self.cli("doctor", "--read-only").stdout)

    def test_missing_head_during_staging_preserves_all_run_dates(self):
        for command_kind in ("journal", "proposal"):
            with self.subTest(command=command_kind):
                root = self.root / f"missing-head-{command_kind}"
                run_id, _ = self.start(
                    f"missing-head-{command_kind}", root=root
                )
                state_path = root / ".bimri" / "state.json"
                state = self.state(root)
                for number in range(2, 502):
                    state["run_dates"][f"R{number:06d}"] = (
                        dt.date(2026, 1, 1)
                        + dt.timedelta(days=number % 200)
                    ).isoformat()
                state["run_count"] = 501
                state_path.write_text(
                    json.dumps(state, indent=2, sort_keys=True) + "\n",
                    encoding="utf-8",
                )
                seeded_dates = dict(self.state(root)["run_dates"])
                self.assertEqual(len(seeded_dates), 501)
                head_revision = state["head_revision"]
                head_path = root.joinpath(
                    ".bimri", "revisions", f"V{head_revision:06d}.md"
                )
                self.assertTrue(head_path.is_file())
                revisions_before = {
                    path.name: path.read_bytes()
                    for path in root.joinpath(".bimri", "revisions").glob("V*.md")
                }

                if command_kind == "journal":
                    durable_text = (
                        "Journal survives accepted-head loss before state save."
                    )
                    command = [
                        "journal",
                        "--run",
                        run_id,
                        "--importance",
                        "4",
                        "--text",
                        durable_text,
                    ]
                else:
                    durable_text = (
                        "Proposal survives accepted-head loss before state save."
                    )
                    command = [
                        "propose",
                        "--run",
                        run_id,
                        "--operation",
                        "set",
                        "--tier",
                        "2",
                        "--key",
                        "recovery.staged-subject",
                        "--text",
                        durable_text,
                        "--source",
                        "user",
                        "--trust",
                        "confirmed",
                        "--new-subject",
                    ]

                staged = subprocess.run(
                    [
                        sys.executable,
                        str(CRASH_WORKER),
                        str(ENGINE),
                        "missing_head_before_state_save",
                        str(root),
                        *command,
                    ],
                    text=True,
                    capture_output=True,
                    timeout=120,
                )
                self.assertEqual(
                    staged.returncode, 0, staged.stdout + staged.stderr
                )
                after = self.state(root)
                self.assertEqual(after["run_dates"], seeded_dates)
                self.assertEqual(after["head_revision"], head_revision)
                self.assertFalse(head_path.exists())
                log_text = root.joinpath(
                    ".bimri", "log", f"{run_id}.md"
                ).read_text("utf-8")
                self.assertIn(durable_text, log_text)

                proposals = self.proposal_records(root)
                if command_kind == "journal":
                    self.assertEqual(proposals, {})
                else:
                    self.assertEqual(len(proposals), 1)
                    proposal = next(iter(proposals.values()))
                    self.assertEqual(proposal["text"], durable_text)
                    self.assertFalse(
                        root.joinpath(
                            ".bimri",
                            "decisions",
                            f"{proposal['proposal_id']}.json",
                        ).exists()
                    )

                blocked = self.cli(
                    "sync", "--run", run_id, root=root, check=False
                )
                self.assertNotEqual(blocked.returncode, 0)
                self.assertRegex(
                    blocked.stdout + blocked.stderr,
                    r"(?i)accepted head(?: revision)? is (?:missing|unsafe)",
                )
                self.assertEqual(self.state(root)["run_dates"], seeded_dates)
                self.assertEqual(self.conflict_files(root), [])
                remaining_revisions = {
                    path.name: path.read_bytes()
                    for path in root.joinpath(".bimri", "revisions").glob("V*.md")
                }
                self.assertEqual(
                    remaining_revisions,
                    {
                        name: content
                        for name, content in revisions_before.items()
                        if name != head_path.name
                    },
                )

    def test_confirmed_user_authority_cannot_be_overwritten_by_an_agent(self):
        owner_run, _ = self.start("owner-authority")
        key = "core.release-rule"
        owner_text = "Only verified builds may be marked ready."
        self.apply_set(
            owner_run,
            key,
            owner_text,
            tier=1,
            new_subject=True,
        )

        agent_run, _ = self.start("agent-attempt")
        candidate_text = "Unverified builds may be marked ready."
        attempted = self.propose_set(
            agent_run,
            key,
            candidate_text,
            tier=1,
            source="agent",
            trust="working",
        )
        self.assertEqual(attempted.returncode, 0)
        self.cli("sync", "--run", agent_run)
        candidate, _decision = self.held_proposal(key, candidate_text)
        self.assertEqual(
            candidate["hold_reason"], "confirmed-user-authority-required"
        )

        current = self.cli("recall", "--key", key, "--history")
        self.assertIn(owner_text, current.stdout)
        self.assertIn("HELD", current.stdout)
        hot = self.hot_text()
        self.assertIn(owner_text, hot)
        self.assertNotIn(candidate_text, hot)
        self.assertIn("[T:confirmed] [SRC:user]", hot)
        self.assertEqual(self.current_count(key), 1)
        self.assertEqual(self.conflict_files(), [])
        pull = self.cli(
            "recall",
            "--query",
            "unverified builds may be marked ready",
            "--limit",
            "5",
        )
        self.assertIn("HELD", pull.stdout)
        self.assertIn(candidate_text, pull.stdout)
        self.assertIn("held", self.cli("status").stdout.lower())
        _later, started = self.start("authority-later")
        self.assertNotIn(candidate["proposal_id"], started.stdout + started.stderr)

        self.apply_set(
            owner_run,
            key,
            "Only verified and signed builds may be marked ready.",
            tier=1,
        )
        updated = self.cli("recall", "--key", key)
        self.assertIn("verified and signed", updated.stdout)

    def test_start_is_quiet_when_a_same_key_conflict_exists(self):
        seed_run, _ = self.start("conflict-seed")
        key = "delivery.current-route"
        self.apply_set(
            seed_run,
            key,
            "Use the staged route.",
            new_subject=True,
        )

        left_run, _ = self.start("writer-left")
        right_run, _ = self.start("writer-right")
        self.propose_set(left_run, key, "Use the direct route.")
        self.propose_set(right_run, key, "Use the partner route.")
        self.cli("sync", "--run", left_run)
        self.cli("sync", "--run", right_run)
        conflicts = self.conflict_files()
        self.assertEqual(len(conflicts), 1)

        _later_run, started = self.start("unrelated-later")
        conflict_id = conflicts[0].stem
        upper_output = (started.stdout + started.stderr).upper()
        self.assertNotIn(conflict_id, started.stdout + started.stderr)
        self.assertNotIn("MEMORY CONFLICT", upper_output)
        self.assertNotIn("HUMAN DECISION", upper_output)
        self.assertNotIn("PENDING REVIEW", upper_output)

    def test_cold_live_conflict_reviews_and_resolves_both_choices(self):
        for choice in ("current", "candidate"):
            with self.subTest(choice=choice):
                case_root = self.root / choice
                key = f"conflict.cold-{choice}"
                self.seed_cold_current(case_root, key=key)

                live_run, _ = self.start("cold-conflict-live", root=case_root)
                candidate_run, _ = self.start(
                    "cold-conflict-candidate", root=case_root
                )
                live_text = (
                    f"Accepted cold live value for {choice}. " + "l" * 300
                )
                candidate_text = (
                    f"Incompatible cold candidate for {choice}. " + "q" * 300
                )
                live_proposal = self.propose_set(
                    live_run, key, live_text, root=case_root
                )
                candidate_proposal = self.propose_set(
                    candidate_run, key, candidate_text, root=case_root
                )
                live_id = re.search(
                    r"R\d{6}-Q\d{3}", live_proposal.stdout
                ).group(0)
                candidate_id = re.search(
                    r"R\d{6}-Q\d{3}", candidate_proposal.stdout
                ).group(0)
                self.cli("sync", "--run", live_run, root=case_root)
                contested = self.cli(
                    "sync", "--run", candidate_run, root=case_root
                )
                self.assertEqual(
                    self.decision(live_id, case_root)["outcome"], "accepted"
                )
                candidate_decision = self.decision(candidate_id, case_root)
                self.assertEqual(candidate_decision["outcome"], "contested")
                conflict_id = candidate_decision["conflict_id"]
                self.assertIn(conflict_id, contested.stdout)
                self.assertIn(key, self.cold_current(case_root))
                self.assertNotIn(f"[K:{key}]", self.hot_text(case_root))

                conflict = json.loads(
                    case_root.joinpath(
                        ".bimri", "conflicts", f"{conflict_id}.json"
                    ).read_text("utf-8")
                )
                self.assertIn(live_text, conflict["current_line"])
                review = self.cli("review", conflict_id, root=case_root)
                self.assertIn(f"MEMORY CONFLICT {conflict_id}", review.stdout)
                self.assertIn(f"Subject: {key}", review.stdout)
                self.assertIn(f'Live value: "{live_text}"', review.stdout)
                self.assertIn(f'Proposed value: "{candidate_text}"', review.stdout)
                self.assertIn(f"Choice {candidate_id}", review.stdout)
                self.assertNotIn("Live value: absent", review.stdout)
                self.assertIn(
                    "PASSED",
                    self.cli(
                        "doctor", "--read-only", root=case_root
                    ).stdout,
                )

                selected = "current" if choice == "current" else candidate_id
                resolved = self.cli(
                    "resolve",
                    conflict_id,
                    "--choose",
                    selected,
                    "--human-approved",
                    root=case_root,
                )
                self.assertIn(f"resolved with {selected}", resolved.stdout)
                resolution = json.loads(
                    case_root.joinpath(
                        ".bimri", "resolutions", f"{conflict_id}.json"
                    ).read_text("utf-8")
                )
                self.assertEqual(resolution["choice"], selected)

                expected_text = live_text if choice == "current" else candidate_text
                rejected_text = candidate_text if choice == "current" else live_text
                current = self.cli("recall", "--key", key, root=case_root)
                history = self.cli(
                    "recall",
                    "--key",
                    key,
                    "--history",
                    "--limit",
                    "20",
                    root=case_root,
                )
                self.assertIn("COLD", current.stdout)
                self.assertIn(expected_text, current.stdout)
                self.assertNotIn(rejected_text, current.stdout)
                self.assertIn(live_text, history.stdout)
                if choice == "candidate":
                    self.assertIn(candidate_text, history.stdout)
                self.assertEqual(self.current_count(key, case_root), 1)
                self.assertIn(key, self.cold_current(case_root))
                self.assertNotIn(f"[K:{key}]", self.hot_text(case_root))
                self.assertIn(
                    "Open conflicts: 0",
                    self.cli("status", root=case_root).stdout,
                )
                resolution_bytes = case_root.joinpath(
                    ".bimri", "resolutions", f"{conflict_id}.json"
                ).read_bytes()
                later_run, _ = self.start(
                    "cold-conflict-later-generation", root=case_root
                )
                later_text = (
                    f"Later accepted cold generation for {choice}. "
                    + "z" * 300
                )
                self.apply_set(later_run, key, later_text, root=case_root)
                later_current = self.cli(
                    "recall", "--key", key, root=case_root
                )
                self.assertIn("COLD", later_current.stdout)
                self.assertIn(later_text, later_current.stdout)
                self.assertEqual(
                    case_root.joinpath(
                        ".bimri", "resolutions", f"{conflict_id}.json"
                    ).read_bytes(),
                    resolution_bytes,
                )
                self.assertIn(
                    "PASSED",
                    self.cli("doctor", "--read-only", root=case_root).stdout,
                )

    def test_cold_candidate_satisfaction_is_derived_without_rewriting_history(self):
        key = "conflict.cold-satisfied"
        self.seed_cold_current(self.root, key=key)
        live_run, _ = self.start("cold-satisfied-live")
        candidate_run, _ = self.start("cold-satisfied-candidate")
        live_text = "Different accepted cold value. " + "l" * 300
        candidate_text = "Exact candidate cold value. " + "c" * 300
        self.propose_set(live_run, key, live_text)
        candidate = self.propose_set(candidate_run, key, candidate_text)
        candidate_id = re.search(
            r"R\d{6}-Q\d{3}", candidate.stdout
        ).group(0)
        self.cli("sync", "--run", live_run)
        self.cli("sync", "--run", candidate_run)
        contested = self.decision(candidate_id)
        self.assertEqual(contested["outcome"], "contested")
        conflict_id = contested["conflict_id"]
        decision_path = self.root.joinpath(
            ".bimri", "decisions", f"{candidate_id}.json"
        )
        conflict_path = self.root.joinpath(
            ".bimri", "conflicts", f"{conflict_id}.json"
        )
        decision_bytes = decision_path.read_bytes()
        conflict_bytes = conflict_path.read_bytes()

        satisfying_run, _ = self.start("cold-satisfying-writer")
        satisfying = self.propose_set(
            satisfying_run, key, candidate_text
        )
        satisfying_id = re.search(
            r"R\d{6}-Q\d{3}", satisfying.stdout
        ).group(0)
        self.cli("sync", "--run", satisfying_run)
        satisfying_revision = self.decision(satisfying_id)["revision"]
        self.assertIn(key, self.cold_current())
        self.assertIn(
            candidate_text, self.cli("recall", "--key", key).stdout
        )

        status = self.cli("status")
        default_review = self.cli("review")
        historical_review = self.cli("review", "--all")
        self.assertIn("Actionable concurrent conflicts: 0", status.stdout)
        self.assertIn("Satisfied historical candidates: 1", status.stdout)
        self.assertIn("Actionable concurrent conflicts: 0", default_review.stdout)
        self.assertNotIn(conflict_id, default_review.stdout)
        self.assertIn("SATISFIED HISTORICAL CANDIDATE", historical_review.stdout)
        self.assertIn(
            f"already satisfied by V{satisfying_revision:06d}",
            historical_review.stdout,
        )
        self.assertEqual(decision_path.read_bytes(), decision_bytes)
        self.assertEqual(conflict_path.read_bytes(), conflict_bytes)
        self.assertFalse(
            self.root.joinpath(
                ".bimri", "resolutions", f"{conflict_id}.json"
            ).exists()
        )
        self.assertIn("PASSED", self.cli("doctor", "--read-only").stdout)

        later_run, _ = self.start("cold-satisfied-later")
        later_text = "Later distinct cold generation. " + "z" * 300
        self.apply_set(later_run, key, later_text)
        later_status = self.cli("status")
        self.assertIn("Actionable concurrent conflicts: 0", later_status.stdout)
        self.assertIn("Satisfied historical candidates: 1", later_status.stdout)
        self.assertIn(later_text, self.cli("recall", "--key", key).stdout)
        self.assertEqual(decision_path.read_bytes(), decision_bytes)
        self.assertEqual(conflict_path.read_bytes(), conflict_bytes)
        self.assertIn("PASSED", self.cli("doctor", "--read-only").stdout)

    def test_hot_resolution_remains_valid_after_its_generation_cools(self):
        seed_run, _ = self.start("hot-resolution-seed")
        key = "conflict.hot-then-cold"
        self.apply_set(
            seed_run,
            key,
            "Initial hot conflict value.",
            new_subject=True,
            importance=1,
        )
        live_run, _ = self.start("hot-resolution-live")
        candidate_run, _ = self.start("hot-resolution-candidate")
        live_text = "Hot live value before resolution."
        candidate_text = "Hot chosen value that will later cool."
        self.propose_set(live_run, key, live_text)
        candidate = self.propose_set(candidate_run, key, candidate_text)
        candidate_id = re.search(
            r"R\d{6}-Q\d{3}", candidate.stdout
        ).group(0)
        self.cli("sync", "--run", live_run)
        self.cli("sync", "--run", candidate_run)
        conflict_id = self.decision(candidate_id)["conflict_id"]
        self.cli(
            "resolve",
            conflict_id,
            "--choose",
            candidate_id,
            "--human-approved",
        )
        chosen_line = next(
            line for line in self.hot_text().splitlines()
            if f"[K:{key}]" in line
        )
        self.assertIn(candidate_text, chosen_line)
        resolution_path = self.root.joinpath(
            ".bimri", "resolutions", f"{conflict_id}.json"
        )
        resolution_bytes = resolution_path.read_bytes()
        self.assertIn("PASSED", self.cli("doctor", "--read-only").stdout)

        current_bytes = len(self.hot_text().encode("utf-8"))
        self.update_state(hot_max_bytes=current_bytes + 32)
        pressure_run, _ = self.start("hot-resolution-pressure")
        self.apply_set(
            pressure_run,
            "conflict.pressure-important",
            "Important incoming pressure value. " + "p" * 80,
            new_subject=True,
            importance=5,
        )
        self.assertIn(key, self.cold_current())
        self.assertEqual(self.cold_current()[key]["raw_line"], chosen_line)
        self.assertNotIn(f"[K:{key}]", self.hot_text())
        self.assertEqual(resolution_path.read_bytes(), resolution_bytes)
        self.assertIn("PASSED", self.cli("doctor", "--read-only").stdout)
        self.start("hot-resolution-cold-restart")
        self.assertEqual(resolution_path.read_bytes(), resolution_bytes)
        self.assertIn(candidate_text, self.cli("recall", "--key", key).stdout)
        self.assertIn("PASSED", self.cli("doctor", "--read-only").stdout)

    def test_later_effects_cannot_retroactively_validate_authority_records(self):
        for operation in ("touch", "close"):
            with self.subTest(record="decision", operation=operation):
                root = self.root / f"terminal-{operation}"
                seed_run, _ = self.start(f"terminal-{operation}-seed", root=root)
                key = f"authority.terminal-{operation}"
                self.apply_set(
                    seed_run,
                    key,
                    f"Base value for the {operation} authority check.",
                    root=root,
                    new_subject=True,
                    importance=2,
                )
                base_revision = self.state(root)["head_revision"]
                writer_run, _ = self.start(
                    f"terminal-{operation}-writer", root=root
                )
                proposed = self.cli(
                    "propose",
                    "--run",
                    writer_run,
                    "--operation",
                    operation,
                    "--key",
                    key,
                    "--source",
                    "user",
                    "--trust",
                    "confirmed",
                    root=root,
                )
                proposal_id = re.search(
                    r"R\d{6}-Q\d{3}", proposed.stdout
                ).group(0)
                self.cli("sync", "--run", writer_run, root=root)
                final = self.decision(proposal_id, root)
                self.assertEqual(final["outcome"], "accepted")
                self.assertGreater(final["revision"], base_revision)
                if operation == "touch":
                    current_line = next(
                        line
                        for line in self.hot_text(root).splitlines()
                        if f"[K:{key}]" in line
                    )
                    self.assertIn(
                        f"[L:{writer_run}]",
                        current_line,
                    )
                else:
                    self.assertEqual(self.current_count(key, root), 0)
                    archive = "\n".join(
                        path.read_text("utf-8")
                        for path in root.joinpath(".bimri", "archive").glob("*.md")
                    )
                    self.assertIn(f"[BY:{proposal_id}] [closed]", archive)

                decision_path = root.joinpath(
                    ".bimri", "decisions", f"{proposal_id}.json"
                )
                forged = dict(final)
                forged["revision"] = base_revision
                decision_path.write_text(
                    json.dumps(forged, indent=2, sort_keys=True) + "\n",
                    encoding="utf-8",
                )
                failed = self.cli(
                    "doctor", "--read-only", root=root, check=False
                )
                self.assertNotEqual(failed.returncode, 0)
                self.assertIn(
                    "does not contain the proposal's recorded effect",
                    failed.stdout + failed.stderr,
                )

        for operation in ("set", "touch"):
            with self.subTest(record="resolution", operation=operation):
                root = self.root / f"resolution-{operation}"
                seed_run, _ = self.start(f"resolution-{operation}-seed", root=root)
                key = f"authority.resolution-{operation}"
                self.apply_set(
                    seed_run,
                    key,
                    f"Initial value before the {operation} conflict.",
                    root=root,
                    new_subject=True,
                    importance=1,
                )
                live_run, _ = self.start(
                    f"resolution-{operation}-live", root=root
                )
                candidate_run, _ = self.start(
                    f"resolution-{operation}-candidate", root=root
                )
                live_text = (
                    f"Competing live value for the {operation} conflict. "
                    + "l" * 180
                )
                self.propose_set(live_run, key, live_text, root=root)
                if operation == "set":
                    candidate_text = "Chosen set generation. " + "c" * 300
                    candidate = self.propose_set(
                        candidate_run, key, candidate_text, root=root
                    )
                else:
                    candidate = self.cli(
                        "propose",
                        "--run",
                        candidate_run,
                        "--operation",
                        "touch",
                        "--key",
                        key,
                        "--source",
                        "user",
                        "--trust",
                        "confirmed",
                        root=root,
                    )
                candidate_id = re.search(
                    r"R\d{6}-Q\d{3}", candidate.stdout
                ).group(0)
                self.cli("sync", "--run", live_run, root=root)
                self.cli("sync", "--run", candidate_run, root=root)
                contested = self.decision(candidate_id, root)
                self.assertEqual(contested["outcome"], "contested")
                conflict_id = contested["conflict_id"]
                self.cli(
                    "resolve",
                    conflict_id,
                    "--choose",
                    candidate_id,
                    "--human-approved",
                    root=root,
                )
                resolution_path = root.joinpath(
                    ".bimri", "resolutions", f"{conflict_id}.json"
                )
                resolution = json.loads(resolution_path.read_text("utf-8"))
                self.assertEqual(resolution["status"], "resolved")
                resolution_revision = resolution["revision_after"]
                self.assertGreater(
                    resolution_revision, resolution["revision_before"]
                )
                reflected = self.cli("recall", "--key", key, root=root).stdout
                if operation == "set":
                    self.assertIn(candidate_text, reflected)
                else:
                    self.assertIn(live_text, reflected)
                    current_line = next(
                        line
                        for line in self.hot_text(root).splitlines()
                        if f"[K:{key}]" in line
                    )
                    self.assertIn(f"[L:{candidate_run}]", current_line)

                pressure_run, _ = self.start(
                    f"resolution-{operation}-pressure", root=root
                )
                current_bytes = len(self.hot_text(root).encode("utf-8"))
                self.update_state(root=root, hot_max_bytes=current_bytes + 32)
                self.apply_set(
                    pressure_run,
                    f"authority.resolution-{operation}-pressure",
                    "Protected pressure value. " + "p" * 220,
                    tier=1,
                    root=root,
                    new_subject=True,
                    importance=5,
                )
                self.assertIn(key, self.cold_current(root))
                self.assertGreater(
                    self.state(root)["head_revision"], resolution_revision
                )

                conflict = json.loads(
                    root.joinpath(
                        ".bimri", "conflicts", f"{conflict_id}.json"
                    ).read_text("utf-8")
                )
                forged_revision = resolution["revision_before"]
                resolution["intended_revision_after"] = forged_revision
                resolution["revision_after"] = forged_revision
                resolution_path.write_text(
                    json.dumps(resolution, indent=2, sort_keys=True) + "\n",
                    encoding="utf-8",
                )
                for proposal_id in conflict["proposal_ids"]:
                    decision_path = root.joinpath(
                        ".bimri", "decisions", f"{proposal_id}.json"
                    )
                    decision = json.loads(decision_path.read_text("utf-8"))
                    decision["revision"] = forged_revision
                    decision_path.write_text(
                        json.dumps(decision, indent=2, sort_keys=True) + "\n",
                        encoding="utf-8",
                    )
                failed = self.cli(
                    "doctor", "--read-only", root=root, check=False
                )
                self.assertNotEqual(failed.returncode, 0)
                self.assertIn(
                    "does not contain the chosen proposal's effect",
                    failed.stdout + failed.stderr,
                )

    def test_crashed_resolution_recovers_after_pressure_and_same_key_activity(self):
        seed_run, _ = self.start("crash-resolution-seed")
        key = "conflict.crash-recovery"
        self.apply_set(
            seed_run,
            key,
            "Initial value before the crash conflict.",
            new_subject=True,
            importance=1,
        )
        live_run, _ = self.start("crash-resolution-live")
        candidate_run, _ = self.start("crash-resolution-candidate")
        live_text = "Accepted live value before the forced resolution."
        chosen_text = "Chosen effect committed before the resolution crash."
        self.propose_set(live_run, key, live_text)
        chosen = self.propose_set(candidate_run, key, chosen_text)
        chosen_id = re.search(r"R\d{6}-Q\d{3}", chosen.stdout).group(0)
        self.cli("sync", "--run", live_run)
        self.cli("sync", "--run", candidate_run)
        conflict_id = self.decision(chosen_id)["conflict_id"]

        crashed = subprocess.run(
            [
                sys.executable,
                str(CRASH_WORKER),
                str(ENGINE),
                "resolution_crash_after_force_apply",
                str(self.root),
                "resolve",
                conflict_id,
                "--choose",
                chosen_id,
                "--human-approved",
            ],
            text=True,
            capture_output=True,
            timeout=120,
        )
        self.assertEqual(crashed.returncode, 106, crashed.stdout + crashed.stderr)
        resolution_path = self.root.joinpath(
            ".bimri", "resolutions", f"{conflict_id}.json"
        )
        applying = json.loads(resolution_path.read_text("utf-8"))
        self.assertEqual(applying["status"], "applying")
        self.assertEqual(applying["choice"], chosen_id)
        intended_revision = applying["intended_revision_after"]
        self.assertEqual(self.state()["head_revision"], intended_revision)
        self.assertIn(chosen_text, self.cli("recall", "--key", key).stdout)

        current_bytes = len(self.hot_text().encode("utf-8"))
        self.update_state(hot_max_bytes=current_bytes + 32)
        pressure_run, _ = self.start("crash-resolution-pressure")
        self.apply_set(
            pressure_run,
            "conflict.crash-pressure",
            "Unrelated important pressure. " + "p" * 80,
            new_subject=True,
            importance=5,
        )
        self.assertIn(key, self.cold_current())

        later_run, _ = self.start("crash-resolution-later")
        later_text = "Later same-key current value survives recovery. " + "z" * 260
        self.apply_set(later_run, key, later_text)
        head_before_retry = self.state()["head_revision"]
        self.assertGreater(head_before_retry, intended_revision)
        self.assertIn(later_text, self.cli("recall", "--key", key).stdout)

        recovered = self.cli(
            "resolve",
            conflict_id,
            "--choose",
            chosen_id,
            "--human-approved",
        )
        self.assertRegex(
            recovered.stdout,
            rf"(?:resolved with|already resolved as) {re.escape(chosen_id)}",
        )
        self.assertEqual(self.state()["head_revision"], head_before_retry)
        resolution = json.loads(resolution_path.read_text("utf-8"))
        self.assertEqual(resolution["status"], "resolved")
        self.assertEqual(resolution["revision_after"], intended_revision)
        final_decision = self.decision(chosen_id)
        self.assertEqual(final_decision["outcome"], "accepted")
        self.assertEqual(final_decision["revision"], intended_revision)
        current = self.cli("recall", "--key", key)
        history = self.cli(
            "recall", "--key", key, "--history", "--limit", "20"
        )
        self.assertIn(later_text, current.stdout)
        self.assertNotIn(chosen_text, current.stdout)
        self.assertIn(chosen_text, history.stdout)
        self.assertIn("Open conflicts: 0", self.cli("status").stdout)
        self.assertIn("PASSED", self.cli("doctor", "--read-only").stdout)
        self.start("crash-resolution-restart")
        self.assertIn(later_text, self.cli("recall", "--key", key).stdout)
        self.assertIn("PASSED", self.cli("doctor", "--read-only").stdout)

    def test_global_authority_damage_blocks_silent_resolution_recovery(self):
        seed_run, _ = self.start("recovery-gate-seed")
        key = "conflict.recovery-gate"
        self.apply_set(
            seed_run,
            key,
            "Initial value before the recovery gate conflict.",
            new_subject=True,
            importance=3,
        )
        live_run, _ = self.start("recovery-gate-live")
        candidate_run, _ = self.start("recovery-gate-candidate")
        live_text = "Accepted live value before interrupted finalization."
        chosen_text = "Chosen value committed before authority damage is found."
        self.propose_set(live_run, key, live_text)
        chosen = self.propose_set(candidate_run, key, chosen_text)
        chosen_id = re.search(r"R\d{6}-Q\d{3}", chosen.stdout).group(0)
        self.cli("sync", "--run", live_run)
        self.cli("sync", "--run", candidate_run)
        conflict_id = self.decision(chosen_id)["conflict_id"]

        unrelated_run, _ = self.start("recovery-gate-unrelated")
        unrelated = self.propose_set(
            unrelated_run,
            "conflict.recovery-gate-unrelated",
            "Pending unrelated intent used only for the authority audit.",
            new_subject=True,
        )
        unrelated_id = re.search(
            r"R\d{6}-Q\d{3}", unrelated.stdout
        ).group(0)
        self.assertGreater(unrelated_id, chosen_id)
        unrelated_path = self.root.joinpath(
            ".bimri", "proposals", f"{unrelated_id}.json"
        )
        unrelated_bytes = unrelated_path.read_bytes()

        crashed = subprocess.run(
            [
                sys.executable,
                str(CRASH_WORKER),
                str(ENGINE),
                "resolution_crash_after_force_apply",
                str(self.root),
                "resolve",
                conflict_id,
                "--choose",
                chosen_id,
                "--human-approved",
            ],
            text=True,
            capture_output=True,
            timeout=120,
        )
        self.assertEqual(crashed.returncode, 106, crashed.stdout + crashed.stderr)
        resolution_path = self.root.joinpath(
            ".bimri", "resolutions", f"{conflict_id}.json"
        )
        candidate_decision_path = self.root.joinpath(
            ".bimri", "decisions", f"{chosen_id}.json"
        )
        applying = json.loads(resolution_path.read_text("utf-8"))
        self.assertEqual(applying["status"], "applying")
        committed_revision = applying["intended_revision_after"]
        self.assertEqual(self.state()["head_revision"], committed_revision)
        self.assertIn(chosen_text, self.hot_text())
        interrupted_decision = self.decision(chosen_id)
        self.assertEqual(interrupted_decision["outcome"], "contested")
        self.assertEqual(interrupted_decision["conflict_id"], conflict_id)

        unrelated_path.write_text("{}\n", encoding="utf-8")
        authority_directories = (
            "proposals",
            "decisions",
            "conflicts",
            "resolutions",
            "revisions",
            "archive",
        )

        def authority_bytes():
            return {
                path.relative_to(self.root).as_posix(): path.read_bytes()
                for directory in authority_directories
                for path in sorted(
                    self.root.joinpath(".bimri", directory).rglob("*")
                )
                if path.is_file() and not path.is_symlink()
            }

        frozen_authority = authority_bytes()
        before_state = self.state()
        blocked_start = self.cli(
            "start", "--actor", "blocked-silent-recovery", check=False
        )
        self.assertIn(
            "authority recovery",
            (blocked_start.stdout + blocked_start.stderr).lower(),
        )
        self.assertEqual(authority_bytes(), frozen_authority)
        after_start = self.state()
        for field in (
            "head_revision",
            "head_hash",
            "last_revision_reason",
            "cold_current",
        ):
            self.assertEqual(after_start[field], before_state[field], field)
        self.assertEqual(resolution_path.read_bytes(), frozen_authority[
            resolution_path.relative_to(self.root).as_posix()
        ])
        self.assertEqual(candidate_decision_path.read_bytes(), frozen_authority[
            candidate_decision_path.relative_to(self.root).as_posix()
        ])

        state_after_start = self.root.joinpath(".bimri", "state.json").read_bytes()
        blocked_doctor = self.cli("doctor", "--read-only", check=False)
        self.assertNotEqual(blocked_doctor.returncode, 0)
        self.assertIn(
            "authority recovery",
            (blocked_doctor.stdout + blocked_doctor.stderr).lower(),
        )
        self.assertEqual(authority_bytes(), frozen_authority)
        self.assertEqual(
            self.root.joinpath(".bimri", "state.json").read_bytes(),
            state_after_start,
        )

        unrelated_path.write_bytes(unrelated_bytes)
        recovered_run, recovered = self.start("recovery-gate-repaired")
        self.assertTrue(recovered_run.startswith("R"))
        self.assertNotIn("AUTHORITY RECOVERY NEEDED", recovered.stdout)
        resolution = json.loads(resolution_path.read_text("utf-8"))
        self.assertEqual(resolution["status"], "resolved")
        self.assertEqual(resolution["revision_after"], committed_revision)
        final = self.decision(chosen_id)
        self.assertEqual(final["outcome"], "accepted")
        self.assertEqual(final["revision"], committed_revision)
        self.assertEqual(self.state()["head_revision"], committed_revision)
        self.assertIn(chosen_text, self.cli("recall", "--key", key).stdout)
        self.assertIn("PASSED", self.cli("doctor", "--read-only").stdout)

    def test_crashed_current_and_dismiss_resolutions_preserve_later_truth(self):
        for choice in ("current", "dismiss"):
            with self.subTest(choice=choice):
                case_root = self.root / choice
                seed_run, _ = self.start(
                    f"{choice}-crash-seed", root=case_root
                )
                key = f"conflict.crash-{choice}"
                self.apply_set(
                    seed_run,
                    key,
                    f"Initial value before {choice} conflict.",
                    root=case_root,
                    new_subject=True,
                    importance=2,
                )
                live_run, _ = self.start(
                    f"{choice}-crash-live", root=case_root
                )
                candidate_run, _ = self.start(
                    f"{choice}-crash-candidate", root=case_root
                )
                live_text = f"Live value retained by {choice}."
                candidate_text = f"Candidate rejected by {choice}."
                self.propose_set(
                    live_run, key, live_text, root=case_root
                )
                candidate = self.propose_set(
                    candidate_run, key, candidate_text, root=case_root
                )
                candidate_id = re.search(
                    r"R\d{6}-Q\d{3}", candidate.stdout
                ).group(0)
                self.cli("sync", "--run", live_run, root=case_root)
                self.cli("sync", "--run", candidate_run, root=case_root)
                conflict_id = self.decision(
                    candidate_id, case_root
                )["conflict_id"]

                crashed = subprocess.run(
                    [
                        sys.executable,
                        str(CRASH_WORKER),
                        str(ENGINE),
                        "resolution_crash_after_applying_record",
                        str(case_root),
                        "resolve",
                        conflict_id,
                        "--choose",
                        choice,
                        "--human-approved",
                    ],
                    text=True,
                    capture_output=True,
                    timeout=120,
                )
                self.assertEqual(
                    crashed.returncode, 107, crashed.stdout + crashed.stderr
                )
                resolution_path = case_root.joinpath(
                    ".bimri", "resolutions", f"{conflict_id}.json"
                )
                applying = json.loads(resolution_path.read_text("utf-8"))
                self.assertEqual(applying["status"], "applying")
                self.assertEqual(applying["choice"], choice)
                intended_revision = applying["intended_revision_after"]
                self.assertEqual(
                    self.state(case_root)["head_revision"], intended_revision
                )
                self.assertIn(
                    live_text,
                    self.cli("recall", "--key", key, root=case_root).stdout,
                )

                later_run, _ = self.start(
                    f"{choice}-crash-later", root=case_root
                )
                later_text = f"Later truth after interrupted {choice}."
                self.apply_set(
                    later_run, key, later_text, root=case_root
                )
                head_before_retry = self.state(case_root)["head_revision"]
                self.assertGreater(head_before_retry, intended_revision)

                recovered = self.cli(
                    "resolve",
                    conflict_id,
                    "--choose",
                    choice,
                    "--human-approved",
                    root=case_root,
                )
                self.assertRegex(
                    recovered.stdout,
                    rf"(?:resolved with|already resolved as) {re.escape(choice)}",
                )
                self.assertEqual(
                    self.state(case_root)["head_revision"], head_before_retry
                )
                resolution = json.loads(resolution_path.read_text("utf-8"))
                self.assertEqual(resolution["status"], "resolved")
                self.assertEqual(
                    resolution["revision_after"], intended_revision
                )
                candidate_decision = self.decision(candidate_id, case_root)
                self.assertEqual(candidate_decision["outcome"], "noop")
                self.assertEqual(
                    candidate_decision["revision"], intended_revision
                )
                current = self.cli(
                    "recall", "--key", key, root=case_root
                )
                self.assertIn(later_text, current.stdout)
                self.assertNotIn(candidate_text, current.stdout)
                self.assertIn(
                    "Open conflicts: 0",
                    self.cli("status", root=case_root).stdout,
                )
                self.assertIn(
                    "PASSED",
                    self.cli(
                        "doctor", "--read-only", root=case_root
                    ).stdout,
                )
                self.start(f"{choice}-crash-restart", root=case_root)
                self.assertIn(
                    later_text,
                    self.cli("recall", "--key", key, root=case_root).stdout,
                )

    def test_candidate_resolution_crash_before_apply_rebinds_without_losing_intent(self):
        seed_run, _ = self.start("preapply-crash-seed")
        key = "conflict.preapply-crash"
        self.apply_set(
            seed_run,
            key,
            "Initial value before the pre-apply crash.",
            new_subject=True,
            importance=2,
        )
        live_run, _ = self.start("preapply-crash-live")
        candidate_run, _ = self.start("preapply-crash-candidate")
        live_text = "Live value while owner resolution is interrupted."
        chosen_text = "Owner candidate applied only on explicit retry."
        self.propose_set(live_run, key, live_text)
        chosen = self.propose_set(candidate_run, key, chosen_text)
        chosen_id = re.search(r"R\d{6}-Q\d{3}", chosen.stdout).group(0)
        self.cli("sync", "--run", live_run)
        self.cli("sync", "--run", candidate_run)
        conflict_id = self.decision(chosen_id)["conflict_id"]

        crashed = subprocess.run(
            [
                sys.executable,
                str(CRASH_WORKER),
                str(ENGINE),
                "resolution_crash_after_applying_record",
                str(self.root),
                "resolve",
                conflict_id,
                "--choose",
                chosen_id,
                "--human-approved",
            ],
            text=True,
            capture_output=True,
            timeout=120,
        )
        self.assertEqual(crashed.returncode, 107, crashed.stdout + crashed.stderr)
        resolution_path = self.root.joinpath(
            ".bimri", "resolutions", f"{conflict_id}.json"
        )
        applying = json.loads(resolution_path.read_text("utf-8"))
        self.assertEqual(applying["status"], "applying")
        original_intended = applying["intended_revision_after"]
        self.assertEqual(self.state()["head_revision"] + 1, original_intended)
        self.assertIn(live_text, self.cli("recall", "--key", key).stdout)
        self.assertNotIn(chosen_text, self.cli("recall", "--key", key).stdout)

        unrelated_run, _ = self.start("preapply-crash-unrelated")
        self.apply_set(
            unrelated_run,
            "conflict.preapply-unrelated",
            "Unrelated accepted activity consumes the intended revision.",
            new_subject=True,
            importance=4,
        )
        self.assertGreaterEqual(self.state()["head_revision"], original_intended)

        later_run, _ = self.start("preapply-crash-later-intent")
        later_text = "Later same-key intent remains durable and held."
        later = self.propose_set(later_run, key, later_text)
        later_id = re.search(r"R\d{6}-Q\d{3}", later.stdout).group(0)
        later_proposal = self.proposal_records()[later_id]
        self.assertEqual(
            later_proposal["hold_reason"], "owner-resolution-in-progress"
        )
        self.cli("sync", "--run", later_run)
        later_decision = self.decision(later_id)
        self.assertEqual(later_decision["outcome"], "held")
        self.assertEqual(
            later_decision["reason"], "owner-resolution-in-progress"
        )
        self.assertIn(live_text, self.cli("recall", "--key", key).stdout)
        self.assertEqual(self.conflict_files(), [
            self.root / ".bimri" / "conflicts" / f"{conflict_id}.json"
        ])

        head_before_retry = self.state()["head_revision"]
        recovered = self.cli(
            "resolve",
            conflict_id,
            "--choose",
            chosen_id,
            "--human-approved",
        )
        self.assertIn(f"resolved with {chosen_id}", recovered.stdout)
        resolution = json.loads(resolution_path.read_text("utf-8"))
        self.assertEqual(resolution["status"], "resolved")
        self.assertEqual(
            resolution["intended_revision_after"], head_before_retry + 1
        )
        self.assertEqual(resolution["revision_after"], head_before_retry + 1)
        current = self.cli("recall", "--key", key)
        held = self.cli(
            "recall", "--query", "later same key intent durable held"
        )
        hot_line = next(
            line for line in current.stdout.splitlines()
            if line.startswith("HOT\t")
        )
        self.assertIn(chosen_text, hot_line)
        self.assertNotIn(later_text, hot_line)
        self.assertIn("HELD", held.stdout)
        self.assertIn(later_text, held.stdout)
        self.assertIn("Open conflicts: 0", self.cli("status").stdout)
        self.assertIn("PASSED", self.cli("doctor", "--read-only").stdout)
        self.start("preapply-crash-restart")
        self.assertIn(chosen_text, self.cli("recall", "--key", key).stdout)

    def test_orphan_cooled_archive_cannot_fake_resolution_commit(self):
        seed_run, _ = self.start("orphan-cooled-seed")
        key = "conflict.orphan-cooled"
        self.apply_set(
            seed_run,
            key,
            "Initial hot value before orphan archive crash.",
            new_subject=True,
            importance=1,
        )
        live_run, _ = self.start("orphan-cooled-live")
        candidate_run, _ = self.start("orphan-cooled-candidate")
        live_text = "Short live value at conflict creation."
        chosen_text = "Chosen value must be committed, not inferred. " + "c" * 400
        self.propose_set(live_run, key, live_text)
        chosen = self.propose_set(candidate_run, key, chosen_text)
        chosen_id = re.search(r"R\d{6}-Q\d{3}", chosen.stdout).group(0)
        self.cli("sync", "--run", live_run)
        self.cli("sync", "--run", candidate_run)
        conflict_id = self.decision(chosen_id)["conflict_id"]
        head_before_crash = self.state()["head_revision"]
        current_bytes = len(self.hot_text().encode("utf-8"))
        self.update_state(hot_max_bytes=current_bytes + 32)

        crashed = subprocess.run(
            [
                sys.executable,
                str(CRASH_WORKER),
                str(ENGINE),
                "resolution_crash_after_cooled_archive",
                str(self.root),
                "resolve",
                conflict_id,
                "--choose",
                chosen_id,
                "--human-approved",
            ],
            text=True,
            capture_output=True,
            timeout=120,
        )
        self.assertEqual(crashed.returncode, 109, crashed.stdout + crashed.stderr)
        resolution_path = self.root.joinpath(
            ".bimri", "resolutions", f"{conflict_id}.json"
        )
        applying = json.loads(resolution_path.read_text("utf-8"))
        self.assertEqual(applying["status"], "applying")
        original_intended = applying["intended_revision_after"]
        self.assertEqual(original_intended, head_before_crash + 1)
        self.assertEqual(self.state()["head_revision"], head_before_crash)
        self.assertNotIn(key, self.cold_current())
        self.assertIn(live_text, self.cli("recall", "--key", key).stdout)
        archive_text = "\n".join(
            path.read_text("utf-8")
            for path in self.root.joinpath(".bimri", "archive").glob("*.md")
        )
        self.assertIn(f"[BY:{chosen_id}] [cooled]", archive_text)
        self.assertIn(chosen_text, archive_text)

        unrelated_run, _ = self.start("orphan-cooled-unrelated")
        unrelated_key = "conflict.orphan-unrelated"
        self.apply_set(
            unrelated_run,
            unrelated_key,
            "Unrelated owner rule remains current.",
            tier=1,
            new_subject=True,
            importance=5,
        )
        self.assertEqual(self.state()["head_revision"], original_intended)
        self.assertIn(f"[K:{unrelated_key}]", self.hot_text())
        live_after_unrelated = self.cli("recall", "--key", key)
        self.assertRegex(live_after_unrelated.stdout, r"(?m)^(?:HOT|COLD)\t")
        self.assertIn(live_text, live_after_unrelated.stdout)
        self.assertEqual(self.current_count(key), 1)

        recovered = self.cli(
            "resolve",
            conflict_id,
            "--choose",
            chosen_id,
            "--human-approved",
        )
        self.assertIn(f"resolved with {chosen_id}", recovered.stdout)
        resolution = json.loads(resolution_path.read_text("utf-8"))
        self.assertEqual(resolution["status"], "resolved")
        self.assertGreater(resolution["revision_after"], original_intended)
        self.assertEqual(
            resolution["revision_after"], self.state()["head_revision"]
        )
        self.assertIn(key, self.cold_current())
        self.assertIn(chosen_text, self.cli("recall", "--key", key).stdout)
        self.assertIn(f"[K:{unrelated_key}]", self.hot_text())
        self.assertIn("PASSED", self.cli("doctor", "--read-only").stdout)
        self.start("orphan-cooled-restart")
        self.assertIn(chosen_text, self.cli("recall", "--key", key).stdout)
        self.assertIn("PASSED", self.cli("doctor", "--read-only").stdout)

    def test_resolution_revision_file_crash_rebinds_without_reusing_orphan(self):
        seed_run, _ = self.start("orphan-revision-seed")
        key = "conflict.orphan-revision"
        self.apply_set(
            seed_run,
            key,
            "Initial value before the orphan revision crash.",
            new_subject=True,
            importance=2,
        )
        live_run, _ = self.start("orphan-revision-live")
        candidate_run, _ = self.start("orphan-revision-candidate")
        live_text = "Accepted live value before the owner resolution."
        chosen_text = "Chosen value written into an orphan immutable revision."
        self.propose_set(live_run, key, live_text)
        chosen = self.propose_set(candidate_run, key, chosen_text)
        chosen_id = re.search(r"R\d{6}-Q\d{3}", chosen.stdout).group(0)
        self.cli("sync", "--run", live_run)
        self.cli("sync", "--run", candidate_run)
        conflict_id = self.decision(chosen_id)["conflict_id"]
        head_before_crash = self.state()["head_revision"]

        crashed = subprocess.run(
            [
                sys.executable,
                str(CRASH_WORKER),
                str(ENGINE),
                "crash_after_revision",
                str(self.root),
                "resolve",
                conflict_id,
                "--choose",
                chosen_id,
                "--human-approved",
            ],
            text=True,
            capture_output=True,
            timeout=120,
        )
        self.assertEqual(crashed.returncode, 91, crashed.stdout + crashed.stderr)
        resolution_path = self.root.joinpath(
            ".bimri", "resolutions", f"{conflict_id}.json"
        )
        applying = json.loads(resolution_path.read_text("utf-8"))
        self.assertEqual(applying["status"], "applying")
        orphan_revision = applying["intended_revision_after"]
        self.assertEqual(orphan_revision, head_before_crash + 1)
        self.assertEqual(self.state()["head_revision"], head_before_crash)
        orphan_path = self.root.joinpath(
            ".bimri", "revisions", f"V{orphan_revision:06d}.md"
        )
        self.assertTrue(orphan_path.is_file())
        orphan_bytes = orphan_path.read_bytes()
        self.assertIn(chosen_text, orphan_bytes.decode("utf-8"))
        self.assertIn(live_text, self.cli("recall", "--key", key).stdout)

        recovered = self.cli(
            "resolve",
            conflict_id,
            "--choose",
            chosen_id,
            "--human-approved",
        )
        self.assertRegex(
            recovered.stdout,
            rf"(?:resolved with|already resolved as) {re.escape(chosen_id)}",
        )
        resolution = json.loads(resolution_path.read_text("utf-8"))
        self.assertEqual(resolution["status"], "resolved")
        committed_revision = resolution["revision_after"]
        self.assertEqual(committed_revision, orphan_revision + 1)
        self.assertEqual(
            resolution["intended_revision_after"], committed_revision
        )
        self.assertEqual(self.state()["head_revision"], committed_revision)
        final = self.decision(chosen_id)
        self.assertEqual(final["outcome"], "accepted")
        self.assertEqual(final["revision"], committed_revision)
        self.assertEqual(orphan_path.read_bytes(), orphan_bytes)
        self.assertTrue(
            self.root.joinpath(
                ".bimri", "revisions", f"V{committed_revision:06d}.md"
            ).is_file()
        )
        self.assertIn(chosen_text, self.cli("recall", "--key", key).stdout)

        doctor = self.cli("doctor", "--read-only")
        self.assertIn("PASSED", doctor.stdout)
        self.assertIn(
            f"unreferenced immutable revision V{orphan_revision:06d}.md",
            doctor.stdout,
        )
        self.start("orphan-revision-restart")
        self.assertIn(chosen_text, self.cli("recall", "--key", key).stdout)
        self.assertIn("PASSED", self.cli("doctor", "--read-only").stdout)

    def test_populated_code_update_preserves_preexisting_memory_bytes(self):
        shutil.copytree(POPULATED_FIXTURE, self.root, dirs_exist_ok=True)
        before = self.protected_snapshot(self.root)
        hot_bytes = self.root.joinpath("bimri.md").read_bytes()
        old_state_bytes = self.root.joinpath(".bimri", "state.json").read_bytes()
        old_state = json.loads(old_state_bytes.decode("utf-8"))
        old_package = self.materialize_release(
            "ed8ea08", self.root / "_v503_source"
        )
        previous_engine = old_package / "bimri-engine.py"

        old_install = self.cli(
            "install",
            "--target",
            self.root,
            "--quiescent",
            root=self.root,
            engine=previous_engine,
            timeout=120,
        )
        self.assertIn("BIMRI 5.0.3 installed", old_install.stdout)
        self.assertEqual(self.root.joinpath("bimri.md").read_bytes(), hot_bytes)
        after_old_install = self.protected_snapshot(self.root)
        for relative, fingerprint in before.items():
            self.assertEqual(after_old_install[relative], fingerprint, relative)
        old_receipts = list(
            self.root.joinpath(".bimri-update-backups").glob(
                "*/install-manifest.json"
            )
        )
        self.assertEqual(len(old_receipts), 1)
        old_receipt_path = old_receipts[0]
        old_receipt_bytes = old_receipt_path.read_bytes()
        old_receipt = json.loads(old_receipt_bytes.decode("utf-8"))
        self.assertEqual(old_receipt["engine_release"], "5.0.3")
        self.assertEqual(old_receipt["status"], "installed")

        installed = self.cli(
            "install",
            "--target",
            self.root,
            "--quiescent",
            root=REPOSITORY,
            timeout=120,
        )

        self.assertIn("BIMRI 5.1.1 installed", installed.stdout)
        self.assertIn("Memory preservation: PASSED", installed.stdout)
        self.assertEqual(old_receipt_path.read_bytes(), old_receipt_bytes)
        self.assertEqual(self.root.joinpath("bimri.md").read_bytes(), hot_bytes)
        after = self.protected_snapshot(self.root)
        for relative, fingerprint in before.items():
            if relative in {
                ".bimri/engine.lock",
                ".bimri/state.json",
            }:
                continue
            self.assertIn(relative, after)
            self.assertEqual(after[relative], fingerprint, relative)
        migrated_state = self.state()
        self.assertEqual(migrated_state["bimri_version"], "5.1.0")
        for field, value in old_state.items():
            if field == "bimri_version":
                continue
            self.assertEqual(migrated_state[field], value, field)
        self.assertEqual(migrated_state["cold_current"], {})
        backed_up_states = list(
            self.root.joinpath(".bimri-update-backups").rglob("state*.json")
        )
        self.assertTrue(backed_up_states)
        self.assertIn(old_state_bytes, [path.read_bytes() for path in backed_up_states])
        self.assertIn(
            'ENGINE_VERSION = "5.1.1"',
            self.root.joinpath("bimri-engine.py").read_text("utf-8"),
        )

        installed_engine = self.root / "bimri-engine.py"
        protected_after_install = self.protected_snapshot(self.root)
        doctor = self.cli(
            "doctor",
            "--read-only",
            engine=installed_engine,
            root=self.root,
        )
        self.assertIn("PASSED", doctor.stdout)
        # Read-only audit leaves every byte in place.
        self.assertEqual(
            self.protected_snapshot(self.root), protected_after_install
        )
        status = self.cli("status", engine=installed_engine, root=self.root)
        self.assertIn("BIMRI engine v5.1.1", status.stdout)

        def without_derived_audit(snapshot):
            prefixes = (
                ".bimri/audit-witness.json",
                ".bimri/audit-manifest.json",
                ".bimri/audit-manifests",
                ".bimri/audit-transition.json",
                ".bimri/audit-drift",
            )
            return {
                relative: fingerprint
                for relative, fingerprint in snapshot.items()
                if not relative.startswith(prefixes)
            }

        # The first full command on the updated store may publish its
        # derived audit checkpoint; authority and memory bytes stay put.
        self.assertEqual(
            without_derived_audit(self.protected_snapshot(self.root)),
            without_derived_audit(protected_after_install),
        )

        before_old_attempt = self.protected_snapshot(self.root)
        old_attempt = self.cli(
            "start",
            "--actor",
            "old-engine-attempt",
            engine=previous_engine,
            root=self.root,
            check=False,
        )
        self.assertNotEqual(old_attempt.returncode, 0)
        self.assertIn("5.1.0", old_attempt.stdout + old_attempt.stderr)
        self.assertEqual(self.protected_snapshot(self.root), before_old_attempt)

        state_after_activation = self.root.joinpath(
            ".bimri", "state.json"
        ).read_bytes()
        before_repeat = self.protected_snapshot(self.root)
        repeated = self.cli(
            "install",
            "--target",
            self.root,
            "--quiescent",
            root=REPOSITORY,
            timeout=120,
        )
        self.assertIn("BIMRI 5.1.1 installed", repeated.stdout)
        self.assertIn("Memory preservation: PASSED", repeated.stdout)
        self.assertEqual(
            self.root.joinpath(".bimri", "state.json").read_bytes(),
            state_after_activation,
        )
        self.assertEqual(self.protected_snapshot(self.root), before_repeat)
        self.assertEqual(old_receipt_path.read_bytes(), old_receipt_bytes)

    def test_interrupted_authority_activation_recovers_on_one_retry(self):
        cases = (
            ("code_update_crash_before_activation_state", 104, "5.0.2"),
            ("code_update_crash_after_activation_state", 105, "5.1.0"),
        )
        for mode, exit_code, interrupted_version in cases:
            with self.subTest(mode=mode):
                case_root = self.root / mode
                shutil.copytree(POPULATED_FIXTURE, case_root)
                before = self.protected_snapshot(case_root)
                hot_bytes = case_root.joinpath("bimri.md").read_bytes()
                crashed = subprocess.run(
                    [
                        sys.executable,
                        str(CRASH_WORKER),
                        str(ENGINE),
                        mode,
                        str(case_root),
                        "install",
                        "--target",
                        str(case_root),
                        "--quiescent",
                    ],
                    text=True,
                    capture_output=True,
                    timeout=120,
                )
                self.assertEqual(
                    crashed.returncode,
                    exit_code,
                    crashed.stdout + crashed.stderr,
                )
                manifests = list(
                    case_root.joinpath(".bimri-update-backups").glob(
                        "*/install-manifest.json"
                    )
                )
                self.assertEqual(len(manifests), 1)
                interrupted = json.loads(manifests[0].read_text("utf-8"))
                self.assertEqual(
                    interrupted["status"], "prepared-for-authority-activation"
                )
                self.assertEqual(interrupted["engine_release"], "5.1.1")
                self.assertEqual(interrupted["memory_format"], "5.1.0")
                interrupted["engine_release"] = "5.1.0"
                manifests[0].write_text(
                    json.dumps(interrupted, indent=2, sort_keys=True) + "\n",
                    encoding="utf-8",
                )
                self.assertEqual(
                    self.state(case_root)["bimri_version"], interrupted_version
                )
                self.assertEqual(case_root.joinpath("bimri.md").read_bytes(), hot_bytes)
                after_crash = self.protected_snapshot(case_root)
                for relative, fingerprint in before.items():
                    if relative in {
                        ".bimri/engine.lock",
                        ".bimri/state.json",
                    }:
                        continue
                    self.assertEqual(after_crash[relative], fingerprint, relative)

                retry = self.cli(
                    "install",
                    "--target",
                    case_root,
                    "--quiescent",
                    root=REPOSITORY,
                    timeout=120,
                )
                self.assertIn("BIMRI 5.1.1 installed", retry.stdout)
                self.assertEqual(self.state(case_root)["bimri_version"], "5.1.0")
                self.assertEqual(case_root.joinpath("bimri.md").read_bytes(), hot_bytes)
                doctor = self.cli(
                    "doctor", "--read-only", root=case_root, engine=case_root / "bimri-engine.py"
                )
                self.assertIn("PASSED", doctor.stdout)
                terminal = {
                    json.loads(path.read_text("utf-8"))["status"]
                    for path in case_root.joinpath(".bimri-update-backups").glob(
                        "*/install-manifest.json"
                    )
                }
                self.assertIn("restored-before-retry", terminal)
                self.assertIn("installed", terminal)

    def test_v510_recovers_a_public_v503_prepared_update_receipt(self):
        shutil.copytree(POPULATED_FIXTURE, self.root, dirs_exist_ok=True)
        before = self.protected_snapshot(self.root)
        hot_bytes = self.root.joinpath("bimri.md").read_bytes()
        old_package = self.materialize_release(
            "ed8ea08", self.root / "_v503_prepared_source"
        )
        old_engine = old_package / "bimri-engine.py"

        crashed = subprocess.run(
            [
                sys.executable,
                str(CRASH_WORKER),
                str(old_engine),
                "code_update_crash_after_engine",
                str(self.root),
                "install",
                "--target",
                str(self.root),
                "--quiescent",
            ],
            text=True,
            capture_output=True,
            timeout=120,
        )
        self.assertEqual(crashed.returncode, 96, crashed.stdout + crashed.stderr)
        manifests = list(
            self.root.joinpath(".bimri-update-backups").glob(
                "*/install-manifest.json"
            )
        )
        self.assertEqual(len(manifests), 1)
        prepared = json.loads(manifests[0].read_text("utf-8"))
        self.assertEqual(prepared["engine_release"], "5.0.3")
        self.assertEqual(prepared["memory_format"], "5.0.2")
        self.assertEqual(prepared["status"], "prepared")
        self.assertEqual(self.root.joinpath("bimri.md").read_bytes(), hot_bytes)
        after_crash = self.protected_snapshot(self.root)
        for relative, fingerprint in before.items():
            self.assertEqual(after_crash[relative], fingerprint, relative)

        retry = self.cli(
            "install",
            "--target",
            self.root,
            "--quiescent",
            root=REPOSITORY,
            timeout=120,
        )
        self.assertIn("BIMRI 5.1.1 installed", retry.stdout)
        self.assertEqual(self.state()["bimri_version"], "5.1.0")
        self.assertEqual(self.root.joinpath("bimri.md").read_bytes(), hot_bytes)
        doctor = self.cli(
            "doctor",
            "--read-only",
            root=self.root,
            engine=self.root / "bimri-engine.py",
        )
        self.assertIn("PASSED", doctor.stdout)
        statuses = {
            json.loads(path.read_text("utf-8"))["status"]
            for path in self.root.joinpath(".bimri-update-backups").glob(
                "*/install-manifest.json"
            )
        }
        self.assertIn("restored-before-retry", statuses)
        self.assertIn("installed", statuses)

    def test_v510_recovers_a_public_v503_rollback_incomplete_receipt(self):
        shutil.copytree(POPULATED_FIXTURE, self.root, dirs_exist_ok=True)
        before = self.protected_snapshot(self.root)
        hot_bytes = self.root.joinpath("bimri.md").read_bytes()
        old_package = self.materialize_release(
            "ed8ea08", self.root / "_v503_rollback_source"
        )
        old_engine = old_package / "bimri-engine.py"

        baseline = self.cli(
            "install",
            "--target",
            self.root,
            "--quiescent",
            root=self.root,
            engine=old_engine,
            timeout=120,
        )
        self.assertIn("BIMRI 5.0.3 installed", baseline.stdout)
        self.assertEqual(self.root.joinpath("bimri.md").read_bytes(), hot_bytes)

        crashed = subprocess.run(
            [
                sys.executable,
                str(CRASH_WORKER),
                str(old_engine),
                "code_update_crash_after_engine",
                str(self.root),
                "install",
                "--target",
                str(self.root),
                "--quiescent",
            ],
            text=True,
            capture_output=True,
            timeout=120,
        )
        self.assertEqual(crashed.returncode, 96, crashed.stdout + crashed.stderr)
        manifests = list(
            self.root.joinpath(".bimri-update-backups").glob(
                "*/install-manifest.json"
            )
        )
        prepared_paths = [
            path
            for path in manifests
            if json.loads(path.read_text("utf-8"))["status"] == "prepared"
        ]
        self.assertEqual(len(prepared_paths), 1)
        interrupted_path = prepared_paths[0]
        interrupted = json.loads(interrupted_path.read_text("utf-8"))
        self.assertEqual(interrupted["engine_release"], "5.0.3")
        self.assertEqual(interrupted["memory_format"], "5.0.2")

        incomplete = subprocess.run(
            [
                sys.executable,
                str(CRASH_WORKER),
                str(old_engine),
                "code_update_fail_one_rollback_restore",
                str(self.root),
                "install",
                "--target",
                str(self.root),
                "--quiescent",
            ],
            text=True,
            capture_output=True,
            timeout=120,
        )
        self.assertEqual(incomplete.returncode, 2, incomplete.stdout + incomplete.stderr)
        self.assertIn(
            "forced transient rollback restore failure", incomplete.stderr
        )
        interrupted = json.loads(interrupted_path.read_text("utf-8"))
        self.assertEqual(interrupted["status"], "rollback-incomplete")
        self.assertEqual(self.root.joinpath("bimri.md").read_bytes(), hot_bytes)

        retry = self.cli(
            "install",
            "--target",
            self.root,
            "--quiescent",
            root=REPOSITORY,
            timeout=120,
        )
        self.assertIn("BIMRI 5.1.1 installed", retry.stdout)
        self.assertEqual(self.state()["bimri_version"], "5.1.0")
        self.assertEqual(self.root.joinpath("bimri.md").read_bytes(), hot_bytes)
        self.assertEqual(
            json.loads(interrupted_path.read_text("utf-8"))["status"],
            "restored-before-retry",
        )
        after = self.protected_snapshot(self.root)
        for relative, fingerprint in before.items():
            if relative in {
                ".bimri/engine.lock",
                ".bimri/state.json",
            }:
                continue
            self.assertEqual(after[relative], fingerprint, relative)
        self.assertIn(
            "PASSED",
            self.cli(
                "doctor",
                "--read-only",
                root=self.root,
                engine=self.root / "bimri-engine.py",
            ).stdout,
        )

    def test_serial_and_concurrent_same_key_updates_remain_bounded(self):
        serial_run, _ = self.start("serial-updates")
        key = "delivery.current-plan"
        values = ["Plan generation 00."]
        self.apply_set(serial_run, key, values[0], new_subject=True)
        for number in range(1, 11):
            value = f"Plan generation {number:02d}."
            values.append(value)
            self.apply_set(serial_run, key, value)
            self.assertEqual(self.current_count(key), 1)

        history = self.cli(
            "recall", "--key", key, "--history", "--limit", "50"
        )
        for value in values:
            self.assertIn(value, history.stdout)

        same_left, _ = self.start("same-left")
        same_right, _ = self.start("same-right")
        identical = "Concurrent identical generation."
        self.propose_set(same_left, key, identical)
        self.propose_set(same_right, key, identical)
        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
            results = list(
                pool.map(
                    lambda run: self.cli("sync", "--run", run, check=False),
                    (same_left, same_right),
                )
            )
        self.assertTrue(all(result.returncode == 0 for result in results))
        self.assertEqual(self.conflict_files(), [])
        self.assertEqual(self.current_count(key), 1)

        different_left, _ = self.start("different-left")
        different_right, _ = self.start("different-right")
        self.propose_set(different_left, key, "Concurrent left generation.")
        self.propose_set(different_right, key, "Concurrent right generation.")
        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
            list(
                pool.map(
                    lambda run: self.cli("sync", "--run", run, check=False),
                    (different_left, different_right),
                )
            )
        self.assertEqual(len(self.conflict_files()), 1)
        self.assertEqual(self.current_count(key), 1)


if __name__ == "__main__":
    unittest.main()
