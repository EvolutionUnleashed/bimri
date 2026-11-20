"""Check the immutable base/delta storage contract independently of history size."""
import hashlib
import importlib.util
import json
from pathlib import Path
import tempfile
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("bimri_store_format", ROOT / "bimri-engine.py")
engine = importlib.util.module_from_spec(spec)
spec.loader.exec_module(engine)


def manifest(rows):
    result = {"manifest_schema": engine.AUDIT_MANIFEST_SCHEMA,
              "created_at": "2026-11-18T20:00:00Z", "manifest": rows,
              "manifest_count": len(rows), "manifest_hash": engine.audit_witness_digest(rows)}
    result["manifest_file_hash"] = engine.audit_record_seal(result, "manifest_file_hash")
    return result


def facts(rows, manifest_hash):
    counts = {"run": len(rows), "pattern": 0, "conflict": 0,
              "proposal": {row["run_id"]: 1 for row in rows}}
    return engine.finalize_audit_facts(
        {"applying_resolutions": [], "open_conflicts": [], "held": [], "settled": {},
         "writers_by_key": {}, "watermarks": counts,
         "unsettled": {key: [] for key in engine.AUDIT_FACTS_UNSETTLED_FIELDS}},
        "2026-11-18T20:00:00Z", manifest_hash, rows)


class CheckpointScaleTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="bimri-521-checkpoint-")
        self.addCleanup(self.temp.cleanup)
        self.paths = engine.Paths(Path(self.temp.name))
        self.paths.audit_manifests.mkdir(parents=True)

    def test_manifest_reuses_one_row_encoding_with_identical_seal_and_wire_bytes(self):
        rows = [{"path": '.bimri/archive/日本語,"item".json', "sha256": "a" * 64}]
        for value in ([], rows):
            expected = manifest(value)
            with mock.patch.object(engine, "audit_json_bytes", wraps=engine.audit_json_bytes) as encode:
                record, raw = engine.sealed_manifest_publication(value, expected["created_at"])
                self.assertEqual(sum(call.args[0] is value for call in encode.call_args_list), 1)
            self.assertEqual(record, expected)
            self.assertEqual(raw, engine.checkpoint_record_bytes(expected))

    def publish(self, size, kind, prior=None, serial=1, version="5.2.1", origin="scoped"):
        rows = [{"path": ".bimri/proposals/R%06d-Q001.json" % i,
                 "sha256": hashlib.sha256(str(i).encode()).hexdigest()} for i in range(1, size+1)]
        m = manifest(rows)
        run_rows = [{"proposal_id": "R%06d-Q001" % i, "run_id": "R%06d" % i,
                     "closed": True, "file_present": True, "marker_present": True}
                    for i in range(1, size+1)]
        f = facts(run_rows, m["manifest_hash"])
        witness = {key: m[key] for key in ("manifest_count", "manifest_hash", "created_at")}
        witness.update(witness_schema=engine.AUDIT_WITNESS_SCHEMA, facts_hash=f["facts_hash"],
                       run_authority_hash=f["run_authority_hash"], scoped_publications=serial,
                       audit_origin=origin)
        witness.update(engine_version=engine.ENGINE_VERSION, memory_format_version=engine.MEMORY_FORMAT_VERSION,
                       policy_version=engine.AUTHORITY_POLICY_VERSION, head_revision=size, audit_epoch=size,
                       audited_at=witness["created_at"], proposal_runs=[], head_hash="0"*64,
                       state_hash="1"*64, write_state_hash="2"*64)
        witness["witness_hash"] = engine.audit_record_seal(witness, "witness_hash")
        record = m if kind == "manifest" else f
        if prior is not None:
            load = engine.load_audit_manifest_evidence if kind == "manifest" else engine.load_audit_facts_evidence
            load(self.paths, prior)
        encoded = engine.publication_bytes(
            self.paths, kind, record, {"bimri_version": version}, witness, {"prior_witness": prior} if prior else None)
        path = engine.checkpoint_generation(self.paths, kind, record[kind + "_hash"])
        path.write_bytes(encoded)
        return record, witness, path

    def test_warm_facts_and_transition_copies_are_isolated_and_exactly_bound(self):
        facts, witness, _ = self.publish(4, "facts")
        first = engine.load_audit_facts_evidence(self.paths, witness)
        first["run_facts"][0]["closed"] = False
        first["watermarks"]["proposal"].clear()
        second = engine.load_audit_facts_evidence(self.paths, witness)
        self.assertEqual(second, facts)
        second["unsettled"]["applying_decisions"].append("R000001-Q001")
        self.assertEqual(engine.load_audit_facts_evidence(self.paths, witness), facts)
        state = {"test": "durability"}
        marker = dict(transition_schema=engine.AUDIT_TRANSITION_SCHEMA,
                      kind="lifecycle", operation="journal", run_id="R000001", scope={},
                      created_at=witness["created_at"], prior_witness=witness,
                      prior_witness_hash=witness["witness_hash"], prior_manifest_hash=witness["manifest_hash"],
                      epoch_before=5, epoch_after=5, pre_write_state=state,
                      pre_write_state_hash=engine.audit_witness_digest(state), post_write_state_hash="1"*64,
                      log_path=".bimri/log/R000001.md", pre_log_hash="absent", post_log_hash="1"*64,
                      log_append="record")
        marker["transition_hash"] = engine.audit_record_seal(marker, "transition_hash")
        file = self.paths.audit_transition
        file.write_text(json.dumps(marker), encoding="utf-8")
        engine.load_audit_transition(self.paths)["pre_write_state"].clear()
        with mock.patch.object(engine, "audit_record_seal", side_effect=AssertionError("same bytes rehashed")):
            self.assertEqual(engine.load_audit_transition(self.paths), marker)
        marker["pre_write_state"]["test"] = "other data"
        file.write_text(json.dumps(marker), encoding="utf-8")
        with self.assertRaisesRegex(engine.BimriError, "pre-write state"):
            engine.load_audit_transition(self.paths)

    def test_both_kinds_use_one_full_base_and_preserve_exact_logical_records(self):
        for kind in ("manifest", "facts"):
            with self.subTest(kind=kind):
                _, prior, base = self.publish(1200, kind)
                for n in (1201, 1202):
                    expected, current, generation = self.publish(n, kind, prior)
                    raw = generation.read_bytes()
                    physical = json.loads(raw)
                    self.assertEqual(physical["_base"], json.loads(base.read_bytes())[kind + "_hash"])
                    self.assertLess(len(raw), 2048)
                    loaded = engine.read_checkpoint_record(self.paths, kind, generation, "generation")
                    self.assertEqual(loaded, expected)
                    prior = current
                _, _, full = self.publish(1203, kind, prior, serial=engine.CHECKPOINT_DELTA_INTERVAL)
                self.assertNotIn("_base", json.loads(full.read_bytes()))

    def test_unfenced_store_and_full_audit_publish_full_generations(self):
        for kind in ("manifest", "facts"):
            _, prior, _ = self.publish(700, kind)
            for version, origin in (("5.2.0", "scoped"), ("5.2.1", "full")):
                _, _, generation = self.publish(701, kind, prior, version=version, origin=origin)
                self.assertNotIn("_base", json.loads(generation.read_bytes()))

    def test_delta_and_base_damage_are_detected_even_after_a_successful_warm_read(self):
        _, prior, base = self.publish(900, "manifest")
        _, current, generation = self.publish(901, "manifest", prior)
        engine.load_audit_manifest_evidence(self.paths, current)
        raw, physical = generation.read_bytes(), json.loads(generation.read_bytes())
        physical["size"] -= 1
        generation.write_text(json.dumps(physical), encoding="utf-8")
        with self.assertRaisesRegex(engine.BimriError, "seal"):
            engine.load_audit_manifest_evidence(self.paths, current)
        generation.write_bytes(raw)
        original = base.read_bytes()
        base.write_bytes(original + b" ")
        with self.assertRaisesRegex(engine.BimriError, "base content changed"):
            engine.load_audit_manifest_evidence(self.paths, current)
        base.unlink()
        with self.assertRaisesRegex(engine.BimriError, "missing or unsafe"):
            engine.load_audit_manifest_evidence(self.paths, current)

    def test_recovery_reuses_existing_generation_bytes_and_pins_fallback_alias(self):
        _, prior, base = self.publish(1000, "manifest")
        record, current, generation = self.publish(1001, "manifest", prior)
        existing = generation.read_bytes()
        self.assertIn("_base", json.loads(existing))
        recovered = engine.write_audit_manifest_generation(
            self.paths, record, engine.checkpoint_record_bytes(record), for_alias=True
        )
        self.assertEqual(recovered, existing)
        self.paths.audit_manifest.write_bytes(existing)
        generation.unlink()
        with mock.patch.object(engine, "referenced_audit_manifest_hashes", return_value={current["manifest_hash"]}):
            engine.prune_audit_manifest_generations(self.paths)
        self.assertTrue(base.is_file())
        self.assertEqual(engine.load_audit_manifest_evidence(self.paths, current), record["manifest"])
        _, _, successor = self.publish(1002, "manifest", current)
        self.assertNotIn("_base", json.loads(successor.read_bytes()))

    def test_pruning_keeps_the_pinned_base_then_removes_it_after_compaction(self):
        for kind in ("manifest", "facts"):
            _, prior, base = self.publish(1500, kind)
            _, current, delta = self.publish(1501, kind, prior)
            with mock.patch.object(engine, "referenced_audit_manifest_hashes",
                                   return_value={current[kind + "_hash"]}):
                engine.prune_audit_manifest_generations(self.paths)
            self.assertTrue(base.is_file())
            self.assertTrue(delta.is_file())
            _, compacted, full = self.publish(1502, kind, current, serial=engine.CHECKPOINT_DELTA_INTERVAL)
            with mock.patch.object(engine, "referenced_audit_manifest_hashes",
                                   return_value={compacted[kind + "_hash"]}):
                engine.prune_audit_manifest_generations(self.paths)
            self.assertTrue(full.is_file())
            self.assertFalse(base.exists())
            self.assertFalse(delta.exists())

    def test_scoped_append_writes_bounded_bytes_at_three_scales(self):
        for kind in ("manifest", "facts"):
            sizes = []
            for count in (750, 1500, 3000):
                _, prior, base = self.publish(count, kind)
                _, _, delta = self.publish(count+1, kind, prior)
                self.assertLess(delta.stat().st_size, base.stat().st_size // 20)
                sizes.append(delta.stat().st_size)
            self.assertLess(max(sizes) - min(sizes), 150)

    def test_exact_cold_lookup_and_cached_validation_preserve_changed_rows(self):
        line = ("[R000001-E001] [K:rule.first] [I:3] [active] [T:working] "
                "[SRC:agent] [F:R000001] [L:R000001] [] Example.")
        record = engine.make_cold_record(engine.parse_entry_line(line), "R000002-Q001", "2026-11-18")
        class KeyedOnly(dict):
            def items(self):
                raise AssertionError("key lookup walked every cold record")
            def values(self):
                raise AssertionError("key lookup walked every cold record")
        state = {"cold_current": KeyedOnly({"rule.first": record})}
        self.assertEqual(engine.cold_entry(state, key="rule.first")["text"], "Example.")
        self.assertIsNone(engine.cold_entry(state, key="missing"))
        self.assertIsNone(engine.cold_entry(state, key="rule.first", target_id="R000003-E001"))
        cold = dict(state["cold_current"])
        proven = {}
        engine.validate_cold_current(cold, proven)
        with mock.patch.object(engine, "parse_entry_line", side_effect=AssertionError("unchanged row reparsed")):
            engine.validate_cold_current(cold, proven)
        record["line_hash"] = "0" * 64
        with self.assertRaisesRegex(engine.BimriError, "line hash"):
            engine.validate_cold_current(cold, proven)

    def test_canonical_log_pointer_checks_leaf_and_falls_back_on_redirects(self):
        self.paths.logs.mkdir()
        entry = {"id": "R000001-E001", "ptr": ".bimri/log/R000001.md"}
        file = self.paths.root / entry["ptr"]
        with mock.patch.object(engine, "pointer_validation_error",
                               side_effect=AssertionError("normal leaf needed another resolve")):
            self.assertIsNone(engine.resident_pointer_error(self.paths, entry, True))
            file.write_text("evidence", encoding="utf-8")
            self.assertIsNone(engine.resident_pointer_error(self.paths, entry, True))
        with mock.patch.object(engine, "pointer_validation_error", return_value="checked") as check:
            with mock.patch.object(engine, "_metadata_is_redirected", return_value=True):
                self.assertEqual(engine.resident_pointer_error(self.paths, entry, True), "checked")
            self.assertEqual(engine.resident_pointer_error(self.paths, entry, False), "checked")
            custom = dict(entry, ptr="other/relative.txt")
            self.assertEqual(engine.resident_pointer_error(self.paths, custom, True), "checked")
            self.assertEqual(check.call_count, 3)
