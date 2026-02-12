"""Stdio MCP Server for NanoClaw — IPC bridge inside the container.

Port of container/agent-runner/src/ipc-mcp-stdio.ts (280 lines).
Standalone process that Claude SDK subagents can inherit.
Reads context from environment variables, writes IPC files for the host.

Run by the agent-runner as an MCP server process.
"""

from __future__ import annotations

import json
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

from mcp.server import Server
from mcp.server.stdio import stdio_server
from mcp.types import TextContent, Tool

IPC_DIR = Path("/workspace/ipc")
MESSAGES_DIR = IPC_DIR / "messages"
TASKS_DIR = IPC_DIR / "tasks"

# Context from environment variables (set by the agent runner)
chat_id = os.environ.get("NANOCLAW_CHAT_ID", "")
group_folder = os.environ.get("NANOCLAW_GROUP_FOLDER", "")
is_main = os.environ.get("NANOCLAW_IS_MAIN", "0") == "1"


def write_ipc_file(directory: Path, data: dict) -> str:
    """Write an IPC file atomically."""
    directory.mkdir(parents=True, exist_ok=True)
    filename = f"{int(time.time() * 1000)}-{os.urandom(4).hex()}.json"
    filepath = directory / filename
    temp_path = filepath.with_suffix(".json.tmp")
    temp_path.write_text(json.dumps(data, indent=2))
    temp_path.rename(filepath)
    return filename


# ──────────────────────────────────────────────────────────────
# MCP Server
# ──────────────────────────────────────────────────────────────

app = Server("nanoclaw")


@app.list_tools()
async def list_tools() -> list[Tool]:
    """List available MCP tools."""
    return [
        Tool(
            name="send_message",
            description=(
                "Send a message to the user or group immediately while you're "
                "still running. Use this for progress updates or to send "
                "multiple messages. Note: when running as a scheduled task, "
                "your final output is NOT sent to the user — use this tool."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "text": {"type": "string", "description": "The message text to send"},
                    "sender": {
                        "type": "string",
                        "description": "Your role/identity name (e.g. 'Researcher')",
                    },
                },
                "required": ["text"],
            },
        ),
        Tool(
            name="schedule_task",
            description=(
                "Schedule a recurring or one-time task. The task will run as "
                "a full agent with access to all tools.\n\n"
                "CONTEXT MODE:\n"
                "• 'group': runs with chat history\n"
                "• 'isolated': fresh session (include context in prompt)\n\n"
                "SCHEDULE VALUE FORMAT (local timezone):\n"
                "• cron: '0 9 * * *' (daily 9am)\n"
                "• interval: '300000' (5 minutes in ms)\n"
                "• once: '2026-02-01T15:30:00' (no Z suffix)"
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "prompt": {"type": "string", "description": "What the agent should do"},
                    "schedule_type": {
                        "type": "string",
                        "enum": ["cron", "interval", "once"],
                        "description": "Schedule type",
                    },
                    "schedule_value": {
                        "type": "string",
                        "description": "Schedule value (cron/ms/timestamp)",
                    },
                    "context_mode": {
                        "type": "string",
                        "enum": ["group", "isolated"],
                        "default": "group",
                        "description": "Context mode",
                    },
                    "target_group_chat_id": {
                        "type": "string",
                        "description": "(Main only) Chat ID of target group",
                    },
                },
                "required": ["prompt", "schedule_type", "schedule_value"],
            },
        ),
        Tool(
            name="list_tasks",
            description="List all scheduled tasks.",
            inputSchema={"type": "object", "properties": {}},
        ),
        Tool(
            name="pause_task",
            description="Pause a scheduled task.",
            inputSchema={
                "type": "object",
                "properties": {"task_id": {"type": "string", "description": "Task ID to pause"}},
                "required": ["task_id"],
            },
        ),
        Tool(
            name="resume_task",
            description="Resume a paused task.",
            inputSchema={
                "type": "object",
                "properties": {"task_id": {"type": "string", "description": "Task ID to resume"}},
                "required": ["task_id"],
            },
        ),
        Tool(
            name="cancel_task",
            description="Cancel and delete a scheduled task.",
            inputSchema={
                "type": "object",
                "properties": {"task_id": {"type": "string", "description": "Task ID to cancel"}},
                "required": ["task_id"],
            },
        ),
        Tool(
            name="register_group",
            description=(
                "Register a new Telegram group so the agent can respond to "
                "messages there. Main group only."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "chat_id": {"type": "string", "description": "Telegram chat ID"},
                    "name": {"type": "string", "description": "Display name"},
                    "folder": {"type": "string", "description": "Folder name (lowercase, hyphens)"},
                    "trigger": {"type": "string", "description": "Trigger word (e.g. '@Andy')"},
                },
                "required": ["chat_id", "name", "folder", "trigger"],
            },
        ),
    ]


