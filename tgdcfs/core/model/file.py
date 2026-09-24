import datetime
import json
from dataclasses import dataclass, field
from typing import Dict, Iterable, List, Optional
from uuid import uuid4 as uuid

from tgdcfs.backends.base import normalize_store_key
from tgdcfs.reqres import SentFileMessage
from tgdcfs.utils.time import FIRST_DAY_OF_EPOCH, ts

from .common import validate_name
from .serialized import TGFSFileDescSerialized, TGFSFileVersionSerialized

EMPTY_FILE_MESSAGE = -1
INVALID_FILE_SIZE = -1
INVALID_VERSION_ID = ""


@dataclass
class TGFSFileVersion:
    id: str
    updated_at: datetime.datetime
    _size: int = INVALID_FILE_SIZE  # total size

    # file can be split into multiple "file messages", each max 1GB
    message_ids: List[int] = field(default_factory=list)
    part_sizes: List[int] = field(default_factory=list)  # sizes of each part

    # Mirror store key -> message ids of the copies of each part in that
    # store, aligned with message_ids. A 0 entry means "this part has no
    # copy in that store (yet)".
    mirrors: Dict[str, List[int]] = field(default_factory=dict)

    # Key of the store that ``message_ids`` belong to. ``None`` for
    # versions written before the field existed (tgfs), which always
    # belong to the configured primary. When the configured primary is a
    # different store (the mirror was promoted), ``relocate`` re-expresses
    # the version relative to the new primary.
    store: Optional[str] = None

    @property
    def updated_at_timestamp(self) -> int:
        return ts(self.updated_at)

    @property
    def size(self) -> int:
        if self._size == INVALID_FILE_SIZE and self.part_sizes:
            self._size = sum(self.part_sizes)
        return self._size

    def to_dict(self) -> dict:
        res = dict(
            type="FV",
            id=self.id,
            updatedAt=self.updated_at_timestamp,
            messageIds=self.message_ids,
            size=self.size,
        )
        # Only serialized when present so pre-redundancy consumers keep
        # seeing the exact format they always did.
        if self.mirrors:
            res["mirrors"] = self.mirrors
        if self.store:
            res["store"] = self.store
        return res

    @staticmethod
    def empty() -> "TGFSFileVersion":
        return TGFSFileVersion(
            id=str(uuid()),
            updated_at=datetime.datetime.now(),
            message_ids=[],
        )

    @staticmethod
    def from_sent_file_message(
        *messages: SentFileMessage, store: Optional[str] = None
    ) -> "TGFSFileVersion":
        mirrors: Dict[str, List[int]] = {}
        for channel in {ch for msg in messages for ch in msg.mirrors}:
            mirrors[channel] = [msg.mirrors.get(channel, 0) for msg in messages]
        return TGFSFileVersion(
            id=str(uuid()),
            updated_at=datetime.datetime.now(),
            message_ids=[msg.message_id for msg in messages],
            part_sizes=[msg.size for msg in messages],
            mirrors=mirrors,
            store=store,
        )

    @staticmethod
    def from_dict(data: TGFSFileVersionSerialized) -> "TGFSFileVersion":
        if (updated_at_ts := data.get("updatedAt", 0)) > 0:
            updated_at = datetime.datetime.fromtimestamp(updated_at_ts / 1000)
        else:
            updated_at = FIRST_DAY_OF_EPOCH

        if (message_ids := data.get("messageIds")) is None:
            if (message_id := data["messageId"]) != EMPTY_FILE_MESSAGE:
                message_ids = [message_id]
            else:
                message_ids = []
        return TGFSFileVersion(
            id=data["id"],
            updated_at=updated_at,
            message_ids=message_ids,
            part_sizes=[],  # part sizes are not serialized
            # Keys written by tgfs are bare Telegram channel ids; they are
            # read as ``tg:`` keys and written back with the prefix.
            mirrors={
                normalize_store_key(channel): list(ids)
                for channel, ids in (data.get("mirrors") or {}).items()
            },
            store=(normalize_store_key(data["store"]) if data.get("store") else None),
        )

    def set_invalid(self):
        self.message_ids = []
        self.part_sizes = []
        self.mirrors = {}
        self._size = INVALID_FILE_SIZE

    def is_valid(self) -> bool:
        return bool(self.message_ids)

    def owned_by(self, store_key: str) -> bool:
        """Whether ``message_ids`` are ids of ``store_key``.

        A version without a recorded store belongs to whatever store is
        the primary; that is what tgfs always assumed.
        """
        return self.store is None or self.store == store_key

    def relocate(self, primary_key: str) -> None:
        """Re-express the version relative to a new primary store.

        After a mirror is promoted, ``message_ids`` still name messages of
        the old primary. They become that store's mirror entry, and the
        new primary's mirror entry, when it is complete, becomes
        ``message_ids``. Without a complete copy in the new primary the
        ids keep pointing at the old store, which readers then treat as a
        mirror: the version stays readable, and nothing is ever written
        to those ids in the wrong store.
        """
        if self.owned_by(primary_key) or self.store is None:
            return
        self.mirrors.setdefault(self.store, list(self.message_ids))
        ids = self.mirrors.get(primary_key)
        if ids and len(ids) == len(self.message_ids) and all(i > 0 for i in ids):
            del self.mirrors[primary_key]
            self.message_ids = list(ids)
            self.store = primary_key


