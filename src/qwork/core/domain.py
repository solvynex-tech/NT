from __future__ import annotations

import math
import re
from dataclasses import asdict, dataclass
from datetime import UTC, date, datetime
from decimal import Decimal
from functools import lru_cache
from zoneinfo import ZoneInfo

EPOCH = datetime(1970, 1, 1, tzinfo=UTC)
SYMBOL_RE = re.compile(r"^[A-Z][A-Z0-9]{0,9}$")


@lru_cache(maxsize=1)
def eastern() -> ZoneInfo:
    """Lazy loading keeps metadata tools usable before tzdata is installed on Windows."""
    return ZoneInfo("America/New_York")


def timestamp_ns(value: object) -> int:
    """Theta's naive exchange timestamps are Eastern; preserve aware timestamps."""
    if isinstance(value, int):
        if value <= 0:
            raise ValueError("Timestamp must be positive")
        return value
    if hasattr(value, "to_pydatetime"):
        value = value.to_pydatetime()
    if not isinstance(value, datetime):
        value = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    if value.tzinfo is None:
        value = value.replace(tzinfo=eastern())
    delta = value.astimezone(UTC) - EPOCH
    return (delta.days * 86400 + delta.seconds) * 1_000_000_000 + delta.microseconds * 1000


def utc_iso(ns: int) -> str:
    from datetime import timedelta

    return (EPOCH + timedelta(microseconds=ns // 1000)).isoformat()


def parse_date(value: object) -> date:
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    text = str(value)
    if len(text) == 8 and text.isdigit():
        return datetime.strptime(text, "%Y%m%d").date()
    return date.fromisoformat(text[:10])


@dataclass(frozen=True)
class Contract:
    symbol: str
    expiration: str
    strike: float
    right: str
    multiplier: int = 100

    def __post_init__(self) -> None:
        if not SYMBOL_RE.fullmatch(self.symbol):
            raise ValueError("Use an uppercase option root, for example SPY or SPXW")
        parse_date(self.expiration)
        if not math.isfinite(self.strike) or self.strike <= 0:
            raise ValueError("Strike must be finite and positive")
        if self.right not in {"call", "put"}:
            raise ValueError("Right must be call or put")
        if self.multiplier != 100:
            raise ValueError("Only standard 100-multiplier US options are supported")

    @property
    def instrument_id(self) -> str:
        # Stable vendor-neutral ID; IB contract resolution is a separate mapping.
        strike = Decimal(str(self.strike)).normalize()
        return f"{self.symbol}-{self.expiration.replace('-', '')}-{self.right[0].upper()}-{strike:f}.SIM"


@dataclass(frozen=True)
class Quote:
    contract: Contract
    ts_ns: int
    bid: float
    ask: float
    bid_size: int
    ask_size: int

    def __post_init__(self) -> None:
        if not all(math.isfinite(v) for v in (self.bid, self.ask)):
            raise ValueError("Quote prices must be finite")
        if self.ts_ns <= 0 or self.bid < 0 or self.ask <= 0 or self.ask < self.bid:
            raise ValueError("Invalid or crossed NBBO quote")
        if self.bid_size <= 0 or self.ask_size <= 0:
            raise ValueError("Both sides of the quote must have positive size")

    def to_dict(self) -> dict:
        return {
            **asdict(self.contract),
            "instrument_id": self.contract.instrument_id,
            "ts_ns": self.ts_ns,
            "timestamp": utc_iso(self.ts_ns),
            "bid": self.bid,
            "ask": self.ask,
            "bid_size": self.bid_size,
            "ask_size": self.ask_size,
            "mid": round((self.bid + self.ask) / 2, 4),
        }


def normalize_quotes(rows: list[dict], fallback: Contract | None = None) -> tuple[list[Quote], int]:
    quotes: dict[tuple[str, int], Quote] = {}
    rejected = 0
    for row in rows:
        try:
            right = str(row.get("right", fallback.right if fallback else "")).lower()
            right = {"c": "call", "p": "put"}.get(right, right)
            contract = Contract(
                symbol=str(row.get("symbol", row.get("root", fallback.symbol if fallback else ""))).upper(),
                expiration=parse_date(row.get("expiration", fallback.expiration if fallback else "")).isoformat(),
                strike=float(row.get("strike", fallback.strike if fallback else 0)),
                right=right,
            )
            quote = Quote(
                contract,
                timestamp_ns(row.get("timestamp", row.get("ts_ns"))),
                float(row["bid"]),
                float(row["ask"]),
                int(row["bid_size"]),
                int(row["ask_size"]),
            )
            quotes[(contract.instrument_id, quote.ts_ns)] = quote
        except (ValueError, TypeError, KeyError, OverflowError):
            rejected += 1
    return sorted(quotes.values(), key=lambda q: (q.ts_ns, q.contract.instrument_id)), rejected
