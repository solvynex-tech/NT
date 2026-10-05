"""QuantLib Greeks and tick-updated roots; trading accounting remains in Nautilus."""

import math

import numpy as np
import QuantLib as ql
from scipy.optimize import brentq

from qwork.analytics.butterfly import probability_of_profit, profit_regions
from qwork.analytics.volatility import implied_vol, maturity


def identity(candidate):
    return "|".join(
        [
            candidate["symbol"] if "symbol" in candidate else candidate["legs"][0]["symbol"],
            candidate["expiration"],
            candidate["right"],
            *[f"{strike:g}" for strike in candidate["strikes"]],
        ]
    )


def quotes_aligned(rows, now_ns, max_age_seconds, max_skew_seconds):
    if not rows or any(row is None for row in rows):
        return False
    if any(
        not 0 <= now_ns - row["ts_ns"] <= max_age_seconds * 1e9
        or row["bid"] < 0
        or row["ask"] <= 0
        or row["ask"] < row["bid"]
        or min(row["bid_size"], row["ask_size"]) <= 0
        for row in rows
    ):
        return False
    stamps = [row["ts_ns"] for row in rows]
    return max(stamps) - min(stamps) <= max_skew_seconds * 1e9


def candidate_gamma(candidate, frame, rate=0.04, dividend_yield=0):
    points = {(p["expiration"], p["strike"], p["right"]): p for p in frame["points"]}
    gamma = 0.0
    for index, strike in enumerate(candidate["strikes"]):
        point = points[(candidate["expiration"], strike, candidate["right"])]
        spot = point["forward"] * math.exp(-(rate - dividend_yield) * point["years"])
        payoff = ql.PlainVanillaPayoff(ql.Option.Call if candidate["right"] == "call" else ql.Option.Put, strike)
        calculator = ql.BlackCalculator(
            payoff, point["forward"], point["iv"] * math.sqrt(point["years"]), math.exp(-rate * point["years"])
        )
        gamma += calculator.gamma(spot) * 100 * (-2 if index == 1 else 1)
    return gamma


def rank_candidates(frame, scan, config):
    result = []
    for candidate in scan["candidates"]:
        if (
            candidate["model_probability"] <= config.min_probability
            or candidate["profit_loss_ratio"] <= config.min_profit_loss_ratio
        ):
            continue
        item = dict(candidate)
        try:
            item["net_gamma"] = candidate_gamma(item, frame, config.rate, config.dividend_yield)
        except (ValueError, RuntimeError, KeyError):
            item["net_gamma"] = None
        item["key"] = identity(item)
        result.append(item)
    if config.rank_gamma:
        result.sort(
            key=lambda item: (item["net_gamma"] is None, abs(item["net_gamma"] or 0), -item["model_probability"])
        )
    else:
        result.sort(key=lambda item: (-item["model_probability"], -item["profit_loss_ratio"]))
    return result


