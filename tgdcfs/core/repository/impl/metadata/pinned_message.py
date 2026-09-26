import json
from typing import AsyncIterator, Dict, Optional

from tgdcfs.backends.base import IStore
from tgdcfs.core.mirror import MirrorGroup
from tgdcfs.core.model import TGFSDirectory, TGFSFileVersion, TGFSMetadata
from tgdcfs.core.repository.interface import IFileContentRepository, IMetaDataRepository
from tgdcfs.errors import (
    MetadataNotInitialized,
    NoPinnedMessage,
    TechnicalError,
)
from tgdcfs.reqres import (
    FileMessageFromBuffer,
    MessageRespWithDocument,
    SentFileMessage,
)


class PinnedMessageMetadataRepository(IMetaDataRepository):
    """The directory tree as a pinned document in the primary store.

    Backend-agnostic; the mirror group keeps a pinned copy in every
    mirror store so any of them can be promoted to primary.
    """

    METADATA_FILE_NAME = "metadata.json"

    def __init__(
        self,
        store: IStore,
        fc_repo: IFileContentRepository,
        mirror_group: Optional[MirrorGroup] = None,
    ):
        super().__init__()

        self._message_api = store
        self._fc_repo = fc_repo
        self._mirror_group = mirror_group

        self._message_id: Optional[int] = None
        # Mirror store key -> message id of the pinned metadata copy there.
        self._mirror_message_ids: Dict[str, int] = {}

    async def push(self) -> None:
        if not self.metadata:
            raise MetadataNotInitialized()

        buffer = json.dumps(self.metadata.to_dict()).encode()
        # The reader below treats the pinned document as a single part, so
        # the blob has to fit one message of the store. That is 2 GiB on
        # Telegram and a few tens of megabytes on Discord.
        if len(buffer) > self._message_api.caps.max_part_bytes:
            raise TechnicalError(
                f"The metadata blob ({len(buffer)} bytes) does not fit one message "
                f"of store {self._message_api.key} "
                f"({self._message_api.caps.max_part_bytes} bytes); use the "
                f"github_repo metadata type for this file system"
            )
        if self._message_id is not None:
            await self._fc_repo.update(
                self._message_id,
                buffer,
                self.METADATA_FILE_NAME,
            )
            # The primary blob was edited in place, so the mirror copies
            # are stale now: forward the fresh blob, pin it, drop the old
            # copy. Keeps every mirror store self-sufficient for a
            # config-level promotion after the primary is lost.
            if self._mirror_group:
                await self._mirror_group.mirror_pinned(
                    self._message_id, self._mirror_message_ids
                )
        else:
            resp = await self._fc_repo.save(
                FileMessageFromBuffer.new(
                    name=self.METADATA_FILE_NAME,
                    buffer=buffer,
                )
            )
            message_id = resp[0].message_id
            await self._message_api.pin_message(message_id=message_id)
            self._message_id = message_id
            # fc_repo.save already mirrored the blob into each mirror
            # channel; adopt those copies (pin them) instead of
            # forwarding a second time.
            if self._mirror_group:
                await self._mirror_group.adopt_pinned(
                    resp[0].mirrors, self._mirror_message_ids
                )

    @staticmethod
    async def _read_all(async_iter: AsyncIterator[bytes]) -> bytes:
        result = bytearray()
        async for chunk in async_iter:
            result.extend(chunk)
        return bytes(result)

    async def new_metadata(self) -> MessageRespWithDocument:
        root = TGFSDirectory.root_dir()
        self.metadata = TGFSMetadata(root)
        self._message_id = None
        await self.push()
        return await self._message_api.get_pinned_message()

    async def get(self) -> TGFSMetadata:
        try:
            pinned_message = await self._message_api.get_pinned_message()
        except NoPinnedMessage:
            pinned_message = await self.new_metadata()

        temp_fv = TGFSFileVersion.from_sent_file_message(
            SentFileMessage(pinned_message.message_id, pinned_message.document.size)
        )
        # A throwaway version around the metadata blob: never cache it.
        temp_fv.cacheable = False

        metadata = TGFSMetadata.from_dict(
            json.loads(
                await self._read_all(
                    await self._fc_repo.get(
                        temp_fv,
                        begin=0,
                        end=-1,
                        name=self.METADATA_FILE_NAME,
                    )
                )
            )
        )

        self._message_id = pinned_message.message_id
        return metadata
