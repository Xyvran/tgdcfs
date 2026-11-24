"""Backfill: mirror pre-existing data into the mirror stores.

When redundancy is enabled on an existing installation, nothing that is
already in the primary store has a mirror copy. This module walks the metadata
tree (the authoritative work list -- everything TGDCFS serves is reachable
from it), finds file versions without a complete mirror set, and copies
them using the same primitives as the live write path. Only the current
version of each file is mirrored unless
``tgdcfs.transfer.background.mirror_old_versions`` is set.

The job is idempotent and resumable for free: a version is skipped when
its ``mirrors`` map already covers every configured store, and the FD
edit that records the map is the commit point of each unit of work. A
crash between forward and commit merely leaves an orphaned copy in the
mirror store, which is harmless. The outcome of every file is kept in
:mod:`tgdcfs.core.backfill_state`, so a restart resumes the run instead
of reading every descriptor again.
"""

from __future__ import annotations

import asyncio
import logging
import time
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from functools import partial
from typing import TYPE_CHECKING, AsyncIterator, Dict, List, Mapping, Optional, Set

from tgdcfs.config import get_config
from tgdcfs.core.backfill_state import BackfillRun, BackfillState, FileEntry
from tgdcfs.core.local_cache import version_cache_for
from tgdcfs.core.mirror import MirrorGroup, MirrorStore
from tgdcfs.core.model import TGFSDirectory, TGFSFileDesc, TGFSFileRef, TGFSFileVersion
from tgdcfs.core.progress import RunProgress, format_bytes, progress_registry
from tgdcfs.core.repository.interface import FDRepositoryResp, IFDRepository
from tgdcfs.errors import TechnicalError
from tgdcfs.reqres import Replica, SentFileMessage
from tgdcfs.tasks import task_store
from tgdcfs.tasks.models import TaskStatus, TaskType
from tgdcfs.utils.throttle import background_work

if TYPE_CHECKING:
    from tgdcfs.core.client import Client

logger = logging.getLogger(__name__)

# A single store call that takes longer than this is logged while it waits.
HEARTBEAT_SECONDS = 60.0
# The descriptor scan and the mirroring log their progress at most this
# often.
SCAN_PROGRESS_SECONDS = 30.0
MIRROR_PROGRESS_SECONDS = 30.0
# The mirroring pushes the metadata and saves its state at most this
# often; a restart repeats at most this much work.
CHECKPOINT_SECONDS = 60.0


@dataclass
class BackfillReport:
    files_scanned: int = 0
    versions_checked: int = 0
    versions_mirrored: int = 0
    versions_promoted: int = 0
    # Write-back uploads moved from the local cache into the primary.
    versions_distributed: int = 0
    fds_mirrored: int = 0
    # Bytes and messages written into the mirror stores (a version copied
    # into two stores counts twice).
    bytes_mirrored: int = 0
    parts_mirrored: int = 0
    # Bytes the run set out to copy, from the descriptor scan.
    bytes_planned: int = 0
    failures: List[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "files_scanned": self.files_scanned,
            "versions_checked": self.versions_checked,
            "versions_mirrored": self.versions_mirrored,
            "versions_promoted": self.versions_promoted,
            "fds_mirrored": self.fds_mirrored,
            "bytes_mirrored": self.bytes_mirrored,
            "parts_mirrored": self.parts_mirrored,
            "bytes_planned": self.bytes_planned,
            "failures": self.failures,
        }


@asynccontextmanager
async def _heartbeat(label: str) -> AsyncIterator[None]:
    """Log every ``HEARTBEAT_SECONDS`` while the body is still running.

    A store call without a timeout can wait forever; this names the call
    it is waiting in instead of leaving the log silent.
    """
    started = time.monotonic()

    async def beat() -> None:
        while True:
            await asyncio.sleep(HEARTBEAT_SECONDS)
            logger.warning(
                f"Backfill: still waiting on {label} after "
                f"{time.monotonic() - started:.0f}s"
            )

    task = asyncio.create_task(beat())
    try:
        yield
    finally:
        task.cancel()


