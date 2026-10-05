"""Data provenance and freshness shared by native analysis and its presentation."""

from datetime import UTC, datetime


def matches_surface(result, surface):
    if not result or not surface or not surface.get("updated_at"):
        return False
    keys = ["generation", "symbol", "updated_at"]
    if surface.get("surface_revision") is not None:
        keys.append("surface_revision")
    return all(result.get(key) == surface.get(key) for key in keys)


def market_health(frame, context=None, now=None):
    context, now = context or {}, now or datetime.now(UTC)
    result = dict(valid=False, label="尚无有效行情", age_seconds=None, quote_age_seconds=None)
    if not frame or not frame.get("points"):
        return result
    if frame.get("mode") == "historical":
        return dict(result, valid=True, label="历史快照 · 非实时")
    if frame.get("mode") != "live":
        return result
    if frame.get("synthetic"):
        return dict(result, label="测试数据不可作为实时行情")
    settings = context.get("request") or {}
    lifetime = max(10, 2 * float(settings.get("refresh_seconds", 15)))
    quote_lifetime = lifetime + float(settings.get("max_quote_age_seconds", 5))
    try:
        age = (now - datetime.fromisoformat(frame["observed_at"])).total_seconds()
        quote_ages = [(now - datetime.fromisoformat(point["timestamp"])).total_seconds() for point in frame["points"]]
        quote_age = max(quote_ages)
        result.update(age_seconds=round(age, 1), quote_age_seconds=round(quote_age, 1))
        if age < -2 or min(quote_ages) < -2:
            return dict(result, label="报价时间异常 · 分析已禁用")
        if age > lifetime or quote_age > quote_lifetime:
            return dict(result, label=f"行情过期 · 快照 {max(age, 0):.0f}s / 最旧报价 {max(quote_age, 0):.0f}s")
    except (ValueError, KeyError, TypeError, OverflowError):
        return dict(result, label="报价时间不完整 · 分析已禁用")
    if context.get("paused") or context.get("status") in {"error", "stopped", "waiting"}:
        return dict(result, label="行情采集暂停或异常 · 分析已禁用")
    return dict(result, valid=True, label=f"实时快照 {max(age, 0):.0f}s · 最旧报价 {max(quote_age, 0):.0f}s")
