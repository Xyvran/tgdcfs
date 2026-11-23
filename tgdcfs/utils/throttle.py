"""Pacing of background transfers, per bot and for every backend.

The backfill and the replication queue move whole libraries. Left alone
they fire requests as fast as the bots answer and run every bot into
flood waits, which then hit the people actually using the file system.
This module keeps the background out of their way:

* Each bot -- every Telegram bot, the user account, every Discord bot --
  has its own request budget, because each has its own limits. The
  budget is an AIMD rate: halved on a flood wait (Telegram) or a 429
  (Discord), the bot blocked for as long as the server asked, and raised
  step by step after a quiet interval.
* Background requests prefer the bot that is free soonest, so a bot in
  cooldown is left alone while others can work.
* While anyone transfers in the foreground, and for a cooldown after,
  every bot serves the background at the minimum rate only.

Nothing here applies to the foreground: its requests are attributed to
their bot, so their flood waits still slow the background down, but they
are never delayed. Work is marked as background with :func:`background`;
everything else is foreground. The marking travels with the asyncio
context, so tasks spawned by background work are background too.

Flood waits are learnt from the libraries' own log records: Telethon,
Pyrogram and discord.py sleep through short waits internally and only
log them. The record is attributed to the bot whose request is running
in the current context (see :func:`attributed`).
"""

import asyncio
import functools
import logging
import re
import time
from contextlib import asynccontextmanager, contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from typing import (
    AsyncIterator,
    Awaitable,
    Callable,
    Dict,
    Iterator,
    Optional,
    ParamSpec,
    Sequence,
    TypeVar,
)

from tgdcfs.config import BackgroundTransferConfig, get_config
from tgdcfs.utils.others import flood_wait_seconds

logger = logging.getLogger(__name__)

T = TypeVar("T")
R = TypeVar("R")
P = ParamSpec("P")

_background: ContextVar[bool] = ContextVar("tgdcfs_background", default=False)
_current_bot: ContextVar[Optional[str]] = ContextVar("tgdcfs_bot", default=None)


def in_background() -> bool:
    return _background.get()


@contextmanager
def background() -> Iterator[None]:
    """Mark the work done in this block (and tasks it spawns) as background."""
    token = _background.set(True)
    try:
        yield
    finally:
        _background.reset(token)


def background_work(fn: Callable[P, Awaitable[R]]) -> Callable[P, Awaitable[R]]:
    """Run the decorated coroutine function as background work."""

    @functools.wraps(fn)
    async def wrapper(*args: P.args, **kwargs: P.kwargs) -> R:
        with background():
            return await fn(*args, **kwargs)

    return wrapper


@contextmanager
def attributed(key: str) -> Iterator[None]:
    """Attribute flood waits logged in this block to the bot ``key``."""
    token = _current_bot.set(key)
    try:
        yield
    finally:
        _current_bot.reset(token)


def backoff_seconds(ex: BaseException) -> Optional[float]:
    """How long the server asked us to wait, or ``None`` for other errors.

    Covers Telegram's flood waits and discord.py's ``RateLimited``.
    """
    if (wait := flood_wait_seconds(ex)) is not None:
        return float(wait)
    if "ratelimited" in type(ex).__name__.lower():
        retry_after = getattr(ex, "retry_after", None)
        if isinstance(retry_after, (int, float)):
            return float(retry_after)
    return None


@dataclass
class _Budget:
    key: str
    rate: float
    # Monotonic time of the next free request slot.
    next_slot: float = 0.0
    blocked_until: float = 0.0
    last_change: float = 0.0
    last_flood: float = 0.0
    floods: int = 0
    # The foreground period this budget last caught up with.
    foreground_epoch: int = 0


