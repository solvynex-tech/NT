from datetime import time
from typing import Literal

from pydantic import Field, model_validator

from qwork.core.validation import StrictModel


class BrokenWingConfig(StrictModel):
    symbol: str = Field(default="SPY", pattern=r"^[A-Z][A-Z0-9]{0,5}$")
    margin_budget: float = Field(default=10000, gt=0, le=10000000)
    max_positions: int = Field(default=3, ge=1, le=20)
    max_units: int = Field(default=100, ge=1, le=1000)
    rank_gamma: bool = True
    probability_horizon: Literal["intraday", "expiration"] = "intraday"
    min_probability: float = Field(default=0.70, ge=0.70, lt=1)
    min_profit_loss_ratio: float = Field(default=1, ge=1, le=100)
    take_profit_fraction: float = Field(default=0.70, gt=0, le=1)
    plateau_remaining_fraction: float = Field(default=0.20, gt=0, lt=1)
    margin_buffer: float = Field(default=1.10, ge=1, le=3)
    commission_per_contract: float = Field(default=0.65, ge=0, le=100)
    slippage_per_share: float = Field(default=0.01, ge=0, le=10)
    quote_max_age_seconds: float = Field(default=5, gt=0, le=120)
    quote_max_skew_seconds: float = Field(default=1, ge=0, le=5)
    max_entry_spread: float = Field(default=1, gt=0, le=10)
    entry_time: time = time(9, 45)
    last_entry_time: time = time(15, 30)
    flatten_time: time = time(15, 55)
    rate: float = 0.04
    dividend_yield: float = 0

    @model_validator(mode="after")
    def session_order(self):
        if not self.entry_time < self.last_entry_time < self.flatten_time:
            raise ValueError("Require entry_time < last_entry_time < flatten_time")
        return self
