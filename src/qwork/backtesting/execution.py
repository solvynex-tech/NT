"""Explicit, uncalibrated execution scenarios using native Nautilus books/models."""

import math
from collections import Counter
from datetime import datetime

from nautilus_trader.execution import DefaultFillModel, StaticLatencyModel
from nautilus_trader.model import BookAction, BookOrder, OrderBookDelta, OrderBookDeltas, OrderSide, Price, Quantity
from pydantic import Field

from qwork.strategies.broken_wing.lifecycle import OrderManagementConfig


class ReplayExecutionConfig(OrderManagementConfig):
    latency_ms: int = Field(default=100, ge=0, le=5000)
    combo_spread_fraction: float = Field(default=1, gt=0, le=1)
    leg_slippage_per_share: float = Field(default=0, ge=0, le=1)
    liquidity_fraction: float = Field(default=1, gt=0, le=1)
    touch_fill_probability: float = Field(default=0, ge=0, le=1)
    random_seed: int = Field(default=42, ge=0)

    def quote(self, legs):
        low, body, high = legs
        mid = sum((q.bid + q.ask) / 2 * (-2 if i == 1 else 1) for i, q in enumerate(legs))
        half = sum((q.ask - q.bid) / 2 * (2 if i == 1 else 1) for i, q in enumerate(legs))
        half = half * self.combo_spread_fraction + 4 * self.leg_slippage_per_share
        bid_size = math.floor(min(low.bid_size, body.ask_size / 2, high.bid_size) * self.liquidity_fraction)
        ask_size = math.floor(min(low.ask_size, body.bid_size / 2, high.ask_size) * self.liquidity_fraction)
        return mid - half, mid + half, bid_size, ask_size

    def fill_model(self):
        return DefaultFillModel(self.touch_fill_probability, 0, self.random_seed)

    def latency_model(self):
        return StaticLatencyModel(base_latency_nanos=self.latency_ms * 1_000_000)

    def describe(self):
        return dict(
            **self.model_dump(),
            calibrated=False,
            actual_combo_quotes=False,
            quote_source="Synthetic finite top levels derived from synchronized leg NBBO",
            inside_spread_fills="No automatic midpoint fill; native limit matching against later quotes",
            book_depth="Two finite top levels in an L2 book; no invented deeper liquidity",
            shared_leg_allocation="Fixed-universe proportional top-size partition; unused capacity is not redistributed",
            scenario="Leg-natural conservative boundary"
            if self.combo_spread_fraction == 1
            else "Assumed narrower combo spread; sensitivity analysis only",
        )


def leg_demands(candidates):
    """Partition shared leg capacity before native, independent proxy books are built."""
    demands = Counter()
    for candidate in candidates:
        for index, strike in enumerate(candidate["strikes"]):
            key = (candidate["expiration"], strike, candidate["right"])
            demands[key] += 2 if index == 1 else 1
    return demands


def allocated_sizes(candidate, legs, demands, fraction=1):
    capacities = []
    for index, quote in enumerate(legs):
        key = (candidate["expiration"], candidate["strikes"][index], candidate["right"])
        # Every proxy receives one unit of capacity per unit of aggregate leg
        # demand. This also bounds opposing proxy directions independently.
        bid_side, ask_side = (quote.ask_size, quote.bid_size) if index == 1 else (quote.bid_size, quote.ask_size)
        capacities.append(
            (math.floor(bid_side * fraction / demands[key]), math.floor(ask_side * fraction / demands[key]))
        )
    return min(row[0] for row in capacities), min(row[1] for row in capacities)


def execution_summary(orders, fills):
    entries = [row for row in orders if row["side"] == "BUY"]
    requested = sum(int(row["quantity"]) for row in entries)
    filled = sum(int(row["filled_qty"]) for row in entries)
    by_id = {row["client_order_id"]: row for row in entries}
    waits = [
        (datetime.fromisoformat(row["ts_event"]).timestamp() * 1e9 - int(by_id[row["client_order_id"]]["ts_init"]))
        / 1e9
        for row in fills
        if row["client_order_id"] in by_id
    ]
    return dict(
        entry_orders=len(entries),
        filled_entry_orders=sum(int(row["filled_qty"]) > 0 for row in entries),
        requested_units=requested,
        filled_units=filled,
        quantity_fill_rate=filled / requested if requested else None,
        canceled_without_fill=sum(row["status"] == "CANCELED" and not int(row["filled_qty"]) for row in entries),
        partial_entry_orders=sum(0 < int(row["filled_qty"]) < int(row["quantity"]) for row in entries),
        mean_fill_wait_seconds=sum(waits) / len(waits) if waits else None,
    )


def finite_book(quote):
    """Represent available top sizes in L2 so market orders cannot invent residual depth."""
    instrument = quote.instrument_id
    stamp = quote.ts_event
    deltas = [OrderBookDelta.clear(instrument, 0, stamp, stamp)]
    levels = [
        (OrderSide.BUY, quote.bid_price, quote.bid_size),
        (OrderSide.SELL, quote.ask_price, quote.ask_size),
    ]
    levels = [level for level in levels if int(level[2]) > 0]
    for index, (side, price, size) in enumerate(levels):
        deltas.append(
            OrderBookDelta(
                instrument,
                BookAction.ADD,
                BookOrder(side, Price.from_str(str(price)), Quantity.from_int(int(size)), index + 1),
                128 if index == len(levels) - 1 else 0,
                0,
                stamp,
                stamp,
            )
        )
    return OrderBookDeltas(instrument, deltas)
