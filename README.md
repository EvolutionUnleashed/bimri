# BIMRI: Portable Memory for Local Agents

**Brief Interaction Memory and Retrieval Intelligence, v5**

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
files. There are no packages to install. Commands below use `python3`; use
whichever executable provides Python 3.8 or newer (`python` on a standard
Windows installation, and commonly `python3` elsewhere). Multiline examples
use POSIX continuation syntax; on Windows, run them on one line or adapt them
to the active shell and path format.

## What v5 Is

`bimri.md` is a generated view of the latest accepted memory revision. Agents
do not compete to rewrite it. Each agent instead receives its own run handle,
writes to its own append-only log, and submits proposals against stable memory
keys. The engine serializes the short shared commit, checks the proposal's base
hash, and either creates a new immutable revision or raises a conflict.

The human remains the authority. BIMRI puts unresolved questions into the next
agent brief. The agent asks the owner in normal conversation and records the
answer with `resolve`; the owner never has to edit a memory file or run a
command. Direct human statements can enter memory as confirmed. Agent
inferences and external material remain working claims until the human confirms
them.

This design gives BIMRI four useful properties:

- **Portable:** the project folder contains the memory, evidence, and history.
- **Agent-independent:** any local agent that can run Python and follow
  `AGENTS.md` can use the same memory.
- **Concurrency-safe in one shared lock domain:** independent run handles, an
  operating-system file lock, atomic replacement, and key-specific base hashes
  prevent one agent from silently overwriting another.
- **Human-governed:** deterministic conflicts and agent-declared uncertainty
  become conversational decisions with a durable resolution record.

## Active Memory, Not a Diary

Flat-file memory tends to become a transcript of completed work. Every session
adds another summary, old context stays visible, and useful signal is buried in
a context swamp. A larger file then makes retrieval and agent judgment worse.

BIMRI keeps the readable view deliberately small. Tier 1 holds durable facts,
preferences, decisions, and rules. Tier 2 holds current work, risks, and next
actions. Tier 3 holds patterns only when evidence and a falsifier exist. Full
detail belongs in run logs, revisions, decisions, and archives, where an agent
can retrieve it when needed without forcing it into every future context.

The result is active memory: the smallest current state that should change the
next useful action, backed by durable evidence and history.

## Install

Give a coding agent this repository URL and say:

```text
Install BIMRI in this project from https://github.com/EvolutionUnleashed/bimri.
Follow INSTALL.md, preserve my existing instructions and memory, and run the
self-check.
```

The installer command the agent runs is:

```text
python3 bimri-engine.py install --target /absolute/path/to/the/project
```

Existing BIMRI v1-v4 memory migrates automatically. See
[`MIGRATION.md`](MIGRATION.md) for preservation and rollback details.
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
python3 bimri-engine.py start --actor codex
```

The engine prints a brief and a handle such as `R000042`. The agent reads
`bimri.md`, then journals durable detail as work happens:

```text
python3 bimri-engine.py journal --run R000042 --importance 3 \
  --text "Checkout retries must use the existing idempotency key."
```

Anything that should affect future runs is proposed under a stable,
lowercase key:

```text
python3 bimri-engine.py propose --run R000042 --tier 2 \
  --key checkout.next-step \
  --text "Verify retry behavior under concurrent requests."
```

Proposals are applied by `sync` or `close`:

```text
python3 bimri-engine.py sync --run R000042
python3 bimri-engine.py close --run R000042 --outcome success \
  --summary "Retry behavior verified."
```

Every agent closes only its own explicit handle. When several runs are active,
a handle-free close is refused.

An orphaned run is never reaped automatically. After the owner explicitly
confirms that it should close, an agent can recover it with:

```text
python3 bimri-engine.py recover-run --run R000042 \
  --summary "Owner confirmed this orphaned run should close."
```

The outcome defaults to `partial`; `--outcome` accepts the normal close
outcomes.

## Memory and Trust

Hot memory has three bounded tiers:

| Tier | Contents | Default cap |
| --- | --- | ---: |
| 1 | Durable facts, decisions, preferences, and operating rules | 12 |
| 2 | Active work, risks, and next actions | 20 |
| 3 | Evidence-backed patterns with a falsifier | 8 |

The generated view also has an independent 16,384-byte cap. It is the primary
size bound and may be reached before any tier reaches its line cap, because
entry metadata, tags, pointers, and text all consume bytes.

Tier 1 and Tier 2 entries carry a stable key, trust, and source:

- `confirmed` means directly stated or approved by the human, or produced by a
  deterministic system function.
- `working` means useful but still provisional.
- `contested` means a conflict is awaiting resolution.
- `user`, `agent`, `external`, `system`, and `legacy` record where the claim
  came from.

Use `--source user --trust confirmed` only when the human directly supplied or
approved the claim. Agent inference uses `--source agent --trust working`.
Material read from outside the project uses
`--source external --trust working`.

Trust and source are transparent provenance labels, not an authentication
boundary. Any local process that can rewrite the project can also rewrite its
memory files. BIMRI detects malformed state, stale proposals, direct hot-view
edits, and changed conflict candidates; operating-system permissions remain
the security boundary.

Stable keys make structural conflicts detectable. If two agents propose
different updates to `checkout.next-step` from the same base, one can commit
and the stale proposal becomes a conflict. Independent keys can merge.
Meaning-level contradictions across different keys require judgment, so agents
raise them with `--needs-human --question "..."`.

## Human Resolution

Open conflicts appear under `HUMAN DECISION NEEDED` in the BIMRI brief. An
agent explains the alternatives and asks the owner. It then records the
owner's choice:

```text
python3 bimri-engine.py resolve C000003 --choose R000042-Q001
```

The valid choice is a listed proposal ID, `current`, or `dismiss`. A chosen
proposal is recorded as human-confirmed where the tier supports trust. Both the
question and the resolution remain in `.bimri/`.

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
| `hooks-example.json` | Optional Claude Code start and close hooks. |
| `MIGRATION.md` | v1-v4 migration, verification, and rollback. |
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
| `.bimri/recovery/` | Preserved direct edits or recoverable material. |
| `.bimri/migrations/` | Completed migration records. |

Markdown carries the human-readable memory and evidence. Small JSON and TSV
files carry transparent bookkeeping. All of it stays in the project folder.

## Operational Commands

```text
python3 bimri-engine.py status
python3 bimri-engine.py doctor
python3 bimri-engine.py maintain
python3 bimri-engine.py index
python3 bimri-engine.py migrate
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
UTF-8, or replacing it with an empty file, the next engine command preserves
the edited bytes in `.bimri/recovery/`, immediately reports the event, and
restores the accepted generated view. An exact byte copy of a referenced
immutable revision is already preserved and is simply healed as a stale view.

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

## Author

**Stu Jordan**, Agent Orchestrator

- Community: [Evolution Unleashed](https://evolutionunleashed.com)
- Patreon: [Evolution Unleashed VIP](https://www.patreon.com/evolutionunleashedvip)
- Web: [evolutionunleashed.com](https://evolutionunleashed.com)

## License

MIT. See [`LICENSE`](LICENSE).
