---
sidebar_position: 3
title: Complete Configuration Reference
description: Detailed schema and reference guide for config.yaml.
---

# ⚙️ Complete Configuration Reference (`config.yaml`)

TGDCFS is configured using a single YAML file located at `$TGDCFS_DATA_DIR/config.yaml`. The `TGDCFS_CONFIG_FILE` environment variable overrides the file *name*; it is resolved relative to `TGDCFS_DATA_DIR`, not the working directory.

---

## Complete Example `config.yaml`

```yaml
backends:
  telegram:
    api_id: 123456
    api_hash: "0123456789abcdef0123456789abcdef"
    bot:
      session_file: bot.session
      tokens:
        - "123456789:ABCdefGHIjklMNOpqrsTUVwxyZ"
    delete_messages_on_remove: false

  discord:
    bot_tokens:
      - "BotTokenString1"
      - "BotTokenString2"
    max_file_size_bytes: 20000000  # 20 MB attachment limit
    delete_messages_on_remove: false

stores:
  tg-primary:
    backend: telegram
    channel: "-1001234567890"

  dc-mirror:
    backend: discord
    channel: "123456789012345678"

filesystems:
  media:
    primary: tg-primary
    mirrors:
      - dc-mirror
    mode: auto           # auto | forward | reupload
    sync: background     # inline | background | tee
    strict: false
    write_ack: primary   # primary | cache
    read_parallel: true
    read_sources:
      - tg-primary
    metadata:
      type: github_repo
      github_repo:
        repo: "your-username/tgdcfs-metadata"
        commit: "master"
        access_token: "ghp_yourPersonalAccessToken"
        tree_fetch_concurrency: 8  # parallel directory reads when rebuilding a big tree

tgdcfs:
  users:
    admin:
      password: "SuperSecretPassword123"
      readonly: false
    guest:
      password: "GuestPassword123"
      readonly: true

  jwt:
    secret: "change-me-to-a-long-random-string"
    algorithm: "HS256"
    life: 604800

  server:
    host: "0.0.0.0"
    port: 1900

  sftp:
    enabled: true
    host: "0.0.0.0"
    port: 2222
    host_key_file: "sftp_host_key"
    # authorized_keys_dir: "sftp_authorized_keys"
    # upload_buffer_size_mb: 64
    # upload_buffer_dir: ""

  encryption:
    enabled: true
    encrypt_names: true
    passphrase_env: "TGDCFS_MASTER_PASSPHRASE"
    master_salt_file: "master.salt"
    chunk_size: 65536

  cache:
    enabled: true
    dir: "cache"
    max_size_mb: 20480
    stage_uploads: true
    keep_for_reads: true
    min_free_mb: 1024

  transfer:
    upload_workers_small: 3
    upload_workers_big: 8
    upload_part_size_kb: 512
    download_piece_size_kb: 4096
    download_pieces_in_flight: 4
```

---

## Configuration Section Details

### 1. `backends` Section
Configures authentication tokens and connection parameters for Telegram and Discord:

The `telegram` block can be left out for a Discord-only setup (no store may then use the `telegram` backend). Singular `bot.token` and `discord.bot_token` are accepted next to the lists.

