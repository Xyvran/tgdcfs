import asyncio
import logging

logging.basicConfig(
    level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s"
)

try:
    import uvloop  # type: ignore[import]

    uvloop.install()
except ImportError:
    logging.warning("uvloop is not installed, using default event loop")

from uvicorn.config import Config as UvicornConfig
from uvicorn.server import Server

from tgdcfs.app import create_app
from tgdcfs.app.sftp import start_sftp_server
from tgdcfs.backends.base import IStore
from tgdcfs.backends.telegram import factory as telegram
from tgdcfs.config import Config, StoreConfig, get_config
from tgdcfs.core import Client, Clients
from tgdcfs.core.client import StoreFactory


async def create_store_factory(config: Config) -> StoreFactory:
    """Log in to every backend the config uses and return the factory
    that turns a store config into a live store."""
    tdlib_api = (
        await telegram.login(config) if config.uses_backend["telegram"] else None
    )

    async def create_store(store_cfg: StoreConfig) -> IStore:
        if store_cfg.backend == "telegram" and tdlib_api is not None:
            return await telegram.create_store(store_cfg, config, tdlib_api)
        raise ValueError(f"Unsupported backend: {store_cfg.backend}")

    return create_store


async def create_clients(config: Config) -> Clients:
    store_factory = await create_store_factory(config)

    clients: Clients = {}
    for filesystem in config.filesystems.values():
        clients[filesystem.name] = await Client.create(
            filesystem,
            config,
            store_factory,
            encryption_cfg=config.tgdcfs.encryption,
        )
    return clients


async def run_server(app, host: str, port: int, name: str):
    """Run a server with proper configuration"""
    logger = logging.getLogger(__name__)
    logger.info(f"Starting {name} server on {host}:{port}")

    server_config = UvicornConfig(
        app,
        host=host,
        port=port,
        loop="none",
        log_level="info",
    )
    server = Server(config=server_config)
    await server.serve()


async def main():
    logger = logging.getLogger(__name__)
    config = get_config()

    clients = await create_clients(config)

    app = create_app(clients, config)

    try:
        sftp_acceptor = await start_sftp_server(clients, config)
    except Exception as ex:
        # A broken SFTP setup (port taken, unwritable host key) must not keep
        # the HTTP interface from coming up.
        logger.error("Failed to start the SFTP server: %s", ex)
        sftp_acceptor = None

    try:
        await run_server(
            app, config.tgdcfs.server.host, config.tgdcfs.server.port, "TGDCFS"
        )
    finally:
        if sftp_acceptor:
            sftp_acceptor.close()
            await sftp_acceptor.wait_closed()


if __name__ == "__main__":
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    loop.run_until_complete(main())
