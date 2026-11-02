"""Persistent mapping between issues and Planner tasks."""

from __future__ import annotations

import contextlib
import json
import logging
import os
import tempfile
from collections.abc import Iterator
from dataclasses import asdict, dataclass, field, fields
from pathlib import Path
from typing import IO, Any

from .errors import StateError

log = logging.getLogger(__name__)

STATE_VERSION = 1


@dataclass
class TaskRecord:
    """What git2planner last *wrote* for an issue.

    Each run compares the desired task with this baseline and only sends the
    fields whose source value changed, so edits made directly in Planner are
    preserved until the corresponding issue field changes.
    """

    task_id: str
    issue_url: str
    issue_state: str = "open"
    etag: str | None = None
    details_etag: str | None = None
    title: str | None = None
    bucket_id: str | None = None
    percent_complete: int | None = None
    priority: int | None = None
    due: str | None = None
    assignees: list[str] = field(default_factory=list)
    categories: list[str] = field(default_factory=list)
    description_hash: str | None = None
    preview: str | None = None
    references: dict[str, str] = field(default_factory=dict)
    checklist: dict[str, bool] = field(default_factory=dict)
    uploads: dict[str, str] = field(default_factory=dict)
    deleted: str | None = None
    """``None`` while the task exists, else why it is gone: ``closed``, ``planner``, ``missing``."""
    missing: bool = False
    """The issue disappeared from the source (deleted or transferred) but the task was kept."""
    synced_at: str | None = None
    origin: str = "git"
    """Where the pair was born: ``git`` (task created from an issue) or ``planner``."""

    def to_dict(self) -> dict[str, Any]:
        return {
            key: value
            for key, value in asdict(self).items()
            if value is not None and value is not False and value not in ([], {})
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> TaskRecord:
        known = {f.name for f in fields(cls)}
        return cls(**{key: value for key, value in data.items() if key in known})


@dataclass
class SyncState:
    plan_id: str | None = None
    last_run: str | None = None
    issues: dict[str, TaskRecord] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "plan_id": self.plan_id,
            "last_run": self.last_run,
            "issues": {
                key: record.to_dict()
                for key, record in sorted(self.issues.items(), key=lambda item: _sort_key(item[0]))
            },
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> SyncState:
        return cls(
            plan_id=data.get("plan_id"),
            last_run=data.get("last_run"),
            issues={
                str(key): TaskRecord.from_dict(value)
                for key, value in (data.get("issues") or {}).items()
            },
        )


def _sort_key(key: str) -> tuple[int, str]:
    return (int(key), "") if key.isdigit() else (1 << 62, key)


class StateStore:
    """JSON state file, written atomically. ``path=None`` keeps state in memory."""

    def __init__(self, path: Path | None) -> None:
        self.path = path
        self.syncs: dict[str, SyncState] = {}
        self._lock_handle: IO[str] | None = None
        self.dirty = False

    def load(self) -> StateStore:
        if self.path is None or not self.path.exists():
            return self
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise StateError(f"cannot read state file {self.path}: {exc}") from exc
        version = data.get("version", STATE_VERSION)
        if version > STATE_VERSION:
            raise StateError(
                f"state file {self.path} was written by a newer git2planner (format {version})"
            )
        self.syncs = {
            name: SyncState.from_dict(value) for name, value in (data.get("syncs") or {}).items()
        }
        return self

    def sync(self, name: str) -> SyncState:
        return self.syncs.setdefault(name, SyncState())

    def to_dict(self) -> dict[str, Any]:
        return {
            "version": STATE_VERSION,
            "syncs": {name: state.to_dict() for name, state in sorted(self.syncs.items())},
        }

    def save(self) -> None:
        self.dirty = False
        if self.path is None:
            return
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            fd, tmp = tempfile.mkstemp(
                dir=self.path.parent, prefix=f".{self.path.name}.", suffix=".tmp"
            )
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump(self.to_dict(), handle, indent=2, sort_keys=False)
                handle.write("\n")
            os.replace(tmp, self.path)
        except OSError as exc:
            raise StateError(f"cannot write state file {self.path}: {exc}") from exc

    @contextlib.contextmanager
    def locked(self) -> Iterator[StateStore]:
        """Hold an exclusive lock so two runs never update the same state file."""
        if self.path is None:
            yield self
            return
        lock_path = self.path.with_name(self.path.name + ".lock")
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        handle = open(lock_path, "a+", encoding="utf-8")  # noqa: SIM115
        try:
            try:
                _lock_file(handle)
            except OSError as exc:
                raise StateError(
                    f"another git2planner run is using {self.path} (lock {lock_path})"
                ) from exc
            self._lock_handle = handle
            yield self
        finally:
            self._lock_handle = None
            handle.close()


def _lock_file(handle: IO[str]) -> None:
    """Take a non-blocking exclusive lock; raise :class:`OSError` if it is held."""
    try:
        import fcntl
    except ImportError:  # pragma: no cover - Windows
        import msvcrt

        msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)  # type: ignore[attr-defined,unused-ignore]
        return
    fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