@dataclass
class TGFSFileDesc:
    name: str
    latest_version_id: str = ""
    created_at: datetime.datetime = field(default_factory=datetime.datetime.now)
    versions: dict[str, TGFSFileVersion] = field(default_factory=dict)

    @property
    def updated_at_timestamp(self) -> int:
        if not self.versions or self.latest_version_id == INVALID_VERSION_ID:
            return ts(self.created_at)
        return self.get_latest_version().updated_at_timestamp

    def __post_init__(self):
        validate_name(self.name)

    def to_dict(self) -> dict:
        return dict(
            type="F",
            versions=[v.to_dict() for v in self.get_versions(sort=True)],
        )

    @staticmethod
    def from_dict(data: TGFSFileDescSerialized, name: str) -> "TGFSFileDesc":
        versions = {v["id"]: TGFSFileVersion.from_dict(v) for v in data["versions"]}
        if versions:
            latest_version_id = max(
                versions, key=lambda k: versions[k].updated_at_timestamp
            )
        else:
            latest_version_id = INVALID_VERSION_ID
        return TGFSFileDesc(
            name=name,
            latest_version_id=latest_version_id,
            versions=versions,
        )

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), ensure_ascii=False)

    @staticmethod
    def empty(name: str) -> "TGFSFileDesc":
        return TGFSFileDesc(
            name=name,
            latest_version_id="",
            versions={},
        )

    def get_latest_version(self) -> TGFSFileVersion:
        return (
            self.versions[self.latest_version_id]
            if self.latest_version_id
            else TGFSFileVersion.empty()
        )

    def get_version(self, version_id: str) -> TGFSFileVersion:
        return self.versions[version_id]

    def add_version(self, version: TGFSFileVersion) -> None:
        self.versions[version.id] = version
        if (
            self.latest_version_id == INVALID_VERSION_ID
            # ``>=`` and not ``>``: two versions can carry the same timestamp
            # (they are only millisecond-precise once serialized), and of two
            # equally-stamped versions the one added later is the newer one.
            or version.updated_at >= self.versions[self.latest_version_id].updated_at
        ):
            self.latest_version_id = version.id
        if not self.created_at or version.updated_at < self.created_at:
            self.created_at = version.updated_at

    def add_empty_version(self) -> None:
        version = TGFSFileVersion.empty()
        self.add_version(version)

    def add_version_from_sent_file_message(self, *msg: SentFileMessage):
        version = TGFSFileVersion.from_sent_file_message(*msg)
        self.add_version(version)
        return self.versions[self.latest_version_id]

    def update_version(self, version_id: str, version: TGFSFileVersion):
        self.versions[version_id] = version

    def get_versions(
        self, sort: bool = False, exclude_invalid: bool = False
    ) -> List[TGFSFileVersion]:
        if not sort:
            res: Iterable[TGFSFileVersion] = self.versions.values()
        else:
            # Newest first. Timestamps are millisecond-precise, so two
            # versions can tie; insertion order decides then, because
            # ``from_dict`` reads the first entry as the latest version and
            # would otherwise resurrect the older one of the two.
            res = [
                version
                for _, version in sorted(
                    enumerate(self.versions.values()),
                    key=lambda item: (item[1].updated_at_timestamp, item[0]),
                    reverse=True,
                )
            ]

        if exclude_invalid:
            res = [v for v in res if v.is_valid()]
        return list(res)

    def delete_version(self, version_id: str) -> None:
        if version_id not in self.versions:
            raise ValueError(f"Version {version_id} not found in file {self.name}.")
        del self.versions[version_id]
        if version_id == self.latest_version_id:
            if self.versions:
                # Ties go to the version added last, as in ``add_version``.
                self.latest_version_id = max(
                    enumerate(self.versions),
                    key=lambda item: (self.versions[item[1]].updated_at, item[0]),
                )[1]
            else:
                self.latest_version_id = ""
