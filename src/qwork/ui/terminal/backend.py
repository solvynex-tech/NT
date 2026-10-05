"""Thread-safe terminal snapshots and explicitly requested native jobs."""

import asyncio
import json
import os
import sqlite3
import subprocess
import sys
import traceback
import uuid
from copy import deepcopy
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from threading import Event, RLock, Thread

import psutil
from nautilus_trader.common import DataActor, DataActorConfig, Environment, LoggerConfig, LogLevel
from nautilus_trader.model import AccountId, ActorId, Currency, OptionContract, TraderId, Venue

from qwork.backtesting.broken_wing import records, run_backtest
from qwork.backtesting.execution import ReplayExecutionConfig
from qwork.backtesting.playback import ReplayController
from qwork.core.config import ROOT
from qwork.core.domain import Contract, eastern, normalize_quotes
from qwork.core.market import market_health, matches_surface
from qwork.core.state import DashboardState, VolatilityRequest
from qwork.runtime.actors import VolatilityRuntime
from qwork.strategies.broken_wing.models import BrokenWingConfig
from qwork.strategies.broken_wing.recovery import CheckpointStore

from .charts import ReplayCandles, candle_rows
from .reliability import LatestWorker, OwnedProcess, ProcessRegistry
from .workflow import account_readiness, candidate_identity, strategy_parameters


class SnapshotCollector:
    """Explicit cached research input for the existing native analysis actors."""

    def __init__(self, frame):
        self.frame = deepcopy(frame)

    def collect(self, request, generation):
        if (
            request.mode != "historical"
            or request.symbol != self.frame["symbol"]
            or str(request.as_of) != self.frame["observed_at"][:10]
        ):
            raise ValueError("缓存分析只能使用所选历史快照")
        return dict(deepcopy(self.frame), generation=generation)

    def close(self):
        pass


def display_rows(rows, metadata, fields):
    result = []
    for original in rows:
        row = dict(original)
        descriptor = metadata.get(str(row.get("instrument_id"))) or {}
        for key in fields:
            if row.get(key) is not None and descriptor:
                row[key] = round(float(row[key]) - descriptor["accounting_price_offset"], 4)
        row["instrument_id"] = descriptor.get("label", row.get("instrument_id"))
        for key in ["ts_init", "ts_opened"]:
            if isinstance(row.get(key), int):
                row[key] = datetime.fromtimestamp(row[key] / 1e9, UTC).astimezone(eastern()).isoformat()
        result.append(row)
    return result


def journal_tail(path):
    if not path.exists():
        return []
    with path.open("rb") as stream:
        stream.seek(0, 2)
        stream.seek(max(0, stream.tell() - 131072))
        lines = stream.read().decode("utf-8", errors="replace").splitlines()[-100:]
    events = []
    for line in lines:
        try:
            event = json.loads(line)
            events.append(
                dict(
                    timestamp=event["timestamp"],
                    kind=event["kind"],
                    key=event.get("key"),
                    reason=event.get("reason"),
                    failures="; ".join(event.get("failures", [])),
                )
            )
        except (ValueError, KeyError):
            continue
    return events


def native_view(cache, account_id):
    """Read on the native owner's thread; export only public account/order fields."""
    account = cache.account(account_id)
    if not account:
        return dict(account=[], positions=[], orders=[], status="等待 NT 账户回报")
    event = account.last_event
    tags = (event.info or {}) if event else {}
    account_rows = [
        {"field": key, "value": tags.get(key, "未回报")}
        for key in [
            "NetLiquidation",
            "AvailableFunds",
            "ExcessLiquidity",
            "InitMarginReq",
            "MaintMarginReq",
            "TotalCashValue",
        ]
        if str(account_id).startswith("IB-")
    ]
    for currency, value in account.balances_total().items():
        account_rows.append(dict(field=f"原生余额 {currency}", value=float(value)))
    position_fields = ["position_id", "instrument_id", "side", "quantity", "avg_px_open", "realized_pnl", "ts_opened"]
    order_fields = [
        "client_order_id",
        "instrument_id",
        "side",
        "type",
        "quantity",
        "price",
        "status",
        "filled_qty",
        "avg_px",
        "ts_init",
    ]
    positions = [
        {key: row.get(key) for key in position_fields}
        for row in (p.to_dict() for p in cache.positions(account_id=account_id))
    ]
    orders = [
        {key: row.get(key) for key in order_fields}
        for row in (o.to_dict() for o in cache.orders(account_id=account_id))
    ]
    metadata = {}
    for row in [*positions, *orders]:
        from nautilus_trader.model import InstrumentId

        instrument = cache.instrument(InstrumentId.from_str(row["instrument_id"]))
        info = (instrument.info or {}) if instrument else {}
        if isinstance(instrument, OptionContract):
            row.update(
                underlying=instrument.underlying,
                expiration=datetime.fromtimestamp(instrument.expiration_ns / 1e9, UTC)
                .astimezone(eastern())
                .date()
                .isoformat(),
                right=str(instrument.option_kind).lower(),
                strike=float(instrument.strike_price),
                multiplier=float(instrument.multiplier),
                contract_type="期权腿",
            )
        if info.get("synthetic_combo_accounting_proxy"):
            legs = info["legs"]
            row.update(
                underlying=legs[0]["symbol"],
                expiration=legs[0]["expiration"],
                right=legs[0]["right"],
                contract_type="回测组合",
            )
            metadata[row["instrument_id"]] = dict(
                accounting_price_offset=info["accounting_price_offset"],
                label=f"{legs[0]['symbol']} {legs[0]['expiration']} {legs[0]['right'].upper()} "
                + "/".join(f"{leg['strike']:g}" for leg in legs),
            )
    positions = display_rows(positions, metadata, ["avg_px_open"])
    orders = display_rows(orders, metadata, ["price", "avg_px"])
    return dict(
        account=account_rows,
        positions=positions,
        orders=orders[-200:],
        account_id=str(account_id),
        account_timestamp=datetime.fromtimestamp(event.ts_event / 1e9, UTC).isoformat() if event else None,
        base_currency=str(event.base_currency) if event else None,
        is_reported=bool(event.is_reported) if event else False,
        status="NT 原生账户快照",
        captured_at=datetime.now(UTC).isoformat(),
        combo_contracts=metadata,
    )


