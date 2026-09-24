import logging
import os
from dataclasses import dataclass, field
from enum import Enum
from typing import Dict, List, Literal, Optional, Self, TypedDict

import yaml

from tgdcfs.backends import BACKEND_NAMES, BACKEND_PREFIXES, make_store_key

logger = logging.getLogger(__name__)


def _env_with_legacy_fallback(name: str, legacy_name: str, default: str) -> str:
    """Read ``name`` from the environment, falling back to the tgfs name.

    tgdcfs started as a copy of tgfs, whose deployments set ``TGFS_*``
    variables. Those keep working for now so an existing container can be
    switched to the tgdcfs image without touching its environment; the
    fallback logs a deprecation warning and is scheduled for removal.
    """
    if (value := os.environ.get(name)) is not None:
        return value
    if (value := os.environ.get(legacy_name)) is not None:
        logger.warning(
            f"Environment variable {legacy_name} is deprecated, use {name} instead"
        )
        return value
    return default


DATA_DIR = _env_with_legacy_fallback(
    "TGDCFS_DATA_DIR", "TGFS_DATA_DIR", os.path.expanduser("~/.tgdcfs")
)
CONFIG_FILE = _env_with_legacy_fallback(
    "TGDCFS_CONFIG_FILE", "TGFS_CONFIG_FILE", "config.yaml"
)


@dataclass
class WebDAVConfig:
    host: str
    port: int
    path: str

    @classmethod
    def from_dict(cls, data: dict) -> Self:
        return cls(host=data["host"], port=data["port"], path=data["path"])


@dataclass
class ManagerConfig:
    host: str
    port: int

    @classmethod
    def from_dict(cls, data: dict) -> Self:
        return cls(host=data["host"], port=data["port"])


@dataclass
class UserConfig:
    password: str
    readonly: bool

    @classmethod
    def from_dict(cls, data: dict) -> Self:
        return cls(password=data["password"], readonly=data.get("readonly", False))


@dataclass
class JWTConfig:
    secret: str
    algorithm: str
    life: int

    @classmethod
    def from_dict(cls, data: dict) -> Self:
        return cls(
            secret=data["secret"], algorithm=data["algorithm"], life=data["life"]
        )


@dataclass
class EncryptionConfig:
    """Optional at-rest encryption settings.

    ``passphrase_env`` / ``passphrase`` / ``passphrase_file`` are mutually
    exclusive; the loader picks the first one that is set. A file containing
    the passphrase is the recommended option for systemd deployments (pair
    it with a ``LoadCredential=`` unit directive).

    ``master_salt_file`` stores the 16-byte master salt produced on the
    very first run. Back this up alongside your TGDCFS metadata -- without it
    the master key cannot be re-derived even with the correct passphrase.

    ``encrypt_names`` additionally replaces the Telegram-visible document
    name (and the pinned metadata document name) with an AES-GCM
    ciphertext blob, so a passive observer of the channel cannot read
    file or directory names from the document metadata. The plaintext
    name is still stored inside the TGDCFS metadata.json, which is itself
    encrypted at rest, so the WebDAV / manager UI is unaffected.
    """

    enabled: bool
    encrypt_names: bool
    passphrase: Optional[str]
    passphrase_env: Optional[str]
    passphrase_file: Optional[str]
    master_salt_file: str
    chunk_size: int

    @classmethod
    def from_dict(cls, data: Optional[dict]) -> "EncryptionConfig":
        if not data:
            return cls(
                enabled=False,
                encrypt_names=False,
                passphrase=None,
                passphrase_env=None,
                passphrase_file=None,
                master_salt_file=expand_path("master.salt"),
                chunk_size=64 * 1024,
            )
        return cls(
            enabled=bool(data.get("enabled", False)),
            encrypt_names=bool(data.get("encrypt_names", False)),
            passphrase=data.get("passphrase"),
            passphrase_env=data.get("passphrase_env"),
            passphrase_file=(
                expand_path(data["passphrase_file"])
                if data.get("passphrase_file")
                else None
            ),
            master_salt_file=expand_path(data.get("master_salt_file", "master.salt")),
            chunk_size=int(data.get("chunk_size", 64 * 1024)),
        )

    def resolve_passphrase(self) -> str:
        """Return the passphrase from whichever source is configured.

        Raises :class:`ValueError` if encryption is enabled but no source
        was configured. Stripping a trailing newline makes the
        ``passphrase_file`` flow forgiving of editors that always add one.
        """
        if self.passphrase_env:
            value = os.environ.get(self.passphrase_env)
            if value is None:
                raise ValueError(
                    f"encryption passphrase env var '{self.passphrase_env}' not set"
                )
            return value
        if self.passphrase_file:
            with open(self.passphrase_file, "r", encoding="utf-8") as fh:
                return fh.read().rstrip("\n")
        if self.passphrase:
            return self.passphrase
        raise ValueError(
            "encryption enabled but no passphrase source configured "
            "(set one of passphrase, passphrase_env, passphrase_file)"
        )


