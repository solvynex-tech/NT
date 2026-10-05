"""MT5-style regions, menus, keyboard and real option Watch; no broker actions."""

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
latest, errors, http_errors, checks = {}, [], [], []


def wait(page, predicate, seconds=40):
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        page.wait_for_timeout(200)
        response = session.get(URL + "/api/snapshot", timeout=5)
        response.raise_for_status()
        latest.clear()
        latest.update(response.json())
        if predicate():
            return
    raise AssertionError("MT5 browser condition not reached")


def menu(page, button):
    page.locator(".terminal-menu").filter(has=page.locator(button)).locator("summary").click()
    page.locator(button).click()


with sync_playwright() as p:
    browser = p.chromium.launch(channel="msedge", headless=True, args=["--use-gl=angle", "--use-angle=swiftshader"])
    page = browser.new_page(viewport=dict(width=1680, height=1050))
    page.on("pageerror", lambda error: errors.append(str(error)))
    page.on("response", lambda response: http_errors.append(response.status) if response.status >= 400 else None)
    try:
        page.goto(URL)
        menu(page, "#t-layout-reset")
        wait(page, lambda: page.locator("#t-watch-grid .ag-row").count() >= 6)
        assert page.locator("#t-chart").is_visible()
        assert page.locator("#t-nav-account").is_visible()
        assert page.locator("#t-positions-grid").is_visible()
        page.locator("#t-watch-grid .ag-row").filter(has_text="756P").locator(".ag-cell[col-id='ask']").click()
        wait(
            page,
            lambda: bool(latest["chart"].get("rows")) and latest["chart"].get("contract", {}).get("right") == "put",
        )
        assert latest["chart"]["contract"]["strike"] == 756
        assert latest["chart"]["source"].startswith("Theta NBBO")
        wait(
            page,
            lambda: page.locator("#t-chart .js-plotly-plot").evaluate(
                "plot => plot._fullLayout?.yaxis?.side === 'right'"
            ),
        )
        assert page.evaluate("getComputedStyle(document.body).backgroundColor") == "rgb(23, 25, 28)"
        checks.append("real option Watch selects the complete PUT contract and chart")
        page.locator(".terminal-periods").get_by_text("M1", exact=True).click()
        wait(page, lambda: latest["chart"].get("minutes") == 1 and bool(latest["chart"].get("rows")))
        checks.append("M1 toolbar changes the actual historical bar aggregation")
        page.keyboard.press("Control+r")
        wait(page, lambda: page.locator("#t-backtest-start").is_visible())
        assert page.locator("#t-backtest-start").is_disabled(), (
            "Research must not start a replay through a view shortcut"
        )
        page.keyboard.press("Control+t")
        wait(page, lambda: page.locator("#t-positions-grid").is_visible())
        page.locator("#t-nav-strategy").click()
        wait(page, lambda: page.locator("#t-strategy-review").is_visible())
        assert page.locator("#t-strategy-review").is_disabled()
        assert not latest["strategy"]["running"]
        assert not latest["broker"].get("account")
        menu(page, "#t-menu-term")
        wait(page, lambda: page.locator("#t-term").is_visible())
        checks.append("menus, Navigator and shortcuts select panels without account connection or trading")
        page.keyboard.press("F1")
        assert page.locator("#t-help-menu").get_attribute("open") is not None
        page.keyboard.press("Escape")
        assert page.locator("#t-help-menu").get_attribute("open") is None
        for width, height in [(1680, 1050), (1366, 800)]:
            page.set_viewport_size(dict(width=width, height=height))
            menu(page, "#t-layout-reset")
            wait(page, lambda: page.locator("#t-chart").is_visible() and page.locator("#t-positions-grid").is_visible())
            watch = page.locator("#t-watch-grid").bounding_box()
            navigator = page.locator("#t-nav-account").bounding_box()
            chart = page.locator("#t-chart").bounding_box()
            toolbox = page.locator("#t-positions-grid").bounding_box()
            assert watch["x"] < chart["x"] and navigator["x"] < chart["x"]
            assert navigator["y"] > watch["y"]
            assert chart["width"] > width * 0.65
            assert toolbox["width"] > width * 0.9 and toolbox["y"] > chart["y"] + chart["height"] - 5
            assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
            page.screenshot(path=str(artifacts / f"terminal-mt5-{width}.png"))
            page.screenshot(path=str(artifacts / f"terminal-atas-{width}.png"))
        assert not errors, errors
        assert not http_errors, http_errors
        result = dict(
            status="passed",
            checks=checks,
            sizes=["1680x1050", "1366x800"],
            browser_errors=errors,
            http_errors=http_errors,
            broker_connected=False,
            trading_started=False,
        )
        (artifacts / "terminal-mt5-browser.json").write_text(
            json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        print(json.dumps(result, ensure_ascii=True), flush=True)
    except Exception:
        page.screenshot(path=str(artifacts / "terminal-mt5-failure.png"))
        raise
    finally:
        browser.close()
        session.close()