def mirror_old_versions() -> bool:
    """Whether versions other than a file's current one are mirrored;
    ``tgdcfs.transfer.background.mirror_old_versions``."""
    return get_config().tgdcfs.transfer.background.mirror_old_versions


def versions_to_mirror(fd: TGFSFileDesc) -> List[TGFSFileVersion]:
    """The versions of ``fd`` the backfill brings up to date, newest first.

    By default only the current version: the newest valid one, which is
    the one a read serves. Older versions keep whatever copies they have.
    """
    versions = fd.get_versions(sort=True, exclude_invalid=True)
    return versions if mirror_old_versions() else versions[:1]


def size_mismatch(version: TGFSFileVersion) -> Optional[str]:
    """Why the primary parts of ``version`` do not make up the file the
    descriptor recorded, or ``None`` when they do (or nothing was recorded)."""
    if version.recorded_size < 0 or not version.part_sizes:
        return None
    total = sum(version.part_sizes)
    if total == version.recorded_size:
        return None
    return (
        f"its {len(version.part_sizes)} parts add up to {total} bytes, "
        f"the descriptor records {version.recorded_size}"
    )


def planned_bytes(group: MirrorGroup, fd: TGFSFileDesc) -> int:
    """Bytes the backfill still has to copy for ``fd``: the size of each
    version it mirrors, once per store that has no copy of it."""
    return sum(
        version.size * len(group.missing_stores(version))
        for version in versions_to_mirror(fd)
        if version.size > 0 and size_mismatch(version) is None
    )


def _collect_file_refs(directory: TGFSDirectory) -> List[TGFSFileRef]:
    refs = list(directory.find_files())
    for child in directory.find_dirs():
        refs.extend(_collect_file_refs(child))
    return refs


async def _verify_mirrors(client: "Client", fd: TGFSFileDesc) -> None:
    """Drop mirror entries whose copies no longer exist.

    Mirror messages can be deleted manually just like primary ones; a
    dropped entry makes the version eligible for re-mirroring below.
    """
    if (mirror_group := client.mirror_group) is None:
        return
    for version in fd.get_versions(exclude_invalid=True):
        copies = [(key, ids) for key, ids in version.mirrors.items()] + [
            (key, replica.message_ids) for key, replica in version.replicas.items()
        ]
        for store_key, all_ids in copies:
            if store_key == version.store:
                continue
            if (api := mirror_group.store_for(store_key)) is None:
                continue
            ids = [mid for mid in all_ids if mid > 0]
            if not ids:
                continue
            try:
                messages = await api.get_messages(ids)
            except Exception as ex:
                logger.warning(
                    f"Verification of mirror store {store_key} failed "
                    f"for {fd.name}@{version.id}: {ex}"
                )
                continue
            if any(m is None or m.document is None for m in messages):
                logger.warning(
                    f"Mirror copy of {fd.name}@{version.id} in store "
                    f"{store_key} is incomplete, scheduling re-mirror"
                )
                version.mirrors.pop(store_key, None)
                version.replicas.pop(store_key, None)


