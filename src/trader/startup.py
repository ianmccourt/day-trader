"""Startup checks: disk space and log size, run before `trader run`.

Warnings by default. `--strict` turns a failed check into a refused start.
"""

from __future__ import annotations

import shutil
from pathlib import Path


class StartupCheckError(RuntimeError):
    """A startup check failed in `--strict` mode."""


def check_disk_space(
    path: Path = Path("."),
    min_mb: int = 1000,
    *,
    strict: bool = False,
) -> tuple[bool, str]:
    try:
        free_mb = shutil.disk_usage(path).free / (1024 * 1024)
        if free_mb < min_mb:
            msg = f"Low disk space: {free_mb:.0f} MB free (minimum {min_mb} MB)"
            if strict:
                raise StartupCheckError(msg)
            return False, msg
        return True, f"Disk space OK: {free_mb:.0f} MB free"
    except StartupCheckError:
        raise
    except Exception as exc:
        msg = f"Could not check disk space: {exc}"
        if strict:
            raise StartupCheckError(msg) from exc
        return False, msg


def check_log_file_size(
    log_file: Path = Path("logs/trader.jsonl"),
    max_mb: int = 500,
    *,
    strict: bool = False,
) -> tuple[bool, str]:
    if not log_file.exists():
        return True, "Log file does not exist yet"
    try:
        size_mb = log_file.stat().st_size / (1024 * 1024)
        if size_mb > max_mb:
            msg = (
                f"Large log file: {size_mb:.0f} MB (maximum {max_mb} MB). "
                "Consider log rotation (see deploy/logrotate-trader.conf)"
            )
            if strict:
                raise StartupCheckError(msg)
            return False, msg
        return True, f"Log file size OK: {size_mb:.1f} MB"
    except StartupCheckError:
        raise
    except Exception as exc:
        msg = f"Could not check log file size: {exc}"
        if strict:
            raise StartupCheckError(msg) from exc
        return False, msg


def run_startup_checks(*, strict: bool = False) -> list[tuple[bool, str]]:
    return [
        check_disk_space(strict=strict),
        check_log_file_size(strict=strict),
    ]
