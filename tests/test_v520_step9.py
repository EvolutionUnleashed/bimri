"""BIMRI v5.2 build step 9: cold listing and warm search.

Spine: DECISIONS D6.2 (`cold`), D6.4 (warm `search`/`recall --query`),
D1.4 (`--audit`), D1.5 (`cold` surface); DESIGN section 01 3.5 (trailers and
exit codes), section 06 2.1-2.3 (cold surface, read path, output), 4.2-4.6
(warm search decision tree, forbid list, foreign archive row), 5 (byte
compatible output) and 7 (the step's tests).

Every test drives the engine out of process (`--root` on a disposable store).
Two seams run in-process through a small probe script written next to the
store: `dated` pins `today()` so cooled subjects carry two archive dates, and
`cold_forbid` refuses the full audit, `archive_records`, `cold_entry` and any
file read under `.bimri/archive/` or `.bimri/log/` so a warm `cold` proves it
lists `state.cold_current` without touching an archive month or a run log.
"""

import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import unittest
import uuid
from pathlib import Path


REPOSITORY = Path(__file__).resolve().parents[1]
ENGINE = REPOSITORY / "bimri-engine.py"
CRASH_WORKER = REPOSITORY / "tests" / "crash_worker.py"
RUN_RE = re.compile(r"BIMRI RUN HANDLE: (R\d{6})")
PROPOSAL_RE = re.compile(r"\bR\d{6}-Q\d{3}\b")
TOKEN_RE = re.compile(r"^V\d{6}:cold:[0-9a-f]{64}$")
DEFAULT_HOT_MAX_BYTES = 49152
FOREIGN_ENTRY_ERROR = "archived line is not a valid memory entry."
PREFIX_ERROR = "cold prefix must be 1-80 characters of a-z 0-9 . _ -"
LIMIT_ERROR = "cold limit must be between 1 and 5000."

# Cold subjects seeded in two cooling batches, each pinned to its own date.
# Texts exercise the first-sentence rule of section 06 2.3: '. ', '? ', '! ',
# no mark at all (whole text, trailing period kept), and the 160 cap.
BATCH_ONE_DATE = "2026-08-20"
BATCH_TWO_DATE = "2026-09-14"
LONG_NO_MARK = "Echo " + "e" * 295
LONG_LATE_MARK = "Golf " + "g" * 195 + ". After the cap."
BATCH_ONE = (
    ("cold.alpha", "Alpha ships first. Then the rest follows.", 4, "active"),
    ("cold.bravo", "Is bravo ready? Nobody knows yet.", 2, "watch"),
    ("cold.charlie", "Charlie works! Keep it that way.", 3, "active"),
)
BATCH_TWO = (
    ("cold.delta", "Delta ends here.", 3, "watch"),
    ("cold.echo", LONG_NO_MARK, 2, "active"),
    ("cold.golf", LONG_LATE_MARK, 5, "active"),
    ("other.hotel", "Hotel sits outside the cold prefix. Second part.", 3, "active"),
)
FIRST_SENTENCES = {
    "cold.alpha": "Alpha ships first.",
    "cold.bravo": "Is bravo ready?",
    "cold.charlie": "Charlie works!",
    "cold.delta": "Delta ends here.",
    "cold.echo": LONG_NO_MARK[:160],
    "cold.golf": LONG_LATE_MARK[:160],
    "other.hotel": "Hotel sits outside the cold prefix.",
}
JSON_EXTRA_FIELDS = {
    "archived_on", "archived_by", "entry_id", "first_sentence", "token",
}

PROBE_SOURCE = r'''
import builtins
import importlib.util
import io
import os
import sys
from pathlib import Path


def load_engine(path):
    spec = importlib.util.spec_from_file_location("bimri_step9_probe", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def main():
    engine_path, mode, root = sys.argv[1], sys.argv[2], Path(sys.argv[3])
    command = sys.argv[4:]
    engine = load_engine(engine_path)
    if mode.startswith("dated:"):
        pinned = mode.split(":", 1)[1]
        engine.today = lambda: pinned
    elif mode == "cold_forbid":
        def forbid(name):
            def reject(*_args, **_kwargs):
                raise AssertionError("warm cold listing called " + name)
            return reject
        for name in (
            "authority_storage_issues", "audit_witness_manifest",
            "audit_witness_run_facts", "load_audit_manifest_evidence",
            "scan_open_conflicts", "held_recall_records",
            "archive_records", "cold_entry", "recall_records",
            "governance_snapshot", "validate_cold_archive_bindings",
            "capture_audit_copy",
        ):
            if hasattr(engine, name):
                setattr(engine, name, forbid(name))
        guarded = [
            (root / ".bimri" / "archive").resolve(),
            (root / ".bimri" / "log").resolve(),
        ]

        def check(target):
            try:
                resolved = Path(os.fspath(target)).resolve()
            except TypeError:
                return
            for directory in guarded:
                if resolved == directory or directory in resolved.parents:
                    raise AssertionError(
                        "warm cold listing read " + resolved.as_posix()
                    )

        original_io_open = io.open
        original_os_open = os.open

        def guarded_open(file, *args, **kwargs):
            check(file)
            return original_io_open(file, *args, **kwargs)

        def guarded_os_open(file, *args, **kwargs):
            check(file)
            return original_os_open(file, *args, **kwargs)

        io.open = guarded_open
        builtins.open = guarded_open
        os.open = guarded_os_open
    else:
        raise SystemExit("unknown probe mode: " + mode)
    return engine.main(["--root", str(root)] + command)


if __name__ == "__main__":
    sys.exit(main())
'''


