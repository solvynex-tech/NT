"""Presentation policy for explicit workspaces; never owns trading inventory."""

import hashlib
import json
import math
from datetime import UTC, datetime

from qwork.core.market import market_health
from qwork.strategies.broken_wing.lifecycle import OrderManagementConfig
from qwork.strategies.broken_wing.models import BrokenWingConfig
from qwork.strategies.broken_wing.risk import AccountRiskConfig

WORKSPACES = {"research": "研究", "replay": "回放", "paper": "Paper 模拟交易", "live": "LIVE 实盘"}


def strategy_parameters(account, mode, margin, positions, execution, symbol, loss, drawdown):
    account = (account or "").strip()
    if mode not in {"paper", "live"} or not account.isalnum() or (mode == "paper") != account.startswith("DU"):
        raise ValueError("交易环境与账户编号不匹配")
    config = BrokenWingConfig(symbol=str(symbol or "").strip().upper(), margin_budget=margin, max_positions=positions)
    OrderManagementConfig(mode=execution)
    AccountRiskConfig(daily_loss_limit_usd=loss, max_drawdown_usd=drawdown)
    return dict(
        account=account,
        mode=mode,
        symbol=config.symbol,
        margin=float(margin),
        positions=int(positions),
        execution=execution,
        loss=float(loss),
        drawdown=float(drawdown),
    )


def account_readiness(view, account, now=None):
    now = now or datetime.now(UTC)
    if view.get("connected") is False:
        return "账户监看已断开"
    if view.get("account_id") != "IB-" + account:
        return "尚未收到目标账户回报"
    try:
        stamp = datetime.fromisoformat(view["account_timestamp"])
        age = (now - stamp).total_seconds()
        if not 0 <= age <= 300:
            return "账户回报过期或时间异常，请等待更新"
        if view.get("base_currency") != "USD" or not view.get("is_reported"):
            return "需要券商回报的 USD 基础币账户"
        values = {row["field"]: row["value"] for row in view.get("account", [])}
        equity, funds = float(values["NetLiquidation"]), float(values["AvailableFunds"])
        if not all(math.isfinite(value) for value in [equity, funds]) or equity <= 0 or funds <= 100:
            return "账户净值或可用资金不足，尚不可启动"
    except (ValueError, KeyError, TypeError):
        return "账户回报不完整，尚不可启动"
    return None


def candidate_identity(candidate, frame):
    value = [
        frame.get("symbol"),
        frame.get("observed_at"),
        frame.get("surface_revision"),
        candidate.get("expiration"),
        candidate.get("right"),
        candidate.get("strikes"),
        candidate.get("legs"),
    ]
    return hashlib.sha256(json.dumps(value, sort_keys=True).encode()).hexdigest()[:24]


def chain_frame(snapshot, workspace):
    if workspace == "replay":
        return (snapshot.get("replay") or {}).get("frame") or {}
    frame = snapshot.get("frame") or {}
    if workspace in {"paper", "live"} and (
        frame.get("mode") != "live" or not market_health(frame, snapshot.get("market_context"))["valid"]
    ):
        return {}
    return frame


def workspace_chart(snapshot, workspace):
    chart = snapshot.get("chart") or {}
    if workspace == "replay":
        replay = snapshot.get("replay") or {}
        symbol = replay.get("config", {}).get("symbol") or (replay.get("report") or {}).get("symbol")
        if chart.get("day") != replay.get("day") or (symbol and chart.get("contract", {}).get("symbol") != symbol):
            return dict(status="请选择本次回放同日、同标的的期权图表", rows=[], source="回放图表来源不匹配")
    if workspace in {"paper", "live"}:
        return dict(status="当前 K 线仅提供历史 NBBO；请在研究工作区查看", rows=[], source="交易工作区不显示历史 K 线")
    return chart


def permissions(snapshot, workspace, parameters):
    replay = snapshot.get("replay") or {}
    strategy = snapshot.get("strategy") or {}
    jobs = snapshot.get("jobs") or {}
    trading = bool(strategy.get("running"))
    busy = bool(jobs.get("backtest")) or replay.get("status") in {"preparing", "running", "stopping"}
    playing = busy and replay.get("status") == "running" and not replay.get("stop_requested")
    reason = "请切换到 Paper 或 LIVE 工作区"
    if workspace in {"paper", "live"}:
        try:
            payload = strategy_parameters(*parameters)
            reason = "" if payload["mode"] == workspace else "交易环境与工作区不一致"
            reason = reason or account_readiness(snapshot.get("broker") or {}, payload["account"]) or ""
        except (ValueError, TypeError):
            reason = "请填写有效账户、预算、执行方式和风控参数"
        if trading:
            reason = "策略已运行，参数变化不会应用到当前进程"
        elif strategy.get("recovery_required"):
            reason = "已有后台策略或恢复记录待核验，请先恢复监看"
        elif busy:
            reason = "请先结束当前回测"
        elif (snapshot.get("broker") or {}).get("operator_stop"):
            reason = "账户存在持久停止信号，请先核验持仓与恢复状态"
    return dict(
        review=bool(reason),
        review_reason=reason or "账户已核验，可审阅启动参数",
        start_backtest=workspace != "replay" or busy or trading,
        pause=not playing or bool(replay.get("paused")),
        play=not playing or not replay.get("paused"),
        step=not playing or not replay.get("paused"),
        stop_backtest=not busy or bool(replay.get("stop_requested")),
        report=busy,
        stop_strategy=not trading or bool(strategy.get("stop_requested")),
    )
