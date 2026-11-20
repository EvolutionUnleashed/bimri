"""BIMRI v5.2 build step 11: ticket compaction and `maintain`.

Spine: design/DECISIONS.md D2.7 (receipts and compaction), D1.16 (`maintain`
adds compaction and the Tier 3 forecast line), section 10 R-Q (a retry
outside the receipts window is a new request) and D8.3 (the window sentence
lands in BIMRI-PROTOCOL.md with this step). Detail from design/DESIGN.md
section 02 2.3 and 2.10 (lookup order, receipts line, the today() clock
seam, local decision L18), section 03 3 and 6 (idempotency rule, L8 replay
from a receipts line), section 05 3.4 step 4 and L5 (doctor compacts only
when it ends PASSED; doctor --read-only never compacts), section 09 step 11
and section 10 (10.1.1 counter seam, 10.4 gate 2 and gate 3 tests).

Process model (section 10.0, 10.4 gate 2): every `maintain`, `doctor` and
retry `save` whose answer depends on the date runs in-process under a pinned
`engine.today` (the one clock seam, `today()` l.464), never as a plain
subprocess. Step 11 owns only this file, so the clock modes section 10 names
(`clock_plus_days_8`, `clock_plus_months_2`) are provided by a small driver
below that loads tests/crash_worker.py for `load_engine` and
`install_save_counter_seam` (the `save_crash_after_write_<n>` counter seam,
10.1.1), pins `engine.today` to an exact date and calls `engine.main`.
`engine.now_iso` is never pinned: compaction that read the wall clock would
see a zero-day-old ticket and move nothing.

Each test proves one promise; the comment above it names the spine id.
"""

import datetime as dt
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


REPOSITORY = Path(__file__).resolve().parents[1]
ENGINE = REPOSITORY / "bimri-engine.py"
CRASH_WORKER = REPOSITORY / "tests" / "crash_worker.py"
PROTOCOL = REPOSITORY / "BIMRI-PROTOCOL.md"

RUN_RE = re.compile(r"BIMRI RUN HANDLE: (R\d{6})")
SAVE_LINE_RE = re.compile(
    r"^BIMRI SAVE (accepted|unchanged|held|stale|conflict) (.+)$", re.M
)
RECEIPTS_NAME_RE = re.compile(r"^receipts-(\d{4}-\d{2})\.tsv$")
REVISION_COLUMN_RE = re.compile(r"^(-|V?\d{1,6})$")
FORECAST_PREFIX = "BIMRI retention forecast:"
TIER3_LINE_RE = re.compile(
    r"^Tier 3 patterns: (0 cooling-eligible\.|\d+ cooling-eligible; "
    r"lowest retention: \S+\.)$"
)
CRASH_POINT = ".test-save-crash-point"
CRASH_EXIT = 150

PROTECTED_EXCLUSIONS = {
    ".bimri/engine.lock",
    ".bimri/runtime.local.json",
    ".bimri/hooks.claude.local.json",
}

SEED_KEY = "seed.alpha"
SEED_TEXT = "Alpha seed value."
OWNER_KEY = "seed.owner"
OWNER_TEXT = "Owner confirmed rule."
MATRIX_KEY = "matrix.subject"
AGENT_REWRITE = "Agent rewrite of the owner rule."

# Section 10.4 gate 2 clock modes, generalised: pin engine.today to an exact
# ISO date ("-" leaves the real clock), optionally install the generic
# counter seam save_crash_after_write_<n> of crash_worker.py (10.1.1), then
# run one engine command in-process.
CLOCK_DRIVER = r"""
import importlib.util, sys
from pathlib import Path
engine_path, worker_path, pinned, crash, root = sys.argv[1:6]
command = sys.argv[6:]
spec = importlib.util.spec_from_file_location("bimri_step11_worker", worker_path)
worker = importlib.util.module_from_spec(spec)
spec.loader.exec_module(worker)
engine = worker.load_engine(engine_path)
if pinned != "-":
    engine.today = lambda: pinned
resolved = Path(root).resolve()
if crash != "0":
    worker.install_save_counter_seam(
        engine, resolved, crash=True, target=int(crash)
    )
sys.exit(engine.main(["--root", str(resolved)] + command))
"""

# The step 2 soak driver (test_v520_step2.py SOAK_DRIVER, the shape section
# 10.4 gives worker mode loop_save_<n>): many saves in one process.
SOAK_DRIVER = r"""
import contextlib, importlib.util, io, json, re, sys
engine_path, root, plan_path = sys.argv[1:4]
spec = importlib.util.spec_from_file_location("bimri_soak_target", engine_path)
engine = importlib.util.module_from_spec(spec)
spec.loader.exec_module(engine)
with open(plan_path, encoding="utf-8") as handle:
    plan = json.load(handle)
token = plan["token"]
done = 0
for index in range(plan["start"], plan["start"] + plan["count"]):
    arguments = [
        "save", "--request", "test-soak-%d" % index, "--observed", token,
        "--key", plan["key"], "--text", "Soak value number %d." % index,
    ]
    buffer = io.StringIO()
    with contextlib.redirect_stdout(buffer):
        code = engine.main(["--root", root] + arguments)
    text = buffer.getvalue()
    match = re.search(r"^BIMRI SAVE accepted .* token=(\S+)$", text, re.M)
    if code != 0 or match is None:
        sys.stderr.write("soak save %d failed: rc=%r\n%s\n" % (index, code, text))
        sys.exit(3)
    token = match.group(1)
    done += 1
print(json.dumps({"done": done, "token": token}))
"""


