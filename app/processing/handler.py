import logging
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import ROUND_HALF_UP, Decimal
from typing import Any
from uuid import UUID

from faststream.rabbit import RabbitMessage
from faststream.utils.context import context
from pydantic import ValidationError
from sqlalchemy import select

from app.core.config import get_settings
from app.core.db import SessionLocal
from app.messaging.topology import (
    ATTEMPT_HEADER,
    RETRY_QUEUE_NAMES,
    broker,
    payments_exchange,
    payments_new_queue,
)
from app.models.payment import STATUS_PENDING, Payment
from app.processing.gateway import GatewayOutcome, emulate_gateway
from app.processing.webhook import deliver_webhook
from app.schemas.events import PaymentCreated

logger = logging.getLogger(__name__)


class WebhookDeliveryError(Exception):
    pass


@dataclass(frozen=True)
class _WebhookSnapshot:
    payment_id: UUID
    status: str
    amount: Decimal
    currency: str
    processed_at: datetime
    url: str

    def as_payload(self) -> dict[str, str]:
        amount = self.amount.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
        return {
            "payment_id": str(self.payment_id),
            "status": self.status,
            "amount": format(amount, "f"),
            "currency": self.currency,
            "processed_at": self.processed_at.isoformat(),
        }


def _read_attempt(headers: Mapping[str, Any] | None) -> int:
    if not headers:
        return 0
    raw = headers.get(ATTEMPT_HEADER, 0)
    if isinstance(raw, bytes):
        raw = raw.decode("utf-8")
    try:
        attempt = int(raw)
    except (TypeError, ValueError):
        return 0
    return attempt if attempt >= 0 else 0


def _load_snapshot(payment: Payment) -> _WebhookSnapshot:
    if payment.processed_at is None:
        raise RuntimeError(f"payment {payment.id} has no processed_at")
    return _WebhookSnapshot(
        payment_id=payment.id,
        status=payment.status,
        amount=payment.amount,
        currency=payment.currency,
        processed_at=payment.processed_at,
        url=payment.webhook_url,
    )


async def process_payment_event(event: PaymentCreated) -> None:
    async with SessionLocal() as session:
        payment = await session.get(Payment, event.payment_id)
        if payment is None:
            logger.info("payment %s does not exist, acknowledging", event.payment_id)
            return
        if payment.webhook_delivered_at is not None:
            logger.info("payment %s webhook already delivered", event.payment_id)
            return
        needs_gateway = payment.status == STATUS_PENDING

    outcome: GatewayOutcome | None = None
    if needs_gateway:
        outcome = await emulate_gateway(get_settings().payment_success_rate)
        logger.info(
            "gateway finished payment_id=%s outcome=%s",
            event.payment_id,
            outcome,
        )

    async with SessionLocal() as session:
        async with session.begin():
            payment = await session.scalar(
                select(Payment).where(Payment.id == event.payment_id).with_for_update()
            )
            if payment is None or payment.webhook_delivered_at is not None:
                return
            if payment.status == STATUS_PENDING:
                if outcome is None:
                    raise RuntimeError("pending payment was processed without the gateway")
                payment.status = outcome
                payment.processed_at = datetime.now(timezone.utc)
            snapshot = _load_snapshot(payment)

    try:
        await deliver_webhook(snapshot.url, snapshot.as_payload())
    except Exception as exc:
        logger.exception("webhook failed payment_id=%s", event.payment_id)
        raise WebhookDeliveryError(str(event.payment_id)) from exc

    async with SessionLocal() as session:
        async with session.begin():
            payment = await session.scalar(
                select(Payment).where(Payment.id == event.payment_id).with_for_update()
            )
            if payment is not None and payment.webhook_delivered_at is None:
                payment.webhook_delivered_at = datetime.now(timezone.utc)
    logger.info("payment %s marked webhook delivered", event.payment_id)


async def _retry_or_dead_letter(body: dict[str, Any], message: RabbitMessage) -> None:
    attempt = _read_attempt(message.headers)
    if attempt >= len(RETRY_QUEUE_NAMES):
        logger.error(
            "technical failure exhausted retries x-attempt=%s, rejecting to DLQ",
            attempt,
        )
        await message.reject(requeue=False)
        return

    queue_name = RETRY_QUEUE_NAMES[attempt]
    next_attempt = attempt + 1
    logger.warning(
        "technical failure, publishing to %s with x-attempt=%s",
        queue_name,
        next_attempt,
    )
    await broker.publish(
        body,
        queue=queue_name,
        persist=True,
        headers={ATTEMPT_HEADER: next_attempt},
    )
    await message.ack()


@broker.subscriber(payments_new_queue, payments_exchange, no_ack=True)
async def on_payment_created(payload: dict[str, Any]) -> None:
    message: RabbitMessage | None = context.get("message")
    if message is None:
        raise RuntimeError("RabbitMQ message is missing from the consumer context")
    try:
        event = PaymentCreated.model_validate(payload)
    except ValidationError:
        logger.exception("invalid payment event, rejecting to DLQ")
        await message.reject(requeue=False)
        return

    try:
        await process_payment_event(event)
    except Exception:
        logger.exception("technical failure payment_id=%s", event.payment_id)
        await _retry_or_dead_letter(payload, message)
        return

    await message.ack()