- **`telegram`**:
  - `api_id` *(int)*: Telegram API App ID from [my.telegram.org](https://my.telegram.org).
  - `api_hash` *(string)*: Telegram API App Hash.
  - `bot.tokens` *(list of strings)*: Telegram Bot tokens from [@BotFather](https://t.me/BotFather). Providing multiple tokens distributes download/upload operations across tokens.
  - `bot.session_file` *(string)*: Telethon session file for the bot, relative to `TGDCFS_DATA_DIR` (default: `bot.session`).
  - `lib` *(pyrogram | telethon)*: MTProto client library used by the Telegram backend (default: `telethon`).
  - `account` *(optional object)*: A regular Telegram user account in addition to the bots, with `session_file` (relative to `TGDCFS_DATA_DIR`), `used_to_upload` and `used_to_download` flags. Parts above the bot limit (up to 4,194,304,000 bytes) are only uploaded when this account has `used_to_upload: true` and Telegram Premium.
  - `delete_messages_on_remove` *(bool, default `false`)*: Delete the channel messages when a file is removed from the file system; without it messages are kept (best-effort deletion is gated on this flag).
- **`discord`**:
  - `bot_tokens` *(list of strings)*: Discord Bot tokens from the Discord Developer Portal.
  - `max_file_size_bytes` *(int)*: Maximum attachment size per uploaded message (default: `10000000` = 10 MB). Adjust based on your Discord server boost tier (Level 0: 20 MB, Level 2: 50 MB, Level 3: 100 MB).
  - `delete_messages_on_remove` *(bool, default `false`)*: Delete the channel messages when a file is removed from the file system.
  - `upload_max_retries` *(int, default `10`)*: Retries per failed attachment upload.
  - `upload_retry_interval` *(float, default `5.0`)*: Seconds between upload retries.
  - `max_concurrent_uploads` *(int, default `max(3, number of bot tokens)`)*: Concurrent attachment uploads across the bot pool.
  - `max_concurrent_downloads` *(int, default `3`)*: Concurrent attachment downloads across the bot pool.

---

### 2. `stores` Section
Defines logical store names mapped to channels:
- `backend` *(telegram | discord)*: Backend driver name.
- `channel` *(string)*: Channel ID (e.g. `-1001234567890` for Telegram or `123456789012345678` for Discord).

---

### 3. `filesystems` Section
Defines top-level virtual directories exposed via WebDAV/SFTP:
- `primary` *(string)*: Name of primary store defined in `stores`.
- `mirrors` *(list of strings)*: Names of mirror stores.
- `mode` *(auto | forward | reupload)*:
  - `auto`: Uses zero-bandwidth server-side copy (forwarding) when possible, falls back to reuploading.
  - `forward`: Requires server-side forwarding (Telegram to Telegram only).
  - `reupload`: Re-downloads and streams uploaded bytes to mirror store.
- `sync` *(inline | background | tee)*: when omitted it is `background` if some mirror has to re-upload (another backend, or any Discord store involved) and `inline` otherwise (Telegram-to-Telegram forwarding).
  - `inline`: Writes wait for primary and mirror operations to finish before responding to client.
  - `background`: Responds after primary write completes; queues mirror operations to background queue.
  - `tee`: Uploads to primary and reuploading mirrors simultaneously from incoming byte stream; mirrors that can be filled by server-side forwarding are still copied inline.
- `strict` *(bool, default `false`)*: If `true`, fails the client write if any mirror write fails. Requires `sync: inline`; the loader refuses it with any other `sync`.
- `write_ack` *(primary | cache)*:
  - `primary`: Client receives 201 Created after primary channel write finishes.
  - `cache`: Client receives 201 Created as soon as upload lands in local cache disk directory. Requires `tgdcfs.cache.enabled: true` and cannot be combined with `sync: tee` or `strict: true`.
- `read_preference` *(list of strings)*: Order in which stores are tried for reads when `read_parallel` is off. Every name must be the file system's primary or one of its mirrors.
- `allow_shared_store` *(bool, default `false`)*: Allow a store that is the primary of one file system to also be a mirror of another. A store can never be the primary of two file systems, with or without this flag.
- `metadata` *(object)*: `type` is `pinned_message` (default) or `github_repo` (which then requires a `github_repo` block).

---

### 4. `tgdcfs` Section
Main application parameters:
- **`users`** and **`server`** are required keys (like `jwt`); `users` may be an empty mapping.
- **`users`**: User accounts for WebDAV and SFTP authentication.
  - `readonly: true`: Users can list and download, but upload/delete operations return permission denied.
  - When `users` is empty, every login is served as a read-only `anonymous` user. SFTP needs no credentials then; HTTP/WebDAV still requires an `Authorization` header, but accepts any credentials in it.
- **`jwt`** *(required)*: Bearer-token settings used by the Manager API, the Telegram Mini App and WebDAV clients that log in via `POST /login` (see [Management API](./management-api.md)). This section has no default; the config loader refuses to start without it.
  - `secret` *(string)*: Secret used to sign the tokens. Use a long random string.
  - `algorithm` *(string)*: JWT signing algorithm (e.g. `HS256`).
  - `life` *(int)*: Token lifetime in seconds (`604800` = 7 days in `demo-config.yaml`).
- **`server`**: Host and port of the HTTP server that serves WebDAV (`/webdav`), the Manager API (`/api`) and `POST /login`.
- **`sftp`**: Optional SFTP interface configuration.
  - `enabled` *(bool, default `false`)*, `host` *(default `0.0.0.0`)*, `port` *(default `2222`)*, `host_key_file` *(default `sftp_host_key`; ed25519, mode `0600`, created on first start)*.
  - `authorized_keys_dir` *(optional)*: Directory with one `<username>` file per user, in the usual `authorized_keys` format, enabling public key login. The user must still exist under `users`, so the `readonly` flag keeps applying.
  - `upload_buffer_size_mb` *(int, default `64`)*: How much of an incoming upload is kept in memory before it spills over to `upload_buffer_dir`. SFTP never announces the file size up front while Telegram uploads need it, so a whole file is buffered before it is sent.
  - `upload_buffer_dir` *(optional)*: Spill directory for the upload buffer (the system temp directory when empty).
- **`encryption`**: Client-side AES-256-GCM settings (see [Security & At-Rest Encryption](./encryption.md)).
- **`cache`** (off unless `enabled: true`): Local disk cache configuration (see [Local Cache & Performance Tuning](./cache-and-performance.md)).
- **`listing`**: Folder listing behaviour (see [Local Cache & Performance Tuning](./cache-and-performance.md)).
- **`transfer`**: Fine-tuning piece size, concurrency workers, and buffers. Every value has a default, so the section can be left out entirely:
  - `upload_workers_small` *(int, default `3`)* / `upload_workers_big` *(int, default `8`)*: Upload workers for files below/above 10 MB.
  - `upload_part_size_kb` *(int, default `512`)*: Telegram part size; must divide 512 and not exceed it.
  - `download_piece_size_kb` *(int, default `4096`)* and `download_pieces_in_flight` *(int, default `4`)*: Read piece size and concurrency; peak buffering per download is their product.
  - `parallel_download_threshold_mb` *(int, default `10`)*: Minimum file size to trigger multi-source reads.
  - `upload_parts_in_flight` *(int, default `1`)*: Parallel part uploads when the source is seekable (a version in the local cache).
  - `read_parallel_window` *(int, default `8`)*: Piece reorder window for multi-source reads.
  - `connection_pool_size` *(int, default `1`)*: MTProto connections each bot opens.
  - `chunk_cache_mb` *(int, default `0` = off)*, `chunk_cache_readahead` *(int, default `2`)*, `chunk_cache_block_kb` *(int, default `1024`)*: In-memory cache for downloaded bytes.
  - `tee_buffer_mb` *(int, default `64`)*: Bytes buffered per mirror during `sync: tee` writes.

---

### Config written for tgfs

A `config.yaml` in the tgfs layout is translated on load: the top-level `tgfs:` block is read as `tgdcfs:` (deprecation warning), `telegram:` as `backends.telegram`, `telegram.private_file_channel` becomes the primary stores, `tgfs.metadata.<channel>` names each file system and `telegram.redundancy.mirrors` lists its mirrors. This layout cannot be combined with `stores`/`filesystems`.
