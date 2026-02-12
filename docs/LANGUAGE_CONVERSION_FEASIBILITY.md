# Language Conversion Feasibility Evaluation

## Executive Summary

This document evaluates the feasibility of converting the NanoClaw codebase (~6,800 lines of TypeScript across host orchestrator and container agent-runner) to four alternative languages: **Python**, **Go**, **Rust**, and **Elixir/OTP**.

NanoClaw is a single Node.js process that connects to WhatsApp via the Baileys library, routes messages to Claude Agent SDK running inside Apple Container Linux VMs, manages per-group isolation with file-based IPC, and schedules recurring tasks. The codebase relies on async I/O, child process management, SQLite, streaming output parsing, and an unofficial WhatsApp protocol library.

### Verdict at a Glance

| Language | Feasibility | Effort | Risk | Recommendation |
|----------|-------------|--------|------|----------------|
| **Python** | High | Medium | Low | Best candidate if conversion is desired |
| **Go** | High | Medium | Low-Medium | Strong candidate with concurrency benefits |
| **Rust** | Medium | High | Medium | Viable but over-engineered for this use case |
| **Elixir** | Medium-Low | High | High | Excellent architecture fit, but critical library gaps |

---

## Codebase Profile

Before evaluating each language, here is what must be ported:

### Host Orchestrator (~4,540 lines)
| Component | Lines | Key Dependencies | Complexity |
|-----------|-------|------------------|------------|
| `index.ts` — Main loop, state | 516 | child_process, fs | Medium |
| `container-runner.ts` — Spawn containers | 657 | child_process (spawn), streaming | High |
| `db.ts` — SQLite layer | 584 | better-sqlite3 | Medium |
| `ipc.ts` — File-based IPC watcher | 381 | fs, cron-parser | Medium |
| `group-queue.ts` — Concurrency control | 302 | Promises, queues | High |
| `mount-security.ts` — Mount validation | 418 | fs, path | Medium |
| `channels/whatsapp.ts` — WhatsApp client | 284 | @whiskeysockets/baileys | High |
| `task-scheduler.ts` — Cron/interval tasks | 218 | cron-parser | Low-Medium |
| `router.ts` — Message formatting | 46 | None (pure string ops) | Low |
| `config.ts` — Constants | 55 | Environment variables | Low |
| `types.ts` — Interfaces | 101 | None | Low |
| `logger.ts` — Pino logging | 16 | pino | Low |

### Container Agent-Runner (~812 lines)
| Component | Lines | Key Dependencies | Complexity |
|-----------|-------|------------------|------------|
| `index.ts` — Claude SDK executor | 533 | @anthropic-ai/claude-agent-sdk | High |
| `ipc-mcp-stdio.ts` — MCP tool server | 279 | @modelcontextprotocol/sdk | Medium-High |

### Critical Dependencies (Blocking if No Equivalent Exists)
1. **WhatsApp client** (`@whiskeysockets/baileys`) — Unofficial reverse-engineered library
2. **Claude Agent SDK** (`@anthropic-ai/claude-agent-sdk`) — Anthropic's official SDK for running Claude Code
3. **MCP SDK** (`@modelcontextprotocol/sdk`) — Model Context Protocol server implementation
4. **Apple Container CLI** — Invoked via shell exec (language-agnostic)

### Non-Blocking Dependencies (Easy to Replace)
- SQLite bindings (available everywhere)
- Cron parsing (libraries exist in all languages)
- JSON structured logging (trivial)
- Schema validation (language-dependent)
- File system operations (standard library)
- Child process management (standard library)

---

## 1. Python

### Overall Feasibility: HIGH

Python is the most natural conversion target. The Anthropic ecosystem is Python-first, the async patterns map cleanly, and mature equivalents exist for almost every dependency.

### Dependency Mapping

