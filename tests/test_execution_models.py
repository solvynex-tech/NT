"""Native execution regressions with explicitly mathematical quote fixtures."""

import unittest
from datetime import datetime
from types import SimpleNamespace
from unittest.mock import patch

from nautilus_trader.backtest import BacktestEngine, BacktestEngineConfig
from nautilus_trader.common import LoggerConfig, LogLevel
from nautilus_trader.execution import PerContractFeeModel
from nautilus_trader.model import AccountType, BookType, Currency, Money, OmsType, Price, Quantity, QuoteTick, Venue

from qwork.backtesting.execution import ReplayExecutionConfig, allocated_sizes, finite_book, leg_demands
from qwork.core.domain import eastern, timestamp_ns
from qwork.strategies.broken_wing.instruments import contract_keys, sim_option, sim_spread
from qwork.strategies.broken_wing.lifecycle import OrderManagementConfig
from qwork.strategies.broken_wing.models import BrokenWingConfig
from qwork.strategies.broken_wing.strategy import BrokenWingStrategy


class ExecutionTests(unittest.TestCase):
    def test_proxy_books_cannot_duplicate_shared_leg_capacity(self):
        candidates = [
            dict(expiration="2026-10-16", right="put", strikes=strikes) for strikes in [[95, 100, 102], [100, 102, 105]]
        ]
        demands = leg_demands(candidates)
        quotes = [SimpleNamespace(bid_size=20, ask_size=20) for _ in range(3)]
        sizes = [allocated_sizes(candidate, quotes, demands) for candidate in candidates]
        self.assertLessEqual(2 * sizes[0][1] + sizes[1][1], 20)
        self.assertLessEqual(sizes[0][1] + 2 * sizes[1][1], 20)

    def replay(
        self,
        observations,
        mode="passive",
        max_units=1,
        attempts=1,
        shared_orders_only=False,
        state_store=None,
        controlled=False,
    ):
        candidate = dict(
            symbol="SPY",
            expiration="2026-10-16",
            right="put",
            strikes=[95, 100, 102],
            legs=[dict(symbol="SPY")],
            max_loss=100,
            key="holding",
        )
        spread = sim_spread(candidate, "2026-10-02", 0)
        offset = spread.info["accounting_price_offset"]
        options = {k: sim_option("SPY", k, "2026-10-02") for k in contract_keys([candidate])}
        execution = ReplayExecutionConfig(mode=mode, latency_ms=0, max_entry_attempts=attempts)
        strategy = BrokenWingStrategy(
            BrokenWingConfig(margin_budget=1000, max_positions=1, max_units=max_units),
            [candidate],
            {"holding": spread},
            {k: v.id for k, v in options.items()},
            execution=None if shared_orders_only else execution,
            orders=OrderManagementConfig(mode=mode, max_entry_attempts=attempts),
            state_store=state_store,
        )

        def quote(instrument_id, stamp, bid, ask, bid_size, ask_size):
            return QuoteTick(
                instrument_id=instrument_id,
                bid_price=Price.from_str(f"{bid:.2f}"),
                ask_price=Price.from_str(f"{ask:.2f}"),
                bid_size=Quantity.from_int(bid_size),
                ask_size=Quantity.from_int(ask_size),
                ts_event=stamp,
                ts_init=stamp,
            )

        data = []
        for at, bid, ask, bid_size, ask_size in observations:
            stamp = timestamp_ns(datetime.fromisoformat(f"2026-10-02T{at}").replace(tzinfo=eastern()))
            for option in options.values():
                leg_quote = quote(option.id, stamp, 1, 1.1, 10, 10)
                data.extend([finite_book(leg_quote), leg_quote])
            combo = quote(spread.id, stamp, offset + bid, offset + ask, bid_size, ask_size)
            data.extend([finite_book(combo), combo])
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
        engine = BacktestEngine(config=BacktestEngineConfig(logging=LoggerConfig(stdout_level=LogLevel.ERROR)))
        usd = Currency.from_str("USD")
        try:
            engine.add_venue(
                venue=Venue("SIM"),
                oms_type=OmsType.NETTING,
                account_type=AccountType.MARGIN,
                base_currency=usd,
                starting_balances=[Money(1000, usd)],
                book_type=BookType.L2_MBP,
                fill_model=execution.fill_model(),
                latency_model=execution.latency_model(),
                liquidity_consumption=True,
                fee_model=PerContractFeeModel(Money(2.6, usd)),
            )
            for item in [*options.values(), spread]:
                engine.add_instrument(item)
            if not controlled:
                engine.add_data(data)
            engine.add_strategy(strategy)
            with patch("qwork.strategies.broken_wing.strategy.tick_metrics", return_value=metrics):
                if controlled:
                    from qwork.backtesting.playback import ReplayController, run_controlled

                    run_controlled(engine, data, ReplayController(speed=0))
                else:
                    engine.run()
            return strategy, engine.generate_orders_report(), engine.generate_fills_report(), engine.cache.positions()
        finally:
            engine.dispose()

    def test_midpoint_order_needs_later_crossing_quote(self):
        _, _, fills, positions = self.replay(
            [("09:45:00", -0.1, 0.1, 10, 10), ("09:46:00", -0.2, -0.1, 10, 10), ("15:55:00", 0.2, 0.3, 10, 10)]
        )
        self.assertEqual(len(fills), 2)
        self.assertEqual(fills.iloc[0]["ts_event"].hour, 13)
        self.assertEqual(fills.iloc[0]["ts_event"].minute, 46)
        self.assertTrue(positions[0].is_closed)

    def test_native_streaming_has_same_fills_and_pnl_as_full_replay(self):
        observations = [("09:45:00", -0.1, 0.1, 10, 2), ("15:55:00", 0.2, 0.3, 1, 2), ("15:56:00", 0.1, 0.2, 1, 2)]
        normal = self.replay(observations, mode="natural", max_units=2)
        streaming = self.replay(observations, mode="natural", max_units=2, controlled=True)
        self.assertEqual(list(normal[1]["status"]), list(streaming[1]["status"]))
        self.assertEqual(list(normal[2]["last_qty"]), list(streaming[2]["last_qty"]))
        self.assertEqual(list(normal[2]["last_px"]), list(streaming[2]["last_px"]))
        self.assertEqual(str(normal[3][0].realized_pnl), str(streaming[3][0].realized_pnl))
        self.assertEqual(normal[0].completed, streaming[0].completed)

    def test_unfilled_order_times_out_and_retries_without_a_position(self):
        strategy, orders, fills, positions = self.replay(
            [
                ("09:45:00", -0.1, 0.1, 10, 10),
                ("09:48:00", -0.1, 0.1, 10, 10),
                ("09:51:00", -0.1, 0.1, 10, 10),
                ("15:55:00", -0.1, 0.1, 10, 10),
            ],
            attempts=2,
        )
        self.assertEqual(strategy.entry_attempts, 2)
        self.assertTrue(all(str(s) == "CANCELED" for s in orders["status"]))
        self.assertEqual(len(fills), 0)
        self.assertEqual(positions, [])

    def test_partial_entry_cancels_remainder_and_closes_actual_units(self):
        strategy, orders, fills, positions = self.replay(
            [
                ("09:45:00", -0.1, 0.1, 10, 2),
                ("09:46:00", -0.2, -0.1, 10, 1),
                ("09:47:00", -0.1, 0.1, 10, 2),
                ("15:55:00", 0.2, 0.3, 10, 2),
            ],
            max_units=2,
        )
        self.assertEqual(int(orders.iloc[0]["filled_qty"]), 1)
        self.assertEqual(str(orders.iloc[0]["status"]), "CANCELED")
        self.assertEqual(len(fills), 2)
        self.assertEqual(int(positions[0].peak_qty), 1)
        self.assertTrue(positions[0].is_closed)
        self.assertEqual(strategy.positions["holding"]["units"], 0)

    def test_market_close_does_not_invent_extra_depth(self):
        _, _, fills, positions = self.replay(
            [("09:45:00", -0.1, 0.1, 10, 2), ("15:55:00", 0.2, 0.3, 1, 2), ("15:56:00", 0.1, 0.2, 1, 2)],
            mode="natural",
            max_units=2,
        )
        sells = fills[fills["order_side"].astype(str) == "SELL"]
        self.assertEqual(len(sells), 2)
        self.assertEqual([int(q) for q in sells["last_qty"]], [1, 1])
        self.assertEqual(sells.iloc[-1]["ts_event"].minute, 56)
        self.assertTrue(positions[0].is_closed)

    def test_shared_order_policy_times_out_without_replay_execution_config(self):
        strategy, orders, fills, _ = self.replay(
            [("09:45:00", -0.1, 0.1, 10, 10), ("09:48:00", -0.1, 0.1, 10, 10), ("15:55:00", -0.1, 0.1, 10, 10)],
            shared_orders_only=True,
        )
        self.assertIsNone(strategy.execution)
        self.assertEqual(len(fills), 0)
        self.assertEqual(str(orders.iloc[0]["status"]), "CANCELED")

    def test_native_callbacks_commit_combination_state(self):
        from pathlib import Path
        from tempfile import TemporaryDirectory

        from qwork.strategies.broken_wing.recovery import CheckpointStore

        with TemporaryDirectory() as directory:
            store = CheckpointStore(Path(directory) / "state.sqlite3", "SIM", "SPY")
            strategy, _, _, positions = self.replay(
                [("09:45:00", -0.1, 0.1, 10, 10), ("09:46:00", -0.2, -0.1, 10, 10), ("15:55:00", 0.2, 0.3, 10, 10)],
                state_store=store,
            )
            loaded = store.load()
            self.assertTrue(positions[0].is_closed)
            self.assertEqual(loaded.completed, {"holding"})
            self.assertEqual(loaded.positions["holding"]["units"], 0)
            self.assertEqual(loaded.pending, {})
            self.assertIn("intent", strategy.on_save())