@dataclass
class GithubRepoConfig:
    repo: str
    commit: str
    access_token: str

    @classmethod
    def from_dict(cls, data: dict) -> Self:
        return cls(
            repo=data["repo"],
            commit=data["commit"],
            access_token=data["access_token"],
        )


class MetadataType(Enum):
    PINNED_MESSAGE = "pinned_message"
    GITHUB_REPO = "github_repo"


class MetadataConfigDict(TypedDict):
    name: str
    type: str
    github_repo: Optional[Dict]


@dataclass
class MetadataConfig:
    name: str
    type: MetadataType
    github_repo: Optional[GithubRepoConfig]

    @classmethod
    def from_dict(cls, data: MetadataConfigDict) -> Self:
        if (
            data.get("type", MetadataType.PINNED_MESSAGE.value)
            == MetadataType.PINNED_MESSAGE.value
        ):
            return cls(
                name=data.get("name", "default"),
                type=MetadataType.PINNED_MESSAGE,
                github_repo=None,
            )
        if data["type"] == MetadataType.GITHUB_REPO.value:
            if not (gh_repo_config := data.get("github_repo")):
                raise ValueError(
                    "GitHub repo configuration is required for GITHUB_REPO type"
                )
            return cls(
                name=data.get("name", "default"),
                type=MetadataType.GITHUB_REPO,
                github_repo=GithubRepoConfig.from_dict(gh_repo_config),
            )
        raise ValueError(
            f"Unknown metadata type: {data['type']}, available options: {', '.join(e.value for e in MetadataType)}"
        )


@dataclass
class ServerConfig:
    host: str
    port: int

    @classmethod
    def from_dict(cls, data: Dict) -> "ServerConfig":
        return cls(host=data["host"], port=data["port"])


@dataclass
class SFTPConfig:
    """Optional SFTP interface, served next to the HTTP/WebDAV surface.

    It exposes the very same virtual file tree and reuses ``tgdcfs.users``,
    so a readonly user stays readonly here as well. SSH cannot share the
    HTTP socket, hence the separate ``port``.

    ``host_key_file`` is created on first start (ed25519, mode 0600) when
    it is missing. Back it up: without it every restart presents a new
    host key and clients refuse to connect until their known_hosts entry
    is cleared.

    ``authorized_keys_dir`` optionally enables public key authentication.
    It holds one file per user, named after the username and written in
    the usual ``authorized_keys`` format. The user must still exist in
    ``tgdcfs.users`` so the readonly flag keeps applying.

    ``upload_buffer_size_mb`` is how much of an incoming upload is kept in
    memory before it spills over to ``upload_buffer_dir`` (the system temp
    directory when empty). SFTP never announces the file size up front
    while Telegram uploads need it, so a whole file has to be buffered
    before it can be sent.
    """

    enabled: bool
    host: str
    port: int
    host_key_file: str
    authorized_keys_dir: Optional[str]
    upload_buffer_size_mb: int
    upload_buffer_dir: Optional[str]

    DEFAULT_PORT = 2222
    DEFAULT_HOST_KEY_FILE = "sftp_host_key"
    DEFAULT_UPLOAD_BUFFER_SIZE_MB = 64

    @property
    def upload_buffer_size_bytes(self) -> int:
        return self.upload_buffer_size_mb * 1024 * 1024

    @classmethod
    def from_dict(cls, data: Optional[dict]) -> "SFTPConfig":
        data = data or {}

        port = int(data.get("port", cls.DEFAULT_PORT))
        if not 1 <= port <= 65535:
            raise ValueError(f"sftp.port must be between 1 and 65535, got {port}")

        buffer_size_mb = int(
            data.get("upload_buffer_size_mb", cls.DEFAULT_UPLOAD_BUFFER_SIZE_MB)
        )
        if buffer_size_mb < 0:
            raise ValueError(
                f"sftp.upload_buffer_size_mb must not be negative, got {buffer_size_mb}"
            )

        return cls(
            enabled=bool(data.get("enabled", False)),
            host=str(data.get("host", "0.0.0.0")),  # noqa: S104
            port=port,
            host_key_file=expand_path(
                data.get("host_key_file") or cls.DEFAULT_HOST_KEY_FILE
            ),
            authorized_keys_dir=(
                expand_path(data["authorized_keys_dir"])
                if data.get("authorized_keys_dir")
                else None
            ),
            upload_buffer_size_mb=buffer_size_mb,
            upload_buffer_dir=(
                expand_path(data["upload_buffer_dir"])
                if data.get("upload_buffer_dir")
                else None
            ),
        )


