"""NanoClaw — main orchestrator entry point.

Port of src/index.ts (517 lines). Ties all subsystems together:
Telegram channel, group queue, IPC watcher, task scheduler, and message loop.

Run with: python -m nanoclaw
"""

from __future__ import annotations

import asyncio
import json
import re
import signal
import sys

from .channels.telegram import TelegramChannel
from .config import (
    ASSISTANT_NAME,
    DATA_DIR,
    IDLE_TIMEOUT,
    MAIN_GROUP_FOLDER,
    POLL_INTERVAL,
    TRIGGER_PATTERN,
)
from .container_runner import (
    ContainerOutput,
    ensure_container_system_running,
    run_container_agent,
    write_groups_snapshot,
    write_tasks_snapshot,
)
from .db import (
    get_all_chats,
    get_all_registered_groups,
    get_all_sessions,
    get_all_tasks,
    get_messages_since,
    get_new_messages,
    get_router_state,
    init_database,
    set_registered_group,
    set_router_state,
    set_session,
    store_chat_metadata,
    store_message,
)
from .group_queue import GroupQueue
from .ipc import start_ipc_watcher, stop_ipc_watcher, IpcDeps
from .logger import logger
from .router import format_messages, format_outbound
from .task_scheduler import start_scheduler_loop, stop_scheduler, SchedulerDeps
from .types import (
    AvailableGroup,
    ContainerInput,
    NewMessage,
    RegisteredGroup,
)


# ──────────────────────────────────────────────────────────────
# Module-level state (mirrors the TypeScript globals)
# ──────────────────────────────────────────────────────────────

_last_timestamp: str = ""
_sessions: dict[str, str] = {}
_registered_groups: dict[str, RegisteredGroup] = {}
_last_agent_timestamp: dict[str, str] = {}
_message_loop_running: bool = False

_telegram: TelegramChannel | None = None
_queue: GroupQueue = GroupQueue()


# ──────────────────────────────────────────────────────────────
# State persistence
# ──────────────────────────────────────────────────────────────


def _load_state() -> None:
    global _last_timestamp, _last_agent_timestamp, _sessions, _registered_groups

    _last_timestamp = get_router_state("last_timestamp") or ""

    agent_ts = get_router_state("last_agent_timestamp")
    try:
        _last_agent_timestamp = json.loads(agent_ts) if agent_ts else {}
    except (json.JSONDecodeError, TypeError):
        logger.warning("Corrupted last_agent_timestamp in DB, resetting")
        _last_agent_timestamp = {}

    _sessions = get_all_sessions()
    _registered_groups = get_all_registered_groups()
    logger.info("State loaded", group_count=len(_registered_groups))


def _save_state() -> None:
    set_router_state("last_timestamp", _last_timestamp)
    set_router_state("last_agent_timestamp", json.dumps(_last_agent_timestamp))


# ──────────────────────────────────────────────────────────────
# Group management
# ──────────────────────────────────────────────────────────────


def _register_group(chat_id: str, group: RegisteredGroup) -> None:
    _registered_groups[chat_id] = group
    set_registered_group(chat_id, group)

    group_dir = DATA_DIR.parent / "groups" / group.folder
    (group_dir / "logs").mkdir(parents=True, exist_ok=True)

    logger.info("Group registered", chat_id=chat_id, name=group.name, folder=group.folder)


def _get_available_groups() -> list[AvailableGroup]:
    """Get available groups list for the agent, ordered by most recent activity."""
    chats = get_all_chats()
    registered_ids = set(_registered_groups.keys())

    return [
        AvailableGroup(
            chat_id=c["id"],
            name=c["name"],
            last_activity=c.get("last_message_time", ""),
            is_registered=c["id"] in registered_ids,
        )
        for c in chats
        if c["id"] != "__group_sync__"
    ]


# ──────────────────────────────────────────────────────────────
# Message processing
# ──────────────────────────────────────────────────────────────


