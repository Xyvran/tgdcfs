"""Writes of the ``github_repo`` metadata backend run off the event loop
and one at a time.

PyGithub is synchronous. A commit on the event loop froze every other
request for as long as GitHub took to answer (seconds per file during a
bulk upload), and two commits made at once raced for the branch head,
so the stats commit kept failing with 422 "not a fast forward".
"""

import asyncio
import threading
import time
from unittest.mock import Mock, patch

import pytest
from github import Github
from github.Repository import Repository

from tgdcfs.config import GithubRepoConfig
from tgdcfs.core.model import FileStat, TGFSMetadata
from tgdcfs.core.repository.impl.metadata.github_repo import (
    GithubRepoMetadataRepository,
)
from tgdcfs.core.repository.impl.metadata.github_repo.dir_timestamps import (
    DirTimestampStore,
)
from tgdcfs.core.repository.impl.metadata.github_repo.gh_directory import (
    GithubConfig,
    GithubDirectory,
)

# How long a mocked GitHub call takes.
SLOW = 0.2


class Overlap:
    """Counts how many mocked GitHub writes run at the same time."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self.running = 0
        self.most = 0
        self.threads: set[int] = set()

    def call(self, *args, **kwargs):
        with self._lock:
            self.running += 1
            self.most = max(self.most, self.running)
            self.threads.add(threading.get_ident())
        try:
            time.sleep(SLOW)
            return Mock(sha="new")
        finally:
            with self._lock:
                self.running -= 1


@pytest.fixture
def repo():
    repo = Mock(spec=Repository)
    repo.created_at = repo.pushed_at = repo.updated_at = None
    return repo


@pytest.fixture
def ghc(repo):
    return GithubConfig(gh=Mock(spec=Github), repo_name="o/r", repo=repo, commit="main")


async def ticks_while(coro) -> int:
    """Run ``coro`` and count how often the event loop got to run other
    work meanwhile."""
    task = asyncio.ensure_future(coro)
    ticks = 0
    while not task.done():
        await asyncio.sleep(0.01)
        ticks += 1
    await task
    return ticks


class TestOffTheLoop:
    @pytest.mark.asyncio
    async def test_a_file_ref_commit_leaves_the_loop_free(self, ghc, repo):
        overlap = Overlap()
        repo.create_file.side_effect = overlap.call
        directory = GithubDirectory(ghc, "root", None)

        ticks = await ticks_while(directory.create_file_ref_async("a.txt", 10))

        # On the loop, the sleep would have run without a single tick.
        assert ticks >= 5
        assert threading.get_ident() not in overlap.threads
        assert [f.name for f in directory.files] == ["a.txt"]

    @pytest.mark.asyncio
    async def test_the_ref_is_in_the_tree_while_its_commit_runs(self, ghc, repo):
        overlap = Overlap()
        repo.create_file.side_effect = overlap.call
        directory = GithubDirectory(ghc, "root", None)

        task = asyncio.ensure_future(directory.create_file_ref_async("a.txt", 10))
        await asyncio.sleep(SLOW / 4)

        assert [f.name for f in directory.files] == ["a.txt"]
        await task

    @pytest.mark.asyncio
    async def test_a_failed_commit_takes_the_ref_out_again(self, ghc, repo):
        repo.create_file.side_effect = RuntimeError("GitHub down")
        directory = GithubDirectory(ghc, "root", None)

        with pytest.raises(RuntimeError):
            await directory.create_file_ref_async("a.txt", 10)

        assert directory.files == []

    @pytest.mark.asyncio
    async def test_a_new_folder_is_a_github_folder_from_the_start(self, ghc, repo):
        overlap = Overlap()
        repo.create_file.side_effect = overlap.call
        root = GithubDirectory(ghc, "root", None)

        task = asyncio.ensure_future(root.create_dir_async("sub"))
        await asyncio.sleep(SLOW / 4)
        # A write into the folder while its own commit runs reaches GitHub.
        assert isinstance(root.children[0], GithubDirectory)
        await task

        assert root.children[0] is task.result()
        repo.create_file.assert_called_once()
        assert repo.create_file.call_args.kwargs["path"] == "sub/.gitkeep"

    @pytest.mark.asyncio
    async def test_a_failed_folder_commit_takes_the_folder_out_again(self, ghc, repo):
        repo.create_file.side_effect = RuntimeError("GitHub down")
        root = GithubDirectory(ghc, "root", None)

        with pytest.raises(RuntimeError):
            await root.create_dir_async("sub")

        assert root.children == []

    @pytest.mark.asyncio
    async def test_a_file_ref_is_deleted_off_the_loop(self, ghc, repo):
        directory = GithubDirectory(ghc, "root", None)
        repo.create_file.return_value = Mock()
        fr = await directory.create_file_ref_async("a.txt", 10)
        overlap = Overlap()
        repo.get_contents.return_value = Mock(sha="abc")
        repo.delete_file.side_effect = overlap.call

        ticks = await ticks_while(fr.delete_async())

        assert ticks >= 5
        assert directory.files == []
        assert repo.delete_file.call_args.kwargs["path"] == "a.txt.10"

    @pytest.mark.asyncio
    async def test_a_move_between_folders_commits_off_the_loop(self, ghc, repo):
        root = GithubDirectory(ghc, "root", None)
        source = root.create_dir_skip_github_ops("src")
        dest = root.create_dir_skip_github_ops("dest")
        repo.create_file.return_value = Mock()
        fr = await source.create_file_ref_async("a.txt", 10)
        fr.stat = FileStat(size=3, created_at=1, modified_at=2)
        repo.get_contents.return_value = Mock(sha="abc")

        moved = await source.relocate_file_ref_async(fr, dest, "b.txt")

        assert source.files == []
        assert [f.name for f in dest.files] == ["b.txt"]
        assert moved.stat == fr.stat
        assert repo.create_file.call_args.kwargs["path"] == "dest/b.txt.10"
        assert repo.delete_file.call_args.kwargs["path"] == "src/a.txt.10"


class TestOneCommitAtATime:
    @pytest.mark.asyncio
    async def test_file_ref_commits_do_not_overlap(self, ghc, repo):
        overlap = Overlap()
        repo.create_file.side_effect = overlap.call
        directory = GithubDirectory(ghc, "root", None)

        await asyncio.gather(
            *(directory.create_file_ref_async(f"{i}.txt", i) for i in range(4))
        )

        assert overlap.most == 1
        assert len(directory.files) == 4

    @pytest.mark.asyncio
    async def test_the_sync_path_waits_for_the_async_one(self, ghc, repo):
        overlap = Overlap()
        repo.create_file.side_effect = overlap.call
        directory = GithubDirectory(ghc, "root", None)

        task = asyncio.ensure_future(directory.create_file_ref_async("a.txt", 1))
        await asyncio.sleep(SLOW / 4)
        await asyncio.to_thread(directory.create_file_ref, "b.txt", 2)
        await task

        assert overlap.most == 1

    @pytest.mark.asyncio
    async def test_the_stats_commit_waits_for_file_ref_commits(self, repo):
        with (
            patch(
                "tgdcfs.core.repository.impl.metadata.github_repo.Github"
            ) as github_class,
            patch(
                "tgdcfs.core.repository.impl.metadata.github_repo.DirTimestampStore",
                return_value=Mock(spec=DirTimestampStore),
            ),
        ):
            github_class.return_value.get_repo.return_value = repo
            repository = GithubRepoMetadataRepository(
                GithubRepoConfig(access_token="t", repo="o/r", commit="main")
            )
        tree = Mock(truncated=False, tree=[])
        repo.get_git_tree.return_value = tree
        root = repository._build_directory_structure()
        repository.metadata = TGFSMetadata(dir=root)

        overlap = Overlap()
        repo.create_file.side_effect = overlap.call
        ref = Mock()
        ref.object.sha = "head"
        ref.edit.side_effect = overlap.call
        repo.get_git_ref.return_value = ref
        repo.create_git_commit.return_value = Mock(sha="new")

        first = await root.create_file_ref_async("a.txt", 10)
        first.stat = FileStat(size=1, created_at=1, modified_at=2)
        await asyncio.gather(
            repository.save_stats([root], keep=True),
            root.create_file_ref_async("b.txt", 11),
            root.create_file_ref_async("c.txt", 12),
        )

        assert overlap.most == 1
        ref.edit.assert_called_once_with("new")


class TestConcurrentChanges:
    @pytest.mark.asyncio
    async def test_two_deletes_of_one_file_do_not_fail(self, ghc, repo):
        directory = GithubDirectory(ghc, "root", None)
        repo.create_file.return_value = Mock()
        fr = await directory.create_file_ref_async("a.txt", 10)
        overlap = Overlap()
        repo.get_contents.return_value = Mock(sha="abc")
        repo.delete_file.side_effect = overlap.call

        await asyncio.gather(fr.delete_async(), fr.delete_async())

        assert directory.files == []
