"""Replicas and background replication on in-memory stores.

The primary cuts files into 2 KiB parts, the "Discord-like" mirror into
64-byte messages, so every copy into the mirror is a replica with its
own layout. Reads fall over to the replica, promotion swaps the layouts,
and the queue drives all of it in the background.
"""

import json
from typing import cast

import pytest

from tgdcfs.backends.base import IStore
from tgdcfs.core.api import DirectoryApi, FileApi, FileDescApi, MetaDataApi
from tgdcfs.core.backfill import BackfillReport, backfill_file
from tgdcfs.core.mirror import MirrorGroup, MirrorStore
from tgdcfs.core.model import TGFSDirectory, TGFSFileVersion, TGFSMetadata
from tgdcfs.core.replication import (
    ReplicationQueue,
    ReplicationWorker,
    file_path,
    find_file,
)
from tgdcfs.core.repository.impl import (
    PinnedMessageMetadataRepository,
    StoreFDRepository,
    StoreFileContentRepository,
)
from tgdcfs.reqres import FileMessageFromBuffer, Replica
from tests.fakes.store import FakeStore


class Stack:
    def __init__(
        self,
        primary: FakeStore,
        mirror: FakeStore,
        inline: bool = True,
        queue: ReplicationQueue | None = None,
        read_preference=None,
    ):
        self.name = "fs"
        self.primary = primary
        self.mirror = mirror
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
            read_preference=read_preference,
        )
        self.fd_repo = StoreFDRepository(primary, mirror_group=self.mirror_group)
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
            FileDescApi(self.fd_repo, self.fc_repo),
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

    async def put(self, name: str, data: bytes, under=None):
        return await self.file_api.upload(
            under or self.root, FileMessageFromBuffer.new(buffer=data, name=name)
        )

    async def read(self, name: str, begin: int = 0, end: int = -1) -> bytes:
        fr = self.root.find_file(name)
        stream = await self.file_api.retrieve(fr, begin, end, name)
        return b"".join([chunk async for chunk in stream])

    async def version(self, name: str) -> TGFSFileVersion:
        fr = self.root.find_file(name)
        return (await self.fd_repo.get(fr)).get_latest_version()


def telegram_like() -> FakeStore:
    return FakeStore(key="tg:1", backend_name="telegram", max_part_bytes=2048)


def discord_like() -> FakeStore:
    return FakeStore(
        key="dc:9", backend_name="discord", max_part_bytes=64, server_copy=False
    )


DATA = bytes(range(256)) * 20  # 5120 bytes: 3 primary parts, 80 replica parts


class TestReplicas:
    async def test_upload_creates_a_replica_with_its_own_layout(self):
        stack = await Stack(telegram_like(), discord_like()).init()
        await stack.put("a.bin", DATA)

        version = await stack.version("a.bin")
        assert len(version.message_ids) == 3
        assert version.mirrors == {}
        replica = version.replicas["dc:9"]
        assert len(replica.message_ids) == 80
        assert replica.part_sizes == [64] * 80
        assert (
            b"".join(stack.mirror.document_of(mid) for mid in replica.message_ids)
            == DATA
        )

    async def test_replica_round_trips_through_the_descriptor(self):
        stack = await Stack(telegram_like(), discord_like()).init()
        await stack.put("a.bin", DATA)
        fr = stack.root.find_file("a.bin")
        text = stack.primary.messages[fr.message_id].text

        # Compact: sizes collapse to [common, last] with the part count;
        # 80 ids stay a plain list (packing starts above 100).
        serialized = json.loads(text)["versions"][0]["replicas"]["dc:9"]
        assert len(serialized["m"]) == 80 and "mb" not in serialized
        assert serialized["p"] == [64, 64] and serialized["n"] == 80

    async def test_reads_fall_over_to_the_replica(self):
        stack = await Stack(telegram_like(), discord_like()).init()
        await stack.put("a.bin", DATA)

        stack.primary.fail.update({"download_file", "get_messages"})

        assert await stack.read("a.bin") == DATA
        assert await stack.read("a.bin", 100, 3000) == DATA[100:3001]
        assert await stack.read("a.bin", 5000, 99999) == DATA[5000:]

    async def test_mid_stream_failover_resumes_at_the_right_byte(self):
        stack = await Stack(telegram_like(), discord_like()).init()
        await stack.put("a.bin", DATA)
        version = await stack.version("a.bin")

        # The second primary part vanishes after the first was served.
        del stack.primary.messages[version.message_ids[1]]

        assert await stack.read("a.bin") == DATA

    async def test_read_preference_serves_from_the_replica_first(self):
        stack = await Stack(
            telegram_like(), discord_like(), read_preference=["dc:9"]
        ).init()
        await stack.put("a.bin", DATA)
        stack.primary.calls.clear()

        assert await stack.read("a.bin") == DATA
        assert "download_file" not in stack.primary.calls

    async def test_replica_survives_a_version_whose_primary_parts_are_gone(self):
        stack = await Stack(telegram_like(), discord_like()).init()
        await stack.put("a.bin", DATA)
        version = await stack.version("a.bin")
        for mid in version.message_ids:
            del stack.primary.messages[mid]

        fd = await stack.fd_repo.get(stack.root.find_file("a.bin"))
        latest = fd.get_latest_version()
        assert latest.is_valid()
        assert latest.part_sizes == []
        assert latest.size == len(DATA)
        assert await stack.read("a.bin") == DATA

    async def test_remove_deletes_the_replica(self):
        stack = await Stack(telegram_like(), discord_like()).init()
        await stack.put("a.bin", DATA)
        version = await stack.version("a.bin")
        replica_ids = version.replicas["dc:9"].message_ids

        await stack.file_api.rm(stack.root.find_file("a.bin"))

        assert not any(mid in stack.mirror.messages for mid in replica_ids)

    async def test_copy_replicates_each_version_on_its_own(self):
        stack = await Stack(telegram_like(), discord_like()).init()
        await stack.put("a.bin", DATA)
        await stack.put("a.bin", DATA[:3000])
        fr = stack.root.find_file("a.bin")

        copied = await stack.file_api.copy(stack.root, fr, "b.bin")
        fd = await stack.fd_repo.get(copied, include_all_versions=True)

        for version in fd.get_versions():
            replica = version.replicas["dc:9"]
            assert replica.size == version.size
        assert await stack.read("b.bin") == DATA[:3000]


