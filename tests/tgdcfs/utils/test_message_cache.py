import pytest

from tgdcfs.reqres import Document, MessageResp
from tgdcfs.utils import message_cache
from tgdcfs.utils.message_cache import (
    MessageCache,
    channel_cache,
    set_message_cache_size,
)


class TestMessageCache:
    def test_init(self):
        cache = MessageCache[str, int]()
        assert cache._lru.get_size() == 1024

    def test_get_existing_key(self):
        cache = MessageCache[str, int]()
        cache["key1"] = 100

        result = cache.get("key1")
        assert result == 100

    def test_get_nonexistent_key(self):
        cache = MessageCache[str, int]()

        result = cache.get("nonexistent")
        assert result is None

    def test_setitem_and_getitem(self):
        cache = MessageCache[str, int]()
        cache["test_key"] = 42

        assert cache["test_key"] == 42

    def test_contains(self):
        cache = MessageCache[str, int]()
        cache["exists"] = 1

        assert "exists" in cache
        assert "not_exists" not in cache

    def test_gets_multiple_keys(self):
        cache = MessageCache[str, int]()
        cache["a"] = 1
        cache["c"] = 3

        result = cache.gets(["a", "b", "c"])
        assert result == [1, None, 3]

    def test_gets_empty_list(self):
        cache = MessageCache[str, int]()

        result = cache.gets([])
        assert result == []

    def test_find_nonexistent(self):
        cache = MessageCache[str, int]()
        cache["exists1"] = 10
        cache["exists2"] = 20

        result = cache.find_nonexistent(["exists1", "missing1", "exists2", "missing2"])
        assert result == ["missing1", "missing2"]

    def test_find_nonexistent_all_exist(self):
        cache = MessageCache[str, int]()
        cache["a"] = 1
        cache["b"] = 2

        result = cache.find_nonexistent(["a", "b"])
        assert result == []

    def test_find_nonexistent_none_exist(self):
        cache = MessageCache[str, int]()

        result = cache.find_nonexistent(["missing1", "missing2"])
        assert result == ["missing1", "missing2"]

    def test_lru_behavior(self):
        cache = MessageCache[int, str]()

        # Fill cache beyond capacity would require more complex testing
        # For now, just verify basic functionality works
        for i in range(10):
            cache[i] = f"value_{i}"

        for i in range(10):
            assert cache[i] == f"value_{i}"


class TestMessageCacheSize:
    def teardown_method(self):
        set_message_cache_size(message_cache.DEFAULT_SIZE)

    def test_resizes_existing_and_new_channels(self):
        existing = channel_cache(-424242).id

        set_message_cache_size(5000)

        assert existing._lru.get_size() == 5000
        assert channel_cache(-434343).id._lru.get_size() == 5000

    def test_rejects_an_empty_cache(self):
        with pytest.raises(ValueError):
            set_message_cache_size(0)

    def test_resizes_the_per_client_document_caches(self):
        documents = channel_cache(-444444).documents_of("bot0")

        set_message_cache_size(4000)

        assert documents._lru.get_size() == 4000
        assert channel_cache(-444444).documents_of("bot1")._lru.get_size() == 4000


def _doc(reference: bytes) -> Document:
    return Document(
        size=1, id=1, access_hash=1, file_reference=reference, mime_type=None
    )


class TestPerClientDocuments:
    def test_each_client_has_its_own_documents(self):
        cache = channel_cache(-454545)
        cache.documents_of("bot0")[7] = _doc(b"bot0")

        assert cache.documents_of("bot0").get(7) == _doc(b"bot0")
        assert cache.documents_of("bot1").get(7) is None

    def test_forget_drops_the_message_for_every_client(self):
        cache = channel_cache(-464646)
        cache.id[7] = MessageResp(message_id=7, text="", document=None)
        cache.documents_of("bot0")[7] = _doc(b"bot0")
        cache.documents_of("bot1")[7] = _doc(b"bot1")
        cache.documents_of("bot1")[8] = _doc(b"other")

        cache.forget(7)

        assert cache.id.get(7) is None
        assert cache.documents_of("bot0").get(7) is None
        assert cache.documents_of("bot1").get(7) is None
        assert cache.documents_of("bot1").get(8) == _doc(b"other")
