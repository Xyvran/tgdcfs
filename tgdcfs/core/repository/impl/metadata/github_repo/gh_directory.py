import asyncio
import datetime
import logging
import threading
from dataclasses import dataclass, field
from typing import Any, Callable, Optional, TypeVar

from github import Github, InputGitTreeElement
from github.Repository import Repository

from tgdcfs.core.model import TGFSDirectory, TGFSFileRef
from tgdcfs.crypto.path_names import encrypt_path_name
from tgdcfs.errors import FileOrDirectoryAlreadyExists
from tgdcfs.utils.time import FIRST_DAY_OF_EPOCH

from .dir_timestamps import DirTimestampStore
from .stats_file import STATS_FILE_NAME

logger = logging.getLogger(__name__)

T = TypeVar("T")


@dataclass
class GithubConfig:
    gh: Github
    repo_name: str
    repo: Repository
    commit: str
    # Deterministic AES-SIV key for path-name encryption. ``None`` keeps the
    # legacy behaviour of storing plaintext directory/file names in the repo.
    name_key: Optional[bytes] = None
    # Where the directory dates come from. ``None`` leaves every loaded
    # directory on the ``now()`` default, as it was before the cache existed.
    timestamps: Optional[DirTimestampStore] = None
    # Every commit this process makes on the branch goes through this lock.
    # Two commits made at once race for the branch head and the loser is
    # refused (409 from the Contents API, 422 "not a fast forward" from a
    # ref update), which kept the stats commit failing for 40 minutes
    # while a bulk upload committed a file ref every few seconds.
    write_lock: threading.Lock = field(
        default_factory=threading.Lock, compare=False, repr=False
    )
    # The same lock for the event loop: async callers queue here, so they
    # do not each hold a worker thread while they wait for their turn.
    _async_locks: dict[int, asyncio.Lock] = field(
        default_factory=dict, compare=False, repr=False
    )

    def write_sync(self, fn: Callable[..., T], *args: Any) -> T:
        """Run ``fn`` (a commit on the branch) under the write lock, on
        the calling thread."""
        with self.write_lock:
            return fn(*args)

    async def write(self, fn: Callable[..., T], *args: Any) -> T:
        """Run ``fn`` (a commit on the branch) under the write lock, off
        the event loop.

        PyGithub is synchronous: a commit takes a few seconds and,
        called on the loop, froze every other request for that long,
        so clients waiting on an upload or a listing timed out.
        """
        async with self._async_lock():
            return await asyncio.to_thread(self.write_sync, fn, *args)

    def _async_lock(self) -> asyncio.Lock:
        # One per event loop: an ``asyncio.Lock`` is bound to the loop it
        # was first used on.
        loop = asyncio.get_running_loop()
        lock = self._async_locks.get(id(loop))
        if lock is None:
            lock = self._async_locks[id(loop)] = asyncio.Lock()
        return lock


