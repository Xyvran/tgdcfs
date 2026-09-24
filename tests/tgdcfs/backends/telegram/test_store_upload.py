"""Upload path of the Telegram store: partitioning and document replacement.

These tests used to target the file content repository; the store owns
the partitioning now.
"""

import pytest

from tgdcfs.backends.telegram.store import (
    PART_SIZE_DEFAULT,
    PART_SIZE_PREMIUM,
    TelegramStore,
)
from tgdcfs.reqres import SentFileMessage, UploadableFileMessage


class MockFileMessage(UploadableFileMessage):
    """Mock file message for testing"""

    def __init__(self, name: str, size: int, caption: str = ""):
        self.name = name
        self.size = size
        self.caption = caption
        self._offset = 0
        self._task_tracker = None
        self.part_names: list[str] = []

    def get_size(self) -> int:
        return self.size

    def next_part(self, size: int):
        self._offset += size

    def file_name(self) -> str:
        return self.name

    async def open(self):
        pass

    async def close(self):
        pass

    async def read(self, size: int) -> bytes:
        return b"mock_content"[:size]


@pytest.fixture
def mock_tdlib(mocker):
    tdlib = mocker.Mock()
    tdlib.account = None  # No account API by default
    tdlib.next_bot = mocker.AsyncMock()
    tdlib.next_bot.get_me = mocker.AsyncMock(return_value=mocker.Mock(name="test_bot"))
    return tdlib


@pytest.fixture
def store(mock_tdlib):
    return TelegramStore(mock_tdlib, 123456789, key="tg:123456789")


@pytest.fixture
def mock_uploader(mocker):
    """Mock file uploader"""
    uploader = mocker.AsyncMock()
    uploader.upload = mocker.AsyncMock(return_value=1000)
    uploader.send = mocker.AsyncMock(
        return_value=SentFileMessage(message_id=12345, size=1000)
    )
    uploader.get_uploaded_file = mocker.Mock(return_value=mocker.Mock(name="test.txt"))
    uploader.client = mocker.AsyncMock()

    mock_response = mocker.Mock()
    mock_response.message_id = 54321
    uploader.client.edit_message_media = mocker.AsyncMock(return_value=mock_response)

    mocker.patch("tgdcfs.backends.telegram.store.FileUploader", return_value=uploader)
    return uploader


class TestCapabilities:
    def test_key_and_backend(self, store):
        assert store.key == "tg:123456789"
        assert store.backend == "telegram"

    def test_key_defaults_to_the_channel(self, mock_tdlib):
        assert TelegramStore(mock_tdlib, -100123).key == "tg:-100123"

    def test_bot_part_size(self, store):
        caps = store.caps
        assert caps.max_part_bytes == PART_SIZE_DEFAULT
        assert caps.supports_server_copy is True

    def test_premium_part_size_needs_an_account(self, mock_tdlib, mocker):
        assert (
            TelegramStore(mock_tdlib, 1, premium_upload=True).caps.max_part_bytes
            == PART_SIZE_DEFAULT
        )
        mock_tdlib.account = mocker.Mock()
        assert (
            TelegramStore(mock_tdlib, 1, premium_upload=True).caps.max_part_bytes
            == PART_SIZE_PREMIUM
        )


class TestPartition:
    def test_partition_single_part(self):
        size = 500 * 1024 * 1024  # 500MB
        part_size = 2 * 1024 * 1024 * 1024  # 2GB default part size
        parts = list(TelegramStore._partition(size, part_size))
        assert parts == [size]

    def test_partition_multiple_parts(self):
        size = int(2.5 * 1024 * 1024 * 1024)  # 2.5GB
        part_size = 1 * 1024 * 1024 * 1024  # 1GB part size for this test
        parts = list(TelegramStore._partition(size, part_size))

        expected_last_part = int(0.5 * 1024 * 1024 * 1024)
        assert parts == [part_size, part_size, expected_last_part]

    def test_partition_exact_multiple(self):
        part_size = 1 * 1024 * 1024 * 1024
        parts = list(TelegramStore._partition(3 * part_size, part_size))
        assert parts == [part_size] * 3

    def test_partition_edge_cases(self):
        part_size = 1024 * 1024 * 1024  # 1GB

        # Zero size: the last-part calculation yields a full part.
        assert list(TelegramStore._partition(0, part_size)) == [part_size]
        assert list(TelegramStore._partition(1, part_size)) == [1]
        assert list(TelegramStore._partition(part_size, part_size)) == [part_size]


