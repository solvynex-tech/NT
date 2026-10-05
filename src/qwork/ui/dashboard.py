from __future__ import annotations

import json
from datetime import datetime, timedelta
from pathlib import Path

import plotly.graph_objects as go
from dash import Dash, Input, Output, State, ctx, dash_table, dcc, html, no_update
from flask import jsonify
from flask import request as flask_request
from pydantic import ValidationError

from qwork.actors.broken_wing.models import ButterflyScanRequest
from qwork.actors.portfolio_pnl.models import PortfolioRequest, PositionLeg
from qwork.core.domain import eastern
from qwork.core.state import VolatilityRequest


def empty_figure(message):
    figure = go.Figure()
    figure.add_annotation(text=message, x=0.5, y=0.5, xref="paper", yref="paper", showarrow=False)
    figure.update_layout(
        template="plotly_dark", paper_bgcolor="#111a27", plot_bgcolor="#111a27", margin=dict(l=48, r=22, t=40, b=40)
    )
    return figure


def surface_figure(frame, right):
    points = [point for point in (frame or {}).get("points", []) if point["right"] == right]
    if not points:
        return empty_figure("等待有效期权报价；休市可选择历史快照")
    strikes = sorted({point["strike"] for point in points})
    expirations = sorted({point["expiration"] for point in points})
    lookup = {(point["expiration"], point["strike"]): point for point in points}
    dtes = [next(point["dte"] for point in points if point["expiration"] == expiration) for expiration in expirations]
    z = [
        [lookup[(expiration, strike)]["iv"] * 100 if (expiration, strike) in lookup else None for strike in strikes]
        for expiration in expirations
    ]
    fig = go.Figure(
        go.Surface(
            x=strikes,
            y=dtes,
            z=z,
            connectgaps=False,
            colorscale="Viridis",
            colorbar=dict(title="IV %"),
            hovertemplate="行权价 %{x}<br>剩余 %{y:.2f} 天<br>IV %{z:.2f}%<extra></extra>",
        )
    )
    fig.add_trace(
        go.Scatter3d(
            x=[point["strike"] for point in points],
            y=[point["dte"] for point in points],
            z=[point["iv"] * 100 for point in points],
            mode="markers",
            marker=dict(size=2, color="#e2e8f0"),
            name="实际观察点",
            text=[f"{point['expiration']} · {point['method']}<br>报价 {point['timestamp']}" for point in points],
            hovertemplate="%{text}<br>行权价 %{x}<br>IV %{z:.2f}%<extra></extra>",
        )
    )
    fig.update_layout(
        template="plotly_dark",
        title=f"{frame['symbol']} · {right.upper()} IV 曲面",
        paper_bgcolor="#111a27",
        scene=dict(xaxis_title="行权价", yaxis_title="剩余天数", zaxis_title="年化 IV (%)"),
        margin=dict(l=0, r=0, t=42, b=0),
        uirevision=f"{frame['symbol']}-{right}",
    )
    return fig


def term_figure(frame, axis):
    rows = (frame or {}).get("rows", [])
    if not rows:
        return empty_figure("等待 ATM 两侧都有有效报价的期限数据")
    fig = go.Figure()
    for right, color in [("call", "#38bdf8"), ("put", "#c084fc")]:
        selected = sorted([row for row in rows if row["right"] == right], key=lambda row: row["dte"])
        fig.add_trace(
            go.Scatter(
                x=[row["expiration"] if axis == "expiration" else row["dte"] for row in selected],
                y=[row["iv"] * 100 for row in selected],
                mode="lines+markers",
                name=right.upper(),
                line=dict(color=color),
                marker=dict(
                    size=8, color=["#fbbf24" if row["calendar_variance_warning"] else color for row in selected]
                ),
                text=[
                    f"到期 {row['expiration']}<br>远期 {row['forward']:.2f}<br>ATM 区间 {row['lower_strike']:g} / {row['upper_strike']:g}<br>{row['method']}"
                    for row in selected
                ],
                hovertemplate="%{text}<br>ATM IV %{y:.2f}%<extra></extra>",
            )
        )
    fig.update_layout(
        template="plotly_dark",
        title=f"{frame['symbol']} · ATM IV 期限结构",
        paper_bgcolor="#111a27",
        plot_bgcolor="#111a27",
        xaxis_title="到期日" if axis == "expiration" else "剩余天数",
        yaxis_title="年化 ATM IV (%)",
        margin=dict(l=58, r=20, t=44, b=50),
        legend=dict(orientation="h", y=1.12),
        uirevision=f"{frame['symbol']}-{axis}",
    )
    return fig


