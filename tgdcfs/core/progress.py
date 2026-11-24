"""Progress of the background copies, in files, bytes and parts.

A :class:`RunProgress` describes one backfill run or one replication
worker: how many files and bytes it has to copy, how far it got, and the
copy it is in right now (:class:`CopyProgress`: one version into one
store, with the source part it is at). The runs register themselves
here, so the manager API (``GET /replication/progress``) and the logs read
the same numbers.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import AsyncIterator, Callable, Dict, List, Optional


def format_bytes(size: int) -> str:
    """``size`` in the largest binary unit that keeps it at or above 1."""
    value = float(size)
    for unit in ("B", "KiB", "MiB", "GiB"):
        if abs(value) < 1024:
            return f"{value:.0f} {unit}" if unit == "B" else f"{value:.1f} {unit}"
        value /= 1024
    return f"{value:.1f} TiB"


def _percent(done: int, total: int) -> str:
    return f" ({100 * done / total:.1f}%)" if total > 0 else ""


@dataclass
class CopyProgress:
    """One version being copied into one store."""

    file: str
    version: str
    store: str
    part_sizes: List[int]
    bytes_done: int = 0
    started: float = field(default_factory=time.monotonic)
    on_bytes: Optional[Callable[[int], None]] = field(default=None, repr=False)

    @property
    def bytes_total(self) -> int:
        return sum(self.part_sizes)

    @property
    def parts(self) -> int:
        return len(self.part_sizes)

    @property
    def part(self) -> int:
        """The source part the copy is at, 1-based; ``parts`` once done."""
        end = 0
        for index, size in enumerate(self.part_sizes, 1):
            end += size
            if self.bytes_done < end:
                return index
        return self.parts

    def advance(self, size: int) -> None:
        self.bytes_done += size
        if self.on_bytes is not None:
            self.on_bytes(size)

    def complete(self) -> None:
        """The whole version is in (a server-side copy moves no bytes
        through this process)."""
        if (rest := self.bytes_total - self.bytes_done) > 0:
            self.advance(rest)

    async def track(self, chunks: AsyncIterator[bytes]) -> AsyncIterator[bytes]:
        """``chunks``, counted as they pass; closing it closes ``chunks``."""
        try:
            async for chunk in chunks:
                self.advance(len(chunk))
                yield chunk
        finally:
            if (aclose := getattr(chunks, "aclose", None)) is not None:
                await aclose()

    def to_dict(self) -> dict:
        return {
            "file": self.file,
            "version": self.version,
            "store": self.store,
            "part": self.part,
            "parts": self.parts,
            "bytes_done": self.bytes_done,
            "bytes_total": self.bytes_total,
            "seconds": round(time.monotonic() - self.started, 1),
        }

    def describe(self) -> str:
        return (
            f"{self.file}@{self.version} -> {self.store}: part {self.part}/"
            f"{self.parts}, {format_bytes(self.bytes_done)}/"
            f"{format_bytes(self.bytes_total)}"
            f"{_percent(self.bytes_done, self.bytes_total)}"
        )


@dataclass
class RunProgress:
    """A backfill run or a replication worker."""

    kind: str
    filesystem: str
    phase: str = "starting"
    files_total: int = 0
    files_done: int = 0
    # Bytes to copy over the whole run: the size of each version that
    # misses a copy, once per store that misses it. Zero when unknown
    # (the replication queue learns its work one file at a time).
    bytes_total: int = 0
    failures: int = 0
    current: Optional[CopyProgress] = None
    started: float = field(default_factory=time.time)
    _bytes_finished: int = 0
    _file_planned: int = 0
    _file_bytes: int = 0

    @property
    def bytes_done(self) -> int:
        current = self._file_bytes
        if self._file_planned > 0:
            current = min(current, self._file_planned)
        return self._bytes_finished + current

    def start_file(self, planned_bytes: int = 0, retry: bool = False) -> None:
        """``retry``: the file failed before and is counted in ``failures``
        until this attempt is finished."""
        self._file_planned = max(0, planned_bytes)
        self._file_bytes = 0
        if retry and self.failures > 0:
            self.failures -= 1

    def finish_file(self, failed: bool = False) -> None:
        """Count the file as done, copied or skipped: its bytes are no
        longer ahead of the run. A failed file is counted in ``failures``
        instead and keeps its bytes ahead, since it is tried again."""
        if failed:
            self.failures += 1
        else:
            self._bytes_finished += max(self._file_planned, self._file_bytes)
            self.files_done += 1
        self._file_planned = self._file_bytes = 0
        self.current = None

    def count_done(self, files: int, size: int) -> None:
        """Files a resumed run finished before it was interrupted."""
        self.files_done += files
        self._bytes_finished += size

    def start_copy(
        self, file: str, version: str, store: str, part_sizes: List[int]
    ) -> CopyProgress:
        self.current = CopyProgress(
            file=file,
            version=version,
            store=store,
            part_sizes=list(part_sizes),
            on_bytes=self._add_bytes,
        )
        return self.current

    def _add_bytes(self, size: int) -> None:
        self._file_bytes += size

    def to_dict(self) -> dict:
        return {
            "kind": self.kind,
            "filesystem": self.filesystem,
            "phase": self.phase,
            "files_done": self.files_done,
            "files_total": self.files_total,
            "bytes_done": self.bytes_done,
            "bytes_total": self.bytes_total,
            "failures": self.failures,
            "seconds": round(time.time() - self.started, 1),
            "current": self.current.to_dict() if self.current else None,
        }

    def describe(self) -> str:
        files = f"{self.files_done}/{self.files_total} files"
        if self.bytes_total > 0:
            size = (
                f"{format_bytes(self.bytes_done)}/{format_bytes(self.bytes_total)}"
                f"{_percent(self.bytes_done, self.bytes_total)}"
            )
        else:
            size = format_bytes(self.bytes_done)
        text = f"{files}, {size}, {self.failures} failed"
        if self.current is not None:
            text += f"; copying {self.current.describe()}"
        return text


class ProgressRegistry:
    """The runs that are going on, per kind and file system."""

    def __init__(self) -> None:
        self._runs: Dict[tuple[str, str], RunProgress] = {}

    def register(self, run: RunProgress) -> RunProgress:
        self._runs[(run.kind, run.filesystem)] = run
        return run

    def unregister(self, run: RunProgress) -> None:
        if self._runs.get((run.kind, run.filesystem)) is run:
            del self._runs[(run.kind, run.filesystem)]

    def get(self, kind: str, filesystem: str) -> Optional[RunProgress]:
        return self._runs.get((kind, filesystem))

    def snapshot(self) -> Dict[str, Dict[str, dict]]:
        res: Dict[str, Dict[str, dict]] = {"backfill": {}, "replication": {}}
        for (kind, filesystem), run in sorted(self._runs.items()):
            res.setdefault(kind, {})[filesystem] = run.to_dict()
        return res


progress_registry = ProgressRegistry()

# Starts the progress of one copy: called with the store key and the part
# sizes of the source, once per store a version is copied into.
CopyStarter = Callable[[str, List[int]], CopyProgress]
