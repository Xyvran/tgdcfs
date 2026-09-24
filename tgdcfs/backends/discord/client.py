"""Thin wrapper around discord.py for one bot.

Everything the store needs from Discord is here, in plain methods;
the store layers rate limiting, retries, partitioning and overflow on
top. Adapted from the dcfs project (Apache 2.0, see NOTICE).

Facts this module relies on (checked September 2026):

* Attachment URLs are signed and expire (the ``ex`` parameter, about a
  day). Fetching the message again yields fresh URLs, so a download
  always starts from a freshly fetched message and never from a stored
  URL.
* Bulk deletion takes at most 100 ids and refuses messages older than
  14 days; those are deleted one by one.
* ``channel.pins()`` is a paginated async iterator since discord.py 2.6.
"""

from __future__ import annotations

import asyncio
import io
import logging
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, AsyncIterator, Dict, List, Optional, Sequence

import aiohttp
import discord
from discord.utils import snowflake_time

from tgdcfs.errors import MessageNotFound, TechnicalError
from tgdcfs.reqres import Document, MessageResp

logger = logging.getLogger(__name__)

# Messages older than this cannot be bulk deleted.
BULK_DELETE_MAX_AGE = timedelta(days=14)
BULK_DELETE_BATCH_SIZE = 100

# Bytes per chunk handed to the consumer of a download.
DOWNLOAD_CHUNK_SIZE = 1024 * 1024

# A download that stalls this long is abandoned rather than hanging a
# WebDAV request forever.
DOWNLOAD_CONNECT_TIMEOUT = 15.0
DOWNLOAD_READ_TIMEOUT = 120.0


@dataclass
class SentMessage:
    message_id: int


def _to_message_resp(message: discord.Message) -> MessageResp:
    doc = None
    if message.attachments:
        att = message.attachments[0]
        # Only ``size`` and ``mime_type`` matter above this layer; the
        # Telegram-specific fields are filled with placeholders.
        doc = Document(
            size=att.size,
            id=att.id,
            access_hash=0,
            file_reference=b"",
            mime_type=att.content_type,
        )
    return MessageResp(message_id=message.id, text=message.content or "", document=doc)


def is_transient(ex: Exception) -> bool:
    """Whether a retry of the failed request is likely to succeed.

    Rate limits, server errors, timeouts and connection failures are
    transient; other client errors (permissions, bad requests) are not.
    """
    if isinstance(ex, discord.RateLimited):
        return True
    status: Any = getattr(ex, "status", None)
    if status is not None:
        if status == 429 or 500 <= status < 600:
            return True
        if 400 <= status < 500:
            return False
    return isinstance(ex, (asyncio.TimeoutError, ConnectionError, IOError))