| Node.js Dependency | Python Equivalent | Maturity | Notes |
|---------------------|-------------------|----------|-------|
| `@whiskeysockets/baileys` | `neonize`, `whatsapp-web.js` via bridge | Medium | `neonize` wraps the Go `whatsmeow` library. Less mature than Baileys but functional. Alternatively, run Baileys as a subprocess with a bridge. |
| `better-sqlite3` | `sqlite3` (stdlib) or `aiosqlite` | Excellent | Python's built-in sqlite3 module is battle-tested. `aiosqlite` for async. |
| `claude-agent-sdk` | `anthropic` SDK + `claude-code-sdk` | Excellent | Anthropic's Python SDK is their primary SDK. The Claude Code SDK (`claude-code-sdk`) is available for Python and is the canonical implementation. |
| `@modelcontextprotocol/sdk` | `mcp` (official Python SDK) | Excellent | The MCP Python SDK is a first-class implementation maintained by Anthropic. |
| `cron-parser` | `croniter` | Excellent | Mature, well-maintained. |
| `pino` | `structlog` or `python-json-logger` | Excellent | Multiple mature options. |
| `zod` | `pydantic` | Excellent | Pydantic is arguably more capable than Zod. |
| Child process mgmt | `asyncio.create_subprocess_exec` | Excellent | Built-in async subprocess support. |
| File watching/polling | `watchfiles` or `os.listdir` polling | Excellent | Multiple approaches available. |

### Architecture Mapping

| TypeScript Pattern | Python Equivalent | Fit |
|--------------------|-------------------|-----|
| `async/await` with Promises | `asyncio` with coroutines | Direct 1:1 |
| Event emitters (Baileys) | `asyncio.Queue`, callback patterns | Good |
| `while(true)` polling loops | `asyncio` tasks with `await asyncio.sleep()` | Direct 1:1 |
| TypeScript interfaces | `dataclasses`, `TypedDict`, Pydantic models | Good (runtime validation bonus) |
| Streaming child process output | `asyncio.create_subprocess_exec` with `readline()` | Direct 1:1 |
| GroupQueue concurrency control | `asyncio.Semaphore` + `asyncio.Queue` | Direct 1:1 |

### Strengths
- **Claude SDK is Python-first.** The agent SDK, MCP SDK, and all Anthropic tooling have Python as the primary target. This is the single biggest advantage.
- **Rapid development.** Python's expressiveness would likely result in fewer lines of code.
- **Pydantic.** More powerful than Zod for runtime validation, with automatic serialization.
- **asyncio maturity.** Python's async ecosystem is mature enough for this workload.
- **Ecosystem breadth.** Every dependency has a well-maintained Python equivalent.

### Risks & Challenges
- **WhatsApp client gap.** No Python WhatsApp library matches Baileys' maturity. `neonize` (Go-backed) works but has a smaller community. May need to maintain a Baileys bridge subprocess.
- **Performance.** Python is slower than Node.js for I/O-heavy workloads, though for this application (container orchestration, not high-throughput), it doesn't matter. The bottleneck is Claude API latency, not host CPU.
- **Single-threaded GIL.** The `asyncio` event loop is single-threaded like Node.js, so the concurrency model maps directly — but CPU-bound work (unlikely in this app) would need `ProcessPoolExecutor`.
- **Type safety regression.** Python's type system (even with `mypy`) is less strict than TypeScript in strict mode. Pydantic compensates at runtime.

### Effort Estimate
- **Core orchestrator:** 2-3 weeks (straightforward port)
- **WhatsApp integration:** 1-2 weeks (library evaluation and adaptation)
- **Container agent-runner:** 1 week (Claude SDK is Python-native)
- **Testing and stabilization:** 1-2 weeks
- **Total: 5-8 weeks**

### Verdict
**Recommended conversion target.** The Anthropic SDK ecosystem being Python-first makes this the most natural choice. The only meaningful friction is the WhatsApp client library, which can be bridged.

---

## 2. Go

### Overall Feasibility: HIGH

Go's strengths in concurrency, subprocess management, and single-binary deployment make it a strong candidate. The ecosystem has solid coverage for most dependencies, with one critical gap.

### Dependency Mapping

| Node.js Dependency | Go Equivalent | Maturity | Notes |
|---------------------|---------------|----------|-------|
| `@whiskeysockets/baileys` | `whatsmeow` (tulir/whatsmeow) | Excellent | The best WhatsApp Web library in any language. More stable than Baileys. Used by Matrix bridges and many production systems. |
| `better-sqlite3` | `modernc.org/sqlite` or `mattn/go-sqlite3` | Excellent | Pure-Go (`modernc`) or CGo (`mattn`). Both battle-tested. |
| `claude-agent-sdk` | None (subprocess bridge) | Gap | No official Go SDK for Claude Code. Would need to shell out to `claude` CLI or wrap the Python/Node SDK. |
| `@modelcontextprotocol/sdk` | `mark3labs/mcp-go` | Medium | Community-maintained Go MCP SDK. Functional but less mature than official SDKs. |
| `cron-parser` | `robfig/cron` | Excellent | De facto standard. Very mature. |
| `pino` | `zerolog` or `zap` | Excellent | High-performance structured logging. |
| `zod` | struct tags + `go-playground/validator` | Good | Go uses struct definitions with validation tags. Different paradigm but effective. |
| Child process mgmt | `os/exec` | Excellent | First-class stdlib support. Streaming stdout trivial. |
| File watching/polling | `fsnotify` or `os.ReadDir` polling | Excellent | `fsnotify` for events, manual polling also simple. |

