"""Quote liquidation PnL, QuantLib T+0 scenarios and exact intrinsic expiry payoff."""

from __future__ import annotations

import math
from datetime import datetime
from statistics import median

import numpy as np
import QuantLib as ql

from qwork.analytics.volatility import maturity


def expiry_risk(legs, entry_fee=0):
    """Exact piecewise-linear envelope on S >= 0, independent of the plotted range."""
    if len({leg.expiration for leg in legs}) != 1:
        return dict(kind="mixed_expirations")

    def payoff(spot):
        return (
            sum(
                ((max(spot - leg.strike, 0) if leg.right == "call" else max(leg.strike - spot, 0)) - leg.entry_premium)
                * (1 if leg.side == "buy" else -1)
                * leg.quantity
                * leg.multiplier
                for leg in legs
            )
            - entry_fee
        )

    knots = sorted({0, *(leg.strike for leg in legs)})
    values = [payoff(spot) for spot in knots]
    tail = sum((1 if leg.side == "buy" else -1) * leg.quantity * leg.multiplier for leg in legs if leg.right == "call")
    roots, zero_ranges = [], []
    for left, right, low, high in zip(knots[:-1], knots[1:], values[:-1], values[1:], strict=True):
        if abs(low) < 1e-9 and abs(high) < 1e-9:
            zero_ranges.append([left, right])
        elif low * high < 0:
            roots.append(left - low * (right - left) / (high - low))
        else:
            if abs(low) < 1e-9:
                roots.append(left)
            if abs(high) < 1e-9:
                roots.append(right)
    if tail and -values[-1] / tail > 0:
        roots.append(knots[-1] - values[-1] / tail)
    elif not tail and abs(values[-1]) < 1e-9:
        zero_ranges.append([knots[-1], None])
    return dict(
        kind="same_expiration",
        unbounded_profit=tail > 0,
        unbounded_loss=tail < 0,
        max_profit=None if tail > 0 else max(0, max(values)),
        max_loss=None if tail < 0 else max(0, -min(values)),
        breakevens=sorted(set(roots)),
        zero_payoff_ranges=zero_ranges,
    )


