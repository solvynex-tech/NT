from nautilus_trader.common import DataActor, DataActorConfig
from nautilus_trader.model import ActorId

from qwork.actors.volatility_surface.actor import SURFACE_TOPIC
from qwork.analytics.volatility import term_structure

TERM_TOPIC = "qwork.volatility.term_structure"


class TermStructureActor(DataActor):
    """Subscribe to native surface messages; derive ATM term structure without fetching twice."""

    def __new__(cls, state):
        return super().__new__(cls, DataActorConfig(actor_id=ActorId("VOLATILITY-TERM-STRUCTURE")))

    def __init__(self, state):
        self.dashboard = state

    def on_start(self):
        self.subscribe_topic(SURFACE_TOPIC, self.on_surface)

    def on_surface(self, frame):
        result = term_structure(frame)
        if self.dashboard.publish_term(result):
            self.publish_message(TERM_TOPIC, result)

    def on_stop(self):
        self.unsubscribe_topic(SURFACE_TOPIC, self.on_surface)
