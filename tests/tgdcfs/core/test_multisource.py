"""Multi-source reads: several stores share one download (plan, 4.11)."""

import asyncio
from typing import cast

import pytest

from tgdcfs.backends.base import IStore
from tgdcfs.config import Config, FilesystemConfig
from tgdcfs.core.api import DirectoryApi, FileApi, FileDescApi, MetaDataApi
from tgdcfs.core.mirror import MirrorGroup, MirrorStore
from tgdcfs.core.multisource import MultiSourceRead, StoreView, read_slots
from tgdcfs.core.repository.impl import (
    PinnedMessageMetadataRepository,
    StoreFDRepository,
    StoreFileContentRepository,
)
from tgdcfs.errors import TechnicalError
from tgdcfs.reqres import FileMessageFromBuffer
from tests.fakes.store import FakeStore
from tests.tgdcfs.config.test_stores import current_layout
from tests.tgdcfs.core.test_local_cache import RecordingStore

DATA = bytes(range(256)) * 40  # 10240 bytes


class SlowStore(RecordingStore):
    """A store whose downloads take a while, to make speed differences visible."""

    def __post_init__(self):
        super().__post_init__()
        self.delay = 0.0

    async def download_file(self, message_id: int, begin: int, end: int):
        if self.delay:
            await asyncio.sleep(self.delay)
        return await super().download_file(message_id, begin, end)


def store_with(data: bytes, key: str, part: int, backend="telegram") -> SlowStore:
    # A tiny delay stands in for the network: an in-memory store that never
    # suspends would take every piece before another store gets a turn.
    store = SlowStore(key=key, backend_name=backend, max_part_bytes=part)
    store.delay = 0.001
    ids = []
    for i in range(0, len(data), part):
        ids.append(store._add(document=data[i : i + part], name=f"part{i}"))
    store.layout = ids  # type: ignore[attr-defined]
    return store


def view_of(store: SlowStore, data: bytes, part: int, slots: int = 2) -> StoreView:
    ids = store.layout  # type: ignore[attr-defined]
    sizes = [min(part, len(data) - i) for i in range(0, len(data), part)]
    return StoreView(store, ids, sizes, slots=slots)


def read(views, begin=0, end=len(DATA) - 1, piece=1000, window=4, size=len(DATA)):
    return MultiSourceRead(
        views,
        size,
        "v",
        begin,
        end,
        piece,
        window,
        StoreFileContentRepository._map_range,
        "a.bin",
    )


async def collect(stream) -> bytes:
    return b"".join([c async for c in stream])