async def _process_group_messages(chat_id: str) -> bool:
    """Process all pending messages for a group.

    Called by the GroupQueue when it's this group's turn.
    Returns True on success, False to trigger retry.
    """
    global _last_agent_timestamp

    group = _registered_groups.get(chat_id)
    if not group:
        return True

    is_main = group.folder == MAIN_GROUP_FOLDER
    since = _last_agent_timestamp.get(chat_id, "")
    missed = get_messages_since(chat_id, since, ASSISTANT_NAME)

    if not missed:
        return True

    # Trigger check for non-main groups
    if not is_main and group.requires_trigger is not False:
        has_trigger = any(
            TRIGGER_PATTERN.search(m.content.strip()) for m in missed
        )
        if not has_trigger:
            return True

    prompt = format_messages(missed)

    # Advance cursor, save old for rollback
    previous_cursor = _last_agent_timestamp.get(chat_id, "")
    _last_agent_timestamp[chat_id] = missed[-1].timestamp
    _save_state()

    logger.info("Processing messages", group=group.name, message_count=len(missed))

    # Idle timer
    idle_handle: asyncio.TimerHandle | None = None

    def reset_idle() -> None:
        nonlocal idle_handle
        if idle_handle:
            idle_handle.cancel()
        loop = asyncio.get_running_loop()
        idle_handle = loop.call_later(
            IDLE_TIMEOUT / 1000,
            lambda: _queue.close_stdin(chat_id),
        )

    if _telegram:
        await _telegram.set_typing(chat_id, True)

    had_error = False
    output_sent = False

    async def on_output(result: ContainerOutput) -> None:
        nonlocal had_error, output_sent
        if result.result:
            raw = result.result if isinstance(result.result, str) else json.dumps(result.result)
            text = re.sub(r"<internal>[\s\S]*?</internal>", "", raw).strip()
            logger.info("Agent output", group=group.name, preview=raw[:200])
            if text and _telegram:
                await _telegram.send_message(chat_id, f"{ASSISTANT_NAME}: {text}")
                output_sent = True
            reset_idle()
        if result.status == "error":
            had_error = True

    status = await _run_agent(group, prompt, chat_id, on_output)

    if _telegram:
        await _telegram.set_typing(chat_id, False)
    if idle_handle:
        idle_handle.cancel()

    if status == "error" or had_error:
        if output_sent:
            logger.warning(
                "Agent error after output was sent, skipping cursor rollback",
                group=group.name,
            )
            return True
        _last_agent_timestamp[chat_id] = previous_cursor
        _save_state()
        logger.warning("Agent error, rolled back cursor for retry", group=group.name)
        return False

    return True


async def _run_agent(
    group: RegisteredGroup,
    prompt: str,
    chat_id: str,
    on_output: ContainerOutput | None = None,
) -> str:
    """Run the agent inside a container. Returns 'success' or 'error'."""
    is_main = group.folder == MAIN_GROUP_FOLDER
    session_id = _sessions.get(group.folder)

    # Snapshots
    tasks = get_all_tasks()
    write_tasks_snapshot(
        group.folder,
        is_main,
        [
            {
                "id": t.id,
                "groupFolder": t.group_folder,
                "prompt": t.prompt,
                "schedule_type": t.schedule_type,
                "schedule_value": t.schedule_value,
                "status": t.status,
                "next_run": t.next_run,
            }
            for t in tasks
        ],
    )
    write_groups_snapshot(
        group.folder,
        is_main,
        _get_available_groups(),
        set(_registered_groups.keys()),
    )

    # Wrap on_output to track session ID
    async def wrapped(output: ContainerOutput) -> None:
        if output.new_session_id:
            _sessions[group.folder] = output.new_session_id
            set_session(group.folder, output.new_session_id)
        if on_output:
            await on_output(output)

    try:
        output = await run_container_agent(
            group,
            ContainerInput(
                prompt=prompt,
                session_id=session_id,
                group_folder=group.folder,
                chat_id=chat_id,
                is_main=is_main,
            ),
            lambda proc, cn: _queue.register_process(chat_id, proc, cn, group.folder),
            wrapped,
        )

        if output.new_session_id:
            _sessions[group.folder] = output.new_session_id
            set_session(group.folder, output.new_session_id)

        if output.status == "error":
            logger.error("Container agent error", group=group.name, error=output.error)
            return "error"

        return "success"
    except Exception as err:
        logger.error("Agent error", group=group.name, error=str(err))
        return "error"


# ──────────────────────────────────────────────────────────────
# Message loop
# ──────────────────────────────────────────────────────────────


async def _start_message_loop() -> None:
    """Poll for new messages and dispatch to groups."""
    global _message_loop_running, _last_timestamp

    if _message_loop_running:
        logger.debug("Message loop already running, skipping duplicate start")
        return

    _message_loop_running = True
    logger.info(f"NanoClaw running (trigger: @{ASSISTANT_NAME})")

    while _message_loop_running:
        try:
            chat_ids = list(_registered_groups.keys())
            messages, new_timestamp = get_new_messages(
                chat_ids, _last_timestamp, ASSISTANT_NAME
            )

            if messages:
                logger.info("New messages", count=len(messages))
                _last_timestamp = new_timestamp
                _save_state()

                # Group by chat_id
                by_group: dict[str, list[NewMessage]] = {}
                for msg in messages:
                    by_group.setdefault(msg.chat_id, []).append(msg)

                for cid, group_msgs in by_group.items():
                    group = _registered_groups.get(cid)
                    if not group:
                        continue

                    is_main = group.folder == MAIN_GROUP_FOLDER
                    needs_trigger = not is_main and group.requires_trigger is not False

                    if needs_trigger:
                        has_trigger = any(
                            TRIGGER_PATTERN.search(m.content.strip())
                            for m in group_msgs
                        )
                        if not has_trigger:
                            continue

                    # Pull all pending messages since last agent timestamp
                    all_pending = get_messages_since(
                        cid,
                        _last_agent_timestamp.get(cid, ""),
                        ASSISTANT_NAME,
                    )
                    to_send = all_pending if all_pending else group_msgs
                    formatted = format_messages(to_send)

                    if _queue.send_message(cid, formatted):
                        logger.debug(
                            "Piped messages to active container",
                            chat_id=cid,
                            count=len(to_send),
                        )
                        _last_agent_timestamp[cid] = to_send[-1].timestamp
                        _save_state()
                    else:
                        _queue.enqueue_message_check(cid)
        except Exception as err:
            logger.error("Error in message loop", error=str(err))

        await asyncio.sleep(POLL_INTERVAL / 1000)


