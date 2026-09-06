# BIMRI: Persistent Memory for AI Agents

**Multi-agent memory with local ownership and portability.**

BIMRI is an open-source persistent AI memory system. Keep long-term project knowledge on your machine, carry it between your local agents, and let them build on the same decisions and context. Your investment in teaching an agent stays with you when you change tools.

Use Claude Code for one part of a project and OpenAI Codex for another. Run a local agent on a schedule. Start a new session next week. BIMRI makes the knowledge you've recorded available for the work that comes next.

The memory engine is agent-agnostic: any local agent that can follow its instructions and execute its Python commands can integrate with it. Memory is stored in plain files in your project folder, with no hosted memory service, database, account or API key. Free under the MIT license.

## Local Ownership and Portable Long-Term Memory

Every project develops knowledge worth keeping: why a decision was made, what a customer needs, which approach failed, what should happen next. Persistent AI memory lets that knowledge accumulate across sessions and remain available as your tools change.

With BIMRI, you control the files that hold it. You can inspect your memory in a text editor, back it up with the project, and move it to another machine. Your next agent inherits the context you've spent months building.

That is the foundation for compounding intelligence: future work can draw on more of what you've learned. BIMRI preserves the decisions, corrections and findings that make this possible; your agents use them in their work.

## Multi-Agent Memory for Shared Project Knowledge

Several agents working on one project on your machine share a common memory. A finding recorded by one becomes available to the others when they next load or retrieve it. This supports parallel work, handoffs between coding agents, and continuity across scheduled runs.

Shared memory also needs a way to handle disagreement. BIMRI combines independent updates automatically. When two agents make incompatible concurrent changes to the same subject, it preserves the conflict for you to resolve. A confirmed decision cannot be silently replaced in memory by an agent's inference.

## Example: Two Coding Agents, One Project Memory

Suppose you're building a booking app with two agents. One works on the booking flow while another investigates the calendar integration. Later, a third agent takes over the next stage with the project memory already in place. In this fictional project, BIMRI keeps track of:

| What happens | What BIMRI keeps for later work |
| --- | --- |
| You decide that customers can book without creating an account. | The confirmed decision, available to the next agent working on the app. |
| An agent discovers that a calendar integration uses UTC. | A saved finding, with its source recorded. |
| You change the cancellation period from 48 hours to 24 hours. | The current decision, with the previous version retained in history. |
| Two agents concurrently propose different changes to the same cancellation rule. | A conflict you can resolve, so one change does not silently replace the other. |

## Install Persistent Memory for Claude Code or Codex

Give your local coding agent access to the project folder and paste:

```text
Install BIMRI in this project from https://github.com/EvolutionUnleashed/bimri.
Follow INSTALL.md, preserve my existing instructions and memory, and run the
self-check.
```

