"""Actual WebView2 CSV save; only the save dialog destination is automated."""

import json
import sys
import time
from pathlib import Path

from qwork.ui.terminal.desktop import configure_webview, watch_downloads

webview = configure_webview()
result = {"allow_downloads": webview.settings["ALLOW_DOWNLOADS"]}
cancel_test = "--cancel" in sys.argv
result["cancel_dialog"] = cancel_test
output = Path("data/verification/desktop-option-chain.csv").resolve()
window = webview.create_window(
    "QWork desktop download verification", "http://127.0.0.1:8766", hidden=True, width=1366, height=800
)


def loaded():
    try:
        from System import Action
        from webview.platforms import edgechromium

        form = window.native

        class SaveDialog:
            def ShowDialog(self, form):
                if result.get("cancel_dialog"):
                    return edgechromium.WinForms.DialogResult.Cancel
                self.FileName = str(output)
                return edgechromium.WinForms.DialogResult.OK

        original = edgechromium.WinForms

        class DialogAutomation:
            SaveFileDialog = SaveDialog

            def __getattr__(self, key):
                return getattr(original, key)

        edgechromium.WinForms = DialogAutomation()

        def observed(sender, args):
            result.update(download_event=True, cancelled=bool(args.Cancel))

        def register():
            form.webview.CoreWebView2.DownloadStarting += observed

        form.Invoke(Action(register))
        watch_downloads(window)
        deadline = time.monotonic() + 20
        while time.monotonic() < deadline:
            if window.evaluate_js(
                "!!document.querySelector('#t-chain-export') && !document.querySelector('#t-chain-export').disabled"
            ):
                break
            time.sleep(0.2)
        window.evaluate_js("document.querySelector('#t-chain-export').click()")
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            if (
                cancel_test
                and window.evaluate_js("document.querySelector('#t-download-status').innerText") == "已取消保存"
            ):
                result["cancel_feedback"] = True
                break
            if output.exists() and "Call BID" in output.read_text(encoding="utf-8-sig"):
                status = window.evaluate_js("document.querySelector('#t-download-status').innerText")
                if status.startswith("已保存"):
                    result.update(saved_csv=True, notification=status)
                    break
            time.sleep(0.2)
        if cancel_test:
            assert result.get("cancel_feedback") and result.get("cancelled") is True, result
        else:
            assert result.get("saved_csv") and result.get("cancelled") is False, result
        edgechromium.WinForms = original
        result["status"] = "passed"
    except Exception as exc:
        result.update(status="failed", error=type(exc).__name__ + ": " + str(exc))
    finally:
        Path("data/verification/desktop-options-" + ("cancel" if cancel_test else "export") + ".json").write_text(
            json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        print(json.dumps(result, ensure_ascii=True), flush=True)
        window.destroy()


window.events.loaded += loaded
webview.start(gui="edgechromium", debug=False)
if result.get("status") != "passed":
    raise SystemExit(1)
