"""Explicit browser/native SPY replay check; never connects IBKR or starts trading.

Run against a local terminal: python tests/terminal_smoke.py --url http://127.0.0.1:8766
Requires cached SPY 2026-10-02 minute data and Edge (or Playwright Chromium).
"""

import argparse
import json
import time
from pathlib import Path

import requests
from playwright.sync_api import sync_playwright

parser = argparse.ArgumentParser()
parser.add_argument("--url", default="http://127.0.0.1:8766")
parser.add_argument("--artifacts", default="data/verification")
args = parser.parse_args()
artifacts = Path(args.artifacts)
artifacts.mkdir(parents=True, exist_ok=True)
latest, errors, http_errors = {}, [], []
session = requests.Session()
session.trust_env = False


def capture(response):
    if "_dash-update-component" not in response.url:
        return
    if response.status >= 400:
        http_errors.append(response.status)


def snapshot():
    # Avoid nested synchronous driver calls from Playwright event handlers.
    response = session.get(args.url + "/api/snapshot", timeout=10)
    response.raise_for_status()
    latest.clear()
    latest.update(response.json())


def wait_until(page, condition, seconds=55):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        page.wait_for_timeout(250)
        snapshot()
        if condition():
            return
    raise AssertionError(
        f"Browser/native state did not reach expected condition: {latest.get('replay', {}).get('message')}"
    )