The [installation guide](https://github.com/EvolutionUnleashed/bimri/blob/main/INSTALL.md) covers setup and runtime requirements. For an existing installation, follow the [upgrade and migration guide](https://github.com/EvolutionUnleashed/bimri/blob/main/MIGRATION.md).

## How BIMRI Manages Agent Memory

An agent starts with the project's current memory, records useful findings as it works, and proposes updates for future sessions. BIMRI checks those updates, maintains the current decisions and preserves their history. Working memory stays within its limits, while additional knowledge remains available for retrieval.

Agents decide what to propose for memory. The engine manages how those entries are stored, updated and kept within the working-memory limit.

### Three Memory Tiers

BIMRI organises working memory by the role each piece of knowledge plays. Continuing the fictional booking-app example:

| Tier | What it remembers | Example |
| --- | --- | --- |
| **1. Core knowledge** | Lasting facts, confirmed decisions, preferences and standing rules | Customers can book without creating an account. |
| **2. Active context** | Current work, recent findings, risks and next steps | Test the calendar integration before launch. |
| **3. Patterns** | Recurring observations, with evidence, confidence and a way to check whether they hold | Calendar failures appear more often around daylight-saving changes. |

Core knowledge gives your agents a consistent foundation. Active context keeps them up to date with the work. Patterns help agents carry useful observations into future decisions, while keeping track of what supports them and what would prove them wrong.

The tiers describe different kinds of knowledge. They are not stages every memory passes through, and Tier 3 is not an archive. An agent's proposed addition to core knowledge needs your confirmation.

### Keeping Decisions Current

**Versioned memory keeps one current answer per subject.** If you change the cancellation period from 48 hours to 24 hours, the new decision replaces the current entry. The earlier version stays in history, with its source record available when you need to understand the change.

**Memory provenance preserves who said what.** Facts and active context record where they came from and whether they are confirmed or provisional. Your confirmed instruction takes priority over an agent's inference. A proposed replacement stays separate until you adopt it.

**Multi-agent conflict handling keeps simultaneous updates organised.** The engine checks each update against the version it was based on. Independent changes merge automatically; incompatible concurrent changes to the same subject are preserved for resolution. Agents can contribute without silently overwriting one another's decisions.

The engine stores entries using fixed Python rules, without asking another model to summarise or rewrite them. Saving and retrieving memory adds no LLM call inside BIMRI.

### Growing Long-Term Memory Without Context Bloat

Working memory is the brief an agent loads at the start of a session. Updating existing subjects keeps that brief from filling with repeated versions of the same decision.

When a new entry would exceed the working-memory limit, BIMRI automatically moves lower-priority active-context entries into longer-term storage. Those entries keep their current status, source and trust level, and remain available for retrieval. Core knowledge stays in the starting brief.

The engine handles this maintenance as it accepts updates, so you do not have to keep trimming the memory file. The limit applies to the starting context; your accumulated project knowledge continues to grow on disk.

### Retrieving Earlier Knowledge

An agent can retrieve the current answer for a specific subject or search saved memory using words from its task. It can also look up previous versions when the history matters. Moving an entry out of the starting brief keeps it available without loading it into every session.

For example, an agent investigating a calendar bug can search for earlier findings about time zones and daylight-saving changes. A decision from months ago can still inform today's work.

### Continuity Across Tools and Interrupted Work

Your project folder holds the working memory, additional saved knowledge and version history together. Move it while BIMRI is idle and rerun installation in the new environment to continue with that knowledge intact.

Crash recovery protects the same continuity during interrupted writes. BIMRI records a change before replacing the accepted memory. After an interruption, recovery establishes whether the change completed, retains the previous accepted state, or identifies the repair needed.

The [reference guide](https://github.com/EvolutionUnleashed/bimri/blob/main/REFERENCE.md) covers memory tiers, retrieval commands, conflict handling and recovery in detail.

## Requirements and Limits

Python 3.8 or newer and a local agent that can read project files, follow BIMRI's instructions and run its engine commands. The installer verifies the Python executable.

Claude Code and OpenAI Codex use project instructions to work with BIMRI. Other local agents need the same file and command access. Cowork support is limited to the documented local execution setup; cloud sessions are unsupported. See [runtime setup](https://github.com/EvolutionUnleashed/bimri/blob/main/INSTALL.md).

- Concurrent agents use the engine within one operating-system locking environment. For multiple machines, synced folders, or separate VM and container environments, use the [handoff procedure](https://github.com/EvolutionUnleashed/bimri/blob/main/REFERENCE.md#concurrency-and-portability-boundary). Cowork's VM and native agents take turns using the memory.
- To move memory, stop its agents and BIMRI processes, copy the project while it is idle, then rerun installation in the new environment.
- Retrieval uses subject keys and text search. Semantic search with embeddings is not included.
- BIMRI makes no network calls. Your agent provider's own data handling still applies to anything the agent reads.
- Filesystem permissions control who can change the files. Memory source labels are not access controls.

## Documentation

| Task | Guide |
| --- | --- |
| Install or configure BIMRI | [Installation](https://github.com/EvolutionUnleashed/bimri/blob/main/INSTALL.md) |
| Upgrade an existing installation | [Migration](https://github.com/EvolutionUnleashed/bimri/blob/main/MIGRATION.md) |
| Store and retrieve your first memory | [Quick start](https://github.com/EvolutionUnleashed/bimri/blob/main/REFERENCE.md#quick-start-store-and-retrieve-project-memory) |
| Understand commands, sharing and recovery | [Reference](https://github.com/EvolutionUnleashed/bimri/blob/main/REFERENCE.md) |
| Read the full memory rules | [Protocol](https://github.com/EvolutionUnleashed/bimri/blob/main/BIMRI-PROTOCOL.md) |
| Review changes or report a problem | [Changelog](https://github.com/EvolutionUnleashed/bimri/blob/main/CHANGELOG.md) · [Issues](https://github.com/EvolutionUnleashed/bimri/issues) |

Created by Stu Jordan. [AgentGuru](https://agentguru.ai) · [MIT license](https://github.com/EvolutionUnleashed/bimri/blob/main/LICENSE)