async def _distribute_pending(
    client: "Client",
    fr: TGFSFileRef,
    fd: TGFSFileDesc,
    version: TGFSFileVersion,
    report: BackfillReport,
) -> bool:
    """Move a write-back upload from the local cache into the stores.

    The primary and every mirror that gets a replica are filled at the
    same time from the one local file; aligned mirrors follow through the
    usual path once the primary's ids exist. The primary's ids are
    committed as soon as they exist -- that is the moment the durability
    window of ``write_ack: cache`` closes. Returns whether the descriptor
    changed.
    """
    cache = getattr(client, "cache", None)
    local = cache.local(version.id) if cache is not None else None
    if local is None:
        # The window this mode admits: the bytes were only here, and here
        # they are gone. Say so loudly; the file falls back to its
        # previous version.
        message = (
            f"{fr.name}@{version.id}: the pending version is not in the local "
            f"cache any more; its {version.size} bytes are lost and the file "
            f"falls back to its previous version"
        )
        logger.error(message)
        try:
            task_id = await task_store.add_task(
                task_type=TaskType.DISTRIBUTION,
                path=f"/{client.name}{fr.location.absolute_path}/{fr.name}",
                filename=fr.name,
                size_total=version.size,
            )
            await task_store.update_task_progress(
                task_id, status=TaskStatus.FAILED, error_message=message
            )
        except Exception as ex:  # pragma: no cover - reporting must not fail the worker
            logger.warning(f"Could not record the lost version as a task: {ex}")
        fd.delete_version(version.id)
        return True

    primary = client.store
    group = client.mirror_group
    planned = primary.plan_parts(local.size)
    replica_targets = (
        [ch for ch in group.stores if group.needs_replica(ch, planned)]
        if group is not None
        else []
    )
    replica_targets = [ch for ch in replica_targets if not version.has_copy_in(ch.key)]

    async def upload_primary() -> List[SentFileMessage]:
        from tgdcfs.core.local_cache import FileMessageFromCache

        return await primary.upload(FileMessageFromCache.new(local, fr.name))

    async def replicate(ch: MirrorStore) -> tuple[str, Replica]:
        if group is None:  # pragma: no cover - replica_targets is empty then
            raise TechnicalError("no mirror group")
        return ch.key, await group.replicate_local(ch, local, fr.name)

    primary_task = asyncio.create_task(upload_primary())
    replica_tasks = [asyncio.create_task(replicate(ch)) for ch in replica_targets]
    await asyncio.gather(primary_task, *replica_tasks, return_exceptions=True)

    changed = False
    for ch, task in zip(replica_targets, replica_tasks):
        if (error := task.exception()) is not None:
            report.failures.append(
                f"{fr.name}@{version.id}: could not replicate to {ch.key} ({error})"
            )
        else:
            key, replica = task.result()
            version.replicas[key] = replica
            changed = True
    if (error := primary_task.exception()) is not None:
        report.failures.append(
            f"{fr.name}@{version.id}: could not upload to the primary ({error})"
        )
        return changed
    sent = primary_task.result()
    version.materialize(
        primary.key, [m.message_id for m in sent], [m.size for m in sent]
    )
    report.versions_distributed += 1
    return True


