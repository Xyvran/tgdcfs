"""The Discord store: one channel, addressed through a pool of bots.

Discord differs from Telegram in three ways that shape this class:

* a message carries at most a few tens of megabytes, so ``upload``
  partitions files into many small parts and buffers one part at a time;
* a bot may send 2000 characters of text, so a file descriptor that does
  not fit is sent as a small JSON attachment ("overflow") and read back
  transparently;
* there is no server-side copy, so ``copy_within`` re-uploads and
  ``copy_from`` always reports that the caller has to re-upload.

Adapted from the dcfs project (Apache 2.0, see NOTICE).
"""

from __future__ import annotations

import asyncio
import logging
from itertools import cycle
from typing import AsyncIterator, Iterable, List, Optional, Sequence

from tgdcfs.backends.base import IStore, StoreCapabilities, make_store_key
from tgdcfs.errors import MessageNotFound, TechnicalError
from tgdcfs.reqres import (
    DownloadFileResp,
    FileMessageFromStream,
    MessageResp,
    MessageRespWithDocument,
    SentFileMessage,
    UploadableFileMessage,
)
from tgdcfs.utils.message_cache import channel_cache

from .client import DiscordBotAPI, is_transient

logger = logging.getLogger(__name__)

BACKEND_NAME = "discord"
KEY_PREFIX = "dc"

# Bots may send this much text per message; Nitro does not raise it.
MAX_TEXT_CHARS = 2000

# Descriptor texts above the limit travel as this attachment, marked by
# the caption so a reader knows to open it.
OVERFLOW_FILENAME = "overflow.json"
OVERFLOW_CAPTION = "TGDCFS_OVERFLOW"

# Reads a part in this many bytes at a time while buffering it.
READ_CHUNK = 1024 * 1024


