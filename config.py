"""Shared constants + one environment fix-up.

Model IDs are pinned to dated snapshots for reproducibility; aliases like "haiku"
are not stable across releases.
"""

from __future__ import annotations

import asyncio
import logging
import os
import warnings

AGENT_MODEL = "claude-haiku-4-5-20251001"
JUDGE_MODEL = "claude-sonnet-5"

# Sonnet 5 has no dated-snapshot alias exposed to the SDK yet; when one lands,
# pin it here and note the swap in prompts/CHANGELOG.md.


def _quiet_asyncio_child_watcher_noise() -> None:
    """claude-agent-sdk spawns each `claude` CLI via `anyio.open_process`, which on the
    asyncio backend uses the (deprecated) child watcher. Running many short-lived
    subprocesses concurrently (the eval) makes the watcher thread's `os.waitpid()`
    race another reap of the same pid -> ChildProcessError -> asyncio logs
    "Unknown child process pid N, will report returncode 255".

    It is cosmetic: the SDK reads the real result off the process stream before the
    watcher runs, and genuine failures still surface through AgentResult.error and
    the eval's error_rate. So we (a) drop that one log line and (b) on Linux switch
    to the race-free pidfd watcher.
    """
    class _Filter(logging.Filter):
        def filter(self, record: logging.LogRecord) -> bool:
            return "Unknown child process pid" not in record.getMessage()

    logging.getLogger("asyncio").addFilter(_Filter())

    # Race-free pidfd watcher, but ONLY where the syscall actually exists
    # (Linux >= 5.3). The class is present on macOS too yet os.pidfd_open is not,
    # and installing it there breaks subprocess spawning.
    pidfd = getattr(asyncio, "PidfdChildWatcher", None)
    if pidfd is not None and hasattr(os, "pidfd_open"):
        try:
            with warnings.catch_warnings():
                warnings.simplefilter("ignore", DeprecationWarning)  # set_child_watcher, 3.12
                asyncio.set_child_watcher(pidfd())
        except (NotImplementedError, RuntimeError):
            pass


_quiet_asyncio_child_watcher_noise()