class DiscordBotAPI:
    """One logged-in bot, addressing channels by id."""

    def __init__(self, bot: discord.Client, name: str = "discord-bot"):
        self._bot = bot
        self.name = name
        self._http: Optional[aiohttp.ClientSession] = None

    # -- plumbing ----------------------------------------------------------

    async def _session(self) -> aiohttp.ClientSession:
        if self._http is None or self._http.closed:
            self._http = aiohttp.ClientSession()
        return self._http

    async def _channel(self, channel_id: int) -> Any:
        channel = self._bot.get_channel(channel_id)
        if channel is None:
            try:
                channel = await self._bot.fetch_channel(channel_id)
            except discord.NotFound:
                raise TechnicalError(f"Discord channel {channel_id} not found")
            except discord.Forbidden:
                raise TechnicalError(
                    f"The bot may not access Discord channel {channel_id}"
                )
        return channel

    async def _fetch(self, channel_id: int, message_id: int) -> discord.Message:
        channel = await self._channel(channel_id)
        try:
            return await channel.fetch_message(message_id)
        except discord.NotFound:
            raise MessageNotFound(message_id=message_id)

    async def close(self) -> None:
        if self._http and not self._http.closed:
            await self._http.close()
        await self._bot.close()

    # -- messages ----------------------------------------------------------

    async def send_text(self, channel_id: int, text: str) -> SentMessage:
        channel = await self._channel(channel_id)
        message = await channel.send(text)
        return SentMessage(message_id=message.id)

    async def send_file(
        self, channel_id: int, data: bytes, name: str, caption: str = ""
    ) -> SentMessage:
        channel = await self._channel(channel_id)
        message = await channel.send(
            content=caption or None, file=discord.File(io.BytesIO(data), filename=name)
        )
        return SentMessage(message_id=message.id)

    async def edit_text(self, channel_id: int, message_id: int, text: str) -> int:
        message = await self._fetch(channel_id, message_id)
        # A text-only message; drop any attachment an overflow left behind.
        await message.edit(content=text, attachments=[])
        return message.id

    async def edit_file(
        self,
        channel_id: int,
        message_id: int,
        data: bytes,
        name: str,
        caption: Optional[str] = None,
    ) -> int:
        message = await self._fetch(channel_id, message_id)
        await message.edit(
            content=caption,
            attachments=[discord.File(io.BytesIO(data), filename=name)],
        )
        return message.id

    async def get_message(
        self, channel_id: int, message_id: int
    ) -> Optional[MessageResp]:
        try:
            return _to_message_resp(await self._fetch(channel_id, message_id))
        except MessageNotFound:
            return None

    async def get_messages(
        self, channel_id: int, message_ids: Sequence[int]
    ) -> List[Optional[MessageResp]]:
        # Discord has no bulk fetch; requests run concurrently and the
        # library's rate limiter paces them.
        return list(
            await asyncio.gather(
                *(self.get_message(channel_id, mid) for mid in message_ids)
            )
        )

    async def get_pinned_messages(self, channel_id: int) -> List[MessageResp]:
        channel = await self._channel(channel_id)
        pins: List[MessageResp] = []
        async for message in channel.pins():
            pins.append(_to_message_resp(message))
        return pins

    async def pin_message(self, channel_id: int, message_id: int) -> None:
        message = await self._fetch(channel_id, message_id)
        await message.pin()

    async def delete_messages(
        self, channel_id: int, message_ids: Sequence[int]
    ) -> None:
        """Delete messages, in bulk where Discord allows it.

        Bulk deletion is refused for messages older than 14 days; their
        age is known from the snowflake, so they are deleted one by one
        without a failing request first.
        """
        channel = await self._channel(channel_id)
        cutoff = datetime.now(timezone.utc) - BULK_DELETE_MAX_AGE + timedelta(minutes=5)
        recent = [mid for mid in message_ids if snowflake_time(mid) > cutoff]
        old = [mid for mid in message_ids if snowflake_time(mid) <= cutoff]

        for start in range(0, len(recent), BULK_DELETE_BATCH_SIZE):
            batch = recent[start : start + BULK_DELETE_BATCH_SIZE]
            if len(batch) == 1:
                await self._delete_one(channel, batch[0])
                continue
            try:
                await channel.delete_messages([discord.Object(id=mid) for mid in batch])
            except discord.NotFound:
                # Some of them are gone already; the rest still have to go.
                for mid in batch:
                    await self._delete_one(channel, mid)

        for mid in old:
            await self._delete_one(channel, mid)

    @staticmethod
    async def _delete_one(channel: Any, message_id: int) -> None:
        try:
            await channel.get_partial_message(message_id).delete()
        except discord.NotFound:
            logger.debug(f"Discord message {message_id} is gone already")

    # -- downloads ---------------------------------------------------------

    async def download(
        self, channel_id: int, message_id: int, begin: int, end: int
    ) -> tuple[AsyncIterator[bytes], int]:
        """Stream ``[begin, end]`` (inclusive; ``end == -1`` is the end) of
        the first attachment. Returns the chunk iterator and the size of
        the range.

        The message is fetched first so the signed CDN URL is fresh; a
        CDN that ignores the ``Range`` header (answers 200) is sliced in
        memory so the contract holds either way.
        """
        message = await self._fetch(channel_id, message_id)
        if not message.attachments:
            raise TechnicalError(f"Discord message {message_id} has no attachment")
        attachment = message.attachments[0]
        if end < 0 or end >= attachment.size:
            end = attachment.size - 1
        if begin > end:
            raise TechnicalError(
                f"Invalid range {begin}-{end} for Discord message {message_id}"
            )
        size = end - begin + 1

        headers: Dict[str, str] = {}
        wants_range = begin > 0 or end < attachment.size - 1
        if wants_range:
            headers["Range"] = f"bytes={begin}-{end}"

        session = await self._session()
        timeout = aiohttp.ClientTimeout(
            sock_connect=DOWNLOAD_CONNECT_TIMEOUT, sock_read=DOWNLOAD_READ_TIMEOUT
        )
        response = await session.get(attachment.url, headers=headers, timeout=timeout)
        if response.status >= 400:
            response.close()
            raise TechnicalError(
                f"Discord CDN answered {response.status} for message {message_id}"
            )
        range_honoured = not wants_range or response.status == 206

        async def chunks() -> AsyncIterator[bytes]:
            try:
                if range_honoured:
                    async for chunk in response.content.iter_chunked(
                        DOWNLOAD_CHUNK_SIZE
                    ):
                        yield chunk
                    return
                # The CDN sent the whole attachment: skip to the range.
                skipped = 0
                delivered = 0
                async for chunk in response.content.iter_chunked(DOWNLOAD_CHUNK_SIZE):
                    if skipped < begin:
                        drop = min(len(chunk), begin - skipped)
                        skipped += drop
                        chunk = chunk[drop:]
                    if not chunk:
                        continue
                    take = min(len(chunk), size - delivered)
                    if take <= 0:
                        break
                    yield chunk[:take]
                    delivered += take
                    if delivered >= size:
                        break
            finally:
                response.close()

        return chunks(), size

    # -- identity ----------------------------------------------------------

    @property
    def user_name(self) -> str:
        user = self._bot.user
        return user.name if user else self.name


async def login_as_bot(token: str) -> discord.Client:
    """Log a bot in and wait until its gateway session is ready."""
    intents = discord.Intents.default()
    intents.message_content = True
    intents.guilds = True

    bot = discord.Client(intents=intents)

    @bot.event
    async def on_ready():
        if bot.user is not None:
            logger.info(f"Discord: logged in as {bot.user} (id {bot.user.id})")

    await bot.login(token)
    asyncio.get_running_loop().create_task(bot.connect())
    await bot.wait_until_ready()
    return bot
