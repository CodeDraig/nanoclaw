# Python + Telegram Rewrite Plan

## Overview

This document is a concrete implementation plan for rewriting NanoClaw from TypeScript/Node.js + WhatsApp to **Python + Telegram**. Replacing WhatsApp with Telegram eliminates the only significant library gap identified in the [language conversion feasibility evaluation](./LANGUAGE_CONVERSION_FEASIBILITY.md), making this a zero-gap conversion where every dependency has a mature, well-supported Python equivalent.

---

## Why This Combination Works

| Current (TypeScript) | Target (Python) | Status |
|---------------------|-----------------|--------|
| `@whiskeysockets/baileys` (unofficial, fragile) | `python-telegram-bot` (official Telegram Bot API) | **Upgrade** |
| `@anthropic-ai/claude-agent-sdk` | `claude-code-sdk` | **Official, Python-first** |
| `@modelcontextprotocol/sdk` | `mcp` (official Python SDK) | **Official, Python-first** |
| `better-sqlite3` | `aiosqlite` + stdlib `sqlite3` | **Equivalent** |
| `cron-parser` | `croniter` | **Equivalent** |
| `pino` | `structlog` | **Equivalent** |
| `zod` | `pydantic` | **Upgrade** (runtime validation + serialization) |
| TypeScript strict mode | `mypy` + `pydantic` | **Comparable** |

Zero bridges. Zero sidecars. Single runtime. Pure Python.

---

## Architecture Mapping

### Current Architecture (TypeScript)

```
┌─────────────────────────────────────────────┐
│  Host Process (Node.js)                     │
│  ┌─────────┐ ┌──────────┐ ┌─────────────┐  │
│  │ index.ts │ │ group-   │ │ task-       │  │
│  │ (main   │ │ queue.ts │ │ scheduler.ts│  │
│  │  loop)  │ │          │ │             │  │
│  └────┬────┘ └────┬─────┘ └──────┬──────┘  │
│       │           │              │          │
│  ┌────┴────┐ ┌────┴─────┐ ┌─────┴───────┐  │
│  │whatsapp │ │container-│ │  ipc.ts     │  │
│  │  .ts    │ │runner.ts │ │  (watcher)  │  │
│  └─────────┘ └──────────┘ └─────────────┘  │
│       │           │              │          │
│  ┌────┴────┐ ┌────┴─────┐ ┌─────┴───────┐  │
│  │ db.ts   │ │ mount-   │ │ router.ts   │  │
│  │         │ │security  │ │             │  │
│  └─────────┘ └──────────┘ └─────────────┘  │
└─────────────────────────────────────────────┘
        │                │
        ▼                ▼
   WhatsApp         Apple Container
   (Baileys)        ┌─────────────────┐
                    │ agent-runner/   │
                    │  index.ts       │
                    │  ipc-mcp-stdio  │
                    │  (Claude SDK)   │
                    └─────────────────┘
```

### Target Architecture (Python)

```
┌─────────────────────────────────────────────┐
│  Host Process (Python / asyncio)            │
│  ┌──────────┐ ┌──────────┐ ┌────────────┐  │
│  │ main.py  │ │ group_   │ │ scheduler  │  │
│  │ (async   │ │ queue.py │ │ .py        │  │
│  │  loop)   │ │          │ │            │  │
│  └────┬─────┘ └────┬─────┘ └─────┬──────┘  │
│       │            │             │          │
│  ┌────┴─────┐ ┌────┴──────┐ ┌───┴────────┐ │
│  │telegram  │ │container_ │ │ ipc.py     │ │
│  │.py       │ │runner.py  │ │ (watcher)  │ │
│  └──────────┘ └───────────┘ └────────────┘ │
│       │            │             │          │
│  ┌────┴─────┐ ┌────┴──────┐ ┌───┴────────┐ │
│  │ db.py    │ │ mount_    │ │ router.py  │ │
│  │          │ │security.py│ │            │ │
│  └──────────┘ └───────────┘ └────────────┘ │
└─────────────────────────────────────────────┘
        │                │
        ▼                ▼
   Telegram          Apple Container
   (Bot API)         ┌─────────────────┐
                     │ agent_runner/   │
                     │  __main__.py    │
                     │  mcp_server.py  │
                     │  (Claude SDK)   │
                     └─────────────────┘
```