class GithubDirectory(TGFSDirectory):
    def __init__(
        self,
        ghc: GithubConfig,
        name: str,
        parent: Optional[TGFSDirectory],
        children: Optional[list[TGFSDirectory]] = None,
        files: Optional[list[TGFSFileRef]] = None,
        stored_encrypted: Optional[bool] = None,
        defer_timestamps: bool = False,
    ):
        # These have to exist before the dataclass initialiser runs: it assigns
        # created_at/modified_at, which goes through the setters below.
        self._created_at = self._modified_at = FIRST_DAY_OF_EPOCH
        self._created_pending = defer_timestamps
        self._modified_pending = defer_timestamps
        self._ts_initialised = False

        super().__init__(name, parent, children or [], files or [])
        self._ghc = ghc
        # Whether THIS directory is stored under an encrypted name in the
        # repo. New directories follow the configured key; directories loaded
        # from the repo carry whatever form they were found in, so encrypted
        # and legacy-plaintext folders can be read and written side by side.
        self._stored_encrypted = (
            stored_encrypted
            if stored_encrypted is not None
            else ghc.name_key is not None
        )
        self._ts_initialised = True
        # Blob sha of this folder's stats file in the repo, ``None`` when
        # there is none; whether its content has been put on the file refs.
        self.stats_sha: Optional[str] = None
        self.stats_loaded = False

    # --- dates ---------------------------------------------------------------
    #
    # A directory loaded from the repo does not know its dates yet: reading them
    # costs two GitHub calls, and doing that for every directory while the tree
    # is built is what made the boot grow with the folder count. So the load
    # marks them pending and they are filled in from the cache on first read --
    # never by blocking the reader, because a PROPFIND of a folder with a
    # hundred children would otherwise pay for a hundred round trips at once.

    @property
    def created_at(self) -> datetime.datetime:
        self._resolve_timestamps()
        return self._created_at

    @created_at.setter
    def created_at(self, value: datetime.datetime) -> None:
        self._created_at = value
        if self._ts_initialised:
            # An explicit write knows better than the git history.
            self._created_pending = False

    @property
    def modified_at(self) -> datetime.datetime:
        self._resolve_timestamps()
        return self._modified_at

    @modified_at.setter
    def modified_at(self, value: datetime.datetime) -> None:
        self._modified_at = value
        if self._ts_initialised:
            self._modified_pending = False

    def _resolve_timestamps(self) -> None:
        if not (self._created_pending or self._modified_pending):
            return

        store = getattr(getattr(self, "_ghc", None), "timestamps", None)
        if store is None:
            return

        path = self._github_path
        if not path:
            # The root is dated from the repo's own metadata, which is free.
            self._created_pending = self._modified_pending = False
            return

        found = store.get(path)
        if found is None:
            # Queued for the background workers; report the default until then.
            return

        created, modified = found
        if self._created_pending:
            self._created_at = created
            self._created_pending = False
        if self._modified_pending:
            self._modified_at = modified
            self._modified_pending = False

    def is_attached(self) -> bool:
        """Whether this folder is still part of the tree (not deleted)."""
        node: TGFSDirectory = self
        while node.parent is not None:
            if not any(child is node for child in node.parent.children):
                return False
            node = node.parent
        return True

    @property
    def stats_path(self) -> str:
        """Repo path of this folder's stats file."""
        return self.join_path(self._github_path, STATS_FILE_NAME)

    @staticmethod
    def join_path(*args: str) -> str:
        """Join paths in a way that is compatible with GitHub"""
        return "/".join(part.strip("/") for part in args if part)

    @property
    def _storage_name(self) -> str:
        """The on-repo name of this directory (encrypted when applicable)."""
        key = self._ghc.name_key
        if key is not None and self._stored_encrypted:
            return encrypt_path_name(key, self.name)
        return self.name

    def _file_ref_storage_name(self, name: str, message_id: int) -> str:
        """On-repo filename ``<name>.<id>``, with ``<name>`` encrypted if keyed."""
        key = self._ghc.name_key
        if key is not None:
            return f"{encrypt_path_name(key, name)}.{message_id}"
        return f"{name}.{message_id}"

    def _message_name(self, name: str) -> str:
        """``name`` as it may appear in a commit message: encrypted when
        keyed, like the path it belongs to."""
        key = self._ghc.name_key
        return encrypt_path_name(key, name) if key is not None else name

    @property
    def _github_path(self) -> str:
        """Get the GitHub repository path for this directory"""
        if self.parent is None:
            return ""

        if isinstance(self.parent, GithubDirectory):
            parent_path = self.parent._github_path
        else:
            parent_path = ""

        return self.join_path(parent_path, self._storage_name)

    def create_dir_skip_github_ops(self, name: str) -> "GithubDirectory":
        res = GithubDirectory(self._ghc, name, self)
        self.children.append(res)
        return res

    def _attach_new_dir(self, name: str) -> tuple["GithubDirectory", str, str]:
        """Add the child ``name`` to the in-memory tree; returns it with the
        path of its placeholder file and the commit message.

        The child is a ``GithubDirectory`` from the start: a plain one,
        swapped later, would take the writes made into it while its own
        commit is still running past GitHub.
        """
        if self.find_dirs([name]):
            raise FileOrDirectoryAlreadyExists(name)
        child = GithubDirectory(self._ghc, name, self)
        self.children.append(child)
        self._touch_modified()
        # The new directory follows the configured key, so its on-repo
        # segment is the encrypted name when path-name encryption is enabled.
        child_segment = child._storage_name
        dir_path = self.join_path(self._github_path, child_segment, ".gitkeep")
        # The on-repo segment, never the plaintext name: commit messages are
        # as public as the paths.
        return child, dir_path, f"Create directory {child_segment}"

    def _create_placeholder(self, path: str, message: str) -> None:
        """Commit an empty file at ``path``. Runs on a worker thread."""
        self._ghc.repo.create_file(
            path=path,
            message=message,
            content="",
            branch=self._ghc.commit,
        )

    def create_dir(self, name: str) -> "GithubDirectory":
        child, dir_path, message = self._attach_new_dir(name)
        try:
            self._ghc.write_sync(self._create_placeholder, dir_path, message)
        except Exception as ex:
            self._dir_not_created(child, ex)
            raise
        logger.info(f"Created directory {name} in GitHub repository at {dir_path}")
        return child

    async def create_dir_async(self, name: str) -> "GithubDirectory":
        child, dir_path, message = self._attach_new_dir(name)
        try:
            await self._ghc.write(self._create_placeholder, dir_path, message)
        except Exception as ex:
            self._dir_not_created(child, ex)
            raise
        logger.info(f"Created directory {name} in GitHub repository at {dir_path}")
        return child

    def _dir_not_created(self, child: "GithubDirectory", ex: Exception) -> None:
        logger.error(f"Failed to create directory {child.name} in GitHub: {ex}")
        if any(c is child for c in self.children):
            self.children.remove(child)

    def delete(self) -> None:
        if self.parent:
            # Remove all files and subdirectories from GitHub
            self._ghc.write_sync(self._delete_github_directory, self._github_path)
        super().delete()

    async def delete_async(self) -> None:
        if self.parent:
            await self._ghc.write(self._delete_github_directory, self._github_path)
        super().delete()

    def _move_in_tree(
        self, new_parent: TGFSDirectory, new_name: Optional[str]
    ) -> tuple[Optional[TGFSDirectory], str, str, str]:
        """Move this directory in the in-memory tree; returns what is
        needed to commit the move and to undo it."""
        old_parent, old_name = self.parent, self.name
        old_prefix = self._github_path
        super().move_to(new_parent, new_name)
        return old_parent, old_name, old_prefix, self._github_path

    def _undo_move(self, old_parent: Optional[TGFSDirectory], old_name: str) -> None:
        # The repo *is* the metadata here, so an in-memory tree that the
        # repo does not back would only survive until the next reload.
        # Put the directory back where it was instead.
        if old_parent is not None and self.parent is not None:
            self.parent.children.remove(self)
            self.name = old_name
            self.parent = old_parent
            old_parent.children.append(self)

    def move_to(
        self, new_parent: TGFSDirectory, new_name: Optional[str] = None
    ) -> None:
        old_parent, old_name, old_prefix, new_prefix = self._move_in_tree(
            new_parent, new_name
        )
        if not old_prefix or not new_prefix or old_prefix == new_prefix:
            return
        try:
            self._ghc.write_sync(self._move_github_directory, old_prefix, new_prefix)
        except Exception:
            self._undo_move(old_parent, old_name)
            raise

    async def move_to_async(
        self, new_parent: TGFSDirectory, new_name: Optional[str] = None
    ) -> None:
        old_parent, old_name, old_prefix, new_prefix = self._move_in_tree(
            new_parent, new_name
        )
        if not old_prefix or not new_prefix or old_prefix == new_prefix:
            return
        try:
            await self._ghc.write(self._move_github_directory, old_prefix, new_prefix)
        except Exception:
            self._undo_move(old_parent, old_name)
            raise

    def _move_github_directory(self, old_prefix: str, new_prefix: str) -> None:
        """Re-path this directory's subtree in a single commit. Runs on a
        worker thread.

        The Contents API has no rename, and deleting plus re-creating every
        blob would lose the git history the timestamps are restored from, so
        rewrite the tree with the affected blobs moved to their new paths --
        the same Git Data API round-trip ``_delete_github_directory`` uses.
        """
        try:
            ref = self._ghc.repo.get_git_ref(f"heads/{self._ghc.commit}")
            base_commit = self._ghc.repo.get_git_commit(ref.object.sha)
            tree = self._ghc.repo.get_git_tree(base_commit.tree.sha, recursive=True)

            elements: list[InputGitTreeElement] = []
            moved = 0
            for entry in tree.tree:
                if entry.type != "blob":
                    continue
                path = entry.path
                if path == old_prefix or path.startswith(old_prefix + "/"):
                    path = new_prefix + path[len(old_prefix) :]
                    moved += 1
                elements.append(
                    InputGitTreeElement(
                        path=path,
                        mode=entry.mode,
                        type="blob",
                        sha=entry.sha,
                    )
                )

            if moved == 0:
                return

            new_tree = self._ghc.repo.create_git_tree(elements)
            new_commit = self._ghc.repo.create_git_commit(
                f"Move directory {old_prefix} to {new_prefix}", new_tree, [base_commit]
            )
            ref.edit(new_commit.sha)
            logger.info(
                f"Moved {moved} object(s) from {old_prefix} to {new_prefix} "
                f"in {self._ghc.repo_name}"
            )
        except Exception as ex:
            logger.error(
                f"Failed to move directory {old_prefix} to {new_prefix} "
                f"in {self._ghc.repo_name}: {ex}"
            )
            raise

    def _file_ref_path(self, name: str, file_message_id: int) -> str:
        # Filename encrypted when keyed.
        return self.join_path(
            self._github_path, self._file_ref_storage_name(name, file_message_id)
        )

    def create_file_ref(self, name: str, file_message_id: int) -> TGFSFileRef:
        file_ref = super().create_file_ref(name, file_message_id)
        file_path = self._file_ref_path(name, file_message_id)
        message = f"Create file reference for {self._message_name(name)}"
        try:
            self._ghc.write_sync(self._create_placeholder, file_path, message)
        except Exception as ex:
            self._file_ref_not_created(file_ref, ex)
            raise
        logger.info(
            f"Created file reference {name} in {self._ghc.repo_name} at {file_path}"
        )
        return file_ref

    async def create_file_ref_async(
        self, name: str, file_message_id: int
    ) -> TGFSFileRef:
        # The ref is in the tree before the commit runs, so a second upload
        # of the same name meanwhile finds it instead of adding a twin.
        file_ref = super().create_file_ref(name, file_message_id)
        file_path = self._file_ref_path(name, file_message_id)
        message = f"Create file reference for {self._message_name(name)}"
        try:
            await self._ghc.write(self._create_placeholder, file_path, message)
        except Exception as ex:
            self._file_ref_not_created(file_ref, ex)
            raise
        logger.info(
            f"Created file reference {name} in {self._ghc.repo_name} at {file_path}"
        )
        return file_ref

    def _file_ref_not_created(self, file_ref: TGFSFileRef, ex: Exception) -> None:
        logger.error(
            f"Failed to create file reference {file_ref.name} "
            f"in {self._ghc.repo_name}: {ex}"
        )
        if any(fr is file_ref for fr in self.files):
            self.files.remove(file_ref)

    def _file_ref_candidates(self, fr: TGFSFileRef) -> list[str]:
        """Repo paths ``fr`` may be stored under, most likely first.

        The ref may be stored under its encrypted or its legacy-plaintext
        name (mixed repos), so the encrypted form comes first, then the
        plaintext one.
        """
        candidates = []
        if self._ghc.name_key is not None:
            candidates.append(self._file_ref_storage_name(fr.name, fr.message_id))
        candidates.append(f"{fr.name}.{fr.message_id}")
        return [
            self.join_path(self._github_path, segment)
            for segment in dict.fromkeys(candidates)  # de-dup, keep order
        ]

    def _delete_stored_file_ref(self, name: str, paths: list[str]) -> None:
        """Delete the first of ``paths`` that exists. Runs on a worker
        thread."""
        for file_path in paths:
            try:
                file_content = self._ghc.repo.get_contents(
                    file_path, ref=self._ghc.commit
                )
                if isinstance(file_content, list):
                    file_content = file_content[0]
                # The name as this path stores it: encrypted, or the legacy
                # plaintext the path already shows.
                stored_name = file_path.rpartition("/")[2].rsplit(".", 1)[0]
                self._ghc.repo.delete_file(
                    path=file_path,
                    message=f"Delete file reference for {stored_name}",
                    sha=file_content.sha,
                    branch=self._ghc.commit,
                )
                logger.info(f"Deleted file reference {name} from {self._ghc.repo_name}")
                return
            except Exception as ex:
                logger.debug(
                    f"Could not delete {file_path} from {self._ghc.repo_name}: {ex}"
                )
        logger.error(
            f"Failed to delete file reference {name} "
            f"from {self._ghc.repo_name}: not found"
        )

    def delete_file_ref(self, fr: TGFSFileRef) -> None:
        self._ghc.write_sync(
            self._delete_stored_file_ref, fr.name, self._file_ref_candidates(fr)
        )
        super().delete_file_ref(fr)

    async def delete_file_ref_async(self, fr: TGFSFileRef) -> None:
        await self._ghc.write(
            self._delete_stored_file_ref, fr.name, self._file_ref_candidates(fr)
        )
        # A second delete of the same file may have finished while this
        # one waited for GitHub.
        if any(ref is fr for ref in self.files):
            super().delete_file_ref(fr)

    def _delete_github_directory(self, prefix: str) -> None:
        """Remove the directory at ``prefix`` and everything beneath it in
        one commit. Runs on a worker thread.

        ``delete_file`` can only remove a single file — it returns HTTP 422
        on a subtree — so a per-entry delete cannot recurse into
        subdirectories (their nested ``.gitkeep`` and files would survive).
        Instead we rewrite the git tree without any blob under this
        directory's path, dropping arbitrarily-nested content atomically in
        a single commit via the Git Data API.
        """
        if not prefix:
            return  # guard: never wipe the whole repo via the root
        try:
            ref = self._ghc.repo.get_git_ref(f"heads/{self._ghc.commit}")
            base_commit = self._ghc.repo.get_git_commit(ref.object.sha)
            tree = self._ghc.repo.get_git_tree(base_commit.tree.sha, recursive=True)

            kept: list[InputGitTreeElement] = []
            removed = 0
            for entry in tree.tree:
                if entry.type != "blob":
                    continue
                if entry.path == prefix or entry.path.startswith(prefix + "/"):
                    removed += 1
                    continue
                kept.append(
                    InputGitTreeElement(
                        path=entry.path,
                        mode=entry.mode,
                        type="blob",
                        sha=entry.sha,
                    )
                )

            if removed == 0:
                return

            new_tree = self._ghc.repo.create_git_tree(kept)
            new_commit = self._ghc.repo.create_git_commit(
                f"Delete directory {prefix}", new_tree, [base_commit]
            )
            ref.edit(new_commit.sha)
            logger.info(
                f"Deleted {removed} object(s) under {prefix} "
                f"from {self._ghc.repo_name}"
            )
        except Exception as ex:
            logger.error(
                f"Failed to delete directory {prefix} "
                f"from {self._ghc.repo_name}: {ex}"
            )
