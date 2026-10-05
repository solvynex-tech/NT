"""Panel registry: add modules without changing the docking implementation."""

from copy import deepcopy
from dataclasses import dataclass


@dataclass(frozen=True)
class PanelSpec:
    key: str
    title: str


PANELS = (
    PanelSpec("market", "市场报价"),
    PanelSpec("navigator", "导航器"),
    PanelSpec("chart", "K 线"),
    PanelSpec("chain", "期权链"),
    PanelSpec("strategy", "EA · 破翼蝴蝶"),
    PanelSpec("account", "账户"),
    PanelSpec("positions", "持仓"),
    PanelSpec("orders", "委托与成交"),
    PanelSpec("backtest", "策略测试器"),
    PanelSpec("equity", "净值与回测结果"),
    PanelSpec("events", "策略日志"),
    PanelSpec("surface", "波动率曲面"),
    PanelSpec("analysis", "组合风险"),
    PanelSpec("term", "期限结构"),
)


def dock_model(workspace="research"):
    def tab(key):
        return dict(type="tab", id=key, name=next(p.title for p in PANELS if p.key == key))

    def tabset(keys, weight, selected=0):
        return dict(type="tabset", weight=weight, selected=selected, children=[tab(key) for key in keys])

    toolbox = ["positions", "orders", "account", "equity", "events", "backtest", "strategy"]
    layout = dict(
        type="row",
        children=[
            dict(
                type="row",
                weight=70 if workspace == "replay" else 76,
                children=[
                    dict(type="row", weight=19, children=[tabset(["market"], 62), tabset(["navigator"], 38)]),
                    tabset(["chart", "chain", "surface", "term", "analysis"], 81),
                ],
            ),
            tabset(
                toolbox, 30 if workspace == "replay" else 24, toolbox.index("backtest") if workspace == "replay" else 0
            ),
        ],
    )

    return {
        "global": {
            "rootOrientationVertical": True,
            "tabEnableClose": False,
            "tabEnableFloat": False,
            "tabSetEnableMaximize": True,
            "splitterSize": 5,
            "tabEnableRenderOnDemand": False,
        },
        "borders": [],
        "layout": layout,
    }


def restore_model(saved, workspace):
    """Preserve docking while upgrading older local layouts with new panels."""
    model = deepcopy(saved)
    if not isinstance(model, dict) or not isinstance(model.get("layout"), dict):
        return dock_model(workspace)
    tabs = []
    parents = {}
    nodes = 0

    def visit(node, depth=0):
        nonlocal nodes
        nodes += 1
        if nodes > 100 or depth > 10 or not isinstance(node, dict) or not isinstance(node.get("children", []), list):
            raise ValueError("Invalid layout structure")
        for child in node.get("children", []):
            if not isinstance(child, dict):
                raise ValueError("Invalid layout child")
            if child.get("type") == "tab":
                tabs.append(child.get("id"))
                parents[child.get("id")] = node
            else:
                visit(child, depth + 1)

    try:
        visit(model["layout"])
        if not isinstance(model.get("borders", []), list) or not isinstance(model.get("global", {}), dict):
            raise ValueError("Invalid layout settings")
        for item in model.get("borders", []):
            visit(item)
    except ValueError:
        return dock_model(workspace)
    if len(tabs) != len(set(tabs)) or any(key not in {panel.key for panel in PANELS} for key in tabs):
        return dock_model(workspace)
    parent = parents.get("chart")
    if not parent:
        return dock_model(workspace)
    for spec in PANELS:
        if spec.key not in tabs:
            parent["children"].append(dict(type="tab", id=spec.key, name=spec.title))
    model.setdefault("global", {})["tabEnableRenderOnDemand"] = False
    return model


def options_model(workspace="research"):
    """Chain and combination risk remain visible together; MT5 preset stays available."""
    model = dock_model(workspace)
    desk, toolbox = model["layout"]["children"]

    def tabs(keys, weight):
        return dict(
            type="tabset",
            weight=weight,
            selected=0,
            children=[dict(type="tab", id=key, name=next(p.title for p in PANELS if p.key == key)) for key in keys],
        )

    desk["children"] = [tabs(["chain", "chart", "market", "navigator", "surface", "term"], 55), tabs(["analysis"], 45)]
    if workspace in {"paper", "live"}:
        desk["weight"], toolbox["weight"] = 62, 38
        toolbox["selected"] = next(i for i, tab in enumerate(toolbox["children"]) if tab["id"] == "strategy")
    return model