class AccountSnapshotActor(DataActor):
    def __new__(cls, monitor):
        return super().__new__(cls, DataActorConfig(actor_id=ActorId("TERMINAL-ACCOUNT")))

    def __init__(self, monitor):
        self.monitor = monitor

    def on_start(self):
        self.clock.set_timer("terminal-account", timedelta(seconds=1), callback=self.on_time_event)

    def on_time_event(self, event):
        view = native_view(self.cache, AccountId("IB-" + self.monitor.account_id))
        self.monitor.publish(view)

    def on_stop(self):
        self.clock.cancel_timer("terminal-account")


class BrokerMonitor:
    """An NT node with no trading strategy; publishes account snapshots only."""

    def __init__(self, account_id):
        if not account_id or not account_id.isalnum():
            raise ValueError("请输入有效 IBKR 账户编号")
        self.account_id = account_id
        self.lock = RLock()
        self.view = dict(status="正在连接 IBKR", account=[], positions=[], orders=[])
        self.handle = None
        self.ready = Event()
        self.stop_requested = Event()
        self.thread = Thread(target=self._run, name="terminal-ibkr-monitor", daemon=True)

    def publish(self, view):
        with self.lock:
            self.view = view

    def snapshot(self):
        with self.lock:
            return deepcopy(self.view)

    def _run(self):
        from nautilus_trader.adapters import interactive_brokers as ib
        from nautilus_trader.live import LiveNode

        node = None
        try:
            client_id = int(os.getenv("IBKR_CLIENT_ID", "71")) + 50
            provider = ib.InteractiveBrokersInstrumentProviderConfig(
                build_options_chain=False, build_futures_chain=False
            )
            builder = LiveNode.builder("QWORK-TERMINAL-MONITOR", TraderId("QWORK-TERMINAL-MONITOR"), Environment.LIVE)
            builder = (
                builder.with_reconciliation(True).with_reconciliation_lookback_mins(1440).with_timeout_connection(30)
            )
            builder = (
                builder.with_logging(LoggerConfig(stdout_level=LogLevel.ERROR))
                .with_delay_post_stop_secs(0)
                .with_delay_shutdown_secs(0)
            )
            builder = builder.add_exec_client(
                None,
                ib.InteractiveBrokersExecutionClientFactory(),
                ib.InteractiveBrokersExecutionClientConfig(
                    host=os.getenv("IBKR_HOST", "127.0.0.1"),
                    port=int(os.getenv("IBKR_PORT", "4002")),
                    client_id=client_id,
                    account_id="IB-" + self.account_id,
                    fetch_all_open_orders=True,
                    instrument_provider=provider,
                ),
            )
            node = builder.build()
            node.add_actor(AccountSnapshotActor(self))
            self.handle = node.handle()
            self.ready.set()
            if not self.stop_requested.is_set():

                async def run_node():
                    await node.run_async()

                asyncio.run(run_node())
        except Exception as exc:
            self.publish(dict(status=f"IBKR 连接失败（{type(exc).__name__}）", account=[], positions=[], orders=[]))
        finally:
            self.ready.set()
            if node:
                node.dispose()

    def start(self):
        self.thread.start()

    def stop(self):
        self.stop_requested.set()
        self.ready.wait(timeout=5)
        if self.handle:
            self.handle.stop()
        if self.thread.is_alive():
            self.thread.join(timeout=5)


