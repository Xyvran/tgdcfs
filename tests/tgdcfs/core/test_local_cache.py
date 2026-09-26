"""The local cache: entries on disk, the budget, and mirrors reading from it.

The unit tests drive ``LocalCache`` directly against a temporary
directory. The integration tests run the real ``FileApi`` /
``StoreFileContentRepository`` / ``MirrorGroup`` stack on in-memory
stores, the way ``test_replication.py`` does, and check that a staged
upload spares the mirror every download from the primary.
"""

import os
from typing import cast

import pytest

from tgdcfs.backends.base import IStore
from tgdcfs.config import CacheConfig
from tgdcfs.core.api import DirectoryApi, FileApi, FileDescApi, MetaDataApi
from tgdcfs.core.local_cache import CacheEntry, LocalCache, StagedFileMessage
from tgdcfs.core.mirror import MirrorGroup, MirrorStore
from tgdcfs.core.model import TGFSDirectory, TGFSFileVersion
from tgdcfs.core.replication import ReplicationQueue, ReplicationWorker, file_path
from tgdcfs.core.repository.impl import (
    PinnedMessageMetadataRepository,
    StoreFDRepository,
    StoreFileContentRepository,
)
from tgdcfs.reqres import FileMessageFromBuffer
from tests.fakes.store import FakeStore

BLOCK = 64 * 1024  # the smallest block the config accepts


def cache_config(**overrides) -> CacheConfig:
    data = {
        "enabled": True,
        "max_size_mb": 0,
        "max_files": 0,
        "max_file_size_mb": 0,
        "block_kb": 64,
        "stage_uploads": True,
        "keep_for_reads": True,
    }
    data.update(overrides)
    return CacheConfig.from_dict(data)


def make_cache(tmp_path, **overrides) -> LocalCache:
    cache = LocalCache(cache_config(**overrides), directory=str(tmp_path / "cache"))
    cache.load()
    return cache


async def stage(cache: LocalCache, version_id: str, data: bytes, fs: str = "fs"):
    writer = cache.open_staging(fs, version_id, len(data))
    assert writer is not None
    # Chunks that do not line up with the blocks, as a network stream.
    for i in range(0, len(data), 100_000):
        await writer.write(data[i : i + 100_000])
    await writer.close()
    return writer


async def read_all(cache: LocalCache, version_id: str, begin=0, end=-1) -> bytes:
    return b"".join([c async for c in cache.read(version_id, begin, end)])


def entry_of(cache: LocalCache, version_id: str) -> CacheEntry:
    entry = cache.entry(version_id)
    assert entry is not None
    return entry


class TestEntries:
    async def test_staged_bytes_come_back(self, tmp_path):
        cache = make_cache(tmp_path)
        data = os.urandom(3 * BLOCK + 123)

        await stage(cache, "v1", data)

        assert cache.complete("v1")
        assert await read_all(cache, "v1") == data
        assert (
            await read_all(cache, "v1", BLOCK + 5, 2 * BLOCK + 7)
            == data[BLOCK + 5 : 2 * BLOCK + 8]
        )

    async def test_blocks_become_present_as_the_write_advances(self, tmp_path):
        cache = make_cache(tmp_path)
        writer = cache.open_staging("fs", "v1", 3 * BLOCK)
        assert writer is not None

        await writer.write(b"a" * (BLOCK + 10))

        entry = cache.entry("v1")
        assert entry is not None
        assert list(entry.present) == [1, 0, 0]
        assert not cache.complete("v1")
        assert cache.has_range("v1", 0, BLOCK - 1)
        assert not cache.has_range("v1", 0, BLOCK)

    async def test_a_short_write_is_dropped_on_close(self, tmp_path):
        cache = make_cache(tmp_path)
        writer = cache.open_staging("fs", "v1", 2 * BLOCK)
        assert writer is not None
        await writer.write(b"a" * BLOCK)

        await writer.close()

        assert cache.entry("v1") is None
        assert not os.path.exists(cache.data_path("v1"))

    async def test_entries_survive_a_restart(self, tmp_path):
        cache = make_cache(tmp_path)
        data = os.urandom(2 * BLOCK)
        await stage(cache, "v1", data)
        partial = cache.open_staging("fs", "v2", 2 * BLOCK)
        assert partial is not None
        await partial.write(b"b" * (2 * BLOCK))
        await partial.close()

        reloaded = make_cache(tmp_path)

        assert reloaded.complete("v1") and reloaded.complete("v2")
        assert await read_all(reloaded, "v1") == data
        # Pins are not persisted; the replication queue re-derives them.
        assert entry_of(reloaded, "v1").pins == 0

    async def test_an_entry_still_being_written_is_dropped_on_restart(self, tmp_path):
        cache = make_cache(tmp_path)
        writer = cache.open_staging("fs", "v1", 2 * BLOCK)
        assert writer is not None
        await writer.write(b"a" * BLOCK)

        reloaded = make_cache(tmp_path)

        assert reloaded.entry("v1") is None
        assert not os.path.exists(cache.data_path("v1"))

    async def test_files_without_an_index_entry_are_swept(self, tmp_path):
        cache = make_cache(tmp_path)
        (tmp_path / "cache" / "orphan.bin").write_bytes(b"x")
        (tmp_path / "cache" / "orphan.map").write_bytes(b"\x01")
        (tmp_path / "cache" / "notes.txt").write_text("keep")

        make_cache(tmp_path)

        assert not (tmp_path / "cache" / "orphan.bin").exists()
        assert (tmp_path / "cache" / "notes.txt").exists()

    async def test_disabled_cache_stages_nothing(self, tmp_path):
        cache = make_cache(tmp_path, enabled=False)
        assert cache.open_staging("fs", "v1", 10) is None

    async def test_a_disk_error_disables_the_writer_not_the_upload(
        self, tmp_path, monkeypatch
    ):
        cache = make_cache(tmp_path)
        writer = cache.open_staging("fs", "v1", 2 * BLOCK)
        assert writer is not None

        def broken_pwrite(*args, **kwargs):
            raise OSError(28, "No space left on device")

        monkeypatch.setattr(os, "pwrite", broken_pwrite)
        await writer.write(b"a" * BLOCK)

        assert writer.failed
        assert cache.entry("v1") is None
        # Further writes are silently dropped.
        await writer.write(b"b" * BLOCK)
        await writer.close()


