# Changelog

This file records the public BIMRI architecture history. Historical instruction
files are preserved under [`legacy/`](legacy/) and are not current installers.

## Unreleased

- Atomic text and byte writes now retry brief Windows access-denied or sharing
  failures while replacing a file. They retry the same flushed temporary file
  at most six times, with 0.63 seconds of total requested sleep per replacement.
  Existing preflight, locking, integrity checks and caller warning behavior are
  unchanged. Persistent failures still propagate to the caller; other errors
  and non-Windows writes do not gain retries.

## 5.1.2

- Kept this release engine-only and defects-only. The authority and
  mutable-state formats remain v5.1.0, and the readable hot-memory grammar
  remains v5.0.2. No memory file changes shape, no new owner prompt is added,
  and the tab-separated output format of `get`, `recall` and `search` is
  unchanged.
- Proposal preflight receipts now stamp engine release v5.1.2. This engine
  accepts receipts written by v5.1.0, v5.1.1 and v5.1.2, but an older v5.1.1
  engine rejects a store after its first v5.1.2 proposal is staged. Upgrades
  therefore require a quiescent, complete copy of `bimri.md` and `.bimri/`
  outside the project; that copy is the rollback boundary.
- Advanced the authority policy version bound into the audit checkpoint to
  `5.1.2-authority-1`. A checkpoint written by v5.1.1 is a cache miss under
  the new policy, so the first command after the update re-proves the store
  with one full audit, about 35 seconds on a 700-run store, and then publishes
  a fresh checkpoint. `INSTALL.md` step 7 now asks for one normal `doctor` run
  from the updated target before any session starts, so that audit is paid
  there rather than inside the first hook-start's timeout.
- Sealed v5.1.1 checkpoints embedded in quarantine and interrupted-operation
  records remain readable as recovery evidence. A lifecycle recovery finishes
  its exact state/log write, and an authority recovery preserves its frozen
  completion before the new policy audit publishes a v5.1.2 checkpoint.
  Old evidence cannot authorize a warm read or bypass the new text validator;
  unrelated drift still blocks an owner-approved restoration.
- Memory text, rationale, falsifier, conflict question and journal text may
  no longer contain U+0085, U+2028 or U+2029. The text validator accepted
  these Unicode line separators while the hot-memory and run-log parsers split
  lines on them, so one agent proposal could carry a second, forged entry
  line, including a forged `[T:confirmed] [SRC:user]` entry, into the accepted
  head; the next full audit then found a decision naming a revision that did
  not contain its recorded effect, and the store entered
  `AUTHORITY RECOVERY NEEDED` with reads and writes blocked (reproduced
  2026-09-07). The same characters in journal text could make `close` skip
  writing its `[OUTCOME:...]` and `[CLOSED:...]` markers. The validator now
  refuses any value that would parse as more than one line. Behavior change
  for an existing store: a stored value that carries one of these characters
  is invalid at its next full audit and needs owner repair through the
  quarantine and restore lane. No known store contains one.
- A failed full audit at the `sync`, authority `close` and `resolve`
  boundaries now invalidates the audit checkpoint by advancing the state's
  audit epoch, with the checkpoint bytes retained as the prior baseline,
  exactly as `doctor` and the other full-audit boundaries (`status`,
  task-language and historical recall, `search`, `review`, `index`, `maintain`
  and `migrate`) already did after the 2026-09-02 ruling. In v5.1.1 those
  three boundaries wrote the drift receipt and refused without invalidating,
  so the next `start` printed a clean brief and exact reads kept serving a
  store whose writes were paused, and a later proposal-free lifecycle `close`
  republished the checkpoint over the proven damage. A degraded `propose`
  still invalidates by deleting the checkpoint file; that is the second
  mechanism and is unchanged. Read-only `doctor`, the quarantine shadow audit,
  blocked-quarantine and prior-evidence-invalid issues and a strict restore
  comparison keep their exemptions. Behavior change: a refused `sync`, `close`
  or `resolve` performs one extra state save, the next `start` prints
  `AUTHORITY RECOVERY NEEDED`, exact reads refuse until the store is repaired,
  and a `hook-close` at session end exits 2 on a store the engine has already
  proved damaged, as in v5.1.0.
