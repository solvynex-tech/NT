"""Actual options workflow, persistence and dock geometry; never connects a broker."""

import json
import time
from pathlib import Path
from urllib.request import urlopen

from playwright.sync_api import sync_playwright

URL = "http://127.0.0.1:8766"
OUT = Path("data/verification")
results = {"errors": [], "geometry": []}


def snapshot():
    with urlopen(URL + "/api/snapshot", timeout=8) as response:
        return json.load(response)


def wait(page, predicate, seconds=30):
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        if predicate():
            return
        page.wait_for_timeout(200)
    raise AssertionError("Workflow timed out")


with sync_playwright() as p:
    browser = p.chromium.launch(channel="msedge", headless=True, args=["--use-gl=angle", "--use-angle=swiftshader"])
    page = browser.new_page(viewport={"width": 1366, "height": 800}, accept_downloads=True)
    page.on("pageerror", lambda e: results["errors"].append(str(e)))
    try:
        page.goto(URL)
        wait(page, lambda: page.locator("#t-chain-grid .ag-row").count() > 0)
        assert page.locator("#t-combo-pnl").is_visible(), "Chain and risk must be visible together"
        assert page.locator("#t-leg-buy").is_disabled()
        page.locator('#t-chain-grid .ag-row .ag-cell[col-id="call_ask"]').first.click()
        page.wait_for_timeout(400)
        assert page.locator("#t-leg-buy").is_disabled(), "Empty quote cannot be added"
        frame = snapshot()["frame"]
        expiration = page.locator("#t-chain-expiration").inner_text().strip()
        point = next(q for q in frame["points"] if q["right"] == "call" and q["expiration"] == expiration)
        key = f"{expiration}-{point['strike']:g}"
        page.evaluate(
            """async key=>{
            const api=await window.dash_ag_grid.getApiAsync('t-chain-grid');
            api.ensureNodeVisible(api.getRowNode(key),'middle');
        }""",
            key,
        )
        page.locator(f'#t-chain-grid .ag-row[row-id="{key}"] .ag-cell[col-id="call_ask"]').click()
        wait(page, lambda: not page.locator("#t-leg-buy").is_disabled())
        assert "BID" in page.locator("#t-chain-selection").inner_text()
        page.locator("#t-leg-buy").click()
        wait(page, lambda: page.locator("#t-builder-legs .ag-row").count() == 1)
        assert "已加入" in page.locator("#t-chain-action").inner_text()
        assert "1 腿" in page.locator("#t-chain-basket-count").inner_text()
        cell = page.locator('#t-builder-legs .ag-row .ag-cell[col-id="quantity"]').first
        cell.dblclick()
        cell.locator("input").fill("2")
        cell.locator("input").press("Enter")
        wait(page, lambda: "已修改并保存" in page.locator("#t-builder-status").inner_text())
        assert "未应用" in page.locator("#t-builder-dirty").inner_text()
        page.reload()
        wait(page, lambda: page.locator("#t-builder-legs .ag-row").count() == 1)
        wait(
            page,
            lambda: (
                page.locator('#t-builder-legs .ag-row .ag-cell[col-id="quantity"]').first.inner_text().strip()
                in {"2", "2.0"}
            ),
        )
        results["draft_restored_quantity"] = 2
        page.locator("#t-builder-apply").click()
        wait(page, lambda: bool((snapshot().get("portfolio") or {}).get("combinations")))
        wait(page, lambda: "一致" in page.locator("#t-builder-dirty").inner_text())
        view = snapshot()
        assert view["portfolio"]["surface_revision"] == view["frame"]["surface_revision"]
        assert view["portfolio_request"]["combinations"][0]["legs"][0]["quantity"] == 2
        results["native_analysis_version_matches"] = True
        page.locator("#t-builder-panel > summary").click()
        cell = page.locator('#t-builder-legs .ag-row .ag-cell[col-id="quantity"]').first
        cell.dblclick()
        cell.locator("input").fill("3")
        cell.locator("input").press("Enter")
        wait(page, lambda: "未应用" in page.locator("#t-builder-dirty").inner_text())
        assert snapshot()["portfolio_request"]["combinations"][0]["legs"][0]["quantity"] == 2
        results["unapplied_edit_does_not_change_analysis"] = True
        for width, height in [(1680, 1050), (1366, 800), (1120, 800)]:
            page.set_viewport_size({"width": width, "height": height})
            page.wait_for_timeout(500)
            geometry = page.evaluate("""()=>{
                const g=document.querySelector('#t-chain-grid'),panel=g.closest('.terminal-panel');
                const h=g.querySelector('.ag-body-horizontal-scroll').getBoundingClientRect();
                const plot=document.querySelector('#t-combo-pnl').getBoundingClientRect();
                const risk=document.querySelector('#t-combo-pnl').closest('.terminal-panel').getBoundingClientRect();
                return {width:innerWidth,panelOverflow:panel.scrollHeight-panel.clientHeight,
                    scrollbarBottom:h.bottom,panelBottom:panel.getBoundingClientRect().bottom,
                    riskPlotVisible:plot.bottom<=risk.bottom,documentOverflow:document.documentElement.scrollWidth>innerWidth};
            }""")
            assert geometry["panelOverflow"] <= 2, geometry
            assert geometry["scrollbarBottom"] <= geometry["panelBottom"] + 2, geometry
            assert geometry["riskPlotVisible"] and not geometry["documentOverflow"], geometry
            results["geometry"].append(geometry)
            page.screenshot(path=str(OUT / f"options-desk-{width}.png"))
        with page.expect_download() as download:
            page.locator("#t-chain-export").click()
        download.value.save_as(OUT / "options-desk-chain.csv")
        assert "Call BID" in (OUT / "options-desk-chain.csv").read_text(encoding="utf-8-sig")
        with page.expect_download():
            page.locator("#t-chain-export").click()
        results["repeated_export"] = True
        page.locator("#t-builder-clear").click()
        wait(page, lambda: "0 腿" in page.locator("#t-chain-basket-count").inner_text())
        page.reload()
        wait(page, lambda: page.locator("#t-chain-basket-count").inner_text() == "篮子 0 腿")
        results["clear_persisted"] = True
        page.locator("#t-workspace").click()
        page.get_by_role("option", name="Paper 模拟交易", exact=True).click()
        wait(page, lambda: page.locator("#t-strategy-review").is_visible())
        assert page.locator("#t-strategy-review").is_disabled()
        assert page.locator("#t-chain-grid").is_visible()
        assert not page.locator("#t-chart").is_visible()
        page.screenshot(path=str(OUT / "options-desk-paper.png"))
        view = snapshot()
        assert not view["broker"].get("connected") and not view["strategy"]["running"]
        assert not results["errors"], results["errors"]
        results["status"] = "passed"
        print(json.dumps(results))
    finally:
        page.screenshot(path=str(OUT / "options-desk-final.png"))
        (OUT / "options-desk-browser.json").write_text(json.dumps(results, indent=2), encoding="utf-8")
        browser.close()