class TestScheduler:
    async def test_pieces_from_several_stores_arrive_in_order(self):
        a = store_with(DATA, "tg:1", 2048)
        b = store_with(DATA, "dc:9", 64, backend="discord")

        out = await collect(
            read([view_of(a, DATA, 2048), view_of(b, DATA, 64)]).stream()
        )

        assert out == DATA
        assert a.downloaded and b.downloaded  # both took pieces

    async def test_a_range_is_honoured(self):
        a = store_with(DATA, "tg:1", 2048)
        b = store_with(DATA, "tg:2", 2048)
        views = [view_of(a, DATA, 2048), view_of(b, DATA, 2048)]

        assert await collect(read(views, 1500, 7777).stream()) == DATA[1500:7778]

    async def test_a_fast_store_takes_more_pieces(self):
        fast = store_with(DATA, "tg:1", 2048)
        slow = store_with(DATA, "tg:2", 2048)
        slow.delay = 0.03

        out = await collect(
            read([view_of(fast, DATA, 2048), view_of(slow, DATA, 2048)]).stream()
        )

        assert out == DATA
        assert len(fast.downloaded) > len(slow.downloaded)

    async def test_a_failing_piece_moves_to_another_store(self):
        a = store_with(DATA, "tg:1", 2048)
        b = store_with(DATA, "tg:2", 2048)
        b.fail.add("download_file")

        scheduler = read([view_of(a, DATA, 2048), view_of(b, DATA, 2048)])
        out = await collect(scheduler.stream())

        assert out == DATA
        assert "tg:2" in scheduler.benched
        # Every piece b failed was fetched from a instead.
        assert len(a.downloaded) >= 11

    async def test_read_fails_only_when_no_store_can_serve_a_piece(self):
        a = store_with(DATA, "tg:1", 2048)
        b = store_with(DATA, "tg:2", 2048)
        a.fail.add("download_file")
        b.fail.add("download_file")

        with pytest.raises(TechnicalError, match="No store can serve|gave up"):
            await collect(
                read([view_of(a, DATA, 2048), view_of(b, DATA, 2048)]).stream()
            )

    async def test_a_short_answer_is_a_failure_not_silent_truncation(self):
        a = store_with(DATA, "tg:1", 2048)
        b = store_with(DATA, "tg:2", 2048)

        real = b.download_file

        async def truncated(message_id, begin, end):
            resp = await real(message_id, begin, min(end, begin + 10))
            return resp

        b.download_file = truncated  # type: ignore[method-assign]
        out = await collect(
            read([view_of(a, DATA, 2048), view_of(b, DATA, 2048)]).stream()
        )

        assert out == DATA

    async def test_the_window_bounds_how_far_ahead_the_stores_run(self):
        a = store_with(DATA, "tg:1", 2048)
        b = store_with(DATA, "tg:2", 2048)
        scheduler = read([view_of(a, DATA, 2048), view_of(b, DATA, 2048)], window=2)
        stream = scheduler.stream()

        first = await stream.__anext__()
        await asyncio.sleep(0.02)

        assert first == DATA[:1000]
        # At most the window can be complete and waiting.
        assert len(scheduler._results) <= 2
        assert await collect(stream) == DATA[1000:]

    async def test_abandoning_a_read_stops_the_workers(self):
        a = store_with(DATA, "tg:1", 2048)
        b = store_with(DATA, "tg:2", 2048)
        b.delay = 0.05
        stream = read([view_of(a, DATA, 2048), view_of(b, DATA, 2048)]).stream()
        await stream.__anext__()

        await stream.aclose()

        tasks = [
            t
            for t in asyncio.all_tasks()
            if not t.done() and t is not asyncio.current_task()
        ]
        assert tasks == []

    def test_read_slots_come_from_the_store(self):
        class Slotted(FakeStore):
            @property
            def read_slots(self) -> int:
                return 7

        assert read_slots(Slotted(key="x:1")) == 7
        assert read_slots(FakeStore(key="x:2")) == 2


class TestConfig:
    def test_defaults(self):
        fs = FilesystemConfig.from_dict("media", {"primary": "a"})
        assert fs.read_parallel is False and fs.read_sources == []

    def test_read_sources_must_be_own_stores(self):
        data = current_layout()
        data["filesystems"]["media"]["read_parallel"] = True
        data["filesystems"]["media"]["read_sources"] = ["nope"]
        with pytest.raises(ValueError, match="read_sources"):
            Config.from_dict(data)
        primary = data["filesystems"]["media"]["primary"]
        data["filesystems"]["media"]["read_sources"] = [primary]
        fs = Config.from_dict(data).filesystems["media"]
        assert fs.read_parallel is True and fs.read_sources == [primary]


# -- through the repository ---------------------------------------------------