- The `--quiescent` attestation is now checked before version routing for
  every existing v5 store. In v5.1.1 a v5.0 or v5.0.1 store upgraded to v5.1.0
  without it and ignored the flag when it was passed, which contradicted the
  protocol and meant the same store was refused without the flag one command
  later, once it was at v5.1.0. Behavior change: an install into a v5.0 or
  v5.0.1 store without `--quiescent` exits 2 with the same handoff message the
  v5.0.2 and v5.1.0 routes already printed, and creates nothing in the target.
- `--trust contested` can no longer be authored from the CLI or admitted at
  proposal preflight. The engine never wrote contested trust itself (only a
  human-approved resolution writes `confirmed`), so an agent could render a
  `[T:contested]` line that `review` had nothing to resolve. Contested trust
  is derived from a conflict record only. Stored records that carry it still
  parse and validate, so no existing store needs repair.
- Tier 3 proposals no longer write a false drift receipt. `propose` advanced
  the pattern counter in `state.json` outside its declared write scope, so
  every Tier 3 proposal left a state change the next audit could not attribute
  to a recorded operation and receipted as unexplained drift. The counter now
  advances when the pattern is accepted, inside the `sync` or `close` scope,
  and pattern IDs remain monotonic. A receipt v5.1.1 already wrote stays on
  disk as retained evidence and needs no action.
- Orphan candidates in the start brief are classified by inactivity rather
  than start time. A run is a candidate when its last recorded activity, the
  `last_activity_at` that `journal`, `propose` and `sync` maintain, falling
  back to `started_at`, is more than 24 hours old; v5.1.1 labeled a run an
  orphan 24 hours after it started even when it had journaled a second
  earlier. The brief prints every id while there are five or fewer
  candidates; above that it prints the count plus the five newest ids and
  points to `status` for the full list (134 ids cost 500 to 800 tokens on
  every start of the development store). This is classification only: nothing
  is closed, stamped or mutated, and `recover-run` still needs the owner's
  word per run.
- `hook-start` refuses a payload that carries no `session_id` or
  `transcript_path` (empty, invalid, non-object or id-less stdin) with exit 2
  and a message ending in `[hook-identity-missing]`, and creates nothing.
  v5.1.1 opened a run under an invented identity that the matching
  `hook-close` could never close, so every such session left an orphan.
  Claude Code does not block a session on a `SessionStart` hook exit code:
  the session opens without a brief, the same state as after a lock-busy
  start, and the explicit `start --actor` in `AGENTS.md` covers it.
  `hook-close` for an unknown or unmapped session remains a successful no-op.
  `hook-close` also flattens the `SessionEnd` reason it records (control
  characters become spaces, whitespace collapses) and bounds it to 200
  characters, so a long or multi-line reason can no longer make the close
  exit 2 and leave the run open.
- `get`, `recall` and `search` accept `--json` and return the full typed
  record. The tab-separated output prints seven columns (location, key, id,
  reason, trust, source, text), so a pattern lost its confidence, observation
  count, evidence ids and falsifier, a Tier 1 or Tier 2 row lost its tier,
  kind or importance and status, first and last run, tags and pointer, and
  two pattern generations printed byte-identical in `--history`. The JSON
  payload is an object with `matches`, `total` and `omitted`; each match
  carries the seven fields above (the entry text under `text`) plus `tier`,
  then `kind` for Tier 1, `importance`, `status`, `first_run` and `last_run`
  for Tier 2, `confidence`, `observations`, `evidence` and `falsifier` for
  Tier 3, and `tags` and `pointer` for Tiers 1 and 2; a held candidate also
  reports its `operation`. The tab-separated output is unchanged. Tags and
  falsifiers are now part of the search text on both output paths, which adds
  matches and removes none. Exit codes are unchanged: 0 matched, 1 no match,
  2 error.
- The installer no longer deletes owner text after an orphan
  `<!-- BIMRI:START -->` marker. The block-merge pattern matched from the
  first start marker to the real block's end marker, so a stray marker in
  owner prose made the next install delete everything between them (the
  install backup still held the text). The merge now pairs a start marker
  only with the first end marker that follows it with no other start marker
  between, so an orphan marker and the prose after it stay in place.
