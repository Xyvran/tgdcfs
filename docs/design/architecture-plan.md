# tgdcfs: Architecture and Implementation Plan

Status: phases 0 to 4 implemented (see "Progress" at the end). This document plans how to build tgdcfs as a new
project: the tgfs code base as the main part, plus Discord as a second
storage backend that can be configured next to Telegram, either as the
primary store or as a mirror, in both directions.

Reference implementations:

* [Xyvran/tgfs](https://github.com/Xyvran/tgfs): the base. Telegram
  storage, WebDAV + SFTP, at-rest encryption, RAID-1-style channel
  redundancy (`docs/design/channel-redundancy.md`, `tgfs/core/mirror.py`,
  `tgfs/core/backfill.py`).
* [VulcanoSoftware/dcfs](https://github.com/VulcanoSoftware/dcfs): a
  fork of tgfs that replaced Telegram with Discord. Useful as a template
  for the Discord client (`dcfs/discord/impl/discord_bot.py`), the
  Discord-specific message API (`dcfs/core/api/message/__init__.py`), the
  buffered uploader and the compact file-descriptor encoding. It is MIT
  licensed, so code can be ported with attribution in `LICENSE`/`NOTICE`.

## 1. Goals and non-goals

Goals:

1. One process serves one or more virtual file systems over WebDAV, SFTP
   and the manager API, exactly like tgfs today.
2. Every file system has one **primary store** and zero or more
   **mirror stores**. A store is a Telegram channel or a Discord channel.
   Primary and mirrors can be any mix: Telegram primary + Discord
   mirror, Discord primary + Telegram mirror, Telegram + Telegram (the
   current tgfs redundancy), Discord + Discord.
3. Swapping primary and mirror is a config change only, as tgfs already
   promises for Telegram mirrors.
4. Everything that works in tgfs keeps working unchanged: encryption,
   encrypted names, GitHub or pinned-message metadata, SFTP, transfer
   tuning, the task store, the manager API, the mini app.
5. An existing tgfs installation (config + metadata) must load without
   migration and behave identically.

Non-goals for the first release:

* FTP and SMB interfaces from dcfs (can be ported later, see phase 5).
* Erasure coding across stores; mirrors stay full copies (RAID-1), for
  the reasons given in the tgfs redundancy design.
* Other backends (archive.org, VK). The abstraction must allow them, the
  first release ships Telegram and Discord only.

## 2. What differs between Telegram and Discord

These differences drive every design decision below. Values marked
"verify" must be confirmed with a spike against the live API before
phase 2 starts, they change over time.

| Property | Telegram (tgfs) | Discord (dcfs) |
|---|---|---|
| Max bytes per message | 2 GiB per document (4 GiB with premium account upload) | 10 MB per attachment for bots on an unboosted server; 50 MB at boost level 2, 100 MB at level 3 (verify). dcfs defaults to 8 MB. Up to 10 attachments per message. |
| Upload path | Chunked `saveBigFilePart`, parallel workers, streamable | One multipart HTTP POST per message, the whole part is buffered in memory first |
| Download path | MTProto `getFile`, 1 MiB boundary rules, parallel pieces | HTTPS from the CDN with `Range` support. Attachment URLs are signed and expire (about 24 h), so the message must be fetched before each download (verify expiry) |
| Server-side copy | `forwardMessages`: free, no bandwidth, blocked on `noforwards` channels | Message forwarding exists since 2024 (`Message.forward` in discord.py 2.5), but whether the forwarded attachment becomes an independent copy that survives deletion of the original is unknown (verify with a spike). Assume "no cheap copy" until proven otherwise. |
| Text message limit | 4096 chars | 2000 chars for bots (dcfs assumes 4000; verify). dcfs solves overflow by sending the text as an `overflow.json` attachment. |
| Message ids | Per-channel sequential ints, small | 64-bit snowflakes, globally unique, above 2^53 (JavaScript loses precision) |
| Rate limits | Flood waits, tgfs limiter at 20 req/s | Per-channel send limit of about 5 messages per 5 s, global 50 req/s; bulk delete is limited to 100 ids and refuses messages older than 14 days (they need one request each) |
| Edit media | `editMessageMedia` | `Message.edit(attachments=...)` |
| Pinned messages | Unlimited in practice | 50 pins per channel |
| Bot login | Multiple bot tokens, optional user account | One or more bot tokens, no user account; needs the message content intent |
| Availability risk | Channel ban | Bot or server termination; storing large volumes of data through a bot is against the spirit of the terms of service, which is precisely why a mirror in another network is valuable |

Consequences:

* A Discord copy of a Telegram part cannot be "aligned" with it: one
  2 GiB Telegram message becomes about 200 Discord messages. The
  `mirrors: {channel: [ids aligned with messageIds]}` model of tgfs does
  not fit cross-backend replication and has to be generalized (section
  4.3).
* Cross-backend mirroring is always a re-upload: the bytes have to leave
  the process once more. It is bandwidth-bound and rate-limit-bound, and
  it must not sit inside the WebDAV PUT request path (section 4.5).
* File descriptors grow with Discord replicas (200 snowflakes per 2 GiB
  version). Both stores need a transparent overflow path for long
  descriptor texts (section 4.4).

## 3. Project setup

### 3.1 Repository

* Create `tgdcfs` as a flat copy of the tgfs working tree (master at
  the time of the copy) with one initial commit. The tgfs history is
  not carried over; the initial commit message records the tgfs commit
  hash the copy was taken from, so later tgfs fixes can be ported as
  patches (`git format-patch` from tgfs, `sed 's#/tgfs/#/tgdcfs/#'`,
  `git am`). Keep the module layout of tgfs so those patches apply.
* Rename the Python package from `tgfs` to `tgdcfs` in the same
  initial step (imports, `TGFS_*` environment variables, `~/.tgfs`
  data dir, Docker user, `TGFS` realm strings, docs). Keep the class
  prefix `TGFS` in the model (`TGFSFileDesc`, ...) for now; renaming
  those touches the serialized `type` tags in metadata and buys nothing.
* Not copied: `tgfs.png`, the tgfs-specific badges and the GitHub Pages
  URL in the README; the `tgfs-gh-pages` frontend is copied as
  `tgdcfs-gh-pages` and gets its own Pages deployment in phase 4.
* Environment variables become `TGDCFS_DATA_DIR`, `TGDCFS_CONFIG_FILE`,
  `TGDCFS_MASTER_PASSPHRASE`. Accept the old `TGFS_*` names as fallback
  for one release, with a deprecation log line.

### 3.2 Build and CI

* `pyproject.toml`: add `discord.py` and `aiohttp` as explicit
  dependencies. Python 3.13 stays.
* GitHub Actions: copy `test.yml`, `docker-publish.yml`,
  `docker-preview.yml`, `gh-pages.yml`; image names `xyvran/tgdcfs` and
  `xyvran/tgdcfs-fe`.
* The build pipeline has to be set up again for the new repository,
  nothing carries over from tgfs: create the Docker Hub repositories
  `xyvran/tgdcfs` and `xyvran/tgdcfs-fe`, add the `DOCKERHUB_USERNAME`
  and `DOCKERHUB_TOKEN` secrets, enable GitHub Pages for the frontend,
  and run the multi-arch build (linux/amd64 + linux/arm64) once by hand
  (`workflow_dispatch`) before the first tag. Existing tgfs deployments
  (web1, Jellyfin on Hetzner, the home PVE server) keep the `xyvran/tgfs`
  image until they are switched to `xyvran/tgdcfs` deliberately.
* Dockerfile: identical to tgfs apart from the package name, the user
  (`tgdcfs`) and the data dir.
* Keep `docs/design/` and add this document, later one design doc per
  phase where the phase changes a data format.

### 3.3 Package layout (target state)

```
tgdcfs/
  config.py                 # schema v2 + legacy tgfs loader
  reqres.py                 # unchanged request/response dataclasses
  backends/
    __init__.py             # registry: backend name -> factory
    base.py                 # IStoreClient, StoreCapabilities
    telegram/               # moved from tgfs/telegram (interface, telethon, pyrogram)
    discord/                # ported from dcfs/discord (interface, discord_bot)
  core/
    store.py                # Store = MessageApi bound to one channel of one backend
    api/                    # DirectoryApi, FileApi, FileDescApi, MetaDataApi, MessageApi
    mirror.py               # MirrorGroup -> replication engine (section 4.5)
    replication/            # queue, replicators (forward / reupload), backfill
    model/                  # TGFSFileVersion with replicas (section 4.3)
    repository/             # fd / file_content / metadata repos, backend-agnostic
  crypto/                   # unchanged
  app/                      # webdav, sftp, manager: unchanged apart from new endpoints
  tasks/                    # unchanged
```

## 4. Design

### 4.1 Store abstraction

Today tgfs has three layers that know about Telegram:

1. `ITDLibClient` (`tgfs/telegram/interface.py`): low-level RPCs.
2. `MessageApi` (`tgfs/core/api/message/__init__.py`): one channel,
   batching, caching, rate limiting, parallel download.
3. The repositories (`TGMsgFileContentRepository`, `TGMsgFDRepository`,
   `TGMsgMetadataRepository`) and `FileUploader`.

dcfs kept exactly this shape and swapped the implementations, so the
seam is already known. tgdcfs introduces one explicit interface at layer
2, the **store**, and makes the repositories depend on it only:

```python
class IStore(Protocol):
    key: str                        # serialization key, section 4.2
    caps: StoreCapabilities

    async def send_text(self, text: str) -> int
    async def edit_text(self, message_id: int, text: str) -> int
    async def get_text(self, message_id: int) -> Optional[str]      # overflow-aware
    async def get_messages(self, ids) -> list[Optional[MessageResp]]
    async def upload(self, file_msg: UploadableFileMessage) -> list[SentFileMessage]
    async def download(self, message_id: int, begin: int, end: int) -> DownloadFileResp
    async def replace_document(self, message_id: int, buffer: bytes, name: str) -> int
    async def delete(self, ids, force: bool = False) -> None
    async def pin(self, message_id: int) -> None
    async def get_pinned(self) -> MessageRespWithDocument
    async def copy_within(self, ids) -> list[int]                   # server-side, or reupload fallback
    async def copy_from(self, other: "IStore", ids) -> Optional[list[int]]
        # server-side copy from another store of the same backend; None when unsupported

@dataclass(frozen=True)
class StoreCapabilities:
    max_part_bytes: int             # 2 GiB / 4 GiB Telegram, 10 MB Discord (config)
    max_text_chars: int             # 4096 / 2000
    supports_server_copy: bool      # Telegram forward
    supports_edit_media: bool
    supports_bulk_delete: bool
    delete_age_limit_days: Optional[int]   # Discord: 14
```

`upload()` owns the partitioning: the Telegram store splits into 2 GiB
parts and streams them with the existing `FileUploader`; the Discord
store splits into `max_part_bytes` parts and buffers one part at a
time (dcfs `FileUploader`). The caller no longer knows the part size,
which is what allows a mirror in another backend to re-partition the
same bytes.

The existing `MessageApi` becomes the Telegram store implementation
almost unchanged (`TelegramStore`). The Discord store is ported from
dcfs: `DiscordBotAPI`, the overflow-aware `send_text`/`get_text`, the
`_is_transient` retry classification, the buffered uploader, the CDN
range download. Improvements to make while porting:

* honour the per-channel send rate limit with a per-store limiter
  instead of the global 20 req/s bucket;
* fetch the message right before each download so the signed CDN URL
  is fresh, and treat a 403/404 from the CDN as "refetch once, then
  fail";
* individual deletes for messages older than 14 days;
* optionally pack up to 10 attachments into one message (10x fewer
  messages per GiB). This changes the addressing to `(message_id,
  attachment_index)`; keep it behind a capability flag and out of the
  first release unless the message count turns out to be the bottleneck.

### 4.2 Store keys

Mirror maps in the metadata are keyed by a string that must be stable
for the life of the data and unique across backends. tgfs uses the
channel id as written in the config. tgdcfs uses

```
<backend>:<channel id as written in config>     e.g. tg:-1001234567890, dc:1234567890123456789
```

A key without a prefix is read as `tg:` so existing tgfs metadata loads
unchanged, and keys are written with the prefix from the first write on.
Config store names (section 5) are aliases for humans and never end up
in the metadata, so renaming a store in the config is safe.

### 4.3 Data model: replicas instead of aligned mirrors

`TGFSFileVersion` today:

```json
{"type": "FV", "id": "...", "updatedAt": 0, "messageIds": [1, 2], "size": 123,
 "mirrors": {"-1009876543210": [17, 18]}}
```

`mirrors` requires the copy to have the same number of parts as the
primary. That holds for Telegram forwarding and breaks for everything
else. tgdcfs adds a second, general map and keeps `mirrors` as the
compact special case:

```json
{"type": "FV", "id": "...", "updatedAt": 0, "messageIds": [1, 2], "size": 123,
 "mirrors":  {"tg:-1009876543210": [17, 18]},
 "replicas": {"dc:1234567890123456789": {"m": [..200 snowflakes..], "p": [10000000, 4711]}}}
```

* `mirrors[key]`: part ids aligned with `messageIds`; part sizes equal
  the primary's. Written by forward-mode replication.
* `replicas[key]`: an independent part layout, `m` message ids and `p`
  part sizes in the compact form dcfs uses (`[common, last]` when all
  parts but the last have the same size; `mb` base64 for more than 100
  ids). Written by reupload-mode replication.
* In memory both collapse into one structure, `Replica(store_key,
  message_ids, part_sizes)`, and the primary itself is just the replica
  with `store_key == primary`. Every reader works on a list of replicas.

`TGFSFileRef.mirrors` (descriptor copies per store) stays as it is: a
descriptor is one text message in every store, so the map of one id per
store fits both backends.

Encryption is unaffected by re-partitioning. The encryption decorator
wraps the content repository and works on the ciphertext byte stream;
the store below it only sees bytes and may cut them wherever it likes.
The inline 60-byte header stays at byte 0 of the stream, so a file is
still decryptable from a Discord replica alone.

### 4.4 Descriptor size and overflow

A version with a Discord replica carries about 200 message ids per
2 GiB. As JSON that is 4 KB per version, above the Telegram text limit
and far above Discord's. Two measures, both from dcfs:

1. Compact encoding of id lists (`mb` base64, 8 bytes per id, roughly
   half the size) and compact part sizes. Applies to `replicas` only,
   `messageIds` keeps its format for compatibility.
2. Transparent overflow in `send_text`/`edit_text`/`get_text` of every
   store: text longer than `caps.max_text_chars` is sent as a small
   document (`fd.json`, caption `TGDCFS_OVERFLOW`) and read back through
   `download`. The repositories never see the difference. Telegram
   descriptors that fit stay plain text, so existing installations keep
   their current on-channel format until a descriptor actually grows.

The GitHub-repo metadata backend is not affected (no length limits) and
remains the recommended choice when stores of different backends are
mixed: the directory tree then survives the loss of both networks.

### 4.5 Replication engine

`MirrorGroup` becomes `ReplicationGroup` with the same responsibilities
(mirror parts, mirror descriptors, mirror the pinned metadata, fan out
deletes, mark the primary dead) but with pluggable replicators:

* `ForwardReplicator`: used when primary and target are the same
  backend and `copy_from` succeeds (Telegram forward). Writes `mirrors`.
* `ReuploadReplicator`: streams the version from any healthy replica
  (`IFileContentRepository.get` on the replica list) into
  `target.upload()`, which re-partitions for the target. Writes
  `replicas`. Used for every cross-backend pair, for `noforwards`
  channels, and as the fallback when forwarding fails.
* `mode: auto` (default) picks forward when possible and falls back to
  reupload; `forward` and `reupload` force one and keep tgfs semantics.

Timing. tgfs replicates synchronously inside `save()`, which is fine for
a forward (one RPC) and unacceptable for a reupload into Discord (a
2 GiB PUT would wait for 200 rate-limited uploads). tgdcfs therefore
adds a replication queue:

* `sync: inline | background` per file system, default `inline` for
  forward-capable pairs and `background` otherwise (derived by the
  loader from the stores' backends and `mode`). With `background`
  the write path records the version with the primary only, enqueues
  `(fs, file ref, version id, target store)` and returns.
* The queue is persisted in the data dir (`replication.sqlite`, one row
  per unit of work) so a restart does not lose pending copies. The
  worker drains it with bounded concurrency per target store, honours
  rate limits, retries transient errors with backoff and reports through
  the existing task store (`TaskType.MIRROR_BACKFILL` gets a sibling
  `REPLICATION`).
* Commit point per unit of work is the descriptor edit that records the
  replica, exactly as in the tgfs backfill design. Orphaned copies from
  a crash between upload and commit are harmless and swept by the
  verification pass.
* `strict: true` remains available and implies `inline`.

Backfill (`core/backfill.py`) is kept and generalized: it walks the
tree, finds versions without a complete replica set for every configured
target, and enqueues them. Newest first, idempotent, resumable.

Deletion fans out per replica; for Discord the store handles the 14-day
rule internally. Descriptor copies and pinned metadata copies follow the
existing tgfs logic, they are text and small documents in every store.

### 4.6 Read path and failover

`_part_sources` in the content repository already tries the primary and
then each mirror, with a circuit breaker on the primary. It changes in
one way: it iterates replicas, and each replica maps the requested byte
range onto its own part layout (`_get_file_parts_indexed` already takes
the part sizes as input, it just needs to be called per replica). The
existing parallel download, chunk cache and read-ahead stay in the
Telegram store; the Discord store gets its CDN range download and the
dcfs producer/consumer part streaming.

Optional `read_preference: [store keys]` per file system lets a
deployment read from a mirror by default (for example a Discord mirror
close to the reader while the Telegram primary is far away). Default is
primary first.

### 4.7 Promotion

Swapping primary and mirror is a config change, as in tgfs. Because
every version knows its per-store ids and part layout, reads work
immediately. New uploads go to the new primary, and the old primary
becomes a mirror target: backfill then copies the versions the old
primary never had (it was primary, it has everything) and, in the other
direction, the versions that were only queued. A `promote` manager
endpoint that rewrites nothing but validates the swap (both stores
configured, metadata reachable) and prints the resulting config block
is enough; editing the YAML stays a human step.

### 4.8 Metadata backends

* `github_repo`: unchanged, backend-agnostic. Recommended for mixed
  deployments.
* `pinned_message`: unchanged in shape; the pinned blob lives in the
  primary store and is mirrored into each mirror store as today. Works
  on Discord (50-pin limit is irrelevant with one pin). The Telegram
  implementation needs a user account to read pins; the Discord one does
  not.

### 4.9 Manager API and UI

New or changed endpoints, all under `/api`:

* `GET /stores`: configured stores, backend, health (last error, dead
  until), capabilities.
* `GET /filesystems`: primary, mirrors, sync mode, queue depth.
* `POST /replication/backfill/{fs}`: replaces
  `/redundancy/backfill/{client_name}` (old path kept as alias).
* `GET /replication/queue`, `POST /replication/retry`.
* `GET /message/{store_key}/{message_id}`: currently keyed by channel
  id, becomes store key.

The mini app and the config generator in `tgfs-gh-pages` need a store
type selector and Discord fields (bot token, guild id, channel id, max
part size), plus JSON handling of snowflakes as strings (the manager API
should serialize message ids as strings, JavaScript numbers cannot hold
them).

### 4.10 Local cache: write staging and read cache

Every byte a mirror needs today is read back from the primary store
first: a Telegram primary with a Discord mirror downloads each gigabyte
once per mirror and uploads it again, and every mirror reads on its own.
A local cache on the data volume removes the read-back and, kept after
replication, serves repeated reads without touching a backend.

**Where it sits.** Inside `StoreFileContentRepository`, below the
encryption decorator, at the same level the replication engine copies
bytes verbatim. The cache therefore holds exactly what the stores hold:
ciphertext when encryption is on, never plaintext. It is one component,
`core/cache.py` (`LocalCache`), used by three callers: the write path,
the replication engine and the read path.

**Unit and layout.** The cache is keyed by *version*, not by store
message, because the stores cut a version differently (2 GiB Telegram
parts, 10 MB Discord messages). Each cached version is a sparse file
`cache/<fs>/<version id>.bin` plus a bitmap `<version id>.blocks` of
present blocks of `block_kb` (default 4 MiB, the download piece size).
A staged upload fills every block; a download fills the blocks it
fetched; a read is served from disk for the blocks that are present and
from the stores for the rest. Versions are immutable (a new upload is a
new version id), so an entry can never be stale; a deleted version
removes its entry in the delete fan-out.

**Write staging.** `save()` wraps the `UploadableFileMessage` so every
`read()` the uploader takes is also appended to the cache file. The
client sees no extra latency: bytes still stream to the primary as they
arrive, the copy to disk happens alongside through a bounded writer.
If the disk cannot keep up, the staging of that upload is dropped and
the upload continues as today; a failed upload removes its cache file.
The version is *pinned* until every mirror has its copy.

**Replication reads locally.** `MirrorGroup._replicate` and
`_reupload_one` read through a `VersionBytes` source: the cache when the
version is complete there, the store download otherwise, and with
`keep_for_reads` the download fills the cache on the way. Several
mirrors read the same file on disk instead of the primary store each.
When `backfill_file` reports no missing store the version is unpinned:
deleted with `keep_for_reads: false`, kept as an LRU entry otherwise.
The cache is an accelerator only: a missing or evicted entry means the
download path of today, never a failed replication.

**Read cache.** `get()` first takes the blocks of the requested range
that are on disk, then fetches the missing runs through the layouts (and
the multi-source scheduler of 4.11), writes whole blocks into the cache
and streams everything in order. Only whole blocks are cached, the last
block of a version being the one legitimate short block. Small reads
keep working the way they do now; the in-memory chunk cache of the
Telegram store stays as the hot layer in front of the disk.

**Budget and eviction.** `max_size_mb`, `max_files` (versions) and
`max_file_size_mb` (larger versions are neither staged nor cached).
Eviction is LRU by last access over unpinned versions; partial entries
count their present bytes. Pinned versions are never evicted. When the
budget is exhausted by pinned entries a new upload is not staged (one
warning, then the download path) rather than throwing away work that
already sits on disk. `ENOSPC` or any other disk error is treated the
same way: the cache steps aside, the transfer goes on.

**Index and recovery.** `cache/index.json` (version, file system, size,
present blocks, last access, pinned) is written atomically like
`replication_queue.json`; on startup it is rebuilt from the files when
missing or unreadable, pins are re-derived from the replication queue,
and files without an index entry or without a version in the metadata
are removed.

### 4.11 Multi-source reads

The read path serves a range from one layout and fails over to the next
layout from the byte where the previous one stopped (4.6). Parallelism
exists only inside the Telegram store, which splits a download into
pieces across its bots. With several stores holding a version, the
stores can share one download.

**Scheduler.** `read_parallel: true` on a file system cuts a range into
pieces of `transfer.download_piece_size_kb` and hands each piece to the
next store with a free slot; per-store slots default to the Telegram
`download_pieces_in_flight` and a small constant for Discord. A store
that finishes early takes the next piece, so a fast store does more of
the work without anyone estimating throughput. Output stays in order
through a bounded reorder window; the memory bound is the window times
the piece size, as it is today for the Telegram-internal split.

**Mapping.** A piece is a logical byte range. For each store it maps
through that store's layout (`_map_range` on the primary layout for the
primary and aligned mirrors, on the replica layout for a replica) to one
or more `(message id, offset)` segments, which the store's
`download_file` fetches and the scheduler concatenates. A piece that
spans two Discord messages is two segments from the same store.

**Failure.** A piece that fails on one store is re-queued to another;
this replaces the per-layout failover with a per-piece one and keeps the
"continue from the last delivered byte" guarantee. A store that fails
several pieces in a row is benched for the rest of the read, the way the
primary is marked dead today. A read fails only when no store can serve
a piece.

**Preference and scope.** `read_preference` keeps its meaning and
decides who gets a piece when several stores are idle. `read_sources`
optionally restricts parallel reads to some stores, for example to keep
a Discord mirror out of bulk media reads and save its rate limit. Reads
below `parallel_download_threshold_mb` stay on the single-store path: a
media player asking for 64 KiB needs latency, not bandwidth.

**With the cache.** Cache hits leave the piece list before scheduling,
misses go to the scheduler, and their results land in the cache when
`keep_for_reads` is on. The first full read of a file thus warms the
cache at the combined speed of every store.

## 5. Configuration

Schema v2. Everything under `tgdcfs:` is the old `tgfs:` block, renamed.
The old `telegram:` block with `private_file_channel` and `redundancy` is
still accepted and translated into stores and file systems on load, so
a tgfs `config.yaml` works as is.

```yaml
backends:
  telegram:
    api_id: 12345
    api_hash: "..."
    lib: telethon
    bot:
      session_file: bot.session
      tokens: ["..."]
    # account: {session_file: account.session, used_to_upload: false, used_to_download: false}
    delete_messages_on_remove: false
  discord:
    bot_tokens: ["..."]
    guild_id: 123456789012345678
    max_file_size_bytes: 10000000      # 10 MB unboosted, raise on boosted servers
    delete_messages_on_remove: false

stores:                                  # human names, never written to metadata
  tg-main:   {backend: telegram, channel: "-1001234567890"}
  tg-spare:  {backend: telegram, channel: "-1009876543210"}
  dc-mirror: {backend: discord,  channel: "1234567890123456789"}

filesystems:
  media:
    primary: tg-main
    mirrors: [dc-mirror, tg-spare]
    mode: auto               # auto | forward | reupload
    sync: background         # inline | background (default derived from mode and backends)
    strict: false
    read_preference: []      # optional, store names
    read_parallel: false     # spread the pieces of one download over every readable store (4.11)
    read_sources: []         # optional, restrict parallel reads to these stores
    metadata:
      type: github_repo
      github_repo: {repo: owner/repo, commit: master, access_token: "..."}
  notes:
    primary: dc-mirror       # Discord as primary, Telegram as mirror: same shape
    mirrors: [tg-main]
    metadata: {type: pinned_message}

tgdcfs:
  users: {...}
  jwt: {...}
  server: {host: 0.0.0.0, port: 1900}
  sftp: {...}
  transfer: {...}            # Telegram tuning, unchanged
  encryption: {...}          # unchanged, applies to every store
  cache:                     # local cache, see 4.10; off by default
    enabled: false
    dir: cache               # relative to the data dir, so it lives on the volume
    max_size_mb: 20480       # total budget, 0 = unlimited
    max_files: 0             # versions, 0 = unlimited
    max_file_size_mb: 4096   # larger versions are neither staged nor cached
    block_kb: 4096
    stage_uploads: true      # write incoming uploads to the cache for the mirrors
    keep_for_reads: true     # keep entries after replication, fill them from downloads
```

Validation on load: a store used as primary of one file system may be a
mirror of another only with an explicit `allow_shared_store: true`
(two trees writing into one channel invites id confusion); a file
system must not list its primary as a mirror; every store referenced
exists; backends referenced by stores are configured; `strict` implies
`sync: inline`.

The legacy translation: `telegram.private_file_channel[i]` becomes store
`tg-<channel>` and file system `tgfs.metadata[channel].name`;
`telegram.redundancy.mirrors[channel]` becomes that file system's
`mirrors`; `mode` and `strict` carry over; `sync` is `inline`.

## 6. Testing strategy

* Keep all 57 tgfs test modules green after the rename (phase 0 exit
  criterion).
* `tests/fakes/store.py`: an in-memory `IStore` with configurable
  capabilities (part size, text limit, server copy on/off, failure
  injection). Every replication, failover, overflow and backfill test
  runs against fakes in both roles, so cross-backend behaviour is tested
  without network: Telegram-like primary with Discord-like mirror and
  the reverse.
* Port the dcfs unit tests for the Discord client, overflow, retry
  classification and parallel download.
* Round-trip tests for the serialized model: old tgfs JSON (no prefix
  keys, `mirrors` only) loads and re-serializes byte-identically when no
  replica was added; `replicas` in compact and plain form round-trip.
* One opt-in integration test per backend behind environment variables
  with real tokens (upload, range download, delete, overflow), run
  manually and in the preview build, not in the default CI.
* Extend `scripts/docker_smoke_check.py` to start with a Discord-only
  config.

## 7. Phases

Each phase ends with green CI and a working Docker image; a phase may be
released on its own.

**Phase 0: bootstrap (small).** Repo from tgfs history, package rename,
env var fallbacks, CI and Docker names, this document. No behaviour
change.

**Phase 1: store abstraction (medium).** `IStore` and
`StoreCapabilities`; `MessageApi` becomes `TelegramStore`; repositories
and `MirrorGroup` depend on `IStore`; store keys with the `tg:` prefix
and prefix-less compatibility; config schema v2 with the legacy
translation; `/stores` and `/filesystems` endpoints. Still Telegram
only, all existing tests pass, a tgfs config runs unchanged.

**Phase 2: Discord backend (medium).** Port and adapt the dcfs Discord
client, store, uploader, overflow, retry; Discord-only file systems work
end to end over WebDAV and SFTP with encryption and both metadata
types; config generator gains Discord fields. Spikes at the start of
this phase: attachment limits per boost level, bot text limit, CDN URL
expiry, forward semantics.

**Phase 3: cross-backend replication (large).** `replicas` in the model
and descriptor overflow; `ReuploadReplicator`; the persistent background
queue and worker; generalized backfill; failover reads across replicas
with different part layouts; delete fan-out with the 14-day rule;
replication endpoints. This is the phase that delivers "Telegram
primary, Discord mirror" and the reverse.

**Phase 4: operations and docs (small to medium).** Promotion runbook and
validation endpoint; README sections for stores, file systems, Discord
setup, limits and the promotion procedure; getting-started page; example
configs; manager UI for queue and backfill progress.

**Phase 5: local cache and multi-source reads (large).** Sections 4.10
and 4.11, in this order so each step ships on its own:

Every step that adds a config field adds it to the config generator
(`tgdcfs-gh-pages/app/config-generator`) in the same step, with the
loader's validation mirrored in the form and the field left out of the
YAML when it equals the default (as `sync` is since Phase 4). A field
the generator does not know is a field the next deployment gets wrong.

1. `LocalCache` with index, budget, LRU eviction, pins and the startup
   sweep; the `cache` config block; write staging through the wrapped
   upload message; `VersionBytes` as the source of `_replicate` and
   `_reupload_one` with the download fallback; the replication worker
   unpins on success. Generator: a "Local cache" section (enabled,
   directory, budget by size, files and per-version size, block size,
   stage uploads, keep for reads) with the hint that the cache holds
   ciphertext and lives on the data volume. Delivers "mirroring without
   the read-back".
2. Read cache: `get()` serves present blocks from disk, fills missing
   whole blocks from the stores; the delete fan-out drops entries;
   `GET /api/cache` with size, entries, pinned bytes and hit ratio and
   `POST /api/cache/evict`; the mini app shows the same.
3. Multi-source scheduler behind `read_parallel`: piece queue, per-store
   slots, reorder window, per-piece failover and benching, the mapping
   through each store's layout, `read_sources`. The Telegram-internal
   piece split becomes one store's way of filling its slots. Generator:
   `read_parallel` and `read_sources` on the file system form, the
   sources limited to the file system's own stores, and a hint that the
   toggle only pays off with more than one readable store.
4. Docs: README sections for the cache (disk sizing, the volume, what
   is stored and that it is ciphertext) and for parallel reads;
   getting-started page; demo config.

Tests per step against the fake channels: staging fills the cache and
the mirror never calls `download_file`; a full cache leaves a write
unstaged and the replication still succeeds; an evicted entry falls back
to the download; `ENOSPC` does not fail a write; cache hits and misses
compose into the right bytes for every range shape; pieces from several
stores arrive in order; a failing store hands its pieces to the others;
a benched store is not asked again in that read.

**Phase 6: optional, not planned.** Multi-attachment Discord messages;
Discord forward as a `ForwardReplicator` if the spike shows copies are
independent; further backends. FTP and SMB from dcfs are explicitly out
of scope for now.

## 8. Risks and open points

* **Discord throughput.** Roughly one 10 MB message per second per
  channel is the realistic ceiling, so about 30 GB per hour per channel.
  Mirroring a terabyte-scale Telegram library into Discord takes days
  and produces 100 000 messages. Discord is a good mirror for the
  valuable subset and a poor one for bulk media; the plan supports
  per-file-system choices for that reason. Several Discord channels as
  parallel targets are not planned; one channel per store keeps the
  model simple.
* **Terms of service.** A bot that stores bulk data may be terminated.
  Mirrors are the answer, but a Discord primary should be reserved for
  data that is also mirrored elsewhere.
* **Descriptor growth.** Overflow keeps it correct, but descriptors of
  large files with Discord replicas become documents of tens of
  kilobytes that are rewritten on every version. Acceptable; noted.
* **Snowflakes in JavaScript.** Any JSON that reaches the browser must
  carry message ids as strings.
* **Memory per Discord upload.** One part (10 to 100 MB) per concurrent
  upload is buffered in memory; bound the concurrency per store.
* **Cache on disk (Phase 5).** The cache holds ciphertext, so a copied
  volume is as safe as the channel, but it is still a copy of the data
  on the host: say so in the README. A full or slow disk must never
  fail a transfer, which is why every cache error is a bypass. The
  budget is enforced by tgdcfs, not by the file system; running the
  cache dir on a volume shared with other data needs headroom for one
  version above the budget while a staging write is in flight.
* **Parallel reads and Discord (Phase 5).** Attachment downloads are
  plain CDN requests and not counted against the API rate limit, but a
  fresh URL for an expired one is an API call per message; a parallel
  read of an old replica can burst those. `read_sources` exists to keep
  a Discord mirror out of bulk reads where that matters.
* **Package rename and flat copy vs. upstream tracking.** A renamed
  package in a repository without the tgfs history means tgfs fixes are
  ported as patches with a path rewrite instead of cherry-picks.
  Accepted for a clean project.

Decisions taken (2026-09-24):

1. The package is named `tgdcfs`.
2. The repository starts from a flat copy of tgfs with one initial
   commit, not from the tgfs history. Build artefacts (Docker images,
   Pages) are set up fresh for the new repository, see section 3.2.
3. Cross-backend replication defaults to `sync: background`.
4. FTP and SMB from dcfs are not ported for now.

## 9. Progress

### Phase 0 (done)

Flat copy of tgfs at commit `7464666`, package renamed, `TGFS_*`
environment variables and the `tgfs:` config block accepted with a
deprecation warning, on-disk format constants kept.

### Phase 1 (done)

* `tgdcfs/backends/base.py`: `IStore`, `StoreCapabilities`, store keys
  (`make_store_key`, `normalize_store_key`). `tgdcfs/backends/telegram/`
  holds the moved Telegram code; `TelegramStore` (formerly `MessageApi`)
  owns the 2 GiB partitioning, `replace_document`, `copy_within` and
  `copy_from`; `factory.py` logs in and builds stores.
* Repositories are backend-agnostic: `StoreFileContentRepository`,
  `StoreFDRepository`, `PinnedMessageMetadataRepository`.
* `MirrorGroup` works on stores with `mode: auto | forward | reupload`;
  `auto` tries `copy_from` and falls back to re-uploading through
  `target.upload()`. A re-upload that the target splits into several
  messages is refused until replicas exist (phase 3).
* Config: `backends`, `stores`, `filesystems`; the tgfs layout is
  translated on load; validation as in section 5. `sync: background` is
  accepted and runs inline for now.
* Manager: `GET /stores`, `GET /filesystems`; `/redundancy` and the
  backfill endpoint kept.
* Metadata keys carry the backend prefix; bare keys from tgfs are read
  as `tg:`.

Deviations from the plan, found while implementing:

* **Promotion needed a data change.** tgfs assumed `messageIds` and
  the descriptor `messageId` belong to whatever store is configured as
  primary, so swapping primary and mirror would have pointed every id
  at the wrong channel. Versions and file refs now record the owning
  store key (`store` field, absent in tgfs metadata) and are relocated
  on load: the old primary's ids become that store's mirror entry, the
  new primary's copy becomes the primary ids, and a version without a
  copy in the new primary stays readable from the old store and is
  never written to by id in the new one. Copying such versions into the
  new primary is left to the replication queue (phase 3). Metadata
  written by tgfs has no `store` field and keeps the old assumption.
* **The primary must be reachable for validation.** `_validate_fv`
  now survives a primary that raises (banned channel) and checks the
  mirrors instead; tgfs only handled missing messages, not a dead
  store.
* **The pinned metadata blob must fit one primary part.** The pinned
  reader assumes a single-part document; that holds on Telegram (2 GiB)
  and needs attention before a Discord primary (10 MB parts) can carry
  `pinned_message` metadata for a large tree. Phase 2 item.

### Phase 2 (done)

Spike results, September 2026 (the Discord developer portal is not
reachable from the build environment, so these come from discord.py
2.7.1 and secondary sources; re-check against the live API when the
first bot runs):

| Question | Answer |
|---|---|
| Attachment limit per file | 20 MB unboosted (raised from 10 MB in August 2026), 50 MB at boost level 2, 100 MB at level 3; 10 attachments per message |
| Bot text limit | 2000 characters, Nitro does not apply to bots |
| CDN URL expiry | signed URLs (`ex`, `is`, `hm`), about a day; a freshly fetched message carries fresh URLs |
| Forwarding | a forward is an immutable snapshot referring to the original attachment, not an independent copy; unusable as a replication primitive |
| Bulk delete | at most 100 ids, none older than 14 days; older ones one by one |
| Pins | `channel.pins()` is a paginated iterator since discord.py 2.6 |

Implemented in `tgdcfs/backends/discord/`: `client.py` (discord.py
wrapper: send, edit, fetch, pins, deletion split by message age, CDN
range download that slices a 200 answer itself), `store.py`
(`DiscordStore`: partitioning at `max_file_size_bytes`, descriptor
overflow as an attachment resolved on read, retries on transient
errors, bounded concurrency, no server-side copy) and `factory.py`.
Config: `backends.discord` (`DiscordConfig`); a store with
`backend: discord` requires it. The pinned metadata repository refuses
a blob larger than one message of its store.

What works: a Discord primary (with `github_repo` or small
`pinned_message` metadata), Discord mirrors of a Discord primary, and
a Telegram mirror of a Discord primary (a Discord part always fits a
Telegram message). What waits for phase 3: a Discord mirror of a
Telegram primary, because a 2 GiB part has to become many Discord
messages (replicas with their own part layout).

### Phase 3 (done)

* `TGFSFileVersion.replicas`: copies with their own part layout,
  serialized compactly (`m`/`mb`, `p` as `[common, last]` plus `n`).
  `mirrors` stays the aligned case. Relocation after a promotion turns
  a replica in the new primary into the primary layout and the old
  layout into a replica.
* `MirrorGroup.mirror_parts` decides per store: same backend or parts
  that fit one message give an aligned copy, otherwise the whole
  version is streamed through the target (`_replicate`).
  `copy_into_primary` gives a version of a former primary a copy in the
  current one; the backfill calls it and counts `versions_promoted`.
* Reads (`StoreFileContentRepository.get`) work on layouts: the
  primary layout with per-part failover across aligned copies, then
  every replica, resuming at the byte where the previous layout
  stopped. `read_preference` reorders them. Validation
  (`StoreFDRepository._validate_fv`) accepts a version whose aligned
  copies are gone when a replica is complete.
* `tgdcfs/core/replication.py`: persistent `ReplicationQueue`
  (`replication_queue.json`) and one `ReplicationWorker` per file
  system with `sync: background`; the write path records the primary
  copy and queues the file, the worker runs the backfill's per-file
  unit of work. Manager: `GET /replication/queue`,
  `POST /replication/retry`.
* A pinned metadata copy is skipped for a mirror whose messages cannot
  hold the blob (content and descriptors are still mirrored).

### Phase 4 (done)

* README: stores and file systems, Discord backend, mirroring with
  replicas and background replication, promotion runbook; example
  configs; NOTICE.
* Manager API: `/stores`, `/filesystems`, `/replication/queue`,
  `/replication/retry`, backfill kept.
* Config generator (`tgdcfs-gh-pages/app/config-generator`): rebuilt
  around the current layout. Stores (name, backend, channel) and file
  systems (primary, mirrors, copy mode, sync, strict, shared-store
  flag, metadata type) with the loader's validation rules mirrored in
  the form; an optional Discord section (bot tokens, attachment limit
  by boost tier); the Telegram block is left out of the YAML when no
  store uses it. The old `ChannelField` is gone.
* Getting-started page: stores and file systems explained, Discord bot
  setup, bot admin rights on every Telegram store.
* Mini app: a "Mirrors and replication" dialog (`replication-dialog.tsx`)
  reachable from the explorer header shows every file system's stores,
  the replication queue with failed attempts, and offers "Retry failed"
  and "Backfill". `manager-client.ts` gained the matching calls.
* Not done, deliberately: the mini app still imports messages by
  Telegram channel id only (`/message`, `/import`), which is what the
  Telegram Mini App is for.
