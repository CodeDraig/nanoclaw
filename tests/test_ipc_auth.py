"""Tests for IPC authorization — ported from ipc-auth.test.ts.

Tests schedule_task, pause/resume/cancel_task, register_group, and
message authorization logic.  All JID → chat_id.
"""

from __future__ import annotations

import pytest

from nanoclaw.db import (
    _init_test_database,
    create_task,
    get_all_tasks,
    get_registered_group,
    get_task_by_id,
    set_registered_group,
)
from nanoclaw.ipc import process_task_ipc
from nanoclaw.types import RegisteredGroup, ScheduledTask


MAIN_GROUP = RegisteredGroup(
    name="Main", folder="main", trigger="always", added_at="2024-01-01T00:00:00.000Z"
)
OTHER_GROUP = RegisteredGroup(
    name="Other", folder="other-group", trigger="@Andy", added_at="2024-01-01T00:00:00.000Z"
)
THIRD_GROUP = RegisteredGroup(
    name="Third", folder="third-group", trigger="@Andy", added_at="2024-01-01T00:00:00.000Z"
)


def _make_task(**overrides) -> ScheduledTask:
    defaults = dict(
        id="task-1",
        group_folder="main",
        chat_id="-100main",
        prompt="test",
        schedule_type="once",
        schedule_value="2025-06-01T00:00:00.000Z",
        context_mode="isolated",
        next_run="2025-06-01T00:00:00.000Z",
        status="active",
        created_at="2024-01-01T00:00:00.000Z",
    )
    defaults.update(overrides)
    return ScheduledTask(**defaults)


@pytest.fixture(autouse=True)
def _setup():
    _init_test_database()
    set_registered_group("-100main", MAIN_GROUP)
    set_registered_group("-100other", OTHER_GROUP)
    set_registered_group("-100third", THIRD_GROUP)


def _groups():
    return {
        "-100main": MAIN_GROUP,
        "-100other": OTHER_GROUP,
        "-100third": THIRD_GROUP,
    }


class _FakeDeps:
    """Minimal deps stub for process_task_ipc."""

    def __init__(self, groups: dict[str, RegisteredGroup]):
        self._groups = groups

    async def send_message(self, chat_id: str, text: str) -> None:
        pass

    def registered_groups(self) -> dict[str, RegisteredGroup]:
        return self._groups

    def register_group(self, chat_id: str, group: RegisteredGroup) -> None:
        self._groups[chat_id] = group
        set_registered_group(chat_id, group)

    async def sync_group_metadata(self, force: bool) -> None:
        pass

    def get_available_groups(self):
        return []

    def write_groups_snapshot(self, *args, **kwargs):
        pass


def _deps():
    return _FakeDeps(_groups())


# ── schedule_task authorization ───────────────────────────────


class TestScheduleTaskAuth:
    @pytest.mark.asyncio
    async def test_main_schedules_for_other(self):
        await process_task_ipc(
            {"type": "schedule_task", "prompt": "do something", "schedule_type": "once",
             "schedule_value": "2025-06-01T00:00:00.000Z", "targetChatId": "-100other"},
            "main", True, _deps(),
        )
        tasks = get_all_tasks()
        assert len(tasks) == 1
        assert tasks[0].group_folder == "other-group"

    @pytest.mark.asyncio
    async def test_non_main_schedules_for_self(self):
        await process_task_ipc(
            {"type": "schedule_task", "prompt": "self task", "schedule_type": "once",
             "schedule_value": "2025-06-01T00:00:00.000Z", "targetChatId": "-100other"},
            "other-group", False, _deps(),
        )
        tasks = get_all_tasks()
        assert len(tasks) == 1
        assert tasks[0].group_folder == "other-group"

    @pytest.mark.asyncio
    async def test_non_main_cannot_schedule_for_other(self):
        await process_task_ipc(
            {"type": "schedule_task", "prompt": "unauthorized", "schedule_type": "once",
             "schedule_value": "2025-06-01T00:00:00.000Z", "targetChatId": "-100main"},
            "other-group", False, _deps(),
        )
        assert len(get_all_tasks()) == 0

    @pytest.mark.asyncio
    async def test_rejects_unregistered_target(self):
        await process_task_ipc(
            {"type": "schedule_task", "prompt": "no target", "schedule_type": "once",
             "schedule_value": "2025-06-01T00:00:00.000Z", "targetChatId": "-100unknown"},
            "main", True, _deps(),
        )
        assert len(get_all_tasks()) == 0


# ── pause_task authorization ─────────────────────────────────