@dataclass
class TransferConfig:
    """Tuning knobs for moving file bytes to and from Telegram.

    Every value here used to be a constant in the code. The right setting
    depends on how many bots a deployment has, how much bandwidth it can
    use and how tolerant its account is of Telegram's rate limits, so none
    of them has a single good answer.

    Downloads are split into pieces of ``download_piece_size_kb`` and
    ``download_pieces_in_flight`` of them are fetched at once. Output must
    stay in order, so a finished piece waits for its turn: peak buffering
    per download is the product of the two -- 16 MiB at the defaults.
    Raising either speeds up a single large download and costs memory per
    concurrent reader.

    ``connection_pool_size`` is how many MTProto connections each bot
    opens. One connection serialises its requests, so a deployment with a
    single bot token gains the most here; with several bots the pieces
    already spread across them.

    ``chunk_cache_mb`` is the memory budget for caching downloaded bytes;
    ``0`` disables the cache. ``chunk_cache_block_kb`` is the unit it caches
    in: a read of a few kilobytes pulls a whole block, so a larger block
    serves more of the reads that follow and wastes more on the ones that
    jump around.
    """

    upload_workers_small: int
    upload_workers_big: int
    upload_part_size_kb: int
    download_piece_size_kb: int
    download_pieces_in_flight: int
    parallel_download_threshold_mb: int
    connection_pool_size: int
    chunk_cache_mb: int
    chunk_cache_readahead: int
    chunk_cache_block_kb: int

    DEFAULT_UPLOAD_WORKERS_SMALL = 3
    DEFAULT_UPLOAD_WORKERS_BIG = 8
    # 512 KiB is the largest part Telegram accepts and is valid for any file
    # size, so there is no reason to send the smaller parts the library
    # would otherwise pick for files below 750 MB.
    DEFAULT_UPLOAD_PART_SIZE_KB = 512
    DEFAULT_DOWNLOAD_PIECE_SIZE_KB = 4096
    DEFAULT_DOWNLOAD_PIECES_IN_FLIGHT = 4
    DEFAULT_PARALLEL_DOWNLOAD_THRESHOLD_MB = 10
    DEFAULT_CONNECTION_POOL_SIZE = 1
    DEFAULT_CHUNK_CACHE_MB = 0
    DEFAULT_CHUNK_CACHE_READAHEAD = 2
    DEFAULT_CHUNK_CACHE_BLOCK_KB = 1024

    @property
    def download_piece_size_bytes(self) -> int:
        return self.download_piece_size_kb * 1024

    @property
    def upload_part_size_bytes(self) -> int:
        return self.upload_part_size_kb * 1024

    @property
    def parallel_download_threshold_bytes(self) -> int:
        return self.parallel_download_threshold_mb * 1024 * 1024

    @property
    def chunk_cache_bytes(self) -> int:
        return self.chunk_cache_mb * 1024 * 1024

    @property
    def chunk_cache_block_bytes(self) -> int:
        return self.chunk_cache_block_kb * 1024

    @classmethod
    def from_dict(cls, data: Optional[dict]) -> "TransferConfig":
        data = data or {}

        def positive(key: str, default: int) -> int:
            value = int(data.get(key, default))
            if value < 1:
                raise ValueError(f"transfer.{key} must be at least 1, got {value}")
            return value

        def non_negative(key: str, default: int) -> int:
            value = int(data.get(key, default))
            if value < 0:
                raise ValueError(f"transfer.{key} must not be negative, got {value}")
            return value

        part_size = positive("upload_part_size_kb", cls.DEFAULT_UPLOAD_PART_SIZE_KB)
        # Telegram only accepts part sizes that divide 512 KiB, and it caps
        # them there; anything else is rejected for every part of the file.
        if part_size > 512 or 512 % part_size != 0:
            raise ValueError(
                "transfer.upload_part_size_kb must divide 512 and not exceed it, "
                f"got {part_size}"
            )

        return cls(
            upload_workers_small=positive(
                "upload_workers_small", cls.DEFAULT_UPLOAD_WORKERS_SMALL
            ),
            upload_workers_big=positive(
                "upload_workers_big", cls.DEFAULT_UPLOAD_WORKERS_BIG
            ),
            upload_part_size_kb=part_size,
            download_piece_size_kb=positive(
                "download_piece_size_kb", cls.DEFAULT_DOWNLOAD_PIECE_SIZE_KB
            ),
            download_pieces_in_flight=positive(
                "download_pieces_in_flight", cls.DEFAULT_DOWNLOAD_PIECES_IN_FLIGHT
            ),
            parallel_download_threshold_mb=positive(
                "parallel_download_threshold_mb",
                cls.DEFAULT_PARALLEL_DOWNLOAD_THRESHOLD_MB,
            ),
            connection_pool_size=positive(
                "connection_pool_size", cls.DEFAULT_CONNECTION_POOL_SIZE
            ),
            chunk_cache_mb=non_negative("chunk_cache_mb", cls.DEFAULT_CHUNK_CACHE_MB),
            chunk_cache_readahead=non_negative(
                "chunk_cache_readahead", cls.DEFAULT_CHUNK_CACHE_READAHEAD
            ),
            chunk_cache_block_kb=positive(
                "chunk_cache_block_kb", cls.DEFAULT_CHUNK_CACHE_BLOCK_KB
            ),
        )


