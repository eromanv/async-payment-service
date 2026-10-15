import asyncio
import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI

from app.api.v1.payments import router as payments_router
from app.core.config import get_settings
from app.core.db import engine
from app.core.logging import setup_logging
from app.messaging.topology import broker, declare_topology
from app.outbox.relay import run_outbox_relay

logger = logging.getLogger(__name__)


async def _connect_broker() -> None:
    delay = 1.0
    last_error: Exception | None = None
    for attempt in range(1, 16):
        try:
            await broker.start()
            await declare_topology(broker)
            return
        except Exception as exc:
            last_error = exc
            logger.warning("rabbitmq is not ready, attempt %s/15", attempt, exc_info=True)
            try:
                await broker.stop()
            except Exception:
                logger.exception("failed to reset the broker after a connection error")
            await asyncio.sleep(delay)
            delay = min(delay * 2, 5.0)
    raise RuntimeError("could not connect to RabbitMQ") from last_error


@asynccontextmanager
async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
    settings = get_settings()
    setup_logging(settings.log_level)
    await _connect_broker()
    stop = asyncio.Event()
    relay_task = asyncio.create_task(run_outbox_relay(broker, stop))
    logger.info("payment api started")
    try:
        yield
    finally:
        stop.set()
        await relay_task
        await broker.stop()
        await engine.dispose()
        logger.info("payment api stopped")


app = FastAPI(title="Payment Service", lifespan=lifespan)
app.include_router(payments_router, prefix="/api/v1")


@app.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok"}
