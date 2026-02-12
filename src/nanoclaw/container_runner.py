"""Container runner — spawns and manages agent containers.

Port of src/container-runner.ts (658 lines). Handles spawning Apple Container
processes, streaming output with OUTPUT_START/END markers, writing task and
group snapshots, and managing container lifecycle.

All `chatJid` references replaced with `chat_id` for Telegram.
"""

from __future__ import annotations

import asyncio
import json
import os
import time
from pathlib import Path
from typing import Any, Callable, Awaitable

from .config import (
    CONTAINER_IMAGE,
    CONTAINER_MAX_OUTPUT_SIZE,
    CONTAINER_TIMEOUT,
    DATA_DIR,
    GROUPS_DIR,
    MAIN_GROUP_FOLDER,
    STORE_DIR,
)
from .logger import logger
from .mount_security import validate_additional_mounts
from .types import (
    AvailableGroup,
    ContainerInput,
    ContainerOutput,
    RegisteredGroup,
)

# Markers for structured output in stdout stream
OUTPUT_START = "===OUTPUT_START==="
OUTPUT_END = "===OUTPUT_END==="


def write_tasks_snapshot(
    group_folder: str,
    is_main: bool,
    tasks: list[dict[str, Any]],
) -> None:
    """Write a JSON snapshot of tasks for the container to read."""
    tasks_dir = DATA_DIR / "ipc" / group_folder
    tasks_dir.mkdir(parents=True, exist_ok=True)

    # Non-main groups only see their own tasks
    if not is_main:
        tasks = [t for t in tasks if t.get("groupFolder") == group_folder]

    snapshot_path = tasks_dir / "tasks.json"
    temp_path = snapshot_path.with_suffix(".json.tmp")
    temp_path.write_text(json.dumps(tasks, indent=2))
    temp_path.rename(snapshot_path)


def write_groups_snapshot(
    group_folder: str,
    is_main: bool,
    available_groups: list[AvailableGroup],
    registered_ids: set[str],
) -> None:
    """Write a JSON snapshot of available groups for the container to read."""
    if not is_main:
        return  # Non-main groups don't need the groups list

    groups_dir = DATA_DIR / "ipc" / group_folder
    groups_dir.mkdir(parents=True, exist_ok=True)

    snapshot = [
        {
            "chatId": g.chat_id,
            "name": g.name,
            "lastActivity": g.last_activity,
            "isRegistered": g.is_registered,
        }
        for g in available_groups
    ]

    snapshot_path = groups_dir / "groups.json"
    temp_path = snapshot_path.with_suffix(".json.tmp")
    temp_path.write_text(json.dumps(snapshot, indent=2))
    temp_path.rename(snapshot_path)


class ContainerRunError(Exception):
    """Raised when a container run fails."""


RegisterProcessFn = Callable[
    [asyncio.subprocess.Process, str],
    None,
]
OnOutputFn = Callable[[ContainerOutput], Awaitable[None]]


async def run_container_agent(
    group: RegisteredGroup,
    input_data: ContainerInput,
    register_process: RegisterProcessFn,
    on_output: OnOutputFn | None = None,
) -> ContainerOutput:
    """Spawn a container and run the agent with the given input.

    Args:
        group: The registered group configuration.
        input_data: Input to send to the container agent via stdin.
        register_process: Callback to register the process for tracking.
        on_output: Optional callback for streamed output chunks.

    Returns:
        The final ContainerOutput from the agent.
    """
    # Ensure group directory exists
    group_dir = GROUPS_DIR / group.folder
    group_dir.mkdir(parents=True, exist_ok=True)

    # IPC directories
    ipc_dir = DATA_DIR / "ipc" / group.folder
    input_dir = ipc_dir / "input"
    output_dir = ipc_dir / "output"
    messages_dir = ipc_dir / "messages"
    tasks_dir = ipc_dir / "tasks"

    for d in [input_dir, output_dir, messages_dir, tasks_dir]:
        d.mkdir(parents=True, exist_ok=True)

    container_name = f"nanoclaw-{group.folder}-{int(time.time())}"
    is_main = group.folder == MAIN_GROUP_FOLDER

    # Build container run command
    cmd = _build_container_command(
        container_name=container_name,
        group=group,
        group_dir=group_dir,
        ipc_dir=ipc_dir,
        is_main=is_main,
    )

    logger.info(
        "Starting container",
        container=container_name,
        group=group.name,
        folder=group.folder,
    )

    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
    except Exception as err:
        msg = f"Failed to start container: {err}"
        logger.error(msg, container=container_name)
        return ContainerOutput(status="error", result=None, error=msg)

    # Register process for tracking
    register_process(proc, container_name)

    # Write input to stdin
    stdin_data = input_data.model_dump_json().encode() + b"\n"
    if proc.stdin:
        proc.stdin.write(stdin_data)
        await proc.stdin.drain()
        proc.stdin.close()

    # Read and parse output
    final_output = ContainerOutput(status="error", result=None, error="No output received")

    try:
        final_output = await asyncio.wait_for(
            _read_container_output(proc, on_output),
            timeout=CONTAINER_TIMEOUT / 1000,
        )
    except asyncio.TimeoutError:
        logger.warning(
            "Container timed out",
            container=container_name,
            timeout_ms=CONTAINER_TIMEOUT,
        )
        final_output = ContainerOutput(
            status="error",
            result=None,
            error=f"Container timed out after {CONTAINER_TIMEOUT}ms",
        )
        # Kill the container process
        try:
            proc.kill()
        except Exception:
            pass

    # Wait for process to finish
    try:
        await asyncio.wait_for(proc.wait(), timeout=10)
    except asyncio.TimeoutError:
        try:
            proc.kill()
        except Exception:
            pass

    return final_output


