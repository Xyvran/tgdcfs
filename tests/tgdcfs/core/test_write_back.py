"""Write-back uploads: ``write_ack: cache`` (design plan, section 4.12).

A PUT lands in the local cache and is answered; the version is recorded
as pending and a worker moves it into the primary and the mirrors from
the one local file. These tests run the real stack on in-memory stores.
"""

import asyncio
import json

import pytest

from tgdcfs.config import Config, FilesystemConfig
from tgdcfs.core.model import TGFSFileVersion
from tgdcfs.core.replication import ReplicationQueue, ReplicationWorker
from tgdcfs.core.repository.impl import StoreFDRepository
from tgdcfs.errors import TechnicalError
from tgdcfs.tasks import task_store
from tgdcfs.tasks.models import TaskStatus, TaskType
from tests.fakes.store import FakeStore
from tests.tgdcfs.config.test_stores import current_layout
from tests.tgdcfs.core.test_local_cache import (
    DATA,
    RecordingStore,
    Stack,
    discord_like,
    downloaded_parts,
    entry_of,
    make_cache,
    telegram_like,
)


class CountingStore(RecordingStore):
    """Counts how many uploads run at the same time."""

    def __post_init__(self):
        super().__post_init__()
        self.in_flight = 0
        self.max_in_flight = 0

    async def upload(self, file_msg):
        self.in_flight += 1
        self.max_in_flight = max(self.max_in_flight, self.in_flight)
        try:
            # Let the other uploads of the same distribution start.
            await asyncio.sleep(0.01)
            return await super().upload(file_msg)
        finally:
            self.in_flight -= 1


def document_names(store: FakeStore) -> list[str]:
    return [m.name for m in store.messages.values() if m.document is not None]


async def write_back_stack(tmp_path, cache=None, mirror=None, **cache_overrides):
    cache = cache or make_cache(tmp_path, **cache_overrides)
    queue = ReplicationQueue(None)
    stack = await Stack(
        telegram_like(),
        mirror or discord_like(),
        cache,
        inline=False,
        queue=queue,
        write_ack="cache",
    ).init()
    return stack, cache, queue


class TestModel:
    def test_pending_version_round_trips(self):
        version = TGFSFileVersion.pending_version("v1", 4096)
        assert version.pending and version.size == 4096 and not version.is_valid()

        data = json.loads(json.dumps(version.to_dict()))
        assert data["pending"] is True and data["messageIds"] == []
        restored = TGFSFileVersion.from_dict(data)
        assert restored.pending and restored.size == 4096

    def test_materialize_ends_the_pending_state(self):
        version = TGFSFileVersion.pending_version("v1", 10)
        version.materialize("tg:1", [7, 8], [6, 4])
        assert not version.pending and version.is_valid()
        assert version.store == "tg:1" and version.size == 10
        assert "pending" not in version.to_dict()

    def test_a_regular_version_serializes_as_before(self):
        version = TGFSFileVersion(
            id="v", updated_at=__import__("datetime").datetime.now()
        )
        assert "pending" not in version.to_dict()


class TestConfig:
    def test_write_ack_defaults_to_primary(self):
        fs = FilesystemConfig.from_dict("media", {"primary": "a"})
        assert fs.write_ack == "primary"

    def test_unknown_value_rejected(self):
        with pytest.raises(ValueError, match="write_ack"):
            FilesystemConfig.from_dict("media", {"primary": "a", "write_ack": "later"})

    def test_strict_refuses_write_back(self):
        with pytest.raises(ValueError, match="strict"):
            FilesystemConfig.from_dict(
                "media", {"primary": "a", "strict": True, "write_ack": "cache"}
            )

    def test_write_back_needs_the_cache(self):
        data = current_layout()
        data["filesystems"]["media"]["write_ack"] = "cache"
        with pytest.raises(ValueError, match="cache.enabled"):
            Config.from_dict(data)
        data["tgdcfs"]["cache"] = {"enabled": True}
        assert Config.from_dict(data).filesystems["media"].write_ack == "cache"


