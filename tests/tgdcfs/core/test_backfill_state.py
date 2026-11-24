"""The backfill keeps its run in ``backfill_state.json``: a restart
resumes it instead of reading every descriptor again, and a file that
fails is tried again later without holding up the others.
"""

import asyncio
import json
import logging
import time

import pytest

from tests.tgdcfs.core.test_progress import client_of
from tests.tgdcfs.core.test_replication import DATA, Stack, discord_like, telegram_like
from tgdcfs.core import backfill as backfill_module
from tgdcfs.core.backfill import (
    BackfillRunning,
    backfill_mirrors,
    resume_backfills,
    start_backfill,
)
from tgdcfs.core.backfill_state import BackfillState, FileEntry
from tgdcfs.errors import TechnicalError


async def two_files() -> Stack:
    stack = await Stack(telegram_like(), discord_like(), inline=False).init()
    await stack.put("a.bin", DATA)
    await stack.put("b.bin", DATA)
    return stack


def mirrored(stack: Stack) -> list[str]:
    return sorted(
        fr.name
        for fr in stack.root.files
        if all(fr.mirrors.get(key, 0) > 0 for key in stack.mirror_group.store_keys)
    )


def count_reads(stack: Stack) -> list[str]:
    reads: list[str] = []
    original = stack.fd_repo.get

    async def get(fr, *args, **kwargs):
        if not kwargs.get("include_all_versions"):
            reads.append(fr.name)
        return await original(fr, *args, **kwargs)

    stack.fd_repo.get = get  # type: ignore[method-assign]
    return reads


def interrupt_at(monkeypatch, call: int) -> list[str]:
    """Cancel the backfill when it gets to its ``call``-th file."""
    seen: list[str] = []
    original = backfill_module.backfill_file

    async def backfill_file(client, fr, *args, **kwargs):
        seen.append(fr.name)
        if len(seen) == call:
            raise asyncio.CancelledError
        return await original(client, fr, *args, **kwargs)

    monkeypatch.setattr(backfill_module, "backfill_file", backfill_file)
    return seen


def fail_uploads(stack: Stack, times: int) -> list[int]:
    """The first ``times`` uploads into the mirror fail."""
    calls: list[int] = []
    original = stack.mirror.upload

    async def upload(file_msg):
        calls.append(1)
        if len(calls) <= times:
            raise TechnicalError("dc:9: upload failed (simulated)")
        return await original(file_msg)

    stack.mirror.upload = upload  # type: ignore[method-assign]
    return calls


class TestState:
    def test_round_trip(self, tmp_path):
        path = str(tmp_path / "backfill_state.json")
        state = BackfillState(path)
        run = state.begin("fs", verify=True)
        run.files["/a.bin"] = FileEntry(path="/a.bin", updated_at=2, bytes=10)
        run.files["/b.bin"] = FileEntry(path="/b.bin", updated_at=1, bytes=20)
        state.done(run.files["/a.bin"])
        state.failed(run.files["/b.bin"], ["b.bin@v: boom"])
        state.save()

        loaded = BackfillState(path).get("fs")

        assert loaded is not None and loaded.verify
        assert [e.path for e in loaded.ordered()] == ["/a.bin", "/b.bin"]
        b = loaded.files["/b.bin"]
        assert (b.state, b.attempts, b.last_error) == ("failed", 1, "b.bin@v: boom")
        assert BackfillState(path).to_dict()["fs"]["failed"] == 1

    def test_backoff_doubles_up_to_the_limit(self, tmp_path):
        state = BackfillState(None, retry_base_seconds=10, retry_max_seconds=25)
        entry = FileEntry(path="/a.bin")
        waits = []
        for _ in range(3):
            state.failed(entry, ["boom"])
            waits.append(round(entry.not_before - time.time()))
        assert waits == [10, 20, 25]
        # Four attempts by default: one left.
        assert state.retryable(entry)
        state.failed(entry, ["boom"])
        assert entry.attempts == 4 and not state.retryable(entry)

    def test_a_broken_file_starts_empty(self, tmp_path, caplog):
        path = tmp_path / "backfill_state.json"
        path.write_text("{not json")
        assert BackfillState(str(path)).runs() == []

    def test_finish_removes_the_run(self, tmp_path):
        path = str(tmp_path / "backfill_state.json")
        state = BackfillState(path)
        state.begin("fs", verify=False)
        state.finish("fs")
        assert json.loads(open(path).read()) == {"runs": []}