class DiscordStore(IStore):
    def __init__(
        self,
        bots: Sequence[DiscordBotAPI],
        channel_id: int,
        key: Optional[str] = None,
        max_part_bytes: int = 10_000_000,
        delete_on_remove: bool = False,
        max_retries: int = 10,
        retry_interval: float = 5.0,
        max_concurrent_uploads: int = 3,
        max_concurrent_downloads: int = 3,
    ):
        IStore.__init__(self, key or make_store_key(KEY_PREFIX, str(channel_id)))
        if not bots:
            raise ValueError("A Discord store needs at least one bot")
        self._bots = list(bots)
        self._bots_cycle = cycle(self._bots)
        self.channel_id = channel_id
        self._max_part_bytes = max_part_bytes
        self._delete_on_remove = delete_on_remove
        self._max_retries = max_retries
        self._retry_interval = retry_interval
        self._upload_slots = asyncio.Semaphore(max_concurrent_uploads)
        self._download_slots = asyncio.Semaphore(max_concurrent_downloads)

    @property
    def backend(self) -> str:
        return BACKEND_NAME

    @property
    def caps(self) -> StoreCapabilities:
        return StoreCapabilities(
            max_part_bytes=self._max_part_bytes,
            max_text_chars=MAX_TEXT_CHARS,
            supports_server_copy=False,
            supports_edit_media=True,
            supports_bulk_delete=True,
            delete_age_limit_days=14,
        )

    @property
    def next_bot(self) -> DiscordBotAPI:
        return next(self._bots_cycle)

    # -- retries -----------------------------------------------------------

    async def _retry(self, what: str, action):
        """Run ``action()`` again on transient failures, with backoff."""
        last: Optional[Exception] = None
        for attempt in range(1, self._max_retries + 1):
            try:
                return await action()
            except Exception as ex:
                if not is_transient(ex):
                    raise
                last = ex
                delay = min(self._retry_interval * attempt, 60.0)
                logger.warning(
                    f"{what} in Discord store {self.key} failed ({ex}); "
                    f"attempt {attempt}/{self._max_retries}, retrying in {delay:.0f}s"
                )
                await asyncio.sleep(delay)
        raise TechnicalError(
            f"{what} in Discord store {self.key} failed after "
            f"{self._max_retries} attempts: {last}"
        ) from last

    # -- text messages -----------------------------------------------------

    async def send_text(self, message: str) -> int:
        cache = channel_cache(self.channel_id).id
        if len(message) <= MAX_TEXT_CHARS:
            sent = await self._retry(
                "send_text", lambda: self.next_bot.send_text(self.channel_id, message)
            )
        else:
            sent = await self._retry(
                "send_text (overflow)",
                lambda: self.next_bot.send_file(
                    self.channel_id,
                    message.encode("utf-8"),
                    OVERFLOW_FILENAME,
                    caption=OVERFLOW_CAPTION,
                ),
            )
        cache[sent.message_id] = MessageResp(
            message_id=sent.message_id, text=message, document=None
        )
        return sent.message_id

    async def edit_message_text(self, message_id: int, message: str) -> int:
        cache = channel_cache(self.channel_id).id
        if len(message) <= MAX_TEXT_CHARS:
            mid = await self._retry(
                "edit_message_text",
                lambda: self.next_bot.edit_text(self.channel_id, message_id, message),
            )
        else:
            mid = await self._retry(
                "edit_message_text (overflow)",
                lambda: self.next_bot.edit_file(
                    self.channel_id,
                    message_id,
                    message.encode("utf-8"),
                    OVERFLOW_FILENAME,
                    caption=OVERFLOW_CAPTION,
                ),
            )
        cache[mid] = MessageResp(message_id=mid, text=message, document=None)
        return mid

    async def _resolve_overflow(self, message: MessageResp) -> MessageResp:
        """Replace the overflow marker with the descriptor text it stands for."""
        if message.text != OVERFLOW_CAPTION or message.document is None:
            return message
        chunks, _ = await self.next_bot.download(
            self.channel_id, message.message_id, 0, -1
        )
        data = bytearray()
        async for chunk in chunks:
            data.extend(chunk)
        # The document stays: content validation looks for it, and the
        # text is what descriptor reads look for.
        return MessageResp(
            message_id=message.message_id,
            text=data.decode("utf-8"),
            document=message.document,
        )

    async def get_messages(self, ids: list[int]) -> list[Optional[MessageResp]]:
        cache = channel_cache(self.channel_id).id
        cached = cache.gets(ids)
        missing = [mid for mid, msg in zip(ids, cached) if msg is None]
        if missing:
            fetched = await self._retry(
                "get_messages",
                lambda: self.next_bot.get_messages(self.channel_id, missing),
            )
            for mid, message in zip(missing, fetched):
                if message is not None:
                    cache[mid] = await self._resolve_overflow(message)
        return cache.gets(ids)

    async def search_messages(self, search: str) -> list[MessageResp]:
        # Discord offers no message search to bots; nothing above this
        # layer depends on it.
        return []

    # -- documents ---------------------------------------------------------

    async def _read_part(self, file_msg: UploadableFileMessage, limit: int) -> bytes:
        """Buffer up to ``limit`` bytes of the message, yielding between reads."""
        data = bytearray()
        while len(data) < limit:
            chunk = await file_msg.read(min(READ_CHUNK, limit - len(data)))
            if not chunk:
                break
            data.extend(chunk)
            # Let the gateway heartbeat and other transfers run.
            await asyncio.sleep(0)
        return bytes(data)

    async def _send_part(self, data: bytes, name: str) -> SentFileMessage:
        async with self._upload_slots:
            sent = await self._retry(
                f"upload of {name}",
                lambda: self.next_bot.send_file(self.channel_id, data, name),
            )
        return SentFileMessage(message_id=sent.message_id, size=len(data))

    async def upload(self, file_msg: UploadableFileMessage) -> List[SentFileMessage]:
        """Send ``file_msg`` as parts of at most ``max_part_bytes``.

        Parts are read sequentially (the message is a stream) and sent
        with bounded concurrency; the result is in file order.
        """
        await file_msg.open()
        file_name = file_msg.file_name()
        size = file_msg.get_size()
        tasks: List[asyncio.Task[SentFileMessage]] = []
        try:
            index = 0
            remaining = size
            while True:
                limit = self._max_part_bytes
                if size >= 0:
                    limit = min(limit, remaining)
                data = await self._read_part(file_msg, limit) if limit > 0 else b""
                if not data and index > 0:
                    break
                index += 1
                tasks.append(
                    asyncio.create_task(
                        self._send_part(data, f"[part{index}]{file_name}")
                    )
                )
                remaining -= len(data)
                if (size >= 0 and remaining <= 0) or len(data) < self._max_part_bytes:
                    break
            return list(await asyncio.gather(*tasks))
        except BaseException:
            for task in tasks:
                task.cancel()
            raise
        finally:
            await file_msg.close()

    async def download_file(
        self, message_id: int, begin: int, end: int
    ) -> DownloadFileResp:
        async with self._download_slots:
            chunks, size = await self._retry(
                f"download of message {message_id}",
                lambda: self.next_bot.download(self.channel_id, message_id, begin, end),
            )

        async def guarded() -> AsyncIterator[bytes]:
            async with self._download_slots:
                async for chunk in chunks:
                    yield chunk

        return DownloadFileResp(chunks=guarded(), size=size)

    async def replace_document(self, message_id: int, buffer: bytes, name: str) -> int:
        if len(buffer) > self._max_part_bytes:
            raise TechnicalError(
                f"A {len(buffer)}-byte document does not fit one Discord message "
                f"({self._max_part_bytes} bytes)"
            )
        mid = await self._retry(
            f"replace_document {message_id}",
            lambda: self.next_bot.edit_file(self.channel_id, message_id, buffer, name),
        )
        cache = channel_cache(self.channel_id).id
        if (cached := cache.get(mid)) is not None and cached.document is not None:
            cache[mid] = MessageResp(
                message_id=mid,
                text=cached.text,
                document=type(cached.document)(
                    size=len(buffer),
                    id=cached.document.id,
                    access_hash=0,
                    file_reference=b"",
                    mime_type=cached.document.mime_type,
                ),
            )
        return mid

    # -- copies ------------------------------------------------------------

    async def copy_within(self, message_ids: List[int]) -> List[int]:
        """Copy by streaming down and up again; Discord has no server-side copy."""
        res: List[int] = []
        for mid in message_ids:
            message = (await self.get_messages([mid]))[0]
            if not message or not message.document:
                raise MessageNotFound(message_id=mid)
            size = message.document.size
            resp = await self.download_file(mid, 0, size - 1)
            sent = await self.upload(
                FileMessageFromStream.new(
                    stream=resp.chunks, size=size, name=f"part-{mid}"
                )
            )
            res.append(sent[0].message_id)
        return res

    async def copy_from(
        self, source: IStore, message_ids: List[int]
    ) -> Optional[List[int]]:
        # Forwarding exists, but a forward is an immutable snapshot that
        # refers to the original attachment; it is not an independent
        # copy. Callers re-upload instead.
        return None

    # -- bookkeeping -------------------------------------------------------

    async def delete_messages(
        self, message_ids: Iterable[int], force: bool = False
    ) -> None:
        if not force and not self._delete_on_remove:
            return
        unique = list({mid for mid in message_ids if mid > 0})
        if not unique:
            return
        cache = channel_cache(self.channel_id).id
        try:
            await self._retry(
                "delete_messages",
                lambda: self.next_bot.delete_messages(self.channel_id, unique),
            )
        except Exception as ex:
            logger.warning(
                f"Failed to delete Discord messages {unique} in store {self.key}: {ex}"
            )
        for mid in unique:
            if mid in cache:
                cache[mid] = None

    async def pin_message(self, message_id: int) -> None:
        await self._retry(
            f"pin_message {message_id}",
            lambda: self.next_bot.pin_message(self.channel_id, message_id),
        )

    async def get_pinned_message(self) -> MessageRespWithDocument:
        from tgdcfs.errors import NoPinnedMessage

        pins = await self._retry(
            "get_pinned_messages",
            lambda: self.next_bot.get_pinned_messages(self.channel_id),
        )
        for message in pins:
            if message.document is not None and message.text != OVERFLOW_CAPTION:
                return MessageRespWithDocument(
                    message_id=message.message_id,
                    text=message.text,
                    document=message.document,
                )
        raise NoPinnedMessage()

    async def close(self) -> None:
        for bot in self._bots:
            await bot.close()
