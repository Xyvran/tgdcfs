"""Local cache on the data volume: write staging and read cache.

Every byte a mirror needs used to be read back from the primary store
first: a Telegram primary with a Discord mirror downloads each gigabyte
once per mirror and uploads it again. The cache keeps a copy of a
version on the local disk while it is written, so the mirrors read it
from there; kept after replication it serves repeated reads as well.

Where it sits
-------------

Inside the file-content repository, below the encryption decorator, at
the level the replication engine copies bytes verbatim. The cache holds
exactly what the stores hold: ciphertext when encryption is on, never
plaintext.

Unit and layout
---------------

Entries are keyed by *version id*, not by store message, because the
stores cut a version differently (2 GiB Telegram parts, 10 MB Discord
messages). Each entry is a sparse data file ``<id>.bin`` plus a map file
``<id>.map`` with one byte per block of ``block_size``: ``1`` when the
block is on disk. A staged upload fills every block; a download fills
the blocks it fetched; a read is served for the blocks that are present.
Versions are immutable, so an entry can never be stale.

Budget
------

``max_size`` (bytes present, plus the full size of entries still being
written), ``max_files`` (entries) and ``max_file_size`` (larger versions
are never cached). Eviction is LRU by last access over *unpinned*
entries; a pinned entry (its mirrors still need it) is never evicted.
When the budget cannot be met a new entry is refused, and the caller
carries on without the cache. Every disk error is handled the same way:
the cache steps aside, the transfer goes on.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import tempfile
import time
from dataclasses import dataclass, field
from typing import AsyncIterator, Callable, Dict, Iterable, List, Optional, Protocol

from tgdcfs.config import CacheConfig
from tgdcfs.reqres import UploadableFileMessage

logger = logging.getLogger(__name__)

INDEX_FILE = "index.json"
INDEX_VERSION = 1
# How much of a cached entry a single read hands out per chunk.
READ_CHUNK = 1024 * 1024
# A warning per refused entry would flood the log during a bulk upload
# into a full cache; one every so often is enough to notice.
REFUSAL_LOG_INTERVAL = 60.0


class VersionBytes(Protocol):
    """A complete, seekable copy of a version's bytes."""

    @property
    def size(self) -> int: ...

    def read(self, begin: int, end: int) -> AsyncIterator[bytes]:
        """Stream the inclusive byte range ``[begin, end]``."""
        ...


@dataclass
class CacheEntry:
    version_id: str
    fs: str
    size: int
    block_size: int
    present: bytearray
    last_access: float
    pins: int = 0
    # True while a StagingWriter appends to it: the whole size is charged
    # against the budget, not just the blocks written so far.
    writing: bool = False

    @property
    def blocks(self) -> int:
        return len(self.present)

    def block_length(self, index: int) -> int:
        if index < self.blocks - 1:
            return self.block_size
        return self.size - index * self.block_size

    def present_bytes(self) -> int:
        return sum(self.block_length(i) for i, p in enumerate(self.present) if p)

    def charged_bytes(self) -> int:
        return self.size if self.writing else self.present_bytes()

    def is_complete(self) -> bool:
        return self.size == 0 or all(self.present)

    def has_range(self, begin: int, end: int) -> bool:
        if self.size == 0:
            return True
        if begin < 0 or end >= self.size or begin > end:
            return False
        first, last = begin // self.block_size, end // self.block_size
        return all(self.present[first : last + 1])

    def missing_runs(self, begin: int, end: int) -> List[tuple[int, int]]:
        """Block-aligned byte ranges of ``[begin, end]`` that are not on disk.

        The first and last run are widened to whole blocks, so a caller can
        fetch and store them as complete blocks. Ranges are inclusive.
        """
        runs: List[tuple[int, int]] = []
        if self.size == 0:
            return runs
        end = min(end, self.size - 1)
        first, last = begin // self.block_size, end // self.block_size
        run_start: Optional[int] = None
        for index in range(first, last + 2):
            missing = index <= last and not self.present[index]
            if missing and run_start is None:
                run_start = index
            elif not missing and run_start is not None:
                runs.append(
                    (
                        run_start * self.block_size,
                        (index - 1) * self.block_size
                        + self.block_length(index - 1)
                        - 1,
                    )
                )
                run_start = None
        return runs

    def to_dict(self) -> dict:
        return {
            "fs": self.fs,
            "size": self.size,
            "block_size": self.block_size,
            "last_access": self.last_access,
            "pins": self.pins,
        }


