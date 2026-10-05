"""Wall-clock playback controls; financial timestamps remain native historical time."""

import time
from itertools import groupby
from threading import Condition


class ReplayController:
    def __init__(self, speed=600, observer=None):
        self.condition = Condition()
        self.speed = 600
        self.paused = False
        self.stopped = False
        self.steps = 0
        self.observer = observer
        self.configure(speed=speed)

    def configure(self, speed=None, paused=None, step=False, stop=False):
        with self.condition:
            if speed is not None:
                speed = float(speed)
                if not 0 <= speed <= 100000:
                    raise ValueError("Replay speed must be 0 (maximum) or up to 100000x")
                self.speed = speed
            if paused is not None:
                self.paused = bool(paused)
            if step:
                self.paused = True
                self.steps += 1
            self.stopped = self.stopped or stop
            self.condition.notify_all()

    def wait(self, simulation_seconds):
        remaining = max(0, simulation_seconds)
        with self.condition:
            while True:
                if self.stopped:
                    return False
                if self.steps:
                    self.steps -= 1
                    return True
                if self.paused:
                    self.condition.wait(timeout=0.5)
                    continue
                if self.speed == 0 or remaining <= 0:
                    return True
                speed = self.speed
                started = time.monotonic()
                self.condition.wait(timeout=min(remaining / speed, 0.25))
                remaining -= (time.monotonic() - started) * speed


def run_controlled(engine, data, controller):
    """Native streaming batches, with no cache or strategy reset between batches."""
    previous = None
    for stamp, group in groupby(data, key=lambda item: item.ts_init):
        if not controller.wait((stamp - previous) / 1e9 if previous is not None else 0):
            engine.end()
            return False
        engine.add_data(list(group))
        engine.run(streaming=True)
        if controller.observer:
            controller.observer(engine, stamp)
        engine.clear_data()
        previous = stamp
    engine.end()
    return True
