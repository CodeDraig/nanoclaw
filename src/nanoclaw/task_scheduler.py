"""Task scheduler — polls for due tasks and enqueues them.

Port of src/task-scheduler.ts (219 lines). Periodically checks the database
for scheduled tasks that are due and enqueues them via the GroupQueue.
"""

from __future__ import annotations

import asyncio
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Awaitable, Protocol

from croniter import croniter

from .config import (
    GROUPS_DIR,
    IDLE_TIMEOUT,
    MAIN_GROUP_FOLDER,
    SCHEDULER_POLL_INTERVAL,
    TIMEZONE,
)
from .container_runner import (
    ContainerOutput,
    run_container_agent,
    write_tasks_snapshot,
)
from .db import (
    get_all_tasks,
    get_due_tasks,
    get_task_by_id,
    log_task_run,
    update_task_after_run,
)
from .group_queue import GroupQueue
from .logger import logger
from .types import ContainerInput, RegisteredGroup, ScheduledTask, TaskRunLog


class SchedulerDeps(Protocol):
    """Dependencies injected into the scheduler."""

    def registered_groups(self) -> dict[str, RegisteredGroup]: ...

    def get_sessions(self) -> dict[str, str]: ...

    @property
    def queue(self) -> GroupQueue: ...

    def on_process(
        self,
        chat_id: str,
        proc: asyncio.subprocess.Process,
        container_name: str,
        group_folder: str,
    ) -> None: ...

    async def send_message(self, chat_id: str, text: str) -> None: ...


async def _run_task(task: ScheduledTask, deps: SchedulerDeps) -> None:
    """Execute a single scheduled task in a container."""
    start_time = time.time()
    group_dir = GROUPS_DIR / task.group_folder
    group_dir.mkdir(parents=True, exist_ok=True)

    logger.info("Running scheduled task", task_id=task.id, group=task.group_folder)

    groups = deps.registered_groups()
    group = next(
        (g for g in groups.values() if g.folder == task.group_folder),
        None,
    )

    if group is None:
        logger.error(
            "Group not found for task",
            task_id=task.id,
            group_folder=task.group_folder,
        )
        log_task_run(
            TaskRunLog(
                id="",
                task_id=task.id,
                started_at=datetime.now(timezone.utc).isoformat(),
                status="error",
                error=f"Group not found: {task.group_folder}",
            )
        )
        return

    # Update tasks snapshot
    is_main = task.group_folder == MAIN_GROUP_FOLDER
    all_tasks = get_all_tasks()
    write_tasks_snapshot(
        task.group_folder,
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
            for t in all_tasks
        ],
    )

    result: str | None = None
    error: str | None = None

    # Session handling
    sessions = deps.get_sessions()
    session_id = sessions.get(task.group_folder) if task.context_mode == "group" else None

    # Idle timer
    idle_handle: asyncio.TimerHandle | None = None

    def _reset_idle_timer() -> None:
        nonlocal idle_handle
        if idle_handle:
            idle_handle.cancel()
        loop = asyncio.get_running_loop()
        idle_handle = loop.call_later(
            IDLE_TIMEOUT / 1000,
            lambda: deps.queue.close_stdin(task.chat_id),
        )

    try:
        output = await run_container_agent(
            group,
            ContainerInput(
                prompt=task.prompt,
                session_id=session_id,
                group_folder=task.group_folder,
                chat_id=task.chat_id,
                is_main=is_main,
                is_scheduled_task=True,
            ),
            lambda proc, cn: deps.on_process(task.chat_id, proc, cn, task.group_folder),
            on_output=_make_on_output(task, deps, _reset_idle_timer),
        )

        if idle_handle:
            idle_handle.cancel()

        if output.status == "error":
            error = output.error or "Unknown error"
        elif output.result:
            result = output.result

        elapsed = time.time() - start_time
        logger.info("Task completed", task_id=task.id, duration_seconds=round(elapsed, 1))

    except Exception as err:
        if idle_handle:
            idle_handle.cancel()
        error = str(err)
        logger.error("Task failed", task_id=task.id, error=error)

    duration_ms = int((time.time() - start_time) * 1000)
    log_task_run(
        TaskRunLog(
            id="",
            task_id=task.id,
            started_at=datetime.now(timezone.utc).isoformat(),
            status="error" if error else "success",
            error=error,
        )
    )

    # Calculate next run
    next_run: str | None = None
    if task.schedule_type == "cron":
        cron = croniter(task.schedule_value)
        next_run = datetime.fromtimestamp(cron.get_next(float), tz=timezone.utc).isoformat()
    elif task.schedule_type == "interval":
        ms = int(task.schedule_value)
        next_run = datetime.fromtimestamp(time.time() + ms / 1000, tz=timezone.utc).isoformat()
    # 'once' tasks have no next run

    result_summary = f"Error: {error}" if error else (result[:200] if result else "Completed")
    update_task_after_run(task.id, next_run, result_summary)


def _make_on_output(
    task: ScheduledTask,
    deps: SchedulerDeps,
    reset_idle: Callable[[], None],
) -> Callable[[ContainerOutput], Awaitable[None]]:
    """Create the on_output callback for streaming task results."""
    result_holder: list[str | None] = [None]
    error_holder: list[str | None] = [None]

    async def _on_output(streamed: ContainerOutput) -> None:
        if streamed.result:
            result_holder[0] = streamed.result
            await deps.send_message(task.chat_id, streamed.result)
            reset_idle()
        if streamed.status == "error":
            error_holder[0] = streamed.error or "Unknown error"

    return _on_output


_scheduler_running = False


async def start_scheduler_loop(deps: SchedulerDeps) -> None:
    """Start the scheduler poll loop (runs as an asyncio task)."""
    global _scheduler_running

    if _scheduler_running:
        logger.debug("Scheduler loop already running, skipping duplicate start")
        return

    _scheduler_running = True
    logger.info("Scheduler loop started")

    while _scheduler_running:
        try:
            due_tasks = get_due_tasks()
            if due_tasks:
                logger.info("Found due tasks", count=len(due_tasks))

            for task in due_tasks:
                # Re-check status
                current = get_task_by_id(task.id)
                if not current or current.status != "active":
                    continue

                deps.queue.enqueue_task(
                    current.chat_id,
                    current.id,
                    lambda t=current: _run_task(t, deps),
                )
        except Exception as err:
            logger.error("Error in scheduler loop", error=str(err))

        await asyncio.sleep(SCHEDULER_POLL_INTERVAL)


def stop_scheduler() -> None:
    """Signal the scheduler to stop."""
    global _scheduler_running
    _scheduler_running = False