def blocks_for(size: int, block_size: int) -> int:
    return max(0, (size + block_size - 1) // block_size)


class StagingWriter:
    """Sequential writer into one entry; marks blocks as they complete.

    A disk error disables the writer: further writes are dropped, the
    entry is removed, and the caller never sees the failure -- the upload
    it accompanies must not fail because the cache did.
    """

    def __init__(self, cache: "LocalCache", entry: CacheEntry):
        self._cache = cache
        self._entry = entry
        self._offset = 0
        self._fd: Optional[int] = None
        self.failed = False

    @property
    def version_id(self) -> str:
        return self._entry.version_id

    @property
    def written(self) -> int:
        return self._offset

    @property
    def complete(self) -> bool:
        return not self.failed and self._offset >= self._entry.size

    async def write(self, data: bytes) -> None:
        if self.failed or not data:
            return
        try:
            await asyncio.to_thread(self._write_sync, data)
        except OSError as ex:
            logger.warning(
                f"Cache: writing {self._entry.version_id} failed ({ex}); "
                f"continuing without the cache"
            )
            await self.abort()
            return
        blocks_done = self._offset // self._entry.block_size
        if self._offset >= self._entry.size:
            blocks_done = self._entry.blocks
        self._cache._mark_present(self._entry, range(0, blocks_done))
        if self._offset >= self._entry.size:
            await self._finish()

    def _write_sync(self, data: bytes) -> None:
        if self._fd is None:
            self._fd = os.open(
                self._cache.data_path(self._entry.version_id), os.O_WRONLY
            )
        view = memoryview(data)
        while view:
            n = os.pwrite(self._fd, view, self._offset)
            self._offset += n
            view = view[n:]

    async def _finish(self) -> None:
        if self._fd is not None:
            await asyncio.to_thread(os.close, self._fd)
            self._fd = None
        self._entry.writing = False
        self._cache._save_index()

    async def abort(self) -> None:
        """Drop the entry; nothing of it is trusted any more."""
        self.failed = True
        if self._fd is not None:
            try:
                await asyncio.to_thread(os.close, self._fd)
            except OSError:
                pass
            self._fd = None
        self._entry.writing = False
        self._cache.remove(self._entry.version_id)

    async def close(self) -> None:
        """Called when the source is exhausted; a short write is discarded."""
        if self.failed:
            return
        if self._offset >= self._entry.size:
            if self._fd is not None:
                await self._finish()
            return
        logger.warning(
            f"Cache: {self._entry.version_id} ended after {self._offset} of "
            f"{self._entry.size} bytes; dropping the entry"
        )
        await self.abort()


@dataclass
class StagedFileMessage(UploadableFileMessage):
    """An upload message whose reads are copied into the cache as they go.

    Wraps the message the uploader consumes; every ``read`` also lands in
    the staging writer, so the client's upload streams to the primary at
    full speed while the copy on disk grows alongside it.
    """

    inner: UploadableFileMessage = field(init=False)
    writer: StagingWriter = field(init=False)

    @classmethod
    def wrap(
        cls, inner: UploadableFileMessage, writer: StagingWriter
    ) -> "StagedFileMessage":
        obj = cls(
            name=inner.name,
            size=inner.get_size(),
            caption=inner.caption,
            tags=inner.tags,
            _offset=0,
            _read_size=0,
            task_tracker=inner.task_tracker,
            version_id=inner.version_id,
        )
        obj.inner = inner
        obj.writer = writer
        return obj

    async def open(self) -> None:
        await self.inner.open()

    async def read(self, length: int) -> bytes:
        data = await self.inner.read(length)
        if data:
            await self.writer.write(data)
        return data

    async def close(self) -> None:
        await self.inner.close()

    def file_name(self) -> str:
        return self.name or self.inner.file_name()

    def next_part(self, part_size: int) -> None:
        self.inner.next_part(part_size)
        self._offset += part_size
        self._read_size = 0


class CachedVersionBytes:
    """``VersionBytes`` over a complete cache entry."""

    def __init__(self, cache: "LocalCache", entry: CacheEntry):
        self._cache = cache
        self._entry = entry

    @property
    def size(self) -> int:
        return self._entry.size

    def read(self, begin: int, end: int) -> AsyncIterator[bytes]:
        return self._cache.read(self._entry.version_id, begin, end)


class LocalCache:
    def __init__(self, config: CacheConfig, directory: Optional[str] = None):
        self.config = config
        self.directory = directory or config.directory
        self._entries: Dict[str, CacheEntry] = {}
        self._last_refusal_log = 0.0
        self.hits = 0
        self.misses = 0

    # -- paths and index ---------------------------------------------------

    def data_path(self, version_id: str) -> str:
        return os.path.join(self.directory, f"{version_id}.bin")

    def map_path(self, version_id: str) -> str:
        return os.path.join(self.directory, f"{version_id}.map")

    @property
    def index_path(self) -> str:
        return os.path.join(self.directory, INDEX_FILE)

    def load(self) -> None:
        """Read the index and the block maps; rebuild what the index lacks.

        A data file without an index entry, or without a map, is removed:
        nothing can say which of its bytes are real.
        """
        os.makedirs(self.directory, exist_ok=True)
        index: dict = {}
        try:
            with open(self.index_path, "r", encoding="utf-8") as fh:
                loaded = json.load(fh)
            if loaded.get("version") == INDEX_VERSION:
                index = loaded.get("entries") or {}
        except FileNotFoundError:
            pass
        except Exception as ex:
            logger.error(f"Cache: could not read {self.index_path} ({ex}); rebuilding")

        self._entries = {}
        for version_id, data in index.items():
            entry = self._load_entry(version_id, data)
            if entry is not None:
                self._entries[version_id] = entry
        self._sweep_orphans()
        self._save_index()
        logger.info(
            f"Cache: {len(self._entries)} entr{'y' if len(self._entries) == 1 else 'ies'}, "
            f"{self.used_bytes()} of {self.config.max_size_bytes or 'unlimited'} bytes "
            f"in {self.directory}"
        )

    def _load_entry(self, version_id: str, data: dict) -> Optional[CacheEntry]:
        try:
            size = int(data["size"])
            block_size = int(data["block_size"])
            with open(self.map_path(version_id), "rb") as fh:
                present = bytearray(fh.read())
            if len(present) != blocks_for(size, block_size):
                raise ValueError("block map does not match the size")
            if os.path.getsize(self.data_path(version_id)) < size:
                raise ValueError("data file is shorter than the entry")
            mtime = os.path.getmtime(self.data_path(version_id))
        except Exception as ex:
            logger.warning(f"Cache: dropping entry {version_id}: {ex}")
            self._unlink(version_id)
            return None
        # Pins are re-derived by the replication queue after a restart; an
        # entry that was still being written when the process died holds
        # an unknown tail and is dropped like any short write.
        if data.get("writing"):
            self._unlink(version_id)
            return None
        return CacheEntry(
            version_id=version_id,
            fs=str(data.get("fs", "")),
            size=size,
            block_size=block_size,
            present=present,
            last_access=float(data.get("last_access", mtime)),
            pins=0,
        )

    def _sweep_orphans(self) -> None:
        try:
            names = os.listdir(self.directory)
        except OSError:
            return
        for name in names:
            stem, dot, ext = name.rpartition(".")
            if ext not in ("bin", "map") or not dot:
                continue
            if stem not in self._entries:
                try:
                    os.remove(os.path.join(self.directory, name))
                except OSError:
                    pass

    def _save_index(self) -> None:
        data = {
            "version": INDEX_VERSION,
            "entries": {
                version_id: {**entry.to_dict(), "writing": entry.writing}
                for version_id, entry in self._entries.items()
            },
        }
        try:
            os.makedirs(self.directory, exist_ok=True)
            fd, tmp = tempfile.mkstemp(dir=self.directory, prefix=".index-")
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                json.dump(data, fh)
            os.replace(tmp, self.index_path)
        except OSError as ex:
            logger.warning(f"Cache: could not write {self.index_path}: {ex}")

    # -- lookups -------------------------------------------------------------

    def entry(self, version_id: str) -> Optional[CacheEntry]:
        return self._entries.get(version_id)

    def complete(self, version_id: str) -> bool:
        entry = self._entries.get(version_id)
        return entry is not None and not entry.writing and entry.is_complete()

    def has_range(self, version_id: str, begin: int, end: int) -> bool:
        entry = self._entries.get(version_id)
        return entry is not None and entry.has_range(begin, end)

    def local(self, version_id: str) -> Optional[VersionBytes]:
        """The version as a seekable source, when it is complete on disk."""
        entry = self._entries.get(version_id)
        if entry is None or entry.writing or not entry.is_complete():
            return None
        return CachedVersionBytes(self, entry)

    def used_bytes(self) -> int:
        return sum(entry.charged_bytes() for entry in self._entries.values())

    def stats(self) -> dict:
        pinned = [e for e in self._entries.values() if e.pins > 0]
        return {
            "enabled": self.config.enabled,
            "directory": self.directory,
            "entries": len(self._entries),
            "complete_entries": sum(
                1 for e in self._entries.values() if e.is_complete() and not e.writing
            ),
            "used_bytes": self.used_bytes(),
            "max_size_bytes": self.config.max_size_bytes,
            "max_files": self.config.max_files,
            "pinned_entries": len(pinned),
            "pinned_bytes": sum(e.charged_bytes() for e in pinned),
            "writing_entries": sum(1 for e in self._entries.values() if e.writing),
            "hits": self.hits,
            "misses": self.misses,
            "per_filesystem": self._per_filesystem(),
        }

    def _per_filesystem(self) -> Dict[str, dict]:
        res: Dict[str, dict] = {}
        for entry in self._entries.values():
            fs = res.setdefault(entry.fs, {"entries": 0, "bytes": 0, "pinned": 0})
            fs["entries"] += 1
            fs["bytes"] += entry.charged_bytes()
            if entry.pins > 0:
                fs["pinned"] += 1
        return res

    # -- budget ---------------------------------------------------------------

    def _fits(self, size: int) -> bool:
        cfg = self.config
        if cfg.max_file_size_bytes and size > cfg.max_file_size_bytes:
            return False
        return True

    def _make_room(self, size: int) -> bool:
        """Evict unpinned entries, oldest first, until ``size`` more bytes fit."""
        cfg = self.config
        while True:
            over_bytes = (
                cfg.max_size_bytes and self.used_bytes() + size > cfg.max_size_bytes
            )
            over_files = cfg.max_files and len(self._entries) + 1 > cfg.max_files
            if not over_bytes and not over_files:
                return True
            victims = [
                e for e in self._entries.values() if e.pins == 0 and not e.writing
            ]
            if not victims:
                return False
            victim = min(victims, key=lambda e: e.last_access)
            logger.info(
                f"Cache: evicting {victim.version_id} ({victim.charged_bytes()} bytes)"
            )
            self.remove(victim.version_id)

    def _refuse(self, reason: str) -> None:
        now = time.monotonic()
        if now - self._last_refusal_log >= REFUSAL_LOG_INTERVAL:
            self._last_refusal_log = now
            logger.warning(f"Cache: not caching a version: {reason}")

    # -- entries ---------------------------------------------------------------

    def open_staging(
        self, fs: str, version_id: str, size: int
    ) -> Optional[StagingWriter]:
        """Start an entry for ``version_id`` to be written front to back.

        ``None`` when the cache is off, the version is too large or the
        budget cannot be met without evicting pinned entries.
        """
        if not self.config.enabled or size < 0:
            return None
        if version_id in self._entries:
            self.remove(version_id)
        if not self._fits(size):
            self._refuse(f"{size} bytes exceed max_file_size")
            return None
        if not self._make_room(size):
            self._refuse("the budget is taken by entries the mirrors still need")
            return None
        entry = CacheEntry(
            version_id=version_id,
            fs=fs,
            size=size,
            block_size=self.config.block_bytes,
            present=bytearray(blocks_for(size, self.config.block_bytes)),
            last_access=time.time(),
            writing=True,
        )
        try:
            os.makedirs(self.directory, exist_ok=True)
            with open(self.data_path(version_id), "wb") as fh:
                fh.truncate(size)
            with open(self.map_path(version_id), "wb") as fh:
                fh.write(bytes(entry.present))
        except OSError as ex:
            self._refuse(f"cannot create files in {self.directory}: {ex}")
            self._unlink(version_id)
            return None
        self._entries[version_id] = entry
        self._save_index()
        return StagingWriter(self, entry)

    def _mark_present(self, entry: CacheEntry, blocks: Iterable[int]) -> None:
        changed = [i for i in blocks if not entry.present[i]]
        if not changed:
            return
        for i in changed:
            entry.present[i] = 1
        try:
            fd = os.open(self.map_path(entry.version_id), os.O_WRONLY)
            try:
                for i in changed:
                    os.pwrite(fd, b"\x01", i)
            finally:
                os.close(fd)
        except OSError as ex:
            logger.warning(
                f"Cache: could not update the block map of {entry.version_id}: {ex}"
            )

    async def write_blocks(self, version_id: str, begin: int, data: bytes) -> None:
        """Store whole blocks starting at the block-aligned offset ``begin``.

        For read-cache fills. Only blocks the data covers completely (or
        the final block of the version) are marked present; a short tail
        is written but not trusted.
        """
        entry = self._entries.get(version_id)
        if entry is None or entry.writing or not data:
            return
        if begin % entry.block_size != 0 or begin + len(data) > entry.size:
            return
        try:
            await asyncio.to_thread(
                self._pwrite, self.data_path(version_id), begin, data
            )
        except OSError as ex:
            logger.warning(f"Cache: writing blocks of {version_id} failed: {ex}")
            self.remove(version_id)
            return
        first = begin // entry.block_size
        end_offset = begin + len(data)
        last_full = end_offset // entry.block_size  # exclusive
        if end_offset >= entry.size:
            last_full = entry.blocks
        self._mark_present(entry, range(first, last_full))
        entry.last_access = time.time()

    @staticmethod
    def _pwrite(path: str, offset: int, data: bytes) -> None:
        fd = os.open(path, os.O_WRONLY)
        try:
            view = memoryview(data)
            while view:
                n = os.pwrite(fd, view, offset)
                offset += n
                view = view[n:]
        finally:
            os.close(fd)

    def reserve(self, fs: str, version_id: str, size: int) -> Optional[CacheEntry]:
        """An empty entry for read-cache fills; ``None`` when it does not fit."""
        writer = self.open_staging(fs, version_id, size)
        if writer is None:
            return None
        entry = self._entries[version_id]
        entry.writing = False
        self._save_index()
        return entry

    async def read(self, version_id: str, begin: int, end: int) -> AsyncIterator[bytes]:
        """Stream ``[begin, end]`` of an entry; the range must be present."""
        entry = self._entries.get(version_id)
        if entry is None:
            raise KeyError(version_id)
        if end < 0 or end >= entry.size:
            end = entry.size - 1
        if not entry.has_range(begin, end):
            raise KeyError(f"{version_id}: bytes {begin}-{end} are not cached")
        entry.last_access = time.time()
        offset = begin
        path = self.data_path(version_id)
        while offset <= end:
            length = min(READ_CHUNK, end - offset + 1)
            chunk = await asyncio.to_thread(self._pread, path, offset, length)
            if not chunk:
                raise OSError(f"{path}: short read at {offset}")
            offset += len(chunk)
            yield chunk

    @staticmethod
    def _pread(path: str, offset: int, length: int) -> bytes:
        fd = os.open(path, os.O_RDONLY)
        try:
            return os.pread(fd, length, offset)
        finally:
            os.close(fd)

    def pin(self, version_id: str) -> None:
        if (entry := self._entries.get(version_id)) is not None:
            entry.pins += 1

    def unpin(self, version_id: str) -> None:
        if (entry := self._entries.get(version_id)) is not None:
            entry.pins = max(0, entry.pins - 1)

    def release(self, version_id: str) -> None:
        """The mirrors are done with a version: unpin it, and drop it unless
        the entry is kept for reads."""
        entry = self._entries.get(version_id)
        if entry is None:
            return
        entry.pins = 0
        if not self.config.keep_for_reads:
            self.remove(version_id)
        else:
            self._save_index()

    def remove(self, version_id: str) -> None:
        if self._entries.pop(version_id, None) is None:
            return
        self._unlink(version_id)
        self._save_index()

    def remove_many(self, version_ids: Iterable[str]) -> None:
        for version_id in list(version_ids):
            self.remove(version_id)

    def _unlink(self, version_id: str) -> None:
        for path in (self.data_path(version_id), self.map_path(version_id)):
            try:
                os.remove(path)
            except FileNotFoundError:
                pass
            except OSError as ex:
                logger.warning(f"Cache: could not remove {path}: {ex}")

    def evict_unpinned(self) -> int:
        """Drop every entry no mirror is waiting for; returns how many."""
        victims = [
            e.version_id
            for e in self._entries.values()
            if e.pins == 0 and not e.writing
        ]
        self.remove_many(victims)
        return len(victims)


class VersionCache:
    """The cache as seen by one version being written or mirrored.

    Bundles what the replication engine needs: the local copy when there
    is one, a writer that fills the cache from a download when there is
    none yet, and the release once every mirror has its copy.
    """

    def __init__(self, cache: LocalCache, fs: str, version_id: str, size: int):
        self._cache = cache
        self._fs = fs
        self.version_id = version_id
        self.size = size
        self._filler: Optional[StagingWriter] = None

    @property
    def cache(self) -> LocalCache:
        return self._cache

    def local(self) -> Optional[VersionBytes]:
        return self._cache.local(self.version_id)

    def filler(self) -> Optional[StagingWriter]:
        """A writer to fill the cache from a download, when worth keeping."""
        if not self._cache.config.keep_for_reads or self.size <= 0:
            return None
        if self._cache.entry(self.version_id) is not None:
            return None
        self._filler = self._cache.open_staging(self._fs, self.version_id, self.size)
        if self._filler is not None:
            self._cache.pin(self.version_id)
        return self._filler

    def release(self) -> None:
        self._cache.release(self.version_id)


def version_cache_for(
    cache: Optional[LocalCache], fs: str, version_id: Optional[str], size: int
) -> Optional[VersionCache]:
    """The version's view of the cache; ``None`` when there is nothing to
    cache under: the cache is off, or the bytes are no version (the
    metadata blob travels through the same repository)."""
    if cache is None or not cache.config.enabled or not version_id or size <= 0:
        return None
    return VersionCache(cache, fs, version_id, size)


CacheProvider = Callable[[], Optional[LocalCache]]
