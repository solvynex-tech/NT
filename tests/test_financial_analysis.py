"""All prices in this file are mathematical fixtures, not market evidence."""

import math
import tempfile
import unittest
from copy import deepcopy
from datetime import datetime, timedelta
from pathlib import Path

import QuantLib as ql
from pydantic import ValidationError

from qwork.actors.broken_wing.models import ButterflyScanRequest
from qwork.actors.portfolio_pnl.models import PortfolioRequest
from qwork.analytics.butterfly import profit_regions, scan_butterflies
from qwork.analytics.portfolio import analyze_portfolio
from qwork.analytics.volatility import calculate_expiration, implied_vol, maturity, term_structure
from qwork.core.config import Settings
from qwork.core.domain import eastern
from qwork.core.state import DashboardState, VolatilityRequest
from qwork.providers.theta.collector import SurfaceCollector


def fixture_frame():
    request = VolatilityRequest(
        source="fixture", mode="historical", as_of="2026-01-05", spot=100, max_expirations=3, max_dte=30
    )
    with tempfile.TemporaryDirectory() as directory:
        collector = SurfaceCollector(Settings(Path(directory)))
        frame = collector.collect(request, 0)
        collector.close()
    return request, frame


def portfolio(expiration, quantity=1, strike=100, name="Call"):
    return PortfolioRequest(
        combinations=[
            dict(
                name=name,
                legs=[
                    dict(
                        symbol="SPY",
                        expiration=expiration,
                        strike=strike,
                        right="call",
                        side="buy",
                        quantity=quantity,
                        entry_premium=2,
                    )
                ],
            )
        ],
        commission_per_contract=0.5,
    )


