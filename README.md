# BIMRI: Portable Memory for Local Agents

**Brief Interaction Memory and Retrieval Intelligence, engine v5.0.3. Memory
and authority-record format: v5.0.2.**

BIMRI gives a project one durable memory that Claude, Codex, and other local
agents can share without a server, database, account, or model-specific
service. The memory travels with the folder. Its readable current state stays
small, while its evidence, decisions, and revision history remain on disk.

Installation is deliberately simple: point your agent at the repo and ask it
to install BIMRI.

> Point your agent at the repo and ask it to install BIMRI.

The agent follows [`INSTALL.md`](INSTALL.md), merges BIMRI into the project's
existing instructions, preserves existing BIMRI memory, and runs a self-check.
The runtime is one Python 3.8+ standard-library script plus ordinary local
files. There are no packages to install. Commands below use
`<verified-python>` as a placeholder for the absolute Python 3.8+ executable
that the installer has executed and verified on the current machine. A name
such as `python3` is never assumed to work. Multiline examples use POSIX
continuation syntax; on Windows, run them on one line or adapt them to the
active shell and path format.

Machine bindings are not portable memory. The installer writes
`.bimri/runtime.local.json` with the verified runtime argv prefix and
`.bimri/hooks.claude.local.json` with the rendered Claude hook source. Do not
commit either file or copy its absolute paths into shared instructions. After
moving the project or replacing Python, rerun installation to regenerate both
files before an agent uses BIMRI.

## What v5 Is

`bimri.md` is a generated view of the latest accepted memory revision. Agents
do not compete to rewrite it. Each agent instead receives its own run handle,
writes to its own append-only log, and submits proposals against stable memory
keys. The engine serializes the short shared commit, checks the proposal's base
hash, and either creates a new immutable revision or stops with an actionable
agent error. A human-facing memory conflict is reserved for genuinely
concurrent, incompatible changes to the same stable key.

The human remains the authority. Normal starts stay quiet, including when an
older review record exists. A newly proven concurrent conflict is explained
once when it is raised and remains available through the pull-based `review`
command. The agent records an explicit owner choice with
`resolve --human-approved`; the owner never has to interpret raw record IDs or
edit a memory file. The flag records the agent's assertion that the owner
approved that exact choice. It is an auditable attestation, not authentication.

This design gives BIMRI four useful properties:

- **Portable:** the project folder contains the memory, evidence, and history.
- **Agent-independent:** any local agent that can run Python and follow
  `AGENTS.md` can use the same memory.
- **Concurrency-safe in one shared lock domain:** independent run handles, an
  operating-system file lock, atomic replacement, and key-specific base hashes
  prevent one agent from silently overwriting another.
- **Human-governed:** exceptional concurrent choices are shown as actions and
  consequences, with a durable resolution record for an explicit owner choice.

## Active Memory, Not a Diary

Flat-file memory tends to become a transcript of completed work. Every session
adds another summary, old context stays visible, and useful signal is buried in
a context swamp. A larger file then makes retrieval and agent judgment worse.

BIMRI keeps the readable view deliberately small. Tier 1 holds durable facts,
preferences, decisions, and rules. Tier 2 holds current work, risks, and next
actions. Tier 3 holds patterns only when evidence and a falsifier exist. Full
detail belongs in run logs, revisions, decisions, and archives, where an agent
can retrieve it when needed without forcing it into every future context.

The result is active memory: the current state most likely to change the next
useful action, backed by a durable long tail. Run logs, immutable revisions,
decisions, resolutions, archives, and migration backups remain available even
when they are outside the generated hot view.

## Install

Give a coding agent this repository URL and say:

```text
Install BIMRI in this project from https://github.com/EvolutionUnleashed/bimri.
Follow INSTALL.md, preserve my existing instructions and memory, and run the
self-check.
```

The installer command the agent runs is:

```text
<verified-python> bimri-engine.py install --target /absolute/path/to/the/project
```

Existing BIMRI v1-v4 memory migrates automatically during installation. The
installer prints a migration receipt with the detected source version and
file, imported counts, converted patterns, backup location, and validation
result. See
[`MIGRATION.md`](MIGRATION.md) for preservation and rollback details.
Existing v5.0 and v5.0.1 states also upgrade automatically. A stock v5.0 limit
profile expands to the v5.0.1 capacity; custom v5.0 profiles remain custom.
Every v5.0.1 limit remains unchanged during the v5.0.2 state and generated-view
header upgrade. Earlier state bytes are backed up, and accepted revisions are
preserved rather than rewritten.

