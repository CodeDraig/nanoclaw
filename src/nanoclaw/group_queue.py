"""Group queue — manages concurrency and retries for container execution.

Port of src/group-queue.ts (303 lines). One container per group, global
concurrency limit via counter, retry with exponential backoff.

All `groupJid` references replaced with `chat_id` for Telegram.
"""

from __future__ import annotations

import asyncio
import json
import os
import time
from pathlib import Path
from typing import Callable, Awaitable

from .config import DATA_DIR, MAX_CONCURRENT_CONTAINERS
from .logger import logger

MAX_RETRIES = 5
BASE_RETRY_SECONDS = 5.0

ProcessMessagesFn = Callable[[str], Awaitable[bool]]


class _QueuedTask:
    """A task waiting in the queue."""

    __slots__ = ("id", "chat_id", "fn")

    def __init__(self, task_id: str, chat_id: str, fn: Callable[[], Awaitable[None]]) -> None:
        self.id = task_id
        self.chat_id = chat_id
        self.fn = fn


class _GroupState:
    """Per-group runtime state."""

    __slots__ = (
        "active",
        "pending_messages",
        "pending_tasks",
        "process",
        "container_name",
        "group_folder",
        "retry_count",
    )

    def __init__(self) -> None:
        self.active: bool = False
        self.pending_messages: bool = False
        self.pending_tasks: list[_QueuedTask] = []
        self.process: asyncio.subprocess.Process | None = None
        self.container_name: str | None = None
        self.group_folder: str | None = None
        self.retry_count: int = 0


