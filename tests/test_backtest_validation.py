"""Mathematical reconciliation fixtures; these are not market-performance evidence."""

import unittest
from datetime import datetime
from types import SimpleNamespace

from qwork.backtesting.validation import audit_replay
from qwork.core.domain import Contract, Quote, eastern, timestamp_ns
from qwork.strategies.broken_wing.models import BrokenWingConfig


class BacktestAuditTests(unittest.TestCase):
    def fixture(self):
        start = datetime(2026, 10, 2, 9, 45, tzinfo=eastern())
        close = datetime(2026, 10, 2, 15, 55, tzinfo=eastern())
        config = BrokenWingConfig(margin_budget=1000, max_positions=1, slippage_per_share=0)
        candidate = dict(key="holding", expiration="2026-10-16", right="put", strikes=[95, 100, 102])
        history = {}
        for strike, first, last in [(95, 0.5, 0.7), (100, 0.5, 0.5), (102, 0.4, 0.4)]:
            key = (candidate["expiration"], strike, "put")
            contract = Contract("SPY", *key)
            history[key] = [
                Quote(contract, timestamp_ns(when), value, value, 10, 10)
                for when, value in [(start, first), (close, last)]
            ]
        fills = [
            dict(
                client_order_id=str(i),
                instrument_id="proxy",
                ts_event=when.isoformat(),
                order_side=side,
                last_qty="1",
                last_px=price,
                commission="2.60 USD",
            )
            for i, (when, side, price) in enumerate([(start, "BUY", "16.90"), (close, "SELL", "17.10")])
        ]
        position = dict(
            position_id="p",
            avg_px_open=16.9,
            avg_px_close=17.1,
            peak_qty=1,
            side="FLAT",
            commissions="['5.20 USD']",
            realized_pnl="14.80 USD",
        )
        native = dict(
            fills=fills,
            positions=[position],
            account=[dict(total="1000", locked="0"), dict(total="1014.80", locked="0")],
        )
        report = dict(
            date="2026-10-02",
            config=config.model_dump(mode="json"),
            selection_timestamp=start.isoformat(),
            session_close=datetime(2026, 10, 2, 16, tzinfo=eastern()).isoformat(),
            net_pnl=14.8,
            flat=True,
            open_or_pending_combinations=[],
            events=[
                dict(kind="open_requested", key="holding", probability=0.8, ratio=1.1, margin_reserved=110),
                dict(kind="combination_completed", key="holding"),
            ],
        )
        spreads = {"holding": SimpleNamespace(id="proxy", info=dict(accounting_price_offset=17))}
        return report, native, [candidate], spreads, history

    def test_correct_source_quotes_and_native_account_reconcile(self):
        audit = audit_replay(*self.fixture())
        self.assertTrue(audit["passed"])
        self.assertEqual(audit["pnl_attribution"]["mid_mark_change"], 20)
        self.assertEqual(audit["pnl_attribution"]["commission"], 5.2)

    def test_wrong_execution_price_and_account_balance_are_rejected(self):
        args = self.fixture()
        args[1]["fills"][1]["last_px"] = "17.20"
        args[1]["account"][-1]["total"] = "1015.80"
        audit = audit_replay(*args)
        failed = {c["name"] for c in audit["checks"] if not c["passed"]}
        self.assertIn("fill_prices_against_source_nbbo", failed)
        self.assertIn("account_cash_reconciliation", failed)

    def test_stale_execution_quote_is_rejected(self):
        args = self.fixture()
        args[1]["fills"][1]["ts_event"] = datetime(2026, 10, 2, 15, 56, tzinfo=eastern()).isoformat()
        audit = audit_replay(*args)
        failures = next(c["failures"] for c in audit["checks"] if c["name"] == "fill_prices_against_source_nbbo")
        self.assertTrue(any(c.get("reason") == "Stale execution quote" for c in failures))

    def test_resting_limit_maker_price_is_checked_against_limit(self):
        args = self.fixture()
        args[0]["execution"] = dict(combo_spread_fraction=1, latency_ms=0)
        args[1]["orders"] = [dict(client_order_id="0", price="16.95")]
        args[1]["fills"][0].update(order_type="LIMIT", liquidity_side="MAKER", last_px="16.95")
        audit = audit_replay(*args)
        price_check = next(c for c in audit["checks"] if c["name"] == "fill_prices_against_source_nbbo")
        self.assertTrue(price_check["passed"])

    def test_inside_spread_limit_cannot_fill_without_observed_cross(self):
        args = self.fixture()
        args[0]["execution"] = dict(combo_spread_fraction=1, latency_ms=0)
        args[1]["orders"] = [dict(client_order_id="0", price="16.85")]
        args[1]["fills"][0].update(order_type="LIMIT", liquidity_side="MAKER", last_px="16.85")
        audit = audit_replay(*args)
        price_check = next(c for c in audit["checks"] if c["name"] == "fill_prices_against_source_nbbo")
        self.assertFalse(price_check["passed"])

    def test_half_cent_combo_rounding_is_attributed_separately(self):
        args = self.fixture()
        args[0]["execution"] = dict(combo_spread_fraction=0.5, latency_ms=0)
        key = ("2026-10-16", 95, "put")
        original = args[4][key][0]
        args[4][key][0] = Quote(original.contract, original.ts_ns, 0.48, 0.5, 10, 10)
        audit = audit_replay(*args)
        attribution_check = next(c for c in audit["checks"] if c["name"] == "source_quote_pnl_attribution")
        self.assertTrue(attribution_check["passed"])
        self.assertAlmostEqual(abs(audit["pnl_attribution"]["quote_rounding"]), 0.5)

    def test_marketable_limit_cannot_invent_inside_spread_improvement(self):
        args = self.fixture()
        args[0]["execution"] = dict(combo_spread_fraction=1, latency_ms=0)
        args[1]["orders"] = [dict(client_order_id="0", price="17.00")]
        args[1]["fills"][0].update(order_type="LIMIT", liquidity_side="TAKER", last_px="16.85")
        audit = audit_replay(*args)
        price_check = next(c for c in audit["checks"] if c["name"] == "fill_prices_against_source_nbbo")
        self.assertFalse(price_check["passed"])
