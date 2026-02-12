"""Tests for db.py — ported from db.test.ts.

All chat_jid references are now chat_id (Telegram chat IDs).
Uses Pydantic models (NewMessage, ScheduledTask) as actual db.py API requires.
"""

from __future__ import annotations

import pytest

from nanoclaw.db import (
    _init_test_database,
    create_task,
    delete_task,
    get_all_chats,
    get_messages_since,
    get_new_messages,
    get_task_by_id,
    store_chat_metadata,
    store_message,
    update_task,
)
from nanoclaw.types import NewMessage, ScheduledTask


@pytest.fixture(autouse=True)
def _setup_db():
    _init_test_database()


def store(
    *,
    id: str,
    chat_id: str,
    sender: str,
    sender_name: str,
    content: str,
    timestamp: str,
    is_from_me: bool = False,
):
    store_message(NewMessage(
        id=id,
        chat_id=chat_id,
        sender=sender,
        sender_name=sender_name,
        content=content,
        timestamp=timestamp,
        is_from_me=is_from_me,
    ))


# ── storeMessage ──────────────────────────────────────────────


class TestStoreMessage:
    def test_stores_and_retrieves(self):
        store_chat_metadata("-1001234567890", "2024-01-01T00:00:00.000Z")
        store(
            id="msg-1",
            chat_id="-1001234567890",
            sender="user-123",
            sender_name="Alice",
            content="hello world",
            timestamp="2024-01-01T00:00:01.000Z",
        )
        messages = get_messages_since("-1001234567890", "2024-01-01T00:00:00.000Z", "BotName")
        assert len(messages) == 1
        assert messages[0].id == "msg-1"
        assert messages[0].sender == "user-123"
        assert messages[0].sender_name == "Alice"
        assert messages[0].content == "hello world"

    def test_stores_empty_content(self):
        store_chat_metadata("-1001234567890", "2024-01-01T00:00:00.000Z")
        store(
            id="msg-2",
            chat_id="-1001234567890",
            sender="user-111",
            sender_name="Dave",
            content="",
            timestamp="2024-01-01T00:00:04.000Z",
        )
        messages = get_messages_since("-1001234567890", "2024-01-01T00:00:00.000Z", "BotName")
        assert len(messages) == 1
        assert messages[0].content == ""

    def test_stores_is_from_me_flag(self):
        store_chat_metadata("-1001234567890", "2024-01-01T00:00:00.000Z")
        store(
            id="msg-3",
            chat_id="-1001234567890",
            sender="me",
            sender_name="Me",
            content="my message",
            timestamp="2024-01-01T00:00:05.000Z",
            is_from_me=True,
        )
        messages = get_messages_since("-1001234567890", "2024-01-01T00:00:00.000Z", "BotName")
        assert len(messages) == 1

    def test_upserts_on_duplicate(self):
        store_chat_metadata("-1001234567890", "2024-01-01T00:00:00.000Z")
        store(
            id="msg-dup",
            chat_id="-1001234567890",
            sender="user-123",
            sender_name="Alice",
            content="original",
            timestamp="2024-01-01T00:00:01.000Z",
        )
        store(
            id="msg-dup",
            chat_id="-1001234567890",
            sender="user-123",
            sender_name="Alice",
            content="updated",
            timestamp="2024-01-01T00:00:01.000Z",
        )
        messages = get_messages_since("-1001234567890", "2024-01-01T00:00:00.000Z", "BotName")
        assert len(messages) == 1
        assert messages[0].content == "updated"


# ── getMessagesSince ──────────────────────────────────────────


class TestGetMessagesSince:
    @pytest.fixture(autouse=True)
    def _seed_messages(self):
        store_chat_metadata("-1001234567890", "2024-01-01T00:00:00.000Z")
        msgs = [
            {"id": "m1", "content": "first", "ts": "2024-01-01T00:00:01.000Z", "sender": "Alice"},
            {"id": "m2", "content": "second", "ts": "2024-01-01T00:00:02.000Z", "sender": "Bob"},
            {"id": "m3", "content": "Andy: bot reply", "ts": "2024-01-01T00:00:03.000Z", "sender": "Bot"},
            {"id": "m4", "content": "third", "ts": "2024-01-01T00:00:04.000Z", "sender": "Carol"},
        ]
        for m in msgs:
            store(
                id=m["id"],
                chat_id="-1001234567890",
                sender=f"user-{m['sender'].lower()}",
                sender_name=m["sender"],
                content=m["content"],
                timestamp=m["ts"],
            )

    def test_returns_messages_after_timestamp(self):
        msgs = get_messages_since("-1001234567890", "2024-01-01T00:00:02.000Z", "Andy")
        # msg m3 "Andy: bot reply" is excluded by bot_prefix filter, so only m4 "third"
        assert len(msgs) == 1
        assert msgs[0].content == "third"

    def test_excludes_assistant_messages(self):
        msgs = get_messages_since("-1001234567890", "2024-01-01T00:00:00.000Z", "Andy")
        bot_msgs = [m for m in msgs if m.content.startswith("Andy:")]
        assert len(bot_msgs) == 0

    def test_returns_all_when_timestamp_empty(self):
        msgs = get_messages_since("-1001234567890", "", "Andy")
        assert len(msgs) == 3  # 3 user messages (bot message excluded)


