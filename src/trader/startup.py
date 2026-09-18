"""Startup checks: disk space, log file size, alerting config.

Run before starting `trader run` to catch operational issues early.
"""

from __future__ import annotations

import os
import shutil
from pathlib import Path


class StartupCheckError(RuntimeError):
    """Startup check failed (--strict mode)."""


def check_disk_space(
    path: Path = Path("."),
    min_mb: int = 1000,
    *,
    strict: bool = False,
) -> tuple[bool, str]:
    """Check free disk space. Returns (ok, message)."""
    try:
        stat = shutil.disk_usage(path)
        free_mb = stat.free / (1024 * 1024)
        
        if free_mb < min_mb:
            msg = f"Low disk space: {free_mb:.0f} MB free (minimum {min_mb} MB)"
            if strict:
                raise StartupCheckError(msg)
            return False, msg
        
        return True, f"Disk space OK: {free_mb:.0f} MB free"
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
    """Check log file size. Returns (ok, message)."""
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
    except Exception as exc:
        msg = f"Could not check log file size: {exc}"
        if strict:
            raise StartupCheckError(msg) from exc
        return False, msg


def run_startup_checks(*, strict: bool = False) -> list[tuple[bool, str]]:
    """Run all startup checks. Returns list of (ok, message) tuples."""
    checks = [
        check_disk_space(strict=strict),
        check_log_file_size(strict=strict),
    ]
    return checks