def analyze_portfolio(frame, request, volatility_request, portfolio_generation=0):
    result = dict(
        generation=frame["generation"],
        portfolio_generation=portfolio_generation,
        symbol=frame["symbol"],
        updated_at=frame["updated_at"],
        observed_at=frame["observed_at"],
        surface_revision=frame.get("surface_revision"),
        mode=frame["mode"],
        synthetic=frame["synthetic"],
        combinations=[],
        diagnostics=[],
        aggregate=None,
    )
    if not request.combinations:
        result["diagnostics"].append("请输入一个或多个组合；不自动推断或创建持仓")
        return result
    lookup = {(p["expiration"], p["strike"], p["right"]): p for p in frame["points"]}
    observed = datetime.fromisoformat(frame["observed_at"])
    spots = [
        p["forward"] * math.exp(-(volatility_request.rate - volatility_request.dividend_yield) * p["years"])
        for p in frame["points"]
    ]
    # Expiry payoff needs no quotes. Without a price reference, centre its axis on
    # the entered strikes and clearly disclose that this is not a market quote.
    spot = volatility_request.spot or (median(spots) if spots else None)
    reference = spot or median(leg.strike for combo in request.combinations for leg in combo.legs)
    lower, upper = reference * (1 - request.scenario_band), reference * (1 + request.scenario_band)
    strikes = {leg.strike for combo in request.combinations for leg in combo.legs if lower <= leg.strike <= upper}
    grid = np.unique([*np.linspace(lower, upper, request.scenario_points), reference, *strikes])
    if spot is None:
        result["diagnostics"].append("没有可用现价或远期价；到期图横轴暂以输入行权价中位数为中心")
    for combo in request.combinations:
        if combo.legs[0].symbol != frame["symbol"]:
            result["diagnostics"].append(f"{combo.name}: 标的与曲面不一致")
            continue
        selected, missing = [], []
        for leg in combo.legs:
            point = lookup.get((leg.expiration.isoformat(), leg.strike, leg.right))
            if point is None:
                missing.append(f"{leg.expiration} {leg.strike:g} {leg.right}")
            else:
                selected.append((leg, point))
        complete = not missing and spot is not None
        if missing:
            result["diagnostics"].append(f"{combo.name}: 缺少 {', '.join(missing)} 的有效报价；只显示到期图")
        contracts = sum(leg.quantity for leg in combo.legs)
        entry_fee = contracts * request.commission_per_contract
        exit_fee = entry_fee
        debit = sum(
            (1 if leg.side == "buy" else -1) * leg.entry_premium * leg.quantity * leg.multiplier for leg in combo.legs
        )
        expiry = np.zeros(len(grid))
        for leg in combo.legs:
            intrinsic = np.maximum(grid - leg.strike, 0) if leg.right == "call" else np.maximum(leg.strike - grid, 0)
            expiry += intrinsic * (1 if leg.side == "buy" else -1) * leg.quantity * leg.multiplier
        expiry -= debit + entry_fee
        current = None
        mark_pnl = None
        if complete:
            current = np.zeros(len(grid))
            quote_value = 0.0
            for leg, point in selected:
                weight = (1 if leg.side == "buy" else -1) * leg.quantity * leg.multiplier
                quote_value += (point["bid"] if leg.side == "buy" else point["ask"]) * weight
                years = maturity(leg.expiration, observed)
                option_type = ql.Option.Call if leg.right == "call" else ql.Option.Put
                forwards = grid * (point["forward"] / spot)
                values = [
                    ql.blackFormula(
                        option_type,
                        leg.strike,
                        float(f),
                        point["iv"] * math.sqrt(years),
                        math.exp(-volatility_request.rate * years),
                    )
                    for f in forwards
                ]
                current += np.asarray(values) * weight
            current -= debit + entry_fee + exit_fee
            mark_pnl = quote_value - debit - entry_fee - exit_fee
        expirations = sorted({leg.expiration.isoformat() for leg in combo.legs})
        result["combinations"].append(
            dict(
                name=combo.name,
                symbol=frame["symbol"],
                expirations=expirations,
                mixed_expirations=len(expirations) > 1,
                spot=spot,
                axis_reference=reference,
                priced=complete,
                missing_quotes=missing,
                entry_debit=debit,
                entry_fee=entry_fee,
                exit_fee_reserve=exit_fee,
                liquidation_pnl=mark_pnl,
                quote_timestamp=min((p["timestamp"] for _, p in selected), default=None),
                scenario_prices=grid.tolist(),
                current_pnl=current.tolist() if current is not None else None,
                expiry_pnl=expiry.tolist(),
                model="QuantLib Black / sticky-strike IV",
                expiry_risk=expiry_risk(combo.legs, entry_fee),
            )
        )
    rows = result["combinations"]
    if rows:
        complete = all(row["priced"] for row in rows) and len(rows) == len(request.combinations)
        result["aggregate"] = dict(
            scenario_prices=grid.tolist(),
            partial=not complete,
            current_pnl=np.sum([row["current_pnl"] for row in rows], axis=0).tolist() if complete else None,
            expiry_pnl=np.sum([row["expiry_pnl"] for row in rows], axis=0).tolist(),
            liquidation_pnl=sum(row["liquidation_pnl"] for row in rows) if complete else None,
            expiry_risk=expiry_risk(
                [
                    leg
                    for combo in request.combinations
                    if combo.name in {row["name"] for row in rows}
                    for leg in combo.legs
                ],
                sum(row["entry_fee"] for row in rows),
            ),
        )
        result["diagnostics"].extend(
            [
                "当前曲线为固定行权价 IV 的欧式模型情景；可平仓盈亏按买入腿 BID、卖出腿 ASK，扣预计双边佣金",
                "到期曲线扣建仓佣金，不含行权/指派费用、融资成本；美式提前行权不在模型中",
            ]
        )
        if len({expiry for row in rows for expiry in row["expirations"]}) > 1:
            result["diagnostics"].append("多个到期日的到期图是假设各到期日标的价格相同的静态情景；不是完整价格路径模拟")
        if not complete:
            result["diagnostics"].append("部分组合缺少报价：隐藏当前合计，保留各组合到期盈亏，缺失行情不记为零")
    return result