class TestBudget:
    async def test_lru_eviction_makes_room(self, tmp_path):
        cache = make_cache(tmp_path, max_size_mb=1, block_kb=256)
        quarter = 256 * 1024
        await stage(cache, "old", b"a" * quarter)
        await stage(cache, "mid", b"b" * quarter)
        await stage(cache, "new", b"c" * quarter)
        entry_of(cache, "old").last_access = 1
        entry_of(cache, "mid").last_access = 2
        entry_of(cache, "new").last_access = 3

        writer = cache.open_staging("fs", "big", 2 * quarter)

        assert writer is not None
        assert cache.entry("old") is None
        assert cache.entry("mid") is not None and cache.entry("new") is not None

    async def test_pinned_entries_are_never_evicted(self, tmp_path):
        cache = make_cache(tmp_path, max_size_mb=1, block_kb=256)
        await stage(cache, "pinned", b"a" * (768 * 1024))
        cache.pin("pinned")

        assert cache.open_staging("fs", "next", 512 * 1024) is None
        assert cache.entry("pinned") is not None

        cache.release("pinned")
        assert cache.open_staging("fs", "next", 512 * 1024) is not None

    async def test_max_files(self, tmp_path):
        cache = make_cache(tmp_path, max_files=2)
        await stage(cache, "a", b"a" * 10)
        await stage(cache, "b", b"b" * 10)
        entry_of(cache, "a").last_access = 1

        await stage(cache, "c", b"c" * 10)

        assert cache.entry("a") is None
        assert cache.entry("b") is not None and cache.entry("c") is not None

    async def test_versions_above_the_file_limit_are_not_cached(self, tmp_path):
        cache = make_cache(tmp_path, max_file_size_mb=1)
        assert cache.open_staging("fs", "huge", 2 * 1024 * 1024) is None
        assert cache.open_staging("fs", "small", 1024) is not None

    async def test_release_without_keep_for_reads_drops_the_entry(self, tmp_path):
        cache = make_cache(tmp_path, keep_for_reads=False)
        await stage(cache, "v1", b"x" * 10)
        cache.pin("v1")

        cache.release("v1")

        assert cache.entry("v1") is None

    async def test_stats(self, tmp_path):
        cache = make_cache(tmp_path)
        await stage(cache, "v1", b"x" * 10)
        cache.pin("v1")

        stats = cache.stats()

        assert stats["entries"] == 1 and stats["pinned_entries"] == 1
        assert stats["used_bytes"] == 10 and stats["pinned_bytes"] == 10
        assert stats["per_filesystem"] == {
            "fs": {"entries": 1, "bytes": 10, "pinned": 1}
        }


class TestStagedFileMessage:
    async def test_reads_pass_through_and_land_in_the_cache(self, tmp_path):
        cache = make_cache(tmp_path)
        data = os.urandom(2 * BLOCK + 17)
        inner = FileMessageFromBuffer.new(buffer=data, name="a.bin")
        inner.version_id = "v1"
        writer = cache.open_staging("fs", "v1", len(data))
        assert writer is not None
        msg = StagedFileMessage.wrap(inner, writer)

        await msg.open()
        out = b""
        while len(out) < len(data):
            out += await msg.read(50_000)
        await msg.close()
        await writer.close()

        assert out == data
        assert msg.version_id == "v1"
        assert await read_all(cache, "v1") == data