# ── getNewMessages ────────────────────────────────────────────


class TestGetNewMessages:
    @pytest.fixture(autouse=True)
    def _seed_multi_group(self):
        store_chat_metadata("-100111111", "2024-01-01T00:00:00.000Z")
        store_chat_metadata("-100222222", "2024-01-01T00:00:00.000Z")
        msgs = [
            {"id": "a1", "chat": "-100111111", "content": "g1 msg1", "ts": "2024-01-01T00:00:01.000Z"},
            {"id": "a2", "chat": "-100222222", "content": "g2 msg1", "ts": "2024-01-01T00:00:02.000Z"},
            {"id": "a3", "chat": "-100111111", "content": "Andy: reply", "ts": "2024-01-01T00:00:03.000Z"},
            {"id": "a4", "chat": "-100111111", "content": "g1 msg2", "ts": "2024-01-01T00:00:04.000Z"},
        ]
        for m in msgs:
            store(
                id=m["id"],
                chat_id=m["chat"],
                sender="user-1",
                sender_name="User",
                content=m["content"],
                timestamp=m["ts"],
            )

    def test_returns_messages_across_groups(self):
        messages, new_ts = get_new_messages(["-100111111", "-100222222"], "2024-01-01T00:00:00.000Z", "Andy")
        assert len(messages) == 3
        assert new_ts == "2024-01-01T00:00:04.000Z"

    def test_filters_by_timestamp(self):
        messages, _ts = get_new_messages(["-100111111", "-100222222"], "2024-01-01T00:00:02.000Z", "Andy")
        assert len(messages) == 1
        assert messages[0].content == "g1 msg2"

    def test_empty_for_no_groups(self):
        messages, new_ts = get_new_messages([], "", "Andy")
        assert len(messages) == 0
        assert new_ts == ""


# ── storeChatMetadata ─────────────────────────────────────────


class TestStoreChatMetadata:
    def test_stores_with_default_name(self):
        store_chat_metadata("-1001234567890", "2024-01-01T00:00:00.000Z")
        chats = get_all_chats()
        assert len(chats) == 1
        assert chats[0].chat_id == "-1001234567890"
        assert chats[0].name == "-1001234567890"  # defaults to chat_id

    def test_stores_with_explicit_name(self):
        store_chat_metadata("-1001234567890", "2024-01-01T00:00:00.000Z", "My Group")
        chats = get_all_chats()
        assert chats[0].name == "My Group"

    def test_updates_name(self):
        store_chat_metadata("-1001234567890", "2024-01-01T00:00:00.000Z")
        store_chat_metadata("-1001234567890", "2024-01-01T00:00:01.000Z", "Updated Name")
        chats = get_all_chats()
        assert len(chats) == 1
        assert chats[0].name == "Updated Name"

    def test_preserves_newer_timestamp(self):
        store_chat_metadata("-1001234567890", "2024-01-01T00:00:05.000Z")
        store_chat_metadata("-1001234567890", "2024-01-01T00:00:01.000Z")
        chats = get_all_chats()
        assert chats[0].last_message_time == "2024-01-01T00:00:05.000Z"


# ── Task CRUD ─────────────────────────────────────────────────


class TestTaskCRUD:
    def test_creates_and_retrieves(self):
        create_task(ScheduledTask(
            id="task-1",
            group_folder="main",
            chat_id="-1001234567890",
            prompt="do something",
            schedule_type="once",
            schedule_value="2024-06-01T00:00:00.000Z",
            context_mode="isolated",
            next_run="2024-06-01T00:00:00.000Z",
            status="active",
            created_at="2024-01-01T00:00:00.000Z",
        ))
        task = get_task_by_id("task-1")
        assert task is not None
        assert task.prompt == "do something"
        assert task.status == "active"

    def test_updates_status(self):
        create_task(ScheduledTask(
            id="task-2",
            group_folder="main",
            chat_id="-1001234567890",
            prompt="test",
            schedule_type="once",
            schedule_value="2024-06-01T00:00:00.000Z",
            context_mode="isolated",
            next_run=None,
            status="active",
            created_at="2024-01-01T00:00:00.000Z",
        ))
        update_task("task-2", status="paused")
        assert get_task_by_id("task-2").status == "paused"

    def test_deletes_task(self):
        create_task(ScheduledTask(
            id="task-3",
            group_folder="main",
            chat_id="-1001234567890",
            prompt="delete me",
            schedule_type="once",
            schedule_value="2024-06-01T00:00:00.000Z",
            context_mode="isolated",
            next_run=None,
            status="active",
            created_at="2024-01-01T00:00:00.000Z",
        ))
        delete_task("task-3")
        assert get_task_by_id("task-3") is None