class FinancialTests(unittest.TestCase):
    def test_quantlib_iv_roundtrip_call_and_put(self):
        for right, option in [("call", ql.Option.Call), ("put", ql.Option.Put)]:
            price = ql.blackFormula(option, 105, 102, 0.31 * math.sqrt(0.4), math.exp(-0.04 * 0.4))
            self.assertAlmostEqual(implied_vol(price, 105, 102, 0.4, 0.04, right), 0.31, places=7)

    def test_actual_expiry_clock_and_timezone(self):
        observed = datetime(2026, 1, 9, 15, tzinfo=eastern())
        self.assertAlmostEqual(maturity(observed.date(), observed), 1 / (365 * 24))
        self.assertEqual(maturity(observed.date(), observed + timedelta(hours=2)), 0)

    def test_stale_and_future_quotes_are_rejected(self):
        request, frame = fixture_frame()
        observed = datetime.fromisoformat(frame["observed_at"])
        expiration = datetime.fromisoformat(frame["expirations"][0]).date()
        rows = SurfaceCollector._fixture(request, expiration, observed)
        for delta in [-1300, 1]:
            bad = deepcopy(rows)
            for row in bad:
                row["ts_ns"] += int(delta * 1e9)
            points, rejected, _ = calculate_expiration(bad, [], expiration, observed, request)
            self.assertFalse(points)
            self.assertEqual(rejected, len(rows))

    def test_forward_parity_without_stock_quotes(self):
        request, frame = fixture_frame()
        observed = datetime.fromisoformat(frame["observed_at"])
        expiration = datetime.fromisoformat(frame["expirations"][0]).date()
        rows = SurfaceCollector._fixture(request, expiration, observed)
        points, _, _ = calculate_expiration(rows, [], expiration, observed, request.model_copy(update={"spot": None}))
        self.assertTrue(points)
        self.assertAlmostEqual(points[0]["forward"], frame["points"][0]["forward"], places=7)

    def test_term_structure_never_extrapolates(self):
        _, frame = fixture_frame()
        frame["points"] = [point for point in frame["points"] if point["strike"] < 99]
        self.assertFalse(term_structure(frame)["rows"])

    def test_calendar_variance_warning(self):
        _, frame = fixture_frame()
        for point in frame["points"]:
            point["iv"] = 1 if point["expiration"] == frame["expirations"][0] else 0.05
        rows = term_structure(frame)["rows"]
        self.assertTrue(any(row["calendar_variance_warning"] for row in rows))

    def test_exact_butterfly_payoff_and_roots(self):
        profit, loss, roots, intervals = profit_regions([80, 95, 100], "put", -300)
        self.assertEqual((profit, loss), (800, 700))
        self.assertEqual(roots, [87])
        self.assertEqual(intervals, [(87, math.inf)])

    def test_butterfly_direction_probability_and_strict_threshold(self):
        _, frame = fixture_frame()
        template = deepcopy(frame["points"][0])
        points = []
        for right, values in [
            ("put", [(80, 0.1, 0.1, 0.4), (95, 2, 2.01, 0.35), (100, 0.89, 0.9, 0.2)]),
            ("call", [(95, 5, 5.1, 0.2), (100, 2, 2.1, 0.2), (105, 1, 1.1, 0.2)]),
        ]:
            for strike, bid, ask, iv in values:
                point = dict(
                    template,
                    strike=strike,
                    bid=bid,
                    ask=ask,
                    iv=iv,
                    right=right,
                    forward=100,
                    moneyness=strike / 100,
                    years=7 / 365,
                    dte=7,
                    bid_size=100,
                    ask_size=100,
                )
                points.append(point)
        frame["points"] = points
        request = ButterflyScanRequest(body_band=0.06, max_wing_fraction=0.2)
        result = scan_butterflies(frame, request)
        self.assertEqual(len(result["candidates"]), 1)
        row = result["candidates"][0]
        self.assertEqual(row["wider_wing"], "downside")
        self.assertGreater(row["model_probability"], 0.7)
        self.assertGreaterEqual(row["profit_loss_ratio"], 1)
        self.assertIsNone(row["historical_win_rate"])
        strict = request.model_copy(update={"min_probability": row["model_probability"]})
        self.assertFalse(scan_butterflies(frame, strict)["candidates"])
        no_size = deepcopy(frame)
        no_size["points"][1]["bid_size"] = 1
        self.assertFalse(scan_butterflies(no_size, request)["candidates"])

    def test_long_call_cost_multiplier_fees_and_expiry_kink(self):
        request, frame = fixture_frame()
        combo = portfolio(frame["expirations"][0], quantity=2)
        row = analyze_portfolio(frame, combo, request)["combinations"][0]
        at_strike = row["scenario_prices"].index(100)
        self.assertEqual(row["expiry_pnl"][at_strike], -401)
        at_110 = min(range(len(row["scenario_prices"])), key=lambda index: abs(row["scenario_prices"][index] - 110))
        self.assertAlmostEqual(row["expiry_pnl"][at_110], 1599)
        point = next(p for p in frame["points"] if p["strike"] == 100 and p["right"] == "call")
        self.assertAlmostEqual(row["liquidation_pnl"], point["bid"] * 200 - 402)

    def test_short_call_liquidation_uses_ask(self):
        request, frame = fixture_frame()
        combo = portfolio(frame["expirations"][0])
        combo.combinations[0].legs[0].side = "sell"
        row = analyze_portfolio(frame, combo, request)["combinations"][0]
        point = next(p for p in frame["points"] if p["strike"] == 100 and p["right"] == "call")
        self.assertAlmostEqual(row["liquidation_pnl"], 200 - point["ask"] * 100 - 1)

    def test_multiple_combinations_sum_and_missing_quotes(self):
        request, frame = fixture_frame()
        combo = portfolio(frame["expirations"][0])
        combo.combinations.append(combo.combinations[0].model_copy(deep=True, update={"name": "Second"}))
        result = analyze_portfolio(frame, combo, request)
        self.assertAlmostEqual(result["aggregate"]["liquidation_pnl"], result["combinations"][0]["liquidation_pnl"] * 2)
        combo.combinations[1].legs[0].strike = 109.5
        result = analyze_portfolio(frame, combo, request)
        self.assertEqual(len(result["combinations"]), 2)
        self.assertIsNone(result["combinations"][1]["current_pnl"])
        self.assertTrue(result["combinations"][1]["expiry_pnl"])
        self.assertIsNone(result["aggregate"]["liquidation_pnl"])
        self.assertTrue(result["aggregate"]["partial"])

    def test_expiry_curve_without_any_market_quotes(self):
        request, frame = fixture_frame()
        combo = portfolio(frame["expirations"][0])
        frame["points"] = []
        request = request.model_copy(update={"spot": None})
        result = analyze_portfolio(frame, combo, request)
        self.assertTrue(result["combinations"][0]["expiry_pnl"])
        self.assertIsNone(result["combinations"][0]["liquidation_pnl"])

    def test_mixed_expiry_is_labelled_static_scenario(self):
        request, frame = fixture_frame()
        combo = portfolio(frame["expirations"][0])
        leg = (
            combo.combinations[0]
            .legs[0]
            .model_copy(update={"expiration": datetime.fromisoformat(frame["expirations"][1]).date()})
        )
        combo.combinations[0].legs.append(leg)
        result = analyze_portfolio(frame, combo, request)
        self.assertTrue(result["combinations"][0]["mixed_expirations"])
        self.assertTrue(any("静态情景" in note for note in result["diagnostics"]))

    def test_stale_results_cannot_overwrite_new_parameters(self):
        request, frame = fixture_frame()
        state = DashboardState(request)
        state.configure(request.model_copy(update={"symbol": "QQQ"}))
        self.assertFalse(state.publish_surface(frame))
        self.assertIsNone(state.snapshot()["surface"])
        self.assertFalse(state.publish_portfolio(dict(frame, portfolio_generation=0)))

    def test_invalid_portfolio_does_not_mutate_state(self):
        request, _ = fixture_frame()
        state = DashboardState(request)
        before = state.snapshot()
        with self.assertRaises(ValidationError):
            state.configure_portfolio(portfolio("2040-01-01"))
        self.assertEqual(state.snapshot(), before)
        with self.assertRaises(ValidationError):
            portfolio("2026-01-12", quantity=0)


if __name__ == "__main__":
    unittest.main()