### Architecture Mapping

| TypeScript Pattern | Go Equivalent | Fit |
|--------------------|---------------|-----|
| `async/await` with Promises | Goroutines + channels | Excellent (more powerful) |
| Event emitters | Channels, callbacks | Good |
| `while(true)` polling loops | Goroutines with `time.Ticker` | Direct 1:1 |
| TypeScript interfaces | Go interfaces + structs | Good |
| Streaming child process output | `bufio.Scanner` on `cmd.StdoutPipe()` | Direct 1:1 |
| GroupQueue concurrency control | `sync.WaitGroup`, channels, semaphore pattern | Excellent |

### Strengths
- **`whatsmeow` is best-in-class.** The Go WhatsApp library is more mature, better documented, and more actively maintained than Baileys. This is a significant upgrade.
- **Native concurrency.** Goroutines and channels are a perfect fit for the concurrent container orchestration model. The GroupQueue would be simpler and more robust.
- **Single binary deployment.** No runtime dependencies (no Node.js, no npm). Simplifies deployment and container builds.
- **Subprocess management.** Go's `os/exec` is excellent for spawning and managing container processes.
- **Memory efficiency.** Lower memory footprint than Node.js, relevant if running many concurrent containers.

### Risks & Challenges
- **No Claude Agent SDK for Go.** This is the critical gap. The container agent-runner (which calls the Claude Agent SDK) would need to either: (a) remain in TypeScript/Python inside the container, (b) shell out to the `claude` CLI, or (c) use the raw Anthropic API without the Agent SDK's orchestration features. Option (a) is pragmatic — only the host orchestrator would be in Go while the container stays Node.js.
- **No official MCP SDK.** The community `mcp-go` library works but lacks the polish of official SDKs. MCP protocol changes could cause breakage.
- **Verbosity.** Go code is typically 1.5-2x more lines than TypeScript for equivalent logic. Error handling alone adds significant boilerplate. Expect ~10,000-12,000 lines.
- **No generics (historically).** Go 1.18+ has generics, but the ecosystem still favors interface-based patterns. The GroupQueue and type-safe message passing would be more verbose.
- **JSON handling verbosity.** Go's `encoding/json` requires explicit struct tags and manual marshaling. More boilerplate than TypeScript's native JSON.

### Effort Estimate
- **Core orchestrator:** 3-4 weeks (more lines of code, error handling)
- **WhatsApp integration:** 1 week (`whatsmeow` is excellent)
- **Container agent-runner:** Keep in TypeScript (0 effort) or rewrite: 2-3 weeks
- **Testing and stabilization:** 1-2 weeks
- **Total: 5-9 weeks** (host only), **7-12 weeks** (full rewrite)

### Verdict
**Strong candidate for the host orchestrator.** The combination of `whatsmeow` (better than Baileys) and Go's native concurrency model makes the host side a natural fit. The pragmatic approach is a **hybrid**: Go host + TypeScript/Python container agent-runner. A full rewrite including the container side is possible but the Claude Agent SDK gap means you'd lose Agent SDK features or add a bridge layer.

---

## 3. Rust

### Overall Feasibility: MEDIUM

Rust brings memory safety, excellent performance, and a strong type system. However, the development overhead is high for an application whose bottleneck is external I/O (API calls, container management), not compute.

### Dependency Mapping

