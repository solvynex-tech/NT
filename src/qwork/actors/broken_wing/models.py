from datetime import date
from typing import Literal

from pydantic import Field

from qwork.core.validation import StrictModel


class ButterflyScanRequest(StrictModel):
    symbol: str | None = Field(default=None, pattern=r"^[A-Z][A-Z0-9]{0,9}$")
    expirations: list[date] = Field(default_factory=list, max_length=24)
    min_probability: float = Field(default=0.7, ge=0, lt=1)
    target_ratio: float = Field(default=1, gt=0, le=100)
    ratio_mode: Literal["at_least", "near", "at_most"] = "at_least"
    ratio_tolerance: float = Field(default=0.02, ge=0, le=1)
    commission_per_contract: float = Field(default=0.65, ge=0, le=100)
    slippage_per_contract: float = Field(default=0.01, ge=0, le=100)
    units: int = Field(default=1, ge=1, le=100)
    body_band: float = Field(default=0.05, gt=0, le=0.5)
    max_wing_fraction: float = Field(default=0.1, gt=0, le=1)
    skew_moneyness: float = Field(default=0.05, gt=0, le=0.5)
    minimum_skew: float = Field(default=0.001, ge=0, le=1)
    max_strikes: int = Field(default=41, ge=3, le=61)
    max_candidates: int = Field(default=30, ge=1, le=100)
