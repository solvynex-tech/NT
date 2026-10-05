from datetime import datetime, time, timedelta
from decimal import Decimal

from nautilus_trader.model import (
    AssetClass,
    Currency,
    FuturesContract,
    InstrumentId,
    OptionContract,
    OptionKind,
    Price,
    Quantity,
    QuoteTick,
    Symbol,
)

from qwork.core.domain import Contract, eastern, parse_date, timestamp_ns


def occ_id(symbol, expiration, strike, right):
    value = Decimal(str(strike)) * 1000
    if len(symbol) > 6 or value != value.to_integral_value():
        raise ValueError("OCC root max 6 characters, strike max 3 decimal places")
    return f"{symbol:<6}{parse_date(expiration):%y%m%d}{'C' if right == 'call' else 'P'}{int(value):08d}"


def ib_spread_id(candidate):
    pieces = [
        ("((2))" if index == 1 else "(1)")
        + occ_id(candidate["legs"][0]["symbol"], candidate["expiration"], strike, candidate["right"])
        for index, strike in enumerate(candidate["strikes"])
    ]
    return InstrumentId.from_str("_".join(pieces) + ".OPRA")


def contract_keys(candidates):
    keys = set()
    for candidate in candidates:
        keys.update((candidate["expiration"], strike, candidate["right"]) for strike in candidate["strikes"])
        keys.add((candidate["expiration"], candidate["strikes"][1], "put" if candidate["right"] == "call" else "call"))
    return sorted(keys)


def sim_option(symbol, key, day):
    expiration, strike, right = key
    contract = Contract(symbol, expiration, strike, right)
    return OptionContract(
        instrument_id=InstrumentId.from_str(contract.instrument_id),
        raw_symbol=Symbol(contract.instrument_id.rsplit(".", 1)[0]),
        asset_class=AssetClass.EQUITY,
        underlying=symbol,
        option_kind=OptionKind.CALL if right == "call" else OptionKind.PUT,
        strike_price=Price.from_str(f"{strike:.3f}"),
        currency=Currency.from_str("USD"),
        activation_ns=timestamp_ns(datetime.combine(parse_date(day) - timedelta(days=1), time(0), eastern())),
        expiration_ns=timestamp_ns(datetime.combine(parse_date(expiration), time(16), eastern())),
        price_precision=2,
        price_increment=Price.from_str("0.01"),
        multiplier=Quantity.from_int(100),
        lot_size=Quantity.from_int(1),
        margin_init=Decimal("1"),
        margin_maint=Decimal("1"),
        ts_event=0,
        ts_init=0,
    )


def sim_spread(candidate, day, index):
    activation = timestamp_ns(datetime.combine(parse_date(day) - timedelta(days=1), time(0), eastern()))
    expiration = timestamp_ns(datetime.combine(parse_date(candidate["expiration"]), time(16), eastern()))
    # NT deliberately does not create positions for spread instruments. Use a
    # multiplier-100 linear accounting proxy for simulated combo prices so its
    # native engine owns cash, commissions and realized PnL. This is never traded live.
    offset = max(candidate["strikes"]) - min(candidate["strikes"]) + 10
    margin_fraction = Decimal(str(candidate["max_loss"] * 1.10 / (offset * 100)))
    return FuturesContract(
        instrument_id=InstrumentId.from_str(f"BWB-{index:03d}.SIM"),
        raw_symbol=Symbol(f"BWB-{index:03d}"),
        asset_class=AssetClass.EQUITY,
        underlying=candidate["legs"][0]["symbol"],
        activation_ns=activation,
        expiration_ns=expiration,
        currency=Currency.from_str("USD"),
        price_precision=2,
        price_increment=Price.from_str("0.01"),
        multiplier=Quantity.from_int(100),
        lot_size=Quantity.from_int(1),
        ts_event=0,
        ts_init=0,
        margin_init=margin_fraction,
        margin_maint=margin_fraction,
        info={"synthetic_combo_accounting_proxy": True, "accounting_price_offset": offset, "legs": candidate["legs"]},
    )


def native_tick(instrument_id, quote, ts=None):
    timestamp = quote.ts_ns if ts is None else ts
    return QuoteTick(
        instrument_id=instrument_id,
        bid_price=Price.from_str(f"{quote.bid:.2f}"),
        ask_price=Price.from_str(f"{quote.ask:.2f}"),
        bid_size=Quantity.from_int(quote.bid_size),
        ask_size=Quantity.from_int(quote.ask_size),
        ts_event=timestamp,
        ts_init=timestamp,
    )
