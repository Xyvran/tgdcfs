"""DiscordBotAPI against a mocked discord.py client and CDN."""

from datetime import datetime, timedelta, timezone
from typing import AsyncIterator, List
from unittest.mock import AsyncMock, MagicMock, Mock

import discord
import pytest

from tgdcfs.backends.discord.client import DiscordBotAPI
from tgdcfs.errors import MessageNotFound, TechnicalError

CHANNEL = 77
DISCORD_EPOCH = 1420070400000


def snowflake(at: datetime) -> int:
    ms = int(at.timestamp() * 1000) - DISCORD_EPOCH
    return ms << 22


def make_attachment(size: int, url: str = "https://cdn.example/a?ex=1") -> Mock:
    att = Mock()
    att.size = size
    att.url = url
    att.id = 5
    att.filename = "f.bin"
    att.content_type = "application/octet-stream"
    return att


def make_message(message_id: int, content: str = "", attachments=None) -> Mock:
    message = Mock()
    message.id = message_id
    message.content = content
    message.attachments = attachments or []
    message.edit = AsyncMock()
    message.pin = AsyncMock()
    return message


@pytest.fixture
def channel():
    channel = Mock()
    channel.send = AsyncMock(return_value=make_message(1))
    channel.fetch_message = AsyncMock()
    channel.delete_messages = AsyncMock()
    partial = Mock()
    partial.delete = AsyncMock()
    channel.get_partial_message = Mock(return_value=partial)
    return channel


@pytest.fixture
def api(channel):
    bot = Mock()
    bot.get_channel = Mock(return_value=channel)
    bot.close = AsyncMock()
    bot.user = Mock()
    bot.user.name = "bot"
    return DiscordBotAPI(bot)


class FakeResponse:
    def __init__(self, status: int, body: bytes, chunk: int = 4):
        self.status = status
        self._body = body
        self._chunk = chunk
        self.closed = False
        self.content = self

    async def iter_chunked(self, _: int) -> AsyncIterator[bytes]:
        for i in range(0, len(self._body), self._chunk):
            yield self._body[i : i + self._chunk]

    def close(self):
        self.closed = True


class TestMessages:
    async def test_send_text(self, api, channel):
        channel.send.return_value = make_message(11)
        assert (await api.send_text(CHANNEL, "hi")).message_id == 11
        channel.send.assert_awaited_once_with("hi")

    async def test_send_file(self, api, channel):
        channel.send.return_value = make_message(12)
        sent = await api.send_file(CHANNEL, b"data", "f.bin", caption="cap")
        assert sent.message_id == 12
        kwargs = channel.send.call_args.kwargs
        assert kwargs["content"] == "cap"
        assert isinstance(kwargs["file"], discord.File)
        assert kwargs["file"].filename == "f.bin"

    async def test_send_file_without_caption(self, api, channel):
        await api.send_file(CHANNEL, b"data", "f.bin")
        assert channel.send.call_args.kwargs["content"] is None

    async def test_edit_text_drops_attachments(self, api, channel):
        message = make_message(13)
        channel.fetch_message.return_value = message
        assert await api.edit_text(CHANNEL, 13, "new") == 13
        message.edit.assert_awaited_once_with(content="new", attachments=[])

    async def test_edit_file(self, api, channel):
        message = make_message(14)
        channel.fetch_message.return_value = message
        await api.edit_file(CHANNEL, 14, b"x", "o.json", caption="cap")
        kwargs = message.edit.call_args.kwargs
        assert kwargs["content"] == "cap"
        assert kwargs["attachments"][0].filename == "o.json"

    async def test_missing_message(self, api, channel):
        channel.fetch_message.side_effect = discord.NotFound(Mock(status=404), "nope")
        with pytest.raises(MessageNotFound):
            await api.edit_text(CHANNEL, 1, "x")
        assert await api.get_message(CHANNEL, 1) is None

    async def test_get_messages_maps_attachments(self, api, channel):
        channel.fetch_message.side_effect = [
            make_message(1, "text", [make_attachment(9)]),
            make_message(2, "plain"),
        ]
        first, second = await api.get_messages(CHANNEL, [1, 2])
        assert first.document.size == 9
        assert first.text == "text"
        assert second.document is None

    async def test_channel_lookup_falls_back_to_fetch(self, channel):
        bot = Mock()
        bot.get_channel = Mock(return_value=None)
        bot.fetch_channel = AsyncMock(return_value=channel)
        api = DiscordBotAPI(bot)
        channel.send.return_value = make_message(3)
        await api.send_text(CHANNEL, "x")
        bot.fetch_channel.assert_awaited_once_with(CHANNEL)

    async def test_unknown_channel(self):
        bot = Mock()
        bot.get_channel = Mock(return_value=None)
        bot.fetch_channel = AsyncMock(
            side_effect=discord.NotFound(Mock(status=404), "nope")
        )
        with pytest.raises(TechnicalError, match="not found"):
            await DiscordBotAPI(bot).send_text(CHANNEL, "x")

    async def test_pins_iterate(self, api, channel):
        async def pins():
            yield make_message(1, "a")
            yield make_message(2, "b", [make_attachment(3)])

        channel.pins = Mock(return_value=pins())
        result = await api.get_pinned_messages(CHANNEL)
        assert [m.message_id for m in result] == [1, 2]
        assert result[1].document.size == 3

    async def test_pin(self, api, channel):
        message = make_message(4)
        channel.fetch_message.return_value = message
        await api.pin_message(CHANNEL, 4)
        message.pin.assert_awaited_once()


