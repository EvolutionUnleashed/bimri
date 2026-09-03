# BIMRI: Persistent Memory for AI Agents, in Files You Own

[![Tests](https://github.com/EvolutionUnleashed/bimri/actions/workflows/tests.yml/badge.svg?branch=main)](https://github.com/EvolutionUnleashed/bimri/actions/workflows/tests.yml)
![Python 3.8 or newer](https://img.shields.io/badge/python-3.8%2B-blue)
![Dependencies: none](https://img.shields.io/badge/dependencies-none-lightgrey)
![License: MIT](https://img.shields.io/badge/license-MIT-green)

**Your agent's memory. On your machine. Yours.**

BIMRI gives Claude Code, OpenAI Codex, local Claude Cowork sessions and other
local agents one persistent memory for a project, kept as plain files in the
project folder. Accepted facts and current work record their source and trust;
learned patterns carry evidence, confidence and a falsifier. One key holds one
current answer, with the history behind it. When two agents make incompatible
concurrent changes to the same key, the engine records one conflict for you to
decide. The runtime is one dependency-free Python script: no database, account
or API key, and no model extracting or rewriting memory inside the engine.
Free and open source under the MIT license. First released in this repository
on 14 March 2026.

## Install BIMRI

Give a local coding agent the project folder and paste this:

```text
Install BIMRI in this project from https://github.com/EvolutionUnleashed/bimri.
Follow INSTALL.md, preserve my existing instructions and memory, and run the
self-check.
```

The agent installs it, keeps any memory and instructions you already have, and
runs a self-check before it reports back.

### Upgrade an Existing BIMRI Installation

<details>
<summary><strong>Already have BIMRI? Paste this upgrade prompt.</strong></summary>

The current installer detects and upgrades every version it supports, from v1
through v5.1.1. You do not need to identify the installed version first. Give a
local coding agent the project folder and paste this:

```text
Upgrade the BIMRI installation in this project to the latest release from
https://github.com/EvolutionUnleashed/bimri. Work from a clean temporary copy
of the current repository outside the target project, and read its INSTALL.md
and MIGRATION.md before changing anything. Detect the installed BIMRI version
and follow the documented upgrade path for that version; do not use this
project's old engine as the installer. Before modifying the project, stop every
other agent and BIMRI process using the folder, wait for every in-flight BIMRI
command to finish, pause any sync or copy operation, and make a verified
complete backup of the project outside it. If v1-v3 BIMRI is active in Claude
Cowork Global Instructions, stop and ask me to disable it. Verify an absolute
Python 3.8+ executable as INSTALL.md requires. Pass --quiescent only for an
existing v5 store and only after the folder is genuinely quiescent. Preserve
all project instructions, BIMRI memory, evidence, history and unrelated files;
never edit bimri.md or .bimri/ by hand. If the version is unsupported, the
source is ambiguous, the store is damaged, the backup cannot be verified or
any safety check fails, stop without changing the project and tell me exactly
what blocked the upgrade. Confirm .bimri/runtime.local.json was rebound. If
hooks are enabled, smoke-test them and confirm the synthetic run closed. Then
run doctor --read-only and status from the installed engine, verify the
migration or update receipt, and report the old and new versions, backup path,
preservation result and validation result.
```

</details>

**Who this is for.** Business owners, consultants and agencies who run AI
agents on real work and are tired of re-explaining the business every morning.
And developers who want durable, inspectable, multi-agent memory without adding
another service to run. The plain-language overview lives at
[agentguru.ai/bimri](https://agentguru.ai/bimri).

```text
 session start
 ────────────►  brief: accepted hot view, under a 49,152-byte ceiling
                (roughly 12,000 tokens for ordinary English)
                          │
                          ▼
                   the agent works
                          │
       journals detail ───┼──►  .bimri/log/R000042.md  (its own journal)
       proposes a change under a stable key
                          │
                          ▼
                        sync
          base hash checked against the accepted head
                          │
            ┌─────────────┴──────────────┐
   independent change,           same key, same base,
                                 different answer
            │                             │
            ▼                             ▼
   new immutable revision        one conflict, raised once,
   bimri.md regenerated          decided by you, recorded
```

## What Makes BIMRI Different

**The owner outranks the model.** Accepted facts and current work carry their
source (`user`, `agent`, `external`, `system` or `legacy`) and trust
(`confirmed`, `working` or `contested`). Learned patterns carry evidence,
confidence and a falsifier. A rule you confirmed cannot be overwritten by
something an agent inferred; the agent's version is held as a candidate until
you adopt it. Correct your agent once and the correction sticks.

**One key, one current answer, the whole history behind it.** Memories are
stored under stable keys such as `offer.spring-promotion`, so a new answer
replaces the old one instead of piling up beside it, and the old one stays
retrievable. Two agents making incompatible, concurrent changes to the same
key from the same base are caught the way a version-control merge is caught:
one conflict, raised once, decided by you, recorded permanently. Independent
changes merge on their own.

**No model inside the memory engine.** Its governance and admission rules are
deterministic standard-library Python. The engine makes no model call to store
or retrieve memory and stores validated one-line text without summarizing or
paraphrasing it. You can read the result in any editor.

**The brief comes first in each installed session.** BIMRI's correctly installed
runtime instructions put reading the accepted hot view before work. Its
49,152-byte ceiling is roughly 12,000 tokens for ordinary English, though
tokenizers and UTF-8 width vary. Tier 1 holds durable facts, decisions and
rules, Tier 2 holds current work and next actions, and Tier 3 holds patterns
that carry evidence and a falsifier. Cold-current and historical memory stays
on disk and comes back on request.

**Built for crashes and stray edits.** Every accepted state-changing proposal
becomes an immutable revision through a write-ahead transaction. After an
interruption, recovery proves the new revision committed, keeps the prior state
authoritative, or reports the exact next action. A direct edit to `bimri.md` is
preserved and reported; changed protected authority is caught at a full-audit
boundary. The 255-test suite covers concurrency, crash recovery, migration,
integrity and safety. Public CI runs it on pull requests and `main` under Python
3.8 and 3.12 on Linux and Windows.

**The folder is the memory.** Copy the project folder while BIMRI is idle and
everything BIMRI preserves arrives with it: on a new machine, under a different
supported local agent, or in another supported local runtime. The model is the
replaceable part; the memory is what preserves the business context, and it can
compound as decisions and corrections are recorded.

## What One Memory Looks Like

```text
[R000012-E004] [K:offer.spring-promotion] [fact] [T:confirmed] [SRC:user] [offer,pricing] The spring promotion runs to 30 April at 20% off the annual plan. -> .bimri/log/R000012.md
```

- `[K:offer.spring-promotion]` is a stable key, so a newer memory on the same
  subject replaces this one instead of piling up beside it.
- `[SRC:user]` says the owner directly supplied the claim. Agent inference uses
  `[SRC:agent]`; outside material uses `[SRC:external]`.
- `[T:confirmed]` records its trust state so a runtime can distinguish an
  owner-confirmed claim from working or contested memory.
- `-> .bimri/log/R000012.md` points at the run journal containing the proposal
  record and rationale; further detail is there when the agent journals it.

The file this line sits in, `bimri.md`, is generated from an immutable revision
and is never edited by hand. Under `.bimri/` sit the run journals, every
accepted revision, every immutable proposal and, once processed, its decision,
every recorded conflict and every owner resolution. Markdown holds the memory;
small JSON and TSV files hold the bookkeeping. All of it stays in the project
folder.

## Where It Earns Its Keep

**Two coding agents, one repository.** Claude Code in the morning, Codex in the
afternoon, one accepted memory between them. The decision made at ten is in the
brief at two. If both concurrently propose incompatible answers to the same key
from the same accepted base, you get asked once.

**An agent on a schedule.** Each run starts from the accepted state. The agent
journals durable detail as it works and proposes anything future runs should
know. Thirty runs later the memory is a record of decisions rather than a pile
of summaries. This is how agents in Stu Jordan's business use BIMRI across
scheduled runs.

**Correcting the agent once.** The owner says the spring promotion ends on 30
April and does not extend. That becomes a confirmed Tier 1 fact under
`[SRC:user]`. No agent's later inference can silently replace it, and six weeks
on you can still trace why BIMRI treats it as accepted.

**Moving to a new machine or a new model.** The memory is a folder. Move the
folder while BIMRI is idle, rerun the installer, and the next supported local
agent receives the same accepted BIMRI memory. Changing runtimes does not cost
you that memory.

**Answering the audit question.** What did BIMRI accept, when, and on whose
say-so? Newly authored v5 fact and current-work lines point to their run
journals. Legacy migrations preserve their source files and migration evidence
even when a converted entry has no run-journal pointer. Revisions, proposals,
decisions and conflicts stay on disk for inspection.

## A Real Concurrency Receipt

One evening a second scheduled run arrived while the first was still mid-job.
The workspace lock made the second run stand down, preventing duplicate work.
BIMRI's role was durable continuity: the run recorded why it stopped, and that
reason and outcome remained for the next session. Twelve minutes later the
first run finished cleanly. The whole event is on the record.
[Read the journal entry](https://agentguru.ai/receipts#concurrency).

## How It Works

Each agent asks the engine for its own run handle and receives a short brief,
then reads `bimri.md`, the accepted hot-memory view. It journals detail as it
works. Anything that should shape future sessions is proposed under a stable,
lowercase key such as `checkout.next-step`. A sync checks each proposal against
the base it was written from. State-changing accepted proposals create an
immutable revision and regenerate `bimri.md`; exact compatible effects are
no-ops, unmatched updates are held as candidates, and agent-action failures
stop with instructions. Only a concurrent, incompatible change to the same key
from the same base becomes a question for you.

```text
<verified-python> bimri-engine.py start --actor codex
<verified-python> bimri-engine.py journal --run R000042 --importance 3 --text "Checkout retries must use the existing idempotency key."
<verified-python> bimri-engine.py propose --run R000042 --tier 2 --new-subject --key checkout.next-step --text "Verify retry behavior under concurrent requests."
<verified-python> bimri-engine.py sync --run R000042
<verified-python> bimri-engine.py recall --key checkout.next-step
<verified-python> bimri-engine.py close --run R000042 --outcome success --summary "Retry behavior verified."
```

`<verified-python>` stands for the absolute Python executable the installer
verified on your machine. The installer records it in
`.bimri/runtime.local.json`, and agents read it from there rather than guessing
a PATH name. When the view would grow past its ceiling, the engine moves the
least-retained current-work subjects into keyed cold storage, where they stay
current and recallable. The
[quick start](REFERENCE.md#quick-start-store-and-retrieve-project-memory)
walks through every command.

## Works With

| Agent runtime | Integration |
| --- | --- |
| Claude Code | A `CLAUDE.md` adapter imports BIMRI's `AGENTS.md` instructions; optional session hooks open and close a run automatically |
| Claude Cowork | Local desktop execution only, with explicit engine commands; cloud sessions are not supported |
| OpenAI Codex | `AGENTS.md` and explicit engine commands |
| Other local coding agents | Supported when they can follow the instruction block and execute the verified Python runtime |

### BIMRI on Claude Cowork: Local Only

BIMRI supports Cowork's local desktop execution, not its cloud sessions.
Anthropic runs Cowork in the cloud by default; local execution remains
available for existing desktop deployments and runs shell commands inside an
isolated Linux VM. Connect the project folder with read, write and delete
access. In Cowork's folder instructions, tell it to read the project's
`AGENTS.md`, then use explicit BIMRI start and close commands.

Cloud Cowork runs its agent loop and commands on Anthropic's servers and reaches
local files through brokered desktop tools. That does not establish the shared
lock and atomic-rename boundary BIMRI requires, so cloud sessions are not
supported. See [how Cowork execution works](https://support.claude.com/en/articles/14479288-claude-cowork-architecture-overview).

Never let two Cowork sessions, or Cowork's VM and a native Claude Code or Codex
process, use the same memory at the same time. Stop the active agent, let every
BIMRI command finish, then rerun installation inside the next runtime so its
Python binding is valid. The local path was verified with BIMRI v5.0.2; v5.1.1
has not yet had a Cowork-specific regression pass.

The runtime is one Python 3.8 or newer standard-library script plus ordinary
local files. There are no packages to install. The BIMRI engine has no network
client, server, account or API key. Normal memory operations stay within local
project files; installation and explicit repair may also use local paths you
supply. Your agent runtime may still send prompts, relevant file excerpts or
tool results to its model provider under that runtime's own data controls.

## Measured on a Real Store

Measured on the development store used by agents running Stu Jordan's business
(about 500 runs and 216 revisions), on Windows 11 on 2026-09-02:

| Operation | Time |
| --- | ---: |
| Exact recall of one memory, warm, end to end | 0.33 s |
| Session start, warm | 0.41 s |
| Journal one line | 0.39 s |
| Propose or sync an authority change | 1.3 to 1.6 s |
| Cold full audit to seed or rebuild the checkpoint | about 33 s per audit |

Warm reads, starts and journals use an integrity checkpoint while it remains
valid. Before any authority-changing write, the engine rechecks the complete
protected record.

## Boundaries, Stated Plainly

- **One machine, one filesystem lock domain.** Several agents can share the
  memory safely there when every writer uses the engine. Two machines, a
  synchronized folder or an unverified container mount are outside the
  guarantee. Move or copy the folder while BIMRI is idle. See the full
  [concurrency boundary](REFERENCE.md#concurrency-and-portability-boundary).
- **Exact and lexical retrieval.** `recall --key` returns the current answer
  for a subject; `recall --query` searches in task language. There are no
  embeddings and no semantic ranking, by design.
- **Provenance is a label, not a lock.** Any process that can write the project
  can write its memory files. The engine validates its protected authority and
  generated hot view; operating-system permissions remain the security
  boundary.
- **v5.1.1 is validated on a store shaped like its own development project:**
  about 500 runs, a few hundred revisions, one machine. Larger fleets and
  ranked retrieval are planned v5.2 work. The full support envelope is in
  [`REFERENCE.md`](REFERENCE.md#exact-recall-and-integrity-performance).

## BIMRI and Compounding Intelligence

BIMRI is the memory layer inside the compounding intelligence engine Stu Jordan
uses to run agents in his own business. Continuity, feedback learning and a
results ledger handle the other parts. BIMRI makes improvement durable by
preserving the accepted decisions, corrections and business context those
systems need, and it is the part released as open source.

What the business records in BIMRI survives the session, the model and the
runtime.

If you want the finished agent, [Agent in a Box](https://agentguru.ai/agent-in-a-box)
is one agent for one named job, built on this architecture. [The Starter
Engine](https://agentguru.ai/start) is a complete scheduled agent you can run
free.

## Questions

### Does Claude Code remember between sessions?

Yes. Claude Code reads your `CLAUDE.md` instructions and can keep editable,
machine-local [auto memory](https://code.claude.com/docs/en/memory) between
sessions. Auto memory is per repository and is not shared across machines or
cloud environments. BIMRI adds project-owned memory that supported local agents
in the folder can share, with source and trust on accepted facts and current
work, plus a decision record for incompatible concurrent changes to the same
key. With the rendered hooks enabled, Claude Code opens and closes a BIMRI run
automatically.

### Does OpenAI Codex remember between sessions?

Yes, when local memories are enabled. Codex can generate local memory files
from eligible prior chats under the Codex home directory (normally
`~/.codex/memories/`) and use them in later sessions. OpenAI documents these as
[inspectable generated state](https://learn.chatgpt.com/docs/customization/memories);
the feature is off by default and updates in the background. BIMRI runs
alongside it as project-owned memory that Codex and other supported local
agents can use from the same folder. Keep persistent project instructions in
`AGENTS.md`.

### How is this different from the memory built into my AI app?

Built-in memory belongs to one product ecosystem, under that product's schema
and lifecycle. BIMRI belongs to the project. It is the same accepted memory for
every supported local agent. Accepted facts and current work carry source and
trust; learned patterns carry evidence, confidence and a falsifier. The full
history stays in files you can open.

### Does BIMRI send my memory anywhere?

No. The engine has no network client, server, account or API key. Your agent
runtime may send prompts, file excerpts or tool results to its model provider
under its own data controls; BIMRI does not change that boundary.

### What happens if an agent crashes mid-write?

BIMRI records the intended change before it replaces the accepted state. After
an interruption, recovery either proves the change committed, keeps the prior
accepted memory authoritative, or reports the exact next action. Accepted
memory is never left half-written, and nothing has to be deleted by hand.

### What does BIMRI stand for?

Brief Interaction Memory and Retrieval Intelligence: brief because the working
memory an agent loads stays small, retrieval because the long tail is recalled
on demand rather than stuffed into every session.

### Is it free?

Yes. MIT licensed, free for personal and commercial use, with no hosted tier
behind it and nothing to cancel.

## Documentation

| I want to... | Go to... |
| --- | --- |
| Install BIMRI in a project | [`INSTALL.md`](INSTALL.md) |
| Upgrade an existing installation | [Safe upgrade prompt](#upgrade-an-existing-bimri-installation) or [`MIGRATION.md`](MIGRATION.md) |
| Operate it: commands, tiers, review, recovery, storage map, boundaries | [`REFERENCE.md`](REFERENCE.md) |
| Read the normative memory protocol | [`BIMRI-PROTOCOL.md`](BIMRI-PROTOCOL.md) |
| Migrate an older BIMRI store | [`MIGRATION.md`](MIGRATION.md) |
| Review releases and architecture changes | [`CHANGELOG.md`](CHANGELOG.md) |
| Ask a question or report a problem | [GitHub Issues](https://github.com/EvolutionUnleashed/bimri/issues) |

The current engine is v5.1.1, the authority format is v5.1.0, and the readable
hot-memory grammar is v5.0.2.

## Author

**Stu Jordan**, Agent Architect

- Product site: [agentguru.ai](https://agentguru.ai)
- Community: [Evolution Unleashed](https://evolutionunleashed.com)
- Patreon: [Evolution Unleashed VIP](https://www.patreon.com/evolutionunleashedvip)

## License

MIT. See [`LICENSE`](LICENSE).
