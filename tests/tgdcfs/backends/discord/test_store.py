"""DiscordStore on a fake bot: overflow, partitioning, retries, deletion."""

from typing import AsyncIterator, Dict, List, Optional
from unittest.mock import AsyncMock

import discord
import pytest

from tgdcfs.backends.discord.client import DiscordBotAPI, SentMessage
from tgdcfs.backends.discord.store import (
    MAX_TEXT_CHARS,
    OVERFLOW_CAPTION,
    OVERFLOW_FILENAME,
    DiscordStore,
)
from tgdcfs.errors import MessageNotFound, NoPinnedMessage, TechnicalError
from tgdcfs.reqres import (
    Document,
    FileMessageFromBuffer,
    FileMessageFromStream,
    MessageResp,
)
from tgdcfs.utils.message_cache import channel_cache

CHANNEL = 4242


class FakeBot(DiscordBotAPI):
    """Records what the store asks for and serves it from memory."""

    def __init__(self):
        self.name = "fake"
        self.messages: Dict[int, dict] = {}
        self.next_id = 1000
        self.calls: List[str] = []
        self.fail_next: Dict[str, List[Exception]] = {}
        self.pinned: List[int] = []
        self.deleted: List[List[int]] = []
        self.honour_range = True

    def _maybe_fail(self, method: str) -> None:
        self.calls.append(method)
        if queue := self.fail_next.get(method):
            raise queue.pop(0)

    def _add(self, text: str = "", data: Optional[bytes] = None, name: str = "") -> int:
        mid = self.next_id
        self.next_id += 1
        self.messages[mid] = {"text": text, "data": data, "name": name}
        return mid

    def _resp(self, mid: int) -> MessageResp:
        m = self.messages[mid]
        doc = None
        if m["data"] is not None:
            doc = Document(
                size=len(m["data"]),
                id=mid,
                access_hash=0,
                file_reference=b"",
                mime_type=None,
            )
        return MessageResp(message_id=mid, text=m["text"], document=doc)

    async def send_text(self, channel_id, text):
        self._maybe_fail("send_text")
        return SentMessage(self._add(text=text))

    async def send_file(self, channel_id, data, name, caption=""):
        self._maybe_fail("send_file")
        return SentMessage(self._add(text=caption, data=data, name=name))

    async def edit_text(self, channel_id, message_id, text):
        self._maybe_fail("edit_text")
        if message_id not in self.messages:
            raise MessageNotFound(message_id=message_id)
        self.messages[message_id] = {"text": text, "data": None, "name": ""}
        return message_id

    async def edit_file(self, channel_id, message_id, data, name, caption=None):
        self._maybe_fail("edit_file")
        if message_id not in self.messages:
            raise MessageNotFound(message_id=message_id)
        self.messages[message_id] = {"text": caption or "", "data": data, "name": name}
        return message_id

    async def get_messages(self, channel_id, message_ids):
        self._maybe_fail("get_messages")
        return [
            self._resp(mid) if mid in self.messages else None for mid in message_ids
        ]

    async def get_pinned_messages(self, channel_id):
        self._maybe_fail("get_pinned_messages")
        return [self._resp(mid) for mid in self.pinned if mid in self.messages]

    async def pin_message(self, channel_id, message_id):
        self._maybe_fail("pin_message")
        if message_id not in self.messages:
            raise MessageNotFound(message_id=message_id)
        self.pinned.append(message_id)

    async def delete_messages(self, channel_id, message_ids):
        self._maybe_fail("delete_messages")
        self.deleted.append(list(message_ids))
        for mid in message_ids:
            self.messages.pop(mid, None)

    async def download(self, channel_id, message_id, begin, end):
        self._maybe_fail("download")
        if message_id not in self.messages or self.messages[message_id]["data"] is None:
            raise MessageNotFound(message_id=message_id)
        data = self.messages[message_id]["data"]
        if end < 0 or end >= len(data):
            end = len(data) - 1
        payload = data[begin : end + 1]

        async def chunks() -> AsyncIterator[bytes]:
            for i in range(0, len(payload), 3):
                yield payload[i : i + 3]

        return chunks(), len(payload)

    async def close(self):
        self.calls.append("close")


