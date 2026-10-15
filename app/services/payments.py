import hashlib
import json
import logging
from datetime import datetime, timezone
from decimal import ROUND_HALF_UP, Decimal
from uuid import UUID, uuid4

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from app.core.db import SessionLocal
from app.models.outbox import (
    AGGREGATE_PAYMENT,
    EVENT_PAYMENT_CREATED,
    STATUS_PENDING as OUTBOX_PENDING,
    OutboxEvent,
)
from app.models.payment import STATUS_PENDING, Payment
from app.schemas.payment import PaymentCreate

logger = logging.getLogger(__name__)


class PaymentNotFoundError(Exception):
    pass


class IdempotencyConflictError(Exception):
    pass


def canonical_request_hash(payload: PaymentCreate) -> str:
    amount = payload.amount.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    document = {
        "amount": format(amount, "f"),
        "currency": payload.currency,
        "description": payload.description,
        "metadata": payload.metadata,
        "webhook_url": str(payload.webhook_url),
    }
    raw = json.dumps(
        document,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    )
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _assert_same_body(payment: Payment, request_hash: str) -> None:
    if payment.request_hash != request_hash:
        raise IdempotencyConflictError


async def create_payment(*, idempotency_key: str, payload: PaymentCreate) -> Payment:
    request_hash = canonical_request_hash(payload)
    amount = payload.amount.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)

    async with SessionLocal() as session:
        existing = await session.scalar(
            select(Payment).where(Payment.idempotency_key == idempotency_key)
        )
        if existing is not None:
            _assert_same_body(existing, request_hash)
            await session.commit()
            return existing

        now = datetime.now(timezone.utc)
        payment_id = uuid4()
        event_id = uuid4()
        occurred_at = now.isoformat()
        payment = Payment(
            id=payment_id,
            amount=amount,
            currency=payload.currency,
            description=payload.description,
            payment_metadata=payload.metadata,
            status=STATUS_PENDING,
            idempotency_key=idempotency_key,
            request_hash=request_hash,
            webhook_url=str(payload.webhook_url),
            created_at=now,
        )
        outbox = OutboxEvent(
            id=event_id,
            aggregate_type=AGGREGATE_PAYMENT,
            aggregate_id=payment_id,
            event_type=EVENT_PAYMENT_CREATED,
            payload={
                "event_id": str(event_id),
                "event_type": EVENT_PAYMENT_CREATED,
                "payment_id": str(payment_id),
                "occurred_at": occurred_at,
            },
            status=OUTBOX_PENDING,
            created_at=now,
        )
        session.add(payment)
        session.add(outbox)
        try:
            await session.commit()
        except IntegrityError:
            await session.rollback()
            raced = await session.scalar(
                select(Payment).where(Payment.idempotency_key == idempotency_key)
            )
            if raced is None:
                raise
            _assert_same_body(raced, request_hash)
            await session.commit()
            logger.info(
                "idempotent replay after unique conflict key=%s payment_id=%s",
                idempotency_key,
                raced.id,
            )
            return raced

        logger.info("payment created payment_id=%s", payment.id)
        return payment


async def get_payment(payment_id: UUID) -> Payment:
    async with SessionLocal() as session:
        payment = await session.get(Payment, payment_id)
        if payment is None:
            raise PaymentNotFoundError
        await session.commit()
        return payment