- The update receipt's `mode` reads `code-only-update` for a same-format
  update, a v5.1.0 store taking a newer engine. v5.1.1 wrote
  `lossless-authority-activation` for every existing-store update even when
  no state was activated, and because terminal receipt validation enforces
  the unchanged tree and state digests only for `code-only-update` receipts,
  the mislabel loosened later validation of those receipts. A v5.0.2 to
  v5.1.0 activation keeps `lossless-authority-activation`.
- A command whose stdout pipe closes early now exits with its own exit code.
  This covers writes during the command and the final flush, with buffered
  and unbuffered output. A disconnected reader no longer interrupts a command
  after its state changes. Real output failures, including a full disk, still
  return a nonzero exit code and report an error when stderr is available.
- Stated documentation corrections with no engine change. A divergent
  `bimri.md` does not stop the v5.1 update: the update completes as
  `installed-recovery-required` with the file untouched, and the next `start`
  heals the view as a manual-edit conflict, while damaged core state or a
  head hash mismatch stops the update before any write (`MIGRATION.md` said
  the opposite). The v5.0 and v5.0.1 upgrade route runs the lifecycle repair
  path and may heal the view and rebuild the index, unlike the v5.0.2 and
  v5.1.0 update path. `.bimri/index.tsv` is a derived export that `recall`,
  `get` and `search` do not read. `REFERENCE.md` now names both checkpoint
  invalidation mechanisms.

## 5.1.1

- Kept the performance work as an engine-only patch. The authority and
  mutable-state formats remain v5.1.0, and the readable hot-memory grammar
  remains v5.0.2.
- Proposal preflight receipts now stamp engine release v5.1.1. This engine
  accepts receipts written by v5.1.0, but an older v5.1.0 engine rejects a
  store after its first v5.1.1 proposal is staged. Upgrades therefore require
  a quiescent, complete copy of `bimri.md` and `.bimri/` outside the project;
  that copy is the rollback boundary.
- Added a compact engine-managed audit checkpoint bound to the v5.1.0 current
  authority, with the detailed protected path-and-hash inventory stored as
  separate audit evidence. Warm exact-current reads and ordinary lifecycle
  bookkeeping no longer enumerate or hash the historical authority tree.
  Behavior change: warm reads defer whole-tree verification to
  authority-changing writes and explicit audit, review, search, and
  historical-recall boundaries, so an out-of-engine edit to unrelated history
  is seen at the next such boundary rather than on every read.
- The checkpoint is a derived cache, never an authority record. Divergence from
  it forces the full semantic audit; when that audit passes over changes the
  engine cannot attribute to its own recorded operation, the engine first
  durably records a sealed drift receipt under `.bimri/audit-drift/`, carrying
  the diverging paths with prior and current hashes (inline up to 2,000 entries
  per section with any remainder counted, the complete delta pinned in a
  hash-and-size-validated attachment when truncated), with an unbounded
  monotonic sequence number and retention bounded to the newest 200, and only
  then publishes the new baseline. The written receipt is re-read and fully
  validated (seal, filename binding, attachments, post-prune existence) before
  the write counts as success, and deduplication only ever reuses a receipt
  that validates. A receipt that cannot be recorded keeps the prior checkpoint
  and surfaces as an error; `doctor` validates every receipt and its
  attachments and reports damaged evidence instead of trusting it; attachments
  cited by retained receipts outlive the unreferenced-attachment bound; and a
  checkpoint whose referenced manifest evidence is missing refuses rebaselining
  instead of adopting new bytes with no recorded delta. A failed semantic audit
  refuses into the existing damaged-authority recovery lane and invalidates the
  checkpoint by advancing the state's audit epoch (owner-ruled 2026-09-02), so
  the next `start` prints `AUTHORITY RECOVERY NEEDED` and exact reads refuse
  until the store is repaired, as in v5.1.0. The checkpoint bytes stay on disk
  as the prior baseline for receipts, quarantine and restore, the same
  retained-but-invalid shape a failed resolution leaves; a blocked receipt
  sink, an open quarantine and a strict restore comparison keep their prior
  checkpoint readable. Interrupted operations self-heal exactly as in v5.1.0;
  no drift or crash state ever requires hand deletion of derived files.
  `audit-blocked.json` now appears only while an owner-approved quarantine
  holds its pre-repair baseline, and restoration clears it. Owner-ruled
  2026-08-27: this receipts contract replaces the earlier normative fail-closed
  rule in the protocol, and the current-only exact recall plus deferred
  warm-read verification below are confirmed semantics.
