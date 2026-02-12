"""Mount security module for NanoClaw.

Validates additional mounts against an allowlist stored OUTSIDE the project root.
This prevents container agents from modifying security configuration.

Allowlist location: ~/.config/nanoclaw/mount-allowlist.json

Port of src/mount-security.ts (419 lines).
"""

from __future__ import annotations

import json
import os
from pathlib import Path

from .config import MOUNT_ALLOWLIST_PATH
from .logger import logger
from .types import AdditionalMount, AllowedRoot, MountAllowlist

# Cache the allowlist in memory — only reloads on process restart
_cached_allowlist: MountAllowlist | None = None
_allowlist_load_error: str | None = None

# Default blocked patterns — paths that should never be mounted
DEFAULT_BLOCKED_PATTERNS: list[str] = [
    ".ssh",
    ".gnupg",
    ".gpg",
    ".aws",
    ".azure",
    ".gcloud",
    ".kube",
    ".docker",
    "credentials",
    ".env",
    ".netrc",
    ".npmrc",
    ".pypirc",
    "id_rsa",
    "id_ed25519",
    "private_key",
    ".secret",
]


def load_mount_allowlist() -> MountAllowlist | None:
    """Load the mount allowlist from the external config location.

    Returns None if the file doesn't exist or is invalid.
    Result is cached in memory for the lifetime of the process.
    """
    global _cached_allowlist, _allowlist_load_error

    if _cached_allowlist is not None:
        return _cached_allowlist

    if _allowlist_load_error is not None:
        return None

    try:
        if not MOUNT_ALLOWLIST_PATH.exists():
            _allowlist_load_error = f"Mount allowlist not found at {MOUNT_ALLOWLIST_PATH}"
            logger.warning(
                "Mount allowlist not found — additional mounts will be BLOCKED. "
                "Create the file to enable additional mounts.",
                path=str(MOUNT_ALLOWLIST_PATH),
            )
            return None

        content = MOUNT_ALLOWLIST_PATH.read_text()
        raw = json.loads(content)

        # Convert camelCase JSON keys to snake_case for Pydantic
        if "allowedRoots" in raw:
            raw["allowed_roots"] = raw.pop("allowedRoots")
        if "blockedPatterns" in raw:
            raw["blocked_patterns"] = raw.pop("blockedPatterns")
        if "nonMainReadOnly" in raw:
            raw["non_main_read_only"] = raw.pop("nonMainReadOnly")

        # Convert allowedRoot items
        if "allowed_roots" in raw:
            for root in raw["allowed_roots"]:
                if "allowReadWrite" in root:
                    root["allow_read_write"] = root.pop("allowReadWrite")

        allowlist = MountAllowlist.model_validate(raw)

        # Merge with default blocked patterns
        merged = list(set(DEFAULT_BLOCKED_PATTERNS + allowlist.blocked_patterns))
        allowlist.blocked_patterns = merged

        _cached_allowlist = allowlist
        logger.info(
            "Mount allowlist loaded successfully",
            path=str(MOUNT_ALLOWLIST_PATH),
            allowed_roots=len(allowlist.allowed_roots),
            blocked_patterns=len(allowlist.blocked_patterns),
        )
        return _cached_allowlist

    except Exception as err:
        _allowlist_load_error = str(err)
        logger.error(
            "Failed to load mount allowlist — additional mounts will be BLOCKED",
            path=str(MOUNT_ALLOWLIST_PATH),
            error=_allowlist_load_error,
        )
        return None


def _reset_cache() -> None:
    """Reset the cached allowlist (for testing)."""
    global _cached_allowlist, _allowlist_load_error
    _cached_allowlist = None
    _allowlist_load_error = None


def _expand_path(p: str) -> Path:
    """Expand ~ to home directory and resolve to absolute path."""
    home_dir = os.environ.get("HOME", "/home/user")
    if p.startswith("~/"):
        return Path(home_dir) / p[2:]
    if p == "~":
        return Path(home_dir)
    return Path(p).resolve()


def _get_real_path(p: Path) -> Path | None:
    """Get the real path, resolving symlinks. Returns None if path doesn't exist."""
    try:
        return p.resolve(strict=True)
    except (OSError, ValueError):
        return None


def _matches_blocked_pattern(real_path: str, blocked_patterns: list[str]) -> str | None:
    """Check if a path matches any blocked pattern."""
    path_parts = real_path.split(os.sep)

    for pattern in blocked_patterns:
        # Check if any path component matches the pattern
        for part in path_parts:
            if part == pattern or pattern in part:
                return pattern

        # Also check if the full path contains the pattern
        if pattern in real_path:
            return pattern

    return None


def _find_allowed_root(
    real_path: str,
    allowed_roots: list[AllowedRoot],
) -> AllowedRoot | None:
    """Check if a real path is under an allowed root."""
    for root in allowed_roots:
        expanded_root = _expand_path(root.path)
        real_root = _get_real_path(expanded_root)

        if real_root is None:
            continue

        # Check if real_path is under real_root
        try:
            Path(real_path).relative_to(real_root)
            return root
        except ValueError:
            continue

    return None