# ---- module helpers (copies of test_bimri_engine.py / test_v520_step2.py) --

def protected_tree_snapshot(root):
    """tests/test_bimri_engine.py:62: bytes of bimri.md and .bimri/."""
    root = Path(root)
    records = {}

    def record(path):
        if path.is_symlink():
            return ("symlink", os.readlink(str(path)))
        if path.is_dir():
            return ("directory", None)
        content = path.read_bytes()
        return ("file", hashlib.sha256(content).hexdigest())

    hot = root / "bimri.md"
    records["bimri.md"] = record(hot) if hot.exists() else ("missing", None)
    for current, directories, files in os.walk(str(root / ".bimri")):
        directories.sort()
        for name in sorted(directories) + sorted(files):
            path = Path(current) / name
            relative = path.relative_to(root).as_posix()
            if relative in PROTECTED_EXCLUSIONS:
                continue
            records[relative] = record(path)
    return records


def changed_paths(before, after):
    return sorted(
        path for path in set(before) | set(after)
        if before.get(path) != after.get(path)
    )


def audit_digest(value):
    encoded = json.dumps(value, separators=(",", ":"), sort_keys=True).encode(
        "utf-8"
    )
    return hashlib.sha256(encoded).hexdigest()


def record_seal(record, seal_field):
    """audit_record_seal (engine l.4246): digest of every other field."""
    return audit_digest(
        {key: value for key, value in record.items() if key != seal_field}
    )


def receipt_line_hash(columns):
    """D2.7 / 02 2.10: line_hash = line_hash("\\t".join(first seven)),
    engine line_hash = sha256 of the stripped text."""
    return hashlib.sha256(
        "\t".join(columns[:7]).strip().encode("utf-8")
    ).hexdigest()


def parse_save_line(stdout):
    match = SAVE_LINE_RE.search(stdout)
    if match is None:
        return None
    fields = {"result": match.group(1), "line": match.group(0)}
    for pair in match.group(2).split(" "):
        name, _, value = pair.partition("=")
        fields[name] = value
    return fields


def copy_store(source, destination):
    shutil.copytree(
        str(source),
        str(destination),
        ignore=shutil.ignore_patterns("engine.lock", "runtime.local.json"),
    )
    lock = Path(destination) / ".bimri" / "engine.lock"
    if not lock.exists():
        lock.write_bytes(b"")


def shift_days(day, days):
    return (dt.date.fromisoformat(day) + dt.timedelta(days=days)).isoformat()


