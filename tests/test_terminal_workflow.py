"""Consequential UI boundaries: wrong context, stale data and altered startup intent."""

import unittest
from datetime import UTC, datetime, timedelta
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import Mock, patch

from qwork.core.config import Settings
from qwork.ui.terminal.app import create_terminal
from qwork.ui.terminal.backend import SnapshotCollector, TerminalBackend
from qwork.ui.terminal.workflow import account_readiness, chain_frame, permissions, workspace_chart


class WorkflowTests(unittest.TestCase):
    def setUp(self):
        self.temp = TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.backend = TerminalBackend(Settings(Path(self.temp.name)))
        self.addCleanup(self.backend.close)
        self.payload = ("DU123", "paper", 10000, 3, "passive", "SPY", 500, 1000)
        self.account = dict(
            account_id="IB-DU123",
            account_timestamp=datetime.now(UTC).isoformat(),
            base_currency="USD",
            is_reported=True,
            account=[dict(field="NetLiquidation", value="20000"), dict(field="AvailableFunds", value="15000")],
        )
        self.backend.monitor = Mock(account_id="DU123")
        self.backend.monitor.snapshot.side_effect = lambda: dict(self.account)
        self.backend.monitor.thread.is_alive.return_value = True

    def test_start_rejects_parameter_change_and_cannot_reuse_consumed_review(self):
        review = self.backend.review_strategy(*self.payload)
        with patch("subprocess.Popen") as process:
            changed = (*self.payload[:2], 9000, *self.payload[3:])
            with self.assertRaisesRegex(ValueError, "审阅"):
                self.backend.start_strategy(*changed, review_id=review["review_id"])
            process.assert_not_called()
            process.return_value.poll.return_value = 0
            self.backend.start_strategy(*self.payload, review_id=review["review_id"])
            self.assertEqual(process.call_count, 1)
            with self.assertRaisesRegex(ValueError, "审阅"):
                self.backend.start_strategy(*self.payload, review_id=review["review_id"])
            self.assertEqual(process.call_count, 1)
            self.assertEqual(self.backend.strategy_context["config"]["margin"], 10000)

    def test_expired_review_never_spawns_process(self):
        review = self.backend.review_strategy(*self.payload)
        self.backend.strategy_reviews[review["review_id"]]["expires_at"] = (
            datetime.now(UTC) - timedelta(seconds=1)
        ).isoformat()
        with patch("subprocess.Popen") as process:
            with self.assertRaisesRegex(ValueError, "审阅"):
                self.backend.start_strategy(*self.payload, review_id=review["review_id"])
            process.assert_not_called()

    def test_account_must_be_rechecked_after_review(self):
        review = self.backend.review_strategy(*self.payload)
        self.account["account_timestamp"] = (datetime.now(UTC) - timedelta(minutes=10)).isoformat()
        with patch("subprocess.Popen") as process:
            with self.assertRaisesRegex(ValueError, "过期"):
                self.backend.start_strategy(*self.payload, review_id=review["review_id"])
            process.assert_not_called()

    def test_operator_stop_prevents_new_start_review(self):
        path = Path(self.temp.name) / "private/trading/paper/DU123/STOP"
        path.parent.mkdir(parents=True)
        path.write_text("operator stop", encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "持久停止"):
            self.backend.review_strategy(*self.payload)

    def test_old_layout_preserves_docking_and_adds_missing_analysis_panels(self):
        from qwork.ui.terminal.panels import restore_model

        model = dict(
            global_=None,
            layout=dict(
                type="row",
                children=[
                    dict(type="tabset", children=[dict(type="tab", id="market", name="市场报价")]),
                    dict(
                        type="row", children=[dict(type="tabset", children=[dict(type="tab", id="chart", name="K 线")])]
                    ),
                ],
            ),
            borders=[],
            **{"global": {}},
        )
        restored = restore_model(model, "research")
        self.assertEqual(restored["layout"]["children"][0], model["layout"]["children"][0])
        self.assertFalse(restored["global"]["tabEnableRenderOnDemand"])
        ids = {node["id"] for node in restored["layout"]["children"][1]["children"][0]["children"]}
        self.assertTrue({"analysis", "term", "navigator"} <= ids)
        self.assertNotIn("tabEnableRenderOnDemand", model["global"])

    def test_cash_or_unreported_account_cannot_arm_trading(self):
        for change in [
            dict(account=[dict(field="TotalCashValue", value=100000)]),
            dict(base_currency="EUR"),
            dict(is_reported=False),
            dict(account_id="IB-DU999"),
        ]:
            with self.subTest(change=change):
                self.assertIsNotNone(account_readiness(dict(self.account, **change), "DU123"))
        self.assertIsNone(account_readiness(self.account, "DU123"))

    def test_review_is_bounded_and_does_not_start_trading(self):
        with patch("subprocess.Popen") as process:
            for _ in range(70):
                self.backend.review_strategy(*self.payload)
            self.assertEqual(len(self.backend.strategy_reviews), 64)
            process.assert_not_called()

    def test_step_requires_pause_and_stop_is_an_explicit_pending_state(self):
        self.backend.controller = Mock(paused=False)
        self.backend.backtest_thread = Mock()
        self.backend.backtest_thread.is_alive.return_value = True
        with self.assertRaisesRegex(ValueError, "暂停"):
            self.backend.control_replay("step", 1)
        self.backend.controller.configure.assert_not_called()
        self.backend.control_replay("stop", 1)
        self.assertEqual(self.backend.replay["status"], "stopping")
        self.assertTrue(self.backend.replay["stop_requested"])
        self.backend.backtest_thread = None

    def test_unknown_checkpoint_is_not_presented_as_flat_or_healthy(self):
        self.backend.strategy_process = Mock()
        self.backend.strategy_process.poll.return_value = None
        self.backend.strategy_context = dict(
            account="DU123", mode="paper", symbol="SPY", output=str(Path(self.temp.name) / "events")
        )
        path = Path(self.temp.name) / "private/trading/paper/DU123/SPY.sqlite3"
        path.parent.mkdir(parents=True)
        path.write_text("not a sqlite database", encoding="utf-8")
        view = self.backend.strategy_snapshot()
        self.assertEqual(view["phase"], "unknown")
        self.assertFalse(view["state_known"])
        self.assertIsNone(view["remaining_units"])
        self.assertIn("不能据此判断无持仓", view["state_error"])

    def test_replay_and_trading_cannot_display_historical_market_chain(self):
        snapshot = dict(
            frame=dict(mode="historical", points=[{"strike": 1}]), replay=dict(frame=dict(points=[{"strike": 2}]))
        )
        self.assertEqual(chain_frame(snapshot, "research")["points"][0]["strike"], 1)
        self.assertEqual(chain_frame(snapshot, "replay")["points"][0]["strike"], 2)
        self.assertEqual(chain_frame(snapshot, "paper"), {})
        self.assertEqual(chain_frame(snapshot, "live"), {})

    def test_mismatched_replay_chart_is_hidden(self):
        snapshot = dict(
            chart=dict(day="2026-10-01", contract=dict(symbol="SPY"), rows=[{"high": 999}]),
            replay=dict(day="2026-10-02", config=dict(symbol="SPY")),
        )
        self.assertEqual(workspace_chart(snapshot, "replay")["rows"], [])
        self.assertEqual(workspace_chart(snapshot, "live")["rows"], [])
        self.assertTrue(workspace_chart(snapshot, "research")["rows"])

    def test_permissions_only_allow_step_when_paused_and_review_in_matching_context(self):
        snapshot = dict(broker=self.account, replay=dict(status="running", paused=False), strategy={})
        access = permissions(snapshot, "replay", self.payload)
        self.assertTrue(access["review"])
        self.assertTrue(access["step"])
        self.assertFalse(access["pause"])
        snapshot["replay"]["paused"] = True
        self.assertFalse(permissions(snapshot, "replay", self.payload)["step"])
        snapshot["replay"]["status"] = "idle"
        self.assertFalse(permissions(snapshot, "paper", self.payload)["review"])
        self.assertTrue(permissions(snapshot, "live", self.payload)["review"])

    def test_ui_backtest_inputs_do_not_reference_ea_parameters(self):
        app = create_terminal(self.backend)
        callback = app.callback_map["t-backtest-action.children"]
        state_ids = {item["id"] for item in callback["state"]}
        self.assertTrue({"t-bt-margin", "t-bt-symbol", "t-bt-execution", "t-bt-positions"} <= state_ids)
        self.assertFalse({"t-margin", "t-ea-symbol", "t-execution", "t-max-positions"} & state_ids)

    def test_snapshot_collector_cannot_relabel_cached_data_as_live(self):
        from qwork.core.state import VolatilityRequest

        collector = SnapshotCollector(dict(symbol="SPY", observed_at="2026-10-02T09:45:00-04:00", points=[]))
        with self.assertRaises(ValueError):
            collector.collect(VolatilityRequest(symbol="SPY", mode="live"), 2)
        frame = collector.collect(VolatilityRequest(symbol="SPY", mode="historical", as_of="2026-10-02"), 2)
        self.assertEqual(frame["generation"], 2)

    def test_candidate_from_old_snapshot_is_rejected_without_actor_start(self):
        with patch("qwork.ui.terminal.backend.VolatilityRuntime") as runtime:
            with self.assertRaisesRegex(ValueError, "行情已变化"):
                self.backend.analyze_candidate("obsolete", 1)
            runtime.assert_not_called()

    def test_modal_parameter_changes_disable_confirmation(self):
        app = create_terminal(self.backend)
        poll = app.callback_map["t-state.data"]["callback"].__wrapped__
        modal = next(
            item["callback"].__wrapped__ for key, item in app.callback_map.items() if "t-review-modal.style" in key
        )
        review = self.backend.review_strategy(*self.payload)
        marker = poll(0)
        result = modal(review, *self.payload, "paper", [], marker)
        self.assertFalse(result[2])
        changed = (*self.payload[:2], 9000, *self.payload[3:])
        self.assertTrue(modal(review, *changed, "paper", [], marker)[2])