- An obstruction on the audit-transition marker path that is not a regular
  file or symlink is a hard error on every surface; doctor never reports
  health past it, and the checkpoint is left untouched until the owner
  removes the obstruction.
- During interrupted-transition recovery an archive month counts as the
  operation's own effect only when its change is byte-provably the prior
  witnessed content plus appended rows stamped by the operation's scope.
  Any other archive change is preserved as drift evidence.
- Read-only doctor and warm exact reads enforce the same legacy-lineage
  refusals as writable load: marker and state must claim each other, and
  unclaimed legacy root files refuse the read.
- Authority-changing writes still re-verify the complete evidence inventory:
  propose 1.31 s, sync 1.26 s and authority close 1.24 s measured on the
  ~500-run live-size store. This exceeds the design brief's one-second
  entry-point target and is recorded as a known, owner-accepted deviation
  (2026-08-27); incremental authenticated manifests are the planned v5.2
  fix. Reads and lifecycle bookkeeping are unaffected.
- Unknown files inside witnessed roots, crash-orphaned engine temp files
  included, are never deleted or blocked on. They enter the audited
  inventory, cost at most one full re-audit when they first appear, and stay
  visible through drift receipts and doctor litter reporting. The engine
  cannot prove a temp-named file is its own, so it preserves it.
- Added a direct current-key path for `get --key` and `recall --key` without
  `--history`. Behavior change: it returns only the accepted current
  generation (hot or cold-current); held candidates and history remain
  reachable through `--history`, `--query`, and `review`. It validates at
  most the bounded hot head plus one selected cold archive month.
- Removed automatic derived-index rebuilds from start, commit, and resolution
  hot paths. `index`, `maintain`, `doctor`, installation, and migration remain
  the explicit rebuild points; the index remains non-authoritative.
- Increased the example Claude hook timeout to 90 seconds so a cold first audit
  on an established store has enough time to complete and seed its witness.
  Dimension this against your store: the cold first audit measured 32.4 s on a
  ~200-revision store and 77.5 s on a 10x synthetic store (2026-08-23, Windows
  11 desktop), so a very large store may need a larger hook timeout for its
  first run after installing this release.
- Made a `failed` owner-resolution record fail closed as an explicit recovery
  condition. Ordinary retrieval and shared-memory writes pause until the owner
  explicitly retries the recorded conflict choice; a failed attempt can no
  longer be mistaken for a healthy audited store.
- Reworked the public README around BIMRI's open-source, local-first persistent
  memory protocol, cross-session retrieval, human-governed provenance, agent
  runtime support, and explicit same-lock-domain concurrency boundary.
- Measured before and after on the live development store (~198 revisions,
  433 run logs, Windows 11 desktop, 2026-08-23): `recall --key` warm
  engine-side p50 11 ms (was ~21.3 s), warm CLI end-to-end p50 282 ms;
  `start` 0.36 s (was 51.8 s); one-line `journal` ~0.30 s (was ~23.4 s);
  `close` 0.35 s (was 43.8 s). On a 10x synthetic store, warm reads held
  p50 11 ms and p99 18 ms.
- Console output is UTF-8 on every host. Memory text is UTF-8 on disk, and
  on Windows a piped or redirected stdout defaulted to the ANSI code page,
  so `get`, `recall` and `search` died with a codec error on any entry
  carrying a character outside it (reproduced 2026-09-02; present since
  v5.1.0). The engine now reconfigures stdout and stderr to UTF-8 at
  startup.
