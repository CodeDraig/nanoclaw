"""SQLite database layer for NanoClaw.

Direct port of src/db.ts — all chat_jid references converted to chat_id
for Telegram integration.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .config import DATA_DIR, STORE_DIR
from .logger import logger
from .types import NewMessage, RegisteredGroup, ScheduledTask, TaskRunLog

# Module-level database connection
_db: sqlite3.Connection | None = None


def _get_db() -> sqlite3.Connection:
    """Get the active database connection (must call init_database first)."""
    if _db is None:
        raise RuntimeError("Database not initialised — call init_database() first")
    return _db


def _create_schema(database: sqlite3.Connection) -> None:
    """Create all tables and indexes if they don't exist."""
    database.executescript("""
        CREATE TABLE IF NOT EXISTS chats (
            chat_id TEXT PRIMARY KEY,
            name TEXT,
            last_message_time TEXT
        );

        CREATE TABLE IF NOT EXISTS messages (
            id TEXT,
            chat_id TEXT,
            sender TEXT,
            sender_name TEXT,
            content TEXT,
            timestamp TEXT,
            is_from_me INTEGER,
            PRIMARY KEY (id, chat_id),
            FOREIGN KEY (chat_id) REFERENCES chats(chat_id)
        );
        CREATE INDEX IF NOT EXISTS idx_timestamp ON messages(timestamp);

        CREATE TABLE IF NOT EXISTS scheduled_tasks (
            id TEXT PRIMARY KEY,
            group_folder TEXT NOT NULL,
            chat_id TEXT NOT NULL,
            prompt TEXT NOT NULL,
            schedule_type TEXT NOT NULL,
            schedule_value TEXT NOT NULL,
            context_mode TEXT DEFAULT 'isolated',
            next_run TEXT,
            last_run TEXT,
            last_result TEXT,
            status TEXT DEFAULT 'active',
            created_at TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_next_run ON scheduled_tasks(next_run);
        CREATE INDEX IF NOT EXISTS idx_status ON scheduled_tasks(status);

        CREATE TABLE IF NOT EXISTS task_run_logs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            task_id TEXT NOT NULL,
            run_at TEXT NOT NULL,
            duration_ms INTEGER NOT NULL,
            status TEXT NOT NULL,
            result TEXT,
            error TEXT,
            FOREIGN KEY (task_id) REFERENCES scheduled_tasks(id)
        );
        CREATE INDEX IF NOT EXISTS idx_task_run_logs ON task_run_logs(task_id, run_at);

        CREATE TABLE IF NOT EXISTS router_state (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS sessions (
            group_folder TEXT PRIMARY KEY,
            session_id TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS registered_groups (
            chat_id TEXT PRIMARY KEY,
            name TEXT NOT NULL,
            folder TEXT NOT NULL UNIQUE,
            trigger_pattern TEXT NOT NULL,
            added_at TEXT NOT NULL,
            container_config TEXT,
            requires_trigger INTEGER DEFAULT 1
        );
    """)


def init_database() -> None:
    """Initialise the production database (SQLite on disk)."""
    global _db
    db_path = STORE_DIR / "messages.db"
    db_path.parent.mkdir(parents=True, exist_ok=True)

    _db = sqlite3.connect(str(db_path))
    _db.row_factory = sqlite3.Row
    _create_schema(_db)

    # Migrate from JSON files if they exist
    _migrate_json_state()


def _init_test_database() -> None:
    """Create a fresh in-memory database (for tests only)."""
    global _db
    _db = sqlite3.connect(":memory:")
    _db.row_factory = sqlite3.Row
    _create_schema(_db)


# ---------------------------------------------------------------------------
# Chat metadata
# ---------------------------------------------------------------------------


def store_chat_metadata(
    chat_id: str,
    timestamp: str,
    name: str | None = None,
) -> None:
    """Store chat metadata only (no message content)."""
    db = _get_db()
    if name:
        db.execute(
            """
            INSERT INTO chats (chat_id, name, last_message_time) VALUES (?, ?, ?)
            ON CONFLICT(chat_id) DO UPDATE SET
                name = excluded.name,
                last_message_time = MAX(last_message_time, excluded.last_message_time)
            """,
            (chat_id, name, timestamp),
        )
    else:
        db.execute(
            """
            INSERT INTO chats (chat_id, name, last_message_time) VALUES (?, ?, ?)
            ON CONFLICT(chat_id) DO UPDATE SET
                last_message_time = MAX(last_message_time, excluded.last_message_time)
            """,
            (chat_id, chat_id, timestamp),
        )
    db.commit()


