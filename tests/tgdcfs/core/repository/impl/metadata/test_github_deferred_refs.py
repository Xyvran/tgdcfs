"""``create_file_ref_deferred``: the reference exists at once, its blob is
written in the background.

An SFTP open reserves the name before the upload starts. A run of GitHub
errors used to hold that open for over 20 s, and FileZilla gives up after
20 s of silence although the upload itself only needs Telegram or Discord.
"""

import asyncio
import threading
import time
from unittest.mock import Mock

import pytest
import requests
from github import Github, GithubException
from github.Repository import Repository

from tgdcfs.core.model import TGFSDirectory
from tgdcfs.core.repository.impl.metadata.github_repo import gh_directory
from tgdcfs.core.repository.impl.metadata.github_repo.gh_directory import (
    GithubConfig,
    GithubDirectory,
)


@pytest.fixture
def ghc():
    repo = Mock(spec=Repository)
    repo.name = "test-repo"
    return GithubConfig(
        gh=Mock(spec=Github), repo_name="owner/test-repo", repo=repo, commit="main"
    )


@pytest.fixture(autouse=True)
def _no_retry_delay(monkeypatch):
    monkeypatch.setattr(gh_directory, "DEFERRED_PUT_RETRY_DELAY", 0)


async def _wait_for_background(directory: GithubDirectory) -> None:
    pending = [t for t in list(directory._deferred_refs) if not t.done()]
    if pending:
        await asyncio.gather(*pending)


def _sha_missing() -> GithubException:
    return GithubException(
        422, {"message": 'Invalid request.\n\n"sha" wasn\'t supplied.'}, {}
    )


async def test_the_ref_is_there_before_the_blob_is(ghc):
    blob_done = threading.Event()

    def create_file(**_):
        time.sleep(0.3)
        blob_done.set()

    ghc.repo.create_file.side_effect = create_file
    directory = GithubDirectory(ghc, "d", None)

    fr = await directory.create_file_ref_deferred("f", 1)

    assert fr.name == "f"
    assert [r.name for r in directory.files] == ["f"]
    assert not blob_done.is_set()

    await _wait_for_background(directory)
    assert blob_done.is_set()
    ghc.repo.create_file.assert_called_once_with(
        path="f.1",
        message="Create file reference for f",
        content="",
        branch="main",
    )


async def test_the_caller_does_not_wait_for_github(ghc):
    ghc.repo.create_file.side_effect = lambda **_: time.sleep(0.3)
    directory = GithubDirectory(ghc, "d", None)

    start = time.monotonic()
    await directory.create_file_ref_deferred("f", 1)

    assert time.monotonic() - start < 0.1
    await _wait_for_background(directory)


async def test_transient_failures_are_retried_in_the_background(ghc):
    ghc.repo.create_file.side_effect = [
        requests.exceptions.ConnectionError("Max retries"),
        None,
    ]
    directory = GithubDirectory(ghc, "d", None)

    await directory.create_file_ref_deferred("f", 1)
    await _wait_for_background(directory)

    assert ghc.repo.create_file.call_count == 2
    assert [r.name for r in directory.files] == ["f"]


async def test_a_blob_that_landed_on_an_earlier_attempt_survives(ghc):
    lost_response = requests.exceptions.ConnectionError("IncompleteRead")
    ghc.repo.create_file.side_effect = [lost_response, _sha_missing()]
    directory = GithubDirectory(ghc, "d", None)

    await directory.create_file_ref_deferred("f", 1)
    await _wait_for_background(directory)

    assert ghc.repo.create_file.call_count == 2
    assert [r.name for r in directory.files] == ["f"]


async def test_a_blob_that_exists_already_is_not_an_error(ghc):
    ghc.repo.create_file.side_effect = [_sha_missing()]
    directory = GithubDirectory(ghc, "d", None)

    await directory.create_file_ref_deferred("f", 1)
    await _wait_for_background(directory)

    assert [r.name for r in directory.files] == ["f"]


async def test_a_permanent_failure_drops_the_ref(ghc):
    ghc.repo.create_file.side_effect = GithubException(404, {}, {})
    directory = GithubDirectory(ghc, "d", None)

    await directory.create_file_ref_deferred("f", 1)
    assert [r.name for r in directory.files] == ["f"]

    await _wait_for_background(directory)

    assert ghc.repo.create_file.call_count == 1
    assert len(directory.files) == 0


async def test_gives_up_after_the_attempts(ghc, monkeypatch):
    monkeypatch.setattr(gh_directory, "DEFERRED_PUT_ATTEMPTS", 3)
    ghc.repo.create_file.side_effect = GithubException(500, {}, {})
    directory = GithubDirectory(ghc, "d", None)

    await directory.create_file_ref_deferred("f", 1)
    await _wait_for_background(directory)

    assert ghc.repo.create_file.call_count == 3
    assert len(directory.files) == 0


async def test_a_ref_removed_meanwhile_is_not_written(ghc):
    """A delete that got in before the blob was written must stay deleted."""
    directory = GithubDirectory(ghc, "d", None)

    async with ghc._async_lock():  # a writer is busy; the blob has to wait
        fr = await directory.create_file_ref_deferred("f", 1)
        await asyncio.sleep(0)
        directory.files.remove(fr)

    await _wait_for_background(directory)

    ghc.repo.create_file.assert_not_called()


async def test_a_plain_directory_defers_to_the_sync_path():
    a = TGFSDirectory("a", None, [], [])

    fr = await a.create_file_ref_deferred("f", 1)

    assert [r.name for r in a.files] == ["f"]
    assert fr.message_id == 1
