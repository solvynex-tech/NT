"""QuantLib IV inversion; retain sparse observations rather than inventing a smooth surface."""

from __future__ import annotations

import math
from datetime import datetime, time
from statistics import median

import numpy as np
import QuantLib as ql

from qwork.core.domain import eastern, timestamp_ns


def maturity(expiration, observed_at):
    close = datetime.combine(expiration, time(16), tzinfo=eastern())
    return max(0, (close - observed_at.astimezone(eastern())).total_seconds()) / (365 * 86400)


def estimate_forward(rows, years, request):
    if request.spot:
        return request.spot * math.exp((request.rate - request.dividend_yield) * years), "manual-spot"
    pairs = {}
    for row in rows:
        pairs.setdefault(row["strike"], {})[row["right"]] = row
    candidates = []
    for strike, pair in pairs.items():
        if "call" in pair and "put" in pair and abs(pair["call"]["ts_ns"] - pair["put"]["ts_ns"]) <= 60_000_000_000:
            difference = pair["call"]["mid"] - pair["put"]["mid"]
            forward = strike + math.exp(request.rate * years) * difference
            if forward > 0 and math.isfinite(forward):
                candidates.append((abs(difference), forward))
    if not candidates:
        raise ValueError("缺少同步 Call/Put 配对；可填写标的现价后重新计算")
    return median(value for _, value in sorted(candidates)[:5]), "put-call-parity"


def implied_vol(price, strike, forward, years, rate, right):
    if years <= 0 or price <= 0:
        raise ValueError("到期时间或期权价格无效")
    discount = math.exp(-rate * years)
    option_type = ql.Option.Call if right == "call" else ql.Option.Put
    stddev = ql.blackFormulaImpliedStdDev(option_type, strike, forward, price, discount)
    value = float(stddev) / math.sqrt(years)
    if not math.isfinite(value) or not 0 < value <= 5:
        raise ValueError("隐含波动率无效或超过 500%")
    return value


def calculate_expiration(rows, greeks, expiration, observed_at, request):
    years = maturity(expiration, observed_at)
    if years <= 0:
        return [], len(rows), ["已到期期权不参与曲面"]
    observed_ns = timestamp_ns(observed_at)
    valid = [
        row
        for row in rows
        if 0 <= observed_ns - row["ts_ns"] <= request.max_quote_age_seconds * 1e9
        and row["mid"] > 0
        and (row["ask"] - row["bid"]) / row["mid"] <= request.max_relative_spread
    ]
    rejected = len(rows) - len(valid)
    if not valid:
        return [], rejected, ["没有足够新且买卖价差有效的报价"]
    forward, forward_source = estimate_forward(valid, years, request)
    greek_map = {}
    for row in greeks:
        try:
            right = {"c": "call", "p": "put"}.get(str(row["right"]).lower(), str(row["right"]).lower())
            greek_map[(float(row["strike"]), right)] = row
        except (ValueError, KeyError, TypeError):
            pass
    points = []
    for row in valid:
        strike = row["strike"]
        if abs(strike / forward - 1) > request.strike_band and strike not in request.required_strikes:
            rejected += 1
            continue
        try:
            greek = greek_map.get((strike, row["right"]), {})
            vendor_iv = float(greek.get("implied_vol", 0) or 0)
            greek_ns = timestamp_ns(greek["timestamp"]) if greek.get("timestamp") is not None else 0
            vendor_ok = (
                math.isfinite(vendor_iv)
                and 0 < vendor_iv <= 5
                and 0 <= observed_ns - greek_ns <= request.max_quote_age_seconds * 1e9
            )
            method = "theta" if request.iv_method == "auto" and vendor_ok else "quantlib-european-proxy"
            iv = (
                vendor_iv
                if method == "theta"
                else implied_vol(row["mid"], strike, forward, years, request.rate, row["right"])
            )
            bid_iv = ask_iv = None
            if method != "theta":
                try:
                    bid_iv = implied_vol(row["bid"], strike, forward, years, request.rate, row["right"])
                    ask_iv = implied_vol(row["ask"], strike, forward, years, request.rate, row["right"])
                except (ValueError, RuntimeError):
                    pass
            points.append(
                {
                    "expiration": expiration.isoformat(),
                    "dte": years * 365,
                    "years": years,
                    "strike": strike,
                    "right": row["right"],
                    "iv": iv,
                    "bid_iv": bid_iv,
                    "ask_iv": ask_iv,
                    "forward": forward,
                    "forward_source": forward_source,
                    "moneyness": strike / forward,
                    "timestamp": row["timestamp"],
                    "age_seconds": (observed_ns - row["ts_ns"]) / 1e9,
                    "method": method,
                }
            )
            points[-1].update(
                bid=row["bid"],
                ask=row["ask"],
                mid=row["mid"],
                bid_size=row.get("bid_size", 0),
                ask_size=row.get("ask_size", 0),
            )
        except (ValueError, RuntimeError, TypeError, OverflowError):
            rejected += 1
    return points, rejected, []


def term_structure(frame):
    """ATM forward variance interpolated between bracketing observed strikes per expiry/right."""
    groups = {}
    for point in frame["points"]:
        groups.setdefault((point["expiration"], point["right"]), []).append(point)
    rows = []
    for (expiration, right), points in sorted(groups.items()):
        points = sorted(points, key=lambda point: point["strike"])
        forward = points[0]["forward"]
        if not points[0]["strike"] <= forward <= points[-1]["strike"]:
            continue  # No extrapolation into unobserved strikes.
        lower = max((point for point in points if point["strike"] <= forward), key=lambda point: point["strike"])
        upper = min((point for point in points if point["strike"] >= forward), key=lambda point: point["strike"])
        log_strikes = [math.log(lower["strike"] / forward), math.log(upper["strike"] / forward)]
        variance = float(
            np.interp(0, log_strikes, [lower["iv"] ** 2 * lower["years"], upper["iv"] ** 2 * upper["years"]])
        )
        rows.append(
            {
                "expiration": expiration,
                "right": right,
                "dte": lower["dte"],
                "iv": math.sqrt(variance / lower["years"]),
                "total_variance": variance,
                "forward": forward,
                "lower_strike": lower["strike"],
                "upper_strike": upper["strike"],
                "timestamp": min(lower["timestamp"], upper["timestamp"]),
                "method": lower["method"] if lower["method"] == upper["method"] else "mixed",
                "calendar_variance_warning": False,
            }
        )
    for right in ("call", "put"):
        previous = None
        for row in sorted((row for row in rows if row["right"] == right), key=lambda row: row["dte"]):
            if previous is not None and row["total_variance"] < previous:
                row["calendar_variance_warning"] = True
            previous = row["total_variance"]
    return {
        "generation": frame["generation"],
        "symbol": frame["symbol"],
        "updated_at": frame["updated_at"],
        "observed_at": frame.get("observed_at"),
        "surface_revision": frame.get("surface_revision"),
        "synthetic": frame["synthetic"],
        "mode": frame["mode"],
        "rows": rows,
    }
