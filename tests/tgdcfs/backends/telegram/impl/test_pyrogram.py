import asyncio
from unittest.mock import AsyncMock, Mock

import pytest
from pyrogram import Client, file_id
from pyrogram import enums as e
from pyrogram import types as t

from tgdcfs.errors import TechnicalError
from tgdcfs.reqres import DownloadFileReq, EditMessageTextReq, SendTextReq
from tgdcfs.backends.telegram.impl.pyrogram import GET_FILE_CHUNK_SIZE, PyrogramAPI


class TestPyrogramDownload:
    @pytest.fixture
    def mock_client(self, mocker) -> AsyncMock:
        client = mocker.AsyncMock(spec=Client)
        client.get_messages = mocker.AsyncMock()
        return client

    @pytest.fixture
    def api(self, mock_client) -> PyrogramAPI:
        return PyrogramAPI(mock_client)

    @pytest.fixture
    def document_message(self, mocker):
        mocker.patch.object(file_id.FileId, "decode", return_value=Mock())
        message = mocker.Mock(spec=t.Message)
        message.id = 54321
        message.document = mocker.Mock(file_id="encoded")
        return message

    @staticmethod
    def _serve(api, calls, *chunks):
        def get_file(**kwargs):
            calls.append(kwargs)

            async def gen():
                for chunk in chunks:
                    yield chunk

            return gen()

        api._client.get_file = get_file

    @pytest.mark.asyncio
    async def test_offset_is_counted_in_chunks(self, api, document_message):
        """``get_file`` takes its offset in 1 MiB chunks, so a byte offset
        is converted and the bytes before it, served first, are dropped."""
        api._client.get_messages.return_value = document_message
        calls: list[dict] = []
        self._serve(api, calls, b"ab", b"cd", b"efgh", b"ijkl")

        begin = 3 * GET_FILE_CHUNK_SIZE + 3
        resp = await api.download_file(
            DownloadFileReq(chat=1, message_id=54321, begin=begin, end=begin + 6)
        )
        chunks = [chunk async for chunk in resp.chunks]

        assert calls[0]["offset"] == 3
        assert chunks == [b"d", b"efgh", b"ij"]

    @pytest.mark.asyncio
    async def test_fails_when_the_stream_ends_early(self, api, document_message):
        api._client.get_messages.return_value = document_message
        self._serve(api, [], b"12345678")

        resp = await api.download_file(
            DownloadFileReq(chat=1, message_id=54321, begin=0, end=99)
        )

        with pytest.raises(TechnicalError, match="ended after 8 of 100 bytes"):
            async for _ in resp.chunks:
                pass

    @pytest.mark.asyncio
    async def test_restarts_a_stalled_stream(self, api, document_message, mocker):
        """The restart asks for the chunk holding the byte reached and drops
        what was already served of it."""
        mocker.patch(
            "tgdcfs.backends.telegram.impl.pyrogram.stall_limits",
            return_value=(0.05, 3),
        )
        api._client.get_messages.return_value = document_message
        calls: list[dict] = []

        def get_file(**kwargs):
            calls.append(kwargs)

            async def gen():
                if len(calls) == 1:
                    yield b"abc"
                    await asyncio.Event().wait()
                yield b"abcdef"

            return gen()

        api._client.get_file = get_file

        resp = await api.download_file(
            DownloadFileReq(chat=1, message_id=54321, begin=0, end=5)
        )
        chunks = [chunk async for chunk in resp.chunks]

        assert chunks == [b"abc", b"def"]
        assert [c["offset"] for c in calls] == [0, 0]

    @pytest.mark.asyncio
    async def test_fails_after_repeated_stalls(self, api, document_message, mocker):
        mocker.patch(
            "tgdcfs.backends.telegram.impl.pyrogram.stall_limits",
            return_value=(0.01, 1),
        )
        api._client.get_messages.return_value = document_message

        def get_file(**_kwargs):
            async def gen():
                await asyncio.Event().wait()
                yield b""  # pragma: no cover - never reached

            return gen()

        api._client.get_file = get_file

        resp = await api.download_file(
            DownloadFileReq(chat=1, message_id=54321, begin=0, end=5)
        )

        with pytest.raises(TechnicalError, match="stalled 2 times"):
            async for _ in resp.chunks:
                pass


class TestPyrogramText:
    """Descriptor JSON must reach Telegram verbatim: base64url replica ids
    contain "__", which markdown parsing would strip."""

    @pytest.fixture
    def mock_client(self, mocker) -> AsyncMock:
        client = mocker.AsyncMock(spec=Client)
        client.send_message.return_value = Mock(id=99999)
        client.edit_message_text.return_value = Mock(id=88888)
        return client

    @pytest.fixture
    def api(self, mock_client) -> PyrogramAPI:
        return PyrogramAPI(mock_client)

    @pytest.mark.asyncio
    async def test_send_text_disables_parsing(self, api, mock_client):
        text = '{"mb": "AB__CD__EF"}'
        result = await api.send_text(SendTextReq(chat=-100123, text=text))

        mock_client.send_message.assert_called_once_with(
            chat_id=-100123, text=text, parse_mode=e.ParseMode.DISABLED
        )
        assert result.message_id == 99999

    @pytest.mark.asyncio
    async def test_edit_message_text_disables_parsing(self, api, mock_client):
        text = '{"mb": "AB__CD__EF"}'
        result = await api.edit_message_text(
            EditMessageTextReq(chat=-100123, message_id=12345, text=text)
        )

        mock_client.edit_message_text.assert_called_once_with(
            chat_id=-100123,
            message_id=12345,
            text=text,
            parse_mode=e.ParseMode.DISABLED,
        )
        assert result.message_id == 88888
