"""Real Theta quote replay into Nautilus, with disclosed synthetic BAG execution."""

import json
import math
from collections import defaultdict
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import exchange_calendars as calendars
from nautilus_trader.backtest import BacktestEngine, BacktestEngineConfig
from nautilus_trader.common import LoggerConfig, LogLevel
from nautilus_trader.execution import PerContractFeeModel
from nautilus_trader.model import AccountType, BookType, Currency, Money, OmsType, Price, Quantity, QuoteTick, Venue

from qwork.actors.broken_wing.models import ButterflyScanRequest
from qwork.analytics.butterfly import scan_butterflies
from qwork.core.domain import Contract, eastern, parse_date
from qwork.core.state import VolatilityRequest
from qwork.providers.theta.collector import SurfaceCollector
from qwork.strategies.broken_wing.analytics import quotes_aligned, rank_candidates
from qwork.strategies.broken_wing.instruments import contract_keys, native_tick, sim_option, sim_spread
from qwork.strategies.broken_wing.strategy import BrokenWingStrategy

from .execution import ReplayExecutionConfig, allocated_sizes, execution_summary, finite_book, leg_demands
from .validation import audit_replay


def records(frame):
    if frame is None or frame.empty:
        return []

    def convert(value):
        if isinstance(value, float) and not math.isfinite(value):
            return None
        if value is None or isinstance(value, (str, int, float, bool)):
            return value
        return value.isoformat() if hasattr(value, "isoformat") else str(value)

    return [{str(k): convert(v) for k, v in row.items()} for row in frame.reset_index().to_dict(orient="records")]


def discover(settings, config, day):
    collector = SurfaceCollector(settings)
    request = VolatilityRequest(
        symbol=config.symbol,
        source="theta",
        mode="historical",
        as_of=day,
        at=config.entry_time,
        min_dte=1,
        max_dte=14,
        max_expirations=3,
        rate=config.rate,
        dividend_yield=config.dividend_yield,
    )
    frame = collector.collect(request, 0)
    scan = scan_butterflies(
        frame,
        ButterflyScanRequest(
            min_probability=config.min_probability,
            target_ratio=config.min_profit_loss_ratio,
            commission_per_contract=config.commission_per_contract,
            slippage_per_contract=config.slippage_per_share,
            max_candidates=100,
        ),
    )
    candidates = rank_candidates(frame, scan, config)
    return collector, frame, scan, candidates


