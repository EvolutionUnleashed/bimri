# Changelog

This file records the public BIMRI architecture history. Historical instruction
files are preserved under [`legacy/`](legacy/) and are not current installers.

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