Updating an existing v5.0.2 store to engine v5.0.3 is a dedicated code-only
operation. Stop every process running the old engine first, then invoke install
with the mandatory `--quiescent` handoff attestation. The updater audits
the accepted head without healing or rebuilding memory, protects `bimri.md`
and every pre-existing path under `.bimri/`, and verifies that their path set,
types, symlink targets, and bytes are unchanged before it reports success.
State and all new authority records remain format v5.0.2; no memory migration,
header change, limit change, reindex, or open-record rewrite occurs.

The v5 installer serializes with other v5 engine commands in the same lock
domain. Before upgrading any earlier version, disable the old Claude Cowork
Global Instructions and stop every agent using that memory. Earlier versions
write files directly and cannot participate in the v5 lock protocol.
If installation fails its self-check, the installer restores the files it
touched and reports the exact `.bimri/install-backups/<timestamp>/` directory.

This repository is the canonical BIMRI project. It originally distributed the
v1 and v3 Claude Cowork Global Instructions, so many existing folders still
contain those formats. v5 preserves that installed base through a conservative
direct migration. Historical instruction files remain under [`legacy/`](legacy/)
for recovery and inspection. The installer packages them into the target as
inert rollback references; they must never be activated or pasted into Claude
Global Instructions while v5 is active.

## Quick Start

An agent starts by requesting its own run handle:

```text
<verified-python> bimri-engine.py start --actor codex
```

The engine prints a brief and a handle such as `R000042`. The agent reads
`bimri.md`, then journals durable detail as work happens:

```text
<verified-python> bimri-engine.py journal --run R000042 --importance 3 \
  --text "Checkout retries must use the existing idempotency key."
```

Anything that should affect future runs is proposed under a stable,
lowercase key:

```text
<verified-python> bimri-engine.py propose --run R000042 --tier 2 \
  --key checkout.next-step \
  --text "Verify retry behavior under concurrent requests."
```

Proposals are applied by `sync` or `close`:

```text
<verified-python> bimri-engine.py sync --run R000042
<verified-python> bimri-engine.py close --run R000042 --outcome success \
  --summary "Retry behavior verified."
```

Every agent closes only its own explicit handle. When several runs are active,
a handle-free close is refused.

The optional Claude `hook-close` adapter also closes only its mapped session.
If Claude sends `SessionEnd` without a corresponding active mapping, the hook
returns a successful no-op and never guesses another run to close.

An orphaned run is never reaped automatically. After the owner explicitly
confirms that it should close, an agent can recover it with:

```text
<verified-python> bimri-engine.py recover-run --run R000042 \
  --summary "Owner confirmed this orphaned run should close."
```

The outcome defaults to `partial`; `--outcome` accepts the normal close
outcomes.

## Memory and Trust

Hot memory has three bounded tiers:

| Tier | Contents | Default cap | Curation target |
| --- | --- | ---: | ---: |
| 1 | Durable facts, decisions, preferences, and operating rules | 20 | ~3,000 tokens |
| 2 | Active work, risks, and next actions | 40 | ~6,000 tokens |
| 3 | Evidence-backed patterns with a falsifier | 12 | ~3,000 tokens |

The generated view has an independent 49,152-byte cap, roughly 12,000 tokens
for ordinary English text. Tokenization and UTF-8 width vary, so bytes are the
enforced limit. The 3k/6k/3k tier split is an elastic curation target, not three
hard partitions: spare capacity can serve another tier while the total byte
cap and line caps still hold. Entry metadata, tags, pointers, and text all
consume the same budget.

These bounds govern only the generated hot view. The durable long tail is not
subject to a 12,000-token total-memory ceiling.

Tier 1 and Tier 2 entries carry a stable key, trust, and source:

- `confirmed` means directly stated or approved by the human, or produced by a
  deterministic system function.
- `working` means useful but still provisional.
- `contested` means a conflict is awaiting resolution.
- `user`, `agent`, `external`, `system`, and `legacy` record where the claim
  came from. Human approval may raise trust to `confirmed`; it does not rewrite
  this immutable origin.

Use `--source user --trust confirmed` only when the human directly supplied the
claim. Agent inference uses `--source agent --trust working`, including before
the owner later confirms it through resolution. Material read from outside the
project uses `--source external --trust working`.

Trust and source are transparent provenance labels, not an authentication
boundary. Any local process that can rewrite the project can also rewrite its
memory files. BIMRI detects malformed state, stale proposals, direct hot-view
edits, and changed conflict candidates; operating-system permissions remain
the security boundary.

Stable keys make structural conflicts detectable. If two agents propose
different updates to `checkout.next-step` from the same accepted keyed base,
one can commit and the later incompatible candidate becomes a concurrent
conflict. Independent keys and exact compatible effects merge without owner
involvement. A stale run is told to sync before it can create a proposal.