@dataclass
class TGFSConfig:
    users: dict[str, UserConfig]
    jwt: JWTConfig
    metadata: Dict[str, MetadataConfig]
    server: ServerConfig
    encryption: EncryptionConfig
    sftp: SFTPConfig
    transfer: TransferConfig

    @classmethod
    def from_dict(cls, data: Dict) -> Self:
        metadata_config: Dict[str, MetadataConfigDict] = data.get("metadata", {})

        return cls(
            users=(
                {
                    username: UserConfig.from_dict(user)
                    for username, user in data["users"].items()
                }
                if data["users"]
                else {}
            ),
            jwt=JWTConfig.from_dict(data["jwt"]),
            metadata={
                k: MetadataConfig.from_dict(v) for k, v in metadata_config.items()
            },
            server=ServerConfig.from_dict(data["server"]),
            encryption=EncryptionConfig.from_dict(data.get("encryption")),
            sftp=SFTPConfig.from_dict(data.get("sftp")),
            transfer=TransferConfig.from_dict(data.get("transfer")),
        )


def expand_path(path: str) -> str:
    return os.path.expanduser(os.path.join(DATA_DIR, path)).replace("/", os.path.sep)


@dataclass
class BotConfig:
    token: str
    session_file: str
    tokens: List[str] = field(default_factory=list)

    @classmethod
    def from_dict(cls, data: dict) -> "BotConfig":
        return cls(
            token=data.get("token", ""),
            tokens=data.get("tokens", []),
            session_file=expand_path(data["session_file"]),
        )


