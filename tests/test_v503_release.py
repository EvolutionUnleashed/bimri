"""Owner-visible release regressions for BIMRI engine v5.1.0.

These tests focus on the owner-visible contract: routine work stays quiet,
only proven incompatible concurrency becomes a pull-based review, tier targets
never block normal work, and current-version installation preserves memory.
"""

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
RUN_RE = re.compile(r"=== BIMRI BRIEF (R\d{6})")
PROPOSAL_RE = re.compile(r"\bR\d{6}-Q\d{3}\b")


class V510ReleaseContractTest(unittest.TestCase):
    maxDiff = None

    def setUp(self):
        self._temp = tempfile.TemporaryDirectory(prefix="bimri-v503-gate-")
        self.root = Path(self._temp.name)

    def tearDown(self):
        self._temp.cleanup()

    def cli(self, *arguments, root=None, check=True, timeout=30, engine=None):
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

    def start(self, actor="gate", root=None):
        result = self.cli("start", "--actor", actor, root=root)
        match = RUN_RE.search(result.stdout)
        self.assertIsNotNone(match, result.stdout)
        return match.group(1), result

    def propose(self, run_id, key, text, *extra, root=None, check=True):
        project = Path(root or self.root)
        hot = project / "bimri.md"
        state_path = project / ".bimri" / "state.json"
        current = hot.is_file() and f"[K:{key}]" in hot.read_text("utf-8")
        if state_path.is_file():
            current = current or key in json.loads(
                state_path.read_text("utf-8")
            ).get("cold_current", {})
        admission = () if current else ("--new-subject",)
        result = self.cli(
            "propose",
            "--run",
            run_id,
            "--tier",
            "2",
            "--key",
            key,
            "--text",
            text,
            *admission,
            *extra,
            root=root,
            check=check,
        )
        return result

    @staticmethod
    def tree_snapshot(root, exclusions=()):
        """Capture path types and bytes without following symlinks."""
        root = Path(root)
        excluded = set(exclusions)
        snapshot = {}
        for path in sorted(root.rglob("*"), key=lambda item: item.as_posix()):
            relative = path.relative_to(root).as_posix()
            if relative in excluded:
                continue
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
        return snapshot

    def authority_snapshot(self, root=None):
        root = Path(root or self.root)
        snapshot = self.tree_snapshot(
            root,
            exclusions=(
                ".bimri/engine.lock",
                ".bimri/runtime.local.json",
                ".bimri/hooks.claude.local.json",
            ),
        )
        return {
            relative: record
            for relative, record in snapshot.items()
            if relative == "bimri.md" or relative.startswith(".bimri/")
        }

    def state(self, root=None):
        return json.loads(
            Path(root or self.root)
            .joinpath(".bimri", "state.json")
            .read_text("utf-8")
        )

    def conflict_files(self, root=None):
        return sorted(
            Path(root or self.root).joinpath(".bimri", "conflicts").glob("C*.json")
        )

    def test_authority_release_is_distinct_from_hot_grammar(self):
        self.start()
        status = self.cli("status")

        self.assertIn("BIMRI engine v5.1.0", status.stdout)
        self.assertIn("memory format v5.1.0", status.stdout)
        self.assertEqual(self.state()["bimri_version"], "5.1.0")
        self.assertIn(
            "<!-- BIMRI v5.0.2 | Generated view.",
            (self.root / "bimri.md").read_text("utf-8"),
        )

    def test_same_run_retry_is_idempotent_and_self_conflict_is_prevented(self):
        run_id, _ = self.start("same-run")
        first = self.propose(run_id, "work.next", "First")
        proposal_id = PROPOSAL_RE.search(first.stdout).group(0)
        before_retry = self.authority_snapshot()

        retry = self.propose(run_id, "work.next", "First")
        self.assertEqual(PROPOSAL_RE.search(retry.stdout).group(0), proposal_id)
        self.assertEqual(self.authority_snapshot(), before_retry)

        different = self.propose(
            run_id,
            "work.next",
            "Second",
            check=False,
        )
        self.assertEqual(different.returncode, 2)
        self.assertIn(proposal_id, different.stderr)
        self.assertIn("sync", different.stderr.lower())
        self.assertEqual(self.authority_snapshot(), before_retry)
        self.assertEqual(self.state()["conflict_count"], 0)
        self.assertEqual(self.conflict_files(), [])

        self.cli("sync", "--run", run_id)
        later = self.propose(run_id, "work.next", "Second")
        self.assertRegex(later.stdout, PROPOSAL_RE)
        self.cli("sync", "--run", run_id)
        self.assertEqual(self.conflict_files(), [])

    def test_normal_tier1_admission_and_invalid_inputs_do_not_create_conflicts(self):
        first_run, _ = self.start("writer-one")
        stale_run, _ = self.start("writer-two")
        self.propose(first_run, "serial.key", "Canonical")
        self.cli("sync", "--run", first_run)

        stale = self.propose(
            stale_run,
            "serial.key",
            "Authored after the key was already changed",
            check=False,
        )
        self.assertEqual(stale.returncode, 2)
        self.assertIn("sync", stale.stderr.lower())

        admitted = self.cli(
            "propose", "--run", first_run, "--tier", "1", "--kind",
            "fact", "--new-subject", "--key", "core.new", "--text",
            "Admit direct owner memory", "--source", "user", "--trust",
            "confirmed",
        )
        self.assertEqual(admitted.returncode, 0)
        self.cli("sync", "--run", first_run)
        self.assertIn("[K:core.new]", (self.root / "bimri.md").read_text("utf-8"))

        rejected_commands = (
            (
                "propose", "--run", first_run, "--tier", "2", "--key",
                "semantic.question", "--new-subject", "--text", "Do not stage", "--needs-human",
                "--question", "Which value?",
            ),
            (
                "propose", "--run", first_run, "--tier", "2", "--key",
                "system.public", "--new-subject", "--text", "Do not stage", "--source", "system",
            ),
        )
        for command in rejected_commands:
            before = self.authority_snapshot()
            rejected = self.cli(*command, check=False)
            self.assertEqual(rejected.returncode, 2, rejected.stdout + rejected.stderr)
            self.assertEqual(self.authority_snapshot(), before)

        self.assertEqual(self.state()["conflict_count"], 0)
        self.assertEqual(self.conflict_files(), [])
        _run, later_start = self.start("later")
        self.assertNotIn("HUMAN DECISION NEEDED", later_start.stdout)
        self.assertNotIn("MEMORY CONFLICT", later_start.stdout)

    def test_concurrent_exact_effect_is_noop_but_incompatibility_notifies_once(self):
        identical_root = self.root / "identical"
        run_a, _ = self.start("identical-a", root=identical_root)
        run_b, _ = self.start("identical-b", root=identical_root)
        self.propose(run_a, "shared.key", "Same", root=identical_root)
        self.propose(run_b, "shared.key", "Same", root=identical_root)
        self.cli("sync", "--run", run_a, root=identical_root)
        second = self.cli("sync", "--run", run_b, root=identical_root)
        self.assertIn("already satisfied/no change", second.stdout)
        self.assertEqual(self.conflict_files(identical_root), [])

        conflict_root = self.root / "conflict"
        run_c, _ = self.start("writer-c", root=conflict_root)
        run_d, _ = self.start("writer-d", root=conflict_root)
        self.propose(run_c, "launch.next", "Verify checkout", root=conflict_root)
        candidate = self.propose(
            run_d,
            "launch.next",
            "Publish campaign",
            root=conflict_root,
        )
        candidate_id = PROPOSAL_RE.search(candidate.stdout).group(0)
        self.cli("sync", "--run", run_c, root=conflict_root)
        raised = self.cli("sync", "--run", run_d, root=conflict_root)
        conflicts = self.conflict_files(conflict_root)
        self.assertEqual(len(conflicts), 1)
        conflict_id = conflicts[0].stem
        self.assertIn(conflict_id, raised.stdout)
        self.assertIn("Concurrent", raised.stdout)

        replay = self.cli("sync", "--run", run_d, root=conflict_root)
        self.assertNotIn(conflict_id, replay.stdout)
        _later, started = self.start("later", root=conflict_root)
        self.assertNotIn(conflict_id, started.stdout)
        self.assertNotIn("HUMAN DECISION NEEDED", started.stdout)

        review = self.cli("review", conflict_id, root=conflict_root)
        for expected in (
            conflict_id,
            "launch.next",
            "Verify checkout",
            "Publish campaign",
            candidate_id,
            "Keep live",
        ):
            self.assertIn(expected, review.stdout)
        self.assertNotIn("[K:launch.next]", review.stdout)

    def test_one_hundred_serial_operations_stay_quiet(self):
        captured = []
        for number in range(25):
            run_id, started = self.start(f"serial-{number}")
            captured.append(started.stdout + started.stderr)
            proposed = self.propose(
                run_id,
                "serial.current",
                f"Iteration {number}",
            )
            captured.append(proposed.stdout + proposed.stderr)
            synced = self.cli("sync", "--run", run_id)
            captured.append(synced.stdout + synced.stderr)
            closed = self.cli(
                "close",
                "--run",
                run_id,
                "--outcome",
                "success",
                "--summary",
                f"Completed serial iteration {number}.",
            )
            captured.append(closed.stdout + closed.stderr)

        output = "\n".join(captured)
        self.assertNotIn("HUMAN DECISION NEEDED", output)
        self.assertNotIn("MEMORY CONFLICT", output)
        self.assertEqual(self.conflict_files(), [])
        self.assertEqual(self.state()["conflict_count"], 0)
        self.assertEqual(self.state()["head_revision"], 25)

    def test_current_version_update_and_read_only_doctor_preserve_memory_tree(self):
        target = self.root / "installed-project"
        self.start("existing", root=target)
        unknown = target / ".bimri" / "owner-unknown.bin"
        unknown.write_bytes(b"owner bytes\x00must survive")
        before = self.authority_snapshot(target)

        first_audit = self.cli("doctor", "--read-only", root=target)
        self.assertIn("BIMRI doctor (read-only): PASSED", first_audit.stdout)
        self.assertEqual(self.authority_snapshot(target), before)

        installed = self.cli(
            "install",
            "--target",
            target,
            "--quiescent",
            root=REPOSITORY,
            timeout=60,
        )
        self.assertIn("BIMRI 5.1.0 installed.", installed.stdout)
        self.assertIn("Existing authority store v5.1.0 verified", installed.stdout)
        self.assertIn("Memory preservation: PASSED", installed.stdout)
        self.assertEqual(self.authority_snapshot(target), before)
        self.assertEqual(self.state(target)["bimri_version"], "5.1.0")

        installed_engine = target / "bimri-engine.py"
        second_audit = self.cli(
            "doctor",
            "--read-only",
            root=target,
            engine=installed_engine,
        )
        self.assertIn("BIMRI doctor (read-only): PASSED", second_audit.stdout)
        self.assertEqual(self.authority_snapshot(target), before)


if __name__ == "__main__":
    unittest.main()
