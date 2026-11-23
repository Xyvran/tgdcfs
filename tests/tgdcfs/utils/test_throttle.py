import asyncio
import logging

import pytest

from tgdcfs.config import BackgroundTransferConfig
from tgdcfs.utils import throttle as throttle_module
from tgdcfs.utils.throttle import (
    FloodLogHandler,
    Throttle,
    attributed,
    background,
    background_work,
    backoff_seconds,
    in_background,
)


class Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


@pytest.fixture
def clock() -> Clock:
    return Clock()


@pytest.fixture
def config() -> BackgroundTransferConfig:
    return BackgroundTransferConfig(
        start_rate=2.0,
        min_rate=0.25,
        max_rate=4.0,
        increase_step=0.5,
        increase_interval_seconds=60.0,
        foreground_cooldown_seconds=30.0,
    )


@pytest.fixture
def throttle(config, clock) -> Throttle:
    return Throttle(config=lambda: config, clock=clock)


@pytest.fixture
def sleeps(monkeypatch, clock) -> list[float]:
    """Replace the throttle's sleep with one that advances the fake clock."""
    recorded: list[float] = []

    async def fake_sleep(seconds: float) -> None:
        recorded.append(seconds)
        clock.advance(seconds)

    monkeypatch.setattr(throttle_module.asyncio, "sleep", fake_sleep)
    return recorded


def rate(throttle: Throttle, key: str) -> float:
    return throttle.snapshot()["bots"][key]["rate"]


class TestMarking:
    def test_foreground_by_default(self):
        assert not in_background()

    def test_background_block(self):
        with background():
            assert in_background()
        assert not in_background()

    @pytest.mark.asyncio
    async def test_decorator_and_spawned_tasks_inherit(self):
        @background_work
        async def work() -> bool:
            async def child() -> bool:
                return in_background()

            return await asyncio.create_task(child())

        assert await work()
        assert not in_background()


class TestBackoffSeconds:
    def test_discord_rate_limited(self):
        class RateLimited(Exception):
            retry_after = 1.5

        assert backoff_seconds(RateLimited()) == 1.5

    def test_other_errors(self):
        assert backoff_seconds(ValueError("nope")) is None


class TestAimd:
    def test_starts_at_start_rate(self, throttle):
        throttle.ready_at("a")
        assert rate(throttle, "a") == 2.0

    def test_flood_halves_and_blocks(self, throttle, clock):
        throttle.flood("a", 10)

        assert rate(throttle, "a") == 1.0
        assert throttle.ready_at("a") == clock.now + 10
        assert throttle.snapshot()["bots"]["a"]["flood_waits"] == 1

    def test_concurrent_floods_halve_once(self, throttle, clock):
        """Requests in flight on one bot all get the same wait."""
        throttle.flood("a", 10)
        throttle.flood("a", 12)

        assert rate(throttle, "a") == 1.0
        assert throttle.ready_at("a") == clock.now + 12

    def test_never_below_min_rate(self, throttle, clock):
        for _ in range(10):
            throttle.flood("a", 1)
            clock.advance(2)

        assert rate(throttle, "a") == 0.25

    def test_ramps_up_after_quiet_time(self, throttle, clock):
        throttle.flood("a", 1)
        clock.advance(59)
        assert rate(throttle, "a") == 1.0
        clock.advance(1)
        assert rate(throttle, "a") == 1.5
        clock.advance(10 * 60)
        assert rate(throttle, "a") == 4.0

    def test_bots_are_independent(self, throttle, clock):
        throttle.ready_at("b")
        throttle.flood("a", 30)

        assert rate(throttle, "a") == 1.0
        assert rate(throttle, "b") == 2.0
        assert throttle.ready_at("b") <= clock.now