@dataclass
class AccountConfig:
    session_file: str
    used_to_upload: bool
    used_to_download: bool

    @classmethod
    def from_dict(cls, data: dict) -> "AccountConfig":
        return cls(
            session_file=expand_path(data["session_file"]),
            used_to_upload=data.get("used_to_upload", False),
            used_to_download=data.get("used_to_download", False),
        )


@dataclass
class RedundancyConfig:
    """RAID-1-style mirroring of file channels.

    ``mirrors`` maps a primary channel id (as listed in
    ``private_file_channel``) to the channel ids of its mirrors. Every
    file part uploaded to the primary is copied to each mirror --
    server-side via message forwarding in ``forward`` mode (no
    re-upload bandwidth), or by downloading and re-uploading in
    ``reupload`` mode (for channels with "restrict saving content"
    enabled, where forwarding is impossible).

    With ``strict: false`` (the default) a failed mirror write is
    logged and the upload still succeeds; the backfill task can close
    the gap later. With ``strict: true`` the upload fails.
    """

    mirrors: Dict[str, List[str]]
    mode: Literal["forward", "reupload"]
    strict: bool

    @classmethod
    def from_dict(cls, data: Optional[dict]) -> Optional["RedundancyConfig"]:
        if not data:
            return None
        raw_mirrors = data.get("mirrors") or {}
        mirrors: Dict[str, List[str]] = {}
        for primary, mirror_ids in raw_mirrors.items():
            if not mirror_ids:
                continue
            mirror_list = [str(m) for m in mirror_ids]
            if str(primary) in mirror_list:
                raise ValueError(
                    f"Channel {primary} cannot be configured as its own mirror"
                )
            if len(set(mirror_list)) != len(mirror_list):
                raise ValueError(
                    f"Duplicate mirror channel for primary channel {primary}"
                )
            mirrors[str(primary)] = mirror_list
        if not mirrors:
            return None
        mode = data.get("mode", "forward")
        if mode not in ("forward", "reupload"):
            raise ValueError(
                f"Unknown redundancy mode: {mode}, available options: forward, reupload"
            )
        return cls(
            mirrors=mirrors,
            mode=mode,
            strict=bool(data.get("strict", False)),
        )


@dataclass
class TelegramConfig:
    api_id: int
    api_hash: str
    account: Optional[AccountConfig]
    bot: BotConfig
    private_file_channel: List[str]
    lib: Literal["pyrogram", "telethon"]
    delete_messages_on_remove: bool
    redundancy: Optional[RedundancyConfig]

    @classmethod
    def from_dict(cls, data: dict) -> "TelegramConfig":
        # ``private_file_channel`` and ``redundancy`` belong to the legacy
        # (tgfs) layout, where the file channels were listed under the
        # backend. In the current layout they are ``stores`` and
        # ``filesystems``; see ``Config.from_dict``.
        channels = data.get("private_file_channel") or []
        if isinstance(channels, (str, int)):
            channels = [channels]
        return cls(
            api_id=data["api_id"],
            api_hash=data["api_hash"],
            account=(
                AccountConfig.from_dict(data["account"]) if "account" in data else None
            ),
            bot=BotConfig.from_dict(data["bot"]),
            private_file_channel=[str(c) for c in channels],
            lib=data.get("lib") or "telethon",
            delete_messages_on_remove=bool(
                data.get("delete_messages_on_remove", False)
            ),
            redundancy=RedundancyConfig.from_dict(data.get("redundancy")),
        )


