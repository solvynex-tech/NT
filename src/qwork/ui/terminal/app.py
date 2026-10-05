"""Dockable EA-style terminal; native execution stays on its owner threads."""

import hashlib
import json
import time
from collections import OrderedDict
from datetime import datetime
from io import BytesIO
from pathlib import Path
from threading import RLock

import dash_ag_grid as dag
import flexlayout_dash as dock
import plotly.graph_objects as go
from dash import Dash, Input, Output, State, ctx, dcc, html, no_update
from dash.exceptions import PreventUpdate
from flask import jsonify, request, send_file

from qwork.core.domain import eastern
from qwork.core.market import market_health, matches_surface
from qwork.ui.dashboard import empty_figure as dashboard_empty_figure
from qwork.ui.dashboard import surface_figure, term_figure

from .combinations import portfolio_request, quote_leg
from .panels import PANELS, dock_model, options_model, restore_model
from .presentation import add_inspection, inspection_time, portfolio_figure
from .rendering import changed_outputs, quiet_outputs
from .theme import GRID_THEME, PALETTE, chart_theme
from .workflow import WORKSPACES, candidate_identity, chain_frame, permissions, strategy_parameters, workspace_chart


def empty_figure(message):
    return chart_theme(dashboard_empty_figure(message))


def grid(key, columns, height="100%"):
    return dag.AgGrid(
        id=key,
        columnDefs=[
            dict(
                field=field,
                headerName=name,
                **(
                    dict(cellClass="terminal-bid")
                    if field in {"bid", "call_bid", "put_bid"}
                    else dict(cellClass="terminal-ask")
                    if field in {"ask", "call_ask", "put_ask"}
                    else {}
                ),
                **(
                    dict(
                        minWidth=110 if field == "contract" else 58, flex=2 if field == "contract" else 1, filter=False
                    )
                    if key == "t-watch-grid"
                    else {}
                ),
                **({"cellDataType": False} if field == "value" else {}),
                **(
                    dict(
                        minWidth=62,
                        hide=field not in {"call_bid", "call_ask", "call_iv", "strike", "put_iv", "put_bid", "put_ask"},
                    )
                    if key == "t-chain-grid"
                    else {}
                ),
                **(
                    dict(
                        cellDataType="number",
                        valueFormatter={
                            "function": "params.value == null ? '' : Number(params.value).toLocaleString('en-US', {maximumFractionDigits:4})"
                        },
                    )
                    if key in {"t-chain-grid", "t-watch-grid"}
                    and field
                    in {"bid", "ask", "strike", "call_bid", "call_ask", "call_iv", "put_bid", "put_ask", "put_iv"}
                    else dict(tooltipField=field)
                    if key in {"t-chain-grid", "t-watch-grid"}
                    else {}
                ),
            )
            for field, name in columns
        ],
        rowData=[],
        getRowId="params.data.candidate_id"
        if key == "t-candidates"
        else "params.data.expiration + '-' + params.data.strike"
        if key == "t-chain-grid"
        else "params.data.symbol + '|' + params.data.expiration + '|' + params.data.strike + '|' + params.data.right"
        if key == "t-watch-grid"
        else None,
        defaultColDef=dict(resizable=True, sortable=True, filter=True, minWidth=95, flex=1),
        dashGridOptions=dict(
            rowSelection={
                "mode": "singleRow",
                "enableClickSelection": True,
                "checkboxes": False,
                "headerCheckbox": False,
            },
            animateRows=False,
            rowBuffer=5,
            localeText={"noRowsToShow": "暂无数据", "loadingOoo": "加载中…", "searchOoo": "筛选…"},
            theme=GRID_THEME,
        ),
        className="terminal-grid",
        style={"height": height, "width": "100%"},
    )


def field(name, component):
    return html.Label([html.Span(name), component], className="terminal-field")


def choice(key, values, default):
    options = [dict(label=label, value=value) for label, value in values]
    return dcc.Dropdown(id=key, options=options, value=default, clearable=False)


def number(key, value, minimum=1, maximum=None):
    return dcc.Input(id=key, type="number", value=value, min=minimum, max=maximum)


def button(name, key, primary=False):
    return html.Button(
        name,
        id=key,
        className="terminal-primary" if primary else "",
        disabled=key
        in {
            "t-strategy-review",
            "t-strategy-start",
            "t-backtest-start",
            "t-pause",
            "t-play",
            "t-step",
            "t-backtest-stop",
            "t-report-load",
        },
    )


def panel(children):
    return html.Div(children, className="terminal-panel terminal-scroll")


def candle_figure(chart):
    rows = chart.get("rows", [])
    if not rows:
        figure = empty_figure(chart.get("status", "请选择合约"))
        figure.update_xaxes(visible=False)
        figure.update_yaxes(visible=False)
        return figure
    figure = go.Figure(
        go.Candlestick(
            x=[datetime.fromisoformat(r["timestamp"]).astimezone(eastern()).replace(tzinfo=None) for r in rows],
            open=[r["open"] for r in rows],
            high=[r["high"] for r in rows],
            low=[r["low"] for r in rows],
            close=[r["close"] for r in rows],
            increasing_line_color=PALETTE["up"],
            increasing_fillcolor=PALETTE["up"],
            decreasing_line_color=PALETTE["down"],
            decreasing_fillcolor=PALETTE["down"],
            name="报价中点",
        )
    )
    figure.update_layout(
        template="plotly_dark",
        paper_bgcolor=PALETTE["panel"],
        plot_bgcolor=PALETTE["canvas"],
        xaxis_rangeslider_visible=False,
        margin=dict(l=15, r=66, t=18, b=34),
        uirevision=chart.get("status"),
        yaxis_title="期权报价 / 股",
        xaxis_title="纽约时间",
    )
    figure.update_yaxes(side="right", title_text="报价 / 股")
    figure.update_xaxes(showgrid=False)
    figure.add_hline(y=rows[-1]["close"], line_color=PALETTE["warning"], line_width=1, line_dash="dot")
    figure.add_annotation(
        x=1,
        xref="paper",
        y=rows[-1]["close"],
        yref="y",
        text=f"{rows[-1]['close']:.4f}".rstrip("0").rstrip("."),
        xanchor="left",
        showarrow=False,
        bgcolor=PALETTE["warning"],
        font=dict(color="#202124", size=10),
        borderpad=3,
    )
    return chart_theme(figure)


def chain_rows(frame, expiration):
    rows = {}
    for point in (frame or {}).get("points", []):
        if expiration and point["expiration"] != expiration:
            continue
        key = (point["expiration"], point["strike"])
        row = rows.setdefault(key, dict(expiration=key[0], strike=key[1]))
        prefix = point["right"]
        for name in ["bid", "ask", "bid_size", "ask_size", "timestamp"]:
            row[f"{prefix}_{name}"] = point.get(name)
        bid, ask = point.get("bid"), point.get("ask")
        row[f"{prefix}_spread"] = round(ask - bid, 4) if bid is not None and ask is not None and ask >= bid else None
        row[f"{prefix}_iv"] = round(point["iv"] * 100, 2) if point.get("iv") is not None else None
    return [rows[key] for key in sorted(rows)]


