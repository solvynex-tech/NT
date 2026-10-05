"""Local snapshot benchmark using an existing real historical option cache."""

import argparse
import cProfile
import io
import json
import pstats
import statistics
import time
from pathlib import Path

from qwork.core.config import Settings
from qwork.ui.terminal.backend import TerminalBackend

parser = argparse.ArgumentParser()
parser.add_argument("--label", default="after")
args = parser.parse_args()
backend = TerminalBackend(Settings.load())
output = Path("data/verification")
try:
    cache = backend.settings.data_dir / "history" / "SPY-2026-10-02-2026-10-16-763-put-1m.json"
    assert cache.exists(), "Benchmark requires the real option cache; do not fetch remote data"
    backend.load_chart("SPY", "2026-10-16", 763, "put", "2026-10-02", 1)
    deadline = time.monotonic() + 20
    while not backend.chart.get("observations"):
        assert time.monotonic() < deadline, backend.chart.get("status")
        time.sleep(0.02)
    backend.replay.update(status="running", day="2026-10-02", clock=backend.chart["observations"][-1]["timestamp"])
    profiler = cProfile.Profile()
    times = []
    profiler.enable()
    for _ in range(100):
        start = time.perf_counter()
        backend.snapshot()
        times.append((time.perf_counter() - start) * 1000)
    profiler.disable()
    profiled_times = times
    times = []
    for _ in range(100):
        start = time.perf_counter()
        backend.snapshot()
        times.append((time.perf_counter() - start) * 1000)
    stream = io.StringIO()
    pstats.Stats(profiler, stream=stream).sort_stats("cumulative").print_stats(18)
    result = dict(
        label=args.label,
        count=100,
        option_observations=len(backend.chart["observations"]),
        median_ms=round(statistics.median(times), 3),
        p95_ms=round(sorted(times)[94], 3),
        profiled_median_ms=round(statistics.median(profiled_times), 3),
    )
    (output / f"terminal-profile-{args.label}.json").write_text(json.dumps(result, indent=2), encoding="utf-8")
    (output / f"terminal-profile-{args.label}.txt").write_text(stream.getvalue(), encoding="utf-8")
    print(json.dumps(result))
finally:
    backend.close()