# ──────────────────────────────────────────────────────────────
# Recovery
# ──────────────────────────────────────────────────────────────


def _recover_pending_messages() -> None:
    """Startup recovery: enqueue unprocessed messages from registered groups."""
    for chat_id, group in _registered_groups.items():
        since = _last_agent_timestamp.get(chat_id, "")
        pending = get_messages_since(chat_id, since, ASSISTANT_NAME)
        if pending:
            logger.info(
                "Recovery: found unprocessed messages",
                group=group.name,
                pending_count=len(pending),
            )
            _queue.enqueue_message_check(chat_id)


# ──────────────────────────────────────────────────────────────
# IPC dependencies adapter
# ──────────────────────────────────────────────────────────────


class _IpcDepsAdapter:
    """Bridges module-level state to the IpcDeps protocol."""

    async def send_message(self, chat_id: str, text: str) -> None:
        if _telegram:
            await _telegram.send_message(chat_id, text)

    def registered_groups(self) -> dict[str, RegisteredGroup]:
        return _registered_groups

    def register_group(self, chat_id: str, group: RegisteredGroup) -> None:
        _register_group(chat_id, group)

    async def sync_group_metadata(self, force: bool) -> None:
        pass  # Telegram doesn't need periodic group sync

    def get_available_groups(self) -> list[AvailableGroup]:
        return _get_available_groups()

    def write_groups_snapshot(
        self,
        group_folder: str,
        is_main: bool,
        available_groups: list[AvailableGroup],
        registered_ids: set[str],
    ) -> None:
        write_groups_snapshot(group_folder, is_main, available_groups, registered_ids)


class _SchedulerDepsAdapter:
    """Bridges module-level state to the SchedulerDeps protocol."""

    def registered_groups(self) -> dict[str, RegisteredGroup]:
        return _registered_groups

    def get_sessions(self) -> dict[str, str]:
        return _sessions

    @property
    def queue(self) -> GroupQueue:
        return _queue

    def on_process(
        self,
        chat_id: str,
        proc: asyncio.subprocess.Process,
        container_name: str,
        group_folder: str,
    ) -> None:
        _queue.register_process(chat_id, proc, container_name, group_folder)

    async def send_message(self, chat_id: str, text: str) -> None:
        text = format_outbound(text)
        if text and _telegram:
            await _telegram.send_message(chat_id, text)


# ──────────────────────────────────────────────────────────────
# Main
# ──────────────────────────────────────────────────────────────


async def main() -> None:
    """Main orchestrator entry point."""
    global _telegram

    # Check container system
    if not await ensure_container_system_running():
        print(
            "\n╔═══════════════════════════════════════════════════╗\n"
            "║ FATAL: Container system not available              ║\n"
            "║                                                    ║\n"
            "║ Agents cannot run without Apple Container. To fix: ║\n"
            "║ 1. Install: github.com/apple/container/releases    ║\n"
            "║ 2. Run: container system start                     ║\n"
            "║ 3. Restart NanoClaw                                ║\n"
            "╚═══════════════════════════════════════════════════╝\n"
        )
        sys.exit(1)

    # Database
    init_database()
    logger.info("Database initialized")
    _load_state()

    # Graceful shutdown
    loop = asyncio.get_running_loop()

    async def shutdown(sig_name: str) -> None:
        logger.info("Shutdown signal received", signal=sig_name)
        stop_ipc_watcher()
        stop_scheduler()
        await _queue.shutdown()
        if _telegram:
            await _telegram.disconnect()
        loop.stop()

    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(
            sig,
            lambda s=sig: asyncio.create_task(shutdown(s.name)),
        )

    # Create Telegram channel
    _telegram = TelegramChannel(
        on_message=lambda chat_id, msg: store_message(msg),
        on_metadata=lambda chat_id, ts: store_chat_metadata(chat_id, ts),
    )

    await _telegram.connect()

    # Start subsystems
    asyncio.create_task(start_scheduler_loop(_SchedulerDepsAdapter()))
    asyncio.create_task(start_ipc_watcher(_IpcDepsAdapter()))
    _queue.set_process_messages_fn(_process_group_messages)
    _recover_pending_messages()
    await _start_message_loop()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
