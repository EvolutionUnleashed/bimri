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
import shutil
import stat
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path


REPOSITORY = Path(__file__).resolve().parents[1]
ENGINE = REPOSITORY / "bimri-engine.py"
CRASH_WORKER = REPOSITORY / "tests" / "crash_worker.py"
V5_0_2_FIXTURE = REPOSITORY / "tests" / "fixtures" / "v5.0.2-populated"
V5_0_2_COMMIT = "dfdd3ccdacdc1e13f34ffd6b1d66b4a73d5373bb"
V5_0_2_ENGINE_SHA256 = (
    "acbb26733ce601835edd59f84f4e3daa3359e91bad27d34e31e4b2ce9a23646b"
)
PROPOSAL_RE = re.compile(r"\bR\d{6}-Q\d{3}\b")
PROTECTED_EXCLUSIONS = {
    ".bimri/engine.lock",
    ".bimri/runtime.local.json",
    ".bimri/hooks.claude.local.json",
}
CODE_UPDATE_TARGETS = (
    "bimri-engine.py",
    "BIMRI-PROTOCOL.md",
    "BIMRI-MEMORY.template.md",
    "BIMRI-STATE.template.json",
    "BIMRI-AGENT-BLOCK.md",
    "INSTALL.md",
    "MIGRATION.md",
    "hooks-example.json",
    "legacy/README.md",
    "legacy/v1/BIMRI-global-instructions.md",
    "legacy/v3/BIMRI-global-instructions.md",
    "legacy/v3/BIMRI-global-instructions-v3.md",
    "legacy/v3/README.md",
    "BIMRI-LICENSE",
    "AGENTS.md",
    "CLAUDE.md",
    ".bimri/runtime.local.json",
    ".bimri/hooks.claude.local.json",
)


