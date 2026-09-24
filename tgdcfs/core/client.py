from typing import Awaitable, Callable, Dict, List, Optional

from tgdcfs.backends.base import IStore
from tgdcfs.config import (
    Config,
    EncryptionConfig,
    FilesystemConfig,
    MetadataType,
    StoreConfig,
)
from tgdcfs.core.api import DirectoryApi, FileApi, FileDescApi, MetaDataApi
from tgdcfs.core.mirror import MirrorGroup, MirrorStore
from tgdcfs.core.repository.impl import (
    PinnedMessageMetadataRepository,
    StoreFDRepository,
    StoreFileContentRepository,
)
from tgdcfs.core.repository.interface import (
    IFDRepository,
    IFileContentRepository,
    IMetaDataRepository,
)

# Builds the live store for a store config; the backends' logins live
# behind it so ``Client.create`` never touches a backend library.
StoreFactory = Callable[[StoreConfig], Awaitable[IStore]]


class Client:
    """One virtual file system: a primary store, its mirrors and the APIs
    that serve the tree stored there."""

    def __init__(
        self,
        name: str,
        store: IStore,
        file_api: FileApi,
        dir_api: DirectoryApi,
        fc_repo: IFileContentRepository,
        fd_repo: Optional[IFDRepository] = None,
        metadata_api: Optional[MetaDataApi] = None,
        mirror_group: Optional[MirrorGroup] = None,
        filesystem: Optional[FilesystemConfig] = None,
    ):
        self.name = name
        self.store = store
        self.file_api = file_api
        self.dir_api = dir_api
        self.fc_repo = fc_repo
        self.fd_repo = fd_repo
        self.metadata_api = metadata_api
        self.mirror_group = mirror_group
        self.filesystem = filesystem

    @property
    def message_api(self) -> IStore:
        """The primary store, under the name the app layer grew up with."""
        return self.store

    @classmethod
    async def create(
        cls,
        filesystem: FilesystemConfig,
        config: Config,
        store_factory: StoreFactory,
        encryption_cfg: Optional[EncryptionConfig] = None,
    ) -> "Client":
        store = await store_factory(config.stores[filesystem.primary])

        # One store per mirror; the serialization key is the store's key
        # (backend prefix + channel id as configured), stable across
        # backend libs and config renames.
        mirror_group: Optional[MirrorGroup] = None
        if filesystem.mirrors:
            mirror_stores: List[MirrorStore] = []
            for mirror_name in filesystem.mirrors:
                mirror_cfg = config.stores[mirror_name]
                mirror_stores.append(
                    MirrorStore(
                        key=mirror_cfg.key, store=await store_factory(mirror_cfg)
                    )
                )
            mirror_group = MirrorGroup(
                primary=store,
                stores=mirror_stores,
                mode=filesystem.mode,
                strict=filesystem.strict,
            )

        fc_repo: IFileContentRepository = StoreFileContentRepository(
            store, mirror_group=mirror_group
        )

        # Wrap the file-content repository in an encryption decorator if
        # encryption is enabled in the config. Everything downstream
        # (FileApi, WebDAV, etc.) is unchanged: the wrapper preserves the
        # IFileContentRepository contract.
        #
        # When ``encrypt_names`` is also set we derive a separate,
        # deterministic key for the metadata path names (directory and
        # file-reference names stored in the GitHub repo) and hand it to the
        # metadata backend below.
        path_name_key: Optional[bytes] = None
        if encryption_cfg is not None and encryption_cfg.enabled:
            from tgdcfs.crypto.bootstrap import load_master_key
            from tgdcfs.crypto.repository import EncryptingFileContentRepository

            master = load_master_key(encryption_cfg)
            fc_repo = EncryptingFileContentRepository(
                fc_repo,
                master_key=master.key,
                chunk_size=encryption_cfg.chunk_size,
                encrypt_names=encryption_cfg.encrypt_names,
            )
            if encryption_cfg.encrypt_names:
                from tgdcfs.crypto.path_names import derive_path_name_key

                path_name_key = derive_path_name_key(master.key)

        fd_repo = StoreFDRepository(store, mirror_group=mirror_group)

        metadata_cfg = filesystem.metadata
        if metadata_cfg.type == MetadataType.PINNED_MESSAGE:
            metadata_repo: IMetaDataRepository = PinnedMessageMetadataRepository(
                store, fc_repo, mirror_group=mirror_group
            )
        else:
            if (github_repo_config := metadata_cfg.github_repo) is None:
                raise ValueError(
                    f"configuration filesystems -> {filesystem.name} -> metadata "
                    f"-> github_repo is required."
                )
            from tgdcfs.core.repository.impl.metadata.github_repo import (
                GithubRepoMetadataRepository,
            )

            metadata_repo = GithubRepoMetadataRepository(
                github_repo_config, name_key=path_name_key
            )

        fd_api = FileDescApi(fd_repo, fc_repo)

        metadata_api = MetaDataApi(metadata_repo)
        await metadata_api.init()

        file_api = FileApi(metadata_api, fd_api, store, mirror_group=mirror_group)
        dir_api = DirectoryApi(metadata_api, file_api, store)

        return cls(
            name=filesystem.name,
            store=store,
            file_api=file_api,
            dir_api=dir_api,
            fc_repo=fc_repo,
            fd_repo=fd_repo,
            metadata_api=metadata_api,
            mirror_group=mirror_group,
            filesystem=filesystem,
        )


Clients = Dict[str, Client]