class GroupQueue:
    """Manages per-group container queueing with global concurrency limits."""

    def __init__(self) -> None:
        self._groups: dict[str, _GroupState] = {}
        self._active_count: int = 0
        self._waiting: list[str] = []
        self._process_messages_fn: ProcessMessagesFn | None = None
        self._shutting_down: bool = False

    def _get_group(self, chat_id: str) -> _GroupState:
        state = self._groups.get(chat_id)
        if state is None:
            state = _GroupState()
            self._groups[chat_id] = state
        return state

    def set_process_messages_fn(self, fn: ProcessMessagesFn) -> None:
        self._process_messages_fn = fn

    def enqueue_message_check(self, chat_id: str) -> None:
        """Enqueue a message-processing run for a group."""
        if self._shutting_down:
            return

        state = self._get_group(chat_id)

        if state.active:
            state.pending_messages = True
            logger.debug("Container active, message queued", chat_id=chat_id)
            return

        if self._active_count >= MAX_CONCURRENT_CONTAINERS:
            state.pending_messages = True
            if chat_id not in self._waiting:
                self._waiting.append(chat_id)
            logger.debug(
                "At concurrency limit, message queued",
                chat_id=chat_id,
                active_count=self._active_count,
            )
            return

        asyncio.create_task(self._run_for_group(chat_id, "messages"))

    def enqueue_task(
        self,
        chat_id: str,
        task_id: str,
        fn: Callable[[], Awaitable[None]],
    ) -> None:
        """Enqueue a scheduled task for a group."""
        if self._shutting_down:
            return

        state = self._get_group(chat_id)

        # Prevent double-queuing
        if any(t.id == task_id for t in state.pending_tasks):
            logger.debug("Task already queued, skipping", chat_id=chat_id, task_id=task_id)
            return

        if state.active:
            state.pending_tasks.append(_QueuedTask(task_id, chat_id, fn))
            logger.debug("Container active, task queued", chat_id=chat_id, task_id=task_id)
            return

        if self._active_count >= MAX_CONCURRENT_CONTAINERS:
            state.pending_tasks.append(_QueuedTask(task_id, chat_id, fn))
            if chat_id not in self._waiting:
                self._waiting.append(chat_id)
            logger.debug(
                "At concurrency limit, task queued",
                chat_id=chat_id,
                task_id=task_id,
                active_count=self._active_count,
            )
            return

        asyncio.create_task(self._run_task(chat_id, _QueuedTask(task_id, chat_id, fn)))

    def register_process(
        self,
        chat_id: str,
        proc: asyncio.subprocess.Process,
        container_name: str,
        group_folder: str | None = None,
    ) -> None:
        """Register the running container process for a group."""
        state = self._get_group(chat_id)
        state.process = proc
        state.container_name = container_name
        if group_folder:
            state.group_folder = group_folder

    def send_message(self, chat_id: str, text: str) -> bool:
        """Send a follow-up message to the active container via IPC file."""
        state = self._get_group(chat_id)
        if not state.active or not state.group_folder:
            return False

        input_dir = DATA_DIR / "ipc" / state.group_folder / "input"
        try:
            input_dir.mkdir(parents=True, exist_ok=True)
            filename = f"{int(time.time() * 1000)}-{os.urandom(3).hex()}.json"
            filepath = input_dir / filename
            temp_path = filepath.with_suffix(".json.tmp")
            temp_path.write_text(json.dumps({"type": "message", "text": text}))
            temp_path.rename(filepath)
            return True
        except Exception:
            return False

    def close_stdin(self, chat_id: str) -> None:
        """Signal the active container to wind down via a close sentinel."""
        state = self._get_group(chat_id)
        if not state.active or not state.group_folder:
            return

        input_dir = DATA_DIR / "ipc" / state.group_folder / "input"
        try:
            input_dir.mkdir(parents=True, exist_ok=True)
            (input_dir / "_close").write_text("")
        except Exception:
            pass

    async def _run_for_group(self, chat_id: str, reason: str) -> None:
        """Execute the process-messages function for a group."""
        state = self._get_group(chat_id)
        state.active = True
        state.pending_messages = False
        self._active_count += 1

        logger.debug(
            "Starting container for group",
            chat_id=chat_id,
            reason=reason,
            active_count=self._active_count,
        )

        try:
            if self._process_messages_fn:
                success = await self._process_messages_fn(chat_id)
                if success:
                    state.retry_count = 0
                else:
                    self._schedule_retry(chat_id, state)
        except Exception as err:
            logger.error("Error processing messages for group", chat_id=chat_id, error=str(err))
            self._schedule_retry(chat_id, state)
        finally:
            state.active = False
            state.process = None
            state.container_name = None
            state.group_folder = None
            self._active_count -= 1
            self._drain_group(chat_id)

    async def _run_task(self, chat_id: str, task: _QueuedTask) -> None:
        """Execute a queued task."""
        state = self._get_group(chat_id)
        state.active = True
        self._active_count += 1

        logger.debug(
            "Running queued task",
            chat_id=chat_id,
            task_id=task.id,
            active_count=self._active_count,
        )

        try:
            await task.fn()
        except Exception as err:
            logger.error("Error running task", chat_id=chat_id, task_id=task.id, error=str(err))
        finally:
            state.active = False
            state.process = None
            state.container_name = None
            state.group_folder = None
            self._active_count -= 1
            self._drain_group(chat_id)

    def _schedule_retry(self, chat_id: str, state: _GroupState) -> None:
        """Schedule a retry with exponential backoff."""
        state.retry_count += 1
        if state.retry_count > MAX_RETRIES:
            logger.error(
                "Max retries exceeded, dropping messages",
                chat_id=chat_id,
                retry_count=state.retry_count,
            )
            state.retry_count = 0
            return

        delay = BASE_RETRY_SECONDS * (2 ** (state.retry_count - 1))
        logger.info(
            "Scheduling retry with backoff",
            chat_id=chat_id,
            retry_count=state.retry_count,
            delay_seconds=delay,
        )

        async def _retry() -> None:
            await asyncio.sleep(delay)
            if not self._shutting_down:
                self.enqueue_message_check(chat_id)

        asyncio.create_task(_retry())

    def _drain_group(self, chat_id: str) -> None:
        """Check for pending work after a group finishes."""
        if self._shutting_down:
            return

        state = self._get_group(chat_id)

        # Tasks first (they won't be re-discovered from DB like messages)
        if state.pending_tasks:
            task = state.pending_tasks.pop(0)
            asyncio.create_task(self._run_task(chat_id, task))
            return

        # Then pending messages
        if state.pending_messages:
            asyncio.create_task(self._run_for_group(chat_id, "drain"))
            return

        # Check waiting groups
        self._drain_waiting()

    def _drain_waiting(self) -> None:
        """Start work for groups waiting for a concurrency slot."""
        while self._waiting and self._active_count < MAX_CONCURRENT_CONTAINERS:
            next_id = self._waiting.pop(0)
            state = self._get_group(next_id)

            if state.pending_tasks:
                task = state.pending_tasks.pop(0)
                asyncio.create_task(self._run_task(next_id, task))
            elif state.pending_messages:
                asyncio.create_task(self._run_for_group(next_id, "drain"))

    async def shutdown(self, grace_period_seconds: float = 5.0) -> None:
        """Shut down the queue, detaching active containers."""
        self._shutting_down = True

        active_containers: list[str] = []
        for chat_id, state in self._groups.items():
            if state.process and state.process.returncode is None and state.container_name:
                active_containers.append(state.container_name)

        logger.info(
            "GroupQueue shutting down (containers detached, not killed)",
            active_count=self._active_count,
            detached_containers=active_containers,
        )
