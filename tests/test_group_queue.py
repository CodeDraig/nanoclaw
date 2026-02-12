"""Tests for GroupQueue — ported from group-queue.test.ts.

Tests concurrency control, task prioritization, retry backoff,
shutdown, and draining.  All JID → chat_id.
"""

from __future__ import annotations

import asyncio
from unittest.mock import patch

import pytest

import nanoclaw.group_queue as gq_mod
from nanoclaw.group_queue import GroupQueue


@pytest.fixture
def queue(monkeypatch):
    monkeypatch.setattr(gq_mod, "MAX_CONCURRENT_CONTAINERS", 2)
    q = GroupQueue()
    yield q


class TestGroupQueue:
    @pytest.mark.asyncio
    async def test_one_container_per_group(self, queue: GroupQueue):
        """Only one container per group at a time."""
        concurrent_count = 0
        max_concurrent = 0

        async def process(chat_id: str) -> bool:
            nonlocal concurrent_count, max_concurrent
            concurrent_count += 1
            max_concurrent = max(max_concurrent, concurrent_count)
            await asyncio.sleep(0.05)
            concurrent_count -= 1
            return True

        queue.set_process_messages_fn(process)
        queue.enqueue_message_check("-100group1")
        await asyncio.sleep(0)  # let first task start (sets active=True)
        queue.enqueue_message_check("-100group1")

        await asyncio.sleep(0.2)
        assert max_concurrent == 1

    @pytest.mark.asyncio
    async def test_respects_global_concurrency_limit(self, queue: GroupQueue):
        """Respects MAX_CONCURRENT_CONTAINERS (mocked to 2)."""
        active_count = 0
        max_active = 0
        events: list[asyncio.Event] = []

        async def process(chat_id: str) -> bool:
            nonlocal active_count, max_active
            active_count += 1
            max_active = max(max_active, active_count)
            ev = asyncio.Event()
            events.append(ev)
            await ev.wait()
            active_count -= 1
            return True

        queue.set_process_messages_fn(process)

        queue.enqueue_message_check("-100group1")
        queue.enqueue_message_check("-100group2")
        await asyncio.sleep(0)  # let first two start
        queue.enqueue_message_check("-100group3")

        await asyncio.sleep(0.05)
        assert max_active == 2
        assert active_count == 2

        # Free a slot
        events[0].set()
        await asyncio.sleep(0.05)

        # Third should be active now
        assert max_active == 2

    @pytest.mark.asyncio
    async def test_tasks_before_messages(self, queue: GroupQueue):
        """Tasks are drained before message checks."""
        order: list[str] = []
        block_first = asyncio.Event()

        call_count = 0

        async def process(chat_id: str) -> bool:
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                await block_first.wait()
            order.append("messages")
            return True

        queue.set_process_messages_fn(process)
        queue.enqueue_message_check("-100group1")
        await asyncio.sleep(0.01)

        # While first process is active, enqueue task + message
        async def task_fn():
            order.append("task")

        queue.enqueue_task("-100group1", "task-1", task_fn)
        queue.enqueue_message_check("-100group1")

        block_first.set()
        await asyncio.sleep(0.1)

        assert order[0] == "messages"  # first call
        assert order[1] == "task"  # task runs before second message check

    @pytest.mark.asyncio
    async def test_retry_on_failure(self, queue: GroupQueue, monkeypatch):
        """Retries with backoff on failure."""
        call_count = 0

        async def process(chat_id: str) -> bool:
            nonlocal call_count
            call_count += 1
            return False

        monkeypatch.setattr(gq_mod, "BASE_RETRY_SECONDS", 0.05)
        queue.set_process_messages_fn(process)
        queue.enqueue_message_check("-100group1")

        await asyncio.sleep(0.01)
        assert call_count == 1

        # First retry after ~50ms
        await asyncio.sleep(0.08)
        assert call_count == 2

    @pytest.mark.asyncio
    async def test_shutdown_prevents_new_enqueues(self, queue: GroupQueue):
        """After shutdown, new enqueues are ignored."""
        call_count = 0

        async def process(chat_id: str) -> bool:
            nonlocal call_count
            call_count += 1
            return True

        queue.set_process_messages_fn(process)
        await queue.shutdown(100)

        queue.enqueue_message_check("-100group1")
        await asyncio.sleep(0.05)
        assert call_count == 0

    @pytest.mark.asyncio
    async def test_drains_waiting_groups(self, queue: GroupQueue):
        """Waiting groups are drained when slots free up."""
        processed: list[str] = []
        events: list[asyncio.Event] = []

        async def process(chat_id: str) -> bool:
            processed.append(chat_id)
            ev = asyncio.Event()
            events.append(ev)
            await ev.wait()
            return True

        queue.set_process_messages_fn(process)

        queue.enqueue_message_check("-100group1")
        queue.enqueue_message_check("-100group2")
        await asyncio.sleep(0.05)

        queue.enqueue_message_check("-100group3")
        await asyncio.sleep(0.05)
        assert processed == ["-100group1", "-100group2"]

        events[0].set()
        await asyncio.sleep(0.05)
        assert "-100group3" in processed
