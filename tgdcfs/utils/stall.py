"""Detection of download streams that stop delivering.

A Telegram request can hang without ever failing: after a flood wait, a
single ``upload.getFile`` was seen never to return, and the download
waiting on it -- a backfill, which waits for nothing else -- sat still
for hours. The download loops read their chunks through
:func:`next_chunk`, which gives up on a chunk after a while so the loop
can start over at the same offset.
"""

import asyncio
from typing import AsyncIterator, Optional

from tgdcfs.config import get_config


class StreamStalled(Exception):
    pass


def stall_limits() -> tuple[float, int]:
    """Seconds without a chunk, and how many restarts a download gets."""
    cfg = get_config().tgdcfs.transfer.background
    return cfg.stall_timeout_seconds, cfg.stall_restarts


async def next_chunk(iterator: AsyncIterator[bytes], timeout: float) -> Optional[bytes]:
    """The next chunk, ``None`` at the end; :class:`StreamStalled` if none
    arrives within ``timeout`` seconds. A stalled iterator is unusable
    afterwards and has to be replaced."""
    try:
        async with asyncio.timeout(timeout):
            return await anext(iterator)
    except StopAsyncIteration:
        return None
    except TimeoutError:
        raise StreamStalled() from None
