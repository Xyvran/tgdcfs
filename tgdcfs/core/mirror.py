"""Redundancy: RAID-1-style mirroring of a file system's primary store.

A :class:`MirrorGroup` binds the primary :class:`IStore` to one store
per mirror and provides the write-side primitives used by the
repositories:

* ``mirror_parts`` copies content part messages into every mirror
  store -- server-side when the pair of stores supports it (Telegram
  forwarding, no re-upload bandwidth) or, otherwise, by streaming the
  part down from the primary and uploading it into the mirror.
* ``mirror_fd`` keeps a copy of a file descriptor (JSON text message)
  in each mirror store. Descriptors are *sent* rather than copied
  because a forwarded message cannot be edited later, and descriptors
  are edited on every new file version.
* ``mirror_pinned`` maintains a copy of the pinned metadata document.
* ``delete`` fans message deletion out to the mirror stores.

Copy strategy (``mode``): ``auto`` (default) tries the server-side copy
and falls back to re-uploading; ``forward`` insists on the server-side
copy and fails otherwise; ``reupload`` never asks for one (for channels
with "restrict saving content", where forwarding is refused).

Failure policy: with ``strict=False`` (default) a failing mirror write
is logged and the affected store is simply omitted from the result --
the primary write has already succeeded and the backfill task can close
the gap later. With ``strict=True`` the error propagates.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from typing import Dict, List, Literal, Optional

from tgdcfs.backends.base import IStore
from tgdcfs.errors import MessageNotFound, TechnicalError
from tgdcfs.reqres import FileMessageFromStream

logger = logging.getLogger(__name__)

MirrorMode = Literal["auto", "forward", "reupload"]
MIRROR_MODES: tuple[MirrorMode, ...] = ("auto", "forward", "reupload")

# How long reads prefer the mirrors after a primary-store failure.
PRIMARY_DEAD_SECONDS = 60.0


@dataclass
class MirrorStore:
    # Serialization key of the store (``tg:<channel>``) -- the key used in
    # the metadata, NOT the lib-specific resolved id and not the config
    # name, which may be renamed.
    key: str
    store: IStore


class MirrorGroup:
    def __init__(
        self,
        primary: IStore,
        stores: List[MirrorStore],
        mode: MirrorMode = "auto",
        strict: bool = False,
    ):
        if mode not in MIRROR_MODES:
            raise ValueError(f"Unknown mirror mode: {mode}")
        self._primary = primary
        self._stores = stores
        self._mode = mode
        self._strict = strict
        self._by_key = {ch.key: ch for ch in stores}
        self._primary_dead_until = 0.0

    @property
    def mode(self) -> MirrorMode:
        return self._mode

    @property
    def strict(self) -> bool:
        return self._strict

    @property
    def store_keys(self) -> List[str]:
        return [ch.key for ch in self._stores]

    def store_for(self, key: str) -> Optional[IStore]:
        ch = self._by_key.get(key)
        return ch.store if ch else None

    # -- primary health (read-path circuit breaker) ------------------------

    def mark_primary_dead(self) -> None:
        self._primary_dead_until = time.monotonic() + PRIMARY_DEAD_SECONDS

    def primary_dead(self) -> bool:
        return time.monotonic() < self._primary_dead_until

    # -- content parts -----------------------------------------------------

    def missing_stores(self, mirrors: Dict[str, List[int]], n_parts: int) -> List[str]:
        """Mirror stores for which a version has no complete copy."""
        res = []
        for key in self.store_keys:
            ids = mirrors.get(key)
            if not ids or len(ids) != n_parts or any(mid <= 0 for mid in ids):
                res.append(key)
        return res

    async def mirror_parts(
        self,
        message_ids: List[int],
        only_stores: Optional[List[str]] = None,
    ) -> Dict[str, List[int]]:
        """Copy the given primary part messages into the mirror stores.

        Returns ``{store_key: [mirror message ids]}`` for every store
        that succeeded, aligned with ``message_ids``.
        """
        res: Dict[str, List[int]] = {}
        for ch in self._stores:
            if only_stores is not None and ch.key not in only_stores:
                continue
            try:
                res[ch.key] = await self._copy_to(ch, message_ids)
            except Exception as ex:
                self._handle_write_error(ch.key, "content parts", ex)
        return res

    async def _copy_to(self, target: MirrorStore, message_ids: List[int]) -> List[int]:
        """Copy primary messages into ``target`` according to ``mode``."""
        if not message_ids:
            return []
        if self._mode != "reupload":
            try:
                ids = await target.store.copy_from(self._primary, message_ids)
            except Exception as ex:
                if self._mode == "forward":
                    raise
                logger.warning(
                    f"Server-side copy into store {target.key} failed ({ex}); "
                    f"falling back to re-upload"
                )
                ids = None
            if ids is not None:
                return ids
            if self._mode == "forward":
                raise TechnicalError(
                    f"Store {target.key} cannot receive server-side copies from "
                    f"{self._primary.key}; use mode 'auto' or 'reupload'"
                )
        return [await self._reupload_one(target, mid) for mid in message_ids]

    async def _reupload_one(self, target: MirrorStore, message_id: int) -> int:
        """Bandwidth-bound fallback: stream a part down and up again.

        Bytes are copied verbatim -- this sits below the encryption
        decorator, so ciphertext stays ciphertext. The target partitions
        the bytes itself; until replicas with their own part layout exist
        (see the architecture plan), a part must land in exactly one
        message of the target, which holds whenever the target's part
        limit is at least the primary's.
        """
        message = (await self._primary.get_messages([message_id]))[0]
        if not message or not message.document:
            raise MessageNotFound(message_id=message_id)
        size = message.document.size
        resp = await self._primary.download_file(message_id, 0, size - 1)
        sent = await target.store.upload(
            FileMessageFromStream.new(
                stream=resp.chunks, size=size, name=f"part-{message_id}"
            )
        )
        if len(sent) != 1:
            raise TechnicalError(
                f"Store {target.key} split the {size}-byte part {message_id} into "
                f"{len(sent)} messages; mirrors with a different part layout are "
                f"not supported yet"
            )
        return sent[0].message_id

    # -- file descriptors --------------------------------------------------

    async def mirror_fd(
        self, text: str, existing: Optional[Dict[str, int]] = None
    ) -> Dict[str, int]:
        """Send or update the FD text message in every mirror store.

        Returns the map of current FD message ids per store: existing
        entries merged with this round's successful writes, so a store
        that fails transiently keeps its (stale but recoverable) copy.
        """
        res: Dict[str, int] = dict(existing or {})
        for ch in self._stores:
            try:
                if mid := res.get(ch.key):
                    try:
                        res[ch.key] = await ch.store.edit_message_text(
                            message_id=mid, message=text
                        )
                        continue
                    except MessageNotFound:
                        logger.warning(
                            f"FD mirror message {mid} in store {ch.key} is "
                            f"gone, sending a fresh copy"
                        )
                res[ch.key] = await ch.store.send_text(text)
            except Exception as ex:
                self._handle_write_error(ch.key, "file descriptor", ex)
        return res

    # -- pinned metadata ---------------------------------------------------

    async def mirror_pinned(
        self, primary_message_id: int, state: Dict[str, int]
    ) -> None:
        """Maintain a pinned copy of the metadata document in each mirror.

        The metadata blob is copied (preserving encryption), pinned, and
        the previous copy is deleted. ``state`` maps store key to the
        current metadata message id in that store and is updated in
        place.
        """
        for ch in self._stores:
            try:
                new_id = (await self._copy_to(ch, [primary_message_id]))[0]
                await ch.store.pin_message(new_id)
                if (old := state.get(ch.key)) and old != new_id:
                    await ch.store.delete_messages([old], force=True)
                state[ch.key] = new_id
            except Exception as ex:
                self._handle_write_error(ch.key, "pinned metadata", ex)

    async def adopt_pinned(
        self, new_ids: Dict[str, int], state: Dict[str, int]
    ) -> None:
        """Pin already-mirrored metadata copies (no copying needed).

        Used right after ``save`` has replicated the metadata blob as
        ordinary content: ``new_ids`` are the fresh copies per store.
        """
        for store_key, new_id in new_ids.items():
            if (ch := self._by_key.get(store_key)) is None or new_id <= 0:
                continue
            try:
                await ch.store.pin_message(new_id)
                if (old := state.get(store_key)) and old != new_id:
                    await ch.store.delete_messages([old], force=True)
                state[store_key] = new_id
            except Exception as ex:
                self._handle_write_error(store_key, "pinned metadata", ex)

    # -- deletion ----------------------------------------------------------

    async def delete(
        self, per_store: Dict[str, List[int]], force: bool = False
    ) -> None:
        """Best-effort deletion of mirrored messages, per store.

        Honors the backend's ``delete_messages_on_remove`` exactly like
        the primary-store deletion does (the gate lives inside
        ``IStore.delete_messages``); ``force`` bypasses that gate for
        internal bookkeeping.
        """
        for key, ids in per_store.items():
            if not ids:
                continue
            if (store := self.store_for(key)) is None:
                logger.warning(
                    f"Cannot delete mirrored messages in store {key}: "
                    f"store is not configured as a mirror anymore"
                )
                continue
            await store.delete_messages(ids, force=force)

    # -- internals ---------------------------------------------------------

    def _handle_write_error(self, store_key: str, what: str, ex: Exception) -> None:
        if self._strict:
            if isinstance(ex, TechnicalError):
                raise ex
            raise TechnicalError(
                f"Mirroring {what} to store {store_key} failed: {ex}"
            ) from ex
        logger.error(
            f"Mirroring {what} to store {store_key} failed (non-strict, "
            f"continuing): {ex}"
        )
