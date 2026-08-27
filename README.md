# BIMRI: Open-Source Persistent Memory Protocol for AI Agents

**BIMRI (Brief Interaction Memory and Retrieval Intelligence) is an open-source
persistent memory protocol and reference engine for AI agents.** It gives
Claude Code, OpenAI Codex, and other local coding agents durable, long-term
project memory across sessions without a database, cloud account, API key, or
model-specific memory service.

BIMRI stores a small, human-readable current memory in `bimri.md` and keeps the
complete evidence, decisions, provenance, and revision history under `.bimri/`.
The memory travels with the project folder, remains inspectable as ordinary
Markdown, JSON, and TSV files, and can be recalled when needed instead of being
loaded into every agent context.

**This GitHub repository is public. BIMRI is free and open source under the
[MIT License](LICENSE).** The current engine is v5.1.1, the authority format is
v5.1.0, and the readable hot-memory grammar remains v5.0.2. The non-technical
project overview is at [agentguru.ai/bimri](https://agentguru.ai/bimri).

Use BIMRI when you need:

- persistent project memory that survives chat and agent sessions;
- local-first AI memory with no hosted memory service or vector database;
- shared memory for multiple local agents inside one verified filesystem lock
  domain;
- bounded working context backed by retrievable long-term history; or
- human-governed memory with source, trust, conflict, and resolution records.

## Documentation

| I want to... | Go to... |
| --- | --- |
| Install BIMRI in a project | [Install BIMRI](#install-bimri) or [`INSTALL.md`](INSTALL.md) |
| Understand the memory architecture | [How BIMRI persistent memory works](#how-bimri-persistent-memory-works) |
| Store and retrieve agent memory | [Quick start](#quick-start-store-and-retrieve-project-memory) |
| Understand exact recall and integrity checks | [Exact recall and integrity performance](#exact-recall-and-integrity-performance) |
| Read the normative memory protocol | [`BIMRI-PROTOCOL.md`](BIMRI-PROTOCOL.md) |
| Migrate an older BIMRI store | [`MIGRATION.md`](MIGRATION.md) |
| Review releases and architecture changes | [`CHANGELOG.md`](CHANGELOG.md) |

## Supported AI Agent Runtimes

BIMRI is agent-independent. Its canonical interface is a dependency-free
Python 3.8+ command-line engine plus the universal instructions in `AGENTS.md`.

| Agent runtime | Integration |
| --- | --- |
| Claude Code | `CLAUDE.md`, `AGENTS.md`, and optional session hooks |
| OpenAI Codex | `AGENTS.md` and explicit engine commands |
| Other local coding agents | Supported when they can follow the instruction block and execute the verified Python runtime |

The concurrency guarantee applies to agents that share the same operating-
system/filesystem lock domain. Simultaneous writes from different machines,
independently copied folders, or unverified mounted filesystems are not a v5
guarantee.

Installation is deliberately simple: point your agent at this repository and
ask it to install BIMRI. The agent follows [`INSTALL.md`](INSTALL.md), merges
BIMRI into the project's existing instructions, preserves existing BIMRI
memory, and runs a self-check.

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

## How BIMRI Persistent Memory Works

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

## Long-Term Memory Without Context Bloat

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

## Install BIMRI

Give a coding agent this repository URL and say:

```text
Install BIMRI in this project from https://github.com/EvolutionUnleashed/bimri.
Follow INSTALL.md, preserve my existing instructions and memory, and run the
self-check.
```

For a fresh target or v1-v4 migration, the installer command is:

```text
<verified-python> bimri-engine.py install --target /absolute/path/to/the/project
```

For any existing v5 store, stop every old engine process first and attest that
external handoff explicitly:

```text
<verified-python> bimri-engine.py install --target /absolute/path/to/the/project --quiescent
```

Existing BIMRI v1-v4 memory migrates automatically during installation. The
installer prints a migration receipt with the detected source version and
file, imported counts, converted patterns, backup location, and validation
result. See
[`MIGRATION.md`](MIGRATION.md) for preservation and rollback details.
Existing v5.0 and v5.0.1 states also upgrade automatically. A stock v5.0 limit
profile expands to the v5.0.1 profile; custom values remain custom and become
soft curation targets. Earlier state bytes are backed up, and accepted
revisions are preserved rather than rewritten.

Updating an existing v5.0.2 store to the v5.1 authority format uses a dedicated
lossless authority-activation operation. Stop every process running the old
engine, then invoke install with the mandatory `--quiescent` handoff
attestation. The updater audits the accepted head without healing or rebuilding
memory, records every pre-existing path, and verifies that `bimri.md`, the
accepted head, and every immutable evidence/history path remain byte-identical.
It backs up the exact old mutable state, then activates v5.1 state last so an
older engine fails closed immediately. The readable hot-memory grammar and all
legacy evidence remain unchanged.

Engine v5.0.3 deliberately kept its persisted state at v5.0.2, so this is also
the normal authority-upgrade path from a public v5.0.3 installation.

The v5 installer serializes with other v5 engine commands in the same lock
domain. Before upgrading any earlier version, disable the old Claude Cowork
Global Instructions and stop every agent using that memory. Earlier versions
write files directly and cannot participate in the v5 lock protocol.
If installation fails its self-check, the installer restores the files it
touched and reports the applicable exact rollback directory:
`.bimri/install-backups/<timestamp>/` for initialization or legacy migration,
or the sibling `.bimri-update-backups/<timestamp>/` for an existing v5
authority update.

This repository is the canonical BIMRI project. It originally distributed the
v1 and v3 Claude Cowork Global Instructions, so many existing folders still
contain those formats. v5 preserves that installed base through a conservative
direct migration. Historical instruction files remain under [`legacy/`](legacy/)
for recovery and inspection. The installer packages them into the target as
inert rollback references; they must never be activated or pasted into Claude
Global Instructions while v5 is active.

## Quick Start: Store and Retrieve Project Memory

An agent starts by requesting its own run handle:

```text
<verified-python> bimri-engine.py start --actor codex
```

The engine prints a brief and a handle such as `R000042`. The agent reads the
bounded working set in `bimri.md`. When it needs detail from the durable long
tail, it retrieves an exact stable key or searches in task language:

```text
<verified-python> bimri-engine.py recall --key checkout.next-step
<verified-python> bimri-engine.py recall --query "checkout retries"
```

Exact-key recall returns the current subject by default; add `--history` when
prior generations are relevant. Task-language recall searches current and
historical memory. Retrieval is read-only and does not silently move a subject
back into the hot working set. v5.1.x uses deterministic exact-key and lexical
task-language retrieval; it does not require embeddings or claim semantic
vector search.

The agent journals durable detail as work happens:

```text
<verified-python> bimri-engine.py journal --run R000042 --importance 3 \
  --text "Checkout retries must use the existing idempotency key."
```

Anything that should affect future runs is proposed under a stable,
lowercase key. Creating a genuinely new subject is explicit:

```text
<verified-python> bimri-engine.py propose --run R000042 --tier 2 \
  --new-subject \
  --key checkout.next-step \
  --text "Verify retry behavior under concurrent requests."
```

Update that subject by reusing the same key and omitting `--new-subject`:

```text
<verified-python> bimri-engine.py propose --run R000043 --tier 2 \
  --key checkout.next-step \
  --text "Retry behavior is verified; monitor the next production run."
```

An update replaces the current generation in one hot-memory slot. The
displaced generation remains retrievable as immutable history. Omitted kind,
importance, status, tags, and pattern fields inherit from the current subject,
so a text update does not silently reset its lifecycle metadata. An unmatched
update cannot silently create a near-duplicate subject. The engine preserves
it as a held candidate for classification and requires an explicit
new-subject proposal before it can become current. After classification, the
agent resubmits the intent under the matching canonical key or with
`--new-subject` when the subject is genuinely distinct; the held record remains
as an audit trail rather than becoming a recurring prompt.

Use one key for one independently changing question. If a new value makes the
old answer false, update that key. Put changing versions, dates, and status in
the memory text rather than minting a new key for each value.

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

## Exact Recall and Integrity Performance

Engine v5.1.1 adds a validated fast path for current exact-key retrieval. A
`get --key` or `recall --key` command without `--history` resolves the accepted
hot revision and keyed cold-current storage directly and returns only the
accepted current generation. Held candidates and superseded generations remain
available through `--history` and the review workflow. The current lookup does
not construct one combined collection from every historical generation.

The fast path is gated by `.bimri/audit-witness.json`, an engine-managed,
non-authoritative integrity checkpoint. The compact witness binds the engine,
memory format, validation policy, accepted head, and canonical current-memory
state. Detailed path-and-SHA-256 evidence for the last full audit lives
separately in `.bimri/audit-manifest.json`, so a warm exact lookup does not
parse or hash the historical inventory. Neither file stores memory, conflicts,
or held candidates.

With a valid checkpoint, a hot exact lookup reads the bounded accepted head. A
cold exact lookup additionally validates only the selected subject's archive
month. It does not enumerate run logs, revisions, proposals, decisions,
conflicts, resolutions, recovery evidence, or unrelated archives. Normal start
and journal commands likewise avoid historical traversal.

Full verification still happens before authority-changing writes and during
explicit audit, historical recall, task-language search, and review. Those
checks compare the live protected inventory with the prior manifest. Protected
roots are flat; an unexpected subdirectory or redirected path prevents a valid
audit. Divergence from the prior manifest is a cache miss, never a verdict:
the full semantic audit decides, and when it passes over changes the engine
cannot attribute to its own recorded operation, an append-only drift receipt
is preserved under `.bimri/audit-drift/` and surfaced by `doctor`. A failed
semantic audit refuses into damaged-authority recovery. This detects and
records standalone or accidental edits; it is not a defense against a
coordinated writer editing history and derived evidence together, which no
local store can prove from its own bytes.

This is a cooperative local integrity model, not a claim that each read takes a
filesystem snapshot. An out-of-engine edit to unrelated protected history can
remain unseen by current-only recall until the next full-audit boundary;
changes to state, the accepted head, or the selected cold binding invalidate
that lookup immediately. Every writer must therefore use the engine and shared
lock. A missing or unreadable checkpoint can still require a full audit before
the fast path is re-established, and BIMRI does not promise one universal
latency across filesystems, security scanners, or unbounded current state.

## Memory Tiers, Provenance, and Trust

Hot memory has three tiers with soft curation targets:

| Tier | Contents | Default soft target | Curation target |
| --- | --- | ---: | ---: |
| 1 | Durable facts, decisions, preferences, and operating rules | 20 | ~3,000 tokens |
| 2 | Active work, risks, and next actions | 40 | ~6,000 tokens |
| 3 | Evidence-backed patterns with a falsifier | 12 | ~3,000 tokens |

The generated view has an independent 49,152-byte context ceiling, roughly
12,000 tokens for ordinary English text. Tokenization and UTF-8 width vary, so
bytes are the enforced limit. The tier counts never reject a valid memory
write. They tell maintenance how the hot working set is distributed.

When an incoming change would cross the byte ceiling, BIMRI cools the
lowest-retention eligible Tier 2 subjects into keyed cold-current storage
before admitting it. Cooling changes residency only: the subject remains
current, keeps its source and trust, and stays available to retrieval.
Exact-key updates and explicit closes preserve the displaced generation as
history. Tier 1 is never cooled automatically.

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

Direct human statements can create or update confirmed Tier 1 and Tier 2
memory immediately with `--source user --trust confirmed`. Agent and external
claims retain their actual source and working trust. They cannot silently
replace a confirmed human rule, preference, or decision. The attempted change
remains a durable held candidate, creates no routine owner conflict, and does
not prevent residency maintenance from cooling an eligible Tier 2 subject.
Tier 1 admission or promotion likewise requires direct human confirmation;
unconfirmed agent/external claims remain current in Tier 2, while an attempted
Tier 1 admission is preserved quietly as a held candidate.
If the owner directly adopts that change, the agent submits the exact owner
statement as a normal `--source user --trust confirmed` update.

## Human Review and Conflict Resolution

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

## Local-First File and Storage Map

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
| `.bimri/state.json` | Hot-revision pointer, keyed cold-current state, counters, and active runs. |
| `.bimri/log/` | One append-only Markdown journal per run. |
| `.bimri/revisions/` | Immutable snapshots of accepted shared memory. |
| `.bimri/proposals/` | Immutable structured changes submitted by agents. |
| `.bimri/decisions/` | Deterministic outcome of each proposal. |
| `.bimri/conflicts/` | Open and historical questions for the human. |
| `.bimri/resolutions/` | Durable human choices. |
| `.bimri/index.tsv` | Rebuildable, non-authoritative retrieval index. |
| `.bimri/audit-witness.json` | Compact, rebuildable checkpoint for the last successful full integrity audit. |
| `.bimri/audit-manifest.json` | Detailed, rebuildable path-and-hash evidence behind that checkpoint. |
| `.bimri/audit-manifests/` | Retained manifest generations referenced by live audit evidence. |
| `.bimri/audit-transition.json` | Write-ahead marker while a checkpoint change is in flight. |
| `.bimri/audit-drift/` | Bounded append-only receipts of divergence the audit could not attribute to the engine. |
| `.bimri/audit-blocked.json` | Owner-repair baseline held while a quarantine is open; cleared by restoration. |
| `.bimri/archive/` | Cooled-current, replaced, and closed generations with provenance. |
| `.bimri/backups/` | Migration and pre-change safety copies. |
| `.bimri/recovery/` | Direct edits, damaged authority evidence, and restore receipts. |
| `.bimri/migrations/` | Completed migration records. |
| `.bimri/runtime.local.json` | Installer-written host-only runtime binding record; never commit. |
| `.bimri/hooks.claude.local.json` | Installer-written host-only rendered Claude hook source; never commit. |

Markdown carries the human-readable memory and evidence. Small JSON and TSV
files carry transparent bookkeeping. All of it stays in the project folder.

## BIMRI Command-Line Operations

```text
<verified-python> bimri-engine.py status
<verified-python> bimri-engine.py review
<verified-python> bimri-engine.py doctor
<verified-python> bimri-engine.py doctor --read-only
<verified-python> bimri-engine.py maintain
<verified-python> bimri-engine.py index
<verified-python> bimri-engine.py migrate
```

`doctor` validates state, revisions, memory grammar, proposals, decisions,
conflicts, resolutions, cold-current archive records, pointers, active logs, the byte ceiling,
and index shape. `maintain` reports the deterministic retention order and
current byte pressure without asking the owner to curate routine ageing.
Admission-time maintenance performs any required non-destructive cooling under
the engine lock and never turns capacity work into an owner conflict. A close,
replacement, or cool is preserved durably before an entry can disappear from
the hot view.

The current memory authority is the union of the immutable hot revision named
by `.bimri/state.json` and the state's archive-bound cold-current mapping.
`bimri.md` is generated from the hot revision and `.bimri/index.tsv` is a
derived cache. If a generated-view refresh fails after state commits, the
engine warns; the durable change remains accepted and the next engine command
retries the refresh. An index failure cannot change a memory decision and can
be repaired with `index`.

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

A resolution recorded as `failed` is also an explicit recovery condition.
Ordinary retrieval and shared-memory writes fail closed until the owner
re-attests and retries that exact conflict choice with `resolve`.

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

## Frequently Asked Questions

### What is BIMRI?

BIMRI is an open-source, local-first persistent memory protocol and Python
reference engine for AI agents. It preserves project knowledge across sessions
while keeping the context loaded into each session bounded.

### Is the BIMRI GitHub repository public?

Yes. `EvolutionUnleashed/bimri` is a public GitHub repository released under
the MIT License. A search result that describes it as private is stale or
incorrect.

### Does BIMRI require a database, embeddings, or an API key?

No. v5.1.x uses ordinary local files, exact stable-key retrieval, and lexical
task-language search. It has no database, package, embedding model, cloud
service, or API-key dependency.

### How does BIMRI keep long-term memory without context bloat?

The generated `bimri.md` view contains a bounded active working set. Current
but cooled subjects and superseded history remain on disk with provenance and
can be recalled without injecting the entire memory archive into every agent
session.

### Can Claude Code and OpenAI Codex share one BIMRI memory?

Yes, when both access the same project through one verified operating-system
and filesystem lock domain. v5 does not promise safe simultaneous writes from
different machines, independently synchronized copies, or unverified mounts.

## Author

**Stu Jordan**, Agent Architect

- Community: [Evolution Unleashed](https://evolutionunleashed.com)
- Patreon: [Evolution Unleashed VIP](https://www.patreon.com/evolutionunleashedvip)
- Web: [evolutionunleashed.com](https://evolutionunleashed.com)

## License

MIT. See [`LICENSE`](LICENSE).