The structure remains nearly identical. The main change is replacing the event-driven WhatsApp library with Telegram's `python-telegram-bot`, and converting async patterns from Node.js Promises to Python `asyncio` coroutines.

---

## Module-by-Module Conversion Plan

### Phase 1: Foundation (Week 1)

#### 1.1 Project Scaffolding

```
nanoclaw/
├── pyproject.toml          # Project config (replaces package.json)
├── src/
│   └── nanoclaw/
│       ├── __init__.py
│       ├── main.py         # Entry point (index.ts)
│       ├── config.py       # Configuration (config.ts)
│       ├── models.py       # Pydantic models (types.ts)
│       ├── db.py           # Database layer (db.ts)
│       ├── router.py       # Message formatting (router.ts)
│       ├── logger.py       # Logging setup (logger.ts)
│       ├── channels/
│       │   ├── __init__.py
│       │   └── telegram.py # Telegram channel (whatsapp.ts)
│       ├── container_runner.py  # Container spawning (container-runner.ts)
│       ├── group_queue.py       # Concurrency control (group-queue.ts)
│       ├── ipc.py               # IPC watcher (ipc.ts)
│       ├── task_scheduler.py    # Task scheduling (task-scheduler.ts)
│       └── mount_security.py    # Mount validation (mount-security.ts)
├── container/
│   ├── Dockerfile
│   ├── build.sh
│   └── agent_runner/
│       ├── pyproject.toml
│       └── src/
│           └── agent_runner/
│               ├── __init__.py
│               ├── __main__.py     # Agent executor (index.ts)
│               └── mcp_server.py   # MCP tools (ipc-mcp-stdio.ts)
├── groups/
├── data/
├── store/
└── tests/
```

**`pyproject.toml`** (host):
```toml
[project]
name = "nanoclaw"
version = "0.1.0"
requires-python = ">=3.12"
dependencies = [
    "python-telegram-bot>=22.0",
    "aiosqlite>=0.20.0",
    "croniter>=5.0.0",
    "structlog>=24.0.0",
    "pydantic>=2.10.0",
]

[project.optional-dependencies]
dev = [
    "mypy>=1.14",
    "pytest>=8.0",
    "pytest-asyncio>=0.24.0",
    "ruff>=0.8.0",
]

[project.scripts]
nanoclaw = "nanoclaw.main:run"
```

**`pyproject.toml`** (container agent-runner):
```toml
[project]
name = "nanoclaw-agent-runner"
version = "0.1.0"
requires-python = ">=3.12"
dependencies = [
    "claude-code-sdk>=0.2.34",
    "mcp>=1.0.0",
    "croniter>=5.0.0",
    "pydantic>=2.10.0",
]
```

#### 1.2 `config.py` — Configuration (from `config.ts`, 55 lines)

Direct port. Python `os.environ` replaces `process.env`. `re.compile()` replaces `new RegExp()`.

```python
import os, re
from pathlib import Path

ASSISTANT_NAME = os.environ.get("ASSISTANT_NAME", "Andy")
POLL_INTERVAL = 2.0  # seconds
SCHEDULER_POLL_INTERVAL = 60.0
PROJECT_ROOT = Path.cwd()
HOME_DIR = Path.home()
MOUNT_ALLOWLIST_PATH = HOME_DIR / ".config" / "nanoclaw" / "mount-allowlist.json"
STORE_DIR = PROJECT_ROOT / "store"
GROUPS_DIR = PROJECT_ROOT / "groups"
DATA_DIR = PROJECT_ROOT / "data"
MAIN_GROUP_FOLDER = "main"
CONTAINER_IMAGE = os.environ.get("CONTAINER_IMAGE", "nanoclaw-agent:latest")
CONTAINER_TIMEOUT = int(os.environ.get("CONTAINER_TIMEOUT", "1800000"))
CONTAINER_MAX_OUTPUT_SIZE = int(os.environ.get("CONTAINER_MAX_OUTPUT_SIZE", "10485760"))
IPC_POLL_INTERVAL = 1.0
IDLE_TIMEOUT = int(os.environ.get("IDLE_TIMEOUT", "1800000"))
MAX_CONCURRENT_CONTAINERS = max(1, int(os.environ.get("MAX_CONCURRENT_CONTAINERS", "5")))
TRIGGER_PATTERN = re.compile(rf"^@{re.escape(ASSISTANT_NAME)}\b", re.IGNORECASE)
TIMEZONE = os.environ.get("TZ") or ...  # Use tzlocal or datetime
```

