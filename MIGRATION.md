# BIMRI Migration and the v5.1.0 Lifecycle Upgrade

Engine v5.1.1 uses authority format v5.1.0 while retaining the readable v5.0.2
hot-memory grammar. The upgrade from v5.1.0 is one-way: v5.1.1 stamps its
own engine release into proposal preflight receipts, and every proposal —
pending or decided — remains an immutable authority record that a v5.1.0
engine keeps validating, so it rejects the store permanently once any
v5.1.1 proposal has been staged. Safe rollback exists only by restoring the
complete pre-update backup taken before the first v5.1.1 proposal. The
engine automatically migrates explicitly versioned v1-v3
tiered Markdown and the engine-based v4 format. This canonical repository publicly
distributed the original v1 and streamlined v3 instructions; the parser also
accepts a valid v2 header. Migration preserves old material before creating v5
state and stops when it cannot identify or interpret that material safely.

Ask an agent to install v5 according to [`INSTALL.md`](INSTALL.md). After the
v5 files are present, the same migration can be requested explicitly:

```text
<verified-python> bimri-engine.py migrate
```

`<verified-python>` means the exact absolute Python executable established by
the execution-and-sentinel procedure in [`INSTALL.md`](INSTALL.md). It is a
placeholder, not a literal command. A PATH name and a zero-output probe are not
evidence that Python ran.

## Required Before Every Upgrade

Make the project quiescent across every runtime boundary:

1. finish or stop every agent using the project;
2. wait for every BIMRI command to finish;
3. pause any synchronization or copy operation affecting the folder;
4. take a complete copy of the whole project folder (`bimri.md` plus the
   entire `.bimri/` tree) somewhere outside it — this snapshot is the only
   rollback that exists, because the first v5.1.1 proposal is an
   intentional one-way boundary that no older engine can read past;
5. if v1-v3 was installed in Claude Cowork Global Instructions, disable or
   remove that BIMRI block; and
6. keep the folder quiescent until migration and `doctor` complete.

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
- structured v5 state coexists with an unclaimed legacy hot-memory candidate;
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
and representable. New and edited v5 claim text is capped at 500 characters.
Migration may preserve longer inherited text without truncation when the
complete serialized v5 entry, including metadata, is at most 4,096 characters.
It marks text above 500 characters as inherited overflow and reports it for
later human-guided compression. If a claim cannot fit losslessly within the
serialized-entry ceiling, migration stops before mutation and its exact source
bytes remain untouched. The old header session count seeds the v5 run count.
Dates, weights, observation counts, confidence, and other legacy-only metadata
remain recoverable in the exact source backup. Generated keys and IDs are
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
10. rebuilds the derived retrieval index; and
11. prints a migration receipt naming the detected source version and file,
    imported Tier 1 and Tier 2 counts, converted-pattern count, byte-exact
    backup location, migration-record path, and validation result.

If any required step fails, installation restores its transaction backup and
reports the failure. The selected legacy files remain available byte-for-byte.
Missing receipt fields or zero output is an installation failure, never an
implicit success.

## v4 Migration

v4 already has structured state, logs, archives, identifiers, and pattern
evidence. The v5 migration keeps these assets in place:

- run logs and journal IDs;
- archive and inbox contents;
- Tier 1 and Tier 2 entry IDs;
- pattern IDs and evidence references;
- existing log pointers;
- project ID, cadence class, and archive policy;
- configured Tier 1, Tier 2, and Tier 3 values, which become soft targets, and
  the maintenance flag threshold;
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

As with v1-v3, inherited v4 claim text above 500 characters is preserved
without truncation and reported for later compression when the complete
converted entry fits the 4,096-character serialized-entry ceiling. Conversion
stops before mutation if the entry cannot fit losslessly. The 500-character
text limit continues to apply to every new or edited v5 claim.

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
8. regenerates `bimri.md`;
9. rebuilds the retrieval index; and
10. prints the same explicit migration receipt used for v1-v3 conversion.

The migration record includes the completion time, exact source-memory hash,
and relative paths to the state and memory backups.

The v4 fields `maintenance_mode`, `tier2_hard`, and
`auto_archive_threshold` are retired. Their values may remain in preserved
backups, but they do not control v5.1 behavior. Tier counts become soft
curation targets. The byte-bounded hot working set uses non-destructive
Tier 2 cooling, while an explicit accepted `close` remains a semantic removal.

## v5 Maintenance Upgrades

### v5.0.2 memory to v5.1.0 authority

An existing v5.0.2 store already uses the visible line grammar retained by
v5.1. Public engine v5.0.3 deliberately retained that persisted version, so
this is also its normal upgrade path. Installing v5.1.0 therefore preserves
root `bimri.md`, the accepted head, every immutable revision, log, proposal,
decision, conflict, resolution, archive, migration record, recovery artifact,
and unknown owner file exactly. It does not collapse, rename, or infer semantic
subjects during installation.

The mutable `state.json` is different. Under the existing engine lock and an
explicit quiescent handoff, the updater validates the complete v5.0.2
authority graph, copies the exact old state into the sibling
`.bimri-update-backups/<timestamp>/` transaction, adds the v5.1 lifecycle
fields, changes the state version to 5.1.0, and commits that state last. This
immediately makes v5.0.3 fail closed instead of writing through v5.1 residency
authority. A repeated or interrupted install resumes or rolls back
idempotently from the prepared receipt.