with sync_playwright() as playwright:
    browser = playwright.chromium.launch(
        channel="msedge", headless=True, args=["--use-gl=angle", "--use-angle=swiftshader"]
    )
    page = browser.new_page(viewport=dict(width=1680, height=1050), accept_downloads=True)
    page.on("pageerror", lambda error: errors.append(str(error)))
    page.on("response", capture)
    try:
        page.goto(args.url)
        wait_until(page, lambda: bool(latest.get("frame")))
        assert not latest["broker"].get("account"), "No implicit account connection"
        page.locator("#t-view-chain").click()
        wait_until(page, lambda: page.locator("#t-chain-grid .ag-cell").count() > 20)
        page.locator("#t-view-market").click()
        page.locator("summary").filter(has_text="期权合约设置").click()
        page.locator("#t-chart-load").click()
        wait_until(page, lambda: bool(latest.get("chart", {}).get("rows")))
        assert latest["chart"]["source"].startswith("Theta NBBO")
        page.locator("#t-workspace").click()
        page.get_by_role("option", name="回放", exact=True).click()
        page.locator("summary").filter(has_text="回测配置").click()
        page.locator("#t-horizon").click()
        page.get_by_role("option", name="到期概率对照", exact=True).click()
        page.locator("#t-bt-execution").click()
        page.get_by_role("option", name="自然 ASK 限价", exact=True).click()
        page.locator("#t-speed").click()
        page.get_by_role("option", name="1×", exact=True).click()
        page.locator("#t-backtest-start").click()
        wait_until(
            page,
            lambda: bool(latest.get("replay", {}).get("clock")) or latest.get("replay", {}).get("status") == "error",
        )
        assert latest["replay"]["status"] != "error", latest["replay"]["message"]
        page.locator("#t-pause").click()
        wait_until(page, lambda: latest["replay"].get("paused"))
        paused_clock = latest["replay"]["clock"]
        page.wait_for_timeout(1500)
        snapshot()
        assert latest["replay"]["clock"] == paused_clock
        page.locator("#t-step").click()
        wait_until(page, lambda: latest["replay"]["clock"] != paused_clock)
        step_clock = latest["replay"]["clock"]
        page.wait_for_timeout(1500)
        snapshot()
        assert latest["replay"]["clock"] == step_clock
        page.locator("#t-speed").click()
        page.get_by_role("option", name="最大速度", exact=True).click()
        page.locator("#t-play").click()
        wait_until(page, lambda: latest["replay"].get("status") in {"completed", "error", "incomplete_liquidation"})
        replay = latest["replay"]
        assert replay["status"] == "completed", replay["message"]
        assert replay["report"]["audit_passed"]
        assert replay["report"]["flat"]
        assert abs(replay["report"]["net_pnl"] + 812.8) < 0.01
        assert len(replay["curve"]) == 375
        assert len(replay["positions"]) == 3
        assert replay["orders"][0]["price"] < 1, "Show combo premium, not accounting proxy offset"
        assert replay["frame"]["points"], "Replay chain must come from native option quotes"
        assert all(point["iv"] is None for point in replay["frame"]["points"]), "Do not reuse old IV as current IV"
        page.locator(".flexlayout__tab_button").filter(has_text="账户").click()
        wait_until(page, lambda: page.locator("#t-account-grid .ag-cell").count() > 0)
        page.locator(".flexlayout__tab_button").filter(has_text="委托与成交").click()
        wait_until(page, lambda: page.locator("#t-orders-grid .ag-cell").count() > 0)
        page.locator(".flexlayout__tab_button").filter(has_text="净值与回测结果").click()
        wait_until(page, lambda: page.locator("#t-equity .js-plotly-plot").is_visible())
        page.screenshot(path=str(artifacts / "terminal-replay.png"), full_page=True)
        # Exercise the library's real docking and persistence, not a CSS simulation.
        tab = page.locator(".flexlayout__tab_button").filter(has_text="波动率曲面")
        target = page.locator(".flexlayout__tab_button").filter(has_text="策略测试器")
        tab_box, target_box = tab.bounding_box(), target.bounding_box()
        page.mouse.move(tab_box["x"] + 25, tab_box["y"] + 12)
        page.mouse.down()
        page.mouse.move(target_box["x"] + target_box["width"] / 2, target_box["y"] + target_box["height"] / 2, steps=25)
        page.wait_for_timeout(300)
        page.mouse.up()
        page.locator(".terminal-menu").filter(has=page.locator("#t-layout-save")).locator("summary").click()
        page.locator("#t-layout-save").click()
        wait_until(page, lambda: page.evaluate("Boolean(localStorage.getItem('t-layout-store'))"))
        saved = page.evaluate("JSON.parse(localStorage.getItem('t-layout-store')).workspaces.replay")

        def find_parent(node, key):
            if any(child.get("id") == key for child in node.get("children", [])):
                return node
            for child in node.get("children", []):
                found = find_parent(child, key)
                if found:
                    return found
            return None

        assert find_parent(saved["layout"], "surface") == find_parent(saved["layout"], "backtest"), (
            "Surface tab must actually dock into the Toolbox tabset"
        )
        page.locator(".terminal-menu").filter(has=page.locator("#t-layout-reset")).locator("summary").click()
        page.locator("#t-layout-reset").click()
        page.wait_for_timeout(1200)
        page.locator(".terminal-menu").filter(has=page.locator("#t-layout-restore")).locator("summary").click()
        page.locator("#t-layout-restore").click()
        page.wait_for_timeout(1200)
        page.locator(".flexlayout__tab_button").filter(has_text="策略测试器").click()
        with page.expect_download() as downloading:
            page.locator("#t-export").click()
        download = downloading.value
        download.save_as(str(artifacts / "terminal-fills.csv"))
        assert (artifacts / "terminal-fills.csv").stat().st_size > 200
        page.locator("#t-saved-run").click()
        page.get_by_role("option").filter(has_text=Path(replay["output"]).name).click()
        page.locator("#t-report-load").click()
        wait_until(page, lambda: latest["replay"].get("status") == "archived")
        assert len(latest["replay"]["curve"]) == 375
        page.wait_for_timeout(1200)
        page.evaluate("window.dash_clientside.set_props('t-poll', {disabled: true})")
        page.wait_for_timeout(700)
        page.screenshot(path=str(artifacts / "terminal-docked.png"), full_page=True)
        assert not errors, errors
        assert not http_errors, http_errors
        result = dict(
            status="passed",
            paused_clock=paused_clock,
            stepped_clock=step_clock,
            timestamp_groups=len(replay["curve"]),
            net_pnl=replay["report"]["net_pnl"],
            audit_passed=True,
            flat=True,
            output=replay["output"],
            browser_errors=errors,
            http_errors=http_errors,
            saved_layout=saved,
        )
        (artifacts / "terminal-browser.json").write_text(
            json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        print(json.dumps({k: v for k, v in result.items() if k != "saved_layout"}, ensure_ascii=True), flush=True)
    finally:
        page.remove_listener("response", capture)
        browser.close()
        session.close()