**Estimated lines:** ~40. **Complexity:** Trivial.

#### 1.3 `models.py` — Pydantic Models (from `types.ts`, 101 lines)

Replace TypeScript interfaces with Pydantic `BaseModel` classes. This is an upgrade — Pydantic provides runtime validation and JSON serialization for free.

```python
from pydantic import BaseModel
from typing import Literal
from datetime import datetime

class AdditionalMount(BaseModel):
    host_path: str
    container_path: str | None = None
    readonly: bool = True

class ContainerConfig(BaseModel):
    additional_mounts: list[AdditionalMount] | None = None
    timeout: int | None = None

class RegisteredGroup(BaseModel):
    name: str
    folder: str
    trigger: str
    added_at: str
    container_config: ContainerConfig | None = None
    requires_trigger: bool | None = None

class NewMessage(BaseModel):
    id: str
    chat_jid: str
    sender: str
    sender_name: str
    content: str
    timestamp: str
    is_from_me: bool = False

class ScheduledTask(BaseModel):
    id: str
    group_folder: str
    chat_jid: str
    prompt: str
    schedule_type: Literal["cron", "interval", "once"]
    schedule_value: str
    context_mode: Literal["group", "isolated"] = "isolated"
    next_run: str | None = None
    last_run: str | None = None
    last_result: str | None = None
    status: Literal["active", "paused", "completed"] = "active"
    created_at: str
# ... etc
```

**Estimated lines:** ~80. **Complexity:** Trivial. Direct mapping with bonus validation.

#### 1.4 `logger.py` — Logging (from `logger.ts`, 16 lines)

```python
import structlog
import logging, os

structlog.configure(
    processors=[
        structlog.processors.TimeStamper(fmt="iso"),
        structlog.dev.ConsoleRenderer(),
    ],
    wrapper_class=structlog.make_filtering_bound_logger(
        getattr(logging, os.environ.get("LOG_LEVEL", "INFO").upper(), logging.INFO)
    ),
)
logger = structlog.get_logger()
```

**Estimated lines:** ~15. **Complexity:** Trivial.

---

### Phase 2: Data Layer (Week 1-2)

#### 2.1 `db.py` — SQLite Database (from `db.ts`, 584 lines)

The schema remains identical. Replace `better-sqlite3` synchronous calls with Python's built-in `sqlite3` module (synchronous, used from async context via `asyncio.to_thread` or kept synchronous since SQLite ops are fast and local).

Key changes:
- `Database.prepare().run()` → `cursor.execute()`
- `Database.prepare().get()` → `cursor.execute().fetchone()`
- `Database.prepare().all()` → `cursor.execute().fetchall()`
- Pydantic models for result mapping instead of manual TypeScript casts

The migration logic from JSON files can be dropped if starting fresh, or kept for backwards compatibility with existing data.

**Estimated lines:** ~400. **Complexity:** Medium (lots of SQL, but all mechanical translation).

---

### Phase 3: Channel Layer (Week 2)

#### 3.1 `channels/telegram.py` — Telegram Channel (replaces `whatsapp.ts`, 284 lines)

This is the biggest **simplification** in the rewrite. The Telegram Bot API is official, stable, and far simpler than Baileys' reverse-engineered WhatsApp protocol.

**What goes away entirely:**
- QR code authentication flow → Telegram uses a bot token (one env var)
- `useMultiFileAuthState` credential management → Not needed
- LID→Phone JID translation → Not needed (Telegram has stable chat IDs)
- Group metadata sync with 24h cache → Telegram provides group info on demand
- Reconnection with exponential backoff → `python-telegram-bot` handles this internally
- `whatsapp-auth.ts` standalone auth script → Not needed

**What maps directly:**
- `sendMessage(jid, text)` → `bot.send_message(chat_id, text)`
- `setTyping(jid, true)` → `bot.send_chat_action(chat_id, ChatAction.TYPING)`
- Message event handler → `MessageHandler` with filters
- Group message routing → Same logic, different IDs (Telegram uses `int64` chat IDs)

**Telegram-specific features to leverage:**
- **Bot commands** — `/ask`, `/schedule`, `/status` instead of trigger patterns
- **Inline keyboards** — Task management UI (pause/resume/cancel buttons)
- **Message editing** — Update progress in-place instead of sending new messages
- **Topics/forums** — Map to per-group isolation more naturally
- **File handling** — Telegram supports up to 2GB files natively