# -- integration --------------------------------------------------------------


class Stack:
    def __init__(
        self,
        primary: "RecordingStore",
        mirror: FakeStore,
        cache: LocalCache | None,
        inline: bool = True,
        queue: ReplicationQueue | None = None,
        write_ack: str = "primary",
    ):
        self.name = "fs"
        self.primary = primary
        self.mirror = mirror
        self.cache = cache
        self.store = primary
        self.mirror_group = MirrorGroup(
            primary=primary,
            stores=[MirrorStore(key=mirror.key, store=mirror)],
            mode="auto",
            strict=True,
        )
        self.fc_repo = StoreFileContentRepository(
            primary,
            mirror_group=self.mirror_group,
            inline_mirroring=inline,
            cache=cache,
            cache_scope=self.name,
        )
        self.fd_repo = StoreFDRepository(
            primary,
            mirror_group=self.mirror_group,
            local_versions=cache.complete if cache is not None else None,
        )
        self.metadata_repo = PinnedMessageMetadataRepository(
            primary, self.fc_repo, mirror_group=self.mirror_group
        )
        self.metadata_api = MetaDataApi(self.metadata_repo)
        self.queue = queue

        async def enqueue(fr):
            assert queue is not None
            queue.enqueue(self.name, file_path(fr))

        self.file_api = FileApi(
            self.metadata_api,
            FileDescApi(self.fd_repo, self.fc_repo, write_ack=write_ack),  # type: ignore[arg-type]
            cast(IStore, primary),
            mirror_group=self.mirror_group,
            inline_mirroring=inline,
            on_written=enqueue if queue is not None else None,
        )
        self.dir_api = DirectoryApi(self.metadata_api, self.file_api, primary)

    async def init(self) -> "Stack":
        await self.metadata_api.init()
        return self

    @property
    def root(self) -> TGFSDirectory:
        return self.metadata_api.get_root_directory()

    async def put(self, name: str, data: bytes):
        return await self.file_api.upload(
            self.root, FileMessageFromBuffer.new(buffer=data, name=name)
        )

    async def read(self, name: str) -> bytes:
        fr = self.root.find_file(name)
        stream = await self.file_api.retrieve(fr, 0, -1, name)
        return b"".join([chunk async for chunk in stream])

    async def version(self, name: str) -> TGFSFileVersion:
        fr = self.root.find_file(name)
        return (await self.fd_repo.get(fr)).get_latest_version()

    def worker_client(self):
        return type(
            "C",
            (),
            {
                "name": self.name,
                "mirror_group": self.mirror_group,
                "fd_repo": self.fd_repo,
                "metadata_api": self.metadata_api,
                "dir_api": self.dir_api,
                "cache": self.cache,
                "store": self.primary,
            },
        )()


class RecordingStore(FakeStore):
    """A FakeStore that remembers which messages were downloaded.

    The metadata blob travels through the same repository and is read
    with ``download_file`` too, so "no download from the primary" has to
    be asked per message id, not per method.
    """

    def __post_init__(self):
        super().__post_init__()
        self.downloaded: list[int] = []

    async def download_file(self, message_id: int, begin: int, end: int):
        self.downloaded.append(message_id)
        return await super().download_file(message_id, begin, end)


def telegram_like() -> RecordingStore:
    return RecordingStore(key="tg:1", backend_name="telegram", max_part_bytes=2048)


def discord_like() -> FakeStore:
    return FakeStore(
        key="dc:9", backend_name="discord", max_part_bytes=64, server_copy=False
    )


def telegram_spare() -> FakeStore:
    return FakeStore(key="tg:2", backend_name="telegram", max_part_bytes=2048)


def downloaded_parts(store: RecordingStore, version: TGFSFileVersion) -> list[int]:
    return [mid for mid in store.downloaded if mid in version.message_ids]


DATA = bytes(range(256)) * 20  # 5120 bytes: 3 primary parts, 80 replica parts


