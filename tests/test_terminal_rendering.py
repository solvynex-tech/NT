"""Rendering optimizations must preserve fresh data and independent clients."""

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from dash import no_update

from qwork.core.config import Settings
from qwork.ui.terminal.app import create_terminal
from qwork.ui.terminal.backend import TerminalBackend
from qwork.ui.terminal.charts import ReplayCandles
from qwork.ui.terminal.rendering import changed_outputs


class RenderingTests(unittest.TestCase):
    def test_replay_bar_cache_preserves_partial_bars_and_snapshot_isolation(self):
        source = [
            dict(timestamp=f"2026-10-02T13:{minute}:00+00:00", mid=mid) for minute, mid in [(45, 2), (46, 3), (47, 999)]
        ]
        cache = ReplayCandles()
        first = cache.snapshot(source, 5, source[1]["timestamp"])
        self.assertEqual([first[0][key] for key in ["open", "high", "low", "close"]], [2, 3, 2, 3])
        first[0]["high"] = -1
        self.assertEqual(cache.snapshot(source, 5, source[1]["timestamp"])[0]["high"], 3)
        self.assertEqual(cache.snapshot(source, 5, source[2]["timestamp"])[0]["high"], 999)
        self.assertEqual(cache.snapshot(source, 5, source[0]["timestamp"])[0]["high"], 2)
        self.assertEqual(cache.snapshot(source, 5, None), [])
        self.assertEqual(len(cache.snapshot(source, 1, source[1]["timestamp"])), 2)
        replacement = [dict(source[0], mid=7)]
        self.assertEqual(cache.snapshot(replacement, 5, source[0]["timestamp"])[0]["close"], 7)

    def test_unchanged_outputs_and_independent_browsers(self):
        _, _, signature = changed_outputs(([{"bid": 2}], "ready"), None)
        rows, text, stored = changed_outputs(([{"bid": 2}], "ready"), signature)
        self.assertIs(rows, no_update)
        self.assertIs(text, no_update)
        self.assertIs(stored, no_update)
        rows, text, _ = changed_outputs(([{"bid": 3}], "ready"), signature)
        self.assertEqual(rows, [{"bid": 3}])
        self.assertIs(text, no_update)
        self.assertEqual(changed_outputs(([{"bid": 2}], "ready"), None)[0], [{"bid": 2}])
        self.assertEqual(changed_outputs(([], "offline"), signature)[0], [])

    def test_hidden_graphs_catch_up_on_activation_and_idle_graphs_are_not_built(self):
        with tempfile.TemporaryDirectory() as directory:
            backend = TerminalBackend(Settings(Path(directory)))
            self.addCleanup(backend.close)
            app = create_terminal(backend)
            poll = app.callback_map["t-state.data"]["callback"].__wrapped__
            render = next(
                item["callback"].__wrapped__
                for key, item in app.callback_map.items()
                if "t-chart-title.children" in key
            )
            marker = poll(0)
            with patch("qwork.ui.terminal.app.empty_figure", side_effect=AssertionError("hidden graph built")):
                hidden = render(marker, "heatmap", "put", None, "research", None, [])
            self.assertTrue(all(value is no_update for value in hidden[9:12]))
            active = render(marker, "heatmap", "put", hidden[-1], "research", None, ["t-equity"])
            self.assertIsNot(active[10], no_update)
            with patch("qwork.ui.terminal.app.empty_figure", side_effect=AssertionError("idle graph rebuilt")):
                idle = render(marker, "heatmap", "put", active[-1], "research", None, ["t-equity"])
            self.assertTrue(all(value is no_update for value in idle[9:12]))
