# Migrating BIMRI v1-v4 to v5

BIMRI v5 directly migrates explicitly versioned v1-v3 tiered Markdown and the
engine-based v4 format. This canonical repository publicly distributed the
original v1 and streamlined v3 instructions; the parser also accepts a valid
v2 header. Migration preserves old material before creating v5 state and stops
when it cannot identify or interpret that material safely.

Ask an agent to install v5 according to [`INSTALL.md`](INSTALL.md). After the
v5 files are present, the same migration can be requested explicitly:

```text
python3 bimri-engine.py migrate
```

Commands in this document use `python3`. The agent must use whichever local
executable provides Python 3.8 or newer. That is commonly `python3` outside
Windows and `python` on a standard Windows installation.

## Required Before Every Upgrade

Make the project quiescent across every runtime boundary:

1. finish or stop every agent using the project;
2. wait for every BIMRI command to finish;
3. pause any synchronization or copy operation affecting the folder;
4. if v1-v3 was installed in Claude Cowork Global Instructions, disable or
   remove that BIMRI block; and
5. keep the folder quiescent until migration and `doctor` complete.

The old v1-v3 Global Instructions directly edit the hot-memory file. v4
uses an older engine contract. None of those writers can participate in the v5
lock, so the v5 installer cannot make a still-running legacy writer safe.

The historical Global Instructions are preserved under [`legacy/`](legacy/)
for inspection and rollback. The installer copies that directory into the
target so the rollback reference remains self-contained. These are inert
files: they must not be activated or pasted into Global Instructions while v5
is active.

## Detection and Fail-Closed Rules

The engine recognizes these sources:

| Source | Typical files |
| --- | --- |
| v1-v3 | `BIMRI.md` and optional `BIMRI-backup.md`, or the lowercase compatibility names |
| v4 | `bimri.md` plus `.bimri/state.json`, logs, archives, and related state |

Detection is conservative. Migration stops before replacing the legacy memory
when any of these conditions applies:

- uppercase and lowercase hot files both exist and are not byte-identical;
- competing non-identical source files create an ambiguous lineage, or a rolling
  backup's filename case does not match its single active source;
- a source or rolling backup is unreadable, malformed, or does not contain the
  expected explicitly versioned tiered structure;
- a claimed v4 state cannot be parsed or reconciled with its memory and logs;
- an existing migration record or `V000000` disagrees with the deterministic
  conversion; or
- a path or symbolic link would redirect a migration write outside the project.

The agent must report the exact ambiguity and ask the owner which source is
authoritative. It must not merge competing files by guesswork.

## v1-v3 Direct Migration

v1-v3 were instruction-driven Markdown systems without an authoritative
engine state. v5 therefore treats their content as preserved legacy claims,
rather than evidence that the human confirmed each claim.

### What Is Preserved

For a successful migration, the engine:

- stores exact byte copies of the selected hot-memory source and its rolling
  backup, when present, under `.bimri/backups/`;
- records each original path, preserved path, byte length, and SHA-256 hash in
  `.bimri/migrations/legacy-to-v5.json`;
- imports every valid Tier 1 and Tier 2 item that can be mapped without
  interpretation;
- records a deterministic mapping from each legacy identifier or source
  position to a unique v5 identifier and stable `legacy.*` key; and
- leaves the original evidence recoverable even when the generated v5 wording
  must be normalized to the one-line v5 grammar.

The rolling backup is preserved as historical recovery evidence. It is not
silently combined with the current source or treated as a second accepted head.
After the marker, state, and generated view are durable, the engine retires the
old root-level backup names and any uppercase active filename. Root
`bimri.md` becomes the v5 generated view. The content-addressed `.bin` copies
under `.bimri/backups/` remain the byte-exact rollback authority.

### Conservative Authority Mapping

Every imported v1-v3 claim begins with:

```text
source: legacy
trust: working
```

Old importance, tags, and text are retained in the converted claim when valid
and representable. The old header session count seeds the v5 run count. Dates,
weights, observation counts, confidence, and other legacy-only metadata remain
recoverable in the exact source backup. Generated keys and IDs are
deterministic, so an interrupted migration can repeat without duplicating
claims.

v1-v3 patterns did not carry the evidence references and falsifier that v5
requires for Tier 3. Every old pattern is therefore converted into a Tier 2
`watch` claim. Its original wording and metadata remain preserved in the
legacy backup, and its v5 mapping remains traceable in the migration record.

This conversion avoids presenting an unevidenced historical inference as an
established v5 pattern. The owner can later confirm, replace, or dismiss the
working claim through normal BIMRI use.

### Direct Migration Sequence

Under the v5 engine lock, the reference engine:

1. discovers the legacy source and optional rolling backup;
2. validates that source selection and parsing are unambiguous;
3. hashes and preserves the exact source bytes;
4. converts recognized tier entries with deterministic IDs and keys;
5. converts unevidenced old patterns into Tier 2 watches;
6. creates immutable revision `V000000`;
7. writes `.bimri/migrations/legacy-to-v5.json` with the source hashes, backup
   paths, mappings, and conversion result;
8. writes v5 state with the revision hash;
9. generates lowercase `bimri.md` and retires the old root-level legacy names;
   and
10. rebuilds the derived retrieval index.

If any required step fails, installation restores its transaction backup and
reports the failure. The selected legacy files remain available byte-for-byte.

## v4 Migration

