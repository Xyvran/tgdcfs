"""Logins and store construction for the Discord backend."""

import logging
from typing import List

from tgdcfs.config import Config, StoreConfig

from .client import DiscordBotAPI, login_as_bot
from .store import DiscordStore

logger = logging.getLogger(__name__)


async def login(config: Config) -> List[DiscordBotAPI]:
    """Log every configured bot in."""
    if config.discord is None:
        raise ValueError("configuration block 'backends.discord' is missing")
    bots: List[DiscordBotAPI] = []
    for token in config.discord.bot_tokens:
        try:
            client = await login_as_bot(token)
        except Exception:
            # Do not leave the bots that already made it logged in.
            for bot in bots:
                await bot.close()
            raise
        bots.append(DiscordBotAPI(client, name=client.user.name if client.user else ""))
    return bots


async def create_store(
    store_cfg: StoreConfig, config: Config, bots: List[DiscordBotAPI]
) -> DiscordStore:
    """Bind a store config to a Discord channel through the logged-in bots."""
    if config.discord is None:
        raise ValueError("configuration block 'backends.discord' is missing")
    try:
        channel_id = int(store_cfg.channel)
    except ValueError:
        raise ValueError(
            f"stores.{store_cfg.name}: Discord channel ids are numeric, "
            f"got '{store_cfg.channel}'"
        )
    discord_cfg = config.discord
    return DiscordStore(
        bots,
        channel_id,
        key=store_cfg.key,
        max_part_bytes=discord_cfg.max_file_size_bytes,
        delete_on_remove=discord_cfg.delete_messages_on_remove,
        max_retries=discord_cfg.upload_max_retries,
        retry_interval=discord_cfg.upload_retry_interval,
        max_concurrent_uploads=discord_cfg.max_concurrent_uploads,
        max_concurrent_downloads=discord_cfg.max_concurrent_downloads,
    )
