import unittest
from types import SimpleNamespace
from unittest.mock import Mock

from nautilus_trader.model import AccountId

from qwork.strategies.broken_wing.instruments import sim_option
from qwork.ui.terminal.backend import native_view
from qwork.ui.terminal.combinations import portfolio_request, quote_leg
from qwork.ui.terminal.presentation import portfolio_figure


class CombinationTests(unittest.TestCase):
    def test_native_option_metadata_keeps_accounting_values(self):
        instrument = sim_option("SPY", ("2026-10-09", 750, "put"), "2026-10-02")
        account = SimpleNamespace(last_event=None, balances_total=lambda: {})
        row = dict(instrument_id=str(instrument.id), quantity="2", avg_px_open="1.40", side="LONG")
        cache = Mock()
        cache.account.return_value = account
        cache.positions.return_value = [SimpleNamespace(to_dict=lambda: row)]
        cache.orders.return_value = []
        cache.instrument.return_value = instrument
        exported = native_view(cache, AccountId("IB-DU123"))["positions"][0]
        self.assertEqual(
            (exported["underlying"], exported["expiration"], exported["right"], exported["strike"]),
            ("SPY", "2026-10-09", "put", 750),
        )
        self.assertEqual(exported["quantity"], "2")
        self.assertEqual(exported["avg_px_open"], "1.40")
        self.assertEqual(exported["multiplier"], 100)

    def setUp(self):
        self.frame = dict(
            symbol="SPY",
            points=[
                dict(
                    expiration="2026-10-09", strike=750, right="put", bid=1.2, ask=1.4, timestamp="2026-10-02T13:45:00Z"
                )
            ],
        )
        self.row = dict(expiration="2026-10-09", strike=750)

    def test_quote_side_cost_units_and_grouping(self):
        buy = quote_leg(self.frame, self.row, "put_ask", "buy", "A")
        sell = quote_leg(self.frame, self.row, "put_bid", "sell", "B")
        self.assertEqual(buy["entry_premium"], 1.4)
        self.assertEqual(sell["entry_premium"], 1.2)
        self.assertEqual(buy["multiplier"], 100)
        request = portfolio_request([buy, sell], 0.65)
        self.assertEqual(len(request.combinations), 2)
        sell["combo_name"] = "A"
        self.assertEqual(len(portfolio_request([buy, sell], 0.65).combinations[0].legs), 2)
        sell["quantity"] = 1.5
        with self.assertRaises(ValueError):
            portfolio_request([sell], 0.65)

    def test_missing_crossed_and_wrong_contract_quotes_are_rejected(self):
        for column in ["strike", "call_ask"]:
            with self.assertRaises(ValueError):
                quote_leg(self.frame, self.row, column, "buy", "A")
        self.frame["points"][0]["ask"] = 0.5
        with self.assertRaises(ValueError):
            quote_leg(self.frame, self.row, "put_ask", "buy", "A")

    def test_individual_graph_uses_actor_values_without_modifying_aggregate(self):
        portfolio = dict(
            aggregate=dict(scenario_prices=[1, 2], expiry_pnl=[3, 4], current_pnl=[5, 6]),
            combinations=[dict(name="A", scenario_prices=[1, 2], expiry_pnl=[7, 8], current_pnl=[9, 10])],
        )
        figure = portfolio_figure(portfolio, combination="A")
        self.assertEqual(list(figure.data[0].y), [7, 8])
        self.assertEqual(portfolio["aggregate"]["expiry_pnl"], [3, 4])