class TestAcceptingAWrite:
    async def test_the_write_returns_before_any_store_has_the_bytes(self, tmp_path):
        stack, cache, queue = await write_back_stack(tmp_path)

        await stack.put("a.bin", DATA)

        version = await stack.version("a.bin")
        assert (
            version.pending and version.message_ids == [] and version.size == len(DATA)
        )
        assert "a.bin" not in "".join(document_names(stack.primary))
        assert document_names(stack.mirror) == []
        assert cache.complete(version.id) and entry_of(cache, version.id).pins == 1
        assert [i.path for i in queue.pending("fs")] == ["/a.bin"]

    async def test_the_accepting_instance_serves_the_pending_version(self, tmp_path):
        stack, cache, queue = await write_back_stack(tmp_path)
        await stack.put("a.bin", DATA)

        assert await stack.read("a.bin") == DATA
        fr = stack.root.find_file("a.bin")
        stream = await stack.file_api.retrieve(fr, 100, 299, "a.bin")
        assert b"".join([c async for c in stream]) == DATA[100:300]

    async def test_another_reader_sees_the_previous_version(self, tmp_path):
        stack, cache, queue = await write_back_stack(tmp_path)
        # A first version that every store has (distributed right away).
        await stack.put("a.bin", b"old" * 100)
        worker = ReplicationWorker(stack.worker_client(), queue)  # type: ignore[arg-type]
        assert await worker.run_once() == 1
        await stack.put("a.bin", DATA)
        fr = stack.root.find_file("a.bin")

        # This instance: the new bytes. A reader without the cache: the old.
        assert await stack.read("a.bin") == DATA
        elsewhere = StoreFDRepository(stack.primary, mirror_group=stack.mirror_group)
        fd = await elsewhere.get(fr)
        assert not fd.get_latest_version().pending
        assert fd.get_latest_version().size == 300
        # Locally the latest version is the pending one.
        fd = await stack.fd_repo.get(fr)
        assert fd.get_latest_version().pending

    async def test_a_first_version_is_not_visible_elsewhere_yet(self, tmp_path):
        stack, cache, queue = await write_back_stack(tmp_path)
        await stack.put("a.bin", DATA)
        fr = stack.root.find_file("a.bin")

        elsewhere = StoreFDRepository(stack.primary, mirror_group=stack.mirror_group)
        fd = await elsewhere.get(fr)
        assert fd.get_versions() == []

    async def test_a_full_cache_uploads_the_way_it_always_did(self, tmp_path):
        stack, cache, queue = await write_back_stack(
            tmp_path, max_size_mb=1, block_kb=256
        )
        hog = cache.open_staging("fs", "hog", 1024 * 1024)
        assert hog is not None
        await hog.write(b"h" * (1024 * 1024))
        await hog.close()
        cache.pin("hog")

        await stack.put("a.bin", DATA)

        version = await stack.version("a.bin")
        assert not version.pending and len(version.message_ids) == 3
        assert await stack.read("a.bin") == DATA

    async def test_a_disk_error_mid_body_fails_the_write(self, tmp_path, monkeypatch):
        stack, cache, queue = await write_back_stack(tmp_path)
        import os

        real_pwrite = os.pwrite
        calls = {"n": 0}

        def flaky_pwrite(fd, data, offset):
            # Block-map writes are single bytes; fail the data write.
            if len(data) > 1:
                calls["n"] += 1
                raise OSError(28, "No space left on device")
            return real_pwrite(fd, data, offset)

        monkeypatch.setattr(os, "pwrite", flaky_pwrite)

        with pytest.raises(TechnicalError, match="stage"):
            await stack.put("a.bin", DATA)
        assert calls["n"] == 1
        assert cache.stats()["entries"] == 0


