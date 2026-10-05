"""Real browser workflow check; only read-only analysis, never connects a broker."""

import json
import time
from pathlib import Path

import requests
from playwright.sync_api import sync_playwright

URL = "http://127.0.0.1:8766"
artifacts = Path("data/verification")
artifacts.mkdir(parents=True, exist_ok=True)
session = requests.Session()
session.trust_env = False
errors, http_errors, checks = [], [], []
latest = {}


def wait_until(page, condition, seconds=45):
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        page.wait_for_timeout(250)
        response = session.get(URL + "/api/snapshot", timeout=10)
        response.raise_for_status()
        latest.clear()
        latest.update(response.json())
        if condition():
            return
    raise AssertionError("Workflow condition not reached")


def workspace(page, name):
    page.locator("#t-workspace").click()
    page.get_by_role("option", name=name, exact=True).click()
    page.wait_for_timeout(600)


with sync_playwright() as p:
    browser = p.chromium.launch(channel="msedge", headless=True, args=["--use-gl=angle", "--use-angle=swiftshader"])
    page = browser.new_page(viewport=dict(width=1680, height=1050))
    page.on("pageerror", lambda error: errors.append(str(error)))
    page.on(
        "response",
        lambda response: (
            http_errors.append(response.status)
            if "_dash-update-component" in response.url and response.status >= 400
            else None
        ),
    )
    try:
        page.goto(URL)
        page.locator("#t-view-chain").click()
        page.locator("#t-chain-select-mode input").uncheck()
        wait_until(page, lambda: page.locator("#t-chain-grid .ag-cell[col-id='put_ask']").count() > 0)
        assert page.locator("#t-strategy-review").is_disabled()
        assert page.locator("#t-backtest-start").is_disabled()
        # Click the actual PUT quote cell: no manual direction/strike edits or load click.
        cell = (
            page.locator("#t-chain-grid .ag-row")
            .filter(has=page.locator(".ag-cell[col-id='strike']", has_text="756"))
            .locator(".ag-cell[col-id='put_ask']")
            .first
        )
        cell.click()
        wait_until(page, lambda: bool(latest.get("chart", {}).get("rows")))
        assert latest["chart"]["contract"]["right"] == "put"
        assert latest["chart"]["contract"]["strike"] == 756
        checks.append("完整 PUT 合约单击联动真实历史 K 线")
        page.locator(".flexlayout__tab_button").filter(has_text="组合风险").click()
        wait_until(page, lambda: page.locator("#t-candidates .ag-row").count() > 0)
        page.locator("#t-candidates .ag-row").first.click()
        wait_until(page, lambda: bool((latest.get("portfolio") or {}).get("aggregate", {}).get("current_pnl")))
        assert latest["portfolio"]["combinations"][0]["priced"]
        assert len(latest["portfolio_request"]["combinations"][0]["legs"]) == 3
        assert latest.get("term", {}).get("rows")
        wait_until(page, lambda: page.locator("#t-combo-pnl .js-plotly-plot").count() > 0)
        wait_until(page, lambda: "未选择组合" not in page.locator("#t-analysis-summary").inner_text())
        page.wait_for_timeout(800)
        checks.append("原生盈亏与期限 Actor 联动，组合当前 / 到期曲线与逐腿报价")
        page.screenshot(path=str(artifacts / "terminal-workflow-research.png"), full_page=True)
        workspace(page, "回放")
        wait_until(page, lambda: page.locator("#t-backtest-start").is_enabled())
        assert page.locator("#t-pause").is_disabled()
        assert page.locator("#t-play").is_disabled()
        assert page.locator("#t-step").is_disabled()
        page.locator("summary").filter(has_text="回测配置").click()
        page.locator("#t-bt-margin").fill("9000")
        page.locator("#t-bt-margin").press("Tab")
        assert page.locator("#t-margin").input_value() == "10000"
        checks.append("回测参数独立，空闲回放禁用暂停 / 继续 / 单步")
        workspace(page, "LIVE 实盘")
        wait_until(page, lambda: "LIVE" in page.locator("#t-context-banner").inner_text())
        assert page.locator("#t-strategy-review").is_disabled()
        assert page.locator("#t-watch-grid .ag-row").count() == 0
        assert page.locator("#t-chart-load").is_disabled()
        assert not latest["broker"].get("account")
        assert latest["strategy"]["phase"] == "idle"
        checks.append("LIVE 红色环境提示，未核验账户无法启动，不显示历史报价，不自动连接")
        page.screenshot(path=str(artifacts / "terminal-workflow-live.png"), full_page=True)
        workspace(page, "研究")
        page.locator("#t-view-chain").click()
        wait_until(page, lambda: page.locator("#t-chain-grid .ag-row").count() > 0)
        assert not errors, errors
        assert not http_errors, http_errors
        result = dict(
            status="passed",
            checks=checks,
            javascript_errors=errors,
            http_errors=http_errors,
            broker_connected=False,
            trading_started=False,
        )
        (artifacts / "terminal-workflow-browser.json").write_text(
            json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        print(json.dumps(result, ensure_ascii=True), flush=True)
    except Exception:
        page.screenshot(path=str(artifacts / "terminal-workflow-failure.png"), full_page=True)
        raise
    finally:
        browser.close()
        session.close()
