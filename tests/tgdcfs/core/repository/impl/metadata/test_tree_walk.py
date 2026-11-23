"""A repo too big for one recursive tree is read one directory at a time.

GitHub cuts the recursive tree off at 100,000 entries or 7 MB, which a few
tens of thousands of encrypted file names reach. The contents API is no way
out: it stops at 1000 entries per directory, so a folder of 20k files came
back with 1000 of them.
"""

from unittest.mock import Mock, patch

import pytest
from github import Github
from github.Repository import Repository

from tgdcfs.config import GithubRepoConfig
from tgdcfs.core.repository.impl.metadata.github_repo import (
    DirTimestampStore,
    GithubRepoMetadataRepository,
)


def entry(path: str, kind: str, sha: str) -> Mock:
    e = Mock()
    e.path, e.type, e.sha = path, kind, sha
    return e


def tree(entries, truncated=False) -> Mock:
    t = Mock()
    t.tree = entries
    t.truncated = truncated
    return t


@pytest.fixture
def repo():
    with (
        patch(
            "tgdcfs.core.repository.impl.metadata.github_repo.DirTimestampStore"
        ) as store_cls,
        patch("tgdcfs.core.repository.impl.metadata.github_repo.Github") as gh_cls,
    ):
        store_cls.return_value = Mock(spec=DirTimestampStore)
        repo = Mock(spec=Repository)
        gh = Mock(spec=Github)
        gh.get_repo.return_value = repo
        gh_cls.return_value = gh
        yield repo


def load(repo):
    import asyncio

    metadata = GithubRepoMetadataRepository(
        GithubRepoConfig(access_token="t", repo="o/r", commit="main")
    )
    return asyncio.run(metadata.get()).dir


def test_a_big_folder_is_read_whole(repo):
    files = [entry(f"f{i}.{i + 1}", "blob", f"s{i}") for i in range(1500)]
    subtree = tree(files)
    root = tree([entry("big", "tree", "sha-big")])
    repo.get_git_tree.side_effect = lambda ref, recursive=False: {
        ("main", True): tree([], truncated=True),
        ("main", False): root,
        ("sha-big", False): subtree,
    }[(ref, recursive)]
    repo.get_contents.side_effect = AssertionError("contents API must not be used")

    directory = load(repo)

    (big,) = directory.children
    assert big.name == "big"
    assert len(big.files) == 1500


def test_nested_folders_get_their_full_path(repo):
    repo.get_git_tree.side_effect = lambda ref, recursive=False: {
        ("main", True): tree([], truncated=True),
        ("main", False): tree([entry("a", "tree", "sha-a")]),
        ("sha-a", False): tree(
            [entry("b", "tree", "sha-b"), entry("one.7", "blob", "s1")]
        ),
        ("sha-b", False): tree([entry("two.8", "blob", "s2")]),
    }[(ref, recursive)]

    directory = load(repo)

    a = directory.children[0]
    assert [f.name for f in a.files] == ["one"]
    assert [f.name for f in a.children[0].files] == ["two"]
    assert a.children[0].name == "b"


def test_a_truncated_directory_falls_back_to_the_contents_api(repo):
    repo.get_git_tree.side_effect = lambda ref, recursive=False: tree(
        [], truncated=True
    )
    repo.get_contents.return_value = []

    directory = load(repo)

    repo.get_contents.assert_called_once()
    assert directory.children == [] and directory.files == []


def load_with(repo, **config_kwargs):
    import asyncio

    metadata = GithubRepoMetadataRepository(
        GithubRepoConfig(access_token="t", repo="o/r", commit="main", **config_kwargs)
    )
    return asyncio.run(metadata.get()).dir


def test_the_walk_reads_directories_with_bounded_concurrency(repo):
    import threading

    state = {"in_flight": 0, "peak": 0}
    lock = threading.Lock()

    def get_tree(ref, recursive=False):
        with lock:
            state["in_flight"] += 1
            state["peak"] = max(state["peak"], state["in_flight"])
        try:
            if ref == "main":
                return tree([entry(f"d{i}", "tree", f"sha-{i}") for i in range(6)])
            return tree([entry(f"f.{ref}", "blob", ref)])
        finally:
            with lock:
                state["in_flight"] -= 1

    repo.get_git_tree.side_effect = get_tree

    directory = load_with(repo, tree_fetch_concurrency=2)

    assert len(directory.children) == 6
    assert state["peak"] <= 2


def test_the_walk_result_is_identical_to_the_recursive_tree(repo):
    """Whatever order the directories are read in, the rebuilt tree is the
    same set of paths the recursive tree would have delivered."""
    repo.get_git_tree.side_effect = lambda ref, recursive=False: (
        tree([], truncated=True)
        if recursive
        else {
            "main": tree(
                [
                    entry("a", "tree", "sha-a"),
                    entry("b", "tree", "sha-b"),
                    entry("root.1", "blob", "s1"),
                ]
            ),
            "sha-a": tree([entry("c", "tree", "sha-c"), entry("one.2", "blob", "s2")]),
            "sha-b": tree([entry("two.3", "blob", "s3")]),
            "sha-c": tree([entry("three.4", "blob", "s4")]),
        }[ref]
    )

    directory = load(repo)

    names = {c.name for c in directory.children}
    files = {f.name for f in directory.files}
    (a,) = [c for c in directory.children if c.name == "a"]
    (b,) = [c for c in directory.children if c.name == "b"]
    (c,) = a.children
    assert names == {"a", "b"}
    assert files == {"root"}
    assert [f.name for f in a.files] == ["one"]
    assert [f.name for f in b.files] == ["two"]
    assert [f.name for f in c.files] == ["three"]


def test_the_walk_logs_how_long_it_took_and_how_much_it_read(repo, caplog):
    import logging

    repo.get_git_tree.side_effect = lambda ref, recursive=False: (
        tree([], truncated=True)
        if recursive
        else (
            tree([entry(f"d{i}", "tree", f"sha-{i}") for i in range(3)])
            if ref == "main"
            else tree([entry("f.1", "blob", "s")])
        )
    )

    with caplog.at_level(logging.INFO):
        load(repo)

    rebuilds = [
        r.getMessage()
        for r in caplog.records
        if "directory by directory" in r.getMessage()
    ]
    assert len(rebuilds) == 1
    assert "4 directories" in rebuilds[0]
    assert " in " in rebuilds[0] and "s" in rebuilds[0]


def test_tree_fetch_concurrency_defaults_to_eight_and_validates():
    assert (
        GithubRepoConfig.from_dict(
            {"repo": "o/r", "commit": "main", "access_token": "t"}
        ).tree_fetch_concurrency
        == 8
    )
    assert (
        GithubRepoConfig.from_dict(
            {
                "repo": "o/r",
                "commit": "main",
                "access_token": "t",
                "tree_fetch_concurrency": "3",
            }
        ).tree_fetch_concurrency
        == 3
    )
    with pytest.raises(ValueError, match="tree_fetch_concurrency"):
        GithubRepoConfig.from_dict(
            {
                "repo": "o/r",
                "commit": "main",
                "access_token": "t",
                "tree_fetch_concurrency": 0,
            }
        )