class TestDelete:
    async def test_recent_messages_are_bulk_deleted(self, api, channel):
        now = datetime.now(timezone.utc)
        recent = [
            snowflake(now - timedelta(days=1)),
            snowflake(now - timedelta(days=2)),
        ]
        await api.delete_messages(CHANNEL, recent)
        channel.delete_messages.assert_awaited_once()
        ids = [obj.id for obj in channel.delete_messages.call_args.args[0]]
        assert ids == recent

    async def test_old_messages_are_deleted_one_by_one(self, api, channel):
        now = datetime.now(timezone.utc)
        old = [snowflake(now - timedelta(days=20)), snowflake(now - timedelta(days=15))]
        await api.delete_messages(CHANNEL, old)
        channel.delete_messages.assert_not_awaited()
        assert channel.get_partial_message.call_count == 2

    async def test_single_recent_message_is_not_bulk_deleted(self, api, channel):
        now = datetime.now(timezone.utc)
        await api.delete_messages(CHANNEL, [snowflake(now)])
        channel.delete_messages.assert_not_awaited()
        assert channel.get_partial_message.call_count == 1

    async def test_bulk_batches_of_100(self, api, channel):
        now = datetime.now(timezone.utc)
        ids = [snowflake(now - timedelta(seconds=i)) for i in range(150)]
        await api.delete_messages(CHANNEL, ids)
        assert channel.delete_messages.await_count == 2

    async def test_bulk_not_found_falls_back_to_single_deletes(self, api, channel):
        now = datetime.now(timezone.utc)
        ids = [snowflake(now), snowflake(now - timedelta(seconds=1))]
        channel.delete_messages.side_effect = discord.NotFound(Mock(status=404), "gone")
        await api.delete_messages(CHANNEL, ids)
        assert channel.get_partial_message.call_count == 2

    async def test_single_delete_of_a_gone_message(self, api, channel):
        channel.get_partial_message.return_value.delete.side_effect = discord.NotFound(
            Mock(status=404), "gone"
        )
        await api.delete_messages(CHANNEL, [snowflake(datetime.now(timezone.utc))])


class TestDownload:
    @pytest.fixture
    def session(self, api, mocker):
        session = Mock()
        session.closed = False
        session.get = AsyncMock()
        mocker.patch.object(api, "_session", AsyncMock(return_value=session))
        return session

    async def collect(self, chunks) -> bytes:
        return b"".join([c async for c in chunks])

    async def test_full_download(self, api, channel, session):
        body = b"0123456789"
        channel.fetch_message.return_value = make_message(1, "", [make_attachment(10)])
        session.get.return_value = FakeResponse(200, body)

        chunks, size = await api.download(CHANNEL, 1, 0, -1)

        assert size == 10
        assert await self.collect(chunks) == body
        assert "Range" not in session.get.call_args.kwargs["headers"]

    async def test_range_honoured(self, api, channel, session):
        channel.fetch_message.return_value = make_message(1, "", [make_attachment(10)])
        session.get.return_value = FakeResponse(206, b"2345")

        chunks, size = await api.download(CHANNEL, 1, 2, 5)

        assert size == 4
        assert await self.collect(chunks) == b"2345"
        assert session.get.call_args.kwargs["headers"]["Range"] == "bytes=2-5"

    async def test_range_ignored_is_sliced(self, api, channel, session):
        channel.fetch_message.return_value = make_message(1, "", [make_attachment(10)])
        response = FakeResponse(200, b"0123456789", chunk=3)
        session.get.return_value = response

        chunks, size = await api.download(CHANNEL, 1, 2, 7)

        assert size == 6
        assert await self.collect(chunks) == b"234567"
        assert response.closed

    async def test_end_is_clamped(self, api, channel, session):
        channel.fetch_message.return_value = make_message(1, "", [make_attachment(10)])
        session.get.return_value = FakeResponse(206, b"89")
        _, size = await api.download(CHANNEL, 1, 8, 100)
        assert size == 2
        assert session.get.call_args.kwargs["headers"]["Range"] == "bytes=8-9"

    async def test_cdn_error(self, api, channel, session):
        channel.fetch_message.return_value = make_message(1, "", [make_attachment(10)])
        session.get.return_value = FakeResponse(403, b"")
        with pytest.raises(TechnicalError, match="403"):
            await api.download(CHANNEL, 1, 0, -1)

    async def test_no_attachment(self, api, channel, session):
        channel.fetch_message.return_value = make_message(1, "text only")
        with pytest.raises(TechnicalError, match="no attachment"):
            await api.download(CHANNEL, 1, 0, -1)

    async def test_bad_range(self, api, channel, session):
        channel.fetch_message.return_value = make_message(1, "", [make_attachment(10)])
        with pytest.raises(TechnicalError, match="Invalid range"):
            await api.download(CHANNEL, 1, 5, 2)

    async def test_url_is_taken_from_the_fresh_message(self, api, channel, session):
        # Signed CDN URLs expire; the download must use the URL of the
        # message it just fetched, never a stored one.
        channel.fetch_message.return_value = make_message(
            1, "", [make_attachment(4, url="https://cdn.example/fresh?ex=2")]
        )
        session.get.return_value = FakeResponse(200, b"abcd")
        await api.download(CHANNEL, 1, 0, -1)
        assert session.get.call_args.args[0] == "https://cdn.example/fresh?ex=2"


class TestClose:
    async def test_close(self, api):
        await api.close()
        api._bot.close.assert_awaited_once()
