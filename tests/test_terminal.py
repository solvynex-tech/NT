"""Terminal isolation, historical time boundaries and native playback controls."""

import json
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Event, Thread
from types import SimpleNamespace
from unittest.mock import Mock, patch

from qwork.backtesting.playback import ReplayController, run_controlled
from qwork.core.config import Settings
from qwork.ui.terminal.app import candle_figure, create_terminal
from qwork.ui.terminal.backend import TerminalBackend, display_rows


class PlaybackTests(unittest.TestCase):
    def test_pause_step_and_stop_preserve_native_timestamp_batches(self):
        controller = ReplayController(speed=0)
        controller.configure(paused=True)
        first, second, third = Event(), Event(), Event()
        observed = []
        done = []
        engine = Mock()
        engine.run.side_effect = lambda **kwargs: first.set()

        def observe(_, stamp):
            observed.append(stamp)
            (second if len(observed) == 1 else third).set()

        controller.observer = observe
        data = [SimpleNamespace(ts_init=n) for n in [10, 10, 20, 30]]
        worker = Thread(target=lambda: done.append(run_controlled(engine, data, controller)))
        worker.start()
        try:
            self.assertFalse(first.wait(0.05), "Paused playback must not deliver data")
            controller.configure(step=True)
            self.assertTrue(second.wait(2))
            self.assertFalse(third.wait(0.05), "Single step must consume exactly one timestamp group")
            self.assertEqual(observed, [10])
            self.assertEqual(len(engine.add_data.call_args.args[0]), 2)
            controller.configure(stop=True)
            worker.join(2)
            self.assertFalse(worker.is_alive())
            self.assertEqual(done, [False])
            engine.end.assert_called_once()
        finally:
            controller.configure(stop=True)
            worker.join(2)

    def test_resuming_at_maximum_speed_preserves_all_groups(self):
        controller = ReplayController(speed=60)
        controller.configure(paused=True)
        stamps = []
        controller.observer = lambda _, stamp: stamps.append(stamp)
        controller.configure(speed=0, paused=False)
        engine = Mock()
        self.assertTrue(run_controlled(engine, [SimpleNamespace(ts_init=n) for n in [1, 2, 2, 3]], controller))
        self.assertEqual(stamps, [1, 2, 3])
        self.assertEqual(engine.run.call_count, 3)
        self.assertEqual(engine.clear_data.call_count, 3)
        with self.assertRaises(ValueError):
            ReplayController(speed=float("nan"))


