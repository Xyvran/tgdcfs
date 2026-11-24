---
sidebar_position: 1
title: Developer Overview & Architecture Tour
description: Deep-dive architecture tour of the TGDCFS codebase for developers and contributors.
---

# 💻 Developer Overview & Architecture Tour

Welcome to the **TGDCFS Developer Guide**! This documentation is written for software engineers who want to understand the inner workings of TGDCFS, add new features, fix bugs, or contribute to the project.

---

## Technical Stack Summary

- **Language**: Python 3.13+ (strict type annotations with `mypy`)
- **Package Manager**: [Poetry](https://python-poetry.org/)
- **Web & Async Framework**: [FastAPI](https://fastapi.tiangolo.com/), [Uvicorn](https://www.uvicorn.org/), `uvloop` (on Linux)
- **Protocols**:
  - **Telegram**: [Telethon](https://github.com/LonamiWebs/Telethon) (MTProto)
  - **Discord**: [discord.py](https://github.com/Rapptz/discord.py)
  - **SFTP**: [AsyncSSH](https://asyncssh.readthedocs.io/)
  - **WebDAV**: Custom ASGI WebDAV pipeline (`asgidav/`)
- **Testing & Quality Assurance**: `pytest`, `pytest-asyncio`, `mypy`, `ruff`, `pre-commit`

---

## Codebase Directory Structure

```
tgdcfs/
├── main.py                     # Server entrypoint (Uvicorn + FastAPI + SFTP initialization)
├── config-test.yaml            # Test configuration file
├── demo-config.yaml            # Full annotated example config
├── pyproject.toml              # Dependencies, build metadata, tool settings
├── makefile                    # Quick development commands (make mypy, make ruff)
├── tgdcfs/                     # Main Python package
│   ├── config.py               # Config loading, schema v2, legacy tgfs translation
│   ├── backends/               # Backend drivers & store abstractions
│   │   ├── base.py             # IStore interface & StoreCapabilities
│   │   ├── telegram/           # TelegramStore implementation (Telethon)
│   │   └── discord/            # DiscordStore implementation (discord.py)
│   ├── core/                   # Core filesystem domain logic
│   │   ├── local_cache.py      # LocalCache, StagingWriter, VersionBytes
│   │   ├── multisource.py      # MultiSourceRead parallel scheduler
│   │   ├── replication.py      # Persistent ReplicationQueue & workers
│   │   ├── backfill.py         # Store backfill & promotion logic
│   │   ├── mirror.py           # MirrorGroup & replication dispatch
│   │   ├── repository/         # File, FileDesc, Metadata repositories
│   │   └── model/              # FileVersion, FileRef, Replica data models
│   ├── crypto/                 # AES-256-GCM encryption & Argon2id key derivation
│   ├── app/                    # Protocol servers & web endpoints
│   │   ├── webdav/             # WebDAV request router & provider
│   │   ├── sftp/               # AsyncSSH SFTP server & user handling
│   │   └── manager/            # Manager REST API endpoints (/api/*)
│   └── tasks/                  # Task store & async task execution runner
├── tests/                      # Full test suite (79 modules)
│   ├── fakes/                  # In-memory fake stores for offline testing
│   ├── tgdcfs/                 # Unit tests mirroring the package layout
│   │   ├── app/                #   WebDAV, SFTP, manager and cache app tests
│   │   ├── auth/               #   Basic/Bearer authentication tests
│   │   ├── backends/           #   Telegram & Discord store tests
│   │   ├── config/             #   Config loader tests
│   │   ├── core/               #   Cache, replication, mirror, ops tests
│   │   ├── tasks/              #   Task store tests
│   │   └── utils/              #   Chunk cache, prefetching, time tests
│   ├── test_asgidav/           # WebDAV pipeline (asgidav) tests
│   └── scripts/                # Helper script tests
├── scripts/                    # Helper scripts
│   ├── changelog.py            # Release notes extraction from CHANGELOG.md
│   ├── docker_smoke_check.py   # Container SFTP smoke test script
│   ├── encrypt_metadata_names.py # One-off name decryption helper
│   └── measure_transfer.py     # Download piece transfer benchmarking tool
├── tgdcfs-gh-pages/            # Next.js web frontend & Telegram Mini App
└── docusaurus/                 # Docusaurus documentation website
```

---

## Key Core Interfaces & Components

### 1. `IStore` Interface (`tgdcfs/backends/base.py`)
`IStore` is the foundational abstraction that decouples repository logic from specific cloud backends:

```python
class IStore(metaclass=ABCMeta):
    key: str
    caps: StoreCapabilities
    backend: str

    async def send_text(self, message: str) -> int: ...
    async def edit_message_text(self, message_id: int, message: str) -> int: ...
    async def get_messages(self, ids: list[int]) -> list[Optional[MessageResp]]: ...
    async def search_messages(self, search: str) -> list[MessageResp]: ...
    async def upload(self, file_msg: UploadableFileMessage) -> List[SentFileMessage]: ...
    async def download_file(self, message_id: int, begin: int, end: int) -> DownloadFileResp: ...
    async def replace_document(self, message_id: int, buffer: bytes, name: str) -> int: ...
    async def copy_within(self, message_ids: List[int]) -> List[int]: ...
    async def copy_from(self, source: "IStore", message_ids: List[int]) -> Optional[List[int]]: ...
    async def delete_messages(self, message_ids: Iterable[int], force: bool = False) -> None: ...
    async def pin_message(self, message_id: int) -> None: ...
    async def get_pinned_message(self) -> MessageRespWithDocument: ...
```

### 2. `StoreFileContentRepository` (`tgdcfs/core/repository/`)
Handles raw byte stream operations, chunking, range slicing, local cache integration, and multi-source read dispatch.

### 3. `ReplicationQueue` & `ReplicationWorker` (`tgdcfs/core/replication.py`)
Provides persistent asynchronous file replication across Telegram and Discord stores, persisting queue items to `replication_queue.json`.

---

## Next Steps for Developers

- 🛠️ [Local Repository Setup](./local-setup.md) – How to clone, set up Python 3.13+, Poetry, git hooks, and build/test Docker containers locally.
- 🧪 [Testing & QA Guide](./testing-and-qa.md) – Running pytest, typechecking, linting, and offline fake store tests.
- 🤝 [Contributing Guidelines](./contributing.md) – GitHub fork setup, repository secrets, CI/CD workflow guide, commit conventions, and releases.
