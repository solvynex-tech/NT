import asyncio
from threading import Event, Thread

from nautilus_trader.common import Environment, LoggerConfig, LogLevel
from nautilus_trader.live import LiveNode
from nautilus_trader.model import TraderId

from qwork.actors.broken_wing.actor import BrokenWingButterflyActor
from qwork.actors.portfolio_pnl.actor import PortfolioPnLActor
from qwork.actors.term_structure.actor import TermStructureActor
from qwork.actors.volatility_surface.actor import VolatilitySurfaceActor
from qwork.core.state import DashboardState
from qwork.providers.theta.collector import SurfaceCollector


class VolatilityRuntime:
    def __init__(self, settings, request, collector=None):
        self.state = DashboardState(request)
        self.collector = collector or SurfaceCollector(settings)
        self.handle = None
        self.ready = Event()
        self.stop_requested = Event()
        self.thread = Thread(target=self._run, name="nautilus-volatility", daemon=True)

    def _run(self):
        node = None
        try:
            # PyLiveNode and Python actors are thread-affine. Construct, run and
            # dispose them on this thread; only the native stop handle crosses threads.
            builder = LiveNode.builder("QWORK-VOLATILITY", TraderId("QWORK-VOLATILITY"), Environment.LIVE)
            builder = builder.with_reconciliation(False).with_logging(LoggerConfig(stdout_level=LogLevel.ERROR))
            builder = builder.with_delay_post_stop_secs(0).with_delay_shutdown_secs(0)
            node = builder.build()
            self.handle = node.handle()
            node.add_actor(TermStructureActor(self.state))
            node.add_actor(BrokenWingButterflyActor(self.state))
            node.add_actor(PortfolioPnLActor(self.state))
            node.add_actor(VolatilitySurfaceActor(self.state, self.collector))
            self.ready.set()
            if not self.stop_requested.is_set():
                # The async entrypoint omits run()'s main-thread signal handlers.
                async def run_node():
                    await node.run_async()

                asyncio.run(run_node())
        except Exception as exc:
            self.state.set_status("error", f"NT Actor 运行失败（{type(exc).__name__}）")
        finally:
            self.ready.set()
            if node:
                node.dispose()

    def start(self):
        self.thread.start()

    def stop(self):
        self.collector.close()
        self.stop_requested.set()
        self.ready.wait(timeout=5)
        if self.handle:
            self.handle.stop()
        if self.thread.is_alive():
            self.thread.join(timeout=5)