v5.0.3 temporarily contains the two areas that previously produced most
routine interruptions. New Tier 1 subjects and promotions into Tier 1 are
rejected before any proposal or conflict is written. Replacement or removal
of confirmed Tier 1 or Tier 2 memory is also rejected before mutation. Journal
the evidence, keep current material in working Tier 2, and ask the owner in the
current conversation when semantic judgment is needed. A separate pull-based
core-review model is planned for a minor release.

## Pull Review and Human Resolution

`start` and `hook-start` never replay open review records. Ask for actionable
concurrent choices explicitly:

```text
<verified-python> bimri-engine.py review
<verified-python> bimri-engine.py review C000003
<verified-python> bimri-engine.py review --all --offset 0 --limit 20
```

The default view contains actionable concurrent conflicts. `--all` also shows
legacy policy or validation records, recovery reviews, and historical
candidates whose exact effect is already satisfied. Each choice names the
stable subject, live value, proposed post-state, run, actor, source, trust,
base revision, rationale, and exact consequence. Internal IDs remain visible
only as the tokens needed by `resolve`.

After the owner explicitly chooses an option, the agent records it:

```text
<verified-python> bimri-engine.py resolve C000003 \
  --choose R000042-Q001 --human-approved
```

The valid choice is a listed proposal ID, `current`, or `dismiss`. A chosen
proposal that sets a Tier 1 or Tier 2 claim is recorded as human-confirmed,
while its `source` continues to record its original provenance. Both the
question and the resolution remain in `.bimri/`. `--human-approved` means the
agent asserts that the owner explicitly chose that option; it cannot prove who
ran the command.

`sync` and `close` distinguish applied changes, exact no-ops, newly created
concurrent conflicts, and agent-action failures. Reprocessing an existing
contested candidate does not repeat its ID, question, or alternatives. If a
later accepted revision contains a historical candidate's exact normalized
effect, BIMRI derives that it is satisfied without rewriting the old proposal,
decision, conflict, or resolution history.

## File Map

Repository files:

| File | Purpose |
| --- | --- |
| `INSTALL.md` | Zero-question installation contract for an agent. |
| `AGENTS.md` | Universal runtime instructions read by local coding agents. |
| `CLAUDE.md` | Claude Code adapter. |
| `BIMRI-AGENT-BLOCK.md` | Marked block merged into an existing `AGENTS.md`. |
| `BIMRI-PROTOCOL.md` | Normative v5 data and lifecycle specification. |
| `bimri-engine.py` | Dependency-free engine for locking, validation, and commits. |
| `BIMRI-MEMORY.template.md` | Initial generated memory view. |
| `BIMRI-STATE.template.json` | Initial engine-state shape. |
| `hooks-example.json` | Portable Claude hook template; installation renders a local copy. |
| `MIGRATION.md` | Earlier-version migration, verification, and rollback. |
| `CHANGELOG.md` | Architecture and release history. |
| `legacy/` | Preserved, non-executable v1 and v3 instructions. |
| `tests/` | Black-box concurrency, recovery, migration, and safety suite. |
| `.github/workflows/tests.yml` | Python 3.8/3.12 tests on Linux and Windows. |

Install also copies this repository's `LICENSE` to `BIMRI-LICENSE` in the
target. It does not replace the target project's own root `LICENSE`.

Runtime files:

| Path | Purpose |
| --- | --- |
| `bimri.md` | Small generated view of the accepted head revision. |
| `.bimri/state.json` | Revision pointer, counters, and active run registry. |
| `.bimri/log/` | One append-only Markdown journal per run. |
| `.bimri/revisions/` | Immutable snapshots of accepted shared memory. |
| `.bimri/proposals/` | Immutable structured changes submitted by agents. |
| `.bimri/decisions/` | Deterministic outcome of each proposal. |
| `.bimri/conflicts/` | Open and historical questions for the human. |
| `.bimri/resolutions/` | Durable human choices. |
| `.bimri/index.tsv` | Rebuildable, non-authoritative retrieval index. |
| `.bimri/archive/` | Closed memory with provenance. |
| `.bimri/backups/` | Migration and pre-change safety copies. |
| `.bimri/recovery/` | Direct edits, damaged authority evidence, and restore receipts. |
| `.bimri/migrations/` | Completed migration records. |
| `.bimri/runtime.local.json` | Installer-written host-only runtime binding record; never commit. |
| `.bimri/hooks.claude.local.json` | Installer-written host-only rendered Claude hook source; never commit. |

Markdown carries the human-readable memory and evidence. Small JSON and TSV
files carry transparent bookkeeping. All of it stays in the project folder.

## Operational Commands

```text
<verified-python> bimri-engine.py status
<verified-python> bimri-engine.py review
<verified-python> bimri-engine.py doctor
<verified-python> bimri-engine.py doctor --read-only
<verified-python> bimri-engine.py maintain
<verified-python> bimri-engine.py index
<verified-python> bimri-engine.py migrate
```