class TestAcquire:
    @pytest.mark.asyncio
    async def test_foreground_is_not_paced(self, throttle, sleeps):
        throttle.flood("a", 30)
        await throttle.acquire("a")
        assert sleeps == []

    @pytest.mark.asyncio
    async def test_background_is_spaced_by_the_rate(self, throttle, sleeps):
        with background():
            for _ in range(3):
                await throttle.acquire("a")
        assert sleeps == [0.5, 0.5]

    @pytest.mark.asyncio
    async def test_background_waits_out_a_block(self, throttle, sleeps):
        throttle.flood("a", 30)
        with background():
            await throttle.acquire("a")
        assert sleeps == [30]

    @pytest.mark.asyncio
    async def test_force_paces_foreground_context(self, throttle, sleeps):
        throttle.flood("a", 5)
        await throttle.acquire("a", force=True)
        assert sleeps == [5]

    @pytest.mark.asyncio
    async def test_request_records_flood_errors(self, throttle):
        class RateLimited(Exception):
            retry_after = 3.0

        with pytest.raises(RateLimited):
            async with throttle.request("discord:1"):
                raise RateLimited()

        assert throttle.snapshot()["bots"]["discord:1"]["flood_waits"] == 1
        assert rate(throttle, "discord:1") == 1.0

    @pytest.mark.asyncio
    async def test_paced_stream(self, throttle, sleeps):
        async def chunks():
            for chunk in (b"a", b"b", b"c"):
                yield chunk

        with background():
            served = [c async for c in throttle.paced("a", chunks())]

        assert served == [b"a", b"b", b"c"]
        # Three chunks and the end of the stream: four requests.
        assert sleeps == [0.5, 0.5, 0.5]


class TestPick:
    def test_foreground_keeps_round_robin(self, throttle):
        assert throttle.pick(["a", "b"], str) is None

    def test_prefers_the_free_bot(self, throttle):
        throttle.flood("a", 30)
        with background():
            assert {throttle.pick(["a", "b"], str) for _ in range(4)} == {"b"}

    def test_rotates_among_equals(self, throttle):
        with background():
            picked = {throttle.pick(["a", "b", "c"], str) for _ in range(3)}
        assert picked == {"a", "b", "c"}

    def test_force(self, throttle):
        assert throttle.pick(["a"], str, force=True) == "a"


class TestForeground:
    def test_slows_background_to_min_rate(self, throttle, clock):
        throttle.ready_at("a")
        with throttle.foreground():
            snap = throttle.snapshot()
            assert snap["foreground_transfers"] == 1
            assert snap["bots"]["a"]["effective_rate"] == 0.25

        clock.advance(29)
        assert throttle.snapshot()["bots"]["a"]["effective_rate"] == 0.25
        clock.advance(1)
        assert throttle.snapshot()["bots"]["a"]["effective_rate"] == 2.0

    def test_resets_a_ramped_rate_after_cooldown(self, throttle, clock):
        throttle.ready_at("a")
        clock.advance(4 * 60)
        assert rate(throttle, "a") == 4.0

        with throttle.foreground():
            clock.advance(10 * 60)
        clock.advance(30)

        assert rate(throttle, "a") == 2.0

    def test_background_is_not_counted(self, throttle):
        with background(), throttle.foreground():
            assert not throttle.foreground_busy()

    @pytest.mark.asyncio
    async def test_stream_counts_while_open(self, throttle):
        seen = []

        async def chunks():
            seen.append(throttle.foreground_busy())
            yield b"x"

        assert [c async for c in throttle.foreground_stream(chunks())] == [b"x"]
        assert seen == [True]
        assert throttle.snapshot()["foreground_transfers"] == 0


class TestFloodLogHandler:
    @pytest.fixture
    def handler(self, throttle):
        return FloodLogHandler(throttle)

    @staticmethod
    def record(name: str, msg: str) -> logging.LogRecord:
        return logging.LogRecord(name, logging.INFO, __file__, 1, msg, None, None)

    @pytest.mark.parametrize(
        "name,msg,seconds",
        [
            (
                "telethon.client.users",
                "Sleeping for 4s (0:00:04) on GetFileRequest flood wait",
                4,
            ),
            (
                "pyrogram.session.session",
                '[bot] Waiting for 3 seconds before continuing (required by "x")',
                3,
            ),
            (
                "discord.http",
                "We are being rate limited. GET /x responded with 429. "
                "Retrying in 1.20 seconds.",
                1.2,
            ),
        ],
    )
    def test_attributes_to_the_running_bot(
        self, handler, throttle, clock, name, msg, seconds
    ):
        with attributed("bot1"):
            handler.emit(self.record(name, msg))

        assert throttle.snapshot()["bots"]["bot1"]["flood_waits"] == 1
        assert throttle.ready_at("bot1") == pytest.approx(clock.now + seconds)

    def test_ignores_unattributed_records(self, handler, throttle):
        handler.emit(
            self.record("telethon.client.users", "Sleeping for 4s on X flood wait")
        )
        assert throttle.snapshot()["bots"] == {}

    def test_ignores_other_loggers(self, handler, throttle):
        with attributed("bot1"):
            handler.emit(self.record("tgdcfs", "Waiting for 3 seconds"))
        assert throttle.snapshot()["bots"] == {}
