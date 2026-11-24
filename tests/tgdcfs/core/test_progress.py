"""Progress of the background copies in files, bytes and parts.

The backfill, the replication worker and ``GET /replication/progress``
read the same :class:`RunProgress`; the copies feed it as their bytes
pass through the target.
"""

import asyncio
import logging

from tests.fakes.store import FakeStore
from tests.tgdcfs.core.test_mirror import make_group
from tests.tgdcfs.core.test_mirror_sizes import stores
from tests.tgdcfs.core.test_replication import DATA, Stack, discord_like, telegram_like
from tgdcfs.core import backfill as backfill_module
from tgdcfs.core.backfill import backfill_mirrors
from tgdcfs.core.mirror import MirrorGroup, MirrorStore
from tgdcfs.core.progress import (
    CopyProgress,
    RunProgress,
    format_bytes,
    progress_registry,
)
from tgdcfs.core.replication import ReplicationQueue, ReplicationWorker


def client_of(stack: Stack):
    return type(
        "C",
        (),
        {
            "name": "fs",
            "mirror_group": stack.mirror_group,
            "fd_repo": stack.fd_repo,
            "metadata_api": stack.metadata_api,
            "dir_api": stack.dir_api,
        },
    )()


class TestFormat:
    def test_format_bytes(self):
        assert format_bytes(0) == "0 B"
        assert format_bytes(1023) == "1023 B"
        assert format_bytes(5120) == "5.0 KiB"
        assert format_bytes(3 * 1024**3) == "3.0 GiB"
        assert format_bytes(5 * 1024**4) == "5.0 TiB"


class TestCopyProgress:
    def test_part_follows_the_part_boundaries(self):
        copy = CopyProgress("a.bin", "v", "dc:9", [100, 50, 10])
        assert (copy.part, copy.parts, copy.bytes_total) == (1, 3, 160)
        copy.advance(99)
        assert copy.part == 1
        copy.advance(1)
        assert copy.part == 2
        copy.advance(60)
        assert copy.part == 3 and copy.bytes_done == 160
        assert "part 3/3, 160 B/160 B (100.0%)" in copy.describe()

    def test_complete_fills_the_rest(self):
        copy = CopyProgress("a.bin", "v", "tg:2", [10, 10])
        copy.advance(4)
        copy.complete()
        assert copy.bytes_done == 20


class TestRunProgress:
    def test_bytes_add_up_over_files(self):
        run = RunProgress(kind="backfill", filesystem="fs", bytes_total=300)
        run.start_file(200)
        copy = run.start_copy("a.bin", "v", "dc:9", [100, 100])
        copy.advance(150)
        assert run.bytes_done == 150
        assert run.to_dict()["current"]["part"] == 2
        run.finish_file()
        # A file counts with its planned bytes once done, copied or not.
        assert (run.bytes_done, run.files_done) == (200, 1)
        assert run.current is None

    def test_a_failed_file_stays_ahead_until_its_retry(self):
        run = RunProgress(kind="backfill", filesystem="fs", bytes_total=300)
        run.count_done(1, 200)
        run.start_file(100)
        run.start_copy("b.bin", "v", "dc:9", [100]).advance(40)
        run.finish_file(failed=True)
        assert (run.bytes_done, run.files_done, run.failures) == (200, 1, 1)
        run.start_file(100, retry=True)
        assert run.failures == 0
        run.finish_file()
        assert (run.bytes_done, run.files_done, run.failures) == (300, 2, 0)

    def test_unplanned_bytes_still_count(self):
        run = RunProgress(kind="replication", filesystem="fs")
        run.start_file()
        run.start_copy("a.bin", "v", "dc:9", [64]).advance(64)
        run.finish_file()
        assert run.bytes_done == 64
        assert "1/0 files, 64 B, 0 failed" == run.describe()

    def test_registry_snapshot(self):
        run = progress_registry.register(RunProgress(kind="backfill", filesystem="x"))
        try:
            assert progress_registry.snapshot()["backfill"]["x"]["phase"] == "starting"
        finally:
            progress_registry.unregister(run)
        assert "x" not in progress_registry.snapshot()["backfill"]


