"""
Pipeline lock: a blob-based mutex so two concurrent operators (or one
operator + the Jenkins cron) can't trample each other.

Usage:
    from pipeline_lock import acquire_lock, release_lock
    lock_id = acquire_lock(cfg, "preanalyze")
    try:
        ... do work ...
    finally:
        release_lock(cfg, "preanalyze", lock_id)

Or as a context manager:
    with PipelineLock(cfg, "preanalyze"):
        ... do work ...

The lock is a small JSON blob at `_dicache/.lock-<name>.json`. Each
acquirer writes their PID + agent hostname + timestamp. Stale locks
(older than `max_age_minutes`) are ignored — protects against a crashed
job that didn't release.

Concurrency model:
- Two processes try to acquire the same lock simultaneously: one wins
  the upload race, the other reads back the lock and sees a different
  owner. Loser raises LockHeldError.
- Crashed process leaves stale lock; next acquirer ignores if older
  than max_age_minutes. Default 4 hours (longer than the longest
  expected preanalyze).

This is NOT a guaranteed mutex (last-write-wins on the blob upload),
but it's good enough for our 1-pipeline-per-environment model and
catches the common case where someone runs preanalyze on their laptop
while Jenkins is running it nightly.
"""

from __future__ import annotations

import json
import os
import socket
import sys
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))


class LockHeldError(RuntimeError):
    """Another process holds the lock."""


def _lock_blob_name(name: str) -> str:
    return f"_dicache/.lock-{name}.json"


def _now_iso() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _parse_iso(s: str) -> datetime | None:
    try:
        return datetime.strptime(s, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=UTC)
    except (ValueError, TypeError):
        return None


def acquire_lock(cfg: dict, name: str, max_age_minutes: int = 240) -> str:
    """Try to acquire the named lock. Returns a unique lock_id on
    success. Raises LockHeldError if another process holds a fresh lock."""
    from preanalyze import (
        _init_storage,
        blob_exists,
        fetch_blob,
        upload_blob,
    )

    _init_storage(cfg)
    lock_blob = _lock_blob_name(name)

    if blob_exists(cfg, lock_blob):
        try:
            existing = json.loads(fetch_blob(cfg, lock_blob))
            held_at = _parse_iso(existing.get("acquired_at", ""))
            now = datetime.now(UTC)
            if held_at and (now - held_at) < timedelta(minutes=max_age_minutes):
                holder = existing.get("holder", "?")
                raise LockHeldError(
                    f"lock '{name}' held by {holder} since "
                    f"{existing.get('acquired_at')} (under {max_age_minutes}min "
                    f"stale threshold). Refusing to acquire. Wait for that run "
                    f"to finish, or if you're sure it's dead, delete "
                    f"_dicache/.lock-{name}.json and try again."
                )
            # Stale -- proceed to overwrite.
        except (json.JSONDecodeError, KeyError):
            # Malformed lock file; treat as stale.
            pass

    lock_id = uuid.uuid4().hex
    body = json.dumps({
        "lock_id": lock_id,
        "name": name,
        "holder": f"{socket.gethostname()}/pid={os.getpid()}",
        "acquired_at": _now_iso(),
    }, separators=(",", ":")).encode("utf-8")
    upload_blob(cfg, lock_blob, body)
    return lock_id


def release_lock(cfg: dict, name: str, lock_id: str) -> bool:
    """Release the lock if we still hold it. Returns True on success.
    Idempotent — if the lock was already released or stolen by a stale
    cleanup, returns False but doesn't raise."""
    from preanalyze import (
        _init_storage,
        blob_exists,
        delete_blob,
        fetch_blob,
    )

    _init_storage(cfg)
    lock_blob = _lock_blob_name(name)

    if not blob_exists(cfg, lock_blob):
        return False
    try:
        existing = json.loads(fetch_blob(cfg, lock_blob))
        if existing.get("lock_id") != lock_id:
            # Someone else's lock — don't touch.
            return False
    except Exception:
        return False
    try:
        return delete_blob(cfg, lock_blob)
    except Exception:
        return False


class PipelineLock:
    """Context manager wrapper for acquire/release."""

    def __init__(self, cfg: dict, name: str, max_age_minutes: int = 240):
        self.cfg = cfg
        self.name = name
        self.max_age_minutes = max_age_minutes
        self.lock_id: str | None = None

    def __enter__(self) -> PipelineLock:
        self.lock_id = acquire_lock(self.cfg, self.name, self.max_age_minutes)
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        if self.lock_id:
            release_lock(self.cfg, self.name, self.lock_id)
            self.lock_id = None
