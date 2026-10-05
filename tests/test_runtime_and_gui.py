"""Native Nautilus message-bus integration with explicitly synthetic data."""

import json
import tempfile
import time
import unittest
from pathlib import Path

from qwork.actors.portfolio_pnl.models import PortfolioRequest
from qwork.core.config import Settings
from qwork.core.state import DashboardState, VolatilityRequest
from qwork.runtime.actors import VolatilityRuntime
from qwork.ui.dashboard import create_dashboard


class NativeRuntimeTests(unittest.TestCase):
    def test_all_four_native_actors_and_reconfiguration(self):
        with tempfile.TemporaryDirectory() as directory:
            request = VolatilityRequest(
                source="fixture", mode="historical", as_of="2026-01-05", spot=100, max_expirations=3
            )
            runtime = VolatilityRuntime(Settings(Path(directory)), request)
            runtime.start()
            try:
                self.assertTrue(runtime.ready.wait(10))
                self.wait_for(
                    runtime, lambda v: all(v[k] is not None for k in ("surface", "term", "scan", "portfolio"))
                )
                initial = runtime.state.snapshot()
                self.assertTrue(initial["surface"]["synthetic"])
                self.assertGreater(len(initial["term"]["rows"]), 1)
                expiry = initial["surface"]["expirations"][0]
                runtime.state.configure_portfolio(
                    PortfolioRequest(
                        combinations=[
                            dict(
                                name="Test",
                                legs=[
                                    dict(
                                        symbol="SPY",
                                        expiration=expiry,
                                        strike=100,
                                        right="call",
                                        side="buy",
                                        quantity=1,
                                        entry_premium=2,
                                    )
                                ],
                            )
                        ]
                    )
                )
                self.wait_for(runtime, lambda v: v["portfolio"] and v["portfolio"]["aggregate"] is not None)
                self.assertIsNotNone(runtime.state.snapshot()["portfolio"]["aggregate"]["liquidation_pnl"])
                current = runtime.state.controls()[0]
                runtime.state.configure(current.model_copy(update={"symbol": "QQQ"}))
                self.wait_for(
                    runtime,
                    lambda v: (
                        v["surface"] and v["surface"]["symbol"] == "QQQ" and v["scan"] and v["scan"]["symbol"] == "QQQ"
                    ),
                )
            finally:
                runtime.stop()
            self.assertFalse(runtime.thread.is_alive(), "Native node must shut down on its owning thread")

    def wait_for(self, runtime, condition):
        deadline = time.monotonic() + 12
        while time.monotonic() < deadline:
            snapshot = runtime.state.snapshot()
            if condition(snapshot):
                return
            if snapshot["status"] == "error":
                self.fail(snapshot["message"])
            time.sleep(0.1)
        self.fail("Native actors did not publish within 12 seconds")


class DashboardTests(unittest.TestCase):
    def setUp(self):
        self.state = DashboardState(VolatilityRequest(source="fixture", mode="historical", as_of="2026-01-05"))
        self.app = create_dashboard(self.state)
        self.client = self.app.server.test_client()

    def test_dash_layout_and_local_origin_boundary(self):
        self.assertEqual(self.client.get("/_dash-layout").status_code, 200)
        self.assertEqual(self.client.get("/_dash-dependencies").status_code, 200)
        self.assertEqual(self.client.get("/api/snapshot").status_code, 200)
        self.assertEqual(
            self.client.get("/api/snapshot", headers={"Origin": "https://external.invalid"}).status_code, 403
        )
        self.assertEqual(self.client.get("/api/snapshot", headers={"Host": "external.invalid"}).status_code, 403)

    def submit(self, text):
        return self.client.post(
            "/_dash-update-component",
            json=dict(
                output="portfolio-validation.children",
                outputs=dict(id="portfolio-validation", property="children"),
                inputs=[dict(id="portfolio-apply", property="n_clicks", value=1)],
                state=[
                    dict(id="portfolio-json", property="value", value=text),
                    dict(id="portfolio-fee", property="value", value=0.65),
                    dict(id="portfolio-band", property="value", value=20),
                ],
                changedPropIds=["portfolio-apply.n_clicks"],
            ),
        )

    def test_gui_accepts_single_combination_and_rejects_invalid_units(self):
        combo = dict(
            name="Entered",
            legs=[
                dict(
                    symbol="SPY",
                    expiration="2026-01-16",
                    strike=600,
                    right="put",
                    side="sell",
                    quantity=2,
                    entry_premium=3,
                )
            ],
        )
        response = self.submit(json.dumps(combo))
        self.assertEqual(response.status_code, 200)
        self.assertEqual(len(self.state.portfolio_controls()[0].combinations), 1)
        self.assertIn(600, self.state.controls()[0].required_strikes)
        before = self.state.snapshot()
        combo["legs"][0]["quantity"] = -1
        response = self.submit(json.dumps(combo))
        self.assertEqual(response.status_code, 200)
        self.assertEqual(self.state.snapshot(), before)


if __name__ == "__main__":
    unittest.main()