- Stated behaviour after an interrupted authority write: while a proposal
  decision or an owner resolution that the next command's recovery pass
  cannot settle on its own remains `applying` or `failed` (a `sync` killed
  mid-batch is the reproduced case), a passing audit still refuses to
  publish a fresh checkpoint, so `start` and exact reads take the
  full-audit path until that run's own `sync` or `close` settles the
  records, or the owner authorizes `recover-run`. `doctor` passes meanwhile
  and lists each unfinished applying decision with the remedy. The first
  command after a killed `propose` may refuse with
  `an earlier audit transition is incomplete; run doctor before retrying`;
  the following command, or `doctor`, completes the recovery. No hand
  deletion is ever required.
- Stated boundary: run logs under `.bimri/log/` are not part of the
  witnessed inventory. The seven witnessed roots are proposals, decisions,
  conflicts, resolutions, revisions, archive and recovery, as in the
  original checkpoint design; an out-of-engine append to a closed run log
  is not detected by any audit boundary in v5.1.0 or v5.1.1.
- The missing-lock refusal on read-only paths now names the lifecycle
  commands that recreate the lock file.

## 5.1.0

- Restored normal Tier 1 authoring and confirmed-memory updates. Direct
  `user/confirmed` proposals may create, promote, update, or close Tier 1 and
  Tier 2 subjects without creating authority-policy conflicts; genuinely
  incompatible cross-run writes to the same key remain concurrent conflicts.
- Replaced hard per-tier counts with soft curation targets. The generated hot
  view keeps its configurable byte ceiling, while valid writes are no longer
  rejected because Tier 1 or Tier 2 reached an arbitrary line count.
- Added deterministic Tier 2 cooling under byte pressure. Cooling changes
  residency rather than truth: the exact current generation, stable key,
  trust, source, and provenance remain retrievable. Maintenance no longer uses
  unrelated global run starts as an ageing accelerator.
- Made subject creation explicit with `--new-subject`. Exact-key `set` updates
  keep one current subject and preserve every displaced generation. Unmatched
  updates and unauthorized semantic changes remain durable held candidates
  instead of disappearing or creating routine owner conflicts.
- Added read-only exact-key and task-language recall across hot, cold-current,
  replaced, and closed generations. Archived rows retain their stable key and
  the derived index can be rebuilt without losing retrieval.
- Made conflicts, deterministic no-ops, human resolutions, and interrupted
  resolution recovery cold-aware. Their immutable snapshots remain valid after
  later cooling or same-key work, without rolling current memory backwards.
- Advanced mutable state and new authority artifacts to v5.1.0 while retaining
  the readable v5.0.2 hot-memory grammar. Existing v5.0.2 installs preserve
  `bimri.md`, accepted revisions, and immutable evidence byte-for-byte, back up
  the exact old state, and activate v5.1 state transactionally so older engines
  fail closed.

## 5.0.3

- Made ordinary operation quiet. `start` and `hook-start` no longer replay
  review records, and `sync`/`close` announce a concurrent conflict only when
  that candidate generation is first created.
- Added pull-based `review`, human-readable add/replace/remove/refresh choices,
  explicit action consequences, pagination totals, and separate status counts
  for actionable concurrency, legacy review records, recovery reviews, and
  satisfied historical candidates.
- Added proposal preflight against the accepted keyed head, same-run retry
  idempotency, capacity reservation, and pre-mutation containment for new core
  memory, confirmed-memory replacement/removal, public system provenance, and
  semantic uncertainty. Routine policy and validation failures no longer
  allocate owner-conflict records.
- Limited new memory conflicts to proven incompatible cross-run writes against
  the same key. Exact sets, compatible closes, and causally compatible touches
  resolve deterministically.
- Derived historical exact-effect satisfaction from accepted revision ancestry
  without rewriting old authority. Close satisfaction now requires exact
  archive provenance, and archive replay rejects a proposal marker whose
  reason or removed line differs.
- Split engine release `5.0.3` from memory and authority-record format `5.0.2`.
  Existing v5.0.2 stores use a dedicated code-only update path with a read-only
  audit and a protected-tree manifest. The accepted head, `bimri.md`, and every
  pre-existing protected `.bimri/` path must remain byte-identical.

## 5.0.2

