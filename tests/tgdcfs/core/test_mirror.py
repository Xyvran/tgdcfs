import datetime
import json
from types import SimpleNamespace

import pytest

from tgdcfs.backends.base import StoreCapabilities
from tgdcfs.config import RedundancyConfig
from tgdcfs.core.backfill import backfill_mirrors
from tgdcfs.core.mirror import MirrorStore, MirrorGroup
from tgdcfs.core.model import TGFSDirectory, TGFSFileDesc, TGFSFileRef, TGFSFileVersion
from tgdcfs.core.repository.impl.fd.store_msg import StoreFDRepository
from tgdcfs.core.repository.impl.file_content import StoreFileContentRepository
from tgdcfs.core.repository.interface import FDRepositoryResp
from tgdcfs.errors import MessageNotFound, TechnicalError
from tgdcfs.reqres import Document, MessageResp, Replica, SentFileMessage


class TestRedundancyConfig:
    def test_from_dict_none(self):
        assert RedundancyConfig.from_dict(None) is None
        assert RedundancyConfig.from_dict({}) is None
        assert RedundancyConfig.from_dict({"mirrors": {}}) is None

    def test_from_dict_valid(self):
        config = RedundancyConfig.from_dict(
            {"mirrors": {"111": ["222", "333"]}, "mode": "forward", "strict": True}
        )
        assert config is not None
        assert config.mirrors == {"111": ["222", "333"]}
        assert config.mode == "forward"
        assert config.strict is True

    def test_from_dict_defaults(self):
        config = RedundancyConfig.from_dict({"mirrors": {"111": ["222"]}})
        assert config is not None
        assert config.mode == "forward"
        assert config.strict is False

    def test_from_dict_self_mirror_rejected(self):
        with pytest.raises(ValueError):
            RedundancyConfig.from_dict({"mirrors": {"111": ["111"]}})

    def test_from_dict_duplicate_mirror_rejected(self):
        with pytest.raises(ValueError):
            RedundancyConfig.from_dict({"mirrors": {"111": ["222", "222"]}})

    def test_from_dict_bad_mode_rejected(self):
        with pytest.raises(ValueError):
            RedundancyConfig.from_dict({"mirrors": {"111": ["222"]}, "mode": "raid5"})


