"""Recovery/fault fixtures; no connection to a broker and no live orders."""

import sqlite3
import unittest
from datetime import datetime
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest.mock import Mock

from qwork.core.domain import eastern, timestamp_ns
from qwork.strategies.broken_wing.lifecycle import OrderManagementConfig
from qwork.strategies.broken_wing.models import BrokenWingConfig
from qwork.strategies.broken_wing.recovery import CheckpointStore, StrategySnapshot, account_lease, reconcile_intent
from qwork.strategies.broken_wing.risk import AccountRiskConfig, AccountRiskGuard
from qwork.strategies.broken_wing.strategy import BrokenWingStrategy


class RecoveryTests(unittest.TestCase):
    def test_recovery_rejects_wrong_instrument_side_and_fractional_bag(self):
        args = self.fixture()
        order = args[1]["buy"]
        order.instrument_id = "foreign"
        order.side = "BUY"
        _, failures = reconcile_intent(*args, spread_ids={"holding": "BAG"})
        self.assertTrue(any("instrument or side mismatch" in f for f in failures))
        order.instrument_id = "BAG"
        order.side = "SELL"
        _, failures = reconcile_intent(*args, spread_ids={"holding": "BAG"})
        self.assertTrue(failures)
        order.side = "BUY"
        order.filled_qty = 0.5
        _, failures = reconcile_intent(*args, spread_ids={"holding": "BAG"})
        self.assertTrue(any("Fractional BAG" in f for f in failures))

    def test_late_fill_after_cancel_blocks_new_orders_without_guessing_inventory(self):
        context = SimpleNamespace(
            expected_account="DU1",
            recovery_ready=True,
            halted=False,
            pending={},
            order_registry={"buy": ("holding", "open")},
            positions={"holding": {"units": 0}},
            cache=SimpleNamespace(order=lambda _: SimpleNamespace(filled_qty=1)),
            record=Mock(),
        )
        BrokenWingStrategy.on_order_event(context, SimpleNamespace(client_order_id="buy"))
        self.assertTrue(context.halted)
        self.assertFalse(context.recovery_ready)
        self.assertEqual(context.positions["holding"]["units"], 0)
        context.record.assert_called_once()

    def test_cancel_timeout_does_not_release_reserved_inventory(self):
        now = timestamp_ns(datetime(2026, 10, 5, 10, tzinfo=eastern()))
        context = SimpleNamespace(
            expected_account=None,
            execution=None,
            clock=SimpleNamespace(timestamp_ns=lambda: now),
            settings=BrokenWingConfig(),
            order_management=OrderManagementConfig(cancel_ack_timeout_seconds=30),
            pending={"order": ("holding", "open")},
            positions={
                "holding": dict(
                    cancel_requested=True, cancel_requested_ns=now - 31_000_000_000, margin_reserved=200, units=1
                )
            },
            record=Mock(),
            halted=False,
        )
        BrokenWingStrategy.manage_pending_entries(context)
        BrokenWingStrategy.manage_pending_entries(context)
        self.assertTrue(context.halted)
        self.assertEqual(context.positions["holding"]["margin_reserved"], 200)
        self.assertEqual(context.positions["holding"]["units"], 1)
        self.assertEqual(context.record.call_count, 1)

    def fixture(self):
        candidate = dict(key="holding", expiration="2026-10-16", right="put", strikes=[95, 100, 102])
        state = StrategySnapshot(
            account="DU_TEST",
            symbol="SPY",
            session_date="2026-10-05",
            candidates=[candidate],
            positions={"holding": dict(candidate=candidate, units=1, state="open")},
            order_registry={"buy": ("holding", "open")},
            used={"holding"},
        )
        order = SimpleNamespace(strategy_id="BWB", filled_qty=1, is_closed=True)
        legs = {(candidate["expiration"], k, "put"): str(k) for k in candidate["strikes"]}
        return state, {"buy": order}, {"95": 1, "100": -2, "102": 1}, legs, "BWB", "2026-10-05"

    def test_committed_checkpoint_survives_new_store(self):
        with TemporaryDirectory() as directory:
            path = Path(directory) / "state.sqlite3"
            original = self.fixture()[0]
            CheckpointStore(path, "DU_TEST", "SPY").save(original)
            loaded = CheckpointStore(path, "DU_TEST", "SPY").load()
            self.assertEqual(loaded.model_dump(), original.model_dump())
            with self.assertRaises(ValueError):
                CheckpointStore(path, "OTHER", "SPY").load()

    def test_uncommitted_update_does_not_replace_checkpoint(self):
        with TemporaryDirectory() as directory:
            store = CheckpointStore(Path(directory) / "state.sqlite3", "DU_TEST", "SPY")
            store.save(self.fixture()[0])
            connection = sqlite3.connect(store.path)
            connection.execute("UPDATE snapshot SET payload='corrupt' WHERE id=1")
            connection.rollback()
            connection.close()
            self.assertEqual(store.load().used, {"holding"})

    def test_account_lease_prevents_duplicate_process_and_releases(self):
        with TemporaryDirectory() as directory:
            path = Path(directory) / "lock.sqlite3"
            with account_lease(path):
                with self.assertRaises(ValueError):
                    with account_lease(path):
                        self.fail("Second process acquired account lease")
            with account_lease(path):
                pass

    def test_missing_native_evidence_blocks_restoration(self):
        args = self.fixture()
        args[1].clear()
        self.assertTrue(reconcile_intent(*args)[1])

    def test_late_partial_fill_is_recovered_from_native_order(self):
        args = self.fixture()
        args[1]["buy"].filled_qty = 2
        args[2].update({"95": 2, "100": -4, "102": 2})
        recovered, failures = reconcile_intent(*args)
        self.assertEqual(failures, [])
        self.assertEqual(recovered.positions["holding"]["units"], 2)
        self.assertEqual(args[0].positions["holding"]["units"], 1)

    def test_pending_close_fill_is_not_counted_from_old_intent(self):
        args = self.fixture()
        args[0].order_registry["sell"] = ("holding", "close")
        args[0].pending["sell"] = ("holding", "close")
        args[1]["sell"] = SimpleNamespace(strategy_id="BWB", filled_qty=1, is_closed=True)
        args[2].clear()
        recovered, failures = reconcile_intent(*args)
        self.assertEqual(failures, [])
        self.assertEqual(recovered.positions["holding"]["units"], 0)

    def test_unknown_order_and_fractional_external_position_block(self):
        args = self.fixture()
        args[1]["foreign"] = SimpleNamespace(strategy_id="EXTERNAL", filled_qty=0, is_closed=False)
        args[2]["SPY-STOCK"] = 0.5
        failures = reconcile_intent(*args)[1]
        self.assertTrue(any("Unowned working order" in reason for reason in failures))
        self.assertTrue(any("SPY-STOCK" in reason for reason in failures))

    def test_previous_session_inventory_blocks_day_rotation(self):
        args = list(self.fixture())
        args[-1] = "2026-10-06"
        self.assertTrue(reconcile_intent(*args)[1])


