import asyncio
import logging
from datetime import datetime, timezone

from faststream.rabbit import RabbitBroker
from sqlalchemy import select

from app.core.db import SessionLocal
from app.messaging.topology import ROUTING_KEY_NEW, payments_exchange
from app.models.outbox import STATUS_PENDING, STATUS_PUBLISHED, OutboxEvent

logger = logging.getLogger(__name__)

POLL_INTERVAL_SECONDS = 1.0
BATCH_SIZE = 50


async def publish_pending(broker: RabbitBroker) -> int:
    published = 0
    async with SessionLocal() as session:
        async with session.begin():
            rows = (
                await session.scalars(
                    select(OutboxEvent)
                    .where(OutboxEvent.status == STATUS_PENDING)
                    .order_by(OutboxEvent.created_at)
                    .limit(BATCH_SIZE)
                    .with_for_update(skip_locked=True)
                )
            ).all()
            now = datetime.now(timezone.utc)
            for row in rows:
                await broker.publish(
                    row.payload,
                    exchange=payments_exchange,
                    routing_key=ROUTING_KEY_NEW,
                    persist=True,
                )
                row.status = STATUS_PUBLISHED
                row.published_at = now
                published += 1
    return published


async def run_outbox_relay(broker: RabbitBroker, stop: asyncio.Event) -> None:
    logger.info("outbox relay started")
    while not stop.is_set():
        try:
            published = await publish_pending(broker)
            if published:
                logger.info("published %s outbox events", published)
        except Exception:
            logger.exception("outbox relay iteration failed")
        try:
            await asyncio.wait_for(stop.wait(), timeout=POLL_INTERVAL_SECONDS)
        except TimeoutError:
            continue
    logger.info("outbox relay stopped")
