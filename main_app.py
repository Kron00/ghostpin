"""Ghostpin — native app entry point."""

import sys
import os
import re
import threading
import time
import socket

# Handle PyInstaller frozen paths
if getattr(sys, "frozen", False):
    _base = sys._MEIPASS
    os.environ.setdefault("FLASK_APP_ROOT", _base)
else:
    _base = os.path.dirname(os.path.abspath(__file__))

if "--tunneld" in sys.argv:
    # Compatibility-only privileged fallback. Normal iOS 17.4+ operation owns
    # a rootless userspace tunnel inside the main Ghostpin process.
    from tunnel_service import run_tunneld_directly

    run_tunneld_directly()
    sys.exit(0)

from app import app, PORT
from device_manager import DeviceManager
from location_service import DATA_DIR, LocationService

import app as app_module


class NativeApi:
    """Native operations that a browser download cannot reliably provide."""

    def __init__(self):
        self.window = None

    def save_gpx(self, content, suggested_filename="route.gpx"):
        """Show a macOS save dialog and write the exported GPX to disk."""
        if not self.window:
            return {"ok": False, "error": "Native window is not ready"}

        safe_name = re.sub(r"[^A-Za-z0-9._ -]+", "-", suggested_filename).strip(" .-")
        if not safe_name.lower().endswith(".gpx"):
            safe_name += ".gpx"
        safe_name = safe_name or "route.gpx"

        try:
            import webview

            selected = self.window.create_file_dialog(
                webview.FileDialog.SAVE,
                save_filename=safe_name,
                file_types=("GPX route (*.gpx)",),
            )
            if not selected:
                return {"ok": False, "cancelled": True}
            path = selected[0] if isinstance(selected, (tuple, list)) else selected
            with open(path, "w", encoding="utf-8", newline="\n") as handle:
                handle.write(content)
            return {"ok": True, "path": path}
        except Exception as exc:
            return {"ok": False, "error": str(exc)}


def start_backend():
    """Initialize the userspace-first device connection and Flask server."""
    device_mgr = DeviceManager()
    app_module.device_mgr = device_mgr
    app_module.loc_svc = None

    print("[*] Looking for device...")
    try:
        device_mgr.connect(retries=3)
        app_module.loc_svc = LocationService(device_mgr.simulator, device_mgr.bridge)
        app_module._start_schedule_checker()
        print("[+] Device connected")
    except Exception:
        print("[*] No device yet — connect from the UI")

    app.run(host="127.0.0.1", port=PORT, debug=False, use_reloader=False)


def wait_for_server(port, timeout=30):
    """Block until Flask is accepting connections."""
    for _ in range(timeout * 2):
        try:
            s = socket.create_connection(("127.0.0.1", port), timeout=0.5)
            s.close()
            return True
        except (ConnectionRefusedError, OSError):
            time.sleep(0.5)
    return False


def _cleanup():
    """Stop all background threads and disconnect device on exit."""
    print("[*] Cleaning up...")
    if app_module.loc_svc:
        app_module.loc_svc.stop_route()
        app_module.loc_svc._stop_keepalive()
    if app_module.device_mgr:
        app_module.device_mgr.shutdown()


def main():
    print("=" * 44)
    print("  Ghostpin")
    print("=" * 44)

    # Start Flask in background
    server = threading.Thread(target=start_backend, daemon=True)
    server.start()

    if not wait_for_server(PORT):
        print("[!] Server failed to start")
        return

    # Try native WebView window, fall back to browser
    try:
        import webview
        native_api = NativeApi()
        window = webview.create_window(
            "Ghostpin",
            f"http://127.0.0.1:{PORT}",
            width=1280,
            height=800,
            min_size=(900, 600),
            background_color="#17363a",
            text_select=False,
            js_api=native_api,
        )
        native_api.window = window
        # pywebview defaults to private mode, which discards localStorage on
        # every launch. Keep UI preferences and onboarding state in the same
        # user-writable Ghostpin data directory as saved locations/routes.
        webview_storage = os.path.join(DATA_DIR, "webview")
        os.makedirs(webview_storage, exist_ok=True)
        webview.start(private_mode=False, storage_path=webview_storage)
    except Exception:
        import webbrowser
        print(f"[*] Opening http://localhost:{PORT}")
        webbrowser.open(f"http://localhost:{PORT}")
        try:
            while True:
                time.sleep(1)
        except KeyboardInterrupt:
            pass
    finally:
        _cleanup()


if __name__ == "__main__":
    main()