class AccountRiskTests(unittest.TestCase):
    def event(self, equity=10000, available=2000, stamp=1_000_000_000):
        return SimpleNamespace(
            base_currency="USD",
            is_reported=True,
            ts_event=stamp,
            info={"NetLiquidation": str(equity), "AvailableFunds": str(available)},
        )

    def test_cash_balance_is_not_used_as_equity_fallback(self):
        guard = AccountRiskGuard(AccountRiskConfig())
        event = self.event()
        event.info = {"TotalCashValue": "10000"}
        self.assertEqual(guard.check(event, event.ts_event, "2026-10-05"), "reported_equity_or_available_funds_missing")
        self.assertIsNone(guard.available_funds)

    def test_stale_account_blocks_entries_without_resetting_baseline(self):
        guard = AccountRiskGuard(AccountRiskConfig(account_max_age_seconds=5))
        guard.check(self.event(), 1_000_000_000, "2026-10-05")
        self.assertEqual(guard.check(self.event(), 10_000_000_000, "2026-10-05"), "account_data_unavailable_or_stale")
        self.assertEqual(guard.state["start_equity"], 10000)

    def test_loss_stop_is_latched_across_restart(self):
        config = AccountRiskConfig(daily_loss_limit_usd=500)
        guard = AccountRiskGuard(config)
        guard.check(self.event(), 1_000_000_000, "2026-10-05")
        self.assertEqual(guard.check(self.event(9500), 1_000_000_000, "2026-10-05"), "daily_account_loss_limit")
        restarted = AccountRiskGuard(config, guard.state)
        self.assertEqual(restarted.check(self.event(12000), 1_000_000_000, "2026-10-05"), "daily_account_loss_limit")

    def test_peak_drawdown_and_funds_buffer(self):
        guard = AccountRiskGuard(AccountRiskConfig(daily_loss_limit_usd=5000, max_drawdown_usd=1000))
        self.assertIsNone(guard.check(self.event(), 1_000_000_000, "2026-10-05"))
        self.assertEqual(guard.available_funds, 1900)
        guard.check(self.event(12000), 1_000_000_000, "2026-10-05")
        self.assertEqual(guard.check(self.event(11000), 1_000_000_000, "2026-10-05"), "account_drawdown_limit")

    def test_repeated_rejections_stop_new_entries(self):
        guard = AccountRiskGuard(AccountRiskConfig(max_entry_rejections=2))
        self.assertIsNone(guard.reject())
        self.assertEqual(guard.reject(), "entry_rejection_limit")
