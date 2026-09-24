import logging
from typing import Generator, List, Optional

from tgdcfs.backends.base import IStore
from tgdcfs.core.mirror import MirrorGroup
from tgdcfs.core.model import TGFSFileVersion
from tgdcfs.core.repository.interface import IFileContentRepository
from tgdcfs.errors import TechnicalError
from tgdcfs.reqres import (
    FileContent,
    SentFileMessage,
    UploadableFileMessage,
)
from tgdcfs.utils.prefetching_chain import prefetching_chain

logger = logging.getLogger(__name__)

# How many parts of one file are fetched at a time. Each part may itself
# be split across bots, so this multiplies with the per-message
# concurrency -- keep it small.
PART_PREFETCH_CONCURRENCY = 2


class StoreFileContentRepository(IFileContentRepository):
    """File content as part messages in a store, mirrored into others.

    Backend-agnostic: the store partitions and moves the bytes, this
    class maps byte ranges onto parts and fails reads over to mirror
    copies.
    """

    def __init__(
        self,
        store: IStore,
        mirror_group: Optional[MirrorGroup] = None,
    ):
        self._store = store
        self._mirror_group = mirror_group

    async def save(self, file_msg: UploadableFileMessage) -> List[SentFileMessage]:
        res = await self._store.upload(file_msg)

        # Replicate the freshly uploaded parts into the mirror stores. The
        # mapping travels back to the caller inside the SentFileMessage
        # objects and ends up in TGFSFileVersion.mirrors.
        if self._mirror_group:
            mirror_map = await self._mirror_group.mirror_parts(
                [m.message_id for m in res]
            )
            for store_key, mirror_ids in mirror_map.items():
                for sent, mirror_id in zip(res, mirror_ids):
                    sent.mirrors[store_key] = mirror_id
        return res

    async def update(self, message_id: int, buffer: bytes, name: str) -> int:
        return await self._store.replace_document(message_id, buffer, name)

    @staticmethod
    def _get_file_part_to_download(
        fv: TGFSFileVersion, begin: int, end: int
    ) -> Generator[tuple[int, int, int]]:
        for (
            _,
            message_id,
            part_begin,
            part_end,
        ) in StoreFileContentRepository._get_file_parts_indexed(fv, begin, end):
            yield message_id, part_begin, part_end

    @staticmethod
    def _get_file_parts_indexed(
        fv: TGFSFileVersion, begin: int, end: int
    ) -> Generator[tuple[int, int, int, int]]:
        """Map the inclusive byte range ``[begin, end]`` onto the parts.

        Yields ``(part index, message id, begin, end)`` with the bounds
        relative to the part and, like the input, inclusive. ``end < 0``
        means the end of the file, and an ``end`` past it is clamped there:
        clients do ask past the end, and the body then stops at the last
        byte that exists. A part is never asked for more bytes than it
        holds: that could only be served by a download stopping short,
        which the backends reject.
        """
        if fv.size <= 0:
            return
        if end < 0 or end >= fv.size:
            end = fv.size - 1
        if begin < 0:
            raise TechnicalError(
                f"Invalid begin value {begin} for file version {fv.id} with size {fv.size}"
            )
        if begin > end:
            raise TechnicalError(
                f"Invalid range: begin {begin} is greater than end {end} for file version {fv.id}"
            )

        offset = 0
        i_part = 0

        while i_part < len(fv.part_sizes) and offset + fv.part_sizes[i_part] <= begin:
            offset += fv.part_sizes[i_part]
            i_part += 1

        if i_part >= len(fv.part_sizes):
            raise TechnicalError(
                f"Begin offset {begin} exceeds total file size {fv.size} for file version {fv.id}"
            )

        while i_part < len(fv.part_sizes) and offset <= end:
            part_size = fv.part_sizes[i_part]
            part_begin = max(0, begin - offset)
            part_end = min(part_size - 1, end - offset)
            if part_begin <= part_end:
                yield i_part, fv.message_ids[i_part], part_begin, part_end
            offset += part_size
            i_part += 1

    def _part_sources(
        self, fv: TGFSFileVersion, part_idx: int, message_id: int
    ) -> List[tuple[Optional[str], int]]:
        """Download sources for one part: the primary plus every mirror
        store that holds a copy. ``None`` denotes the primary store.

        While the primary is marked dead (circuit breaker), mirrors are
        tried first so each read does not pay a doomed primary RPC; the
        primary stays in the list as the source of last resort.
        """
        # A version relocated from a former primary has no copy here yet:
        # its ids name that store, which is one of the mirrors now.
        sources: List[tuple[Optional[str], int]] = (
            [(None, message_id)] if fv.owned_by(self._store.key) else []
        )
        if self._mirror_group:
            for store_key, mirror_ids in (fv.mirrors or {}).items():
                if (
                    self._mirror_group.store_for(store_key)
                    and part_idx < len(mirror_ids)
                    and mirror_ids[part_idx] > 0
                ):
                    sources.append((store_key, mirror_ids[part_idx]))
            if self._mirror_group.primary_dead() and len(sources) > 1:
                sources = sources[1:] + sources[:1]
        return sources

    async def _download_part(
        self, fv: TGFSFileVersion, part_idx: int, message_id: int, begin: int, end: int
    ):
        """Stream one part, failing over to mirror copies on error.

        Failover also works mid-stream: bytes already delivered are
        skipped by advancing ``begin`` before retrying the next source.
        """
        sources = self._part_sources(fv, part_idx, message_id)
        served = 0
        last_ex: Optional[Exception] = None
        for store_key, mid in sources:
            store = (
                self._store
                if store_key is None
                else self._mirror_group.store_for(store_key)  # type: ignore[union-attr]
            )
            if store is None:
                continue
            try:
                resp = await store.download_file(mid, begin + served, end)
                async for chunk in resp.chunks:
                    yield chunk
                    served += len(chunk)
                return
            except Exception as ex:
                last_ex = ex
                if store_key is None and self._mirror_group:
                    self._mirror_group.mark_primary_dead()
                if len(sources) > 1:
                    logger.warning(
                        f"Downloading part {part_idx} (message {mid}) from "
                        f"{'primary' if store_key is None else f'mirror {store_key}'} "
                        f"failed: {ex}. Trying next source."
                    )
        if last_ex:
            raise last_ex
        raise TechnicalError(
            f"No download source available for part {part_idx} of {fv.id}"
        )

    async def get(
        self, fv: TGFSFileVersion, begin: int, end: int, name: str
    ) -> FileContent:
        logger.info(f"Retrieving file content for {name}@{fv.id} from {begin} to {end}")

        parts = [
            self._download_part(fv, part_idx, message_id, part_begin, part_end)
            for part_idx, message_id, part_begin, part_end in (
                self._get_file_parts_indexed(fv, begin, end)
            )
        ]
        return prefetching_chain(parts, concurrency=PART_PREFETCH_CONCURRENCY)
