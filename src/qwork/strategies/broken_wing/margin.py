"""Read-only IB what-if preflight. Actual trading orders use native NT execution."""

import math
from threading import Event, Thread

from ibapi.client import EClient
from ibapi.contract import ComboLeg, Contract
from ibapi.order import Order
from ibapi.wrapper import EWrapper


class MarginProbe(EWrapper, EClient):
    def __init__(self, host, port, client_id, account_id):
        EWrapper.__init__(self)
        EClient.__init__(self, self)
        self.target_account = account_id
        self.ready = Event()
        self.accounts_ready = Event()
        self.done = Event()
        self.accounts = []
        self.next_id = None
        self.result = None
        self.failure = None
        self.thread = None
        self.connect(host, port, client_id)
        self.thread = Thread(target=self.run, name="ib-margin-preflight", daemon=True)
        self.thread.start()
        if not self.ready.wait(15) or not self.accounts_ready.wait(15):
            self.close()
            raise ValueError("IBKR margin preflight connection timed out")
        if account_id not in self.accounts:
            self.close()
            raise ValueError("Configured IBKR account was not returned by this connection")

    def nextValidId(self, orderId):
        self.next_id = orderId
        self.ready.set()

    def managedAccounts(self, accountsList):
        self.accounts = accountsList.split(",")
        self.accounts_ready.set()

    def contractDetails(self, reqId, contractDetails):
        self.result = contractDetails.contract

    def contractDetailsEnd(self, reqId):
        self.done.set()

    def openOrder(self, orderId, contract, order, orderState):
        if not order.whatIf:
            self.failure = "Unexpected non-what-if order callback; no trading order will be sent"
        else:
            try:
                value = float(orderState.initMarginChange)
                if not math.isfinite(value) or not 0 < value < 1e8:
                    raise ValueError("IBKR returned no usable initial margin change")
                self.result = value
            except (TypeError, ValueError):
                self.failure = "IBKR returned no usable initial margin change"
        self.done.set()

    def error(self, reqId, errorCode, errorString, advancedOrderRejectJson=""):
        if reqId >= 0 and errorCode not in {2104, 2106, 2158}:
            self.failure = f"IBKR margin preflight rejected request (code {errorCode})"
            self.done.set()

    def _wait(self):
        if not self.done.wait(20):
            raise ValueError("IBKR margin preflight request timed out")
        if self.failure:
            raise ValueError(self.failure)
        if self.result is None:
            raise ValueError("IBKR margin preflight returned no result")
        return self.result

    def reset(self):
        self.done.clear()
        self.result = self.failure = None
        self.next_id += 1
        return self.next_id

    def estimate(self, candidate):
        contract = Contract()
        contract.symbol = candidate["legs"][0]["symbol"]
        contract.secType = "BAG"
        contract.currency = "USD"
        contract.exchange = "SMART"
        contract.comboLegs = []
        for index, strike in enumerate(candidate["strikes"]):
            option = Contract()
            option.symbol = contract.symbol
            option.secType = "OPT"
            option.currency = "USD"
            option.exchange = "SMART"
            option.lastTradeDateOrContractMonth = candidate["expiration"].replace("-", "")
            option.strike = strike
            option.right = "C" if candidate["right"] == "call" else "P"
            option.multiplier = "100"
            request_id = self.reset()
            self.reqContractDetails(request_id, option)
            qualified = self._wait()
            leg = ComboLeg()
            leg.conId = qualified.conId
            leg.ratio = 2 if index == 1 else 1
            leg.action = "SELL" if index == 1 else "BUY"
            leg.exchange = "SMART"
            contract.comboLegs.append(leg)
        order = Order()
        order.account = self.target_account
        order.action = "BUY"
        order.orderType = "LMT"
        order.totalQuantity = 1
        order.lmtPrice = round(candidate["net_premium_per_share"], 2)
        order.tif = "DAY"
        order.whatIf = True
        order.transmit = True
        order.eTradeOnly = False
        order.firmQuoteOnly = False
        request_id = self.reset()
        self.placeOrder(request_id, contract, order)
        return self._wait()

    def close(self):
        self.disconnect()
        if self.thread and self.thread.is_alive():
            self.thread.join(timeout=2)