class TestPromotionWithReplicas:
    async def test_replica_becomes_the_primary_layout(self):
        stack = await Stack(telegram_like(), discord_like()).init()
        await stack.put("a.bin", DATA)
        metadata = TGFSMetadata.from_dict(stack.metadata_repo.metadata.to_dict())  # type: ignore[union-attr]

        promoted = Stack(stack.mirror, stack.primary)
        promoted.metadata_repo.metadata = metadata
        version = await promoted.version("a.bin")

        assert version.store == "dc:9"
        assert len(version.message_ids) == 80
        # The former primary's parts are a replica now.
        assert version.replicas["tg:1"].message_ids
        assert await promoted.read("a.bin") == DATA

        stack.primary.fail.update({"download_file", "get_messages"})
        assert await promoted.read("a.bin") == DATA


class TestBackfillIntoPrimary:
    async def test_version_of_a_former_primary_is_copied_into_the_new_one(self):
        # The mirror never received the version; after promotion the
        # backfill copies it into the new primary and re-mirrors it.
        stack = await Stack(telegram_like(), discord_like()).init()
        stack.mirror.fail.update({"upload", "copy_from"})
        stack.mirror_group._strict = False
        await stack.put("a.bin", DATA)
        stack.mirror.fail.clear()
        metadata = TGFSMetadata.from_dict(stack.metadata_repo.metadata.to_dict())  # type: ignore[union-attr]

        promoted = Stack(stack.mirror, stack.primary)
        promoted.metadata_repo.metadata = metadata
        fr = promoted.root.find_file("a.bin")
        assert (await promoted.fd_repo.get(fr)).get_latest_version().store == "tg:1"

        client = type(
            "C",
            (),
            {
                "name": "fs",
                "mirror_group": promoted.mirror_group,
                "fd_repo": promoted.fd_repo,
                "metadata_api": promoted.metadata_api,
                "dir_api": promoted.dir_api,
            },
        )()
        report = BackfillReport()
        await backfill_file(client, fr, verify=False, report=report)  # type: ignore[arg-type]

        assert report.failures == []
        assert report.versions_promoted == 1
        version = (await promoted.fd_repo.get(fr)).get_latest_version()
        assert version.store == "dc:9"
        assert len(version.message_ids) == 80
        # And the old primary, now a mirror, holds it as an aligned copy
        # (its messages are large enough for 64-byte parts).
        assert version.has_copy_in("tg:1")
        assert await promoted.read("a.bin") == DATA


class TestQueue:
    def test_persistence_round_trip(self, tmp_path):
        path = str(tmp_path / "queue.json")
        queue = ReplicationQueue(path)
        queue.enqueue("fs", "/a.bin")
        queue.enqueue("fs", "/dir/b.bin")
        queue.enqueue("fs", "/a.bin")  # idempotent
        queue.failed("fs", "/a.bin", "boom")

        reloaded = ReplicationQueue(path)
        items = reloaded.pending("fs")
        assert [i.path for i in items] == ["/a.bin", "/dir/b.bin"]
        assert items[0].attempts == 1 and items[0].last_error == "boom"

        reloaded.done("fs", "/a.bin")
        assert [i.path for i in ReplicationQueue(path).pending("fs")] == ["/dir/b.bin"]

    def test_backoff_and_retry(self, tmp_path):
        queue = ReplicationQueue(str(tmp_path / "q.json"))
        queue.enqueue("fs", "/a.bin")
        queue.failed("fs", "/a.bin", "boom")
        assert queue.ready("fs") == []
        queue.retry_now("fs")
        assert [i.path for i in queue.ready("fs")] == ["/a.bin"]

    def test_corrupt_file_is_ignored(self, tmp_path):
        path = tmp_path / "q.json"
        path.write_text("{not json")
        assert ReplicationQueue(str(path)).pending("fs") == []

    def test_in_memory_queue(self):
        queue = ReplicationQueue(None)
        queue.enqueue("fs", "/a")
        assert queue.to_dict() == {
            "fs": [
                {
                    "path": "/a",
                    "queued_at": queue.pending("fs")[0].queued_at,
                    "attempts": 0,
                    "last_error": None,
                }
            ]
        }

    def test_find_file(self):
        root = TGFSDirectory.root_dir()
        docs = root.create_dir("docs")
        fr = docs.create_file_ref("a.txt", 1)
        assert file_path(fr) == "/docs/a.txt"
        assert find_file(root, "/docs/a.txt") is fr
        assert find_file(root, "/docs/missing.txt") is None
        assert find_file(root, "/nope/a.txt") is None
        assert find_file(root, "/") is None