async def _read_container_output(
    proc: asyncio.subprocess.Process,
    on_output: OnOutputFn | None,
) -> ContainerOutput:
    """Read structured output from the container stdout.

    Output is delimited by OUTPUT_START and OUTPUT_END markers.
    Multiple output blocks are supported (streamed results).
    """
    assert proc.stdout is not None

    final = ContainerOutput(status="error", result=None, error="No output received")
    buffer: list[str] = []
    in_output = False
    total_size = 0

    while True:
        line_bytes = await proc.stdout.readline()
        if not line_bytes:
            break  # EOF

        line = line_bytes.decode("utf-8", errors="replace").rstrip("\n")

        if line == OUTPUT_START:
            in_output = True
            buffer = []
            continue

        if line == OUTPUT_END and in_output:
            in_output = False
            raw = "\n".join(buffer)
            try:
                parsed = json.loads(raw)
                output = ContainerOutput(
                    status=parsed.get("status", "success"),
                    result=parsed.get("result"),
                    new_session_id=parsed.get("newSessionId"),
                    error=parsed.get("error"),
                )
                final = output
                if on_output:
                    await on_output(output)
            except json.JSONDecodeError as err:
                logger.warning("Failed to parse container output", error=str(err))
            continue

        if in_output:
            total_size += len(line)
            if total_size > CONTAINER_MAX_OUTPUT_SIZE:
                logger.warning(
                    "Container output exceeds max size, truncating",
                    max_size=CONTAINER_MAX_OUTPUT_SIZE,
                )
                in_output = False
            else:
                buffer.append(line)

    return final


def _build_container_command(
    container_name: str,
    group: RegisteredGroup,
    group_dir: Path,
    ipc_dir: Path,
    is_main: bool,
) -> list[str]:
    """Build the Apple Container CLI command.

    This produces the command line for `container run` with appropriate
    mounts, environment variables, and the agent-runner entry point.
    """
    cmd = [
        "container",
        "run",
        "--name",
        container_name,
        "--rm",
        "--memory",
        "2g",
    ]

    # Mount the group workspace
    cmd.extend(["--mount", f"type=bind,src={group_dir},dst=/workspace/group"])

    # Mount IPC directories
    cmd.extend(["--mount", f"type=bind,src={ipc_dir},dst=/workspace/ipc"])

    # Mount store for shared config/state
    cmd.extend(["--mount", f"type=bind,src={STORE_DIR},dst=/workspace/store,readonly"])

    # Additional mounts (validated)
    if group.container_config and group.container_config.additional_mounts:
        validated = validate_additional_mounts(
            group.container_config.additional_mounts,
            group.name,
            is_main,
        )
        for mount in validated:
            ro = ",readonly" if mount.readonly else ""
            cmd.extend([
                "--mount",
                f"type=bind,src={mount.host_path},dst={mount.container_path}{ro}",
            ])

    # Environment variables
    cmd.extend(["--env", f"GROUP_FOLDER={group.folder}"])
    cmd.extend(["--env", f"IS_MAIN={'true' if is_main else 'false'}"])

    # Image and entry point
    cmd.extend([CONTAINER_IMAGE, "python", "/app/main.py"])

    return cmd


async def ensure_container_system_running() -> bool:
    """Check that the container runtime is available."""
    try:
        proc = await asyncio.create_subprocess_exec(
            "container", "version",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        await proc.wait()
        return proc.returncode == 0
    except FileNotFoundError:
        logger.error("'container' command not found — is Apple Container installed?")
        return False
    except Exception as err:
        logger.error("Error checking container system", error=str(err))
        return False