def run_backtest(
    settings, config, day, output, interval="1m", universe_size=8, progress=print, execution=None, controller=None
):
    execution = execution or ReplayExecutionConfig()
    day = parse_date(day)
    calendar = calendars.get_calendar("XNYS")
    if not calendar.is_session(str(day)):
        raise ValueError("Requested date is not a US trading session")
    close = calendar.session_close(str(day)).to_pydatetime().astimezone(eastern())
    flatten = min(config.flatten_time, (close - timedelta(minutes=5)).time().replace(tzinfo=None))
    config = config.model_copy(
        update={
            "flatten_time": flatten,
            "last_entry_time": min(config.last_entry_time, (close - timedelta(minutes=30)).time().replace(tzinfo=None)),
            "quote_max_age_seconds": 65 if interval == "1m" else config.quote_max_age_seconds,
            "slippage_per_share": execution.leg_slippage_per_share,
        }
    )
    output = Path(output)
    output.mkdir(parents=True, exist_ok=True)
    progress(f"Discovering {config.symbol} candidates at {day} {config.entry_time} ET")
    # Keep selection assumptions fixed across execution sensitivity scenarios.
    collector, frame, scan, candidates = discover(settings, config.model_copy(update={"slippage_per_share": 0}), day)
    (output / "selection.json").write_text(
        json.dumps(dict(frame=frame, scan=scan), ensure_ascii=False, allow_nan=False), encoding="utf-8"
    )
    candidates = candidates[:universe_size]
    report = dict(
        symbol=config.symbol,
        date=day.isoformat(),
        source="Thetadata historical NBBO",
        synthetic_market_data=False,
        interval=interval,
        session_close=close.isoformat(),
        execution=execution.describe(),
        config=config.model_dump(mode="json"),
        universe_size=len(candidates),
        selection_timestamp=frame["observed_at"],
        trades=[],
        notes=[
            "Nautilus 原生账户、订单和持仓；逐腿 NBBO 合成组合 BBO，采用乘数 100 的线性记账代理进行组合回放，非真实期货或 BAG 成交保证",
            "保证金预算采用每组最大亏损 × 缓冲系数，是研究资金占用上限，不是 IBKR 保证金计算",
            "分钟数据不能验证分钟内触发顺序、瞬时 Gamma 或 tick 退出；单日结果不能证明长期胜率",
            "成交参数未经真实 BAG 成交校准；价差收窄与触价概率是明确的情景假设，不是实际成交证明",
        ],
    )
    if not candidates:
        report.update(status="no_candidates", net_pnl=0, closed_combinations=0, win_rate=None, flat=True)
        save_report(output, report)
        collector.close()
        return report
    # The discovery universe is fixed using information available at entry_time.
    # No candidates are selected from future outcomes.
    legs = {}
    history = {}
    by_timestamp = defaultdict(list)
    for key in contract_keys(candidates):
        expiration, strike, right = key
        progress(f"Fetching {expiration} {strike:g} {right} ({interval})")
        instrument = sim_option(config.symbol, key, day)
        legs[key] = instrument
        cache = settings.data_dir / "history" / f"{config.symbol}-{day}-{expiration}-{strike:g}-{right}-{interval}.json"
        if cache.exists():
            from qwork.core.domain import normalize_quotes

            quotes, _ = normalize_quotes(json.loads(cache.read_text(encoding="utf-8")))
        else:
            quotes, _ = collector.provider.history(
                Contract(config.symbol, expiration, strike, right), str(day), str(day), interval
            )
            cache.parent.mkdir(parents=True, exist_ok=True)
            cache.write_text(json.dumps([quote.to_dict() for quote in quotes], allow_nan=False), encoding="utf-8")
        eligible = [
            q
            for q in quotes
            if config.entry_time
            <= datetime.fromtimestamp(q.ts_ns / 1e9, UTC).astimezone(eastern()).time().replace(tzinfo=None)
            < close.time().replace(tzinfo=None)
        ]
        if not eligible or not any(
            datetime.fromtimestamp(q.ts_ns / 1e9, UTC).astimezone(eastern()).time().replace(tzinfo=None) >= flatten
            for q in eligible
        ):
            raise ValueError(f"Incomplete entry/flatten quote coverage for {key}; cannot certify intraday liquidation")
        history[key] = eligible
        for quote in eligible:
            by_timestamp[quote.ts_ns].append((key, quote))
    collector.close()
    spreads = {candidate["key"]: sim_spread(candidate, day, index) for index, candidate in enumerate(candidates)}
    report["combo_contracts"] = {
        str(spreads[candidate["key"]].id): dict(
            label=f"{config.symbol} {candidate['expiration']} {candidate['right'].upper()} "
            + "/".join(f"{strike:g}" for strike in candidate["strikes"]),
            accounting_price_offset=spreads[candidate["key"]].info["accounting_price_offset"],
        )
        for candidate in candidates
    }
    latest = {}
    ticks = []
    demands = leg_demands(candidates)
    for timestamp, changes in sorted(by_timestamp.items()):
        for key, quote in changes:
            latest[key] = quote
            leg_quote = native_tick(legs[key].id, quote)
            ticks.extend([finite_book(leg_quote), leg_quote])
        for candidate in candidates:
            keys = [(candidate["expiration"], strike, candidate["right"]) for strike in candidate["strikes"]]
            quotes = [latest.get(key) for key in keys]
            if not quotes_aligned(
                [q.to_dict() if q else None for q in quotes],
                timestamp,
                config.quote_max_age_seconds,
                config.quote_max_skew_seconds,
            ):
                continue
            if any(q.ts_ns != timestamp for q in quotes):
                # A shared leg snapshot must not be refreshed in several proxy
                # books by asynchronous updates to unrelated legs.
                continue
            bid, ask, bid_size, ask_size = execution.quote(quotes)
            bid_size, ask_size = allocated_sizes(candidate, quotes, demands, execution.liquidity_fraction)
            if bid > ask:
                continue
            offset = spreads[candidate["key"]].info["accounting_price_offset"]
            combo_quote = QuoteTick(
                instrument_id=spreads[candidate["key"]].id,
                bid_price=Price.from_str(f"{bid + offset:.2f}"),
                ask_price=Price.from_str(f"{ask + offset:.2f}"),
                bid_size=Quantity.from_int(bid_size),
                ask_size=Quantity.from_int(ask_size),
                ts_event=timestamp,
                ts_init=timestamp,
            )
            ticks.extend([finite_book(combo_quote), combo_quote])
    engine = BacktestEngine(
        config=BacktestEngineConfig(logging=LoggerConfig(stdout_level=LogLevel.ERROR), run_analysis=True)
    )
    journal = output / "events.jsonl"
    # Reports in an explicitly reused output directory describe this replay only.
    journal.write_text("", encoding="utf-8")
    strategy = BrokenWingStrategy(
        config, candidates, spreads, {key: item.id for key, item in legs.items()}, journal, execution=execution
    )
    usd = Currency.from_str("USD")
    try:
        engine.add_venue(
            venue=Venue("SIM"),
            oms_type=OmsType.NETTING,
            account_type=AccountType.MARGIN,
            base_currency=usd,
            starting_balances=[Money(config.margin_budget, usd)],
            default_leverage=Decimal(1),
            fee_model=PerContractFeeModel(Money(4 * config.commission_per_contract, usd)),
            book_type=BookType.L2_MBP,
            fill_model=execution.fill_model(),
            latency_model=execution.latency_model(),
            bar_execution=False,
            trade_execution=False,
            liquidity_consumption=True,
        )
        for instrument in [*legs.values(), *spreads.values()]:
            engine.add_instrument(instrument)
        if controller is None:
            engine.add_data(ticks)
        engine.add_strategy(strategy)
        quote_count = sum(isinstance(item, QuoteTick) for item in ticks)
        progress(f"Replaying {quote_count:,} quotes and {len(ticks) - quote_count:,} finite book updates")
        finished = True
        if controller is None:
            engine.run()
        else:
            from .playback import run_controlled

            finished = run_controlled(engine, ticks, controller)
        reports = dict(
            orders=records(engine.generate_orders_report()),
            fills=records(engine.generate_fills_report()),
            positions=records(engine.generate_positions_report()),
            account=records(engine.generate_account_report(venue=Venue("SIM"))),
        )
        for name, rows in reports.items():
            (output / f"{name}.json").write_text(
                json.dumps(rows, ensure_ascii=False, allow_nan=False), encoding="utf-8"
            )
        native_positions = engine.cache.positions()
        profits = [float(position.realized_pnl) for position in native_positions if position.is_closed]
        report.update(
            status="completed" if finished else "canceled",
            quote_events=quote_count,
            native_data_events=len(ticks),
            native_orders=len(reports["orders"]),
            native_fills=len(reports["fills"]),
            closed_combinations=len(profits),
            net_pnl=sum(profits),
            win_rate=sum(p > 0 for p in profits) / len(profits) if profits else None,
            flat=all(position.is_closed for position in native_positions),
            events=strategy.events,
            open_or_pending_combinations=[
                key for key, p in strategy.positions.items() if p["units"] > 0 or p["state"] == "opening"
            ],
            profit_factor=sum(p for p in profits if p > 0) / abs(sum(p for p in profits if p < 0))
            if any(p < 0 for p in profits)
            else None,
            realized_pnl_by_combination=profits,
            order_outcomes={
                status: sum(row["status"] == status for row in reports["orders"])
                for status in {row["status"] for row in reports["orders"]}
            },
            execution_summary=execution_summary(reports["orders"], reports["fills"]),
        )
        if not report["flat"] or report["open_or_pending_combinations"]:
            report["status"] = "incomplete_liquidation"
        audit = audit_replay(report, reports, candidates, spreads, history)
        report["audit_passed"] = audit["passed"]
        (output / "audit.json").write_text(json.dumps(audit, ensure_ascii=False, indent=2), encoding="utf-8")
        if report["status"] == "completed" and not audit["passed"]:
            report["status"] = "failed_validation"
        save_report(output, report)
        return report
    finally:
        engine.dispose()