| Node.js Dependency | Rust Equivalent | Maturity | Notes |
|---------------------|-----------------|----------|-------|
| `@whiskeysockets/baileys` | None | Critical Gap | No Rust WhatsApp Web library exists. Would need to write bindings to `whatsmeow` (Go) via CGo/FFI, or run a sidecar process. |
| `better-sqlite3` | `rusqlite` | Excellent | Mature, well-maintained SQLite bindings. |
| `claude-agent-sdk` | None | Critical Gap | No Rust SDK. Would need subprocess bridge to `claude` CLI or raw API calls. |
| `@modelcontextprotocol/sdk` | `mcp-rust-sdk` (community) | Early | Exists but immature compared to Python/TypeScript SDKs. |
| `cron-parser` | `cron` crate | Good | Functional, less feature-rich than Node equivalent. |
| `pino` | `tracing` + `tracing-subscriber` | Excellent | Industry standard for Rust. More capable than pino. |
| `zod` | `serde` + custom validation | Excellent | `serde` for serialization is best-in-class. Validation requires manual implementation or crates like `validator`. |
| Child process mgmt | `tokio::process` | Excellent | First-class async subprocess support in tokio. |
| File watching/polling | `notify` crate or manual polling | Good | `notify` for FS events, async polling with tokio also clean. |

### Architecture Mapping

| TypeScript Pattern | Rust Equivalent | Fit |
|--------------------|-----------------|-----|
| `async/await` with Promises | `tokio` async runtime | Good (steeper learning curve) |
| Event emitters | `tokio::sync::broadcast`, callbacks | Adequate |
| `while(true)` polling loops | `tokio::time::interval` + `tokio::select!` | Good |
| TypeScript interfaces | Traits + structs | Excellent (stronger guarantees) |
| Streaming child process output | `tokio::io::BufReader` on stdout | Good |
| GroupQueue concurrency control | `tokio::sync::Semaphore` + channels | Good |

### Strengths
- **Memory safety guarantees.** No null pointer dereferences, no data races. Valuable for a long-running service.
- **Performance.** Lowest memory footprint and fastest execution of all candidates. Relevant if scaling to many concurrent containers.
- **`serde` is exceptional.** JSON serialization/deserialization in Rust is type-safe and performant.
- **`tracing` ecosystem.** Structured logging and distributed tracing are best-in-class.
- **Strong type system.** Enums with data (ADTs) would model message types, task states, and IPC commands more precisely than TypeScript's union types.

### Risks & Challenges
- **No WhatsApp library.** This is a showstopper for a pure-Rust rewrite. The only viable approaches are: (a) FFI bridge to `whatsmeow` (Go), which adds build complexity and CGo overhead, (b) a sidecar WhatsApp process with IPC, or (c) writing a Rust WhatsApp Web client from scratch (months of work, not recommended).
- **No Claude Agent SDK.** Same gap as Go, but worse since there's no community effort for Rust at all.
- **Development speed.** Rust's borrow checker, lifetimes, and explicit error handling slow initial development significantly. Expect 2-3x the development time of TypeScript for equivalent features.
- **Async complexity.** Tokio is powerful but the `Pin<Box<dyn Future>>`, lifetime annotations in async contexts, and `Send + Sync` bounds add friction that doesn't exist in TypeScript or Python.
- **Ecosystem immaturity for AI/ML tooling.** The Anthropic/AI ecosystem is heavily Python/TypeScript. Rust is an afterthought.
- **Code volume.** Expect 12,000-15,000 lines for equivalent functionality due to explicit error handling, trait implementations, and type definitions.

### Effort Estimate
- **Core orchestrator:** 5-7 weeks (fighting the borrow checker, async lifetimes)
- **WhatsApp integration:** 3-4 weeks (FFI bridge or sidecar architecture)
- **Container agent-runner:** Keep in TypeScript (0 effort) or rewrite: 3-4 weeks
- **Testing and stabilization:** 2-3 weeks
- **Total: 10-14 weeks** (host only), **13-18 weeks** (full rewrite)

### Verdict
**Viable but over-engineered.** Rust's strengths (memory safety, performance) solve problems NanoClaw doesn't have. The application is I/O-bound with low throughput. The critical library gaps (WhatsApp, Claude SDK) and high development cost make this a poor choice unless performance at scale or memory safety for a long-running service are hard requirements. A hybrid approach (Rust host + sidecar for WhatsApp + TypeScript container) is possible but architecturally complex.

---

## 4. Elixir / OTP

### Overall Feasibility: MEDIUM-LOW

Elixir's OTP framework is architecturally the best match for NanoClaw's design patterns: isolated per-group processes, supervised long-running services, fault tolerance, and concurrent message processing. However, critical library gaps make a pure conversion impractical.

### Dependency Mapping