class TestPauseTaskAuth:
    @pytest.fixture(autouse=True)
    def _seed_tasks(self):
        create_task(_make_task(id="task-main", group_folder="main", chat_id="-100main", prompt="main task"))
        create_task(_make_task(id="task-other", group_folder="other-group", chat_id="-100other", prompt="other task"))

    @pytest.mark.asyncio
    async def test_main_can_pause_any(self):
        await process_task_ipc(
            {"type": "pause_task", "taskId": "task-other"}, "main", True, _deps()
        )
        assert get_task_by_id("task-other").status == "paused"

    @pytest.mark.asyncio
    async def test_non_main_can_pause_own(self):
        await process_task_ipc(
            {"type": "pause_task", "taskId": "task-other"}, "other-group", False, _deps()
        )
        assert get_task_by_id("task-other").status == "paused"

    @pytest.mark.asyncio
    async def test_non_main_cannot_pause_other(self):
        await process_task_ipc(
            {"type": "pause_task", "taskId": "task-main"}, "other-group", False, _deps()
        )
        assert get_task_by_id("task-main").status == "active"


# ── resume_task authorization ─────────────────────────────────


class TestResumeTaskAuth:
    @pytest.fixture(autouse=True)
    def _seed_paused(self):
        create_task(_make_task(id="task-paused", group_folder="other-group", chat_id="-100other", prompt="paused task", status="paused"))

    @pytest.mark.asyncio
    async def test_main_can_resume(self):
        await process_task_ipc(
            {"type": "resume_task", "taskId": "task-paused"}, "main", True, _deps()
        )
        assert get_task_by_id("task-paused").status == "active"

    @pytest.mark.asyncio
    async def test_non_main_can_resume_own(self):
        await process_task_ipc(
            {"type": "resume_task", "taskId": "task-paused"}, "other-group", False, _deps()
        )
        assert get_task_by_id("task-paused").status == "active"

    @pytest.mark.asyncio
    async def test_non_main_cannot_resume_other(self):
        await process_task_ipc(
            {"type": "resume_task", "taskId": "task-paused"}, "third-group", False, _deps()
        )
        assert get_task_by_id("task-paused").status == "paused"


# ── cancel_task authorization ─────────────────────────────────


class TestCancelTaskAuth:
    @pytest.mark.asyncio
    async def test_main_can_cancel_any(self):
        create_task(_make_task(id="task-to-cancel", group_folder="other-group", chat_id="-100other", prompt="cancel me", next_run=None))
        await process_task_ipc(
            {"type": "cancel_task", "taskId": "task-to-cancel"}, "main", True, _deps()
        )
        assert get_task_by_id("task-to-cancel") is None

    @pytest.mark.asyncio
    async def test_non_main_can_cancel_own(self):
        create_task(_make_task(id="task-own", group_folder="other-group", chat_id="-100other", prompt="my task", next_run=None))
        await process_task_ipc(
            {"type": "cancel_task", "taskId": "task-own"}, "other-group", False, _deps()
        )
        assert get_task_by_id("task-own") is None

    @pytest.mark.asyncio
    async def test_non_main_cannot_cancel_other(self):
        create_task(_make_task(id="task-foreign", group_folder="main", chat_id="-100main", prompt="not yours", next_run=None))
        await process_task_ipc(
            {"type": "cancel_task", "taskId": "task-foreign"}, "other-group", False, _deps()
        )
        assert get_task_by_id("task-foreign") is not None


# ── register_group authorization ──────────────────────────────


class TestRegisterGroupAuth:
    @pytest.mark.asyncio
    async def test_non_main_cannot_register(self):
        deps = _deps()
        await process_task_ipc(
            {"type": "register_group", "chatId": "-100new", "name": "New Group",
             "folder": "new-group", "trigger": "@Andy"},
            "other-group", False, deps,
        )
        assert "-100new" not in deps._groups

    @pytest.mark.asyncio
    async def test_main_can_register(self):
        deps = _deps()
        await process_task_ipc(
            {"type": "register_group", "chatId": "-100new", "name": "New Group",
             "folder": "new-group", "trigger": "@Andy"},
            "main", True, deps,
        )
        group = get_registered_group("-100new")
        assert group is not None
        assert group.name == "New Group"
        assert group.folder == "new-group"

    @pytest.mark.asyncio
    async def test_register_rejects_missing_fields(self):
        deps = _deps()
        await process_task_ipc(
            {"type": "register_group", "chatId": "-100partial", "name": "Partial"},
            "main", True, deps,
        )
        assert get_registered_group("-100partial") is None


# ── IPC message authorization ─────────────────────────────────


