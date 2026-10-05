"""Durable strategy intent; native orders and broker positions remain authoritative."""

import sqlite3
from collections import defaultdict
from contextlib import closing, contextmanager
from copy import deepcopy
from pathlib import Path
from typing import Literal

from pydantic import Field, model_validator

from qwork.core.validation import StrictModel


class StrategySnapshot(StrictModel):
    version: Literal[1] = 1
    account: str
    symbol: str
    session_date: str
    candidates: list[dict]
    positions: dict[str, dict] = Field(default_factory=dict)
    pending: dict[str, tuple[str, str]] = Field(default_factory=dict)
    order_registry: dict[str, tuple[str, str]] = Field(default_factory=dict)
    completed: set[str] = Field(default_factory=set)
    used: set[str] = Field(default_factory=set)
    attempts_by_key: dict[str, int] = Field(default_factory=dict)
    next_attempt_ns: dict[str, int] = Field(default_factory=dict)
    halted: bool = False
    risk: dict = Field(default_factory=dict)

    @model_validator(mode="after")
    def consistent_intent(self):
        from datetime import date

        date.fromisoformat(self.session_date)
        keys = {candidate["key"] for candidate in self.candidates}
        if len(keys) != len(self.candidates) or not set(self.positions) <= keys:
            raise ValueError("Checkpoint candidates do not match combination state")
        if not (self.used | self.completed) <= keys:
            raise ValueError("Checkpoint contains unknown completed combinations")
        for key, position in self.positions.items():
            units = position["units"]
            if type(units) is not int or units < 0 or position["candidate"]["key"] != key:
                raise ValueError("Invalid checkpoint position intent")
            if position["state"] in {"closed", "rejected"} and units:
                raise ValueError("Terminal combination still has inventory")
            if position["state"] not in {"opening", "partial_open", "open", "closing", "closed", "rejected"}:
                raise ValueError("Unknown combination lifecycle state")
        for mapping in (self.pending, self.order_registry):
            if any(key not in self.positions or action not in {"open", "close"} for key, action in mapping.values()):
                raise ValueError("Checkpoint order has no combination owner")
        if not set(self.pending) <= set(self.order_registry):
            raise ValueError("Pending order is absent from intent registry")
        if any(owner != self.order_registry[client_id] for client_id, owner in self.pending.items()):
            raise ValueError("Pending order ownership disagrees with registry")
        return self


class CheckpointStore:
    def __init__(self, path, account, symbol):
        self.path = Path(path)
        self.account = account
        self.symbol = symbol

    def load(self, timeout=5):
        if not self.path.exists():
            return None
        with closing(sqlite3.connect(self.path, timeout=timeout)) as connection:
            row = connection.execute("SELECT payload FROM snapshot WHERE id=1").fetchone()
        if row is None:
            raise ValueError("Checkpoint exists without committed strategy state")
        state = StrategySnapshot.model_validate_json(row[0])
        if state.account != self.account or state.symbol != self.symbol:
            raise ValueError("Checkpoint belongs to a different account or symbol")
        return state

    def save(self, state):
        if state.account != self.account or state.symbol != self.symbol:
            raise ValueError("Refusing checkpoint for a different account or symbol")
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with closing(sqlite3.connect(self.path)) as connection, connection:
            connection.execute("PRAGMA synchronous=FULL")
            connection.execute(
                "CREATE TABLE IF NOT EXISTS snapshot (id INTEGER PRIMARY KEY CHECK(id=1), payload TEXT NOT NULL)"
            )
            connection.execute("INSERT OR REPLACE INTO snapshot VALUES (1, ?)", (state.model_dump_json(),))


@contextmanager
def account_lease(path):
    """SQLite's OS-backed write lock releases even when the process crashes."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(path, timeout=0)
    try:
        connection.execute("BEGIN IMMEDIATE")
    except sqlite3.OperationalError as exc:
        connection.close()
        raise ValueError("Another QWORK trading process owns this account") from exc
    try:
        yield
    finally:
        connection.rollback()
        connection.close()


def reconcile_intent(state, orders, broker_quantities, leg_ids, strategy_id, today, spread_ids=None):
    """Recover only when current native order evidence explains every broker leg."""
    restored = deepcopy(state)
    failures = []
    for client_id in restored.pending:
        if client_id not in orders:
            failures.append(f"Pending order missing from native reconciliation: {client_id}")
    expected = defaultdict(int)
    for key, position in restored.positions.items():
        if position["state"] in {"closed", "rejected"}:
            continue
        owned = {client_id: action for client_id, (owner, action) in restored.order_registry.items() if owner == key}
        units = 0
        if not owned:
            failures.append(f"No native order evidence for {key}")
        for client_id, action in owned.items():
            order = orders.get(client_id)
            if not order or str(order.strategy_id) != str(strategy_id):
                failures.append(f"Missing or foreign native order: {client_id}")
                continue
            if spread_ids is not None and (
                str(order.instrument_id) != str(spread_ids[key])
                or str(order.side) != ("BUY" if action == "open" else "SELL")
            ):
                failures.append(f"Native order instrument or side mismatch: {client_id}")
                continue
            if float(order.filled_qty) != int(order.filled_qty):
                failures.append(f"Fractional BAG fill quantity: {client_id}")
                continue
            units += int(order.filled_qty) * (1 if action == "open" else -1)
        if units < 0:
            failures.append(f"Over-closed combination: {key}")
            continue
        position["units"] = units
        candidate = position["candidate"]
        for index, strike in enumerate(candidate["strikes"]):
            leg = (candidate["expiration"], strike, candidate["right"])
            if leg not in leg_ids:
                failures.append(f"Unloaded leg: {leg}")
                continue
            expected[str(leg_ids[leg])] += units * (-2 if index == 1 else 1)
    for client_id, order in orders.items():
        if not order.is_closed and client_id not in restored.pending:
            failures.append(f"Unowned working order: {client_id}")
    for instrument in set(expected) | set(broker_quantities):
        if expected[instrument] != broker_quantities.get(instrument, 0):
            failures.append(
                f"Broker leg mismatch: {instrument}: intent={expected[instrument]}, broker={broker_quantities.get(instrument, 0)}"
            )
    if restored.session_date != today:
        if restored.pending or any(p["units"] for p in restored.positions.values()):
            failures.append("Unresolved previous-session exposure; operator reconciliation required")
        elif not failures:
            restored = StrategySnapshot(
                account=state.account, symbol=state.symbol, session_date=today, candidates=state.candidates
            )
    return restored, failures
