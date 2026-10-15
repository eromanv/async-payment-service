import asyncio
import logging

from faststream import FastStream

from app.core.config import get_settings
from app.core.db import engine
from app.core.logging import setup_logging
from app.messaging.topology import broker, declare_topology
from app.processing import handler as handler  # noqa: F401

logger = logging.getLogger(__name__)

app = FastStream(broker)


@app.after_startup
async def prepare_topology() -> None:
    await declare_topology(broker)
    logger.info("consumer topology declared")


@app.on_shutdown
async def dispose_engine() -> None:
    await engine.dispose()


def main() -> None:
    setup_logging(get_settings().log_level)
    asyncio.run(app.run())


if __name__ == "__main__":
    main()