class TerminalTests(unittest.TestCase):
    def test_browser_poll_sends_small_revision_and_preserves_each_snapshot(self):
        app = create_terminal(self.backend)
        poll = app.callback_map["t-state.data"]["callback"].__wrapped__
        render = next(
            value["callback"].__wrapped__ for key, value in app.callback_map.items() if "t-chart-title.children" in key
        )
        self.backend.chart["status"] = "first snapshot"
        first = poll(0)
        self.backend.chart["status"] = "second snapshot"
        second = poll(1)
        self.assertLess(len(json.dumps(first)), 64)
        self.assertNotEqual(first, second)
        self.assertEqual(render(first, "heatmap", "put", None)[5], "first snapshot")
        self.assertEqual(render(second, "heatmap", "put", None)[5], "second snapshot")
        client = app.server.test_client()
        self.assertEqual(client.get("/api/snapshot").json["chart"]["status"], "second snapshot")
        self.assertEqual(client.get("/api/snapshot", headers={"Origin": "https://untrusted.invalid"}).status_code, 403)

    def test_csv_exports_combo_premium_and_preserves_native_fill(self):
        self.backend.replay.update(
            fills=[dict(instrument_id="BWB-001.SIM", last_px="14.94", last_qty="10")],
            combo_contracts={"BWB-001.SIM": dict(label="SPY PUT 752/756/758", accounting_price_offset=15)},
        )
        client = create_terminal(self.backend).server.test_client()
        response = client.get("/api/replay/fills.csv")
        self.assertEqual(response.status_code, 200)
        self.assertIn("attachment", response.headers["Content-Disposition"])
        text = response.data.decode("utf-8-sig")
        self.assertIn("combo_premium", text)
        self.assertIn("-0.06", text)
        self.assertIn("14.94", text)
        self.assertEqual(
            client.get("/api/replay/fills.csv", headers={"Origin": "https://untrusted.invalid"}).status_code, 403
        )
        self.backend.replay["fills"] = []
        self.assertEqual(client.get("/api/replay/fills.csv").status_code, 409)

    def test_no_candidate_archive_remains_viewable_without_fake_orders(self):
        path = self.backend.reports_dir / "SPY-empty"
        path.mkdir(parents=True)
        (path / "report.json").write_text(
            json.dumps(dict(date="2026-10-02", status="no_candidates", net_pnl=0)), encoding="utf-8"
        )
        self.backend.load_report("SPY-empty")
        state = self.backend.snapshot()["replay"]
        self.assertEqual(state["report"]["status"], "no_candidates")
        self.assertEqual(state["orders"], [])
        self.assertEqual(state["account"], [])

    def test_display_only_removes_proxy_offset_and_preserves_raw_records(self):
        source = [dict(instrument_id="BWB-001.SIM", price="14.94", quantity="10")]
        shown = display_rows(
            source, {"BWB-001.SIM": dict(label="SPY PUT 752/756/758", accounting_price_offset=15)}, ["price"]
        )
        self.assertEqual(shown[0]["price"], -0.06)
        self.assertEqual(source[0]["price"], "14.94")
        self.assertEqual(shown[0]["quantity"], "10")

    def test_archived_account_and_cash_curve_are_explicit(self):
        path = self.backend.reports_dir / "SPY-result"
        path.mkdir(parents=True)
        (path / "report.json").write_text(
            json.dumps(dict(date="2026-10-02", status="completed", net_pnl=-50, audit_passed=True)), encoding="utf-8"
        )
        for name in ["orders", "positions", "fills"]:
            (path / f"{name}.json").write_text("[]", encoding="utf-8")
        (path / "account.json").write_text(
            json.dumps([dict(index="2026-10-02T13:45:00+00:00", currency="USD", total="950", locked="0", free="950")]),
            encoding="utf-8",
        )
        self.backend.load_report("SPY-result")
        view = self.backend.snapshot()["replay"]
        self.assertEqual(view["curve_kind"], "cash")
        self.assertEqual(view["curve"][0]["equity"], 950)
        self.assertEqual(view["account"][0]["field"], "USD total")

    def setUp(self):
        self.temp = TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.backend = TerminalBackend(Settings(Path(self.temp.name), "secret-never-in-browser"))
        self.addCleanup(self.backend.close)

    def test_empty_terminal_has_no_fake_account_or_automatic_connection(self):
        with patch("qwork.ui.terminal.backend.BrokerMonitor") as monitor, patch("subprocess.Popen") as process:
            app = create_terminal(self.backend)
            client = app.server.test_client()
            response = client.get("/_dash-layout")
            self.assertEqual(response.status_code, 200)
            self.assertNotIn("secret-never-in-browser", response.get_data(as_text=True))
            self.assertEqual(client.get("/_dash-dependencies").status_code, 200)
            self.assertEqual(client.get("/health", headers={"Origin": "https://untrusted.invalid"}).status_code, 403)
            self.assertEqual(client.get("/health", headers={"Host": "untrusted.invalid"}).status_code, 403)
            self.assertEqual(self.backend.snapshot()["broker"]["account"], [])
            monitor.assert_not_called()
            process.assert_not_called()

    def test_live_start_requires_explicit_account_confirmation(self):
        with patch("subprocess.Popen") as process:
            with self.assertRaisesRegex(ValueError, "勾选"):
                self.backend.start_strategy("U123", "live", 10000, 3, "passive", "SPY", 500, 1000)
            process.assert_not_called()

    def test_future_quotes_cannot_change_current_partial_candle(self):
        self.backend.chart = dict(
            status="SPY",
            rows=[dict(timestamp="2026-10-02T13:45:00+00:00", high=999)],
            day="2026-10-02",
            minutes=5,
            observations=[
                dict(timestamp="2026-10-02T13:45:00+00:00", mid=2),
                dict(timestamp="2026-10-02T13:46:00+00:00", mid=3),
                dict(timestamp="2026-10-02T13:47:00+00:00", mid=999),
            ],
        )
        self.backend.replay.update(status="running", day="2026-10-02", clock="2026-10-02T13:46:00+00:00")
        chart = self.backend.snapshot()["chart"]
        self.assertEqual(len(chart["rows"]), 1)
        self.assertEqual(chart["rows"][0]["high"], 3)
        self.assertEqual(chart["rows"][0]["close"], 3)
        self.assertNotIn("observations", chart)
        figure = candle_figure(chart)
        self.assertEqual(figure.data[0].x[0].hour, 9)
        self.backend.replay.update(clock=None)
        self.assertEqual(self.backend.snapshot()["chart"]["rows"], [])

    def test_archive_rejects_path_traversal(self):
        for name in ["..", "../private", "x/../../private", ""]:
            with self.assertRaises(ValueError):
                self.backend.load_report(name)

    def test_account_monitor_disconnect_does_not_terminate_strategy(self):
        self.backend.monitor = Mock()
        monitor = self.backend.monitor
        self.backend.strategy_process = Mock()
        process = self.backend.strategy_process
        self.backend.disconnect_broker()
        monitor.stop.assert_called_once()
        self.assertIsNone(self.backend.monitor)
        process.terminate.assert_not_called()

    def test_saved_reports_expose_actual_completion_and_audit_status(self):
        path = self.backend.reports_dir / "SPY-result"
        path.mkdir(parents=True)
        (path / "report.json").write_text(
            json.dumps(dict(date="2026-10-02", status="incomplete_liquidation", net_pnl=-50, audit_passed=False)),
            encoding="utf-8",
        )
        runs = self.backend.list_runs()
        self.assertFalse(runs[0]["audit_passed"])
        self.assertEqual(runs[0]["status"], "incomplete_liquidation")