@app.call_tool()
async def call_tool(name: str, arguments: dict) -> list[TextContent]:
    """Handle MCP tool calls."""
    now = datetime.now(timezone.utc).isoformat()

    if name == "send_message":
        data = {
            "type": "message",
            "chatId": chat_id,
            "text": arguments["text"],
            "sender": arguments.get("sender"),
            "groupFolder": group_folder,
            "timestamp": now,
        }
        write_ipc_file(MESSAGES_DIR, data)
        return [TextContent(type="text", text="Message sent.")]

    elif name == "schedule_task":
        # Validate schedule_value
        stype = arguments["schedule_type"]
        sval = arguments["schedule_value"]

        if stype == "cron":
            try:
                from croniter import croniter
                croniter(sval)
            except Exception:
                return [TextContent(
                    type="text",
                    text=f'Invalid cron: "{sval}". Use format like "0 9 * * *".',
                )]
        elif stype == "interval":
            try:
                ms = int(sval)
                if ms <= 0:
                    raise ValueError
            except (ValueError, TypeError):
                return [TextContent(
                    type="text",
                    text=f'Invalid interval: "{sval}". Must be positive milliseconds.',
                )]
        elif stype == "once":
            try:
                datetime.fromisoformat(sval)
            except ValueError:
                return [TextContent(
                    type="text",
                    text=f'Invalid timestamp: "{sval}". Use ISO 8601 format.',
                )]

        target = (
            arguments.get("target_group_chat_id")
            if is_main and arguments.get("target_group_chat_id")
            else chat_id
        )

        data = {
            "type": "schedule_task",
            "prompt": arguments["prompt"],
            "schedule_type": stype,
            "schedule_value": sval,
            "context_mode": arguments.get("context_mode", "group"),
            "targetChatId": target,
            "createdBy": group_folder,
            "timestamp": now,
        }
        filename = write_ipc_file(TASKS_DIR, data)
        return [TextContent(type="text", text=f"Task scheduled ({filename}): {stype} - {sval}")]

    elif name == "list_tasks":
        tasks_file = IPC_DIR / "current_tasks.json"
        try:
            if not tasks_file.exists():
                return [TextContent(type="text", text="No scheduled tasks found.")]

            all_tasks = json.loads(tasks_file.read_text())
            tasks = all_tasks if is_main else [t for t in all_tasks if t.get("groupFolder") == group_folder]

            if not tasks:
                return [TextContent(type="text", text="No scheduled tasks found.")]

            formatted = "\n".join(
                f"- [{t['id']}] {t['prompt'][:50]}... ({t['schedule_type']}: {t['schedule_value']}) - {t['status']}, next: {t.get('next_run', 'N/A')}"
                for t in tasks
            )
            return [TextContent(type="text", text=f"Scheduled tasks:\n{formatted}")]
        except Exception as err:
            return [TextContent(type="text", text=f"Error reading tasks: {err}")]

    elif name == "pause_task":
        data = {
            "type": "pause_task",
            "taskId": arguments["task_id"],
            "groupFolder": group_folder,
            "isMain": is_main,
            "timestamp": now,
        }
        write_ipc_file(TASKS_DIR, data)
        return [TextContent(type="text", text=f"Task {arguments['task_id']} pause requested.")]

    elif name == "resume_task":
        data = {
            "type": "resume_task",
            "taskId": arguments["task_id"],
            "groupFolder": group_folder,
            "isMain": is_main,
            "timestamp": now,
        }
        write_ipc_file(TASKS_DIR, data)
        return [TextContent(type="text", text=f"Task {arguments['task_id']} resume requested.")]

    elif name == "cancel_task":
        data = {
            "type": "cancel_task",
            "taskId": arguments["task_id"],
            "groupFolder": group_folder,
            "isMain": is_main,
            "timestamp": now,
        }
        write_ipc_file(TASKS_DIR, data)
        return [TextContent(type="text", text=f"Task {arguments['task_id']} cancellation requested.")]

    elif name == "register_group":
        if not is_main:
            return [TextContent(type="text", text="Only the main group can register new groups.")]

        data = {
            "type": "register_group",
            "chatId": arguments["chat_id"],
            "name": arguments["name"],
            "folder": arguments["folder"],
            "trigger": arguments["trigger"],
            "timestamp": now,
        }
        write_ipc_file(TASKS_DIR, data)
        return [TextContent(
            type="text",
            text=f"Group \"{arguments['name']}\" registered. It will start receiving messages immediately.",
        )]

    return [TextContent(type="text", text=f"Unknown tool: {name}")]


async def run() -> None:
    """Start the MCP server with stdio transport."""
    async with stdio_server() as (read_stream, write_stream):
        await app.run(read_stream, write_stream, app.create_initialization_options())


if __name__ == "__main__":
    import asyncio
    asyncio.run(run())
