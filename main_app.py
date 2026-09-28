"""Ghostpin — native app entry point."""

import sys
import os
import re
import threading
import time
import socket
from werkzeug.serving import make_server

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
from updater import NativeUpdater, VERSION


class NativeApi:
    """Native operations that a browser download cannot reliably provide."""

    def __init__(self):
        self.window = None
        self._updater = NativeUpdater()

    def check_for_updates(self):
        return self._updater.check()

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
    """Bind before opening the window; phone discovery belongs to the UI."""
    app_module.device_mgr = DeviceManager()
    if app_module.loc_svc is None:
        app_module.loc_svc = LocationService(None, None)
    # Keep a stable origin for webview preferences; fall back when another
    # process owns the normal port. Reserve the socket before starting Flask.
    with socket.socket() as listener:
        listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            listener.bind(("127.0.0.1", PORT))
        except OSError:
            listener.bind(("127.0.0.1", 0))
        listener.listen()
        server = make_server("127.0.0.1", listener.getsockname()[1], app,
                             threaded=True, fd=listener.fileno())
    port = server.port
    app_module._ALLOWED_ORIGINS = frozenset({
        f"http://127.0.0.1:{port}", f"http://localhost:{port}",
    })
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server


def _cleanup():
    """Stop all background threads and disconnect device on exit."""
    print("[*] Cleaning up...")
    if app_module.loc_svc:
        app_module.loc_svc.stop_route()
        app_module.loc_svc.stop_wander()
        app_module.loc_svc.joystick_stop()
        app_module.loc_svc._stop_keepalive()
    if app_module.device_mgr:
        app_module.device_mgr.shutdown()


def main():
    print("=" * 44)
    print("  Ghostpin")
    print("=" * 44)

    server = start_backend()
    port = server.port

    # Try native WebView window, fall back to browser
    try:
        import webview
        native_api = NativeApi()
        app_module._native_update_check = native_api.check_for_updates
        window = webview.create_window(
            "Ghostpin",
            f"http://127.0.0.1:{port}",
            width=1280,
            height=800,
            min_size=(900, 600),
            background_color="#17363a",
            text_select=False,
            js_api=native_api,
        )
        native_api.window = window
        window.events.loaded += native_api._updater.start
        # pywebview defaults to private mode, which discards localStorage on
        # every launch. Keep UI preferences and onboarding state in the same
        # user-writable Ghostpin data directory as saved locations/routes.
        webview_storage = os.path.join(DATA_DIR, "webview")
        os.makedirs(webview_storage, exist_ok=True)
        webview.start(private_mode=False, storage_path=webview_storage,
                      user_agent=f"Ghostpin/{VERSION} (+https://github.com/Kron00/ghostpin)")
    except Exception:
        import webbrowser
        print(f"[*] Opening http://localhost:{port}")
        webbrowser.open(f"http://localhost:{port}")
        try:
            while True:
                time.sleep(1)
        except KeyboardInterrupt:
            pass
    finally:
        server.shutdown()
        server.server_close()
        _cleanup()


if __name__ == "__main__":
    main()
