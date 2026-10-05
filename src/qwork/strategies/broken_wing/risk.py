"""Entry guard using reported account equity, never a duplicate PnL ledger."""

import math

from pydantic import Field

from qwork.core.validation import StrictModel


class AccountRiskConfig(StrictModel):
    daily_loss_limit_usd: float = Field(default=500, gt=0)
    max_drawdown_usd: float = Field(default=1000, gt=0)
    account_max_age_seconds: int = Field(default=300, ge=1, le=3600)
    available_funds_buffer_usd: float = Field(default=100, ge=0)
    max_entry_rejections: int = Field(default=3, ge=1, le=20)


class AccountRiskGuard:
    def __init__(self, config, state=None):
        self.config = config
        self.state = dict(state or {})
        self.available_funds = None

    def check(self, event, now_ns, today):
        self.available_funds = None
        if self.state.get("stop_reason"):
            return self.state["stop_reason"]
        if (
            not event
            or not event.is_reported
            or not 0 <= now_ns - event.ts_event <= self.config.account_max_age_seconds * 1e9
        ):
            return "account_data_unavailable_or_stale"
        if str(event.base_currency) != "USD":
            return "USD_account_equity_required"
        info = event.info or {}
        try:
            equity = float(info["NetLiquidation"])
            available = float(info["AvailableFunds"])
            if not all(math.isfinite(v) for v in [equity, available]) or equity <= 0:
                raise ValueError()
        except (KeyError, TypeError, ValueError):
            return "reported_equity_or_available_funds_missing"
        if self.state.get("session_date") != today:
            self.state = dict(session_date=today, start_equity=equity, peak_equity=equity, entry_rejections=0)
        self.state["peak_equity"] = max(self.state["peak_equity"], equity)
        self.available_funds = max(0, available - self.config.available_funds_buffer_usd)
        if self.state["start_equity"] - equity >= self.config.daily_loss_limit_usd:
            self.state["stop_reason"] = "daily_account_loss_limit"
        elif self.state["peak_equity"] - equity >= self.config.max_drawdown_usd:
            self.state["stop_reason"] = "account_drawdown_limit"
        return self.state.get("stop_reason")

    def reject(self):
        self.state["entry_rejections"] = self.state.get("entry_rejections", 0) + 1
        if self.state["entry_rejections"] >= self.config.max_entry_rejections:
            self.state["stop_reason"] = "entry_rejection_limit"
        return self.state.get("stop_reason")
