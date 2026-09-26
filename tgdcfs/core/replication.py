"""Background replication: ``sync: background`` for a file system.

A write records the version in the primary store only and hands the
file's path to the :class:`ReplicationQueue`; a :class:`ReplicationWorker`
per file system drains the queue by running the backfill's unit of work
(:func:`tgdcfs.core.backfill.backfill_file`) on that file, which copies
every version that lacks a copy in some mirror store and commits the
result into the descriptor and the metadata.

The queue is persisted as a small JSON file in the data directory, so a
restart does not lose pending copies. Items are paths, which makes the
queue idempotent: a file queued twice is processed once, and a file
whose copies are already complete is a no-op. Failures keep the item and
back off; nothing is ever dropped silently.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Dict, List, Optional

from tgdcfs.core.backfill import BackfillReport, backfill_file
from tgdcfs.core.model import TGFSDirectory, TGFSFileRef

if TYPE_CHECKING:
    from tgdcfs.core.client import Client

logger = logging.getLogger(__name__)

# Backoff after a failed item, growing with consecutive failures.
RETRY_BASE_SECONDS = 15.0
RETRY_MAX_SECONDS = 15 * 60.0


def file_path(fr: TGFSFileRef) -> str:
    """Absolute path of a file ref inside its file system."""
    return f"{fr.location.absolute_path}/{fr.name}"


def find_file(root: TGFSDirectory, path: str) -> Optional[TGFSFileRef]:
    """Resolve a path produced by :func:`file_path`; ``None`` when gone."""
    parts = [p for p in path.split("/") if p]
    if not parts:
        return None
    node = root
    for name in parts[:-1]:
        dirs = node.find_dirs([name])
        if not dirs:
            return None
        node = dirs[0]
    files = node.find_files([parts[-1]])
    return files[0] if files else None


@dataclass
class QueueItem:
    path: str
    queued_at: float
    attempts: int = 0
    last_error: Optional[str] = None
    not_before: float = 0.0

    def to_dict(self) -> dict:
        return {
            "path": self.path,
            "queued_at": self.queued_at,
            "attempts": self.attempts,
            "last_error": self.last_error,
        }


@dataclass
class ReplicationQueue:
    """Pending files per file system, persisted to ``path``."""

    path: Optional[str]
    _items: Dict[str, Dict[str, QueueItem]] = field(default_factory=dict)
    _wakeups: Dict[str, asyncio.Event] = field(default_factory=dict)

    def __post_init__(self) -> None:
        self._load()

    # -- persistence -------------------------------------------------------

    def _load(self) -> None:
        if not self.path or not os.path.exists(self.path):
            return
        try:
            with open(self.path, "r", encoding="utf-8") as fh:
                data = json.load(fh)
        except Exception as ex:
            logger.error(f"Could not read the replication queue {self.path}: {ex}")
            return
        for fs_name, items in (data or {}).items():
            for item in items:
                self._items.setdefault(fs_name, {})[item["path"]] = QueueItem(
                    path=item["path"],
                    queued_at=float(item.get("queued_at", time.time())),
                    attempts=int(item.get("attempts", 0)),
                    last_error=item.get("last_error"),
                )

    def _save(self) -> None:
        if not self.path:
            return
        data = {
            fs_name: [item.to_dict() for item in items.values()]
            for fs_name, items in self._items.items()
            if items
        }
        tmp = f"{self.path}.tmp"
        try:
            os.makedirs(os.path.dirname(self.path) or ".", exist_ok=True)
            with open(tmp, "w", encoding="utf-8") as fh:
                json.dump(data, fh)
            os.replace(tmp, self.path)
        except OSError as ex:
            # A full disk must not fail the write that enqueued the file:
            # the item stays in memory and the worker still gets to it; a
            # restart before the next successful save loses the queue
            # entry, which the backfill task repairs.
            logger.warning(f"Could not save the replication queue to {self.path}: {ex}")

    # -- queue -------------------------------------------------------------

    def wakeup(self, fs_name: str) -> asyncio.Event:
        if fs_name not in self._wakeups:
            self._wakeups[fs_name] = asyncio.Event()
        return self._wakeups[fs_name]

    def enqueue(self, fs_name: str, path: str) -> None:
        items = self._items.setdefault(fs_name, {})
        if path not in items:
            items[path] = QueueItem(path=path, queued_at=time.time())
            self._save()
        self.wakeup(fs_name).set()

    def pending(self, fs_name: str) -> List[QueueItem]:
        return sorted(self._items.get(fs_name, {}).values(), key=lambda i: i.queued_at)

    def ready(self, fs_name: str, now: Optional[float] = None) -> List[QueueItem]:
        now = time.time() if now is None else now
        return [item for item in self.pending(fs_name) if item.not_before <= now]

    def done(self, fs_name: str, path: str) -> None:
        if self._items.get(fs_name, {}).pop(path, None) is not None:
            self._save()

    def failed(self, fs_name: str, path: str, error: str) -> None:
        item = self._items.get(fs_name, {}).get(path)
        if item is None:
            return
        item.attempts += 1
        item.last_error = error
        item.not_before = time.time() + min(
            RETRY_BASE_SECONDS * 2 ** (item.attempts - 1), RETRY_MAX_SECONDS
        )
        self._save()

    def retry_now(self, fs_name: Optional[str] = None) -> None:
        """Clear the backoff of every item (of one file system)."""
        for name, items in self._items.items():
            if fs_name is not None and name != fs_name:
                continue
            for item in items.values():
                item.not_before = 0.0
            self.wakeup(name).set()

    def to_dict(self) -> Dict[str, List[dict]]:
        return {
            fs_name: [item.to_dict() for item in self.pending(fs_name)]
            for fs_name in self._items
        }


class ReplicationWorker:
    """Drains one file system's queue in the background."""

    def __init__(self, client: "Client", queue: ReplicationQueue):
        self._client = client
        self._queue = queue
        self._task: Optional[asyncio.Task[None]] = None
        self.last_report: Optional[BackfillReport] = None

    @property
    def name(self) -> str:
        return self._client.name

    def start(self) -> None:
        if self._task is None:
            self._task = asyncio.get_running_loop().create_task(self.run())

    async def stop(self) -> None:
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None

    async def process_one(self, path: str) -> bool:
        """Replicate one queued file; returns whether it is done."""
        fr = find_file(self._client.dir_api.root, path)
        if fr is None:
            # Removed in the meantime: nothing left to copy.
            self._queue.done(self.name, path)
            return True
        report = BackfillReport()
        try:
            await backfill_file(self._client, fr, verify=False, report=report)
            if report.failures:
                raise RuntimeError("; ".join(report.failures))
            if self._client.metadata_api:
                await self._client.metadata_api.push()
        except Exception as ex:
            logger.warning(f"Replication of {path} in '{self.name}' failed: {ex}")
            self._queue.failed(self.name, path, str(ex))
            return False
        self.last_report = report
        self._queue.done(self.name, path)
        return True

    async def run_once(self) -> int:
        """Process everything that is ready; returns how many were done."""
        done = 0
        for item in self._queue.ready(self.name):
            if await self.process_one(item.path):
                done += 1
        return done

    async def run(self) -> None:
        wakeup = self._queue.wakeup(self.name)
        while True:
            wakeup.clear()
            try:
                await self.run_once()
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception(f"Replication worker for '{self.name}' crashed")
            pending = self._queue.pending(self.name)
            if not pending:
                await wakeup.wait()
                continue
            delay = max(0.0, min(item.not_before for item in pending) - time.time())
            try:
                await asyncio.wait_for(wakeup.wait(), timeout=max(delay, 1.0))
            except asyncio.TimeoutError:
                pass
