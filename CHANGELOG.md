# Changelog

This file records the public BIMRI architecture history. Historical instruction
files are preserved under [`legacy/`](legacy/) and are not current installers.

## 5.1.1

- Kept the performance work as an engine-only patch. The authority and
  mutable-state formats remain v5.1.0, and the readable hot-memory grammar
  remains v5.0.2.
- Added a compact engine-managed audit checkpoint bound to the v5.1.0 current
  authority, with the detailed protected path-and-hash inventory stored as
  separate audit evidence. Warm exact-current reads and ordinary lifecycle
  bookkeeping no longer enumerate or hash the historical authority tree.
  Behavior change: warm reads defer whole-tree verification to
  authority-changing writes and explicit audit, review, search, and
  historical-recall boundaries, so an out-of-engine edit to unrelated history
  is seen at the next such boundary rather than on every read.
- The checkpoint is a derived cache, never an authority record. Divergence
  from it forces the full semantic audit; when that audit passes over changes
  the engine cannot attribute to its own recorded operation, the engine
  preserves a bounded append-only receipt under `.bimri/audit-drift/`,
  rebaselines, and continues, and `doctor` reports the receipt count and the
  newest reasons. A failed semantic audit refuses into the existing
  damaged-authority recovery lane. Interrupted operations self-heal exactly as
  in v5.1.0; no drift or crash state ever requires hand deletion of derived
  files. `audit-blocked.json` now appears only while an owner-approved
  quarantine holds its pre-repair baseline, and restoration clears it.
- Unknown files inside witnessed roots — crash-orphaned engine temp files
  included — are never deleted or blocked on. They enter the audited
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