@background_work
async def backfill_file(
    client: "Client",
    fr: TGFSFileRef,
    verify: bool,
    report: BackfillReport,
    progress: Optional[RunProgress] = None,
) -> None:
    """Bring one file's copies up to date: the unit of work of both the
    backfill task and the replication queue. ``progress`` follows the
    bytes and parts of each copy."""
    mirror_group = client.mirror_group
    fd_repo = client.fd_repo
    if fd_repo is None:
        return

    fd = await fd_repo.get(fr, include_all_versions=True)  # type: ignore[call-arg]
    if not fd.get_versions():
        # Unreadable or fully invalid descriptor (fd_repo.get returns an
        # empty FD in that case). Never save it back -- that would
        # overwrite the real descriptor message with an empty one.
        return

    # Write-back uploads first: a pending version has no store copy at all
    # yet. Its ids are committed on their own so a mirror failure later
    # cannot keep the primary's copy out of the metadata.
    pending = [v for v in fd.get_versions() if v.pending]
    if pending:
        distributed = False
        for version in pending:
            distributed |= await _distribute_pending(client, fr, fd, version, report)
        if distributed:
            resp = await fd_repo.save(fd, fr)
            _sync_ref(fr, resp)
        if mirror_group is None:
            cache = getattr(client, "cache", None)
            if cache is not None:
                for version in pending:
                    if not version.pending:
                        cache.release(version.id)
            return

    if mirror_group is None:
        return
    if verify:
        await _verify_mirrors(client, fd)

    changed = False
    to_mirror = {version.id for version in versions_to_mirror(fd)}
    for version in fd.get_versions(exclude_invalid=True):
        # A version that still lives in a former primary gets its copy in
        # the current primary first; that copy becomes its primary layout.
        # Every version, not only the mirrored ones: after a promotion the
        # old primary may be gone for good.
        if not version.owned_by(mirror_group.primary.key):
            try:
                if await mirror_group.copy_into_primary(version):
                    report.versions_promoted += 1
                    changed = True
                else:
                    report.failures.append(
                        f"{fr.name}@{version.id}: could not copy into the primary"
                    )
            except Exception as ex:
                report.failures.append(
                    f"{fr.name}@{version.id}: could not copy into the primary ({ex})"
                )

        cache = version_cache_for(
            getattr(client, "cache", None), client.name, version.id, version.size
        )
        if version.id not in to_mirror:
            # An older version is not mirrored, so no mirror waits for its
            # staged bytes any more.
            if cache is not None:
                cache.release()
            continue
        report.versions_checked += 1

        missing = mirror_group.missing_stores(version)
        if not missing:
            if cache is not None:
                cache.release()
            continue
        if not version.owned_by(mirror_group.primary.key):
            # No primary copy to mirror from yet; the next run retries.
            continue
        if (mismatch := size_mismatch(version)) is not None:
            # The source itself is incomplete: a copy of it would be just
            # as short, and recording it would make it look complete.
            message = f"{fr.name}@{version.id}: {mismatch}; not mirrored"
            logger.error(message)
            report.failures.append(message)
            continue
        logger.info(
            f"Mirroring {fr.name}@{version.id} ({format_bytes(version.size)}, "
            f"{len(version.message_ids)} parts) to {', '.join(missing)}"
        )
        copies = await mirror_group.mirror_parts(
            version.message_ids,
            version.part_sizes or None,
            only_stores=missing,
            cache=cache,
            progress=(
                partial(progress.start_copy, fr.name, version.id)
                if progress is not None
                else None
            ),
        )
        if copies.store_keys:
            copies.apply_to(version)
            report.versions_mirrored += 1
            report.bytes_mirrored += version.size * len(copies.store_keys)
            report.parts_mirrored += copies.parts_written
            changed = True
        still_missing = [key for key in missing if key not in copies.store_keys]
        if still_missing:
            reasons = [
                f"{key} ({copies.errors[key]})" if key in copies.errors else key
                for key in still_missing
            ]
            report.failures.append(
                f"{fr.name}@{version.id}: could not mirror to {', '.join(reasons)}"
            )
        elif cache is not None:
            # Every mirror has its copy: the staged bytes are no longer
            # needed for replication.
            cache.release()

    fd_missing = set(mirror_group.store_keys) - {
        key for key, mid in fr.mirrors.items() if mid > 0
    }

    if changed or fd_missing:
        # Commit point: the (possibly updated) mirrors map is persisted
        # in the FD message, and the FD itself gets its mirror copies.
        resp = await fd_repo.save(fd, fr)
        _sync_ref(fr, resp)
        if fd_missing:
            report.fds_mirrored += 1


def _sync_ref(fr: TGFSFileRef, resp: "FDRepositoryResp") -> None:
    if (
        resp.mirrors != fr.mirrors
        or resp.message_id != fr.message_id
        or resp.store != fr.store
    ):
        fr.message_id = resp.message_id
        fr.mirrors = dict(resp.mirrors)
        fr.store = resp.store