class Throttle:
    def __init__(
        self,
        config: Optional[Callable[[], BackgroundTransferConfig]] = None,
        clock: Callable[[], float] = time.monotonic,
    ):
        self._config = config or (lambda: get_config().tgdcfs.transfer.background)
        self._clock = clock
        self._budgets: Dict[str, _Budget] = {}
        self._foreground_active = 0
        self._foreground_ended: Optional[float] = None
        self._foreground_epoch = 0
        self._pick_offset = 0

    # -- state ---------------------------------------------------------------

    def _set_rate(self, budget: _Budget, rate: float, reason: str) -> None:
        if abs(rate - budget.rate) < 1e-9:
            return
        logger.info(
            f"Background rate of {budget.key}: {budget.rate:.2f} -> {rate:.2f} "
            f"requests/s ({reason})"
        )
        budget.rate = rate

    def _budget(self, key: str) -> _Budget:
        if (budget := self._budgets.get(key)) is None:
            now = self._clock()
            budget = _Budget(
                key=key,
                rate=self._config().start_rate,
                last_change=now,
                foreground_epoch=self._foreground_epoch,
            )
            self._budgets[key] = budget
        return budget

    def foreground_busy(self) -> bool:
        if self._foreground_active > 0:
            return True
        if self._foreground_ended is None:
            return False
        cooldown = self._config().foreground_cooldown_seconds
        return self._clock() - self._foreground_ended < cooldown

    def _refresh(self, budget: _Budget) -> None:
        """Bring the budget up to date: ramp up after quiet time, and start
        over from the start rate once a foreground period is over."""
        if self.foreground_busy():
            return
        cfg = self._config()
        now = self._clock()
        if budget.foreground_epoch != self._foreground_epoch:
            budget.foreground_epoch = self._foreground_epoch
            budget.last_change = now
            if budget.rate > cfg.start_rate:
                self._set_rate(budget, cfg.start_rate, "foreground transfers ended")
            return
        if budget.rate >= cfg.max_rate or cfg.increase_step <= 0:
            return
        quiet = now - max(budget.last_change, budget.last_flood)
        steps = int(quiet // cfg.increase_interval_seconds)
        if steps < 1:
            return
        rate = min(cfg.max_rate, budget.rate + steps * cfg.increase_step)
        self._set_rate(budget, rate, f"no flood wait for {quiet:.0f}s")
        budget.last_change = now

    def _effective_rate(self, budget: _Budget) -> float:
        if self.foreground_busy():
            return self._config().min_rate
        return budget.rate

    def ready_at(self, key: str) -> float:
        """Monotonic time at which ``key`` could serve the next request."""
        budget = self._budget(key)
        self._refresh(budget)
        return max(budget.next_slot, budget.blocked_until)

    # -- feedback --------------------------------------------------------------

    def flood(self, key: str, seconds: float, source: str = "") -> None:
        """Record that ``key`` was told to wait ``seconds``.

        Requests running on the same bot at once all receive the wait; only
        the first of them halves the rate; the others just extend the block.
        """
        budget = self._budget(key)
        cfg = self._config()
        now = self._clock()
        budget.floods += 1
        if now >= budget.blocked_until:
            rate = max(cfg.min_rate, budget.rate / 2)
            what = f"{source} " if source else ""
            self._set_rate(budget, rate, f"{what}flood wait of {seconds:.0f}s")
        budget.last_flood = now
        budget.last_change = now
        budget.blocked_until = max(budget.blocked_until, now + seconds)
        budget.next_slot = max(budget.next_slot, budget.blocked_until)

    # -- pacing ----------------------------------------------------------------

    async def acquire(self, key: str, force: bool = False) -> None:
        """Wait for a request slot of ``key``. Foreground work passes at once
        unless ``force`` says the request is background regardless of the
        context it runs in."""
        if not force and not in_background():
            return
        budget = self._budget(key)
        while True:
            self._refresh(budget)
            now = self._clock()
            slot = max(now, budget.next_slot, budget.blocked_until)
            budget.next_slot = slot + 1 / self._effective_rate(budget)
            if slot > now:
                await asyncio.sleep(slot - now)
            # A flood wait that arrived meanwhile takes the slot back.
            if self._clock() >= budget.blocked_until:
                return

    def pick(
        self,
        candidates: Sequence[T],
        key_of: Callable[[T], str],
        force: bool = False,
    ) -> Optional[T]:
        """The candidate a background request should go to: the one free
        soonest, rotating among equals. ``None`` for foreground work, which
        keeps its round robin."""
        if not (force or in_background()) or not candidates:
            return None
        self._pick_offset = (self._pick_offset + 1) % len(candidates)
        rotated = [*candidates[self._pick_offset :], *candidates[: self._pick_offset]]
        return min(rotated, key=lambda c: self.ready_at(key_of(c)))

    @asynccontextmanager
    async def request(
        self, key: str, pace: bool = True, force: bool = False
    ) -> AsyncIterator[None]:
        """One request on bot ``key``: paced if it is background work,
        attributed either way, and its flood errors recorded."""
        if pace:
            await self.acquire(key, force=force)
        with attributed(key):
            try:
                yield
            except Exception as ex:
                if (wait := backoff_seconds(ex)) is not None:
                    self.flood(key, wait, type(ex).__name__)
                raise

    async def paced(
        self, key: str, chunks: AsyncIterator[bytes]
    ) -> AsyncIterator[bytes]:
        """Pace a download stream that issues one request per chunk."""
        iterator = aiter(chunks)
        while True:
            async with self.request(key):
                try:
                    chunk = await anext(iterator)
                except StopAsyncIteration:
                    return
            yield chunk

    # -- foreground ------------------------------------------------------------

    @contextmanager
    def foreground(self) -> Iterator[None]:
        """Count a foreground transfer for as long as the block runs.
        Background work passes through uncounted."""
        if in_background():
            yield
            return
        if self._foreground_active == 0:
            self._foreground_epoch += 1
            logger.info("Foreground transfer started, background slowed down")
        self._foreground_active += 1
        try:
            yield
        finally:
            self._foreground_active -= 1
            if self._foreground_active == 0:
                self._foreground_ended = self._clock()

    async def foreground_stream(
        self, chunks: AsyncIterator[bytes]
    ) -> AsyncIterator[bytes]:
        with self.foreground():
            async for chunk in chunks:
                yield chunk

    # -- reporting -------------------------------------------------------------

    def snapshot(self) -> dict:
        now = self._clock()
        bots = {}
        for key, budget in sorted(self._budgets.items()):
            self._refresh(budget)
            bots[key] = {
                "rate": round(budget.rate, 2),
                "effective_rate": round(self._effective_rate(budget), 2),
                "blocked_for_seconds": round(max(0.0, budget.blocked_until - now), 1),
                "flood_waits": budget.floods,
            }
        return {
            "foreground_transfers": self._foreground_active,
            "foreground_busy": self.foreground_busy(),
            "bots": bots,
        }


throttle = Throttle()


# Flood waits the libraries sleep through on their own, as they log them.
_FLOOD_PATTERNS = {
    # "Sleeping for 2s (0:00:02) on GetFileRequest flood wait"
    "telethon.client.users": re.compile(r"for (\d+)s .*flood wait"),
    # '[bot] Waiting for 3 seconds before continuing (required by "...")'
    "pyrogram.session.session": re.compile(r"Waiting for (\d+) seconds"),
    # "We are being rate limited. GET ... responded with 429. Retrying in 1.20 seconds."
    "discord.http": re.compile(r"responded with 429\. Retrying in ([\d.]+) seconds"),
}


class FloodLogHandler(logging.Handler):
    def __init__(self, target: Throttle):
        super().__init__(level=logging.DEBUG)
        self._target = target

    def emit(self, record: logging.LogRecord) -> None:
        if (key := _current_bot.get()) is None:
            return
        pattern = _FLOOD_PATTERNS.get(record.name)
        if pattern is None:
            return
        try:
            if match := pattern.search(record.getMessage()):
                source = record.name.split(".")[0]
                self._target.flood(key, float(match.group(1)), source)
        except Exception:
            self.handleError(record)


_installed = False


def install_flood_listeners(target: Throttle = throttle) -> None:
    """Listen to the libraries' flood wait log records. Idempotent."""
    global _installed
    if _installed:
        return
    _installed = True
    handler = FloodLogHandler(target)
    for name in _FLOOD_PATTERNS:
        lib_logger = logging.getLogger(name)
        lib_logger.addHandler(handler)
        # Telethon logs its waits at INFO; a quieter logger would drop the
        # record before the handler sees it.
        if lib_logger.getEffectiveLevel() > logging.INFO:
            lib_logger.setLevel(logging.INFO)


install_flood_listeners()
