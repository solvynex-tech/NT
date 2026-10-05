"""Bounded-payoff BWB screening, with explicitly risk-neutral model probabilities."""

from __future__ import annotations

import math
from itertools import combinations

import numpy as np
import QuantLib as ql

from qwork.analytics.volatility import term_structure


def interpolate_iv(points, moneyness):
    rows = sorted(points, key=lambda point: point["moneyness"])
    if not rows or not rows[0]["moneyness"] <= moneyness <= rows[-1]["moneyness"]:
        return None
    return math.sqrt(
        float(
            np.interp(
                math.log(moneyness),
                [math.log(point["moneyness"]) for point in rows],
                [point["iv"] ** 2 for point in rows],
            )
        )
    )


def profit_regions(strikes, right, cost):
    """Piecewise linear expiry PnL: exact break-even roots and positive intervals."""

    def payoff(spot):
        values = [max(spot - strike, 0) if right == "call" else max(strike - spot, 0) for strike in strikes]
        return (values[0] - 2 * values[1] + values[2]) * 100 - cost

    knots = [0, *strikes]
    values = [payoff(spot) for spot in knots]
    roots = []
    for left, upper, pleft, pupper in zip(knots[:-1], knots[1:], values[:-1], values[1:], strict=True):
        if pleft == 0:
            roots.append(left)
        if pleft * pupper < 0:
            roots.append(left - pleft * (upper - left) / (pupper - pleft))
    if values[-1] == 0:
        roots.append(strikes[-1])
    boundaries = sorted({0, *roots}) + [math.inf]
    intervals = []
    for left, upper in zip(boundaries[:-1], boundaries[1:], strict=True):
        test = (left + upper) / 2 if math.isfinite(upper) else left + max(strikes[-1], 1)
        if payoff(test) > 0:
            intervals.append((left, upper))
    return max(values), max(0, -min(values)), sorted(set(roots)), intervals


def probability_of_profit(intervals, forward, volatility, years):
    stddev = volatility * math.sqrt(years)

    def cdf(value):
        if value <= 0:
            return 0.0
        if not math.isfinite(value):
            return 1.0
        return float(ql.blackFormulaCashItmProbability(ql.Option.Put, value, forward, stddev))

    return min(1, max(0, sum(cdf(upper) - cdf(lower) for lower, upper in intervals)))


