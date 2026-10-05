"""One entrypoint for desktop analysis, historical replay and explicit IBKR trading."""

import argparse
import sys
from datetime import UTC, datetime
from pathlib import Path

from qwork.core.config import Settings


def latest_complete_session():
    import exchange_calendars as calendars

    from qwork.core.domain import eastern

    now = datetime.now(UTC)
    calendar = calendars.get_calendar("XNYS")
    session = calendar.date_to_session(str(now.astimezone(eastern()).date()), direction="previous")
    if now < calendar.session_close(session).to_pydatetime():
        session = calendar.previous_session(session)
    return session.date().isoformat()


def bounded_universe(value):
    number = int(value)
    if not 1 <= number <= 40:
        raise argparse.ArgumentTypeError("universe-size must be between 1 and 40")
    return number


def strategy_command(command, argv):
    from qwork.strategies.broken_wing.models import BrokenWingConfig

    parser = argparse.ArgumentParser(prog=f"qwork {command}", description="Native broken-wing butterfly strategy")
    if command == "trade":
        parser.add_argument("--mode", choices=["paper", "live"], default="paper")
        parser.add_argument("--account-id", required=True)
    parser.add_argument("--symbol", default="SPY")
    parser.add_argument("--margin", type=float, default=10000)
    parser.add_argument("--date", default=latest_complete_session())
    parser.add_argument("--interval", choices=["tick", "1s", "1m"], default="1m")
    parser.add_argument("--max-positions", type=int, default=3)
    parser.add_argument("--universe-size", type=bounded_universe, default=8)
    parser.add_argument("--no-gamma", action="store_true")
    parser.add_argument("--probability-horizon", choices=["intraday", "expiration"], default="intraday")
    parser.add_argument("--output")
    parser.add_argument("--execution", choices=["passive", "natural"], default="passive")
    parser.add_argument("--entry-limit-fraction", type=float, default=0)
    parser.add_argument("--entry-timeout", type=int, default=120)
    parser.add_argument("--retry-cooldown", type=int, default=60)
    parser.add_argument("--max-entry-attempts", type=int, default=3)
    if command == "trade":
        parser.add_argument("--daily-loss-limit", type=float, default=500)
        parser.add_argument("--drawdown-limit", type=float, default=1000)
        parser.add_argument("--account-max-age", type=int, default=300)
        parser.add_argument("--funds-buffer", type=float, default=100)
    else:
        parser.add_argument("--latency-ms", type=int, default=100)
        parser.add_argument("--combo-spread-fraction", type=float, default=1)
        parser.add_argument("--leg-slippage", type=float, default=0)
        parser.add_argument("--liquidity-fraction", type=float, default=1)
        parser.add_argument("--touch-fill-probability", type=float, default=0)
        parser.add_argument("--fill-seed", type=int, default=42)
    args = parser.parse_args(argv)
    settings = Settings.load()
    config = BrokenWingConfig(
        symbol=args.symbol.upper(),
        margin_budget=args.margin,
        max_positions=args.max_positions,
        rank_gamma=not args.no_gamma,
        probability_horizon=args.probability_horizon,
    )
    output = (
        Path(args.output).resolve()
        if args.output
        else settings.data_dir / "backtests" / f"{args.symbol}-{args.date}-{datetime.now():%Y%m%d-%H%M%S}"
    )
    if command == "trade":
        output = (
            Path(args.output).resolve()
            if args.output
            else settings.data_dir / "trading" / f"{args.symbol}-{datetime.now():%Y%m%d-%H%M%S}"
        )
    output.mkdir(parents=True, exist_ok=True)
    if command == "backtest":
        from qwork.backtesting.broken_wing import run_backtest
        from qwork.backtesting.execution import ReplayExecutionConfig

        execution = ReplayExecutionConfig(
            mode=args.execution,
            entry_limit_fraction=args.entry_limit_fraction,
            entry_timeout_seconds=args.entry_timeout,
            retry_cooldown_seconds=args.retry_cooldown,
            max_entry_attempts=args.max_entry_attempts,
            latency_ms=args.latency_ms,
            combo_spread_fraction=args.combo_spread_fraction,
            leg_slippage_per_share=args.leg_slippage,
            liquidity_fraction=args.liquidity_fraction,
            touch_fill_probability=args.touch_fill_probability,
            random_seed=args.fill_seed,
        )
        report = run_backtest(
            settings, config, args.date, output, args.interval, args.universe_size, execution=execution
        )
        print(
            f"Status={report['status']} | closed={report['closed_combinations']} | net_pnl={report['net_pnl']:.2f} USD | flat={report['flat']}"
        )
        print(f"Report: {output}")
        if report["status"] not in {"completed", "no_candidates"}:
            raise SystemExit(1)
    else:
        from qwork.runtime.trading import run_live
        from qwork.strategies.broken_wing.lifecycle import OrderManagementConfig
        from qwork.strategies.broken_wing.risk import AccountRiskConfig

        orders = OrderManagementConfig(
            mode=args.execution,
            entry_limit_fraction=args.entry_limit_fraction,
            entry_timeout_seconds=args.entry_timeout,
            retry_cooldown_seconds=args.retry_cooldown,
            max_entry_attempts=args.max_entry_attempts,
        )
        risk = AccountRiskConfig(
            daily_loss_limit_usd=args.daily_loss_limit,
            max_drawdown_usd=args.drawdown_limit,
            account_max_age_seconds=args.account_max_age,
            available_funds_buffer_usd=args.funds_buffer,
        )
        run_live(settings, config, output, args.mode, args.account_id, orders=orders, risk=risk)


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    parser = argparse.ArgumentParser(prog="qwork", description="Nautilus options research and intraday strategies")
    subcommands = parser.add_subparsers(dest="command", required=True)
    for name, description in [
        ("dashboard", "Dash / pywebview analysis"),
        ("backtest", "Real-data intraday replay"),
        ("trade", "Explicit IBKR paper/live execution"),
        ("terminal", "Dockable Dash options trading terminal"),
    ]:
        subcommands.add_parser(name, help=description, add_help=False)
    parsed, remaining = parser.parse_known_args(argv)
    if parsed.command == "terminal":
        from qwork.ui.terminal.desktop import main as terminal

        terminal(remaining)
    elif parsed.command == "dashboard":
        from qwork.ui.desktop import main as desktop

        desktop(remaining)
    else:
        strategy_command(parsed.command, remaining)
