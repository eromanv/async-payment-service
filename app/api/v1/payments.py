from decimal import ROUND_HALF_UP, Decimal
from typing import Annotated, cast
from uuid import UUID

from fastapi import APIRouter, Depends, Header, HTTPException, status

from app.api.deps import require_api_key
from app.models.payment import Payment
from app.schemas.payment import (
    Currency,
    PaymentAccepted,
    PaymentCreate,
    PaymentRead,
    PaymentStatus,
)
from app.services.payments import (
    IdempotencyConflictError,
    PaymentNotFoundError,
    create_payment,
    get_payment,
)

router = APIRouter(
    prefix="/payments",
    tags=["payments"],
    dependencies=[Depends(require_api_key)],
)


def _amount_to_str(amount: Decimal) -> str:
    return format(amount.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP), "f")


def to_payment_read(payment: Payment) -> PaymentRead:
    return PaymentRead(
        payment_id=payment.id,
        amount=_amount_to_str(payment.amount),
        currency=cast(Currency, payment.currency),
        description=payment.description,
        metadata=payment.payment_metadata,
        status=cast(PaymentStatus, payment.status),
        idempotency_key=payment.idempotency_key,
        webhook_url=payment.webhook_url,
        created_at=payment.created_at,
        processed_at=payment.processed_at,
        webhook_delivered_at=payment.webhook_delivered_at,
    )


def to_payment_accepted(payment: Payment) -> PaymentAccepted:
    return PaymentAccepted(
        payment_id=payment.id,
        status=cast(PaymentStatus, payment.status),
        created_at=payment.created_at,
    )


@router.post(
    "",
    status_code=status.HTTP_202_ACCEPTED,
    response_model=PaymentAccepted,
)
async def create_payment_endpoint(
    payload: PaymentCreate,
    idempotency_key: Annotated[str, Header(min_length=1, max_length=255)],
) -> PaymentAccepted:
    try:
        payment = await create_payment(idempotency_key=idempotency_key, payload=payload)
    except IdempotencyConflictError:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Idempotency key already used with a different request body",
        ) from None
    return to_payment_accepted(payment)


@router.get("/{payment_id}", response_model=PaymentRead)
async def get_payment_endpoint(payment_id: UUID) -> PaymentRead:
    try:
        payment = await get_payment(payment_id)
    except PaymentNotFoundError:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Payment not found",
        ) from None
    return to_payment_read(payment)
