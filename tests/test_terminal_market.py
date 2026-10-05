"""Provenance and freshness prevent plausible-looking analysis from using wrong data."""

import unittest
from datetime import UTC, datetime, timedelta
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest.mock import patch

from qwork.actors.portfolio_pnl.models import PortfolioRequest
from qwork.analytics.portfolio import analyze_portfolio
from qwork.core.config import Settings
from qwork.core.market import market_health
from qwork.core.state import DashboardState, VolatilityRequest
from qwork.ui.terminal.app import create_terminal
from qwork.ui.terminal.backend import TerminalBackend
from qwork.ui.terminal.workflow import chain_frame


class MarketIntegrityTests(unittest.TestCase):
    def frame(self, stamp, mode="live"):
        return dict(
            generation=0,
            symbol="SPY",
            mode=mode,
            synthetic=False,
            updated_at=stamp.isoformat(),
            observed_at=stamp.isoformat(),
            points=[dict(timestamp=stamp.isoformat())],
        )

    def test_old_result_rejected_even_if_collection_timestamps_repeat(self):
        stamp = datetime(2026, 10, 2, 14, tzinfo=UTC)
        config = VolatilityRequest(mode="historical", as_of="2026-10-02")
        state = DashboardState(config)
        first = self.frame(stamp, "historical")
        state.publish_surface(first)
        old = analyze_portfolio(first, PortfolioRequest(), config)
        self.assertTrue(state.publish_portfolio(old))
        second = self.frame(stamp, "historical")
        state.publish_surface(second)
        self.assertFalse(state.publish_portfolio(old))
        self.assertFalse(state.publish_term(old))
        self.assertFalse(state.publish_scan(dict(old, scan_generation=0)))
        current = analyze_portfolio(second, PortfolioRequest(), config)
        self.assertTrue(state.publish_portfolio(current))
        self.assertTrue(state.publish_term(current))
        self.assertTrue(state.publish_scan(dict(current, scan_generation=0)))

    def test_live_quote_age_provider_errors_and_future_clock_block_analysis(self):
        now = datetime(2026, 10, 5, 14, tzinfo=UTC)
        context = dict(status="ready", request=dict(refresh_seconds=5, max_quote_age_seconds=5))
        current = self.frame(now - timedelta(seconds=3))
        self.assertTrue(market_health(current, context, now)["valid"])
        self.assertTrue(market_health(current, dict(context, status="updating"), now)["valid"])
        invalid = [
            self.frame(now - timedelta(seconds=16)),
            self.frame(now + timedelta(seconds=10)),
            dict(current, synthetic=True),
            dict(current, points=[{}]),
        ]
        for frame in invalid:
            self.assertFalse(market_health(frame, context, now)["valid"])
        for change in [dict(status="error"), dict(status="stopped"), dict(paused=True)]:
            self.assertFalse(market_health(current, dict(context, **change), now)["valid"])
        stale_leg = dict(current, points=[dict(timestamp=(now - timedelta(seconds=20)).isoformat())])
        self.assertFalse(market_health(stale_leg, context, now)["valid"])

    def test_history_remains_available_but_not_in_live_workspace(self):
        frame = self.frame(datetime(2026, 10, 2, 14, tzinfo=UTC), "historical")
        self.assertTrue(market_health(frame)["valid"])
        self.assertEqual(chain_frame(dict(frame=frame), "research"), frame)
        self.assertEqual(chain_frame(dict(frame=frame), "live"), {})

    def test_stale_live_frame_cannot_start_native_portfolio_analysis(self):
        with TemporaryDirectory() as directory:
            backend = TerminalBackend(Settings(Path(directory)))
            try:
                backend.cached_frame = self.frame(datetime.now(UTC) - timedelta(hours=1))
                with patch.object(backend, "_apply_portfolio") as apply:
                    with self.assertRaisesRegex(ValueError, "过期"):
                        backend.analyze_combinations(PortfolioRequest(), "live")
                    apply.assert_not_called()
            finally:
                backend.close()

    def test_display_uses_portfolio_and_term_times_and_discloses_pending_recalculation(self):
        old = datetime(2026, 10, 2, 13, 45, tzinfo=UTC)
        new = datetime(2026, 10, 2, 14, tzinfo=UTC)
        portfolio = dict(
            self.frame(old, "historical"),
            combinations=[],
            diagnostics=[],
            aggregate=dict(scenario_prices=[750, 760], expiry_pnl=[0, 10], current_pnl=[0, 5], liquidation_pnl=42),
        )
        term = dict(self.frame(old, "historical"), rows=[])
        snapshot = dict(frame=self.frame(new, "historical"), portfolio=portfolio, term=term)
        backend = SimpleNamespace(
            cached_frame={}, cached_scan={}, default_day="2026-10-02", list_runs=lambda: [], snapshot=lambda: snapshot
        )
        app = create_terminal(backend)
        poll = app.callback_map["t-state.data"]["callback"].__wrapped__
        render = next(
            value["callback"].__wrapped__
            for key, value in app.callback_map.items()
            if "t-analysis-summary.children" in key
        )
        output = render(poll(0), "research", "aggregate", None)
        self.assertIn(old.isoformat(), output[2])
        self.assertNotIn(new.isoformat(), output[2])
        self.assertIn("等待新行情重算", output[3])
        self.assertIn(old.isoformat(), output[5])

    def test_desktop_download_configuration(self):
        import webview

        from qwork.ui.terminal.desktop import configure_webview

        previous = webview.settings["ALLOW_DOWNLOADS"]
        try:
            webview.settings["ALLOW_DOWNLOADS"] = False
            self.assertTrue(configure_webview().settings["ALLOW_DOWNLOADS"])
        finally:
            webview.settings["ALLOW_DOWNLOADS"] = previous