def save_report(output, report):
    (output / "report.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2, allow_nan=False), encoding="utf-8"
    )
    win = f"{report['win_rate']:.1%}" if report.get("win_rate") is not None else "N/A"
    text = f"# {report['symbol']} 日内回测\n\n日期：{report['date']}，数据粒度：{report['interval']}，状态：{report['status']}。\n\n已平组合：{report.get('closed_combinations', 0)}；净盈亏：{report.get('net_pnl', 0):.2f} USD；样本胜率：{win}；全部日内平仓：{report.get('flat', False)}。\n\n"
    text += (
        "\n".join("- " + note for note in report["notes"])
        + "\n\n配置和原生订单 / 成交 / 持仓报告见同目录 JSON，事件记录见 events.jsonl。\n"
    )
    if "audit_passed" in report:
        text += f"\n逐笔报价与原生账户审计通过：{report['audit_passed']}；明细见 audit.json。\n"
    if "execution_summary" in report:
        summary = report["execution_summary"]
        text += (
            f"\n开仓委托 {summary['entry_orders']} 笔，实际成交 {summary['filled_entry_orders']} 笔；"
            f"累计请求 {summary['requested_units']} 组、成交 {summary['filled_units']} 组，"
            f"无成交撤单 {summary['canceled_without_fill']} 笔。请求数量包含重试，零成交不代表策略盈利。\n"
        )
    (output / "README.md").write_text(text, encoding="utf-8")
