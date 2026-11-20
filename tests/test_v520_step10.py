"""BIMRI v5.2 build step 10: retention provenance and Tier 3 cooling.

Spine: design/DECISIONS.md D2.10 (Tier 3 cold-current records), D6.3
(retention score with trust and source weights, Tier 3 score, Tier 3 cooling
under pressure, Tier 1 never cooled), D0.6 (a Tier 3 cooling is an authority
write and therefore one-way). Detail from design/DESIGN.md section 06 part 3
(3.1 score, 3.2 candidates and run dates, 3.3 cold records) and section 09
step 10.

Every test drives the public command boundary in separate processes, the same
way tests/test_v510_lifecycle.py does. Byte pressure is produced the way that
file produces it: ``hot_max_bytes`` is set to the current view plus 32 bytes and
one more subject is admitted, so exactly one resident cools. Every incoming
subject is user/confirmed importance 5 dated today (score 12.5), above every
fixture resident, so the cooled victim is decided by the retention order alone.

The 5.1.2 comparison engine is release commit 69757b3, materialised with
``git archive`` exactly as tests/test_v510_lifecycle.py:272 does.
"""

import datetime as dt
import hashlib
import io
import json
import os
import re
import subprocess
import sys
import tarfile
import tempfile
import unittest
from pathlib import Path


REPOSITORY = Path(__file__).resolve().parents[1]
ENGINE = REPOSITORY / "bimri-engine.py"
PRIOR_RELEASE_COMMIT = "69757b3"
RUN_RE = re.compile(r"BIMRI RUN HANDLE: (R\d{6})")
PROPOSAL_ID_RE = re.compile(r"\bR\d{6}-Q\d{3}\b")
PATTERN_ID_RE = re.compile(r"^P\d+$")
STATE_REFUSAL = "unsupported BIMRI state version: 5.2.0"
DEFAULT_HOT_MAX_BYTES = 49152


def days_ago(days):
    return (dt.date.today() - dt.timedelta(days=days)).isoformat()