`doctor` validates state, revisions, memory grammar, caps, proposals,
decisions, conflicts, resolutions, pointers, active logs, and index shape.
`maintain` reports aging or closed Tier 2 entries for judgment. The engine
archives through explicit accepted changes and never silently hard-deletes
memory. A close is archived durably before its entry can disappear from the
accepted view.

The immutable revision named by `.bimri/state.json` is authoritative.
`bimri.md` is generated from it and `.bimri/index.tsv` is a derived cache. If a
generated-view refresh fails after state commits, the engine warns; the durable
change remains accepted and the next engine command retries the refresh. An
index failure cannot change a memory decision and can be repaired with
`index`.

If any process edits `bimri.md` directly, including CRLF-only changes, invalid
UTF-8, or replacing it with an empty file, the next synchronizing command
preserves the edited bytes under a content-addressed
`manual-hot-<sha256>.md` or `.bin` path in `.bimri/recovery/`, immediately
reports the event, and restores the accepted generated view. Repeating the same
edit reuses the same exact recovery bytes. An exact byte copy of a referenced
immutable revision is already preserved and is simply healed as a stale view.

## Authority Damage and Recovery

Proposals, decisions, conflicts, and resolutions form the authority graph. If
one of those JSON records is unreadable or invalid while state and the accepted
head remain healthy, BIMRI enters a degraded recovery mode. `start` still
provides a run and a brief, and `status` still prints the complete status plus
`AUTHORITY RECOVERY NEEDED`; `status` exits nonzero so automation cannot mistake
the warning for health. Shared-memory commits, conflict resolution,
maintenance, migration, and index rebuilding remain paused. Isolated run
journals and proposals can remain staged until recovery completes.

After the owner reviews the damaged record, preserve it with:

```text
<verified-python> bimri-engine.py quarantine-authority \
  --kind conflict --id C000003 --human-approved
```

`--kind` accepts `proposal`, `decision`, `conflict`, or `resolution` with the
matching record ID. Quarantine refuses a valid regular record, stores damaged
file bytes under a content-addressed path in `.bimri/recovery/`, and replaces
the original path with a validated blocking stub. An unsafe symbolic-link
record is replaced without following its target; the preserved evidence records
the exact link target and its filesystem bytes, while the external target is
left untouched. If a durably referenced authority record has been deleted,
quarantine preserves canonical absence evidence before installing the blocker;
an unknown ID with no log, dependency, or counter reference is refused as a
likely typo. Quarantine never makes the remaining graph healthy by omission.

Repair and review a separate copy, then restore it explicitly:

```text
<verified-python> bimri-engine.py restore-authority \
  --kind conflict --id C000003 --from /path/to/repaired.json \
  --human-approved
```

The engine validates the replacement's identity, structure, immutable effects,
and relationships in an isolated shadow before it writes an authorization
receipt or changes the blocker. Missing or altered evidence makes `doctor`
fail even after canonical memory is healthy. When several related records are
quarantined, valid restores may be staged one at a time; only recognized
quarantine dependencies may remain unresolved. Shared-memory writes stay
blocked until the complete authority graph validates. Run `doctor` after the
final restore and resume only after it passes.
For both recovery commands, `--human-approved` is an attestation of the owner's
choice, not authentication of the caller.

## Concurrency and Portability Boundary

The v5 concurrency guarantee covers processes that access the same folder
through one shared operating-system/filesystem lock domain. Every writer must
observe the same lock on `.bimri/engine.lock` and the same atomic-rename
semantics, and every shared write must go through the engine.

Move, copy, back up, or synchronize the folder only while BIMRI is quiescent:
no active run is writing and no engine command is in progress. v5 does not
guarantee simultaneous operation over NFS, Dropbox-style synchronization, two
machines, or independently copied folders.

An agent harness running inside a VM or sandbox against a mounted host folder
is also outside the guarantee unless that exact mount has been verified to
share both lock and atomic-rename semantics with host processes. Containers do
not all behave alike, so the boundary must be verified rather than inferred
from the word “container.” The safe default is one runtime boundary active at
a time, with a quiescent handoff before another boundary uses the folder.

## Privacy

Memory may contain private project context. Decide deliberately whether it
belongs in version control. For local-only memory:

```gitignore
bimri.md
.bimri/
```

If the project deliberately versions other `.bimri/` content, ignore the two
host-bound adapter records explicitly:

```gitignore
.bimri/runtime.local.json
.bimri/hooks.claude.local.json
```

## Author

**Stu Jordan**, Agent Architect

- Community: [Evolution Unleashed](https://evolutionunleashed.com)
- Patreon: [Evolution Unleashed VIP](https://www.patreon.com/evolutionunleashedvip)
- Web: [evolutionunleashed.com](https://evolutionunleashed.com)

## License

MIT. See [`LICENSE`](LICENSE).
