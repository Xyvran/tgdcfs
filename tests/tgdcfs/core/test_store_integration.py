"""The whole stack on in-memory stores: upload, mirror, fail over, delete.

Two ``FakeStore`` instances stand in for a primary and a mirror. One
scenario keeps them on the same backend (server-side copies, the
Telegram case), the other puts the mirror on a different backend, so
every copy has to be re-uploaded -- the shape of a Discord mirror.
"""

from typing import cast

import pytest

from tgdcfs.backends.base import IStore
from tgdcfs.core.api import DirectoryApi, FileApi, FileDescApi, MetaDataApi
from tgdcfs.core.mirror import MirrorGroup, MirrorStore
from tgdcfs.core.model import TGFSDirectory, TGFSMetadata
from tgdcfs.core.repository.impl import (
    PinnedMessageMetadataRepository,
    StoreFDRepository,
    StoreFileContentRepository,
)
from tgdcfs.reqres import FileMessageFromBuffer
from tests.fakes.store import FakeStore


class Stack:
    def __init__(self, primary: FakeStore, mirror: FakeStore, mode: str = "auto"):
        self.primary = primary
        self.mirror = mirror
        self.group = MirrorGroup(
            primary=primary,
            stores=[MirrorStore(key=mirror.key, store=mirror)],
            mode=mode,  # type: ignore[arg-type]
            strict=True,
        )
        self.fc_repo = StoreFileContentRepository(primary, mirror_group=self.group)
        self.fd_repo = StoreFDRepository(primary, mirror_group=self.group)
        self.metadata_repo = PinnedMessageMetadataRepository(
            primary, self.fc_repo, mirror_group=self.group
        )
        self.metadata_api = MetaDataApi(self.metadata_repo)
        self.file_api = FileApi(
            self.metadata_api,
            FileDescApi(self.fd_repo, self.fc_repo),
            cast(IStore, primary),
            mirror_group=self.group,
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

    async def read(self, name: str, begin: int = 0, end: int = -1) -> bytes:
        fr = self.root.find_file(name)
        stream = await self.file_api.retrieve(fr, begin, end, name)
        return b"".join([chunk async for chunk in stream])


def same_backend_stack() -> Stack:
    return Stack(
        FakeStore(key="tg:1", backend_name="telegram"),
        FakeStore(key="tg:2", backend_name="telegram"),
    )


def cross_backend_stack() -> Stack:
    # The mirror cannot copy server-side and has a part size that still
    # fits every primary part (a smaller one needs replicas, phase 3).
    return Stack(
        # The pinned metadata blob has to fit one primary part (as on
        # Telegram, where a part is 2 GiB), so the primary part stays well
        # above the blob and the test files are sized to span several parts.
        FakeStore(key="tg:1", backend_name="telegram", max_part_bytes=2048),
        FakeStore(
            key="dc:9", backend_name="discord", max_part_bytes=8192, server_copy=False
        ),
    )


@pytest.fixture(params=["same_backend", "cross_backend"])
async def stack(request) -> Stack:
    build = (
        same_backend_stack if request.param == "same_backend" else cross_backend_stack
    )
    return await build().init()


class TestUploadAndMirror:
    async def test_upload_is_mirrored_and_recorded(self, stack: Stack):
        data = b"0123456789" * 500  # 5000 bytes: several parts on the 2 KiB primary
        await stack.put("a.bin", data)

        fr = stack.root.find_file("a.bin")
        fd = await stack.fd_repo.get(fr)
        version = fd.get_latest_version()

        assert stack.mirror.key in version.mirrors
        mirror_ids = version.mirrors[stack.mirror.key]
        assert len(mirror_ids) == len(version.message_ids)
        # Byte-identical copies, part by part.
        for primary_id, mirror_id in zip(version.message_ids, mirror_ids):
            assert stack.mirror.document_of(mirror_id) == stack.primary.document_of(
                primary_id
            )
        # The descriptor has a copy too.
        assert stack.mirror.key in fr.mirrors
        assert stack.mirror.messages[fr.mirrors[stack.mirror.key]].text == fd.to_json()

    async def test_copy_strategy_matches_the_backends(self, stack: Stack):
        await stack.put("a.bin", b"x" * 3000)

        if stack.mirror.backend_name == stack.primary.backend_name:
            assert "upload" not in stack.mirror.calls
        else:
            assert "upload" in stack.mirror.calls

    async def test_read_survives_a_dead_primary(self, stack: Stack):
        data = b"abcdefghijklmnopqrstuvwxyz0123456789" * 100
        await stack.put("a.bin", data)

        stack.primary.fail.update({"download_file", "get_messages"})

        assert await stack.read("a.bin") == data
        assert await stack.read("a.bin", 5, 3000) == data[5:3001]
        assert stack.group.primary_dead()

    async def test_remove_deletes_the_mirror_copies_too(self, stack: Stack):
        await stack.put("a.bin", b"x" * 3000)
        fr = stack.root.find_file("a.bin")
        fd = await stack.fd_repo.get(fr)
        mirror_ids = fd.get_latest_version().mirrors[stack.mirror.key]
        assert all(mid in stack.mirror.messages for mid in mirror_ids)

        await stack.file_api.rm(fr)

        assert all(mid not in stack.mirror.messages for mid in mirror_ids)
        assert fr.mirrors[stack.mirror.key] not in stack.mirror.messages

    async def test_pinned_metadata_is_mirrored(self, stack: Stack):
        await stack.dir_api.create("docs", stack.root)

        assert stack.primary.pinned is not None
        assert stack.mirror.pinned is not None
        assert stack.mirror.document_of(
            stack.mirror.pinned
        ) == stack.primary.document_of(stack.primary.pinned)

    async def test_copy_keeps_the_mirror_in_step(self, stack: Stack):
        data = b"y" * 4500
        await stack.put("a.bin", data)
        fr = stack.root.find_file("a.bin")

        copied = await stack.file_api.copy(stack.root, fr, "b.bin")
        fd = await stack.fd_repo.get(copied)
        version = fd.get_latest_version()

        assert version.mirrors[stack.mirror.key]
        assert await stack.read("b.bin") == data


class TestPromotion:
    async def test_mirror_serves_as_primary_after_a_swap(self):
        """Promotion is a config change: the old mirror becomes primary.

        Content ids of the old primary are then found in the version's
        ``mirrors`` map under its key, and reads keep working.
        """
        stack = await same_backend_stack().init()
        data = b"promote me " * 300
        await stack.put("a.bin", data)
        metadata = TGFSMetadata.from_dict(stack.metadata_repo.metadata.to_dict())  # type: ignore[union-attr]

        promoted = Stack(stack.mirror, stack.primary)
        promoted.metadata_repo.metadata = metadata
        root = promoted.root
        fr = root.find_file("a.bin")
        fd = await promoted.fd_repo.get(fr)
        version = fd.get_latest_version()

        # The old primary's parts are the old mirror's mirrors now, and the
        # version has been re-expressed relative to the new primary.
        assert version.store == stack.mirror.key
        assert version.mirrors[stack.primary.key]
        assert fr.store == stack.mirror.key
        assert fr.mirrors[stack.primary.key]
        assert await promoted.read("a.bin") == data

        # And reads still work when the old primary is gone for good.
        stack.primary.fail.update({"download_file", "get_messages"})
        assert await promoted.read("a.bin") == data

    async def test_versions_without_a_copy_in_the_new_primary_stay_readable(self):
        """A mirror that never received a version can still be promoted:
        the version is served from the old store and nothing is ever
        written to its ids in the new one."""
        stack = await same_backend_stack().init()
        data = b"only here " * 300
        # Neither copy path reaches the mirror, and the group is lenient.
        stack.mirror.fail.update({"copy_from", "upload"})
        stack.group._strict = False
        await stack.put("a.bin", data)
        stack.mirror.fail.clear()
        old_count = len(stack.mirror.messages)
        metadata = TGFSMetadata.from_dict(stack.metadata_repo.metadata.to_dict())  # type: ignore[union-attr]

        promoted = Stack(stack.mirror, stack.primary)
        promoted.metadata_repo.metadata = metadata
        fr = promoted.root.find_file("a.bin")
        fd = await promoted.fd_repo.get(fr)
        version = fd.get_latest_version()

        # Still owned by the old store, and readable from there.
        assert version.store == stack.primary.key
        assert version.mirrors[stack.primary.key] == version.message_ids
        assert await promoted.read("a.bin") == data

        # A new version written now lands in the new primary; the old
        # version keeps pointing at the old store. Versions are looked up
        # by id: two versions written within the same millisecond sort
        # arbitrarily, and this test can be that fast.
        old_version_id = version.id
        await promoted.put("a.bin", b"new " * 500)
        fd = await promoted.fd_repo.get(fr, include_all_versions=True)
        new_version = fd.get_latest_version()
        assert new_version.id != old_version_id
        assert new_version.store == stack.mirror.key
        assert fd.get_version(old_version_id).store == stack.primary.key
        assert await promoted.read("a.bin") == b"new " * 500
        # The descriptor was sent afresh rather than edited by a foreign id.
        assert fr.store == stack.mirror.key
        assert len(stack.mirror.messages) > old_count

        # Removing the file deletes the right messages in each store.
        old_ids = set(fd.get_version(old_version_id).message_ids)
        await promoted.file_api.rm(fr)
        assert not old_ids & set(stack.primary.messages)
