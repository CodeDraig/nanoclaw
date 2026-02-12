"""NanoClaw Agent Runner — container entry point.

Port of container/agent-runner/src/index.ts (534 lines).
Runs inside a container, receives config via stdin JSON, invokes the
Claude Agent SDK (Python), streams results via OUTPUT markers, and
polls IPC files for follow-up messages.
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

IPC_INPUT_DIR = Path("/workspace/ipc/input")
IPC_INPUT_CLOSE = IPC_INPUT_DIR / "_close"
IPC_POLL_SECONDS = 0.5

OUTPUT_START_MARKER = "---NANOCLAW_OUTPUT_START---"
OUTPUT_END_MARKER = "---NANOCLAW_OUTPUT_END---"


# ──────────────────────────────────────────────────────────────
# Helpers
# ──────────────────────────────────────────────────────────────


def log(message: str) -> None:
    """Write to stderr (stdout is reserved for structured output)."""
    print(f"[agent-runner] {message}", file=sys.stderr, flush=True)


def write_output(output: dict[str, Any]) -> None:
    """Write a structured output block to stdout."""
    print(OUTPUT_START_MARKER, flush=True)
    print(json.dumps(output), flush=True)
    print(OUTPUT_END_MARKER, flush=True)


def should_close() -> bool:
    """Check for the _close sentinel."""
    if IPC_INPUT_CLOSE.exists():
        try:
            IPC_INPUT_CLOSE.unlink()
        except Exception:
            pass
        return True
    return False


def drain_ipc_input() -> list[str]:
    """Read and consume all pending IPC input messages."""
    try:
        IPC_INPUT_DIR.mkdir(parents=True, exist_ok=True)
        files = sorted(IPC_INPUT_DIR.glob("*.json"))
        messages: list[str] = []
        for f in files:
            try:
                data = json.loads(f.read_text())
                f.unlink()
                if data.get("type") == "message" and data.get("text"):
                    messages.append(data["text"])
            except Exception as err:
                log(f"Failed to process input file {f.name}: {err}")
                try:
                    f.unlink()
                except Exception:
                    pass
        return messages
    except Exception as err:
        log(f"IPC drain error: {err}")
        return []


async def wait_for_ipc_message() -> str | None:
    """Wait for a new IPC message or _close sentinel."""
    while True:
        if should_close():
            return None
        messages = drain_ipc_input()
        if messages:
            return "\n".join(messages)
        await asyncio.sleep(IPC_POLL_SECONDS)


# ──────────────────────────────────────────────────────────────
# Transcript archiving
# ──────────────────────────────────────────────────────────────


def _sanitize_filename(summary: str) -> str:
    return re.sub(r"^-+|-+$", "", re.sub(r"[^a-z0-9]+", "-", summary.lower()))[:50]


def _format_transcript_markdown(messages: list[dict[str, str]], title: str | None = None) -> str:
    now = datetime.now(timezone.utc)
    lines = [
        f"# {title or 'Conversation'}",
        "",
        f"Archived: {now.strftime('%b %d, %I:%M %p')}",
        "",
        "---",
        "",
    ]
    for msg in messages:
        sender = "User" if msg["role"] == "user" else "Andy"
        content = msg["content"][:2000] + "..." if len(msg["content"]) > 2000 else msg["content"]
        lines.append(f"**{sender}**: {content}")
        lines.append("")
    return "\n".join(lines)


def _parse_transcript(content: str) -> list[dict[str, str]]:
    messages: list[dict[str, str]] = []
    for line in content.split("\n"):
        if not line.strip():
            continue
        try:
            entry = json.loads(line)
            if entry.get("type") == "user" and entry.get("message", {}).get("content"):
                mc = entry["message"]["content"]
                text = mc if isinstance(mc, str) else "".join(c.get("text", "") for c in mc)
                if text:
                    messages.append({"role": "user", "content": text})
            elif entry.get("type") == "assistant" and entry.get("message", {}).get("content"):
                parts = [c["text"] for c in entry["message"]["content"] if c.get("type") == "text"]
                text = "".join(parts)
                if text:
                    messages.append({"role": "assistant", "content": text})
        except (json.JSONDecodeError, KeyError, TypeError):
            pass
    return messages


def _get_session_summary(session_id: str, transcript_path: str) -> str | None:
    project_dir = os.path.dirname(transcript_path)
    index_path = os.path.join(project_dir, "sessions-index.json")
    if not os.path.exists(index_path):
        return None
    try:
        with open(index_path) as f:
            index = json.load(f)
        for entry in index.get("entries", []):
            if entry.get("sessionId") == session_id and entry.get("summary"):
                return entry["summary"]
    except Exception as err:
        log(f"Failed to read sessions index: {err}")
    return None


# ──────────────────────────────────────────────────────────────
# Agent query
# ──────────────────────────────────────────────────────────────


async def run_query(
    prompt: str,
    session_id: str | None,
    mcp_server_path: str,
    container_input: dict[str, Any],
    resume_at: str | None = None,
) -> dict[str, Any]:
    """Run a single Claude SDK query and stream results.

    Uses the claude-code-sdk Python package.
    """
    from claude_code_sdk import query as claude_query, ClaudeCodeOptions

    # Load global CLAUDE.md
    global_claude_md: str | None = None
    global_path = Path("/workspace/global/CLAUDE.md")
    if not container_input.get("isMain", False) and global_path.exists():
        global_claude_md = global_path.read_text()

    options = ClaudeCodeOptions(
        cwd="/workspace/group",
        allowed_tools=[
            "Bash",
            "Read", "Write", "Edit", "Glob", "Grep",
            "WebSearch", "WebFetch",
            "Task", "TaskOutput", "TaskStop",
            "TeamCreate", "TeamDelete", "SendMessage",
            "TodoWrite", "ToolSearch", "Skill",
            "NotebookEdit",
            "mcp__nanoclaw__*",
        ],
        permission_mode="bypassPermissions",
    )

    # Configure MCP server
    if mcp_server_path:
        options.mcp_servers = {
            "nanoclaw": {
                "command": "python",
                "args": [mcp_server_path],
                "env": {
                    "NANOCLAW_CHAT_ID": container_input.get("chatId", ""),
                    "NANOCLAW_GROUP_FOLDER": container_input.get("groupFolder", ""),
                    "NANOCLAW_IS_MAIN": "1" if container_input.get("isMain") else "0",
                },
            },
        }

    if session_id:
        options.resume = session_id

    new_session_id: str | None = None
    last_assistant_uuid: str | None = None
    message_count = 0
    result_count = 0
    closed_during_query = False

    # IPC polling task
    ipc_polling = True

    async def poll_ipc() -> None:
        nonlocal closed_during_query, ipc_polling
        while ipc_polling:
            if should_close():
                log("Close sentinel detected during query")
                closed_during_query = True
                ipc_polling = False
                return
            # Drain and discard during active query
            # (follow-up messages will be piped by the main loop)
            drain_ipc_input()
            await asyncio.sleep(IPC_POLL_SECONDS)

    poll_task = asyncio.create_task(poll_ipc())

    try:
        async for message in claude_query(prompt=prompt, options=options):
            message_count += 1
            msg_type = getattr(message, "type", "unknown")
            log(f"[msg #{message_count}] type={msg_type}")

            if msg_type == "assistant" and hasattr(message, "uuid"):
                last_assistant_uuid = message.uuid

            if msg_type == "system":
                subtype = getattr(message, "subtype", "")
                if subtype == "init":
                    new_session_id = getattr(message, "session_id", None)
                    log(f"Session initialized: {new_session_id}")

            if msg_type == "result":
                result_count += 1
                text_result = getattr(message, "result", None)
                log(f"Result #{result_count}: {(text_result or '')[:200]}")
                write_output({
                    "status": "success",
                    "result": text_result,
                    "newSessionId": new_session_id,
                })
    except Exception as err:
        log(f"Query error: {err}")
        write_output({
            "status": "error",
            "result": None,
            "newSessionId": new_session_id,
            "error": str(err),
        })
    finally:
        ipc_polling = False
        poll_task.cancel()
        try:
            await poll_task
        except asyncio.CancelledError:
            pass

    log(f"Query done. Messages: {message_count}, results: {result_count}, closedDuringQuery: {closed_during_query}")

    return {
        "newSessionId": new_session_id,
        "lastAssistantUuid": last_assistant_uuid,
        "closedDuringQuery": closed_during_query,
    }


# ──────────────────────────────────────────────────────────────
# Main
# ──────────────────────────────────────────────────────────────


async def main() -> None:
    # Read input from stdin
    try:
        stdin_data = sys.stdin.read()
        container_input = json.loads(stdin_data)
        log(f"Received input for group: {container_input.get('groupFolder', 'unknown')}")
    except Exception as err:
        write_output({
            "status": "error",
            "result": None,
            "error": f"Failed to parse input: {err}",
        })
        sys.exit(1)

    mcp_server_path = str(Path(__file__).parent / "ipc_mcp_stdio.py")

    session_id = container_input.get("sessionId")
    IPC_INPUT_DIR.mkdir(parents=True, exist_ok=True)

    # Clean stale _close sentinel
    try:
        IPC_INPUT_CLOSE.unlink()
    except Exception:
        pass

    # Build initial prompt
    prompt = container_input["prompt"]
    if container_input.get("isScheduledTask"):
        prompt = (
            "[SCHEDULED TASK - The following message was sent automatically "
            "and is not coming directly from the user or group.]\n\n" + prompt
        )
    pending = drain_ipc_input()
    if pending:
        log(f"Draining {len(pending)} pending IPC messages into initial prompt")
        prompt += "\n" + "\n".join(pending)

    # Query loop
    resume_at: str | None = None
    try:
        while True:
            log(f"Starting query (session: {session_id or 'new'}, resumeAt: {resume_at or 'latest'})...")

            result = await run_query(prompt, session_id, mcp_server_path, container_input, resume_at)

            if result.get("newSessionId"):
                session_id = result["newSessionId"]
            if result.get("lastAssistantUuid"):
                resume_at = result["lastAssistantUuid"]

            if result.get("closedDuringQuery"):
                log("Close sentinel consumed during query, exiting")
                break

            # Emit session update
            write_output({"status": "success", "result": None, "newSessionId": session_id})

            log("Query ended, waiting for next IPC message...")
            next_message = await wait_for_ipc_message()
            if next_message is None:
                log("Close sentinel received, exiting")
                break

            log(f"Got new message ({len(next_message)} chars), starting new query")
            prompt = next_message

    except Exception as err:
        log(f"Agent error: {err}")
        write_output({
            "status": "error",
            "result": None,
            "newSessionId": session_id,
            "error": str(err),
        })
        sys.exit(1)


if __name__ == "__main__":
    asyncio.run(main())