@background_work
async def backfill_mirrors(
    client: "Client",
    verify: bool = False,
    task_id: Optional[str] = None,
    state: Optional[BackfillState] = None,
    rescan: bool = False,
) -> BackfillReport:
    """Mirror every unmirrored file version of ``client``'s file system.

    Files are processed newest first so the most recent data is
    protected earliest. A file that fails is tried again with a backoff
    while the run goes on with the others; what still fails after the
    last attempt is reported.

    ``state`` keeps the run across restarts: an unfinished run of the
    file system is resumed (only the descriptors it has not read yet are
    read, the files it finished are skipped) unless ``rescan`` starts
    over. Without a ``state`` the run makes one pass and keeps nothing.
    """
    report = BackfillReport()

    if client.mirror_group is None or client.fd_repo is None:
        report.failures.append(
            f"File system '{client.name}' has no mirror stores configured"
        )
        return report

    if state is None:
        state = BackfillState(None, max_attempts=1)
    run = state.get(client.name)
    if run is not None and not rescan:
        run.verify = verify
        for entry in run.files.values():
            if entry.state == "failed":
                # A restart is a fresh chance for what failed before.
                entry.state = "pending"
                entry.attempts = 0
                entry.not_before = 0.0
        logger.info(
            f"Backfill for '{client.name}': resuming the run started "
            f"{time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(run.started_at))}, "
            f"{run.count('done')}/{len(run.files)} files done"
        )
    else:
        run = state.begin(client.name, verify)

    progress = progress_registry.register(
        RunProgress(kind="backfill", filesystem=client.name, phase="scanning")
    )
    try:
        return await _backfill(
            client,
            client.mirror_group,
            client.fd_repo,
            state,
            run,
            task_id,
            report,
            progress,
        )
    finally:
        progress_registry.unregister(progress)


async def _scan(
    client: "Client",
    group: MirrorGroup,
    fd_repo: IFDRepository,
    state: BackfillState,
    run: BackfillRun,
    report: BackfillReport,
    progress: RunProgress,
) -> Dict[str, TGFSFileRef]:
    """Read the descriptors the run has no entry for yet; returns every
    file of the file system by path."""
    from tgdcfs.core.replication import file_path

    paths = {file_path(fr): fr for fr in _collect_file_refs(client.dir_api.root)}
    to_read = [(path, fr) for path, fr in paths.items() if path not in run.files]
    total = len(to_read)
    known = f" ({len(run.files)} already read)" if run.files else ""
    logger.info(f"Backfill for '{client.name}': reading {total} descriptors{known}")

    # Newest first: load descriptors to know each file's timestamp, and
    # how many bytes each one still has to copy.
    run.unreadable = []
    scan_started = last_progress = time.monotonic()
    progress.files_total = total
    for index, (path, fr) in enumerate(to_read, 1):
        progress.files_done = index - 1
        try:
            async with _heartbeat(f"the descriptor of {fr.name} ({index}/{total})"):
                fd = await fd_repo.get(fr)
            run.files[path] = FileEntry(
                path=path,
                updated_at=fd.updated_at_timestamp,
                bytes=planned_bytes(group, fd),
            )
        except Exception as ex:
            run.unreadable.append(path)
            report.failures.append(f"{fr.name}: cannot read descriptor ({ex})")
            logger.warning(f"Backfill: cannot read the descriptor of {fr.name}: {ex}")
        now = time.monotonic()
        if now - last_progress >= SCAN_PROGRESS_SECONDS or index == total:
            last_progress = now
            # What has been read so far survives a restart.
            state.save()
            logger.info(
                f"Backfill for '{client.name}': read {index}/{total} descriptors "
                f"in {now - scan_started:.0f}s, {len(run.unreadable)} unreadable"
            )
    run.scanned = True
    state.save()
    return paths


