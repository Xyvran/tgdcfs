---
sidebar_position: 7
title: Management API & Monitoring
description: REST endpoints reference for system status, stores, cache, and replication management.
---

# 📡 Management API & Monitoring

TGDCFS provides a RESTful **Manager API** served on port `1900` under `/api`.

---

## Authentication

Every request on the HTTP server — including `/api` and `/webdav` — requires an `Authorization` header (only `OPTIONS` and `POST /login` are exempt). Two schemes are accepted:

- **Basic**: `Authorization: Basic <base64(username:password)>`, checked against the users configured under `tgdcfs.users`.
- **Bearer**: `Authorization: Bearer <jwt_token>`, a JWT signed with the `tgdcfs.jwt` secret:

```http
Authorization: Bearer <jwt_token>
```

Tokens are issued by the login endpoint on the same server:

#### `POST /login`

Body: `{"username": "...", "password": "..."}`. Returns `{"token": "<jwt_token>"}`. The token carries the user's `readonly` flag and expires after `tgdcfs.jwt.life` seconds. `/login` itself requires no `Authorization` header. When no users are configured at all, an anonymous read-only token is returned without credentials.

Read-only users may call read-only endpoints; write operations (such as `POST /api/cache/evict`, `POST /api/import` or replication retries) answer `403 Forbidden` for them.

---

## Endpoint Reference

### 1. Store & Filesystem Information

#### `GET /api/stores`
Returns an object keyed by store name with backend, channel, serialization key, the file systems the store belongs to, and (for stores that are running) their capabilities.

**Example Response**:
```json
{
  "tg-primary": {
    "backend": "telegram",
    "channel": "-1001234567890",
    "key": "tg:-1001234567890",
    "primary_of": ["media"],
    "mirror_of": [],
    "capabilities": {
      "max_part_bytes": 2097152000,
      "max_text_chars": 4096,
      "supports_server_copy": true,
      "supports_edit_media": true
    }
  }
}
```

The `capabilities` entry is omitted for stores whose file system failed to start. `primary_dead` on `/api/filesystems` is a circuit breaker, not a live probe: it is `true` for 60 seconds after a read from the primary store failed, during which reads prefer the mirrors.

#### `GET /api/filesystems`
Returns an object keyed by file system name with primary/mirror stores, mirroring settings, and primary-store health.

**Example Response**:
```json
{
  "media": {
    "primary": "tg-primary",
    "mirrors": ["dc-mirror"],
    "mode": "auto",
    "sync": "background",
    "strict": false,
    "read_preference": [],
    "read_parallel": true,
    "read_sources": ["tg-primary"],
    "write_ack": "primary",
    "metadata": "github_repo",
    "primary_dead": false
  }
}
```

---

### 2. Cache Operations

#### `GET /api/cache`
Reports local disk cache status. When the cache is disabled, returns `{"enabled": false}`.

**Example Response**:
```json
{
  "enabled": true,
  "directory": "/home/tgdcfs/.tgdcfs/cache",
  "entries": 42,
  "complete_entries": 40,
  "used_bytes": 1073741824,
  "max_size_bytes": 21474836480,
  "max_files": 0,
  "pinned_entries": 2,
  "pinned_bytes": 104857600,
  "writing_entries": 1,
  "hits": 512,
  "misses": 64,
  "disk_free_bytes": 161716543488,
  "min_free_bytes": 1073741824,
  "target_bytes": 19327352832,
  "max_age_hours": 0,
  "last_sweep": 1735689600.0,
  "per_filesystem": {}
}
```

#### `POST /api/cache/evict`
Triggers an immediate cache eviction pass (`400` when the cache is disabled), dropping every cache entry no mirror is waiting for. Returns the number of evicted entries plus the post-eviction stats.

---

### 3. Replication & Tasks

#### `GET /api/replication/queue`
Returns pending background replication queue items, keyed by file system. Returns `{}` when no queue is active.

#### `POST /api/replication/retry`
Clears the backoff of failed replication items so the workers retry them now. Pass `?filesystem=<name>` to retry only that file system's queue.

#### `GET /api/redundancy`
Redundancy overview per file system, keyed by mirror store key (kept for the tgfs manager UI; `/api/filesystems` is the fuller view).

#### `POST /api/redundancy/backfill/{filesystem}`
Triggers a full scan and backfill task for the specified file system (`404` for an unknown file system, `400` when it has no mirror stores). Pass `?verify=true` to re-verify existing copies. Returns `{"task_id": "..."}`; progress is reported through `/api/tasks` (type `mirror_backfill`).

#### `GET /api/tasks`
Returns the status of active and historical tasks. Task types: `upload`, `download`, `mirror_backfill` and `distribution` (moving a `write_ack: cache` upload from the local cache into the stores). Tasks are only created for uploads, backfills and distributions today; `download` is defined but not created yet. Pass `?path=<path>` to filter tasks under a specific path.

#### `GET /api/tasks/{task_id}`
Returns a single task; `404` when unknown.

#### `DELETE /api/tasks/{task_id}`
Deletes a task record; `404` when unknown.

#### `GET /api/message/{channel_id}/{message_id}`
Looks up a Telegram message (used by the Mini App import flow); returns id, file size, caption and MIME type.

#### `POST /api/import`
Imports a pre-existing Telegram message document into the virtual file system. Body: `directory`, `name`, `channel_id`, `message_id`. The message must already be in the primary Telegram channel of the file system that owns `directory`; otherwise the call answers `400` (forward the message to that channel first).

---

## Health Monitoring

`/api/stores` and `/api/filesystems` report the configuration (plus the circuit-breaker flag above); they do not contact Telegram or Discord. A probe on them only shows that the HTTP server is up. Like every request they need an `Authorization` header, so a bare `curl -f` answers `401`:

```bash
curl -f -u probe_user:probe_password http://localhost:1900/api/stores || exit 1
```

When `tgdcfs.users` is empty, any Basic credentials are accepted (as the read-only `anonymous` user), but the header must still be present.

### Transport security and CORS

The server speaks plain HTTP: there is no TLS option in `config.yaml`. Basic credentials and bearer tokens travel unencrypted unless you put a TLS-terminating reverse proxy in front. The HTTP server also answers CORS requests from any origin (with credentials allowed), which is what lets the hosted Mini App talk to your server.