- Made human resolution explicit and provenance-preserving. A new resolution
  requires `--human-approved`, which records an auditable human-approval
  attestation without pretending to authenticate the caller. Choosing an agent
  or external proposal may raise its trust to `confirmed`, while `source`
  remains its immutable origin. Existing resolution history is not rewritten.
- Added fail-closed recovery for damaged proposal, decision, conflict, and
  resolution records. Degraded `start` and full `status` remain available;
  `status` exits nonzero and shared-memory writes stay paused. Owner-approved
  quarantine preserves exact damaged file bytes (or exact unsafe-link target
  metadata) behind a validated blocker, and
  staged restore validates replacements, retains the originals, and writes
  content-addressed authorization receipts until the full graph is healthy.
  Deleted records are recoverable through durably referenced absence evidence;
  unreferenced IDs are refused, proposal IDs are never reused, and replacement
  semantics are preflighted in an isolated authority-graph shadow.
- Made direct hot-view recovery content-addressed and repeatable. Existing
  v5.0.1 states upgrade their version and generated-view header automatically
  without changing their limit profile or rewriting accepted revisions. An
  unmapped Claude `hook-close` is now a successful no-op that cannot close a
  different active run.

## 5.0.1

- Expanded the generated hot view to 49,152 bytes, roughly 12,000 tokens for
  ordinary English, with 20 Tier 1, 40 Tier 2, and 12 Tier 3 entries. The
  elastic curation target is approximately 3k/6k/3k tokens across the tiers;
  the durable long tail remains in logs, revisions, decisions, resolutions,
  archives, and backups. Stock v5.0 limit profiles expand automatically while
  custom profiles remain unchanged. Historical v5.0 view labels are normalized
  in a new immutable revision without rewriting their original revision or
  worsening a preserved custom capacity.
- Made v1-v4 conversion visible through an explicit installation receipt and
  fail-closed handling for unclaimed legacy files beside structured state.
  Inherited claims above 500 characters are preserved for later compression
  when their complete serialized entries fit the 4,096-character safety
  ceiling; new and edited claim text remains capped at 500 characters.
- Replaced assumed `python3` invocations with verified absolute Python 3.8+
  runtime bindings. Discovery rejects silent aliases, the installer validates
  both its source and installed engine, and the rendered Claude hook snippet
  pins that interpreter. The installing agent can merge and smoke-test the
  snippet in machine-local Claude configuration without changing shared
  settings. The installer writes host-only binding records at
  `.bimri/runtime.local.json` and `.bimri/hooks.claude.local.json`; they are not
  portable memory and must not be committed.

## 5.0.0

- Made BIMRI a portable memory layer for Claude, Codex, and other local agents.
- Replaced direct hot-file writes with isolated run logs, proposals, immutable
  revisions, stable keys, deterministic conflict detection, and durable human
  resolutions.
- Made `bimri.md` a bounded generated view while preserving full evidence and
  history under `.bimri/`.
- Added cross-process locking, atomic durable writes, crash recovery,
  byte-exact direct-edit preservation, transactional installation, validation,
  and a rebuildable retrieval index.
- Added direct, conservative migration from v1-v3 tiered workspaces and
  engine-based v4 workspaces, with byte-exact backups and deterministic,
  semantically validated source-to-v5 authority records.
- Made rollback self-contained by packaging the inert historical references
  and BIMRI's MIT notice without replacing a target project's own license.
- Kept Claude Code hooks as an optional adapter rather than a requirement.

## 3.0.0

- Simplified the public Claude Cowork protocol to one active memory file and
  one rolling backup.
- Defined memory deltas, day-and-session freshness, bounded tiers, mandatory
  pruning, and stronger separation between active context and session history.
- Used uppercase filenames for new workspaces while retaining lowercase
  compatibility.

The exact pre-v5 public explanation is preserved at
[`legacy/v3/README.md`](legacy/v3/README.md).

## 1.0.0

- Introduced Brief Interaction Memory and Retrieval Intelligence as a tiered
  Markdown memory protocol for Claude Cowork.
- Defined core intelligence, active context, pattern recognition, freshness,
  backups, and session maintenance through Global Instructions.
