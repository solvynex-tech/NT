from datetime import date
from typing import Literal

from pydantic import Field, model_validator

from qwork.core.validation import StrictModel


class PositionLeg(StrictModel):
    symbol: str = Field(pattern=r"^[A-Z][A-Z0-9]{0,9}$")
    expiration: date
    strike: float = Field(gt=0, le=100000)
    right: Literal["call", "put"]
    side: Literal["buy", "sell"]
    quantity: int = Field(ge=1, le=10000)
    entry_premium: float = Field(ge=0, le=100000)
    multiplier: Literal[100] = 100


class OptionCombination(StrictModel):
    name: str = Field(min_length=1, max_length=80)
    legs: list[PositionLeg] = Field(min_length=1, max_length=32)

    @model_validator(mode="after")
    def one_underlying(self):
        if len({leg.symbol for leg in self.legs}) != 1:
            raise ValueError("一个组合须使用同一标的；不同标的不能共用一条价格情景横轴")
        return self


class PortfolioRequest(StrictModel):
    combinations: list[OptionCombination] = Field(default_factory=list, max_length=20)
    commission_per_contract: float = Field(default=0.65, ge=0, le=100)
    scenario_band: float = Field(default=0.2, gt=0, le=0.95)
    scenario_points: int = Field(default=101, ge=21, le=301)

    @model_validator(mode="after")
    def distinct_names(self):
        if len({item.name for item in self.combinations}) != len(self.combinations):
            raise ValueError("每个组合名称必须唯一")
        if len({leg.symbol for item in self.combinations for leg in item.legs}) > 1:
            raise ValueError("当前窗口的多组合须使用同一标的；请为不同标的分别启动窗口")
        return self
