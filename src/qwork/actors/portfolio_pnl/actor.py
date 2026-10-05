from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta

from nautilus_trader.common import DataActor, DataActorConfig
from nautilus_trader.model import ActorId

from qwork.actors.volatility_surface.actor import SURFACE_TOPIC
from qwork.analytics.portfolio import analyze_portfolio

PNL_TOPIC = "qwork.volatility.portfolio_pnl"


class PortfolioPnLActor(DataActor):
    """Evaluate one or many user-specified combinations from shared live surface updates."""

    def __new__(cls, state):
        return super().__new__(cls, DataActorConfig(actor_id=ActorId("PORTFOLIO-PNL")))

    def __init__(self, state):
        self.dashboard = state
        self.latest = None
        self.future = None
        self.pool = None
        self.processed_key = None

    def on_start(self):
        self.pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="portfolio-pnl")
        self.subscribe_topic(SURFACE_TOPIC, self.on_surface)
        self.clock.set_timer("portfolio-pnl", timedelta(milliseconds=250), callback=self.on_time_event)

    def on_surface(self, frame):
        self.latest = frame

    def on_time_event(self, event):
        if self.future and self.future.done():
            future, self.future = self.future, None
            try:
                result = future.result()
                if self.dashboard.publish_portfolio(result):
                    self.publish_message(PNL_TOPIC, result)
            except Exception as exc:
                self.dashboard.set_portfolio_error(f"组合估值失败（{type(exc).__name__}）")
        request, portfolio_generation = self.dashboard.portfolio_controls()
        volatility_request, generation, _, _ = self.dashboard.controls()
        key = (
            (
                self.latest["generation"],
                self.latest.get("surface_revision"),
                self.latest["updated_at"],
                portfolio_generation,
            )
            if self.latest
            else None
        )
        if key and key[0] == generation and key != self.processed_key and self.future is None:
            self.processed_key = key
            self.future = self.pool.submit(
                analyze_portfolio, self.latest, request, volatility_request, portfolio_generation
            )

    def on_stop(self):
        self.unsubscribe_topic(SURFACE_TOPIC, self.on_surface)
        self.clock.cancel_timer("portfolio-pnl")
        if self.pool:
            self.pool.shutdown(wait=False, cancel_futures=True)
