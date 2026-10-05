"""Historical display bars only; no execution or financial-clock caching."""

from threading import RLock


def candle_rows(series, minutes):
    bars = series.resample(f"{int(minutes)}min").ohlc().dropna()
    return [
        dict(timestamp=stamp.isoformat(), **row)
        for stamp, row in zip(bars.index, bars.to_dict(orient="records"), strict=True)
    ]


class ReplayCandles:
    """One immutable quote source and one clock result, shared by UI readers."""

    def __init__(self):
        self.lock = RLock()
        self.source = None
        self.series = None
        self.key = None
        self.rows = []

    def snapshot(self, observations, minutes, clock):
        import pandas as pd

        with self.lock:
            if observations is not self.source:
                self.source = observations
                self.series = pd.Series(
                    [row["mid"] for row in observations],
                    index=pd.to_datetime([row["timestamp"] for row in observations], utc=True),
                ).sort_index()
                self.key = None
            key = (int(minutes), clock)
            if key != self.key:
                # Slice before OHLC: later quotes cannot change the partial bar.
                visible = self.series.loc[: pd.Timestamp(clock)] if clock else self.series.iloc[:0]
                self.rows = candle_rows(visible, minutes) if len(visible) else []
                self.key = key
            return [dict(row) for row in self.rows]
