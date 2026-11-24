---
sidebar_position: 3
title: Local Testing & Quality Assurance
description: Guide to running unit tests, static typechecking, linting, and smoke tests locally.
---

# 🧪 Local Testing & Quality Assurance

TGDCFS maintains high test coverage and strict type safety. All code changes must pass unit tests, type checks, and linter rules before being merged.

---

## Testing Pipeline Overview

Commands to run before submitting any code:

```bash
# 1. Run unit tests
poetry run pytest

# 2. Run static type checker
poetry run mypy .

# 3. Run linter and code style checks
poetry run ruff check .
```

---

## 1. Running Unit Tests with `pytest`

The test suite contains over 75 test modules covering all subsystems (`tests/`).

### Basic Test Invocations

The tests read a configuration through the environment. The Makefile targets set it for you:

```bash
make test   # TGDCFS_DATA_DIR=. TGDCFS_CONFIG_FILE=config-test.yaml pytest
make cov    # same, with --cov --cov-report=term-missing
```

When invoking `pytest` directly, export the variables first — without them many test modules fail at collection because no config file is found:

```bash
TGDCFS_DATA_DIR=. TGDCFS_CONFIG_FILE=config-test.yaml poetry run pytest
```

Run the complete test suite:
```bash
make test
```

Run a specific test file:
```bash
poetry run pytest tests/tgdcfs/core/test_local_cache.py
```

Run a specific test function with verbose output:
```bash
poetry run pytest tests/tgdcfs/core/test_replication.py -k "test_reads_fall_over_to_the_replica" -v
```

Generate test coverage report:
```bash
poetry run pytest --cov=tgdcfs --cov-report=term-missing
```

---

## 2. Testing with In-Memory Fake Stores (`tests/fakes/store.py`)

You **do not need live Telegram or Discord API tokens** to write or run unit tests!

TGDCFS provides a feature-rich in-memory fake store implementation in `tests/fakes/store.py`:
- `FakeStore`: Implements `IStore` entirely in memory.
- Configurable capabilities (`max_part_bytes`, `max_text_chars`, `supports_server_copy`).
- Fault injection: Simulate network timeouts, `FloodWait` rate limits, transient channel errors, or dead stores.

Example unit test using `FakeStore` (`pytest-asyncio` runs in `auto` mode, so async tests need no marker):

```python
from tests.fakes.store import FakeStore


async def test_fake_store_send_and_fetch():
    # A fake Discord channel: 10 MB parts, no server-side copy
    store = FakeStore(
        key="dc:123456",
        backend_name="discord",
        max_part_bytes=10_000_000,
        max_text_chars=2000,
        server_copy=False,
    )

    msg_id = await store.send_text("Hello TGDCFS")
    [message] = await store.get_messages([msg_id])
    assert message.text == "Hello TGDCFS"
```

Fault injection is a set of method names: `store.fail.add("download_file")` makes the next `download_file` calls raise, which is how the replication and failover tests simulate network trouble.

---

## 3. Static Typechecking with `mypy`

TGDCFS uses strict `mypy` configuration for full static type coverage:

```bash
poetry run mypy .
# or use the Makefile shortcut
make mypy
```

Ensure no type errors (`Success: no issues found in XX source files`) remain.

---

## 4. Linting & Code Style with `ruff`

TGDCFS uses **Ruff** for fast linting and code formatting:

```bash
# Check code for lint errors
poetry run ruff check .

# Auto-fix fixable lint errors
poetry run ruff check --fix .

# Makefile shortcut
make ruff
```

---

## 5. Docker Smoke Check Script (`scripts/docker_smoke_check.py`)

To verify that a built Docker image or container starts properly and serves SFTP/WebDAV requests:

```bash
docker run --rm -v "$PWD/scripts:/app/scripts:ro" <namespace>/tgdcfs:latest \
  python scripts/docker_smoke_check.py
```
