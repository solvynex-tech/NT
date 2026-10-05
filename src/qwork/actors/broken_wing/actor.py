from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta

from nautilus_trader.common import DataActor, DataActorConfig
from nautilus_trader.model import ActorId

from qwork.actors.volatility_surface.actor import SURFACE_TOPIC
from qwork.analytics.butterfly import scan_butterflies

BUTTERFLY_TOPIC = "qwork.volatility.broken_wing"


class BrokenWingButterflyActor(DataActor):
    """Read-only research scanner; consumes IV surfaces and emits qualified candidates."""

    def __new__(cls, state):
        return super().__new__(cls, DataActorConfig(actor_id=ActorId("BROKEN-WING-SCANNER")))

    def __init__(self, state):
        self.dashboard = state
        self.latest = None
        self.future = None
        self.pool = None
        self.processed_key = None

    def on_start(self):
        self.pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="butterfly-scan")
        self.subscribe_topic(SURFACE_TOPIC, self.on_surface)
        self.clock.set_timer("broken-wing-scan", timedelta(milliseconds=250), callback=self.on_time_event)

    def on_surface(self, frame):
        self.latest = frame

    def on_time_event(self, event):
        if self.future and self.future.done():
            future, self.future = self.future, None
            try:
                result = future.result()
                if self.dashboard.publish_scan(result):
                    self.publish_message(BUTTERFLY_TOPIC, result)
            except Exception as exc:
                self.dashboard.set_scan_error(f"组合扫描失败（{type(exc).__name__}）")
        request, scan_generation = self.dashboard.scan_controls()
        key = (
            (self.latest["generation"], self.latest.get("surface_revision"), self.latest["updated_at"], scan_generation)
            if self.latest
            else None
        )
        if key and key != self.processed_key and self.future is None:
            self.processed_key = key
            self.future = self.pool.submit(scan_butterflies, self.latest, request, scan_generation)

    def on_stop(self):
        self.unsubscribe_topic(SURFACE_TOPIC, self.on_surface)
        self.clock.cancel_timer("broken-wing-scan")
        if self.pool:
            self.pool.shutdown(wait=False, cancel_futures=True)
