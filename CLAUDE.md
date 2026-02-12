# NanoClaw

Personal Claude assistant. See [README.md](README.md) for philosophy and setup. See [docs/REQUIREMENTS.md](docs/REQUIREMENTS.md) for architecture decisions.

## Quick Context

Single Python process that connects to Telegram Bot API, routes messages to Claude Agent SDK running in Linux containers. Each group has isolated filesystem and memory.

## Key Files

| File                                      | Purpose                                                    |
| ----------------------------------------- | ---------------------------------------------------------- |
| `src/nanoclaw/__main__.py`                | Orchestrator: state, message loop, agent invocation        |
| `src/nanoclaw/channels/telegram.py`       | Telegram Bot API connection, polling, send/receive         |
| `src/nanoclaw/ipc.py`                     | IPC watcher and task processing                            |
| `src/nanoclaw/router.py`                  | Message formatting and outbound routing                    |
| `src/nanoclaw/config.py`                  | Trigger pattern, paths, intervals                          |
| `src/nanoclaw/container_runner.py`        | Spawns agent containers with mounts                        |
| `src/nanoclaw/task_scheduler.py`          | Runs scheduled tasks                                       |
| `src/nanoclaw/db.py`                      | SQLite operations                                          |
| `src/nanoclaw/types.py`                   | Pydantic models and Protocol classes                       |
| `src/nanoclaw/mount_security.py`          | Mount validation and allowlist enforcement                 |
| `src/nanoclaw/group_queue.py`             | Per-group queue with global concurrency limit              |
| `src/nanoclaw/logger.py`                  | Structured logging (structlog)                             |
| `groups/{name}/CLAUDE.md`                 | Per-group memory (isolated)                                |
| `container/agent-runner/main.py`          | Agent entry point inside container                         |
| `container/agent-runner/ipc_mcp_stdio.py` | MCP server for agent tools                                 |
| `container/skills/agent-browser.md`       | Browser automation tool (available to all agents via Bash) |

## Skills

| Skill        | When to Use                                                    |
| ------------ | -------------------------------------------------------------- |
| `/setup`     | First-time installation, authentication, service configuration |
| `/customize` | Adding channels, integrations, changing behavior               |
| `/debug`     | Container issues, logs, troubleshooting                        |

## Development

Run commands directly—don't tell the user to run them.

```bash
pip install -e ".[dev]"  # Install in development mode
python -m nanoclaw       # Run the assistant
pytest                   # Run tests
./container/build.sh     # Rebuild agent container
```

## Container Build Cache

Apple Container's buildkit caches the build context aggressively. `--no-cache` alone does NOT invalidate COPY steps — the builder's volume retains stale files. To force a truly clean rebuild:

```bash
container builder stop && container builder rm && container builder start
./container/build.sh
```