def tick_metrics(
    candidate,
    legs,
    counterpart,
    observed,
    config,
    entry_price=None,
    compute_roots=True,
    liquidation_price=None,
    closing_friction_per_share=None,
):
    """Use a synchronous body call/put pair, avoiding a separate stock feed."""
    from datetime import date, datetime

    from qwork.core.domain import eastern

    years = maturity(date.fromisoformat(candidate["expiration"]), observed)
    if years <= 0:
        raise ValueError("Expired options")
    middle = legs[1]
    middle_mid = (middle["bid"] + middle["ask"]) / 2
    other_mid = (counterpart["bid"] + counterpart["ask"]) / 2
    call_put_difference = middle_mid - other_mid if candidate["right"] == "call" else other_mid - middle_mid
    forward = candidate["strikes"][1] + math.exp(config.rate * years) * call_put_difference
    if forward <= 0:
        raise ValueError("Invalid parity forward")
    spot = forward * math.exp(-(config.rate - config.dividend_yield) * years)
    vols = [
        implied_vol((q["bid"] + q["ask"]) / 2, k, forward, years, config.rate, candidate["right"])
        for k, q in zip(candidate["strikes"], legs, strict=True)
    ]
    entry = legs[0]["ask"] - 2 * legs[1]["bid"] + legs[2]["ask"] + 4 * config.slippage_per_share
    liquidation = legs[0]["bid"] - 2 * legs[1]["ask"] + legs[2]["bid"] - 4 * config.slippage_per_share
    if liquidation_price is not None:
        liquidation = liquidation_price
    cost = (entry if entry_price is None else entry_price) * 100 + 8 * config.commission_per_contract
    maximum_profit, maximum_loss, roots, intervals = profit_regions(candidate["strikes"], candidate["right"], cost)
    expiry_probability = probability_of_profit(intervals, forward, vols[1], years)

    def model_pnl(scenario):
        value = 0.0
        for index, (strike, vol) in enumerate(zip(candidate["strikes"], vols, strict=True)):
            option = ql.Option.Call if candidate["right"] == "call" else ql.Option.Put
            value += (
                ql.blackFormula(
                    option,
                    strike,
                    max(scenario, 1e-8) * forward / spot,
                    vol * math.sqrt(years),
                    math.exp(-config.rate * years),
                )
                * (-2 if index == 1 else 1)
                * 100
            )
        return value - cost

    grid = np.unique([*np.geomspace(max(spot * 0.1, 0.01), spot * 3, 81), *candidate["strikes"], spot])
    values = [model_pnl(float(value)) for value in grid] if compute_roots else []
    realtime_roots = []
    for left, upper, pl, pu in zip(grid[:-1], grid[1:], values[:-1], values[1:], strict=False):
        if pl == 0:
            realtime_roots.append(float(left))
        elif pl * pu < 0:
            realtime_roots.append(float(brentq(model_pnl, float(left), float(upper), xtol=1e-7)))
    plateau = model_pnl(spot * 0.001) if candidate["right"] == "call" else model_pnl(spot * 10)
    pnl = liquidation * 100 - cost
    probability = expiry_probability
    intraday_probability = None
    if config.probability_horizon == "intraday":
        local = observed.astimezone(eastern())
        close_at = datetime.combine(local.date(), config.flatten_time, tzinfo=eastern())
        horizon = max(0, (close_at - local).total_seconds()) / (365 * 86400)
        remaining = max(0, years - horizon)
        closing_spread = sum((q["ask"] - q["bid"]) / 2 * (2 if i == 1 else 1) for i, q in enumerate(legs)) * 100
        closing_friction = closing_spread + 4 * config.slippage_per_share * 100
        if closing_friction_per_share is not None:
            closing_friction = closing_friction_per_share * 100

        def close_pnl(scenario):
            value = 0.0
            for index, (strike, vol) in enumerate(zip(candidate["strikes"], vols, strict=True)):
                option = ql.Option.Call if candidate["right"] == "call" else ql.Option.Put
                value += (
                    ql.blackFormula(
                        option,
                        strike,
                        max(scenario, 1e-8) * math.exp((config.rate - config.dividend_yield) * remaining),
                        vol * math.sqrt(remaining),
                        math.exp(-config.rate * remaining),
                    )
                    * (-2 if index == 1 else 1)
                    * 100
                )
            return value - cost - closing_friction

        roots_at_close = []
        close_values = [close_pnl(float(value)) for value in grid]
        for left, upper, pl, pu in zip(grid[:-1], grid[1:], close_values[:-1], close_values[1:], strict=True):
            if pl == 0:
                roots_at_close.append(float(left))
            elif pl * pu < 0:
                roots_at_close.append(float(brentq(close_pnl, float(left), float(upper), xtol=1e-7)))
        boundaries = [0, *sorted(set(roots_at_close)), math.inf]
        close_intervals = []
        for left, upper in zip(boundaries[:-1], boundaries[1:], strict=True):
            sample = (left + upper) / 2 if math.isfinite(upper) else max(left + spot, spot * 10)
            if close_pnl(sample) > 0:
                close_intervals.append((left, upper))
        intraday_probability = (
            probability_of_profit(
                close_intervals, spot * math.exp((config.rate - config.dividend_yield) * horizon), vols[1], horizon
            )
            if horizon > 0
            else 0
        )
        probability = intraday_probability
    gamma = 0.0
    for index, (strike, vol) in enumerate(zip(candidate["strikes"], vols, strict=True)):
        payoff = ql.PlainVanillaPayoff(ql.Option.Call if candidate["right"] == "call" else ql.Option.Put, strike)
        gamma += (
            ql.BlackCalculator(payoff, forward, vol * math.sqrt(years), math.exp(-config.rate * years)).gamma(spot)
            * 100
            * (-2 if index == 1 else 1)
        )
    return dict(
        spot=spot,
        forward=forward,
        ivs=vols,
        entry_price=entry,
        liquidation_price=liquidation,
        max_profit=maximum_profit,
        max_loss=maximum_loss,
        probability=probability,
        probability_horizon=config.probability_horizon,
        expiration_probability=expiry_probability,
        intraday_probability=intraday_probability,
        ratio=maximum_profit / maximum_loss if maximum_loss > 0 else 0,
        pnl=pnl,
        breakevens=realtime_roots,
        expiry_breakevens=roots,
        plateau_model_profit=plateau,
        expiry_plateau_profit=-cost,
        net_gamma=gamma,
        bid_size=min(legs[0]["bid_size"], legs[1]["ask_size"] // 2, legs[2]["bid_size"]),
        ask_size=min(legs[0]["ask_size"], legs[1]["bid_size"] // 2, legs[2]["ask_size"]),
    )


def exit_decision(position, metrics, config):
    if metrics["pnl"] >= config.take_profit_fraction * position["max_profit_per_unit"]:
        return "take_profit_70"
    body = position["candidate"]["strikes"][1]
    # The exact expiry maximum lies at the body strike. Half the narrow wing
    # defines the explicitly documented maximum-profit zone used for path tracking.
    narrow = min(body - position["candidate"]["strikes"][0], position["candidate"]["strikes"][2] - body)
    if abs(metrics["spot"] - body) <= narrow / 2:
        position["visited_profit_zone"] = True
    previous = position.get("last_spot")
    if not position["visited_profit_zone"] and previous is not None:
        if any(
            min(previous, metrics["spot"]) <= root <= max(previous, metrics["spot"]) for root in metrics["breakevens"]
        ):
            position["breakeven_before_zone"] = True
    away = previous is not None and abs(metrics["spot"] - body) > abs(previous - body)
    position["last_spot"] = metrics["spot"]
    plateau = position["initial_plateau_profit"]
    threshold = plateau * config.plateau_remaining_fraction
    previous_pnl = position.get("last_pnl")
    position["last_pnl"] = metrics["pnl"]
    if (
        position["breakeven_before_zone"]
        and away
        and plateau > 0
        and previous_pnl is not None
        and previous_pnl > threshold
        and metrics["pnl"] <= threshold
    ):
        return "iv_plateau_20"
    return None