class TerminalBackend:
    def __init__(self, settings):
        self.settings = settings
        self.lock = RLock()
        self.lifecycle_lock = RLock()
        self.closed = False
        self.runtime = None
        self.monitor = None
        self.controller = None
        self.backtest_thread = None
        self.strategy_process = None
        self.strategy_context = None
        self.strategy_reviews = {}
        self.portfolio_action = "选择候选组合查看风险；此操作不会下单"
        self.chart_thread = None
        self.chart_worker = LatestWorker("terminal-history")
        self.chart_generation = 0
        self.replay_candles = ReplayCandles()
        self.chart = dict(status="请选择期权并加载 K 线", rows=[], source="")
        self.replay = dict(
            status="idle", message="尚未运行回测", orders=[], positions=[], fills=[], account=[], curve=[]
        )
        self.cached_frame = None
        self.cached_scan = None
        self.reports_dir = settings.data_dir / "backtests"
        for row in self.list_runs():
            path = self.reports_dir / row["name"] / "selection.json"
            if row["audit_passed"] and path.exists():
                try:
                    selected = json.loads(path.read_text(encoding="utf-8"))
                    if selected["frame"].get("synthetic") or selected["frame"].get("mode") != "historical":
                        continue
                    self.cached_frame, self.cached_scan = selected["frame"], selected["scan"]
                    break
                except (ValueError, KeyError, OSError):
                    continue
        self.default_day = (self.cached_frame or {}).get("observed_at", datetime.now(eastern()).isoformat())[:10]
        self.state = DashboardState(
            VolatilityRequest(mode="historical", as_of=self.default_day, min_dte=1, max_dte=14, max_expirations=3)
        )
        self.process_registry = ProcessRegistry(settings.data_dir)
        active, self.recovery_errors = self.process_registry.discover()
        self.recovery_count = len(active)
        self.persistence_error = None
        if len(active) == 1 and not self.recovery_errors:
            self.strategy_process, context = active[0]
            self.strategy_context = dict(context, attached=True)

    def list_runs(self):
        rows = []
        for path in sorted(self.reports_dir.glob("*/report.json"), key=lambda p: p.stat().st_mtime, reverse=True):
            try:
                report = json.loads(path.read_text(encoding="utf-8"))
                rows.append(
                    dict(
                        name=path.parent.name,
                        date=report["date"],
                        status=report["status"],
                        net_pnl=report.get("net_pnl"),
                        audit_passed=report.get("audit_passed", False),
                    )
                )
            except (ValueError, KeyError, OSError):
                continue
            if len(rows) >= 100:
                break
        return rows[:100]

    def analysis(self, value=None):
        if self.runtime:
            value = self.runtime.state.snapshot() if value is None else value
            if value.get("surface"):
                scan = value.get("scan")
                if not scan and isinstance(self.runtime.collector, SnapshotCollector):
                    scan = self.cached_scan if matches_surface(self.cached_scan, value["surface"]) else None
                return value["surface"], scan, value["message"]
            if isinstance(self.runtime.collector, SnapshotCollector):
                return self.runtime.collector.frame, self.cached_scan, "原生 Actor 正在计算所选历史快照"
            return None, None, value["message"]
        return (
            self.cached_frame,
            self.cached_scan,
            "已载入真实历史快照 · 非实时" if self.cached_frame else "请选择行情模式并刷新",
        )

    def refresh_analysis(self, symbol, mode, day, at):
        with self.lifecycle_lock:
            return self._refresh_analysis(symbol, mode, day, at)

    def _refresh_analysis(self, symbol, mode, day, at):
        if self.closed:
            raise ValueError("后台服务正在关闭")
        request = VolatilityRequest(
            symbol=symbol,
            mode=mode,
            as_of=day if mode == "historical" else None,
            at=at,
            min_dte=1,
            max_dte=14,
            max_expirations=3,
            max_quote_age_seconds=1200 if mode == "historical" else 5,
        )
        if self.runtime and isinstance(self.runtime.collector, SnapshotCollector):
            self.runtime.stop()
            self.runtime = None
        if not self.runtime:
            self.runtime = VolatilityRuntime(self.settings, request)
            self.runtime.start()
        else:
            self.runtime.state.configure(request)
        return "已请求行情；实时模式不使用历史快照替代"

    def analyze_candidate(self, identity, units):
        with self.lifecycle_lock:
            return self._analyze_candidate(identity, units)

    def _analyze_candidate(self, identity, units):
        if self.closed:
            raise ValueError("后台服务正在关闭")
        from qwork.actors.portfolio_pnl.models import PortfolioRequest

        frame, scan, _ = self.analysis()
        if not matches_surface(scan, frame):
            raise ValueError("候选所属行情已变化，等待重新扫描")
        self.require_analysis_frame(frame)
        candidate = next(
            (row for row in (scan or {}).get("candidates", []) if candidate_identity(row, frame or {}) == identity),
            None,
        )
        if not candidate:
            raise ValueError("候选所属行情已变化，请重新选择组合")
        if type(units) not in {int, float} or not float(units).is_integer() or not 1 <= units <= 100:
            raise ValueError("分析数量须为 1–100 整数组")
        legs = [
            dict(
                symbol=leg["symbol"],
                expiration=leg["expiration"],
                strike=leg["strike"],
                right=leg["right"],
                side=leg["side"],
                quantity=(2 if i == 1 else 1) * int(units),
                entry_premium=leg["price"],
            )
            for i, leg in enumerate(candidate["legs"])
        ]
        request = PortfolioRequest(
            combinations=[
                dict(
                    name=f"{frame['symbol']} {candidate['expiration']} {candidate['right'].upper()} "
                    + "/".join(f"{strike:g}" for strike in candidate["strikes"]),
                    legs=legs,
                )
            ]
        )
        return self._apply_portfolio(frame, request)

    def analyze_combinations(self, request, workspace):
        with self.lifecycle_lock:
            if self.closed:
                raise ValueError("后台服务正在关闭")
            frame, _, _ = self.analysis()
            if workspace not in {"research", "paper", "live"} or not frame:
                raise ValueError("请在研究或交易工作区加载有效期权行情后分析")
            if workspace in {"paper", "live"} and frame.get("mode") != "live":
                raise ValueError("交易工作区只能使用有效实时曲面")
            self.require_analysis_frame(frame)
            if any(leg.symbol != frame["symbol"] for combo in request.combinations for leg in combo.legs):
                raise ValueError("组合标的与当前波动率曲面不一致")
            return self._apply_portfolio(frame, request)

    def require_analysis_frame(self, frame):
        context = self.runtime.state.snapshot() if self.runtime else {}
        health = market_health(frame, context)
        if not health["valid"]:
            raise ValueError(health["label"])

    def _apply_portfolio(self, frame, request):
        if not self.runtime:
            config = VolatilityRequest(
                symbol=frame["symbol"],
                mode="historical",
                as_of=frame["observed_at"][:10],
                min_dte=1,
                max_dte=14,
                max_expirations=3,
            )
            self.runtime = VolatilityRuntime(self.settings, config, collector=SnapshotCollector(frame))
            self.runtime.state.configure_portfolio(request)
            self.runtime.start()
        else:
            self.runtime.state.configure_portfolio(request)
        self.portfolio_action = "组合已送入原生盈亏 Actor；图中价格为分析假设，不创建交易委托"
        return self.portfolio_action

    def connect_broker(self, account_id):
        with self.lifecycle_lock:
            return self._connect_broker(account_id)

    def _connect_broker(self, account_id):
        if self.closed:
            raise ValueError("后台服务正在关闭")
        if self.monitor and self.monitor.thread.is_alive():
            if self.monitor.account_id != account_id:
                raise ValueError("请先断开当前账户")
            return "账户连接已在运行"
        self.monitor = BrokerMonitor(account_id)
        self.monitor.start()
        return "正在连接 NT IBKR 账户；此连接不提交交易委托"

    def disconnect_broker(self):
        with self.lifecycle_lock:
            if self.monitor:
                self.monitor.stop()
                self.monitor = None
        return "账户监看已断开；策略进程不受影响"

    def load_chart(self, symbol, expiration, strike, right, day, minutes=5):
        expiration = date.fromisoformat(expiration).isoformat()
        day = date.fromisoformat(day).isoformat()
        if int(minutes) not in {1, 5, 15, 30, 60}:
            raise ValueError("K 线周期必须为 1、5、15、30 或 60 分钟")
        contract = Contract(symbol, expiration, float(strike), right)
        with self.lock:
            if self.closed:
                raise ValueError("后台服务正在关闭")
            self.chart_generation += 1
            generation = self.chart_generation
            self.chart = dict(
                status="正在加载 K 线",
                rows=[],
                source="",
                contract=contract.to_dict()
                if hasattr(contract, "to_dict")
                else dict(symbol=symbol, expiration=expiration, strike=strike, right=right),
            )

        def work():
            try:
                import pandas as pd

                from qwork.providers.theta.provider import ThetaProvider

                provider = ThetaProvider(self.settings.theta_key, self.settings.data_dir)
                path = (
                    self.settings.data_dir
                    / "history"
                    / f"{symbol}-{day}-{expiration}-{float(strike):g}-{right}-1m.json"
                )
                quotes = (
                    normalize_quotes(json.loads(path.read_text(encoding="utf-8")))[0]
                    if path.exists()
                    else provider.history(contract, day, day, "1m")[0]
                )
                if not quotes:
                    raise ValueError("该合约没有历史报价")
                series = pd.Series(
                    [(q.bid + q.ask) / 2 for q in quotes], index=pd.to_datetime([q.ts_ns for q in quotes], utc=True)
                )
                rows = candle_rows(series, minutes)
                value = dict(
                    status=f"{symbol} {expiration} {float(strike):g} {right.upper()} · {minutes} 分钟",
                    rows=rows,
                    source="Theta NBBO 中点 K 线 · 非成交 OHLC · 非实时",
                    day=day,
                    minutes=int(minutes),
                    contract=dict(symbol=symbol, expiration=expiration, strike=float(strike), right=right),
                    observations=[
                        dict(timestamp=datetime.fromtimestamp(q.ts_ns / 1e9, UTC).isoformat(), mid=(q.bid + q.ask) / 2)
                        for q in quotes
                    ],
                )
            except Exception as exc:
                value = dict(
                    status=str(exc) if isinstance(exc, ValueError) else f"K 线加载失败（{type(exc).__name__}）",
                    rows=[],
                    source="",
                )
            with self.lock:
                if generation == self.chart_generation and not self.closed:
                    self.chart = value

        self.chart_worker.submit(work)
        self.chart_thread = self.chart_worker.thread
        return "K 线加载中"

    def start_backtest(self, symbol, day, margin, positions, horizon, execution, universe, speed):
        with self.lock:
            return self._start_backtest(symbol, day, margin, positions, horizon, execution, universe, speed)

    def _start_backtest(self, symbol, day, margin, positions, horizon, execution, universe, speed):
        if self.closed:
            raise ValueError("后台服务正在关闭")
        day = date.fromisoformat(day).isoformat()
        if self.backtest_thread and self.backtest_thread.is_alive():
            raise ValueError("已有回测运行，可暂停、单步或终止")
        if self.strategy_process and self.strategy_process.poll() is None:
            raise ValueError("策略进程运行期间不启动另一原生回测")
        config = BrokenWingConfig(
            symbol=symbol, margin_budget=margin, max_positions=positions, probability_horizon=horizon
        )
        profile = ReplayExecutionConfig(mode=execution)
        if not 1 <= int(universe) <= 40:
            raise ValueError("候选池必须在 1–40 之间")
        output = self.reports_dir / f"{config.symbol}-{day}-terminal-{datetime.now():%Y%m%d-%H%M%S-%f}"
        self.controller = ReplayController(speed=speed, observer=self._observe_replay)
        with self.lock:
            self.replay = dict(
                status="preparing",
                message="准备真实历史数据",
                output=str(output),
                orders=[],
                positions=[],
                fills=[],
                account=[],
                curve=[],
                clock=None,
                day=day,
                config=dict(
                    symbol=config.symbol,
                    day=day,
                    margin=margin,
                    positions=positions,
                    horizon=horizon,
                    execution=execution,
                    universe=int(universe),
                ),
                stop_requested=False,
            )

        def progress(message):
            with self.lock:
                self.replay["message"] = message

        def work():
            try:
                report = run_backtest(
                    self.settings,
                    config,
                    day,
                    output,
                    universe_size=int(universe),
                    execution=profile,
                    controller=self.controller,
                    progress=progress,
                )
                with self.lock:
                    self.replay.update(
                        status=report["status"],
                        report=report,
                        message=f"{report['status']} · 净盈亏 {report['net_pnl']:.2f} USD",
                    )
                    curve = deepcopy(self.replay["curve"])
                (output / "terminal-curve.json").write_text(json.dumps(curve, allow_nan=False), encoding="utf-8")
            except Exception as exc:
                try:
                    output.mkdir(parents=True, exist_ok=True)
                    (output / "failure.json").write_text(
                        json.dumps(
                            dict(
                                error_type=type(exc).__name__,
                                frames=[
                                    dict(file=f.filename, line=f.lineno, function=f.name)
                                    for f in traceback.extract_tb(exc.__traceback__)
                                ],
                            )
                        ),
                        encoding="utf-8",
                    )
                except OSError:
                    pass
                with self.lock:
                    self.replay.update(
                        status="error",
                        message=f"回测失败（{type(exc).__name__}），诊断位置已保存；请检查数据覆盖、套餐及参数",
                    )

        self.backtest_thread = Thread(target=work, name="terminal-native-backtest", daemon=True)
        self.backtest_thread.start()
        return "原生回测已启动"

    def _observe_replay(self, engine, stamp):
        usd = Currency.from_str("USD")
        account = engine.cache.account_for_venue(Venue("SIM"))
        balance = float(account.balance_total(usd))
        unrealized = 0
        for position in engine.cache.positions_open():
            quote = engine.cache.quote(position.instrument_id)
            if quote:
                unrealized += float(position.unrealized_pnl(quote.bid_price))
        view = native_view(engine.cache, account.id)
        view["fills"] = records(engine.generate_fills_report())
        points = []
        for instrument in engine.cache.instruments():
            if not isinstance(instrument, OptionContract):
                continue
            quote = engine.cache.quote(instrument.id)
            if not quote:
                continue
            points.append(
                dict(
                    expiration=datetime.fromtimestamp(instrument.expiration_ns / 1e9, UTC)
                    .astimezone(eastern())
                    .date()
                    .isoformat(),
                    strike=float(instrument.strike_price),
                    right=str(instrument.option_kind).lower(),
                    bid=float(quote.bid_price),
                    ask=float(quote.ask_price),
                    bid_size=int(quote.bid_size),
                    ask_size=int(quote.ask_size),
                    timestamp=datetime.fromtimestamp(quote.ts_event / 1e9, UTC).isoformat(),
                    iv=None,
                )
            )
        view["frame"] = dict(
            symbol=self.replay["config"]["symbol"],
            points=points,
            expirations=sorted({point["expiration"] for point in points}),
            observed_at=datetime.fromtimestamp(stamp / 1e9, UTC).isoformat(),
        )
        with self.lock:
            curve = self.replay["curve"]
            curve.append(
                dict(
                    timestamp=datetime.fromtimestamp(stamp / 1e9, UTC).isoformat(),
                    equity=balance + unrealized,
                    cash=balance,
                )
            )
            self.replay.update(
                view,
                status="stopping" if self.replay.get("stop_requested") else "running",
                clock=curve[-1]["timestamp"],
                curve=curve,
                message="原生历史时间回放 · 模拟账户与合成组合成交",
            )

    def control_replay(self, action, speed):
        if not self.controller or not self.backtest_thread or not self.backtest_thread.is_alive():
            raise ValueError("请先运行回测")
        if action == "step" and not self.controller.paused:
            raise ValueError("请先暂停，再单步推进")
        if action == "stop":
            with self.lock:
                self.replay.update(stop_requested=True, status="stopping")
        self.controller.configure(
            speed=speed,
            paused=action == "pause" if action in {"pause", "play"} else None,
            step=action == "step",
            stop=action == "stop",
        )
        return {
            "pause": "已暂停",
            "play": "继续回放",
            "step": "推进下一报价时刻",
            "stop": "已请求终止；未平仓会明确标错",
            "speed": "播放速度已更新",
        }[action]

    def load_report(self, name):
        if not name or Path(name).name != name or name in {".", ".."}:
            raise ValueError("报告名称无效")
        path = self.reports_dir / name
        report = json.loads((path / "report.json").read_text(encoding="utf-8"))

        def load_rows(key):
            file = path / f"{key}.json"
            if not file.exists() and report["status"] == "no_candidates":
                return []
            return json.loads(file.read_text(encoding="utf-8"))

        with self.lock:
            if self.backtest_thread and self.backtest_thread.is_alive():
                raise ValueError("请先结束当前回测")
            self.replay = dict(
                status="archived",
                message=f"已完成报告 · {report['status']} · 审计 {report.get('audit_passed', False)}",
                report=report,
                output=str(path),
                clock=None,
                day=report.get("date"),
                config=report.get("config", {}),
                curve=[],
                **{key: load_rows(key) for key in ["orders", "positions", "fills", "account"]},
            )
            metadata = report.get("combo_contracts", {})
            self.replay["positions"] = display_rows(self.replay["positions"], metadata, ["avg_px_open", "avg_px_close"])
            self.replay["orders"] = display_rows(self.replay["orders"], metadata, ["price", "avg_px"])
            curve_path = path / "terminal-curve.json"
            if curve_path.exists():
                self.replay["curve"] = json.loads(curve_path.read_text(encoding="utf-8"))
            else:
                self.replay["curve"] = [
                    dict(timestamp=row["index"], equity=float(row["total"]))
                    for row in self.replay["account"]
                    if row.get("currency") == "USD"
                ]
                self.replay["curve_kind"] = "cash"
            account_rows = self.replay["account"]
            self.replay["account"] = [
                dict(field=f"{row['currency']} {key}", value=float(row[key]))
                for row in account_rows[-1:]
                for key in ["total", "locked", "free"]
            ]
        return "已载入完整历史报告，非正在运行的回放"

    def review_strategy(self, account_id, mode, margin, positions, execution, symbol, loss, drawdown):
        payload = strategy_parameters(account_id, mode, margin, positions, execution, symbol, loss, drawdown)
        with self.lock:
            self._strategy_ready(payload)
            token = uuid.uuid4().hex
            expires = datetime.now(UTC) + timedelta(seconds=90)
            self.strategy_reviews[token] = dict(parameters=payload, expires_at=expires.isoformat())
            while len(self.strategy_reviews) > 64:
                self.strategy_reviews.pop(next(iter(self.strategy_reviews)))
            return dict(review_id=token, parameters=payload, expires_at=expires.isoformat())

    def _strategy_ready(self, payload):
        if self.closed:
            raise ValueError("后台服务正在关闭")
        active, problems = self.process_registry.discover()
        if self.recovery_errors or problems:
            raise ValueError("后台恢复记录需人工核验，不启动新的策略")
        if active and (not self.strategy_process or self.strategy_process.poll() is not None):
            raise ValueError("已发现后台策略，请先恢复该账户的监看")
        stop = self.settings.data_dir / "private" / "trading" / payload["mode"] / payload["account"] / "STOP"
        if stop.exists():
            raise ValueError("账户存在持久停止信号，请先核验券商持仓和恢复状态")
        if self.strategy_process and self.strategy_process.poll() is None:
            raise ValueError("策略进程已运行")
        if self.backtest_thread and self.backtest_thread.is_alive():
            raise ValueError("请先结束原生回测")
        if not self.monitor or self.monitor.account_id != payload["account"] or not self.monitor.thread.is_alive():
            raise ValueError("请先连接并核验目标账户回报")
        reason = account_readiness(self.monitor.snapshot(), payload["account"])
        if reason:
            raise ValueError(reason)

    def start_strategy(
        self, account_id, mode, margin, positions, execution, symbol, loss, drawdown, confirmed=False, review_id=None
    ):
        with self.lock:
            return self._start_strategy(
                account_id, mode, margin, positions, execution, symbol, loss, drawdown, confirmed, review_id
            )

    def _start_strategy(
        self, account_id, mode, margin, positions, execution, symbol, loss, drawdown, confirmed, review_id
    ):
        if (
            mode not in {"paper", "live"}
            or not account_id.isalnum()
            or (mode == "paper") != account_id.startswith("DU")
        ):
            raise ValueError("交易环境与账户编号不匹配")
        if mode == "live" and not confirmed:
            raise ValueError("请勾选实盘委托确认后启动")
        payload = strategy_parameters(account_id, mode, margin, positions, execution, symbol, loss, drawdown)
        review = self.strategy_reviews.get(review_id)
        if (
            not review
            or review["parameters"] != payload
            or datetime.fromisoformat(review["expires_at"]) <= datetime.now(UTC)
        ):
            raise ValueError("参数审阅已失效，请重新审阅当前账户和参数")
        self._strategy_ready(payload)
        self.strategy_reviews.pop(review_id)
        config = BrokenWingConfig(symbol=symbol, margin_budget=margin, max_positions=positions)
        from qwork.strategies.broken_wing.risk import AccountRiskConfig

        AccountRiskConfig(daily_loss_limit_usd=loss, max_drawdown_usd=drawdown)
        output = self.settings.data_dir / "trading" / f"{config.symbol}-terminal-{datetime.now():%Y%m%d-%H%M%S-%f}"
        output.mkdir(parents=True, exist_ok=True)
        log = open(output / "process.log", "a", encoding="utf-8")
        args = [
            sys.executable,
            "-m",
            "qwork",
            "trade",
            "--mode",
            mode,
            "--account-id",
            account_id,
            "--symbol",
            config.symbol,
            "--margin",
            str(margin),
            "--max-positions",
            str(positions),
            "--execution",
            execution,
            "--daily-loss-limit",
            str(loss),
            "--drawdown-limit",
            str(drawdown),
            "--output",
            str(output),
        ]
        try:
            self.strategy_process = subprocess.Popen(
                args,
                cwd=ROOT,
                stdout=log,
                stderr=log,
                creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
            )
        finally:
            log.close()
        self.strategy_context = dict(
            account=account_id,
            mode=mode,
            symbol=config.symbol,
            output=str(output),
            config=payload,
            started_at=datetime.now(UTC).isoformat(),
            stop_requested=False,
        )
        try:
            self.process_registry.capture(self.strategy_process, args, self.strategy_context)
        except (psutil.Error, ValueError, OSError) as exc:
            self.persistence_error = f"后台监看恢复记录未写入（{type(exc).__name__}）；本服务仍管理当前进程"
        return "策略进程启动中；恢复和风控通过后才允许开仓"

    def attach_strategy(self, account, mode):
        with self.lock:
            if self.closed:
                raise ValueError("后台服务正在关闭")
            active, problems = self.process_registry.discover()
            if problems:
                raise ValueError("进程身份无法完整核验，不接管未知进程")
            selected = [
                (process, context)
                for process, context in active
                if context["account"] == account and context["mode"] == mode
            ]
            if len(selected) != 1:
                raise ValueError("该账户没有唯一、可核验的后台策略")
            if (
                self.strategy_process
                and self.strategy_process.poll() is None
                and self.strategy_context["account"] != account
            ):
                raise ValueError("请先处理当前监看的策略")
            self.strategy_process, context = selected[0]
            self.strategy_context = dict(context, attached=True)
            self.recovery_count = len(active)
            self.recovery_errors = []
            return "已恢复后台进程监看；未连接券商，也未提交新委托"

    def stop_strategy(self):
        if not self.strategy_context or not self.strategy_process or self.strategy_process.poll() is not None:
            raise ValueError("没有本交易台启动的活动策略")
        context = self.strategy_context
        if isinstance(self.strategy_process, OwnedProcess) and self.strategy_process.uncertain:
            raise ValueError("进程身份暂不可核验，请直接核验券商状态")
        directory = self.settings.data_dir / "private" / "trading" / context["mode"] / context["account"]
        directory.mkdir(parents=True, exist_ok=True)
        (directory / "STOP").write_text("Operator requested halt and managed-position liquidation", encoding="utf-8")
        self.strategy_context["stop_requested"] = True
        return "已请求停止开仓并平仓；继续检查券商持仓，进程仍负责管理退出"

    def strategy_snapshot(self):
        if self.strategy_context and self.strategy_process.poll() is not None:
            active, problems = self.process_registry.discover()
            selected = [
                (process, context)
                for process, context in active
                if context["output"] == self.strategy_context["output"]
            ]
            if len(selected) == 1 and not problems:
                self.strategy_process, context = selected[0]
                self.strategy_context = dict(context, attached=True)
        if not self.strategy_context:
            recovery = bool(self.recovery_errors or self.recovery_count)
            return dict(
                status="已有后台进程待核验 / 恢复监看" if recovery else "未启动策略",
                phase="unverified" if recovery else "idle",
                running=False,
                state_known=False,
                rows=[],
                events=[],
                recovery_required=recovery,
                recovery_errors=self.recovery_errors,
            )
        context = self.strategy_context
        path = (
            self.settings.data_dir
            / "private"
            / "trading"
            / context["mode"]
            / context["account"]
            / f"{context['symbol']}.sqlite3"
        )
        rows, events, risk, state = [], [], {}, None
        state_error = "等待策略写入状态"
        if path.exists():
            try:
                state = CheckpointStore(path, context["account"], context["symbol"]).load(timeout=0.05)
                if state is None:
                    raise ValueError("State has not been committed")
                rows = [
                    dict(
                        key=key,
                        units=p["units"],
                        state=p["state"],
                        entry_price=p.get("entry_price"),
                        close_reason=p.get("close_reason"),
                        margin_reserved=p.get("margin_reserved"),
                    )
                    for key, p in state.positions.items()
                ]
                risk = state.risk
                state_error = None
                if (
                    context.get("started_at")
                    and path.stat().st_mtime < datetime.fromisoformat(context["started_at"]).timestamp()
                ):
                    state_error = "当前进程尚未更新旧状态，等待恢复核验"
            except (ValueError, sqlite3.Error, OSError) as exc:
                state = None
                state_error = f"状态不可读取（{type(exc).__name__}）；不能据此判断无持仓"
        events = journal_tail(Path(context["output"]) / "events.jsonl")
        running = self.strategy_process.poll() is None
        if isinstance(self.strategy_process, OwnedProcess) and self.strategy_process.uncertain:
            state_error = "后台进程身份暂不可核验，持仓状态未知"
        phase = "recovering"
        lifecycle = next(
            (
                event["kind"]
                for event in reversed(events)
                if event["kind"] in {"recovery_blocked", "execution_halted", "recovery_completed"}
            ),
            None,
        )
        if lifecycle in {"recovery_blocked", "execution_halted"}:
            phase = "halted"
        elif state and not state_error:
            phase = "halted" if state.halted else "active"
        if context.get("stop_requested"):
            phase = "stopping"
        if state_error and running:
            phase = "unknown"
        if not running:
            phase = "exited"
        labels = dict(
            recovering="启动 / 恢复核验中",
            active="策略运行 · 以券商回报为准",
            halted="开仓已阻断 · 检查原因",
            stopping="停止请求已发出 · 正在核验退出",
            unknown="策略状态未知",
            exited=f"进程退出（{self.strategy_process.returncode}）",
        )
        return dict(
            status=labels[phase],
            phase=phase,
            running=running,
            state_known=state_error is None,
            state_error=state_error,
            rows=rows,
            events=events,
            risk=risk,
            mode=context["mode"],
            config=context.get("config", {}),
            account=context["account"],
            symbol=context["symbol"],
            stop_requested=context.get("stop_requested", False),
            pending_orders=len(state.pending) if state else None,
            remaining_units=sum(p["units"] for p in state.positions.values()) if state else None,
            last_reason=next(
                (
                    event.get("reason") or event.get("failures")
                    for event in reversed(events)
                    if event.get("reason") or event.get("failures")
                ),
                None,
            ),
            attached=bool(context.get("attached")),
            persistence_error=self.persistence_error,
        )

    def snapshot(self):
        analysis = self.runtime.state.snapshot() if self.runtime else {}
        frame, scan, message = self.analysis(analysis)
        with self.lock:
            replay = deepcopy(self.replay)
            # Chart jobs publish a fresh dictionary; observations are never edited.
            # Avoid copying an entire quote day only to discard it in the UI.
            observations = self.chart.get("observations", [])
            chart = deepcopy({key: value for key, value in self.chart.items() if key != "observations"})
        if (
            replay["status"] in {"preparing", "running", "stopping"}
            and observations
            and chart.get("day") == replay.get("day")
        ):
            clock = replay.get("clock")
            chart["rows"] = self.replay_candles.snapshot(observations, chart["minutes"], clock)
        if self.controller:
            replay.update(paused=self.controller.paused, speed=self.controller.speed)
        if replay.get("output"):
            replay["events"] = journal_tail(Path(replay["output"]) / "events.jsonl")
        broker = (
            self.monitor.snapshot()
            if self.monitor
            else dict(status="IBKR 尚未连接", account=[], positions=[], orders=[])
        )
        broker["connected"] = bool(self.monitor and self.monitor.thread.is_alive())
        if self.monitor:
            mode = "paper" if self.monitor.account_id.startswith("DU") else "live"
            broker["operator_stop"] = (
                self.settings.data_dir / "private" / "trading" / mode / self.monitor.account_id / "STOP"
            ).exists()
        chart_job = self.chart_worker.status()
        return dict(
            frame=frame,
            scan=scan,
            market_message=message,
            market_context={key: analysis.get(key) for key in ["request", "status", "paused"]},
            market_health=market_health(frame, analysis),
            chart=chart,
            replay=replay,
            broker=broker,
            strategy=self.strategy_snapshot(),
            jobs=dict(
                backtest=bool(self.backtest_thread and self.backtest_thread.is_alive()),
                chart=chart_job["running"] or chart_job["pending"],
                chart_queue=chart_job["pending"],
            ),
            portfolio=analysis.get("portfolio"),
            portfolio_error=analysis.get("portfolio_error"),
            portfolio_request=analysis.get("portfolio_request"),
            term=analysis.get("term"),
            portfolio_action=self.portfolio_action,
        )

    def close(self):
        with self.lifecycle_lock:
            with self.lock:
                self.closed = True
                self.chart_generation += 1
            self.chart_worker.close()
            if self.controller:
                self.controller.configure(stop=True)
            if self.backtest_thread and self.backtest_thread.is_alive():
                self.backtest_thread.join(timeout=5)
            if self.runtime:
                self.runtime.stop()
            if self.monitor:
                self.monitor.stop()
        # Do not terminate a trading subprocess with potentially open positions.