@dataclass
class DiscordConfig:
    """Credentials and limits of the Discord backend.

    ``max_file_size_bytes`` is the largest attachment the bot may send
    in the channels it serves: 20 MB on an unboosted server (10 MB before
    August 2026), 50 MB at boost level 2 and 100 MB at level 3. The
    default is conservative; raise it to match the server, a too large
    value makes every upload fail. Files are cut into parts of this size.
    """

    bot_tokens: List[str]
    max_file_size_bytes: int
    delete_messages_on_remove: bool
    upload_max_retries: int
    upload_retry_interval: float
    max_concurrent_uploads: int
    max_concurrent_downloads: int

    DEFAULT_MAX_FILE_SIZE_BYTES = 10_000_000

    @classmethod
    def from_dict(cls, data: dict) -> "DiscordConfig":
        tokens = list(data.get("bot_tokens") or [])
        if token := data.get("bot_token"):
            tokens.insert(0, token)
        if not tokens:
            raise ValueError("backends.discord: 'bot_tokens' is required")
        max_file_size = int(
            data.get("max_file_size_bytes", cls.DEFAULT_MAX_FILE_SIZE_BYTES)
        )
        if max_file_size < 1024:
            raise ValueError(
                f"backends.discord.max_file_size_bytes is too small: {max_file_size}"
            )

        def positive(key: str, default: int) -> int:
            value = int(data.get(key, default))
            if value < 1:
                raise ValueError(f"backends.discord.{key} must be at least 1")
            return value

        return cls(
            bot_tokens=[str(t) for t in tokens],
            max_file_size_bytes=max_file_size,
            delete_messages_on_remove=bool(
                data.get("delete_messages_on_remove", False)
            ),
            upload_max_retries=positive("upload_max_retries", 10),
            upload_retry_interval=float(data.get("upload_retry_interval", 5.0)),
            max_concurrent_uploads=positive("max_concurrent_uploads", 3),
            max_concurrent_downloads=positive("max_concurrent_downloads", 3),
        )


@dataclass
class StoreConfig:
    """One channel of one backend, under a name of the user's choosing.

    The name is only used inside the config (file systems refer to
    stores by it); what ends up in the metadata is ``key``, built from
    the backend and the channel id, so a store can be renamed freely.
    """

    name: str
    backend: str
    channel: str

    @property
    def key(self) -> str:
        return make_store_key(BACKEND_PREFIXES[self.backend], self.channel)

    @classmethod
    def from_dict(cls, name: str, data: dict) -> "StoreConfig":
        backend = str(data.get("backend") or "telegram")
        if backend not in BACKEND_PREFIXES:
            raise ValueError(
                f"stores.{name}: unknown backend '{backend}', "
                f"available: {', '.join(BACKEND_PREFIXES)}"
            )
        if backend not in BACKEND_NAMES:
            raise ValueError(
                f"stores.{name}: backend '{backend}' is not available in this "
                f"version yet"
            )
        if (channel := data.get("channel")) is None or str(channel) == "":
            raise ValueError(f"stores.{name}: 'channel' is required")
        return cls(name=name, backend=backend, channel=str(channel))


MirrorMode = Literal["auto", "forward", "reupload"]
SyncMode = Literal["inline", "background"]


@dataclass
class FilesystemConfig:
    """A virtual file system: one primary store plus mirror stores.

    ``mode`` picks how parts reach a mirror: ``auto`` copies server-side
    when the pair of stores allows it and re-uploads otherwise,
    ``forward`` insists on the server-side copy, ``reupload`` never asks
    for one. ``sync`` says whether mirroring happens inside the write
    (``inline``) or from a background queue (``background``; accepted
    already, but it behaves like ``inline`` until the replication queue
    exists). ``strict`` fails the write when a mirror write fails and
    therefore requires ``inline``.
    """

    name: str
    primary: str
    mirrors: List[str]
    mode: MirrorMode
    sync: SyncMode
    strict: bool
    read_preference: List[str]
    metadata: MetadataConfig
    allow_shared_store: bool = False

    @classmethod
    def from_dict(cls, name: str, data: dict) -> "FilesystemConfig":
        if not (primary := data.get("primary")):
            raise ValueError(f"filesystems.{name}: 'primary' is required")
        mirrors = [str(m) for m in (data.get("mirrors") or [])]
        if str(primary) in mirrors:
            raise ValueError(
                f"filesystems.{name}: the primary store cannot be its own mirror"
            )
        if len(set(mirrors)) != len(mirrors):
            raise ValueError(f"filesystems.{name}: duplicate mirror store")

        mode = str(data.get("mode", "auto"))
        if mode not in ("auto", "forward", "reupload"):
            raise ValueError(
                f"filesystems.{name}: unknown mode '{mode}', "
                f"available options: auto, forward, reupload"
            )
        sync = str(data.get("sync", "inline"))
        if sync not in ("inline", "background"):
            raise ValueError(
                f"filesystems.{name}: unknown sync '{sync}', "
                f"available options: inline, background"
            )
        strict = bool(data.get("strict", False))
        if strict and sync == "background":
            raise ValueError(
                f"filesystems.{name}: 'strict: true' requires 'sync: inline'"
            )
        if sync == "background":
            logger.warning(
                f"filesystems.{name}: 'sync: background' is not implemented yet, "
                f"mirroring runs inline"
            )

        metadata_data: MetadataConfigDict = {
            "name": name,
            "type": MetadataType.PINNED_MESSAGE.value,
            "github_repo": None,
        }
        metadata_data.update(data.get("metadata") or {})  # type: ignore[typeddict-item]
        metadata = MetadataConfig.from_dict(metadata_data)
        return cls(
            name=name,
            primary=str(primary),
            mirrors=mirrors,
            mode=mode,  # type: ignore[arg-type]
            sync=sync,  # type: ignore[arg-type]
            strict=strict,
            read_preference=[str(s) for s in (data.get("read_preference") or [])],
            metadata=metadata,
            allow_shared_store=bool(data.get("allow_shared_store", False)),
        )

    @property
    def store_names(self) -> List[str]:
        return [self.primary, *self.mirrors]


