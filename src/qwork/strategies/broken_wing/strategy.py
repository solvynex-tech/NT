"""Native BAG strategy. Orders, fills, positions and cash are owned by Nautilus."""

import json
import math
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

from nautilus_trader.config import StrategyConfig
from nautilus_trader.model import AccountId, ClientOrderId, OrderSide, TimeInForce
from nautilus_trader.trading import Strategy

from qwork.actors.broken_wing.actor import BUTTERFLY_TOPIC
from qwork.core.domain import eastern

from .analytics import exit_decision, quotes_aligned, tick_metrics
from .lifecycle import OrderManagementConfig
from .recovery import StrategySnapshot, reconcile_intent
from .risk import AccountRiskConfig, AccountRiskGuard


class BrokenWingStrategy(Strategy):
    def __new__(
        cls,
        config,
        candidates,
        spreads,
        leg_ids,
        journal=None,
        expected_account=None,
        execution=None,
        orders=None,
        state_store=None,
        risk=None,
    ):
        claims = (
            [spread.id if hasattr(spread, "id") else spread for spread in spreads.values()]
            if expected_account
            else None
        )
        return super().__new__(
            cls,
            StrategyConfig(
                order_id_tag="BWB",
                external_order_instrument_ids=claims,
                use_uuid_client_order_ids=bool(expected_account),
                log_events=False,
                log_commands=False,
            ),
        )

    def __init__(
        self,
        config,
        candidates,
        spreads,
        leg_ids,
        journal=None,
        expected_account=None,
        execution=None,
        orders=None,
        state_store=None,
        risk=None,
    ):
        self.settings = config
        self.candidates = candidates
        self.spreads = spreads
        self.leg_ids = leg_ids
        self.expected_account = expected_account
        self.journal = Path(journal) if journal else None
        self.latest = {}
        self.positions = {}
        self.completed = set()
        self.used = set()
        self.pending = {}
        self.events = []
        self.metrics = {}
        self.halted = False
        self.entry_attempts = 0
        self.execution = execution
        self.attempts_by_key = {}
        self.next_attempt_ns = {}
        self.order_management = execution or orders or OrderManagementConfig(mode="natural")
        self.order_registry = {}
        self.state_store = state_store
        self.recovery_ready = not expected_account
        self.recovery_failures = []
        self.session_date = None
        self.risk_guard = AccountRiskGuard(risk or AccountRiskConfig())
        self.risk_stop_reason = None
        self.last_guard_reason = None
        self._last_checkpoint = None
        if state_store:
            saved = state_store.load()
            if saved:
                self.restore_intent(saved)

    def restore_intent(self, saved):
        self.positions = saved.positions
        self.pending = saved.pending
        self.order_registry = saved.order_registry
        self.completed = saved.completed
        self.used = saved.used
        self.attempts_by_key = saved.attempts_by_key
        self.next_attempt_ns = saved.next_attempt_ns
        self.halted = saved.halted
        self.session_date = saved.session_date
        self.risk_guard.state = saved.risk

    def snapshot(self):
        return StrategySnapshot(
            account=self.expected_account or "SIM",
            symbol=self.settings.symbol,
            session_date=self.session_date
            or datetime.fromtimestamp(self.clock.timestamp_ns() / 1e9, UTC).astimezone(eastern()).date().isoformat(),
            candidates=self.candidates,
            positions=self.positions,
            pending=self.pending,
            order_registry=self.order_registry,
            completed=self.completed,
            used=self.used,
            attempts_by_key=self.attempts_by_key,
            next_attempt_ns={key: int(value) for key, value in self.next_attempt_ns.items()},
            halted=self.halted,
            risk=self.risk_guard.state,
        )

    def checkpoint(self):
        if not self.state_store or not self.recovery_ready:
            return
        state = self.snapshot()
        encoded = state.model_dump_json()
        if encoded == self._last_checkpoint:
            return
        try:
            self.state_store.save(state)
        except Exception:
            self.halted = True
            self.recovery_ready = False
            self.log.error("Checkpoint failed; new orders blocked, reconcile broker positions")
            raise
        self._last_checkpoint = encoded

    def on_save(self):
        return {"intent": self.snapshot().model_dump_json().encode()}

    def on_load(self, state):
        saved = StrategySnapshot.model_validate_json(state["intent"])
        if saved.account != (self.expected_account or "SIM") or saved.symbol != self.settings.symbol:
            raise ValueError("Native strategy state belongs to another account or symbol")
        self.restore_intent(saved)

    def recover(self):
        account_id = AccountId("IB-" + self.expected_account)
        account = self.cache.account(account_id)
        if not account or not account.last_event or not account.last_event.is_reported:
            self.halted = True
            self.recovery_failures = ["No reported broker account at startup reconciliation"]
            self.record("recovery_blocked", failures=self.recovery_failures)
            return
        today = datetime.fromtimestamp(self.clock.timestamp_ns() / 1e9, UTC).astimezone(eastern()).date().isoformat()
        native_orders = {str(order.client_order_id): order for order in self.cache.orders(account_id=account_id)}
        quantities = {}
        for position in self.cache.positions_open(account_id=account_id):
            name = str(position.instrument_id)
            quantities[name] = quantities.get(name, 0) + float(position.signed_qty)
        saved, failures = reconcile_intent(
            self.snapshot(),
            native_orders,
            quantities,
            self.leg_ids,
            self.id,
            today,
            {key: spread.id for key, spread in self.spreads.items()},
        )
        self.recovery_failures = failures
        if failures:
            self.halted = True
            self.record("recovery_blocked", failures=failures)
            return
        self.restore_intent(saved)
        self.recovery_ready = True
        for client_id, (key, action) in list(self.pending.items()):
            order = native_orders[client_id]
            if action == "close":
                self.positions[key]["close_filled_for_order"] = int(order.filled_qty)
            if not order.is_closed:
                self.positions[key]["cancel_requested"] = False
                self.positions[key]["close_cancel_requested"] = False
            self.on_order_event(type("RecoveredOrder", (), {"client_order_id": order.client_order_id})())
        self.record("recovery_completed", combinations=len(self.positions))
        self.checkpoint()

    def enforce_account_risk(self):
        if not self.expected_account or not self.recovery_ready:
            return
        now = self.clock.timestamp_ns()
        today = datetime.fromtimestamp(now / 1e9, UTC).astimezone(eastern()).date().isoformat()
        account = self.cache.account(AccountId("IB-" + self.expected_account))
        reason = self.risk_guard.check(account.last_event if account else None, now, today)
        if self.state_store and (self.state_store.path.parent / "STOP").exists():
            reason = "operator_stop"
            self.risk_guard.state["stop_reason"] = reason
        if reason != self.last_guard_reason:
            self.last_guard_reason = reason
            self.record("account_entry_guard", reason=reason)
        if reason:
            for client_id, (key, action) in list(self.pending.items()):
                if action == "open":
                    self.request_cancel(client_id, key, action, reason)
        if reason and self.risk_guard.state.get("stop_reason"):
            self.halted = True
            self.risk_stop_reason = reason
            for client_id, (key, action) in list(self.pending.items()):
                if action == "open":
                    self.positions[key]["close_reason"] = reason
                    self.request_cancel(client_id, key, action, reason)
            for key, position in list(self.positions.items()):
                if position["units"] > 0:
                    self.close_position(key, reason)

    def request_cancel(self, client_id, key, action, reason):
        position = self.positions[key]
        flag = "cancel_requested" if action == "open" else "close_cancel_requested"
        if position.get(flag):
            return
        position[flag] = True
        position["cancel_requested_ns"] = self.clock.timestamp_ns()
        self.record(
            "entry_cancel_requested" if action == "open" else "close_cancel_requested",
            key=key,
            client_order_id=client_id,
            reason=reason,
        )
        self.cancel_order(ClientOrderId(client_id))

    def execution_prices(self, spread, quote):
        if not self.execution or not quote:
            return {}
        offset = (spread.info or {}).get("accounting_price_offset", 0)
        return dict(
            liquidation_price=quote["bid"] - offset, closing_friction_per_share=(quote["ask"] - quote["bid"]) / 2
        )

    def manage_pending_entries(self):
        if self.expected_account and not self.recovery_ready:
            return

        now = self.clock.timestamp_ns()
        local = datetime.fromtimestamp(now / 1e9, UTC).astimezone(eastern())
        cutoff = datetime.combine(local.date(), self.settings.last_entry_time, eastern()) - timedelta(
            seconds=math.ceil(getattr(self.execution, "latency_ms", 0) / 1000) + 1
        )
        for client_id, (key, action) in list(self.pending.items()):
            position = self.positions[key]
            flag = "cancel_requested" if action == "open" else "close_cancel_requested"
            if position.get(flag):
                if now - position["cancel_requested_ns"] >= self.order_management.cancel_ack_timeout_seconds * 1e9:
                    self.halted = True
                    if not position.get("cancel_alarm"):
                        position["cancel_alarm"] = True
                        self.record(
                            "execution_halted", key=key, reason="Cancel acknowledgement timed out; order may still fill"
                        )
                continue
            requested = position["entry_requested_ns"] if action == "open" else position["close_requested_ns"]
            timeout = (
                self.order_management.entry_timeout_seconds
                if action == "open"
                else self.order_management.close_timeout_seconds
            )
            expired = now - requested >= timeout * 1e9
            if expired or (action == "open" and local >= cutoff):
                self.request_cancel(client_id, key, action, "timeout" if expired else "entry_cutoff")

    def record(self, kind, **payload):
        row = dict(
            timestamp=datetime.fromtimestamp(self.clock.timestamp_ns() / 1e9, UTC).isoformat(), kind=kind, **payload
        )
        self.events.append(row)
        if self.journal:
            self.journal.parent.mkdir(parents=True, exist_ok=True)
            with self.journal.open("a", encoding="utf-8") as output:
                output.write(json.dumps(row, ensure_ascii=False, allow_nan=False) + "\n")
        self.checkpoint()

    def on_start(self):
        self.spreads = {
            key: (self.cache.instrument(spread) if not hasattr(spread, "make_qty") else spread)
            for key, spread in self.spreads.items()
        }
        if any(spread is None for spread in self.spreads.values()):
            raise ValueError("Native broker did not qualify all BAG contracts")
        if self.expected_account:
            if not self.state_store:
                raise ValueError("IBKR strategy requires a stable checkpoint store")
            self.recover()
        self.subscribe_topic(BUTTERFLY_TOPIC, self.on_scan)
        for instrument_id in set(self.leg_ids.values()) | {spread.id for spread in self.spreads.values()}:
            self.subscribe_quotes(instrument_id)
        self.clock.set_timer("bwb-session", timedelta(seconds=1), callback=self.on_time_event)
        self.record("started", config=self.settings.model_dump(mode="json"))

    def on_scan(self, scan):
        if scan["symbol"] != self.settings.symbol or scan["mode"] != "live" or scan["synthetic"]:
            return
        fresh = {(c["expiration"], c["right"], tuple(c["strikes"])): c for c in scan["candidates"]}
        for candidate in self.candidates:
            update = fresh.get((candidate["expiration"], candidate["right"], tuple(candidate["strikes"])))
            if update:
                candidate.update({key: update[key] for key in ("model_probability", "profit_loss_ratio")})

    def on_quote(self, quote):
        bid, ask = float(quote.bid_price), float(quote.ask_price)
        if not all(math.isfinite(value) for value in (bid, ask)) or ask < bid:
            return
        self.latest[str(quote.instrument_id)] = dict(
            bid=bid, ask=ask, bid_size=int(quote.bid_size), ask_size=int(quote.ask_size), ts_ns=quote.ts_event
        )
        self.evaluate()

    def quotes(self, candidate):
        keys = [(candidate["expiration"], strike, candidate["right"]) for strike in candidate["strikes"]]
        keys.append(
            (candidate["expiration"], candidate["strikes"][1], "put" if candidate["right"] == "call" else "call")
        )
        rows = [self.latest.get(str(self.leg_ids.get(key))) for key in keys]
        now = self.clock.timestamp_ns()
        if not quotes_aligned(rows, now, self.settings.quote_max_age_seconds, self.settings.quote_max_skew_seconds):
            return None
        return rows[:3], rows[3]

    def account_ready(self):
        if not self.expected_account:
            return True
        account = self.cache.account(AccountId("IB-" + self.expected_account))
        if not self.recovery_ready or not account or not account.balances_total() or self.last_guard_reason:
            return False
        # Native execution and risk engine also enforce the broker account state.
        return True

    def evaluate(self, allow_entry=True):
        observed = datetime.fromtimestamp(self.clock.timestamp_ns() / 1e9, UTC)
        local = observed.astimezone(eastern())
        session = local.time().replace(tzinfo=None)
        flatten = session >= self.settings.flatten_time
        if flatten:
            for client_id, (key, action) in list(self.pending.items()):
                if action == "open":
                    self.positions[key]["close_reason"] = "intraday_flatten"
                    self.request_cancel(client_id, key, action, "intraday_flatten")
        for key, position in list(self.positions.items()):
            if position["state"] not in {"open", "partial_open"} or position["units"] <= 0:
                continue
            if flatten:
                # Liquidation must also proceed when the IV solver fails on present quotes.
                self.close_position(key, "intraday_flatten")
                continue
            if position["close_reason"]:
                self.close_position(key, position["close_reason"])
                continue
            rows = self.quotes(position["candidate"])
            if rows:
                spread = self.spreads[key]
                combo_quote = self.latest.get(str(spread.id))
                if self.execution and (
                    not combo_quote
                    or abs(combo_quote["ts_ns"] - max(row["ts_ns"] for row in [*rows[0], rows[1]]))
                    > self.settings.quote_max_skew_seconds * 1e9
                ):
                    continue
                try:
                    metrics = tick_metrics(
                        position["candidate"],
                        *rows,
                        observed,
                        self.settings,
                        position["entry_price"],
                        **self.execution_prices(spread, combo_quote),
                    )
                    self.metrics[key] = metrics
                    reason = exit_decision(position, metrics, self.settings)
                    if reason:
                        self.close_position(key, reason)
                except (ValueError, RuntimeError):
                    pass
        if (
            not allow_entry
            or flatten
            or self.halted
            or not self.account_ready()
            or not self.settings.entry_time <= session < self.settings.last_entry_time
        ):
            return
        occupied = sum(
            position["margin_reserved"]
            for position in self.positions.values()
            if position["state"] not in {"closed", "rejected"}
        )
        count = sum(position["state"] not in {"closed", "rejected"} for position in self.positions.values())
        if count >= self.settings.max_positions:
            return
        prepared = {}
        for candidate in self.candidates:
            key = candidate["key"]
            if key in self.used or key in self.completed:
                continue
            if self.attempts_by_key.get(
                key, 0
            ) >= self.order_management.max_entry_attempts or self.clock.timestamp_ns() < self.next_attempt_ns.get(
                key, 0
            ):
                continue
            rows = self.quotes(candidate)
            if not rows:
                continue
            spread = self.spreads[key]
            spread_quote = self.latest.get(str(spread.id))
            if (
                not spread_quote
                or not 0
                <= self.clock.timestamp_ns() - spread_quote["ts_ns"]
                <= self.settings.quote_max_age_seconds * 1e9
            ):
                continue
            if spread_quote["ask"] - spread_quote["bid"] > self.settings.max_entry_spread:
                continue
            if (
                abs(spread_quote["ts_ns"] - max(row["ts_ns"] for row in [*rows[0], rows[1]]))
                > self.settings.quote_max_skew_seconds * 1e9
            ):
                continue
            increment = Decimal(str(spread.price_increment))
            limit_price = self.order_management.limit_price(spread_quote["bid"], spread_quote["ask"], increment)
            offset = (spread.info or {}).get("accounting_price_offset", 0)
            conservative_entry = (
                limit_price - offset + (4 * self.settings.slippage_per_share if self.expected_account else 0)
            )
            try:
                metrics = tick_metrics(
                    candidate,
                    *rows,
                    observed,
                    self.settings,
                    entry_price=conservative_entry,
                    compute_roots=False,
                    **self.execution_prices(spread, spread_quote),
                )
            except (ValueError, RuntimeError):
                continue
            if (
                metrics["probability"] <= self.settings.min_probability
                or metrics["ratio"] <= self.settings.min_profit_loss_ratio
                or metrics["max_loss"] <= 0
            ):
                continue
            prepared[key] = (candidate, metrics, spread, spread_quote, limit_price)
        ordered = list(prepared.values())
        if self.settings.rank_gamma:
            ordered.sort(key=lambda item: abs(item[1]["net_gamma"]))
        else:
            ordered.sort(key=lambda item: (-item[1]["probability"], -item[1]["ratio"]))
        for candidate, metrics, spread, spread_quote, limit_price in ordered:
            key = candidate["key"]
            if count >= self.settings.max_positions:
                break
            if self.expected_account and candidate.get("broker_margin_per_unit", 0) <= 0:
                continue
            risk_margin = metrics["max_loss"] * self.settings.margin_buffer
            # Live launch requires a broker-confirmed per-unit margin bound.
            margin = max(risk_margin, candidate.get("broker_margin_per_unit", 0))
            allocation = min(
                self.settings.margin_budget / self.settings.max_positions, self.settings.margin_budget - occupied
            )
            if self.expected_account:
                funds = self.risk_guard.available_funds
                if funds is None:
                    continue
                allocation = min(allocation, max(0, funds - occupied))
            units = min(
                int(allocation // margin), self.settings.max_units, metrics["ask_size"], spread_quote["ask_size"]
            )
            if units < 1:
                continue
            position = dict(
                candidate=candidate,
                units=0,
                requested_units=units,
                state="opening",
                margin_reserved=margin * units,
                entry_price=None,
                max_profit_per_unit=metrics["max_profit"],
                initial_plateau_profit=metrics["expiry_plateau_profit"],
                visited_profit_zone=False,
                breakeven_before_zone=False,
                last_spot=metrics["spot"],
                close_reason=None,
                entry_requested_ns=self.clock.timestamp_ns(),
                cancel_requested=False,
            )
            self.positions[key] = position
            self.used.add(key)
            order = self.order_factory.limit(
                instrument_id=spread.id,
                order_side=OrderSide.BUY,
                quantity=spread.make_qty(units),
                price=spread.make_price(limit_price),
                time_in_force=TimeInForce.DAY,
                tags=['IBOrderTags:{"outsideRth":false}'],
            )
            self.pending[str(order.client_order_id)] = (key, "open")
            self.order_registry[str(order.client_order_id)] = (key, "open")
            self.entry_attempts += 1
            self.attempts_by_key[key] = self.attempts_by_key.get(key, 0) + 1
            self.record(
                "open_requested",
                key=key,
                units=units,
                margin_reserved=margin * units,
                probability=metrics["probability"],
                probability_horizon=metrics["probability_horizon"],
                expiration_probability=metrics["expiration_probability"],
                ratio=metrics["ratio"],
                gamma=metrics["net_gamma"],
                client_order_id=str(order.client_order_id),
                attempt=self.attempts_by_key[key],
                limit_price=limit_price - offset,
                reference_bid=spread_quote["bid"] - offset,
                reference_ask=spread_quote["ask"] - offset,
            )
            self.submit_order(order, params={"exchange": "SMART"} if self.expected_account else None)
            occupied += margin * units
            count += 1

    def close_position(self, key, reason):
        if self.expected_account and not self.recovery_ready:
            return
        position = self.positions[key]
        if position["state"] == "closing" or position["units"] <= 0:
            return
        # If an entry only partly filled, cancel its remainder and wait for the
        # terminal acknowledgement before submitting a close.
        for client_id, (pending_key, action) in list(self.pending.items()):
            if pending_key == key and action == "open":
                position["close_reason"] = reason
                self.request_cancel(client_id, key, "open", reason)
                return
        if position.get("close_attempts", 0) >= self.order_management.max_close_attempts:
            self.halted = True
            if not position.get("exit_alarm"):
                position["exit_alarm"] = True
                self.record("execution_halted", key=key, reason="Exit attempt limit reached; inventory remains")
            return
        spread = self.spreads[key]
        close_units = position["units"]
        if (spread.info or {}).get("synthetic_combo_accounting_proxy"):
            quote = self.latest.get(str(spread.id))
            if (
                not quote
                or not 0 <= self.clock.timestamp_ns() - quote["ts_ns"] <= self.settings.quote_max_skew_seconds * 1e9
            ):
                # NT matches market orders immediately against its latest BBO.
                # Wait for an observable current proxy quote, retaining the exit reason.
                position["close_reason"] = reason
                return
            if self.execution and position.get("last_close_quote_ns") == quote["ts_ns"]:
                position["close_reason"] = reason
                return
            position["last_close_quote_ns"] = quote["ts_ns"]
            if self.execution:
                close_units = min(close_units, quote["bid_size"])
                if close_units < 1:
                    position["close_reason"] = reason
                    return
        order = self.order_factory.market(
            instrument_id=spread.id,
            order_side=OrderSide.SELL,
            quantity=spread.make_qty(close_units),
            time_in_force=TimeInForce.IOC if self.execution else TimeInForce.DAY,
            tags=['IBOrderTags:{"outsideRth":false}'],
        )
        position["state"] = "closing"
        position["close_reason"] = reason
        position["close_filled_for_order"] = 0
        position["close_cancel_requested"] = False
        position["close_requested_ns"] = self.clock.timestamp_ns()
        position["close_attempts"] = position.get("close_attempts", 0) + 1
        self.pending[str(order.client_order_id)] = (key, "close")
        self.order_registry[str(order.client_order_id)] = (key, "close")
        self.record(
            "close_requested",
            key=key,
            reason=reason,
            units=close_units,
            client_order_id=str(order.client_order_id),
            metrics={
                name: self.metrics.get(key, {}).get(name)
                for name in ["spot", "pnl", "breakevens", "ivs", "probability_horizon"]
            },
            visited_profit_zone=position["visited_profit_zone"],
            breakeven_before_zone=position["breakeven_before_zone"],
        )
        self.submit_order(order, params={"exchange": "SMART"} if self.expected_account else None)

    def on_order_event(self, event):
        if self.expected_account and not self.recovery_ready:
            return
        client_id = str(event.client_order_id)
        item = self.pending.get(client_id)
        if not item:
            # A canceled entry may fill late, including after a retry. Never silently
            # overwrite the newer attempt's quantity or infer a safe combination.
            owner = self.order_registry.get(client_id)
            if self.expected_account and owner:
                key = owner[0]
                native_units = 0
                complete_evidence = True
                for registered, (combination, action) in self.order_registry.items():
                    if combination != key:
                        continue
                    native_order = self.cache.order(ClientOrderId(registered))
                    if native_order is None:
                        complete_evidence = False
                        break
                    native_units += int(native_order.filled_qty) * (1 if action == "open" else -1)
                if not complete_evidence or native_units != self.positions[key]["units"]:
                    self.halted = True
                    self.recovery_ready = False
                    self.record(
                        "recovery_blocked",
                        key=key,
                        failures=[
                            "Unexpected late order update; reconcile native orders and broker legs before resuming"
                        ],
                    )
            return
        key, action = item
        position = self.positions[key]
        order = self.cache.order(event.client_order_id)
        if not order:
            return
        filled = int(order.filled_qty)
        if action == "open" and filled:
            position["units"] = filled
            spread = self.spreads[key]
            offset = (spread.info or {}).get("accounting_price_offset", 0)
            position["entry_price"] = float(order.avg_px) - offset
            position["state"] = "open" if order.is_closed else "partial_open"
            from qwork.analytics.butterfly import profit_regions

            cost = position["entry_price"] * 100 + 8 * self.settings.commission_per_contract
            position["max_profit_per_unit"] = profit_regions(
                position["candidate"]["strikes"], position["candidate"]["right"], cost
            )[0]
            position["initial_plateau_profit"] = -cost
        elif action == "close":
            newly_filled = filled - position.get("close_filled_for_order", 0)
            position["units"] -= newly_filled
            position["close_filled_for_order"] = filled
            if self.execution and filled and not order.is_closed and not position["close_cancel_requested"]:
                # Native market orders can remain partially filled with finite depth.
                # Cancel the residual, then send only the actual inventory on fresh liquidity.
                self.record("partial_close_cancel_requested", key=key, remaining_units=position["units"])
                self.request_cancel(client_id, key, "close", "partial_close")
        if order.is_closed:
            self.pending.pop(client_id, None)
            self.record(
                "order_terminal",
                key=key,
                action=action,
                status=str(order.status),
                filled=filled,
                average_price=float(order.avg_px) if order.avg_px is not None else None,
            )
            if action == "open":
                if not filled:
                    position["state"] = "rejected"
                    position["margin_reserved"] = 0
                    if str(order.status) in {"CANCELED", "EXPIRED"}:
                        self.used.discard(key)
                        self.next_attempt_ns[key] = (
                            self.clock.timestamp_ns() + self.order_management.retry_cooldown_seconds * 1e9
                        )
                        self.record("entry_not_filled", key=key, status=str(order.status))
                    elif self.expected_account and self.risk_guard.reject():
                        self.enforce_account_risk()
                elif position["close_reason"]:
                    self.close_position(key, position["close_reason"])
                else:
                    position["margin_reserved"] *= filled / position["requested_units"]
            else:
                if position["units"] == 0:
                    position["state"] = "closed"
                    position["margin_reserved"] = 0
                    self.completed.add(key)
                    self.record("combination_completed", key=key, reason=position["close_reason"])
                else:
                    position["state"] = "open"
                    if self.execution and filled == int(order.quantity):
                        self.record("close_slice_completed", key=key, remaining_units=position["units"])
                    else:
                        self.halted = True
                        self.record(
                            "execution_halted",
                            key=key,
                            reason="Close was rejected or incomplete; reconcile before resuming",
                        )
        self.checkpoint()

    def on_time_event(self, event):
        self.manage_pending_entries()
        self.enforce_account_risk()
        self.checkpoint()
        local = (
            datetime.fromtimestamp(self.clock.timestamp_ns() / 1e9, UTC)
            .astimezone(eastern())
            .time()
            .replace(tzinfo=None)
        )
        if local >= self.settings.flatten_time:
            self.evaluate(allow_entry=False)

    def on_stop(self):
        self.unsubscribe_topic(BUTTERFLY_TOPIC, self.on_scan)
        self.clock.cancel_timer("bwb-session")
        self.record("stopped", remaining_positions=[key for key, p in self.positions.items() if p["units"] > 0])