def update_chat_name(chat_id: str, name: str) -> None:
    """Update chat name without changing timestamp for existing chats."""
    db = _get_db()
    now = datetime.now(timezone.utc).isoformat()
    db.execute(
        """
        INSERT INTO chats (chat_id, name, last_message_time) VALUES (?, ?, ?)
        ON CONFLICT(chat_id) DO UPDATE SET name = excluded.name
        """,
        (chat_id, name, now),
    )
    db.commit()


class ChatInfo:
    """Lightweight chat info (from db row)."""

    def __init__(self, chat_id: str, name: str, last_message_time: str) -> None:
        self.chat_id = chat_id
        self.name = name
        self.last_message_time = last_message_time


def get_all_chats() -> list[ChatInfo]:
    """Get all known chats, ordered by most recent activity."""
    db = _get_db()
    rows = db.execute(
        "SELECT chat_id, name, last_message_time FROM chats ORDER BY last_message_time DESC"
    ).fetchall()
    return [ChatInfo(r["chat_id"], r["name"], r["last_message_time"]) for r in rows]


# ---------------------------------------------------------------------------
# Messages
# ---------------------------------------------------------------------------


def store_message(msg: NewMessage) -> None:
    """Store a message with full content (for registered groups)."""
    db = _get_db()
    db.execute(
        """INSERT OR REPLACE INTO messages
           (id, chat_id, sender, sender_name, content, timestamp, is_from_me)
           VALUES (?, ?, ?, ?, ?, ?, ?)""",
        (
            msg.id,
            msg.chat_id,
            msg.sender,
            msg.sender_name,
            msg.content,
            msg.timestamp,
            1 if msg.is_from_me else 0,
        ),
    )
    db.commit()


def get_new_messages(
    chat_ids: list[str],
    last_timestamp: str,
    bot_prefix: str,
) -> tuple[list[NewMessage], str]:
    """Get new messages across multiple chats since a timestamp."""
    if not chat_ids:
        return [], last_timestamp

    placeholders = ",".join("?" for _ in chat_ids)
    sql = f"""
        SELECT id, chat_id, sender, sender_name, content, timestamp
        FROM messages
        WHERE timestamp > ? AND chat_id IN ({placeholders}) AND content NOT LIKE ?
        ORDER BY timestamp
    """
    db = _get_db()
    rows = db.execute(sql, [last_timestamp, *chat_ids, f"{bot_prefix}:%"]).fetchall()

    messages: list[NewMessage] = []
    new_timestamp = last_timestamp
    for row in rows:
        msg = NewMessage(
            id=row["id"],
            chat_id=row["chat_id"],
            sender=row["sender"],
            sender_name=row["sender_name"],
            content=row["content"],
            timestamp=row["timestamp"],
        )
        messages.append(msg)
        if row["timestamp"] > new_timestamp:
            new_timestamp = row["timestamp"]

    return messages, new_timestamp


def get_messages_since(
    chat_id: str,
    since_timestamp: str,
    bot_prefix: str,
) -> list[NewMessage]:
    """Get messages for a single chat since a timestamp."""
    db = _get_db()
    rows = db.execute(
        """
        SELECT id, chat_id, sender, sender_name, content, timestamp
        FROM messages
        WHERE chat_id = ? AND timestamp > ? AND content NOT LIKE ?
        ORDER BY timestamp
        """,
        (chat_id, since_timestamp, f"{bot_prefix}:%"),
    ).fetchall()

    return [
        NewMessage(
            id=row["id"],
            chat_id=row["chat_id"],
            sender=row["sender"],
            sender_name=row["sender_name"],
            content=row["content"],
            timestamp=row["timestamp"],
        )
        for row in rows
    ]


# ---------------------------------------------------------------------------
# Scheduled tasks
# ---------------------------------------------------------------------------