```python
from telegram import Update, Bot
from telegram.ext import Application, MessageHandler, CommandHandler, filters

class TelegramChannel:
    name = "telegram"
    prefix_assistant_name = False  # Telegram bots already show their name

    def __init__(self, token: str, on_message, on_chat_metadata, registered_groups):
        self.app = Application.builder().token(token).build()
        self.on_message = on_message
        self.on_chat_metadata = on_chat_metadata
        self.registered_groups = registered_groups

    async def connect(self):
        self.app.add_handler(MessageHandler(filters.TEXT, self._handle_message))
        await self.app.initialize()
        await self.app.start()
        await self.app.updater.start_polling()

    async def _handle_message(self, update: Update, context):
        msg = update.effective_message
        chat_id = str(msg.chat_id)
        # ... route to on_message callback

    async def send_message(self, chat_id: str, text: str):
        await self.app.bot.send_message(int(chat_id), text)

    async def set_typing(self, chat_id: str, is_typing: bool):
        if is_typing:
            await self.app.bot.send_chat_action(int(chat_id), "typing")

    async def disconnect(self):
        await self.app.updater.stop()
        await self.app.stop()
        await self.app.shutdown()
```

**Key design decision:** Telegram uses `int64` chat IDs, not JID strings. The database schema's `chat_jid` column name can stay as-is (it's just a string identifier), or be renamed to `chat_id` during the rewrite. JID references throughout the codebase become chat ID references.

**Estimated lines:** ~120 (down from 284). **Complexity:** Low (simpler than WhatsApp).

---

### Phase 4: Container Management (Week 2-3)

#### 4.1 `container_runner.py` — Container Spawning (from `container-runner.ts`, 657 lines)

The container spawning logic is language-agnostic — it shells out to `container run`. Python's `asyncio.create_subprocess_exec` is a direct replacement for Node's `child_process.spawn`.

Key changes:
- `spawn('container', args)` → `asyncio.create_subprocess_exec('container', *args)`
- Stdout streaming: `proc.stdout.on('data', ...)` → `async for line in proc.stdout`
- Promise-based timeout → `asyncio.wait_for()` or `asyncio.timeout()`
- `writeFileSync` for IPC → `pathlib.Path.write_text()` (or async variant)

The volume mount building logic (`buildVolumeMounts`) is pure path manipulation — direct translation.

**Estimated lines:** ~500. **Complexity:** High (streaming output parsing, timeout management). This is the most complex module to port.

#### 4.2 `group_queue.py` — Concurrency Control (from `group-queue.ts`, 302 lines)

Python's `asyncio` has native primitives that simplify this:
- `MAX_CONCURRENT_CONTAINERS` → `asyncio.Semaphore(5)`
- Manual queue + activeCount tracking → `asyncio.Queue` + semaphore
- `setTimeout` for retry backoff → `asyncio.sleep()` in a task

```python
class GroupQueue:
    def __init__(self):
        self._semaphore = asyncio.Semaphore(MAX_CONCURRENT_CONTAINERS)
        self._groups: dict[str, GroupState] = {}

    async def enqueue_message_check(self, chat_id: str):
        state = self._get_group(chat_id)
        if state.active:
            state.pending_messages = True
            return
        asyncio.create_task(self._run_for_group(chat_id))

    async def _run_for_group(self, chat_id: str):
        async with self._semaphore:
            state = self._get_group(chat_id)
            state.active = True
            try:
                success = await self._process_messages_fn(chat_id)
                if not success:
                    await self._schedule_retry(chat_id, state)
            finally:
                state.active = False
                await self._drain_group(chat_id)
```

**Estimated lines:** ~200. **Complexity:** Medium-High (concurrent state management, but asyncio primitives simplify it).

---

### Phase 5: IPC and Scheduling (Week 3)

#### 5.1 `ipc.py` — IPC Watcher (from `ipc.ts`, 381 lines)

Nearly identical logic. Poll directories for JSON files, process them, delete them. The authorization model (main vs non-main) stays the same.

- `fs.readdirSync` → `Path.iterdir()` or `os.listdir()`
- `setTimeout(processIpcFiles, interval)` → `asyncio.sleep(interval)` in a `while True` loop
- `CronExpressionParser.parse()` → `croniter()`