def scan_butterflies(frame, request, scan_generation=0):
    result = {
        "generation": frame["generation"],
        "scan_generation": scan_generation,
        "symbol": frame["symbol"],
        "updated_at": frame["updated_at"],
        "observed_at": frame.get("observed_at"),
        "surface_revision": frame.get("surface_revision"),
        "synthetic": frame["synthetic"],
        "mode": frame["mode"],
        "candidates": [],
        "diagnostics": [],
        "model": "risk-neutral lognormal / ATM IV",
        "thresholds": request.model_dump(mode="json"),
    }
    if request.symbol and request.symbol != frame["symbol"]:
        result["diagnostics"].append("扫描标的与曲面标的不一致")
        return result
    atm = {(row["expiration"], row["right"]): row for row in term_structure(frame)["rows"]}
    grouped = {}
    for point in frame["points"]:
        if not request.expirations or point["expiration"] in {item.isoformat() for item in request.expirations}:
            grouped.setdefault(point["expiration"], []).append(point)
    for expiration, points in sorted(grouped.items()):
        sides = {right: [point for point in points if point["right"] == right] for right in ("call", "put")}
        left_iv = interpolate_iv(sides["put"], 1 - request.skew_moneyness)
        right_iv = interpolate_iv(sides["call"], 1 + request.skew_moneyness)
        if left_iv is None or right_iv is None or abs(left_iv - right_iv) <= request.minimum_skew:
            result["diagnostics"].append(f"{expiration}: 两侧偏斜数据不足或差异不明显")
            continue
        right = "put" if left_iv > right_iv else "call"
        reference = atm.get((expiration, right))
        if not reference:
            result["diagnostics"].append(f"{expiration}: 缺少可插值的 ATM IV")
            continue
        forward, years, volatility = reference["forward"], reference["dte"] / 365, reference["iv"]
        strikes = sorted(
            sorted(sides[right], key=lambda point: abs(math.log(point["strike"] / forward)))[: request.max_strikes],
            key=lambda point: point["strike"],
        )
        checked, qualified = 0, 0
        for low, middle, high in combinations(strikes, 3):
            lower_wing, upper_wing = middle["strike"] - low["strike"], high["strike"] - middle["strike"]
            if lower_wing <= upper_wing if right == "put" else upper_wing <= lower_wing:
                continue  # The wider loss wing points toward the higher IV wing.
            if (
                abs(middle["strike"] / forward - 1) > request.body_band
                or max(lower_wing, upper_wing) / forward > request.max_wing_fraction
            ):
                continue
            if min(low.get("ask", 0), middle.get("bid", 0), high.get("ask", 0)) <= 0:
                continue
            if min(low.get("ask_size", 0), middle.get("bid_size", 0) / 2, high.get("ask_size", 0)) < request.units:
                continue
            checked += 1
            premium = low["ask"] - 2 * middle["bid"] + high["ask"] + 4 * request.slippage_per_contract
            # Eight contract-sides: opening and a conservative closing-fee reserve.
            fee = 8 * request.commission_per_contract
            cost = premium * 100 + fee
            strike_values = [low["strike"], middle["strike"], high["strike"]]
            maximum_profit, maximum_loss, roots, intervals = profit_regions(strike_values, right, cost)
            if maximum_loss <= 0 or maximum_profit <= 0:
                continue
            ratio = maximum_profit / maximum_loss
            ratio_ok = (
                ratio >= request.target_ratio
                if request.ratio_mode == "at_least"
                else ratio <= request.target_ratio
                if request.ratio_mode == "at_most"
                else abs(ratio - request.target_ratio) <= request.ratio_tolerance
            )
            if not ratio_ok:
                continue
            probability = probability_of_profit(intervals, forward, volatility, years)
            if probability <= request.min_probability:
                continue
            qualified += 1
            result["candidates"].append(
                {
                    "expiration": expiration,
                    "right": right,
                    "strikes": strike_values,
                    "ratio": "1:2:1",
                    "units": request.units,
                    "wider_wing": "downside" if right == "put" else "upside",
                    "lower_wing": lower_wing,
                    "upper_wing": upper_wing,
                    "put_wing_iv": left_iv,
                    "call_wing_iv": right_iv,
                    "skew_difference": abs(left_iv - right_iv),
                    "net_premium_per_share": premium,
                    "estimated_fees": fee * request.units,
                    "entry_cost": cost * request.units,
                    "max_profit": maximum_profit * request.units,
                    "max_loss": maximum_loss * request.units,
                    "profit_loss_ratio": ratio,
                    "model_probability": probability,
                    "historical_win_rate": None,
                    "breakevens": roots,
                    "quote_timestamp": min(point["timestamp"] for point in (low, middle, high)),
                    "forward": forward,
                    "atm_iv": volatility,
                    "legs": [
                        {
                            "symbol": frame["symbol"],
                            "expiration": expiration,
                            "strike": point["strike"],
                            "right": right,
                            "side": "sell" if index == 1 else "buy",
                            "quantity": (2 if index == 1 else 1) * request.units,
                            "price": point["bid"] if index == 1 else point["ask"],
                        }
                        for index, point in enumerate((low, middle, high))
                    ],
                }
            )
        result["diagnostics"].append(
            f"{expiration}: {right.upper()} 偏斜侧，检查 {checked} 个组合，满足条件 {qualified} 个"
        )
    result["candidates"].sort(key=lambda row: (row["model_probability"], row["profit_loss_ratio"]), reverse=True)
    result["candidates"] = result["candidates"][: request.max_candidates]
    result["diagnostics"].append("概率仅为 ATM IV 对数正态风险中性模型估计，不代表历史胜率；无符合组合时返回空列表")
    return result
