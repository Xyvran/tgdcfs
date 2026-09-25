"""Stores and file systems: the current config layout, the tgfs layout
translated on load, and the validation between the two."""

import logging

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

    def test_discord_store(self):
        store = StoreConfig.from_dict("main", {"backend": "discord", "channel": 1234})
        assert store.key == "dc:1234"

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
        assert fs.sync_is_default is False


class TestDerivedSync:
    """Left out, ``sync`` follows the mirrors: background when one re-uploads."""

    @staticmethod
    def stores(**backends: str):
        return {
            name: StoreConfig.from_dict(name, {"backend": backend, "channel": "1"})
            for name, backend in backends.items()
        }

    @staticmethod
    def fs(**data):
        return FilesystemConfig.from_dict("media", {"primary": "main", **data})

    def test_no_mirrors_stays_inline(self):
        fs = self.fs()
        fs.derive_sync(self.stores(main="telegram"))
        assert fs.sync == "inline"

    def test_telegram_forwarding_stays_inline(self):
        fs = self.fs(mirrors=["spare"])
        fs.derive_sync(self.stores(main="telegram", spare="telegram"))
        assert fs.sync == "inline"

    def test_discord_mirror_of_telegram_goes_background(self):
        fs = self.fs(mirrors=["spare", "dc"])
        fs.derive_sync(self.stores(main="telegram", spare="telegram", dc="discord"))
        assert fs.sync == "background"

    def test_telegram_mirror_of_discord_goes_background(self):
        fs = self.fs(mirrors=["tg"])
        fs.derive_sync(self.stores(main="discord", tg="telegram"))
        assert fs.sync == "background"

    def test_discord_to_discord_goes_background(self):
        fs = self.fs(mirrors=["dc2"])
        fs.derive_sync(self.stores(main="discord", dc2="discord"))
        assert fs.sync == "background"

    def test_reupload_mode_goes_background(self):
        fs = self.fs(mirrors=["spare"], mode="reupload")
        fs.derive_sync(self.stores(main="telegram", spare="telegram"))
        assert fs.sync == "background"

    def test_explicit_inline_is_kept(self):
        fs = self.fs(mirrors=["dc"], sync="inline")
        fs.derive_sync(self.stores(main="telegram", dc="discord"))
        assert fs.sync == "inline"

    def test_strict_keeps_inline(self):
        fs = self.fs(mirrors=["dc"], strict=True)
        fs.derive_sync(self.stores(main="telegram", dc="discord"))
        assert fs.sync == "inline"

    def test_applied_when_the_config_loads(self):
        data = current_layout()
        data["backends"]["discord"] = {"bot_tokens": ["t"]}
        data["stores"]["dc"] = {"backend": "discord", "channel": "1"}
        data["filesystems"]["media"]["mirrors"].append("dc")
        assert Config.from_dict(data).filesystems["media"].sync == "background"
        data["filesystems"]["media"]["mirrors"].remove("dc")
        assert Config.from_dict(data).filesystems["media"].sync == "inline"


