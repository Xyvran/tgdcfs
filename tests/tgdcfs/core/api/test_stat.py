import datetime
from unittest.mock import AsyncMock, Mock

import pytest

from tgdcfs.core.api import FileStatApi
from tgdcfs.core.model import FileStat, TGFSDirectory, TGFSFileDesc, TGFSFileVersion
from tgdcfs.utils.time import ts

OLD = datetime.datetime(2024, 1, 1, 12, 0, 0)
NEW = datetime.datetime(2024, 6, 1, 12, 0, 0)


def file_desc_api(fd: TGFSFileDesc, size: int = 1234) -> Mock:
    api = Mock()
    api.get_file_desc = AsyncMock(return_value=fd)
    api.content_length = AsyncMock(return_value=size)
    return api


def two_versions() -> TGFSFileDesc:
    fd = TGFSFileDesc(name="a.txt")
    fd.add_version(TGFSFileVersion(id="old", updated_at=OLD, message_ids=[1]))
    fd.add_version(TGFSFileVersion(id="new", updated_at=NEW, message_ids=[2]))
    return fd


class TestFileStatApi:
    @pytest.mark.asyncio
    async def test_dates_come_from_the_versions(self):
        stat = await FileStatApi(file_desc_api(two_versions())).get(Mock())

        assert stat.size == 1234
        assert stat.created_at == ts(OLD)
        assert stat.modified_at == ts(NEW)

    @pytest.mark.asyncio
    async def test_the_validate_setting_reaches_the_descriptor_read(self):
        api = file_desc_api(two_versions())
        fr = Mock()

        await FileStatApi(api, validate=False).get(fr)

        api.get_file_desc.assert_awaited_once_with(fr, validate=False)

    @pytest.mark.asyncio
    async def test_an_unknown_size_is_reported_as_zero(self):
        stat = await FileStatApi(
            file_desc_api(TGFSFileDesc.empty("a.txt"), size=-1)
        ).get(Mock())

        assert stat.size == 0


def github_like(stored: bool = True, loaded: bool = True) -> Mock:
    """A metadata API that keeps the stats apart from the tree, like
    ``github_repo``: a push does not write them."""
    api = Mock()
    api.push_writes_stats = False
    api.stats_stored = Mock(return_value=stored)
    api.stats_loaded = Mock(return_value=loaded)
    api.load_stats = AsyncMock(return_value=True)
    api.save_stats = AsyncMock()
    return api


def file_in_tree(name: str = "a.txt"):
    directory = TGFSDirectory.root_dir()
    return directory.create_file_ref(name, 10)


class TestUrgency:
    """A dropped stat is written soon only when the metadata may hold it:
    a bulk upload must not cause a stats commit every few seconds."""

    def stats(self, metadata_api) -> FileStatApi:
        return FileStatApi(
            file_desc_api(two_versions()), metadata_api=metadata_api, persist=True
        )

    @pytest.mark.asyncio
    async def test_a_stat_remembered_by_a_write_is_written_without_hurry(self):
        api = self.stats(github_like())
        fr = file_in_tree()

        await api.remember(fr, two_versions())

        assert fr.stat == FileStat(1234, ts(OLD), ts(NEW))
        assert api._dirty and api._urgent is False

    @pytest.mark.asyncio
    async def test_overwriting_an_unwritten_stat_is_not_urgent(self):
        api = self.stats(github_like())
        fr = file_in_tree()
        await api.remember(fr, two_versions())

        api.invalidate(fr)

        assert fr.stat is None
        assert api._urgent is False

    @pytest.mark.asyncio
    async def test_overwriting_a_stored_stat_is_urgent(self):
        api = self.stats(github_like())
        fr = file_in_tree()
        fr.stat = FileStat(1, 1, 1)
        fr.stat_stored = True

        api.invalidate(fr)

        assert api._urgent is True

    @pytest.mark.asyncio
    async def test_a_folder_whose_stats_were_not_read_yet_is_urgent(self):
        api = self.stats(github_like(stored=True, loaded=False))
        fr = file_in_tree()

        api.invalidate(fr)

        assert api._urgent is True

    @pytest.mark.asyncio
    async def test_a_file_without_any_stat_marks_nothing(self):
        api = self.stats(github_like(stored=True, loaded=True))
        fr = file_in_tree()

        api.invalidate(fr)

        assert not api._dirty

    @pytest.mark.asyncio
    async def test_a_write_notes_the_stats_it_stored(self):
        api = self.stats(github_like())
        fr = file_in_tree()
        await api.remember(fr, two_versions())

        assert await api.flush() is True

        assert fr.stat_stored is True

    @pytest.mark.asyncio
    async def test_a_change_during_the_read_keeps_the_stat_out(self):
        fd_api = file_desc_api(two_versions())
        api = FileStatApi(fd_api, metadata_api=github_like(), persist=True)
        fr = file_in_tree()

        async def size_while_overwritten(_fv):
            api.invalidate(fr)  # a second write lands meanwhile
            return 1234

        fd_api.content_length = AsyncMock(side_effect=size_while_overwritten)
        await api.remember(fr, two_versions())

        assert fr.stat is None

    @pytest.mark.asyncio
    async def test_off_remembers_nothing(self):
        api = FileStatApi(file_desc_api(two_versions()))
        fr = file_in_tree()

        await api.remember(fr, two_versions())

        assert fr.stat is None
