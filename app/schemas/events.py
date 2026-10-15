from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import BaseModel


class PaymentCreated(BaseModel):
    event_id: UUID
    event_type: Literal["payment.created"]
    payment_id: UUID
    occurred_at: datetime