class Stack:
    def __init__(self, primary: RecordingStore, mirror: RecordingStore, **repo):
        self.primary, self.mirror = primary, mirror
        self.mirror_group = MirrorGroup(
            primary=primary,
            stores=[MirrorStore(key=mirror.key, store=mirror)],
            mode="auto",
            strict=True,
        )
        self.fc_repo = StoreFileContentRepository(
            primary, mirror_group=self.mirror_group, inline_mirroring=True, **repo
        )
        self.fd_repo = StoreFDRepository(primary, mirror_group=self.mirror_group)
        self.metadata_api = MetaDataApi(
            PinnedMessageMetadataRepository(
                primary, self.fc_repo, mirror_group=self.mirror_group
            )
        )
        self.file_api = FileApi(
            self.metadata_api,
            FileDescApi(self.fd_repo, self.fc_repo),
            cast(IStore, primary),
            mirror_group=self.mirror_group,
            inline_mirroring=True,
        )
        self.dir_api = DirectoryApi(self.metadata_api, self.file_api, primary)

    async def init(self):
        await self.metadata_api.init()
        return self

    async def put(self, name, data):
        return await self.file_api.upload(
            self.metadata_api.get_root_directory(),
            FileMessageFromBuffer.new(buffer=data, name=name),
        )

    async def read(self, name, begin=0, end=-1):
        fr = self.metadata_api.get_root_directory().find_file(name)
        stream = await self.file_api.retrieve(fr, begin, end, name)
        return b"".join([c async for c in stream])

    async def version(self, name):
        fr = self.metadata_api.get_root_directory().find_file(name)
        return (await self.fd_repo.get(fr)).get_latest_version()


def parts_from(store: RecordingStore, ids) -> list[int]:
    return [m for m in store.downloaded if m in ids]


def slow_primary() -> SlowStore:
    store = SlowStore(key="tg:1", backend_name="telegram", max_part_bytes=2048)
    store.delay = 0.001
    return store


def slow_mirror() -> SlowStore:
    store = SlowStore(
        key="dc:9", backend_name="discord", max_part_bytes=64, server_copy=False
    )
    store.delay = 0.001
    return store


class TestThroughTheRepository:
    async def test_a_large_read_is_shared_between_primary_and_replica(self):
        primary = slow_primary()
        mirror = slow_mirror()
        stack = await Stack(
            primary,
            mirror,
            read_parallel=True,
            piece_size=1000,
            parallel_threshold=2000,
        ).init()
        await stack.put("a.bin", DATA)
        version = await stack.version("a.bin")
        primary.downloaded.clear()
        mirror.downloaded.clear()

        assert await stack.read("a.bin") == DATA

        assert parts_from(primary, version.message_ids) != []
        assert parts_from(mirror, version.replicas["dc:9"].message_ids) != []

    async def test_a_small_read_stays_on_one_store(self):
        primary = slow_primary()
        mirror = slow_mirror()
        stack = await Stack(
            primary,
            mirror,
            read_parallel=True,
            piece_size=1000,
            parallel_threshold=2000,
        ).init()
        await stack.put("a.bin", DATA)
        version = await stack.version("a.bin")
        mirror.downloaded.clear()

        assert await stack.read("a.bin", 100, 1500) == DATA[100:1501]

        assert parts_from(mirror, version.replicas["dc:9"].message_ids) == []

    async def test_read_sources_limits_the_participants(self):
        primary = slow_primary()
        mirror = slow_mirror()
        stack = await Stack(
            primary,
            mirror,
            read_parallel=True,
            read_sources=["tg:1"],
            piece_size=1000,
            parallel_threshold=2000,
        ).init()
        await stack.put("a.bin", DATA)
        version = await stack.version("a.bin")
        mirror.downloaded.clear()

        assert await stack.read("a.bin") == DATA

        assert parts_from(mirror, version.replicas["dc:9"].message_ids) == []

    async def test_off_by_default(self):
        primary = slow_primary()
        mirror = slow_mirror()
        stack = await Stack(
            primary, mirror, piece_size=1000, parallel_threshold=2000
        ).init()
        await stack.put("a.bin", DATA)
        version = await stack.version("a.bin")
        mirror.downloaded.clear()

        assert await stack.read("a.bin") == DATA

        assert parts_from(mirror, version.replicas["dc:9"].message_ids) == []

    async def test_a_dead_primary_leaves_the_replica_to_finish(self):
        primary = slow_primary()
        mirror = slow_mirror()
        stack = await Stack(
            primary,
            mirror,
            read_parallel=True,
            piece_size=1000,
            parallel_threshold=2000,
        ).init()
        await stack.put("a.bin", DATA)
        primary.fail.add("download_file")

        assert await stack.read("a.bin") == DATA
