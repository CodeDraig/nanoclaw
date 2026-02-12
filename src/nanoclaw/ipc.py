"""IPC watcher — processes inter-process communication files.

Port of src/ipc.ts (382 lines). Polls per-group IPC directories for
message and task files, processes them with authorization checks,
and delegates actions.

All `chatJid` / `jid` references replaced with `chat_id` for Telegram.
"""

from __future__ import annotations

import asyncio
import json
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Awaitable, Protocol

from croniter import croniter

from .config import (
    ASSISTANT_NAME,
    DATA_DIR,
    IPC_POLL_INTERVAL,
    MAIN_GROUP_FOLDER,
    TIMEZONE,
)
from .db import create_task, delete_task, get_task_by_id, update_task
from .logger import logger
from .types import AvailableGroup, RegisteredGroup, ScheduledTask


class IpcDeps(Protocol):
    """Dependencies injected into the IPC watcher."""

    async def send_message(self, chat_id: str, text: str) -> None: ...

    def registered_groups(self) -> dict[str, RegisteredGroup]: ...

    def register_group(self, chat_id: str, group: RegisteredGroup) -> None: ...

    async def sync_group_metadata(self, force: bool) -> None: ...

    def get_available_groups(self) -> list[AvailableGroup]: ...

    def write_groups_snapshot(
        self,
        group_folder: str,
        is_main: bool,
        available_groups: list[AvailableGroup],
        registered_ids: set[str],
    ) -> None: ...


_ipc_watcher_running = False


async def start_ipc_watcher(deps: IpcDeps) -> None:
    """Start the IPC watcher loop (runs as an asyncio task)."""
    global _ipc_watcher_running

    if _ipc_watcher_running:
        logger.debug("IPC watcher already running, skipping duplicate start")
        return

    _ipc_watcher_running = True
    ipc_base_dir = DATA_DIR / "ipc"
    ipc_base_dir.mkdir(parents=True, exist_ok=True)

    logger.info("IPC watcher started (per-group namespaces)")

    while _ipc_watcher_running:
        try:
            await _process_ipc_files(ipc_base_dir, deps)
        except Exception as err:
            logger.error("Error in IPC watcher loop", error=str(err))

        await asyncio.sleep(IPC_POLL_INTERVAL)


def stop_ipc_watcher() -> None:
    """Signal the IPC watcher to stop."""
    global _ipc_watcher_running
    _ipc_watcher_running = False


async def _process_ipc_files(ipc_base_dir: Path, deps: IpcDeps) -> None:
    """Process all pending IPC files across group directories."""
    try:
        group_folders = [
            d.name
            for d in ipc_base_dir.iterdir()
            if d.is_dir() and d.name != "errors"
        ]
    except Exception as err:
        logger.error("Error reading IPC base directory", error=str(err))
        return

    registered_groups = deps.registered_groups()

    for source_group in group_folders:
        is_main = source_group == MAIN_GROUP_FOLDER
        messages_dir = ipc_base_dir / source_group / "messages"
        tasks_dir = ipc_base_dir / source_group / "tasks"

        # Process messages
        if messages_dir.exists():
            try:
                for file_path in sorted(messages_dir.glob("*.json")):
                    try:
                        data = json.loads(file_path.read_text())
                        if data.get("type") == "message" and data.get("chatId") and data.get("text"):
                            target_chat_id = data["chatId"]
                            target_group = registered_groups.get(target_chat_id)

                            # Authorization check
                            if is_main or (target_group and target_group.folder == source_group):
                                await deps.send_message(
                                    target_chat_id,
                                    f"{ASSISTANT_NAME}: {data['text']}",
                                )
                                logger.info(
                                    "IPC message sent",
                                    chat_id=target_chat_id,
                                    source_group=source_group,
                                )
                            else:
                                logger.warning(
                                    "Unauthorized IPC message attempt blocked",
                                    chat_id=target_chat_id,
                                    source_group=source_group,
                                )
                        file_path.unlink()
                    except Exception as err:
                        logger.error(
                            "Error processing IPC message",
                            file=file_path.name,
                            source_group=source_group,
                            error=str(err),
                        )
                        _move_to_errors(file_path, ipc_base_dir, source_group)
            except Exception as err:
                logger.error(
                    "Error reading IPC messages directory",
                    source_group=source_group,
                    error=str(err),
                )

        # Process tasks
        if tasks_dir.exists():
            try:
                for file_path in sorted(tasks_dir.glob("*.json")):
                    try:
                        data = json.loads(file_path.read_text())
                        await process_task_ipc(data, source_group, is_main, deps)
                        file_path.unlink()
                    except Exception as err:
                        logger.error(
                            "Error processing IPC task",
                            file=file_path.name,
                            source_group=source_group,
                            error=str(err),
                        )
                        _move_to_errors(file_path, ipc_base_dir, source_group)
            except Exception as err:
                logger.error(
                    "Error reading IPC tasks directory",
                    source_group=source_group,
                    error=str(err),
                )


def _move_to_errors(file_path: Path, ipc_base_dir: Path, source_group: str) -> None:
    """Move a failed IPC file to the errors directory."""
    error_dir = ipc_base_dir / "errors"
    error_dir.mkdir(parents=True, exist_ok=True)
    try:
        file_path.rename(error_dir / f"{source_group}-{file_path.name}")
    except Exception:
        pass