class TestDistribution:
    async def test_the_worker_fills_the_primary_and_the_mirror_from_the_cache(
        self, tmp_path
    ):
        stack, cache, queue = await write_back_stack(tmp_path)
        await stack.put("a.bin", DATA)
        pending = await stack.version("a.bin")

        worker = ReplicationWorker(stack.worker_client(), queue)  # type: ignore[arg-type]
        assert await worker.run_once() == 1

        version = await stack.version("a.bin")
        assert version.id == pending.id and not version.pending
        assert version.store == "tg:1" and len(version.message_ids) == 3
        assert len(version.replicas["dc:9"].message_ids) == 80
        assert queue.pending("fs") == []
        assert entry_of(cache, version.id).pins == 0
        # The bytes went straight from the disk into both stores.
        assert downloaded_parts(stack.primary, version) == []
        cache.remove(version.id)
        assert await stack.read("a.bin") == DATA
        stack.primary.fail.update({"download_file", "get_messages"})
        assert await stack.read("a.bin") == DATA

    async def test_primary_and_replica_upload_at_the_same_time(self, tmp_path):
        cache = make_cache(tmp_path)
        queue = ReplicationQueue(None)
        primary = CountingStore(
            key="tg:1", backend_name="telegram", max_part_bytes=2048
        )
        mirror = CountingStore(
            key="dc:9", backend_name="discord", max_part_bytes=64, server_copy=False
        )
        stack = await Stack(
            primary, mirror, cache, inline=False, queue=queue, write_ack="cache"
        ).init()
        await stack.put("a.bin", DATA)
        primary.max_in_flight = mirror.max_in_flight = 0
        started = []

        real_primary_upload, real_mirror_upload = primary.upload, mirror.upload

        async def primary_upload(msg):
            started.append("primary")
            return await real_primary_upload(msg)

        async def mirror_upload(msg):
            started.append("mirror")
            return await real_mirror_upload(msg)

        primary.upload, mirror.upload = primary_upload, mirror_upload  # type: ignore[method-assign]

        worker = ReplicationWorker(stack.worker_client(), queue)  # type: ignore[arg-type]
        assert await worker.run_once() == 1

        # Both started before either finished: the mirror did not wait for
        # the primary's ids.
        assert sorted(started) == ["mirror", "primary"]
        version = await stack.version("a.bin")
        assert not version.pending and "dc:9" in version.replicas

    async def test_an_aligned_mirror_follows_the_primary(self, tmp_path):
        spare = FakeStore(key="tg:2", backend_name="telegram", max_part_bytes=2048)
        stack, cache, queue = await write_back_stack(tmp_path, mirror=spare)
        await stack.put("a.bin", DATA)

        worker = ReplicationWorker(stack.worker_client(), queue)  # type: ignore[arg-type]
        assert await worker.run_once() == 1

        version = await stack.version("a.bin")
        assert not version.pending
        assert len(version.mirrors["tg:2"]) == 3 and "tg:2" not in version.replicas

    async def test_a_failing_primary_keeps_the_version_pending_and_retries(
        self, tmp_path
    ):
        stack, cache, queue = await write_back_stack(tmp_path)
        await stack.put("a.bin", DATA)
        stack.primary.fail.add("upload")

        worker = ReplicationWorker(stack.worker_client(), queue)  # type: ignore[arg-type]
        assert await worker.run_once() == 0

        version = await stack.version("a.bin")
        assert version.pending
        # The replica that did succeed is recorded and not made twice.
        assert len(version.replicas["dc:9"].message_ids) == 80
        assert await stack.read("a.bin") == DATA
        item = queue.pending("fs")[0]
        assert "primary" in (item.last_error or "")

        stack.primary.fail.clear()
        queue.retry_now()
        mirror_messages = len(stack.mirror.messages)
        assert await worker.run_once() == 1
        version = await stack.version("a.bin")
        assert not version.pending and len(version.message_ids) == 3
        assert len(stack.mirror.messages) == mirror_messages

    async def test_a_lost_cache_entry_is_reported_and_the_file_falls_back(
        self, tmp_path, caplog
    ):
        stack, cache, queue = await write_back_stack(tmp_path)
        await stack.put("a.bin", b"old" * 100)
        worker = ReplicationWorker(stack.worker_client(), queue)  # type: ignore[arg-type]
        assert await worker.run_once() == 1
        await stack.put("a.bin", DATA)
        pending = await stack.version("a.bin")
        cache.remove(pending.id)

        assert await worker.run_once() == 1

        version = await stack.version("a.bin")
        assert version.id != pending.id and version.size == 300
        assert await stack.read("a.bin") == b"old" * 100
        assert any("lost" in r.getMessage() for r in caplog.records if r.levelno >= 40)
        failed = [
            t
            for t in await task_store.get_all_tasks()
            if t.type == TaskType.DISTRIBUTION and t.status == TaskStatus.FAILED
        ]
        assert failed and "a.bin" in failed[0].filename

    async def test_a_file_system_without_mirrors_distributes_too(self, tmp_path):
        cache = make_cache(tmp_path)
        queue = ReplicationQueue(None)
        stack = await Stack(
            telegram_like(),
            discord_like(),
            cache,
            inline=False,
            queue=queue,
            write_ack="cache",
        ).init()
        stack.mirror_group = None  # type: ignore[assignment]
        await stack.put("a.bin", DATA)

        client = stack.worker_client()
        client.mirror_group = None
        worker = ReplicationWorker(client, queue)  # type: ignore[arg-type]
        assert await worker.run_once() == 1

        version = await stack.version("a.bin")
        assert not version.pending and len(version.message_ids) == 3
        assert entry_of(cache, version.id).pins == 0

    async def test_pins_survive_a_restart(self, tmp_path):
        stack, cache, queue = await write_back_stack(tmp_path)
        await stack.put("a.bin", DATA)
        version = await stack.version("a.bin")

        reloaded = make_cache(tmp_path)

        assert entry_of(reloaded, version.id).pins == 1
        assert reloaded.complete(version.id)