class RateLimited(Exception):
    status = 429


class Forbidden(Exception):
    status = 403


@pytest.fixture(autouse=True)
def clear_cache():
    channel_cache(CHANNEL).id._lru.clear()
    yield
    channel_cache(CHANNEL).id._lru.clear()


@pytest.fixture
def bot():
    return FakeBot()


@pytest.fixture
def store(bot):
    return DiscordStore(
        [bot], CHANNEL, max_part_bytes=10, retry_interval=0.0, max_retries=3
    )


class TestBasics:
    def test_key_and_caps(self, store):
        assert store.key == "dc:4242"
        assert store.backend == "discord"
        caps = store.caps
        assert caps.max_part_bytes == 10
        assert caps.max_text_chars == MAX_TEXT_CHARS
        assert caps.supports_server_copy is False
        assert caps.delete_age_limit_days == 14

    def test_explicit_key(self, bot):
        assert DiscordStore([bot], CHANNEL, key="dc:custom").key == "dc:custom"

    def test_needs_a_bot(self):
        with pytest.raises(ValueError):
            DiscordStore([], CHANNEL)

    def test_bots_rotate(self):
        a, b = FakeBot(), FakeBot()
        store = DiscordStore([a, b], CHANNEL)
        assert store.next_bot is a
        assert store.next_bot is b
        assert store.next_bot is a


class TestText:
    async def test_short_text_is_sent_as_content(self, store, bot):
        mid = await store.send_text("hello")
        assert bot.messages[mid] == {"text": "hello", "data": None, "name": ""}
        assert (await store.get_messages([mid]))[0].text == "hello"

    async def test_long_text_overflows_into_an_attachment(self, store, bot):
        text = "x" * (MAX_TEXT_CHARS + 1)
        mid = await store.send_text(text)

        stored = bot.messages[mid]
        assert stored["text"] == OVERFLOW_CAPTION
        assert stored["name"] == OVERFLOW_FILENAME
        assert stored["data"] == text.encode()

    async def test_overflow_is_resolved_on_read(self, store, bot):
        text = "y" * (MAX_TEXT_CHARS + 5)
        mid = await store.send_text(text)
        channel_cache(CHANNEL).id._lru.clear()

        message = (await store.get_messages([mid]))[0]

        assert message is not None
        assert message.text == text
        assert message.document is not None  # the attachment stays visible
        assert "download" in bot.calls

    async def test_edit_switches_between_content_and_overflow(self, store, bot):
        mid = await store.send_text("short")

        long = "z" * (MAX_TEXT_CHARS + 1)
        assert await store.edit_message_text(mid, long) == mid
        assert bot.messages[mid]["text"] == OVERFLOW_CAPTION
        assert bot.messages[mid]["data"] == long.encode()

        assert await store.edit_message_text(mid, "short again") == mid
        assert bot.messages[mid] == {"text": "short again", "data": None, "name": ""}

    async def test_edit_of_a_missing_message(self, store):
        with pytest.raises(MessageNotFound):
            await store.edit_message_text(1, "x")

    async def test_get_messages_uses_the_cache(self, store, bot):
        mid = await store.send_text("cached")
        bot.calls.clear()

        assert (await store.get_messages([mid]))[0].text == "cached"
        assert "get_messages" not in bot.calls

    async def test_get_messages_reports_missing_ones(self, store, bot):
        mid = await store.send_text("here")
        assert await store.get_messages([mid, 999]) == [
            MessageResp(message_id=mid, text="here", document=None),
            None,
        ]

    async def test_search_is_unsupported(self, store):
        assert await store.search_messages("x") == []


