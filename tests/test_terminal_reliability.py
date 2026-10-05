"""Bounded work, stalled state readers and ownership-safe UI recovery."""

import json
import sqlite3
import time
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Event
from unittest.mock import Mock, patch

import psutil
from dash.exceptions import PreventUpdate

from qwork.core.config import Settings
from qwork.strategies.broken_wing.recovery import CheckpointStore
from qwork.ui.terminal.app import create_terminal
from qwork.ui.terminal.backend import TerminalBackend
from qwork.ui.terminal.panels import PANELS, dock_model, restore_model
from qwork.ui.terminal.reliability import LatestWorker, OwnedProcess, ProcessRegistry


class ReliabilityTests(unittest.TestCase):
    def setUp(self):
        self.temp = TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.data = Path(self.temp.name)

    def descriptor(self):
        config = dict(
            account="DU123",
            mode="paper",
            symbol="SPY",
            margin=10000.0,
            positions=3,
            execution="passive",
            loss=500.0,
            drawdown=1000.0,
        )
        output = self.data / "trading" / "owned"
        argv = [
            "python.exe",
            "-m",
            "qwork",
            "trade",
            "--mode",
            "paper",
            "--account-id",
            "DU123",
            "--symbol",
            "SPY",
            "--margin",
            "10000",
            "--max-positions",
            "3",
            "--execution",
            "passive",
            "--daily-loss-limit",
            "500",
            "--drawdown-limit",
            "1000",
            "--output",
            str(output),
        ]
        return dict(
            version=1,
            id="owned",
            argv=argv,
            context=dict(account="DU123", mode="paper", symbol="SPY", config=config, output=str(output)),
            members=[dict(pid=123, created=100.0)],
        )

    def test_rapid_requests_replace_pending_job_instead_of_growing_queue(self):
        worker = LatestWorker("test-bounded")
        release, started, done = Event(), Event(), Event()
        called = []
        self.addCleanup(worker.close)

        def first():
            started.set()
            release.wait(2)
            called.append(0)

        worker.submit(first)
        self.assertTrue(started.wait(2))
        for index in range(1, 101):

            def job(index=index):
                called.append(index)
                done.set()

            worker.submit(job)
        self.assertEqual(worker.status()["pending"], 1)
        release.set()
        self.assertTrue(done.wait(2))
        self.assertEqual(called, [0, 100])

    def test_closing_worker_discards_pending_job_and_rejects_new_jobs(self):
        worker = LatestWorker("test-close")
        release, started = Event(), Event()
        called = []
        worker.submit(lambda: (started.set(), release.wait(2)))
        self.assertTrue(started.wait(2))
        worker.submit(lambda: called.append("queued"))
        worker.close(timeout=0)
        release.set()
        worker.thread.join(2)
        self.assertFalse(worker.thread.is_alive())
        self.assertEqual(called, [])
        with self.assertRaises(ValueError):
            worker.submit(lambda: None)

    def test_job_exception_does_not_kill_the_worker(self):
        worker = LatestWorker("test-errors")
        self.addCleanup(worker.close)
        first, second = Event(), Event()

        def fail():
            first.set()
            raise ValueError("not logged")

        worker.submit(fail)
        self.assertTrue(first.wait(2))
        worker.submit(second.set)
        self.assertTrue(second.wait(2))
        self.assertEqual(worker.last_error, "ValueError")

    def test_expired_snapshot_keeps_existing_ui_instead_of_publishing_empty_accounts(self):
        backend = TerminalBackend(Settings(self.data))
        self.addCleanup(backend.close)
        app = create_terminal(backend)
        poll = app.callback_map["t-state.data"]["callback"].__wrapped__
        render = next(
            value["callback"].__wrapped__ for key, value in app.callback_map.items() if "t-chart-title.children" in key
        )
        first = poll(0)
        for index in range(40):
            poll(index + 1)
        with self.assertRaises(PreventUpdate):
            render(first, "heatmap", "put", None)

    def test_busy_sqlite_reader_fails_fast_without_waiting_for_writer(self):
        path = self.data / "state.sqlite3"
        connection = sqlite3.connect(path)
        self.addCleanup(connection.close)
        connection.execute("CREATE TABLE snapshot (id INTEGER, payload TEXT)")
        connection.commit()
        connection.execute("BEGIN EXCLUSIVE")
        start = time.monotonic()
        with self.assertRaises(sqlite3.OperationalError):
            CheckpointStore(path, "DU123", "SPY").load(timeout=0.05)
        self.assertLess(time.monotonic() - start, 0.5)

    def test_pid_reuse_and_changed_command_are_not_attached(self):
        descriptor = self.descriptor()
        process = Mock()
        process.create_time.return_value = 101.0
        process.cmdline.return_value = descriptor["argv"]
        with patch("qwork.ui.terminal.reliability.psutil.Process", return_value=process):
            self.assertIsNotNone(OwnedProcess(descriptor).poll())
            process.create_time.return_value = 100.0
            process.cmdline.return_value = ["python.exe", "other.py"]
            self.assertIsNotNone(OwnedProcess(descriptor).poll())
        process.terminate.assert_not_called()

    def test_unreadable_process_identity_is_unknown_instead_of_exited(self):
        descriptor = self.descriptor()
        process = OwnedProcess(descriptor)
        with patch("qwork.ui.terminal.reliability.psutil.Process", side_effect=psutil.AccessDenied(123)):
            self.assertIsNone(process.poll())
            self.assertTrue(process.uncertain)

    def test_registry_rejects_foreign_paths_and_changed_launch_parameters(self):
        registry = ProcessRegistry(self.data)
        descriptor = self.descriptor()
        self.assertEqual(registry.validate(descriptor), descriptor)
        descriptor["argv"][5] = "live"
        with self.assertRaises(ValueError):
            registry.validate(descriptor)
        descriptor = self.descriptor()
        descriptor["context"]["output"] = str(self.data.parent / "foreign")
        with self.assertRaises(ValueError):
            registry.validate(descriptor)

    def test_restart_restores_only_verified_process_and_never_launches_or_connects(self):
        registry = ProcessRegistry(self.data)
        registry.directory.mkdir(parents=True)
        descriptor = self.descriptor()
        (registry.directory / "paper-DU123.json").write_text(json.dumps(descriptor), encoding="utf-8")
        process = Mock()
        process.create_time.return_value = 100.0
        process.cmdline.return_value = descriptor["argv"]
        process.is_running.return_value = True
        process.status.return_value = psutil.STATUS_RUNNING
        with (
            patch("qwork.ui.terminal.reliability.psutil.Process", return_value=process),
            patch("subprocess.Popen") as spawn,
            patch("qwork.ui.terminal.backend.BrokerMonitor") as broker,
        ):
            backend = TerminalBackend(Settings(self.data))
            self.addCleanup(backend.close)
            self.assertTrue(backend.strategy_snapshot()["attached"])
            self.assertEqual(backend.strategy_context["account"], "DU123")
            self.assertTrue(backend.strategy_snapshot()["running"])
            spawn.assert_not_called()
            broker.assert_not_called()
        process.terminate.assert_not_called()

    def test_corrupted_restore_metadata_blocks_new_strategy_review(self):
        registry = ProcessRegistry(self.data)
        registry.directory.mkdir(parents=True)
        (registry.directory / "broken.json").write_text("invalid", encoding="utf-8")
        backend = TerminalBackend(Settings(self.data))
        self.addCleanup(backend.close)
        self.assertTrue(backend.strategy_snapshot()["recovery_required"])
        with self.assertRaisesRegex(ValueError, "恢复记录"):
            backend.review_strategy("DU123", "paper", 10000, 3, "passive", "SPY", 500, 1000)

    def test_each_workspace_has_all_panels_once_and_mt5_regions(self):
        for workspace in ["research", "replay", "paper", "live"]:
            model = dock_model(workspace)
            ids = []

            def visit(node, ids=ids):
                if node.get("type") == "tab":
                    ids.append(node["id"])
                for child in node.get("children", []):
                    visit(child)

            visit(model["layout"])
            for border in model["borders"]:
                visit(border)
            self.assertEqual(sorted(ids), sorted(spec.key for spec in PANELS))
            self.assertTrue(model["global"]["rootOrientationVertical"])
            upper, toolbox = model["layout"]["children"]
            self.assertEqual(upper["children"][0]["children"][0]["children"][0]["id"], "market")
            self.assertEqual(upper["children"][0]["children"][1]["children"][0]["id"], "navigator")
            self.assertEqual(
                toolbox["children"][toolbox["selected"]]["id"], "backtest" if workspace == "replay" else "positions"
            )
        self.assertNotEqual(dock_model("replay")["layout"], dock_model("research")["layout"])

    def test_corrupt_layout_falls_back_without_breaking_the_app(self):
        for malformed in [
            dict(layout=dict(children=[None])),
            dict(layout=dict(children="broken")),
            dict(layout={}, global_=None, borders=None),
            dict(layout={}, **{"global": None}),
        ]:
            self.assertEqual(restore_model(malformed, "replay"), dock_model("replay"))

    def test_close_rejects_new_analysis_broker_and_replay_jobs(self):
        backend = TerminalBackend(Settings(self.data))
        backend.close()
        with patch("qwork.ui.terminal.backend.BrokerMonitor") as broker:
            for action in [
                lambda: backend.connect_broker("DU123"),
                lambda: backend.refresh_analysis("SPY", "historical", "2026-10-02", "09:45:00"),
                lambda: backend.analyze_candidate("stale", 1),
                lambda: backend.start_backtest("SPY", "2026-10-02", 10000, 3, "expiration", "natural", 6, 0),
                lambda: backend.attach_strategy("DU123", "paper"),
            ]:
                with self.assertRaisesRegex(ValueError, "关闭"):
                    action()
            broker.assert_not_called()
