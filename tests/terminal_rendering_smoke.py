"""Measure idle rendering and exercise dock repainting without broker actions."""

import argparse
import json
import time
from collections import Counter
from pathlib import Path

import requests
from playwright.sync_api import sync_playwright

parser = argparse.ArgumentParser()
parser.add_argument("--label", default="after")
args = parser.parse_args()
artifacts = Path("data/verification")
artifacts.mkdir(parents=True, exist_ok=True)
updates, errors, latencies, sizing_errors = Counter(), [], [], []
session = requests.Session()
session.trust_env = False


def response_received(response):
    if "_dash-update-component" not in response.url or response.status != 200:
        return
    # Read after the response finishes; never re-enter Playwright from its callback.
    pending.append(response)


pending = []
with sync_playwright() as p:
    browser = p.chromium.launch(channel="msedge", headless=True, args=["--use-gl=angle", "--use-angle=swiftshader"])
    page = browser.new_page(viewport={"width": 1680, "height": 1050})
    page.on("pageerror", lambda error: errors.append(str(error)))
    page.on("response", response_received)
    try:
        page.goto("http://127.0.0.1:8766")
        page.wait_for_function("document.querySelector('#t-service-state')?.textContent === '服务在线'")
        page.wait_for_timeout(5000)
        pending.clear()
        page.evaluate("""() => {
            window.renderLongTasks = [];
            new PerformanceObserver(list => list.getEntries().forEach(e => window.renderLongTasks.push(e.duration)))
                .observe({type:'longtask', buffered:false});
        }""")
        page.wait_for_timeout(12000)
        idle_tasks = page.evaluate("window.renderLongTasks.slice()")
        responses = list(pending)
        idle_bytes = 0
        for response in responses:
            body = response.body()
            idle_bytes += len(body)
            for key, props in json.loads(body).get("response", {}).items():
                for prop in props:
                    updates[f"{key}.{prop}"] += 1
        for control, target in [
            ("#t-menu-term", "#t-term"),
            ("#t-menu-surface", "#t-surface"),
            ("#t-view-chain", "#t-chain-grid"),
            ("#t-view-strategy", "#t-strategy-review"),
            ("#t-view-chart", "#t-chart"),
        ] * 3:
            start = time.perf_counter()
            if "menu-" in control:
                page.locator(".terminal-menu").filter(has=page.locator(control)).locator("summary").click()
            page.locator(control).click()
            page.locator(target).wait_for(state="visible")
            latencies.append(round((time.perf_counter() - start) * 1000, 2))
        splitter = page.locator(".flexlayout__splitter").filter(visible=True).first
        box = splitter.bounding_box()
        assert box
        x, y = box["x"] + box["width"] / 2, box["y"] + box["height"] / 2
        page.mouse.move(x, y)
        page.mouse.down()
        page.mouse.move(
            x + 100 if box["height"] > box["width"] else x, y if box["height"] > box["width"] else y - 60, steps=40
        )
        page.mouse.up()
        page.wait_for_timeout(700)
        for width, height in [(1366, 800), (1680, 1050)]:
            page.set_viewport_size({"width": width, "height": height})
            page.wait_for_timeout(700)
            dimensions = page.locator("#t-chart .js-plotly-plot").evaluate("""plot => {
                const rect = plot.getBoundingClientRect();
                return {width:rect.width,height:rect.height,plotWidth:plot._fullLayout.width,plotHeight:plot._fullLayout.height};
            }""")
            if (
                abs(dimensions["width"] - dimensions["plotWidth"]) >= 3
                or abs(dimensions["height"] - dimensions["plotHeight"]) >= 3
            ):
                sizing_errors.append(dimensions)
        assert page.locator(".flexlayout__outline_rect").count() == 0
        assert not errors, errors
        snapshot = session.get("http://127.0.0.1:8766/api/snapshot", timeout=10).json()
        assert not snapshot["strategy"]["running"] and not snapshot["broker"].get("connected")
        result = dict(
            label=args.label,
            idle_seconds=12,
            idle_response_bytes=idle_bytes,
            idle_updates=dict(updates),
            idle_long_tasks_ms=idle_tasks,
            panel_switch_ms=latencies,
            chart_dimensions=dimensions,
            sizing_errors=sizing_errors,
            browser_errors=errors,
            broker_connected=False,
            trading_started=False,
        )
        (artifacts / f"terminal-rendering-{args.label}.json").write_text(json.dumps(result, indent=2), encoding="utf-8")
        page.screenshot(path=str(artifacts / f"terminal-rendering-{args.label}.png"))
        print(json.dumps(result))
        if args.label != "before":
            assert not sizing_errors, sizing_errors
            # One in-flight initial delivery may cross the measurement boundary.
            assert all(count <= 1 for key, count in updates.items() if key.endswith(".rowData")), updates
    finally:
        browser.close()