class TestUpload:
    async def test_partitions_at_the_part_size(self, store, bot):
        data = b"0123456789" * 2 + b"abc"  # 23 bytes -> 10 + 10 + 3
        sent = await store.upload(FileMessageFromBuffer.new(buffer=data, name="f.bin"))

        assert [s.size for s in sent] == [10, 10, 3]
        assert b"".join(bot.messages[s.message_id]["data"] for s in sent) == data
        assert [bot.messages[s.message_id]["name"] for s in sent] == [
            "[part1]f.bin",
            "[part2]f.bin",
            "[part3]f.bin",
        ]

    async def test_exact_multiple(self, store, bot):
        data = b"0123456789" * 2
        sent = await store.upload(FileMessageFromBuffer.new(buffer=data, name="f"))
        assert [s.size for s in sent] == [10, 10]

    async def test_empty_file_is_one_empty_part(self, store, bot):
        sent = await store.upload(FileMessageFromBuffer.new(buffer=b"", name="e"))
        assert [s.size for s in sent] == [0]

    async def test_stream_without_size(self, store, bot):
        async def source():
            yield b"abcdefgh"
            yield b"ijklmnop"
            yield b"q"

        file_msg = FileMessageFromStream.new(stream=source(), size=17, name="s")
        sent = await store.upload(file_msg)
        assert [s.size for s in sent] == [10, 7]
        assert b"".join(bot.messages[s.message_id]["data"] for s in sent) == (
            b"abcdefghijklmnopq"
        )

    async def test_transient_failures_are_retried(self, store, bot):
        bot.fail_next["send_file"] = [RateLimited(), RateLimited()]
        sent = await store.upload(FileMessageFromBuffer.new(buffer=b"data", name="f"))
        assert len(sent) == 1
        assert bot.calls.count("send_file") == 3

    async def test_permanent_failures_are_not_retried(self, store, bot):
        bot.fail_next["send_file"] = [Forbidden()]
        with pytest.raises(Forbidden):
            await store.upload(FileMessageFromBuffer.new(buffer=b"data", name="f"))
        assert bot.calls.count("send_file") == 1

    async def test_gives_up_after_max_retries(self, store, bot):
        bot.fail_next["send_file"] = [RateLimited()] * 5
        with pytest.raises(TechnicalError, match="after 3 attempts"):
            await store.upload(FileMessageFromBuffer.new(buffer=b"data", name="f"))


class TestDownload:
    async def test_ranges(self, store, bot):
        data = b"0123456789abcdef"
        mid = bot._add(data=data, name="f")

        async def read(begin, end):
            resp = await store.download_file(mid, begin, end)
            return b"".join([c async for c in resp.chunks]), resp.size

        assert await read(0, -1) == (data, 16)
        assert await read(3, 7) == (b"34567", 5)
        assert await read(10, 100) == (b"abcdef", 6)

    async def test_missing_document(self, store):
        with pytest.raises(MessageNotFound):
            await store.download_file(1, 0, -1)


class TestReplaceAndCopy:
    async def test_replace_document(self, store, bot):
        mid = bot._add(data=b"old", name="f")
        channel_cache(CHANNEL).id[mid] = bot._resp(mid)

        assert await store.replace_document(mid, b"new!", "g") == mid
        assert bot.messages[mid]["data"] == b"new!"
        cached = channel_cache(CHANNEL).id.get(mid)
        assert cached is not None and cached.document is not None
        assert cached.document.size == 4

    async def test_replace_document_too_large(self, store):
        with pytest.raises(TechnicalError, match="does not fit"):
            await store.replace_document(1, b"x" * 11, "g")

    async def test_copy_within_reuploads(self, store, bot):
        mid = bot._add(data=b"copy me", name="f")
        [new] = await store.copy_within([mid])
        assert new != mid
        assert bot.messages[new]["data"] == b"copy me"

    async def test_copy_from_is_unsupported(self, store):
        assert await store.copy_from(store, [1]) is None


