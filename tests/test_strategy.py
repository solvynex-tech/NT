"""Strategy rule regressions and real Nautilus accounting using mathematical data."""

import unittest
from datetime import datetime, timedelta
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import Mock, patch

from nautilus_trader.backtest import BacktestEngine, BacktestEngineConfig
from nautilus_trader.common import LoggerConfig, LogLevel
from nautilus_trader.config import StrategyConfig
from nautilus_trader.execution import PerContractFeeModel
from nautilus_trader.model import AccountType, Currency, Money, OmsType, OrderSide, Price, Quantity, QuoteTick, Venue
from nautilus_trader.trading import Strategy

from qwork.core.domain import eastern, timestamp_ns
from qwork.strategies.broken_wing.analytics import (
    exit_decision,
    identity,
    quotes_aligned,
    rank_candidates,
    tick_metrics,
)
from qwork.strategies.broken_wing.instruments import contract_keys, sim_option, sim_spread
from qwork.strategies.broken_wing.models import BrokenWingConfig
from qwork.strategies.broken_wing.strategy import BrokenWingStrategy


class StrategyRulesTests(unittest.TestCase):
    def test_native_flatten_waits_for_current_quote_before_market_fill(self):
        # Mathematical execution fixture; model scores are fixed to isolate timing.
        candidate = dict(
            symbol="SPY",
            expiration="2026-10-16",
            right="put",
            strikes=[95, 100, 102],
            legs=[dict(symbol="SPY")],
            max_loss=100,
            key="holding",
        )
        instrument = sim_spread(candidate, "2026-10-02", 0)
        offset = instrument.info["accounting_price_offset"]
        options = {key: sim_option("SPY", key, "2026-10-02") for key in contract_keys([candidate])}
        config = BrokenWingConfig(margin_budget=1000, max_positions=1, max_units=1)
        strategy = BrokenWingStrategy(
            config, [candidate], {"holding": instrument}, {k: v.id for k, v in options.items()}
        )
        opening = timestamp_ns(datetime(2026, 10, 2, 9, 45, tzinfo=eastern()))
        flatten = timestamp_ns(datetime(2026, 10, 2, 15, 55, tzinfo=eastern()))

        def quote(instrument_id, stamp, bid, ask):
            return QuoteTick(
                instrument_id=instrument_id,
                bid_price=Price.from_str(f"{bid:.2f}"),
                ask_price=Price.from_str(f"{ask:.2f}"),
                bid_size=Quantity.from_int(10),
                ask_size=Quantity.from_int(10),
                ts_event=stamp,
                ts_init=stamp,
            )

        ticks = [quote(option.id, opening, 1, 1.1) for option in options.values()]
        ticks += [quote(instrument.id, opening, offset - 0.2, offset - 0.1)]
        ticks += [quote(instrument.id, flatten, offset + 0.3, offset + 0.4)]
        metrics = dict(
            spot=103,
            pnl=0,
            breakevens=[],
            ivs=[0.2] * 3,
            probability=0.8,
            probability_horizon="intraday",
            expiration_probability=0.8,
            ratio=2,
            net_gamma=0,
            max_loss=100,
            max_profit=200,
            expiry_plateau_profit=10,
            ask_size=10,
        )
        usd = Currency.from_str("USD")
        engine = BacktestEngine(config=BacktestEngineConfig(logging=LoggerConfig(stdout_level=LogLevel.ERROR)))
        try:
            engine.add_venue(
                venue=Venue("SIM"),
                oms_type=OmsType.NETTING,
                account_type=AccountType.MARGIN,
                base_currency=usd,
                starting_balances=[Money(1000, usd)],
                fee_model=PerContractFeeModel(Money(2.6, usd)),
            )
            for item in [*options.values(), instrument]:
                engine.add_instrument(item)
            engine.add_data(ticks)
            engine.add_strategy(strategy)
            with patch("qwork.strategies.broken_wing.strategy.tick_metrics", return_value=metrics):
                engine.run()
            position = engine.cache.positions()[0]
            self.assertTrue(position.is_closed)
            self.assertAlmostEqual(float(position.avg_px_close), offset + 0.3)
            self.assertAlmostEqual(float(position.realized_pnl), 34.8, places=2)
            self.assertEqual(strategy.positions["holding"]["state"], "closed")
        finally:
            engine.dispose()

    def test_intraday_flatten_does_not_depend_on_quote_or_iv_validation(self):
        now = timestamp_ns(datetime(2026, 10, 2, 15, 55, tzinfo=eastern()))
        context = SimpleNamespace(
            clock=SimpleNamespace(timestamp_ns=lambda: now),
            settings=BrokenWingConfig(),
            pending={},
            positions={"holding": dict(state="open", units=2)},
            quotes=Mock(side_effect=RuntimeError("No usable IV")),
            close_position=Mock(),
        )
        BrokenWingStrategy.evaluate(context)
        context.close_position.assert_called_once_with("holding", "intraday_flatten")
        context.quotes.assert_not_called()

    def test_new_and_old_minute_legs_cannot_be_combined(self):
        row = dict(bid=1, ask=1.1, bid_size=10, ask_size=10, ts_ns=120_000_000_000)
        mixed = [row, row, row, dict(row, ts_ns=60_000_000_000)]
        self.assertFalse(quotes_aligned(mixed, 120_000_000_000, 65, 1))
        self.assertTrue(quotes_aligned([row] * 4, 120_000_000_000, 65, 1))
        self.assertFalse(quotes_aligned([row] * 4, 119_000_000_000, 65, 1))

    def position(self):
        return dict(
            candidate=dict(strikes=[95, 100, 102]),
            max_profit_per_unit=100,
            initial_plateau_profit=10,
            visited_profit_zone=False,
            breakeven_before_zone=False,
            last_spot=103,
        )

    def test_initial_negative_pnl_is_not_a_profit_drawdown(self):
        position = self.position()
        metrics = dict(spot=104, pnl=-8, breakevens=[103.5])
        self.assertIsNone(exit_decision(position, metrics, BrokenWingConfig()))
        self.assertTrue(position["breakeven_before_zone"])

    def test_profit_must_cross_twenty_percent_from_above(self):
        position = self.position()
        position.update(breakeven_before_zone=True, last_pnl=3)
        metrics = dict(spot=104, pnl=1.5, breakevens=[])
        self.assertEqual(exit_decision(position, metrics, BrokenWingConfig()), "iv_plateau_20")

    def test_seventy_percent_take_profit_is_independent(self):
        self.assertEqual(
            exit_decision(self.position(), dict(spot=101, pnl=70, breakevens=[]), BrokenWingConfig()), "take_profit_70"
        )

    def test_entered_profit_zone_disables_early_breakeven_exit(self):
        position = self.position()
        position.update(visited_profit_zone=True, last_pnl=3)
        self.assertIsNone(exit_decision(position, dict(spot=104, pnl=1.5, breakevens=[103.5]), BrokenWingConfig()))

    def test_gamma_optional_but_probability_and_ratio_strict(self):
        config = BrokenWingConfig(rank_gamma=False)
        base = dict(legs=[dict(symbol="SPY")], expiration="2026-01-16", right="put", strikes=[95, 100, 102])
        scan = dict(
            candidates=[
                dict(base, model_probability=0.7, profit_loss_ratio=1.1),
                dict(base, model_probability=0.71, profit_loss_ratio=1),
                dict(base, model_probability=0.71, profit_loss_ratio=1.1),
            ]
        )
        candidates = rank_candidates(dict(points=[]), scan, config)
        self.assertEqual(len(candidates), 1)
        self.assertIsNone(candidates[0]["net_gamma"])
        self.assertNotEqual(identity(base), identity(dict(base, strikes=[94, 100, 102])))

    def test_expiry_probability_is_not_intraday_probability(self):
        import QuantLib as ql

        from qwork.analytics.volatility import maturity

        observed = datetime(2026, 1, 5, 9, 45, tzinfo=eastern())
        candidate = dict(expiration="2026-01-16", right="put", strikes=[99, 101, 102])
        config = BrokenWingConfig(commission_per_contract=0, slippage_per_share=0)
        years = maturity(datetime(2026, 1, 16).date(), observed)
        forward = 102
        discount = ql.blackFormula(ql.Option.Put, 99, forward, 0.2 * years**0.5, 2.718281828 ** (-0.04 * years))
        high = ql.blackFormula(ql.Option.Put, 102, forward, 0.2 * years**0.5, 2.718281828 ** (-0.04 * years))
        middle = (discount + high + 0.1) / 2
        rows = [dict(bid=value, ask=value, bid_size=10, ask_size=10) for value in (discount, middle, high)]
        call = middle + (forward - 101) * 2.718281828 ** (-0.04 * years)
        counterpart = dict(bid=call, ask=call, bid_size=10, ask_size=10)
        expiry = tick_metrics(
            candidate, rows, counterpart, observed, config.model_copy(update={"probability_horizon": "expiration"})
        )
        intraday = tick_metrics(candidate, rows, counterpart, observed, config)
        self.assertEqual(expiry["probability"], expiry["expiration_probability"])
        self.assertEqual(intraday["probability"], intraday["intraday_probability"])
        self.assertNotAlmostEqual(expiry["probability"], intraday["probability"], places=3)

    def test_native_proxy_cash_and_commission_reconcile(self):
        candidate = dict(legs=[], strikes=[95, 100, 102], expiration="2026-01-16", max_loss=100)
        candidate["legs"] = [dict(symbol="SPY")]
        instrument = sim_spread(candidate, "2026-01-05", 0)
        offset = instrument.info["accounting_price_offset"]

        class RoundTrip(Strategy):
            def __new__(cls):
                return super().__new__(cls, StrategyConfig(order_id_tag="TEST", log_events=False, log_commands=False))

            def __init__(self):
                self.count = 0

            def on_start(self):
                self.subscribe_quotes(instrument.id)

            def on_quote(self, quote):
                self.count += 1
                if self.count in (1, 2):
                    self.submit_order(
                        self.order_factory.market(
                            instrument_id=instrument.id,
                            order_side=OrderSide.BUY if self.count == 1 else OrderSide.SELL,
                            quantity=instrument.make_qty(1),
                        )
                    )

        usd = Currency.from_str("USD")
        engine = BacktestEngine(config=BacktestEngineConfig(logging=LoggerConfig(stdout_level=LogLevel.ERROR)))
        try:
            engine.add_venue(
                venue=Venue("SIM"),
                oms_type=OmsType.NETTING,
                account_type=AccountType.MARGIN,
                base_currency=usd,
                starting_balances=[Money(1000, usd)],
                default_leverage=Decimal(1),
                fee_model=PerContractFeeModel(Money(2.6, usd)),
            )
            engine.add_instrument(instrument)
            start = datetime(2026, 1, 5, 10, tzinfo=eastern())
            ticks = []
            for index, value in enumerate([offset - 0.1, offset + 0.1]):
                stamp = timestamp_ns(start + timedelta(minutes=index))
                ticks.append(
                    QuoteTick(
                        instrument_id=instrument.id,
                        bid_price=Price.from_str(f"{value:.2f}"),
                        ask_price=Price.from_str(f"{value:.2f}"),
                        bid_size=Quantity.from_int(10),
                        ask_size=Quantity.from_int(10),
                        ts_event=stamp,
                        ts_init=stamp,
                    )
                )
            engine.add_data(ticks)
            engine.add_strategy(RoundTrip())
            engine.run()
            positions = engine.cache.positions()
            self.assertEqual(len(positions), 1)
            self.assertTrue(positions[0].is_closed)
            self.assertAlmostEqual(float(positions[0].realized_pnl), 14.8, places=2)
            account = engine.generate_account_report(venue=Venue("SIM"))
            self.assertAlmostEqual(float(account.iloc[-1]["total"]), 1014.8, places=2)
        finally:
            engine.dispose()


if __name__ == "__main__":
    unittest.main()