async def _backfill(
    client: "Client",
    group: MirrorGroup,
    fd_repo: IFDRepository,
    state: BackfillState,
    run: BackfillRun,
    task_id: Optional[str],
    report: BackfillReport,
    progress: RunProgress,
) -> BackfillReport:
    paths = await _scan(client, group, fd_repo, state, run, report, progress)

    done = [entry for entry in run.files.values() if entry.state == "done"]
    report.bytes_planned = sum(entry.bytes for entry in run.files.values())
    progress.files_total = len(run.files)
    progress.files_done = 0
    progress.count_done(len(done), sum(entry.bytes for entry in done))
    progress.bytes_total = report.bytes_planned
    progress.phase = "mirroring"

    if task_id:
        await task_store.update_task_progress(
            task_id, status=TaskStatus.IN_PROGRESS, size_delta=len(done)
        )
    pending = [entry for entry in run.ordered() if entry.state == "pending"]
    logger.info(
        f"Backfill for '{client.name}': mirroring {len(pending)} files, "
        f"{format_bytes(progress.bytes_total - progress.bytes_done)} to copy"
    )

    # Files finished since the last metadata push: they are saved as done
    # only once their copies are recorded in the pushed metadata.
    unpushed: List[FileEntry] = []
    metadata_dirty = False
    last_checkpoint = time.monotonic()

    async def checkpoint() -> None:
        nonlocal metadata_dirty, last_checkpoint
        if metadata_dirty and client.metadata_api:
            await client.metadata_api.push()
        metadata_dirty = False
        unpushed.clear()
        state.save()
        last_checkpoint = time.monotonic()

    async def process(entry: FileEntry, label: str) -> None:
        nonlocal metadata_dirty
        retry = entry.state == "failed"
        if (fr := paths.get(entry.path)) is None:
            # Removed or renamed since the scan: nothing left to copy here.
            state.done(entry)
            progress.start_file(entry.bytes, retry=retry)
            progress.finish_file()
            if task_id and not retry:
                await task_store.update_task_progress(task_id, size_delta=1)
            return
        before = (fr.message_id, dict(fr.mirrors), fr.store)
        mirrored = report.versions_mirrored
        copied = report.bytes_mirrored
        parts = report.parts_mirrored
        failed = len(report.failures)
        file_started = time.monotonic()
        progress.start_file(entry.bytes, retry=retry)
        try:
            async with _heartbeat(f"{fr.name} ({label})"):
                await backfill_file(client, fr, run.verify, report, progress)
        except Exception as ex:
            report.failures.append(f"{fr.name}: {ex}")
            logger.error(f"Backfill failed for {fr.name}: {ex}")
        if before != (fr.message_id, fr.mirrors, fr.store):
            metadata_dirty = True
        # The failures of a file belong to its entry until the run ends:
        # a later attempt may still succeed.
        failures = report.failures[failed:]
        del report.failures[failed:]
        if failures:
            state.failed(entry, failures)
        else:
            state.done(entry)
            unpushed.append(entry)
        progress.finish_file(failed=bool(failures))
        if not retry:
            report.files_scanned += 1
        logger.info(
            f"Backfill [{label}] {fr.name}: "
            f"{report.versions_mirrored - mirrored} versions mirrored "
            f"({format_bytes(report.bytes_mirrored - copied)} in "
            f"{report.parts_mirrored - parts} parts), "
            f"{len(failures)} failures, "
            f"{time.monotonic() - file_started:.1f}s; run at "
            f"{format_bytes(progress.bytes_done)}/"
            f"{format_bytes(progress.bytes_total)}"
        )
        if task_id and not retry:
            await task_store.update_task_progress(task_id, size_delta=1)
        if time.monotonic() - last_checkpoint >= CHECKPOINT_SECONDS:
            await checkpoint()

    async def log_progress() -> None:
        while True:
            await asyncio.sleep(MIRROR_PROGRESS_SECONDS)
            logger.info(f"Backfill for '{client.name}': {progress.describe()}")

    progress_log = asyncio.create_task(log_progress())
    finished = False
    try:
        for index, entry in enumerate(pending, len(done) + 1):
            await process(entry, f"{index}/{len(run.files)}")

        # Failed files get their next attempts once the others are done,
        # each after its backoff.
        while retryable := [e for e in run.ordered() if state.retryable(e)]:
            wait = min(entry.not_before for entry in retryable) - time.time()
            if wait > 0:
                progress.phase = "retrying"
                await checkpoint()
                logger.info(
                    f"Backfill for '{client.name}': {len(retryable)} files "
                    f"failed, next attempt in {wait:.0f}s"
                )
                await asyncio.sleep(wait)
                progress.phase = "mirroring"
            now = time.time()
            for entry in retryable:
                if entry.not_before <= now:
                    await process(
                        entry,
                        f"attempt {entry.attempts + 1}/{state.max_attempts}",
                    )

        progress.phase = "finishing"
        await checkpoint()
        finished = True
    finally:
        progress_log.cancel()
        if not finished:
            # Interrupted before their copies were pushed: done again on
            # the next run, which finds the copies that did make it.
            for entry in unpushed:
                entry.state = "pending"
            state.save()

    for entry in run.ordered():
        if entry.state == "failed":
            report.failures.extend(entry.failures)
    state.finish(client.name)

    if task_id:
        await task_store.update_task_progress(
            task_id,
            status=(TaskStatus.COMPLETED if not report.failures else TaskStatus.FAILED),
            error_message="; ".join(report.failures) or None,
        )

    logger.info(
        f"Backfill for '{client.name}' finished: "
        f"{report.versions_mirrored}/{report.versions_checked} versions "
        f"mirrored ({format_bytes(report.bytes_mirrored)} in "
        f"{report.parts_mirrored} parts), {len(report.failures)} failures"
    )
    return report


