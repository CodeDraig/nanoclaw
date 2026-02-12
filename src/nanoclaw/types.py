"""Type definitions for NanoClaw — Pydantic models and Protocol classes."""

from __future__ import annotations

from typing import Any, Protocol, runtime_checkable

from pydantic import BaseModel


# ---------------------------------------------------------------------------
# Channel abstraction
# ---------------------------------------------------------------------------

class OnInboundMessage(Protocol):
    """Callback invoked when an inbound message arrives."""

    def __call__(self, chat_id: str, msg: NewMessage) -> None: ...


class OnChatMetadata(Protocol):
    """Callback invoked when chat metadata is observed."""

    def __call__(self, chat_id: str, timestamp: str) -> None: ...


@runtime_checkable
class Channel(Protocol):
    """Messaging channel protocol (Telegram, etc.)."""

    name: str

    async def connect(self) -> None: ...

    async def send_message(self, chat_id: str, text: str) -> None: ...

    def is_connected(self) -> bool: ...

    async def disconnect(self) -> None: ...

    async def set_typing(self, chat_id: str, is_typing: bool) -> None: ...


# ---------------------------------------------------------------------------
# Data models
# ---------------------------------------------------------------------------

class NewMessage(BaseModel):
    """A message received from the messaging platform."""

    id: str
    chat_id: str
    sender: str
    sender_name: str
    content: str
    timestamp: str
    is_from_me: bool = False


class RegisteredGroup(BaseModel):
    """A group that the bot is configured to monitor and respond in."""

    name: str
    folder: str
    trigger: str
    added_at: str
    requires_trigger: bool | None = None  # None means default (True for non-main)
    container_config: ContainerConfig | None = None


class AdditionalMount(BaseModel):
    """An extra filesystem mount for containers."""

    host_path: str  # Absolute path on host (supports ~ for home)
    container_path: str | None = None  # Defaults to basename of host_path
    readonly: bool = True  # Default: true for safety


class AllowedRoot(BaseModel):
    """An allowed root directory for additional mounts."""

    path: str  # Absolute path or ~ for home (e.g., "~/projects", "/var/repos")
    allow_read_write: bool = False  # Whether read-write mounts are allowed under this root
    description: str | None = None  # Optional description for documentation


class MountAllowlist(BaseModel):
    """Security configuration for additional mounts.

    This file should be stored at ~/.config/nanoclaw/mount-allowlist.json
    and is NOT mounted into any container, making it tamper-proof from agents.
    """

    allowed_roots: list[AllowedRoot] = []
    blocked_patterns: list[str] = []
    non_main_read_only: bool = True  # If true, non-main groups can only mount read-only


class ContainerConfig(BaseModel):
    """Per-group container configuration."""

    additional_mounts: list[AdditionalMount] = []
    timeout: int | None = None  # Default: 300000 (5 minutes)


class ScheduledTask(BaseModel):
    """A scheduled task stored in the database."""

    id: str
    group_folder: str
    chat_id: str
    prompt: str
    schedule_type: str  # 'cron' | 'interval' | 'once'
    schedule_value: str
    context_mode: str  # 'group' | 'isolated'
    next_run: str | None
    status: str  # 'active' | 'paused' | 'completed'
    created_at: str
    last_run: str | None = None
    last_error: str | None = None


class TaskRunLog(BaseModel):
    """Log entry for a task execution."""

    id: str
    task_id: str
    started_at: str
    finished_at: str | None = None
    status: str  # 'running' | 'success' | 'error'
    error: str | None = None


class ContainerInput(BaseModel):
    """Input data sent to the container agent via stdin."""

    prompt: str
    session_id: str | None = None
    group_folder: str
    chat_id: str
    is_main: bool
    is_scheduled_task: bool = False


class ContainerOutput(BaseModel):
    """Output data received from the container agent via stdout."""

    status: str  # 'success' | 'error'
    result: str | None
    new_session_id: str | None = None
    error: str | None = None


class AvailableGroup(BaseModel):
    """A group visible to the bot (registered or not)."""

    chat_id: str
    name: str
    last_activity: str
    is_registered: bool