**Estimated lines:** ~300. **Complexity:** Medium.

#### 5.2 `task_scheduler.py` — Task Scheduling (from `task-scheduler.ts`, 218 lines)

Direct port. The scheduler loop polls `getDueTasks()` and enqueues work to the GroupQueue.

- `cron-parser` → `croniter` (slightly different API but same semantics)
- `setTimeout(loop, interval)` → `asyncio.sleep(interval)` in a `while True` loop

**Estimated lines:** ~180. **Complexity:** Low-Medium.

#### 5.3 `mount_security.py` — Mount Validation (from `mount-security.ts`, 418 lines)

Pure logic with no external dependencies. Path manipulation, allowlist validation, pattern matching.

- `path.resolve()` → `Path.resolve()`
- `fs.realpathSync()` → `Path.resolve()` (Python resolves symlinks)
- `path.relative()` → `Path.relative_to()` with try/except

**Estimated lines:** ~300. **Complexity:** Medium. Straightforward port.

---

### Phase 6: Container Agent-Runner (Week 3-4)

#### 6.1 `agent_runner/__main__.py` — Claude SDK Executor (from `index.ts`, 533 lines)

This is where the Python rewrite shines. The Claude Code SDK (`claude-code-sdk`) is Python-native.

```python
from claude_code_sdk import query, ClaudeCodeOptions

async def run_query(prompt, session_id, mcp_server_path, container_input, resume_at=None):
    options = ClaudeCodeOptions(
        cwd="/workspace/group",
        resume=session_id,
        allowed_tools=["Bash", "Read", "Write", "Edit", "Glob", "Grep", ...],
        permission_mode="bypassPermissions",
        mcp_servers={
            "nanoclaw": {
                "command": "python",
                "args": [mcp_server_path],
                "env": {
                    "NANOCLAW_CHAT_ID": container_input.chat_id,
                    "NANOCLAW_GROUP_FOLDER": container_input.group_folder,
                    "NANOCLAW_IS_MAIN": "1" if container_input.is_main else "0",
                },
            }
        },
    )

    async for message in query(prompt=prompt, options=options):
        if message.type == "result":
            write_output(ContainerOutput(
                status="success",
                result=message.result,
                new_session_id=new_session_id,
            ))
```

The `MessageStream` class (async iterable for streaming input) maps directly to a Python `AsyncGenerator`.

**Estimated lines:** ~400. **Complexity:** High (SDK integration, IPC polling, streaming). But cleaner than TypeScript since the SDK is Python-native.

#### 6.2 `agent_runner/mcp_server.py` — MCP Tool Server (from `ipc-mcp-stdio.ts`, 279 lines)

The official Python MCP SDK (`mcp`) provides the same `McpServer` / `StdioServerTransport` abstractions.

```python
from mcp.server import Server
from mcp.server.stdio import stdio_server

server = Server("nanoclaw")

@server.tool()
async def send_message(text: str, sender: str | None = None) -> str:
    write_ipc_file(MESSAGES_DIR, {
        "type": "message",
        "chatId": chat_id,
        "text": text,
        "sender": sender,
        "timestamp": datetime.now().isoformat(),
    })
    return "Message sent."

@server.tool()
async def schedule_task(prompt: str, schedule_type: str, ...) -> str:
    # ... same logic as TypeScript version
```

**Estimated lines:** ~220. **Complexity:** Medium. Cleaner than TypeScript thanks to Python MCP SDK's decorator syntax.

#### 6.3 Container Dockerfile Update

```dockerfile
FROM python:3.12-slim

# System dependencies (same as current: Chromium, fonts, etc.)
RUN apt-get update && apt-get install -y \
    chromium fonts-liberation fonts-noto-color-emoji \
    libgbm1 libnss3 ... \
    curl git nodejs npm \
    && rm -rf /var/lib/apt/lists/*

# Chromium for agent-browser
ENV AGENT_BROWSER_EXECUTABLE_PATH=/usr/bin/chromium

# Install agent-browser and claude-code globally (still Node.js tools)
RUN npm install -g agent-browser @anthropic-ai/claude-code

# Install Python agent-runner
WORKDIR /app
COPY agent_runner/pyproject.toml .
RUN pip install --no-cache-dir .
COPY agent_runner/ .

# Workspace directories (same as current)
RUN mkdir -p /workspace/group /workspace/global /workspace/extra \
    /workspace/ipc/messages /workspace/ipc/tasks /workspace/ipc/input

# Entrypoint
RUN printf '#!/bin/bash\nset -e\n[ -f /workspace/env-dir/env ] && export $(cat /workspace/env-dir/env | xargs)\ncat > /tmp/input.json\npython -m agent_runner < /tmp/input.json\n' > /app/entrypoint.sh && chmod +x /app/entrypoint.sh

RUN chown -R 1000:1000 /workspace
USER 1000
WORKDIR /workspace/group
ENTRYPOINT ["/app/entrypoint.sh"]
```

