[![Tests](https://img.shields.io/github/actions/workflow/status/Xyvran/tgdcfs/test.yml?branch=master&style=for-the-badge&label=tests)](https://github.com/Xyvran/tgdcfs/actions/workflows/test.yml)
[![Docker build](https://img.shields.io/github/actions/workflow/status/Xyvran/tgdcfs/docker-publish.yml?branch=master&style=for-the-badge&label=docker%20build)](https://github.com/Xyvran/tgdcfs/actions/workflows/docker-publish.yml)
[![Docker](https://img.shields.io/badge/docker-%230db7ed.svg?style=for-the-badge&logo=docker&logoColor=white)](https://hub.docker.com/r/xyvran/tgdcfs)

# tgdcfs

Telegram and Discord as storage backends behind one WebDAV and SFTP
server.

tgdcfs is a copy of [tgfs](https://github.com/Xyvran/tgfs) (taken at
commit `7464666`) that grows a second storage backend, Discord. Every
file system has a primary store and optional mirror stores, and a store
can be a Telegram channel or a Discord channel in either role. The
design is described in
[docs/design/architecture-plan.md](docs/design/architecture-plan.md);
the Discord backend, cross-backend mirroring in both directions and
background replication are in.

Many thanks to [WheatCarrier](https://github.com/TheodoreKrypton/tgfs)
for creating the original tgfs project this repository is built upon.

## Coming from tgfs

* The Python package, the Docker image (`xyvran/tgdcfs`) and the
  environment variables are renamed. `TGFS_DATA_DIR` and
  `TGFS_CONFIG_FILE` still work with a deprecation warning; the new
  names are `TGDCFS_DATA_DIR` and `TGDCFS_CONFIG_FILE`.
* The data directory inside the image is `/home/tgdcfs/.tgdcfs`
  (was `/home/tgfs/.tgfs`), so adjust the volume mount when switching
  images.
* A `config.yaml` written for tgfs loads unchanged: the top-level
  `tgfs:` block is read as `tgdcfs:` with a deprecation warning.
* The metadata format is unchanged; a channel written by tgfs is served
  by tgdcfs as is.

## Tested Clients
* [rclone](https://rclone.org/)
* [Cyberduck](https://cyberduck.io/)
* [WinSCP](https://winscp.net/)
* [Documents](https://readdle.com/documents) by Readdle
* [VidHub](https://okaapps.com/product/1659622164)

WinSCP, Cyberduck and rclone speak both protocols; the OpenSSH `sftp`
command line and `sshfs` work against the SFTP interface as well.

## Features
* Upload and download files to/from a private Telegram channel via WebDAV
* Uploads keep their modification time: clients that set it after the
  upload (CarotDAV, Windows Explorer, WebDAV sync tools) are honoured via PROPPATCH
* **Optional SFTP interface** serving the same tree next to WebDAV (see below)
* Group files on Telegram channels into folders
* Infinite versioning of files and folders (Folder versioning is only available when Metadata is maintained on Github repository)
* Importing files that are already on Telegram (Only via the Telegram Mini App)
* File size is unlimited (larger files are chunked into parts but appear as a single file to the user)
* Live streaming of videos
* **Optional at-rest encryption** (AES-256-GCM, see below)
* **Optional channel redundancy** (RAID-1-style mirroring to extra channels, see below)


## SFTP interface

Next to the HTTP surface (WebDAV under `/webdav`, manager API under `/api`)
TGDCFS can serve the very same file tree over SFTP. It is off by default;
switch it on with an `sftp` block in `config.yaml`:

```yaml
tgdcfs:
  sftp:
    enabled: true
    host: 0.0.0.0
    port: 2222
    host_key_file: sftp_host_key
    # authorized_keys_dir: sftp_authorized_keys
    # upload_buffer_size_mb: 64
    # upload_buffer_dir: ''
```

Both interfaces run in the same process and go through the same core, so
what you see over SFTP is what you see over WebDAV — including at-rest
encryption, channel redundancy and the manager UI's task list. SSH cannot
share a socket with HTTP, hence the separate port.

**Layout and accounts.** The root lists one directory per configured
file system, exactly like `/webdav` does, and everything below it is that
file system's tree. Accounts are the `tgdcfs.users` from the same config: a user
with `readonly: true` can list and download but gets "permission denied" on
uploads, deletes, renames and directory creation. With no users configured
at all, SFTP allows anonymous read-only access — the same deal the HTTP
side offers.

```bash
sftp -P 2222 <user>@<host>          # interactive
sshfs -p 2222 <user>@<host>:/ /mnt  # mount it
rclone config create tgdcfs sftp host=<host> port=2222 user=<user> pass=<pass>
```

**Host key.** On the first start an ed25519 key is generated at
`host_key_file` (relative paths resolve against the TGDCFS data directory)
with mode 0600, and its fingerprint is logged. Back it up together with the
rest of that directory: without it every restart presents a new host key
and clients refuse to connect until their `known_hosts` entry is cleared.

**Public keys** are optional. Point `authorized_keys_dir` at a directory
holding one file per user — named after the username, in the usual
`authorized_keys` format — and those users may log in with a key. They
still have to be listed under `tgdcfs.users` so the readonly flag keeps
applying; everyone else falls back to password authentication.

**Uploads are buffered.** Telegram needs a file's total size before the
first byte goes out and SFTP never announces it, so an incoming upload is
held in memory up to `upload_buffer_size_mb` (default 64) and spills to
`upload_buffer_dir` (the system temp directory when unset) beyond that.
Plan for that much scratch space when pushing large files. A transfer that
is cut off mid-flight is discarded rather than committed, so an interrupted
upload never truncates an existing file.

**Known limits**, all of them shared with the WebDAV interface: files
cannot be appended to or partially updated (every write stores a new
version), moving between two channels is refused, and there are no symlinks
or real POSIX permissions — `chmod`, `chown` and `touch -t` are accepted
and ignored so clients like `rsync` do not abort. (Over WebDAV, a
modification time set with PROPPATCH is stored, see above.)

## Stores and file systems

A **store** is one channel of one backend (Telegram or Discord), under
a name of your choosing. A **file system** is what you see
as a top-level directory over WebDAV and SFTP: it has one *primary*
store and any number of *mirror* stores, and the same store can play
either role. Swapping primary and mirror is a config change.

The [config generator](https://xyvran.github.io/tgdcfs/config-generator/)
writes this file in the browser, with the same validation rules the
loader applies; nothing you type leaves the page. The
[getting-started guide](https://xyvran.github.io/tgdcfs/getting-started/)
walks through the bots and channels it asks for.

```yaml
backends:
  telegram:
    api_id: 12345
    api_hash: "..."
    bot:
      session_file: bot.session
      tokens: ["..."]
    delete_messages_on_remove: false

stores:
  tg-main:  {backend: telegram, channel: "-1001234567890"}
  tg-spare: {backend: telegram, channel: "-1009876543210"}

filesystems:
  media:
    primary: tg-main
    mirrors: [tg-spare]
    mode: auto           # auto (default) | forward | reupload
    sync: inline         # inline | background; default: background when a mirror re-uploads
    strict: false        # true: an upload fails when mirroring fails
    metadata:
      type: pinned_message   # or github_repo, see below

tgdcfs:
  users: {...}
  jwt: {...}
  server: {host: 0.0.0.0, port: 1900}
```

The tgfs layout (`telegram.private_file_channel`, `tgfs.metadata` keyed
by channel id, `telegram.redundancy`) is still accepted and translated
on load, so a tgfs `config.yaml` works unchanged. The two layouts cannot
be mixed in one file.

Rules the loader enforces: every store a file system names exists; a
store is the primary of at most one file system; a file system does not
mirror into its own primary; a store that is the primary of one file
system is not a mirror of another unless that file system sets
`allow_shared_store: true` (two trees writing into one channel invites
id confusion); `strict: true` requires `sync: inline`.

The manager API describes the result: `GET /api/stores` lists the stores
with their backend, key and capabilities, `GET /api/filesystems` the
file systems with their primary, mirrors and mirroring settings.

## Discord backend

Discord channels can be stores too, as primaries or as mirrors. A
Discord store partitions files into attachments of
`max_file_size_bytes` (see the limits below), sends descriptors that
exceed a bot's 2000 characters as a small JSON attachment, and streams
downloads from the CDN with HTTP range requests.

```yaml
backends:
  discord:
    bot_tokens: ["..."]            # one or more bots, used round-robin
    max_file_size_bytes: 10000000  # per attachment; raise to match the server
    delete_messages_on_remove: false
    # upload_max_retries: 10
    # upload_retry_interval: 5
    # max_concurrent_uploads: 3
    # max_concurrent_downloads: 3

stores:
  dc-main: {backend: discord, channel: "123456789012345678"}

filesystems:
  notes:
    primary: dc-main
    mirrors: [tg-spare]            # a Telegram mirror of a Discord primary works
    metadata: {type: github_repo, github_repo: {...}}
```

Setting up the bot: create an application at
https://discord.com/developers/applications, add a bot, enable the
*Message Content* intent, and invite it to your server with *View
Channel*, *Send Messages*, *Manage Messages*, *Read Message History*,
*Attach Files* and *Pin Messages* on the channels you use as stores.
The channel id is the numeric id from *Copy Channel ID* (developer
mode).

Limits that shape a Discord store (as of September 2026):

* A bot may attach 20 MB per file on an unboosted server (10 MB before
  August 2026), 50 MB at boost level 2 and 100 MB at level 3. The
  default `max_file_size_bytes` is a conservative 10 MB; a value above
  the server's limit makes every upload fail.
* Roughly one message per second per channel is the practical send
  rate, so about 30 GB per hour per channel at 10 MB parts. Discord is
  a fine mirror for the valuable part of a library and a slow home for
  bulk media.
* Attachment URLs are signed and expire after about a day; TGDCFS
  fetches the message before every download, so this is invisible.
* Messages older than 14 days cannot be bulk deleted; they are deleted
  one by one, which is slow for large removals.
* There is no server-side copy: mirroring *into* a Discord store always
  re-uploads the bytes, and a Discord primary cannot be mirrored into
  Telegram by forwarding either.
* Mirroring a Telegram primary *into* Discord re-partitions every
  version into Discord-sized messages (a replica, see Mirroring) and
  re-uploads the bytes. Such a file system defaults to
  `sync: background` so uploads do not wait for it.
* `pinned_message` metadata must fit one attachment; use
  `github_repo` metadata for anything but small trees on a Discord
  primary.

Storing large volumes of data through a bot is not what Discord is
for, and a bot or server can be terminated. Treat a Discord primary as
something that is mirrored elsewhere.

## Mirroring

Telegram channels can be banned or deleted, taking every stored file with
them. With mirror stores configured, TGDCFS keeps a full copy of everything
in each of them:

* **Copies are server-side where possible.** Between two Telegram stores
  new uploads are copied via message forwarding — no re-upload, one API
  call per file part. Where a server-side copy is impossible (a channel
  with "Restrict saving content", or a mirror on another backend) the
  bytes are streamed down and up again. `mode: auto` picks the cheap
  path and falls back; `forward` insists on the server-side copy;
  `reupload` never asks for one.
* **Mirrors with smaller messages get replicas.** A store whose messages
  can hold the primary's parts receives an aligned copy, one message per
  part. A store whose messages are smaller (a Discord mirror of a
  Telegram primary: 2 GiB parts, 10 MB messages) receives a *replica*:
  the whole version streamed through the mirror, which cuts it its own
  way. Reads use whichever copy is available, switching layouts at the
  byte where the previous one stopped.
* **Slow mirrors run in the background.** With `sync: background` a
  write records the primary copy only and queues the file; a worker per
  file system copies it to the mirrors afterwards and commits the result
  into the descriptor. The queue lives in `replication_queue.json` in
  the data directory, so nothing is lost on a restart; failed items back
  off and retry. `GET /api/replication/queue` shows what is pending,
  `POST /api/replication/retry` retries failed items now. When `sync`
  is not set, a file system with a mirror that has to re-upload (a
  Discord store on either side, another backend, `mode: reupload`)
  runs in the background and Telegram-to-Telegram forwarding inline;
  a client would otherwise time out waiting for a large upload to pass
  through the mirror a second time. `strict: true` keeps `inline`. An
  explicit `sync: inline` or `strict: true` over such a mirror is
  accepted, and the server logs a warning at startup that names the
  mirrors every write will wait for.
* **Reads fail over automatically.** If a part (or the whole primary
  store) becomes unavailable, downloads are served from a mirror.
* **File descriptors are mirrored too**, and in `pinned_message` metadata
  mode a pinned copy of the metadata blob is maintained in every mirror,
  so a mirror store is self-sufficient.
* **Promotion is a config change.** Every descriptor and version written
  by TGDCFS records which store its ids belong to. When the primary is
  lost, make the mirror the `primary` and the old primary a mirror (or
  drop it): versions the new primary holds, aligned or as replicas, are
  served from it, versions it never received stay readable from the old
  store, and new uploads go to the new primary. The backfill task then
  copies the versions the new primary lacks into it and re-mirrors
  everything. Metadata written by tgfs carries no store information and
  is assumed to belong to the configured primary, so run the backfill
  task once under TGDCFS before relying on a swap. The runbook below has
  the steps.
* **Pre-existing files are covered by the backfill task**
  (`POST /api/redundancy/backfill/<filesystem>`, progress via the
  regular `/api/tasks` endpoints; add `?verify=true` to also re-mirror
  copies that were manually deleted). Between Telegram stores backfill
  forwards server-side as well, so mirroring a multi-terabyte library
  costs API calls, not bandwidth.
* **Works with encryption**: mirrors receive the ciphertext messages
  including the inline header, so files remain decryptable from a mirror
  alone.

Requirements: the bot(s) must be admin in every mirror channel. With
`strict: false` (default) a failing mirror never fails the upload; gaps
are logged and can be closed by re-running the backfill task.

When redundancy matters to you, prefer the `github_repo` metadata type:
the directory tree then survives even the loss of *all* stores. With
`pinned_message` metadata a mirror only holds a pinned copy when the
metadata blob fits one of its messages; a Discord mirror of a large
Telegram tree does not, and promotion to it then needs `github_repo`.

**Promotion runbook.** The primary store of `media` is gone (banned,
deleted) and `tg-spare` is its mirror:

1. Stop TGDCFS. Reads were already failing over to the mirror; writes
   need the new primary.
2. In `config.yaml` swap the roles: `primary: tg-spare`, and either keep
   the old store in `mirrors` (if it may come back) or remove it.
3. Start TGDCFS. Every file whose descriptor or versions live in the old
   store is re-expressed against the new primary on first access.
4. Run `POST /api/redundancy/backfill/media`. It copies versions the new
   primary never received into it (when the old store is still
   reachable and configured), mirrors everything into the remaining
   mirrors and rewrites the descriptors. `versions_promoted` in the
   report counts the copies made into the primary.
5. Add a fresh mirror store and run the backfill again to restore
   redundancy.

Verify with `GET /api/filesystems` (primary, mirrors) and
`GET /api/replication/queue` (nothing pending).

**Metadata keys.** Mirror entries in the metadata are keyed by the
store's key, `<backend prefix>:<channel id as configured>`, for example
`tg:-1001234567890`. Entries written by tgfs carry the bare channel id;
they are read as Telegram keys and written back with the prefix. Store
*names* never reach the metadata, so a store can be renamed freely.

## At-rest encryption

When ``encryption.enabled: true`` is set in ``config.yaml``, every byte
TGDCFS uploads to Telegram is encrypted client-side. The Telegram channel and
the metadata repository never see plaintext.

* **Cipher:** AES-256-GCM in 64 KiB chunks, each with its own nonce + auth tag.
  Random-access decryption (HTTP Range requests, video streaming) keeps working.
* **Keys:** the master key is derived from a passphrase via Argon2id at startup.
  Per-file keys are derived via HKDF-SHA256 from the master key and a 32-byte
  random salt stored in the file header.
* **Header:** each encrypted file starts with a self-describing 60-byte header
  embedded *inline* in the first Telegram message, so a file can be decrypted
  from the channel even if the TGDCFS metadata store is lost.
* **Tamper detection:** every chunk has its own GCM tag plus an HMAC on the
  header, so flipped bits or chunk reordering are caught before plaintext is
  returned.
* **Optional name obfuscation:** with ``encrypt_names: true`` the Telegram
  document name of every new upload (and the pinned metadata blob) is
  replaced with an AES-GCM ciphertext token. The plaintext names stay
  inside the (already encrypted) metadata, so WebDAV and the manager UI
  are unaffected. Only new uploads are obfuscated; pre-existing files keep
  their original document name in Telegram.

Set up:

```yaml
tgdcfs:
  encryption:
    enabled: true
    encrypt_names: true  # optional, hide file/dir names from channel observers
    passphrase_env: TGDCFS_MASTER_PASSPHRASE
    master_salt_file: master.salt
    chunk_size: 65536
```

### Master salt

The Argon2 master salt is the value referenced by ``master_salt_file``. It is
**not** secret, but it is required to re-derive the master key from your
passphrase, so it must survive container/host rebuilds.

* **Auto-generated on first start.** If ``master_salt_file`` does not exist
  when TGDCFS boots, 16 random bytes are written there via
  ``secrets.token_bytes`` and the file is ``chmod 0600``'d. No manual step is
  required.
* **Path resolution.** The value is resolved relative to ``TGDCFS_DATA_DIR``
  (defaults to ``~/.tgdcfs``), so ``master_salt_file: master.salt`` lands at
  ``~/.tgdcfs/master.salt`` unless you override the data dir.
* **Manual creation (optional).** If you prefer to seed the salt yourself --
  e.g. to push it into a secret manager before the first start -- generate at
  least 8 bytes (16 recommended) and drop them at the configured path:

  ```bash
  mkdir -p ~/.tgdcfs
  head -c 16 /dev/urandom > ~/.tgdcfs/master.salt
  chmod 600 ~/.tgdcfs/master.salt
  ```

* **Back it up, never rotate it in place.** Losing the salt (or replacing it
  with fresh random bytes) makes every previously uploaded file unreadable,
  even with the correct passphrase. Back ``master.salt`` up alongside your
  passphrase and your metadata.

See ``demo-config.yaml`` for the full set of options.

### Master passphrase

The master passphrase is the only secret an attacker needs to decrypt
every file in your channel, so treat it like a long-lived database
credential: never commit it, never log it, and back it up to the same
place you keep your other production secrets.

TGDCFS reads the passphrase from exactly one of three sources, checked in
this order:

1. ``passphrase_env`` -- the name of an environment variable to read
   (recommended for container deployments)
2. ``passphrase_file`` -- a path to a file containing just the
   passphrase (recommended for systemd via ``LoadCredential=``)
3. ``passphrase`` -- the literal passphrase inlined in ``config.yaml``
   (development only; the value ends up on disk in cleartext)

The recipes below all assume the default ``passphrase_env:
TGDCFS_MASTER_PASSPHRASE`` from ``demo-config.yaml``. Replace the variable
name if you picked a different one.

**Generate a strong passphrase** (only needed once -- store the output
in your password manager):

```bash
# 32 random base64 characters; ~190 bits of entropy.
python -c "import secrets; print(secrets.token_urlsafe(24))"
```

**Docker / docker-compose.** Pass the variable through to the
container -- never hard-code it into the image:

```bash
docker run -e TGDCFS_MASTER_PASSPHRASE \
  -v ~/.tgdcfs:/home/tgdcfs/.tgdcfs \
  xyvran/tgdcfs
```

```yaml
# docker-compose.yml
services:
  tgdcfs:
    image: xyvran/tgdcfs
    environment:
      TGDCFS_MASTER_PASSPHRASE: ${TGDCFS_MASTER_PASSPHRASE}
    volumes:
      - ~/.tgdcfs:/home/tgdcfs/.tgdcfs
```

Keep the actual value in a ``.env`` file next to ``docker-compose.yml``
(and add ``.env`` to ``.gitignore``):

```
TGDCFS_MASTER_PASSPHRASE=your-long-random-passphrase-here
```

**systemd.** Prefer a credential file managed by systemd so the secret
is mode-0400 and only visible to the unit:

```ini
# /etc/systemd/system/tgdcfs.service
[Service]
LoadCredential=master_passphrase:/etc/tgdcfs/master.passphrase
Environment=TGDCFS_MASTER_PASSPHRASE_FILE=%d/master_passphrase
ExecStart=/usr/local/bin/tgdcfs
```

Then set ``passphrase_file: ${TGDCFS_MASTER_PASSPHRASE_FILE}`` in
``config.yaml`` (or read the variable in a wrapper script). Make sure
``/etc/tgdcfs/master.passphrase`` is ``chmod 0400`` and owned by
``root:root``.

**Plain shell / development.** Export it in your current shell only;
do **not** persist it in ``~/.bashrc`` or ``~/.zshrc``:

```bash
read -rs TGDCFS_MASTER_PASSPHRASE && export TGDCFS_MASTER_PASSPHRASE
poetry run python main.py
```

**Kubernetes.** Store the passphrase in a Secret and project it as an
env var:

```yaml
apiVersion: v1
kind: Secret
metadata:
  name: tgdcfs-master-passphrase
type: Opaque
stringData:
  passphrase: your-long-random-passphrase-here
---
apiVersion: apps/v1
kind: Deployment
spec:
  template:
    spec:
      containers:
        - name: tgdcfs
          image: xyvran/tgdcfs
          env:
            - name: TGDCFS_MASTER_PASSPHRASE
              valueFrom:
                secretKeyRef:
                  name: tgdcfs-master-passphrase
                  key: passphrase
```

**Operational notes.**

* **Never change the passphrase in place.** TGDCFS has no built-in key
  rotation: changing the passphrase makes every previously uploaded
  file unreadable. Decrypt everything to a staging location and
  re-upload under a fresh passphrase if you really need to rotate.
* **Pair with the master salt.** Backups MUST include both the
  passphrase and ``master.salt`` -- either one missing is equivalent
  to a total key loss.
* **Forgetting the passphrase is fatal.** There is no recovery
  mechanism (that's the entire point of the design). Keep a copy in
  your password manager.


## Transfer performance

Uploads and downloads are tuned through an optional `transfer` block. Every
value has a default matching the behaviour you get without the block, so
existing configurations keep working untouched.

```yaml
tgdcfs:
  transfer:
    upload_workers_small: 3
    upload_workers_big: 8
    upload_part_size_kb: 512      # must divide 512; Telegram caps it there
    download_piece_size_kb: 4096
    download_pieces_in_flight: 4
    parallel_download_threshold_mb: 10
    connection_pool_size: 1
    chunk_cache_mb: 0
    chunk_cache_readahead: 2
    chunk_cache_block_kb: 1024
```

**Downloads** are cut into pieces and several are fetched at once. Bytes
must be handed out in order, so a piece that arrives early waits its turn:
peak buffering per download is `download_piece_size_kb x
download_pieces_in_flight`, 16 MiB at the defaults. Raising either speeds
up a single large download and costs memory for every concurrent reader.

**More bots means more parallelism.** Pieces are handed to the configured
bot tokens round-robin, so each additional token is another connection a
single download can use. With one token, raise `connection_pool_size`
instead: one MTProto connection sends its requests one after another, so
extra connections are what let a single bot overlap transfers.

**The chunk cache** (`chunk_cache_mb`, off by default) keeps downloaded
blocks in memory. It pays off for readers that revisit bytes: seeking in a
video, and SFTP clients that walk a file in small reads. A read of a few
kilobytes pulls a whole `chunk_cache_block_kb` block, so a larger block
serves more of the reads that follow it and wastes more on readers that
jump around. `chunk_cache_readahead` additionally pulls that many blocks
past each request so the next sequential read is already in memory.

**Rate limits.** More parallelism means more requests per second. Telegram
answers a flood with a wait, which TGDCFS honours; if uploads start logging
flood waits, lower `upload_workers_big` and `connection_pool_size` before
raising anything else.

`scripts/measure_transfer.py` shows what the piece-level parallelism is
worth against a simulated link, without needing a channel.

## Web frontend

The Next.js app in `tgdcfs-gh-pages/` is published at
<https://xyvran.github.io/tgdcfs/> and as the `xyvran/tgdcfs-fe` image.
It has:

* the [config generator](https://xyvran.github.io/tgdcfs/config-generator/)
  for the stores and file systems layout described above: Telegram and
  Discord backends, any number of stores, primary and mirror stores per
  file system with copy mode, sync and strictness, metadata as pinned
  message or GitHub repository, and the WebDAV, SFTP and manager
  settings. The YAML is generated on the fly and can be copied or
  downloaded as `config.yaml`. Tokens and passphrases stay in the
  browser.
* the [getting-started guide](https://xyvran.github.io/tgdcfs/getting-started/)
  for creating the Telegram app and bots, the Discord bot and the
  channels.
* the Telegram Mini App with a file explorer, background tasks and a
  "Mirrors and replication" view (queue, retry, backfill), served by the
  manager.

A push to `master` deploys the site through the "Deploy Next.js to
GitHub Pages" workflow; the image is built by the Docker workflows.

## Development

Install the dependencies:
```bash
poetry install
```

Run the app:
```bash
poetry run python main.py
```

Typecheck && lint:
```bash
make mypy
make ruff
```

Before committing and pushing, run the following command to install git hooks:
```bash
pre-commit install
```

### Preview builds

Pushing any branch other than `master` runs the full test suite and, in
parallel, builds a Docker image from that exact commit so a change can be tried
out on a real machine before it is merged. The release tags are never touched.

```bash
docker pull xyvran/tgdcfs:preview-<commit>     # pinned to one commit
docker pull xyvran/tgdcfs:preview              # whichever branch built last
```

The commit-pinned tag is printed as a ready-to-copy command in the workflow run
summary; prefer it whenever more than one branch is in flight. The frontend is
built the same way as `xyvran/tgdcfs-fe:preview-<commit>`.

Preview images are amd64 only (release images are also arm64) and are built
before the test workflow finishes, so a green preview image is not a statement
about the tests. Both images are smoke-checked first, though: the backend has to
serve a working SFTP session and the frontend has to answer on `/tgdcfs/`, so a
broken build never reaches the registry.

These tags accumulate — delete them from Docker Hub once a branch is merged.

The backend smoke check runs against any image, locally too:

```bash
docker run --rm -v "$PWD/scripts:/app/scripts:ro" xyvran/tgdcfs:preview \
  python scripts/docker_smoke_check.py
```
