from __future__ import annotations

import time
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta

from nautilus_trader.common import DataActor, DataActorConfig
from nautilus_trader.model import ActorId

from qwork.providers.theta.provider import ProviderError

SURFACE_TOPIC = "qwork.volatility.surface"


class VolatilitySurfaceActor(DataActor):
    """Native clock schedules one bounded data job, then publishes completed IV surfaces."""

    def __new__(cls, state, collector):
        return super().__new__(cls, DataActorConfig(actor_id=ActorId("VOLATILITY-SURFACE")))

    def __init__(self, state, collector):
        self.dashboard = state
        self.collector = collector
        self.pool = None
        self.future = None
        self.next_due = 0
        self.last_generation = -1
        self.last_refresh = -1

    def on_start(self):
        self.pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="volatility-fetch")
        self.clock.set_timer("volatility-surface-poll", timedelta(milliseconds=250), callback=self.on_time_event)
        self.on_time_event(None)

    def on_time_event(self, event):
        request, generation, paused, refresh_id = self.dashboard.controls()
        if generation != self.last_generation or refresh_id != self.last_refresh:
            self.next_due = 0
            self.last_generation, self.last_refresh = generation, refresh_id
        if self.future and self.future.done():
            future, self.future = self.future, None
            try:
                frame = future.result()
                if self.dashboard.publish_surface(frame):
                    self.publish_message(SURFACE_TOPIC, frame)
            except ProviderError as exc:
                if self.job_generation == generation:
                    self.dashboard.set_status("error", str(exc))
            except Exception as exc:
                if self.job_generation == generation:
                    self.dashboard.set_status("error", f"计算失败（{type(exc).__name__}），请检查参数和依赖")
        if not paused and self.future is None and time.monotonic() >= self.next_due:
            self.job_generation = generation
            self.future = self.pool.submit(self.collector.collect, request, generation)
            self.next_due = time.monotonic() + request.refresh_seconds
            self.dashboard.set_status("updating", "正在更新期权报价与 IV 曲面")

    def on_stop(self):
        self.clock.cancel_timer("volatility-surface-poll")
        self.collector.close()
        if self.future:
            self.future.cancel()
        if self.pool:
            self.pool.shutdown(wait=False, cancel_futures=True)
        self.dashboard.set_status("stopped", "Actor 已停止")