| Node.js Dependency | Elixir Equivalent | Maturity | Notes |
|---------------------|-------------------|----------|-------|
| `@whiskeysockets/baileys` | None | Critical Gap | No Elixir/Erlang WhatsApp Web library. Would need a sidecar (Node/Go) or NIF binding to `whatsmeow`. |
| `better-sqlite3` | `Exqlite` or `Ecto.Adapters.SQLite3` | Good | `exqlite` provides raw SQLite bindings. Ecto adds an ORM layer. |
| `claude-agent-sdk` | None | Critical Gap | No Elixir SDK. Would need subprocess bridge. |
| `@modelcontextprotocol/sdk` | None | Critical Gap | No Elixir MCP SDK exists. Would need to implement the protocol or use a bridge. |
| `cron-parser` | `Quantum` or `Oban` | Excellent | `Quantum` for cron scheduling, `Oban` for job queues. Both are production-grade. |
| `pino` | `Logger` (stdlib) | Excellent | Elixir's built-in Logger with JSON backends is excellent. |
| `zod` | Ecto changesets or `Norm` | Good | Different paradigm but effective for validation. |
| Child process mgmt | `System.cmd`, `Port`, `Rambo` | Good | Ports for streaming I/O, `Rambo` for simpler cases. |
| File watching/polling | `FileSystem` hex package or `:timer` polling | Good | OTP timers are natural for polling. |

### Architecture Mapping

| TypeScript Pattern | Elixir/OTP Equivalent | Fit |
|--------------------|----------------------|-----|
| `async/await` with Promises | GenServer `call/cast`, Task.async | Excellent |
| Event emitters | `Phoenix.PubSub`, GenServer callbacks | Excellent |
| `while(true)` polling loops | GenServer with `Process.send_after` | Excellent (more idiomatic) |
| TypeScript interfaces | `@type` specs, structs, behaviours | Good |
| Streaming child process output | Erlang Ports with `{:line}` option | Excellent |
| GroupQueue concurrency control | Per-group GenServer + DynamicSupervisor | Excellent (native pattern) |
| Per-group isolation | Separate OTP processes per group | **Perfect** |
| Graceful shutdown | OTP Supervisor shutdown strategies | **Perfect** |
| Retry with backoff | Built-in supervisor restart strategies | **Perfect** |

### Strengths
- **Architecture is a perfect match.** NanoClaw's design — isolated per-group state, concurrent processing with limits, polling loops, graceful shutdown, retry with backoff — is exactly what OTP was built for. Every GenServer is an isolated "group" with its own state, mailbox, and lifecycle.
- **Fault tolerance.** OTP supervisors would make the system self-healing. If a group's process crashes, only that group restarts. Currently, a crash in `GroupQueue` could affect all groups.
- **Concurrency model.** The BEAM VM handles millions of lightweight processes. The `MAX_CONCURRENT_CONTAINERS` limit would be a simple `Semaphore` or `PoolBoy` pool. No need for the manual GroupQueue.
- **Hot code reloading.** Elixir supports hot code upgrades — deploy changes without stopping WhatsApp connections or active containers.
- **Pattern matching.** Elixir's pattern matching would make IPC message routing, output marker parsing, and state machine logic more concise and readable.
- **Scheduling is native.** `Quantum` or `Oban` provide production-grade cron scheduling with persistence, far more robust than the manual scheduler loop.

### Risks & Challenges
- **Three critical library gaps.** No WhatsApp client, no Claude Agent SDK, and no MCP SDK. This means three separate bridge/sidecar processes, which undermines the simplicity of OTP. The application would become a distributed system coordinator rather than a self-contained service.
- **Smaller ecosystem.** Elixir's package ecosystem (Hex) is much smaller than npm or PyPI. Edge-case libraries may not exist.
- **Team expertise.** Elixir/OTP has a steep learning curve for developers not familiar with functional programming and the actor model. The BEAM VM's operational characteristics (schedulers, memory, GC) require specific knowledge.
- **Apple Container interaction.** Spawning external processes in Elixir uses Erlang Ports, which work differently from typical subprocess APIs. Streaming I/O requires careful handling of port ownership and process linking.
- **Debugging complexity.** OTP's process-based architecture can make debugging harder when issues span multiple processes and supervisors.
- **Deployment.** Elixir releases are self-contained but require the BEAM VM. Mix releases handle this, but it's more complex than a single Node.js process.

### Effort Estimate
- **Core orchestrator (OTP app):** 4-5 weeks (GenServers, supervisors, porting logic)
- **WhatsApp integration:** 2-3 weeks (sidecar bridge + Elixir adapter)
- **Container agent-runner:** Keep in TypeScript (0 effort) or rewrite with bridges: 3-4 weeks
- **MCP bridge:** 1-2 weeks
- **Testing and stabilization:** 2-3 weeks
- **Total: 9-13 weeks** (host only), **12-17 weeks** (full rewrite)

