"""
In-memory payment context store with TTL.

Maps sha256(normalized_phone) → PaymentContext.
Single-process safe. Upgrade path: replace _data with aioredis calls.
"""
import hashlib
import time
from dataclasses import dataclass, field
from typing import Any

PAYMENT_TTL_SECONDS = 600  # 10-minute intent window


@dataclass
class PaymentContext:
    order_id: str
    merchant_id: str
    amount: float
    currency: str
    items: list[str]
    iab_category: str
    phone_last4: str
    stored_at: float = field(default_factory=time.time)

    def is_expired(self) -> bool:
        return time.time() - self.stored_at > PAYMENT_TTL_SECONDS

    def to_dict(self) -> dict[str, Any]:
        return {
            "order_id": self.order_id,
            "merchant_id": self.merchant_id,
            "amount": self.amount,
            "currency": self.currency,
            "items": self.items,
            "iab_category": self.iab_category,
            "phone_last4": self.phone_last4,
            "stored_at": self.stored_at,
            "age_seconds": int(time.time() - self.stored_at),
        }


class PaymentStore:
    def __init__(self) -> None:
        self._data: dict[str, PaymentContext] = {}

    def put(self, phone_hash: str, ctx: PaymentContext) -> None:
        self._data[phone_hash] = ctx
        self._evict()

    def get(self, phone_hash: str) -> PaymentContext | None:
        ctx = self._data.get(phone_hash)
        if ctx is None:
            return None
        if ctx.is_expired():
            del self._data[phone_hash]
            return None
        return ctx

    def _evict(self) -> None:
        expired = [k for k, v in self._data.items() if v.is_expired()]
        for k in expired:
            del self._data[k]

    @property
    def size(self) -> int:
        return len(self._data)


def phone_hash(normalized_phone: str) -> str:
    return hashlib.sha256(normalized_phone.encode()).hexdigest()


# Module-level singleton
payment_store = PaymentStore()
