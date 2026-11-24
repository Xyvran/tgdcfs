"""Persistent state of the backfill runs, per file system.

A backfill reads every descriptor before it copies anything (to plan
the bytes and to go newest first), which takes a long time on a large
library. The state remembers the result of that scan and the outcome of
every file, so a restart resumes where the run stopped instead of
scanning again, and a file that failed is retried later without holding
up the others.

The state lives in ``backfill_state.json`` in the data directory and is
written atomically, like the replication queue. A file counts as done
in the saved state only once the metadata that records its copies has
been pushed. A run is removed from the state once it has finished; a
run that is still in it after a restart is resumed at startup.
"""

from __future__ import annotations

import json
import logging
import os
import time
from dataclasses import dataclass, field
from typing import Dict, List, Literal, Optional

logger = logging.getLogger(__name__)

FileState = Literal["pending", "done", "failed"]
FILE_STATES: tuple[FileState, ...] = ("pending", "done", "failed")

# Backoff of a failed file, doubling with every attempt.
RETRY_BASE_SECONDS = 60.0
RETRY_MAX_SECONDS = 60 * 60.0
# Attempts a file gets within one run before it is reported as failed.
MAX_ATTEMPTS = 4


@dataclass
class FileEntry:
    path: str
    # Timestamp of the descriptor: the run goes newest first.
    updated_at: int = 0
    # Bytes the file had to copy when it was scanned.
    bytes: int = 0
    state: FileState = "pending"
    attempts: int = 0
    last_error: Optional[str] = None
    # Failures of the last attempt, as reported by the backfill.
    failures: List[str] = field(default_factory=list)
    not_before: float = 0.0

    def to_dict(self) -> dict:
        return {
            "path": self.path,
            "updated_at": self.updated_at,
            "bytes": self.bytes,
            "state": self.state,
            "attempts": self.attempts,
            "last_error": self.last_error,
            "failures": self.failures,
            "not_before": self.not_before,
        }

    @classmethod
    def from_dict(cls, data: dict) -> "FileEntry":
        state = data.get("state", "pending")
        return cls(
            path=data["path"],
            updated_at=int(data.get("updated_at", 0)),
            bytes=int(data.get("bytes", 0)),
            state=state if state in FILE_STATES else "pending",
            attempts=int(data.get("attempts", 0)),
            last_error=data.get("last_error"),
            failures=list(data.get("failures") or []),
            not_before=float(data.get("not_before", 0.0)),
        )


@dataclass
class BackfillRun:
    filesystem: str
    verify: bool = False
    started_at: float = field(default_factory=time.time)
    # Whether every descriptor has been read; until then a resume reads
    # the ones that have no entry yet.
    scanned: bool = False
    # Descriptors the scan could not read; read again on resume.
    unreadable: List[str] = field(default_factory=list)
    files: Dict[str, FileEntry] = field(default_factory=dict)

    def count(self, state: FileState) -> int:
        return sum(1 for entry in self.files.values() if entry.state == state)

    def ordered(self) -> List[FileEntry]:
        """Every entry, newest first."""
        return sorted(self.files.values(), key=lambda e: e.updated_at, reverse=True)

    def to_dict(self) -> dict:
        return {
            "filesystem": self.filesystem,
            "verify": self.verify,
            "started_at": self.started_at,
            "scanned": self.scanned,
            "unreadable": self.unreadable,
            "files": [entry.to_dict() for entry in self.files.values()],
        }

    @classmethod
    def from_dict(cls, data: dict) -> "BackfillRun":
        run = cls(
            filesystem=data["filesystem"],
            verify=bool(data.get("verify", False)),
            started_at=float(data.get("started_at", time.time())),
            scanned=bool(data.get("scanned", False)),
            unreadable=list(data.get("unreadable") or []),
        )
        for item in data.get("files") or []:
            entry = FileEntry.from_dict(item)
            run.files[entry.path] = entry
        return run

    def summary(self) -> dict:
        return {
            "verify": self.verify,
            "started_at": self.started_at,
            "scanned": self.scanned,
            "files": len(self.files),
            "done": self.count("done"),
            "pending": self.count("pending"),
            "failed": self.count("failed"),
            "failures": [
                {
                    "path": entry.path,
                    "attempts": entry.attempts,
                    "last_error": entry.last_error,
                }
                for entry in self.files.values()
                if entry.state == "failed"
            ],
        }


@dataclass
class BackfillState:
    """The unfinished backfill runs, persisted to ``path``.

    Without a ``path`` the state is kept in memory only (one run, nothing
    to resume). ``max_attempts`` is how often a file is tried within a
    run before the run reports it as failed.
    """

    path: Optional[str]
    max_attempts: int = MAX_ATTEMPTS
    retry_base_seconds: float = RETRY_BASE_SECONDS
    retry_max_seconds: float = RETRY_MAX_SECONDS
    _runs: Dict[str, BackfillRun] = field(default_factory=dict)

    def __post_init__(self) -> None:
        self._load()

    # -- persistence -------------------------------------------------------

    def _load(self) -> None:
        if not self.path or not os.path.exists(self.path):
            return
        try:
            with open(self.path, "r", encoding="utf-8") as fh:
                data = json.load(fh)
            for item in (data or {}).get("runs") or []:
                run = BackfillRun.from_dict(item)
                self._runs[run.filesystem] = run
        except Exception as ex:
            # A broken state costs a fresh scan, nothing else.
            logger.error(f"Could not read the backfill state {self.path}: {ex}")
            self._runs.clear()

    def save(self) -> None:
        if not self.path:
            return
        data = {"runs": [run.to_dict() for run in self._runs.values()]}
        tmp = f"{self.path}.tmp"
        try:
            os.makedirs(os.path.dirname(self.path) or ".", exist_ok=True)
            with open(tmp, "w", encoding="utf-8") as fh:
                json.dump(data, fh)
            os.replace(tmp, self.path)
        except OSError as ex:
            # The run goes on; a restart before the next successful save
            # repeats the work since the last one.
            logger.warning(f"Could not save the backfill state to {self.path}: {ex}")

    # -- runs --------------------------------------------------------------

    def get(self, filesystem: str) -> Optional[BackfillRun]:
        return self._runs.get(filesystem)

    def runs(self) -> List[BackfillRun]:
        return list(self._runs.values())

    def begin(self, filesystem: str, verify: bool) -> BackfillRun:
        """A fresh run, replacing an unfinished one of ``filesystem``."""
        run = BackfillRun(filesystem=filesystem, verify=verify)
        self._runs[filesystem] = run
        self.save()
        return run

    def finish(self, filesystem: str) -> None:
        if self._runs.pop(filesystem, None) is not None:
            self.save()

    # -- files -------------------------------------------------------------
    # The outcome of a file is saved by the backfill's next checkpoint,
    # after the metadata that records its copies has been pushed.

    def done(self, entry: FileEntry) -> None:
        entry.state = "done"
        entry.last_error = None
        entry.failures = []
        entry.not_before = 0.0

    def failed(self, entry: FileEntry, failures: List[str]) -> None:
        entry.state = "failed"
        entry.attempts += 1
        entry.failures = list(failures)
        entry.last_error = "; ".join(failures)
        entry.not_before = time.time() + min(
            self.retry_base_seconds * 2 ** (entry.attempts - 1),
            self.retry_max_seconds,
        )

    def retryable(self, entry: FileEntry) -> bool:
        return entry.state == "failed" and entry.attempts < self.max_attempts

    def to_dict(self) -> Dict[str, dict]:
        return {name: run.summary() for name, run in self._runs.items()}
