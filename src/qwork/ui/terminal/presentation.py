"""Option-risk and audit inspection views; all accounting stays in Nautilus."""

from datetime import datetime

import numpy as np
import plotly.graph_objects as go

from qwork.core.domain import eastern
from qwork.ui.dashboard import empty_figure

from .theme import PALETTE, chart_theme


def portfolio_figure(portfolio, legs=None, combination=None):
    aggregate = (portfolio or {}).get("aggregate")
    if combination:
        row = next((row for row in (portfolio or {}).get("combinations", []) if row["name"] == combination), None)
        if row:
            aggregate = row
    if not aggregate:
        return chart_theme(empty_figure("点击候选组合，查看逐腿报价与盈亏情景"))
    figure = go.Figure()
    prices = aggregate["scenario_prices"]
    figure.add_trace(
        go.Scatter(x=prices, y=aggregate["expiry_pnl"], name="到期盈亏", line=dict(color=PALETTE["accent"]))
    )
    if aggregate.get("current_pnl") is not None:
        figure.add_trace(
            go.Scatter(x=prices, y=aggregate["current_pnl"], name="当前模型情景", line=dict(color=PALETTE["warning"]))
        )
    figure.add_hline(y=0, line_color="#8594ad", line_dash="dot")
    first = (
        next((row for row in portfolio.get("combinations", []) if row["name"] == combination), {})
        if combination
        else next(iter(portfolio.get("combinations", [])), {})
    )
    if first.get("spot"):
        figure.add_vline(x=first["spot"], line_dash="dot", line_color="#8594ad")
    strikes = [float(leg["strike"]) for leg in legs or []]
    if strikes:
        padding = max(max(strikes) - min(strikes), max(strikes) * 0.005)
        references = [*strikes, *([first["spot"]] if first.get("spot") else [])]
        figure.update_xaxes(range=[min(references) - padding, max(references) + padding])
        lower, upper = min(references) - padding, max(references) + padding
        visible_y = []
        for key in ["expiry_pnl", "current_pnl"]:
            values = aggregate.get(key)
            if values is not None:
                visible_y.extend(value for spot, value in zip(prices, values, strict=True) if lower <= spot <= upper)
                visible_y.extend(np.interp([lower, upper], prices, values))
        if visible_y:
            low, high = min(visible_y), max(visible_y)
            gap = max((high - low) * 0.12, 1)
            figure.update_yaxes(range=[low - gap, high + gap])
    figure.update_layout(
        template="plotly_dark",
        paper_bgcolor=PALETTE["panel"],
        plot_bgcolor=PALETTE["canvas"],
        margin=dict(l=55, r=15, t=20, b=35),
        legend=dict(orientation="h", y=1.12),
        xaxis_title="标的价格情景",
        yaxis_title="盈亏 USD · 含模型费用",
        uirevision=str(portfolio.get("portfolio_generation")),
    )
    return chart_theme(figure)


def inspection_time(row):
    if not row:
        return None
    value = row.get("ts_event") or row.get("ts_init") or row.get("timestamp")
    try:
        return (
            datetime.fromtimestamp(value / 1e9, eastern()).isoformat()
            if isinstance(value, int)
            else datetime.fromisoformat(value).astimezone(eastern()).isoformat()
        )
    except (ValueError, TypeError, OverflowError):
        return None


def add_inspection(figure, stamp):
    if stamp:
        value = datetime.fromisoformat(stamp).astimezone(eastern()).replace(tzinfo=None)
        figure.add_shape(
            type="line", x0=value, x1=value, y0=0, y1=1, yref="paper", line=dict(color="#fbbf24", dash="dot")
        )
    return figure