def protected_tree_snapshot(root):
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
            return {
                "type": "symlink",
                "target": target,
                "target_bytes_hex": os.fsencode(target).hex(),
            }
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
        record(hot)
        if hot.exists() or hot.is_symlink()
        else {"type": "missing"}
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
        new_subject=None,
        root=None,
    ):
        target_root = Path(root or self.root)
        if new_subject is None:
            marker = f"[K:{key}]"
            state_path = target_root / ".bimri" / "state.json"
            current_state = (
                json.loads(state_path.read_text("utf-8"))
                if state_path.is_file()
                else {}
            )
            run_meta = current_state.get("active_runs", {}).get(run_id, {})
            base_revision = run_meta.get("base_revision")
            base_path = target_root.joinpath(
                ".bimri", "revisions", f"V{base_revision:06d}.md"
            ) if isinstance(base_revision, int) else None
            hot_has_key = bool(
                base_path
                and base_path.is_file()
                and marker in base_path.read_text("utf-8")
            )
            cold_has_key = False
            if base_revision == current_state.get("head_revision"):
                cold_has_key = key in current_state.get("cold_current", {})
            new_subject = not (hot_has_key or cold_has_key)
        admission = ("--new-subject",) if new_subject else ()
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
            *admission,
            *extra,
            root=root,
        )
        match = PROPOSAL_RE.search(result.stdout)
        self.assertIsNotNone(match, result.stdout)
        return match.group(0)

    def stage_concurrent_candidate(
        self,
        key,
        candidate_text,
        *,
        root=None,
        candidate_run=None,
        candidate_actor="candidate",
        candidate_source="agent",
        writer_text=None,
    ):
        """Stage one current concurrent candidate without syncing it."""
        candidate_run = candidate_run or self.start(candidate_actor, root=root)
        writer_run = self.start("concurrent-writer", root=root)
        candidate = self.propose(
            candidate_run,
            key,
            candidate_text,
            source=candidate_source,
            trust="working",
            root=root,
        )
        writer = self.propose(
            writer_run,
            key,
            writer_text or f"Canonical writer for {key}.",
            source="agent",
            trust="working",
            root=root,
        )
        self.cli("sync", "--run", writer_run, root=root)
        self.assertEqual(self.decision(writer, root=root)["outcome"], "accepted")
        return candidate_run, candidate, writer_run, writer

    def state(self, root=None):
        target = root or self.root
        return json.loads((target / ".bimri" / "state.json").read_text("utf-8"))

    def decision(self, proposal_id, root=None):
        target = root or self.root
        path = target / ".bimri" / "decisions" / f"{proposal_id}.json"
        return json.loads(path.read_text("utf-8"))

    def hot(self, root=None):
        return (root or self.root).joinpath("bimri.md").read_text("utf-8")

    def seed_code_update_targets(self, target):
        target = Path(target)
        originals = {}
        for index, relative in enumerate(CODE_UPDATE_TARGETS, 1):
            path = target / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            if relative == ".bimri/runtime.local.json":
                content = b'{"owner":"old runtime binding"}\n'
            elif relative == ".bimri/hooks.claude.local.json":
                content = b'{"owner":"old hook binding"}\n'
            else:
                content = (
                    f"owner sentinel {index}: {relative}\n".encode("utf-8")
                )
            path.write_bytes(content)
            originals[relative] = content
        return originals

    def assert_installed_runtime_binding(self, target, agents_text=None):
        target = Path(target)
        agents_text = agents_text or (target / "AGENTS.md").read_text("utf-8")
        python_executable = str(Path(sys.executable).resolve())
        engine_path = str((target / "bimri-engine.py").resolve())
        installed_block = agents_text.split(
            "<!-- BIMRI:START -->", 1
        )[1].split("<!-- BIMRI:END -->", 1)[0].strip()
        self.assertEqual(
            installed_block,
            (target / "BIMRI-AGENT-BLOCK.md").read_text("utf-8").strip(),
        )
        self.assertEqual(
            installed_block.count("<!-- BIMRI:RUNTIME-BINDING:START -->"), 1
        )
        self.assertEqual(
            installed_block.count("<!-- BIMRI:RUNTIME-BINDING:END -->"), 1
        )
        for generic_path in (
            target / "AGENTS.md",
            target / "BIMRI-AGENT-BLOCK.md",
            target / "CLAUDE.md",
            target / "hooks-example.json",
        ):
            generic_text = generic_path.read_text("utf-8")
            self.assertNotIn(python_executable, generic_text)
            self.assertNotIn(engine_path, generic_text)

        runtime = json.loads(
            (target / ".bimri" / "runtime.local.json").read_text("utf-8")
        )
        self.assertEqual(runtime, {
            "argv_prefix": [python_executable, engine_path],
            "engine_path": engine_path,
            "host_bound": True,
            "python_executable": python_executable,
            "version": "5.1.1",
        })

        template = json.loads(
            (target / "hooks-example.json").read_text("utf-8")
        )
        hooks = json.loads(
            (target / ".bimri" / "hooks.claude.local.json").read_text("utf-8")
        )
        expected = {
            "SessionStart": "hook-start",
            "SessionEnd": "hook-close",
        }
        for event, subcommand in expected.items():
            template_command = template["hooks"][event][0]["hooks"][0]
            self.assertEqual(
                template_command["command"], "__BIMRI_VERIFIED_PYTHON__"
            )
            command = hooks["hooks"][event][0]["hooks"][0]
            self.assertEqual(command["type"], "command")
            self.assertEqual(command["command"], python_executable)
            self.assertEqual(command["args"], [
                "${CLAUDE_PROJECT_DIR}/bimri-engine.py",
                subcommand,
            ])
            self.assertEqual(command["timeout"], 90)
        self.assertNotIn("__BIMRI_VERIFIED_PYTHON__", json.dumps(hooks))
        self.assertFalse((target / ".claude").exists())

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
        before = {
            path.relative_to(self.root).as_posix(): path.read_bytes()
            for path in self.root.rglob("*")
            if path.is_file() and not path.is_symlink()
        }

        unknown = self.cli(
            "hook-close",
            input_text=json.dumps({
                "session_id": "unknown-session",
                "reason": "test",
            }),
            check=False,
        )
        self.assertEqual(unknown.returncode, 0)
        self.assertEqual(unknown.stdout, "")
        self.assertEqual(unknown.stderr, "")
        after = {
            path.relative_to(self.root).as_posix(): path.read_bytes()
            for path in self.root.rglob("*")
            if path.is_file() and not path.is_symlink()
        }
        self.assertEqual(after, before)
        self.assertEqual(set(self.state()["active_runs"]), {run_id})
        log = (self.root / ".bimri" / "log" / f"{run_id}.md").read_text("utf-8")
        self.assertNotIn("[CLOSED:", log)

        explicit = self.cli(
            "close",
            "--actor",
            "claude-code",
            "--session",
            "unknown-session",
            check=False,
        )
        self.assertEqual(explicit.returncode, 2)
        self.assertIn(
            "no active BIMRI run is mapped to that actor and session",
            explicit.stderr,
        )
        self.assertEqual(set(self.state()["active_runs"]), {run_id})

        mapped = self.cli(
            "hook-close",
            input_text=json.dumps({
                "session_id": "known-session",
                "reason": "test",
            }),
        )
        self.assertIn(f"run {run_id} closed", mapped.stdout)
        self.assertNotIn(run_id, self.state()["active_runs"])

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
                "--new-subject",
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
                "--new-subject",
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
                return self.propose(
                    run_id,
                    key,
                    text,
                    source="agent",
                    trust="working",
                )

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
            notices = sum(
                result.stdout.count("MEMORY CONFLICT")
                for result in sync_results
            )
            self.assertEqual(notices, 1, sync_results)

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
            first,
            "product.name",
            "The product is called Northstar.",
            source="user",
            trust="working",
        )
        second_proposal = self.propose(
            second,
            "product.name",
            "The product is called Wayfinder.",
            source="user",
            trust="working",
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

        self.cli(
            "resolve", conflict_id, "--choose", second_proposal,
            "--human-approved",
        )
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

    def test_v503_same_run_preflight_is_idempotent_and_write_free(self):
        run_id = self.start("codex")
        first = self.propose(
            run_id,
            "retry.subject",
            "Keep this working value.",
            source="agent",
            trust="working",
            extra=("--rationale", "Evidence from the current task."),
        )

        def snapshot():
            return {
                path.relative_to(self.root).as_posix(): path.read_bytes()
                for path in self.root.rglob("*")
                if (
                    path.is_file()
                    and not path.is_symlink()
                    and path != self.root / ".bimri" / "audit-witness.json"
                )
            }

        before_retry = snapshot()
        retry = self.cli(
            "propose",
            "--run",
            run_id,
            "--tier",
            "2",
            "--key",
            "retry.subject",
            "--text",
            "Keep this working value.",
            "--source",
            "agent",
            "--trust",
            "working",
            "--rationale",
            "Evidence from the current task.",
            "--new-subject",
        )
        self.assertEqual(retry.stdout.strip(), first)
        self.assertEqual(snapshot(), before_retry)

        changed_rationale = self.cli(
            "propose",
            "--run",
            run_id,
            "--tier",
            "2",
            "--key",
            "retry.subject",
            "--text",
            "Keep this working value.",
            "--source",
            "agent",
            "--trust",
            "working",
            "--rationale",
            "A materially different reason.",
            check=False,
        )
        self.assertEqual(changed_rationale.returncode, 2)
        self.assertIn(first, changed_rationale.stderr)
        self.assertIn("sync", changed_rationale.stderr)
        self.assertEqual(snapshot(), before_retry)

    def test_v503_stale_serial_proposal_is_rejected_before_mutation(self):
        stale_run = self.start("long-lived")
        writer_run = self.start("writer")
        committed = self.propose(
            writer_run,
            "serial.subject",
            "The committed value.",
            source="agent",
            trust="working",
        )
        self.cli("sync", "--run", writer_run)
        self.assertEqual(self.decision(committed)["outcome"], "accepted")
        before = {
            path.relative_to(self.root).as_posix(): path.read_bytes()
            for path in self.root.rglob("*")
            if path.is_file() and not path.is_symlink()
        }

        rejected = self.cli(
            "propose",
            "--run",
            stale_run,
            "--tier",
            "2",
            "--key",
            "serial.subject",
            "--text",
            "A stale serial overwrite.",
            "--source",
            "agent",
            "--trust",
            "working",
            check=False,
        )
        self.assertEqual(rejected.returncode, 2)
        self.assertIn(f"sync {stale_run}", rejected.stderr)
        after = {
            path.relative_to(self.root).as_posix(): path.read_bytes()
            for path in self.root.rglob("*")
            if path.is_file() and not path.is_symlink()
        }
        self.assertEqual(after, before)
        self.assertEqual(list((self.root / ".bimri" / "conflicts").glob("C*.json")), [])

    def test_v503_preflight_receipt_binds_current_head_after_unrelated_change(self):
        run_id = self.start("candidate")
        unrelated_run = self.start("unrelated")
        unrelated = self.propose(
            unrelated_run,
            "unrelated.subject",
            "An unrelated accepted change.",
            source="agent",
            trust="working",
        )
        self.cli("sync", "--run", unrelated_run)
        self.assertEqual(self.decision(unrelated)["outcome"], "accepted")
        state = self.state()

        proposal_id = self.propose(
            run_id,
            "receipt.subject",
            "Bind this proposal to the live head.",
            source="agent",
            trust="working",
        )
        proposal = json.loads(
            (
                self.root / ".bimri" / "proposals" / f"{proposal_id}.json"
            ).read_text("utf-8")
        )
        receipt = proposal["preflight_receipt"]
        self.assertEqual(proposal["bimri_version"], "5.1.0")
        self.assertEqual(proposal["base_revision"], state["head_revision"])
        self.assertEqual(receipt["engine_release"], "5.1.1")
        self.assertEqual(receipt["observed_head_revision"], state["head_revision"])
        self.assertEqual(receipt["observed_head_hash"], state["head_hash"])
        self.assertEqual(receipt["observed_key_hash"], "absent")

    def test_v510_authority_holds_and_soft_targets_create_no_conflicts(self):
        confirmed_run = self.start("owner")
        confirmed = self.propose(
            confirmed_run,
            "confirmed.subject",
            "Preserve this confirmed value.",
            source="user",
            trust="confirmed",
        )
        self.cli("sync", "--run", confirmed_run)
        self.assertEqual(self.decision(confirmed)["outcome"], "accepted")
        promotable = self.propose(
            confirmed_run,
            "working.subject",
            "Keep this in working memory.",
            source="agent",
            trust="working",
        )
        self.cli("sync", "--run", confirmed_run)
        self.assertEqual(self.decision(promotable)["outcome"], "accepted")
        policy_run = self.start("policy-agent")

        replacement = self.cli(
            "propose", "--run", policy_run, "--tier", "2",
            "--key", "confirmed.subject", "--text", "Replace it.",
            "--source", "agent", "--trust", "working",
        )
        replacement_id = PROPOSAL_RE.search(replacement.stdout).group(0)
        self.cli("sync", "--run", policy_run)
        replacement_proposal = json.loads((
            self.root / ".bimri" / "proposals" / f"{replacement_id}.json"
        ).read_text("utf-8"))
        self.assertEqual(
            replacement_proposal["hold_reason"],
            "confirmed-user-authority-required",
        )
        self.assertEqual(self.decision(replacement_id)["outcome"], "held")
        removal = self.cli(
            "propose", "--run", policy_run, "--operation", "close",
            "--key", "confirmed.subject", "--source", "agent",
            "--trust", "working",
        )
        removal_id = PROPOSAL_RE.search(removal.stdout).group(0)
        self.cli("sync", "--run", policy_run)
        removal_proposal = json.loads((
            self.root / ".bimri" / "proposals" / f"{removal_id}.json"
        ).read_text("utf-8"))
        self.assertEqual(
            removal_proposal["hold_reason"],
            "confirmed-user-authority-required",
        )
        self.assertEqual(self.decision(removal_id)["outcome"], "held")
        self.assertIn("Preserve this confirmed value.", self.hot())

        human_core = self.cli(
            "propose", "--run", policy_run, "--tier", "1",
            "--new-subject", "--key", "new.core", "--text", "New core.",
            "--kind", "fact", "--source", "user", "--trust", "confirmed",
        )
        human_core_id = PROPOSAL_RE.search(human_core.stdout).group(0)
        self.cli("sync", "--run", policy_run)
        self.assertEqual(self.decision(human_core_id)["outcome"], "accepted")
        self.assertIn("[K:new.core]", self.hot())

        promotion = self.cli(
            "propose", "--run", policy_run, "--tier", "1",
            "--key", "working.subject", "--text", "Promote it.",
            "--kind", "fact", "--source", "agent", "--trust", "working",
        )
        promotion_id = PROPOSAL_RE.search(promotion.stdout).group(0)
        self.cli("sync", "--run", policy_run)
        promotion_record = json.loads((
            self.root / ".bimri" / "proposals" / f"{promotion_id}.json"
        ).read_text("utf-8"))
        self.assertEqual(
            promotion_record["hold_reason"], "tier1-human-authority-required"
        )
        self.assertEqual(self.decision(promotion_id)["outcome"], "held")

        invalid_attempts = (
            (
                "system provenance",
                ("--tier", "2", "--new-subject", "--key", "system.claim",
                 "--text", "System claim.", "--source", "system", "--trust",
                 "confirmed"),
            ),
            (
                "semantic uncertainty",
                ("--tier", "2", "--new-subject", "--key", "uncertain.claim",
                 "--text", "Maybe.", "--source", "agent", "--trust", "working",
                 "--needs-human", "--question", "Which value?"),
            ),
        )
        for label, arguments in invalid_attempts:
            with self.subTest(label=label):
                result = self.cli(
                    "propose", "--run", policy_run, *arguments, check=False
                )
                self.assertEqual(result.returncode, 2, result.stderr)

        before_touch = next(
            line for line in self.hot().splitlines()
            if "[K:confirmed.subject]" in line
        )
        touch_id = self.cli(
            "propose", "--run", policy_run, "--operation", "touch",
            "--key", "confirmed.subject", "--source", "agent", "--trust", "working",
        ).stdout.strip()
        self.cli("sync", "--run", policy_run)
        self.assertEqual(self.decision(touch_id)["outcome"], "accepted")
        after_touch = next(
            line for line in self.hot().splitlines()
            if "[K:confirmed.subject]" in line
        )
        for field in (
            "[T:confirmed]", "[SRC:user]", "[I:3]", "[active]",
            f"[F:{confirmed_run}]", "Preserve this confirmed value.",
        ):
            self.assertIn(field, before_touch)
            self.assertIn(field, after_touch)

        state_path = self.root / ".bimri" / "state.json"
        state = self.state()
        state["tier2_max"] = 2
        state_path.write_text(json.dumps(state, indent=2, sort_keys=True) + "\n", "utf-8")
        capacity = self.cli(
            "propose",
            "--run",
            policy_run,
            "--tier",
            "2",
            "--new-subject",
            "--key",
            "over.capacity",
            "--text",
            "This cannot fit.",
            "--source",
            "agent",
            "--trust",
            "working",
        )
        capacity_id = PROPOSAL_RE.search(capacity.stdout).group(0)
        self.cli("sync", "--run", policy_run)
        self.assertEqual(self.decision(capacity_id)["outcome"], "accepted")
        self.assertIn("[K:over.capacity]", self.hot())
        self.assertEqual(self.state()["conflict_count"], 0)
        self.assertEqual(list((self.root / ".bimri" / "conflicts").glob("C*.json")), [])

    def test_pending_proposal_does_not_reserve_a_soft_tier_target(self):
        first_run = self.start("first")
        second_run = self.start("second")
        state_path = self.root / ".bimri" / "state.json"
        state = self.state()
        state["tier2_max"] = 1
        state_path.write_text(json.dumps(state, indent=2, sort_keys=True) + "\n", "utf-8")
        first = self.propose(
            first_run,
            "slot.one",
            "Reserve the final slot.",
            source="agent",
            trust="working",
        )
        before = {
            path.relative_to(self.root).as_posix(): path.read_bytes()
            for path in self.root.rglob("*")
            if path.is_file() and not path.is_symlink()
        }
        second = self.cli(
            "propose",
            "--run",
            second_run,
            "--tier",
            "2",
            "--new-subject",
            "--key",
            "slot.two",
            "--text",
            "Compete for the final slot.",
            "--source",
            "agent",
            "--trust",
            "working",
        )
        second_id = PROPOSAL_RE.search(second.stdout).group(0)
        self.assertEqual(
            [path.stem for path in (self.root / ".bimri" / "proposals").glob("*.json")],
            [first, second_id],
        )
        self.assertNotEqual(before, {
            path.relative_to(self.root).as_posix(): path.read_bytes()
            for path in self.root.rglob("*")
            if path.is_file() and not path.is_symlink()
        })
        self.cli("sync", "--run", first_run)
        self.cli("sync", "--run", second_run)
        self.assertEqual(self.decision(first)["outcome"], "accepted")
        self.assertEqual(self.decision(second_id)["outcome"], "accepted")
        self.assertIn("[K:slot.one]", self.hot())
        self.assertIn("[K:slot.two]", self.hot())
        self.assertEqual(list((self.root / ".bimri" / "conflicts").glob("C*.json")), [])

    def test_v503_genuine_concurrent_edit_notifies_once(self):
        first_run = self.start("same-label")
        second_run = self.start("same-label")
        first = self.propose(
            first_run,
            "concurrent.subject",
            "Candidate one.",
            source="agent",
            trust="working",
        )
        second = self.propose(
            second_run,
            "concurrent.subject",
            "Candidate two.",
            source="agent",
            trust="working",
        )
        self.cli("sync", "--run", first_run)
        self.assertEqual(self.decision(first)["outcome"], "accepted")
        created = self.cli("sync", "--run", second_run)
        self.assertIn("MEMORY CONFLICT C000001", created.stdout)
        self.assertIn("Subject: concurrent.subject", created.stdout)
        self.assertEqual(self.decision(second)["outcome"], "contested")
        self.assertEqual(self.state()["conflict_count"], 1)
        self.assertEqual(
            len(list((self.root / ".bimri" / "conflicts").glob("C*.json"))),
            1,
        )
        for _ in range(5):
            replay = self.cli("sync", "--run", second_run)
            self.assertNotIn("MEMORY CONFLICT", replay.stdout)
            self.assertNotIn("C000001", replay.stdout)
        self.assertEqual(self.state()["conflict_count"], 1)

    def test_v503_compatible_touch_and_close_are_strict_noops(self):
        first_set_run = self.start("set-a")
        second_set_run = self.start("set-b")
        first_set = self.propose(
            first_set_run,
            "compatible.set",
            "The exact shared value.",
            source="agent",
            trust="working",
        )
        second_set = self.propose(
            second_set_run,
            "compatible.set",
            "The exact shared value.",
            source="agent",
            trust="working",
        )
        self.cli("sync", "--run", first_set_run)
        self.cli("sync", "--run", second_set_run)
        self.assertEqual(self.decision(first_set)["outcome"], "accepted")
        self.assertEqual(self.decision(second_set)["outcome"], "noop")

        creator = self.start("creator")
        initial = self.propose(
            creator,
            "compatible.subject",
            "Working memory.",
            source="agent",
            trust="working",
        )
        self.cli("sync", "--run", creator)
        self.assertEqual(self.decision(initial)["outcome"], "accepted")

        first_touch_run = self.start("touch-a")
        second_touch_run = self.start("touch-b")
        first_touch = self.cli(
            "propose", "--run", first_touch_run, "--operation", "touch",
            "--key", "compatible.subject", "--source", "agent", "--trust", "working",
        ).stdout.strip()
        second_touch = self.cli(
            "propose", "--run", second_touch_run, "--operation", "touch",
            "--key", "compatible.subject", "--source", "agent", "--trust", "working",
        ).stdout.strip()
        self.cli("sync", "--run", first_touch_run)
        second_touch_sync = self.cli("sync", "--run", second_touch_run)
        self.assertEqual(self.decision(first_touch)["outcome"], "accepted")
        self.assertEqual(self.decision(second_touch)["outcome"], "noop")
        self.assertIn("already satisfied/no change 1", second_touch_sync.stdout)

        first_close_run = self.start("close-a")
        second_close_run = self.start("close-b")
        first_close = self.cli(
            "propose", "--run", first_close_run, "--operation", "close",
            "--key", "compatible.subject", "--source", "agent", "--trust", "working",
        ).stdout.strip()
        second_close = self.cli(
            "propose", "--run", second_close_run, "--operation", "close",
            "--key", "compatible.subject", "--source", "agent", "--trust", "working",
        ).stdout.strip()
        self.cli("sync", "--run", first_close_run)
        self.cli("sync", "--run", second_close_run)
        self.assertEqual(self.decision(first_close)["outcome"], "accepted")
        self.assertEqual(self.decision(second_close)["outcome"], "noop")
        self.assertEqual(list((self.root / ".bimri" / "conflicts").glob("C*.json")), [])

    def test_v503_unreceipted_stale_proposal_cannot_create_conflict(self):
        first_run = self.start("first")
        second_run = self.start("legacy-pending")
        first = self.propose(
            first_run,
            "legacy.concurrent",
            "Accepted writer.",
            source="agent",
            trust="working",
        )
        second = self.propose(
            second_run,
            "legacy.concurrent",
            "Unreceipted stale candidate.",
            source="agent",
            trust="working",
        )
        second_path = self.root / ".bimri" / "proposals" / f"{second}.json"
        data = json.loads(second_path.read_text("utf-8"))
        del data["preflight_receipt"]
        second_path.write_text(json.dumps(data, indent=2, sort_keys=True) + "\n", "utf-8")
        self.cli("sync", "--run", first_run)
        self.assertEqual(self.decision(first)["outcome"], "accepted")
        blocked = self.cli("sync", "--run", second_run, check=False)
        self.assertEqual(blocked.returncode, 2)
        self.assertIn("no validated versioned preflight receipt", blocked.stderr)
        self.assertFalse(
            (self.root / ".bimri" / "decisions" / f"{second}.json").exists()
        )
        self.assertEqual(self.state()["conflict_count"], 0)
        self.assertEqual(list((self.root / ".bimri" / "conflicts").glob("C*.json")), [])

    def test_start_is_quiet_and_review_renders_one_time_concurrent_choice(self):
        first = self.start("quiet-first")
        second_start = self.cli("start", "--actor", "quiet-second")
        second = re.search(
            r"=== BIMRI BRIEF (R\d{6})", second_start.stdout
        ).group(1)
        self.assertNotIn("HUMAN DECISION NEEDED", second_start.stdout)

        first_id = self.propose(
            first,
            "quiet.subject",
            "The working live value.",
            source="agent",
            trust="working",
        )
        candidate = self.propose(
            second,
            "quiet.subject",
            "The incompatible candidate value.",
        )
        self.cli("sync", "--run", first)
        created = self.cli("sync", "--run", second)
        conflict_id = self.decision(candidate)["conflict_id"]
        self.assertIn(f"MEMORY CONFLICT {conflict_id}", created.stdout)
        self.assertIn("Subject: quiet.subject", created.stdout)
        self.assertIn('Live value: "The working live value."', created.stdout)
        self.assertIn("Action: replace", created.stdout)
        self.assertIn(f"Choice {candidate}", created.stdout)
        self.assertIn("Choose " + candidate, created.stdout)
        self.assertIn("New conflict notices: total 1", created.stdout)
        self.assertNotIn("[K:quiet.subject]", created.stdout)

        repeated = self.cli("sync", "--run", second)
        self.assertNotIn(conflict_id, repeated.stdout)
        self.assertNotIn("MEMORY CONFLICT", repeated.stdout)
        self.assertIn("new concurrent conflicts 0", repeated.stdout)

        later_start = self.cli("start", "--actor", "quiet-third")
        self.assertNotIn("HUMAN DECISION NEEDED", later_start.stdout)
        self.assertNotIn(conflict_id, later_start.stdout)
        self.assertNotIn("quiet.subject", later_start.stdout)

        review = self.cli("review")
        self.assertIn("Actionable concurrent conflicts: 1", review.stdout)
        self.assertIn("Review records: total 1", review.stdout)
        self.assertIn(f"MEMORY CONFLICT {conflict_id}", review.stdout)
        self.assertIn(f"run {second} (quiet-second)", review.stdout)
        self.assertIn("source user | trust confirmed", review.stdout)
        self.assertIn("base V000000", review.stdout)

        closed = self.cli(
            "close",
            "--run",
            second,
            "--summary",
            "Conflict remains available through pull review.",
        )
        self.assertNotIn(conflict_id, closed.stdout)
        self.assertNotIn("human decisions needed", closed.stdout.lower())
        self.assertEqual(self.decision(first_id)["outcome"], "accepted")

    def test_review_pagination_and_conflict_enlargement_never_truncate(self):
        conflicts = []
        enlarged_candidate = None
        for index in range(7):
            live_run = self.start(f"page-live-{index}")
            candidate_run = self.start(f"page-candidate-{index}")
            extra_run = self.start("page-enlargement") if index == 0 else None
            key = f"page.subject-{index}"
            self.propose(
                live_run,
                key,
                f"Live value {index}.",
                source="agent",
                trust="working",
            )
            candidate = self.propose(
                candidate_run,
                key,
                f"Candidate value {index}.",
                source="agent",
                trust="working",
            )
            if extra_run:
                enlarged_candidate = self.propose(
                    extra_run,
                    key,
                    "A genuinely new candidate for the first conflict.",
                    source="agent",
                    trust="working",
                )
            self.cli("sync", "--run", live_run)
            created = self.cli("sync", "--run", candidate_run)
            conflict_id = self.decision(candidate)["conflict_id"]
            conflicts.append(conflict_id)
            self.assertIn(conflict_id, created.stdout)
            if extra_run:
                enlarged = self.cli("sync", "--run", extra_run)
                self.assertEqual(
                    self.decision(enlarged_candidate)["conflict_id"],
                    conflict_id,
                )
                self.assertIn(conflict_id, enlarged.stdout)
                self.assertIn(f"Choice {enlarged_candidate}", enlarged.stdout)
                replayed = self.cli("sync", "--run", extra_run)
                self.assertNotIn(conflict_id, replayed.stdout)

        first_page = self.cli("review", "--limit", 3)
        self.assertIn(
            "Review records: total 7 | displayed 1-3 | remaining 4",
            first_page.stdout,
        )
        for conflict_id in conflicts[:3]:
            self.assertIn(conflict_id, first_page.stdout)
        for conflict_id in conflicts[3:]:
            self.assertNotIn(conflict_id, first_page.stdout)
        self.assertIn(f"Choice {enlarged_candidate}", first_page.stdout)

        second_page = self.cli("review", "--offset", 3, "--limit", 3)
        self.assertIn(
            "Review records: total 7 | displayed 4-6 | remaining 1",
            second_page.stdout,
        )
        for conflict_id in conflicts[3:6]:
            self.assertIn(conflict_id, second_page.stdout)

        final_page = self.cli("review", "--offset", 6, "--limit", 3)
        self.assertIn(
            "Review records: total 7 | displayed 7-7 | remaining 0",
            final_page.stdout,
        )
        self.assertIn(conflicts[6], final_page.stdout)

        no_page = self.cli("review", "--offset", 7, "--limit", 3)
        self.assertIn(
            "Review records: total 7 | displayed 0-0 | remaining 0",
            no_page.stdout,
        )

    def test_historical_exact_effect_is_derived_without_authority_rewrite(self):
        first = self.start("historical-first")
        second = self.start("historical-second")
        self.propose(
            first,
            "history.subject",
            "The earlier working value.",
            source="agent",
            trust="working",
        )
        candidate = self.propose(
            second,
            "history.subject",
            "The exact candidate value.",
        )
        self.cli("sync", "--run", first)
        self.cli("sync", "--run", second)
        decision_before = (
            self.root / ".bimri" / "decisions" / f"{candidate}.json"
        ).read_bytes()
        conflict_id = self.decision(candidate)["conflict_id"]
        conflict_before = (
            self.root / ".bimri" / "conflicts" / f"{conflict_id}.json"
        ).read_bytes()

        later = self.start("historical-later")
        self.propose(
            later,
            "history.subject",
            "The exact candidate value.",
        )
        self.cli("sync", "--run", later)
        satisfying_revision = self.state()["head_revision"]

        status = self.cli("status")
        self.assertIn("Actionable concurrent conflicts: 0", status.stdout)
        self.assertIn("Satisfied historical candidates: 1", status.stdout)
        review = self.cli("review", "--all")
        self.assertIn("SATISFIED HISTORICAL CANDIDATE", review.stdout)
        self.assertIn(
            f"already satisfied by V{satisfying_revision:06d}",
            review.stdout,
        )
        self.assertEqual(
            (
                self.root / ".bimri" / "decisions" / f"{candidate}.json"
            ).read_bytes(),
            decision_before,
        )
        self.assertEqual(
            (
                self.root / ".bimri" / "conflicts" / f"{conflict_id}.json"
            ).read_bytes(),
            conflict_before,
        )
        self.assertFalse(
            (
                self.root / ".bimri" / "resolutions" / f"{conflict_id}.json"
            ).exists()
        )

    def test_partial_historical_satisfaction_is_exact_and_never_resurrects(self):
        first = self.start("partial-live")
        exact_run = self.start("partial-exact")
        similar_run = self.start("partial-similar")
        common = (
            "--confidence",
            "developing",
            "--observations",
            "3",
            "--falsifier",
            "A controlled replay does not reproduce the pattern.",
        )
        self.propose(
            first,
            "history.pattern",
            "The initial live pattern.",
            tier=3,
            extra=(
                "--confidence",
                "emerging",
                "--observations",
                "2",
                "--falsifier",
                "A later run disproves the initial pattern.",
            ),
        )
        exact = self.propose(
            exact_run,
            "history.pattern",
            "The same candidate prose.",
            tier=3,
            extra=common,
        )
        similar = self.propose(
            similar_run,
            "history.pattern",
            "The same candidate prose.",
            tier=3,
            extra=(
                "--confidence",
                "established",
                "--observations",
                "3",
                "--falsifier",
                "A controlled replay does not reproduce the pattern.",
            ),
        )
        self.cli("sync", "--run", first)
        created = self.cli("sync", "--run", exact_run)
        conflict_id = self.decision(exact)["conflict_id"]
        self.assertIn(conflict_id, created.stdout)
        enlarged = self.cli("sync", "--run", similar_run)
        self.assertEqual(self.decision(similar)["conflict_id"], conflict_id)
        self.assertIn(conflict_id, enlarged.stdout)
        self.assertIn(f"Choice {similar}", enlarged.stdout)
        replayed = self.cli("sync", "--run", similar_run)
        self.assertNotIn(conflict_id, replayed.stdout)

        satisfying = self.start("partial-satisfying")
        self.propose(
            satisfying,
            "history.pattern",
            "The same candidate prose.",
            tier=3,
            extra=(*common, "--evidence", f"{exact_run}-E001"),
        )
        self.cli("sync", "--run", satisfying)
        satisfying_revision = self.state()["head_revision"]

        review = self.cli("review")
        self.assertIn("Actionable concurrent conflicts: 1", review.stdout)
        self.assertIn(f"Choice {similar}", review.stdout)
        self.assertNotIn(f"Choice {exact}", review.stdout)
        all_review = self.cli("review", "--all")
        self.assertIn("Satisfied historical candidates: 1", all_review.stdout)
        self.assertIn(f"Choice {exact}", all_review.stdout)
        self.assertIn(
            f"already satisfied by V{satisfying_revision:06d}",
            all_review.stdout,
        )
        self.assertIn(f"Choice {similar}", all_review.stdout)

        moved = self.start("partial-moved")
        self.propose(
            moved,
            "history.pattern",
            "A later different pattern.",
            tier=3,
            extra=(
                "--confidence",
                "emerging",
                "--observations",
                "1",
                "--falsifier",
                "The later pattern does not recur.",
            ),
        )
        self.cli("sync", "--run", moved)
        after_movement = self.cli("review", "--all")
        self.assertIn("Satisfied historical candidates: 1", after_movement.stdout)
        self.assertIn(f"Choice {exact}", after_movement.stdout)
        self.assertIn(f"Choice {similar}", after_movement.stdout)

    def test_first_resolution_of_currently_reflected_candidate_is_revision_free(self):
        first = self.start("resolve-first")
        second = self.start("resolve-second")
        self.propose(
            first,
            "resolve.reflected",
            "Working value.",
            source="agent",
            trust="working",
        )
        candidate = self.propose(
            second,
            "resolve.reflected",
            "Chosen value.",
        )
        self.cli("sync", "--run", first)
        self.cli("sync", "--run", second)
        conflict_id = self.decision(candidate)["conflict_id"]
        later = self.start("resolve-later")
        self.propose(later, "resolve.reflected", "Chosen value.")
        self.cli("sync", "--run", later)
        revision_before = self.state()["head_revision"]

        resolved = self.cli(
            "resolve",
            conflict_id,
            "--choose",
            candidate,
            "--human-approved",
        )
        self.assertIn(f"resolved with {candidate}", resolved.stdout)
        self.assertEqual(self.state()["head_revision"], revision_before)
        resolution = json.loads(
            (
                self.root
                / ".bimri"
                / "resolutions"
                / f"{conflict_id}.json"
            ).read_text("utf-8")
        )
        self.assertEqual(resolution["revision_after"], revision_before)
        self.assertEqual(resolution["authority"], "human-asserted")
        self.assertIn("BIMRI doctor: PASSED", self.cli("doctor").stdout)

    def test_archive_marker_must_match_reason_and_exact_removed_line(self):
        run_id = self.start("archive-hardening")
        self.propose(
            run_id,
            "archive.hardening",
            "The exact line that will be archived.",
            source="agent",
            trust="working",
        )
        self.cli("sync", "--run", run_id)
        current_line = next(
            line
            for line in self.hot().splitlines()
            if "[K:archive.hardening]" in line
        )
        target = re.match(r"\[([^\]]+)\]", current_line).group(1)
        close_result = self.cli(
            "propose",
            "--run",
            run_id,
            "--operation",
            "close",
            "--key",
            "archive.hardening",
            "--target",
            target,
            "--source",
            "agent",
            "--trust",
            "working",
        )
        close_id = PROPOSAL_RE.search(close_result.stdout).group(0)
        self.cli("sync", "--run", run_id)
        archive_path = (
            self.root
            / ".bimri"
            / "archive"
            / f"{dt.date.today():%Y-%m}.md"
        )
        archive = archive_path.read_text("utf-8")
        archive_path.write_text(
            archive.replace(
                f"[BY:{close_id}] [closed] {current_line}",
                f"[BY:{close_id}] [closed] a different raw line",
            ),
            "utf-8",
        )
        proposal = json.loads(
            (
                self.root / ".bimri" / "proposals" / f"{close_id}.json"
            ).read_text("utf-8")
        )
        decision_path = (
            self.root / ".bimri" / "decisions" / f"{close_id}.json"
        )
        decision_path.write_text(
            json.dumps({
                "bimri_version": "5.0",
                "proposal_id": close_id,
                "outcome": "applying",
                "recorded_at": self.decision(close_id)["recorded_at"],
                "base_hash": proposal["base_hash"],
                "revision_before": proposal["base_revision"],
            }, indent=2, sort_keys=True) + "\n",
            "utf-8",
        )
        replay = self.cli("sync", "--run", run_id, check=False)
        self.assertEqual(replay.returncode, 2)
        self.assertIn("authority recovery is required", replay.stderr)
        self.assertIn(
            "close target is absent without exact archive provenance",
            replay.stderr,
        )

    def test_resolution_requires_attestation_and_preserves_claim_origin(self):
        for source in ("agent", "external"):
            with self.subTest(source=source):
                root = self.root / source
                candidate_run, candidate, _, original = self.stage_concurrent_candidate(
                    "authority.claim",
                    f"The {source}-origin replacement.",
                    root=root,
                    candidate_actor=source,
                    candidate_source=source,
                    writer_text="The live working value.",
                )
                self.cli("sync", "--run", candidate_run, root=root)
                contested = self.decision(candidate, root=root)
                self.assertEqual(contested["outcome"], "contested")
                conflict_id = contested["conflict_id"]
                resolution_path = (
                    root / ".bimri" / "resolutions" / f"{conflict_id}.json"
                )
                proposal_path = (
                    root / ".bimri" / "proposals" / f"{candidate}.json"
                )
                proposal_before = proposal_path.read_bytes()
                head_before = self.state(root=root)["head_revision"]
                hot_before = self.hot(root=root)
                decision_before = self.decision(candidate, root=root)

                choices = (candidate, "current", "dismiss") if source == "agent" else (candidate,)
                for choice in choices:
                    denied = self.cli(
                        "resolve",
                        conflict_id,
                        "--choose",
                        choice,
                        root=root,
                        check=False,
                    )
                    self.assertEqual(denied.returncode, 2)
                    self.assertIn("requires --human-approved", denied.stderr)
                    self.assertFalse(resolution_path.exists())
                    self.assertEqual(
                        self.state(root=root)["head_revision"], head_before
                    )
                    self.assertEqual(self.hot(root=root), hot_before)
                    self.assertEqual(
                        self.decision(candidate, root=root), decision_before
                    )

                self.cli(
                    "resolve",
                    conflict_id,
                    "--choose",
                    candidate,
                    "--human-approved",
                    root=root,
                )
                resolution = json.loads(resolution_path.read_text("utf-8"))
                self.assertEqual(resolution["bimri_version"], "5.1.0")
                self.assertEqual(resolution["authority"], "human-asserted")
                self.assertIn(
                    f"[T:confirmed] [SRC:{source}]",
                    self.hot(root=root),
                )
                self.assertEqual(proposal_path.read_bytes(), proposal_before)
                immutable = json.loads(proposal_path.read_text("utf-8"))
                self.assertEqual(immutable["trust"], "working")
                self.assertEqual(immutable["source"], source)

    def test_legacy_resolution_without_attestation_keeps_legacy_effect_semantics(self):
        candidate_run, candidate, _, original = self.stage_concurrent_candidate(
            "legacy.resolution",
            "The legacy human-approved agent proposal.",
            candidate_actor="legacy-agent",
            candidate_source="agent",
            writer_text="The live working value.",
        )
        self.cli("sync", "--run", candidate_run)
        contested = self.decision(candidate)
        conflict_id = contested["conflict_id"]
        proposal_path = (
            self.root / ".bimri" / "proposals" / f"{candidate}.json"
        )
        conflict_path = (
            self.root / ".bimri" / "conflicts" / f"{conflict_id}.json"
        )
        original_proposal = proposal_path.read_bytes()
        original_conflict = conflict_path.read_bytes()

        legacy_candidate = json.loads(original_proposal.decode("utf-8"))
        legacy_candidate["bimri_version"] = "5.0.1"
        legacy_candidate["source"] = "user"
        legacy_candidate["trust"] = "confirmed"
        legacy_proposal_bytes = (
            json.dumps(legacy_candidate, indent=2, sort_keys=True) + "\n"
        ).encode("utf-8")
        proposal_path.write_bytes(legacy_proposal_bytes)
        temporary_conflict = json.loads(original_conflict.decode("utf-8"))
        temporary_conflict["proposal_hashes"][candidate] = hashlib.sha256(
            legacy_proposal_bytes
        ).hexdigest()
        conflict_path.write_text(
            json.dumps(temporary_conflict, indent=2, sort_keys=True) + "\n",
            "utf-8",
        )
        self.cli(
            "resolve",
            conflict_id,
            "--choose",
            candidate,
            "--human-approved",
        )
        self.assertIn("[T:confirmed] [SRC:user]", self.hot())

        proposal_path.write_bytes(original_proposal)
        conflict_path.write_bytes(original_conflict)
        resolution_path = (
            self.root / ".bimri" / "resolutions" / f"{conflict_id}.json"
        )
        legacy_resolution = json.loads(resolution_path.read_text("utf-8"))
        legacy_resolution["bimri_version"] = "5.0.1"
        legacy_resolution.pop("authority", None)
        resolution_path.write_text(
            json.dumps(legacy_resolution, indent=2, sort_keys=True) + "\n",
            "utf-8",
        )

        self.assertIn("Open conflicts: 0", self.cli("status").stdout)
        self.assertIn("BIMRI doctor: PASSED", self.cli("doctor").stdout)
        replay = self.cli(
            "resolve", conflict_id, "--choose", candidate
        )
        self.assertIn("already resolved", replay.stdout)

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

        before = {
            path.relative_to(self.root).as_posix(): path.read_bytes()
            for path in self.root.rglob("*")
            if path.is_file() and not path.is_symlink()
        }
        late = self.cli(
            "propose", "--run", earlier_run, "--tier", "2",
            "--key", "roadmap.priority",
            "--text", "Ship the reporting feature first.",
            "--source", "user", "--trust", "confirmed", check=False,
        )
        self.assertEqual(late.returncode, 2)
        self.assertIn(f"sync {earlier_run}", late.stderr)
        after = {
            path.relative_to(self.root).as_posix(): path.read_bytes()
            for path in self.root.rglob("*")
            if path.is_file() and not path.is_symlink()
        }
        self.assertEqual(after, before)
        self.assertEqual(self.state()["conflict_count"], 0)
        self.assertEqual(list((self.root / ".bimri" / "conflicts").glob("C*.json")), [])
        self.assertIn("Ship the search feature first.", self.hot())
        self.assertNotIn("Ship the reporting feature first.", self.hot())

    def test_tampered_proposal_base_hash_cannot_bypass_stale_detection(self):
        stale_run = self.start("stale-agent")
        committer = self.start("committer")
        candidate = self.propose(
            stale_run, "optimistic.lock",
            "A tampered stale overwrite must not land.",
            source="agent", trust="working",
        )
        committed = self.propose(
            committer, "optimistic.lock",
            "The value committed after the stale run began.",
            source="agent", trust="working",
        )
        self.cli("sync", "--run", committer)
        self.assertEqual(self.decision(committed)["outcome"], "accepted")
        proposal_path = (
            self.root / ".bimri" / "proposals" / f"{candidate}.json"
        )
        proposal = json.loads(proposal_path.read_text("utf-8"))
        current_line = next(
            line
            for line in self.hot().splitlines()
            if "[K:optimistic.lock]" in line
        )
        proposal["base_hash"] = hashlib.sha256(
            current_line.encode("utf-8")
        ).hexdigest()
        proposal_path.write_text(
            json.dumps(proposal, indent=2, sort_keys=True) + "\n",
            "utf-8",
        )
        head_before = self.state()["head_revision"]
        hot_before = self.hot()

        status = self.cli("status", check=False)
        self.assertEqual(status.returncode, 1)
        self.assertIn(
            "proposal preflight key hash does not match proposal base hash",
            status.stdout,
        )
        blocked = self.cli("sync", "--run", stale_run, check=False)
        self.assertEqual(blocked.returncode, 2)
        self.assertIn("shared-memory writes are paused", blocked.stderr)
        self.assertEqual(self.state()["head_revision"], head_before)
        self.assertEqual(self.hot(), hot_before)
        self.assertFalse(
            (
                self.root / ".bimri" / "decisions" / f"{candidate}.json"
            ).exists()
        )

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
        self.assertIn(
            "applied 1, held candidates 0, already satisfied/no change 0",
            recovered.stdout,
        )
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
            source="agent",
            trust="working",
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
        silently_finalized = self.decision(crashed_proposal)
        self.assertEqual(silently_finalized["outcome"], "accepted")
        self.assertEqual(silently_finalized["revision"], landed_revision)
        later_proposal = self.propose(
            later_run,
            "crash.same-key",
            "Value B is the later committed update.",
            source="agent",
            trust="working",
        )
        self.cli("sync", "--run", later_run)
        self.assertEqual(self.decision(later_proposal)["outcome"], "accepted")
        self.assertEqual(self.state()["head_revision"], 2)
        self.assertIn("Value B is the later committed update.", self.hot())

        retry = self.cli("sync", "--run", crashed_run, check=False)
        self.assertEqual(retry.returncode, 0)
        self.assertIn(
            "applied 0, held candidates 0, already satisfied/no change 0",
            retry.stdout,
        )
        recovered = self.decision(crashed_proposal)
        self.assertEqual(recovered["outcome"], "accepted")
        self.assertEqual(recovered["revision"], landed_revision)
        self.assertEqual(self.state()["head_revision"], 2)
        self.assertIn("Value B is the later committed update.", self.hot())
        self.assertNotIn("Value A landed before the child died.", self.hot())
        same_key_conflicts = [
            json.loads(path.read_text("utf-8"))
            for path in (self.root / ".bimri" / "conflicts").glob("C*.json")
            if '"key": "crash.same-key"' in path.read_text("utf-8")
        ]
        self.assertEqual(same_key_conflicts, [])

    def test_resolution_and_decision_finalization_are_crash_idempotent(self):
        first_candidate_run = self.start("candidate-one")
        second_candidate_run = self.start("candidate-two")
        committer_run = self.start("committer")
        committed = self.propose(
            committer_run,
            "shared.choice",
            "Keep the committed value.",
            source="agent",
            trust="working",
        )
        first_candidate = self.propose(
            first_candidate_run,
            "shared.choice",
            "Choose candidate one.",
            source="agent",
            trust="working",
        )
        second_candidate = self.propose(
            second_candidate_run,
            "shared.choice",
            "Choose candidate two.",
            source="agent",
            trust="working",
        )
        self.cli("sync", "--run", committer_run)
        self.assertEqual(self.decision(committed)["outcome"], "accepted")
        self.cli("sync", "--run", first_candidate_run)
        self.cli("sync", "--run", second_candidate_run)
        first_decision = self.decision(first_candidate)
        second_decision = self.decision(second_candidate)
        self.assertEqual(first_decision["outcome"], "contested")
        self.assertEqual(second_decision["outcome"], "contested")
        conflict_id = first_decision["conflict_id"]
        self.assertEqual(second_decision["conflict_id"], conflict_id)

        self.cli(
            "resolve", conflict_id, "--choose", first_candidate,
            "--human-approved",
        )
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
            source="agent",
            trust="working",
        )
        crash_candidate = self.propose(
            stale_run,
            "crash.choice",
            "The chosen value already committed before the crash.",
            source="agent",
            trust="working",
        )
        self.cli("sync", "--run", crash_committer)
        self.assertEqual(self.decision(crash_committed)["outcome"], "accepted")
        self.cli("sync", "--run", stale_run)
        crash_decision = self.decision(crash_candidate)
        self.assertEqual(crash_decision["outcome"], "contested")
        crash_conflict = crash_decision["conflict_id"]
        self.cli(
            "resolve", crash_conflict, "--choose", crash_candidate,
            "--human-approved",
        )
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
        # A real crash before decision finalization retains the original
        # contested snapshot revision, not the later resolved revision.
        interrupted_decision["revision"] = applying["revision_before"]
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
            "--human-approved",
        )
        self.assertRegex(
            recovered.stdout,
            f"(already resolved as|resolved with) {crash_candidate}",
        )
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
                writer_run = self.start("writer", root=root)
                first_id = self.propose(
                    first_run,
                    "resolve.preflight",
                    "First candidate.",
                    source="agent",
                    trust="working",
                    root=root,
                )
                second_id = self.propose(
                    second_run,
                    "resolve.preflight",
                    "Second candidate.",
                    source="agent",
                    trust="working",
                    root=root,
                )
                writer_id = self.propose(
                    writer_run,
                    "resolve.preflight",
                    "Accepted competing value.",
                    source="agent",
                    trust="working",
                    root=root,
                )
                self.cli("sync", "--run", writer_run, root=root)
                self.assertEqual(
                    self.decision(writer_id, root=root)["outcome"], "accepted"
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
                    "--human-approved",
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
        run_id, proposal_id, _, _ = self.stage_concurrent_candidate(
            "conflict.recovery",
            "A concurrent conflict survives a missing decision write.",
            candidate_actor="conflict-recovery",
            writer_text="The competing accepted value.",
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

        recovered = self.cli("sync", "--run", run_id, check=False)
        self.assertEqual(recovered.returncode, 2)
        self.assertIn("shared-memory writes are paused", recovered.stderr)
        self.assertNotIn("MEMORY CONFLICT", recovered.stdout)
        self.assertFalse(decision_path.exists())
        conflict_path = (
            self.root / ".bimri" / "conflicts" / f"{original['conflict_id']}.json"
        )
        self.assertTrue(conflict_path.is_file())
        status = self.cli("status", check=False)
        self.assertIn("AUTHORITY RECOVERY NEEDED", status.stdout)

    def test_stale_close_resolution_archives_removed_current_value_after_retry(self):
        creator = self.start("creator")
        created = self.propose(
            creator,
            "stale.close",
            "Value A from the stale run base.",
            source="agent",
            trust="working",
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
            source="agent",
            trust="working",
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
        review = self.cli("review", conflict_id)
        self.assertIn("Concurrent removal", review.stdout)
        self.assertIn("Action: remove/archive", review.stdout)
        self.assertIn("absent from current memory", review.stdout)
        self.assertIn("preserve the exact prior line", review.stdout)
        self.assertNotIn("promote", review.stdout.lower())

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
            "--human-approved",
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
            "--human-approved",
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
            source="agent",
            trust="working",
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
            source="agent",
            trust="working",
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
            source="agent",
            trust="working",
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
        # Finalizing this run's own interrupted apply counts as applied; the
        # asserts below prove no second effect landed.
        self.assertIn(
            "applied 1, held candidates 0, already satisfied/no change 0",
            close_replay.stdout,
        )
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
            source="agent",
            trust="working",
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
        self.assertIn(
            "applied 1, held candidates 0, already satisfied/no change 0",
            touch_replay.stdout,
        )
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
            "--new-subject",
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
            "--new-subject",
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

        human_core = self.cli(
            "propose", "--run", run_id, "--tier", "1",
            "--new-subject", "--key", "owner.preference",
            "--text", "The owner prefers concise status reports.",
            "--source", "user", "--trust", "confirmed", "--kind", "pref",
        )
        human_core_id = PROPOSAL_RE.search(human_core.stdout).group(0)
        self.cli("sync", "--run", run_id)
        self.assertEqual(self.decision(human_core_id)["outcome"], "accepted")
        self.assertIn("[K:owner.preference]", self.hot())
        self.assertIn("[T:confirmed] [SRC:user]", self.hot())

        second = self.start("agent")
        agent_core = self.cli(
            "propose", "--run", second, "--tier", "1",
            "--new-subject", "--key", "architecture.assumption",
            "--text", "The service should use a queue.",
            "--source", "agent", "--trust", "working", "--kind", "decision",
        )
        agent_core_id = PROPOSAL_RE.search(agent_core.stdout).group(0)
        self.cli("sync", "--run", second)
        proposal = json.loads((
            self.root / ".bimri" / "proposals" / f"{agent_core_id}.json"
        ).read_text("utf-8"))
        self.assertEqual(
            proposal["hold_reason"], "tier1-human-authority-required"
        )
        self.assertEqual(self.decision(agent_core_id)["outcome"], "held")
        self.assertNotIn("[K:architecture.assumption]", self.hot())
        self.assertEqual(self.state()["conflict_count"], 0)
        self.assertEqual(list((self.root / ".bimri" / "conflicts").glob("C*.json")), [])

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
        changed = self.cli(
            "propose", "--run", agent_run, "--tier", "2",
            "--key", "launch.date", "--text", "Launch on Tuesday.",
            "--source", "agent", "--trust", "working",
        )
        changed_id = PROPOSAL_RE.search(changed.stdout).group(0)
        proposal = json.loads((
            self.root / ".bimri" / "proposals" / f"{changed_id}.json"
        ).read_text("utf-8"))
        self.assertEqual(
            proposal["hold_reason"], "confirmed-user-authority-required"
        )
        self.cli("sync", "--run", agent_run)
        decision = self.decision(changed_id)
        self.assertEqual(decision["outcome"], "held")
        self.assertEqual(
            decision["reason"], "confirmed-user-authority-required"
        )
        self.assertIn("Launch on Monday.", self.hot())
        self.assertNotIn("Launch on Tuesday.", self.hot())
        recall = self.cli("recall", "--key", "launch.date", "--history")
        self.assertIn("Launch on Monday.", recall.stdout)
        self.assertIn("HELD", recall.stdout)
        self.assertIn("Launch on Tuesday.", recall.stdout)
        self.assertEqual(self.state()["conflict_count"], 0)
        self.assertEqual(list((self.root / ".bimri" / "conflicts").glob("C*.json")), [])

    def test_soft_tier_targets_and_byte_pressure_do_not_silently_truncate(self):
        self.cli("migrate")
        state_path = self.root / ".bimri" / "state.json"
        state = self.state()
        state["entry_max_chars"] = 50
        state["tier2_max"] = 1
        state_path.write_text(json.dumps(state, indent=2, sort_keys=True) + "\n", "utf-8")

        first = self.start("codex")
        oversize = self.cli(
            "propose",
            "--run",
            first,
            "--tier",
            "2",
            "--new-subject",
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
        second_proposal = self.cli(
            "propose", "--run", second, "--tier", "2",
            "--new-subject", "--key", "second.item",
            "--text", "Second compact item.", "--source", "user",
            "--trust", "confirmed",
        )
        second_id = PROPOSAL_RE.search(second_proposal.stdout).group(0)
        self.cli("sync", "--run", second)
        self.assertEqual(self.decision(second_id)["outcome"], "accepted")
        self.assertEqual(self.hot().count("[K:first.item]"), 1)
        self.assertEqual(self.hot().count("[K:second.item]"), 1)

        state = self.state()
        state["tier2_max"] = 20
        state["hot_max_bytes"] = len(self.hot().encode("utf-8")) + 10
        state_path.write_text(json.dumps(state, indent=2, sort_keys=True) + "\n", "utf-8")
        third = self.start("other")
        byte_cap = self.cli(
            "propose", "--run", third, "--tier", "2",
            "--new-subject", "--key", "byte.cap",
            "--text", "This cannot fit.", "--source", "user",
            "--trust", "confirmed",
        )
        byte_id = PROPOSAL_RE.search(byte_cap.stdout).group(0)
        self.cli("sync", "--run", third)
        self.assertEqual(self.decision(byte_id)["outcome"], "accepted")
        state = self.state()
        self.assertTrue(state["cold_current"])
        self.assertLessEqual(
            len(self.hot().encode("utf-8")), state["hot_max_bytes"]
        )
        self.assertEqual(
            self.hot().count("[K:byte.cap]")
            + int("byte.cap" in state["cold_current"]),
            1,
        )
        self.assertEqual(self.state()["conflict_count"], 0)

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

    def test_corrupt_authority_degrades_reads_blocks_writes_and_can_be_restored(self):
        run_id, candidate, _, _ = self.stage_concurrent_candidate(
            "corruption.choice",
            "This agent-origin claim needs owner review.",
            candidate_actor="corruption-fixture",
            writer_text="The competing live authority value.",
        )
        self.cli("sync", "--run", run_id)
        contested = self.decision(candidate)
        conflict_id = contested["conflict_id"]
        conflict_path = (
            self.root / ".bimri" / "conflicts" / f"{conflict_id}.json"
        )
        valid_conflict = conflict_path.read_bytes()
        corrupt_bytes = b"{broken authority json"
        conflict_path.write_bytes(corrupt_bytes)
        canonical_hot = self.hot().encode("utf-8")
        direct_edit = b"forged direct hot memory while authority is corrupt\n"
        (self.root / "bimri.md").write_bytes(direct_edit)
        blocked_recovery = self.cli(
            "sync", "--run", run_id, check=False
        )
        self.assertEqual(blocked_recovery.returncode, 2)
        self.assertIn("direct edit to bimri.md was preserved", blocked_recovery.stderr)
        self.assertEqual((self.root / "bimri.md").read_bytes(), canonical_hot)
        state_before_start = self.state()

        degraded = self.cli("start", "--actor", "degraded-agent")
        self.assertEqual(degraded.returncode, 0)
        self.assertIn("AUTHORITY RECOVERY NEEDED", degraded.stdout)
        self.assertIn(
            f".bimri/conflicts/{conflict_id}.json", degraded.stdout
        )
        degraded_match = re.search(
            r"=== BIMRI BRIEF (R\d{6})", degraded.stdout
        )
        self.assertIsNotNone(degraded_match, degraded.stdout)
        degraded_run = degraded_match.group(1)
        state_after_start = self.state()
        self.assertEqual(
            state_after_start["run_count"],
            state_before_start["run_count"] + 1,
        )
        self.assertIn(degraded_run, state_after_start["active_runs"])
        self.assertEqual((self.root / "bimri.md").read_bytes(), canonical_hot)
        manual_recovery = list(
            (self.root / ".bimri" / "recovery").glob("manual-hot-*")
        )
        self.assertEqual(len(manual_recovery), 1)
        self.assertEqual(manual_recovery[0].read_bytes(), direct_edit)

        status = self.cli("status", check=False)
        self.assertEqual(status.returncode, 1)
        self.assertIn("AUTHORITY RECOVERY NEEDED", status.stdout)
        doctor = self.cli("doctor", check=False)
        self.assertEqual(doctor.returncode, 1)
        self.assertIn(f"{conflict_id}.json", doctor.stdout)

        head_before_sync = self.state()["head_revision"]
        staged = self.cli(
            "propose", "--run", degraded_run, "--tier", "2",
            "--key", "corruption.unrelated",
            "--text", "This unrelated proposal remains durably staged.",
            "--source", "user", "--trust", "confirmed", check=False,
        )
        self.assertEqual(staged.returncode, 0, staged.stdout + staged.stderr)
        staged_id = PROPOSAL_RE.search(staged.stdout).group(0)
        staged_path = (
            self.root / ".bimri" / "proposals" / f"{staged_id}.json"
        )
        staged_record = json.loads(staged_path.read_text("utf-8"))
        self.assertEqual(staged_record["hold_reason"], "classification-required")
        self.assertFalse(
            (
                self.root / ".bimri" / "decisions" / f"{staged_id}.json"
            ).exists()
        )
        self.assertEqual(self.state()["head_revision"], head_before_sync)
        blocked_sync = self.cli(
            "sync", "--run", degraded_run, check=False
        )
        self.assertEqual(blocked_sync.returncode, 2)
        self.assertIn("shared-memory writes are paused", blocked_sync.stderr)
        self.assertEqual(staged_path.read_text("utf-8"), json.dumps(
            staged_record, indent=2, sort_keys=True
        ) + "\n")

        denied = self.cli(
            "quarantine-authority",
            "--kind",
            "conflict",
            "--id",
            conflict_id,
            check=False,
        )
        self.assertEqual(denied.returncode, 2)
        self.assertEqual(conflict_path.read_bytes(), corrupt_bytes)

        quarantined = self.cli(
            "quarantine-authority",
            "--kind",
            "conflict",
            "--id",
            conflict_id,
            "--human-approved",
        )
        self.assertIn("exact bytes preserved", quarantined.stdout)
        stub_bytes = conflict_path.read_bytes()
        stub = json.loads(stub_bytes.decode("utf-8"))
        self.assertEqual(stub["record_type"], "authority-quarantine")
        recovery = self.root / stub["recovery_file"]
        self.assertEqual(recovery.read_bytes(), corrupt_bytes)
        self.assertEqual(
            stub["sha256"], hashlib.sha256(corrupt_bytes).hexdigest()
        )
        repeated_quarantine = self.cli(
            "quarantine-authority",
            "--kind",
            "conflict",
            "--id",
            conflict_id,
            "--human-approved",
        )
        self.assertIn("already quarantined", repeated_quarantine.stdout)
        self.assertEqual(conflict_path.read_bytes(), stub_bytes)
        still_blocked = self.cli(
            "sync", "--run", degraded_run, check=False
        )
        self.assertEqual(still_blocked.returncode, 2)

        replacement = self.root / "reviewed-conflict.json"
        replacement.write_bytes(valid_conflict)
        denied_restore = self.cli(
            "restore-authority",
            "--kind",
            "conflict",
            "--id",
            conflict_id,
            "--from",
            replacement,
            check=False,
        )
        self.assertEqual(denied_restore.returncode, 2)
        self.assertEqual(conflict_path.read_bytes(), stub_bytes)

        restored = self.cli(
            "restore-authority",
            "--kind",
            "conflict",
            "--id",
            conflict_id,
            "--from",
            replacement,
            "--human-approved",
        )
        self.assertIn("restored validated", restored.stdout)
        self.assertEqual(conflict_path.read_bytes(), valid_conflict)
        receipts = list(
            (self.root / ".bimri" / "recovery").glob(
                f"authority-restore-conflict-{conflict_id}-*.json"
            )
        )
        self.assertEqual(len(receipts), 1)
        receipt_bytes = receipts[0].read_bytes()
        repeated_restore = self.cli(
            "restore-authority",
            "--kind",
            "conflict",
            "--id",
            conflict_id,
            "--from",
            replacement,
            "--human-approved",
        )
        self.assertIn("already restored", repeated_restore.stdout)
        self.assertEqual(receipts[0].read_bytes(), receipt_bytes)
        self.assertEqual(recovery.read_bytes(), corrupt_bytes)

        healthy = self.cli("status")
        self.assertIn("Open conflicts: 2", healthy.stdout)
        manual_conflicts = [
            json.loads(path.read_text("utf-8"))
            for path in (self.root / ".bimri" / "conflicts").glob("C*.json")
            if path != conflict_path
            and json.loads(path.read_text("utf-8")).get("type") == "manual-edit"
        ]
        self.assertEqual(len(manual_conflicts), 1)
        self.cli(
            "resolve",
            manual_conflicts[0]["conflict_id"],
            "--choose",
            "current",
            "--human-approved",
        )
        released = self.cli("sync", "--run", degraded_run)
        self.assertIn("held candidates 1", released.stdout)
        held = self.decision(staged_id)
        self.assertEqual(held["outcome"], "held")
        self.assertEqual(held["reason"], "classification-required")
        self.assertNotIn("[K:corruption.unrelated]", self.hot())

        classified = self.propose(
            degraded_run,
            "corruption.unrelated",
            "This unrelated proposal is staged after authority recovery.",
        )
        self.cli("sync", "--run", degraded_run)
        self.assertEqual(self.decision(classified)["outcome"], "accepted")
        self.assertIn("[K:corruption.unrelated]", self.hot())

    def test_semantic_or_orphan_authority_corruption_is_recoverable(self):
        semantic_root = self.root / "semantic-decision"
        run_id = self.start("semantic", root=semantic_root)
        proposal_id = self.propose(
            run_id,
            "semantic.corruption",
            "A valid proposal with a forged terminal decision.",
            root=semantic_root,
        )
        self.cli("sync", "--run", run_id, root=semantic_root)
        decision_path = (
            semantic_root
            / ".bimri"
            / "decisions"
            / f"{proposal_id}.json"
        )
        forged = json.loads(decision_path.read_text("utf-8"))
        forged["revision"] = 999999
        forged_bytes = (
            json.dumps(forged, indent=2, sort_keys=True) + "\n"
        ).encode("utf-8")
        decision_path.write_bytes(forged_bytes)
        status = self.cli("status", root=semantic_root, check=False)
        self.assertEqual(status.returncode, 1)
        self.assertIn("beyond the current canonical head", status.stdout)
        quarantined = self.cli(
            "quarantine-authority",
            "--kind",
            "decision",
            "--id",
            proposal_id,
            "--human-approved",
            root=semantic_root,
        )
        self.assertIn("exact bytes preserved", quarantined.stdout)
        stub = json.loads(decision_path.read_text("utf-8"))
        self.assertEqual(
            (semantic_root / stub["recovery_file"]).read_bytes(), forged_bytes
        )

        orphan_root = self.root / "orphan-resolution"
        self.cli("migrate", root=orphan_root)
        orphan_id = "C000123"
        orphan_path = (
            orphan_root / ".bimri" / "resolutions" / f"{orphan_id}.json"
        )
        orphan = {
            "authority": "human-asserted",
            "bimri_version": "5.0.2",
            "by": "user",
            "choice": "current",
            "conflict_id": orphan_id,
            "proposal_ids": [],
            "revision_before": 0,
            "started_at": "2026-08-02T00:00:00Z",
            "status": "applying",
        }
        orphan_bytes = (
            json.dumps(orphan, indent=2, sort_keys=True) + "\n"
        ).encode("utf-8")
        orphan_path.write_bytes(orphan_bytes)
        orphan_status = self.cli("status", root=orphan_root, check=False)
        self.assertEqual(orphan_status.returncode, 1)
        self.assertIn(f"resolution {orphan_id}", orphan_status.stdout)
        # Start is a warm entry point: at-rest damage behind a valid
        # checkpoint surfaces at the explicit full-audit boundaries (status
        # above, doctor, authority writes), not on the warm path.
        warm_start = self.cli("start", "--actor", "recovery", root=orphan_root)
        self.assertNotIn("AUTHORITY RECOVERY NEEDED", warm_start.stdout)
        boundary = self.cli(
            "doctor", "--read-only", root=orphan_root, check=False
        )
        self.assertEqual(boundary.returncode, 1)
        self.assertIn(
            f"resolution {orphan_id}",
            boundary.stdout + boundary.stderr,
        )
        self.cli(
            "quarantine-authority",
            "--kind",
            "resolution",
            "--id",
            orphan_id,
            "--human-approved",
            root=orphan_root,
        )
        orphan_stub = json.loads(orphan_path.read_text("utf-8"))
        self.assertEqual(
            (orphan_root / orphan_stub["recovery_file"]).read_bytes(),
            orphan_bytes,
        )

    def test_invalid_stub_and_multiple_quarantines_have_a_recovery_path(self):
        invalid_root = self.root / "invalid-stub"
        run_id = self.start("invalid-stub", root=invalid_root)
        proposal_id = self.propose(
            run_id,
            "invalid.stub",
            "The invalid stub bytes must remain recoverable.",
            root=invalid_root,
        )
        proposal_path = (
            invalid_root
            / ".bimri"
            / "proposals"
            / f"{proposal_id}.json"
        )
        invalid_bytes = b'{"record_type":"authority-quarantine","status":"quarantined"}\n'
        proposal_path.write_bytes(invalid_bytes)
        self.cli(
            "quarantine-authority",
            "--kind",
            "proposal",
            "--id",
            proposal_id,
            "--human-approved",
            root=invalid_root,
        )
        valid_stub = json.loads(proposal_path.read_text("utf-8"))
        self.assertEqual(valid_stub["record_type"], "authority-quarantine")
        self.assertEqual(
            (invalid_root / valid_stub["recovery_file"]).read_bytes(),
            invalid_bytes,
        )

        graph_root = self.root / "multi-quarantine"
        graph_run, candidate, _, _ = self.stage_concurrent_candidate(
            "multi.graph",
            "This candidate creates linked conflict authority.",
            root=graph_root,
            candidate_actor="multi",
            writer_text="The competing live graph value.",
        )
        self.cli("sync", "--run", graph_run, root=graph_root)
        decision = self.decision(candidate, root=graph_root)
        conflict_id = decision["conflict_id"]
        conflict_path = (
            graph_root / ".bimri" / "conflicts" / f"{conflict_id}.json"
        )
        decision_path = (
            graph_root / ".bimri" / "decisions" / f"{candidate}.json"
        )
        valid_conflict = conflict_path.read_bytes()
        valid_decision = decision_path.read_bytes()
        conflict_path.write_bytes(b"{broken conflict")
        decision_path.write_bytes(b"{broken decision")
        for kind, record_id in (
            ("conflict", conflict_id),
            ("decision", candidate),
        ):
            self.cli(
                "quarantine-authority",
                "--kind",
                kind,
                "--id",
                record_id,
                "--human-approved",
                root=graph_root,
            )
        conflict_repair = graph_root / "conflict-repair.json"
        decision_repair = graph_root / "decision-repair.json"
        conflict_repair.write_bytes(valid_conflict)
        decision_repair.write_bytes(valid_decision)

        # A replacement may be well-formed JSON yet still contradict the
        # immutable authority graph.  Preflight must reject it before either
        # the quarantine stub or an authorization receipt is mutated.
        forged_conflict = json.loads(valid_conflict.decode("utf-8"))
        forged_conflict["proposal_hashes"][candidate] = "0" * 64
        forged_repair = graph_root / "forged-conflict-repair.json"
        forged_repair.write_text(
            json.dumps(forged_conflict, indent=2, sort_keys=True) + "\n",
            "utf-8",
        )
        conflict_stub_before = conflict_path.read_bytes()
        receipts_before = {
            path.name: path.read_bytes()
            for path in (graph_root / ".bimri" / "recovery").glob(
                f"authority-restore-conflict-{conflict_id}-*.json"
            )
        }
        rejected = self.cli(
            "restore-authority",
            "--kind",
            "conflict",
            "--id",
            conflict_id,
            "--from",
            forged_repair,
            "--human-approved",
            root=graph_root,
            check=False,
        )
        self.assertEqual(rejected.returncode, 2)
        self.assertIn(
            "replacement fails authority-graph validation", rejected.stderr
        )
        self.assertEqual(conflict_path.read_bytes(), conflict_stub_before)
        self.assertEqual(
            {
                path.name: path.read_bytes()
                for path in (graph_root / ".bimri" / "recovery").glob(
                    f"authority-restore-conflict-{conflict_id}-*.json"
                )
            },
            receipts_before,
        )

        staged = self.cli(
            "restore-authority",
            "--kind",
            "conflict",
            "--id",
            conflict_id,
            "--from",
            conflict_repair,
            "--human-approved",
            root=graph_root,
        )
        self.assertIn("restored staged", staged.stdout)
        restored = self.cli(
            "restore-authority",
            "--kind",
            "decision",
            "--id",
            candidate,
            "--from",
            decision_repair,
            "--human-approved",
            root=graph_root,
        )
        self.assertIn("restored validated", restored.stdout)
        self.assertIn(
            "Open conflicts: 1", self.cli("status", root=graph_root).stdout
        )

    def test_authority_symlink_quarantine_preserves_link_evidence_and_external_target(self):
        run_id, candidate, _, _ = self.stage_concurrent_candidate(
            "symlink.quarantine",
            "This candidate creates authority that will be redirected.",
            candidate_actor="symlink-quarantine",
            writer_text="The competing live symlink value.",
        )
        self.cli("sync", "--run", run_id)
        conflict_id = self.decision(candidate)["conflict_id"]
        conflict_path = (
            self.root / ".bimri" / "conflicts" / f"{conflict_id}.json"
        )
        valid_conflict = conflict_path.read_bytes()
        external_target = (
            self.root.parent
            / f"{self.root.name}-external-authority-target.json"
        )
        self.addCleanup(
            lambda: external_target.exists() and external_target.unlink()
        )
        external_bytes = b"external authority target must remain untouched\n"
        external_target.write_bytes(external_bytes)
        conflict_path.unlink()
        try:
            conflict_path.symlink_to(external_target)
        except (NotImplementedError, OSError) as exc:
            self.skipTest(f"symbolic links unavailable: {exc}")
        reviewed_target = os.readlink(conflict_path)

        unhealthy = self.cli("status", check=False)
        self.assertEqual(unhealthy.returncode, 1)
        self.assertIn("cannot be a symbolic link", unhealthy.stdout)
        quarantined = self.cli(
            "quarantine-authority",
            "--kind",
            "conflict",
            "--id",
            conflict_id,
            "--human-approved",
        )
        self.assertIn("exact link metadata preserved", quarantined.stdout)
        self.assertFalse(conflict_path.is_symlink())
        self.assertEqual(external_target.read_bytes(), external_bytes)

        stub = json.loads(conflict_path.read_text("utf-8"))
        self.assertEqual(stub["original_type"], "symbolic-link")
        recovery = self.root / stub["recovery_file"]
        recovery_bytes = recovery.read_bytes()
        self.assertEqual(
            hashlib.sha256(recovery_bytes).hexdigest(), stub["sha256"]
        )
        evidence = json.loads(recovery_bytes.decode("utf-8"))
        self.assertEqual(evidence, {
            "evidence_type": "symbolic-link",
            "link_target": reviewed_target,
            "link_target_bytes_hex": os.fsencode(reviewed_target).hex(),
            "original_path": f".bimri/conflicts/{conflict_id}.json",
        })

        replacement = self.root / "reviewed-symlink-conflict.json"
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
        )
        self.assertIn("restored validated", restored.stdout)
        self.assertEqual(conflict_path.read_bytes(), valid_conflict)
        self.assertEqual(external_target.read_bytes(), external_bytes)
        self.assertEqual(self.cli("doctor").returncode, 0)

    def test_noncanonical_authority_json_is_warning_not_governance(self):
        run_id = self.start("noncanonical-json")
        junk = self.root / ".bimri" / "conflicts" / "Cjunk.json"
        junk_bytes = b"{not canonical authority json"
        junk.write_bytes(junk_bytes)
        before = self.state()["head_revision"]

        proposal_id = self.propose(
            run_id,
            "noncanonical.write",
            "A noncanonical filename must not block this accepted write.",
        )
        self.cli("sync", "--run", run_id)
        self.assertEqual(self.decision(proposal_id)["outcome"], "accepted")
        self.assertEqual(self.state()["head_revision"], before + 1)
        self.assertIn("[K:noncanonical.write]", self.hot())
        self.assertEqual(junk.read_bytes(), junk_bytes)

        status = self.cli("status")
        self.assertNotIn("AUTHORITY RECOVERY NEEDED", status.stdout)
        doctor = self.cli("doctor")
        self.assertIn("BIMRI doctor: PASSED", doctor.stdout)
        self.assertIn(
            "ignored non-authority JSON filename in conflict directory: "
            "Cjunk.json.",
            doctor.stdout,
        )

    def test_damaged_restore_recovery_evidence_fails_doctor_without_blocking_writes(self):
        for damage in ("deleted", "tampered"):
            with self.subTest(damage=damage):
                root = self.root / damage
                run_id, candidate, _, _ = self.stage_concurrent_candidate(
                    f"restore.{damage}",
                    "This candidate creates authority for a restore receipt.",
                    root=root,
                    candidate_actor=f"restore-{damage}",
                    writer_text=f"The competing live {damage} value.",
                )
                self.cli("sync", "--run", run_id, root=root)
                conflict_id = self.decision(
                    candidate, root=root
                )["conflict_id"]
                conflict_path = (
                    root
                    / ".bimri"
                    / "conflicts"
                    / f"{conflict_id}.json"
                )
                valid_conflict = conflict_path.read_bytes()
                conflict_path.write_bytes(b"{damaged conflict authority")
                self.cli(
                    "quarantine-authority",
                    "--kind",
                    "conflict",
                    "--id",
                    conflict_id,
                    "--human-approved",
                    root=root,
                )
                stub = json.loads(conflict_path.read_text("utf-8"))
                recovery = root / stub["recovery_file"]
                replacement = root / "reviewed-conflict.json"
                replacement.write_bytes(valid_conflict)
                self.cli(
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
                self.assertEqual(conflict_path.read_bytes(), valid_conflict)

                if damage == "deleted":
                    recovery.unlink()
                    expected = "authority restore receipt recovery copy is missing"
                else:
                    recovery.write_bytes(b"tampered recovery evidence")
                    expected = (
                        "authority restore receipt recovery hash does not match"
                    )

                doctor = self.cli("doctor", root=root, check=False)
                self.assertEqual(doctor.returncode, 1)
                self.assertIn("recovery evidence is damaged", doctor.stdout)
                self.assertIn(expected, doctor.stdout)
                status = self.cli("status", root=root)
                self.assertNotIn("AUTHORITY RECOVERY NEEDED", status.stdout)

                writer = self.start(f"writer-{damage}", root=root)
                accepted = self.propose(
                    writer,
                    f"restore.{damage}.write",
                    "Current authority remains usable despite damaged evidence.",
                    root=root,
                )
                head_before = self.state(root=root)["head_revision"]
                self.cli("sync", "--run", writer, root=root)
                self.assertEqual(
                    self.decision(accepted, root=root)["outcome"], "accepted"
                )
                self.assertEqual(
                    self.state(root=root)["head_revision"], head_before + 1
                )
                self.assertIn(
                    f"[K:restore.{damage}.write]", self.hot(root=root)
                )
                self.assertEqual(
                    self.cli("doctor", root=root, check=False).returncode, 1
                )

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
        status = self.cli("status", root=decision_root, check=False)
        self.assertEqual(status.returncode, 1)
        self.assertIn("AUTHORITY RECOVERY NEEDED", status.stdout)
        self.assertEqual(self.state(root=decision_root)["head_revision"], 0)

        resolution_root = self.root / "forged-resolution"
        resolution_run, contested_id, _, _ = self.stage_concurrent_candidate(
            "forged.resolution",
            "A concurrent candidate needs resolution.",
            root=resolution_root,
            candidate_actor="resolution-agent",
            writer_text="The competing live resolution value.",
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
            "A concurrent candidate needs resolution.",
            self.hot(root=resolution_root),
        )
        self.assertIn(
            "The competing live resolution value.",
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
        resolved_run, proposal_id, _, _ = self.stage_concurrent_candidate(
            "semantic.resolution",
            "A resolved candidate must exist in its recorded revision.",
            root=resolved_root,
            candidate_actor="semantic-resolution",
            writer_text="The competing live semantic value.",
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
            "A resolved candidate must exist in its recorded revision.",
            self.hot(root=resolved_root),
        )
        self.assertIn(
            "The competing live semantic value.",
            self.hot(root=resolved_root),
        )

    def test_started_resolution_freezes_conflict_candidate_snapshot(self):
        for resolution_status in ("applying", "failed"):
            with self.subTest(resolution_status=resolution_status):
                root = self.root / f"snapshot-{resolution_status}"
                first_run = self.start("first-agent", root=root)
                second_run = self.start("second-agent", root=root)
                owner_run = self.start("writer", root=root)
                first_candidate = self.propose(
                    first_run,
                    "snapshot.choice",
                    "Use the first agent candidate.",
                    source="agent",
                    trust="working",
                    root=root,
                )
                second_candidate = self.propose(
                    second_run,
                    "snapshot.choice",
                    "Use the later agent candidate.",
                    source="agent",
                    trust="working",
                    root=root,
                )
                original = self.propose(
                    owner_run,
                    "snapshot.choice",
                    "Keep the accepted writer value.",
                    source="agent",
                    trust="working",
                    root=root,
                )
                self.cli("sync", "--run", owner_run, root=root)
                self.assertEqual(
                    self.decision(original, root=root)["outcome"],
                    "accepted",
                )

                self.cli("sync", "--run", first_run, root=root)
                first_decision = self.decision(first_candidate, root=root)
                self.assertEqual(first_decision["outcome"], "contested")
                first_conflict_id = first_decision["conflict_id"]
                first_conflict_path = (
                    root
                    / ".bimri"
                    / "conflicts"
                    / f"{first_conflict_id}.json"
                )
                first_conflict_bytes = first_conflict_path.read_bytes()
                first_conflict = json.loads(
                    first_conflict_bytes.decode("utf-8")
                )
                head = self.state(root=root)["head_revision"]
                resolution = {
                    "authority": "human-asserted",
                    "bimri_version": "5.0.2",
                    "by": "user",
                    "choice": first_candidate,
                    "conflict_id": first_conflict_id,
                    "proposal_ids": [first_candidate],
                    "revision_before": head,
                    "started_at": "2026-08-02T00:00:00Z",
                    "status": resolution_status,
                }
                if resolution_status == "failed":
                    resolution.update({
                        "error": "Injected resolution failure.",
                        "failed_at": "2026-08-02T00:00:01Z",
                    })
                resolution_path = (
                    root
                    / ".bimri"
                    / "resolutions"
                    / f"{first_conflict_id}.json"
                )
                resolution_path.write_bytes(
                    (
                        json.dumps(resolution, indent=2, sort_keys=True)
                        + "\n"
                    ).encode("utf-8")
                )
                if resolution_status == "failed":
                    status = self.cli("status", root=root, check=False)
                    self.assertEqual(status.returncode, 1)
                    self.assertIn("Open conflicts: 1", status.stdout)
                    self.assertIn("AUTHORITY RECOVERY NEEDED", status.stdout)
                    self.assertIn("explicit retry", status.stdout)
                    blocked = self.cli(
                        "sync",
                        "--run",
                        second_run,
                        root=root,
                        check=False,
                    )
                    self.assertEqual(blocked.returncode, 2)
                    self.assertIn(
                        "authority recovery is required", blocked.stderr
                    )
                    self.assertFalse(
                        root.joinpath(
                            ".bimri",
                            "decisions",
                            f"{second_candidate}.json",
                        ).exists()
                    )
                    self.assertEqual(
                        first_conflict_path.read_bytes(), first_conflict_bytes
                    )
                    self.assertEqual(
                        json.loads(resolution_path.read_text("utf-8"))[
                            "proposal_ids"
                        ],
                        [first_candidate],
                    )
                    self.cli(
                        "resolve",
                        first_conflict_id,
                        "--choose",
                        "current",
                        "--human-approved",
                        root=root,
                    )
                else:
                    self.assertIn(
                        "Open conflicts: 1",
                        self.cli("status", root=root).stdout,
                    )

                self.cli("sync", "--run", second_run, root=root)
                second_decision = self.decision(
                    second_candidate, root=root
                )
                self.assertEqual(second_decision["outcome"], "contested")
                second_conflict_id = second_decision["conflict_id"]
                self.assertNotEqual(second_conflict_id, first_conflict_id)
                self.assertEqual(
                    first_conflict_path.read_bytes(), first_conflict_bytes
                )
                self.assertEqual(
                    json.loads(resolution_path.read_text("utf-8"))[
                        "proposal_ids"
                    ],
                    [first_candidate],
                )
                second_conflict = json.loads(
                    (
                        root
                        / ".bimri"
                        / "conflicts"
                        / f"{second_conflict_id}.json"
                    ).read_text("utf-8")
                )
                self.assertEqual(
                    second_conflict["proposal_ids"], [second_candidate]
                )
                self.assertEqual(
                    second_conflict["current_hash"],
                    first_conflict["current_hash"],
                )
                expected_open = 1 if resolution_status == "failed" else 2
                self.assertIn(
                    f"Open conflicts: {expected_open}",
                    self.cli("status", root=root).stdout,
                )

    def test_impossible_touch_and_close_bases_are_quarantine_recoverable(self):
        for operation in ("touch", "close"):
            with self.subTest(operation=operation):
                root = self.root / f"forged-{operation}-base"
                creator = self.start("creator", root=root)
                created = self.propose(
                    creator,
                    "forged.base",
                    "The base entry must exist in the recorded revision.",
                    source="agent",
                    trust="working",
                    root=root,
                )
                self.cli("sync", "--run", creator, root=root)
                self.assertEqual(
                    self.decision(created, root=root)["outcome"],
                    "accepted",
                )
                line = next(
                    line
                    for line in self.hot(root=root).splitlines()
                    if "[K:forged.base]" in line
                )
                target_id = re.match(r"\[([^\]]+)\]", line).group(1)

                attacker = self.start("attacker", root=root)
                proposed = self.cli(
                    "propose",
                    "--run",
                    attacker,
                    "--operation",
                    operation,
                    "--key",
                    "forged.base",
                    "--target",
                    target_id,
                    "--source",
                    "agent",
                    "--trust",
                    "working",
                    root=root,
                )
                proposal_id = PROPOSAL_RE.search(proposed.stdout).group(0)
                proposal_path = (
                    root
                    / ".bimri"
                    / "proposals"
                    / f"{proposal_id}.json"
                )
                valid_proposal = proposal_path.read_bytes()
                forged = json.loads(valid_proposal.decode("utf-8"))
                forged["base_revision"] = 0
                forged_bytes = (
                    json.dumps(forged, indent=2, sort_keys=True) + "\n"
                ).encode("utf-8")
                proposal_path.write_bytes(forged_bytes)

                head_before = self.state(root=root)["head_revision"]
                status = self.cli("status", root=root, check=False)
                self.assertEqual(status.returncode, 1)
                self.assertIn("AUTHORITY RECOVERY NEEDED", status.stdout)
                self.assertIn(proposal_id, status.stdout)
                self.assertIn(
                    "proposal preflight revision does not match proposal base revision",
                    status.stdout,
                )
                blocked = self.cli(
                    "sync", "--run", attacker, root=root, check=False
                )
                self.assertEqual(blocked.returncode, 2)
                self.assertIn(
                    "shared-memory writes are paused", blocked.stderr
                )
                self.assertEqual(
                    self.state(root=root)["head_revision"], head_before
                )

                quarantined = self.cli(
                    "quarantine-authority",
                    "--kind",
                    "proposal",
                    "--id",
                    proposal_id,
                    "--human-approved",
                    root=root,
                )
                self.assertIn("exact bytes preserved", quarantined.stdout)
                stub = json.loads(proposal_path.read_text("utf-8"))
                self.assertEqual(
                    (root / stub["recovery_file"]).read_bytes(), forged_bytes
                )
                replacement = root / f"reviewed-{operation}-proposal.json"
                replacement.write_bytes(valid_proposal)
                restored = self.cli(
                    "restore-authority",
                    "--kind",
                    "proposal",
                    "--id",
                    proposal_id,
                    "--from",
                    replacement,
                    "--human-approved",
                    root=root,
                )
                self.assertIn("restored validated", restored.stdout)
                self.assertEqual(proposal_path.read_bytes(), valid_proposal)
                self.assertIn(
                    "BIMRI doctor: PASSED",
                    self.cli("doctor", root=root).stdout,
                )

    def test_resolution_cannot_predate_a_candidate_base_revision(self):
        for resolution_status in ("applying", "resolved"):
            with self.subTest(resolution_status=resolution_status):
                root = self.root / f"resolution-before-base-{resolution_status}"
                seed_run = self.start("seed", root=root)
                seed = self.propose(
                    seed_run,
                    "seed.revision",
                    "Create revision one before the candidate run starts.",
                    root=root,
                )
                self.cli("sync", "--run", seed_run, root=root)
                self.assertEqual(
                    self.decision(seed, root=root)["outcome"], "accepted"
                )

                candidate_run = self.start("candidate", root=root)
                writer_run = self.start("candidate-writer", root=root)
                candidate = self.propose(
                    candidate_run,
                    "future.approval",
                    "This candidate is based on revision one.",
                    source="agent",
                    trust="working",
                    root=root,
                )
                writer = self.propose(
                    writer_run,
                    "future.approval",
                    "The competing value is based on revision one.",
                    source="agent",
                    trust="working",
                    root=root,
                )
                proposal = json.loads(
                    (
                        root
                        / ".bimri"
                        / "proposals"
                        / f"{candidate}.json"
                    ).read_text("utf-8")
                )
                self.assertEqual(proposal["base_revision"], 1)
                self.cli("sync", "--run", writer_run, root=root)
                self.assertEqual(
                    self.decision(writer, root=root)["outcome"], "accepted"
                )
                self.cli("sync", "--run", candidate_run, root=root)
                decision = self.decision(candidate, root=root)
                self.assertEqual(decision["outcome"], "contested")
                conflict_id = decision["conflict_id"]
                forged = {
                    "authority": "human-asserted",
                    "bimri_version": "5.0.2",
                    "by": "user",
                    "choice": candidate,
                    "conflict_id": conflict_id,
                    "proposal_ids": [candidate],
                    "revision_before": 0,
                    "started_at": "2026-08-02T00:00:00Z",
                    "status": resolution_status,
                }
                if resolution_status == "resolved":
                    forged.update({
                        "resolved_at": "2026-08-02T00:00:01Z",
                        "revision_after": 0,
                    })
                resolution_path = (
                    root
                    / ".bimri"
                    / "resolutions"
                    / f"{conflict_id}.json"
                )
                resolution_path.write_bytes(
                    (
                        json.dumps(forged, indent=2, sort_keys=True) + "\n"
                    ).encode("utf-8")
                )
                head_before = self.state(root=root)["head_revision"]
                status = self.cli("status", root=root, check=False)
                attempted = self.cli(
                    "resolve",
                    conflict_id,
                    "--choose",
                    candidate,
                    "--human-approved",
                    root=root,
                    check=False,
                )
                combined = (
                    status.stdout
                    + status.stderr
                    + attempted.stdout
                    + attempted.stderr
                )
                self.assertNotEqual(status.returncode, 0)
                self.assertNotEqual(attempted.returncode, 0)
                self.assertIn(
                    "conflict hot snapshot is not present in its recorded revision",
                    combined,
                )
                self.assertEqual(
                    self.state(root=root)["head_revision"], head_before
                )
                self.assertNotIn(
                    "This candidate is based on revision one.",
                    self.hot(root=root),
                )
                self.assertIn(
                    "The competing value is based on revision one.",
                    self.hot(root=root),
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
        self.assertIn("invalid proposal ID", result.stderr)
        self.assertNotIn("Traceback", result.stderr)
        self.assertEqual(sentinel.read_text("utf-8"), original)

    def test_non_head_proposal_base_revision_symlink_fails_closed(self):
        stale_run = self.start("stale-base-reader")
        committer = self.start("committer")
        committed = self.propose(
            committer,
            "symlink.advance",
            "Advance the head while the stale run remains active.",
        )
        self.cli("sync", "--run", committer)
        self.assertEqual(self.decision(committed)["outcome"], "accepted")
        self.assertEqual(self.state()["head_revision"], 1)

        base = self.root / ".bimri" / "revisions" / "V000000.md"
        external = self.root / "external-base.md"
        external.write_bytes(base.read_bytes())
        base.unlink()
        try:
            base.symlink_to(external)
        except (NotImplementedError, OSError) as exc:
            self.skipTest(f"symbolic links unavailable: {exc}")

        proposed = self.cli(
            "propose",
            "--run",
            stale_run,
            "--tier",
            "2",
            "--key",
            "symlink.read",
            "--text",
            "This must not be based on a followed symlink.",
            "--source",
            "user",
            "--trust",
            "confirmed",
            check=False,
        )
        self.assertEqual(proposed.returncode, 2)
        self.assertIn("base revision V000000 is missing or unsafe", proposed.stderr)
        status = self.cli("status", check=False)
        self.assertEqual(status.returncode, 1)
        self.assertIn("missing or unsafe revision V000000", status.stdout)
        self.assertEqual(external.read_bytes(), base.read_bytes())
        self.assertEqual(self.state()["head_revision"], 1)

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
                path.relative_to(self.root).as_posix()
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
                path.relative_to(self.root).as_posix()
                for path in recovery_files
            },
        )

        revision_path.write_bytes(canonical.replace(b"\n", b"\r\n"))
        corrupted = self.cli("status", check=False)
        self.assertEqual(corrupted.returncode, 2)
        self.assertIn(
            "state head hash does not match the accepted head revision",
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
        self.assertIn("complete at memory format v5.1.0", first.stdout)
        state = self.state()
        self.assertEqual(state["bimri_version"], "5.1.0")
        self.assertEqual(state["project_id"], "legacy-project")
        self.assertEqual(state["run_count"], 3)
        self.assertEqual(
            (state["tier1_max"], state["tier2_max"], state["tier3_max"]),
            (12, 20, 8),
        )
        self.assertEqual(state["entry_max_chars"], 500)
        self.assertEqual(state["hot_max_bytes"], 49152)
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
        self.assertIn("complete at memory format v5.1.0", second.stdout)
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
            [
                sys.executable,
                str(ENGINE),
                "install",
                "--target",
                str(target),
            ],
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
        self.assert_installed_runtime_binding(target, agents)
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
            [
                sys.executable,
                str(ENGINE),
                "install",
                "--target",
                str(target),
                "--quiescent",
            ],
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
        self.assert_installed_runtime_binding(target, agents)
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

    def test_install_keeps_v5_0_2_recovery_tools_for_corrupt_v5_0_1_authority(self):
        target = self.root / "corrupt-v5.0.1-install"
        self.cli("migrate", root=target)
        state_path = target / ".bimri" / "state.json"
        state = self.state(root=target)
        revision_path = (
            target
            / ".bimri"
            / "revisions"
            / f"V{state['head_revision']:06d}.md"
        )
        historical = revision_path.read_text("utf-8").replace(
            "<!-- BIMRI v5.0.2 | Generated view. Do not edit directly. -->",
            "<!-- BIMRI v5.0.1 | Generated view. Do not edit directly. -->",
        )
        revision_path.write_bytes(historical.encode("utf-8"))
        (target / "bimri.md").write_bytes(historical.encode("utf-8"))
        state["bimri_version"] = "5.0.1"
        state["head_hash"] = hashlib.sha256(
            historical.encode("utf-8")
        ).hexdigest()
        state_path.write_text(
            json.dumps(state, indent=2, sort_keys=True) + "\n",
            "utf-8",
        )
        corrupt_path = target / ".bimri" / "conflicts" / "C000001.json"
        corrupt_bytes = b"{corrupt pre-upgrade conflict"
        corrupt_path.write_bytes(corrupt_bytes)

        installed = subprocess.run(
            [sys.executable, str(ENGINE), "install", "--target", str(target)],
            text=True,
            capture_output=True,
            timeout=60,
        )

        self.assertEqual(
            installed.returncode, 0, installed.stdout + installed.stderr
        )
        self.assertIn("AUTHORITY RECOVERY NEEDED", installed.stdout)
        self.assertIn("repair tools remain installed", installed.stdout)
        self.assertEqual(self.state(root=target)["bimri_version"], "5.1.0")
        self.assert_installed_runtime_binding(target)
        manifests = list(
            (target / ".bimri" / "install-backups").glob(
                "*/install-manifest.json"
            )
        )
        self.assertEqual(len(manifests), 1)
        manifest = json.loads(manifests[0].read_text("utf-8"))
        self.assertEqual(manifest["status"], "installed-recovery-required")

        quarantined = self.cli(
            "quarantine-authority",
            "--kind",
            "conflict",
            "--id",
            "C000001",
            "--human-approved",
            root=target,
            engine=target / "bimri-engine.py",
        )
        self.assertIn("exact bytes preserved", quarantined.stdout)
        stub = json.loads(corrupt_path.read_text("utf-8"))
        self.assertEqual(
            (target / stub["recovery_file"]).read_bytes(), corrupt_bytes
        )

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
        original_local_artifacts = {
            "runtime.local.json": b'{"old":"runtime binding"}\n',
            "hooks.claude.local.json": b'{"old":"hook binding"}\n',
        }
        for name, content in original_local_artifacts.items():
            (bdir / name).write_bytes(content)
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
        for name, content in original_local_artifacts.items():
            self.assertEqual((bdir / name).read_bytes(), content)
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
        revision_path.write_bytes(escaped.encode("utf-8"))
        (self.root / "bimri.md").write_bytes(escaped.encode("utf-8"))
        state["head_hash"] = hashlib.sha256(escaped.encode("utf-8")).hexdigest()
        state_path.write_text(json.dumps(state, indent=2, sort_keys=True) + "\n", "utf-8")

        pointer_result = self.cli("doctor", check=False)
        self.assertNotEqual(pointer_result.returncode, 0)
        self.assertIn(
            "pointer escapes the BIMRI project",
            pointer_result.stdout + pointer_result.stderr,
        )

        malformed = escaped.replace(
            "## Tier 2: Active Context",
            "## Tier 2: Active Context\nthis is malformed shared memory",
        )
        revision_path.write_bytes(malformed.encode("utf-8"))
        (self.root / "bimri.md").write_bytes(malformed.encode("utf-8"))
        state["head_hash"] = hashlib.sha256(malformed.encode("utf-8")).hexdigest()
        state_path.write_text(json.dumps(state, indent=2, sort_keys=True) + "\n", "utf-8")
        malformed_result = self.cli("doctor", check=False)
        self.assertNotEqual(malformed_result.returncode, 0)
        self.assertIn(
            "malformed Tier 2 entry",
            malformed_result.stdout + malformed_result.stderr,
        )

    def test_migrate_does_not_claim_validation_for_duplicate_memory_keys(self):
        initialized = self.cli("migrate")
        self.assertNotIn("index is missing", initialized.stdout)
        self.assertTrue((self.root / ".bimri" / "index.tsv").is_file())
        state_path = self.root / ".bimri" / "state.json"
        state = self.state()
        revision_path = (
            self.root
            / ".bimri"
            / "revisions"
            / f"V{state['head_revision']:06d}.md"
        )
        duplicate = revision_path.read_text("utf-8").replace(
            "<!-- Confirmed facts, decisions, preferences and rules. Soft target: state.json. -->",
            "<!-- Confirmed facts, decisions, preferences and rules. Soft target: state.json. -->\n\n"
            "[R0-E1] [K:duplicate.key] [fact] [T:working] "
            "[SRC:legacy] [] First value.\n\n"
            "[R0-E2] [K:duplicate.key] [fact] [T:working] "
            "[SRC:legacy] [] Second value.",
        )
        revision_path.write_bytes(duplicate.encode("utf-8"))
        (self.root / "bimri.md").write_bytes(duplicate.encode("utf-8"))
        state["head_hash"] = hashlib.sha256(duplicate.encode("utf-8")).hexdigest()
        state_path.write_text(
            json.dumps(state, indent=2, sort_keys=True) + "\n", "utf-8"
        )

        result = self.cli("migrate", check=False)

        self.assertEqual(result.returncode, 2)
        self.assertIn("accepted head memory grammar is invalid", result.stderr)
        self.assertIn("duplicate memory key: duplicate.key", result.stderr)
        self.assertNotIn("Validation: PASSED", result.stdout)

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
        ).read_bytes()
        (
            self.root / ".bimri" / "revisions" / "V000001.md"
        ).write_bytes(revision_zero)
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
        self.assertIn(
            "applied 1, held candidates 0, already satisfied/no change 0",
            result.stdout,
        )
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
        self.assertIn(
            "applied 1, held candidates 0, already satisfied/no change 0",
            result.stdout,
        )
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

    def test_hot_operations_do_not_call_retired_automatic_index_rebuild(self):
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
        self.assertNotIn("forced index failure", started.stderr)
        self.assertIn(
            start_run,
            self.state(root=start_root)["active_runs"],
        )
        index_path = start_root / ".bimri" / "index.tsv"
        index_path.write_bytes(b"owner-controlled stale index sentinel\n")

        journaled = self.worker(
            "index_failure",
            "journal",
            "--run",
            start_run,
            "--text",
            "Journal must not rebuild the unused derived index.",
            root=start_root,
        )
        self.assertNotIn("forced index failure", journaled.stderr)
        self.assertEqual(
            index_path.read_bytes(), b"owner-controlled stale index sentinel\n"
        )

        proposed = self.worker(
            "index_failure",
            "propose",
            "--run",
            start_run,
            "--tier",
            "2",
            "--key",
            "index.sync",
            "--text",
            "Sync commits without touching the retired hot-path index.",
            "--source",
            "agent",
            "--trust",
            "working",
            "--new-subject",
            root=start_root,
        )
        sync_proposal = PROPOSAL_RE.search(proposed.stdout).group(0)
        self.assertNotIn("forced index failure", proposed.stderr)
        synced = self.worker(
            "index_failure",
            "sync",
            "--run",
            start_run,
            root=start_root,
        )
        self.assertNotIn("forced index failure", synced.stderr)
        self.assertEqual(
            self.decision(sync_proposal, root=start_root)["outcome"],
            "accepted",
        )
        self.assertIn("[K:index.sync]", self.hot(root=start_root))
        self.assertEqual(
            index_path.read_bytes(), b"owner-controlled stale index sentinel\n"
        )

        closed = self.worker(
            "index_failure",
            "close",
            "--run",
            start_run,
            "--outcome",
            "success",
            "--summary",
            "Close also leaves the unused index untouched.",
            root=start_root,
        )
        self.assertNotIn("forced index failure", closed.stderr)
        self.assertNotIn(
            start_run,
            self.state(root=start_root)["active_runs"],
        )
        close_log = (
            start_root / ".bimri" / "log" / f"{start_run}.md"
        ).read_text("utf-8")
        self.assertIn(f"[CLOSED:{start_run} ", close_log)
        self.assertEqual(
            index_path.read_bytes(), b"owner-controlled stale index sentinel\n"
        )

        resolve_root = self.root / "index-resolve"
        resolve_run, candidate, _, _ = self.stage_concurrent_candidate(
            "index.resolve",
            "Human-approved resolution without automatic indexing.",
            root=resolve_root,
            candidate_actor="index-resolve",
            writer_text="The competing current value.",
        )
        self.cli("sync", "--run", resolve_run, root=resolve_root)
        contested = self.decision(candidate, root=resolve_root)
        self.assertEqual(contested["outcome"], "contested")
        resolve_index = resolve_root / ".bimri" / "index.tsv"
        resolve_index.write_bytes(b"resolution index sentinel\n")
        resolved = self.worker(
            "index_failure",
            "resolve",
            contested["conflict_id"],
            "--choose",
            candidate,
            "--human-approved",
            root=resolve_root,
        )
        self.assertNotIn("forced index failure", resolved.stderr)
        self.assertEqual(
            self.decision(candidate, root=resolve_root)["outcome"],
            "accepted",
        )
        self.assertEqual(
            resolve_index.read_bytes(), b"resolution index sentinel\n"
        )

    def test_index_and_doctor_are_deterministic(self):
        run_id = self.start("codex")
        self.cli(
            "journal", "--run", run_id,
            "--text", "Index paths stay portable across operating systems.",
        )
        first = self.propose(run_id, "zeta.item", "Zeta comes second alphabetically.")
        second = self.propose(run_id, "alpha.item", "Alpha comes first alphabetically.")
        self.cli("sync", "--run", run_id)
        self.assertEqual(self.decision(first)["outcome"], "accepted")
        self.assertEqual(self.decision(second)["outcome"], "accepted")
        closed = self.cli(
            "propose",
            "--run",
            run_id,
            "--operation",
            "close",
            "--key",
            "zeta.item",
            "--source",
            "user",
            "--trust",
            "confirmed",
        )
        close_id = PROPOSAL_RE.search(closed.stdout).group(0)
        self.cli("sync", "--run", run_id)
        self.assertEqual(self.decision(close_id)["outcome"], "accepted")
        archive_path = (
            self.root
            / ".bimri"
            / "archive"
            / f"{dt.date.today():%Y-%m}.md"
        )
        self.assertTrue(archive_path.is_file())

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
        indexed_files = {row.split("\t")[6] for row in rows[1:]}
        self.assertIn(f".bimri/log/{run_id}.md", indexed_files)
        self.assertIn(
            archive_path.relative_to(self.root).as_posix(), indexed_files
        )
        self.assertTrue(all("\\" not in path for path in indexed_files))

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
        self.assertIn("complete at memory format v5.1.0", first.stdout)
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
        for number in range(1, 51):
            lines.extend([
                f"[ID:T2-20260720-{number:02d}] [IMP:3] [CREATED:2026-07-20] "
                f"[SESSION:1] [LAST_USED:2026-07-20] [LAST_USED_SESSION:1] "
                f"[TAGS:legacy] [W:3.0]",
                f"Inherited active claim number {number} with deliberately retained detail "
                f"that makes the generated view exceed its normal byte ceiling. " + ("x" * 950),
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
        self.assertEqual(self.hot().count("[K:legacy.v3.t2-20260720-"), 50)
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

    def test_v4_long_pattern_is_preserved_as_repair_only_overflow(self):
        bdir = self.root / ".bimri"
        bdir.mkdir()
        hypothesis = "Inherited long v4 pattern: " + ("p" * 700)
        legacy_hot = (
            "# BIMRI Memory\n\n"
            "## Tier 1: Core Intelligence\n\n"
            "## Tier 2: Active Context\n\n"
            "## Tier 3: Pattern Recognition\n\n"
            f"[P1] [emerging] [obs:2] [ev:R1-E1] {hypothesis} "
            "| Falsify: contrary evidence\n\n"
            "<!-- END BIMRI -->\n"
        )
        (self.root / "bimri.md").write_text(legacy_hot, "utf-8")
        (bdir / "state.json").write_text(
            json.dumps({
                "bimri_version": "4.0",
                "project_id": "long-v4-pattern",
                "run_count": 0,
                "current_run_id": "R000",
            }, indent=2) + "\n",
            "utf-8",
        )

        migrated = self.cli("migrate")

        self.assertIn("Memory: migrated BIMRI v4.0", migrated.stdout)
        self.assertIn("inherited overlength 1", migrated.stdout)
        self.assertIn(hypothesis, self.hot())
        pattern_line = next(
            line for line in self.hot().splitlines() if line.startswith("[P1]")
        )
        self.assertLess(len(pattern_line), 4096)
        doctor = self.cli("doctor")
        self.assertIn("BIMRI doctor: PASSED", doctor.stdout)
        self.assertIn(
            "inherited v4 pattern text exceeds active entry cap", doctor.stdout
        )
        run_id = self.start("pattern-repair")
        proposal_id = self.propose(
            run_id,
            "legacy.pattern-p1",
            "Condensed inherited pattern.",
            tier=3,
            source="user",
            trust="confirmed",
            extra=(
                "--target", "P1",
                "--confidence", "emerging",
                "--observations", "2",
                "--evidence", "R1-E1",
                "--falsifier", "Contrary evidence appears.",
            ),
        )
        self.cli("sync", "--run", run_id)
        self.assertEqual(self.decision(proposal_id)["outcome"], "accepted")
        self.assertNotIn(
            "inherited v4 pattern text exceeds active entry cap",
            self.cli("doctor").stdout,
        )

    def test_v4_marker_forgery_fails_before_authority_changes(self):
        hot = (
            "# BIMRI Memory\n\n"
            "## Tier 1: Core Intelligence\n\n"
            "## Tier 2: Active Context\n\n"
            "## Tier 3: Pattern Recognition\n\n"
            "<!-- END BIMRI -->\n"
        ).encode("utf-8")
        base_state = {
            "bimri_version": "4.0",
            "project_id": "marker-validation",
            "run_count": 0,
            "current_run_id": "R000",
        }
        cases = ("empty", "missing", "traversal", "corrupt")
        for case in cases:
            with self.subTest(case=case):
                root = self.root / case
                bdir = root / ".bimri"
                backups = bdir / "backups"
                migrations = bdir / "migrations"
                backups.mkdir(parents=True)
                migrations.mkdir()
                state_bytes = (
                    json.dumps(base_state, indent=2) + "\n"
                ).encode("utf-8")
                state_path = bdir / "state.json"
                hot_path = root / "bimri.md"
                state_path.write_bytes(state_bytes)
                hot_path.write_bytes(hot)
                state_backup = backups / "state-v4.json-test"
                hot_backup = backups / "bimri-v4.md-test"
                if case == "corrupt":
                    state_backup.write_bytes(state_bytes)
                    hot_backup.write_bytes(b"not the authoritative hot memory\n")
                marker = {} if case == "empty" else {
                    "migration": "v4-to-v5",
                    "completed_at": "2026-08-02T00:00:00Z",
                    "source_hot_hash": hashlib.sha256(hot).hexdigest(),
                    "backup_state": (
                        "../escaped-state.json"
                        if case == "traversal"
                        else ".bimri/backups/state-v4.json-test"
                    ),
                    "backup_hot": (
                        "../escaped-hot.md"
                        if case == "traversal"
                        else ".bimri/backups/bimri-v4.md-test"
                    ),
                }
                marker_path = migrations / "v4-to-v5.json"
                marker_bytes = (
                    json.dumps(marker, indent=2, sort_keys=True) + "\n"
                ).encode("utf-8")
                marker_path.write_bytes(marker_bytes)

                result = self.cli("migrate", root=root, check=False)

                self.assertEqual(result.returncode, 2)
                self.assertIn("v4 migration", result.stderr)
                self.assertEqual(state_path.read_bytes(), state_bytes)
                self.assertEqual(hot_path.read_bytes(), hot)
                self.assertEqual(marker_path.read_bytes(), marker_bytes)
                self.assertFalse((bdir / "revisions" / "V000000.md").exists())

    def test_v4_late_writers_stop_before_revision_or_state_commit(self):
        hot = (
            "# BIMRI Memory\n\n"
            "## Tier 1: Core Intelligence\n\n"
            "## Tier 2: Active Context\n\n"
            "## Tier 3: Pattern Recognition\n\n"
            "<!-- END BIMRI -->\n"
        ).encode("utf-8")
        state = (
            json.dumps({
                "bimri_version": "4.0",
                "project_id": "late-v4-writer",
                "run_count": 0,
                "current_run_id": "R000",
            }, indent=2) + "\n"
        ).encode("utf-8")
        for mode, changed_name in (
            ("v4_hot_change_after_backup", "bimri.md"),
            ("v4_state_change_after_backup", "state.json"),
        ):
            with self.subTest(mode=mode):
                root = self.root / mode
                bdir = root / ".bimri"
                bdir.mkdir(parents=True)
                (root / "bimri.md").write_bytes(hot)
                (bdir / "state.json").write_bytes(state)

                result = self.worker(mode, "migrate", root=root, check=False)

                self.assertEqual(result.returncode, 2)
                self.assertIn("changed while migration was preparing", result.stderr)
                changed_path = root / changed_name if changed_name == "bimri.md" else bdir / changed_name
                original = hot if changed_name == "bimri.md" else state
                self.assertTrue(changed_path.read_bytes().startswith(original))
                unchanged_path = bdir / "state.json" if changed_name == "bimri.md" else root / "bimri.md"
                unchanged = state if changed_name == "bimri.md" else hot
                self.assertEqual(unchanged_path.read_bytes(), unchanged)
                self.assertFalse((bdir / "revisions" / "V000000.md").exists())
                self.assertFalse((bdir / "migrations" / "v4-to-v5.json").exists())
                hot_backups = list((bdir / "backups").glob("bimri-v4.md-*"))
                self.assertEqual(len(hot_backups), 1)
                self.assertEqual(hot_backups[0].read_bytes(), hot)

    def test_v4_pointer_escape_fails_before_authority_commit(self):
        bdir = self.root / ".bimri"
        bdir.mkdir()
        hot = (
            "# BIMRI Memory\n\n"
            "## Tier 1: Core Intelligence\n\n"
            "[R1-E1] [fact] [legacy] Unsafe pointer -> ../../outside.md\n\n"
            "## Tier 2: Active Context\n\n"
            "## Tier 3: Pattern Recognition\n\n"
            "<!-- END BIMRI -->\n"
        ).encode("utf-8")
        state = (
            json.dumps({
                "bimri_version": "4.0",
                "project_id": "pointer-escape",
                "run_count": 0,
                "current_run_id": "R000",
            }, indent=2) + "\n"
        ).encode("utf-8")
        (self.root / "bimri.md").write_bytes(hot)
        (bdir / "state.json").write_bytes(state)

        result = self.cli("migrate", check=False)

        self.assertEqual(result.returncode, 2)
        self.assertIn("pointer escapes the BIMRI project", result.stderr)
        self.assertEqual((self.root / "bimri.md").read_bytes(), hot)
        self.assertEqual((bdir / "state.json").read_bytes(), state)
        self.assertFalse((bdir / "revisions" / "V000000.md").exists())
        self.assertFalse((bdir / "migrations" / "v4-to-v5.json").exists())

    def test_v4_marker_and_backups_resume_after_crash_before_state(self):
        bdir = self.root / ".bimri"
        bdir.mkdir()
        hot = (
            "# BIMRI Memory\n\n"
            "## Tier 1: Core Intelligence\n\n"
            "[R2-E1] [fact] [legacy] Resume this v4 fact\n\n"
            "## Tier 2: Active Context\n\n"
            "## Tier 3: Pattern Recognition\n\n"
            "<!-- END BIMRI -->\n"
        ).encode("utf-8")
        state_bytes = (
            json.dumps({
                "bimri_version": "4.0",
                "project_id": "v4-crash-resume",
                "run_count": 2,
                "current_run_id": "R000",
            }, indent=2) + "\n"
        ).encode("utf-8")
        (self.root / "bimri.md").write_bytes(hot)
        (bdir / "state.json").write_bytes(state_bytes)

        crashed = self.worker(
            "v4_crash_before_state", "migrate", root=self.root, check=False
        )

        self.assertEqual(crashed.returncode, 95)
        self.assertEqual((self.root / "bimri.md").read_bytes(), hot)
        self.assertEqual((bdir / "state.json").read_bytes(), state_bytes)
        marker_path = bdir / "migrations" / "v4-to-v5.json"
        marker_before = marker_path.read_bytes()
        marker = json.loads(marker_before)
        for field in ("backup_state", "backup_hot"):
            backup = self.root / marker[field]
            self.assertTrue(backup.is_file())
        revision_before = (bdir / "revisions" / "V000000.md").read_bytes()

        resumed = self.cli("migrate")

        self.assertIn("Memory: migrated BIMRI v4.0", resumed.stdout)
        self.assertEqual(marker_path.read_bytes(), marker_before)
        self.assertEqual(
            (bdir / "revisions" / "V000000.md").read_bytes(), revision_before
        )
        self.assertEqual(self.state()["bimri_version"], "5.1.0")
        self.assertIn("BIMRI doctor: PASSED", self.cli("doctor").stdout)

    def test_v4_historical_conversion_keeps_v000000_and_normalizes_active_head(self):
        bdir = self.root / ".bimri"
        revisions = bdir / "revisions"
        revisions.mkdir(parents=True)
        claim = "Preserve the historical v4 conversion exactly."
        source = (
            "# BIMRI Memory\n\n"
            "<!-- BIMRI v4 | Generated view. Do not edit directly. -->\n"
            "<!-- Engine: legacy -->\n\n"
            "## Tier 1: Core Intelligence\n\n"
            "<!-- Confirmed facts, decisions, preferences and rules. Cap: 12. -->\n\n"
            f"[R1-E1] [fact] [legacy] {claim}\n\n"
            "## Tier 2: Active Context\n\n"
            "<!-- Current work, risks and next actions. Cap: 20. -->\n\n"
            "## Tier 3: Pattern Recognition\n\n"
            "<!-- Evidence-backed patterns. Cap: 8. -->\n\n"
            "<!-- END BIMRI -->\n"
        )
        historical = source.replace(
            "<!-- BIMRI v4 | Generated view. Do not edit directly. -->",
            "<!-- BIMRI v5 | Generated view. Do not edit directly. -->",
        ).replace(
            "<!-- Engine: legacy -->",
            "<!-- Full history: .bimri/log/ | Revisions: .bimri/revisions/ -->",
        ).replace(
            f"[R1-E1] [fact] [legacy] {claim}",
            "[R1-E1] [K:legacy.r1-e1] [fact] [T:working] "
            f"[SRC:legacy] [legacy] {claim}",
        )
        (self.root / "bimri.md").write_bytes(source.encode("utf-8"))
        state = {
            "bimri_version": "4.0",
            "project_id": "historical-v4",
            "run_count": 1,
            "current_run_id": "R000",
        }
        (bdir / "state.json").write_text(
            json.dumps(state, indent=2) + "\n", "utf-8"
        )
        v0 = revisions / "V000000.md"
        v0.write_bytes(historical.encode("utf-8"))

        migrated = self.cli("migrate")

        self.assertEqual(v0.read_text("utf-8"), historical)
        self.assertIn(
            "Memory metadata normalized in immutable revision V000001",
            migrated.stdout,
        )
        current = self.state()
        self.assertEqual(current["head_revision"], 1)
        normalized = (revisions / "V000001.md").read_text("utf-8")
        self.assertEqual((self.root / "bimri.md").read_text("utf-8"), normalized)
        self.assertIn("BIMRI v5.0.2", normalized)
        self.assertNotIn("Cap: 12", normalized)
        self.assertEqual(normalized.count(claim), 1)
        self.assertIn("BIMRI doctor: PASSED", self.cli("doctor").stdout)

    def test_v4_state_refuses_distinct_unclaimed_legacy_before_migration(self):
        probe = self.root / "case-probe"
        probe.write_text("probe", "utf-8")
        case_insensitive = (self.root / "CASE-PROBE").exists()
        probe.unlink()
        if case_insensitive:
            return

        def prepare(root):
            bdir = root / ".bimri"
            bdir.mkdir(parents=True)
            state = (
                json.dumps({
                    "bimri_version": "4.0",
                    "project_id": "v4-unclaimed",
                    "run_count": 0,
                    "current_run_id": "R000",
                }, indent=2) + "\n"
            ).encode("utf-8")
            hot = (
                "# BIMRI Memory\n\n"
                "## Tier 1: Core Intelligence\n\n"
                "## Tier 2: Active Context\n\n"
                "## Tier 3: Pattern Recognition\n\n"
                "<!-- END BIMRI -->\n"
            ).encode("utf-8")
            upper = self.legacy_v3_bytes("Competing legacy authority.")
            (bdir / "state.json").write_bytes(state)
            (root / "bimri.md").write_bytes(hot)
            (root / "BIMRI.md").write_bytes(upper)
            return state, hot, upper

        direct_root = self.root / "direct"
        state, hot, upper = prepare(direct_root)
        direct = self.cli("migrate", root=direct_root, check=False)
        self.assertEqual(direct.returncode, 2)
        self.assertIn("unclaimed legacy root file", direct.stderr)
        self.assertEqual((direct_root / ".bimri" / "state.json").read_bytes(), state)
        self.assertEqual((direct_root / "bimri.md").read_bytes(), hot)
        self.assertEqual((direct_root / "BIMRI.md").read_bytes(), upper)
        self.assertFalse(
            (direct_root / ".bimri" / "migrations" / "v4-to-v5.json").exists()
        )

        install_root = self.root / "install"
        state, hot, upper = prepare(install_root)
        installed = subprocess.run(
            [sys.executable, str(ENGINE), "install", "--target", str(install_root)],
            text=True,
            capture_output=True,
            timeout=45,
        )
        self.assertEqual(installed.returncode, 2)
        self.assertIn("unclaimed legacy root file", installed.stderr)
        self.assertEqual((install_root / ".bimri" / "state.json").read_bytes(), state)
        self.assertEqual((install_root / "bimri.md").read_bytes(), hot)
        self.assertEqual((install_root / "BIMRI.md").read_bytes(), upper)
        self.assertFalse((install_root / "BIMRI-PROTOCOL.md").exists())

    def test_python_verification_failures_precede_target_mutation(self):
        cases = {
            "python_verify_silent": "no valid BIMRI verification sentinel",
            "python_verify_wrong": "wrong BIMRI verification sentinel",
            "python_verify_old": "not Python 3.8 or newer",
            "python_verify_timeout": "five-second verification check",
        }
        for mode, message in cases.items():
            with self.subTest(mode=mode):
                target = self.root / mode
                result = self.worker(
                    mode,
                    "install",
                    "--target",
                    str(target),
                    root=self.root,
                    check=False,
                )
                self.assertEqual(result.returncode, 2)
                self.assertIn(message, result.stderr)
                self.assertFalse(target.exists())

    def test_repository_runtime_templates_are_host_generic(self):
        python_executable = str(Path(sys.executable).resolve())
        repository_path = str(REPOSITORY.resolve())
        for name in (
            "AGENTS.md",
            "BIMRI-AGENT-BLOCK.md",
            "CLAUDE.md",
            "hooks-example.json",
        ):
            with self.subTest(name=name):
                text = (REPOSITORY / name).read_text("utf-8")
                self.assertNotIn(python_executable, text)
                self.assertNotIn(repository_path, text)
        hooks = json.loads((REPOSITORY / "hooks-example.json").read_text("utf-8"))
        for event in ("SessionStart", "SessionEnd"):
            self.assertEqual(
                hooks["hooks"][event][0]["hooks"][0]["command"],
                "__BIMRI_VERIFIED_PYTHON__",
            )

    def test_self_install_rebinds_old_host_hooks_and_run_hint_is_safe(self):
        target = self.root / "portable project with spaces"
        install = subprocess.run(
            [sys.executable, str(ENGINE), "install", "--target", str(target)],
            text=True,
            capture_output=True,
            timeout=45,
        )
        self.assertEqual(install.returncode, 0, install.stdout + install.stderr)
        hooks_path = target / ".bimri" / "hooks.claude.local.json"
        hooks = json.loads(hooks_path.read_text("utf-8"))
        for event in ("SessionStart", "SessionEnd"):
            hooks["hooks"][event][0]["hooks"][0]["command"] = (
                r"C:\Old Host\Python 3.10\python.exe"
            )
        hooks_path.write_text(
            json.dumps(hooks, indent=2, ensure_ascii=False) + "\n", "utf-8"
        )
        runtime_path = target / ".bimri" / "runtime.local.json"
        runtime = json.loads(runtime_path.read_text("utf-8"))
        runtime.update({
            "python_executable": r"C:\Old Host\Python 3.10\python.exe",
            "engine_path": r"C:\Old Host\BIMRI\bimri-engine.py",
            "argv_prefix": [
                r"C:\Old Host\Python 3.10\python.exe",
                r"C:\Old Host\BIMRI\bimri-engine.py",
            ],
        })
        runtime_path.write_text(
            json.dumps(runtime, indent=2, ensure_ascii=False) + "\n", "utf-8"
        )

        reinstall = subprocess.run(
            [
                sys.executable,
                str(target / "bimri-engine.py"),
                "install",
                "--target",
                str(target),
                "--quiescent",
            ],
            text=True,
            capture_output=True,
            timeout=45,
        )
        self.assertEqual(
            reinstall.returncode, 0, reinstall.stdout + reinstall.stderr
        )
        self.assert_installed_runtime_binding(target)

        started = subprocess.run(
            [
                sys.executable,
                str(target / "bimri-engine.py"),
                "start",
                "--actor",
                "rebind-test",
            ],
            text=True,
            capture_output=True,
            timeout=30,
        )
        self.assertEqual(started.returncode, 0, started.stdout + started.stderr)
        run_id = re.search(r"BIMRI RUN HANDLE: (R\d{6})", started.stdout).group(1)
        run_log = (target / ".bimri" / "log" / f"{run_id}.md").read_text("utf-8")
        self.assertIn("argv_prefix recorded in .bimri/runtime.local.json", run_log)
        unsafe_hint = f"{Path(sys.executable).name} bimri-engine.py journal"
        self.assertNotIn(unsafe_hint, run_log)

    def test_v5_0_profiles_upgrade_with_metadata_only_revision(self):
        profiles = {
            "stock": {
                "old": (12, 20, 8, 500, 16384),
                "new": (20, 40, 12, 500, 49152),
                "message": "stock limits expanded",
            },
            "custom": {
                "old": (7, 9, 3, 240, 20000),
                "new": (7, 9, 3, 240, 20000),
                "message": "custom limits preserved",
            },
        }
        fields = (
            "tier1_max",
            "tier2_max",
            "tier3_max",
            "entry_max_chars",
            "hot_max_bytes",
        )
        for label, profile in profiles.items():
            with self.subTest(profile=label):
                root = self.root / label
                self.cli("migrate", root=root)
                state_path = root / ".bimri" / "state.json"
                state = json.loads(state_path.read_text("utf-8"))
                revision_path = root / ".bimri" / "revisions" / "V000000.md"
                current_template = revision_path.read_text("utf-8")
                historical = (
                    current_template.replace(
                        "<!-- BIMRI v5.0.2 | Generated view. Do not edit directly. -->",
                        "<!-- BIMRI v5 | Generated view. Do not edit directly. -->",
                    )
                    .replace(
                        "<!-- Confirmed facts, decisions, preferences and rules. Soft target: state.json. -->",
                        "<!-- Confirmed facts, decisions, preferences and rules. Cap: 12. -->",
                    )
                    .replace(
                        "<!-- Current work, risks and next actions. Soft target: state.json. -->",
                        "<!-- Current work, risks and next actions. Cap: 20. -->",
                    )
                    .replace(
                        "<!-- Evidence-backed patterns. Soft target: state.json. -->",
                        "<!-- Evidence-backed patterns. Cap: 8. -->",
                    )
                )
                entry = (
                    "[R0-E1] [K:upgrade.metadata] [fact] [T:working] "
                    "[SRC:legacy] [] Preserve this entry byte-for-byte."
                )
                historical = historical.replace(
                    "<!-- Confirmed facts, decisions, preferences and rules. Cap: 12. -->",
                    "<!-- Confirmed facts, decisions, preferences and rules. Cap: 12. -->"
                    f"\n\n{entry}",
                )
                expected = (
                    historical.replace(
                        "<!-- BIMRI v5 | Generated view. Do not edit directly. -->",
                        "<!-- BIMRI v5.0.2 | Generated view. Do not edit directly. -->",
                    )
                    .replace(
                        "<!-- Confirmed facts, decisions, preferences and rules. Cap: 12. -->",
                        "<!-- Confirmed facts, decisions, preferences and rules. Capacity: state.json. -->",
                    )
                    .replace(
                        "<!-- Current work, risks and next actions. Cap: 20. -->",
                        "<!-- Current work, risks and next actions. Capacity: state.json. -->",
                    )
                    .replace(
                        "<!-- Evidence-backed patterns. Cap: 8. -->",
                        "<!-- Evidence-backed patterns. Capacity: state.json. -->",
                    )
                )
                revision_path.write_bytes(historical.encode("utf-8"))
                (root / "bimri.md").write_bytes(historical.encode("utf-8"))
                state["bimri_version"] = "5.0"
                state.update(dict(zip(fields, profile["old"])))
                state["head_hash"] = hashlib.sha256(
                    historical.encode("utf-8")
                ).hexdigest()
                old_state_bytes = (
                    json.dumps(state, indent=2, sort_keys=True) + "\n"
                ).encode("utf-8")
                state_path.write_bytes(old_state_bytes)

                upgraded = self.cli("migrate", root=root)

                self.assertIn("Memory: upgraded v5.0 to v5.1.0", upgraded.stdout)
                self.assertIn(profile["message"], upgraded.stdout)
                self.assertIn(
                    f"entry {profile['new'][3]} chars", upgraded.stdout
                )
                if label == "custom":
                    self.assertGreaterEqual(
                        upgraded.stdout.count("entry 240 chars"), 2
                    )
                self.assertIn(
                    "Memory metadata normalized in immutable revision V000001",
                    upgraded.stdout,
                )
                current = self.state(root=root)
                self.assertEqual(current["bimri_version"], "5.1.0")
                self.assertEqual(
                    tuple(current[field] for field in fields), profile["new"]
                )
                self.assertEqual(current["head_revision"], 1)
                self.assertEqual(revision_path.read_text("utf-8"), historical)
                normalized_revision = (
                    root / ".bimri" / "revisions" / "V000001.md"
                )
                self.assertEqual(normalized_revision.read_text("utf-8"), expected)
                self.assertEqual((root / "bimri.md").read_text("utf-8"), expected)
                self.assertEqual(expected.count(entry), 1)
                backups = list(
                    (root / ".bimri" / "backups").glob("state-v5.0-*.json")
                )
                self.assertEqual(len(backups), 1)
                self.assertEqual(backups[0].read_bytes(), old_state_bytes)
                self.assertIn(
                    "existing v5.1.0 verified; no migration performed",
                    self.cli("migrate", root=root).stdout,
                )
                self.assertIn(
                    "BIMRI doctor: PASSED", self.cli("doctor", root=root).stdout
                )

    def test_v5_0_1_upgrades_to_v5_0_2_without_changing_limits(self):
        self.cli("migrate")
        state_path = self.root / ".bimri" / "state.json"
        revision_path = self.root / ".bimri" / "revisions" / "V000000.md"
        current = revision_path.read_text("utf-8")
        historical = current.replace(
            "<!-- BIMRI v5.0.2 | Generated view. Do not edit directly. -->",
            "<!-- BIMRI v5.0.1 | Generated view. Do not edit directly. -->",
        )
        revision_path.write_bytes(historical.encode("utf-8"))
        (self.root / "bimri.md").write_bytes(historical.encode("utf-8"))
        state = self.state()
        limits_before = tuple(
            state[field]
            for field in (
                "tier1_max", "tier2_max", "tier3_max",
                "entry_max_chars", "hot_max_bytes",
            )
        )
        state["bimri_version"] = "5.0.1"
        state["head_hash"] = hashlib.sha256(
            historical.encode("utf-8")
        ).hexdigest()
        old_state_bytes = (
            json.dumps(state, indent=2, sort_keys=True) + "\n"
        ).encode("utf-8")
        state_path.write_bytes(old_state_bytes)

        upgraded = self.cli("migrate")

        self.assertIn(
            "Memory: upgraded v5.0.1 to v5.1.0; limits preserved",
            upgraded.stdout,
        )
        new_state = self.state()
        self.assertEqual(new_state["bimri_version"], "5.1.0")
        self.assertEqual(
            tuple(
                new_state[field]
                for field in (
                    "tier1_max", "tier2_max", "tier3_max",
                    "entry_max_chars", "hot_max_bytes",
                )
            ),
            limits_before,
        )
        self.assertEqual(new_state["head_revision"], 1)
        self.assertEqual(revision_path.read_text("utf-8"), historical)
        normalized = (
            self.root / ".bimri" / "revisions" / "V000001.md"
        ).read_text("utf-8")
        self.assertIn("BIMRI v5.0.2", normalized)
        backups = list(
            (self.root / ".bimri" / "backups").glob(
                "state-v5.0.1-*.json"
            )
        )
        self.assertEqual(len(backups), 1)
        self.assertEqual(backups[0].read_bytes(), old_state_bytes)
        repeated = self.cli("migrate")
        self.assertIn(
            "existing v5.1.0 verified; no migration performed",
            repeated.stdout,
        )
        self.assertFalse(
            (self.root / ".bimri" / "revisions" / "V000002.md").exists()
        )

    def test_incomplete_v5_0_state_fails_without_default_guessing_or_mutation(self):
        self.cli("migrate")
        state_path = self.root / ".bimri" / "state.json"
        state = self.state()
        state["bimri_version"] = "5.0"
        state.pop("hot_max_bytes")
        incomplete = (
            json.dumps(state, indent=2, sort_keys=True) + "\n"
        ).encode("utf-8")
        state_path.write_bytes(incomplete)
        hot_before = (self.root / "bimri.md").read_bytes()
        revision_before = (
            self.root / ".bimri" / "revisions" / "V000000.md"
        ).read_bytes()

        result = self.cli("migrate", check=False)

        self.assertEqual(result.returncode, 2)
        self.assertIn("missing required v5 field(s): hot_max_bytes", result.stderr)
        self.assertIn("stopped without filling them from defaults", result.stderr)
        self.assertEqual(state_path.read_bytes(), incomplete)
        self.assertEqual((self.root / "bimri.md").read_bytes(), hot_before)
        self.assertEqual(
            (self.root / ".bimri" / "revisions" / "V000000.md").read_bytes(),
            revision_before,
        )
        self.assertEqual(
            list((self.root / ".bimri" / "backups").glob("state-v5.0-*.json")),
            [],
        )

    def test_v5_0_metadata_normalization_respects_exact_custom_byte_cap(self):
        self.cli("migrate")
        revision_path = self.root / ".bimri" / "revisions" / "V000000.md"
        historical = (
            revision_path.read_text("utf-8")
            .replace(
                "<!-- BIMRI v5.0.2 | Generated view. Do not edit directly. -->",
                "<!-- BIMRI v5 | Generated view. Do not edit directly. -->",
            )
            .replace("Soft target: state.json.", "Cap: 12.", 1)
            .replace("Soft target: state.json.", "Cap: 20.", 1)
            .replace("Soft target: state.json.", "Cap: 8.", 1)
        )
        revision_path.write_bytes(historical.encode("utf-8"))
        (self.root / "bimri.md").write_bytes(historical.encode("utf-8"))
        state_path = self.root / ".bimri" / "state.json"
        state = self.state()
        state.update({
            "bimri_version": "5.0",
            "tier1_max": 7,
            "tier2_max": 9,
            "tier3_max": 3,
            "entry_max_chars": 240,
            "hot_max_bytes": len(historical.encode("utf-8")),
            "head_hash": hashlib.sha256(historical.encode("utf-8")).hexdigest(),
        })
        state_path.write_text(
            json.dumps(state, indent=2, sort_keys=True) + "\n", "utf-8"
        )

        upgraded = self.cli("migrate")

        current = self.state()
        normalized = (self.root / "bimri.md").read_bytes()
        self.assertIn("custom limits preserved", upgraded.stdout)
        self.assertLessEqual(len(normalized), current["hot_max_bytes"])
        self.assertIn(b"BIMRI v5.0.2", normalized)
        self.assertNotIn(b"Cap: 12", normalized)
        doctor = self.cli("doctor")
        self.assertNotIn("exceeds byte cap", doctor.stdout + doctor.stderr)

    def test_current_v5_stale_metadata_preserves_manual_hot_edit(self):
        self.cli("migrate")
        revision = self.root / ".bimri" / "revisions" / "V000000.md"
        historical = (
            revision.read_text("utf-8")
            .replace(
                "<!-- BIMRI v5.0.2 | Generated view. Do not edit directly. -->",
                "<!-- BIMRI v5 | Generated view. Do not edit directly. -->",
            )
            .replace("Soft target: state.json.", "Cap: 12.", 1)
            .replace("Soft target: state.json.", "Cap: 20.", 1)
            .replace("Soft target: state.json.", "Cap: 8.", 1)
        )
        revision.write_bytes(historical.encode("utf-8"))
        state_path = self.root / ".bimri" / "state.json"
        state = self.state()
        state["head_hash"] = hashlib.sha256(
            historical.encode("utf-8")
        ).hexdigest()
        state_path.write_text(
            json.dumps(state, indent=2, sort_keys=True) + "\n", "utf-8"
        )
        direct_edit = b"Uncommitted owner edit on a current-version stale view.\n"
        (self.root / "bimri.md").write_bytes(direct_edit)

        migrated = self.cli("migrate")

        self.assertIn("direct edit to bimri.md was preserved", migrated.stderr)
        self.assertIn(
            "Memory metadata normalized in immutable revision V000001",
            migrated.stdout,
        )
        recovery = list(
            (self.root / ".bimri" / "recovery").glob("manual-hot-*.md")
        )
        self.assertEqual(len(recovery), 1)
        self.assertEqual(recovery[0].read_bytes(), direct_edit)
        self.assertEqual(self.state()["head_revision"], 1)
        normalized = (self.root / ".bimri" / "revisions" / "V000001.md")
        self.assertEqual((self.root / "bimri.md").read_bytes(), normalized.read_bytes())

    def test_current_v5_stale_metadata_conflicting_next_revision_is_fail_closed(self):
        self.cli("migrate")
        revisions = self.root / ".bimri" / "revisions"
        revision = revisions / "V000000.md"
        historical = revision.read_text("utf-8").replace(
            "<!-- BIMRI v5.0.2 | Generated view. Do not edit directly. -->",
            "<!-- BIMRI v5 | Generated view. Do not edit directly. -->",
        )
        revision.write_bytes(historical.encode("utf-8"))
        (self.root / "bimri.md").write_bytes(historical.encode("utf-8"))
        state_path = self.root / ".bimri" / "state.json"
        state = self.state()
        state["head_hash"] = hashlib.sha256(
            historical.encode("utf-8")
        ).hexdigest()
        state_bytes = (
            json.dumps(state, indent=2, sort_keys=True) + "\n"
        ).encode("utf-8")
        state_path.write_bytes(state_bytes)
        collision = b"unrelated immutable revision bytes\n"
        (revisions / "V000001.md").write_bytes(collision)

        result = self.cli("migrate", check=False)

        self.assertEqual(result.returncode, 2)
        self.assertIn("metadata revision conflicts", result.stderr)
        self.assertEqual(state_path.read_bytes(), state_bytes)
        self.assertEqual((self.root / "bimri.md").read_text("utf-8"), historical)
        self.assertEqual((revisions / "V000001.md").read_bytes(), collision)
        self.assertEqual(list((self.root / ".bimri" / "recovery").iterdir()), [])

    def test_v5_0_pointer_escape_fails_before_upgrade_authority_write(self):
        self.cli("migrate")
        state_path = self.root / ".bimri" / "state.json"
        revision_path = self.root / ".bimri" / "revisions" / "V000000.md"
        escaped = revision_path.read_text("utf-8").replace(
            "<!-- Confirmed facts, decisions, preferences and rules. Soft target: state.json. -->",
            "<!-- Confirmed facts, decisions, preferences and rules. Cap: 12. -->\n\n"
            "[R0-E1] [K:upgrade.pointer] [fact] [T:working] "
            "[SRC:legacy] [] Unsafe inherited pointer -> ../../outside.md",
        ).replace(
            "<!-- BIMRI v5.0.2 | Generated view. Do not edit directly. -->",
            "<!-- BIMRI v5 | Generated view. Do not edit directly. -->",
        ).replace(
            "<!-- Current work, risks and next actions. Soft target: state.json. -->",
            "<!-- Current work, risks and next actions. Cap: 20. -->",
        ).replace(
            "<!-- Evidence-backed patterns. Soft target: state.json. -->",
            "<!-- Evidence-backed patterns. Cap: 8. -->",
        )
        revision_path.write_bytes(escaped.encode("utf-8"))
        (self.root / "bimri.md").write_bytes(escaped.encode("utf-8"))
        state = self.state()
        state.update({
            "bimri_version": "5.0",
            "tier1_max": 12,
            "tier2_max": 20,
            "tier3_max": 8,
            "entry_max_chars": 500,
            "hot_max_bytes": 16384,
            "head_hash": hashlib.sha256(escaped.encode("utf-8")).hexdigest(),
        })
        state_bytes = (
            json.dumps(state, indent=2, sort_keys=True) + "\n"
        ).encode("utf-8")
        state_path.write_bytes(state_bytes)

        result = self.cli("migrate", check=False)

        self.assertEqual(result.returncode, 2)
        self.assertIn("pointer escapes the BIMRI project", result.stderr)
        self.assertEqual(state_path.read_bytes(), state_bytes)
        self.assertEqual(revision_path.read_text("utf-8"), escaped)
        self.assertEqual((self.root / "bimri.md").read_text("utf-8"), escaped)
        self.assertFalse(
            (self.root / ".bimri" / "revisions" / "V000001.md").exists()
        )
        self.assertEqual(
            list((self.root / ".bimri" / "backups").glob("state-v5.0-*.json")),
            [],
        )

    def test_v5_0_upgrade_preserves_direct_hot_edit_before_normalizing_view(self):
        self.cli("migrate")
        state_path = self.root / ".bimri" / "state.json"
        revision_path = self.root / ".bimri" / "revisions" / "V000000.md"
        historical = (
            revision_path.read_text("utf-8")
            .replace(
                "<!-- BIMRI v5.0.2 | Generated view. Do not edit directly. -->",
                "<!-- BIMRI v5 | Generated view. Do not edit directly. -->",
            )
            .replace("Soft target: state.json.", "Cap: 12.", 1)
            .replace("Soft target: state.json.", "Cap: 20.", 1)
            .replace("Soft target: state.json.", "Cap: 8.", 1)
        )
        revision_path.write_bytes(historical.encode("utf-8"))
        state = self.state()
        state.update({
            "bimri_version": "5.0",
            "tier1_max": 12,
            "tier2_max": 20,
            "tier3_max": 8,
            "entry_max_chars": 500,
            "hot_max_bytes": 16384,
            "head_hash": hashlib.sha256(historical.encode("utf-8")).hexdigest(),
        })
        state_path.write_text(
            json.dumps(state, indent=2, sort_keys=True) + "\n", "utf-8"
        )
        direct_edit = (
            b"Owner-authored direct edit that must survive the v5.0.2 upgrade.\n"
            b"Second byte-exact line.\n"
        )
        (self.root / "bimri.md").write_bytes(direct_edit)

        upgraded = self.cli("migrate")

        self.assertIn("direct edit to bimri.md was preserved", upgraded.stderr)
        recovery_files = list(
            (self.root / ".bimri" / "recovery").glob("manual-hot-*.md")
        )
        self.assertEqual(len(recovery_files), 1)
        self.assertEqual(recovery_files[0].read_bytes(), direct_edit)
        conflicts = list((self.root / ".bimri" / "conflicts").glob("C*.json"))
        self.assertEqual(len(conflicts), 1)
        conflict = json.loads(conflicts[0].read_text("utf-8"))
        self.assertEqual(conflict["type"], "manual-edit")
        self.assertEqual(
            conflict["extra"]["recovery_file"],
            recovery_files[0].relative_to(self.root).as_posix(),
        )
        normalized = (self.root / "bimri.md").read_text("utf-8")
        self.assertIn("BIMRI v5.0.2", normalized)
        self.assertIn("Capacity: state.json.", normalized)
        self.assertNotEqual(normalized.encode("utf-8"), direct_edit)
        self.assertEqual(self.state()["head_revision"], 1)

    def test_v5_0_pending_proposal_remains_consumable_after_upgrade(self):
        run_id = self.start("upgrade-agent")
        proposal_id = self.propose(
            run_id,
            "upgrade.pending",
            "This pending v5.0 proposal must survive the upgrade.",
        )
        proposal_path = self.root / ".bimri" / "proposals" / f"{proposal_id}.json"
        proposal = json.loads(proposal_path.read_text("utf-8"))
        proposal["bimri_version"] = "5.0"
        proposal_path.write_text(
            json.dumps(proposal, indent=2, sort_keys=True) + "\n", "utf-8"
        )
        state_path = self.root / ".bimri" / "state.json"
        state = self.state()
        state.update({
            "bimri_version": "5.0",
            "tier1_max": 12,
            "tier2_max": 20,
            "tier3_max": 8,
            "entry_max_chars": 500,
            "hot_max_bytes": 16384,
        })
        state_path.write_text(
            json.dumps(state, indent=2, sort_keys=True) + "\n", "utf-8"
        )

        self.cli("sync", "--run", run_id)

        self.assertEqual(self.decision(proposal_id)["outcome"], "accepted")
        self.assertIn("[K:upgrade.pending]", self.hot())
        self.assertEqual(self.state()["bimri_version"], "5.1.0")
        self.assertIn("BIMRI doctor: PASSED", self.cli("doctor").stdout)

    def test_installer_migrates_long_legacy_claim_with_receipt_and_repair_path(self):
        target = self.root / "legacy target"
        target.mkdir()
        long_claim = "Preserve this inherited context exactly: " + ("x" * 700)
        source = self.legacy_v3_bytes(long_claim)
        (target / "BIMRI.md").write_bytes(source)

        install = subprocess.run(
            [sys.executable, str(ENGINE), "install", "--target", str(target)],
            text=True,
            capture_output=True,
            timeout=45,
        )

        self.assertEqual(install.returncode, 0, install.stdout + install.stderr)
        self.assertIn(
            "Memory: migrated BIMRI v3 from BIMRI.md to v5.1.0.",
            install.stdout,
        )
        self.assertIn("Tier 1 1; Tier 2 0; Tier 3 0; total 1", install.stdout)
        self.assertIn("inherited overlength 1", install.stdout)
        self.assertIn("Migration record: .bimri/migrations/legacy-to-v5.json", install.stdout)
        self.assertIn(long_claim, self.hot(root=target))
        doctor = self.cli("doctor", root=target)
        self.assertIn("BIMRI doctor: PASSED", doctor.stdout)
        self.assertIn("inherited legacy text exceeds active entry cap", doctor.stdout)

        run_id = self.start("repair-agent", root=target)
        oversize = self.cli(
            "propose",
            "--run",
            run_id,
            "--tier",
            "2",
            "--key",
            "new.too-long",
            "--text",
            "y" * 501,
            root=target,
            check=False,
        )
        self.assertEqual(oversize.returncode, 2)
        self.assertIn("exceeds 500 characters", oversize.stderr)
        replacement = self.propose(
            run_id,
            "legacy.v3.t1-0001",
            "Condensed inherited context.",
            tier=1,
            source="user",
            trust="confirmed",
            extra=("--target", "R0-E1"),
            root=target,
        )
        self.cli("sync", "--run", run_id, root=target)
        self.assertEqual(
            self.decision(replacement, root=target)["outcome"], "accepted"
        )
        repaired = self.cli("doctor", root=target)
        self.assertNotIn(
            "inherited legacy text exceeds active entry cap", repaired.stdout
        )

        too_large_root = self.root / "serialized-ceiling"
        too_large_root.mkdir()
        too_large_source = self.legacy_v3_bytes("z" * 4096)
        (too_large_root / "BIMRI.md").write_bytes(too_large_source)
        refused = self.cli("migrate", root=too_large_root, check=False)
        self.assertEqual(refused.returncode, 2)
        self.assertIn("serialized entry exceeds 4096 characters", refused.stderr)
        self.assertEqual(
            (too_large_root / "BIMRI.md").read_bytes(), too_large_source
        )
        self.assertFalse((too_large_root / ".bimri" / "state.json").exists())

    def test_valid_state_refuses_distinct_unclaimed_legacy_root(self):
        self.cli("migrate")
        probe = self.root / "case-probe"
        probe.write_text("probe", "utf-8")
        case_insensitive = (self.root / "CASE-PROBE").exists()
        probe.unlink()
        if case_insensitive:
            return

        state_path = self.root / ".bimri" / "state.json"
        revision_path = self.root / ".bimri" / "revisions" / "V000000.md"
        state_before = state_path.read_bytes()
        hot_before = (self.root / "bimri.md").read_bytes()
        revision_before = revision_path.read_bytes()
        legacy = self.legacy_v3_bytes("The owner must choose this lineage.")
        upper = self.root / "BIMRI.md"
        upper.write_bytes(legacy)

        result = self.cli("doctor", check=False)

        self.assertEqual(result.returncode, 2)
        self.assertIn("unclaimed legacy root file", result.stderr)
        self.assertEqual(state_path.read_bytes(), state_before)
        self.assertEqual((self.root / "bimri.md").read_bytes(), hot_before)
        self.assertEqual(revision_path.read_bytes(), revision_before)
        self.assertEqual(upper.read_bytes(), legacy)

    def test_v5_0_crash_revision_resumes_with_historical_conversion_bytes(self):
        claim = "Resume the historical converter without rewriting its revision."
        source = self.legacy_v3_bytes(claim)
        (self.root / "BIMRI.md").write_bytes(source)
        revisions = self.root / ".bimri" / "revisions"
        revisions.mkdir(parents=True)
        historical = (
            "# BIMRI Memory\n\n"
            "<!-- BIMRI v5 | Generated view. Do not edit directly. -->\n"
            "<!-- Full history: .bimri/log/ | Revisions: .bimri/revisions/ -->\n\n"
            "## Tier 1: Core Intelligence\n\n"
            "<!-- Confirmed facts, decisions, preferences and rules. Cap: 12. -->\n\n"
            "[R0-E1] [K:legacy.v3.t1-0001] [fact] [T:working] "
            f"[SRC:legacy] [] {claim}\n\n"
            "## Tier 2: Active Context\n\n"
            "<!-- Current work, risks and next actions. Cap: 20. -->\n\n"
            "## Tier 3: Pattern Recognition\n\n"
            "<!-- Evidence-backed patterns. Cap: 8. -->\n\n"
            "<!-- END BIMRI -->\n"
        ).encode("utf-8")
        revision = revisions / "V000000.md"
        revision.write_bytes(historical)

        resumed = self.cli("migrate")

        self.assertEqual(revision.read_bytes(), historical)
        marker = json.loads(
            (self.root / ".bimri" / "migrations" / "legacy-to-v5.json").read_text(
                "utf-8"
            )
        )
        self.assertEqual(marker["converter_version"], "5.0")
        self.assertIn("Memory: migrated BIMRI v3", resumed.stdout)
        self.assertIn(
            "Memory metadata normalized in immutable revision V000001",
            resumed.stdout,
        )
        state = self.state()
        self.assertEqual(state["bimri_version"], "5.1.0")
        self.assertEqual(state["head_revision"], 1)
        normalized = (revisions / "V000001.md").read_bytes()
        self.assertEqual((self.root / "bimri.md").read_bytes(), normalized)
        self.assertIn(b"BIMRI v5.0.2", normalized)
        self.assertIn(b"Capacity: state.json.", normalized)
        self.assertNotIn(b"Cap: 12", normalized)
        self.assertEqual(normalized.count(claim.encode("utf-8")), 1)
        self.assertIn("BIMRI doctor: PASSED", self.cli("doctor").stdout)
        verified = self.cli("migrate")
        self.assertIn("no migration performed", verified.stdout)
        self.assertEqual((revisions / "V000001.md").read_bytes(), normalized)
        self.assertFalse((revisions / "V000002.md").exists())

        # A completed v5.0 legacy migration must retire sources first, retain
        # its exact state backup, and then use the normal v5.0 upgrade path.
        (revisions / "V000001.md").unlink()
        old_state = self.state()
        old_state.update({
            "bimri_version": "5.0",
            "head_revision": 0,
            "head_hash": hashlib.sha256(historical).hexdigest(),
            "tier1_max": 12,
            "tier2_max": 20,
            "tier3_max": 8,
            "entry_max_chars": 500,
            "hot_max_bytes": 16384,
        })
        old_state_bytes = (
            json.dumps(old_state, indent=2, sort_keys=True) + "\n"
        ).encode("utf-8")
        (self.root / ".bimri" / "state.json").write_bytes(old_state_bytes)
        (self.root / "bimri.md").write_bytes(historical)

        upgraded = self.cli("migrate")

        self.assertIn("Memory: upgraded v5.0 to v5.1.0", upgraded.stdout)
        state_backups = list(
            (self.root / ".bimri" / "backups").glob("state-v5.0-*.json")
        )
        self.assertEqual(len(state_backups), 1)
        self.assertEqual(state_backups[0].read_bytes(), old_state_bytes)
        self.assertEqual(revision.read_bytes(), historical)
        self.assertEqual((revisions / "V000001.md").read_bytes(), normalized)
        self.assertEqual(self.state()["head_revision"], 1)

    def test_missing_authority_records_use_absence_stubs_and_restore(self):
        def recover_missing(root, kind, record_id, path, valid_bytes):
            unhealthy = self.cli("status", root=root, check=False)
            self.assertEqual(unhealthy.returncode, 1)
            self.assertIn(record_id, unhealthy.stdout + unhealthy.stderr)

            quarantined = self.cli(
                "quarantine-authority",
                "--kind",
                kind,
                "--id",
                record_id,
                "--human-approved",
                root=root,
            )
            self.assertIn("exact absence evidence preserved", quarantined.stdout)
            stub = json.loads(path.read_text("utf-8"))
            self.assertEqual(stub["original_type"], "missing")
            evidence = json.loads(
                (root / stub["recovery_file"]).read_text("utf-8")
            )
            self.assertEqual(evidence, {
                "evidence_type": "missing-authority-record",
                "original_path": path.relative_to(root).as_posix(),
            })

            repair = root / f"reviewed-{kind}-{record_id}.json"
            repair.write_bytes(valid_bytes)
            restored = self.cli(
                "restore-authority",
                "--kind",
                kind,
                "--id",
                record_id,
                "--from",
                repair,
                "--human-approved",
                root=root,
            )
            self.assertIn("restored validated", restored.stdout)
            self.assertEqual(path.read_bytes(), valid_bytes)

        proposal_root = self.root / "missing-proposal"
        proposal_run = self.start("missing-proposal", root=proposal_root)
        proposal_id = self.propose(
            proposal_run,
            "missing.proposal",
            "A durable log reference must make this absence recoverable.",
            root=proposal_root,
        )
        proposal_path = (
            proposal_root
            / ".bimri"
            / "proposals"
            / f"{proposal_id}.json"
        )
        valid_proposal = proposal_path.read_bytes()
        proposal_path.unlink()
        recover_missing(
            proposal_root,
            "proposal",
            proposal_id,
            proposal_path,
            valid_proposal,
        )

        decision_root = self.root / "missing-closed-decision"
        decision_run = self.start("missing-decision", root=decision_root)
        decision_id = self.propose(
            decision_run,
            "missing.decision",
            "A closed run makes its terminal decision durable authority.",
            root=decision_root,
        )
        self.cli("sync", "--run", decision_run, root=decision_root)
        self.cli(
            "close",
            "--run",
            decision_run,
            "--outcome",
            "success",
            "--summary",
            "Close the run before deleting its decision.",
            root=decision_root,
        )
        decision_path = (
            decision_root
            / ".bimri"
            / "decisions"
            / f"{decision_id}.json"
        )
        valid_decision = decision_path.read_bytes()
        decision_path.unlink()
        recover_missing(
            decision_root,
            "decision",
            decision_id,
            decision_path,
            valid_decision,
        )

        conflict_root = self.root / "missing-conflict"
        conflict_run, candidate, _writer_run, _writer = (
            self.stage_concurrent_candidate(
                "missing.conflict",
                "The durable conflict counter must anchor this record.",
                root=conflict_root,
                candidate_actor="missing-conflict",
                writer_text=(
                    "A concurrent writer establishes the accepted value."
                ),
            )
        )
        self.cli("sync", "--run", conflict_run, root=conflict_root)
        conflict_id = self.decision(candidate, root=conflict_root)["conflict_id"]
        conflict_path = (
            conflict_root
            / ".bimri"
            / "conflicts"
            / f"{conflict_id}.json"
        )
        valid_conflict = conflict_path.read_bytes()
        conflict_path.unlink()
        recover_missing(
            conflict_root,
            "conflict",
            conflict_id,
            conflict_path,
            valid_conflict,
        )
        self.assertIn(
            "Open conflicts: 1",
            self.cli("status", root=conflict_root).stdout,
        )

    def test_unreferenced_missing_authority_id_is_refused_without_mutation(self):
        self.cli("migrate")

        def snapshot():
            return {
                path.relative_to(self.root).as_posix(): path.read_bytes()
                for path in self.root.rglob("*")
                if (
                    path.is_file()
                    and not path.is_symlink()
                    and path != self.root / ".bimri" / "audit-witness.json"
                )
            }

        before = snapshot()
        refused = self.cli(
            "quarantine-authority",
            "--kind",
            "conflict",
            "--id",
            "C000999",
            "--human-approved",
            check=False,
        )

        self.assertEqual(refused.returncode, 2)
        self.assertIn("has no durable BIMRI reference", refused.stderr)
        self.assertEqual(snapshot(), before)
        self.assertFalse(
            (self.root / ".bimri" / "conflicts" / "C000999.json").exists()
        )
        self.assertEqual(
            list(
                (self.root / ".bimri" / "recovery").glob(
                    "authority-conflict-C000999-*"
                )
            ),
            [],
        )

    def test_deleted_log_referenced_proposal_blocks_without_reusing_its_id(self):
        run_id = self.start("proposal-anchor")
        first = self.propose(
            run_id,
            "proposal.anchor.first",
            "The first proposal ID remains durable in the run log.",
        )
        self.assertEqual(first, f"{run_id}-Q001")
        first_path = (
            self.root / ".bimri" / "proposals" / f"{first}.json"
        )
        first_path.unlink()

        blocked = self.cli("status", check=False)
        self.assertEqual(blocked.returncode, 1)
        self.assertIn(
            f"proposal {first} is missing despite its durable reference",
            blocked.stdout,
        )

        second = self.propose(
            run_id,
            "proposal.anchor.second",
            "A later proposal must skip the deleted durable ID.",
        )
        self.assertEqual(second, f"{run_id}-Q002")
        self.assertTrue(
            (self.root / ".bimri" / "proposals" / f"{second}.json").is_file()
        )
        sync = self.cli("sync", "--run", run_id, check=False)
        self.assertEqual(sync.returncode, 2)
        self.assertIn("authority recovery is required", sync.stderr)

    def test_missing_conflict_decision_requires_explicit_recovery(self):
        run_id, candidate, _writer_run, _writer = (
            self.stage_concurrent_candidate(
                "candidate.decision",
                "A conflict candidate decision may be recreated only safely.",
                candidate_actor="candidate-decision",
                writer_text=(
                    "A concurrent writer establishes the accepted value."
                ),
            )
        )
        self.cli("sync", "--run", run_id)
        decision_path = (
            self.root / ".bimri" / "decisions" / f"{candidate}.json"
        )
        decision_bytes = decision_path.read_bytes()
        decision_path.unlink()

        # A durably processed proposal cannot silently recreate missing
        # authority, even while its writer is active and its log is safe.
        active = self.cli("status", check=False)
        self.assertEqual(active.returncode, 1, active.stdout + active.stderr)
        self.assertIn(
            f"decision {candidate} is missing after its proposal was "
            "durably processed",
            active.stdout + active.stderr,
        )

        log_path = self.root / ".bimri" / "log" / f"{run_id}.md"
        saved_log = self.root / f"saved-{run_id}.md"
        log_path.replace(saved_log)
        try:
            log_path.symlink_to(saved_log)
        except (NotImplementedError, OSError) as exc:
            saved_log.replace(log_path)
            self.skipTest(f"symbolic links unavailable: {exc}")

        unsafe = self.cli("status", check=False)
        self.assertNotEqual(unsafe.returncode, 0)
        unsafe_output = unsafe.stdout + unsafe.stderr
        self.assertTrue(
            f"candidate {candidate} has no safe decision record" in unsafe_output
            or "run log cannot be a symbolic link" in unsafe_output,
            unsafe_output,
        )
        log_path.unlink()
        saved_log.replace(log_path)

        blocked = self.cli("sync", "--run", run_id, check=False)
        self.assertEqual(blocked.returncode, 2)
        self.assertIn(
            "authority recovery is required",
            blocked.stdout + blocked.stderr,
        )
        self.assertNotIn(
            "MEMORY CONFLICT",
            blocked.stdout + blocked.stderr,
        )

        self.cli(
            "quarantine-authority",
            "--kind",
            "decision",
            "--id",
            candidate,
            "--human-approved",
        )
        repair = self.root / "missing-conflict-decision-repair.json"
        repair.write_bytes(decision_bytes)
        self.cli(
            "restore-authority",
            "--kind",
            "decision",
            "--id",
            candidate,
            "--from",
            repair,
            "--human-approved",
        )
        self.assertEqual(self.decision(candidate)["outcome"], "contested")
        self.cli(
            "close",
            "--run",
            run_id,
            "--outcome",
            "success",
            "--summary",
            "The conflict remains open after this run closes.",
        )
        decision_path.unlink()

        inactive = self.cli("status", check=False)
        self.assertEqual(inactive.returncode, 1)
        self.assertIn(
            f"candidate {candidate} has no safe decision record",
            inactive.stdout + inactive.stderr,
        )

    def test_linked_missing_final_decision_and_resolution_restore_in_any_order(self):
        run_id, candidate, _writer_run, _writer = (
            self.stage_concurrent_candidate(
                "linked.missing",
                "A human-approved candidate anchors the missing resolution.",
                candidate_actor="linked-missing",
                writer_text=(
                    "A concurrent writer establishes the accepted value."
                ),
            )
        )
        self.cli("sync", "--run", run_id)
        conflict_id = self.decision(candidate)["conflict_id"]
        self.cli(
            "resolve",
            conflict_id,
            "--choose",
            candidate,
            "--human-approved",
        )
        decision_path = (
            self.root / ".bimri" / "decisions" / f"{candidate}.json"
        )
        resolution_path = (
            self.root / ".bimri" / "resolutions" / f"{conflict_id}.json"
        )
        decision_bytes = decision_path.read_bytes()
        resolution_bytes = resolution_path.read_bytes()
        decision_path.unlink()
        resolution_path.unlink()

        # The conflict snapshot plus the accepted candidate effect in HEAD is
        # durable evidence for this exact missing resolution ID.
        self.cli(
            "quarantine-authority",
            "--kind",
            "resolution",
            "--id",
            conflict_id,
            "--human-approved",
        )
        self.cli(
            "quarantine-authority",
            "--kind",
            "decision",
            "--id",
            candidate,
            "--human-approved",
        )
        decision_repair = self.root / "linked-decision-repair.json"
        resolution_repair = self.root / "linked-resolution-repair.json"
        decision_repair.write_bytes(decision_bytes)
        resolution_repair.write_bytes(resolution_bytes)

        staged = self.cli(
            "restore-authority",
            "--kind",
            "decision",
            "--id",
            candidate,
            "--from",
            decision_repair,
            "--human-approved",
        )
        self.assertIn("restored staged", staged.stdout)
        completed = self.cli(
            "restore-authority",
            "--kind",
            "resolution",
            "--id",
            conflict_id,
            "--from",
            resolution_repair,
            "--human-approved",
        )
        self.assertIn("restored validated", completed.stdout)
        self.assertEqual(self.cli("doctor").returncode, 0)

    def test_closed_log_anchors_linked_missing_proposal_and_decision(self):
        run_id = self.start("closed-linked-missing")
        proposal_id = self.propose(
            run_id,
            "closed.linked.missing",
            "The closed log anchors both authority records.",
        )
        self.cli("sync", "--run", run_id)
        self.cli(
            "close",
            "--run",
            run_id,
            "--outcome",
            "success",
            "--summary",
            "Both processed records are now durably expected.",
        )
        proposal_path = (
            self.root / ".bimri" / "proposals" / f"{proposal_id}.json"
        )
        decision_path = (
            self.root / ".bimri" / "decisions" / f"{proposal_id}.json"
        )
        proposal_bytes = proposal_path.read_bytes()
        decision_bytes = decision_path.read_bytes()
        proposal_path.unlink()
        decision_path.unlink()

        for kind in ("proposal", "decision"):
            self.cli(
                "quarantine-authority",
                "--kind",
                kind,
                "--id",
                proposal_id,
                "--human-approved",
            )
        proposal_repair = self.root / "closed-proposal-repair.json"
        decision_repair = self.root / "closed-decision-repair.json"
        proposal_repair.write_bytes(proposal_bytes)
        decision_repair.write_bytes(decision_bytes)
        staged = self.cli(
            "restore-authority",
            "--kind",
            "proposal",
            "--id",
            proposal_id,
            "--from",
            proposal_repair,
            "--human-approved",
        )
        self.assertIn("restored staged", staged.stdout)
        completed = self.cli(
            "restore-authority",
            "--kind",
            "decision",
            "--id",
            proposal_id,
            "--from",
            decision_repair,
            "--human-approved",
        )
        self.assertIn("restored validated", completed.stdout)
        self.assertEqual(self.cli("doctor").returncode, 0)

    def test_sync_backfills_proposal_log_anchor_before_deciding(self):
        run_id = self.start("marker-backfill")
        proposal_id = self.propose(
            run_id,
            "marker.backfill",
            "The proposal survives the pre-marker crash window.",
        )
        log_path = self.root / ".bimri" / "log" / f"{run_id}.md"
        marker = f"[PROPOSE:{proposal_id}]"
        lines = [
            line
            for line in log_path.read_text("utf-8").splitlines()
            if marker not in line
        ]
        log_path.write_text("\n".join(lines) + "\n", "utf-8")

        self.cli("sync", "--run", run_id)
        repaired_log = log_path.read_text("utf-8")
        self.assertEqual(repaired_log.count(marker), 1)
        self.assertEqual(self.decision(proposal_id)["outcome"], "accepted")
        self.cli(
            "close",
            "--run",
            run_id,
            "--outcome",
            "success",
            "--summary",
            "The proposal marker is now durable.",
        )
        (
            self.root / ".bimri" / "decisions" / f"{proposal_id}.json"
        ).unlink()
        status = self.cli("status", check=False)
        self.assertEqual(status.returncode, 1)
        self.assertIn(
            f"decision {proposal_id} is missing after its proposal was "
            "durably processed",
            status.stdout,
        )

    def test_engine_release_is_separate_from_memory_format(self):
        self.cli("migrate")
        state = self.state()
        self.assertEqual(state["bimri_version"], "5.1.0")
        self.assertIn("<!-- BIMRI v5.0.2 |", self.hot())
        status = self.cli("status")
        self.assertIn(
            "BIMRI engine v5.1.1 | memory format v5.1.0 | revision V000000",
            status.stdout,
        )

    def test_read_only_doctor_never_heals_or_indexes_current_store(self):
        self.cli("migrate")
        hot_path = self.root / "bimri.md"
        edited = hot_path.read_bytes() + b"\nowner direct edit must survive\n"
        hot_path.write_bytes(edited)
        index_path = self.root / ".bimri" / "index.tsv"
        index_path.write_bytes(b"custom index bytes\n")
        before = protected_tree_snapshot(self.root)

        result = self.cli("doctor", "--read-only", check=False)

        self.assertEqual(result.returncode, 1)
        self.assertIn("read-only doctor left the file unchanged", result.stdout)
        self.assertEqual(hot_path.read_bytes(), edited)
        self.assertEqual(index_path.read_bytes(), b"custom index bytes\n")
        self.assertEqual(protected_tree_snapshot(self.root), before)
        self.assertEqual(
            list((self.root / ".bimri" / "recovery").glob("manual-hot-*")),
            [],
        )

    def test_read_only_doctor_and_updater_attempt_zero_protected_mutations(self):
        self.cli("migrate")
        protected = protected_tree_snapshot(self.root)

        audit = self.worker(
            "protected_mutation_monitor",
            "doctor",
            "--read-only",
            root=self.root,
            timeout=60,
        )

        self.assertIn("BIMRI doctor (read-only): PASSED", audit.stdout)
        self.assertNotIn("protected mutation monitor", audit.stderr)
        self.assertEqual(protected_tree_snapshot(self.root), protected)

        installed = self.worker(
            "protected_mutation_monitor",
            "install",
            "--target",
            self.root,
            "--quiescent",
            engine_root=REPOSITORY,
            timeout=60,
        )

        self.assertIn("Memory preservation: PASSED", installed.stdout)
        self.assertNotIn("protected mutation monitor", installed.stderr)
        self.assertEqual(protected_tree_snapshot(self.root), protected)

    @unittest.skipIf(os.name == "nt", "symlink creation requires privileges")
    def test_read_only_audit_rejects_redirected_revision_directory(self):
        self.cli("migrate")
        program = self.root / "bimri-engine.py"
        program.write_bytes(b"old installed engine sentinel\n")
        with tempfile.TemporaryDirectory(
            prefix="bimri-external-revisions-"
        ) as temporary:
            external = Path(temporary) / "revisions"
            revisions = self.root / ".bimri" / "revisions"
            revisions.rename(external)
            revisions.symlink_to(external, target_is_directory=True)
            external_before = {
                path.name: path.read_bytes()
                for path in external.iterdir()
                if path.is_file()
            }
            protected = protected_tree_snapshot(self.root)

            audit = self.cli("doctor", "--read-only", check=False)
            self.assertEqual(audit.returncode, 1)
            self.assertIn(
                ".bimri/revisions cannot be a symbolic link", audit.stdout
            )
            self.assertEqual(protected_tree_snapshot(self.root), protected)

            installed = self.cli(
                "install",
                "--target",
                self.root,
                "--quiescent",
                root=REPOSITORY,
                check=False,
                timeout=60,
            )
            self.assertEqual(installed.returncode, 2)
            self.assertIn(
                ".bimri/revisions cannot be a symbolic link", installed.stderr
            )
            self.assertEqual(
                program.read_bytes(), b"old installed engine sentinel\n"
            )
            self.assertEqual(protected_tree_snapshot(self.root), protected)
            self.assertEqual(
                {
                    path.name: path.read_bytes()
                    for path in external.iterdir()
                    if path.is_file()
                },
                external_before,
            )

    @unittest.skipUnless(os.name == "nt", "NTFS junction regression")
    def test_code_update_rejects_backup_root_junction_before_write(self):
        self.cli("migrate")
        program = self.root / "bimri-engine.py"
        program.write_bytes(b"old installed engine sentinel\n")
        recovery = self.root / ".bimri" / "recovery"
        backup_root = self.root / ".bimri-update-backups"
        created = subprocess.run(
            ["cmd", "/c", "mklink", "/J", str(backup_root), str(recovery)],
            text=True,
            capture_output=True,
            timeout=30,
        )
        self.assertEqual(created.returncode, 0, created.stderr)
        protected = protected_tree_snapshot(self.root)
        recovery_before = sorted(path.name for path in recovery.iterdir())
        try:
            result = self.cli(
                "install",
                "--target",
                self.root,
                "--quiescent",
                root=REPOSITORY,
                check=False,
                timeout=60,
            )

            self.assertEqual(result.returncode, 2)
            self.assertIn("reparse point", result.stderr)
            self.assertEqual(
                program.read_bytes(), b"old installed engine sentinel\n"
            )
            self.assertEqual(protected_tree_snapshot(self.root), protected)
            self.assertEqual(
                sorted(path.name for path in recovery.iterdir()),
                recovery_before,
            )
        finally:
            os.rmdir(backup_root)

    @unittest.skipIf(os.name == "nt", "covered by the junction regression")
    def test_code_update_rejects_redirected_backup_root_before_write(self):
        self.cli("migrate")
        program = self.root / "bimri-engine.py"
        program.write_bytes(b"old installed engine sentinel\n")
        recovery = self.root / ".bimri" / "recovery"
        backup_root = self.root / ".bimri-update-backups"
        backup_root.symlink_to(recovery, target_is_directory=True)
        protected = protected_tree_snapshot(self.root)
        recovery_before = sorted(path.name for path in recovery.iterdir())

        result = self.cli(
            "install",
            "--target",
            self.root,
            "--quiescent",
            root=REPOSITORY,
            check=False,
            timeout=60,
        )

        self.assertEqual(result.returncode, 2)
        self.assertIn("symbolic link or reparse point", result.stderr)
        self.assertEqual(
            program.read_bytes(), b"old installed engine sentinel\n"
        )
        self.assertEqual(protected_tree_snapshot(self.root), protected)
        self.assertEqual(
            sorted(path.name for path in recovery.iterdir()), recovery_before
        )

    def test_current_store_update_requires_quiescent_attestation(self):
        self.cli("migrate")
        program = self.root / "bimri-engine.py"
        program.write_text("old installed engine sentinel\n", "utf-8")
        before = protected_tree_snapshot(self.root)

        result = self.cli(
            "install", "--target", self.root, check=False, root=REPOSITORY
        )

        self.assertEqual(result.returncode, 2)
        self.assertIn("requires an externally verified quiescent handoff", result.stderr)
        self.assertEqual(program.read_text("utf-8"), "old installed engine sentinel\n")
        self.assertEqual(protected_tree_snapshot(self.root), before)
        self.assertFalse((self.root / ".bimri-update-backups").exists())

    def test_exact_v5_0_2_waiter_proves_external_quiescence_is_required(self):
        self.cli("migrate")
        historical = subprocess.run(
            [
                "git",
                "show",
                f"{V5_0_2_COMMIT}:bimri-engine.py",
            ],
            cwd=str(REPOSITORY),
            capture_output=True,
            timeout=30,
        )
        self.assertEqual(
            historical.returncode,
            0,
            "the test checkout must include the exact v5.0.2 generator commit",
        )
        self.assertEqual(
            hashlib.sha256(historical.stdout).hexdigest(),
            V5_0_2_ENGINE_SHA256,
        )
        old_engine = self.root / "bimri-engine.py"
        old_engine.write_bytes(historical.stdout)
        protected_before = protected_tree_snapshot(self.root)
        signal = self.root / ".test-candidate-lock-held"
        release = self.root / ".test-release-candidate-lock"
        candidate_command = [
            sys.executable,
            str(CRASH_WORKER),
            str(ENGINE),
            "code_update_hold_lock",
            str(REPOSITORY),
            "install",
            "--target",
            str(self.root),
            "--quiescent",
        ]
        candidate = subprocess.Popen(
            candidate_command,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        deadline = time.monotonic() + 10
        while not signal.exists() and candidate.poll() is None:
            if time.monotonic() >= deadline:
                candidate.kill()
                self.fail("candidate updater did not acquire the existing lock")
            time.sleep(0.01)
        self.assertTrue(signal.exists())

        old_waiter = subprocess.Popen(
            [
                sys.executable,
                str(old_engine),
                "--root",
                str(self.root),
                "start",
                "--actor",
                "exact-v502-waiter",
            ],
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        time.sleep(0.25)
        self.assertIsNone(
            old_waiter.poll(),
            "the already-loaded v5.0.2 process should be waiting on the lock",
        )
        self.assertEqual(
            protected_tree_snapshot(self.root), protected_before
        )

        release.write_text("release candidate\n", encoding="utf-8")
        candidate_stdout, candidate_stderr = candidate.communicate(timeout=60)
        old_stdout, old_stderr = old_waiter.communicate(timeout=60)
        self.assertEqual(candidate.returncode, 0, candidate_stderr)
        self.assertIn("Memory preservation: PASSED", candidate_stdout)
        self.assertEqual(old_waiter.returncode, 2, old_stdout + old_stderr)
        self.assertIn(
            "unsupported BIMRI state version: 5.1.0",
            old_stdout + old_stderr,
        )
        self.assertNotIn("BIMRI RUN HANDLE:", old_stdout)
        after_old_waiter = protected_tree_snapshot(self.root)
        self.assertEqual(after_old_waiter, protected_before)

        signal.unlink()
        release.unlink()
        supported = self.cli(
            "install",
            "--target",
            self.root,
            "--quiescent",
            root=REPOSITORY,
            timeout=60,
        )
        self.assertIn("Memory preservation: PASSED", supported.stdout)
        self.assertEqual(
            protected_tree_snapshot(self.root), after_old_waiter
        )

    def test_code_only_update_preserves_complete_current_store_tree(self):
        self.cli("migrate")
        run_id = self.start("fixture-writer")
        self.propose(
            run_id,
            "fixture.subject",
            "Populated v5.0.2 memory remains exact.",
            source="agent",
            trust="working",
        )
        self.cli("sync", "--run", run_id)
        bdir = self.root / ".bimri"
        head = self.state()["head_revision"]
        shutil.copyfile(
            bdir / "revisions" / f"V{head:06d}.md",
            bdir / "revisions" / "V000099.md",
        )
        (bdir / "unknown-owner-file.dat").write_bytes(b"unknown bytes\x00\xff")
        (bdir / "install-backups" / "historical").mkdir(parents=True)
        (bdir / "install-backups" / "historical" / "receipt.txt").write_text(
            "protected old installer receipt\n", "utf-8"
        )
        (bdir / "recovery" / ".bimri-tmp-recovery-litter").write_bytes(
            b"recovery litter"
        )
        before = protected_tree_snapshot(self.root)

        result = self.cli(
            "install",
            "--target",
            self.root,
            "--quiescent",
            root=REPOSITORY,
            timeout=60,
        )

        self.assertIn("BIMRI 5.1.1 installed.", result.stdout)
        self.assertIn("Existing authority store v5.1.0 verified", result.stdout)
        self.assertIn("Memory preservation: PASSED", result.stdout)
        self.assertEqual(protected_tree_snapshot(self.root), before)
        self.assertEqual(self.state()["bimri_version"], "5.1.0")
        runtime = json.loads(
            (bdir / "runtime.local.json").read_text("utf-8")
        )
        self.assertEqual(runtime["version"], "5.1.1")
        manifests = list(
            (self.root / ".bimri-update-backups").glob(
                "*/install-manifest.json"
            )
        )
        self.assertEqual(len(manifests), 1)
        manifest = json.loads(manifests[0].read_text("utf-8"))
        self.assertEqual(manifest["engine_release"], "5.1.1")
        self.assertEqual(manifest["memory_format"], "5.1.0")
        self.assertEqual(manifest["mode"], "lossless-authority-activation")
        self.assertEqual(manifest["before_tree_digest"], manifest["after_tree_digest"])
        self.assertEqual(manifest["preservation"], "passed")
        self.assertEqual(manifest["protected_write_attempts"], 0)

        repeated = self.cli(
            "install",
            "--target",
            self.root,
            "--quiescent",
            root=REPOSITORY,
            timeout=60,
        )
        self.assertIn("Memory preservation: PASSED", repeated.stdout)
        self.assertEqual(protected_tree_snapshot(self.root), before)
        agents = (self.root / "AGENTS.md").read_text("utf-8")
        self.assertEqual(agents.count("<!-- BIMRI:START -->"), 1)
        self.assertEqual(agents.count("<!-- BIMRI:END -->"), 1)

    def test_code_only_update_preserves_sparse_store_and_direct_hot_edit(self):
        self.cli("migrate")
        bdir = self.root / ".bimri"
        for name in ("inbox", "backups", "recovery"):
            (bdir / name).rmdir()
        hot = self.root / "bimri.md"
        edited = hot.read_bytes() + b"\nmanual owner note\n"
        hot.write_bytes(edited)
        before = protected_tree_snapshot(self.root)

        result = self.cli(
            "install",
            "--target",
            self.root,
            "--quiescent",
            root=REPOSITORY,
            timeout=60,
        )

        self.assertIn("READ-ONLY AUDIT: RECOVERY REQUIRED", result.stdout)
        self.assertEqual(hot.read_bytes(), edited)
        self.assertEqual(protected_tree_snapshot(self.root), before)
        for name in ("inbox", "backups", "recovery"):
            self.assertFalse((bdir / name).exists())

    @unittest.skipIf(os.name == "nt", "symlink creation requires privileges")
    def test_code_update_preserves_unknown_protected_directory_symlink(self):
        self.cli("migrate")
        with tempfile.TemporaryDirectory(
            prefix="bimri-external-owner-dir-"
        ) as temporary:
            external = Path(temporary)
            evidence = external / "owner-evidence.bin"
            evidence.write_bytes(b"external owner evidence\x00\xff")
            link = self.root / ".bimri" / "owner-directory-link"
            link.symlink_to(external, target_is_directory=True)
            protected = protected_tree_snapshot(self.root)
            target_before = os.readlink(link)

            installed = self.cli(
                "install",
                "--target",
                self.root,
                "--quiescent",
                root=REPOSITORY,
                timeout=60,
            )

            self.assertIn("Memory preservation: PASSED", installed.stdout)
            self.assertEqual(protected_tree_snapshot(self.root), protected)
            self.assertEqual(os.readlink(link), target_before)
            self.assertEqual(
                evidence.read_bytes(), b"external owner evidence\x00\xff"
            )

    def test_completed_update_receipts_remain_valid_after_project_move(self):
        original = self.root / "original-project"
        moved = self.root / "moved-project"
        original.mkdir()
        self.cli("migrate", root=original)
        first = self.cli(
            "install",
            "--target",
            original,
            "--quiescent",
            root=REPOSITORY,
            timeout=60,
        )
        self.assertIn("Memory preservation: PASSED", first.stdout)
        original.rename(moved)
        protected = protected_tree_snapshot(moved)

        repeated = self.cli(
            "install",
            "--target",
            moved,
            "--quiescent",
            root=REPOSITORY,
            timeout=60,
        )

        self.assertIn("Memory preservation: PASSED", repeated.stdout)
        self.assertEqual(protected_tree_snapshot(moved), protected)
        runtime = json.loads(
            (moved / ".bimri" / "runtime.local.json").read_text("utf-8")
        )
        self.assertEqual(
            runtime["engine_path"], str((moved / "bimri-engine.py").resolve())
        )
        manifests = list((
            moved / ".bimri-update-backups"
        ).glob("*/install-manifest.json"))
        self.assertEqual(len(manifests), 2)

    def test_terminal_receipt_accepts_historical_target_from_other_os(self):
        self.cli("migrate")
        first = self.cli(
            "install",
            "--target",
            self.root,
            "--quiescent",
            root=REPOSITORY,
            timeout=60,
        )
        self.assertIn("Memory preservation: PASSED", first.stdout)
        protected = protected_tree_snapshot(self.root)
        manifest_path = next((
            self.root / ".bimri-update-backups"
        ).glob("*/install-manifest.json"))
        manifest = json.loads(manifest_path.read_text("utf-8"))
        manifest["target"] = (
            "/home/owner/project"
            if os.name == "nt"
            else "C:\\Users\\owner\\project"
        )
        manifest_path.write_text(
            json.dumps(manifest, indent=2, sort_keys=True) + "\n", "utf-8"
        )

        repeated = self.cli(
            "install",
            "--target",
            self.root,
            "--quiescent",
            root=REPOSITORY,
            timeout=60,
        )

        self.assertIn("Memory preservation: PASSED", repeated.stdout)
        self.assertEqual(protected_tree_snapshot(self.root), protected)

    def test_committed_v5_0_2_fixture_is_exactly_preserved_by_update(self):
        shutil.copytree(V5_0_2_FIXTURE, self.root, dirs_exist_ok=True)
        fixture_manifest = json.loads(
            (self.root / "FIXTURE-MANIFEST.json").read_text("utf-8")
        )
        self.assertEqual(
            fixture_manifest["generator_commit"],
            "dfdd3ccdacdc1e13f34ffd6b1d66b4a73d5373bb",
        )
        before = protected_tree_snapshot(self.root)
        digest = hashlib.sha256(json.dumps(
            before, sort_keys=True, separators=(",", ":")
        ).encode("utf-8")).hexdigest()
        self.assertEqual(len(before), fixture_manifest["protected_path_count"])
        self.assertEqual(digest, fixture_manifest["protected_tree_digest"])

        audit = self.cli("doctor", "--read-only", check=False)
        self.assertNotEqual(audit.returncode, 0)
        self.assertIn(
            "read-only audit requires memory format v5.1.0; found 5.0.2",
            audit.stdout + audit.stderr,
        )
        self.assertEqual(protected_tree_snapshot(self.root), before)

        installed = self.cli(
            "install",
            "--target",
            self.root,
            "--quiescent",
            root=REPOSITORY,
            timeout=60,
        )

        self.assertIn("Memory preservation: PASSED", installed.stdout)
        after = protected_tree_snapshot(self.root)
        for relative, fingerprint in before.items():
            if relative == ".bimri/state.json":
                continue
            self.assertEqual(after[relative], fingerprint, relative)
        state = self.state()
        self.assertEqual(state["bimri_version"], "5.1.0")
        self.assertEqual(
            state["head_revision"], fixture_manifest["accepted_head_revision"]
        )
        self.assertEqual(
            state["head_hash"], fixture_manifest["accepted_head_hash"]
        )
        activated_audit = self.cli("doctor", "--read-only")
        self.assertIn(
            "BIMRI doctor (read-only): PASSED", activated_audit.stdout
        )

    def test_code_update_caught_failure_rolls_back_authorized_files_only(self):
        self.cli("migrate")
        program = self.root / "bimri-engine.py"
        protocol = self.root / "BIMRI-PROTOCOL.md"
        program.write_text("old engine sentinel\n", "utf-8")
        protocol.write_text("old protocol sentinel\n", "utf-8")
        before = protected_tree_snapshot(self.root)

        result = self.worker(
            "code_update_fail_before_engine",
            "install",
            "--target",
            self.root,
            "--quiescent",
            engine_root=REPOSITORY,
            check=False,
            timeout=60,
        )

        self.assertEqual(result.returncode, 2)
        self.assertIn("forced failure before engine replacement", result.stderr)
        self.assertEqual(program.read_text("utf-8"), "old engine sentinel\n")
        self.assertEqual(protocol.read_text("utf-8"), "old protocol sentinel\n")
        self.assertEqual(protected_tree_snapshot(self.root), before)
        manifests = list((
            self.root / ".bimri-update-backups"
        ).glob("*/install-manifest.json"))
        self.assertEqual(len(manifests), 1)
        manifest = json.loads(manifests[0].read_text("utf-8"))
        self.assertEqual(manifest["status"], "rolled-back")
        self.assertEqual(manifest["preservation"], "passed")

    def test_every_caught_install_replacement_restores_all_authorized_files(self):
        for fault_index in range(1, len(CODE_UPDATE_TARGETS) + 1):
            with self.subTest(fault_index=fault_index), tempfile.TemporaryDirectory(
                prefix="bimri-v503-caught-"
            ) as temporary:
                target = Path(temporary)
                self.cli("migrate", root=target)
                originals = self.seed_code_update_targets(target)
                protected = protected_tree_snapshot(target)

                result = self.worker(
                    f"code_update_caught_install_replace_{fault_index}",
                    "install",
                    "--target",
                    target,
                    "--quiescent",
                    engine_root=REPOSITORY,
                    check=False,
                    timeout=60,
                )

                self.assertEqual(result.returncode, 2)
                self.assertIn(
                    "forced caught failure after authorized replacement",
                    result.stderr,
                )
                for relative, content in originals.items():
                    self.assertEqual(
                        (target / relative).read_bytes(),
                        content,
                        relative,
                    )
                self.assertEqual(protected_tree_snapshot(target), protected)
                manifest_path = next((
                    target / ".bimri-update-backups"
                ).glob("*/install-manifest.json"))
                manifest = json.loads(manifest_path.read_text("utf-8"))
                self.assertEqual(manifest["status"], "rolled-back")
                self.assertEqual(manifest["preservation"], "passed")

    def test_every_abrupt_install_replacement_is_safely_recoverable(self):
        for fault_index in range(1, len(CODE_UPDATE_TARGETS) + 1):
            with self.subTest(fault_index=fault_index), tempfile.TemporaryDirectory(
                prefix="bimri-v503-abrupt-"
            ) as temporary:
                target = Path(temporary)
                self.cli("migrate", root=target)
                self.seed_code_update_targets(target)
                protected = protected_tree_snapshot(target)

                crashed = self.worker(
                    f"code_update_abrupt_install_replace_{fault_index}",
                    "install",
                    "--target",
                    target,
                    "--quiescent",
                    engine_root=REPOSITORY,
                    check=False,
                    timeout=60,
                )

                self.assertEqual(crashed.returncode, 100)
                self.assertEqual(protected_tree_snapshot(target), protected)
                resumed = self.cli(
                    "install",
                    "--target",
                    target,
                    "--quiescent",
                    root=REPOSITORY,
                    timeout=60,
                )
                self.assertIn("Memory preservation: PASSED", resumed.stdout)
                self.assertEqual(protected_tree_snapshot(target), protected)
                statuses = {
                    json.loads(path.read_text("utf-8"))["status"]
                    for path in (
                        target / ".bimri-update-backups"
                    ).glob("*/install-manifest.json")
                }
                self.assertEqual(
                    statuses, {"restored-before-retry", "installed"}
                )

    def test_code_update_abrupt_engine_exit_resumes_without_memory_write(self):
        self.cli("migrate")
        (self.root / "bimri-engine.py").write_text(
            "old engine sentinel\n", "utf-8"
        )
        before = protected_tree_snapshot(self.root)

        crashed = self.worker(
            "code_update_crash_after_engine",
            "install",
            "--target",
            self.root,
            "--quiescent",
            engine_root=REPOSITORY,
            check=False,
            timeout=60,
        )

        self.assertEqual(crashed.returncode, 96)
        self.assertEqual(protected_tree_snapshot(self.root), before)
        prepared = list((
            self.root / ".bimri-update-backups"
        ).glob("*/install-manifest.json"))
        self.assertEqual(len(prepared), 1)
        self.assertEqual(
            json.loads(prepared[0].read_text("utf-8"))["status"],
            "prepared",
        )

        resumed = self.cli(
            "install",
            "--target",
            self.root,
            "--quiescent",
            root=REPOSITORY,
            timeout=60,
        )
        self.assertIn("Memory preservation: PASSED", resumed.stdout)
        self.assertEqual(protected_tree_snapshot(self.root), before)
        statuses = {
            json.loads(path.read_text("utf-8"))["status"]
            for path in (
                self.root / ".bimri-update-backups"
            ).glob("*/install-manifest.json")
        }
        self.assertEqual(statuses, {"restored-before-retry", "installed"})

    def test_incomplete_rollback_keeps_recovery_engine_and_can_retry(self):
        self.cli("migrate")
        originals = self.seed_code_update_targets(self.root)
        protected = protected_tree_snapshot(self.root)

        crashed = self.worker(
            "code_update_crash_after_engine",
            "install",
            "--target",
            self.root,
            "--quiescent",
            engine_root=REPOSITORY,
            check=False,
            timeout=60,
        )
        self.assertEqual(crashed.returncode, 96)
        candidate_engine = (self.root / "bimri-engine.py").read_bytes()
        self.assertNotEqual(candidate_engine, originals["bimri-engine.py"])

        incomplete = self.worker(
            "code_update_fail_one_rollback_restore",
            "install",
            "--target",
            self.root,
            "--quiescent",
            engine_root=REPOSITORY,
            check=False,
            timeout=60,
        )

        self.assertEqual(incomplete.returncode, 2)
        self.assertIn("forced transient rollback restore failure", incomplete.stderr)
        self.assertEqual(
            (self.root / "bimri-engine.py").read_bytes(), candidate_engine
        )
        self.assertEqual(protected_tree_snapshot(self.root), protected)
        manifest_path = next((
            self.root / ".bimri-update-backups"
        ).glob("*/install-manifest.json"))
        manifest = json.loads(manifest_path.read_text("utf-8"))
        self.assertEqual(manifest["status"], "rollback-incomplete")

        restored = self.worker(
            "code_update_stop_after_recovery",
            "install",
            "--target",
            self.root,
            "--quiescent",
            engine_root=REPOSITORY,
            check=False,
            timeout=60,
        )

        self.assertEqual(restored.returncode, 2)
        self.assertIn("forced stop before fresh update transaction", restored.stderr)
        for relative, content in originals.items():
            self.assertEqual((self.root / relative).read_bytes(), content, relative)
        self.assertEqual(protected_tree_snapshot(self.root), protected)
        manifest = json.loads(manifest_path.read_text("utf-8"))
        self.assertEqual(manifest["status"], "restored-before-retry")

        completed = self.cli(
            "install",
            "--target",
            self.root,
            "--quiescent",
            root=REPOSITORY,
            timeout=60,
        )
        self.assertIn("Memory preservation: PASSED", completed.stdout)
        self.assertEqual(protected_tree_snapshot(self.root), protected)

    def test_external_candidate_recovers_death_after_old_engine_restore(self):
        self.cli("migrate")
        originals = self.seed_code_update_targets(self.root)
        protected = protected_tree_snapshot(self.root)

        crashed = self.worker(
            "code_update_crash_after_rollback_engine_replace",
            "install",
            "--target",
            self.root,
            "--quiescent",
            engine_root=REPOSITORY,
            check=False,
            timeout=60,
        )

        self.assertEqual(crashed.returncode, 103)
        self.assertEqual(
            (self.root / "bimri-engine.py").read_bytes(),
            originals["bimri-engine.py"],
        )
        self.assertEqual(protected_tree_snapshot(self.root), protected)
        manifest_path = next((
            self.root / ".bimri-update-backups"
        ).glob("*/install-manifest.json"))
        self.assertEqual(
            json.loads(manifest_path.read_text("utf-8"))["status"],
            "prepared",
        )

        resumed = self.cli(
            "install",
            "--target",
            self.root,
            "--quiescent",
            root=REPOSITORY,
            timeout=60,
        )

        self.assertIn("Memory preservation: PASSED", resumed.stdout)
        self.assertEqual(protected_tree_snapshot(self.root), protected)
        statuses = {
            json.loads(path.read_text("utf-8"))["status"]
            for path in (
                self.root / ".bimri-update-backups"
            ).glob("*/install-manifest.json")
        }
        self.assertEqual(statuses, {"restored-before-retry", "installed"})

    def test_verification_and_final_receipt_boundaries_are_recoverable(self):
        cases = (
            ("code_update_caught_installed_verify", 2, True),
            ("code_update_caught_final_receipt", 2, True),
            ("code_update_abrupt_installed_verify", 101, False),
            ("code_update_abrupt_final_receipt", 102, False),
        )
        for mode, returncode, caught in cases:
            with self.subTest(mode=mode), tempfile.TemporaryDirectory(
                prefix="bimri-v503-final-boundary-"
            ) as temporary:
                target = Path(temporary)
                self.cli("migrate", root=target)
                originals = self.seed_code_update_targets(target)
                protected = protected_tree_snapshot(target)

                faulted = self.worker(
                    mode,
                    "install",
                    "--target",
                    target,
                    "--quiescent",
                    engine_root=REPOSITORY,
                    check=False,
                    timeout=60,
                )

                self.assertEqual(faulted.returncode, returncode)
                self.assertEqual(protected_tree_snapshot(target), protected)
                if caught:
                    for relative, content in originals.items():
                        self.assertEqual(
                            (target / relative).read_bytes(), content, relative
                        )
                    manifest_path = next((
                        target / ".bimri-update-backups"
                    ).glob("*/install-manifest.json"))
                    manifest = json.loads(manifest_path.read_text("utf-8"))
                    self.assertEqual(manifest["status"], "rolled-back")
                else:
                    resumed = self.cli(
                        "install",
                        "--target",
                        target,
                        "--quiescent",
                        root=REPOSITORY,
                        timeout=60,
                    )
                    self.assertIn(
                        "Memory preservation: PASSED", resumed.stdout
                    )
                    self.assertEqual(
                        protected_tree_snapshot(target), protected
                    )

    def test_incomplete_backup_preparation_never_blocks_retry(self):
        self.cli("migrate")
        protected = protected_tree_snapshot(self.root)
        modes = (
            ("code_update_crash_after_preparing_mkdir", 97),
            ("code_update_crash_during_first_backup", 98),
            ("code_update_crash_before_prepared_publish", 99),
        )

        for mode, returncode in modes:
            with self.subTest(mode=mode):
                crashed = self.worker(
                    mode,
                    "install",
                    "--target",
                    self.root,
                    "--quiescent",
                    engine_root=REPOSITORY,
                    check=False,
                    timeout=60,
                )
                self.assertEqual(crashed.returncode, returncode)
                self.assertEqual(
                    protected_tree_snapshot(self.root), protected
                )

                resumed = self.cli(
                    "install",
                    "--target",
                    self.root,
                    "--quiescent",
                    root=REPOSITORY,
                    timeout=60,
                )
                self.assertIn("Memory preservation: PASSED", resumed.stdout)
                self.assertEqual(
                    protected_tree_snapshot(self.root), protected
                )

    def test_code_update_guard_blocks_even_byte_identical_memory_write(self):
        self.cli("migrate")
        (self.root / "bimri-engine.py").write_text(
            "old engine sentinel\n", "utf-8"
        )
        before = protected_tree_snapshot(self.root)

        result = self.worker(
            "code_update_protected_attempt",
            "install",
            "--target",
            self.root,
            "--quiescent",
            engine_root=REPOSITORY,
            check=False,
            timeout=60,
        )

        self.assertEqual(result.returncode, 2)
        self.assertIn("blocked a protected write: bimri.md", result.stderr)
        self.assertEqual(protected_tree_snapshot(self.root), before)
        manifest_path = next((
            self.root / ".bimri-update-backups"
        ).glob("*/install-manifest.json"))
        manifest = json.loads(manifest_path.read_text("utf-8"))
        self.assertEqual(manifest["protected_write_attempts"], 1)
        self.assertEqual(manifest["preservation"], "passed")

    def test_pre_and_post_audits_cannot_rebaseline_same_byte_writes(self):
        modes = (
            "code_update_pre_audit_same_byte_attempt",
            "code_update_post_audit_same_byte_attempt",
        )
        for mode in modes:
            with self.subTest(mode=mode), tempfile.TemporaryDirectory(
                prefix="bimri-v503-audit-guard-"
            ) as temporary:
                target = Path(temporary)
                self.cli("migrate", root=target)
                originals = self.seed_code_update_targets(target)
                protected = protected_tree_snapshot(target)

                result = self.worker(
                    mode,
                    "install",
                    "--target",
                    target,
                    "--quiescent",
                    engine_root=REPOSITORY,
                    check=False,
                    timeout=60,
                )

                self.assertEqual(result.returncode, 2)
                self.assertIn(
                    "blocked a protected atomic-write: bimri.md",
                    result.stderr,
                )
                self.assertEqual(protected_tree_snapshot(target), protected)
                backup_root = target / ".bimri-update-backups"
                if mode == "code_update_pre_audit_same_byte_attempt":
                    self.assertFalse(backup_root.exists())
                else:
                    for relative, content in originals.items():
                        self.assertEqual(
                            (target / relative).read_bytes(), content, relative
                        )
                    manifest_path = next(
                        backup_root.glob("*/install-manifest.json")
                    )
                    manifest = json.loads(manifest_path.read_text("utf-8"))
                    self.assertEqual(manifest["status"], "rolled-back")
                    self.assertEqual(manifest["protected_write_attempts"], 1)

    def test_code_update_never_stages_temporary_files_in_memory_tree(self):
        self.cli("migrate")
        bdir = self.root / ".bimri"
        runtime = bdir / "runtime.local.json"
        hooks = bdir / "hooks.claude.local.json"
        runtime_before = b'{"owner":"runtime sentinel"}\n'
        hooks_before = b'{"owner":"hooks sentinel"}\n'
        runtime.write_bytes(runtime_before)
        hooks.write_bytes(hooks_before)
        (self.root / "bimri-engine.py").write_text(
            "old engine sentinel\n", "utf-8"
        )
        protected_before = protected_tree_snapshot(self.root)

        failed = self.worker(
            "code_update_fail_before_engine_no_bdir_temp",
            "install",
            "--target",
            self.root,
            "--quiescent",
            engine_root=REPOSITORY,
            check=False,
            timeout=60,
        )

        self.assertEqual(failed.returncode, 2)
        self.assertNotIn("temporary file in .bimri", failed.stderr)
        self.assertEqual(runtime.read_bytes(), runtime_before)
        self.assertEqual(hooks.read_bytes(), hooks_before)
        self.assertEqual(protected_tree_snapshot(self.root), protected_before)
        self.assertEqual(list(bdir.glob(".bimri-tmp-*")), [])

        installed = self.worker(
            "code_update_forbid_bdir_temp",
            "install",
            "--target",
            self.root,
            "--quiescent",
            engine_root=REPOSITORY,
            timeout=60,
        )

        self.assertIn("Memory preservation: PASSED", installed.stdout)
        self.assertEqual(protected_tree_snapshot(self.root), protected_before)
        self.assertEqual(list(bdir.glob(".bimri-tmp-*")), [])

    def test_code_update_guard_checks_destructive_source_endpoint(self):
        self.cli("migrate")
        before = protected_tree_snapshot(self.root)

        result = self.worker(
            "code_update_protected_source_attempt",
            "install",
            "--target",
            self.root,
            "--quiescent",
            engine_root=REPOSITORY,
            check=False,
            timeout=60,
        )

        self.assertEqual(result.returncode, 2)
        self.assertIn(
            "blocked a protected replace-source: bimri.md", result.stderr
        )
        self.assertEqual(protected_tree_snapshot(self.root), before)

    def test_code_update_rejects_untrusted_prepared_manifest_before_mutation(self):
        self.cli("migrate")
        victim = self.root / "victim.txt"
        victim.write_bytes(b"owner project bytes\n")
        protected_before = protected_tree_snapshot(self.root)

        crashed = self.worker(
            "code_update_crash_after_engine",
            "install",
            "--target",
            self.root,
            "--quiescent",
            engine_root=REPOSITORY,
            check=False,
            timeout=60,
        )
        self.assertEqual(crashed.returncode, 96)
        manifest_path = next((
            self.root / ".bimri-update-backups"
        ).glob("*/install-manifest.json"))
        manifest = json.loads(manifest_path.read_text("utf-8"))
        manifest["records"]["victim.txt"] = {
            "existed": False,
            "backup": None,
            "sha256": None,
        }
        manifest_path.write_text(
            json.dumps(manifest, indent=2, sort_keys=True) + "\n", "utf-8"
        )

        resumed = self.cli(
            "install",
            "--target",
            self.root,
            "--quiescent",
            root=REPOSITORY,
            check=False,
            timeout=60,
        )

        self.assertEqual(resumed.returncode, 2)
        self.assertIn(
            "prepared code-update target records are invalid", resumed.stderr
        )
        self.assertEqual(victim.read_bytes(), b"owner project bytes\n")
        self.assertEqual(protected_tree_snapshot(self.root), protected_before)

    def test_code_update_rejects_tampered_prepared_identity_and_status(self):
        mutations = (
            ("target", "/tampered-target"),
            ("status", "installed"),
            ("status", "unrecognized-status"),
        )
        for field, value in mutations:
            with self.subTest(field=field, value=value), tempfile.TemporaryDirectory(
                prefix="bimri-v503-receipt-"
            ) as temporary:
                target = Path(temporary)
                self.cli("migrate", root=target)
                protected = protected_tree_snapshot(target)
                crashed = self.worker(
                    "code_update_crash_after_engine",
                    "install",
                    "--target",
                    target,
                    "--quiescent",
                    engine_root=REPOSITORY,
                    check=False,
                    timeout=60,
                )
                self.assertEqual(crashed.returncode, 96)
                manifest_path = next((
                    target / ".bimri-update-backups"
                ).glob("*/install-manifest.json"))
                manifest = json.loads(manifest_path.read_text("utf-8"))
                manifest[field] = value
                manifest_path.write_text(
                    json.dumps(manifest, indent=2, sort_keys=True) + "\n",
                    "utf-8",
                )
                receipts_before = list((
                    target / ".bimri-update-backups"
                ).iterdir())

                resumed = self.cli(
                    "install",
                    "--target",
                    target,
                    "--quiescent",
                    root=REPOSITORY,
                    check=False,
                    timeout=60,
                )

                self.assertEqual(resumed.returncode, 2)
                self.assertIn("code-update", resumed.stderr)
                self.assertEqual(
                    list((target / ".bimri-update-backups").iterdir()),
                    receipts_before,
                )
                self.assertEqual(protected_tree_snapshot(target), protected)

    @unittest.skipIf(
        os.name == "nt", "backslash cannot be a filename character on Windows"
    )
    def test_code_update_recovers_with_backslash_in_unknown_memory_filename(self):
        self.cli("migrate")
        unknown = self.root / ".bimri" / "owner\\unknown.bin"
        unknown.write_bytes(b"unknown protected bytes\x00\xff")
        protected = protected_tree_snapshot(self.root)

        crashed = self.worker(
            "code_update_crash_after_engine",
            "install",
            "--target",
            self.root,
            "--quiescent",
            engine_root=REPOSITORY,
            check=False,
            timeout=60,
        )
        self.assertEqual(crashed.returncode, 96)

        resumed = self.cli(
            "install",
            "--target",
            self.root,
            "--quiescent",
            root=REPOSITORY,
            timeout=60,
        )

        self.assertIn("Memory preservation: PASSED", resumed.stdout)
        self.assertEqual(unknown.read_bytes(), b"unknown protected bytes\x00\xff")
        self.assertEqual(protected_tree_snapshot(self.root), protected)

    def test_code_update_resume_refuses_changed_protected_tree(self):
        self.cli("migrate")
        protected_before = protected_tree_snapshot(self.root)

        crashed = self.worker(
            "code_update_crash_after_engine",
            "install",
            "--target",
            self.root,
            "--quiescent",
            engine_root=REPOSITORY,
            check=False,
            timeout=60,
        )
        self.assertEqual(crashed.returncode, 96)
        owner_file = self.root / ".bimri" / "owner-created-after-crash.txt"
        owner_file.write_bytes(b"new protected owner bytes\n")
        changed = protected_tree_snapshot(self.root)
        self.assertNotEqual(changed, protected_before)

        resumed = self.cli(
            "install",
            "--target",
            self.root,
            "--quiescent",
            root=REPOSITORY,
            check=False,
            timeout=60,
        )

        self.assertEqual(resumed.returncode, 2)
        self.assertIn(
            "protected memory differs from its prepared manifest",
            resumed.stderr,
        )
        self.assertIn(
            "created protected path .bimri/owner-created-after-crash.txt",
            resumed.stderr,
        )
        self.assertEqual(owner_file.read_bytes(), b"new protected owner bytes\n")
        self.assertEqual(protected_tree_snapshot(self.root), changed)

    def test_code_update_installs_recovery_tools_without_rewriting_governance(self):
        self.cli("migrate")
        conflict = self.root / ".bimri" / "conflicts" / "C000001.json"
        conflict.write_bytes(b"{malformed historical governance")
        before = protected_tree_snapshot(self.root)

        result = self.cli(
            "install",
            "--target",
            self.root,
            "--quiescent",
            root=REPOSITORY,
            timeout=60,
        )

        self.assertIn("READ-ONLY AUDIT: RECOVERY REQUIRED", result.stdout)
        self.assertIn("authority recovery needed", result.stdout)
        self.assertEqual(conflict.read_bytes(), b"{malformed historical governance")
        self.assertEqual(protected_tree_snapshot(self.root), before)
        manifest_path = next((
            self.root / ".bimri-update-backups"
        ).glob("*/install-manifest.json"))
        manifest = json.loads(manifest_path.read_text("utf-8"))
        self.assertEqual(manifest["status"], "installed-recovery-required")
        self.assertEqual(manifest["read_only_audit"], "recovery-required")

    def test_code_update_rejects_invalid_head_before_package_replacement(self):
        self.cli("migrate")
        program = self.root / "bimri-engine.py"
        program.write_text("old engine sentinel\n", "utf-8")
        state_path = self.root / ".bimri" / "state.json"
        state = json.loads(state_path.read_text("utf-8"))
        state["head_hash"] = "0" * 64
        state_path.write_text(
            json.dumps(state, indent=2, sort_keys=True) + "\n", "utf-8"
        )
        before = protected_tree_snapshot(self.root)

        result = self.cli(
            "install",
            "--target",
            self.root,
            "--quiescent",
            root=REPOSITORY,
            check=False,
            timeout=60,
        )

        self.assertEqual(result.returncode, 2)
        self.assertIn("state head hash does not match", result.stderr)
        self.assertEqual(program.read_text("utf-8"), "old engine sentinel\n")
        self.assertEqual(protected_tree_snapshot(self.root), before)
        self.assertFalse((self.root / ".bimri-update-backups").exists())


if __name__ == "__main__":
    unittest.main()
