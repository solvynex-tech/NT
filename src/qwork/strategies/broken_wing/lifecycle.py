"""Order policy shared by native historical and IBKR execution."""

from decimal import ROUND_CEILING, Decimal
from typing import Literal

from pydantic import Field

from qwork.core.validation import StrictModel


class OrderManagementConfig(StrictModel):
    mode: Literal["passive", "natural"] = "passive"
    entry_limit_fraction: float = Field(default=0, ge=0, le=1)
    entry_timeout_seconds: int = Field(default=120, ge=1, le=3600)
    retry_cooldown_seconds: int = Field(default=60, ge=1, le=3600)
    max_entry_attempts: int = Field(default=3, ge=1, le=20)
    cancel_ack_timeout_seconds: int = Field(default=30, ge=1, le=300)
    close_timeout_seconds: int = Field(default=30, ge=1, le=300)
    max_close_attempts: int = Field(default=20, ge=1, le=100)

    def limit_price(self, bid, ask, increment):
        fraction = 1 if self.mode == "natural" else self.entry_limit_fraction
        raw = Decimal(str((bid + ask) / 2 + fraction * (ask - bid) / 2))
        step = Decimal(str(increment))
        price = float((raw / step).to_integral_value(rounding=ROUND_CEILING) * step)
        if self.mode == "passive" and fraction < 1:
            price = min(price, float(Decimal(str(ask)) - step))
        return price