class TestResume:
    async def test_a_finished_run_leaves_no_state(self, tmp_path):
        stack = await two_files()
        path = str(tmp_path / "backfill_state.json")

        report = await backfill_mirrors(client_of(stack), state=BackfillState(path))  # type: ignore[arg-type]

        assert report.failures == [] and report.files_scanned == 2
        assert mirrored(stack) == ["a.bin", "b.bin"]
        assert BackfillState(path).runs() == []

    async def test_a_restart_resumes_without_reading_again(
        self, tmp_path, monkeypatch, caplog
    ):
        monkeypatch.setattr(backfill_module, "CHECKPOINT_SECONDS", 0)
        stack = await two_files()
        path = str(tmp_path / "backfill_state.json")
        seen = interrupt_at(monkeypatch, 2)

        with pytest.raises(asyncio.CancelledError):
            await backfill_mirrors(client_of(stack), state=BackfillState(path))  # type: ignore[arg-type]

        # The first file is done and pushed, the second was interrupted.
        first, second = seen
        run = BackfillState(path).get("fs")
        assert run is not None and run.scanned
        assert {e.path: e.state for e in run.files.values()} == {
            f"/{first}": "done",
            f"/{second}": "pending",
        }

        monkeypatch.undo()
        seen = interrupt_at(monkeypatch, 0)
        reads = count_reads(stack)
        with caplog.at_level(logging.INFO, logger="tgdcfs.core.backfill"):
            report = await backfill_mirrors(client_of(stack), state=BackfillState(path))  # type: ignore[arg-type]

        assert reads == []
        assert seen == [second]
        assert report.failures == [] and report.files_scanned == 1
        assert mirrored(stack) == ["a.bin", "b.bin"]
        assert "resuming the run started" in caplog.text
        assert "reading 0 descriptors (2 already read)" in caplog.text
        assert f"Backfill [2/2] {second}" in caplog.text
        assert BackfillState(path).runs() == []

    async def test_files_not_pushed_yet_are_done_again(self, tmp_path, monkeypatch):
        stack = await two_files()
        path = str(tmp_path / "backfill_state.json")
        interrupt_at(monkeypatch, 2)

        with pytest.raises(asyncio.CancelledError):
            await backfill_mirrors(client_of(stack), state=BackfillState(path))  # type: ignore[arg-type]

        # No checkpoint since the first file: its copies are not in the pushed
        # metadata, so it is not saved as done.
        run = BackfillState(path).get("fs")
        assert run is not None
        assert {e.state for e in run.files.values()} == {"pending"}

    async def test_rescan_starts_over(self, tmp_path):
        stack = await two_files()
        path = str(tmp_path / "backfill_state.json")
        state = BackfillState(path)
        run = state.begin("fs", verify=False)
        run.files["/b.bin"] = FileEntry(path="/b.bin", state="done")
        run.scanned = True
        reads = count_reads(stack)

        await backfill_mirrors(client_of(stack), state=state, rescan=True)  # type: ignore[arg-type]

        assert sorted(reads) == ["a.bin", "b.bin"]
        assert mirrored(stack) == ["a.bin", "b.bin"]

    async def test_a_file_gone_since_the_scan_is_done(self, tmp_path):
        stack = await two_files()
        state = BackfillState(str(tmp_path / "backfill_state.json"))
        run = state.begin("fs", verify=False)
        run.files["/gone.bin"] = FileEntry(path="/gone.bin", updated_at=2**40)

        report = await backfill_mirrors(client_of(stack), state=state)  # type: ignore[arg-type]

        assert report.failures == []
        assert mirrored(stack) == ["a.bin", "b.bin"]


