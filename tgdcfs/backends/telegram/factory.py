"""Logins and store construction for the Telegram backend."""

import logging

from tgdcfs.config import Config, StoreConfig

from .impl import pyrogram, telethon
from .interface import TDLibApi
from .store import TelegramStore

logger = logging.getLogger(__name__)


async def login(config: Config) -> TDLibApi:
    """Log the configured bots (and the optional user account) in."""
    if config.telegram.lib == "pyrogram":
        return TDLibApi(
            account=(
                pyrogram.PyrogramAPI(await pyrogram.login_as_account(config))
                if config.telegram.account
                else None
            ),
            bots=[
                pyrogram.PyrogramAPI(bot)
                for bot in await pyrogram.login_as_bots(config)
            ],
        )

    account = None
    if config.telegram.account:
        account_client = await telethon.login_as_account(config)
        account = telethon.TelethonAPI(
            account_client,
            await telethon.open_extra_connections(config, account_client),
        )

    bots = []
    for bot in await telethon.login_as_bots(config):
        bots.append(
            telethon.TelethonAPI(
                bot, await telethon.open_extra_connections(config, bot)
            )
        )

    return TDLibApi(account=account, bots=bots)


async def create_store(
    store_cfg: StoreConfig, config: Config, tdlib_api: TDLibApi
) -> TelegramStore:
    """Bind a store config to a live channel through the logged-in bots."""
    resolved = await tdlib_api.next_bot.resolve_channel_id(store_cfg.channel)
    premium_upload = bool(
        config.telegram.account
        and config.telegram.account.used_to_upload
        and tdlib_api.account is not None
        and (await tdlib_api.account.get_me()).is_premium
    )
    return TelegramStore(
        tdlib_api, resolved, key=store_cfg.key, premium_upload=premium_upload
    )