class TestInlineMirroringFromTheCache:
    async def test_the_replica_is_built_without_downloading_from_the_primary(
        self, tmp_path
    ):
        cache = make_cache(tmp_path)
        stack = await Stack(telegram_like(), discord_like(), cache).init()

        await stack.put("a.bin", DATA)

        version = await stack.version("a.bin")
        assert len(version.replicas["dc:9"].message_ids) == 80
        assert downloaded_parts(stack.primary, version) == []
        # The replica holds the right bytes.
        stack.primary.fail.update({"download_file", "get_messages"})
        assert await stack.read("a.bin") == DATA
        # Kept for reads and no longer pinned once the mirror has its copy.
        assert cache.complete(version.id)
        assert entry_of(cache, version.id).pins == 0

    async def test_without_keep_for_reads_the_entry_is_gone_afterwards(self, tmp_path):
        cache = make_cache(tmp_path, keep_for_reads=False)
        stack = await Stack(telegram_like(), discord_like(), cache).init()

        await stack.put("a.bin", DATA)

        version = await stack.version("a.bin")
        assert cache.entry(version.id) is None
        assert downloaded_parts(stack.primary, version) == []

    async def test_a_re_uploaded_aligned_copy_reads_locally_too(self, tmp_path):
        cache = make_cache(tmp_path)
        spare = telegram_spare()
        spare.server_copy = False
        stack = await Stack(telegram_like(), spare, cache).init()

        await stack.put("a.bin", DATA)

        version = await stack.version("a.bin")
        assert len(version.mirrors["tg:2"]) == 3
        assert downloaded_parts(stack.primary, version) == []
        assert b"".join(spare.document_of(m) for m in version.mirrors["tg:2"]) == DATA

    async def test_a_full_cache_leaves_the_upload_unstaged(self, tmp_path):
        cache = make_cache(tmp_path, max_size_mb=1, block_kb=256)
        await stage(cache, "hog", b"h" * (1024 * 1024))
        cache.pin("hog")
        stack = await Stack(telegram_like(), discord_like(), cache).init()

        await stack.put("a.bin", DATA)

        version = await stack.version("a.bin")
        assert len(version.replicas["dc:9"].message_ids) == 80
        assert downloaded_parts(stack.primary, version) != []
        assert cache.entry(version.id) is None
        assert await stack.read("a.bin") == DATA

    async def test_a_failed_upload_leaves_no_entry(self, tmp_path):
        cache = make_cache(tmp_path)
        stack = await Stack(telegram_like(), discord_like(), cache).init()
        stack.primary.fail.add("upload")

        with pytest.raises(Exception):
            await stack.put("a.bin", DATA)

        assert cache.stats()["entries"] == 0


class TestBackgroundReplicationFromTheCache:
    async def test_the_worker_reads_the_staged_copy(self, tmp_path):
        cache = make_cache(tmp_path)
        queue = ReplicationQueue(None)
        stack = await Stack(
            telegram_like(), discord_like(), cache, inline=False, queue=queue
        ).init()
        await stack.put("a.bin", DATA)
        version = await stack.version("a.bin")
        assert cache.complete(version.id) and entry_of(cache, version.id).pins == 1

        worker = ReplicationWorker(stack.worker_client(), queue)  # type: ignore[arg-type]
        assert await worker.run_once() == 1

        version = await stack.version("a.bin")
        assert len(version.replicas["dc:9"].message_ids) == 80
        assert downloaded_parts(stack.primary, version) == []
        assert entry_of(cache, version.id).pins == 0
        stack.primary.fail.update({"download_file", "get_messages"})
        assert await stack.read("a.bin") == DATA

    async def test_an_evicted_entry_falls_back_to_the_download(self, tmp_path):
        cache = make_cache(tmp_path, keep_for_reads=False)
        queue = ReplicationQueue(None)
        stack = await Stack(
            telegram_like(), discord_like(), cache, inline=False, queue=queue
        ).init()
        await stack.put("a.bin", DATA)
        version = await stack.version("a.bin")
        cache.remove(version.id)

        worker = ReplicationWorker(stack.worker_client(), queue)  # type: ignore[arg-type]
        assert await worker.run_once() == 1

        version = await stack.version("a.bin")
        assert downloaded_parts(stack.primary, version) != []
        assert len(version.replicas["dc:9"].message_ids) == 80

    async def test_a_download_fills_the_cache_when_entries_are_kept(self, tmp_path):
        cache = make_cache(tmp_path)
        queue = ReplicationQueue(None)
        stack = await Stack(
            telegram_like(), discord_like(), cache, inline=False, queue=queue
        ).init()
        await stack.put("a.bin", DATA)
        version = await stack.version("a.bin")
        cache.remove(version.id)

        worker = ReplicationWorker(stack.worker_client(), queue)  # type: ignore[arg-type]
        assert await worker.run_once() == 1

        assert cache.complete(version.id)
        assert await read_all(cache, version.id) == DATA
        assert entry_of(cache, version.id).pins == 0

    async def test_without_a_cache_everything_works_as_before(self):
        queue = ReplicationQueue(None)
        stack = await Stack(
            telegram_like(), discord_like(), None, inline=False, queue=queue
        ).init()
        await stack.put("a.bin", DATA)

        worker = ReplicationWorker(stack.worker_client(), queue)  # type: ignore[arg-type]
        assert await worker.run_once() == 1

        assert downloaded_parts(stack.primary, await stack.version("a.bin")) != []
        assert await stack.read("a.bin") == DATA