def _legacy_stores_and_filesystems(
    telegram: TelegramConfig, app: TGFSConfig
) -> tuple[Dict[str, StoreConfig], Dict[str, FilesystemConfig]]:
    """Translate the tgfs layout into stores and file systems.

    ``telegram.private_file_channel`` lists the primary channels,
    ``tgfs.metadata[<channel>]`` names each file system, and
    ``telegram.redundancy.mirrors[<channel>]`` lists its mirrors. Store
    names are derived from the channel id; they never reach the
    metadata, so the choice is free.
    """
    stores: Dict[str, StoreConfig] = {}
    filesystems: Dict[str, FilesystemConfig] = {}

    def store_name(channel: str) -> str:
        name = f"tg-{channel}"
        if name not in stores:
            stores[name] = StoreConfig(name=name, backend="telegram", channel=channel)
        return name

    redundancy = telegram.redundancy
    for channel in telegram.private_file_channel:
        if (metadata := app.metadata.get(channel)) is None:
            raise ValueError(
                f"configuration tgdcfs -> metadata -> {channel} is missing"
            )
        mirror_channels = redundancy.mirrors.get(channel, []) if redundancy else []
        filesystems[metadata.name] = FilesystemConfig(
            name=metadata.name,
            primary=store_name(channel),
            mirrors=[store_name(m) for m in mirror_channels],
            mode=redundancy.mode if redundancy else "auto",
            sync="inline",
            strict=redundancy.strict if redundancy else False,
            read_preference=[],
            metadata=metadata,
            # tgfs allowed any channel arrangement; keep that.
            allow_shared_store=True,
        )
    return stores, filesystems


def _validate_filesystems(
    stores: Dict[str, StoreConfig], filesystems: Dict[str, FilesystemConfig]
) -> None:
    primaries: Dict[str, str] = {}
    for fs in filesystems.values():
        for store_name in fs.store_names:
            if store_name not in stores:
                raise ValueError(f"filesystems.{fs.name}: unknown store '{store_name}'")
        for store_name in fs.read_preference:
            if store_name not in fs.store_names:
                raise ValueError(
                    f"filesystems.{fs.name}: read_preference names '{store_name}', "
                    f"which is neither its primary nor one of its mirrors"
                )
        if (other := primaries.get(fs.primary)) is not None:
            raise ValueError(
                f"filesystems.{fs.name}: store '{fs.primary}' is already the "
                f"primary of '{other}'"
            )
        primaries[fs.primary] = fs.name
    for fs in filesystems.values():
        for mirror in fs.mirrors:
            if (owner := primaries.get(mirror)) and not (
                fs.allow_shared_store or filesystems[owner].allow_shared_store
            ):
                raise ValueError(
                    f"filesystems.{fs.name}: mirror '{mirror}' is the primary of "
                    f"'{owner}'; two trees writing into one store invites id "
                    f"confusion, set 'allow_shared_store: true' to allow it"
                )


