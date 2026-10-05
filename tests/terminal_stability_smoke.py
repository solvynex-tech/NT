"""Browser layout, lost UI connection and short local soak; no broker actions."""

import json
import statistics
import time
from pathlib import Path

import psutil
import requests
from playwright.sync_api import sync_playwright

URL = "http://127.0.0.1:8766"
artifacts = Path("data/verification")
artifacts.mkdir(parents=True, exist_ok=True)
session = requests.Session()
session.trust_env = False
errors, http_errors, layouts, samples = [], [], [], []
server_pid = next(
    item.pid for item in psutil.net_connections("tcp") if item.laddr.port == 8766 and item.status == "LISTEN"
)
server = psutil.Process(server_pid)


def wait(page, predicate, seconds=30):
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        if predicate():
            return
        page.wait_for_timeout(200)
    raise AssertionError("Browser stability condition not reached")


def workspace(page, label):
    page.locator("#t-workspace").click()
    page.get_by_role("option", name=label, exact=True).click()
    page.wait_for_timeout(650)
    page.locator(".terminal-menu").filter(has=page.locator("#t-layout-reset")).locator("summary").click()
    page.locator("#t-layout-reset").click()
    page.wait_for_timeout(1700)


with sync_playwright() as p:
    browser = p.chromium.launch(channel="msedge", headless=True, args=["--use-gl=angle", "--use-angle=swiftshader"])
    page = browser.new_page(viewport=dict(width=1680, height=1050))
    page.on("pageerror", lambda exc: errors.append(str(exc)))
    page.on("response", lambda response: http_errors.append(response.status) if response.status >= 500 else None)
    try:
        page.goto(URL)
        wait(page, lambda: page.locator("#t-service-state").inner_text() == "服务在线")
        for width, height in [(1680, 1050), (1366, 800)]:
            page.set_viewport_size(dict(width=width, height=height))
            for label, primary in [
                ("研究", "#t-chart"),
                ("回放", "#t-backtest-start"),
                ("Paper 模拟交易", "#t-nav-strategy"),
                ("LIVE 实盘", "#t-nav-strategy"),
            ]:
                workspace(page, label)
                wait(page, lambda primary=primary: page.locator(primary).is_visible())
                assert page.locator("#t-view-market").is_visible()
                assert page.locator(primary).is_visible(), (label, width, primary)
                box = page.locator(primary).bounding_box()
                assert 0 <= box["x"] and box["x"] + box["width"] <= width
                assert 0 <= box["y"] and box["y"] + box["height"] <= height
                assert page.locator("#t-watch-grid").is_visible()
                assert page.locator("#t-nav-account").is_visible()
                assert page.locator("#t-positions-grid").is_visible() or label == "回放"
                assert page.evaluate("document.documentElement.scrollWidth <= window.innerWidth"), (
                    label,
                    width,
                    "overflow",
                )
                if label == "研究":
                    wait(page, lambda: not page.locator("#t-strategy-review").is_visible())
                if label == "回放":
                    page.locator("summary").filter(has_text="回测配置").click()
                    configuration = page.locator(".terminal-config-grid").filter(has=page.locator("#t-bt-margin"))
                    columns = configuration.evaluate("element => getComputedStyle(element).gridTemplateColumns")
                    assert len(columns.split()) == 2, columns
                    page.locator("summary").filter(has_text="回测配置").click()
                layouts.append(
                    dict(workspace=label, width=width, height=height, primary_visible=True, horizontal_overflow=False)
                )
                page.screenshot(path=str(artifacts / f"terminal-layout-{width}-{len(layouts)}.png"))
        # Only interrupt this browser's HTTP callbacks. The actual backend stays online.
        workspace(page, "研究")
        page.route("**/*_dash-update-component*", lambda route: route.abort())
        wait(page, lambda: "terminal-network-stale" in page.locator("#t-root").get_attribute("class"), seconds=20)
        assert page.locator("#t-network-warning").is_visible()
        assert page.locator("#t-service-state").inner_text() == "服务响应超时"
        assert page.locator("#t-backtest-start").is_disabled()
        assert (
            page.locator("#t-market-refresh").evaluate("element => getComputedStyle(element).pointerEvents") == "none"
        )
        assert session.get(URL + "/health", timeout=5).ok
        page.screenshot(path=str(artifacts / "terminal-offline.png"))
        page.unroute("**/*_dash-update-component*")
        wait(page, lambda: page.locator("#t-service-state").inner_text() == "服务在线")
        assert not page.locator("#t-network-warning").is_visible()
        print("layout and browser connection recovery passed", flush=True)
        # Corrupt saved layout must restore a usable preset rather than crash.
        workspace(page, "回放")
        page.evaluate(
            "window.dash_clientside.set_props('t-layout-store', {data: {version:2,workspaces:{replay:{layout:{children:[null]}}}}})"
        )
        page.locator(".terminal-menu").filter(has=page.locator("#t-layout-restore")).locator("summary").click()
        page.locator("#t-layout-restore").click()
        wait(page, lambda: page.locator("#t-backtest-start").is_visible())
        # A short, explicit soak measures UI HTTP requests, never execution latency.
        started = time.monotonic()
        for i in range(60):
            before = time.monotonic()
            response = session.get(URL + "/api/snapshot", timeout=5)
            response.raise_for_status()
            state = response.json()
            assert not state["strategy"]["running"]
            assert not state["broker"].get("account")
            memory = psutil.virtual_memory()
            samples.append(
                dict(
                    seconds=round(time.monotonic() - started, 2),
                    http_ms=round((time.monotonic() - before) * 1000, 2),
                    rss_mb=round(server.memory_info().rss / 1048576, 2),
                    free_mb=round(memory.available / 1048576, 2),
                )
            )
            page.wait_for_timeout(1000)
            if i in {29, 59}:
                print(f"local UI soak {i + 1}/60 samples", flush=True)
        assert not errors, errors
        assert not http_errors, http_errors
        latencies = sorted(row["http_ms"] for row in samples)
        result = dict(
            status="passed",
            layouts=layouts,
            offline_detection_and_recovery=True,
            corrupt_layout_fallback=True,
            soak_seconds=round(time.monotonic() - started, 2),
            sample_count=len(samples),
            http_median_ms=statistics.median(latencies),
            http_p95_ms=latencies[int(len(latencies) * 0.95) - 1],
            server_rss_first_mb=samples[0]["rss_mb"],
            server_rss_last_mb=samples[-1]["rss_mb"],
            min_free_mb=min(row["free_mb"] for row in samples),
            browser_errors=errors,
            http_errors=http_errors,
            broker_connected=False,
            trading_started=False,
            samples=samples,
        )
        (artifacts / "terminal-stability-browser.json").write_text(
            json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        print(
            json.dumps(
                {key: value for key, value in result.items() if key not in {"samples", "layouts"}}, ensure_ascii=True
            ),
            flush=True,
        )
    except Exception:
        page.screenshot(path=str(artifacts / "terminal-stability-failure.png"))
        raise
    finally:
        browser.close()
        session.close()
