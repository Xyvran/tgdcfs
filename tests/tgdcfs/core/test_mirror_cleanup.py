"""Parts an aborted copy left in a store are deleted again.

No descriptor ever points at them, so without the clean-up every failed
mirror attempt leaves orphaned messages behind. Only the parts of the
attempt itself are deleted, never a copy that is recorded.
"""

import asyncio
import logging

import pytest

from tgdcfs.backends.base import record_sent_parts, sent_parts_of
from tgdcfs.core.mirror import MirrorGroup, MirrorStore, SizeMismatch
from tgdcfs.errors import TechnicalError
from tgdcfs.reqres import FileMessageFromBuffer, SentFileMessage
from tests.fakes.store import FakeStore
from tests.tgdcfs.core.test_mirror_sizes import (
    DATA,
    ShortDownloadStore,
    ShortUploadStore,
    stores,
)


def documents(store: FakeStore) -> list[int]:
    return [mid for mid, m in store.messages.items() if m.document is not None]


class FailingSecondUpload(FakeStore):
    """Accepts the first upload and fails every later one."""

    uploads: int = 0

    async def upload(self, file_msg):
        self.uploads += 1
        if self.uploads > 1:
            raise TechnicalError("upload refused (simulated)")
        return await super().upload(file_msg)


class TestSentParts:
    def test_parts_accumulate_on_the_exception(self):
        ex = RuntimeError("boom")
        assert sent_parts_of(ex) == []
        record_sent_parts(ex, [SentFileMessage(1, 3)])
        record_sent_parts(ex, [SentFileMessage(2, 4)])
        assert [m.message_id for m in sent_parts_of(ex)] == [1, 2]


class TestReplicaCleanup:
    @pytest.mark.asyncio
    async def test_parts_of_a_short_source_are_deleted(self, caplog):
        group, _, mirror, ids = stores(primary_cls=ShortDownloadStore)
        with caplog.at_level(logging.INFO, logger="tgdcfs.core.mirror"):
            res = await group.mirror_parts(ids, [100, 100])

        assert "dc:9" in res.errors
        # Three 30-byte parts had been sent before the first source part
        # came up short; all of them are gone again.
        assert documents(mirror) == []
        assert "delete_messages" in mirror.calls
        assert "Deleting 3 parts of the aborted copy of message 1" in caplog.text
        assert "Deleted 3 parts" in caplog.text

    @pytest.mark.asyncio
    async def test_parts_of_a_refused_replica_are_deleted(self):
        group, _, mirror, ids = stores(mirror_cls=ShortUploadStore)
        res = await group.mirror_parts(ids, [100, 100])
        assert res.replicas == {}
        assert documents(mirror) == []

    @pytest.mark.asyncio
    async def test_a_recorded_replica_is_left_alone(self):
        group, _, mirror, ids = stores(primary_cls=ShortDownloadStore)
        recorded = mirror._add(document=b"x" * 30)

        await group.mirror_parts(ids, [100, 100])

        assert documents(mirror) == [recorded]

    @pytest.mark.asyncio
    async def test_a_failing_delete_is_logged_not_raised(self, caplog):
        group, _, mirror, ids = stores(primary_cls=ShortDownloadStore)
        mirror.fail.add("delete_messages")
        with caplog.at_level(logging.WARNING, logger="tgdcfs.core.mirror"):
            res = await group.mirror_parts(ids, [100, 100])

        # The copy fails for its own reason, not for the clean-up.
        assert "95 of 200 bytes" in res.errors["dc:9"]
        assert "Could not delete the parts" in caplog.text

    @pytest.mark.asyncio
    async def test_a_complete_replica_is_kept(self):
        group, _, mirror, ids = stores()
        res = await group.mirror_parts(ids, [100, 100])
        assert documents(mirror) == res.replicas["dc:9"].message_ids
        assert "delete_messages" not in mirror.calls

    @pytest.mark.asyncio
    async def test_a_cancelled_copy_is_cleaned_up_in_the_background(self):
        primary = FakeStore(key="tg:1", backend_name="telegram", max_part_bytes=100)
        mirror = FakeStore(
            key="dc:9", backend_name="discord", max_part_bytes=30, server_copy=False
        )
        ids = [primary._add(document=DATA[:100]), primary._add(document=DATA[100:])]
        gate = asyncio.Event()
        original = primary.download_file

        async def download(mid, begin, end):
            if mid == ids[1]:
                await gate.wait()
            return await original(mid, begin, end)

        primary.download_file = download  # type: ignore[method-assign]
        group = MirrorGroup(
            primary=primary,
            stores=[MirrorStore(key=mirror.key, store=mirror)],
            mode="auto",
        )
        copy = asyncio.ensure_future(group.mirror_parts(ids, [100, 100]))
        for _ in range(20):
            await asyncio.sleep(0)
        assert documents(mirror)  # the first parts are in

        copy.cancel()
        with pytest.raises(asyncio.CancelledError):
            await copy
        for _ in range(20):
            await asyncio.sleep(0)
        assert documents(mirror) == []


class TestAlignedCleanup:
    @pytest.mark.asyncio
    async def test_parts_copied_before_a_failure_are_deleted(self):
        primary = FakeStore(key="tg:1", backend_name="telegram")
        mirror = FailingSecondUpload(key="tg:2", backend_name="telegram")
        mids = [primary._add(document=DATA[:100]), primary._add(document=DATA[100:])]
        group = MirrorGroup(
            primary=primary,
            stores=[MirrorStore(key=mirror.key, store=mirror)],
            mode="reupload",
        )

        res = await group.mirror_parts(mids, [100, 100])

        assert res.mirrors == {}
        assert "upload refused" in res.errors["tg:2"]
        assert documents(mirror) == []

    @pytest.mark.asyncio
    async def test_a_short_part_is_deleted(self):
        primary = FakeStore(key="tg:1", backend_name="telegram")
        mirror = ShortUploadStore(key="tg:2", backend_name="telegram")
        mid = primary._add(document=DATA)
        group = MirrorGroup(
            primary=primary,
            stores=[MirrorStore(key=mirror.key, store=mirror)],
            mode="reupload",
        )

        await group.mirror_parts([mid], [len(DATA)])

        assert documents(mirror) == []


class TestTeeCleanup:
    @pytest.mark.asyncio
    async def test_a_refused_tee_replica_is_deleted(self):
        primary = FakeStore(key="tg:1", backend_name="telegram", max_part_bytes=2048)
        mirror = ShortUploadStore(
            key="dc:9", backend_name="discord", max_part_bytes=64, server_copy=False
        )
        group = MirrorGroup(
            primary=primary,
            stores=[MirrorStore(key=mirror.key, store=mirror)],
            mode="auto",
        )
        tee = group.tee(FileMessageFromBuffer.new(buffer=DATA, name="a.bin"), 1 << 20)
        assert tee is not None

        await primary.upload(tee.message)
        outcome = await tee.collect()

        assert isinstance(outcome["dc:9"], SizeMismatch)
        assert documents(mirror) == []
