from typing import Dict, List, Literal, TypedDict


class TGFSFileVersionSerialized(TypedDict, total=False):
    type: Literal["FV"]
    id: str
    updatedAt: int
    messageId: int
    messageIds: List[int]
    size: int
    # Mirror store key (``tg:<channel>``; a bare channel id is a Telegram
    # key written by tgfs) -> message ids of the copies of each part, in
    # the same order as messageIds. Absent when redundancy is not in use,
    # so pre-redundancy metadata parses unchanged.
    mirrors: Dict[str, List[int]]
    # Store key that messageIds belong to. Absent for versions written by
    # tgfs (they belong to the configured primary).
    store: str


class TGFSFileDescSerialized(TypedDict, total=False):
    type: Literal["F"]
    name: str
    versions: List[TGFSFileVersionSerialized]


class TGFSFileRefSerialized(TypedDict, total=False):
    type: Literal["FR"]
    messageId: int
    name: str
    # Mirror store key -> message id of the file descriptor copy in that
    # store. Absent when redundancy is not in use.
    mirrors: Dict[str, int]
    # Store key that messageId belongs to; absent for refs written by tgfs.
    store: str


class TGFSDirectorySerialized(TypedDict, total=False):
    type: Literal["D"]
    name: str
    createdAt: int
    modifiedAt: int
    children: List["TGFSDirectorySerialized"]
    files: List[TGFSFileRefSerialized]
