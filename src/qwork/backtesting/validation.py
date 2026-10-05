"""Independent reconciliation of native reports against input option quotes."""

import ast
from bisect import bisect_right
from collections import defaultdict
from datetime import UTC, datetime

from qwork.core.domain import eastern, timestamp_ns


def audit_replay(report, native_reports, candidates, spreads, history):
    checks = []

    def check(name, failures, **details):
        checks.append(dict(name=name, passed=not failures, failures=failures, **details))

    config = report["config"]
    execution = report.get("execution", {})
    orders = {row["client_order_id"]: row for row in native_reports.get("orders", [])}
    spread_fraction = execution.get("combo_spread_fraction", 1)
    by_instrument = {str(spreads[c["key"]].id): c for c in candidates}
    indexes = {key: ([q.ts_ns for q in rows], rows) for key, rows in history.items()}
    selection = timestamp_ns(report["selection_timestamp"])
    failures = []
    for key, (_, rows) in indexes.items():
        if any(
            datetime.fromtimestamp(q.ts_ns / 1e9, UTC).astimezone(eastern()).date().isoformat() != report["date"]
            for q in rows
        ):
            failures.append(str(key))
    check("history_session", failures, contracts=len(indexes))

    quote_failures, fee_failures, time_failures = [], [], []
    fills = native_reports["fills"]
    attribution = dict(
        mid_mark_change=0.0,
        bid_ask_crossing_cost=0.0,
        slippage_cost=0.0,
        commission=0.0,
        price_improvement=0.0,
        quote_rounding=0.0,
    )
    usage = defaultdict(int)
    capacity = {}
    for fill in fills:
        order_id = fill["client_order_id"]
        stamp = timestamp_ns(fill["ts_event"])
        local = datetime.fromtimestamp(stamp / 1e9, UTC).astimezone(eastern())
        buying = fill["order_side"] == "BUY"
        if (
            stamp < selection
            or stamp >= timestamp_ns(report["session_close"])
            or local.date().isoformat() != report["date"]
        ):
            time_failures.append(order_id)
        if buying and not config["entry_time"] <= local.time().isoformat() < config["last_entry_time"]:
            time_failures.append(order_id)
        commission = float(fill["commission"].split()[0])
        attribution["commission"] += commission
        units = int(fill["last_qty"])
        if abs(commission - 4 * config["commission_per_contract"] * units) > 0.005:
            fee_failures.append(order_id)
        candidate = by_instrument[fill["instrument_id"]]
        quotes = []
        for i, strike in enumerate(candidate["strikes"]):
            key = (candidate["expiration"], strike, candidate["right"])
            stamps, rows = indexes[key]
            index = bisect_right(stamps, stamp) - 1
            if index < 0:
                break
            q = rows[index]
            quotes.append(q)
            side = "ask" if buying != (i == 1) else "bid"
            usage_key = (q.ts_ns, key, side)
            usage[usage_key] += units * (2 if i == 1 else 1)
            capacity[usage_key] = getattr(q, side + "_size")
        if len(quotes) != 3:
            quote_failures.append(dict(order=order_id, reason="Missing causal leg quote"))
            continue
        if max(q.ts_ns for q in quotes) - min(q.ts_ns for q in quotes) > config["quote_max_skew_seconds"] * 1e9:
            quote_failures.append(dict(order=order_id, reason="Unsynchronized source legs"))
        max_execution_age = max(config["quote_max_skew_seconds"], execution.get("latency_ms", 0) / 1000 + 1)
        if stamp - min(q.ts_ns for q in quotes) > max_execution_age * 1e9:
            quote_failures.append(dict(order=order_id, reason="Stale execution quote"))
        low, body, high = quotes
        mid = sum((q.ask + q.bid) / 2 * (-2 if i == 1 else 1) for i, q in enumerate(quotes))
        half_spread = sum((q.ask - q.bid) / 2 * (2 if i == 1 else 1) for i, q in enumerate(quotes))
        attribution["mid_mark_change"] += mid * units * 100 * (-1 if buying else 1)
        half_spread *= spread_fraction
        attribution["bid_ask_crossing_cost"] += half_spread * units * 100
        attribution["slippage_cost"] += 4 * config["slippage_per_share"] * units * 100
        value = mid + (half_spread + 4 * config["slippage_per_share"]) * (1 if buying else -1)
        offset = spreads[candidate["key"]].info["accounting_price_offset"]
        expected = round(value + offset, 2)
        actual = float(fill["last_px"])
        attribution["quote_rounding"] += (value + offset - expected) * (1 if buying else -1) * units * 100
        attribution["price_improvement"] += (expected - actual) * (1 if buying else -1) * units * 100
        order = orders.get(order_id)
        if execution and buying and fill.get("order_type") == "LIMIT" and order:
            limit = float(order["price"])
            if (
                actual > limit + 0.005
                or expected > limit + 0.005
                or (fill["liquidity_side"] == "MAKER" and abs(actual - limit) > 0.005)
                or (fill["liquidity_side"] == "TAKER" and abs(actual - expected) > 0.005)
            ):
                quote_failures.append(
                    dict(
                        order=order_id,
                        reason="Limit fill violates observable quote or limit",
                        expected=expected,
                        actual=actual,
                        limit=limit,
                    )
                )
        elif abs(actual - expected) > 0.005:
            quote_failures.append(dict(order=order_id, expected=expected, actual=actual))
    check("fill_time_and_causality", time_failures, fills=len(fills))
    check("fill_prices_against_source_nbbo", quote_failures, fills=len(fills))
    check("per_contract_commissions", fee_failures)
    attributed_pnl = (
        attribution["mid_mark_change"]
        - attribution["bid_ask_crossing_cost"]
        - attribution["slippage_cost"]
        - attribution["commission"]
        + attribution["price_improvement"]
        + attribution["quote_rounding"]
    )
    check(
        "source_quote_pnl_attribution",
        []
        if abs(attributed_pnl - report["net_pnl"]) <= 0.005
        else [dict(attributed_pnl=attributed_pnl, native_net_pnl=report["net_pnl"])],
    )
    liquidity = [
        dict(timestamp_ns=k[0], contract=str(k[1]), side=k[2], used=n, available=capacity[k])
        for k, n in usage.items()
        if n > capacity[k]
    ]
    check("shared_leg_liquidity", liquidity)

    positions = native_reports["positions"]
    account = native_reports["account"]
    check(
        "native_report_completeness",
        [] if account and (not fills or positions) else ["Missing native account or positions"],
    )
    pnl_failures = []
    for position in positions:
        fees = sum(float(v.split()[0]) for v in ast.literal_eval(position["commissions"]))
        expected = (
            (float(position["avg_px_close"]) - float(position["avg_px_open"])) * float(position["peak_qty"]) * 100
            - fees
            if position["side"] == "FLAT"
            else None
        )
        if expected is None or abs(expected - float(position["realized_pnl"].split()[0])) > 0.005:
            pnl_failures.append(position["position_id"])
    check("native_position_pnl", pnl_failures, positions=len(positions))
    cash_change = float(account[-1]["total"]) - float(account[0]["total"]) if account else 0
    check(
        "account_cash_reconciliation",
        []
        if abs(cash_change - report["net_pnl"]) <= 0.005
        else [dict(cash_change=cash_change, net_pnl=report["net_pnl"])],
        cash_change=cash_change,
    )
    check(
        "intraday_flat_and_margin_released",
        []
        if report["flat"]
        and not report["open_or_pending_combinations"]
        and (not account or float(account[-1]["locked"]) == 0)
        else ["Unresolved position, order or margin"],
    )

    active, used, entry_failures = {}, set(), []
    peak_margin, peak_positions = 0, 0
    for event in report["events"]:
        key = event.get("key")
        if event["kind"] == "open_requested":
            if (
                key in used
                or event["probability"] <= config["min_probability"]
                or event["ratio"] <= config["min_profit_loss_ratio"]
            ):
                entry_failures.append(key)
            used.add(key)
            active[key] = event["margin_reserved"]
            peak_margin = max(peak_margin, sum(active.values()))
            peak_positions = max(peak_positions, len(active))
        elif event["kind"] == "combination_completed" or (
            event["kind"] == "order_terminal" and event["action"] == "open" and event["filled"] == 0
        ):
            active.pop(key, None)
            if event["kind"] == "order_terminal":
                used.discard(key)
    check("entry_thresholds_and_unique_combinations", entry_failures, unique_combinations=len(used))
    check(
        "margin_budget_and_concurrency",
        []
        if peak_margin <= config["margin_budget"] + 0.005 and peak_positions <= config["max_positions"]
        else [dict(peak_margin=peak_margin, peak_positions=peak_positions)],
        peak_reserved_margin=peak_margin,
        peak_positions=peak_positions,
    )
    return dict(
        passed=all(c["passed"] for c in checks),
        checks=checks,
        pnl_attribution={k: round(v, 2) for k, v in attribution.items()},
    )
