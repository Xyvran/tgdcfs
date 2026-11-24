"""A copy is only recorded when its parts add up to the version it copies.

Discord's upload stops quietly when the byte stream ends early, so a
short source would otherwise turn into a replica that looks complete
and serves a truncated file.
"""

import datetime
from types import SimpleNamespace

import pytest

from tgdcfs.core.backfill import backfill_mirrors, size_mismatch
from tgdcfs.core.mirror import MirrorGroup, MirrorStore, SizeMismatch, checked_replica
from tgdcfs.core.model import TGFSDirectory, TGFSFileDesc, TGFSFileVersion
from tgdcfs.reqres import SentFileMessage
from tests.fakes.store import FakeStore
from tests.tgdcfs.core.test_mirror import FakeFDRepo, make_group

DATA = bytes(range(200))


class ShortDownloadStore(FakeStore):
    """Serves every part ``missing`` bytes short, as a broken source would."""

    missing: int = 5

    async def download_file(self, message_id, begin, end):
        resp = await super().download_file(message_id, begin, end - self.missing)
        return resp


class ShortUploadStore(FakeStore):
    """Reports one byte less than it received for every part."""

    async def upload(self, file_msg):
        sent = await super().upload(file_msg)
        return [SentFileMessage(message_id=m.message_id, size=m.size - 1) for m in sent]


def stores(primary_cls=FakeStore, mirror_cls=FakeStore):
    primary = primary_cls(key="tg:1", backend_name="telegram", max_part_bytes=100)
    mirror = mirror_cls(
        key="dc:9", backend_name="discord", max_part_bytes=30, server_copy=False
    )
    ids = [primary._add(document=DATA[:100]), primary._add(document=DATA[100:])]
    group = MirrorGroup(
        primary=primary,
        stores=[MirrorStore(key=mirror.key, store=mirror)],
        mode="auto",
    )
    return group, primary, mirror, ids


class TestCheckedReplica:
    def test_matching_parts_make_a_replica(self):
        sent = [SentFileMessage(message_id=1, size=6), SentFileMessage(2, 4)]
        replica = checked_replica("dc:9", sent, 10, "a.bin")
        assert replica.message_ids == [1, 2] and replica.part_sizes == [6, 4]

    def test_short_parts_are_refused(self):
        sent = [SentFileMessage(message_id=1, size=6)]
        with pytest.raises(SizeMismatch, match="6 bytes instead of 10"):
            checked_replica("dc:9", sent, 10, "a.bin")


class TestReplicaSizes:
    @pytest.mark.asyncio
    async def test_complete_source_is_recorded(self):
        group, _, mirror, ids = stores()
        res = await group.mirror_parts(ids, [100, 100])
        assert res.replicas["dc:9"].size == 200
        assert res.errors == {}

    @pytest.mark.asyncio
    async def test_short_source_is_not_recorded(self):
        group, _, _, ids = stores(primary_cls=ShortDownloadStore)
        res = await group.mirror_parts(ids, [100, 100])
        assert res.replicas == {}
        assert "95 of 200 bytes from tg:1 (part 1)" in res.errors["dc:9"]
        assert "not recorded" in res.errors["dc:9"]

    @pytest.mark.asyncio
    async def test_short_target_parts_are_not_recorded(self):
        group, _, _, ids = stores(mirror_cls=ShortUploadStore)
        res = await group.mirror_parts(ids, [100, 100])
        assert res.replicas == {}
        assert "instead of 200" in res.errors["dc:9"]

    @pytest.mark.asyncio
    async def test_strict_group_raises(self):
        group, primary, mirror, ids = stores(primary_cls=ShortDownloadStore)
        strict = MirrorGroup(
            primary=primary,
            stores=[MirrorStore(key=mirror.key, store=mirror)],
            mode="auto",
            strict=True,
        )
        with pytest.raises(SizeMismatch):
            await strict.mirror_parts(ids, [100, 100])


class TestAlignedCopySizes:
    @pytest.mark.asyncio
    async def test_short_reupload_is_not_recorded(self):
        primary = FakeStore(key="tg:1", backend_name="telegram")
        mirror = ShortUploadStore(key="tg:2", backend_name="telegram")
        mid = primary._add(document=DATA)
        group = MirrorGroup(
            primary=primary,
            stores=[MirrorStore(key=mirror.key, store=mirror)],
            mode="reupload",
        )
        res = await group.mirror_parts([mid], [len(DATA)])
        assert res.mirrors == {}
        assert "199 bytes instead of 200" in res.errors["tg:2"]


def _version(part_sizes, recorded_size):
    return TGFSFileVersion(
        id="v",
        updated_at=datetime.datetime.now(),
        message_ids=list(range(1, len(part_sizes) + 1)),
        part_sizes=part_sizes,
        recorded_size=recorded_size,
    )


class TestBackfillSizeCheck:
    def test_size_mismatch(self):
        assert size_mismatch(_version([6, 4], 10)) is None
        assert size_mismatch(_version([6, 4], -1)) is None
        assert size_mismatch(_version([], 10)) is None
        assert size_mismatch(_version([6, 3], 10)) == (
            "its 2 parts add up to 9 bytes, the descriptor records 10"
        )

    @pytest.mark.asyncio
    async def test_an_incomplete_version_is_not_mirrored(self, mocker):
        group, _, mirror_api = make_group(mocker)
        fd = TGFSFileDesc(name="a.txt")
        fd.add_version(_version([6, 3], 10))
        root = TGFSDirectory.root_dir()
        root.create_file_ref("a.txt", 5)
        client = SimpleNamespace(
            name="test",
            mirror_group=group,
            fd_repo=FakeFDRepo(fd),
            metadata_api=mocker.AsyncMock(),
            dir_api=SimpleNamespace(root=root),
        )

        report = await backfill_mirrors(client)  # type: ignore[arg-type]

        mirror_api.copy_from.assert_not_awaited()
        assert report.versions_mirrored == 0
        assert report.failures == [
            "a.txt@v: its 2 parts add up to 9 bytes, the descriptor records 10; "
            "not mirrored"
        ]
        assert fd.get_version("v").mirrors == {}

    @pytest.mark.asyncio
    async def test_a_failure_names_its_reason(self, mocker):
        group, _, mirror_api = make_group(mocker)
        mirror_api.copy_from.side_effect = RuntimeError("channel is gone")
        fd = TGFSFileDesc(name="a.txt")
        fd.add_version(_version([10], 10))
        root = TGFSDirectory.root_dir()
        root.create_file_ref("a.txt", 5)
        client = SimpleNamespace(
            name="test",
            mirror_group=group,
            fd_repo=FakeFDRepo(fd),
            metadata_api=mocker.AsyncMock(),
            dir_api=SimpleNamespace(root=root),
        )

        report = await backfill_mirrors(client)  # type: ignore[arg-type]

        assert report.failures == [
            "a.txt@v: could not mirror to tg:999 (channel is gone)"
        ]