async def process_task_ipc(
    data: dict[str, Any],
    source_group: str,
    is_main: bool,
    deps: IpcDeps,
) -> None:
    """Process a single task IPC request.

    Handles: schedule_task, pause_task, resume_task, cancel_task,
             refresh_groups, register_group.
    """
    registered_groups = deps.registered_groups()
    task_type = data.get("type", "")

    if task_type == "schedule_task":
        prompt = data.get("prompt")
        schedule_type = data.get("schedule_type")
        schedule_value = data.get("schedule_value")
        target_chat_id = data.get("targetChatId") or data.get("targetJid")

        if not all([prompt, schedule_type, schedule_value, target_chat_id]):
            return

        target_group = registered_groups.get(target_chat_id)
        if not target_group:
            logger.warning(
                "Cannot schedule task: target group not registered",
                target_chat_id=target_chat_id,
            )
            return

        # Authorization: non-main can only schedule for themselves
        if not is_main and target_group.folder != source_group:
            logger.warning(
                "Unauthorized schedule_task attempt blocked",
                source_group=source_group,
                target_folder=target_group.folder,
            )
            return

        # Calculate next_run
        next_run: str | None = None
        if schedule_type == "cron":
            try:
                cron = croniter(schedule_value)
                next_run = datetime.fromtimestamp(
                    cron.get_next(float), tz=timezone.utc
                ).isoformat()
            except (ValueError, KeyError):
                logger.warning("Invalid cron expression", schedule_value=schedule_value)
                return
        elif schedule_type == "interval":
            ms = int(schedule_value)
            if ms <= 0:
                logger.warning("Invalid interval", schedule_value=schedule_value)
                return
            next_run = datetime.fromtimestamp(
                time.time() + ms / 1000, tz=timezone.utc
            ).isoformat()
        elif schedule_type == "once":
            try:
                scheduled = datetime.fromisoformat(schedule_value)
                next_run = scheduled.isoformat()
            except ValueError:
                logger.warning("Invalid timestamp", schedule_value=schedule_value)
                return

        task_id = f"task-{int(time.time() * 1000)}-{uuid.uuid4().hex[:8]}"
        context_mode = data.get("context_mode", "isolated")
        if context_mode not in ("group", "isolated"):
            context_mode = "isolated"

        create_task(
            ScheduledTask(
                id=task_id,
                group_folder=target_group.folder,
                chat_id=target_chat_id,
                prompt=prompt,
                schedule_type=schedule_type,
                schedule_value=schedule_value,
                context_mode=context_mode,
                next_run=next_run,
                status="active",
                created_at=datetime.now(timezone.utc).isoformat(),
            )
        )
        logger.info(
            "Task created via IPC",
            task_id=task_id,
            source_group=source_group,
            target_folder=target_group.folder,
            context_mode=context_mode,
        )

    elif task_type == "pause_task":
        task_id = data.get("taskId")
        if task_id:
            task = get_task_by_id(task_id)
            if task and (is_main or task.group_folder == source_group):
                update_task(task_id, status="paused")
                logger.info("Task paused via IPC", task_id=task_id, source_group=source_group)
            else:
                logger.warning(
                    "Unauthorized task pause attempt",
                    task_id=task_id,
                    source_group=source_group,
                )

    elif task_type == "resume_task":
        task_id = data.get("taskId")
        if task_id:
            task = get_task_by_id(task_id)
            if task and (is_main or task.group_folder == source_group):
                update_task(task_id, status="active")
                logger.info("Task resumed via IPC", task_id=task_id, source_group=source_group)
            else:
                logger.warning(
                    "Unauthorized task resume attempt",
                    task_id=task_id,
                    source_group=source_group,
                )

    elif task_type == "cancel_task":
        task_id = data.get("taskId")
        if task_id:
            task = get_task_by_id(task_id)
            if task and (is_main or task.group_folder == source_group):
                delete_task(task_id)
                logger.info("Task cancelled via IPC", task_id=task_id, source_group=source_group)
            else:
                logger.warning(
                    "Unauthorized task cancel attempt",
                    task_id=task_id,
                    source_group=source_group,
                )

    elif task_type == "refresh_groups":
        if is_main:
            logger.info("Group metadata refresh requested via IPC", source_group=source_group)
            await deps.sync_group_metadata(True)
            available_groups = deps.get_available_groups()
            deps.write_groups_snapshot(
                source_group,
                True,
                available_groups,
                set(registered_groups.keys()),
            )
        else:
            logger.warning(
                "Unauthorized refresh_groups attempt blocked",
                source_group=source_group,
            )

    elif task_type == "register_group":
        if not is_main:
            logger.warning(
                "Unauthorized register_group attempt blocked",
                source_group=source_group,
            )
            return

        chat_id = data.get("chatId") or data.get("jid")
        name = data.get("name")
        folder = data.get("folder")
        trigger = data.get("trigger")

        if all([chat_id, name, folder, trigger]):
            deps.register_group(
                chat_id,
                RegisteredGroup(
                    name=name,
                    folder=folder,
                    trigger=trigger,
                    added_at=datetime.now(timezone.utc).isoformat(),
                ),
            )
        else:
            logger.warning("Invalid register_group request — missing required fields", data=data)

    else:
        logger.warning("Unknown IPC task type", type=task_type)