def _is_valid_container_path(container_path: str) -> bool:
    """Validate the container path to prevent escaping /workspace/extra/."""
    if ".." in container_path:
        return False
    if container_path.startswith("/"):
        return False
    if not container_path or not container_path.strip():
        return False
    return True


class MountValidationResult:
    """Result of validating a single additional mount."""

    def __init__(
        self,
        allowed: bool,
        reason: str,
        real_host_path: str | None = None,
        resolved_container_path: str | None = None,
        effective_readonly: bool | None = None,
    ) -> None:
        self.allowed = allowed
        self.reason = reason
        self.real_host_path = real_host_path
        self.resolved_container_path = resolved_container_path
        self.effective_readonly = effective_readonly


def validate_mount(mount: AdditionalMount, is_main: bool) -> MountValidationResult:
    """Validate a single additional mount against the allowlist."""
    allowlist = load_mount_allowlist()

    if allowlist is None:
        return MountValidationResult(
            allowed=False,
            reason=f"No mount allowlist configured at {MOUNT_ALLOWLIST_PATH}",
        )

    # Derive containerPath from hostPath basename if not specified
    container_path = mount.container_path or Path(mount.host_path).name

    # Validate container path
    if not _is_valid_container_path(container_path):
        return MountValidationResult(
            allowed=False,
            reason=(
                f'Invalid container path: "{container_path}" — '
                'must be relative, non-empty, and not contain ".."'
            ),
        )

    # Expand and resolve the host path
    expanded = _expand_path(mount.host_path)
    real_path = _get_real_path(expanded)

    if real_path is None:
        return MountValidationResult(
            allowed=False,
            reason=f'Host path does not exist: "{mount.host_path}" (expanded: "{expanded}")',
        )

    real_path_str = str(real_path)

    # Check against blocked patterns
    blocked_match = _matches_blocked_pattern(real_path_str, allowlist.blocked_patterns)
    if blocked_match is not None:
        return MountValidationResult(
            allowed=False,
            reason=f'Path matches blocked pattern "{blocked_match}": "{real_path_str}"',
        )

    # Check if under an allowed root
    allowed_root = _find_allowed_root(real_path_str, allowlist.allowed_roots)
    if allowed_root is None:
        roots_str = ", ".join(str(_expand_path(r.path)) for r in allowlist.allowed_roots)
        return MountValidationResult(
            allowed=False,
            reason=f'Path "{real_path_str}" is not under any allowed root. Allowed roots: {roots_str}',
        )

    # Determine effective readonly status
    requested_read_write = mount.readonly is False
    effective_readonly = True  # Default to readonly

    if requested_read_write:
        if not is_main and allowlist.non_main_read_only:
            logger.info("Mount forced to read-only for non-main group", mount=mount.host_path)
        elif not allowed_root.allow_read_write:
            logger.info(
                "Mount forced to read-only — root does not allow read-write",
                mount=mount.host_path,
                root=allowed_root.path,
            )
        else:
            effective_readonly = False

    desc = f' ({allowed_root.description})' if allowed_root.description else ""
    return MountValidationResult(
        allowed=True,
        reason=f'Allowed under root "{allowed_root.path}"{desc}',
        real_host_path=real_path_str,
        resolved_container_path=container_path,
        effective_readonly=effective_readonly,
    )


class ValidatedMount:
    """A mount that has passed validation."""

    def __init__(self, host_path: str, container_path: str, readonly: bool) -> None:
        self.host_path = host_path
        self.container_path = container_path
        self.readonly = readonly


def validate_additional_mounts(
    mounts: list[AdditionalMount],
    group_name: str,
    is_main: bool,
) -> list[ValidatedMount]:
    """Validate all additional mounts for a group.

    Returns list of validated mounts (only those that passed).
    Logs warnings for rejected mounts.
    """
    validated: list[ValidatedMount] = []

    for mount in mounts:
        result = validate_mount(mount, is_main)

        if result.allowed:
            validated.append(
                ValidatedMount(
                    host_path=result.real_host_path or "",
                    container_path=f"/workspace/extra/{result.resolved_container_path}",
                    readonly=result.effective_readonly or True,
                )
            )
            logger.debug(
                "Mount validated successfully",
                group=group_name,
                host_path=result.real_host_path,
                container_path=result.resolved_container_path,
                readonly=result.effective_readonly,
                reason=result.reason,
            )
        else:
            logger.warning(
                "Additional mount REJECTED",
                group=group_name,
                requested_path=mount.host_path,
                container_path=mount.container_path,
                reason=result.reason,
            )

    return validated


def generate_allowlist_template() -> str:
    """Generate a template allowlist file for users to customize."""
    template = {
        "allowedRoots": [
            {
                "path": "~/projects",
                "allowReadWrite": True,
                "description": "Development projects",
            },
            {
                "path": "~/repos",
                "allowReadWrite": True,
                "description": "Git repositories",
            },
            {
                "path": "~/Documents/work",
                "allowReadWrite": False,
                "description": "Work documents (read-only)",
            },
        ],
        "blockedPatterns": ["password", "secret", "token"],
        "nonMainReadOnly": True,
    }
    return json.dumps(template, indent=2)
