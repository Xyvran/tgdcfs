"""Stores and file systems: the current config layout, the tgfs layout
translated on load, and the validation between the two."""

import pytest

from tgdcfs.config import Config, FilesystemConfig, MetadataType, StoreConfig

TELEGRAM = {
    "api_id": 12345,
    "api_hash": "hash123",
    "bot": {"token": "bot_token", "session_file": "bot.session"},
}
APP = {
    "users": {},
    "jwt": {"secret": "jwt_secret", "algorithm": "HS256", "life": 3600},
    "server": {"host": "0.0.0.0", "port": 8080},
}


def current_layout(**overrides):
    data = {
        "backends": {"telegram": dict(TELEGRAM)},
        "stores": {
            "tg-main": {"backend": "telegram", "channel": "-1001"},
            "tg-spare": {"backend": "telegram", "channel": "-1002"},
        },
        "filesystems": {
            "media": {
                "primary": "tg-main",
                "mirrors": ["tg-spare"],
                "metadata": {"type": "pinned_message"},
            },
        },
        "tgdcfs": dict(APP),
    }
    data.update(overrides)
    return data


class TestStoreConfig:
    def test_from_dict(self):
        store = StoreConfig.from_dict("main", {"backend": "telegram", "channel": -1001})
        assert store.backend == "telegram"
        assert store.channel == "-1001"
        assert store.key == "tg:-1001"

    def test_backend_defaults_to_telegram(self):
        assert StoreConfig.from_dict("main", {"channel": "x"}).backend == "telegram"

    def test_unknown_backend_rejected(self):
        with pytest.raises(ValueError, match="unknown backend"):
            StoreConfig.from_dict("main", {"backend": "carrier-pigeon", "channel": "x"})

    def test_unavailable_backend_rejected(self):
        # Known in the key scheme, not implemented yet.
        with pytest.raises(ValueError, match="not available"):
            StoreConfig.from_dict("main", {"backend": "discord", "channel": "1"})

    def test_channel_required(self):
        with pytest.raises(ValueError, match="channel"):
            StoreConfig.from_dict("main", {"backend": "telegram"})


class TestFilesystemConfig:
    def test_defaults(self):
        fs = FilesystemConfig.from_dict("media", {"primary": "tg-main"})
        assert fs.mirrors == []
        assert fs.mode == "auto"
        assert fs.sync == "inline"
        assert fs.strict is False
        assert fs.read_preference == []
        assert fs.metadata.type == MetadataType.PINNED_MESSAGE
        assert fs.metadata.name == "media"
        assert fs.store_names == ["tg-main"]

    def test_github_metadata(self):
        fs = FilesystemConfig.from_dict(
            "media",
            {
                "primary": "tg-main",
                "metadata": {
                    "type": "github_repo",
                    "github_repo": {
                        "repo": "o/r",
                        "commit": "master",
                        "access_token": "t",
                    },
                },
            },
        )
        assert fs.metadata.type == MetadataType.GITHUB_REPO
        assert fs.metadata.github_repo is not None
        assert fs.metadata.github_repo.repo == "o/r"

    def test_primary_required(self):
        with pytest.raises(ValueError, match="primary"):
            FilesystemConfig.from_dict("media", {})

    def test_primary_cannot_be_a_mirror(self):
        with pytest.raises(ValueError, match="own mirror"):
            FilesystemConfig.from_dict("media", {"primary": "a", "mirrors": ["a"]})

    def test_duplicate_mirror_rejected(self):
        with pytest.raises(ValueError, match="duplicate"):
            FilesystemConfig.from_dict("media", {"primary": "a", "mirrors": ["b", "b"]})

    def test_bad_mode_rejected(self):
        with pytest.raises(ValueError, match="mode"):
            FilesystemConfig.from_dict("media", {"primary": "a", "mode": "raid5"})

    def test_bad_sync_rejected(self):
        with pytest.raises(ValueError, match="sync"):
            FilesystemConfig.from_dict("media", {"primary": "a", "sync": "later"})

    def test_strict_requires_inline(self):
        with pytest.raises(ValueError, match="inline"):
            FilesystemConfig.from_dict(
                "media", {"primary": "a", "strict": True, "sync": "background"}
            )

    def test_background_sync_is_accepted(self):
        fs = FilesystemConfig.from_dict("media", {"primary": "a", "sync": "background"})
        assert fs.sync == "background"