class TestWritesWaitForMirrorsWarning:
    """An explicit ``inline`` (or ``strict``) over a re-uploading mirror is
    legal but keeps every upload waiting; the loader says so once."""

    stores = staticmethod(TestDerivedSync.stores)
    fs = staticmethod(TestDerivedSync.fs)

    def warnings(self, caplog) -> list[str]:
        return [
            r.getMessage()
            for r in caplog.records
            if r.levelno == logging.WARNING and r.name == "tgdcfs.config"
        ]

    def test_explicit_inline_over_discord_mirror_warns(self, caplog):
        fs = self.fs(mirrors=["dc"], sync="inline")
        with caplog.at_level(logging.WARNING):
            fs.warn_if_writes_wait_for_mirrors(
                self.stores(main="telegram", dc="discord")
            )
        (message,) = self.warnings(caplog)
        assert "filesystems.media" in message
        assert "'sync: inline'" in message
        assert "'dc'" in message
        assert "sync: background" in message

    def test_strict_over_discord_mirror_names_strict(self, caplog):
        fs = self.fs(mirrors=["dc"], strict=True)
        with caplog.at_level(logging.WARNING):
            fs.warn_if_writes_wait_for_mirrors(
                self.stores(main="telegram", dc="discord")
            )
        (message,) = self.warnings(caplog)
        assert "'strict: true'" in message
        assert "drop 'strict: true'" in message

    def test_only_the_reuploading_mirrors_are_named(self, caplog):
        fs = self.fs(mirrors=["spare", "dc"], sync="inline")
        with caplog.at_level(logging.WARNING):
            fs.warn_if_writes_wait_for_mirrors(
                self.stores(main="telegram", spare="telegram", dc="discord")
            )
        (message,) = self.warnings(caplog)
        assert "'dc'" in message
        assert "'spare'" not in message

    def test_reupload_mode_names_every_mirror(self, caplog):
        fs = self.fs(mirrors=["spare"], mode="reupload", sync="inline")
        with caplog.at_level(logging.WARNING):
            fs.warn_if_writes_wait_for_mirrors(
                self.stores(main="telegram", spare="telegram")
            )
        (message,) = self.warnings(caplog)
        assert "'spare'" in message

    def test_background_is_silent(self, caplog):
        fs = self.fs(mirrors=["dc"], sync="background")
        with caplog.at_level(logging.WARNING):
            fs.warn_if_writes_wait_for_mirrors(
                self.stores(main="telegram", dc="discord")
            )
        assert self.warnings(caplog) == []

    def test_telegram_forwarding_is_silent(self, caplog):
        fs = self.fs(mirrors=["spare"], sync="inline", strict=True)
        with caplog.at_level(logging.WARNING):
            fs.warn_if_writes_wait_for_mirrors(
                self.stores(main="telegram", spare="telegram")
            )
        assert self.warnings(caplog) == []

    def test_derived_default_is_silent(self, caplog):
        fs = self.fs(mirrors=["dc"])
        stores = self.stores(main="telegram", dc="discord")
        fs.derive_sync(stores)
        with caplog.at_level(logging.WARNING):
            fs.warn_if_writes_wait_for_mirrors(stores)
        assert self.warnings(caplog) == []

    def test_emitted_when_the_config_loads(self, caplog):
        data = current_layout()
        data["backends"]["discord"] = {"bot_tokens": ["t"]}
        data["stores"]["dc"] = {"backend": "discord", "channel": "1"}
        data["filesystems"]["media"]["mirrors"].append("dc")
        data["filesystems"]["media"]["sync"] = "inline"
        data["filesystems"]["media"]["strict"] = True
        with caplog.at_level(logging.WARNING):
            Config.from_dict(data)
        assert any("'strict: true'" in m for m in self.warnings(caplog))


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

    def test_discord_store_needs_the_discord_backend(self):
        data = current_layout()
        data["stores"]["dc"] = {"backend": "discord", "channel": "1"}
        with pytest.raises(ValueError, match="backends.discord"):
            Config.from_dict(data)

    def test_discord_backend(self):
        data = current_layout()
        data["backends"]["discord"] = {
            "bot_tokens": ["t1", "t2"],
            "max_file_size_bytes": 20_000_000,
        }
        data["stores"]["dc"] = {"backend": "discord", "channel": "1"}
        data["filesystems"]["media"]["mirrors"].append("dc")
        config = Config.from_dict(data)
        assert config.discord is not None
        assert config.discord.bot_tokens == ["t1", "t2"]
        assert config.discord.max_file_size_bytes == 20_000_000
        assert config.discord.upload_max_retries == 10
        assert config.uses_backend == {"telegram": True, "discord": True}

    def test_discord_backend_single_token(self):
        data = current_layout()
        data["backends"]["discord"] = {"bot_token": "t"}
        config = Config.from_dict(data)
        assert config.discord is not None
        assert config.discord.bot_tokens == ["t"]
        assert config.discord.max_file_size_bytes == 10_000_000

    def test_discord_backend_needs_a_token(self):
        data = current_layout()
        data["backends"]["discord"] = {}
        with pytest.raises(ValueError, match="bot_tokens"):
            Config.from_dict(data)

    def test_discord_backend_rejects_a_tiny_part_size(self):
        data = current_layout()
        data["backends"]["discord"] = {"bot_token": "t", "max_file_size_bytes": 10}
        with pytest.raises(ValueError, match="too small"):
            Config.from_dict(data)

    def test_telegram_store_needs_the_telegram_block(self):
        data = current_layout()
        del data["backends"]
        with pytest.raises(ValueError, match="backends.telegram"):
            Config.from_dict(data)

    def test_discord_only_deployment_needs_no_telegram_block(self):
        data = current_layout()
        data["backends"] = {"discord": {"bot_token": "t"}}
        data["stores"] = {"dc": {"backend": "discord", "channel": "1"}}
        data["filesystems"] = {"notes": {"primary": "dc"}}
        config = Config.from_dict(data)
        assert config.uses_backend == {"telegram": False, "discord": True}
        assert config.telegram.api_hash == ""
        assert config.telegram.delete_messages_on_remove is False

    def test_legacy_layout_still_needs_the_telegram_block(self):
        with pytest.raises(ValueError, match="backends.telegram"):
            Config.from_dict({"tgdcfs": dict(APP)})


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