class TestBackgroundSync:
    async def test_write_records_the_primary_only_and_queues_the_file(self):
        queue = ReplicationQueue(None)
        stack = await Stack(
            telegram_like(), discord_like(), inline=False, queue=queue
        ).init()
        docs = await stack.dir_api.create("docs", stack.root)
        await stack.put("a.bin", DATA, under=docs)

        fr = docs.find_file("a.bin")
        version = (await stack.fd_repo.get(fr)).get_latest_version()
        assert version.replicas == {} and version.mirrors == {}
        assert [i.path for i in queue.pending("fs")] == ["/docs/a.bin"]
        # The descriptor copy is cheap and still made inline.
        assert fr.mirrors["dc:9"] in stack.mirror.messages

    async def test_worker_replicates_the_queued_file(self):
        queue = ReplicationQueue(None)
        stack = await Stack(
            telegram_like(), discord_like(), inline=False, queue=queue
        ).init()
        await stack.put("a.bin", DATA)
        client = type(
            "C",
            (),
            {
                "name": "fs",
                "mirror_group": stack.mirror_group,
                "fd_repo": stack.fd_repo,
                "metadata_api": stack.metadata_api,
                "dir_api": stack.dir_api,
            },
        )()
        worker = ReplicationWorker(client, queue)  # type: ignore[arg-type]

        assert await worker.run_once() == 1

        assert queue.pending("fs") == []
        version = await stack.version("a.bin")
        assert len(version.replicas["dc:9"].message_ids) == 80
        stack.primary.fail.update({"download_file", "get_messages"})
        assert await stack.read("a.bin") == DATA

    async def test_worker_keeps_failed_items_with_backoff(self):
        queue = ReplicationQueue(None)
        stack = await Stack(
            telegram_like(), discord_like(), inline=False, queue=queue
        ).init()
        await stack.put("a.bin", DATA)
        stack.mirror.fail.add("upload")
        client = type(
            "C",
            (),
            {
                "name": "fs",
                "mirror_group": stack.mirror_group,
                "fd_repo": stack.fd_repo,
                "metadata_api": stack.metadata_api,
                "dir_api": stack.dir_api,
            },
        )()
        worker = ReplicationWorker(client, queue)  # type: ignore[arg-type]

        assert await worker.run_once() == 0
        item = queue.pending("fs")[0]
        assert item.attempts == 1 and "upload failed" in (item.last_error or "")
        assert queue.ready("fs") == []

        stack.mirror.fail.clear()
        queue.retry_now()
        assert await worker.run_once() == 1

    async def test_removed_file_is_dropped_from_the_queue(self):
        queue = ReplicationQueue(None)
        stack = await Stack(
            telegram_like(), discord_like(), inline=False, queue=queue
        ).init()
        await stack.put("a.bin", DATA)
        await stack.file_api.rm(stack.root.find_file("a.bin"))
        client = type(
            "C",
            (),
            {
                "name": "fs",
                "mirror_group": stack.mirror_group,
                "fd_repo": stack.fd_repo,
                "metadata_api": stack.metadata_api,
                "dir_api": stack.dir_api,
            },
        )()
        assert await ReplicationWorker(client, queue).run_once() == 1  # type: ignore[arg-type]
        assert queue.pending("fs") == []


class TestModel:
    def test_replica_compaction_thresholds(self):
        from tgdcfs.core.model.file import replica_from_dict, replica_to_dict

        small = Replica(message_ids=[1, 2, 3], part_sizes=[5, 5, 2])
        assert replica_to_dict(small) == {"m": [1, 2, 3], "p": [5, 2], "n": 3}
        assert replica_from_dict(replica_to_dict(small)) == small

        odd = Replica(message_ids=[1, 2, 3], part_sizes=[5, 4, 2])
        assert replica_to_dict(odd) == {"m": [1, 2, 3], "p": [5, 4, 2]}
        assert replica_from_dict(replica_to_dict(odd)) == odd

        big = Replica(message_ids=list(range(1, 202)), part_sizes=[7] * 200 + [1])
        packed = replica_to_dict(big)
        assert "mb" in packed and "m" not in packed
        assert replica_from_dict(packed) == big

        assert replica_from_dict({}) == Replica()
