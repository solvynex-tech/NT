"""Loopback Dash terminal, optionally displayed by pywebview."""

import argparse
import json
import os
import socket
import time
from threading import Thread
from urllib.request import urlopen

from waitress import create_server

from qwork.core.config import Settings

from .app import create_terminal
from .backend import TerminalBackend


def configure_webview():
    import webview

    webview.settings["ALLOW_DOWNLOADS"] = True
    return webview


def watch_downloads(window):
    if os.name != "nt" or getattr(window, "_download_watch_registered", False):
        return
    from System import Action

    def notify(message):
        stamp = time.monotonic()
        script = (
            "(() => {const stamp=" + str(stamp) + "; if ((window.qworkDownloadStamp||0)>stamp) return; "
            "window.qworkDownloadStamp=stamp; window.dash_clientside?.set_props('t-download-status', {children:"
            + json.dumps(message)
            + "});})()"
        )

        def deliver():
            try:
                window.evaluate_js(script)
            except Exception:
                pass  # The user may have closed the window during a download.

        Thread(target=deliver, daemon=True).start()

    def download(sender, args):
        if args.Cancel:
            notify("已取消保存")
            return
        operation = args.DownloadOperation
        filename = os.path.basename(args.ResultFilePath)

        def state_changed(sender, event):
            state = str(operation.State)
            if state == "Completed":
                notify("已保存：" + filename)
            elif state == "Interrupted":
                notify("下载中断，请检查保存位置或重试")

        operation.StateChanged += state_changed
        notify("正在保存：" + filename)

    def register():
        window.native.webview.CoreWebView2.DownloadStarting += download

    window.native.Invoke(Action(register))
    window._download_watch_registered = True


def show_window(url):
    webview = configure_webview()

    window = webview.create_window(
        "QWork · 期权交易终端", url, width=1680, height=1080, min_size=(1120, 800), background_color="#17191c"
    )
    window.events.loaded += lambda: print("Trading window loaded", flush=True)
    window.events.loaded += lambda: watch_downloads(window)
    storage = Settings.load().data_dir / "ui" / "webview"
    storage.mkdir(parents=True, exist_ok=True)
    webview.start(
        gui="edgechromium" if os.name == "nt" else None, debug=False, private_mode=False, storage_path=str(storage)
    )


def main(argv=None):
    parser = argparse.ArgumentParser(description="QWork dockable options trading terminal")
    parser.add_argument("--port", type=int, default=8766)
    parser.add_argument("--no-gui", action="store_true")
    args = parser.parse_args(argv)
    if args.port:
        try:
            with socket.socket() as probe:
                if os.name == "nt":
                    probe.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
                probe.bind(("127.0.0.1", args.port))
        except OSError as occupied:
            url = f"http://127.0.0.1:{args.port}"
            try:
                with urlopen(url + "/health", timeout=3) as response:
                    if json.load(response).get("application") != "qwork-terminal":
                        raise occupied
            except Exception:
                raise occupied from None
            print(f"Trading terminal already running: {url}", flush=True)
            if not args.no_gui:
                show_window(url)
            return
    backend = TerminalBackend(Settings.load())
    app = create_terminal(backend)
    server = create_server(app.server, host="127.0.0.1", port=args.port, threads=6)
    thread = Thread(target=server.run, name="terminal-waitress", daemon=True)
    url = f"http://127.0.0.1:{server.effective_port}"
    try:
        thread.start()
        print(f"Trading terminal: {url} | No automatic broker connection or strategy start", flush=True)
        if args.no_gui:
            while thread.is_alive():
                time.sleep(0.5)
        else:
            show_window(url)
    except KeyboardInterrupt:
        pass
    finally:
        server.close()
        backend.close()
        thread.join(timeout=2)