**Key note:** The container still needs Node.js installed because `claude-code` and `agent-browser` are Node.js CLI tools invoked by the agent at runtime. The agent-runner itself is Python, but the tools it orchestrates include Node.js binaries. This is unavoidable until Anthropic ships native Python Claude Code tooling.

---

### Phase 7: Integration and Testing (Week 4)

#### 7.1 `main.py` — Orchestrator (from `index.ts`, 516 lines)

The main entry point ties everything together. The conversion is structural:

```python
import asyncio
import signal
from nanoclaw.config import *
from nanoclaw.db import init_database, ...
from nanoclaw.channels.telegram import TelegramChannel
from nanoclaw.group_queue import GroupQueue
from nanoclaw.ipc import start_ipc_watcher
from nanoclaw.task_scheduler import start_scheduler_loop

queue = GroupQueue()

async def main():
    ensure_container_system_running()
    init_database()
    load_state()

    telegram = TelegramChannel(
        token=os.environ["TELEGRAM_BOT_TOKEN"],
        on_message=lambda chat_id, msg: store_message(msg),
        on_chat_metadata=lambda chat_id, ts, name=None: store_chat_metadata(chat_id, ts, name),
        registered_groups=lambda: registered_groups,
    )
    await telegram.connect()

    # Start subsystems as concurrent tasks
    async with asyncio.TaskGroup() as tg:
        tg.create_task(start_message_loop(telegram, queue))
        tg.create_task(start_scheduler_loop(deps))
        tg.create_task(start_ipc_watcher(ipc_deps))

    # Graceful shutdown
    loop = asyncio.get_event_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, lambda: asyncio.create_task(shutdown()))

def run():
    asyncio.run(main())
```

**Estimated lines:** ~400. **Complexity:** Medium.

#### 7.2 `router.py` — Message Formatting (from `router.ts`, 46 lines)

Trivial port. XML escaping and message formatting are pure string operations.

**Estimated lines:** ~35. **Complexity:** Trivial.

#### 7.3 Testing

Port the existing Vitest tests to `pytest` + `pytest-asyncio`. The test structure remains the same — unit tests per module with mocked dependencies.

```bash
pytest tests/ --asyncio-mode=auto
mypy src/nanoclaw --strict
ruff check src/
```

---

## Telegram-Specific Design Decisions

### 1. Chat Identification

WhatsApp uses JIDs (`120363...@g.us`). Telegram uses numeric chat IDs (`-100123456789` for groups, `123456789` for users). The database schema stores these as strings, so no schema change is needed — just different values.

### 2. Trigger Mechanism

WhatsApp required `@Andy` prefix detection because WhatsApp doesn't have native bot commands. Telegram offers two approaches:

**Option A: Bot commands (recommended)**
- `/ask <question>` — Direct query
- `/task <description>` — Schedule a task
- No trigger needed for DMs (bot always responds)
- Groups: bot responds to commands or when mentioned via `@botname`

**Option B: Same trigger pattern**
- Keep `@Andy` detection for group messages
- Use Telegram's `@mention` format natively

Recommendation: **Option A** for groups, always respond in DMs. This is more idiomatic for Telegram and eliminates the trigger pattern complexity.

### 3. Message Prefixing