def create_task(task: ScheduledTask) -> None:
    """Insert a new scheduled task."""
    db = _get_db()
    db.execute(
        """
        INSERT INTO scheduled_tasks
            (id, group_folder, chat_id, prompt, schedule_type, schedule_value,
             context_mode, next_run, status, created_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            task.id,
            task.group_folder,
            task.chat_id,
            task.prompt,
            task.schedule_type,
            task.schedule_value,
            task.context_mode or "isolated",
            task.next_run,
            task.status,
            task.created_at,
        ),
    )
    db.commit()


def get_task_by_id(task_id: str) -> ScheduledTask | None:
    """Get a scheduled task by ID."""
    db = _get_db()
    row = db.execute("SELECT * FROM scheduled_tasks WHERE id = ?", (task_id,)).fetchone()
    if row is None:
        return None
    return _row_to_task(row)


def get_tasks_for_group(group_folder: str) -> list[ScheduledTask]:
    """Get all tasks for a specific group folder."""
    db = _get_db()
    rows = db.execute(
        "SELECT * FROM scheduled_tasks WHERE group_folder = ? ORDER BY created_at DESC",
        (group_folder,),
    ).fetchall()
    return [_row_to_task(r) for r in rows]


def get_all_tasks() -> list[ScheduledTask]:
    """Get all scheduled tasks."""
    db = _get_db()
    rows = db.execute("SELECT * FROM scheduled_tasks ORDER BY created_at DESC").fetchall()
    return [_row_to_task(r) for r in rows]


def update_task(task_id: str, **updates: Any) -> None:
    """Update one or more fields on a scheduled task.

    Accepts keyword arguments: prompt, schedule_type, schedule_value, next_run, status.
    """
    allowed = {"prompt", "schedule_type", "schedule_value", "next_run", "status"}
    fields: list[str] = []
    values: list[Any] = []

    for key, val in updates.items():
        if key not in allowed:
            raise ValueError(f"Unknown update field: {key}")
        fields.append(f"{key} = ?")
        values.append(val)

    if not fields:
        return

    values.append(task_id)
    db = _get_db()
    db.execute(
        f"UPDATE scheduled_tasks SET {', '.join(fields)} WHERE id = ?",
        values,
    )
    db.commit()


def delete_task(task_id: str) -> None:
    """Delete a scheduled task and its run logs."""
    db = _get_db()
    db.execute("DELETE FROM task_run_logs WHERE task_id = ?", (task_id,))
    db.execute("DELETE FROM scheduled_tasks WHERE id = ?", (task_id,))
    db.commit()


def get_due_tasks() -> list[ScheduledTask]:
    """Get tasks whose next_run has passed."""
    now = datetime.now(timezone.utc).isoformat()
    db = _get_db()
    rows = db.execute(
        """
        SELECT * FROM scheduled_tasks
        WHERE status = 'active' AND next_run IS NOT NULL AND next_run <= ?
        ORDER BY next_run
        """,
        (now,),
    ).fetchall()
    return [_row_to_task(r) for r in rows]


def update_task_after_run(
    task_id: str,
    next_run: str | None,
    last_result: str,
) -> None:
    """Update task after it has been executed."""
    now = datetime.now(timezone.utc).isoformat()
    db = _get_db()
    db.execute(
        """
        UPDATE scheduled_tasks
        SET next_run = ?, last_run = ?, last_result = ?,
            status = CASE WHEN ? IS NULL THEN 'completed' ELSE status END
        WHERE id = ?
        """,
        (next_run, now, last_result, next_run, task_id),
    )
    db.commit()


def log_task_run(log: TaskRunLog) -> None:
    """Insert a task run log entry."""
    db = _get_db()
    db.execute(
        """
        INSERT INTO task_run_logs (task_id, run_at, duration_ms, status, result, error)
        VALUES (?, ?, ?, ?, ?, ?)
        """,
        (log.task_id, log.started_at, 0, log.status, None, log.error),
    )
    db.commit()


def _row_to_task(row: sqlite3.Row) -> ScheduledTask:
    """Convert a db row to a ScheduledTask model."""
    return ScheduledTask(
        id=row["id"],
        group_folder=row["group_folder"],
        chat_id=row["chat_id"],
        prompt=row["prompt"],
        schedule_type=row["schedule_type"],
        schedule_value=row["schedule_value"],
        context_mode=row["context_mode"] or "isolated",
        next_run=row["next_run"],
        status=row["status"],
        created_at=row["created_at"],
        last_run=row["last_run"],
    )


# ---------------------------------------------------------------------------
# Router state
# ---------------------------------------------------------------------------


def get_router_state(key: str) -> str | None:
    """Get a value from the router_state key-value store."""
    db = _get_db()
    row = db.execute("SELECT value FROM router_state WHERE key = ?", (key,)).fetchone()
    return row["value"] if row else None


def set_router_state(key: str, value: str) -> None:
    """Set a value in the router_state key-value store."""
    db = _get_db()
    db.execute("INSERT OR REPLACE INTO router_state (key, value) VALUES (?, ?)", (key, value))
    db.commit()


# ---------------------------------------------------------------------------
# Sessions
# ---------------------------------------------------------------------------


def get_session(group_folder: str) -> str | None:
    """Get the Claude session ID for a group folder."""
    db = _get_db()
    row = db.execute(
        "SELECT session_id FROM sessions WHERE group_folder = ?", (group_folder,)
    ).fetchone()
    return row["session_id"] if row else None


def set_session(group_folder: str, session_id: str) -> None:
    """Store a Claude session ID for a group folder."""
    db = _get_db()
    db.execute(
        "INSERT OR REPLACE INTO sessions (group_folder, session_id) VALUES (?, ?)",
        (group_folder, session_id),
    )
    db.commit()


def get_all_sessions() -> dict[str, str]:
    """Get all group_folder → session_id mappings."""
    db = _get_db()
    rows = db.execute("SELECT group_folder, session_id FROM sessions").fetchall()
    return {r["group_folder"]: r["session_id"] for r in rows}


# ---------------------------------------------------------------------------
# Registered groups
# ---------------------------------------------------------------------------


def get_registered_group(chat_id: str) -> RegisteredGroup | None:
    """Get a registered group by its chat ID."""
    db = _get_db()
    row = db.execute("SELECT * FROM registered_groups WHERE chat_id = ?", (chat_id,)).fetchone()
    if row is None:
        return None
    return _row_to_registered_group(row)


def set_registered_group(chat_id: str, group: RegisteredGroup) -> None:
    """Insert or update a registered group."""
    db = _get_db()
    container_config_json: str | None = None
    if group.container_config is not None:
        container_config_json = group.container_config.model_dump_json()

    requires_trigger_val: int | None
    if group.requires_trigger is None:
        requires_trigger_val = 1
    else:
        requires_trigger_val = 1 if group.requires_trigger else 0

    db.execute(
        """INSERT OR REPLACE INTO registered_groups
           (chat_id, name, folder, trigger_pattern, added_at, container_config, requires_trigger)
           VALUES (?, ?, ?, ?, ?, ?, ?)""",
        (
            chat_id,
            group.name,
            group.folder,
            group.trigger,
            group.added_at,
            container_config_json,
            requires_trigger_val,
        ),
    )
    db.commit()


def get_all_registered_groups() -> dict[str, RegisteredGroup]:
    """Get all registered groups as a dict keyed by chat_id."""
    db = _get_db()
    rows = db.execute("SELECT * FROM registered_groups").fetchall()
    result: dict[str, RegisteredGroup] = {}
    for row in rows:
        result[row["chat_id"]] = _row_to_registered_group(row)
    return result


def _row_to_registered_group(row: sqlite3.Row) -> RegisteredGroup:
    """Convert a db row to a RegisteredGroup model."""
    from .types import ContainerConfig

    container_config = None
    if row["container_config"]:
        container_config = ContainerConfig.model_validate_json(row["container_config"])

    requires_trigger: bool | None
    rt_val = row["requires_trigger"]
    if rt_val is None:
        requires_trigger = None
    else:
        requires_trigger = bool(rt_val)

    return RegisteredGroup(
        name=row["name"],
        folder=row["folder"],
        trigger=row["trigger_pattern"],
        added_at=row["added_at"],
        container_config=container_config,
        requires_trigger=requires_trigger,
    )


# ---------------------------------------------------------------------------
# JSON state migration (one-time, from legacy data dir)
# ---------------------------------------------------------------------------


def _migrate_json_state() -> None:
    """Migrate legacy JSON state files to the database."""

    def _migrate_file(filename: str) -> Any | None:
        file_path = DATA_DIR / filename
        if not file_path.exists():
            return None
        try:
            data = json.loads(file_path.read_text())
            file_path.rename(file_path.with_suffix(file_path.suffix + ".migrated"))
            return data
        except Exception:
            return None

    # Migrate router_state.json
    router_state = _migrate_file("router_state.json")
    if router_state and isinstance(router_state, dict):
        if "last_timestamp" in router_state:
            set_router_state("last_timestamp", router_state["last_timestamp"])
        if "last_agent_timestamp" in router_state:
            set_router_state(
                "last_agent_timestamp",
                json.dumps(router_state["last_agent_timestamp"]),
            )

    # Migrate sessions.json
    sessions = _migrate_file("sessions.json")
    if sessions and isinstance(sessions, dict):
        for folder, session_id in sessions.items():
            set_session(folder, session_id)

    # Migrate registered_groups.json
    groups = _migrate_file("registered_groups.json")
    if groups and isinstance(groups, dict):
        for chat_id, group_data in groups.items():
            group = RegisteredGroup.model_validate(group_data)
            set_registered_group(chat_id, group)