class TestRetry:
    async def test_a_failed_file_is_retried_after_the_others(
        self, tmp_path, monkeypatch, caplog
    ):
        stack = await two_files()
        fail_uploads(stack, 1)
        seen = interrupt_at(monkeypatch, 0)
        state = BackfillState(None, retry_base_seconds=0.01)

        with caplog.at_level(logging.INFO, logger="tgdcfs.core.backfill"):
            report = await backfill_mirrors(client_of(stack), state=state)  # type: ignore[arg-type]

        # The first file fails, the second goes on, then the first again.
        assert len(seen) == 3 and seen[0] == seen[2] != seen[1]
        assert report.failures == []
        assert report.files_scanned == 2
        assert mirrored(stack) == ["a.bin", "b.bin"]
        assert "1 files failed, next attempt in" in caplog.text
        assert f"Backfill [attempt 2/4] {seen[0]}" in caplog.text

    async def test_a_file_that_keeps_failing_is_reported_once(self, tmp_path):
        stack = await two_files()
        calls = fail_uploads(stack, 10**6)
        path = str(tmp_path / "backfill_state.json")
        state = BackfillState(path, max_attempts=3, retry_base_seconds=0.01)

        report = await backfill_mirrors(client_of(stack), state=state)  # type: ignore[arg-type]

        assert len(report.failures) == 2
        assert all("upload failed" in f for f in report.failures)
        assert {f.split(":")[0] for f in report.failures} == {"a.bin", "b.bin"}
        assert len(calls) >= 6
        assert BackfillState(path).runs() == []

    async def test_without_a_state_a_failure_is_not_retried(self, monkeypatch):
        stack = await two_files()
        fail_uploads(stack, 1)
        seen = interrupt_at(monkeypatch, 0)

        report = await backfill_mirrors(client_of(stack))  # type: ignore[arg-type]

        assert len(report.failures) == 1 and "upload failed" in report.failures[0]
        assert sorted(seen) == ["a.bin", "b.bin"]

    async def test_a_resume_retries_what_failed_before(self, tmp_path):
        stack = await two_files()
        path = str(tmp_path / "backfill_state.json")
        state = BackfillState(path)
        run = state.begin("fs", verify=False)
        run.scanned = True
        for name in ("a.bin", "b.bin"):
            run.files[f"/{name}"] = FileEntry(path=f"/{name}")
            state.failed(run.files[f"/{name}"], ["boom"])
            run.files[f"/{name}"].attempts = state.max_attempts
        state.save()

        report = await backfill_mirrors(client_of(stack), state=BackfillState(path))  # type: ignore[arg-type]

        assert report.failures == []
        assert mirrored(stack) == ["a.bin", "b.bin"]


class TestStart:
    async def test_one_run_per_file_system(self, tmp_path):
        stack = await two_files()
        client = client_of(stack)
        state = BackfillState(str(tmp_path / "backfill_state.json"))

        await start_backfill(client, state)  # type: ignore[arg-type]
        with pytest.raises(BackfillRunning):
            await start_backfill(client, state)  # type: ignore[arg-type]
        await asyncio.gather(*backfill_module._tasks)

        assert not backfill_module.backfill_running("fs")
        assert mirrored(stack) == ["a.bin", "b.bin"]

    async def test_startup_resumes_the_interrupted_runs(self, tmp_path):
        stack = await two_files()
        path = str(tmp_path / "backfill_state.json")
        state = BackfillState(path)
        state.begin("fs", verify=True)
        state.begin("gone", verify=False)

        task_ids = await resume_backfills({"fs": client_of(stack)}, state)  # type: ignore[dict-item]
        await asyncio.gather(*backfill_module._tasks)

        assert len(task_ids) == 1
        assert mirrored(stack) == ["a.bin", "b.bin"]
        # The run of a file system that is not configured is kept.
        assert [run.filesystem for run in BackfillState(path).runs()] == ["gone"]