### Verdict
**Architecturally ideal but practically hamstrung.** Elixir/OTP is the best conceptual fit — the codebase would be cleaner, more fault-tolerant, and more maintainable. But the triple library gap (WhatsApp + Claude SDK + MCP) means you'd spend more time building bridges than porting business logic. Only recommended if you're committed to Elixir long-term and willing to invest in maintaining bridge infrastructure.

---

## Comparative Analysis

### Library Ecosystem Coverage

| Dependency | TypeScript (current) | Python | Go | Rust | Elixir |
|-----------|---------------------|--------|-----|------|--------|
| WhatsApp client | Baileys (Good) | neonize (Fair) | whatsmeow (Excellent) | None | None |
| Claude Agent SDK | Official | Official | None | None | None |
| MCP SDK | Official | Official | Community | Early | None |
| SQLite | Excellent | Excellent | Excellent | Excellent | Good |
| Cron parsing | Good | Excellent | Excellent | Good | Excellent |
| Structured logging | Good | Excellent | Excellent | Excellent | Excellent |
| Schema validation | Good (Zod) | Excellent (Pydantic) | Good | Excellent (serde) | Good |

### Development Effort Comparison

| Metric | Python | Go | Rust | Elixir |
|--------|--------|-----|------|--------|
| Lines of code (est.) | ~5,500 | ~10,500 | ~13,000 | ~4,500 |
| Development time | 5-8 weeks | 5-12 weeks | 10-18 weeks | 9-17 weeks |
| Bridge/sidecar needed | WhatsApp (maybe) | Agent SDK | WhatsApp + Agent SDK | WhatsApp + Agent SDK + MCP |
| Learning curve | Low | Low-Medium | High | High |

### Dimension Scoring (1-5, higher is better)

| Dimension | TypeScript (baseline) | Python | Go | Rust | Elixir |
|-----------|----------------------|--------|-----|------|--------|
| Ecosystem fit | 5 | 4 | 3 | 2 | 2 |
| Concurrency model | 3 | 3 | 5 | 4 | 5 |
| Type safety | 4 | 3 | 4 | 5 | 3 |
| Development speed | 4 | 5 | 3 | 2 | 3 |
| Runtime performance | 3 | 2 | 4 | 5 | 4 |
| Fault tolerance | 2 | 2 | 3 | 4 | 5 |
| Deployment simplicity | 3 | 3 | 5 | 5 | 4 |
| Maintainability | 4 | 4 | 3 | 3 | 4 |
| **Total** | **28** | **26** | **30** | **30** | **30** |

While Go, Rust, and Elixir tie on total score, their strengths serve different priorities. The current TypeScript implementation scores highest on ecosystem fit because it matches where the Anthropic SDK ecosystem lives today.

---

## Recommendations

### If You Must Convert: Choose Python
Python has the smallest gap to bridge (only WhatsApp) and the strongest Anthropic SDK support. The container agent-runner would be simpler in Python than in any other language since Claude Code SDK is Python-native.

### If You Want Better Architecture: Consider Go (Hybrid)
Rewrite the host orchestrator in Go for `whatsmeow` (superior WhatsApp library), native concurrency, and single-binary deployment. Keep the container agent-runner in TypeScript or Python where the Claude SDK lives. This gives you the best of both worlds.

### If You Want Maximum Robustness: Consider Elixir (Long-Term)
If you're building NanoClaw as a long-term platform and are willing to invest in bridge infrastructure, Elixir/OTP's supervision trees and per-group process isolation would make the system significantly more resilient. This is a 6+ month investment.

### Default Recommendation: Stay on TypeScript
The current TypeScript implementation is well-structured, reasonably concise at ~6,800 lines, and sits in the exact ecosystem where Anthropic's SDKs are best supported. None of the alternative languages offer a compelling enough advantage to justify the conversion cost, given that:
1. The application is I/O-bound (Claude API latency dominates)
2. The current concurrency model (GroupQueue) works within Node.js constraints
3. Baileys, while imperfect, is functional
4. The Claude Agent SDK and MCP SDK are TypeScript-native

A conversion only makes strategic sense if one of these conditions changes (e.g., Baileys becomes unmaintained, Anthropic releases a Go Agent SDK, or the system needs to scale beyond Node.js's single-thread limitations).