Before success, the updater proves that root `bimri.md`, the accepted head and
hash, and every immutable/unknown pre-existing path remain byte-identical. It
runs the installed engine's read-only doctor against the activated state and
records both the old-state backup and the preservation digests. A divergent
`bimri.md` or damaged core authority stops activation without healing or
rewriting it. See [`INSTALL.md`](INSTALL.md) for the exact transaction and
receipt contract.

The old-process shutdown is a real precondition. The file lock cannot safely
upgrade an already-loaded old process that resumes after installation. Once
the quiescent transaction commits v5.1 state, any later v5.0.3 command rejects
the unsupported state version before mutation.

### v5.0 and v5.0.1 to v5.1.0 authority

Existing v5.0 and v5.0.1 states upgrade automatically under the engine lock.
Before changing state, the engine validates the complete source state and
accepted head and preserves an exact content-addressed state backup under
`.bimri/backups/`. Accepted historical revisions and existing governance
records are never rewritten. Historical v5.0 and v5.0.1 resolutions retain
their original effect semantics. The readable hot grammar is normalized only
through the existing v5.0.2-compatible metadata path.

For v5.0, if the complete stored profile exactly matches the original defaults,
the upgrade adopts values of 20 for Tier 1, 40 for Tier 2, 12 for Tier 3, a
500-character new-entry text ceiling, and a 49,152-byte hot view. Tier values
become soft targets. If any v5.0 value was customized, the complete custom
profile is retained; the engine does not guess which individual values the
owner meant to customize.

For v5.0.1, the complete configured profile is preserved as soft curation
targets. The v5.1 upgrade changes authority state and generated-view metadata,
not the owner's stored values.

When the active revision still contains a historical v5.0 or v5.0.1 header or
fixed cap comments, the engine preserves that revision byte-for-byte and makes
a metadata-only next revision the active head. Entry lines are unchanged. If
the normal descriptive comments would exceed a preserved custom byte cap, a
shorter `state.json`-referenced form is used; normalization may not worsen any
existing overflow. Interrupted retries reuse only byte-identical next-revision
content, and conflicting revision bytes stop the upgrade.

The upgrade receipt names the source and target versions, reports whether the
v5.0 defaults expanded or the existing limits were preserved, and records the
old and active profiles, state-backup path, and any metadata-only revision. A
full semantic validation runs before `migrate` reports `Validation: PASSED`.
State/head/metadata preflight failures stop before upgrade authority changes.

### Authority Damage Found During Upgrade

Damage to a proposal, decision, conflict, or resolution does not authorize the
engine to discard that record. When core state and the accepted head are valid,
installation can complete the v5.1.0 authority upgrade while
reporting `AUTHORITY RECOVERY NEEDED`. The install manifest records the
recovery-required result. `start` remains available with a degraded brief, and
`status` prints the full status but exits nonzero. Shared-memory commits remain
paused until the authority graph validates.

Recovery is explicit and owner-approved. First preserve the damaged record and
put a validated blocker at its authority path:

```text
<verified-python> bimri-engine.py quarantine-authority \
  --kind conflict --id C000003 --human-approved
```

The recovery copy is named by its SHA-256 hash and retains the exact original
file bytes. For an unsafe symbolic-link authority path, BIMRI replaces the link
without following it and preserves a canonical evidence record containing the
exact target and target bytes; it does not touch the external target. Repair
and review a separate copy. A deleted record can be quarantined only when a
durable log, dependency, or conflict counter proves that exact ID existed;
BIMRI records canonical absence evidence and refuses unreferenced IDs. Then
stage restoration:

```text
<verified-python> bimri-engine.py restore-authority \
  --kind conflict --id C000003 --from /path/to/repaired.json \
  --human-approved
```

The engine validates the replacement's identity, structure, effects, and graph
relationships in an isolated shadow before recording a content-addressed
authorization receipt or replacing the blocker. It keeps the damaged recovery
evidence. Missing or altered evidence makes `doctor` fail even when the
canonical authority graph itself is healthy.
Related proposal, decision, conflict, and resolution repairs may be restored
one at a time. A partial restore remains blocked; only a complete, valid
authority graph resumes shared-memory writes. Run `doctor` after the final
restore and do not report the upgrade healthy until it passes.

## Idempotence and Bounded-Memory Repair

After state reaches v5, running `migrate` again validates the current state,
synchronizes the generated view, and rebuilds the index. It does not reconvert
entries, allocate different legacy keys, or duplicate history.

If interruption leaves a migration marker or `V000000` while state still
appears legacy, the engine recomputes the deterministic conversion. It resumes
only when the existing artifacts are byte-identical to that conversion. Any
mismatch stops without overwriting either version.

If valid legacy memory already exceeds the hot-view byte ceiling or inherited
entry-text ceiling, migration preserves it and `doctor` reports a
bounded-memory repair warning. Subsequent changes may only reduce inherited
overflow without worsening another enforced bound. Legacy tier counts become
soft targets and do not block new memory.

The 49,152-byte generated-view ceiling remains independent of tier counts.
Tier counts alone do not prove that converted memory fits the complete
rendered-view bound.

## Verify the Upgrade

Have the installing agent run:

```text
<verified-python> bimri-engine.py doctor
<verified-python> bimri-engine.py status
```

The agent should confirm:

- `doctor` reports `PASSED`, or only the documented inherited-overflow warning;
- the authority version is `5.1.0`, the hot grammar is `5.0.2`, and the head
  is `V000000` or later;
- Tier 1, Tier 2, and Tier 3 counts are plausible;
- expected legacy text appears in lowercase `bimri.md`;
- the applicable migration record exists;
- every preserved source and backup path named in that record exists;
- recomputed source hashes match the hashes in the migration record; and
- the printed receipt agrees with the migration record and validation result.

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
