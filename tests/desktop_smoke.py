"""Explicit Windows WebView2 smoke check against an already running local dashboard."""

import argparse
import json
import time

import webview

from qwork.core.config import ROOT


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--url", default="http://127.0.0.1:8765")
    args = parser.parse_args()
    if not args.url.startswith("http://127.0.0.1:"):
        raise ValueError("Smoke check requires a loopback URL")
    window = webview.create_window("QWork desktop verification", args.url, hidden=True, width=1280, height=900)
    result = {}

    def loaded():
        try:
            deadline = time.monotonic() + 20
            while time.monotonic() < deadline:
                value = window.evaluate_js(
                    "JSON.stringify(['surface','term'].map(id=>({id,traces:document.querySelector('#'+id+' .js-plotly-plot')?._fullData?.length??0})))"
                )
                graphs = json.loads(value)
                if all(row["traces"] > 0 for row in graphs):
                    result.update(renderer="edgechromium", graphs=graphs, passed=True)
                    break
                time.sleep(0.25)
            else:
                result.update(passed=False, message="Charts did not load within 20 seconds")
        except Exception as exc:
            result.update(passed=False, message=type(exc).__name__)
        finally:
            window.destroy()

    window.events.loaded += loaded
    webview.start(gui="edgechromium", debug=False)
    path = ROOT / "data/verification/desktop.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(result, indent=2), encoding="utf-8")
    print(json.dumps(result))
    if not result.get("passed"):
        raise SystemExit(1)


if __name__ == "__main__":
    main()