v4 already has structured state, logs, archives, identifiers, and pattern
evidence. The v5 migration keeps these assets in place:

- run logs and journal IDs;
- archive and inbox contents;
- Tier 1 and Tier 2 entry IDs;
- pattern IDs and evidence references;
- existing log pointers;
- project ID, cadence class, and archive policy;
- active Tier 1, Tier 2, and Tier 3 caps and the maintenance flag threshold;
- run dates; and
- a run count at least as high as the old state or existing log numbers.

The engine does not delete or renumber historical memory. New v5 runs use
six-digit IDs, while migrated IDs keep their original width and remain valid
references.

v5 adds a stable key, source, and trust to shared claims. Migrated v4 content
is mapped conservatively:

| v4 object | v5 conversion |
| --- | --- |
| Tier 1 entry `R12-E3` | key `legacy.r12-e3`, source `legacy`, trust `working` |
| Tier 2 entry `R12-E3` | key `legacy.r12-e3`, source `legacy`, trust `working` |
| Tier 2 status `decision` | status `watch` |
| Pattern `P3` | key `legacy.pattern-p3`, original pattern and evidence IDs retained |

Other valid kinds, importance, active/watch/closed status, first and last run,
tags, text, evidence, falsifier, and pointers remain intact.

The converted memory becomes immutable revision `V000000`. `bimri.md` becomes
the generated view of that revision. The runtime adds:

```text
.bimri/revisions/
.bimri/proposals/
.bimri/decisions/
.bimri/conflicts/
.bimri/resolutions/
.bimri/recovery/
.bimri/migrations/
```

Existing logs, archives, inbox files, and other historical material remain
where they were.

### v4 Migration Sequence

When `state.json` declares BIMRI v4, or has the v4 `current_run_id` shape, the
engine performs these steps under the local engine lock:

1. validates that the migration is unambiguous;
2. copies the old state and hot memory into `.bimri/backups/`;
3. converts recognized v4 hot-memory lines to v5 grammar;
4. preserves the original IDs and adds deterministic legacy keys;
5. creates immutable revision `V000000`;
6. writes resumable `.bimri/migrations/v4-to-v5.json` state;
7. writes v5 state with the revision hash;
8. regenerates `bimri.md`; and
9. rebuilds the retrieval index.

The migration record includes the completion time, exact source-memory hash,
and relative paths to the state and memory backups.

The v4 fields `maintenance_mode`, `tier2_hard`, and
`auto_archive_threshold` are retired. Their values may remain in preserved
backups, but they do not control v5 behavior. v5 maintenance is
judgment-first, Tier 2 uses `tier2_max`, and archival requires an explicit
accepted `close`.

## Idempotence and Bounded-Memory Repair

After state reaches v5, running `migrate` again validates the current state,
synchronizes the generated view, and rebuilds the index. It does not reconvert
entries, allocate different legacy keys, or duplicate history.

If interruption leaves a migration marker or `V000000` while state still
appears legacy, the engine recomputes the deterministic conversion. It resumes
only when the existing artifacts are byte-identical to that conversion. Any
mismatch stops without overwriting either version.

If valid legacy memory already exceeds a v5 tier or byte cap, migration
preserves it and `doctor` reports a bounded-memory repair warning. Subsequent
changes may only reduce inherited overflow without worsening another limit
until all caps are satisfied.

The 16,384-byte generated-view cap is independent of the tier line caps and may
bind first. Tier counts alone do not prove that converted memory is within the
complete rendered-view bound.

## Verify the Upgrade

Have the installing agent run:

```text
python3 bimri-engine.py doctor
python3 bimri-engine.py status
```

The agent should confirm:

- `doctor` reports `PASSED`, or only the documented inherited-overflow warning;
- the version is `5.0` and the head is `V000000` or later;
- Tier 1, Tier 2, and Tier 3 counts are plausible;
- expected legacy text appears in lowercase `bimri.md`;
- the applicable migration record exists;
- every preserved source and backup path named in that record exists; and
- recomputed source hashes match the hashes in the migration record.

Legacy entries intentionally remain `working` until the owner confirms them.
No bulk confirmation is required. Review important claims conversationally as
they become relevant.

## Roll Back

Rollback is an agent task. Tell the agent:

```text
Roll this project back from BIMRI v5 to the exact preserved legacy version.
Use the backup paths and hashes in the applicable migration record, preserve
all v5 artifacts in a separate dated recovery folder, restore the complete old
file set, and validate the restored bytes. Do not delete anything.
```

The agent should:

1. make the folder quiescent across every runtime boundary;
2. copy the complete v5 state into a dated recovery folder;
3. identify `.bimri/migrations/legacy-to-v5.json` or
   `.bimri/migrations/v4-to-v5.json`;
4. verify the preserved source hashes before restoration;
5. restore every source and rolling-backup file named in the record;
6. for v4, restore the matching state, engine, and instruction files as one
   coherent version;
7. for v1-v3, restore the matching historical instruction file from
   `legacy/` only if the owner deliberately wants that old runtime re-enabled;
8. leave the v5 recovery copy untouched; and
9. verify the restored file hashes and report the result.

Restoring only the readable hot-memory file is insufficient for v4 because its
state schema and engine behavior must match. For v1-v3, the exact hot file
and its optional rolling backup are the recoverable memory, while the Global
Instructions are runtime behavior rather than memory content.