class TestModelMirrors:
    def test_version_round_trip(self):
        fv = TGFSFileVersion(
            id="v1",
            updated_at=datetime.datetime.now(),
            message_ids=[1, 2],
            part_sizes=[10, 20],
            mirrors={"tg:999": [11, 12]},
        )
        parsed = TGFSFileVersion.from_dict(fv.to_dict())  # type: ignore[arg-type]
        assert parsed.mirrors == {"tg:999": [11, 12]}

    def test_version_without_mirrors_serializes_like_before(self):
        fv = TGFSFileVersion(
            id="v1",
            updated_at=datetime.datetime.now(),
            message_ids=[1],
        )
        assert "mirrors" not in fv.to_dict()
        parsed = TGFSFileVersion.from_dict(fv.to_dict())  # type: ignore[arg-type]
        assert parsed.mirrors == {}

    def test_version_from_sent_file_messages_aggregates_mirrors(self):
        fv = TGFSFileVersion.from_sent_file_message(
            SentFileMessage(message_id=1, size=10, mirrors={"tg:999": 11}),
            SentFileMessage(message_id=2, size=20, mirrors={"tg:999": 12}),
        )
        assert fv.mirrors == {"tg:999": [11, 12]}

    def test_version_partial_mirror_padded_with_zero(self):
        fv = TGFSFileVersion.from_sent_file_message(
            SentFileMessage(message_id=1, size=10, mirrors={"tg:999": 11}),
            SentFileMessage(message_id=2, size=20, mirrors={}),
        )
        assert fv.mirrors == {"tg:999": [11, 0]}

    def test_file_ref_round_trip(self):
        root = TGFSDirectory.root_dir()
        fr = root.create_file_ref("a.txt", 5)
        fr.mirrors = {"tg:999": 15}
        parsed = TGFSDirectory.from_dict(root.to_dict())
        assert parsed.find_file("a.txt").mirrors == {"tg:999": 15}

    def test_bare_keys_are_read_as_telegram_keys(self):
        # Metadata written by tgfs keys mirrors by bare channel id.
        fv = TGFSFileVersion.from_dict(
            {"type": "FV", "id": "v1", "updatedAt": 0, "messageIds": [1], "mirrors": {"999": [11]}}  # type: ignore[arg-type]
        )
        assert fv.mirrors == {"tg:999": [11]}
        assert fv.to_dict()["mirrors"] == {"tg:999": [11]}

        root = TGFSDirectory.root_dir()
        fr = root.create_file_ref("a.txt", 5)
        fr.mirrors = {"999": 15}
        parsed = TGFSDirectory.from_dict(root.to_dict())
        assert parsed.find_file("a.txt").mirrors == {"tg:999": 15}

    def test_store_round_trip(self):
        fv = TGFSFileVersion(
            id="v1",
            updated_at=datetime.datetime.now(),
            message_ids=[1],
            store="tg:111",
        )
        parsed = TGFSFileVersion.from_dict(fv.to_dict())  # type: ignore[arg-type]
        assert parsed.store == "tg:111"

        root = TGFSDirectory.root_dir()
        fr = root.create_file_ref("a.txt", 5)
        fr.store = "111"
        parsed_root = TGFSDirectory.from_dict(root.to_dict())
        assert parsed_root.find_file("a.txt").store == "tg:111"

    def test_store_absent_serializes_like_before(self):
        fv = TGFSFileVersion(
            id="v1", updated_at=datetime.datetime.now(), message_ids=[1]
        )
        assert "store" not in fv.to_dict()
        assert fv.owned_by("tg:anything")

    def test_relocate_takes_the_new_primary_copy(self):
        fv = TGFSFileVersion(
            id="v1",
            updated_at=datetime.datetime.now(),
            message_ids=[1, 2],
            mirrors={"tg:222": [11, 12]},
            store="tg:111",
        )
        fv.relocate("tg:222")
        assert fv.store == "tg:222"
        assert fv.message_ids == [11, 12]
        assert fv.mirrors == {"tg:111": [1, 2]}

    def test_relocate_keeps_ids_when_the_new_primary_has_no_copy(self):
        fv = TGFSFileVersion(
            id="v1",
            updated_at=datetime.datetime.now(),
            message_ids=[1, 2],
            mirrors={"tg:222": [11, 0]},
            store="tg:111",
        )
        fv.relocate("tg:222")
        assert fv.store == "tg:111"
        assert fv.message_ids == [1, 2]
        assert fv.mirrors == {"tg:222": [11, 0], "tg:111": [1, 2]}
        assert not fv.owned_by("tg:222")

    def test_relocate_is_a_no_op_without_a_store(self):
        fv = TGFSFileVersion(
            id="v1", updated_at=datetime.datetime.now(), message_ids=[1]
        )
        fv.relocate("tg:222")
        assert fv.store is None and fv.message_ids == [1]

    def test_file_ref_relocate(self):
        root = TGFSDirectory.root_dir()
        fr = root.create_file_ref("a.txt", 5)
        fr.store = "tg:111"
        fr.mirrors = {"tg:222": 15}
        fr.relocate("tg:222")
        assert fr.message_id == 15
        assert fr.store == "tg:222"
        assert fr.mirrors == {"tg:111": 5}

        moved = root.create_dir("d").find_files()  # noqa: F841
        relocated = fr.location.relocate_file_ref(fr, root.find_dir("d"))
        assert relocated.store == "tg:222"

    def test_file_ref_without_mirrors_serializes_like_before(self):
        root = TGFSDirectory.root_dir()
        root.create_file_ref("a.txt", 5)
        serialized = root.to_dict()
        assert "mirrors" not in serialized["files"][0]


def make_mirror_api(mocker, key="tg:999", backend="telegram", max_part_bytes=1 << 31):
    api = mocker.AsyncMock()
    api.key = key
    api.backend = backend
    api.caps = StoreCapabilities(
        max_part_bytes=max_part_bytes,
        max_text_chars=4096,
        supports_server_copy=backend == "telegram",
    )
    return api