async def create_backfill_task(client: "Client", total_files: int) -> str:
    return await task_store.add_task(
        task_type=TaskType.MIRROR_BACKFILL,
        path=f"/{client.name}",
        filename=f"mirror-backfill-{client.name}",
        size_total=total_files,
    )


def count_files(client: "Client") -> int:
    return len(_collect_file_refs(client.dir_api.root))


class BackfillRunning(Exception):
    """A backfill of the file system is already going on."""


# File systems with a backfill going on, and the tasks that run them.
_running: Set[str] = set()
_tasks: Set["asyncio.Task[None]"] = set()


def backfill_running(name: str) -> bool:
    return name in _running


async def start_backfill(
    client: "Client",
    state: Optional[BackfillState] = None,
    verify: bool = False,
    rescan: bool = False,
) -> str:
    """Run :func:`backfill_mirrors` in the background; returns its task id.

    Raises :class:`BackfillRunning` when ``client`` already has a run
    going on: two runs would race on the same files and state.
    """
    if client.name in _running:
        raise BackfillRunning(f"A backfill of '{client.name}' is already running")
    _running.add(client.name)
    try:
        task_id = await create_backfill_task(client, count_files(client))
    except BaseException:
        _running.discard(client.name)
        raise

    async def run() -> None:
        try:
            await backfill_mirrors(
                client, verify=verify, task_id=task_id, state=state, rescan=rescan
            )
        except Exception:
            logger.exception(f"Mirror backfill for '{client.name}' crashed")
        finally:
            _running.discard(client.name)

    task = asyncio.get_running_loop().create_task(run())
    _tasks.add(task)
    task.add_done_callback(_tasks.discard)
    return task_id


async def resume_backfills(
    clients: Mapping[str, "Client"], state: BackfillState
) -> List[str]:
    """Resume the runs a restart interrupted; returns their task ids."""
    task_ids = []
    for run in state.runs():
        client = clients.get(run.filesystem)
        if client is None or client.mirror_group is None:
            # Kept: the file system may come back with its mirrors.
            logger.warning(
                f"Not resuming the backfill of '{run.filesystem}': "
                "no such file system with mirror stores"
            )
            continue
        logger.info(f"Resuming the interrupted backfill of '{run.filesystem}'")
        task_ids.append(await start_backfill(client, state, verify=run.verify))
    return task_ids
