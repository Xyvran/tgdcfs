"""Storage backends.

A backend is a chat network that tgdcfs stores files in; a *store* is
one channel of one backend, bound to the credentials of that backend.
The rest of the code base talks to :class:`tgdcfs.backends.base.IStore`
only, so adding a backend means implementing that interface and
registering its name in :data:`BACKEND_NAMES`.
"""

from .base import (
    IStore,
    StoreCapabilities,
    make_store_key,
    normalize_store_key,
    parse_store_key,
)

# Names accepted in ``stores.<name>.backend``. Discord is added in phase 2.
BACKEND_NAMES = ("telegram",)

# Prefix of the serialization key per backend, see ``make_store_key``.
BACKEND_PREFIXES = {"telegram": "tg", "discord": "dc"}

__all__ = [
    "BACKEND_NAMES",
    "BACKEND_PREFIXES",
    "IStore",
    "StoreCapabilities",
    "make_store_key",
    "normalize_store_key",
    "parse_store_key",
]