class V520Step10Test(unittest.TestCase):
    maxDiff = None
    _prior_release = None
    _class_temp = None

    @classmethod
    def tearDownClass(cls):
        if cls._class_temp is not None:
            cls._class_temp.cleanup()
            cls._class_temp = None
            cls._prior_release = None

    def setUp(self):
        self._temp = tempfile.TemporaryDirectory(prefix="bimri-v520-s10-")
        self.root = Path(self._temp.name)

    def tearDown(self):
        self._temp.cleanup()

    # ------------------------------------------------------------------
    # Helpers (same shapes as tests/test_v510_lifecycle.py)
    # ------------------------------------------------------------------

    def cli(self, *arguments, root=None, check=True, timeout=120, engine=None):
        command = [
            sys.executable,
            str(engine or ENGINE),
            "--root",
            str(root or self.root),
        ] + [str(item) for item in arguments]
        result = subprocess.run(
            command, text=True, capture_output=True, timeout=timeout
        )
        if check and result.returncode != 0:
            self.fail(
                "command failed: {!r}\nstdout:\n{}\nstderr:\n{}".format(
                    command, result.stdout, result.stderr
                )
            )
        return result

    def start(self, actor="step10", root=None, engine=None):
        # The legacy lane is start --open-run on this engine (D1.1); the
        # frozen 5.1.2 fixture engine opens a run with a plain start.
        arguments = ["start", "--actor", actor]
        if engine is None or Path(engine).resolve() == ENGINE.resolve():
            arguments.insert(1, "--open-run")
        result = self.cli(*arguments, root=root, engine=engine)
        match = RUN_RE.search(result.stdout)
        self.assertIsNotNone(match, result.stdout)
        return match.group(1)

    def close_run(self, run_id, root=None, engine=None):
        return self.cli(
            "close", "--run", run_id, "--outcome", "success",
            "--summary", "step 10 fixture run", root=root, engine=engine,
        )

    def propose_set(self, run_id, key, text, tier=2, new_subject=False,
                    importance=None, kind=None, status=None, source="user",
                    trust="confirmed", root=None, check=True, engine=None):
        arguments = [
            "propose", "--run", run_id, "--operation", "set",
            "--tier", str(tier), "--key", key, "--text", text,
            "--source", source, "--trust", trust,
        ]
        if kind is not None or (new_subject and tier == 1):
            arguments.extend(("--kind", kind or "fact"))
        if importance is not None:
            arguments.extend(("--importance", str(importance)))
        if status is not None:
            arguments.extend(("--status", status))
        if new_subject:
            arguments.append("--new-subject")
        return self.cli(*arguments, root=root, check=check, engine=engine)

    def apply_set(self, run_id, key, text, root=None, engine=None, **kwargs):
        proposed = self.propose_set(
            run_id, key, text, root=root, engine=engine, **kwargs
        )
        synced = self.cli("sync", "--run", run_id, root=root, engine=engine)
        return proposed, synced

    def propose_pattern(self, run_id, key, text, confidence="emerging",
                        observations=2, evidence=None, new_subject=True,
                        root=None, check=True, engine=None):
        arguments = [
            "propose", "--run", run_id, "--operation", "set", "--tier", "3",
            "--key", key, "--text", text, "--confidence", confidence,
            "--observations", str(observations),
            "--falsifier", "A controlled replay does not reproduce it.",
        ]
        if evidence is not None:
            arguments.extend(("--evidence", evidence))
        if new_subject:
            arguments.append("--new-subject")
        return self.cli(*arguments, root=root, check=check, engine=engine)

    def apply_pattern(self, run_id, key, text, root=None, engine=None,
                      **kwargs):
        proposed = self.propose_pattern(
            run_id, key, text, root=root, engine=engine, **kwargs
        )
        synced = self.cli("sync", "--run", run_id, root=root, engine=engine)
        return proposed, synced

    def state(self, root=None):
        return json.loads(
            Path(root or self.root).joinpath(".bimri", "state.json")
            .read_text("utf-8")
        )

    def write_state(self, state, root=None):
        Path(root or self.root).joinpath(".bimri", "state.json").write_text(
            json.dumps(state, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )

    def update_state(self, root=None, **updates):
        state = self.state(root)
        state.update(updates)
        self.write_state(state, root)
        return state

    def set_run_date(self, run_id, date, root=None):
        state = self.state(root)
        state["run_dates"][run_id] = date
        self.write_state(state, root)

    def hot_text(self, root=None):
        return Path(root or self.root).joinpath("bimri.md").read_text("utf-8")

    def hot_line(self, key, root=None):
        lines = [
            line for line in self.hot_text(root).splitlines()
            if line.startswith("[") and "[K:{}]".format(key) in line
        ]
        return lines[0] if len(lines) == 1 else None

    def cold_current(self, root=None):
        value = self.state(root).get("cold_current", {})
        self.assertIsInstance(value, dict)
        return value

    def tighten_hot_view(self, root=None):
        current = len(self.hot_text(root).encode("utf-8"))
        self.update_state(root=root, hot_max_bytes=current + 32)

    def admit_pressure(self, key="delivery.pressure-incoming", root=None):
        """Admit one user/confirmed importance-5 subject under byte pressure."""
        run_id = self.start("step10-pressure", root=root)
        self.tighten_hot_view(root)
        proposed, synced = self.apply_set(
            run_id, key, "New important pressure context. " + "n" * 80,
            root=root, new_subject=True, importance=5,
        )
        self.assertEqual(proposed.returncode, 0)
        self.assertEqual(synced.returncode, 0)
        return run_id, key

    def decision(self, proposal_id, root=None):
        path = Path(root or self.root).joinpath(
            ".bimri", "decisions", "{}.json".format(proposal_id)
        )
        self.assertTrue(path.is_file(), "missing decision for " + proposal_id)
        return json.loads(path.read_text("utf-8"))

    @staticmethod
    def protected_snapshot(root):
        root = Path(root)
        snapshot = {}
        candidates = [root / "bimri.md"]
        candidates.extend(
            sorted(root.joinpath(".bimri").rglob("*"),
                   key=lambda item: item.as_posix())
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
                    "file", len(content), hashlib.sha256(content).hexdigest()
                )
            else:
                snapshot[relative] = ("missing", None)
        return snapshot

    def prior_engine(self):
        cls = type(self)
        if cls._prior_release is None:
            archived = subprocess.run(
                ["git", "-c", "safe.directory=" + REPOSITORY.as_posix(),
                 "archive", "--format=tar", PRIOR_RELEASE_COMMIT],
                cwd=str(REPOSITORY), capture_output=True, timeout=60,
            )
            self.assertEqual(
                archived.returncode, 0,
                "the checkout must contain release commit {}: {!r}".format(
                    PRIOR_RELEASE_COMMIT, archived.stderr
                ),
            )
            cls._class_temp = tempfile.TemporaryDirectory(
                prefix="bimri-v520-s10-prior-"
            )
            destination = Path(cls._class_temp.name) / "release-5.1.2"
            destination.mkdir()
            package_root = destination.resolve()
            with tarfile.open(
                fileobj=io.BytesIO(archived.stdout), mode="r:"
            ) as archive:
                for member in archive.getmembers():
                    target = (destination / member.name).resolve()
                    self.assertTrue(
                        target == package_root or package_root in target.parents,
                        member.name,
                    )
                archive.extractall(str(destination))
            engine = destination / "bimri-engine.py"
            self.assertIn(b'ENGINE_VERSION = "5.1.2"', engine.read_bytes())
            cls._prior_release = engine
        return cls._prior_release

    def build_cooled_pattern(self, root=None, fixture_engine=None):
        """A stale emerging pattern cools ahead of a fresh Tier 2 neighbour.

        Pattern score: 1 (emerging) x 0.1 (evidence run 365 days old) = 0.1.
        Neighbour: importance 1 x 1.0 x 2.0 (confirmed) x 1.25 (user) = 2.5.
        ``fixture_engine`` writes the pattern and the neighbour (the 5.1.2
        engine for the one-way test); the pressure admission is always 5.2.0.
        """
        pattern_key = "pattern.stale-signal"
        neighbour_key = "notes.fresh-neighbour"
        pattern_text = "Stale signal recurs after deploys. " + "p" * 280
        pattern_run = self.start("step10-pattern", root=root,
                                 engine=fixture_engine)
        self.apply_pattern(pattern_run, pattern_key, pattern_text, root=root,
                           engine=fixture_engine)
        self.close_run(pattern_run, root=root, engine=fixture_engine)
        self.set_run_date(pattern_run, days_ago(365), root=root)

        neighbour_run = self.start("step10-neighbour", root=root,
                                   engine=fixture_engine)
        self.apply_set(
            neighbour_run, neighbour_key,
            "Fresh neighbour context. " + "f" * 300,
            root=root, new_subject=True, importance=1, engine=fixture_engine,
        )
        self.close_run(neighbour_run, root=root, engine=fixture_engine)
        if fixture_engine is not None:
            self.assertEqual(self.state(root)["bimri_version"], "5.1.0")
        self.admit_pressure(root=root)
        return {
            "pattern_key": pattern_key,
            "pattern_text": pattern_text,
            "pattern_run": pattern_run,
            "neighbour_key": neighbour_key,
        }

    # ------------------------------------------------------------------
    # D6.3 provenance in the Tier 2 score
    # ------------------------------------------------------------------

    def test_confirmed_outranks_working(self):
        # D6.3: trust_weight 2.0 for confirmed. Same source (user), same run
        # date. Confirmed importance 3 scores 3 x 2.0 x 1.25 = 7.5; working
        # importance 5 scores 5 x 1.0 x 1.25 = 6.25. Importance alone (5.1.2)
        # would cool the confirmed subject; the 2.0 weight cools the working one.
        run_id = self.start("step10-trust")
        confirmed_key = "rules.confirmed-owner-rule"
        working_key = "notes.working-agent-note"
        self.apply_set(
            run_id, confirmed_key, "Confirmed owner rule. " + "c" * 300,
            new_subject=True, importance=3, source="user", trust="confirmed",
        )
        self.apply_set(
            run_id, working_key, "Working note. " + "w" * 300,
            new_subject=True, importance=5, source="user", trust="working",
        )
        self.admit_pressure()
        cold = self.cold_current()
        self.assertIn(working_key, cold)
        self.assertNotIn(confirmed_key, cold)
        self.assertIsNotNone(self.hot_line(confirmed_key))

    def test_user_outranks_agent(self):
        # D6.3: source_weight 1.25 for user. Same trust (working), same
        # importance, same run date. The user key sorts first, so the 5.1.2
        # tie-break (key ascending) would cool it; the 1.25 weight cools the
        # agent subject instead (3.75 against 3.0).
        run_id = self.start("step10-source")
        user_key = "alpha.user-sourced"
        agent_key = "beta.agent-sourced"
        self.apply_set(
            run_id, user_key, "User sourced note. " + "u" * 300,
            new_subject=True, importance=3, source="user", trust="working",
        )
        self.apply_set(
            run_id, agent_key, "Agent sourced note. " + "a" * 300,
            new_subject=True, importance=3, source="agent", trust="working",
        )
        self.admit_pressure()
        cold = self.cold_current()
        self.assertIn(agent_key, cold)
        self.assertNotIn(user_key, cold)
        self.assertIsNotNone(self.hot_line(user_key))

    # ------------------------------------------------------------------
    # D6.3 / D2.10 Tier 3 cooling
    # ------------------------------------------------------------------

    def test_tier3_cools_and_rehydrates(self):
        # D2.10 + D6.3: a stale pattern is a pressure candidate; its cold record
        # is tier 3 with its P#### id and [P####] raw line; get shows COLD; a
        # Tier 3 set on the same key rehydrates it with the same pattern id.
        fixture = self.build_cooled_pattern()
        key = fixture["pattern_key"]
        cold = self.cold_current()
        self.assertIn(key, cold)
        self.assertNotIn(fixture["neighbour_key"], cold)
        record = cold[key]
        self.assertEqual(record["tier"], 3)
        self.assertRegex(record["entry_id"], PATTERN_ID_RE)
        self.assertTrue(
            record["raw_line"].startswith(
                "[{}] [K:{}] ".format(record["entry_id"], key)
            ),
            record["raw_line"],
        )
        self.assertIn(fixture["pattern_text"], record["raw_line"])
        self.assertIsNone(self.hot_line(key))
        pattern_id = record["entry_id"]
        pattern_count = self.state()["pattern_count"]

        got = self.cli("get", "--key", key)
        self.assertTrue(
            any(
                line.startswith("COLD\t{}\t{}".format(key, pattern_id))
                for line in got.stdout.splitlines()
            ),
            got.stdout,
        )

        # Room for the pattern again, so the rehydration is not itself pressure.
        self.update_state(hot_max_bytes=DEFAULT_HOT_MAX_BYTES)
        rehydrate_run = self.start("step10-rehydrate")
        new_text = "Stale signal recurs after every deploy."
        proposed, synced = self.apply_pattern(
            rehydrate_run, key, new_text, confidence="developing",
            observations=3, new_subject=False,
        )
        proposal_id = PROPOSAL_ID_RE.search(proposed.stdout).group(0)
        self.assertEqual(self.decision(proposal_id)["outcome"], "accepted")
        self.assertNotIn(key, self.cold_current())
        line = self.hot_line(key)
        self.assertIsNotNone(line)
        self.assertTrue(line.startswith("[{}] [K:{}] [developing]".format(
            pattern_id, key)), line)
        self.assertIn(new_text, line)
        self.assertEqual(self.state()["pattern_count"], pattern_count)

    def test_tier3_cold_store_passes_doctor(self):
        # D2.10: validate_cold_current accepts tier 3 with a PATTERN_ID_RE id
        # whose parsed line is Tier 3, so a store holding a cold pattern audits
        # clean and restarts.
        self.build_cooled_pattern()
        doctor = self.cli("doctor", "--read-only")
        self.assertIn("PASSED", doctor.stdout)
        self.start("step10-restart")

    def test_tier3_cold_touch_is_refused(self):
        # D2.10: touch keeps refusing a non-Tier-2 target, cold included; the
        # cold record is unchanged.
        fixture = self.build_cooled_pattern()
        key = fixture["pattern_key"]
        before = dict(self.cold_current()[key])
        run_id = self.start("step10-touch")
        proposed = self.cli(
            "propose", "--run", run_id, "--operation", "touch", "--key", key,
            "--source", "user", "--trust", "confirmed", check=False,
        )
        self.assertNotEqual(proposed.returncode, 0)
        self.assertIn("tier 2", (proposed.stdout + proposed.stderr).lower())
        self.cli("sync", "--run", run_id)
        self.assertEqual(self.cold_current()[key], before)

    def test_tier3_cold_record_relabelled_tier2_is_refused(self):
        # D2.10: a Tier 2 cold record must carry a legacy entry id and a parsed
        # Tier 2 line; a tier-3 record relabelled tier 2 fails validation before
        # start mutates anything.
        fixture = self.build_cooled_pattern()
        key = fixture["pattern_key"]
        state = self.state()
        state["cold_current"][key]["tier"] = 2
        self.write_state(state)
        before = self.protected_snapshot(self.root)
        started = self.cli("start", "--actor", "step10-probe", check=False)
        self.assertNotEqual(started.returncode, 0)
        self.assertIn("cold", (started.stdout + started.stderr).lower())
        self.assertEqual(self.protected_snapshot(self.root), before)

    def test_tier3_legacy_id_cold_record_is_refused(self):
        # D2.10: a tier-3 cold record must carry a PATTERN_ID_RE id; a Tier 2
        # legacy id under tier 3 fails validation before start mutates anything.
        fixture = self.build_cooled_pattern()
        key = fixture["pattern_key"]
        state = self.state()
        state["cold_current"][key]["entry_id"] = "R000001-E001"
        self.write_state(state)
        before = self.protected_snapshot(self.root)
        started = self.cli("start", "--actor", "step10-probe", check=False)
        self.assertNotEqual(started.returncode, 0)
        self.assertIn("cold", (started.stdout + started.stderr).lower())
        self.assertEqual(self.protected_snapshot(self.root), before)

    def test_tier3_confidence_orders_cooling(self):
        # D6.3: Tier 3 score = conf_weight {emerging 1, developing 2,
        # established 3} x decay of the newest evidence run. Same evidence run;
        # the established key sorts first, yet the emerging pattern cools.
        run_id = self.start("step10-confidence")
        established_key = "alpha.established-pattern"
        emerging_key = "beta.emerging-pattern"
        self.apply_pattern(
            run_id, established_key, "Established pattern. " + "e" * 300,
            confidence="established", observations=5,
        )
        self.apply_pattern(
            run_id, emerging_key, "Emerging pattern. " + "m" * 300,
            confidence="emerging", observations=1,
        )
        self.admit_pressure()
        cold = self.cold_current()
        self.assertIn(emerging_key, cold)
        self.assertNotIn(established_key, cold)
        self.assertEqual(cold[emerging_key]["tier"], 3)

    def test_tier3_newest_evidence_run_sets_the_clock(self):
        # D6.3: the Tier 3 clock is the newest evidence run's run_date. Pattern
        # A cites an old run and a fresh run (newest fresh, score 3 x 1.0);
        # pattern B cites only the old run (3 x 0.1). B cools although A's
        # oldest evidence is just as old.
        old_run = self.start("step10-old-evidence")
        old_entry = self.cli(
            "journal", "--run", old_run, "--importance", "3",
            "--text", "Old observation.",
        ).stdout.strip().splitlines()[-1]
        self.close_run(old_run)
        self.set_run_date(old_run, days_ago(365))
        fresh_run = self.start("step10-fresh-evidence")
        fresh_entry = self.cli(
            "journal", "--run", fresh_run, "--importance", "3",
            "--text", "Fresh observation.",
        ).stdout.strip().splitlines()[-1]
        renewed_key = "alpha.renewed-pattern"
        stale_key = "beta.stale-pattern"
        self.apply_pattern(
            fresh_run, renewed_key, "Renewed pattern. " + "r" * 300,
            confidence="established", observations=4,
            evidence="{},{}".format(old_entry, fresh_entry),
        )
        self.apply_pattern(
            fresh_run, stale_key, "Stale pattern. " + "s" * 300,
            confidence="established", observations=4, evidence=old_entry,
        )
        self.admit_pressure()
        cold = self.cold_current()
        self.assertIn(stale_key, cold)
        self.assertNotIn(renewed_key, cold)

    def test_tier3_missing_evidence_date_takes_the_floor(self):
        # D6.3: a Tier 3 evidence run with no run_dates entry takes the 0.1
        # floor (l.252-255), never "fresh". Established pattern with an
        # unknown evidence run: 3 x 0.1 = 0.3 against a fresh user/working
        # importance-1 note at 1.25, so the pattern cools first.
        run_id = self.start("step10-floor")
        pattern_key = "pattern.undated-evidence"
        note_key = "notes.fresh-low"
        self.apply_pattern(
            run_id, pattern_key, "Undated evidence pattern. " + "u" * 300,
            confidence="established", observations=3,
            evidence="R000099-E001",
        )
        self.assertNotIn("R000099", self.state()["run_dates"])
        self.apply_set(
            run_id, note_key, "Fresh low note. " + "l" * 300,
            new_subject=True, importance=1, source="user", trust="working",
        )
        self.admit_pressure()
        cold = self.cold_current()
        self.assertIn(pattern_key, cold)
        self.assertNotIn(note_key, cold)

    def test_maintain_forecast_names_a_pattern_as_next_to_cool(self):
        # D6.3: cmd_maintain's candidate filter is tier in {2, 3}; at >= 90%
        # pressure the next-cool sentence names the lowest-retention subject,
        # here a stale pattern.
        pattern_run = self.start("step10-forecast-pattern")
        pattern_key = "pattern.forecast-stale"
        self.apply_pattern(
            pattern_run, pattern_key, "Forecast stale pattern. " + "q" * 300,
        )
        self.close_run(pattern_run)
        self.set_run_date(pattern_run, days_ago(365))
        note_run = self.start("step10-forecast-note")
        self.apply_set(
            note_run, "notes.forecast-fresh", "Fresh forecast note. " + "g" * 300,
            new_subject=True, importance=1,
        )
        self.tighten_hot_view()
        forecast = self.cli("maintain")
        self.assertIn("BIMRI retention forecast:", forecast.stdout)
        self.assertIn(
            "Next byte-pressure admission can cool {} deterministically".format(
                pattern_key
            ),
            forecast.stdout,
        )

    # ------------------------------------------------------------------
    # D6.3 run dates: current_run_date_references keeps Tier 3 evidence runs
    # ------------------------------------------------------------------

    def _flood_run_dates(self, protected_run, protected_date):
        state = self.state()
        state["run_dates"][protected_run] = protected_date
        highest = max(int(run[1:]) for run in state["run_dates"])
        for number in range(highest + 1, highest + 502):
            state["run_dates"]["R{:06d}".format(number)] = dt.date.today().isoformat()
        state["run_count"] = highest + 501
        self.write_state(state)
        self.assertGreater(len(self.state()["run_dates"]), 500)

    def test_tier3_evidence_dates_survive_compaction(self):
        # D6.3: current_run_date_references adds every run prefix of every
        # evidence id of a hot Tier 3 entry, so run_dates compaction past 500
        # keeps the closed evidence run's date (the pattern's clock).
        evidence_run = self.start("step10-evidence")
        evidence_id = self.cli(
            "journal", "--run", evidence_run, "--importance", "3",
            "--text", "Observed the pattern once.",
        ).stdout.strip().splitlines()[-1]
        self.close_run(evidence_run)
        pattern_run = self.start("step10-compaction-pattern")
        key = "pattern.compaction-clock"
        self.apply_pattern(
            pattern_run, key, "Compaction clock pattern. " + "k" * 200,
            evidence=evidence_id,
        )
        self.close_run(pattern_run)
        self.assertIn("[ev:{}]".format(evidence_id), self.hot_line(key))
        old_date = days_ago(200)
        self._flood_run_dates(evidence_run, old_date)

        self.start("step10-compaction-restart")
        state = self.state()
        self.assertLessEqual(len(state["run_dates"]), 502)
        self.assertEqual(state["run_dates"].get(evidence_run), old_date)

    def test_tier3_cold_evidence_dates_survive_compaction(self):
        # D6.3: the cold loop of current_run_date_references does the same for
        # Tier 3 raw lines, so a cooled pattern keeps its evidence run's date
        # through compaction and stays cold-current.
        fixture = self.build_cooled_pattern()
        key = fixture["pattern_key"]
        evidence_run = fixture["pattern_run"]
        self.assertIn(key, self.cold_current())
        self.assertIn(
            "[ev:{}-E".format(evidence_run), self.cold_current()[key]["raw_line"]
        )
        old_date = days_ago(365)
        self.assertEqual(self.state()["run_dates"][evidence_run], old_date)
        self._flood_run_dates(evidence_run, old_date)

        self.start("step10-cold-compaction-restart")
        state = self.state()
        self.assertEqual(state["run_dates"].get(evidence_run), old_date)
        self.assertIn(key, state["cold_current"])

    # ------------------------------------------------------------------
    # D6.3 Tier 1 never cooled; a pattern storm cannot hold Tier 1 out
    # ------------------------------------------------------------------

    def test_pattern_storm_cannot_hold_tier1_out(self):
        # D6.3: patterns are pressure candidates and Tier 1 never is. A view
        # filled with fresh established patterns admits a new Tier 1 subject by
        # cooling a pattern (5.1.2 held it capacity-residency-required), and
        # the resident Tier 1 subject stays hot.
        run_id = self.start("step10-storm")
        resident_tier1 = "owner.resident-rule"
        self.apply_set(
            run_id, resident_tier1, "Resident owner rule stays hot.",
            tier=1, new_subject=True, kind="rule",
        )
        pattern_keys = []
        for number in range(1, 13):
            key = "storm.pattern-{:02d}".format(number)
            pattern_keys.append(key)
            self.apply_pattern(
                run_id, key,
                "Storm pattern {:02d}. ".format(number) + chr(97 + number) * 300,
                confidence="established", observations=3,
            )
        self.tighten_hot_view()

        tier1_run = self.start("step10-storm-tier1")
        incoming = "owner.incoming-rule"
        proposed, synced = self.apply_set(
            tier1_run, incoming,
            "Incoming owner rule must reach the hot view. " + "t" * 120,
            tier=1, new_subject=True, kind="rule",
        )
        proposal_id = PROPOSAL_ID_RE.search(proposed.stdout).group(0)
        self.assertEqual(self.decision(proposal_id)["outcome"], "accepted")
        self.assertIsNotNone(self.hot_line(incoming))
        self.assertIsNotNone(self.hot_line(resident_tier1))
        cold = self.cold_current()
        self.assertNotIn(incoming, cold)
        self.assertNotIn(resident_tier1, cold)
        cooled = [key for key in pattern_keys if key in cold]
        self.assertTrue(cooled, "no pattern cooled to admit the Tier 1 subject")
        self.assertTrue(all(cold[key]["tier"] == 3 for key in cooled))
        self.assertLessEqual(
            len(self.hot_text().encode("utf-8")), self.state()["hot_max_bytes"]
        )

    # ------------------------------------------------------------------
    # D0.6 one-way write
    # ------------------------------------------------------------------

    def test_tier3_cooling_is_a_one_way_write(self):
        # D0.6 / D2.10 (R-I): the pattern and its neighbour are written by the
        # 5.1.2 engine, so the store is still state 5.1.0 when the 5.2.0
        # pressure admission cools the pattern. That cooling is an authority
        # write: the store becomes 5.2.0 and 5.1.2 refuses it with the
        # state-version line (exit 2) before its cold_current tier check and
        # before any mutation.
        prior = self.prior_engine()
        fixture = self.build_cooled_pattern(fixture_engine=prior)
        self.assertEqual(
            self.cold_current()[fixture["pattern_key"]]["tier"], 3
        )
        self.assertEqual(self.state()["bimri_version"], "5.2.0")
        before = self.protected_snapshot(self.root)
        refused = self.cli(
            "start", "--actor", "too-old", engine=prior, check=False
        )
        self.assertEqual(refused.returncode, 2, refused.stdout + refused.stderr)
        self.assertIn(STATE_REFUSAL, refused.stderr)
        self.assertNotIn("cold_current", refused.stdout + refused.stderr)
        self.assertNotIn("invalid key or tier", refused.stdout + refused.stderr)
        self.assertEqual(self.protected_snapshot(self.root), before)

    # ------------------------------------------------------------------
    # Section 06 3.1: the confirmed decay floor (DEVIATIONS step 10 item 1)
    # ------------------------------------------------------------------

    def test_owner_rule_survives_fresh_agent_saves_under_pressure(self):
        # D6.3 + section 06 3.1 CONFIRMED_DECAY_FLOOR 0.35: a confirmed/user
        # importance-3 owner rule 15 days old scores 3 x 0.35 x 2.5 = 2.625
        # (1.5 without the floor). Ten working/agent importance-4 notes 5 days
        # old score 2.0 each. Ten fresh working/agent importance-4 admissions
        # (4.0) under byte pressure cool the ten notes, never the owner rule.
        owner_key = "owner.standing-rule"
        owner_run = self.start("step10-owner")
        self.apply_set(
            owner_run, owner_key, "Standing owner rule. " + "o" * 300,
            new_subject=True, importance=3, source="user", trust="confirmed",
        )
        self.close_run(owner_run)
        self.set_run_date(owner_run, days_ago(15))

        notes_run = self.start("step10-aging-notes")
        old_keys = ["notes.aging-{:02d}".format(n) for n in range(1, 11)]
        for key in old_keys:
            # Longer than each incoming line, so one aging note frees room.
            self.propose_set(
                notes_run, key, "Aging agent note. " + "a" * 360,
                new_subject=True, importance=4, source="agent",
                trust="working",
            )
        self.cli("sync", "--run", notes_run)
        self.close_run(notes_run)
        self.set_run_date(notes_run, days_ago(5))

        fresh_run = self.start("step10-fresh-notes")
        fresh_keys = ["notes.fresher-{:02d}".format(n) for n in range(1, 11)]
        for key in fresh_keys:
            # Byte pressure on every admission: the view plus 32 bytes.
            self.tighten_hot_view()
            proposed, _ = self.apply_set(
                fresh_run, key, "Fresh agent note. " + "b" * 300,
                new_subject=True, importance=4, source="agent",
                trust="working",
            )
            proposal_id = PROPOSAL_ID_RE.search(proposed.stdout).group(0)
            self.assertEqual(self.decision(proposal_id)["outcome"], "accepted")
            self.assertIsNotNone(self.hot_line(key), key)
            self.assertIsNotNone(self.hot_line(owner_key), "after " + key)
            self.assertNotIn(owner_key, self.cold_current())
        cold = self.cold_current()
        self.assertEqual(sorted(k for k in old_keys if k in cold), old_keys)
        for key in fresh_keys:
            self.assertNotIn(key, cold)

    # ------------------------------------------------------------------
    # Section 06 3.2: admission compares before it evicts
    # ------------------------------------------------------------------

    def _full_view_of_strong_residents(self):
        run_id = self.start("step10-strong-residents")
        keys = ["owner.strong-{:02d}".format(n) for n in range(1, 4)]
        for key in keys:
            self.propose_set(
                run_id, key, "Strong owner context. " + "s" * 300,
                new_subject=True, importance=4, source="user",
                trust="confirmed",
            )
        self.cli("sync", "--run", run_id)
        self.close_run(run_id)
        self.tighten_hot_view()
        return keys

    def test_low_scoring_incoming_self_cools_and_says_so(self):
        # Section 06 3.2 (DEVIATIONS step 10 item 3): every resident is fresh
        # confirmed/user importance 4 (10.0); a working/agent importance-1 new
        # subject (1.0) under pressure is accepted straight into cold memory,
        # sync prints the BIMRI NOTICE line after its summary line, no
        # resident cools, and get shows the key COLD.
        residents = self._full_view_of_strong_residents()
        run_id = self.start("step10-weak-incoming")
        key = "notes.weak-incoming"
        proposed, synced = self.apply_set(
            run_id, key, "Weak incoming note. " + "w" * 200,
            new_subject=True, importance=1, source="agent", trust="working",
        )
        proposal_id = PROPOSAL_ID_RE.search(proposed.stdout).group(0)
        self.assertEqual(self.decision(proposal_id)["outcome"], "accepted")
        notice = (
            "BIMRI NOTICE: {} accepted into cold memory (hot view full); "
            "get and search still find it.".format(key)
        )
        lines = synced.stdout.splitlines()
        self.assertIn(notice, lines)
        summary = [
            index for index, line in enumerate(lines)
            if line.startswith("BIMRI: synced {};".format(run_id))
        ]
        self.assertEqual(len(summary), 1, synced.stdout)
        self.assertGreater(lines.index(notice), summary[0])
        cold = self.cold_current()
        self.assertIn(key, cold)
        self.assertIsNone(self.hot_line(key))
        for resident in residents:
            self.assertNotIn(resident, cold)
            self.assertIsNotNone(self.hot_line(resident))
        got = self.cli("get", "--key", key)
        self.assertTrue(
            any(line.startswith("COLD\t{}\t".format(key))
                for line in got.stdout.splitlines()),
            got.stdout,
        )

    def test_equal_scoring_incoming_still_evicts(self):
        # Section 06 3.2 tie rule: an incoming that scores exactly the lowest
        # resident's score (fresh confirmed/user importance 4, 10.0) is not
        # strictly lower, so it displaces a resident, stays hot, and no
        # notice is printed.
        residents = self._full_view_of_strong_residents()
        run_id = self.start("step10-equal-incoming")
        key = "owner.strong-incoming"
        proposed, synced = self.apply_set(
            run_id, key, "Strong incoming context. " + "e" * 200,
            new_subject=True, importance=4, source="user", trust="confirmed",
        )
        proposal_id = PROPOSAL_ID_RE.search(proposed.stdout).group(0)
        self.assertEqual(self.decision(proposal_id)["outcome"], "accepted")
        self.assertNotIn("BIMRI NOTICE:", synced.stdout)
        cold = self.cold_current()
        self.assertNotIn(key, cold)
        self.assertIsNotNone(self.hot_line(key))
        self.assertTrue(any(resident in cold for resident in residents))

    # ------------------------------------------------------------------
    # D1.16 / section 06 3.2: maintain's Tier 3 line
    # ------------------------------------------------------------------

    def _maintain_lines(self):
        result = self.cli("maintain")
        lines = result.stdout.splitlines()
        forecast = [
            index for index, line in enumerate(lines)
            if line.startswith("BIMRI retention forecast:")
        ]
        self.assertEqual(len(forecast), 1, result.stdout)
        return lines, forecast[0]

    def test_maintain_prints_tier3_line_with_lowest_pattern(self):
        # D1.16: directly after the forecast line, "Tier 3 patterns: {m}
        # cooling-eligible; lowest retention: {key}." m counts hot patterns;
        # key is the lowest by retention order (emerging 1.0 under
        # established 3.0, although the established key sorts first).
        run_id = self.start("step10-maintain-patterns")
        self.apply_pattern(
            run_id, "alpha.maintain-established", "Established. " + "x" * 80,
            confidence="established", observations=4,
        )
        self.apply_pattern(
            run_id, "beta.maintain-emerging", "Emerging. " + "y" * 80,
            confidence="emerging", observations=1,
        )
        lines, index = self._maintain_lines()
        self.assertEqual(
            lines[index + 1],
            "Tier 3 patterns: 2 cooling-eligible; lowest retention: "
            "beta.maintain-emerging.",
        )

    def test_maintain_prints_tier3_zero_line(self):
        # D1.16: with no hot pattern the line directly after the forecast is
        # exactly "Tier 3 patterns: 0 cooling-eligible."
        run_id = self.start("step10-maintain-none")
        self.apply_set(
            run_id, "notes.maintain-only-tier2", "Only Tier 2 here.",
            new_subject=True, importance=3,
        )
        lines, index = self._maintain_lines()
        self.assertEqual(
            lines[index + 1], "Tier 3 patterns: 0 cooling-eligible."
        )

    def test_maintain_no_candidate_sentence_names_tier2_and_tier3(self):
        # D6.3 / section 06 3.2: at >= 90% pressure with only Tier 1 hot, the
        # no-candidate sentence reads "No Tier 2 or Tier 3 resident is
        # currently eligible" (Tier 1 is never a candidate).
        run_id = self.start("step10-maintain-tier1")
        self.apply_set(
            run_id, "owner.maintain-tier1", "Only Tier 1 here. " + "z" * 200,
            tier=1, new_subject=True, kind="rule",
        )
        self.tighten_hot_view()
        result = self.cli("maintain")
        self.assertIn("Tier 3 patterns: 0 cooling-eligible.", result.stdout)
        self.assertIn(
            "No Tier 2 or Tier 3 resident is currently eligible; an oversized "
            "incoming intent will be held durably without changing current "
            "truth.",
            result.stdout,
        )
        self.assertNotIn("Next byte-pressure admission", result.stdout)

    # ------------------------------------------------------------------
    # D6.3: a stale pattern cools before fresher memory for a Tier 1 save
    # ------------------------------------------------------------------

    def test_pattern_storm_cools_stale_pattern_before_fresher_tier2(self):
        # D6.3 (section 06 7): eight established patterns fill the view, four
        # with evidence 365 days old (3 x 0.1 = 0.3) and four 12 days old
        # (3 x 0.2 = 0.6); a fresh working/agent importance-1 Tier 2 note
        # scores 1.0. A confirmed Tier 1 save is admitted by cooling a
        # 365-day pattern, never a fresher pattern or the Tier 2 note.
        stale_run = self.start("step10-storm-stale")
        stale_keys = ["storm.stale-{:02d}".format(n) for n in range(1, 5)]
        for number, key in enumerate(stale_keys, 1):
            self.apply_pattern(
                stale_run, key,
                "Stale storm pattern {:02d}. ".format(number) + "s" * 300,
                confidence="established", observations=3,
            )
        self.close_run(stale_run)
        self.set_run_date(stale_run, days_ago(365))
        recent_run = self.start("step10-storm-recent")
        recent_keys = ["storm.recent-{:02d}".format(n) for n in range(1, 5)]
        for number, key in enumerate(recent_keys, 1):
            self.apply_pattern(
                recent_run, key,
                "Recent storm pattern {:02d}. ".format(number) + "r" * 300,
                confidence="established", observations=3,
            )
        self.close_run(recent_run)
        self.set_run_date(recent_run, days_ago(12))
        note_run = self.start("step10-storm-note")
        note_key = "notes.storm-fresh"
        self.apply_set(
            note_run, note_key, "Fresh low note. " + "n" * 300,
            new_subject=True, importance=1, source="agent", trust="working",
        )
        self.close_run(note_run)
        self.tighten_hot_view()

        tier1_run = self.start("step10-storm-owner")
        incoming = "owner.storm-incoming"
        proposed, _ = self.apply_set(
            tier1_run, incoming,
            "Incoming owner rule must reach the hot view. " + "t" * 120,
            tier=1, new_subject=True, kind="rule",
        )
        proposal_id = PROPOSAL_ID_RE.search(proposed.stdout).group(0)
        self.assertEqual(self.decision(proposal_id)["outcome"], "accepted")
        self.assertIsNotNone(self.hot_line(incoming))
        cold = self.cold_current()
        self.assertTrue(any(key in cold for key in stale_keys), sorted(cold))
        for key in recent_keys + [note_key]:
            self.assertNotIn(key, cold)
            self.assertIsNotNone(self.hot_line(key), key)

    # ------------------------------------------------------------------
    # D2.10: a cooled pattern keeps its P id out of the allocator
    # ------------------------------------------------------------------

    def test_cooled_legacy_pattern_id_is_never_reissued(self):
        # A v4-migrated store keeps its [P####] ids with pattern_count 0 and
        # no proposal file behind any of them. Cooling the highest legacy
        # pattern must not let next_pattern_id hand its id to a new pattern:
        # the cold record still owns it, and rehydrating it later under its
        # own id would otherwise meet a duplicate in the hot view.
        bimri_dir = self.root / ".bimri"
        log_dir = bimri_dir / "log"
        log_dir.mkdir(parents=True)
        falsify = " | Falsify: A controlled replay does not reproduce it."
        legacy_hot = "\n".join([
            "# BIMRI Memory",
            "",
            "## Tier 1: Core Intelligence",
            "",
            "[R1-E1] [fact] [legacy] First observation -> .bimri/log/R1.md",
            "",
            "## Tier 2: Active Context",
            "",
            "## Tier 3: Pattern Recognition",
            "",
            "[P0001] [developing] [obs:2] [ev:R1-E1] Legacy kept pattern."
            + falsify,
            "[P0002] [emerging] [obs:2] [ev:R1-E1] Legacy highest pattern. "
            + "h" * 360 + falsify,
            "",
            "<!-- END BIMRI -->",
            "",
        ])
        (self.root / "bimri.md").write_text(legacy_hot, "utf-8")
        (bimri_dir / "state.json").write_text(
            json.dumps({
                "bimri_version": "4.0",
                "project_id": "legacy-pattern-ids",
                "run_count": 1,
                "current_run_id": "R1",
            }, indent=2) + "\n",
            "utf-8",
        )
        (log_dir / "R1.md").write_text(
            "# Legacy run\n\n[CLOSED:R1 2026-07-26T00:00:00Z]\n", "utf-8"
        )
        self.cli("migrate")
        self.assertEqual(self.state()["pattern_count"], 0)
        self.assertEqual(
            sorted(bimri_dir.joinpath("proposals").glob("*.json")), []
        )
        cooled_key = "legacy.pattern-p0002"
        self.assertTrue(self.hot_line(cooled_key).startswith("[P0002] "))

        # Byte pressure cools the emerging legacy pattern, the highest P id.
        self.admit_pressure()
        cold = self.cold_current()
        self.assertEqual(sorted(cold), [cooled_key])
        self.assertEqual(cold[cooled_key]["tier"], 3)
        self.assertEqual(cold[cooled_key]["entry_id"], "P0002")
        self.assertEqual(self.state()["pattern_count"], 0)

        self.update_state(hot_max_bytes=DEFAULT_HOT_MAX_BYTES)
        fresh_run = self.start("step10-fresh-pattern")
        fresh_key = "pattern.fresh-after-cooling"
        proposed, _ = self.apply_pattern(
            fresh_run, fresh_key, "Fresh signal after the cooling."
        )
        proposal_id = PROPOSAL_ID_RE.search(proposed.stdout).group(0)
        self.assertEqual(self.decision(proposal_id)["outcome"], "accepted")
        proposal = json.loads(
            bimri_dir.joinpath("proposals", proposal_id + ".json")
            .read_text("utf-8")
        )
        fresh_id = proposal["pattern_id"]
        self.assertRegex(fresh_id, PATTERN_ID_RE)
        self.assertGreater(int(fresh_id[1:]), 2)
        self.assertTrue(
            self.hot_line(fresh_key).startswith("[{}] ".format(fresh_id))
        )
        self.assertEqual(self.cold_current()[cooled_key]["entry_id"], "P0002")

        # The cold pattern rehydrates under its own id beside the new one.
        rehydrated, _ = self.apply_pattern(
            fresh_run, cooled_key, "Legacy highest pattern returns.",
            confidence="developing", observations=3, new_subject=False,
        )
        rehydrate_id = PROPOSAL_ID_RE.search(rehydrated.stdout).group(0)
        self.assertEqual(self.decision(rehydrate_id)["outcome"], "accepted")
        self.assertNotIn(cooled_key, self.cold_current())
        self.assertTrue(
            self.hot_line(cooled_key).startswith(
                "[P0002] [K:{}] [developing]".format(cooled_key)
            ),
            self.hot_text(),
        )
        doctor = self.cli("doctor", "--read-only")
        self.assertIn("PASSED", doctor.stdout)


if __name__ == "__main__":
    unittest.main()