def nearby_chain_rows(rows, strike, count):
    if not count or not rows:
        return rows
    anchor = float(strike) if strike is not None else rows[len(rows) // 2]["strike"]
    selected = sorted(rows, key=lambda row: (abs(row["strike"] - anchor), row["strike"]))[: int(count)]
    return sorted(selected, key=lambda row: (row["expiration"], row["strike"]))


def watch_rows(frame, expiration, strike):
    """Show real nearby option contracts, never an inferred stock quote."""
    points = [point for point in (frame or {}).get("points", []) if point["expiration"] == expiration]
    strikes = sorted({point["strike"] for point in points}, key=lambda value: abs(value - float(strike or 0)))[:4]
    rows = []
    for point in sorted(points, key=lambda point: (point["strike"], point["right"])):
        if point["strike"] not in strikes:
            continue
        rows.append(
            dict(
                symbol=frame["symbol"],
                expiration=point["expiration"],
                strike=point["strike"],
                right=point["right"],
                contract=f"{frame['symbol']} {point['expiration'][5:]} {point['strike']:g}{point['right'][0].upper()}",
                bid=point.get("bid"),
                ask=point.get("ask"),
                timestamp=point.get("timestamp"),
            )
        )
    return rows


def iv_figure(frame, right, mode):
    if mode == "3d":
        return chart_theme(surface_figure(frame or None, right))
    points = [point for point in (frame or {}).get("points", []) if point["right"] == right]
    if not points:
        return empty_figure("等待波动率数据")
    expirations = sorted({point["expiration"] for point in points})
    strikes = sorted({point["strike"] for point in points})
    values = {(point["expiration"], point["strike"]): point["iv"] * 100 for point in points}
    figure = go.Figure(
        go.Heatmap(
            x=strikes,
            y=expirations,
            z=[[values.get((expiration, strike)) for strike in strikes] for expiration in expirations],
            colorscale="Viridis",
            colorbar=dict(title="IV %"),
            hoverongaps=False,
            hovertemplate="到期 %{y}<br>行权价 %{x}<br>IV %{z:.2f}%<extra></extra>",
        )
    )
    figure.update_layout(
        template="plotly_dark",
        paper_bgcolor=PALETTE["panel"],
        plot_bgcolor=PALETTE["canvas"],
        xaxis_title="行权价",
        yaxis_title="到期日",
        yaxis=dict(type="category", categoryorder="array", categoryarray=expirations),
        margin=dict(l=85, r=55, t=20, b=40),
        uirevision=f"{frame['symbol']}-{right}-heatmap",
    )
    return chart_theme(figure)


def create_terminal(backend):
    snapshot_lock = RLock()
    snapshots = OrderedDict()
    revision = 0

    def read_snapshot(marker):
        with snapshot_lock:
            key = (marker or {}).get("revision")
            if key is None:
                return {}
            if key not in snapshots:
                raise PreventUpdate
            return snapshots[key]

    app = Dash(
        __name__, title="QWork · 期权交易终端", assets_folder=str(Path(__file__).parent / "assets"), update_title=None
    )
    frame = backend.cached_frame or {}
    expirations = frame.get("expirations", [])
    default_expiration = next(iter(expirations), None)
    default_candidate = next(iter((backend.cached_scan or {}).get("candidates", [])), None)
    if default_candidate:
        default_expiration = default_candidate["expiration"]
    points = [p for p in frame.get("points", []) if p["expiration"] == default_expiration and p["right"] == "put"]
    default_strike = min(points, key=lambda p: abs(p.get("moneyness", 1) - 1))["strike"] if points else 0
    if default_candidate:
        default_strike = default_candidate["strikes"][1]
    market = panel(
        [
            html.Div(
                [
                    dcc.Input(
                        id="t-symbol",
                        value=frame.get("symbol", "SPY"),
                        debounce=True,
                        placeholder="标的",
                        name="symbol",
                    ),
                    button("刷新", "t-market-refresh", True),
                ],
                className="terminal-watch-search",
            ),
            html.Div(id="t-watch-source", className="terminal-note"),
            grid("t-watch-grid", [("contract", "期权合约"), ("bid", "BID"), ("ask", "ASK")], "190px"),
            html.Details(
                [
                    html.Summary("行情设置"),
                    field(
                        "行情模式",
                        choice("t-market-mode", [("历史快照", "historical"), ("Theta 实时快照", "live")], "historical"),
                    ),
                    field("交易日期 · 纽约", dcc.DatePickerSingle(id="t-day", date=backend.default_day)),
                    field("快照时间 · 纽约", dcc.Input(id="t-at", value="09:45:00", debounce=True)),
                ]
            ),
            html.Div(id="t-market-action", className="terminal-note"),
            html.Details(
                [
                    html.Summary("期权合约设置"),
                    field(
                        "到期日",
                        dcc.Dropdown(id="t-expiration", options=expirations, value=default_expiration, clearable=False),
                    ),
                    field("行权价", number("t-strike", default_strike, 0)),
                    field("方向", choice("t-right", [("PUT", "put"), ("CALL", "call")], "put")),
                    button("加载 K 线", "t-chart-load"),
                ]
            ),
            html.Div(id="t-chart-action", className="terminal-note"),
        ]
    )
    view_commands = {}

    def panel_link(label, key, panel_id):
        view_commands[key] = panel_id
        return html.Button(label, id=key, className="terminal-panel-link", **{"data-panel": panel_id})

    navigator = panel(
        [
            html.Div(id="t-nav-context", className="terminal-note"),
            html.Details([html.Summary("账户"), panel_link("IBKR / SIM 账户", "t-nav-account", "account")], open=True),
            html.Details(
                [html.Summary("Expert Advisors"), panel_link("破翼蝴蝶 · EA 管理", "t-nav-strategy", "strategy")],
                open=True,
            ),
            html.Details(
                [
                    html.Summary("分析 Actors"),
                    panel_link("波动率曲面", "t-nav-surface", "surface"),
                    panel_link("期限结构", "t-nav-term", "term"),
                    panel_link("组合风险与盈亏", "t-nav-analysis", "analysis"),
                ],
                open=True,
            ),
            html.Details(
                [
                    html.Summary("研究与测试"),
                    panel_link("策略测试器", "t-nav-backtest", "backtest"),
                    panel_link("净值与回测结果", "t-nav-equity", "equity"),
                    panel_link("策略日志", "t-nav-events", "events"),
                ]
            ),
        ]
    )
    navigator.className += " terminal-navigator"
    strategy = panel(
        [
            html.Div(
                [html.Span("BWB", className="terminal-tag"), html.Strong("破翼蝴蝶 · 日内")],
                className="terminal-strategy-title",
            ),
            html.Div(id="t-trade-context", className="terminal-context"),
            html.Div(
                [
                    button("审阅并启动 EA", "t-strategy-review", True),
                    html.Button("停止开仓并平仓", id="t-strategy-stop", className="terminal-danger", disabled=True),
                ],
                className="terminal-buttons terminal-ea-actions",
            ),
            field("账户编号", dcc.Input(id="t-account-id", placeholder="DU… / 实盘账户", debounce=True)),
            button("连接账户 · 只读监看", "t-account-connect"),
            button("断开监看", "t-account-disconnect"),
            button("恢复此账户后台监看", "t-strategy-attach"),
            field(
                "交易环境", choice("t-trade-mode", [("Paper 模拟账户", "paper"), ("Live 实盘账户", "live")], "paper")
            ),
            field("EA 标的 · 独立配置", dcc.Input(id="t-ea-symbol", value=frame.get("symbol", "SPY"), debounce=True)),
            html.Div(
                [
                    field("保证金预算 USD", number("t-margin", 10000)),
                    field("最大并发组合", number("t-max-positions", 3, 1, 20)),
                ],
                className="terminal-two-fields",
            ),
            html.Details(
                [
                    field(
                        "开仓执行",
                        choice("t-execution", [("中点限价等待", "passive"), ("自然 ASK 限价", "natural")], "passive"),
                    ),
                    html.Div(
                        [
                            field("当日亏损限制 USD", number("t-loss", 500)),
                            field("净值回撤限制 USD", number("t-drawdown", 1000)),
                        ],
                        className="terminal-two-fields",
                    ),
                    html.P("账户级亏损 / 回撤限制用于 EA；研究回测尚未模拟这两项账户风控。", className="terminal-note"),
                    html.Summary("执行与账户风控参数"),
                ]
            ),
            html.Div(id="t-strategy-action", className="terminal-note"),
            html.Div(id="t-strategy-status", className="terminal-note"),
            html.Div(id="t-strategy-detail", className="terminal-status-card"),
            html.Details([html.Summary("当前进程生效参数"), html.Pre(id="t-running-config")]),
            html.Details(
                [
                    html.Summary("EA 组合与委托进度"),
                    grid(
                        "t-ea-combos",
                        [
                            ("key", "组合"),
                            ("state", "生命周期"),
                            ("units", "意图单位"),
                            ("entry_price", "开仓价"),
                            ("close_reason", "退出原因"),
                        ],
                        "180px",
                    ),
                ]
            ),
            html.P("平仓以券商成交为准。关闭窗口不会强杀有持仓的策略进程。", className="terminal-note"),
        ]
    )
    account = panel(
        [
            dcc.RadioItems(
                id="t-account-source",
                options=[
                    dict(label="IBKR", value="broker", disabled=True),
                    dict(label="回测", value="replay", disabled=True),
                ],
                value="broker",
                inline=True,
            ),
            html.Div(id="t-account-status", className="terminal-note"),
            grid("t-account-grid", [("field", "账户指标"), ("value", "值")], "calc(100% - 62px)"),
        ]
    )
    chart = panel(
        [
            html.Div(
                [html.Strong(id="t-chart-title"), html.Span(id="t-chart-source", className="terminal-note")],
                className="terminal-chart-header",
            ),
            dcc.Graph(
                id="t-chart",
                responsive=True,
                figure=empty_figure("选择期权并加载 K 线"),
                style={"height": "calc(100% - 46px)"},
                config={"displaylogo": False, "scrollZoom": True},
            ),
        ]
    )
    chain = panel(
        [
            html.Div(
                [
                    html.Span("到期日"),
                    choice("t-chain-expiration", [(day, day) for day in expirations], default_expiration),
                    choice("t-chain-range", [("全部行权价", 0), ("所选价附近 20 档", 20), ("所选价附近 40 档", 40)], 0),
                    choice(
                        "t-chain-columns",
                        [("核心报价", "core"), ("报价及流动性", "liquidity"), ("完整明细", "all")],
                        "core",
                    ),
                    dcc.Checklist(
                        id="t-chain-select-mode", options=[dict(label="组合选腿", value="legs")], value=["legs"]
                    ),
                    dcc.Input(
                        id="t-builder-name", value="组合 A", placeholder="组合名称", debounce=True, persistence=True
                    ),
                    button("加入买入腿", "t-leg-buy"),
                    button("加入卖出腿", "t-leg-sell"),
                    panel_link("组合篮子", "t-view-builder", "analysis"),
                    button("清除筛选", "t-chain-clear"),
                    html.Button("导出当前链 CSV", id="t-chain-export", disabled=True),
                    html.Span(id="t-chain-basket-count"),
                ],
                className="terminal-chain-toolbar",
            ),
            dcc.RadioItems(
                id="t-chain-source",
                options=[
                    dict(label="行情快照", value="market", disabled=True),
                    dict(label="回放报价", value="replay", disabled=True),
                ],
                value="market",
                inline=True,
            ),
            html.Div(id="t-chain-status", className="terminal-note"),
            html.Div(id="t-chain-selection", className="terminal-selection", role="status"),
            html.Div(id="t-chain-action", className="terminal-note", role="status"),
            html.Div(id="t-export-status", className="terminal-note", role="status"),
            grid(
                "t-chain-grid",
                [
                    ("call_bid", "Call BID"),
                    ("call_ask", "Call ASK"),
                    ("call_iv", "Call IV %"),
                    ("strike", "行权价"),
                    ("put_iv", "Put IV %"),
                    ("put_bid", "Put BID"),
                    ("put_ask", "Put ASK"),
                    ("expiration", "到期日"),
                    ("call_timestamp", "Call 报价时间"),
                    ("put_timestamp", "Put 报价时间"),
                    ("call_bid_size", "Call BID量"),
                    ("call_ask_size", "Call ASK量"),
                    ("call_spread", "Call 价差"),
                    ("put_bid_size", "Put BID量"),
                    ("put_ask_size", "Put ASK量"),
                    ("put_spread", "Put 价差"),
                ],
                "100%",
            ),
        ]
    )
    chain.className = "terminal-panel terminal-chain-panel"
    backtest = panel(
        [
            html.Details(
                [
                    html.Summary("回测配置 · 与 EA 参数独立"),
                    html.Div(
                        [
                            field(
                                "回测标的", dcc.Input(id="t-bt-symbol", value=frame.get("symbol", "SPY"), debounce=True)
                            ),
                            field("回测日期 · 纽约", dcc.DatePickerSingle(id="t-bt-day", date=backend.default_day)),
                            field("初始预算 USD", number("t-bt-margin", 10000)),
                            field("并发组合", number("t-bt-positions", 3, 1, 20)),
                            field(
                                "开仓执行",
                                choice(
                                    "t-bt-execution",
                                    [("中点限价等待", "passive"), ("自然 ASK 限价", "natural")],
                                    "passive",
                                ),
                            ),
                        ],
                        className="terminal-config-grid",
                    ),
                ],
                open=False,
            ),
            html.Div(
                [
                    field(
                        "概率口径",
                        choice("t-horizon", [("日内模型", "intraday"), ("到期概率对照", "expiration")], "intraday"),
                    ),
                    field("候选池", number("t-universe", 6, 1, 40)),
                    field(
                        "播放速度",
                        choice("t-speed", [("最大速度", 0), *[(f"{n}×", n) for n in [1, 60, 300, 600, 3600]]], 600),
                    ),
                    button("开始回测", "t-backtest-start", True),
                    button("暂停", "t-pause"),
                    button("继续", "t-play"),
                    button("单步", "t-step"),
                    button("终止", "t-backtest-stop"),
                ],
                className="terminal-test-toolbar",
            ),
            html.Div(id="t-backtest-action", className="terminal-note"),
            html.Div(id="t-replay-status", className="terminal-replay-status"),
            html.Div(
                [
                    dcc.Dropdown(
                        id="t-saved-run",
                        options=[
                            dict(label=f"{r['date']} · {r['name']} · {r['status']}", value=r["name"])
                            for r in backend.list_runs()
                        ],
                        placeholder="已保存回测报告",
                    ),
                    button("查看报告", "t-report-load"),
                    html.A(
                        "导出成交 CSV",
                        id="t-export",
                        href="/api/replay/fills.csv",
                        download="qwork-fills.csv",
                        className="terminal-download",
                    ),
                ],
                className="terminal-report-toolbar",
            ),
            html.Div(id="t-backtest-config-summary", className="terminal-context"),
            html.Div(id="t-inspection-label", className="terminal-note"),
            grid(
                "t-fills-grid",
                [
                    ("instrument_id", "成交组合"),
                    ("side", "方向"),
                    ("last_qty", "成交量"),
                    ("combo_premium", "组合权利金"),
                    ("commission", "费用"),
                    ("timestamp", "纽约时间"),
                ],
                "170px",
            ),
        ]
    )
    analysis = panel(
        [
            html.Details(
                [
                    html.Summary("多腿组合构建 · 分析草稿"),
                    html.Details(
                        [
                            html.Summary("操作说明"),
                            html.P(
                                "在期权链选择 Call / Put 报价后加入买入或卖出腿。相同名称合并为一组；更改名称可加入另一组。张数、买卖和每股成本可编辑，点击分析后应用。",
                                className="terminal-note",
                            ),
                        ],
                        className="terminal-builder-help",
                    ),
                    dag.AgGrid(
                        id="t-builder-legs",
                        rowData=[],
                        persistence=True,
                        persisted_props=["rowData"],
                        persistence_type="local",
                        getRowId="params.data.leg_id",
                        columnDefs=[
                            dict(
                                field=key,
                                headerName=label,
                                editable=editable,
                                **(
                                    dict(cellEditor="agSelectCellEditor", cellEditorParams={"values": ["buy", "sell"]})
                                    if key == "side"
                                    else {}
                                ),
                            )
                            for key, label, editable in [
                                ("combo_name", "组合", True),
                                ("expiration", "到期日", False),
                                ("right", "Call / Put", False),
                                ("strike", "行权价", False),
                                ("side", "买卖", True),
                                ("quantity", "张数", True),
                                ("entry_premium", "每股成本 USD", True),
                                ("quote_timestamp", "参考报价时间", False),
                            ]
                        ],
                        defaultColDef=dict(resizable=True, minWidth=85, flex=1),
                        dashGridOptions=dict(
                            theme=GRID_THEME,
                            animateRows=False,
                            rowSelection={"mode": "singleRow", "checkboxes": False, "enableClickSelection": True},
                        ),
                        className="terminal-grid",
                        style={"height": "115px"},
                    ),
                    html.Div(
                        [
                            field(
                                "每张单边佣金 USD",
                                dcc.Input(
                                    id="t-builder-fee", type="number", value=0.65, min=0, max=100, persistence=True
                                ),
                            ),
                            button("分析全部组合", "t-builder-apply", True),
                            button("移除所选腿", "t-builder-remove"),
                            button("清空草稿", "t-builder-clear"),
                        ],
                        className="terminal-builder-toolbar",
                    ),
                    html.Div(id="t-builder-status", className="terminal-note"),
                ],
                open=False,
                id="t-builder-panel",
            ),
            html.Details(
                [
                    html.Summary("候选组合与分析数量"),
                    html.Div(
                        [
                            html.Strong("候选组合 · 分析操作不下单"),
                            field("分析组数", number("t-analysis-units", 1, 1, 100)),
                        ],
                        className="terminal-analysis-toolbar",
                    ),
                    grid(
                        "t-candidates",
                        [
                            ("expiration", "到期日"),
                            ("right", "方向"),
                            ("strikes", "1 : −2 : 1 行权价"),
                            ("model_probability_percent", "到期模型 POP %"),
                            ("profit_loss_ratio", "到期盈亏比"),
                            ("max_profit", "最大盈利"),
                            ("max_loss", "最大亏损"),
                        ],
                        "125px",
                    ),
                ],
                id="t-analysis-picker",
                open=True,
            ),
            html.Div(id="t-analysis-action", className="terminal-note"),
            choice("t-analysis-combination", [("全部组合合计", "aggregate")], "aggregate"),
            html.Div(id="t-analysis-summary", className="terminal-context"),
            dcc.Graph(
                id="t-combo-pnl",
                responsive=True,
                figure=empty_figure("选择候选组合"),
                style={"height": "200px"},
                config={"displaylogo": False},
            ),
            grid(
                "t-combo-legs",
                [
                    ("side", "买卖"),
                    ("expiration", "到期"),
                    ("right", "方向"),
                    ("strike", "行权价"),
                    ("quantity", "合约数"),
                    ("entry_premium", "分析建仓报价"),
                ],
                "105px",
            ),
            html.Div(id="t-analysis-diagnostics", className="terminal-note"),
        ]
    )
    analysis.children = [
        analysis.children[3],
        analysis.children[4],
        html.Div(id="t-analysis-risk", className="terminal-risk-summary", role="status"),
        html.Div(id="t-builder-dirty", className="terminal-draft-state", role="status"),
        analysis.children[-1],
        analysis.children[5],
        analysis.children[0],
        analysis.children[1],
        analysis.children[2],
        html.Details([html.Summary("已应用组合逐腿明细"), analysis.children[6]], open=False),
    ]
    analysis.className = "terminal-panel terminal-analysis-panel"
    sources = [
        dict(label="IBKR 实际账户", value="broker", disabled=True),
        dict(label="回测模拟账户", value="replay", disabled=True),
    ]
    positions = panel(
        [
            dcc.RadioItems(
                id="t-position-source",
                options=[*sources],
                value="replay",
                inline=True,
            ),
            grid(
                "t-positions-grid",
                [
                    ("instrument_id", "合约 / 模拟组合"),
                    ("side", "方向"),
                    ("quantity", "张数 / 组数"),
                    ("avg_px_open", "开仓均价"),
                    ("realized_pnl", "已实现盈亏"),
                    ("ts_opened", "开仓时间"),
                    ("state", "EA 状态"),
                    ("close_reason", "退出原因"),
                    ("underlying", "标的"),
                    ("expiration", "到期日"),
                    ("right", "Call / Put"),
                    ("strike", "行权价"),
                    ("multiplier", "合约乘数"),
                    ("contract_type", "期权腿 / 组合"),
                ],
                "calc(100% - 32px)",
            ),
        ]
    )
    orders = panel(
        [
            dcc.RadioItems(id="t-order-source", options=sources, value="replay", inline=True),
            grid(
                "t-orders-grid",
                [
                    ("client_order_id", "委托号"),
                    ("instrument_id", "合约 / 组合"),
                    ("side", "方向"),
                    ("type", "类型"),
                    ("quantity", "委托量"),
                    ("filled_qty", "成交量"),
                    ("price", "限价"),
                    ("status", "状态"),
                    ("underlying", "标的"),
                    ("expiration", "到期日"),
                    ("right", "Call / Put"),
                    ("strike", "行权价"),
                    ("contract_type", "期权腿 / 组合"),
                ],
                "calc(100% - 32px)",
            ),
        ]
    )
    equity = panel(
        [
            html.Div(id="t-result-summary", className="terminal-note"),
            dcc.Graph(
                id="t-equity",
                responsive=True,
                figure=empty_figure("运行回测后显示净值"),
                style={"height": "calc(100% - 36px)"},
                config={"displaylogo": False},
            ),
        ]
    )
    events = panel(
        [
            dcc.RadioItems(
                id="t-event-source",
                options=[
                    dict(label="EA", value="strategy", disabled=True),
                    dict(label="回测", value="replay", disabled=True),
                ],
                value="replay",
                inline=True,
            ),
            grid(
                "t-events-grid",
                [
                    ("timestamp", "时间"),
                    ("kind", "事件"),
                    ("key", "组合"),
                    ("reason", "原因"),
                    ("failures", "恢复差异"),
                ],
            ),
        ]
    )
    surface = panel(
        [
            html.Div(
                [
                    choice("t-surface-mode", [("二维热力图", "heatmap"), ("三维曲面", "3d")], "heatmap"),
                    choice("t-surface-right", [("PUT IV", "put"), ("CALL IV", "call")], "put"),
                ],
                className="terminal-two-fields",
            ),
            dcc.Graph(
                id="t-surface",
                responsive=True,
                figure=empty_figure("等待曲面"),
                style={"height": "calc(100% - 40px)"},
                config={"displaylogo": False},
            ),
        ]
    )
    term = panel(
        [
            html.Div(id="t-term-source", className="terminal-context"),
            dcc.Graph(
                id="t-term",
                responsive=True,
                figure=empty_figure("等待期限结构 Actor"),
                style={"height": "calc(100% - 40px)"},
                config={"displaylogo": False},
            ),
        ]
    )
    ea = strategy.children
    strategy.children = [
        html.Div(
            [
                *ea[:3],
                html.Div([ea[3], ea[7], ea[8], ea[9]], className="terminal-ea-config-grid"),
                html.Div(ea[4:7], className="terminal-buttons"),
                ea[10],
            ],
            className="terminal-tester-controls",
        ),
        html.Div(ea[11:], className="terminal-ea-reports"),
    ]
    strategy.className = "terminal-panel terminal-ea-workspace"
    backtest.children = [
        html.Div(backtest.children[:4], className="terminal-tester-controls"),
        html.Div(backtest.children[4:], className="terminal-tester-results"),
    ]
    backtest.className = "terminal-panel terminal-tester"
    contents = dict(
        market=market,
        navigator=navigator,
        chart=chart,
        chain=chain,
        strategy=strategy,
        account=account,
        positions=positions,
        orders=orders,
        backtest=backtest,
        equity=equity,
        events=events,
        surface=surface,
        analysis=analysis,
        term=term,
    )
    app.layout = html.Div(
        [
            html.Header(
                [
                    html.Div(
                        [
                            html.Span("Q", className="terminal-logo"),
                            html.Strong("QWork · Options"),
                        ]
                    ),
                    html.Nav(
                        [
                            html.Details(
                                [
                                    html.Summary("文件"),
                                    html.Div(
                                        [
                                            button("保存当前布局", "t-layout-save"),
                                            button("恢复已保存布局", "t-layout-restore"),
                                            button("MT5 默认布局", "t-layout-reset"),
                                            button("期权交易布局", "t-layout-options"),
                                        ],
                                        className="terminal-menu-popup",
                                    ),
                                ],
                                className="terminal-menu",
                            ),
                            html.Details(
                                [
                                    html.Summary("视图"),
                                    html.Div(
                                        [
                                            panel_link("市场报价 · Ctrl+M", "t-menu-market", "market"),
                                            panel_link("导航器 · Ctrl+N", "t-menu-navigator", "navigator"),
                                            panel_link("交易工具箱 · Ctrl+T", "t-menu-toolbox", "positions"),
                                            panel_link("期权链", "t-menu-chain", "chain"),
                                            panel_link("账户", "t-menu-account", "account"),
                                        ],
                                        className="terminal-menu-popup",
                                    ),
                                ],
                                className="terminal-menu",
                            ),
                            html.Details(
                                [
                                    html.Summary("图表"),
                                    html.Div(
                                        [
                                            panel_link("期权 K 线", "t-menu-chart", "chart"),
                                            panel_link("波动率曲面", "t-menu-surface", "surface"),
                                            panel_link("期限结构", "t-menu-term", "term"),
                                            panel_link("组合风险与盈亏", "t-menu-analysis", "analysis"),
                                        ],
                                        className="terminal-menu-popup",
                                    ),
                                ],
                                className="terminal-menu",
                            ),
                            html.Details(
                                [
                                    html.Summary("工具"),
                                    html.Div(
                                        [
                                            panel_link("策略测试器 · Ctrl+R", "t-menu-backtest", "backtest"),
                                            panel_link("EA 管理", "t-menu-strategy", "strategy"),
                                            panel_link("净值与回测结果", "t-menu-equity", "equity"),
                                            panel_link("策略日志", "t-menu-events", "events"),
                                        ],
                                        className="terminal-menu-popup",
                                    ),
                                ],
                                className="terminal-menu",
                            ),
                            html.Details(
                                [
                                    html.Summary("帮助"),
                                    html.Div(
                                        [
                                            html.Strong("期权交易终端"),
                                            html.P(
                                                "左侧点击期权报价查看 K 线；导航器打开账户、EA 和分析；底部工具箱管理持仓、回测与日志。"
                                            ),
                                            html.P(
                                                "Ctrl+M / N / T / R 定位报价、导航、交易与测试器。M1–H1 切换当前历史期权图表周期。"
                                            ),
                                            html.P(
                                                "EA 管理仅打开参数，实盘启动仍需账户核验与单次确认；界面停止请求以券商成交为准。"
                                            ),
                                            html.A(
                                                "MT5 界面参考",
                                                href="https://www.metatrader5.com/en/terminal/help/startworking/interface",
                                                target="_blank",
                                                rel="noopener noreferrer",
                                            ),
                                        ],
                                        className="terminal-menu-popup terminal-help-popup",
                                    ),
                                ],
                                id="t-help-menu",
                                className="terminal-menu",
                            ),
                        ],
                        className="terminal-menubar",
                        **{"aria-label": "主菜单"},
                    ),
                    html.Span("Nautilus · Options", className="terminal-caption"),
                ],
                className="terminal-header",
            ),
            html.Div(
                [
                    html.Div(
                        [
                            panel_link("市场报价", "t-view-market", "market"),
                            panel_link("导航器", "t-view-navigator", "navigator"),
                            panel_link("工具箱", "t-view-toolbox", "positions"),
                            panel_link("测试器", "t-view-backtest", "backtest"),
                        ],
                        className="terminal-toolbar-group",
                    ),
                    html.Div(
                        [
                            panel_link("K 线", "t-view-chart", "chart"),
                            panel_link("期权链", "t-view-chain", "chain"),
                            panel_link("EA 管理", "t-view-strategy", "strategy"),
                        ],
                        className="terminal-toolbar-group",
                    ),
                    dcc.RadioItems(
                        id="t-candle-minutes",
                        options=[
                            dict(label=label, value=value)
                            for label, value in [("M1", 1), ("M5", 5), ("M15", 15), ("M30", 30), ("H1", 60)]
                        ],
                        value=5,
                        inline=True,
                        className="terminal-periods",
                    ),
                    html.Span(id="t-ea-badge", className="terminal-toolbar-ea"),
                    choice("t-workspace", [(label, key) for key, label in WORKSPACES.items()], "research"),
                ],
                className="terminal-toolbar",
                **{"aria-label": "交易终端工具栏"},
            ),
            html.Div(id="t-layout-status", className="terminal-layout-feedback", role="status"),
            html.Div(
                id="t-download-status", className="terminal-layout-feedback terminal-download-feedback", role="status"
            ),
            html.Div(
                "界面数据已停止更新；后台策略可能仍在运行。请在券商核验账户状态。",
                id="t-network-warning",
                className="terminal-network-warning",
                style={"display": "none"},
                role="alert",
            ),
            html.Div(id="t-context-banner", className="terminal-context-banner", role="status"),
            html.Div(
                dock.DashFlexLayout(
                    id="t-dock",
                    model=options_model(),
                    children=[dock.Tab(id=p.key, children=contents[p.key]) for p in PANELS],
                    useStateForModel=False,
                    colorScheme="dark",
                    supportsPopout=False,
                    realtimeResize=False,
                    style={"height": "100%", "visibility": "hidden"},
                ),
                className="terminal-dock-frame",
            ),
            html.Footer(
                [
                    html.Span(id="t-footer-status"),
                    html.Div(
                        [
                            html.Span("正在连接服务", id="t-service-state", className="terminal-service-wait"),
                            html.Span(id="t-market-badge"),
                            html.Span(id="t-clock"),
                        ],
                        className="terminal-statusbar",
                    ),
                ],
                className="terminal-footer",
            ),
            dcc.Interval(id="t-poll", interval=1000),
            dcc.Interval(id="t-heartbeat", interval=2000),
            dcc.Interval(id="t-report-poll", interval=5000),
            dcc.Store(id="t-state"),
            dcc.Store(id="t-render-signatures"),
            dcc.Store(id="t-layout-store", storage_type="local"),
            dcc.Store(id="t-layout-request", data=options_model()),
            dcc.Store(id="t-layout-mounted"),
            dcc.Store(id="t-view-command"),
            dcc.Store(id="t-review"),
            dcc.Store(id="t-inspection"),
            dcc.Store(id="t-analysis-signature"),
            dcc.Store(id="t-chain-focus"),
            dcc.Store(id="t-chain-column-note"),
            dcc.Store(id="t-visible-graphs"),
            *[dcc.Store(id="t-output-" + key) for key in ["chain", "watch", "accounts", "fills", "controls"]],
            html.Div(
                [
                    html.Div(
                        [
                            html.H3("启动参数审阅"),
                            html.Div(id="t-review-summary"),
                            html.P("EA 在启动后采集行情并选择组合；此处预算不是券商保证金估算，也不保证立即开仓。"),
                            dcc.Checklist(
                                id="t-live-confirm",
                                options=[dict(label="确认向此实盘账户发送真实委托", value="confirmed")],
                                value=[],
                            ),
                            html.Div(
                                [button("取消", "t-review-cancel"), button("确认启动", "t-strategy-start", True)],
                                className="terminal-buttons",
                            ),
                        ],
                        className="terminal-modal-card",
                        role="dialog",
                        **{"aria-modal": "true", "aria-label": "启动参数审阅"},
                    )
                ],
                id="t-review-modal",
                className="terminal-modal",
                style={"display": "none"},
            ),
        ],
        className="terminal-root",
        id="t-root",
    )

    @app.server.before_request
    def local_only():
        origin = request.headers.get("Origin")
        if request.host.split(":")[0] not in {"localhost", "127.0.0.1"} or (
            origin and origin != f"http://{request.host}"
        ):
            return jsonify(error="请求来源未授权"), 403

    @app.server.get("/health")
    def health():
        return jsonify(status="ok", application="qwork-terminal")

    @app.server.get("/api/snapshot")
    def api_snapshot():
        return jsonify(backend.snapshot())

    @app.server.get("/api/replay/fills.csv")
    def download_fills():
        import pandas as pd

        replay = backend.snapshot()["replay"]
        fills = replay.get("fills", [])
        if not fills:
            return jsonify(error="当前回测没有成交可导出"), 409
        metadata = (replay.get("report") or {}).get("combo_contracts") or replay.get("combo_contracts", {})
        rows = []
        for fill in fills:
            row = dict(fill)
            combo = metadata.get(fill.get("instrument_id"))
            if combo:
                row["combination"] = combo["label"]
                row["combo_premium"] = round(float(fill["last_px"]) - combo["accounting_price_offset"], 4)
            rows.append(row)
        return send_file(
            BytesIO(pd.DataFrame(rows).to_csv(index=False).encode("utf-8-sig")),
            mimetype="text/csv",
            as_attachment=True,
            download_name="qwork-fills.csv",
        )

    @app.callback(Output("t-state", "data"), Input("t-poll", "n_intervals"))
    def poll(_):
        nonlocal revision
        value = backend.snapshot()
        with snapshot_lock:
            revision += 1
            snapshots[revision] = value
            while len(snapshots) > 32:
                snapshots.popitem(last=False)
            return {"revision": revision, "at": time.time()}

    @app.callback(
        Output("t-market-action", "children"),
        Input("t-market-refresh", "n_clicks"),
        State("t-symbol", "value"),
        State("t-market-mode", "value"),
        State("t-day", "date"),
        State("t-at", "value"),
        prevent_initial_call=True,
    )
    def refresh(_, symbol, mode, day, at):
        try:
            return backend.refresh_analysis(symbol, mode, day, at)
        except ValueError:
            return "行情参数无效，请检查标的、日期和时间"

    @app.callback(
        Output("t-chart-action", "children"),
        Output("t-strike", "value"),
        Output("t-right", "value"),
        Output("t-expiration", "value", allow_duplicate=True),
        Input("t-chart-load", "n_clicks"),
        Input("t-chain-grid", "cellClicked"),
        Input("t-watch-grid", "cellClicked"),
        Input("t-candle-minutes", "value"),
        *[State(k, "value") for k in ["t-symbol", "t-expiration", "t-strike", "t-right"]],
        State("t-day", "date"),
        State("t-workspace", "value"),
        State("t-state", "data"),
        State("t-bt-symbol", "value"),
        State("t-bt-day", "date"),
        State("t-chain-grid", "virtualRowData"),
        State("t-watch-grid", "virtualRowData"),
        State("t-chain-select-mode", "value"),
        prevent_initial_call=True,
    )
    def chart_load(
        _,
        cell,
        watch_cell,
        minutes,
        symbol,
        expiration,
        strike,
        right,
        day,
        workspace,
        marker,
        bt_symbol,
        bt_day,
        visible_rows,
        visible_watch,
        select_mode,
    ):
        try:
            if ctx.triggered_id == "t-chain-grid" and "legs" in (select_mode or []):
                return (no_update,) * 4
            if workspace in {"paper", "live"}:
                return "历史 K 线请在研究工作区查看", no_update, no_update, no_update
            if workspace == "replay":
                run = read_snapshot(marker).get("replay") or {}
                symbol = run.get("config", {}).get("symbol") or bt_symbol
                day = run.get("day") or bt_day
            if ctx.triggered_id == "t-watch-grid":
                row = next(
                    (
                        item
                        for item in visible_watch or []
                        if f"{item['symbol']}|{item['expiration']}|{float(item['strike']):g}|{item['right']}"
                        == str((watch_cell or {}).get("rowId"))
                    ),
                    None,
                )
                if not row:
                    raise ValueError("旧报价选择")
                frame = chain_frame(read_snapshot(marker), workspace)
                if row not in watch_rows(frame, row["expiration"], row["strike"]):
                    raise ValueError("旧报价选择")
                symbol, expiration, strike, right = [row[key] for key in ["symbol", "expiration", "strike", "right"]]
                day = frame.get("observed_at", "")[:10]
            if ctx.triggered_id == "t-chain-grid":
                row = next(
                    (
                        item
                        for item in visible_rows or []
                        if f"{item['expiration']}-{float(item['strike']):g}" == str((cell or {}).get("rowId"))
                    ),
                    {},
                )
                column = (cell or {}).get("colId", "")
                if not row or column not in {
                    "strike",
                    "call_bid",
                    "call_ask",
                    "call_iv",
                    "put_bid",
                    "put_ask",
                    "put_iv",
                }:
                    return (no_update,) * 4
                view = read_snapshot(marker)
                frame = chain_frame(view, workspace)
                symbol = frame.get("symbol")
                day = frame.get("observed_at", "")[:10]
                expiration, strike = row["expiration"], row["strike"]
                right = "call" if column.startswith("call_") else "put" if column.startswith("put_") else right
                if row not in chain_rows(frame, expiration):
                    raise ValueError("旧合约选择")
            return backend.load_chart(symbol, expiration, strike, right, day, minutes), strike, right, expiration
        except (ValueError, TypeError):
            return "合约或行情已变化，请重新选择有效合约", no_update, no_update, no_update

    @app.callback(
        Output("t-strategy-action", "children"),
        Output("t-review", "data"),
        *[
            Input(k, "n_clicks")
            for k in [
                "t-account-connect",
                "t-account-disconnect",
                "t-strategy-review",
                "t-strategy-start",
                "t-strategy-stop",
                "t-review-cancel",
                "t-strategy-attach",
            ]
        ],
        *[
            State(k, "value")
            for k in [
                "t-account-id",
                "t-trade-mode",
                "t-margin",
                "t-max-positions",
                "t-execution",
                "t-ea-symbol",
                "t-loss",
                "t-drawdown",
                "t-live-confirm",
            ]
        ],
        State("t-review", "data"),
        State("t-workspace", "value"),
        prevent_initial_call=True,
    )
    def strategy_action(
        _connect,
        _disconnect,
        _review,
        _start,
        _stop,
        _cancel,
        _attach,
        account,
        mode,
        margin,
        positions,
        execution,
        symbol,
        loss,
        drawdown,
        confirmed,
        review,
        workspace,
    ):
        try:
            if ctx.triggered_id == "t-account-connect":
                return backend.connect_broker(account or ""), None
            if ctx.triggered_id == "t-account-disconnect":
                return backend.disconnect_broker(), None
            if ctx.triggered_id == "t-strategy-attach":
                return backend.attach_strategy(account or "", mode), None
            if ctx.triggered_id == "t-strategy-stop":
                return backend.stop_strategy(), None
            if ctx.triggered_id == "t-review-cancel":
                return "已取消启动审阅，未启动策略", None
            if workspace not in {"paper", "live"} or workspace != mode:
                raise ValueError("请在目标交易工作区审阅和启动")
            if ctx.triggered_id == "t-strategy-review":
                return "请审阅目标账户和参数", backend.review_strategy(
                    account or "", mode, margin, positions, execution, symbol, loss, drawdown
                )
            return backend.start_strategy(
                account or "",
                mode,
                margin,
                positions,
                execution,
                symbol,
                loss,
                drawdown,
                "confirmed" in (confirmed or []),
                review_id=(review or {}).get("review_id"),
            ), None
        except (ValueError, TypeError) as exc:
            return str(exc), None

    @app.callback(
        Output("t-backtest-action", "children"),
        *[
            Input(k, "n_clicks")
            for k in ["t-backtest-start", "t-pause", "t-play", "t-step", "t-backtest-stop", "t-report-load"]
        ],
        Input("t-speed", "value"),
        State("t-bt-symbol", "value"),
        State("t-bt-day", "date"),
        *[
            State(k, "value")
            for k in ["t-bt-margin", "t-bt-positions", "t-horizon", "t-bt-execution", "t-universe", "t-saved-run"]
        ],
        State("t-workspace", "value"),
        prevent_initial_call=True,
    )
    def backtest_action(
        _start,
        _pause,
        _play,
        _step,
        _stop,
        _report,
        speed,
        symbol,
        day,
        margin,
        positions,
        horizon,
        execution,
        universe,
        saved,
        workspace,
    ):
        try:
            if ctx.triggered_id == "t-backtest-start":
                if workspace != "replay":
                    raise ValueError("请切换到回放工作区")
                return backend.start_backtest(symbol, day, margin, positions, horizon, execution, universe, speed)
            if ctx.triggered_id == "t-report-load":
                return backend.load_report(saved or "")
            actions = {
                "t-pause": "pause",
                "t-play": "play",
                "t-step": "step",
                "t-backtest-stop": "stop",
                "t-speed": "speed",
            }
            if ctx.triggered_id == "t-speed" and (
                not backend.backtest_thread or not backend.backtest_thread.is_alive()
            ):
                return "回放速度已设置，将用于下次回测"
            return backend.control_replay(actions[ctx.triggered_id], speed)
        except (ValueError, TypeError, KeyError, OSError):
            return "操作未完成：请检查输入参数、报告或当前运行状态"

    @app.callback(
        Output("t-layout-store", "data"),
        Output("t-layout-status", "children"),
        Input("t-layout-save", "n_clicks"),
        State("t-dock", "model"),
        State("t-layout-store", "data"),
        State("t-workspace", "value"),
        prevent_initial_call=True,
    )
    def save_layout(_, model, previous, workspace):
        saved = (
            dict(previous)
            if isinstance(previous, dict)
            and previous.get("version") == 2
            and isinstance(previous.get("workspaces"), dict)
            else dict(version=2, workspaces={})
        )
        saved["workspaces"] = dict(saved["workspaces"], **{workspace: model})
        return saved, f"{WORKSPACES[workspace]}布局已保存"

    @app.callback(
        Output("t-layout-request", "data"),
        Input("t-layout-restore", "n_clicks"),
        Input("t-layout-reset", "n_clicks"),
        Input("t-layout-options", "n_clicks"),
        Input("t-workspace", "value"),
        State("t-layout-store", "data"),
        prevent_initial_call=True,
    )
    def restore_layout(_restore, _reset, _options, workspace, saved):
        if ctx.triggered_id == "t-layout-reset":
            return dock_model(workspace)
        if ctx.triggered_id == "t-layout-options":
            return options_model(workspace)
        if saved:
            if saved.get("version") == 2:
                layouts = saved.get("workspaces") or {}
                return (
                    restore_model(layouts.get(workspace, options_model(workspace)), workspace)
                    if isinstance(layouts, dict)
                    else options_model(workspace)
                )
            if workspace == "research" and "layout" in saved:
                return restore_model(saved, workspace)
        return options_model(workspace)

    app.clientside_callback(
        """function() {
            const trigger = window.dash_clientside.callback_context.triggered_id;
            const commands = """
        + json.dumps(view_commands)
        + """;
            const gridAction = ['t-chain-grid','t-watch-grid','t-candle-minutes'].includes(trigger);
            if (gridAction && !window.dash_clientside.callback_context.triggered[0].value)
                return window.dash_clientside.no_update;
            const panel = gridAction ? 'chart' : commands[trigger];
            if (trigger === 't-chain-grid' && document.querySelector('#t-chain-select-mode input')?.checked)
                return window.dash_clientside.no_update;
            if (!panel) return window.dash_clientside.no_update;
            window.dash_clientside.set_props('t-dock', {modelAction:{type:'selectTab',tabNodeId:panel,nonce:Date.now()+':'+trigger}});
            document.querySelectorAll('.terminal-menu[open]').forEach(menu => menu.open=false);
            return {panel:panel,at:Date.now()};
        }""",
        Output("t-view-command", "data"),
        *[Input(key, "n_clicks") for key in view_commands],
        Input("t-watch-grid", "cellClicked"),
        Input("t-chain-grid", "cellClicked"),
        Input("t-candle-minutes", "value"),
        prevent_initial_call=True,
    )

    # FlexLayout mounts a tab only after its parent has a positive content rect.
    # Briefly open collapsed borders offscreen, then close them through library
    # actions so their controls stay mounted and retain cross-panel callback state.
    app.clientside_callback(
        """async function(request) {
            if (!request) return window.dash_clientside.no_update;
            const nonce = (window.qworkLayoutGeneration || 0) + 1;
            window.qworkLayoutGeneration = nonce;
            const model = JSON.parse(JSON.stringify(request));
            const collapsed = (model.borders || []).filter(b => b.selected === -1 && b.children.length);
            for (const border of collapsed) border.selected = 0;
            const style = {height:'100%',visibility:'hidden'};
            window.dash_clientside.set_props('t-dock', {model:model,style:style});
            if (collapsed.length) await new Promise(resolve => setTimeout(resolve, 750));
            else await new Promise(resolve => requestAnimationFrame(() => requestAnimationFrame(resolve)));
            for (const border of collapsed) {
                if (window.qworkLayoutGeneration !== nonce) return window.dash_clientside.no_update;
                window.dash_clientside.set_props('t-dock', {modelAction:{
                    type:'updateNodeAttributes',nodeId:border.id || 'border_' + border.location,
                    attributes:{selected:-1},nonce:nonce + ':' + border.location
                }});
                await new Promise(resolve => setTimeout(resolve, 250));
            }
            if (window.qworkLayoutGeneration !== nonce) return window.dash_clientside.no_update;
            window.dash_clientside.set_props('t-dock', {style:{height:'100%',visibility:'visible'}});
            window.dash_clientside.set_props('t-chain-focus', {data:null});
            return nonce;
        }""",
        Output("t-layout-mounted", "data"),
        Input("t-layout-request", "data"),
    )

    @app.callback(
        Output("t-chain-grid", "rowData"),
        Output("t-expiration", "options"),
        Output("t-chain-status", "children"),
        Output("t-expiration", "value"),
        Output("t-chain-expiration", "options"),
        Output("t-chain-expiration", "value"),
        Output("t-chain-export", "disabled"),
        Output("t-output-chain", "data"),
        Input("t-state", "data"),
        Input("t-expiration", "value"),
        Input("t-chain-expiration", "value"),
        Input("t-chain-range", "value"),
        Input("t-strike", "value"),
        Input("t-chain-source", "value"),
        Input("t-workspace", "value"),
        State("t-expiration", "options"),
        State("t-output-chain", "data"),
    )
    @quiet_outputs
    def chain_update(value, expiration, chain_expiration, count, strike, source, workspace, previous_options):
        value = read_snapshot(value)
        frame = chain_frame(value, workspace)
        options = (frame or {}).get("expirations", [])
        if ctx.triggered_id == "t-chain-expiration":
            expiration = chain_expiration
        selected = expiration if expiration in options or not options else options[0]
        message = (
            f"回放原始 NBBO · {value.get('replay', {}).get('clock', '尚未运行')} · IV 未重算"
            if source == "replay"
            else value.get("market_message", "等待行情") + f" · 快照 {(frame or {}).get('observed_at', '—')}"
        )
        if workspace in {"paper", "live"} and not frame:
            message = (
                market_health(value.get("frame"), value.get("market_context"))["label"] + "；刷新行情后等待有效报价"
            )
        full_rows = chain_rows(frame, selected)
        rows = nearby_chain_rows(full_rows, strike, count)
        message += f" · 行权价 {len(rows)} / {len(full_rows)} 档"
        return (
            rows,
            no_update
            if (frame or {}).get("expirations", []) == previous_options
            else (frame or {}).get("expirations", []),
            message,
            no_update if selected == expiration else selected,
            options,
            selected,
            not bool(rows),
        )

    app.clientside_callback(
        """async function(clear, download) {
            const no = window.dash_clientside.no_update;
            if (window.dash_clientside.callback_context.triggered_id === 't-chain-clear') return [{}, ''];
            const api = await window.dash_ag_grid.getApiAsync('t-chain-grid');
            api.exportDataAsCsv({fileName:'option-chain.csv'});
            return [no, '已请求导出；桌面请选择保存位置，浏览器请查看下载列表'];
        }""",
        Output("t-chain-grid", "filterModel"),
        Output("t-export-status", "children"),
        Input("t-chain-clear", "n_clicks"),
        Input("t-chain-export", "n_clicks"),
        prevent_initial_call=True,
    )

    @app.callback(
        Output("t-watch-grid", "rowData"),
        Output("t-watch-source", "children"),
        Output("t-output-watch", "data"),
        Input("t-state", "data"),
        Input("t-workspace", "value"),
        Input("t-expiration", "value"),
        Input("t-strike", "value"),
        State("t-output-watch", "data"),
    )
    @quiet_outputs
    def market_watch(marker, workspace, expiration, strike):
        frame = chain_frame(read_snapshot(marker), workspace)
        source = (
            "SIM 回放报价"
            if workspace == "replay"
            else "实时快照"
            if workspace in {"paper", "live"}
            else "历史快照 · 非实时"
        )
        return watch_rows(frame, expiration, strike), source + (f" · {frame['symbol']}" if frame else " · 等待行情")

    @app.callback(
        *[
            Output(k, "children")
            for k in [
                "t-market-badge",
                "t-clock",
                "t-footer-status",
                "t-strategy-status",
                "t-replay-status",
                "t-chart-title",
                "t-chart-source",
                "t-result-summary",
            ]
        ],
        Output("t-candidates", "rowData"),
        Output("t-chart", "figure"),
        Output("t-equity", "figure"),
        Output("t-surface", "figure"),
        Output("t-render-signatures", "data"),
        Input("t-state", "data"),
        Input("t-surface-mode", "value"),
        Input("t-surface-right", "value"),
        State("t-render-signatures", "data"),
        Input("t-workspace", "value"),
        Input("t-inspection", "data"),
        Input("t-visible-graphs", "data"),
    )
    def render(value, surface_mode, surface_right, previous, workspace="research", inspection=None, visible=None):
        value = read_snapshot(value)
        frame, broker, strategy, replay, chart = [
            value.get(k) or {} for k in ["frame", "broker", "strategy", "replay", "chart"]
        ]
        chart = workspace_chart(value, workspace)
        frame = {} if workspace == "replay" else chain_frame(value, workspace)
        stamp = (inspection or {}).get("timestamp") if workspace == "replay" else None
        curve = replay.get("curve", [])
        previous = previous or {}
        signatures = dict(
            chart=[hashlib.sha256(json.dumps(chart, sort_keys=True).encode()).hexdigest(), stamp],
            equity=[replay.get("output"), replay.get("curve_kind"), len(curve), curve[-1] if curve else None, stamp],
            surface=[
                surface_mode,
                surface_right,
                workspace,
                *[frame.get(key) for key in ["symbol", "generation", "updated_at", "observed_at", "mode"]],
            ],
        )
        shown = set(visible) if visible is not None else {"t-chart", "t-equity", "t-surface"}
        dirty = {key: signatures[key] != previous.get(key) and "t-" + key in shown for key in signatures}
        # Hidden graphs catch up once on activation, rather than render every tick.
        for key in signatures:
            if not dirty[key]:
                signatures[key] = previous.get(key)
        equity = no_update
        if dirty["equity"]:
            equity = (
                go.Figure(
                    go.Scatter(
                        x=[
                            datetime.fromisoformat(r["timestamp"]).astimezone(eastern()).replace(tzinfo=None)
                            for r in curve
                        ],
                        y=[r["equity"] for r in curve],
                        name="模拟现金余额 · 非净值" if replay.get("curve_kind") == "cash" else "模拟净值 · BID 估值",
                        line=dict(color=PALETTE["accent"], width=1.5),
                    )
                )
                if curve
                else empty_figure("运行回测后显示净值；历史报告见结果摘要")
            )
            equity.update_layout(
                template="plotly_dark",
                paper_bgcolor=PALETTE["panel"],
                plot_bgcolor=PALETTE["canvas"],
                margin=dict(l=55, r=20, t=30, b=35),
                uirevision=replay.get("output"),
                xaxis_title="纽约时间",
            )
            chart_theme(equity)
        candidates = [
            dict(
                row,
                candidate_id=candidate_identity(row, frame),
                strikes=" / ".join(f"{strike:g}" for strike in row["strikes"]),
                profit_loss_ratio=round(row["profit_loss_ratio"], 3),
                max_profit=round(row["max_profit"], 2),
                max_loss=round(row["max_loss"], 2),
                model_probability_percent=round(row["model_probability"] * 100, 2),
            )
            for row in (value.get("scan") or {}).get("candidates", [])[:100]
            if frame
            and matches_surface(value.get("scan"), frame)
            and market_health(frame, value.get("market_context"))["valid"]
        ]
        report = replay.get("report") or {}
        clock_label = (
            datetime.fromisoformat(replay["clock"]).astimezone(eastern()).strftime("%Y-%m-%d %H:%M:%S ET")
            if replay.get("clock")
            else "—"
        )
        phase = (
            ("暂停" if replay.get("paused") else "播放")
            if replay.get("status") == "running"
            else {"preparing": "准备", "archived": "查看报告", "idle": "待运行"}.get(replay.get("status"), "已结束")
        )
        summary = f"已平组合 {report.get('closed_combinations', '—')} · 净盈亏 {report.get('net_pnl', '—')} USD · 审计 {report.get('audit_passed', '—')}"
        text_values = (
            "回放报价 · SIM"
            if workspace == "replay"
            else "实时行情尚未就绪"
            if workspace in {"paper", "live"} and not frame
            else market_health(value.get("frame"), value.get("market_context"))["label"],
            f"回放 {clock_label}"
            if workspace == "replay" and replay.get("clock")
            else datetime.now(eastern()).strftime("%H:%M:%S ET"),
            value.get("market_message", "等待行情"),
            strategy.get("status", "")
            + (f" · 风控 {strategy['risk'].get('stop_reason', '正常')}" if strategy.get("risk") else ""),
            f"{replay.get('message', '')} · {phase} · {'最大速度' if replay.get('speed') == 0 else str(replay.get('speed', '—')) + '×'} · 时间 {clock_label}",
            chart.get("status", "请选择期权"),
            chart.get("source", ""),
            summary,
            candidates,
        )
        *text_outputs, text_signatures = changed_outputs(text_values, previous.get("text"))
        signatures["text"] = previous.get("text") if text_signatures is no_update else text_signatures
        return (
            *text_outputs,
            add_inspection(candle_figure(chart), stamp) if dirty["chart"] else no_update,
            add_inspection(equity, stamp) if dirty["equity"] else no_update,
            (
                iv_figure(frame or None, surface_right, surface_mode)
                if frame
                else empty_figure(
                    "回放未重算 IV；历史曲面请在研究工作区查看" if workspace == "replay" else "等待有效实时 IV 行情"
                )
            )
            if dirty["surface"]
            else no_update,
            signatures if signatures != previous else no_update,
        )

    @app.callback(
        Output("t-saved-run", "options"), Input("t-report-poll", "n_intervals"), State("t-saved-run", "options")
    )
    def saved_reports(_, previous):
        options = [
            dict(label=f"{r['date']} · {r['name']} · {r['status']}", value=r["name"]) for r in backend.list_runs()
        ]
        return no_update if options == previous else options

    @app.callback(
        Output("t-positions-grid", "rowData"),
        Output("t-orders-grid", "rowData"),
        Output("t-account-grid", "rowData"),
        Output("t-account-status", "children"),
        Output("t-events-grid", "rowData"),
        Output("t-output-accounts", "data"),
        Input("t-state", "data"),
        Input("t-position-source", "value"),
        Input("t-order-source", "value"),
        Input("t-account-source", "value"),
        Input("t-event-source", "value"),
        State("t-output-accounts", "data"),
    )
    @quiet_outputs
    def account_tables(value, position_source, order_source, account_source, event_source):
        value = read_snapshot(value)
        account = value.get(account_source) or {}
        positions = (value.get(position_source) or {}).get("positions", [])
        if position_source == "strategy":
            positions = [
                dict(row, instrument_id=row["key"], quantity=row["units"], avg_px_open=row.get("entry_price"))
                for row in (value.get("strategy") or {}).get("rows", [])
            ]
        return (
            positions,
            (value.get(order_source) or {}).get("orders", []),
            account.get("account", []),
            account.get("status", "等待账户") + (" · 模拟账户" if account_source == "replay" else " · IBKR 账户"),
            (value.get(event_source) or {}).get("events", []),
        )

    @app.callback(
        *[
            Output(key, "value")
            for key in [
                "t-chain-source",
                "t-account-source",
                "t-position-source",
                "t-order-source",
                "t-event-source",
                "t-trade-mode",
                "t-market-mode",
            ]
        ],
        Input("t-workspace", "value"),
    )
    def workspace_sources(workspace):
        trading = workspace in {"paper", "live"}
        source = "broker" if trading else "replay"
        return (
            "replay" if workspace == "replay" else "market",
            source,
            source,
            source,
            "strategy" if trading else "replay",
            "live" if workspace == "live" else "paper",
            "live" if trading else "historical",
        )

    @app.callback(
        Output("t-chain-grid", "scrollTo"),
        Output("t-chain-focus", "data"),
        Input("t-chain-grid", "rowData"),
        Input("t-strike", "value"),
        State("t-chain-focus", "data"),
    )
    def focus_strike(rows, strike, previous):
        if not rows or strike is None:
            return no_update, None
        row = min(rows, key=lambda item: abs(float(item["strike"]) - float(strike)))
        key = f"{row['expiration']}-{float(row['strike']):g}"
        if key == previous:
            return no_update, no_update
        return {"rowId": key, "rowPosition": "middle"}, key

    parameter_keys = [
        "t-account-id",
        "t-trade-mode",
        "t-margin",
        "t-max-positions",
        "t-execution",
        "t-ea-symbol",
        "t-loss",
        "t-drawdown",
    ]

    @app.callback(
        Output("t-live-confirm", "value"),
        *[Input(key, "value") for key in parameter_keys],
        Input("t-workspace", "value"),
        Input("t-review", "data"),
        prevent_initial_call=True,
    )
    def reset_confirmation(*_):
        return []

    @app.callback(
        Output("t-review-modal", "style"),
        Output("t-review-summary", "children"),
        Output("t-strategy-start", "disabled"),
        Output("t-live-confirm", "style"),
        Input("t-review", "data"),
        *[Input(key, "value") for key in parameter_keys],
        Input("t-workspace", "value"),
        Input("t-live-confirm", "value"),
        Input("t-state", "data"),
    )
    def review_display(
        review, account, mode, margin, positions, execution, symbol, loss, drawdown, workspace, confirmation, marker
    ):
        if not review:
            return {"display": "none"}, [], True, {}
        try:
            payload = strategy_parameters(account, mode, margin, positions, execution, symbol, loss, drawdown)
            valid = (
                payload == review["parameters"]
                and workspace == mode
                and datetime.fromisoformat(review["expires_at"]).timestamp() > datetime.now().timestamp()
            )
            reason = permissions(
                read_snapshot(marker), workspace, (account, mode, margin, positions, execution, symbol, loss, drawdown)
            )["review_reason"]
            valid = (
                valid
                and not permissions(
                    read_snapshot(marker),
                    workspace,
                    (account, mode, margin, positions, execution, symbol, loss, drawdown),
                )["review"]
            )
        except (ValueError, KeyError, TypeError):
            valid, reason = False, "参数无效"
        labels = dict(
            account="目标账户",
            mode="环境",
            symbol="标的",
            margin="保证金预算 USD",
            positions="并发组合上限",
            execution="执行方式",
            loss="当日亏损限制 USD",
            drawdown="净值回撤限制 USD",
        )
        summary = [
            html.Div([html.Span(label), html.Strong(str(review["parameters"][key]))], className="terminal-review-row")
            for key, label in labels.items()
        ]
        summary.append(
            html.P(
                "审阅有效期 90 秒；修改账户或参数须重新审阅。"
                if valid
                else f"审阅不可执行：参数已变化、审阅过期或 {reason}",
                className="terminal-alert",
            )
        )
        return (
            {"display": "flex"},
            summary,
            not valid or (mode == "live" and "confirmed" not in (confirmation or [])),
            {"display": "block" if mode == "live" else "none"},
        )

    @app.callback(
        *[
            Output(key, "disabled")
            for key in [
                "t-strategy-review",
                "t-strategy-stop",
                "t-backtest-start",
                "t-pause",
                "t-play",
                "t-step",
                "t-backtest-stop",
                "t-report-load",
                "t-chart-load",
            ]
        ],
        Output("t-context-banner", "children"),
        Output("t-context-banner", "className"),
        Output("t-trade-context", "children"),
        Output("t-strategy-detail", "children"),
        Output("t-running-config", "children"),
        Output("t-backtest-config-summary", "children"),
        Output("t-ea-combos", "rowData"),
        Output("t-nav-context", "children"),
        Output("t-ea-badge", "children"),
        Output("t-output-controls", "data"),
        Input("t-state", "data"),
        Input("t-workspace", "value"),
        *[Input(key, "value") for key in parameter_keys],
        Input("t-saved-run", "value"),
        State("t-output-controls", "data"),
    )
    def controls(
        marker, workspace, account, mode, margin, positions, execution, symbol, loss, drawdown, saved, previous
    ):
        view = read_snapshot(marker)
        access = permissions(view, workspace, (account, mode, margin, positions, execution, symbol, loss, drawdown))
        strategy, replay, frame = [view.get(key) or {} for key in ["strategy", "replay", "frame"]]
        labels = {
            "research": "研究 · 行情分析与报告查看 · 不启动交易",
            "replay": "回放 · SIM 模拟账户 · 按原生历史时钟推进",
            "paper": "PAPER · 券商模拟账户",
            "live": "LIVE · 真实委托环境",
        }
        source = (
            frame.get("observed_at", "尚无行情")
            if workspace == "research"
            else replay.get("clock") or replay.get("day", "未开始")
            if workspace == "replay"
            else (view.get("broker") or {}).get("account_timestamp", "等待账户回报")
        )
        banner = f"{labels.get(workspace, '未知工作区')} | {account or '未选择账户' if workspace in {'paper', 'live'} else '历史时间'} | {source}"
        if strategy.get("running"):
            banner += f" | 后台 EA：{strategy.get('account')} {strategy.get('symbol')} {strategy.get('status')}"
        detail = (
            strategy.get("persistence_error")
            or strategy.get("state_error")
            or strategy.get("last_reason")
            or "尚未收到可解释的策略事件"
        )
        units, pending = strategy.get("remaining_units"), strategy.get("pending_orders")
        detail += f" · 剩余组合单位 {units if units is not None else '未知'} · 待确认委托 {pending if pending is not None else '未知'}"
        if strategy.get("attached"):
            detail = "已恢复原后台进程监看；以当前券商回报为准。" + detail
        active = strategy.get("config") or {}
        config_labels = dict(
            account="账户",
            mode="环境",
            symbol="标的",
            margin="预算 USD",
            positions="并发上限",
            execution="执行",
            loss="日亏损 USD",
            drawdown="回撤 USD",
        )
        running = (
            "\n".join(f"{config_labels.get(key, key)}: {value}" for key, value in active.items())
            or "尚未启动；修改表单不会更新运行中的进程"
        )
        run = replay.get("config") or {}
        summary = (
            "运行 / 报告参数：" + " · ".join(f"{key}={value}" for key, value in run.items())
            if run
            else "请先设置独立回测参数，再开始回放"
        )
        values = (
            access["review"],
            access["stop_strategy"],
            access["start_backtest"],
            access["pause"],
            access["play"],
            access["step"],
            access["stop_backtest"],
            access["report"] or not saved,
            workspace in {"paper", "live"} or bool(view.get("jobs", {}).get("chart")),
            banner,
            "terminal-context-banner terminal-live" if workspace == "live" else "terminal-context-banner",
            access["review_reason"],
            detail,
            running,
            summary,
            strategy.get("rows", []),
            f"{WORKSPACES.get(workspace, workspace)} · {account or '账户未连接'}"
            if workspace in {"paper", "live"}
            else f"{WORKSPACES.get(workspace, workspace)} · 期权研究",
            "EA：" + (strategy.get("status", "运行中") if strategy.get("running") else "未启动"),
        )
        # Always reapply permissions after the client watchdog disables actions.
        # Only descriptive text and EA rows are deduplicated.
        return (*values[:9], *changed_outputs(values[9:], previous))

    @app.callback(
        Output("t-builder-legs", "rowData"),
        Output("t-builder-status", "children"),
        Output("t-chain-action", "children"),
        *[
            Input(key, "n_clicks")
            for key in ["t-leg-buy", "t-leg-sell", "t-builder-remove", "t-builder-clear", "t-builder-apply"]
        ],
        Input("t-builder-legs", "cellValueChanged"),
        State("t-chain-grid", "cellClicked"),
        State("t-chain-grid", "virtualRowData"),
        State("t-builder-legs", "rowData"),
        State("t-builder-legs", "selectedRows"),
        State("t-builder-name", "value"),
        State("t-builder-fee", "value"),
        State("t-state", "data"),
        State("t-workspace", "value"),
        prevent_initial_call=True,
    )
    def build_combinations(
        _buy, _sell, _remove, _clear, _apply, edits, cell, visible_rows, draft, selected, name, fee, marker, workspace
    ):
        action, draft = ctx.triggered_id, list(draft or [])

        def reply(rows, message):
            return rows, message, message

        if action == "t-builder-clear":
            return reply([], "草稿已清空；已应用分析保留")
        if action == "t-builder-remove":
            removed = {row["leg_id"] for row in selected or []}
            return reply([row for row in draft if row["leg_id"] not in removed], "已移除所选分析腿")
        if action == "t-builder-legs":
            for edit in edits or []:
                draft = [
                    dict(row, **{edit["colId"]: edit["value"]}) if row["leg_id"] == edit["data"]["leg_id"] else row
                    for row in draft
                ]
            return reply(draft, "草稿已修改并保存；点击分析应用修改")
        try:
            if action == "t-builder-apply":
                return reply(no_update, backend.analyze_combinations(portfolio_request(draft, fee), workspace))
            if workspace == "replay":
                raise ValueError("回放未重算 IV，请在研究或有效实时行情工作区构建组合")
            view = read_snapshot(marker)
            frame = chain_frame(view, workspace)
            health = market_health(frame, view.get("market_context"))
            if not health["valid"]:
                raise ValueError(health["label"])
            row = next(
                (
                    row
                    for row in visible_rows or []
                    if f"{row['expiration']}-{float(row['strike']):g}" == str((cell or {}).get("rowId"))
                ),
                None,
            )
            if not row or row not in chain_rows(frame, row["expiration"]):
                raise ValueError("报价已变化，请重新点击有效 Call / Put 报价")
            draft.append(
                quote_leg(frame, row, (cell or {}).get("colId", ""), "buy" if action == "t-leg-buy" else "sell", name)
            )
            portfolio_request(draft, fee)
            return reply(
                draft,
                f"已加入 {draft[-1]['expiration']} {draft[-1]['strike']:g}{draft[-1]['right'][0].upper()} {'买入' if draft[-1]['side'] == 'buy' else '卖出'}；草稿 {len(draft)} 条腿",
            )
        except ValueError as exc:
            return reply(no_update, "未应用：" + str(exc))
        except (KeyError, TypeError):
            return reply(no_update, "未应用：草稿格式无效，请检查期权腿")

    @app.callback(
        Output("t-chain-selection", "children"),
        Output("t-chain-basket-count", "children"),
        Output("t-leg-buy", "disabled"),
        Output("t-leg-sell", "disabled"),
        Output("t-builder-apply", "disabled"),
        Output("t-builder-dirty", "children"),
        Input("t-chain-grid", "cellClicked"),
        Input("t-chain-grid", "virtualRowData"),
        Input("t-builder-legs", "rowData"),
        Input("t-builder-fee", "value"),
        Input("t-state", "data"),
        Input("t-workspace", "value"),
        Input("t-builder-name", "value"),
    )
    def draft_controls(cell, visible_rows, draft, fee, marker, workspace, name):
        view = read_snapshot(marker)
        frame = chain_frame(view, workspace)
        health = market_health(frame, view.get("market_context"))
        blocked = workspace == "replay" or not health["valid"]
        selection, disabled = "点击有效的 Call / Put 报价选择期权腿", True
        if not blocked and cell:
            row = next(
                (
                    r
                    for r in visible_rows or []
                    if f"{r['expiration']}-{float(r['strike']):g}" == str(cell.get("rowId"))
                ),
                None,
            )
            try:
                if not row or row not in chain_rows(frame, row["expiration"]):
                    raise ValueError("报价已变化，请重新选择")
                leg = quote_leg(frame, row, cell.get("colId", ""), "buy", name)
                right = leg["right"]
                selection = f"{leg['symbol']} {leg['expiration']} {leg['strike']:g} {right.upper()} · BID {row[right + '_bid']} / ASK {row[right + '_ask']}"
                disabled = False
            except (ValueError, KeyError, TypeError):
                selection = "该单元格没有有效期权报价，请选择其他 Call / Put"
        elif blocked:
            selection = "回放不支持组合选腿" if workspace == "replay" else health["label"]
        try:
            request = portfolio_request(draft, fee).model_dump(mode="json")
            dirty = request != view.get("portfolio_request")
            state = (
                (
                    "草稿已修改 · 未应用"
                    + (
                        "；图表为已应用版本"
                        if (view.get("portfolio_request") or {}).get("combinations")
                        else "；等待提交分析"
                    )
                )
                if dirty
                else "草稿与已应用分析一致"
            )
            apply_disabled = blocked
        except (ValueError, KeyError, TypeError):
            state = "草稿为空 · 可从期权链加入期权腿" if not draft else "草稿参数无效 · 请检查名称、张数和成本"
            apply_disabled = True
        return selection, f"篮子 {len(draft or [])} 腿", disabled, disabled, apply_disabled, state

    app.clientside_callback(
        """function(rows, apply) {
            return [window.dash_clientside.callback_context.triggered_id === 't-builder-apply'
                ? false : !!rows?.length, {height:(46 + 26*Math.min(4,Math.max(1,rows?.length||0)))+'px'}];
        }""",
        Output("t-builder-panel", "open"),
        Output("t-builder-legs", "style"),
        Input("t-builder-legs", "rowData"),
        Input("t-builder-apply", "n_clicks"),
    )

    app.clientside_callback(
        """async function(mode) {
            const api = await window.dash_ag_grid.getApiAsync('t-chain-grid');
            const core = ['call_bid','call_ask','call_iv','strike','put_iv','put_bid','put_ask'];
            for (const column of api.getColumns()) {
                const id = column.getColId();
                api.setColumnsVisible([id], core.includes(id) || mode === 'all' ||
                    mode === 'liquidity' && (id.includes('_size') || id.endsWith('_spread')));
            }
            return mode === 'all' ? '完整明细：可横向滚动查看报价时间' : '';
        }""",
        Output("t-chain-column-note", "data"),
        Input("t-chain-columns", "value"),
    )

    @app.callback(
        Output("t-analysis-action", "children"),
        Output("t-analysis-picker", "open"),
        Input("t-candidates", "selectedRows"),
        Input("t-analysis-units", "value"),
        Input("t-builder-apply", "n_clicks"),
        Input("t-leg-buy", "n_clicks"),
        Input("t-leg-sell", "n_clicks"),
        State("t-workspace", "value"),
        prevent_initial_call=True,
    )
    def select_candidate(rows, units, _apply, _buy, _sell, workspace):
        if ctx.triggered_id in {"t-builder-apply", "t-leg-buy", "t-leg-sell"}:
            return "多腿草稿使用组合篮子的“分析全部组合”进行估值", False
        if not rows:
            return "选择候选组合查看逐腿报价和风险", True
        if workspace == "replay":
            return "回放工作区不使用历史 IV 候选；请到研究工作区分析", True
        try:
            return backend.analyze_candidate(rows[0]["candidate_id"], units), False
        except (ValueError, KeyError, TypeError):
            return "分析未应用：行情已变化或组数无效，请重新选择候选", True

    @app.callback(
        Output("t-combo-pnl", "figure"),
        Output("t-combo-legs", "rowData"),
        Output("t-analysis-summary", "children"),
        Output("t-analysis-diagnostics", "children"),
        Output("t-term", "figure"),
        Output("t-term-source", "children"),
        Output("t-analysis-signature", "data"),
        Output("t-analysis-combination", "options"),
        Output("t-analysis-combination", "value"),
        Output("t-analysis-risk", "children"),
        Input("t-state", "data"),
        Input("t-workspace", "value"),
        Input("t-analysis-combination", "value"),
        State("t-analysis-signature", "data"),
    )
    def analysis_render(marker, workspace, selection, previous):
        view = read_snapshot(marker)
        frame = view.get("frame") or {}
        allowed = workspace != "replay" and (workspace == "research" or frame.get("mode") == "live")
        portfolio = (view.get("portfolio") or {}) if allowed else {}
        term = view.get("term") if allowed else None
        combinations = portfolio.get("combinations", [])
        options = [
            dict(label="全部组合合计", value="aggregate"),
            *[dict(label=row["name"], value="name:" + row["name"]) for row in combinations],
        ]
        selection = selection if selection in {row["value"] for row in options} else "aggregate"
        selected_name = selection[5:] if selection.startswith("name:") else None
        signature = hashlib.sha256(
            json.dumps(
                [
                    portfolio,
                    term,
                    workspace,
                    view.get("portfolio_request"),
                    view.get("portfolio_error"),
                    selection,
                    frame.get("surface_revision"),
                    frame.get("observed_at"),
                    market_health(frame, view.get("market_context"))["valid"],
                ],
                sort_keys=True,
            ).encode()
        ).hexdigest()
        if signature == previous:
            return (no_update,) * 10
        request = view.get("portfolio_request") or {}
        legs = (
            [
                dict(leg, combo_name=combo["name"])
                for combo in request.get("combinations", [])
                if not selected_name or combo["name"] == selected_name
                for leg in combo["legs"]
            ]
            if allowed
            else []
        )
        row = (
            next((row for row in combinations if row["name"] == selected_name), {})
            if selected_name
            else next(iter(combinations), {})
        )
        aggregate = row if selected_name else portfolio.get("aggregate") or {}
        risk = aggregate.get("expiry_risk") or {}
        if risk.get("kind") == "mixed_expirations":
            risk_text = "混合到期日：图表假设各到期日标的价格相同；不代表路径最大盈亏"
        elif risk:
            profit = "无限" if risk["unbounded_profit"] else f"{risk['max_profit']:.2f} USD"
            loss = "无限风险" if risk["unbounded_loss"] else f"{risk['max_loss']:.2f} USD"
            roots = " / ".join(f"{root:.2f}" for root in risk["breakevens"]) or (
                "存在零盈亏区间" if risk.get("zero_payoff_ranges") else "无"
            )
            risk_text = f"同日到期 · 最大盈利 {profit} · 最大亏损 {loss} · 盈亏平衡 {roots} · 含建仓佣金"
        else:
            risk_text = "分析适用标准乘数 100；当前曲线为固定 IV 的模型情景"
        count = len(combinations)
        label = row.get("name", "未选择组合") if selected_name or count <= 1 else f"{count} 组期权组合合计"
        pnl = aggregate.get("liquidation_pnl")
        result_time = portfolio.get("observed_at") or portfolio.get("updated_at") or "—"
        summary = f"{label} · 估值报价 {result_time} · 可平仓盈亏 {pnl if pnl is not None else '未估值'} USD"
        diagnostics = (portfolio.get("diagnostics") or []) + (
            [view["portfolio_error"]] if view.get("portfolio_error") else []
        )
        if portfolio and not matches_surface(portfolio, frame):
            diagnostics.insert(0, "结果对应旧快照 · 等待新行情重算")
        if frame.get("mode") == "live" and not market_health(frame, view.get("market_context"))["valid"]:
            diagnostics.insert(0, "行情无效 · 保留只读分析，禁止应用草稿")
        term_time = (term or {}).get("observed_at") or (term or {}).get("updated_at") or "尚无分析"
        return (
            portfolio_figure(portfolio, legs, selected_name),
            legs,
            summary,
            "；".join(diagnostics),
            chart_theme(term_figure(term, "expiration")),
            f"原生期限结构 Actor · 报价 {term_time}"
            + (" · 等待重算" if term and not matches_surface(term, frame) else "")
            if allowed
            else "回放未重算期限结构",
            signature,
            options,
            selection,
            risk_text,
        )

    @app.callback(
        Output("t-fills-grid", "rowData"),
        Output("t-output-fills", "data"),
        Input("t-state", "data"),
        State("t-output-fills", "data"),
    )
    @quiet_outputs
    def fills_table(marker):
        replay = read_snapshot(marker).get("replay") or {}
        metadata = replay.get("combo_contracts") or (replay.get("report") or {}).get("combo_contracts", {})
        rows = []
        for source in replay.get("fills", [])[-200:]:
            row = dict(source)
            combo = metadata.get(row.get("instrument_id"), {})
            row["instrument_id"] = combo.get("label", row.get("instrument_id"))
            row["combo_premium"] = round(float(row["last_px"]) - combo.get("accounting_price_offset", 0), 4)
            row["timestamp"] = inspection_time(row)
            rows.append(row)
        return (rows,)

    @app.callback(
        Output("t-inspection", "data"),
        Output("t-inspection-label", "children"),
        Input("t-fills-grid", "selectedRows"),
        Input("t-workspace", "value"),
        prevent_initial_call=True,
    )
    def inspect_fill(rows, workspace):
        stamp = inspection_time(rows[0]) if rows and workspace == "replay" else None
        return (
            {"timestamp": stamp} if stamp else None,
            f"图表定位：{stamp} · 仅检查，不倒退原生回测时钟" if stamp else "点击成交，在图表标记对应时刻",
        )

    app.clientside_callback(
        """function(marker, n, workspace) {
            const age = marker && marker.at ? Date.now()/1000 - marker.at : Infinity;
            const ok = age < 12;
            if (marker && !ok) {
                for (const id of ['t-strategy-review','t-strategy-start','t-backtest-start','t-chart-load','t-builder-apply','t-leg-buy','t-leg-sell'])
                    window.dash_clientside.set_props(id, {disabled:true});
            }
            if (marker) window.dash_clientside.set_props('t-market-refresh', {disabled:!ok});
            const text = !marker ? '正在连接服务' : !ok ? '服务响应超时' : age > 4 ? '服务响应变慢' : '服务在线';
            return [text, !ok ? 'terminal-service-error' : age > 4 ? 'terminal-service-wait' : 'terminal-service-ok',
                    ok || !marker && n < 6 ? {display:'none'} : {display:'block'},
                    'terminal-root workspace-' + workspace + (ok ? '' : ' terminal-network-stale')];
        }""",
        Output("t-service-state", "children"),
        Output("t-service-state", "className"),
        Output("t-network-warning", "style"),
        Output("t-root", "className"),
        Input("t-state", "data"),
        Input("t-heartbeat", "n_intervals"),
        Input("t-workspace", "value"),
    )
    return app
