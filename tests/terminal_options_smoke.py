"""Real chain, editable multileg drafts and native portfolio Actor, no orders."""

import json
import time
from pathlib import Path

import requests
from playwright.sync_api import sync_playwright

URL = "http://127.0.0.1:8766"
output = Path("data/verification")
session = requests.Session()
session.trust_env = False
latest, errors = {}, []


def wait(page, predicate):
    deadline = time.monotonic() + 40
    while time.monotonic() < deadline:
        page.wait_for_timeout(200)
        latest.clear()
        latest.update(session.get(URL + "/api/snapshot", timeout=10).json())
        if predicate():
            return
    raise AssertionError("Option workflow timed out")


def quote(page, point):
    key = f"{point['expiration']}-{point['strike']:g}"
    page.evaluate(
        """async key => {
        const api = await window.dash_ag_grid.getApiAsync('t-chain-grid');
        api.ensureNodeVisible(api.getRowNode(key), 'middle');
    }""",
        key,
    )
    page.locator(f"#t-chain-grid .ag-row[row-id='{key}'] .ag-cell[col-id='{point['right']}_ask']").click()


with sync_playwright() as p:
    browser = p.chromium.launch(channel="msedge", headless=True, args=["--use-gl=angle", "--use-angle=swiftshader"])
    page = browser.new_page(viewport=dict(width=1680, height=1050), accept_downloads=True)
    page.on("pageerror", lambda error: errors.append(str(error)))
    try:
        page.goto(URL)
        page.locator("#t-view-chain").click()
        wait(page, lambda: page.locator("#t-chain-grid .ag-row").count() > 0)
        frame = latest["frame"]
        expiration = page.locator("#t-chain-expiration").inner_text().strip()
        other = next(day for day in frame["expirations"] if day != expiration)
        page.locator("#t-chain-expiration").click()
        page.get_by_role("option", name=other, exact=True).click()
        wait(page, lambda: other in page.locator("#t-chain-grid").inner_text())
        page.locator("#t-chain-expiration").click()
        page.get_by_role("option", name=expiration, exact=True).click()
        wait(page, lambda: expiration in page.locator("#t-chain-grid").inner_text())
        with page.expect_download() as download:
            page.locator("#t-chain-export").click()
        file = output / "option-chain-export.csv"
        download.value.save_as(file)
        assert "Call BID" in file.read_text(encoding="utf-8-sig")
        page.locator("#t-chain-select-mode input").check()
        points = sorted(
            [point for point in frame["points"] if point["expiration"] == expiration and point["right"] == "put"],
            key=lambda point: abs(point["strike"] - 756),
        )
        assert len(points) >= 2
        for index, point in enumerate(points[:2]):
            quote(page, point)
            assert page.locator("#t-chain-grid").is_visible(), "Selecting a leg must retain the chain"
            page.locator("#t-leg-buy" if index == 0 else "#t-leg-sell").click()
            page.wait_for_timeout(300)
        page.locator("#t-builder-name").fill("组合 B")
        page.locator("#t-builder-name").press("Tab")
        quote(page, points[0])
        page.locator("#t-leg-buy").click()
        page.locator("#t-view-builder").click()
        wait(page, lambda: page.locator("#t-builder-legs .ag-row").count() == 3)
        cell = page.locator("#t-builder-legs .ag-row").first.locator(".ag-cell[col-id='quantity']")
        cell.dblclick()
        editor = cell.locator("input")
        editor.fill("2")
        editor.press("Enter")
        page.locator("#t-builder-apply").click()
        wait(page, lambda: len((latest.get("portfolio") or {}).get("combinations", [])) == 2)
        assert latest["portfolio_request"]["combinations"][0]["legs"][0]["quantity"] == 2
        assert all(row["priced"] for row in latest["portfolio"]["combinations"])
        wait(page, lambda: "2 组期权组合合计" in page.locator("#t-analysis-summary").inner_text())
        page.locator("#t-analysis-combination").click()
        page.get_by_role("option", name="组合 B", exact=True).click()
        wait(page, lambda: "组合 B" in page.locator("#t-analysis-summary").inner_text())
        assert page.locator("#t-combo-legs .ag-row").count() == 1
        assert not latest["broker"].get("connected") and not latest["strategy"]["running"]
        assert not errors, errors
        page.screenshot(path=str(output / "terminal-options-combinations.png"))
        result = dict(
            status="passed",
            expiration_switch=True,
            chain_csv=True,
            leg_mode_preserves_chain=True,
            edited_quantity=2,
            native_actor_combinations=2,
            individual_and_aggregate_pnl=True,
            browser_errors=errors,
            broker_connected=False,
            trading_started=False,
        )
        (output / "terminal-options-browser.json").write_text(json.dumps(result, indent=2), encoding="utf-8")
        print(json.dumps(result))
    except Exception:
        page.screenshot(path=str(output / "terminal-options-failure.png"))
        raise
    finally:
        browser.close()