class TestUpload:
    @pytest.mark.asyncio
    async def test_upload_single_part_file(self, store, mock_uploader):
        file_msg = MockFileMessage("test.txt", 500 * 1024 * 1024)  # 500MB

        result = await store.upload(file_msg)

        assert len(result) == 1
        assert result[0].message_id == 12345
        assert result[0].size == 1000
        mock_uploader.upload.assert_awaited_once()
        mock_uploader.send.assert_awaited_once_with(123456789)

    @pytest.mark.asyncio
    async def test_upload_multiple_part_file(self, store, mock_uploader):
        large_size = int(2.5 * 1024 * 1024 * 1024)  # 2.5GB -> 2 parts of 2GB
        file_msg = MockFileMessage("large_file.bin", large_size)

        result = await store.upload(file_msg)

        assert len(result) == 2
        assert all(r.size == 1000 for r in result)
        assert mock_uploader.upload.call_count == 2

    @pytest.mark.asyncio
    async def test_upload_names_the_parts(self, store, mock_uploader, mocker):
        file_msg = MockFileMessage("document.pdf", int(2.1 * 1024 * 1024 * 1024))
        seen = []

        def build_uploader(api, msg):
            seen.append(msg.name)
            return mock_uploader

        uploader_cls = mocker.patch(
            "tgdcfs.backends.telegram.store.FileUploader", side_effect=build_uploader
        )

        await store.upload(file_msg)

        assert uploader_cls.call_count == 2
        assert seen == ["[part1]document.pdf", "[part2]document.pdf"]

    @pytest.mark.asyncio
    async def test_upload_failure_propagates(self, store, mock_uploader):
        mock_uploader.upload.side_effect = Exception("Upload failed")

        with pytest.raises(Exception, match="Upload failed"):
            await store.upload(MockFileMessage("test.txt", 100))

    @pytest.mark.asyncio
    async def test_upload_unnamed_file(self, store, mock_uploader):
        result = await store.upload(MockFileMessage("", 100))

        assert len(result) == 1
        mock_uploader.upload.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_big_files_use_the_premium_account(
        self, mock_tdlib, mock_uploader, mocker
    ):
        mock_tdlib.account = mocker.AsyncMock()
        mock_tdlib.account.get_me = mocker.AsyncMock(
            return_value=mocker.Mock(name="premium")
        )
        store = TelegramStore(mock_tdlib, 1, premium_upload=True)
        uploader_cls = mocker.patch(
            "tgdcfs.backends.telegram.store.FileUploader", return_value=mock_uploader
        )

        result = await store.upload(MockFileMessage("big.bin", PART_SIZE_DEFAULT + 1))

        # One 4 GiB part through the account instead of two bot parts.
        assert len(result) == 1
        assert uploader_cls.call_args[0][0] is mock_tdlib.account

    @pytest.mark.asyncio
    async def test_small_files_stay_on_the_bots(
        self, mock_tdlib, mock_uploader, mocker
    ):
        mock_tdlib.account = mocker.AsyncMock()
        store = TelegramStore(mock_tdlib, 1, premium_upload=True)
        uploader_cls = mocker.patch(
            "tgdcfs.backends.telegram.store.FileUploader", return_value=mock_uploader
        )

        await store.upload(MockFileMessage("small.bin", 100))

        assert uploader_cls.call_args[0][0] is mock_tdlib.next_bot


class TestReplaceDocument:
    @pytest.mark.asyncio
    async def test_replace_document(self, store, mock_uploader):
        result = await store.replace_document(54321, b"updated content", "updated.txt")

        assert result == 54321
        mock_uploader.upload.assert_awaited_once()
        req = mock_uploader.client.edit_message_media.call_args[0][0]
        assert req.chat == 123456789
        assert req.message_id == 54321

    @pytest.mark.asyncio
    async def test_replace_document_builds_a_buffer_message(
        self, store, mock_uploader, mocker
    ):
        mock_from_buffer = mocker.patch(
            "tgdcfs.backends.telegram.store.FileMessageFromBuffer.new"
        )

        await store.replace_document(12345, b"test data", "test.bin")

        mock_from_buffer.assert_called_once_with(buffer=b"test data", name="test.bin")