def first_of_month_after(day, months):
    """The first day of the calendar month `months` after `day`'s month."""
    parsed = dt.date.fromisoformat(day)
    index = parsed.year * 12 + (parsed.month - 1) + months
    return dt.date(index // 12, index % 12 + 1, 1).isoformat()


class V520Step11Base(unittest.TestCase):
    maxDiff = None

    @classmethod
    def setUpClass(cls):
        cls._class_temp = tempfile.TemporaryDirectory(prefix="bimri-v520-s11-")
        cls.class_workspace = Path(cls._class_temp.name)
        cls.template = cls.class_workspace / "template"
        cls.build_template(cls.template)
        probe = cls.class_workspace / "probe"
        copy_store(cls.template, probe)
        cls.template_tokens = {}
        for key in (SEED_KEY, OWNER_KEY, MATRIX_KEY):
            result = cls.run_engine(probe, "get", "--key", key, "--json")
            try:
                payload = json.loads(result.stdout)
            except ValueError:
                raise AssertionError(
                    "template get failed for {}: rc={}\nstdout:\n{}\n"
                    "stderr:\n{}".format(
                        key, result.returncode, result.stdout, result.stderr
                    )
                )
            cls.template_tokens[key] = cls.token_from_payload(payload, key)
        shutil.rmtree(str(probe), ignore_errors=True)

    @classmethod
    def tearDownClass(cls):
        cls._class_temp.cleanup()

    def setUp(self):
        self._temp = tempfile.TemporaryDirectory(prefix="bimri-v520-s11-")
        self.workspace = Path(self._temp.name)
        self.root = self.workspace / "store"
        copy_store(self.template, self.root)

    def tearDown(self):
        self._temp.cleanup()

    # ---- process helpers (test_v512_defects.py:59 recipe) ------------------

    @classmethod
    def run_engine(cls, root, *arguments, timeout=600):
        command = [
            sys.executable, str(ENGINE), "--root", str(root),
        ] + [str(argument) for argument in arguments]
        return subprocess.run(
            command, text=True, capture_output=True, timeout=timeout,
            encoding="utf-8",
        )

    @staticmethod
    def token_from_payload(payload, key):
        for match in payload.get("matches", []):
            if match.get("key") == key and match.get("location") in ("HOT", "COLD"):
                return match["token"]
        return payload["token"]

    @classmethod
    def class_cli(cls, root, *arguments):
        result = cls.run_engine(root, *arguments)
        if result.returncode != 0:
            raise AssertionError(
                "seed command failed: {!r}\nstdout:\n{}\nstderr:\n{}".format(
                    arguments, result.stdout, result.stderr
                )
            )
        return result

    @classmethod
    def build_template(cls, root):
        # Legacy lane (start --open-run, D1.1): one agent/working subject and
        # one user/confirmed subject, then one read publishes the witness.
        started = cls.class_cli(root, "start", "--open-run", "--actor", "seed")
        run_id = RUN_RE.search(started.stdout).group(1)
        cls.class_cli(
            root, "propose", "--run", run_id, "--tier", "2", "--key",
            SEED_KEY, "--text", SEED_TEXT, "--source", "agent", "--trust",
            "working", "--new-subject",
        )
        cls.class_cli(
            root, "propose", "--run", run_id, "--tier", "2", "--key",
            OWNER_KEY, "--text", OWNER_TEXT, "--source", "user", "--trust",
            "confirmed", "--new-subject",
        )
        cls.class_cli(root, "sync", "--run", run_id)
        cls.class_cli(
            root, "close", "--run", run_id, "--outcome", "success",
            "--summary", "seeded",
        )
        cls.run_engine(root, "get", "--key", SEED_KEY, "--json")

    def cli(self, *arguments, root=None, check=True):
        result = self.run_engine(root or self.root, *arguments)
        if check and result.returncode != 0:
            self.fail(
                "command failed: {!r}\nstdout:\n{}\nstderr:\n{}".format(
                    arguments, result.stdout, result.stderr
                )
            )
        return result

    def pinned(self, day, *arguments, root=None, crash=0, timeout=900):
        """One engine command in-process with engine.today pinned to `day`."""
        command = [
            sys.executable, "-c", CLOCK_DRIVER, str(ENGINE), str(CRASH_WORKER),
            day, str(crash), str(root or self.root),
        ] + [str(argument) for argument in arguments]
        return subprocess.run(
            command, text=True, capture_output=True, timeout=timeout,
            encoding="utf-8",
        )

    def pinned_ok(self, day, *arguments, root=None):
        result = self.pinned(day, *arguments, root=root)
        if result.returncode != 0:
            self.fail(
                "pinned command failed ({}): {!r}\nstdout:\n{}\nstderr:\n{}".format(
                    day, arguments, result.stdout, result.stderr
                )
            )
        return result

    def maintain(self, day, root=None):
        return self.pinned_ok(day, "maintain", root=root)

    def save_arguments(self, rid, key, observed, text=None, new_subject=False,
                       json_out=False):
        arguments = ["save", "--request", rid, "--observed", observed,
                     "--key", key]
        if new_subject:
            arguments.extend(["--new-subject", "--tier", "2"])
        if text is not None:
            arguments.extend(["--text", text])
        if json_out:
            arguments.append("--json")
        return arguments

    def save(self, rid, key, observed, text=None, new_subject=False,
             root=None, json_out=False):
        return self.cli(
            *self.save_arguments(rid, key, observed, text, new_subject, json_out),
            root=root, check=False,
        )

    def pinned_save(self, day, rid, key, observed, text=None,
                    new_subject=False, root=None, json_out=False):
        return self.pinned(
            day,
            *self.save_arguments(rid, key, observed, text, new_subject, json_out),
            root=root,
        )

    def assert_save(self, result, outcome, code):
        self.assertEqual(result.returncode, code, result.stdout + result.stderr)
        fields = parse_save_line(result.stdout)
        self.assertIsNotNone(fields, result.stdout + result.stderr)
        self.assertEqual(fields["result"], outcome, result.stdout)
        return fields

    # ---- store readers -----------------------------------------------------

    def bdir(self, root=None):
        return Path(root or self.root) / ".bimri"

    def state(self, root=None):
        return json.loads(self.bdir(root).joinpath("state.json").read_text("utf-8"))

    def key_token(self, key, root=None):
        result = self.cli("get", "--key", key, "--json", root=root, check=False)
        self.assertIn(result.returncode, (0, 1), result.stdout + result.stderr)
        return self.token_from_payload(json.loads(result.stdout), key)

    def done_dir(self, root=None):
        return self.bdir(root) / "saves" / "done"

    def open_dir(self, root=None):
        return self.bdir(root) / "saves" / "open"

    def done_ticket_path(self, rid, root=None):
        return self.done_dir(root) / "{}.json".format(rid)

    def done_ids(self, root=None):
        return sorted(path.stem for path in self.done_dir(root).glob("*.json"))

    def open_ids(self, root=None):
        directory = self.open_dir(root)
        if not directory.is_dir():
            return []
        return sorted(path.stem for path in directory.glob("*.json"))

    def done_ticket(self, rid, root=None):
        return json.loads(self.done_ticket_path(rid, root).read_text("utf-8"))

    def finished_day(self, rid, root=None):
        return self.done_ticket(rid, root)["finished_at"][:10]

    def latest_finished_day(self, root=None):
        return max(
            json.loads(path.read_text("utf-8"))["finished_at"][:10]
            for path in self.done_dir(root).glob("*.json")
        )

    def receipt_files(self, root=None):
        directory = self.bdir(root) / "saves"
        return sorted(
            path.name for path in directory.glob("receipts-*.tsv")
        )

    def receipt_lines(self, root=None):
        """{file name: [columns, ...]} for every receipts file."""
        found = {}
        directory = self.bdir(root) / "saves"
        for path in sorted(directory.glob("receipts-*.tsv")):
            raw = path.read_bytes().decode("utf-8")
            found[path.name] = [
                line.split("\t") for line in raw.splitlines() if line.strip()
            ]
        return found

    def receipt_ids(self, root=None):
        ids = []
        for rows in self.receipt_lines(root).values():
            ids.extend(columns[0] for columns in rows)
        return ids

    def revision_numbers(self, root=None):
        return sorted(
            int(path.stem[1:])
            for path in self.bdir(root).joinpath("revisions").glob("V*.md")
        )

    def log_names(self, root=None):
        return sorted(
            path.name for path in self.bdir(root).joinpath("log").glob("R*.md")
        )

    def assert_receipt_matches_ticket(self, columns, ticket):
        """D2.7 / 02 2.10 line shape: request_id, intent_hash, outcome,
        run_id, proposal_id, revision, finished_at, line_hash; null planned
        fields render as "-"."""
        self.assertEqual(len(columns), 8, columns)
        self.assertEqual(columns[0], ticket["request_id"])
        self.assertEqual(columns[1], ticket["intent_hash"])
        self.assertEqual(columns[2], ticket["result"]["outcome"])
        self.assertEqual(columns[3], ticket["run_id"] or "-")
        self.assertEqual(columns[4], ticket["proposal_id"] or "-")
        self.assertRegex(columns[5], REVISION_COLUMN_RE)
        if ticket["revision"] is not None:
            self.assertEqual(int(columns[5].lstrip("V")), ticket["revision"])
        self.assertEqual(columns[6], ticket["finished_at"])
        self.assertEqual(columns[7], receipt_line_hash(columns))

    def seed_save(self, rid, text, root=None):
        """An accepted save of SEED_KEY from the template's observation."""
        result = self.save(
            rid, SEED_KEY, self.template_tokens[SEED_KEY], text, root=root
        )
        return self.assert_save(result, "accepted", 0)


class CompactionTest(V520Step11Base):
    """D2.7: maintain and doctor (never a save) move done/ tickets older than
    seven days by today() into receipts-<finished_at[:7]>.tsv."""

    # Spine D2.7 (02 2.10): every done/ ticket older than 7 days becomes one
    # line of receipts-<finished_at[:7]>.tsv with the exact eight columns and
    # a reproducing line_hash, and its JSON is unlinked; open/ is untouched.
    def test_maintain_moves_old_done_tickets_into_monthly_receipts(self):
        self.assert_save(
            self.save(
                "test-compact-unchanged", SEED_KEY,
                self.template_tokens[SEED_KEY], SEED_TEXT,
            ),
            "unchanged", 0,
        )
        self.seed_save("test-compact-accepted", "Compacted value.")
        self.assert_save(
            self.save(
                "test-compact-stale", SEED_KEY,
                self.template_tokens[SEED_KEY], "Too late.",
            ),
            "stale", 1,
        )
        ids = ["test-compact-accepted", "test-compact-stale",
               "test-compact-unchanged"]
        tickets = {rid: self.done_ticket(rid) for rid in ids}
        self.maintain(shift_days(self.latest_finished_day(), 8))
        self.assertEqual(self.done_ids(), [])
        self.assertEqual(self.open_ids(), [])
        lines = self.receipt_lines()
        expected_files = sorted(set(
            "receipts-{}.tsv".format(ticket["finished_at"][:7])
            for ticket in tickets.values()
        ))
        self.assertEqual(sorted(lines), expected_files)
        seen = {}
        for name, rows in lines.items():
            for columns in rows:
                self.assertNotIn(columns[0], seen, "duplicate receipt line")
                seen[columns[0]] = columns
                ticket = tickets[columns[0]]
                self.assertEqual(
                    name, "receipts-{}.tsv".format(ticket["finished_at"][:7])
                )
                self.assert_receipt_matches_ticket(columns, ticket)
        self.assertEqual(sorted(seen), ids)
        self.assertEqual(seen["test-compact-stale"][3:5], ["-", "-"])
        self.assertEqual(seen["test-compact-unchanged"][3:5], ["-", "-"])

    # Spine D2.7, L18, 10.4 gate 2 cross-reference
    # (test_compaction_age_and_receipt_window_read_only_the_pinned_today):
    # the age test and the lookup window read only the pinned today(); a
    # ticket exactly 7 days old stays, 8 days old moves; on the 1st of the
    # next month the id replays from last month's receipts; two months on
    # it is a new request.
    def test_compaction_age_and_receipt_window_read_only_the_pinned_today(self):
        rid = "test-window-1"
        original = self.seed_save(rid, "Window value.")
        ticket = self.done_ticket(rid)
        finished = ticket["finished_at"][:10]
        receipts_name = "receipts-{}.tsv".format(ticket["finished_at"][:7])

        self.maintain(shift_days(finished, 7))
        self.assertEqual(self.done_ids(), [rid])
        self.assertEqual(self.receipt_files(), [])

        self.maintain(shift_days(finished, 8))
        self.assertEqual(self.done_ids(), [])
        self.assertEqual(self.receipt_files(), [receipts_name])
        self.assertEqual(self.receipt_ids(), [rid])

        runs = self.state()["run_count"]
        revisions = self.revision_numbers()
        next_month = first_of_month_after(finished, 1)
        replay = self.pinned_save(
            next_month, rid, SEED_KEY, self.template_tokens[SEED_KEY],
            "Window value.",
        )
        fields = self.assert_save(replay, "accepted", 0)
        self.assertEqual(fields["line"], original["line"])
        self.assertEqual(self.done_ids(), [])

        two_on = first_of_month_after(finished, 2)
        fresh = self.pinned_save(
            two_on, rid, SEED_KEY, self.template_tokens[SEED_KEY],
            "Window value.", json_out=True,
        )
        self.assertEqual(fresh.returncode, 0, fresh.stdout + fresh.stderr)
        payload = json.loads(fresh.stdout.strip().splitlines()[-1])
        self.assertEqual(payload["result"], "unchanged")
        self.assertFalse(payload["replayed"])
        self.assertEqual(self.done_ids(), [rid])
        self.assertIsNone(self.done_ticket(rid)["run_id"])
        self.assertEqual(self.state()["run_count"], runs)
        self.assertEqual(self.revision_numbers(), revisions)

    # Spine D2.7 ("maintain and doctor, never a save"): a save under a clock
    # that makes an existing done/ ticket eight days old compacts nothing.
    def test_save_never_compacts(self):
        self.seed_save("test-never-1", "First value.")
        day = shift_days(self.finished_day("test-never-1"), 8)
        result = self.pinned_save(
            day, "test-never-2", SEED_KEY, self.key_token(SEED_KEY),
            "Second value.",
        )
        self.assert_save(result, "accepted", 0)
        self.assertEqual(self.done_ids(), ["test-never-1", "test-never-2"])
        self.assertEqual(self.receipt_files(), [])

    # Spine D2.7 (doctor compacts too), section 05 3.4 step 4 and L5: a
    # writable doctor that ends PASSED compacts; doctor --read-only never
    # does and leaves the done/ ticket and the saves/ directory untouched.
    def test_doctor_compacts_and_read_only_doctor_does_not(self):
        rid = "test-doctor-1"
        self.seed_save(rid, "Doctor value.")
        day = shift_days(self.finished_day(rid), 8)
        sibling = self.workspace / "read-only"
        copy_store(self.root, sibling)

        read_only = self.pinned(
            day, "doctor", "--read-only", "--budget-ms", "600000", root=sibling
        )
        self.assertEqual(read_only.returncode, 0, read_only.stdout + read_only.stderr)
        self.assertEqual(self.done_ids(sibling), [rid])
        self.assertEqual(self.receipt_files(sibling), [])

        writable = self.pinned(day, "doctor", "--budget-ms", "600000")
        self.assertEqual(writable.returncode, 0, writable.stdout + writable.stderr)
        self.assertIn("PASSED", writable.stdout)
        self.assertEqual(self.done_ids(), [])
        self.assertEqual(self.receipt_ids(), [rid])

    # Spine D2.7 ("a crash between the two leaves both"), section 09 step 11
    # (save_crash_after_write_<n> on maintain): at every durable boundary of
    # maintain, a crash followed by a clean maintain ends with every ticket
    # compacted exactly once: no done/ JSON left, one receipts line per id.
    def test_compaction_is_idempotent_across_a_crash_between_append_and_unlink(self):
        self.seed_save("test-crash-a", "Crash value A.")
        self.assert_save(
            self.save(
                "test-crash-b", SEED_KEY, self.template_tokens[SEED_KEY],
                "Crash value B.",
            ),
            "stale", 1,
        )
        ids = ["test-crash-a", "test-crash-b"]
        day = shift_days(self.latest_finished_day(), 8)
        prepared = self.workspace / "prepared"
        copy_store(self.root, prepared)
        labels = []
        both_seen = False
        completed = False
        for number in range(1, 120):
            root = self.workspace / "n{}".format(number)
            copy_store(prepared, root)
            result = self.pinned(day, "maintain", root=root, crash=number)
            point = root / CRASH_POINT
            if not point.is_file():
                self.assertEqual(
                    result.returncode, 0, result.stdout + result.stderr
                )
                self.assertEqual(self.done_ids(root), [])
                self.assertEqual(sorted(self.receipt_ids(root)), ids)
                completed = True
                shutil.rmtree(str(root), ignore_errors=True)
                break
            self.assertEqual(
                result.returncode, CRASH_EXIT, result.stdout + result.stderr
            )
            label = point.read_text("utf-8").strip().partition("\t")[2]
            label = label.replace("\\", "/")
            point.unlink()
            labels.append(label)
            if RECEIPTS_NAME_RE.match(label.rsplit("/", 1)[-1]):
                # Crash right after the append: the line is durable and at
                # least one JSON ticket it names is still in done/.
                in_receipts = set(self.receipt_ids(root))
                in_done = set(self.done_ids(root))
                self.assertTrue(in_receipts & in_done, (label, in_done))
                both_seen = True
            with self.subTest(n=number, label=label):
                again = self.pinned(day, "maintain", root=root)
                self.assertEqual(
                    again.returncode, 0, again.stdout + again.stderr
                )
                self.assertEqual(self.done_ids(root), [])
                self.assertEqual(sorted(self.receipt_ids(root)), ids)
                tickets = {
                    rid: json.loads(
                        (prepared / ".bimri" / "saves" / "done" /
                         "{}.json".format(rid)).read_text("utf-8")
                    )
                    for rid in ids
                }
                for rows in self.receipt_lines(root).values():
                    for columns in rows:
                        self.assert_receipt_matches_ticket(
                            columns, tickets[columns[0]]
                        )
            shutil.rmtree(str(root), ignore_errors=True)
        self.assertTrue(completed, labels)
        self.assertTrue(both_seen, labels)

    # Spine D2.7 ("JSON wins at lookup"): with a request in both done/ and a
    # receipts line, a retry replays the JSON ticket's recorded line (its
    # own token, not the live token a receipt replay renders, L8), and the
    # next compaction unlinks the JSON without appending a second line.
    def test_ticket_in_both_done_and_receipts_replays_the_json(self):
        rid = "test-both-1"
        original = self.seed_save(rid, "Both value.")
        kept = self.done_ticket_path(rid).read_bytes()
        day = shift_days(self.finished_day(rid), 8)
        self.maintain(day)
        self.assertEqual(self.receipt_ids(), [rid])
        self.assert_save(
            self.save(
                "test-both-mover", SEED_KEY, self.key_token(SEED_KEY),
                "Moved on.",
            ),
            "accepted", 0,
        )
        # The state an interrupted compaction leaves: the line and the JSON.
        self.done_ticket_path(rid).write_bytes(kept)
        replay = self.pinned_save(
            day, rid, SEED_KEY, self.template_tokens[SEED_KEY], "Both value."
        )
        fields = self.assert_save(replay, "accepted", 0)
        self.assertEqual(fields["line"], original["line"])
        self.maintain(shift_days(self.latest_finished_day(), 8))
        self.assertEqual(self.done_ids(), [])
        self.assertEqual(
            sorted(self.receipt_ids()), sorted([rid, "test-both-mover"])
        )


class CompactedReplayTest(V520Step11Base):
    """D2.7 lookup order, section 03 6 and L8: a compacted id inside the
    window is the same request."""

    # Spine D2.7 lookup order, section 03 L8: a compacted accepted id
    # replays its exact line with exit 0, writes nothing, and --json says
    # replayed; a compacted held id replays its held line with exit 1.
    def test_compacted_id_replays_its_line(self):
        accepted = self.seed_save("test-replay-accepted", "Replay value.")
        held = self.assert_save(
            self.save(
                "test-replay-held", OWNER_KEY,
                self.template_tokens[OWNER_KEY], AGENT_REWRITE,
            ),
            "held", 1,
        )
        day = shift_days(self.latest_finished_day(), 8)
        self.maintain(day)
        self.assertEqual(self.done_ids(), [])
        before = protected_tree_snapshot(self.root)
        with self.subTest(outcome="accepted"):
            result = self.pinned_save(
                day, "test-replay-accepted", SEED_KEY,
                self.template_tokens[SEED_KEY], "Replay value.",
            )
            fields = self.assert_save(result, "accepted", 0)
            self.assertEqual(fields["line"], accepted["line"])
        with self.subTest(outcome="held"):
            result = self.pinned_save(
                day, "test-replay-held", OWNER_KEY,
                self.template_tokens[OWNER_KEY], AGENT_REWRITE,
            )
            fields = self.assert_save(result, "held", 1)
            self.assertEqual(fields["line"], held["line"])
        with self.subTest(output="json"):
            result = self.pinned_save(
                day, "test-replay-accepted", SEED_KEY,
                self.template_tokens[SEED_KEY], "Replay value.", json_out=True,
            )
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            payload = json.loads(result.stdout.strip().splitlines()[-1])
            self.assertEqual(payload["result"], "accepted")
            self.assertTrue(payload["replayed"])
            self.assertEqual(payload["run"], accepted["run"])
            self.assertEqual(payload["proposal"], accepted["proposal"])
        self.assertEqual(
            changed_paths(before, protected_tree_snapshot(self.root)), []
        )

    # Spine D2.7 with section 03 6 rule 2: inside the window a compacted id
    # reused with different content is failed, exit 2, and nothing is
    # written or allocated.
    def test_compacted_id_with_different_content_fails(self):
        rid = "test-reuse-1"
        self.seed_save(rid, "First content.")
        day = shift_days(self.finished_day(rid), 8)
        self.maintain(day)
        before = protected_tree_snapshot(self.root)
        result = self.pinned_save(
            day, rid, SEED_KEY, self.template_tokens[SEED_KEY],
            "Other content.",
        )
        self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
        self.assertIsNone(parse_save_line(result.stdout), result.stdout)
        self.assertIn("BIMRI ERROR: save {} failed:".format(rid), result.stderr)
        self.assertEqual(
            changed_paths(before, protected_tree_snapshot(self.root)), []
        )

    # Spine R-Q, D2.7, section 03 6 ("a retry outside the receipts window is
    # a new request and lands on rule 5"), section 10.4 gate 3
    # (test_retry_outside_the_receipt_window_is_a_new_request): compacted two
    # months on, retried a third month on, the same id and intent is
    # `unchanged` (proposal_equivalent) without "replayed"; after the
    # subject moved, the same retry is `stale`; never a second run,
    # revision or effect.
    def test_retry_outside_the_receipt_window_is_a_new_request(self):
        rid = "test-outside-1"
        text = "Outside window value."
        self.seed_save(rid, text)
        finished = self.finished_day(rid)
        self.maintain(first_of_month_after(finished, 2))
        self.assertEqual(self.done_ids(), [])
        self.assertEqual(self.receipt_ids(), [rid])
        third = first_of_month_after(finished, 3)
        compacted = self.workspace / "compacted"
        copy_store(self.root, compacted)

        with self.subTest(result="unchanged"):
            runs = self.state()["run_count"]
            revisions = self.revision_numbers()
            logs = self.log_names()
            result = self.pinned_save(
                third, rid, SEED_KEY, self.template_tokens[SEED_KEY], text,
                json_out=True,
            )
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            payload = json.loads(result.stdout.strip().splitlines()[-1])
            self.assertEqual(payload["result"], "unchanged")
            self.assertFalse(payload["replayed"])
            self.assertIsNone(payload["run"])
            self.assertEqual(self.state()["run_count"], runs)
            self.assertEqual(self.revision_numbers(), revisions)
            self.assertEqual(self.log_names(), logs)
            self.assertIsNone(self.done_ticket(rid)["run_id"])

        with self.subTest(result="stale"):
            mover = self.pinned_save(
                third, "test-outside-mover", SEED_KEY,
                self.key_token(SEED_KEY, root=compacted), "Moved since.",
                root=compacted,
            )
            self.assert_save(mover, "accepted", 0)
            runs = self.state(compacted)["run_count"]
            revisions = self.revision_numbers(compacted)
            current = self.key_token(SEED_KEY, root=compacted)
            result = self.pinned_save(
                third, rid, SEED_KEY, self.template_tokens[SEED_KEY], text,
                root=compacted,
            )
            fields = self.assert_save(result, "stale", 1)
            self.assertEqual(fields["current"], current)
            self.assertEqual(self.state(compacted)["run_count"], runs)
            self.assertEqual(self.revision_numbers(compacted), revisions)
            self.assertEqual(self.key_token(SEED_KEY, root=compacted), current)


class MaintainOutputTest(V520Step11Base):
    """D1.16: maintain prints the compaction count and the Tier 3 line."""

    def compaction_lines(self, stdout):
        return [
            line for line in stdout.splitlines()
            if "compact" in line.lower()
        ]

    # Spine D1.16 (section 09 step 11 "maintain prints the compaction
    # count"): one line names the number of tickets moved, 2 the first time
    # and 0 when nothing is old enough.
    def test_maintain_prints_the_compaction_count(self):
        self.assert_save(
            self.save(
                "test-count-2", SEED_KEY, self.template_tokens[SEED_KEY],
                SEED_TEXT,
            ),
            "unchanged", 0,
        )
        self.seed_save("test-count-1", "Count value.")
        day = shift_days(self.latest_finished_day(), 8)
        first = self.maintain(day)
        lines = self.compaction_lines(first.stdout)
        self.assertEqual(len(lines), 1, first.stdout)
        self.assertRegex(lines[0], r"(?<![\d.])2(?![\d.])")
        second = self.maintain(day)
        lines = self.compaction_lines(second.stdout)
        self.assertEqual(len(lines), 1, second.stdout)
        self.assertRegex(lines[0], r"(?<![\d.])0(?![\d.])")
        self.assertNotRegex(lines[0], r"(?<![\d.])2(?![\d.])")

    # Spine D1.16, section 06 3.2: the Tier 3 forecast line stays directly
    # after the retention forecast line, alongside the compaction count.
    def test_maintain_keeps_the_tier3_line_after_the_forecast(self):
        self.seed_save("test-tier3-line-1", "Tier three line value.")
        result = self.maintain(shift_days(self.latest_finished_day(), 8))
        lines = result.stdout.splitlines()
        forecast = [
            index for index, line in enumerate(lines)
            if line.startswith(FORECAST_PREFIX)
        ]
        self.assertEqual(len(forecast), 1, result.stdout)
        self.assertRegex(lines[forecast[0] + 1], TIER3_LINE_RE)
        self.assertEqual(lines[forecast[0] + 1], "Tier 3 patterns: 0 cooling-eligible.")
        self.assertEqual(len(self.compaction_lines(result.stdout)), 1, result.stdout)


class CompactionAcceptanceTest(V520Step11Base):
    """Section 09 step 11 acceptance: 1,100 finished tickets."""

    # Spine D2.7, section 09 step 11 acceptance: maintain on a store with
    # 1,100 finished tickets leaves exactly the unexpired JSON tickets (age
    # <= 7 days by the pinned today) and one receipts file per month of the
    # expired ones, each line naming its ticket once.
    def test_maintain_on_eleven_hundred_finished_tickets(self):
        self.seed_save("test-bulk-source", "Bulk source value.")
        source = self.done_ticket("test-bulk-source")
        pinned_today = shift_days(source["finished_at"][:10], 1)
        done = self.done_dir()
        expected_left = {"test-bulk-source"}
        expected_by_month = {}
        tickets = {}
        for index in range(1100):
            rid = "test-bulk-{:04d}".format(index)
            age = index % 150
            day = shift_days(pinned_today, -age)
            record = dict(source)
            record["request_id"] = rid
            record["finished_at"] = day + "T12:00:00Z"
            record["ticket_hash"] = record_seal(record, "ticket_hash")
            done.joinpath(rid + ".json").write_text(
                json.dumps(record, indent=2, sort_keys=True) + "\n",
                encoding="utf-8",
            )
            tickets[rid] = record
            if age > 7:
                expected_by_month.setdefault(
                    "receipts-{}.tsv".format(day[:7]), set()
                ).add(rid)
            else:
                expected_left.add(rid)
        self.maintain(pinned_today)
        self.assertEqual(set(self.done_ids()), expected_left)
        lines = self.receipt_lines()
        self.assertEqual(sorted(lines), sorted(expected_by_month))
        for name, rows in lines.items():
            ids = [columns[0] for columns in rows]
            self.assertEqual(len(ids), len(set(ids)), name)
            self.assertEqual(set(ids), expected_by_month[name], name)
            for columns in rows:
                self.assert_receipt_matches_ticket(columns, tickets[columns[0]])


class ThousandSaveCompactionTest(V520Step11Base):
    """Gate 2 soak with the compaction assertions step 11 adds."""

    # Spine D3.2 and gate 2 (test_successful_saves_leave_no_open_run_over_
    # one_thousand_saves) with D2.7: 1,000 saves leave no open run; eight
    # days on one more save leaves done/ at 1,001 (a save never compacts);
    # maintain empties done/ into receipts (1,001 lines); a compacted id
    # replays its exact line; doctor on a second copy compacts the same.
    def test_successful_saves_leave_no_open_run_over_one_thousand_saves(self):
        count = int(os.environ.get("BIMRI_V520_SOAK_SAVES", "1000"))
        by_subprocess = max(1, count // 5)
        before = self.state()
        token = self.template_tokens[MATRIX_KEY]
        first_line = None
        for index in range(by_subprocess):
            result = self.save(
                "test-soak-{}".format(index), MATRIX_KEY, token,
                "Soak value number {}.".format(index),
                new_subject=(index == 0),
            )
            fields = self.assert_save(result, "accepted", 0)
            if index == 0:
                first_line = fields
            token = fields["token"]
        remaining = count - by_subprocess
        if remaining:
            plan = self.workspace / "soak-plan.json"
            plan.write_text(json.dumps({
                "token": token, "key": MATRIX_KEY, "start": by_subprocess,
                "count": remaining,
            }), encoding="utf-8")
            driven = subprocess.run(
                [sys.executable, "-c", SOAK_DRIVER, str(ENGINE),
                 str(self.root), str(plan)],
                text=True, capture_output=True, timeout=7200,
                encoding="utf-8",
            )
            self.assertEqual(driven.returncode, 0, driven.stderr[-4000:])
            summary = json.loads(driven.stdout.strip().splitlines()[-1])
            self.assertEqual(summary["done"], remaining)
            token = summary["token"]
        after = self.state()
        self.assertEqual(after["active_runs"], before["active_runs"])
        self.assertEqual(after["session_runs"], before["session_runs"])
        self.assertEqual(after["run_count"], before["run_count"] + count)
        self.assertEqual(self.open_ids(), [])
        self.assertEqual(len(self.done_ids()), count)
        headers = {}
        for path in self.bdir().joinpath("log").glob("R*.md"):
            text = path.read_text("utf-8")
            header = text.split("\n", 1)[0].rstrip("\r")
            marker = "| save:"
            if marker in header:
                headers[header.rsplit(marker, 1)[1]] = text
        for index in range(count):
            text = headers.get("test-soak-{}".format(index))
            self.assertIsNotNone(text, index)
            self.assertIn("[CLOSED:", text)
        result = self.cli(
            "doctor", "--read-only", "--budget-ms", "600000", check=False
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("PASSED", result.stdout)

        # A save never compacts (D2.7): eight days on, done/ grows to 1,001.
        extra_day = shift_days(self.latest_finished_day(), 8)
        extra = self.pinned_save(
            extra_day, "test-soak-extra", MATRIX_KEY, token,
            "Soak value extra.",
        )
        extra_line = self.assert_save(extra, "accepted", 0)
        self.assertEqual(len(self.done_ids()), count + 1)
        self.assertEqual(self.receipt_files(), [])
        second = self.workspace / "doctor-copy"
        copy_store(self.root, second)
        day = shift_days(self.latest_finished_day(), 8)

        # maintain empties done/ into receipts, one line per ticket.
        self.maintain(day)
        self.assertEqual(self.done_ids(), [])
        ids = self.receipt_ids()
        self.assertEqual(len(ids), count + 1)
        self.assertEqual(len(set(ids)), count + 1)
        for name in self.receipt_files():
            self.assertRegex(name, RECEIPTS_NAME_RE)

        # A compacted id replays its exact line (D2.7 lookup order, L8).
        replay = self.pinned_save(
            day, "test-soak-extra", MATRIX_KEY, token, "Soak value extra."
        )
        self.assertEqual(
            self.assert_save(replay, "accepted", 0)["line"], extra_line["line"]
        )
        early = self.pinned_save(
            day, "test-soak-0", MATRIX_KEY, self.template_tokens[MATRIX_KEY],
            "Soak value number 0.", new_subject=True,
        )
        early_fields = self.assert_save(early, "accepted", 0)
        for name in ("run", "proposal", "decision", "revision"):
            self.assertEqual(early_fields[name], first_line[name], name)
        self.assertEqual(self.state()["run_count"], before["run_count"] + count + 1)

        # The same on a second copy with doctor in place of maintain.
        doctor = self.pinned(day, "doctor", "--budget-ms", "600000", root=second)
        self.assertEqual(doctor.returncode, 0, doctor.stdout + doctor.stderr)
        self.assertIn("PASSED", doctor.stdout)
        self.assertEqual(self.done_ids(second), [])
        self.assertEqual(len(set(self.receipt_ids(second))), count + 1)
        self.assertEqual(len(self.receipt_ids(second)), count + 1)


class ProtocolWindowSentenceTest(unittest.TestCase):
    """D8.3: the request-identity window sentence lands with step 11."""

    # Spine D2.7 and D8.3 (section 09 step 11 protocol text): BIMRI-PROTOCOL.md
    # carries the window sentence (backticks and line wrapping ignored).
    def test_protocol_states_the_request_identity_window(self):
        text = PROTOCOL.read_text(encoding="utf-8").replace("`", "")
        normalised = " ".join(text.split())
        sentence = (
            "A request id is recognised while its ticket is in open/ or "
            "done/ or in the receipts of this or the previous month; a retry "
            "outside that window is a new request and yields unchanged or "
            "stale, never a second effect."
        )
        self.assertIn(sentence, normalised)


if __name__ == "__main__":
    unittest.main()