@dataclass
class Config:
    """The whole configuration.

    Current layout::

        backends:
          telegram: {...}
        stores:
          <name>: {backend: telegram, channel: "..."}
        filesystems:
          <name>: {primary: <store>, mirrors: [<store>], metadata: {...}}
        tgdcfs: {...}

    The tgfs layout (``telegram.private_file_channel``, ``tgfs.metadata``
    keyed by channel, ``telegram.redundancy``) is translated on load.
    """

    telegram: TelegramConfig
    tgdcfs: TGFSConfig
    stores: Dict[str, StoreConfig] = field(default_factory=dict)
    filesystems: Dict[str, FilesystemConfig] = field(default_factory=dict)
    discord: Optional[DiscordConfig] = None

    def filesystem_for_store(self, store_name: str) -> Optional[FilesystemConfig]:
        """The file system whose primary is ``store_name``."""
        for fs in self.filesystems.values():
            if fs.primary == store_name:
                return fs
        return None

    def filesystem_for_channel(
        self, backend: str, channel: str
    ) -> Optional[FilesystemConfig]:
        """The file system whose primary store is ``channel`` of ``backend``."""
        for store in self.stores.values():
            if store.backend == backend and store.channel == str(channel):
                if fs := self.filesystem_for_store(store.name):
                    return fs
        return None

    @property
    def uses_backend(self) -> Dict[str, bool]:
        return {
            name: any(store.backend == name for store in self.stores.values())
            for name in BACKEND_PREFIXES
        }

    @classmethod
    def from_dict(cls, data: dict) -> "Config":
        # A config written for tgfs keeps its ``tgfs:`` block name; it is
        # read as the ``tgdcfs:`` block so existing installations load
        # unchanged.
        if "tgdcfs" in data:
            app_data = data["tgdcfs"]
        elif "tgfs" in data:
            logger.warning(
                "The top-level config key 'tgfs' is deprecated, rename it to 'tgdcfs'"
            )
            app_data = data["tgfs"]
        else:
            raise ValueError("configuration block 'tgdcfs' is missing")

        # Backend credentials: ``backends.telegram`` in the current layout,
        # top-level ``telegram`` in the tgfs layout.
        backends = data.get("backends") or {}
        if "telegram" in backends:
            telegram_data = backends["telegram"]
        elif "telegram" in data:
            telegram_data = data["telegram"]
        else:
            raise ValueError("configuration block 'backends.telegram' is missing")
        telegram = TelegramConfig.from_dict(telegram_data)
        discord = (
            DiscordConfig.from_dict(backends["discord"])
            if "discord" in backends
            else None
        )
        app = TGFSConfig.from_dict(app_data)

        if "stores" in data or "filesystems" in data:
            if telegram.private_file_channel:
                raise ValueError(
                    "'telegram.private_file_channel' cannot be combined with "
                    "'stores'/'filesystems'; list the channel as a store instead"
                )
            stores = {
                str(name): StoreConfig.from_dict(str(name), store or {})
                for name, store in (data.get("stores") or {}).items()
            }
            filesystems = {
                str(name): FilesystemConfig.from_dict(str(name), fs or {})
                for name, fs in (data.get("filesystems") or {}).items()
            }
        else:
            stores, filesystems = _legacy_stores_and_filesystems(telegram, app)

        for store in stores.values():
            if store.backend == "discord" and discord is None:
                raise ValueError(
                    f"stores.{store.name}: backend 'discord' needs the "
                    f"'backends.discord' block"
                )
        _validate_filesystems(stores, filesystems)
        return cls(
            telegram=telegram,
            tgdcfs=app,
            stores=stores,
            filesystems=filesystems,
            discord=discord,
        )


__config_file_path = expand_path(os.path.join(DATA_DIR, CONFIG_FILE))
__config: Config | None = None


def _load_config(file_path: str) -> Config:
    with open(file_path, "r") as file:
        data = yaml.safe_load(file)
        return Config.from_dict(data)


def get_config() -> Config:
    global __config
    if __config is None:
        logger.info(f"Using configuration file: {__config_file_path}")
        __config = _load_config(__config_file_path)
    return __config
