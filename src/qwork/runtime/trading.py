import os
from datetime import datetime

import exchange_calendars as calendars
from nautilus_trader.adapters import interactive_brokers as ib
from nautilus_trader.common import Environment, LoggerConfig, LogLevel
from nautilus_trader.live import LiveNode
from nautilus_trader.model import InstrumentId, TraderId

from qwork.actors.broken_wing.actor import BrokenWingButterflyActor
from qwork.actors.broken_wing.models import ButterflyScanRequest
from qwork.actors.portfolio_pnl.actor import PortfolioPnLActor
from qwork.actors.term_structure.actor import TermStructureActor
from qwork.actors.volatility_surface.actor import VolatilitySurfaceActor
from qwork.core.domain import eastern
from qwork.core.state import DashboardState, VolatilityRequest
from qwork.providers.theta.collector import SurfaceCollector
from qwork.strategies.broken_wing.analytics import rank_candidates
from qwork.strategies.broken_wing.instruments import contract_keys, ib_spread_id, occ_id
from qwork.strategies.broken_wing.lifecycle import OrderManagementConfig
from qwork.strategies.broken_wing.margin import MarginProbe
from qwork.strategies.broken_wing.recovery import CheckpointStore, account_lease
from qwork.strategies.broken_wing.strategy import BrokenWingStrategy


def run_live(settings, config, output, mode, account_id, orders=None, risk=None):
    if not account_id or (mode == "paper") != account_id.startswith("DU"):
        raise ValueError("Supply an explicit matching paper/live IBKR account ID")
    # A stable account lease and checkpoint are independent of per-run reports.
    from pathlib import Path

    if not account_id.isalnum():
        raise ValueError("Account ID must be alphanumeric")
    state_dir = settings.data_dir / "private" / "trading" / mode / account_id
    store = CheckpointStore(state_dir / f"{config.symbol}.sqlite3", account_id, config.symbol)
    with account_lease(state_dir / "account-lock.sqlite3"):
        saved = store.load()
        collector = SurfaceCollector(settings)
        try:
            _run_live(
                settings,
                config,
                Path(output),
                account_id,
                store,
                saved,
                orders or OrderManagementConfig(),
                risk,
                collector,
            )
        finally:
            collector.close()


def _run_live(settings, config, output, account_id, store, saved, orders, risk, collector):
    host = os.getenv("IBKR_HOST", "127.0.0.1")
    port = int(os.getenv("IBKR_PORT", "4002"))
    client_id = int(os.getenv("IBKR_CLIENT_ID", "71")) + 20
    request = VolatilityRequest(
        symbol=config.symbol,
        min_dte=1,
        max_dte=14,
        max_expirations=3,
        max_quote_age_seconds=5,
        rate=config.rate,
        dividend_yield=config.dividend_yield,
    )
    try:
        frame = collector.collect(request, 0)
    except Exception:
        if not saved or not saved.candidates:
            raise
        frame = {"points": []}
    if not frame["points"]:
        if not saved or not saved.candidates:
            raise ValueError("No fresh real Theta quotes; strategy will not submit orders")
    from qwork.analytics.butterfly import scan_butterflies

    scan_request = ButterflyScanRequest(
        min_probability=config.min_probability, target_ratio=config.min_profit_loss_ratio, max_candidates=100
    )
    candidates = rank_candidates(frame, scan_butterflies(frame, scan_request), config)[:12] if frame["points"] else []
    if saved:
        # Retain qualified instruments for every restored combination, including
        # completed identities which must not silently reopen after a restart.
        by_key = {candidate["key"]: candidate for candidate in saved.candidates}
        by_key.update({candidate["key"]: candidate for candidate in candidates})
        candidates = list(by_key.values())
    if not candidates:
        raise ValueError("No candidates pass mandatory probability and payoff filters")
    calendar = calendars.get_calendar("XNYS")
    today = datetime.now(eastern()).date()
    if not calendar.is_session(str(today)):
        raise ValueError("Not a US market session; automatic entry is disabled")
    from datetime import timedelta

    close = calendar.session_close(str(today)).to_pydatetime().astimezone(eastern())
    config = config.model_copy(
        update={
            "flatten_time": min(config.flatten_time, (close - timedelta(minutes=5)).time().replace(tzinfo=None)),
            "last_entry_time": min(config.last_entry_time, (close - timedelta(minutes=30)).time().replace(tzinfo=None)),
        }
    )
    probe = MarginProbe(host, port, client_id + 2, account_id)
    try:
        for candidate in candidates:
            candidate["broker_margin_per_unit"] = probe.estimate(candidate)
    finally:
        probe.close()
    keys = contract_keys(candidates)
    leg_ids = {key: InstrumentId.from_str(occ_id(config.symbol, *key) + ".OPRA") for key in keys}
    spread_ids = {candidate["key"]: ib_spread_id(candidate) for candidate in candidates}
    provider = ib.InteractiveBrokersInstrumentProviderConfig(
        load_ids=set(leg_ids.values()) | set(spread_ids.values()), build_options_chain=False, build_futures_chain=False
    )
    builder = LiveNode.builder("QWORK-BWB", TraderId("QWORK-BWB"), Environment.LIVE)
    builder = (
        builder.with_reconciliation(True)
        .with_reconciliation_lookback_mins(1440)
        .with_timeout_connection(30)
        .with_logging(LoggerConfig(stdout_level=LogLevel.ERROR))
    )
    builder = builder.add_data_client(
        None,
        ib.InteractiveBrokersDataClientFactory(),
        ib.InteractiveBrokersDataClientConfig(
            host=host,
            port=port,
            client_id=client_id,
            market_data_type=ib.MarketDataType.REALTIME,
            instrument_provider=provider,
        ),
    )
    builder = builder.add_exec_client(
        None,
        ib.InteractiveBrokersExecutionClientFactory(),
        ib.InteractiveBrokersExecutionClientConfig(
            host=host,
            port=port,
            client_id=client_id + 1,
            account_id="IB-" + account_id,
            fetch_all_open_orders=True,
            instrument_provider=provider,
        ),
    )
    node = builder.build()
    try:
        spreads = {key: node.cache.instrument(instrument_id) for key, instrument_id in spread_ids.items()}
        if any(spread is None for spread in spreads.values()):
            # Native providers finish during node startup. Resolve from the strategy's
            # on_start instead of claiming that build() has already loaded contracts.
            spreads = spread_ids
        state = DashboardState(request)
        state.configure_scan(scan_request)
        node.add_actor(TermStructureActor(state))
        node.add_actor(BrokenWingButterflyActor(state))
        node.add_actor(PortfolioPnLActor(state))
        node.add_actor(VolatilitySurfaceActor(state, collector))
        strategy = BrokenWingStrategy(
            config,
            candidates,
            spreads,
            leg_ids,
            output / "events.jsonl",
            expected_account=account_id,
            orders=orders,
            state_store=store,
            risk=risk,
        )
        node.add_strategy(strategy)
        node.run()
    finally:
        node.dispose()