def create_dashboard(state):
    config, _, _, _ = state.controls()
    app = Dash(
        __name__, title="QWork · 波动率工作台", assets_folder=str(Path(__file__).parent / "assets"), update_title=None
    )
    today = datetime.now(eastern()).date()
    previous = today - timedelta(days=1)
    while previous.weekday() >= 5:
        previous -= timedelta(days=1)

    def field(label, component):
        return html.Label([html.Span(label), component], className="field")

    app.layout = html.Div(
        [
            html.Header(
                [
                    html.Div(
                        [
                            html.Div("QWORK / NAUTILUS", className="eyebrow"),
                            html.H1("波动率工作台"),
                            html.P("曲面 / 期限结构 / 破翼蝴蝶 / 组合盈亏 · 四个 DataActor"),
                        ]
                    ),
                    html.Div(id="badge", className="badge"),
                ]
            ),
            html.Div(
                [
                    html.Aside(
                        [
                            html.H3("标的与数据"),
                            field(
                                "标的名称",
                                dcc.Input(
                                    id="symbol",
                                    value=config.symbol,
                                    type="text",
                                    debounce=True,
                                    placeholder="SPY / QQQ / SPXW",
                                ),
                            ),
                            field(
                                "数据源",
                                dcc.Dropdown(
                                    id="source",
                                    value=config.source,
                                    clearable=False,
                                    options=[
                                        {"label": "Thetadata · 真实数据", "value": "theta"},
                                        {"label": "合成测试数据", "value": "fixture"},
                                    ],
                                ),
                            ),
                            field(
                                "查询方式",
                                dcc.Dropdown(
                                    id="mode",
                                    value=config.mode,
                                    clearable=False,
                                    options=[
                                        {"label": "快照轮询（可能延迟）", "value": "live"},
                                        {"label": "历史快照 · ET", "value": "historical"},
                                    ],
                                ),
                            ),
                            html.Div(
                                [
                                    field(
                                        "历史日期",
                                        dcc.DatePickerSingle(
                                            id="as-of",
                                            date=(config.as_of or previous).isoformat(),
                                            max_date_allowed=today.isoformat(),
                                        ),
                                    ),
                                    field("历史时间 ET", dcc.Input(id="at", value=config.at.isoformat(), type="text")),
                                ],
                                id="historical-controls",
                            ),
                            html.H3("到期范围"),
                            html.Div(
                                [
                                    field(
                                        "最小 DTE", dcc.Input(id="min-dte", type="number", value=config.min_dte, min=0)
                                    ),
                                    field(
                                        "最大 DTE", dcc.Input(id="max-dte", type="number", value=config.max_dte, min=1)
                                    ),
                                ],
                                className="two-fields",
                            ),
                            field(
                                "最多到期日",
                                dcc.Input(
                                    id="max-expirations", type="number", value=config.max_expirations, min=1, max=24
                                ),
                            ),
                            field(
                                "指定到期日（空=自动）",
                                dcc.Dropdown(
                                    id="expirations",
                                    multi=True,
                                    value=[item.isoformat() for item in config.expirations],
                                    placeholder="完成首次查询后可选择",
                                ),
                            ),
                            field(
                                "刷新间隔（秒）",
                                dcc.Input(id="refresh", type="number", value=config.refresh_seconds, min=5, max=3600),
                            ),
                            html.Details(
                                [
                                    html.Summary("定价与报价质量"),
                                    field(
                                        "IV 方法",
                                        dcc.Dropdown(
                                            id="iv-method",
                                            value=config.iv_method,
                                            clearable=False,
                                            options=[
                                                {"label": "优先数据商 IV", "value": "auto"},
                                                {"label": "QuantLib 报价反算", "value": "quantlib"},
                                            ],
                                        ),
                                    ),
                                    field(
                                        "标的现价（可选）",
                                        dcc.Input(
                                            id="spot",
                                            type="number",
                                            value=config.spot,
                                            min=0.01,
                                            placeholder="留空：Call/Put 平价估计远期",
                                        ),
                                    ),
                                    field(
                                        "年化利率 %（QuantLib）",
                                        dcc.Input(id="rate", type="number", value=config.rate * 100),
                                    ),
                                    field(
                                        "股息收益率 %（手动现价）",
                                        dcc.Input(id="dividend", type="number", value=config.dividend_yield * 100),
                                    ),
                                    field(
                                        "远期附近行权价范围 ±%",
                                        dcc.Input(
                                            id="band", type="number", value=config.strike_band * 100, min=1, max=100
                                        ),
                                    ),
                                    field(
                                        "最大相对买卖价差 %",
                                        dcc.Input(
                                            id="spread", type="number", value=config.max_relative_spread * 100, min=1
                                        ),
                                    ),
                                    field(
                                        "最大报价年龄（秒）",
                                        dcc.Input(id="age", type="number", value=config.max_quote_age_seconds, min=1),
                                    ),
                                ]
                            ),
                            html.Button("应用参数 / 立即刷新", id="apply", n_clicks=0, className="primary"),
                            html.Button("暂停更新", id="pause", n_clicks=0),
                            html.Div(id="validation", className="validation"),
                            html.P(
                                "历史数据不会自动替代实时查询。图上缺口表示报价缺失，不做外推。", className="helper"
                            ),
                        ]
                    ),
                    html.Main(
                        [
                            html.Div(
                                [html.Div(id="status"), html.Div(id="stats", className="stats")], className="status-bar"
                            ),
                            html.Div(
                                [
                                    field(
                                        "曲面方向",
                                        dcc.Dropdown(
                                            id="right", value="call", options=["call", "put"], clearable=False
                                        ),
                                    ),
                                    field(
                                        "期限横轴",
                                        dcc.Dropdown(
                                            id="axis",
                                            value="expiration",
                                            clearable=False,
                                            options=[
                                                {"label": "到期日", "value": "expiration"},
                                                {"label": "剩余天数", "value": "dte"},
                                            ],
                                        ),
                                    ),
                                ],
                                className="view-controls",
                            ),
                            html.Div(
                                [
                                    dcc.Graph(
                                        id="surface",
                                        figure=empty_figure("等待 Actor 启动"),
                                        config={"displaylogo": False},
                                    ),
                                    dcc.Graph(
                                        id="term", figure=empty_figure("等待 Actor 启动"), config={"displaylogo": False}
                                    ),
                                ],
                                className="charts",
                            ),
                            html.H3("期限数据与质量"),
                            dash_table.DataTable(
                                id="table",
                                columns=[
                                    {"name": title, "id": key}
                                    for key, title in [
                                        ("expiration", "到期日"),
                                        ("right", "方向"),
                                        ("dte", "剩余天数"),
                                        ("iv_percent", "ATM IV %"),
                                        ("forward", "远期价"),
                                        ("method", "IV 方法"),
                                        ("timestamp", "报价时间"),
                                        ("warning", "总方差下降"),
                                    ]
                                ],
                                data=[],
                                page_size=12,
                                style_table={"overflowX": "auto"},
                                style_cell={
                                    "backgroundColor": "#111a27",
                                    "color": "#e2e8f0",
                                    "border": "1px solid #253249",
                                    "fontFamily": "Segoe UI",
                                    "textAlign": "left",
                                    "padding": "10px",
                                },
                                style_header={"backgroundColor": "#172438", "fontWeight": "bold"},
                            ),
                            html.Div(id="notes", className="notes"),
                            html.H3("破翼蝴蝶候选 · 仅研究输出"),
                            html.P(
                                "获利概率是风险中性模型估计，不代表回测胜率。候选采用买 ASK、卖 BID，并计入滑点和双边佣金；不会发送交易委托。",
                                className="helper",
                            ),
                            html.Div(
                                [
                                    field(
                                        "最低模型获利概率 %（严格大于）",
                                        dcc.Input(id="scan-probability", type="number", value=70, min=0, max=99.99),
                                    ),
                                    field(
                                        "最大盈利 / 最大亏损阈值",
                                        dcc.Input(id="scan-ratio", type="number", value=1, min=0.01),
                                    ),
                                    field(
                                        "盈亏比条件",
                                        dcc.Dropdown(
                                            id="scan-mode",
                                            value="at_least",
                                            clearable=False,
                                            options=[
                                                {"label": "至少为阈值", "value": "at_least"},
                                                {"label": "接近阈值（±0.02）", "value": "near"},
                                                {"label": "不超过阈值", "value": "at_most"},
                                            ],
                                        ),
                                    ),
                                    field(
                                        "扫描到期日（空=曲面全部）",
                                        dcc.Dropdown(id="scan-expirations", multi=True, value=[]),
                                    ),
                                    field("组数", dcc.Input(id="scan-units", type="number", value=1, min=1, max=100)),
                                    field(
                                        "每张每边佣金 USD", dcc.Input(id="scan-fee", type="number", value=0.65, min=0)
                                    ),
                                    field(
                                        "每张报价滑点 USD",
                                        dcc.Input(id="scan-slippage", type="number", value=0.01, min=0),
                                    ),
                                ],
                                className="scan-controls",
                            ),
                            html.Button("应用组合筛选条件", id="scan-apply", n_clicks=0, className="primary"),
                            html.Div(id="scan-validation", className="validation"),
                            html.Div(id="scan-status", className="helper"),
                            dash_table.DataTable(
                                id="candidates",
                                columns=[
                                    {"name": title, "id": key}
                                    for key, title in [
                                        ("expiration", "到期日"),
                                        ("right", "偏斜方向"),
                                        ("strikes", "买1 / 卖2 / 买1"),
                                        ("model_probability_percent", "模型获利概率 %"),
                                        ("profit_loss_ratio", "最大盈亏比"),
                                        ("max_profit", "最大盈利 USD"),
                                        ("max_loss", "最大亏损 USD"),
                                        ("entry_cost", "估算净成本 USD"),
                                        ("breakevens", "盈亏平衡点"),
                                        ("quote_timestamp", "报价时间"),
                                    ]
                                ],
                                data=[],
                                page_size=10,
                                row_selectable="multi",
                                selected_rows=[],
                                style_table={"overflowX": "auto"},
                                style_cell={
                                    "backgroundColor": "#111a27",
                                    "color": "#e2e8f0",
                                    "border": "1px solid #253249",
                                    "textAlign": "left",
                                    "padding": "10px",
                                },
                                style_header={"backgroundColor": "#172438", "fontWeight": "bold"},
                            ),
                            html.Div(id="scan-notes", className="notes"),
                            html.H3("一个或多个期权组合 · 实时与到期盈亏"),
                            html.P(
                                "填写下方参数逐腿加入同名组合，或导入选中候选。建仓权利金为每股价格，数量为合约张数；也可直接编辑 JSON。",
                                className="helper",
                            ),
                            html.Button("复制选中候选到组合输入", id="copy-candidates", n_clicks=0),
                            html.Div(
                                [
                                    field("组合名称", dcc.Input(id="leg-name", value="组合 A", type="text")),
                                    field(
                                        "到期日",
                                        dcc.DatePickerSingle(
                                            id="leg-expiration",
                                            date=(config.as_of or datetime.now(eastern()).date()) + timedelta(days=7),
                                        ),
                                    ),
                                    field("行权价", dcc.Input(id="leg-strike", type="number", min=0.01)),
                                    field(
                                        "Call / Put",
                                        dcc.Dropdown(
                                            id="leg-right", options=["call", "put"], value="put", clearable=False
                                        ),
                                    ),
                                    field(
                                        "方向",
                                        dcc.Dropdown(
                                            id="leg-side",
                                            options=[
                                                {"label": "买入", "value": "buy"},
                                                {"label": "卖出", "value": "sell"},
                                            ],
                                            value="buy",
                                            clearable=False,
                                        ),
                                    ),
                                    field("合约张数", dcc.Input(id="leg-quantity", type="number", value=1, min=1)),
                                    field("实际建仓权利金 USD", dcc.Input(id="leg-premium", type="number", min=0)),
                                ],
                                className="scan-controls",
                            ),
                            html.Button("将这一腿加入组合输入", id="add-leg", n_clicks=0),
                            html.Div(id="draft-validation", className="validation"),
                            dcc.Textarea(
                                id="portfolio-json",
                                value="[]",
                                placeholder='[{"name":"组合 A","legs":[{"symbol":"SPY","expiration":"YYYY-MM-DD","strike":600,"right":"call","side":"buy","quantity":1,"entry_premium":2.5}]}]',
                                style={"width": "100%", "height": "180px", "marginTop": "12px"},
                            ),
                            html.Div(
                                [
                                    field(
                                        "每张每边佣金 USD",
                                        dcc.Input(id="portfolio-fee", type="number", value=0.65, min=0),
                                    ),
                                    field(
                                        "情景价格范围 ±%",
                                        dcc.Input(id="portfolio-band", type="number", value=20, min=1, max=95),
                                    ),
                                ],
                                className="view-controls",
                            ),
                            html.Button("应用组合并开始估值", id="portfolio-apply", n_clicks=0, className="primary"),
                            html.Div(id="portfolio-validation", className="validation"),
                            html.Div(id="portfolio-status", className="helper"),
                            html.Div(
                                [
                                    dcc.Graph(id="current-pnl", figure=empty_figure("请先输入组合")),
                                    dcc.Graph(id="expiry-pnl", figure=empty_figure("请先输入组合")),
                                ],
                                className="charts",
                            ),
                            html.Div(id="portfolio-notes", className="notes"),
                        ]
                    ),
                ],
                className="workspace",
            ),
            dcc.Interval(id="tick", interval=1000),
            dcc.Store(id="render-key"),
        ]
    )
    app.server.config["MAX_CONTENT_LENGTH"] = 1024 * 1024

    @app.server.before_request
    def local_only():
        host = flask_request.host.split(":")[0]
        origin = flask_request.headers.get("Origin")
        if host not in {"127.0.0.1", "localhost"} or (
            origin
            and origin
            not in {f"http://{flask_request.host}", f"http://127.0.0.1:{flask_request.host.rsplit(':', 1)[-1]}"}
        ):
            return jsonify({"error": "请求来源未授权"}), 403

    @app.server.get("/api/snapshot")
    def snapshot():
        return jsonify(state.snapshot())

    @app.callback(Output("historical-controls", "style"), Input("mode", "value"))
    def history_controls(mode):
        return {} if mode == "historical" else {"display": "none"}

    @app.callback(
        Output("validation", "children"),
        Input("apply", "n_clicks"),
        *[State(name, "value") for name in ["symbol", "source", "mode"]],
        State("as-of", "date"),
        *[
            State(name, "value")
            for name in [
                "at",
                "min-dte",
                "max-dte",
                "max-expirations",
                "refresh",
                "expirations",
                "spot",
                "rate",
                "dividend",
                "band",
                "spread",
                "age",
                "iv-method",
            ]
        ],
        prevent_initial_call=True,
    )
    def apply(
        _clicks,
        symbol,
        source,
        mode,
        as_of,
        at,
        min_dte,
        max_dte,
        count,
        refresh,
        expirations,
        spot,
        rate,
        dividend,
        band,
        spread,
        age,
        method,
    ):
        try:
            state.configure(
                VolatilityRequest(
                    symbol=symbol,
                    source=source,
                    mode=mode,
                    as_of=as_of if mode == "historical" else None,
                    at=at,
                    min_dte=min_dte,
                    max_dte=max_dte,
                    max_expirations=count,
                    refresh_seconds=refresh,
                    expirations=expirations or [],
                    spot=spot,
                    rate=float(rate) / 100,
                    dividend_yield=float(dividend) / 100,
                    strike_band=float(band) / 100,
                    max_relative_spread=float(spread) / 100,
                    max_quote_age_seconds=age,
                    iv_method=method,
                    required_expirations=state.controls()[0].required_expirations,
                    required_strikes=state.controls()[0].required_strikes,
                )
            )
            return "参数已应用"
        except (ValidationError, ValueError, TypeError) as exc:
            if isinstance(exc, ValidationError):
                return "；".join(
                    f"{'.'.join(map(str, error['loc'])) or '参数'}: {error['msg']}"
                    for error in exc.errors(include_input=False, include_url=False)
                )
            return "请填写有效的数值参数"

    @app.callback(Output("pause", "children"), Input("pause", "n_clicks"), prevent_initial_call=True)
    def pause(_clicks):
        with state.lock:
            state.paused = not state.paused
            return "恢复更新" if state.paused else "暂停更新"

    @app.callback(
        Output("scan-validation", "children"),
        Input("scan-apply", "n_clicks"),
        *[
            State(name, "value")
            for name in [
                "scan-probability",
                "scan-ratio",
                "scan-mode",
                "scan-expirations",
                "scan-units",
                "scan-fee",
                "scan-slippage",
            ]
        ],
        prevent_initial_call=True,
    )
    def apply_scan(_clicks, probability, ratio, mode, expirations, units, fee, slippage):
        try:
            state.configure_scan(
                ButterflyScanRequest(
                    expirations=expirations or [],
                    min_probability=float(probability) / 100,
                    target_ratio=ratio,
                    ratio_mode=mode,
                    units=units,
                    commission_per_contract=fee,
                    slippage_per_contract=slippage,
                )
            )
            return "组合筛选条件已应用"
        except (ValueError, TypeError):
            return "请填写有效的概率、盈亏比、组数与费用"

    @app.callback(
        Output("candidates", "data"),
        Output("scan-status", "children"),
        Output("scan-notes", "children"),
        Output("scan-expirations", "options"),
        Input("tick", "n_intervals"),
    )
    def render_scan(_tick):
        value = state.snapshot()
        scan = value["scan"]
        candidates = [
            {
                **row,
                "strikes": " / ".join(f"{strike:g}" for strike in row["strikes"]),
                "breakevens": " / ".join(f"{strike:.3f}" for strike in row["breakevens"]),
                "model_probability_percent": round(row["model_probability"] * 100, 3),
                "profit_loss_ratio": round(row["profit_loss_ratio"], 3),
                "max_profit": round(row["max_profit"], 2),
                "max_loss": round(row["max_loss"], 2),
                "entry_cost": round(row["entry_cost"], 2),
            }
            for row in (scan or {}).get("candidates", [])
        ]
        status = value["scan_error"] or (
            f"筛选到 {len(candidates)} 个组合；没有符合条件时保持空列表" if scan else "等待曲面结果与组合扫描"
        )
        options = [{"label": item, "value": item} for item in (value["surface"] or {}).get("expirations", [])]
        return candidates, status, [html.P(note) for note in (scan or {}).get("diagnostics", [])], options

    @app.callback(
        Output("portfolio-json", "value"),
        Output("draft-validation", "children"),
        Input("copy-candidates", "n_clicks"),
        Input("add-leg", "n_clicks"),
        State("candidates", "selected_rows"),
        State("candidates", "data"),
        State("portfolio-json", "value"),
        *[
            State(name, "value")
            for name in ["leg-name", "leg-strike", "leg-right", "leg-side", "leg-quantity", "leg-premium"]
        ],
        State("leg-expiration", "date"),
        prevent_initial_call=True,
    )
    def copy_candidates(
        _clicks, _add, selection, rows, contents, name, strike, right, side, quantity, premium, expiration
    ):
        try:
            if len(contents or "") > 200000:
                return no_update, "组合输入过大"
            value = json.loads(contents or "[]")
            combinations = value if isinstance(value, list) else value.get("combinations", [value])
            combinations = PortfolioRequest(combinations=combinations).model_dump(mode="json")["combinations"]
            if ctx.triggered_id == "add-leg":
                leg = PositionLeg(
                    symbol=state.controls()[0].symbol,
                    expiration=expiration,
                    strike=strike,
                    right=right,
                    side=side,
                    quantity=quantity,
                    entry_premium=premium,
                ).model_dump(mode="json")
                existing = next((combo for combo in combinations if combo["name"] == name), None)
                if existing:
                    existing["legs"].append(leg)
                else:
                    combinations.append({"name": name, "legs": [leg]})
                PortfolioRequest(combinations=combinations)
                return json.dumps(
                    combinations, ensure_ascii=False, indent=2
                ), "这一腿已加入草稿；点击“应用组合并开始估值”后生效"
        except (ValueError, TypeError, AttributeError):
            return no_update, "请检查组合 JSON、名称、到期日和腿参数"
        for index in selection or []:
            if 0 <= index < len(rows):
                candidate = rows[index]
                combinations.append(
                    {
                        "name": f"候选 {len(combinations) + 1} · {candidate['expiration']} {candidate['right']}",
                        "legs": [
                            {
                                **{
                                    key: leg[key]
                                    for key in ("symbol", "expiration", "strike", "right", "side", "quantity")
                                },
                                "entry_premium": leg["price"],
                            }
                            for leg in candidate["legs"]
                        ],
                    }
                )
        if not selection:
            return no_update, "请先在候选表格中勾选组合"
        try:
            PortfolioRequest(combinations=combinations)
        except ValueError:
            return no_update, "导入后组合超过数量限制或名称重复，请调整组合输入"
        return json.dumps(
            combinations, ensure_ascii=False, indent=2
        ), "候选已加入草稿；报价仅作假设成本，请填写实际成交价后应用"

    @app.callback(
        Output("portfolio-validation", "children"),
        Input("portfolio-apply", "n_clicks"),
        State("portfolio-json", "value"),
        State("portfolio-fee", "value"),
        State("portfolio-band", "value"),
        prevent_initial_call=True,
    )
    def apply_portfolio(_clicks, contents, fee, band):
        try:
            if len(contents or "") > 200000:
                return "组合输入过大"
            value = json.loads(contents or "[]")
            combinations = value if isinstance(value, list) else value.get("combinations", [value])
            state.configure_portfolio(
                PortfolioRequest(
                    combinations=combinations, commission_per_contract=fee, scenario_band=float(band) / 100
                )
            )
            return "组合已应用；候选导入的权利金是假设成本，请复核或填写实际成交价"
        except (ValueError, TypeError, AttributeError) as exc:
            if isinstance(exc, ValidationError):
                return "；".join(error["msg"] for error in exc.errors(include_input=False, include_url=False))
            return (
                str(exc)
                if isinstance(exc, ValueError) and not isinstance(exc, json.JSONDecodeError)
                else "请输入有效的组合 JSON 与参数"
            )

    @app.callback(
        Output("current-pnl", "figure"),
        Output("expiry-pnl", "figure"),
        Output("portfolio-status", "children"),
        Output("portfolio-notes", "children"),
        Input("tick", "n_intervals"),
    )
    def render_portfolio(_tick):
        value = state.snapshot()
        portfolio = value["portfolio"]
        if not portfolio or not portfolio["combinations"]:
            notes = [html.P(note) for note in (portfolio or {}).get("diagnostics", [])]
            return (
                empty_figure("等待组合及完整报价"),
                empty_figure("等待组合及完整报价"),
                value["portfolio_error"] or "尚无可估值组合",
                notes,
            )
        current, expiry = go.Figure(), go.Figure()
        for row in portfolio["combinations"]:
            if row["current_pnl"] is not None:
                current.add_trace(
                    go.Scatter(x=row["scenario_prices"], y=row["current_pnl"], name=row["name"], mode="lines")
                )
            expiry.add_trace(
                go.Scatter(
                    x=row["scenario_prices"],
                    y=row["expiry_pnl"],
                    name=row["name"] + " · " + ",".join(row["expirations"]),
                    mode="lines",
                )
            )
        if len(portfolio["combinations"]) > 1:
            for figure, key in ((current, "current_pnl"), (expiry, "expiry_pnl")):
                if portfolio["aggregate"][key] is not None:
                    figure.add_trace(
                        go.Scatter(
                            x=portfolio["aggregate"]["scenario_prices"],
                            y=portfolio["aggregate"][key],
                            name="组合合计",
                            line=dict(width=3, dash="dash"),
                        )
                    )
        if not current.data:
            current = empty_figure("缺少有效报价，当前盈亏暂不可估值")
        for figure, title in ((current, "当前估值盈亏曲线 · 固定 IV 情景"), (expiry, "到期盈亏曲线 · 静态终值情景")):
            figure.update_layout(
                template="plotly_dark",
                title=title,
                paper_bgcolor="#111a27",
                plot_bgcolor="#111a27",
                xaxis_title="标的价格",
                yaxis_title="盈亏 USD",
                margin=dict(l=60, r=20, t=45, b=50),
                uirevision=portfolio["symbol"],
            )
            figure.add_hline(y=0, line_dash="dot")
        status = " · ".join(
            f"{row['name']} 可平仓盈亏 {row['liquidation_pnl']:.2f} USD"
            if row["liquidation_pnl"] is not None
            else f"{row['name']} 缺少报价"
            for row in portfolio["combinations"]
        )
        timestamps = [row["quote_timestamp"] for row in portfolio["combinations"] if row["quote_timestamp"]]
        status += " · 报价 " + (min(timestamps) if timestamps else "无")
        if value["paused"] or value["status"] in {"error", "stopped"}:
            status += " · 更新已暂停或失败，请检查行情状态"
        return current, expiry, status, [html.P(note) for note in portfolio["diagnostics"]]

    @app.callback(
        Output("surface", "figure"),
        Output("term", "figure"),
        Output("table", "data"),
        Output("status", "children"),
        Output("stats", "children"),
        Output("notes", "children"),
        Output("badge", "children"),
        Output("expirations", "options"),
        Output("render-key", "data"),
        Input("tick", "n_intervals"),
        Input("right", "value"),
        Input("axis", "value"),
        State("render-key", "data"),
    )
    def render(_tick, right, axis, previous_key):
        value = state.snapshot()
        surface, term = value["surface"], value["term"]
        key = [value["generation"], (surface or {}).get("updated_at"), (term or {}).get("updated_at"), right, axis]
        unchanged = key == previous_key
        table = [
            {
                **row,
                "dte": round(row["dte"], 2),
                "iv_percent": round(row["iv"] * 100, 3),
                "forward": round(row["forward"], 3),
                "warning": "是" if row["calendar_variance_warning"] else "",
            }
            for row in (term or {}).get("rows", [])
        ]
        notes = list((surface or {}).get("notes", []))
        if any(row["calendar_variance_warning"] for row in (term or {}).get("rows", [])):
            notes.append("部分期限总方差下降，已用黄色标记；请检查报价质量与模型差异")
        oldest = max((point["age_seconds"] for point in (surface or {}).get("points", [])), default=0)
        if surface and surface["mode"] == "live":
            oldest += max(
                0, (datetime.now().astimezone() - datetime.fromisoformat(surface["observed_at"])).total_seconds()
            )
        stats = f"{len((surface or {}).get('expirations', []))} 个到期日 · {len((surface or {}).get('points', []))} 个 IV 点 · 剔除 {(surface or {}).get('rejected', 0)} · 最大报价年龄 {oldest:.0f}s"
        badge = (
            "合成测试数据"
            if value["request"]["source"] == "fixture"
            else "历史快照 · 非实时"
            if value["request"]["mode"] == "historical"
            else "Thetadata 快照 · 可能延迟"
        )
        status = ("已暂停 · " if value["paused"] else "") + value["message"] + " · " + value["updated_at"]
        options = [{"label": item, "value": item} for item in (surface or {}).get("available_expirations", [])]
        return (
            no_update if unchanged else surface_figure(surface, right),
            no_update if unchanged else term_figure(term, axis),
            no_update if unchanged else table,
            status,
            stats,
            [html.P(note) for note in notes],
            badge,
            options,
            key,
        )

    return app