class TestIpcMessageAuth:
    def _is_authorized(
        self, source_group: str, is_main: bool, target_chat_id: str,
        registered: dict[str, RegisteredGroup],
    ) -> bool:
        target = registered.get(target_chat_id)
        return is_main or (target is not None and target.folder == source_group)

    def test_main_can_send_anywhere(self):
        groups = _groups()
        assert self._is_authorized("main", True, "-100other", groups)
        assert self._is_authorized("main", True, "-100third", groups)

    def test_non_main_can_send_to_own(self):
        assert self._is_authorized("other-group", False, "-100other", _groups())

    def test_non_main_cannot_send_to_other(self):
        groups = _groups()
        assert not self._is_authorized("other-group", False, "-100main", groups)
        assert not self._is_authorized("other-group", False, "-100third", groups)

    def test_non_main_cannot_send_to_unregistered(self):
        assert not self._is_authorized("other-group", False, "-100unknown", _groups())

    def test_main_can_send_to_unregistered(self):
        assert self._is_authorized("main", True, "-100unknown", _groups())


# ── schedule_task schedule types ──────────────────────────────


class TestScheduleTypes:
    @pytest.mark.asyncio
    async def test_cron_computes_next_run(self):
        await process_task_ipc(
            {"type": "schedule_task", "prompt": "cron task", "schedule_type": "cron",
             "schedule_value": "0 9 * * *", "targetChatId": "-100other"},
            "main", True, _deps(),
        )
        tasks = get_all_tasks()
        assert len(tasks) == 1
        assert tasks[0].schedule_type == "cron"
        assert tasks[0].next_run  # truthy

    @pytest.mark.asyncio
    async def test_rejects_invalid_cron(self):
        await process_task_ipc(
            {"type": "schedule_task", "prompt": "bad cron", "schedule_type": "cron",
             "schedule_value": "not a cron", "targetChatId": "-100other"},
            "main", True, _deps(),
        )
        assert len(get_all_tasks()) == 0

    @pytest.mark.asyncio
    async def test_interval_computes_next_run(self):
        await process_task_ipc(
            {"type": "schedule_task", "prompt": "interval task", "schedule_type": "interval",
             "schedule_value": "3600000", "targetChatId": "-100other"},
            "main", True, _deps(),
        )
        tasks = get_all_tasks()
        assert len(tasks) == 1
        assert tasks[0].schedule_type == "interval"

    @pytest.mark.asyncio
    async def test_rejects_non_numeric_interval(self):
        # int("abc") raises ValueError in process_task_ipc
        with pytest.raises(ValueError):
            await process_task_ipc(
                {"type": "schedule_task", "prompt": "bad interval", "schedule_type": "interval",
                 "schedule_value": "abc", "targetChatId": "-100other"},
                "main", True, _deps(),
            )
        assert len(get_all_tasks()) == 0

    @pytest.mark.asyncio
    async def test_rejects_zero_interval(self):
        await process_task_ipc(
            {"type": "schedule_task", "prompt": "zero", "schedule_type": "interval",
             "schedule_value": "0", "targetChatId": "-100other"},
            "main", True, _deps(),
        )
        assert len(get_all_tasks()) == 0

    @pytest.mark.asyncio
    async def test_rejects_invalid_once(self):
        await process_task_ipc(
            {"type": "schedule_task", "prompt": "bad once", "schedule_type": "once",
             "schedule_value": "not-a-date", "targetChatId": "-100other"},
            "main", True, _deps(),
        )
        assert len(get_all_tasks()) == 0


# ── context_mode ──────────────────────────────────────────────


class TestContextMode:
    @pytest.mark.asyncio
    async def test_accepts_group(self):
        await process_task_ipc(
            {"type": "schedule_task", "prompt": "group context", "schedule_type": "once",
             "schedule_value": "2025-06-01T00:00:00.000Z", "context_mode": "group",
             "targetChatId": "-100other"},
            "main", True, _deps(),
        )
        tasks = get_all_tasks()
        assert tasks[0].context_mode == "group"

    @pytest.mark.asyncio
    async def test_accepts_isolated(self):
        await process_task_ipc(
            {"type": "schedule_task", "prompt": "isolated", "schedule_type": "once",
             "schedule_value": "2025-06-01T00:00:00.000Z", "context_mode": "isolated",
             "targetChatId": "-100other"},
            "main", True, _deps(),
        )
        tasks = get_all_tasks()
        assert tasks[0].context_mode == "isolated"

    @pytest.mark.asyncio
    async def test_defaults_invalid_to_isolated(self):
        await process_task_ipc(
            {"type": "schedule_task", "prompt": "bad", "schedule_type": "once",
             "schedule_value": "2025-06-01T00:00:00.000Z", "context_mode": "bogus",
             "targetChatId": "-100other"},
            "main", True, _deps(),
        )
        tasks = get_all_tasks()
        assert tasks[0].context_mode == "isolated"

    @pytest.mark.asyncio
    async def test_defaults_missing_to_isolated(self):
        await process_task_ipc(
            {"type": "schedule_task", "prompt": "no ctx", "schedule_type": "once",
             "schedule_value": "2025-06-01T00:00:00.000Z", "targetChatId": "-100other"},
            "main", True, _deps(),
        )
        tasks = get_all_tasks()
        assert tasks[0].context_mode == "isolated"