def make_group(mocker, strict=False, mode="forward", channel_key="tg:999"):
    primary = mocker.AsyncMock()
    primary.key = "tg:111"
    primary.backend = "telegram"
    primary.caps = StoreCapabilities(
        max_part_bytes=1 << 31, max_text_chars=4096, supports_server_copy=True
    )
    mirror_api = make_mirror_api(mocker, channel_key)
    group = MirrorGroup(
        primary=primary,
        stores=[MirrorStore(key=channel_key, store=mirror_api)],
        mode=mode,
        strict=strict,
    )
    return group, primary, mirror_api


class TestMirrorGroup:
    @pytest.mark.asyncio
    async def test_mirror_parts_forwards(self, mocker):
        group, primary, mirror_api = make_group(mocker)
        mirror_api.copy_from.return_value = [11, 12]

        res = await group.mirror_parts([1, 2], [5, 5])

        mirror_api.copy_from.assert_awaited_once_with(primary, [1, 2])
        mirror_api.upload.assert_not_awaited()
        assert res.mirrors == {"tg:999": [11, 12]}
        assert res.replicas == {}

    @pytest.mark.asyncio
    async def test_mirror_parts_non_strict_swallows_errors(self, mocker):
        group, _, mirror_api = make_group(mocker, strict=False)
        mirror_api.copy_from.side_effect = Exception("boom")

        res = await group.mirror_parts([1, 2], [5, 5])

        assert res.store_keys == []

    @pytest.mark.asyncio
    async def test_mirror_parts_strict_raises(self, mocker):
        group, _, mirror_api = make_group(mocker, strict=True)
        mirror_api.copy_from.side_effect = Exception("boom")

        with pytest.raises(TechnicalError):
            await group.mirror_parts([1, 2], [5, 5])

    @pytest.mark.asyncio
    async def test_forward_mode_refuses_a_store_without_server_copy(self, mocker):
        group, _, mirror_api = make_group(mocker, strict=True, mode="forward")
        mirror_api.copy_from.return_value = None

        with pytest.raises(TechnicalError, match="server-side"):
            await group.mirror_parts([1], [4])
        mirror_api.upload.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_auto_mode_reuploads_when_server_copy_is_unavailable(self, mocker):
        group, primary, mirror_api = make_group(mocker, strict=True, mode="auto")
        mirror_api.copy_from.return_value = None
        primary.get_messages.return_value = [
            MessageResp(
                message_id=1,
                text="",
                document=Document(
                    size=4, id=1, access_hash=1, file_reference=b"", mime_type=None
                ),
            )
        ]

        async def chunks():
            yield b"data"

        primary.download_file.return_value = mocker.Mock(chunks=chunks())
        mirror_api.upload.return_value = [SentFileMessage(message_id=21, size=4)]

        res = await group.mirror_parts([1], [4])

        assert res.mirrors == {"tg:999": [21]}
        primary.download_file.assert_awaited_once_with(1, 0, 3)
        uploaded = mirror_api.upload.call_args[0][0]
        assert uploaded.size == 4

    @pytest.mark.asyncio
    async def test_auto_mode_falls_back_when_server_copy_fails(self, mocker):
        group, primary, mirror_api = make_group(mocker, strict=True, mode="auto")
        mirror_api.copy_from.side_effect = Exception("CHAT_FORWARDS_RESTRICTED")
        primary.get_messages.return_value = [
            MessageResp(
                message_id=1,
                text="",
                document=Document(
                    size=4, id=1, access_hash=1, file_reference=b"", mime_type=None
                ),
            )
        ]

        async def chunks():
            yield b"data"

        primary.download_file.return_value = mocker.Mock(chunks=chunks())
        mirror_api.upload.return_value = [SentFileMessage(message_id=21, size=4)]

        assert (await group.mirror_parts([1], [4])).mirrors == {"tg:999": [21]}

    @pytest.mark.asyncio
    async def test_reupload_mode_never_asks_for_a_server_copy(self, mocker):
        group, primary, mirror_api = make_group(mocker, strict=True, mode="reupload")
        primary.get_messages.return_value = [
            MessageResp(
                message_id=1,
                text="",
                document=Document(
                    size=4, id=1, access_hash=1, file_reference=b"", mime_type=None
                ),
            )
        ]

        async def chunks():
            yield b"data"

        primary.download_file.return_value = mocker.Mock(chunks=chunks())
        mirror_api.upload.return_value = [SentFileMessage(message_id=21, size=4)]

        assert (await group.mirror_parts([1], [4])).mirrors == {"tg:999": [21]}
        mirror_api.copy_from.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_part_sizes_are_looked_up_when_not_given(self, mocker):
        group, primary, mirror_api = make_group(mocker, strict=True)
        mirror_api.copy_from.return_value = [11]
        primary.get_messages.return_value = [
            MessageResp(
                message_id=1,
                text="",
                document=Document(
                    size=4, id=1, access_hash=1, file_reference=b"", mime_type=None
                ),
            )
        ]

        res = await group.mirror_parts([1])

        assert res.mirrors == {"tg:999": [11]}
        primary.get_messages.assert_awaited_once_with([1])

    @pytest.mark.asyncio
    async def test_small_parts_store_receives_a_replica(self, mocker):
        # The mirror's messages are smaller than the primary's parts (a
        # Discord mirror of a Telegram primary): the whole version is
        # streamed through the mirror, which cuts it its own way.
        group, primary, _ = make_group(mocker, strict=True, mode="auto")
        small = make_mirror_api(mocker, "dc:9", backend="discord", max_part_bytes=3)
        group = MirrorGroup(
            primary=primary,
            stores=[MirrorStore(key="dc:9", store=small)],
            mode="auto",
            strict=True,
        )
        downloads = []

        async def download(mid, begin, end):
            downloads.append((mid, begin, end))

            async def chunks():
                yield b"ab" if mid == 1 else b"cdef"

            return mocker.Mock(chunks=chunks())

        primary.download_file.side_effect = download
        uploaded = []

        async def upload(file_msg):
            data = b""
            while chunk := await file_msg.read(3):
                data += chunk
            uploaded.append(data)
            return [
                SentFileMessage(message_id=100 + i, size=len(data[i : i + 3]))
                for i in range(0, len(data), 3)
            ]

        small.upload.side_effect = upload

        res = await group.mirror_parts([1, 2], [2, 4])

        assert res.mirrors == {}
        assert res.replicas["dc:9"].message_ids == [100, 103]
        assert res.replicas["dc:9"].part_sizes == [3, 3]
        assert uploaded == [b"abcdef"]
        assert downloads == [(1, 0, 1), (2, 0, 3)]
        small.copy_from.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_same_backend_is_always_aligned(self, mocker):
        # A server-side copy keeps the message whatever its size.
        group, primary, mirror_api = make_group(mocker, strict=True, mode="auto")
        mirror_api.caps = StoreCapabilities(
            max_part_bytes=1, max_text_chars=4096, supports_server_copy=True
        )
        mirror_api.copy_from.return_value = [11]

        res = await group.mirror_parts([1], [4000])

        assert res.mirrors == {"tg:999": [11]}

    def test_rejects_unknown_mode(self, mocker):
        with pytest.raises(ValueError):
            MirrorGroup(primary=mocker.AsyncMock(), stores=[], mode="raid5")  # type: ignore[arg-type]

    def test_missing_stores(self, mocker):
        group, _, _ = make_group(mocker)

        def version(mirrors=None, replicas=None):
            return TGFSFileVersion(
                id="v",
                updated_at=datetime.datetime.now(),
                message_ids=[1, 2],
                mirrors=mirrors or {},
                replicas=replicas or {},
            )

        assert group.missing_stores(version()) == ["tg:999"]
        assert group.missing_stores(version({"tg:999": [11]})) == ["tg:999"]
        assert group.missing_stores(version({"tg:999": [11, 0]})) == ["tg:999"]
        assert group.missing_stores(version({"tg:999": [11, 12]})) == []
        assert (
            group.missing_stores(
                version(replicas={"tg:999": Replica(message_ids=[5, 6, 7])})
            )
            == []
        )

    @pytest.mark.asyncio
    async def test_mirror_fd_edits_existing(self, mocker):
        group, _, mirror_api = make_group(mocker)
        mirror_api.edit_message_text.return_value = 20

        res = await group.mirror_fd("{}", existing={"tg:999": 20})

        mirror_api.edit_message_text.assert_awaited_once_with(
            message_id=20, message="{}"
        )
        mirror_api.send_text.assert_not_awaited()
        assert res == {"tg:999": 20}

    @pytest.mark.asyncio
    async def test_mirror_fd_resends_when_edit_target_gone(self, mocker):
        group, _, mirror_api = make_group(mocker)
        mirror_api.edit_message_text.side_effect = MessageNotFound(message_id=20)
        mirror_api.send_text.return_value = 21

        res = await group.mirror_fd("{}", existing={"tg:999": 20})

        assert res == {"tg:999": 21}

    @pytest.mark.asyncio
    async def test_mirror_fd_keeps_stale_id_on_failure(self, mocker):
        group, _, mirror_api = make_group(mocker)
        mirror_api.edit_message_text.side_effect = Exception("boom")

        res = await group.mirror_fd("{}", existing={"tg:999": 20})

        # A stale FD copy is still recoverable, so the id is kept.
        assert res == {"tg:999": 20}


