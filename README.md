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

**Versioned memory keeps the current answer clear.** Each subject has one current entry. Updating a decision replaces that entry while retaining its history, so old and new answers do not accumulate as equally current instructions.

**Memory provenance preserves who said what.** Facts and current work record their source and trust level. The engine keeps an agent's proposed change separate when it would replace something you confirmed.

**Multi-agent conflict handling protects shared decisions.** The engine checks a proposed update against the version it was based on before accepting it. The [reference guide](https://github.com/EvolutionUnleashed/bimri/blob/main/REFERENCE.md) explains how independent changes merge and concurrent conflicts are resolved.

**Tiered memory manages the context window.** Agents start with a bounded working memory covering durable rules, current work and recorded patterns. Other saved knowledge remains on disk for retrieval when needed. Long-term memory can grow without loading the entire history into every session.

**Deterministic storage preserves the text you save.** The engine uses fixed Python rules to accept, store and retrieve entries. It does not ask a model to rewrite saved text or make an LLM call for memory operations. The agent decides what to propose for memory.

**Crash recovery protects continuity.** Accepted changes have a version history. Interrupted writes can be checked and recovered using the engine's recovery process.

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
