from __future__ import annotations

import math
from datetime import UTC, datetime, timedelta
from threading import Event

import numpy as np
import QuantLib as ql

from qwork.analytics.volatility import calculate_expiration, maturity
from qwork.core.config import now
from qwork.core.domain import eastern
from qwork.providers.theta.provider import ProviderError, ThetaProvider


class DeadlineStub:
    """Apply a gRPC deadline without changing the official SDK's response conversion."""

    def __init__(self, stub):
        self.stub = stub

    def __getattr__(self, name):
        call = getattr(self.stub, name)
        return lambda *args, **kwargs: call(*args, **{"timeout": 15, **kwargs})


class BoundedThetaProvider(ThetaProvider):
    def _new_client(self, previous_session=None):
        client = super()._new_client(previous_session)
        client.stub = DeadlineStub(client.stub)
        return client


class SurfaceCollector:
    def __init__(self, settings):
        self.provider = BoundedThetaProvider(settings.theta_key, settings.data_dir)
        self.stopped = Event()
        self.expiration_cache = {}
        self.greeks_supported = True

    def close(self):
        self.stopped.set()

    def collect(self, request, generation):
        observed = (
            datetime.combine(request.as_of, request.at, tzinfo=eastern())
            if request.mode == "historical"
            else datetime.now(UTC)
        )
        notes = []
        if request.source == "fixture":
            dates = [
                observed.date() + timedelta(days=day) for day in (7, 14, 30, 60, 90, 180)
            ] + request.required_expirations
        else:
            cache_key = (request.symbol, datetime.now(eastern()).date())
            if cache_key not in self.expiration_cache:
                self.expiration_cache = {cache_key: self.provider.expirations(request.symbol)}
            dates = [datetime.fromisoformat(value).date() for value in self.expiration_cache[cache_key]]
        eligible = sorted(
            {
                value
                for value in dates
                if request.min_dte <= (value - observed.astimezone(eastern()).date()).days <= request.max_dte
            }
        )
        dates = [value for value in eligible if not request.expirations or value in request.expirations]
        if len(dates) > request.max_expirations:
            dates = [dates[index] for index in np.linspace(0, len(dates) - 1, request.max_expirations, dtype=int)]
        required = [value for value in request.required_expirations if value in eligible]
        dates = sorted(
            set(
                required
                + [value for value in dates if value not in required][: max(0, request.max_expirations - len(required))]
            )
        )
        points, rejected = [], 0
        batches = []
        for expiration in dates:
            if self.stopped.is_set():
                break
            try:
                greeks = []
                if request.source == "fixture":
                    rows = self._fixture(request, expiration, observed)
                elif request.mode == "historical":
                    rows = self.provider.historical_chain(
                        request.symbol, expiration.isoformat(), request.as_of.isoformat(), request.at.isoformat()
                    )["rows"]
                else:
                    rows = self.provider.chain(request.symbol, expiration.isoformat())["rows"]
                    if request.iv_method == "auto" and self.greeks_supported and not self.stopped.is_set():
                        try:
                            greeks = self.provider._call(
                                "option_snapshot_greeks_implied_volatility",
                                symbol=request.symbol,
                                expiration=expiration,
                                strike="*",
                                right="both",
                            )
                        except ProviderError as exc:
                            if "套餐" in str(exc):
                                self.greeks_supported = False
                            notes.append("数据商 IV 不可用，使用 QuantLib 报价反算")
                batches.append((expiration, rows, greeks))
            except (ProviderError, ValueError) as exc:
                notes.append(f"{expiration}: {exc}")
        if request.mode == "live":
            observed = datetime.now(UTC)
        for expiration, rows, greeks in batches:
            try:
                calculated, bad, warnings = calculate_expiration(rows, greeks, expiration, observed, request)
                points.extend(calculated)
                rejected += bad
                notes.extend(f"{expiration}: {warning}" for warning in warnings)
            except (ProviderError, ValueError) as exc:
                notes.append(f"{expiration}: {exc}")
        if any(point["method"] != "theta" for point in points):
            notes.append("QuantLib 为欧式 Black IV 近似；美式提前行权及离散分红可能造成差异")
        if not request.spot and points:
            notes.append("远期价由同步 Call/Put 平价估计，不额外请求股票数据；可填写现价覆盖")
        return {
            "generation": generation,
            "symbol": request.symbol,
            "mode": request.mode,
            "synthetic": request.source == "fixture",
            "updated_at": now(),
            "observed_at": observed.isoformat(),
            "points": points,
            "rejected": rejected,
            "notes": list(dict.fromkeys(notes)),
            "expirations": [value.isoformat() for value in dates],
            "available_expirations": [value.isoformat() for value in eligible],
        }

    @staticmethod
    def _fixture(request, expiration, observed):
        """Explicit mathematical fixtures; never used as a fallback for Theta failures."""
        spot = request.spot or 600
        years = maturity(expiration, observed)
        forward = spot * math.exp((request.rate - request.dividend_yield) * years)
        rows = []
        for strike in sorted(set(np.linspace(spot * 0.9, spot * 1.1, 21)) | set(request.required_strikes)):
            vol = (
                0.2
                + 0.07 * math.sqrt(years)
                - 0.15 * math.log(strike / forward)
                + 0.5 * math.log(strike / forward) ** 2
            )
            for right in ("call", "put"):
                price = ql.blackFormula(
                    ql.Option.Call if right == "call" else ql.Option.Put,
                    float(strike),
                    forward,
                    vol * math.sqrt(years),
                    math.exp(-request.rate * years),
                )
                rows.append(
                    {
                        "strike": float(strike),
                        "right": right,
                        "mid": price,
                        "bid": max(0.00001, price * 0.99),
                        "ask": max(0.00002, price * 1.01),
                        "bid_size": 100,
                        "ask_size": 100,
                        "ts_ns": int(observed.timestamp() * 1e9),
                        "timestamp": observed.isoformat(),
                    }
                )
        return rows
