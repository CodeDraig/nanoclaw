"""Configuration constants and environment variable loading."""

from __future__ import annotations

import os
import re
from pathlib import Path

# Assistant identity
ASSISTANT_NAME: str = os.environ.get("ASSISTANT_NAME", "Andy")

# Polling intervals (seconds)
POLL_INTERVAL: float = 2.0
SCHEDULER_POLL_INTERVAL: float = 60.0
IPC_POLL_INTERVAL: float = 1.0

# Paths
PROJECT_ROOT: Path = Path.cwd()
HOME_DIR: Path = Path(os.environ.get("HOME", "/home/user"))

# Mount security: allowlist stored OUTSIDE project root, never mounted into containers
MOUNT_ALLOWLIST_PATH: Path = HOME_DIR / ".config" / "nanoclaw" / "mount-allowlist.json"
STORE_DIR: Path = (PROJECT_ROOT / "store").resolve()
GROUPS_DIR: Path = (PROJECT_ROOT / "groups").resolve()
DATA_DIR: Path = (PROJECT_ROOT / "data").resolve()
MAIN_GROUP_FOLDER: str = "main"

# Container settings
CONTAINER_IMAGE: str = os.environ.get("CONTAINER_IMAGE", "nanoclaw-agent:latest")
CONTAINER_TIMEOUT: int = int(os.environ.get("CONTAINER_TIMEOUT", "1800000"))  # 30min ms
CONTAINER_MAX_OUTPUT_SIZE: int = int(
    os.environ.get("CONTAINER_MAX_OUTPUT_SIZE", "10485760")
)  # 10MB
IDLE_TIMEOUT: int = int(
    os.environ.get("IDLE_TIMEOUT", "1800000")
)  # 30min — how long to keep container alive after last result
MAX_CONCURRENT_CONTAINERS: int = max(
    1, int(os.environ.get("MAX_CONCURRENT_CONTAINERS", "5") or "5")
)

# Telegram bot token
TELEGRAM_BOT_TOKEN: str = os.environ.get("TELEGRAM_BOT_TOKEN", "")


def _escape_regex(s: str) -> str:
    return re.escape(s)


# Trigger pattern: messages starting with @AssistantName
TRIGGER_PATTERN: re.Pattern[str] = re.compile(
    rf"^@{_escape_regex(ASSISTANT_NAME)}\b", re.IGNORECASE
)

# Timezone for scheduled tasks (cron expressions, etc.)
# Uses system timezone by default
TIMEZONE: str = os.environ.get("TZ", "")
if not TIMEZONE:
    try:
        import zoneinfo
        import datetime

        TIMEZONE = datetime.datetime.now(datetime.timezone.utc).astimezone().tzinfo.key  # type: ignore[union-attr]
    except Exception:
        TIMEZONE = "UTC"
