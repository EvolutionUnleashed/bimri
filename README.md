# BIMRI: Persistent Memory for AI Agents, in Files You Own

[![Tests](https://github.com/EvolutionUnleashed/bimri/actions/workflows/tests.yml/badge.svg?branch=main)](https://github.com/EvolutionUnleashed/bimri/actions/workflows/tests.yml)
![Python 3.8 or newer](https://img.shields.io/badge/python-3.8%2B-blue)
![Dependencies: none](https://img.shields.io/badge/dependencies-none-lightgrey)
![License: MIT](https://img.shields.io/badge/license-MIT-green)

**BIMRI gives local AI agents persistent project memory across sessions.**
Installed project instructions tell each agent to read the current accepted
memory at the start of a session, so your decisions, preferences and the state
of the work survive the handoff. The memory lives in your project folder as
plain files you can open and read. It works with Claude Code, OpenAI Codex and
any local agent that can run a Python script. Free and open source under the
MIT license, with no database, no BIMRI account and no API key.

Setup is one sentence to your agent:

```text
Install BIMRI in this project from https://github.com/EvolutionUnleashed/bimri.
Follow INSTALL.md, preserve my existing instructions and memory, and run the
self-check.
```

**Who this is for.** Business owners, consultants and agencies who run AI
agents on real work and are tired of re-explaining the business every morning.
And developers who want durable, inspectable, multi-agent memory without adding
another service to run. The plain-language overview lives at
[agentguru.ai/bimri](https://agentguru.ai/bimri); everything technical is
below.

## AI Agent Memory Should Outlast the Runtime

Claude Code can keep editable, machine-local project memory. OpenAI Codex can
generate local memory files you can inspect. Both give one runtime useful
continuity under that runtime's schema, loading rules and lifecycle.

The gap appears when several supported agents need one accepted memory they can
all use and take with the project. A shared notes file travels, but by itself it
does not label sources, preserve accepted history, govern conflicting writes or
recover a half-finished change. BIMRI does.

## Your Agent's Memory Lives on Your Machine

BIMRI keeps accepted memory in a folder on your machine and uses an engine to
protect its history. Accepted facts, decisions, preferences, rules and current
work record their source and trust level. Learned patterns record their
evidence, confidence and what would prove them wrong. When two agents
concurrently propose incompatible changes to the same stable key from the same
accepted base, BIMRI records one conflict and leaves the choice to you.

When BIMRI is idle, copy the project folder and rerun the installer on the new
machine. Its accepted memory, evidence and history move with it. The model and
runtime can change; what BIMRI has preserved about your business stays with the
project.

| What you get | What it means day to day |
| --- | --- |
| **Portable** | When BIMRI is idle, move the project to another machine or supported agent and its accepted memory, evidence and history go with it. |
| **Readable by you** | Plain Markdown you can open in any editor. Read exactly what BIMRI currently carries, then tell the agent to correct it through the engine if it is wrong. |
| **Safe with many agents** | On one machine and one filesystem lock domain, several agents can work at once without silently overwriting one another. |
| **Governed by you** | An incompatible concurrent change to the same keyed subject stops for your decision, and the choice is recorded. |
| **Provenance built in** | Accepted facts and current work record source and trust. Learned patterns record evidence, confidence and what would prove them wrong. |
| **Long-term memory that stays small in context** | The agent loads a bounded working set of roughly 12,000 tokens. Everything else stays on disk and can be recalled when needed. |
| **No hosted dependency** | MIT licensed, with no BIMRI account or subscription. The memory stays in files you control. |

## What One Memory Looks Like

```text
[R000012-E004] [K:offer.spring-promotion] [fact] [T:confirmed] [SRC:user] [offer,pricing] The spring promotion runs to 30 April at 20% off the annual plan. -> .bimri/log/R000012.md
```

- `[K:offer.spring-promotion]` is a stable key, so a newer memory on the same
  subject replaces this one instead of piling up beside it.
- `[SRC:user]` says the owner directly supplied the claim. Agent inference uses
  `[SRC:agent]`; outside material uses `[SRC:external]`.
- `[T:confirmed]` records how much to trust it, so agents weigh what they read
  instead of swallowing it whole.
- `-> .bimri/log/R000012.md` points at the journal where the full detail and
  the reasoning live.

The file this line sits in, `bimri.md`, is generated from an immutable revision
and is never edited by hand. Under `.bimri/` sit the run journals, every
accepted revision, every immutable proposal, its decision record once decided,
every recorded BIMRI conflict and every recorded owner resolution. Markdown
holds the memory; small JSON and TSV files hold the bookkeeping, all of it in
the project folder.

## Works With Claude Code, OpenAI Codex and Local Agents

| Agent runtime | Integration |
| --- | --- |
| Claude Code | `CLAUDE.md`, `AGENTS.md`, and optional session hooks that open and close a run automatically |
| OpenAI Codex | `AGENTS.md` and explicit engine commands |
| Other local coding agents | Supported when they can follow the instruction block and execute the verified Python runtime |

The runtime is one Python 3.8 or newer standard-library script plus ordinary
local files. There are no packages to install. The BIMRI engine has no network
client, server, account or API key; it reads and writes local project files
only. Your agent runtime may still send prompts, relevant file excerpts or tool
results to its model provider under that runtime's own data controls. Several
agents can share one memory folder as long as they share one operating system
and filesystem lock domain; see the concurrency boundary in the reference
section for the exact rule.

## How It Works, in One Minute

Each agent asks the engine for its own run handle, receives a short brief, and
then reads the accepted current memory from `bimri.md`. It journals detail as it
works. Anything that should shape future sessions is proposed under a stable,
lowercase key such as `checkout.next-step`. A sync commits accepted proposals
as a new immutable revision and regenerates `bimri.md`. Only concurrent
incompatible changes to the same key from the same accepted base become human
conflicts. Independent-key changes can commit without a human decision, while
exact compatible same-key effects become no-ops. Other refusals stay with the
agent instead of becoming owner conflicts.

The readable view stays small on purpose. Tier 1 holds durable facts,
decisions, preferences and rules. Tier 2 holds current work, risks and next
actions. Tier 3 holds patterns only when evidence and a falsifier exist. When
an incoming change would make the view too large, the engine moves eligible
Tier 2 subjects into keyed cold storage and retries the change. Those subjects
stay current and recallable. Earlier generations remain in immutable history
rather than being deleted.

```text
<verified-python> bimri-engine.py start --actor codex
<verified-python> bimri-engine.py journal --run R000042 --importance 3 --text "Checkout retries must use the existing idempotency key."
<verified-python> bimri-engine.py propose --run R000042 --tier 2 --new-subject --key checkout.next-step --text "Verify retry behavior under concurrent requests."
<verified-python> bimri-engine.py sync --run R000042
<verified-python> bimri-engine.py recall --key checkout.next-step
<verified-python> bimri-engine.py close --run R000042 --outcome success --summary "Retry behavior verified."
```

`<verified-python>` stands for the absolute Python executable the installer
verified on your machine; the installer records it in
`.bimri/runtime.local.json`, and agents read it from there rather than guessing
a PATH name. The [quick start](#quick-start-store-and-retrieve-project-memory)
below walks through every command.

## Fast Enough to Run on Every Session

Measured on the development store used by agents running Stu Jordan's business
(about 500 runs and 216 revisions), on Windows 11 on 2026-09-02:

| Operation | Time |
| --- | ---: |
| Exact recall of one memory, warm, end to end | 0.33 s |
| Session start, warm | 0.41 s |
| Journal one line | 0.39 s |
| Propose or sync an authority change | 1.3 to 1.6 s |
| Cold full audit to seed or rebuild the checkpoint | about 33 s per audit |

Warm exact-current reads, starts and journals use the checkpoint while it
remains valid. `doctor`, review, search and historical recall deliberately run
the full audit, and a missing or invalid checkpoint is rebuilt. Before an
authority-changing write, the engine rechecks the complete protected record
and runs a semantic audit if anything changed. 255 tests cover concurrency,
crash recovery, migration, integrity and safety, and run in public CI on Linux
and Windows under Python 3.8 and 3.12 on every change.

## BIMRI and Compounding Intelligence

BIMRI is the memory layer inside the compounding intelligence engine Stu Jordan
uses to run agents in his own business. Continuity, feedback learning and a
results ledger handle the other parts. BIMRI makes improvement durable by
preserving the accepted decisions, corrections and business context those
systems need, and it is the part released as open source.

What the business teaches the agent can survive the session, the model and the
runtime.

If you want the finished agent, [Agent in a Box](https://agentguru.ai/agent-in-a-box)
is one agent for one named job, built on this architecture. [The Starter
Engine](https://agentguru.ai/start) is a complete scheduled agent you can run
free.

## Frequently Asked Questions

### What is BIMRI?

BIMRI is a free, open-source persistent memory system for Claude Code, OpenAI
Codex and other local agents. It stores accepted project knowledge in
human-readable files, preserves its provenance and verified history, and lets
supported agents share one governed memory across sessions.

### What is AI agent memory?

AI agent memory is the project knowledge an agent carries from one session to
the next: decisions, preferences and the current state of the work. Each
runtime has its own way of carrying or rebuilding that picture. BIMRI keeps one
accepted record in the project, bounds what loads into each session and lets
the agent recall the rest when needed.

### Does Claude Code remember between sessions?

Yes. Claude Code reads your `CLAUDE.md` instructions and can keep editable,
machine-local [project memory](https://code.claude.com/docs/en/memory) between
sessions. That gives one runtime useful continuity. By default, its automatic
notes are Claude Code-specific and stored per repository on one machine, and
they do not require provenance on each fact. BIMRI adds a governed project
memory that supported agents in the folder can share, with stable keys,
provenance, immutable history and bounded loading. When the rendered hooks are
enabled, Claude Code opens and closes a BIMRI run automatically.

### Does OpenAI Codex remember between sessions?

Yes, when local memories are enabled. Codex can generate local memory files
from eligible prior chats, store them under the Codex home directory
(`$CODEX_HOME/memories/`, normally `~/.codex/memories/`), and use them in later
sessions. OpenAI documents these as
[inspectable generated state](https://learn.chatgpt.com/docs/customization/memories);
the feature is off by default and updates in the background rather than after
every chat. BIMRI can run alongside it for project-owned memory that supported
agents can share, with stable keys, provenance, immutable history and bounded
loading. Keep rules that must always apply in `AGENTS.md`.

### What does BIMRI stand for?

Brief Interaction Memory and Retrieval Intelligence: brief because the working
memory an agent loads stays small, retrieval because the long tail is recalled
on demand rather than stuffed into every session.

### Is BIMRI free?

Yes. BIMRI is MIT licensed and free for personal and commercial use. The source
and full documentation are in this public GitHub repository.

### Does BIMRI need a database, embeddings or an API key?

No. It uses ordinary local files, exact stable-key retrieval and lexical
task-language search. There is no database, no package to install, no embedding
model, no cloud service and no API key.

### Does BIMRI send my memory anywhere?

The BIMRI engine has no network client, server, account or API key; it reads
and writes local project files only. Your agent runtime may send prompts,
relevant file excerpts or tool results to its model provider under that
runtime's own data controls. BIMRI does not change that boundary.

### Which agents does it work with?

Claude Code and OpenAI Codex today, and any local agent that can run a Python
script and follow written instructions. Claude Code can open and close a run
automatically through session hooks.

### How is this different from the memory built into my AI app?

Built-in memory is useful, and runtimes differ. Local Codex can generate
inspectable files, while Claude Code can keep editable project memory. BIMRI
adds a project-owned layer that supported local agents can share: stable keys,
source and trust labels on Tier 1 and Tier 2 claims, immutable revisions,
recorded conflict decisions and a defined one-lock-domain concurrency
boundary.

### Can several agents share one memory?

Yes, when every agent shares one machine and operating-system/filesystem lock
domain. Each agent has its own log. Independent changes can commit without a
human decision; concurrent incompatible changes to the same key from the same
accepted base stop for yours. Simultaneous writes from different machines or
synchronized copies are outside the guarantee.

### What happens if an agent crashes mid-write?

BIMRI records the intended memory change before replacing accepted state. After
an interruption, recovery either proves the change committed, keeps the prior
accepted memory authoritative, or reports the exact next action. That may be
the original run finishing its write or the owner re-approving the same conflict
choice. Accepted memory is never left half-written, and recovery does not
require deleting files by hand.

### Does it run on Windows?

Yes. The suite runs on Linux and Windows in public CI, and the development
store that produced the numbers above lives on Windows 11.

---

## Reference

Everything below is the documentation of record for people installing,
operating or building on BIMRI. The normative protocol is
[`BIMRI-PROTOCOL.md`](BIMRI-PROTOCOL.md). Questions and problems go to
[GitHub Issues](https://github.com/EvolutionUnleashed/bimri/issues).

| I want to... | Go to... |
| --- | --- |
| Install BIMRI in a project | [Install BIMRI](#install-bimri) or [`INSTALL.md`](INSTALL.md) |
| Understand the memory architecture | [How BIMRI persistent memory works](#how-bimri-persistent-memory-works) |
| Store and retrieve agent memory | [Quick start](#quick-start-store-and-retrieve-project-memory) |
| Understand exact recall and integrity checks | [Exact recall and integrity performance](#exact-recall-and-integrity-performance) |
| Read the normative memory protocol | [`BIMRI-PROTOCOL.md`](BIMRI-PROTOCOL.md) |
| Migrate an older BIMRI store | [`MIGRATION.md`](MIGRATION.md) |
| Review releases and architecture changes | [`CHANGELOG.md`](CHANGELOG.md) |

The current engine is v5.1.1, the authority format is v5.1.0, and the readable
hot-memory grammar remains v5.0.2.

### Supported AI Agent Runtimes

BIMRI is agent-independent. Its canonical interface is a dependency-free
Python 3.8+ command-line engine plus the universal instructions in `AGENTS.md`.

The concurrency guarantee applies to agents that share the same operating-
system/filesystem lock domain. Simultaneous writes from different machines,
independently copied folders, or unverified mounted filesystems are not a v5
guarantee.

Commands in this document use `<verified-python>` as a placeholder for the
absolute Python 3.8+ executable that the installer has executed and verified on
the current machine. A name such as `python3` is never assumed to work.
Multiline examples use POSIX continuation syntax; on Windows, run them on one
line or adapt them to the active shell and path format.

Machine bindings are not portable memory. The installer writes
`.bimri/runtime.local.json` with the verified runtime argv prefix and
`.bimri/hooks.claude.local.json` with the rendered Claude hook source. Do not
commit either file or copy its absolute paths into shared instructions. After
moving the project or replacing Python, rerun installation to regenerate both
files before an agent uses BIMRI.

### How BIMRI Persistent Memory Works

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

### Long-Term Memory Without Context Bloat

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

### Install BIMRI

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

Before any upgrade, make the target quiescent and copy `bimri.md` plus the
entire `.bimri/` tree somewhere outside the project. For v5.1.0 to v5.1.1,
that complete snapshot is the only rollback after the first v5.1.1 proposal is
staged: an older v5.1.0 engine rejects the newer proposal receipt. A store the
new engine has only started and closed, with no v5.1.1 proposal, remains
readable by v5.1.0.

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

### Quick Start: Store and Retrieve Project Memory

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

### Exact Recall and Integrity Performance

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
the full semantic audit decides. When it passes over changes the engine cannot
attribute to its own recorded operation, a sealed drift receipt is durably
recorded and validated under `.bimri/audit-drift/` before the new baseline can
publish. The receipt carries the diverging paths with prior and current hashes,
inline up to a documented per-section bound with any remainder counted, and
pins the complete delta in a validated attachment when truncated. `doctor`
validates every receipt (seal, filename binding, and each referenced
attachment's existence, size and hash) before trusting it. A receipt that
cannot be written keeps the prior checkpoint as the baseline and surfaces as an
error, and a checkpoint whose referenced manifest evidence is missing refuses
rebaselining instead of adopting new bytes blind. A failed semantic audit
refuses into damaged-authority recovery and invalidates the checkpoint by
advancing the audit epoch, so the next `start` prints
`AUTHORITY RECOVERY NEEDED` and exact reads refuse until the store is repaired,
exactly as v5.1.0 behaved. The checkpoint bytes stay on disk as the prior
baseline for receipts, quarantine and restore; only a blocked receipt sink, an
open quarantine, or a strict restore comparison keeps the prior checkpoint
readable. This detects and records standalone or accidental edits; it is not a
defense against a coordinated writer editing history and derived evidence
together, which no local store can prove from its own bytes.

#### Support envelope

v5.1.1 is a bounded, single-store performance release, validated on a store
shaped like its own development project: roughly 500 runs, a few hundred
revisions and current subjects, one machine, one lock domain. Inside that
shape, warm exact reads run in the low hundreds of milliseconds end to end,
start and journal likewise, authority-changing writes about 1.3 seconds,
and the full audit that seeds the checkpoint runs once at about 30 seconds.
Outside that shape the documented ceilings apply until the planned v5.2
work: exact reads scale with total current-state size, a selected cold key
scans its whole archive month, every operation serializes behind one
exclusive lock, task-language `recall --query` is unranked ASCII substring
matching on the fully audited path, and authority writes rescan retained
history. Larger current-state sets, high reader concurrency, ranked or
non-ASCII retrieval, multi-machine fleets, and indefinite-lifetime storage
are explicitly not claims of this release.

This is a cooperative local integrity model, not a claim that each read takes a
filesystem snapshot. An out-of-engine edit to unrelated protected history can
remain unseen by current-only recall until the next full-audit boundary;
changes to state, the accepted head, or the selected cold binding invalidate
that lookup immediately. Every writer must therefore use the engine and shared
lock. Run logs under `.bimri/log/` sit outside the witnessed inventory
altogether: the audit validates active-run logs and the run facts that bear on
authority, and it does not hash closed journals. A missing or unreadable
checkpoint can still require a full audit before the fast path is
re-established. While an interrupted authority write leaves a decision or
resolution `applying` that the next command's recovery pass cannot settle on
its own, the engine withholds the checkpoint and every start and exact read
takes the full-audit path until that run's own sync or close settles it;
`doctor` passes meanwhile and lists each unfinished applying decision. BIMRI
does not promise one universal latency across filesystems, security scanners,
or unbounded current state.

### Memory Tiers, Provenance, and Trust

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

### Human Review and Conflict Resolution

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

### Local-First File and Storage Map

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
| `.bimri/audit-drift/` | Bounded rolling receipts (newest 200) sealing diverging paths with prior and current hashes; truncated receipts pin their complete delta in a validated attachment. |
| `.bimri/audit-blocked.json` | Owner-repair baseline held while a quarantine is open; cleared by restoration. |
| `.bimri/archive/` | Cooled-current, replaced, and closed generations with provenance. |
| `.bimri/backups/` | Migration and pre-change safety copies. |
| `.bimri/recovery/` | Direct edits, damaged authority evidence, and restore receipts. |
| `.bimri/migrations/` | Completed migration records. |
| `.bimri/runtime.local.json` | Installer-written host-only runtime binding record; never commit. |
| `.bimri/hooks.claude.local.json` | Installer-written host-only rendered Claude hook source; never commit. |

Markdown carries the human-readable memory and evidence. Small JSON and TSV
files carry transparent bookkeeping. All of it stays in the project folder.

### BIMRI Command-Line Operations

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

### Authority Damage and Recovery

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

### Concurrency and Portability Boundary

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
from the word "container." The safe default is one runtime boundary active at
a time, with a quiescent handoff before another boundary uses the folder.

### Privacy

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

- Product site: [agentguru.ai](https://agentguru.ai)
- Community: [Evolution Unleashed](https://evolutionunleashed.com)
- Patreon: [Evolution Unleashed VIP](https://www.patreon.com/evolutionunleashedvip)

## License

MIT. See [`LICENSE`](LICENSE).
