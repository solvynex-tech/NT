"""Independent payoff cases check tail risk beyond the finite Plotly viewport."""

import unittest

from qwork.actors.portfolio_pnl.models import PositionLeg
from qwork.analytics.portfolio import expiry_risk
from qwork.ui.terminal.presentation import portfolio_figure


class ExpiryRiskTests(unittest.TestCase):
    def leg(self, strike, side, premium, right="call", expiration="2026-10-09"):
        return PositionLeg(
            symbol="SPY",
            expiration=expiration,
            strike=strike,
            side=side,
            entry_premium=premium,
            right=right,
            quantity=1,
        )

    def test_long_call_loss_and_unbounded_profit_include_entry_fee(self):
        risk = expiry_risk([self.leg(100, "buy", 5)], 0.65)
        self.assertAlmostEqual(risk["max_loss"], 500.65)
        self.assertTrue(risk["unbounded_profit"])
        self.assertFalse(risk["unbounded_loss"])
        self.assertIsNone(risk["max_profit"])
        self.assertAlmostEqual(risk["breakevens"][0], 105.0065)

    def test_short_call_does_not_infer_bounded_loss_from_a_chart(self):
        risk = expiry_risk([self.leg(100, "sell", 5)], 0.65)
        self.assertTrue(risk["unbounded_loss"])
        self.assertIsNone(risk["max_loss"])
        self.assertAlmostEqual(risk["max_profit"], 499.35)
        self.assertAlmostEqual(risk["breakevens"][0], 104.9935)

    def test_put_credit_spread_exact_maximum_loss_and_break_even(self):
        risk = expiry_risk([self.leg(90, "buy", 1, "put"), self.leg(100, "sell", 3, "put")], 1.3)
        self.assertAlmostEqual(risk["max_profit"], 198.7)
        self.assertAlmostEqual(risk["max_loss"], 801.3)
        self.assertAlmostEqual(risk["breakevens"][0], 98.013)
        self.assertFalse(risk["unbounded_loss"])

    def test_calendar_and_flat_positions_have_no_misleading_point_extrema(self):
        mixed = expiry_risk([self.leg(100, "buy", 1), self.leg(100, "sell", 2, expiration="2026-10-16")])
        self.assertEqual(mixed, {"kind": "mixed_expirations"})
        flat = expiry_risk([self.leg(100, "buy", 1), self.leg(100, "sell", 1)])
        self.assertEqual(flat["max_loss"], 0)
        self.assertTrue(flat["zero_payoff_ranges"])

    def test_payoff_axis_tracks_visible_prices_without_changing_actor_values(self):
        aggregate = dict(scenario_prices=[0, 100, 110, 1000], expiry_pnl=[-500, 0, 500, 50000], current_pnl=None)
        portfolio = dict(aggregate=aggregate, combinations=[dict(spot=105)])
        figure = portfolio_figure(portfolio, [dict(strike=100)])
        self.assertEqual(list(figure.data[0].y), aggregate["expiry_pnl"])
        self.assertLess(figure.layout.yaxis.range[1], 1000)