class TestCurrentLayout:
    def test_stores_and_filesystems(self):
        config = Config.from_dict(current_layout())

        assert set(config.stores) == {"tg-main", "tg-spare"}
        assert config.stores["tg-main"].key == "tg:-1001"
        fs = config.filesystems["media"]
        assert fs.primary == "tg-main"
        assert fs.mirrors == ["tg-spare"]
        assert fs.metadata.name == "media"
        assert config.telegram.api_id == 12345
        assert config.telegram.private_file_channel == []
        assert config.uses_backend == {"telegram": True, "discord": False}

    def test_lookups(self):
        config = Config.from_dict(current_layout())

        assert config.filesystem_for_store("tg-main").name == "media"  # type: ignore[union-attr]
        assert config.filesystem_for_store("tg-spare") is None
        assert config.filesystem_for_channel("telegram", "-1001").name == "media"  # type: ignore[union-attr]
        assert config.filesystem_for_channel("telegram", "-1002") is None
        assert config.filesystem_for_channel("discord", "-1001") is None

    def test_top_level_telegram_block_is_accepted(self):
        data = current_layout()
        data["telegram"] = data.pop("backends")["telegram"]
        config = Config.from_dict(data)
        assert config.telegram.api_id == 12345

    def test_unknown_store_rejected(self):
        data = current_layout()
        data["filesystems"]["media"]["mirrors"] = ["nope"]
        with pytest.raises(ValueError, match="unknown store 'nope'"):
            Config.from_dict(data)

    def test_read_preference_must_name_a_store_of_the_filesystem(self):
        data = current_layout()
        data["stores"]["other"] = {"backend": "telegram", "channel": "-1003"}
        data["filesystems"]["media"]["read_preference"] = ["other"]
        with pytest.raises(ValueError, match="read_preference"):
            Config.from_dict(data)

    def test_one_primary_per_store(self):
        data = current_layout()
        data["filesystems"]["notes"] = {"primary": "tg-main"}
        with pytest.raises(ValueError, match="already the primary"):
            Config.from_dict(data)

    def test_primary_of_one_cannot_mirror_another_by_default(self):
        data = current_layout()
        data["filesystems"]["notes"] = {"primary": "tg-spare", "mirrors": ["tg-main"]}
        with pytest.raises(ValueError, match="allow_shared_store"):
            Config.from_dict(data)

    def test_shared_store_can_be_allowed(self):
        data = current_layout()
        data["filesystems"]["notes"] = {
            "primary": "tg-spare",
            "mirrors": ["tg-main"],
            "allow_shared_store": True,
        }
        config = Config.from_dict(data)
        assert config.filesystems["notes"].mirrors == ["tg-main"]

    def test_private_file_channel_cannot_mix_with_stores(self):
        data = current_layout()
        data["backends"]["telegram"]["private_file_channel"] = ["-1001"]
        with pytest.raises(ValueError, match="private_file_channel"):
            Config.from_dict(data)

    def test_missing_telegram_block(self):
        data = current_layout()
        del data["backends"]
        with pytest.raises(ValueError, match="backends.telegram"):
            Config.from_dict(data)


class TestLegacyLayout:
    def legacy(self, redundancy=None):
        telegram = dict(TELEGRAM, private_file_channel=["-1001", "-1002"])
        if redundancy:
            telegram["redundancy"] = redundancy
        return {
            "telegram": telegram,
            "tgfs": dict(
                APP,
                metadata={
                    "-1001": {"name": "media", "type": "pinned_message"},
                    "-1002": {
                        "name": "notes",
                        "type": "github_repo",
                        "github_repo": {
                            "repo": "o/r",
                            "commit": "master",
                            "access_token": "t",
                        },
                    },
                },
            ),
        }

    def test_channels_become_stores_and_filesystems(self):
        config = Config.from_dict(self.legacy())

        assert set(config.stores) == {"tg--1001", "tg--1002"}
        assert config.stores["tg--1001"].key == "tg:-1001"
        assert set(config.filesystems) == {"media", "notes"}
        media = config.filesystems["media"]
        assert media.primary == "tg--1001"
        assert media.mirrors == []
        assert media.mode == "auto"
        assert media.metadata.type == MetadataType.PINNED_MESSAGE
        notes = config.filesystems["notes"]
        assert notes.metadata.type == MetadataType.GITHUB_REPO
        # The legacy fields stay readable for code that still looks there.
        assert config.telegram.private_file_channel == ["-1001", "-1002"]

    def test_redundancy_becomes_mirrors(self):
        config = Config.from_dict(
            self.legacy(
                redundancy={
                    "mirrors": {"-1001": ["-1003"]},
                    "mode": "reupload",
                    "strict": True,
                }
            )
        )

        assert "tg--1003" in config.stores
        media = config.filesystems["media"]
        assert media.mirrors == ["tg--1003"]
        assert media.mode == "reupload"
        assert media.strict is True
        assert media.sync == "inline"

    def test_legacy_allows_a_primary_to_mirror_another(self):
        config = Config.from_dict(
            self.legacy(redundancy={"mirrors": {"-1001": ["-1002"]}})
        )
        assert config.filesystems["media"].mirrors == ["tg--1002"]

    def test_channel_without_metadata_rejected(self):
        data = self.legacy()
        del data["tgfs"]["metadata"]["-1002"]
        with pytest.raises(ValueError, match="-1002"):
            Config.from_dict(data)

    def test_single_channel_as_scalar(self):
        data = self.legacy()
        data["telegram"]["private_file_channel"] = "-1001"
        del data["tgfs"]["metadata"]["-1002"]
        config = Config.from_dict(data)
        assert list(config.filesystems) == ["media"]