def make_sent_version(mirrors=None):
    return TGFSFileVersion(
        id="v1",
        updated_at=datetime.datetime.now(),
        message_ids=[1],
        part_sizes=[7],
        mirrors=mirrors or {},
    )


class TestContentFailover:
    @pytest.mark.asyncio
    async def test_get_serves_from_mirror_when_primary_fails(self, mocker):
        group, primary, mirror_api = make_group(mocker)
        repo = StoreFileContentRepository(primary, mirror_group=group)

        primary.download_file.side_effect = Exception("CHANNEL_PRIVATE")

        async def mirror_download(message_id, begin, end):
            assert message_id == 11

            async def chunks():
                yield b"mirror!"

            return mocker.Mock(chunks=chunks())

        mirror_api.download_file.side_effect = mirror_download

        fv = make_sent_version(mirrors={"tg:999": [11]})
        result = await repo.get(fv, 0, -1, "a.txt")
        data = b"".join([chunk async for chunk in result])

        assert data == b"mirror!"
        assert group.primary_dead()

    @pytest.mark.asyncio
    async def test_get_raises_without_mirror(self, mocker):
        group, primary, _ = make_group(mocker)
        repo = StoreFileContentRepository(primary, mirror_group=group)
        primary.download_file.side_effect = Exception("boom")

        fv = make_sent_version()
        result = await repo.get(fv, 0, -1, "a.txt")
        with pytest.raises(Exception, match="boom"):
            async for _ in result:
                pass


