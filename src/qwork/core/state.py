from __future__ import annotations

from copy import deepcopy
from datetime import date, datetime, time
from threading import RLock
from typing import Literal

from pydantic import Field, field_validator, model_validator

from qwork.core.config import now
from qwork.core.domain import eastern
from qwork.core.market import matches_surface
from qwork.core.validation import StrictModel


class VolatilityRequest(StrictModel):
    symbol: str = Field(default="SPY", pattern=r"^[A-Z][A-Z0-9]{0,9}$")
    source: Literal["theta", "fixture"] = "theta"
    mode: Literal["live", "historical"] = "live"
    as_of: date | None = None
    at: time = time(10, 0)
    min_dte: int = Field(default=1, ge=0, le=3650)
    max_dte: int = Field(default=180, ge=1, le=3650)
    max_expirations: int = Field(default=8, ge=1, le=24)
    refresh_seconds: int = Field(default=15, ge=5, le=3600)
    expirations: list[date] = Field(default_factory=list, max_length=24)
    required_expirations: list[date] = Field(default_factory=list, max_length=24)
    required_strikes: list[float] = Field(default_factory=list, max_length=640)
    spot: float | None = Field(default=None, gt=0, le=100000)
    rate: float = Field(default=0.04, ge=-0.1, le=1)
    dividend_yield: float = Field(default=0, ge=0, le=1)
    strike_band: float = Field(default=0.15, ge=0.01, le=1)
    max_relative_spread: float = Field(default=0.5, gt=0, le=2)
    max_quote_age_seconds: int = Field(default=1200, ge=1, le=86400)
    iv_method: Literal["auto", "quantlib"] = "auto"

    @field_validator("symbol", mode="before")
    @classmethod
    def normalize_symbol(cls, value):
        return str(value).strip().upper()

    @model_validator(mode="after")
    def validate_window(self):
        if self.symbol == "SPX":
            raise ValueError("请用 SPXW 明确表示 PM 结算期权")
        if self.min_dte > self.max_dte:
            raise ValueError("最小剩余天数不能大于最大剩余天数")
        if self.mode == "historical" and (self.as_of is None or self.as_of > datetime.now(eastern()).date()):
            raise ValueError("历史快照需要有效的日期，且不能晚于今天")
        return self


class DashboardState:
    """Immutable copies across Dash request threads and the Nautilus event loop."""

    def __init__(self, request: VolatilityRequest):
        from qwork.actors.broken_wing.models import ButterflyScanRequest
        from qwork.actors.portfolio_pnl.models import PortfolioRequest

        self.lock = RLock()
        self.request = request
        self.generation = 0
        self.paused = False
        self.refresh_id = 0
        self.surface = None
        self.surface_revision = 0
        self.term = None
        self.scan = None
        self.scan_request = ButterflyScanRequest()
        self.scan_generation = 0
        self.scan_error = None
        self.portfolio_request = PortfolioRequest()
        self.portfolio_generation = 0
        self.portfolio = None
        self.portfolio_error = None
        self.status = "waiting"
        self.message = "等待 Actor 启动"
        self.updated_at = now()

    def configure(self, request: VolatilityRequest):
        with self.lock:
            if request != self.request:
                self.request = request
                self.generation += 1
                self.surface = self.term = self.scan = self.portfolio = None
                self.status, self.message = "waiting", "参数已应用，等待新行情"
            self.refresh_id += 1

    def controls(self):
        with self.lock:
            return self.request.model_copy(deep=True), self.generation, self.paused, self.refresh_id

    def set_status(self, status, message):
        with self.lock:
            self.status, self.message, self.updated_at = status, message, now()

    def publish_surface(self, frame):
        with self.lock:
            if frame["generation"] != self.generation:
                return False
            self.surface_revision += 1
            frame["surface_revision"] = self.surface_revision
            self.surface = deepcopy(frame)
            self.status = "ready" if frame["points"] else "empty"
            self.message = "行情与曲面已更新" if frame["points"] else "没有符合质量要求的报价；休市可切换历史快照"
            self.updated_at = now()
            return True

    def publish_term(self, frame):
        with self.lock:
            if frame["generation"] == self.generation and matches_surface(frame, self.surface):
                self.term = deepcopy(frame)
                return True
            return False

    def snapshot(self):
        with self.lock:
            return deepcopy(
                {
                    "request": self.request.model_dump(mode="json"),
                    "generation": self.generation,
                    "paused": self.paused,
                    "status": self.status,
                    "message": self.message,
                    "updated_at": self.updated_at,
                    "surface": self.surface,
                    "term": self.term,
                    "scan": self.scan,
                    "scan_error": self.scan_error,
                    "scan_request": self.scan_request.model_dump(mode="json"),
                    "portfolio": self.portfolio,
                    "portfolio_error": self.portfolio_error,
                    "portfolio_request": self.portfolio_request.model_dump(mode="json"),
                }
            )

    def scan_controls(self):
        with self.lock:
            return self.scan_request.model_copy(deep=True), self.scan_generation

    def configure_scan(self, request):
        with self.lock:
            if request != self.scan_request:
                self.scan_request = request
                self.scan_generation += 1
                self.scan = self.scan_error = None

    def publish_scan(self, frame):
        with self.lock:
            if (
                frame["generation"] != self.generation
                or frame["scan_generation"] != self.scan_generation
                or not matches_surface(frame, self.surface)
            ):
                return False
            self.scan = deepcopy(frame)
            self.scan_error = None
            return True

    def set_scan_error(self, message):
        with self.lock:
            self.scan_error = message

    def portfolio_controls(self):
        with self.lock:
            return self.portfolio_request.model_copy(deep=True), self.portfolio_generation

    def configure_portfolio(self, request):
        with self.lock:
            if request != self.portfolio_request:
                expirations = sorted(
                    {leg.expiration for combination in request.combinations for leg in combination.legs}
                )
                base = self.request.as_of if self.request.mode == "historical" else datetime.now(eastern()).date()
                if any(expiration < base for expiration in expirations):
                    raise ValueError("组合有已到期期权；请使用到期前的历史快照")
                if len(expirations) > 24:
                    raise ValueError("最多同时分析 24 个不同到期日")
                if any(
                    leg.symbol != self.request.symbol
                    for combination in request.combinations
                    for leg in combination.legs
                ):
                    raise ValueError("请先把窗口标的设置为组合的标的")
                values = self.request.model_dump()
                values.update(
                    required_expirations=expirations,
                    max_expirations=max(self.request.max_expirations, len(expirations)),
                    required_strikes=sorted(
                        {leg.strike for combination in request.combinations for leg in combination.legs}
                    ),
                    min_dte=min([self.request.min_dte, *[(expiration - base).days for expiration in expirations]]),
                    max_dte=max([self.request.max_dte, *[(expiration - base).days for expiration in expirations]]),
                )
                new_request = VolatilityRequest.model_validate(values)
                self.portfolio_request = request
                self.portfolio_generation += 1
                self.portfolio = self.portfolio_error = None
                self.configure(new_request)

    def publish_portfolio(self, frame):
        with self.lock:
            if (
                frame["generation"] != self.generation
                or frame["portfolio_generation"] != self.portfolio_generation
                or not matches_surface(frame, self.surface)
            ):
                return False
            self.portfolio = deepcopy(frame)
            self.portfolio_error = None
            return True

    def set_portfolio_error(self, message):
        with self.lock:
            self.portfolio_error = message
