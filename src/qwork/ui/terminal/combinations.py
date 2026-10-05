"""Option analysis drafts; these objects never represent broker positions."""

from collections import OrderedDict
from uuid import uuid4

from qwork.actors.portfolio_pnl.models import PortfolioRequest, PositionLeg


def quote_leg(frame, row, column, side, name):
    if not column.startswith(("call_", "put_")):
        raise ValueError("请点击 Call 或 Put 报价，再加入对应买入 / 卖出腿")
    right = column.split("_")[0]
    point = next(
        (
            p
            for p in frame.get("points", [])
            if p["expiration"] == row.get("expiration") and p["strike"] == row.get("strike") and p["right"] == right
        ),
        None,
    )
    if not point or not name or not str(name).strip():
        raise ValueError("请选择有效的期权报价和组合名称")
    bid, ask = point.get("bid"), point.get("ask")
    if bid is None or ask is None or bid < 0 or ask < bid:
        raise ValueError("该期权缺少有效双边报价，无法预填分析成本")
    leg = PositionLeg(
        symbol=frame["symbol"],
        expiration=point["expiration"],
        strike=point["strike"],
        right=right,
        side=side,
        quantity=1,
        entry_premium=ask if side == "buy" else bid,
    )
    return dict(
        leg.model_dump(mode="json"),
        leg_id=uuid4().hex,
        combo_name=str(name).strip(),
        quote_timestamp=point.get("timestamp"),
    )


def portfolio_request(rows, fee):
    combinations = OrderedDict()
    for row in rows or []:
        leg = PositionLeg(**{key: row[key] for key in PositionLeg.model_fields if key in row})
        name = str(row.get("combo_name") or "").strip()
        combinations.setdefault(name, []).append(leg)
    if not combinations:
        raise ValueError("先在期权链中加入至少一条分析腿")
    return PortfolioRequest(
        combinations=[dict(name=name, legs=legs) for name, legs in combinations.items()], commission_per_contract=fee
    )