class TestDeleteAndPins:
    async def test_delete_is_gated(self, store, bot):
        mid = bot._add(text="x")
        await store.delete_messages([mid])
        assert mid in bot.messages
        await store.delete_messages([mid], force=True)
        assert mid not in bot.messages

    async def test_delete_when_enabled(self, bot):
        store = DiscordStore([bot], CHANNEL, delete_on_remove=True)
        mid = bot._add(text="x")
        channel_cache(CHANNEL).id[mid] = bot._resp(mid)
        await store.delete_messages([mid, 0, mid])
        assert bot.deleted == [[mid]]
        assert channel_cache(CHANNEL).id.get(mid) is None

    async def test_delete_failures_are_logged_not_raised(self, bot):
        store = DiscordStore([bot], CHANNEL, delete_on_remove=True, max_retries=1)
        bot.fail_next["delete_messages"] = [Forbidden()]
        await store.delete_messages([1])

    async def test_pinned_message(self, store, bot):
        text_mid = bot._add(text="just text")
        doc_mid = bot._add(data=b"{}", name="metadata.json")
        await store.pin_message(text_mid)
        await store.pin_message(doc_mid)

        pinned = await store.get_pinned_message()
        assert pinned.message_id == doc_mid
        assert pinned.document is not None and pinned.document.size == 2

    async def test_overflow_pins_are_not_metadata(self, store, bot):
        mid = bot._add(text=OVERFLOW_CAPTION, data=b"{}", name=OVERFLOW_FILENAME)
        await store.pin_message(mid)
        with pytest.raises(NoPinnedMessage):
            await store.get_pinned_message()

    async def test_no_pins(self, store):
        with pytest.raises(NoPinnedMessage):
            await store.get_pinned_message()

    async def test_pin_missing(self, store):
        with pytest.raises(MessageNotFound):
            await store.pin_message(1)

    async def test_close_closes_the_bots(self, store, bot):
        await store.close()
        assert "close" in bot.calls


class TestTransientClassification:
    def test_rate_limited(self):
        from tgdcfs.backends.discord.client import is_transient

        assert is_transient(discord.RateLimited(1.0))
        assert is_transient(RateLimited())
        assert not is_transient(Forbidden())
        assert is_transient(ConnectionError())
        assert not is_transient(ValueError())


class TestEndToEnd:
    """The store under the real repositories: a Discord-only file system."""

    async def test_upload_read_and_descriptor_overflow(self, bot):
        from tgdcfs.core.api import FileApi, FileDescApi, MetaDataApi
        from tgdcfs.core.model import TGFSDirectory, TGFSMetadata
        from tgdcfs.core.repository.impl import (
            StoreFDRepository,
            StoreFileContentRepository,
        )
        from tgdcfs.core.repository.interface import IMetaDataRepository

        class MemoryMetadata(IMetaDataRepository):
            async def push(self) -> None:
                pass

            async def get(self) -> TGFSMetadata:
                return TGFSMetadata(dir=TGFSDirectory.root_dir())

        # Real message ids are 19-digit snowflakes; 160 of them do not fit
        # a 2000 character message.
        bot.next_id = 1_000_000_000_000_000_000
        store = DiscordStore([bot], CHANNEL, max_part_bytes=64, retry_interval=0.0)
        fc_repo = StoreFileContentRepository(store)
        fd_repo = StoreFDRepository(store)
        metadata_api = MetaDataApi(MemoryMetadata())
        await metadata_api.init()
        file_api = FileApi(metadata_api, FileDescApi(fd_repo, fc_repo), store)
        root = metadata_api.get_root_directory()

        data = bytes(range(256)) * 40  # 10240 bytes -> 160 parts of 64
        await file_api.upload(root, FileMessageFromBuffer.new(buffer=data, name="big"))
        fr = root.find_file("big")

        # 160 snowflake-sized ids do not fit 2000 characters: the
        # descriptor overflowed into an attachment and reads back whole.
        assert bot.messages[fr.message_id]["text"] == OVERFLOW_CAPTION
        fd = await fd_repo.get(fr)
        version = fd.get_latest_version()
        assert len(version.message_ids) == 160
        assert version.store == "dc:4242"

        stream = await file_api.retrieve(fr, 100, 5000, "big")
        assert b"".join([c async for c in stream]) == data[100:5001]