class TestMirrorPartsProgress:
    async def test_a_replica_counts_its_bytes_and_parts(self):
        group, _, _, ids = stores()
        copies: list[CopyProgress] = []

        def start(store, part_sizes):
            copies.append(CopyProgress("a.bin", "v", store, part_sizes))
            return copies[-1]

        res = await group.mirror_parts(ids, [100, 100], progress=start)

        [copy] = copies
        assert copy.store == "dc:9"
        assert (copy.bytes_done, copy.part, copy.parts) == (200, 2, 2)
        # Seven 30-byte messages carry the 200 bytes.
        assert res.parts_written == 7

    async def test_a_server_side_copy_completes_at_once(self, mocker):
        group, _, mirror_api = make_group(mocker)
        mirror_api.copy_from.return_value = [11, 12]
        copies: list[CopyProgress] = []

        def start(store, part_sizes):
            copies.append(CopyProgress("a.bin", "v", store, part_sizes))
            return copies[-1]

        res = await group.mirror_parts([1, 2], [5, 5], progress=start)

        assert copies[0].bytes_done == 10
        assert res.parts_written == 2

    async def test_a_reupload_counts_each_part(self):
        primary = FakeStore(key="tg:1", backend_name="telegram")
        mirror = FakeStore(key="tg:2", backend_name="telegram")
        mids = [primary._add(document=b"a" * 30), primary._add(document=b"b" * 20)]
        group = MirrorGroup(
            primary=primary,
            stores=[MirrorStore(key=mirror.key, store=mirror)],
            mode="reupload",
        )
        copies: list[CopyProgress] = []

        def start(store, part_sizes):
            copies.append(CopyProgress("a.bin", "v", store, part_sizes))
            return copies[-1]

        await group.mirror_parts(mids, [30, 20], progress=start)

        assert copies[0].bytes_done == 50


class TestBackfillProgress:
    async def test_backfill_reports_bytes_and_parts(self, caplog, monkeypatch):
        stack = await Stack(telegram_like(), discord_like(), inline=False).init()
        await stack.put("a.bin", DATA)
        seen: list[dict] = []
        original = stack.mirror.upload

        async def upload(file_msg):
            seen.append(progress_registry.snapshot()["backfill"]["fs"])
            return await original(file_msg)

        stack.mirror.upload = upload  # type: ignore[method-assign]

        with caplog.at_level(logging.INFO, logger="tgdcfs.core.backfill"):
            report = await backfill_mirrors(client_of(stack))  # type: ignore[arg-type]

        assert report.failures == []
        assert report.bytes_planned == len(DATA)
        assert report.bytes_mirrored == len(DATA)
        assert report.parts_mirrored == 80
        assert report.to_dict()["parts_mirrored"] == 80
        [during] = seen
        assert during["phase"] == "mirroring"
        assert during["bytes_total"] == len(DATA)
        assert during["current"]["store"] == "dc:9"
        assert during["current"]["parts"] == 3
        assert "fs" not in progress_registry.snapshot()["backfill"]
        assert "mirroring 1 files, 5.0 KiB to copy" in caplog.text
        assert "(5.0 KiB in 80 parts)" in caplog.text
        assert "run at 5.0 KiB/5.0 KiB" in caplog.text

    async def test_the_progress_is_logged_periodically(self, caplog, monkeypatch):
        monkeypatch.setattr(backfill_module, "MIRROR_PROGRESS_SECONDS", 0)
        stack = await Stack(telegram_like(), discord_like(), inline=False).init()
        await stack.put("a.bin", DATA)
        original = stack.mirror.upload

        async def upload(file_msg):
            # The in-memory store never waits; give the progress log a turn.
            for _ in range(3):
                await asyncio.sleep(0)
            return await original(file_msg)

        stack.mirror.upload = upload  # type: ignore[method-assign]

        with caplog.at_level(logging.INFO, logger="tgdcfs.core.backfill"):
            await backfill_mirrors(client_of(stack))  # type: ignore[arg-type]

        assert "Backfill for 'fs': 0/1 files" in caplog.text
        assert "copying a.bin@" in caplog.text

    async def test_a_mirrored_file_plans_nothing(self):
        stack = await Stack(telegram_like(), discord_like(), inline=True).init()
        await stack.put("a.bin", DATA)

        report = await backfill_mirrors(client_of(stack))  # type: ignore[arg-type]

        assert report.bytes_planned == 0 and report.bytes_mirrored == 0


class TestWorkerProgress:
    async def test_the_worker_counts_files_and_bytes(self):
        queue = ReplicationQueue(None)
        stack = await Stack(
            telegram_like(), discord_like(), inline=False, queue=queue
        ).init()
        await stack.put("a.bin", DATA)
        worker = ReplicationWorker(client_of(stack), queue)  # type: ignore[arg-type]
        worker.start()
        try:
            assert progress_registry.get("replication", "fs") is worker.progress
            assert await worker.run_once() == 1
        finally:
            await worker.stop()

        progress = worker.progress
        assert (progress.files_done, progress.files_total) == (1, 1)
        assert progress.bytes_done == len(DATA)
        assert progress.phase == "idle" and progress.current is None
        assert worker.last_report is not None
        assert worker.last_report.parts_mirrored == 80
        assert progress_registry.get("replication", "fs") is None
