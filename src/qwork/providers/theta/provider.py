from __future__ import annotations

from pathlib import Path
from threading import RLock

from qwork.core.domain import Contract, normalize_quotes, parse_date


class ProviderError(RuntimeError):
    pass


class ThetaProvider:
    """Use Theta's official gRPC SDK; API keys never travel to the browser."""

    def __init__(self, api_key: str, data_dir: Path | None = None):
        self._key = api_key
        self._client = None
        self._lock = RLock()
        from qwork.providers.theta.session import ThetaSessionCache

        self._sessions = ThetaSessionCache(data_dir) if data_dir else None

    def _new_client(self, previous_session=None):
        import logging

        from thetadata import ThetaClient

        # The SDK logs its authentication response, including its session token.
        logging.getLogger("thetadata.client").disabled = True

        def factory(**kwargs):
            return ThetaClient(dataframe_type="pandas", **kwargs)

        return (
            self._sessions.get(self._key, factory, previous_session) if self._sessions else factory(api_key=self._key)
        )

    def _call(self, method: str, **kwargs) -> list[dict]:
        with self._lock:
            if not self._key:
                raise ProviderError("请在后端 .env 中配置 THETADATA_API_KEY")
            try:
                if self._client is None:
                    self._client = self._new_client()
                try:
                    frame = getattr(self._client, method)(**kwargs)
                except Exception as exc:
                    code = exc.code().name if callable(getattr(exc, "code", None)) else None
                    if code != "UNAUTHENTICATED":
                        raise
                    # Another application or an expired session can invalidate this token.
                    previous = self._client.auth_token.session_uuid
                    self._client = self._new_client(previous)
                    frame = getattr(self._client, method)(**kwargs)
                return frame.to_dict(orient="records")
            except ImportError as exc:
                raise ProviderError("未安装 Thetadata SDK，请安装项目依赖") from exc
            except Exception as exc:
                # SDK exception text can contain authentication material; do not expose it.
                kind = type(exc).__name__
                code = exc.code().name if callable(getattr(exc, "code", None)) else None
                if "Auth" in kind or code == "UNAUTHENTICATED":
                    message = "Thetadata 身份验证失败，请检查后端密钥"
                elif code == "PERMISSION_DENIED" or any(
                    word in kind for word in ("Permission", "Subscription", "Forbidden")
                ):
                    message = "当前 Thetadata 套餐不支持此接口"
                elif "NoData" in kind:
                    message = "该合约或日期没有数据；盘外请使用历史回测"
                elif code in {"UNAVAILABLE", "DEADLINE_EXCEEDED"}:
                    message = "Thetadata 网络连接暂时不可用，请稍后重试"
                else:
                    message = f"Thetadata 请求失败（{kind}），请检查网络、套餐和查询参数"
                raise ProviderError(message) from exc

    def expirations(self, symbol: str) -> list[str]:
        rows = self._call("option_list_expirations", symbol=symbol)
        return sorted({parse_date(row["expiration"]).isoformat() for row in rows})

    def chain(self, symbol: str, expiration: str) -> dict:
        # Value plans expose quotes; Greeks are fetched explicitly, not assumed available.
        rows = self._call(
            "option_snapshot_quote", symbol=symbol, expiration=parse_date(expiration), strike="*", right="both"
        )
        quotes, rejected = normalize_quotes(rows)
        return {
            "source": "theta",
            "synthetic": False,
            "rows": [q.to_dict() for q in quotes],
            "notes": [f"剔除 {rejected} 条无效/交叉/无量报价", "快照可能在闭市后为空。"],
        }

    def historical_chain(self, symbol: str, expiration: str, day: str, at: str) -> dict:
        rows = self._call(
            "option_at_time_quote",
            symbol=symbol,
            expiration=parse_date(expiration),
            strike="*",
            right="both",
            start_date=parse_date(day),
            end_date=parse_date(day),
            time_of_day=at,
        )
        quotes, rejected = normalize_quotes(rows)
        return {
            "source": "theta",
            "synthetic": False,
            "rows": [q.to_dict() for q in quotes],
            "notes": [f"历史切片 {day} {at} ET；剔除 {rejected} 条无效报价"],
        }

    def greeks(self, symbol: str, expiration: str) -> list[dict]:
        rows = self._call(
            "option_snapshot_greeks_all", symbol=symbol, expiration=parse_date(expiration), strike="*", right="both"
        )
        # Only approved public fields; do not serialize arbitrary SDK objects.
        fields = ("strike", "right", "delta", "gamma", "theta", "vega", "rho", "implied_vol", "underlying_price")
        import math

        result = []
        for row in rows:
            values = {
                key: value if not isinstance(value, float) or math.isfinite(value) else None
                for key in fields
                if (value := row.get(key)) is not None
            }
            right = str(values.get("right", "")).lower()
            values["right"] = {"c": "call", "p": "put"}.get(right, right)
            result.append(values)
        return result

    def history(self, contract: Contract, start: str, end: str, interval: str) -> tuple[list, int]:
        rows = self._call(
            "option_history_quote",
            symbol=contract.symbol,
            expiration=parse_date(contract.expiration),
            strike=str(contract.strike),
            right=contract.right,
            start_date=parse_date(start),
            end_date=parse_date(end),
            interval=interval,
        )
        return normalize_quotes(rows, contract)
