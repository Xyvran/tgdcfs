---
sidebar_position: 6
title: Local Cache & Performance Tuning
description: Tuning disk cache, write-back uploads, parallel multi-source reads, and transfer settings.
---

# ⚡ Local Cache & Performance Tuning

TGDCFS provides high-performance local disk caching, write-back uploads, parallel multi-source downloads, and granular transfer tuning.

---

## Local Disk Cache (`tgdcfs.cache`)

The local cache lives inside `$TGDCFS_DATA_DIR/cache/`. It operates on **ciphertext** (below the encryption layer) and optimizes uploads and downloads.

```yaml
tgdcfs:
  cache:
    enabled: true
    dir: "cache"              # Path relative to TGDCFS_DATA_DIR
    max_size_mb: 20480        # Total cache budget (20 GB)
    max_files: 0              # Max cached versions (0 = unlimited)
    max_file_size_mb: 4096    # Max version size staged whole
    block_kb: 4096            # Block unit size (4 MB)
    stage_uploads: true       # Stage incoming uploads on disk for mirror workers
    keep_for_reads: true      # Keep entries after replication for repeat reads
    min_free_mb: 1024         # Disk space headroom to reserve
    max_age_hours: 0          # Drop entries unread after X hours (0 = disabled)
    target_fill_percent: 90   # Background sweep target
```

### Cache Features
- **Upload Staging (`stage_uploads: true`)**: Incoming client uploads write to disk simultaneously. Mirror workers upload to secondary channels from local disk instead of downloading from primary channel first!
- **Read Caching (`keep_for_reads: true`)**: Serves video seeking, media playback, and SFTP directory walks from local disk. Missing blocks are fetched on demand from cloud channels.
- **Sparse File Storage**: Read cache entries use sparse files. Only touched blocks consume disk space (`du`).
- **Disk Headroom Protection (`min_free_mb: 1024`)**: Ensures cache never exhausts disk space needed for metadata, session databases, or SFTP spools.

---

## Write-Back Uploads (`write_ack: cache`)

By default (`write_ack: primary`), a WebDAV/SFTP upload completes when the primary cloud channel receives the bytes.

Setting `write_ack: cache` enables **Write-Back Uploading**:

```yaml
filesystems:
  media:
    primary: tg-main
    mirrors: [dc-mirror]
    write_ack: cache   # Responds 201 Created as soon as file hits local disk!
```

### How Write-Back Works
1. Client upload writes to local disk cache at full local network speed.
2. TGDCFS returns `201 Created` immediately to the client.
3. Distribution workers upload the file from local disk to primary and mirror channels concurrently in the background.

> 🔒 **Durability Note**: Between client HTTP 201 response and cloud store completion, data lives on local disk. If disk is cleared or lost before background distribution completes, the pending file falls back to its previous version.

---

## Parallel Multi-Source Reads (`read_parallel: true`)

When a file exists across multiple complete stores (e.g., primary Telegram channel AND mirror Telegram/Discord channels):

```yaml
filesystems:
  media:
    primary: tg-main
    mirrors: [tg-spare, dc-mirror]
    read_parallel: true
    read_sources: [tg-main, tg-spare] # Optional: restrict participating stores
```

### Multi-Source Worker Pipeline
1. TGDCFS cuts the download request into logical pieces (`download_piece_size_kb: 4096`).
2. Concurrent async workers fetch pieces simultaneously from `tg-main` AND `tg-spare`.
3. Output pieces are reassembled in order through a reorder window (`read_parallel_window: 8`).
4. **Result**: Download speed equals the combined bandwidth of participating channels!

---

## Transfer Fine-Tuning (`tgdcfs.transfer`)

Fine-tune MTProto / REST connection pools and worker threads in `config.yaml`:

```yaml
tgdcfs:
  transfer:
    upload_workers_small: 3          # Workers for files < 10 MB
    upload_workers_big: 8            # Workers for files > 10 MB
    upload_part_size_kb: 512         # Telegram part size (512 KB max)
    download_piece_size_kb: 4096     # Read piece size (4 MB)
    download_pieces_in_flight: 4     # Pieces fetched concurrently per download
    parallel_download_threshold_mb: 10 # Min file size to trigger multi-source read
    upload_parts_in_flight: 1        # Parallel part uploads for seekable cache sources
    read_parallel_window: 8          # Piece reorder window
```

---

## Large Folders (`tgdcfs.listing`)

A folder listing (WebDAV `PROPFIND`, SFTP `ls`) shows the size and dates of
every file. The directory tree in the metadata only knows the names; size
and dates are in each file's descriptor message in the store. Listing a
folder of 10,000 files therefore reads 10,000 descriptors and checks that
the content messages of each one still exist: about 20,000 messages, which
on Telegram means hundreds of requests and on Discord one request per
message. Clients give up long before that.

The optional `listing` block makes this cheaper. Without it nothing changes.

```yaml
tgdcfs:
  listing:
    stat_in_metadata: false    # true: keep size and dates in the metadata
    validate_on_list: true     # false: check the content messages on read only
    message_cache_size: 1024   # messages kept in memory per channel
```

**`stat_in_metadata: true`** keeps each file's size and dates next to its
entry in the metadata, so a listing reads no descriptor at all:

* `pinned_message`: in `metadata.json`, as a `stat` field on each file.
* `github_repo`: in one `.tgdcfsstats.json` per folder, next to the file
  entries, keyed by descriptor message id. It is read on the folder's
  first listing after a start.

Nothing has to be prepared. A file whose values are not stored yet is read
from its descriptor on the first listing, and the values are written back
in the background: after 5 seconds without new values, at least once a
minute during a long listing, and on shutdown. A client that gives up on a
huge folder does not lose that work: the descriptor reads it started run to
the end, and the next attempt finds their results. A write to a file drops
its stored values, and the next listing reads them again.

Keep in mind:

* **Every instance that writes must support it.** An older tgdcfs or tgfs
  that overwrites a file leaves the stored size unchanged, and listings
  keep showing the old one. Instances that only read are fine; tgfs ignores
  the extra fields. tgfs logs an "Invalid name format" warning for each
  `.tgdcfsstats.json` it finds in a `github_repo` metadata repository.
* **The metadata grows** by about 70 bytes per file. With
  `pinned_message` on a Discord primary the whole `metadata.json` has to
  fit one attachment; a tree of very many files can outgrow it there.
* **Sizes and dates go into the metadata store.** With encryption on, they
  are the plaintext sizes; for a public `github_repo` repository that is
  more than the file names alone give away.
* With `github_repo`, writing the values is a commit (`Update file stats of
  N folder(s)`). Folder dates come from the git history and ignore these
  commits, so a listing does not make a folder look changed.
* Turning the option off again removes the stored values bit by bit: the
  stats file of a folder goes on the next write in that folder
  (`github_repo`), all values go on the next write anywhere
  (`pinned_message`).

**`validate_on_list: false`** skips the check of the content messages while
listing and halves the reads. The sizes are the ones the descriptor recorded
when the file was written. A file whose messages were deleted in the channel
by hand is then listed with its old size and fails on download, instead of
showing up with 0 bytes.

**`message_cache_size`** is how many messages are kept in memory per
channel (default 1024). A folder with more files than that evicts its own
descriptors during the listing, so the next listing reads all of them
again. Descriptor messages are small; 50,000 entries cost a few tens of
megabytes.