def fd_json(message_ids, mirrors):
    fd = TGFSFileDesc(name="a.txt")
    fd.add_version(
        TGFSFileVersion(
            id="v1",
            updated_at=datetime.datetime.now(),
            message_ids=message_ids,
            mirrors=mirrors,
        )
    )
    return fd.to_json()


class TestFDFailover:
    @pytest.mark.asyncio
    async def test_fd_read_falls_back_to_mirror(self, mocker):
        group, primary, mirror_api = make_group(mocker)
        repo = StoreFDRepository(primary, mirror_group=group)

        root = TGFSDirectory.root_dir()
        fr = root.create_file_ref("a.txt", 5)
        fr.mirrors = {"tg:999": 15}

        text = fd_json([100], {"tg:999": [110]})
        doc = Document(size=7, id=1, access_hash=1, file_reference=b"", mime_type=None)

        async def primary_get(ids):
            # FD message and the content message are both gone.
            return [None for _ in ids]

        async def mirror_get(ids):
            res: list = []
            for mid in ids:
                if mid == 15:
                    res.append(MessageResp(message_id=15, text=text, document=None))
                elif mid == 110:
                    res.append(MessageResp(message_id=110, text="", document=doc))
                else:
                    res.append(None)
            return res

        primary.get_messages.side_effect = primary_get
        mirror_api.get_messages.side_effect = mirror_get

        fd = await repo.get(fr)

        version = fd.get_latest_version()
        assert version.is_valid()
        assert version.part_sizes == [7]

    @pytest.mark.asyncio
    async def test_save_mirrors_fd(self, mocker):
        group, primary, mirror_api = make_group(mocker)
        repo = StoreFDRepository(primary, mirror_group=group)
        primary.send_text.return_value = 5
        mirror_api.send_text.return_value = 15

        fd = TGFSFileDesc(name="a.txt")
        resp = await repo.save(fd)

        assert resp.message_id == 5
        assert resp.mirrors == {"tg:999": 15}