WhatsApp messages needed `Andy: ` prefixed to distinguish bot messages (since the bot shares the user's WhatsApp account). Telegram bots have their own identity, so the `prefix_assistant_name` flag is `False`. The `Channel` interface already supports this via the existing `prefixAssistantName` field in `types.ts`.

### 4. Agent Swarm Integration

The existing `/add-telegram-swarm` skill creates per-subagent bot identities in Telegram. In the Python rewrite, this can be a first-class feature: each `TeamCreate` subagent gets a dedicated bot token registered in a bot pool, and messages from that subagent appear under its own bot identity. This is naturally supported by Telegram's multi-bot architecture.

### 5. Rich Responses

Telegram supports Markdown in messages. The agent's output can include formatted text, code blocks, and inline links without the plain-text limitations of WhatsApp. The `router.py` module can format agent output as Telegram MarkdownV2.

---

## Data Migration

### Existing Data

If you have an existing NanoClaw deployment:

1. **SQLite database** — Schema is identical. Copy `store/messages.db` as-is. Chat JIDs will no longer match (WhatsApp JIDs vs Telegram IDs), so message history from WhatsApp chats won't link to Telegram chats. But the schema works without modification.

2. **Group folders** — `groups/{name}/` content (CLAUDE.md memory, conversations, logs) carries over directly. Re-register groups with new Telegram chat IDs.

3. **Scheduled tasks** — Will need their `chat_jid` updated to Telegram chat IDs.

4. **Claude sessions** — Session IDs in `data/sessions/` persist across the rewrite. The container agent-runner session format is independent of the host language.

### Clean Start (Recommended)

For a fresh deployment, skip migration entirely. Register groups from scratch in Telegram.

---

## Line Count Estimate

| Module | TypeScript Lines | Python Lines (est.) | Notes |
|--------|-----------------|--------------------|----|
| `config.py` | 55 | ~40 | Slightly shorter |
| `models.py` | 101 | ~80 | Pydantic is more concise |
| `logger.py` | 16 | ~15 | Equivalent |
| `db.py` | 584 | ~400 | Less boilerplate |
| `router.py` | 46 | ~35 | Equivalent |
| `telegram.py` | 284 (whatsapp) | ~120 | Massive simplification |
| `container_runner.py` | 657 | ~500 | Slightly shorter |
| `group_queue.py` | 302 | ~200 | asyncio simplifies |
| `ipc.py` | 381 | ~300 | Equivalent |
| `task_scheduler.py` | 218 | ~180 | Equivalent |
| `mount_security.py` | 418 | ~300 | Slightly shorter |
| `main.py` | 516 | ~400 | Slightly shorter |
| **Container:** | | | |
| `__main__.py` | 533 | ~400 | Python SDK is cleaner |
| `mcp_server.py` | 279 | ~220 | Decorator syntax helps |
| **Total** | **4,390** | **~3,190** | **~27% reduction** |

---

## Timeline

| Week | Milestone | Deliverables |
|------|-----------|-------------|
| 1 | Foundation | Project scaffolding, config, models, logger, db |
| 2 | Channels + Containers | Telegram channel, container runner, group queue |
| 3 | IPC + Agent Runner | IPC watcher, scheduler, mount security, agent runner, MCP server |
| 4 | Integration | Main orchestrator, Dockerfile, end-to-end testing |
| 5 | Stabilization | Bug fixes, edge cases, production hardening |

**Total: ~4-5 weeks** for an experienced Python developer familiar with the existing codebase.

---

## Risks and Mitigations

| Risk | Likelihood | Mitigation |
|------|-----------|-----------|
| `claude-code-sdk` Python API differences from Node SDK | Medium | Read SDK source; both are thin wrappers around the same CLI |
| Apple Container CLI interaction quirks | Low | CLI is language-agnostic; same commands, same behavior |
| Telegram Bot API rate limits | Low | `python-telegram-bot` handles rate limiting internally |
| `asyncio` debugging complexity | Medium | Use `asyncio.TaskGroup` (Python 3.11+) for structured concurrency |
| Container build with both Python + Node.js | Low | Already common pattern; `node:22-slim` base + `pip install` works |

---

## What You Gain

1. **No more TypeScript/JavaScript** — Pure Python host, Python container agent
2. **Official, stable messaging API** — Telegram Bot API vs. reverse-engineered WhatsApp
3. **Python-first Anthropic SDKs** — Claude Code SDK and MCP SDK are primary targets
4. **~27% less code** — Python's expressiveness + Telegram simplicity
5. **Pydantic runtime validation** — Stronger than Zod with automatic serialization
6. **Richer messaging** — Markdown formatting, inline keyboards, file handling up to 2GB
7. **Simpler auth** — One env var (`TELEGRAM_BOT_TOKEN`) vs. QR code ceremony
8. **Bot commands** — Native `/ask`, `/schedule` etc. instead of trigger pattern hacking
