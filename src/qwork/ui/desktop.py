"""Desktop entrypoint: Nautilus actors + loopback Waitress/Dash + pywebview."""

from __future__ import annotations

import argparse
import os
import time
from threading import Thread

from waitress import create_server

from qwork.core.config import Settings
from qwork.core.state import VolatilityRequest
from qwork.runtime.actors import VolatilityRuntime

from .dashboard import create_dashboard


def main(argv=None):
    parser = argparse.ArgumentParser(description="Nautilus IV surface and ATM term structure")
    parser.add_argument("--symbol", default="SPY", help="Option underlying, e.g. SPY, QQQ or SPXW")
    parser.add_argument("--source", choices=["theta", "fixture"], default="theta")
    parser.add_argument("--date", help="Explicit historical snapshot date; otherwise poll live snapshots")
    parser.add_argument("--at", default="10:00:00", help="Historical snapshot time in America/New_York")
    parser.add_argument("--refresh", type=int, default=15)
    parser.add_argument("--min-dte", type=int, default=1)
    parser.add_argument("--max-dte", type=int, default=180)
    parser.add_argument("--max-expirations", type=int, default=8)
    parser.add_argument("--spot", type=float, help="Optional spot override; otherwise infer forward from option parity")
    parser.add_argument("--port", type=int, default=8765, help="Local dashboard port; 0 selects a free port")
    parser.add_argument("--no-gui", action="store_true", help="Serve Dash in a browser without pywebview")
    parser.add_argument("--width", type=int, default=1560)
    parser.add_argument("--height", type=int, default=980)
    args = parser.parse_args(argv)
    request = VolatilityRequest(
        symbol=args.symbol,
        source=args.source,
        mode="historical" if args.date else "live",
        as_of=args.date,
        at=args.at,
        refresh_seconds=args.refresh,
        min_dte=args.min_dte,
        max_dte=args.max_dte,
        max_expirations=args.max_expirations,
        spot=args.spot,
    )
    runtime = VolatilityRuntime(Settings.load(), request)
    app = create_dashboard(runtime.state)
    server = create_server(app.server, host="127.0.0.1", port=args.port, threads=4)
    server_thread = Thread(target=server.run, name="dash-waitress", daemon=True)
    url = f"http://127.0.0.1:{server.effective_port}"
    try:
        runtime.start()
        server_thread.start()
        print(f"Volatility dashboard: {url} | {request.symbol} | {request.source} / {request.mode}", flush=True)
        if args.no_gui:
            while runtime.thread.is_alive():
                time.sleep(0.5)
        else:
            import webview

            webview.create_window(
                "QWork · 波动率曲面与期限结构",
                url,
                width=args.width,
                height=args.height,
                min_size=(900, 680),
                background_color="#0b111b",
            )
            webview.start(gui="edgechromium" if os.name == "nt" else None, debug=False)
    except KeyboardInterrupt:
        pass
    finally:
        server.close()
        runtime.stop()
        server_thread.join(timeout=2)


if __name__ == "__main__":
    main()