def first_sentence(text):
    """Section 06 2.3: up to the first '. ', '? ' or '! ', mark kept; 160 cap."""
    positions = [
        index for index in (text.find(". "), text.find("? "), text.find("! "))
        if index >= 0
    ]
    sentence = text[:min(positions) + 1] if positions else text
    return sentence[:160]


class V520Step9Test(unittest.TestCase):
    maxDiff = None

    @classmethod
    def setUpClass(cls):
        cls._help_cache = {}
        cls._seed_temp = tempfile.TemporaryDirectory(prefix="bimri-v520-s9c-")
        cls._seeded = {}

    @classmethod
    def tearDownClass(cls):
        cls._seed_temp.cleanup()

    def setUp(self):
        self._temp = tempfile.TemporaryDirectory(prefix="bimri-v520-s9-")
        self.workspace = Path(self._temp.name)
        self.root = self.workspace / "store"

    def tearDown(self):
        self._temp.cleanup()

    # ---- process helpers (test_v520_step1.py recipe) ----------------------

    def cli(self, *arguments, root=None, check=True, timeout=180):
        command = [
            sys.executable, str(ENGINE), "--root", str(root or self.root),
            *map(str, arguments),
        ]
        result = subprocess.run(
            command, text=True, capture_output=True, timeout=timeout,
            encoding="utf-8",
        )
        if check and result.returncode != 0:
            self.fail(
                f"command failed: {command!r}\n"
                f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}"
            )
        return result

    def help_text(self, *command):
        key = command
        if key not in self._help_cache:
            result = subprocess.run(
                [sys.executable, str(ENGINE), *command, "--help"],
                text=True, capture_output=True, timeout=60, encoding="utf-8",
            )
            type(self)._help_cache[key] = result.stdout + result.stderr
        return self._help_cache[key]

    def worker(self, mode, *arguments, root=None, check=True, timeout=180):
        command = [
            sys.executable, str(CRASH_WORKER), str(ENGINE), mode,
            str(root or self.root), *map(str, arguments),
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

    def probe(self, mode, *arguments, root=None, check=True, timeout=180):
        script = self.workspace / "v520_step9_probe.py"
        if not script.is_file():
            script.write_text(PROBE_SOURCE, encoding="utf-8")
        command = [
            sys.executable, str(script), str(ENGINE), mode,
            str(root or self.root), *map(str, arguments),
        ]
        result = subprocess.run(
            command, text=True, capture_output=True, timeout=timeout,
            encoding="utf-8",
        )
        if check and result.returncode != 0:
            self.fail(
                f"probe failed: {command!r}\n"
                f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}"
            )
        return result

    def run_command(self, mode, *arguments, root=None, check=True):
        if mode is None:
            return self.cli(*arguments, root=root, check=check)
        return self.probe(mode, *arguments, root=root, check=check)

    def start_arguments(self):
        arguments = ["start"]
        if "--open-run" in self.help_text("start"):
            arguments.append("--open-run")
        return arguments

    def doctor_arguments(self):
        arguments = ["doctor"]
        if "--live" in self.help_text("doctor"):
            arguments.append("--live")
        return arguments

    def start(self, actor, root=None, mode=None):
        result = self.run_command(
            mode, *self.start_arguments(), "--actor", actor, root=root
        )
        match = RUN_RE.search(result.stdout)
        self.assertIsNotNone(match, result.stdout)
        return match.group(1)

    def propose(
        self, run_id, key, text, *extra, tier=2, source="agent",
        trust="working", new_subject=True, root=None, mode=None,
    ):
        arguments = [
            "propose", "--run", run_id, "--tier", str(tier), "--key", key,
            "--text", text, "--source", source, "--trust", trust, *extra,
        ]
        if new_subject:
            arguments.append("--new-subject")
        result = self.run_command(mode, *arguments, root=root)
        match = PROPOSAL_RE.search(result.stdout)
        self.assertIsNotNone(match, result.stdout)
        return match.group(0)

    def sync(self, run_id, root=None, mode=None):
        return self.run_command(mode, "sync", "--run", run_id, root=root)

    # ---- store helpers ---------------------------------------------------

    def state_path(self, root=None):
        return Path(root or self.root) / ".bimri" / "state.json"

    def state(self, root=None):
        return json.loads(self.state_path(root).read_text("utf-8"))

    def witness_path(self, root=None):
        return Path(root or self.root) / ".bimri" / "audit-witness.json"

    def decision(self, proposal_id, root=None):
        path = (
            Path(root or self.root) / ".bimri" / "decisions"
            / f"{proposal_id}.json"
        )
        return json.loads(path.read_text("utf-8"))

    def hot_bytes(self, root=None):
        return len((Path(root or self.root) / "bimri.md").read_bytes())

    def set_hot_budget(self, value, root=None):
        # The v511 seed_cold_current recipe: change the ceiling by hand and
        # drop the witness so the next command audits the edited state.
        state = self.state(root)
        state["hot_max_bytes"] = value
        self.state_path(root).write_text(
            json.dumps(state, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        try:
            self.witness_path(root).unlink()
        except FileNotFoundError:
            pass

    def warm(self, root=None):
        # A full live audit publishes the checkpoint the warm paths read.
        result = self.cli(*self.doctor_arguments(), root=root, check=False)
        self.assertTrue(
            self.witness_path(root).is_file(), result.stdout + result.stderr
        )
        return result

    def cool_batch(self, subjects, date, anchor_text, root=None):
        """Create `subjects` hot, then cool all of them on `date`."""
        base = self.hot_bytes(root)
        mode = f"dated:{date}"
        # The subjects are created on `date` too: from v5.2.0 (section 06
        # 3.2, local decision 17) an incoming generation that scores below
        # the resident it would displace cools itself, so the importance-5
        # anchor must not be older than the subjects it displaces.
        run_id = self.start("cold-batch-create", root=root, mode=mode)
        for key, text, importance, status in subjects:
            self.propose(
                run_id, key, text, "--importance", str(importance),
                "--status", status, root=root, mode=mode,
            )
        self.sync(run_id, root=root, mode=mode)
        # Room for the anchor only: every new subject must cool.
        self.set_hot_budget(base, root=root)
        cooling_run = self.start("cold-batch-cool", root=root, mode=mode)
        self.propose(
            cooling_run, "anchor.resident", anchor_text,
            "--importance", "5", "--status", "active",
            new_subject=False, root=root, mode=mode,
        )
        self.sync(cooling_run, root=root, mode=mode)
        cold = self.state(root)["cold_current"]
        for key, _text, _importance, _status in subjects:
            self.assertIn(key, cold)
            self.assertEqual(cold[key]["archived_on"], date, key)
        self.set_hot_budget(DEFAULT_HOT_MAX_BYTES, root=root)

    def seed_cold_store(self, root=None):
        """Two cooling batches on two pinned dates; a warm checkpoint."""
        root = Path(root or self.root)
        run_id = self.start("cold-anchor", root=root)
        self.propose(
            run_id, "anchor.resident", "Anchor resident value 0.",
            "--importance", "5", "--status", "active", root=root,
        )
        self.sync(run_id, root=root)
        self.cool_batch(BATCH_ONE, BATCH_ONE_DATE, "Anchor resident value 1.", root)
        self.cool_batch(BATCH_TWO, BATCH_TWO_DATE, "Anchor resident value 2.", root)
        self.warm(root)
        return root

    def seeded_clone(self, label="seeded"):
        # Seeding costs ~30 engine invocations; build it once per class and
        # copy it per test (copytree ignoring the lock, then a fresh lock).
        template = self._seeded.get("cold")
        if template is None:
            template = Path(self._seed_temp.name) / "cold-template"
            self.seed_cold_store(template)
            type(self)._seeded["cold"] = template
        destination = self.workspace / label
        self.copy_store(template, destination)
        return destination

    @staticmethod
    def copy_store(source, destination):
        shutil.copytree(
            source, destination,
            ignore=shutil.ignore_patterns("engine.lock", "runtime.local.json"),
        )
        lock = destination / ".bimri" / "engine.lock"
        lock.parent.mkdir(parents=True, exist_ok=True)
        lock.write_bytes(b"")

    def expected_rows(self, root, prefix=None):
        cold = self.state(root)["cold_current"]
        rows = []
        for key, record in cold.items():
            if prefix and not key.startswith(prefix):
                continue
            rows.append((record["archived_on"], key))
        rows.sort(key=lambda item: item[1])
        rows.sort(key=lambda item: item[0], reverse=True)
        return [key for _date, key in rows]

    def cold_json(self, *arguments, root=None, expect=0, mode=None):
        result = self.run_command(
            mode, "cold", *arguments, "--json", root=root, check=False
        )
        self.assertEqual(
            result.returncode, expect, result.stdout + result.stderr
        )
        lines = result.stdout.splitlines()
        self.assertEqual(len(lines), 1, result.stdout)
        return json.loads(lines[0])

    @staticmethod
    def tree_digest(root):
        digest = {}
        base = Path(root)
        for path in sorted(base.rglob("*")):
            if path.is_file() and path.name != "engine.lock":
                digest[path.relative_to(base).as_posix()] = hashlib.sha256(
                    path.read_bytes()
                ).hexdigest()
        return digest

    # ---- D6.2 / D1.5: the cold listing ------------------------------------

    def test_cold_lists_key_first_sentence_and_token(self):
        # D6.2, section 06 2.3: seven TSV columns, archived_on desc then key,
        # first-sentence rule, trailer `BIMRI: <total> cold subjects.`.
        root = self.seeded_clone()
        result = self.cli("cold", root=root)
        self.assertEqual(result.returncode, 0, result.stderr)
        lines = result.stdout.splitlines()
        keys = self.expected_rows(root)
        self.assertEqual(len(keys), 7)
        self.assertEqual(lines[-1], f"BIMRI: {len(keys)} cold subjects.")
        rows = [line.split("\t") for line in lines[:-1]]
        self.assertEqual([row[0] for row in rows], keys)
        cold = self.state(root)["cold_current"]
        specs = {key: (text, imp, status) for key, text, imp, status in BATCH_ONE + BATCH_TWO}
        for row in rows:
            self.assertEqual(len(row), 7, row)
            key = row[0]
            text, importance, status = specs[key]
            self.assertEqual(
                row,
                [
                    key, cold[key]["archived_on"], "working", "agent",
                    str(importance), status, FIRST_SENTENCES[key],
                ],
            )
            self.assertEqual(FIRST_SENTENCES[key], first_sentence(text))

    def test_cold_sorts_newest_archive_date_first(self):
        # D6.2: sorted by archived_on descending, then key ascending, over
        # two archive dates.
        root = self.seeded_clone()
        lines = self.cli("cold", root=root).stdout.splitlines()[:-1]
        dates = [line.split("\t")[1] for line in lines]
        keys = [line.split("\t")[0] for line in lines]
        self.assertEqual(
            dates,
            [BATCH_TWO_DATE] * len(BATCH_TWO) + [BATCH_ONE_DATE] * len(BATCH_ONE),
        )
        self.assertEqual(
            keys,
            sorted(key for key, *_ in BATCH_TWO) + sorted(key for key, *_ in BATCH_ONE),
        )

    def test_cold_first_sentence_is_capped_at_160(self):
        # D6.2 first-sentence rule: no mark -> whole text, then [:160]; a mark
        # past the cap still cuts at 160.
        root = self.seeded_clone()
        by_key = {
            line.split("\t")[0]: line.split("\t")[6]
            for line in self.cli("cold", root=root).stdout.splitlines()[:-1]
        }
        self.assertEqual(len(by_key["cold.echo"]), 160)
        self.assertEqual(by_key["cold.echo"], LONG_NO_MARK[:160])
        self.assertEqual(len(by_key["cold.golf"]), 160)
        self.assertEqual(by_key["cold.delta"], "Delta ends here.")

    def test_cold_json_fields_and_token_equal_get(self):
        # D6.2 --json: typed_recall_fields plus archived_on, archived_by,
        # entry_id, first_sentence, token; token equals get --key --json's
        # (D1.3, section 06 2.3).
        root = self.seeded_clone()
        payload = self.cold_json(root=root)
        cold = self.state(root)["cold_current"]
        head = self.state(root)["head_revision"]
        self.assertEqual(set(payload), {"subjects", "total", "omitted"})
        self.assertEqual((payload["total"], payload["omitted"]), (7, 0))
        self.assertEqual(
            [item["key"] for item in payload["subjects"]],
            self.expected_rows(root),
        )
        for item in payload["subjects"]:
            key = item["key"]
            record = cold[key]
            self.assertTrue(JSON_EXTRA_FIELDS <= set(item), item)
            self.assertEqual(item["entry_id"], record["entry_id"])
            self.assertEqual(item["id"], record["entry_id"])
            self.assertEqual(item["archived_on"], record["archived_on"])
            self.assertEqual(item["archived_by"], record["archived_by"])
            self.assertEqual(item["first_sentence"], FIRST_SENTENCES[key])
            self.assertEqual(item["tier"], 2)
            self.assertEqual((item["trust"], item["source"]), ("working", "agent"))
            self.assertEqual(
                item["token"], f"V{head:06d}:cold:{record['line_hash']}"
            )
            self.assertRegex(item["token"], TOKEN_RE)
        alpha = next(i for i in payload["subjects"] if i["key"] == "cold.alpha")
        got = self.cli("get", "--key", "cold.alpha", "--json", root=root)
        got_payload = json.loads(got.stdout.splitlines()[0])
        cold_rows = [m for m in got_payload["matches"] if m["location"] == "COLD"]
        self.assertEqual(len(cold_rows), 1, got_payload)
        if "token" in cold_rows[0]:
            self.assertEqual(cold_rows[0]["token"], alpha["token"])
        self.assertEqual(cold_rows[0]["text"], alpha["text"])

    def test_cold_limit_prints_omitted_trailer(self):
        # Section 01 3.5 / 06 2.3: a cut listing ends with both trailers and
        # exits 0; --json omitted counts the cut.
        root = self.seeded_clone()
        result = self.cli("cold", "--limit", "2", root=root)
        lines = result.stdout.splitlines()
        self.assertEqual(len(lines), 4, result.stdout)
        self.assertEqual(
            [line.split("\t")[0] for line in lines[:2]], self.expected_rows(root)[:2]
        )
        self.assertEqual(lines[2], "BIMRI: 7 cold subjects.")
        self.assertEqual(
            lines[3],
            "BIMRI: 5 additional subjects omitted; narrow with --prefix "
            "<key-prefix> or use search.",
        )
        payload = self.cold_json("--limit", "2", root=root)
        self.assertEqual((payload["total"], payload["omitted"]), (7, 5))
        self.assertEqual(len(payload["subjects"]), 2)

    def test_cold_prefix_counts_within_the_filter(self):
        # Section 06 2.1/2.3: --prefix keeps keys starting with P; total,
        # omitted and the trailer count only matches.
        root = self.seeded_clone()
        result = self.cli("cold", "--prefix", "cold.", root=root)
        lines = result.stdout.splitlines()
        expected = self.expected_rows(root, prefix="cold.")
        self.assertEqual(len(expected), 6)
        self.assertEqual([line.split("\t")[0] for line in lines[:-1]], expected)
        self.assertEqual(lines[-1], "BIMRI: 6 cold subjects under cold..")
        payload = self.cold_json("--prefix", "cold.", "--limit", "4", root=root)
        self.assertEqual((payload["total"], payload["omitted"]), (6, 2))

    def test_cold_prefix_without_match_exits_1(self):
        # Section 06 2.3: no match prints `BIMRI: no cold subjects under P.`
        # alone, exit 1.
        root = self.seeded_clone()
        result = self.cli("cold", "--prefix", "zulu.", root=root, check=False)
        self.assertEqual(result.returncode, 1, result.stderr)
        self.assertEqual(result.stdout, "BIMRI: no cold subjects under zulu..\n")

    def test_cold_rejects_bad_limit_and_prefix(self):
        # Section 06 2.1: --limit 1..5000, --prefix 1-80 of [a-z0-9._-]; exit 2.
        root = self.seeded_clone()
        for limit in ("0", "5001"):
            result = self.cli("cold", "--limit", limit, root=root, check=False)
            self.assertEqual(result.returncode, 2, limit)
            self.assertIn(LIMIT_ERROR, result.stdout + result.stderr)
        for prefix in ("Bad Prefix!", "a" * 81):
            result = self.cli("cold", "--prefix", prefix, root=root, check=False)
            self.assertEqual(result.returncode, 2, prefix)
            self.assertIn(PREFIX_ERROR, result.stdout + result.stderr)

    def test_cold_matches_state_without_archive_reads(self):
        # D6.2 warm read: state.cold_current is inside state_hash; no
        # cold_entry, no archive_records, no archive month or run log read,
        # no full audit; the read leaves every byte unchanged.
        root = self.seeded_clone()
        before = self.tree_digest(root)
        result = self.probe("cold_forbid", "cold", "--limit", "5000", root=root)
        keys = [line.split("\t")[0] for line in result.stdout.splitlines()[:-1]]
        self.assertEqual(sorted(keys), sorted(self.state(root)["cold_current"]))
        self.assertEqual(keys, self.expected_rows(root))
        payload = self.cold_json(root=root, mode="cold_forbid")
        self.assertEqual(payload["total"], len(self.state(root)["cold_current"]))
        self.assertEqual(self.tree_digest(root), before)

    def test_cold_under_valid_witness_forbid_mode(self):
        # Step 9 assertion update: valid_witness_forbid_full_audit covers cold.
        root = self.seeded_clone()
        result = self.worker("valid_witness_forbid_full_audit", "cold", root=root)
        self.assertEqual(result.stdout.splitlines()[-1], "BIMRI: 7 cold subjects.")

    def test_cold_without_witness_reads_state_and_matches_warm(self):
        # D6.2: no witness -> load_current_state_read_only; same output.
        root = self.seeded_clone()
        warm_tsv = self.cli("cold", root=root).stdout
        warm_json = self.cli("cold", "--json", root=root).stdout
        self.witness_path(root).unlink()
        self.assertEqual(self.cli("cold", root=root).stdout, warm_tsv)
        self.assertEqual(self.cli("cold", "--json", root=root).stdout, warm_json)

    def test_cold_exit_1_when_none(self):
        # D1.5, section 01 local decision 6: no cold subjects -> TSV
        # `BIMRI: no cold subjects.` and --json `{"omitted": 0, "subjects": [],
        # "total": 0}`, both exit 1.
        self.start("cold-empty")
        tsv = self.cli("cold", check=False)
        self.assertEqual(tsv.returncode, 1, tsv.stderr)
        self.assertEqual(tsv.stdout, "BIMRI: no cold subjects.\n")
        payload = self.cold_json(expect=1)
        self.assertEqual(payload, {"omitted": 0, "subjects": [], "total": 0})
        raw = self.cli("cold", "--json", check=False).stdout
        self.assertEqual(raw, '{"omitted": 0, "subjects": [], "total": 0}\n')

    def test_cold_on_missing_store_exits_2_and_creates_nothing(self):
        # Section 06 2.1: existing_store_lock; a missing store is an error,
        # never created.
        missing = self.workspace / "nothing-here"
        missing.mkdir()
        result = self.cli("cold", root=missing, check=False)
        self.assertEqual(result.returncode, 2, result.stdout)
        self.assertFalse((missing / ".bimri").exists())

    # ---- D6.4: warm search ----------------------------------------------

    T1_TEXT = "The quokka class ticket is 149 dollars."
    T2_TEXT = "Verify the quokka checkout flow before the class."
    T2_NEXT = "Verify the quokka refund flow before the class."
    HELD_TEXT = "The quokka class ticket is 199 dollars, per an agent inference."
    T3_TEXT = "The nightly sweep double-fires when no idempotency guard exists."
    T3_FALSIFIER = "A day on which the sweep fires exactly once after the guard ships."

    def seed_search_store(self, root):
        """Hot, cold, held, replaced (HISTORY) and cooled rows."""
        root = Path(root)
        run_id = self.start("search-seed", root=root)
        self.propose(
            run_id, "pricing.class-ticket", self.T1_TEXT, "--kind", "fact",
            "--tags", "pricing,quokka-tag", tier=1, source="user",
            trust="confirmed", root=root,
        )
        self.propose(
            run_id, "launch.next-step", self.T2_TEXT, "--importance", "4",
            "--status", "active", "--tags", "launch,checkout,wombat-tag",
            root=root,
        )
        # The v512:455-482 Tier 3 pattern: journal evidence and a falsifier
        # whose words join the search haystack.
        journaled = self.cli(
            "journal", "--run", run_id, "--text", "Observed once.", root=root
        ).stdout.strip()
        self.propose(
            run_id, "pattern.sweep-double-fire", self.T3_TEXT,
            "--confidence", "developing", "--observations", "2",
            "--evidence", journaled, "--falsifier", self.T3_FALSIFIER,
            tier=3, root=root,
        )
        self.sync(run_id, root=root)
        later = self.start("search-later", root=root)
        held = self.propose(
            later, "pricing.class-ticket", self.HELD_TEXT, "--kind", "fact",
            tier=1, new_subject=False, root=root,
        )
        self.propose(
            later, "launch.next-step", self.T2_NEXT, "--importance", "4",
            "--status", "active", new_subject=False, root=root,
        )
        self.sync(later, root=root)
        self.assertEqual(self.decision(held, root)["outcome"], "held")
        quokka_cold = (
            ("cold.quokka-one", "A cooled quokka subject. More words.", 3, "active"),
            ("cold.quokka-two", "Another cooled quokka? Yes indeed.", 2, "watch"),
        )
        base_run = self.start("search-anchor", root=root)
        self.propose(
            base_run, "anchor.resident", "Anchor resident value 0.",
            "--importance", "5", "--status", "active", root=root,
        )
        self.sync(base_run, root=root)
        # Cooling must keep the Tier 1 and the replaced Tier 2 subject hot:
        # pin them by giving the cooled pair the lowest retention.
        self.cool_batch(quokka_cold, BATCH_TWO_DATE, "Anchor resident value 1.", root)
        self.warm(root)
        return held

    @staticmethod
    def engine_has_copy_path():
        source = ENGINE.read_text("utf-8")
        return re.search(r"^def capture_audit_copy\(", source, re.M) is not None

    def search_outputs(self, root, query, warm_mode=None):
        outputs = {}
        for command in (("search", "--query", query), ("recall", "--query", query)):
            for extra in ((), ("--json",), ("--limit", "1"), ("--limit", "1", "--json")):
                arguments = list(command) + list(extra)
                if warm_mode:
                    result = self.worker(warm_mode, *arguments, root=root, check=False)
                else:
                    result = self.cli(*arguments, "--audit", root=root, check=False)
                outputs[tuple(arguments)] = (result.returncode, result.stdout)
        return outputs

    def test_search_warm_equals_audit(self):
        # D6.4 / D1.4 / section 06 5: the warm checkpoint path (under
        # warm_search_forbid_full_audit, section 06 4.5) and --audit print
        # byte-identical TSV and JSON; live witness bytes unchanged by both.
        root = self.workspace / "search"
        held = self.seed_search_store(root)
        witness = self.witness_path(root).read_bytes()
        for query in ("quokka", "class", "anchor", "wombat", "exactly once"):
            warm = self.search_outputs(root, query, "warm_search_forbid_full_audit")
            # The warm path never publishes (section 06 4.2).
            self.assertEqual(
                self.witness_path(root).read_bytes(), witness, ("warm", query)
            )
            audited = self.search_outputs(root, query)
            self.assertEqual(warm, audited, query)
            if query != "wombat":
                # "wombat" lives only in the replaced generation's tags; its
                # exit code is whatever today's path says, warm or audited.
                for arguments, (code, _stdout) in warm.items():
                    self.assertEqual(code, 0, (query, arguments))
            if self.engine_has_copy_path():
                # --audit runs on a copy (step 8, D5.3.3): no live publish.
                self.assertEqual(
                    self.witness_path(root).read_bytes(), witness,
                    ("--audit", query),
                )
            else:
                # Before step 8 merges, --audit is today's live full path,
                # which republishes; re-baseline for the next warm leg.
                witness = self.witness_path(root).read_bytes()
        tsv = self.search_outputs(root, "quokka", "warm_search_forbid_full_audit")[
            ("search", "--query", "quokka")
        ][1]
        locations = {line.split("\t")[0] for line in tsv.splitlines() if "\t" in line}
        self.assertTrue({"HOT", "COLD", "HELD", "HISTORY"} <= locations, tsv)
        self.assertIn(held, tsv)
        # The falsifier-only words find the Tier 3 pattern on the warm path.
        falsifier = self.search_outputs(
            root, "exactly once", "warm_search_forbid_full_audit"
        )[("search", "--query", "exactly once")][1]
        self.assertIn("pattern.sweep-double-fire", falsifier)

    def test_search_warm_miss_is_unchanged(self):
        # D6.4 output byte-identical: a miss keeps exit 1 and today's lines.
        root = self.workspace / "search"
        self.seed_search_store(root)
        tsv = self.worker(
            "warm_search_forbid_full_audit", "search", "--query", "zzz",
            root=root, check=False,
        )
        self.assertEqual(tsv.returncode, 1, tsv.stderr)
        self.assertEqual(tsv.stdout, "BIMRI: no memory matched zzz.\n")
        payload = self.worker(
            "warm_search_forbid_full_audit", "search", "--query", "zzz", "--json",
            root=root, check=False,
        )
        self.assertEqual(payload.returncode, 1, payload.stderr)
        self.assertEqual(
            json.loads(payload.stdout), {"matches": [], "omitted": 0, "total": 0}
        )

    def test_search_falls_to_copy_on_archive_drift(self):
        # D6.4: a live archive month that hashes differently from M0 is a
        # cache miss -> full path; output equals --audit; the next get is
        # still warm.
        root = self.workspace / "search"
        self.seed_search_store(root)
        month = sorted(root.joinpath(".bimri", "archive").glob("*.md"))[0]
        with month.open("a", encoding="utf-8", newline="\n") as handle:
            handle.write("\n")
        warm = self.cli("search", "--query", "quokka", root=root, check=False)
        audited = self.cli("search", "--query", "quokka", "--audit", root=root, check=False)
        self.assertEqual(
            (warm.returncode, warm.stdout), (audited.returncode, audited.stdout)
        )
        got = self.worker(
            "valid_witness_forbid_full_audit", "get", "--key",
            "launch.next-step", root=root,
        )
        self.assertIn("launch.next-step", got.stdout)

    def test_foreign_archive_row_is_caught_by_doctor_and_named(self):
        # D6.4 last sentence, section 06 4.6: parse_archive_record runs
        # parse_entry_line on raw_line; doctor and search (warm and --audit)
        # name `<month>.md:<n>: archived line is not a valid memory entry.`;
        # get stays warm.
        root = self.seeded_clone()
        month = sorted(root.joinpath(".bimri", "archive").glob("*.md"))[0]
        content = month.read_text("utf-8")
        if not content.endswith("\n"):
            content += "\n"
        foreign = (
            f"[ARCHIVED:{month.stem}-01] [BY:R999999-Q999] [closed] "
            # The v511 foreign row (test_v511_performance.py:3019-3023):
            # an entry line with no entry id.
            "[T2][K:archive-drift.foreign][I:3][S:agent][Q:working] "
            "Foreign row without a scoped stamp.\n"
        )
        month.write_bytes((content + foreign).encode("utf-8"))
        number = len((content + foreign).splitlines())
        named = (
            f"archive authority/recovery corruption in {month.name}:{number}: "
            + FOREIGN_ENTRY_ERROR
        )
        doctor = self.cli("doctor", root=root, check=False)
        self.assertNotEqual(doctor.returncode, 0, doctor.stdout)
        self.assertIn(named, doctor.stdout + doctor.stderr)
        for extra in ((), ("--audit",)):
            searched = self.cli(
                "search", "--query", "alpha", *extra, root=root, check=False
            )
            self.assertEqual(searched.returncode, 2, (extra, searched.stdout))
            self.assertIn(named, searched.stdout + searched.stderr)
        got = self.worker(
            "valid_witness_forbid_full_audit", "get", "--key",
            "anchor.resident", root=root,
        )
        self.assertIn("anchor.resident", got.stdout)

    # ---- section 06 7: the worst-case listing fits one tool call -----------

    @staticmethod
    def worst_case_key(prefix, number):
        head = f"{prefix}.s{number:03d}."
        return head + "x" * (80 - len(head))

    def test_cold_default_fits_one_tool_call_and_prefix_filters(self):
        # Section 06 2.1 / local decision 1: 150 cold subjects with
        # 80-character keys and 300-character texts; the 100-row default
        # plus both trailers stays under 30,000 characters; --prefix counts
        # within the filter.
        self.start("cold-worst-case")
        empty = self.hot_bytes()
        self.set_hot_budget(10 ** 7)
        run_id = self.start("cold-worst-case-fill")
        keys = []
        for number in range(150):
            key = self.worst_case_key("k" if number < 100 else "m", number)
            text = f"Row {number:03d} " + "w" * 292
            self.assertEqual(len(key), 80)
            self.assertEqual(len(text), 300)
            self.propose(
                run_id, key, text, "--importance", "5", "--status", "active",
                source="user", trust="confirmed",
            )
            keys.append(key)
        self.sync(run_id)
        base = self.hot_bytes()
        anchor = self.start("cold-worst-case-anchor")
        # The anchor carries the rows' provenance: from v5.2.0 (section 06
        # 3.2, local decision 17) an incoming generation scoring below the
        # residents it would displace cools itself; a tie still evicts.
        self.propose(
            anchor, "anchor.resident", "Anchor resident value 0.",
            "--importance", "5", "--status", "active",
            source="user", trust="confirmed",
        )
        self.sync(anchor)
        # Room for the empty view, the anchor and less than one worst-case
        # row (about 400 bytes): every row must cool.
        self.set_hot_budget(empty + (self.hot_bytes() - base) + 200)
        cooling = self.start("cold-worst-case-cool")
        self.propose(
            cooling, "anchor.resident", "Anchor resident value 1.",
            "--importance", "5", "--status", "active", new_subject=False,
            source="user", trust="confirmed",
        )
        self.sync(cooling)
        cold = self.state()["cold_current"]
        self.assertTrue(set(keys) <= set(cold), sorted(set(keys) - set(cold))[:3])
        self.set_hot_budget(DEFAULT_HOT_MAX_BYTES)
        self.warm(self.root)
        total = len(cold)

        listing = self.cli("cold")
        lines = listing.stdout.splitlines()
        self.assertLess(len(listing.stdout), 30000)
        self.assertEqual(len(lines), 102, lines[-3:])
        self.assertEqual(lines[100], f"BIMRI: {total} cold subjects.")
        self.assertEqual(
            lines[101],
            f"BIMRI: {total - 100} additional subjects omitted; narrow with "
            "--prefix <key-prefix> or use search.",
        )
        for line in lines[:100]:
            self.assertEqual(len(line.split("\t")), 7, line)
            self.assertEqual(len(line.split("\t")[6]), 160)

        narrowed = self.cli("cold", "--prefix", "k.").stdout.splitlines()
        self.assertEqual(len(narrowed), 101)
        self.assertEqual(narrowed[-1], "BIMRI: 100 cold subjects under k..")
        self.assertEqual(
            sorted(line.split("\t")[0] for line in narrowed[:-1]), keys[:100]
        )
        payload = self.cold_json("--prefix", "m.", "--limit", "20")
        self.assertEqual((payload["total"], payload["omitted"]), (50, 30))
        self.assertEqual(len(payload["subjects"]), 20)

    # ---- acceptance on a store-copy clone ---------------------------------

    def store_copy_clone(self):
        supplied = os.environ.get("BIMRI_STORE_COPY")
        if not supplied:
            self.skipTest("set BIMRI_STORE_COPY to run the optional store-copy checks")
        source = Path(supplied)
        if not (source / ".bimri" / "state.json").is_file():
            self.skipTest("store-copy not available")
        destination = self.workspace / f"store-copy-{uuid.uuid4().hex[:8]}"
        self.copy_store(source, destination)
        return destination

    def test_store_copy_cold_lists_190_rows(self):
        # PLAN step 9 acceptance: cold on a store-copy clone lists 190 rows;
        # the default (100) cut names 90 omitted and fits one tool call.
        root = self.store_copy_clone()
        full = self.cli("cold", "--limit", "5000", root=root)
        lines = full.stdout.splitlines()
        self.assertEqual(lines[-1], "BIMRI: 190 cold subjects.")
        self.assertEqual(len(lines) - 1, 190)
        default = self.cli("cold", root=root)
        self.assertLess(len(default.stdout), 30000)
        self.assertIn(
            "BIMRI: 90 additional subjects omitted; narrow with --prefix "
            "<key-prefix> or use search.",
            default.stdout,
        )

    def test_store_copy_search_warm_equals_audit(self):
        # PLAN step 9 acceptance: warm search equals --audit byte for byte on
        # the clone for three queries. Six full audits of a 54 MB store take
        # about ten minutes, so this leg runs only when BIMRI_STORE_COPY is
        # set (section 06 7, section 10 F4).
        root = self.store_copy_clone()
        for query in ("pea", "checkout", "patreon"):
            for extra in ((), ("--json",)):
                warm = self.cli("search", "--query", query, *extra, root=root, check=False)
                audited = self.cli(
                    "search", "--query", query, *extra, "--audit",
                    root=root, check=False, timeout=600,
                )
                self.assertEqual(
                    (warm.returncode, warm.stdout),
                    (audited.returncode, audited.stdout),
                    (query, extra, warm.stderr, audited.stderr),
                )


if __name__ == "__main__":
    unittest.main()