class FakeFDRepo:
    def __init__(self, fd):
        self.fd = fd
        self.saved = []

    async def get(self, fr, include_all_versions=False):
        return self.fd

    async def save(self, fd, fr=None):
        self.saved.append((fd, fr))
        return FDRepositoryResp(
            message_id=fr.message_id if fr else 5,
            fd=fd,
            mirrors={"tg:999": 15},
        )


class TestBackfill:
    @pytest.mark.asyncio
    async def test_backfill_mirrors_unmirrored_version(self, mocker):
        group, _, mirror_api = make_group(mocker)
        mirror_api.copy_from.return_value = [11]

        root = TGFSDirectory.root_dir()
        fr = root.create_file_ref("a.txt", 5)

        fd = TGFSFileDesc(name="a.txt")
        fd.add_version(make_sent_version())
        fd_repo = FakeFDRepo(fd)

        metadata_api = mocker.AsyncMock()
        client = SimpleNamespace(
            name="test",
            mirror_group=group,
            fd_repo=fd_repo,
            metadata_api=metadata_api,
            dir_api=SimpleNamespace(root=root),
        )

        report = await backfill_mirrors(client)  # type: ignore[arg-type]

        assert report.files_scanned == 1
        assert report.versions_mirrored == 1
        assert report.versions_promoted == 0
        assert report.failures == []
        assert fd.get_latest_version().mirrors == {"tg:999": [11]}
        assert fr.mirrors == {"tg:999": 15}
        assert len(fd_repo.saved) == 1
        metadata_api.push.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_backfill_skips_fully_mirrored(self, mocker):
        group, _, mirror_api = make_group(mocker)

        root = TGFSDirectory.root_dir()
        fr = root.create_file_ref("a.txt", 5)
        fr.mirrors = {"tg:999": 15}

        fd = TGFSFileDesc(name="a.txt")
        fd.add_version(make_sent_version(mirrors={"tg:999": [11]}))
        fd_repo = FakeFDRepo(fd)

        metadata_api = mocker.AsyncMock()
        client = SimpleNamespace(
            name="test",
            mirror_group=group,
            fd_repo=fd_repo,
            metadata_api=metadata_api,
            dir_api=SimpleNamespace(root=root),
        )

        report = await backfill_mirrors(client)  # type: ignore[arg-type]

        assert report.versions_mirrored == 0
        mirror_api.copy_from.assert_not_awaited()
        assert fd_repo.saved == []
        metadata_api.push.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_backfill_without_mirror_group(self):
        client = SimpleNamespace(
            name="test",
            mirror_group=None,
            fd_repo=None,
            metadata_api=None,
            dir_api=SimpleNamespace(root=TGFSDirectory.root_dir()),
        )
        report = await backfill_mirrors(client)  # type: ignore[arg-type]
        assert report.failures


class TestSaveMirrorsParts:
    @pytest.mark.asyncio
    async def test_save_records_mirror_ids_on_sent_messages(self, mocker):
        group, primary, mirror_api = make_group(mocker)
        mirror_api.copy_from.return_value = [11]
        primary.upload.return_value = [SentFileMessage(message_id=1, size=4)]

        repo = StoreFileContentRepository(primary, mirror_group=group)

        from tgdcfs.reqres import FileMessageFromBuffer

        res = await repo.save(FileMessageFromBuffer.new(buffer=b"data", name="a"))

        primary.upload.assert_awaited_once()
        assert res[0].mirrors == {"tg:999": 11}
