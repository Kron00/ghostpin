"""Isolated browser QA server: python tests/run_preview.py.

Uses the real Flask routes, persistence, and movement engine with an in-memory
device transport and deterministic provider fixtures. Never connects a phone.
All saved data is temporary. Map tiles still require internet access.
"""
import asyncio
import os
from pathlib import Path
import sys
import tempfile
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


class PreviewSimulator:
    async def set(self, lat, lon):
        pass

    async def clear(self):
        pass


class PreviewBridge:
    def run(self, coroutine, timeout=None):
        return asyncio.run(coroutine)


class PreviewDevice:
    def __init__(self):
        self.simulator = PreviewSimulator()
        self.bridge = PreviewBridge()
        self.device_info = dict(connected=True, name="Preview iPhone", ios_version="17.4",
                                model="Test device", udid="preview-device", connection_type="USB",
                                developer_mode=True, ddi_mounted=True, tunnel_mode="userspace")

    def get_device_info(self):
        return self.device_info

    def get_tunnel_status(self):
        return {"running": True, "mode": "userspace"}

    def get_available_connections(self):
        return [self.device_info]

    def get_all_devices(self):
        return [self.device_info]

    def connect(self, **kwargs):
        self.device_info["connection_type"] = "WiFi" if kwargs.get("prefer_wifi") else "USB"
        return self.device_info

    reconnect = connect

    def enable_auto_reconnect(self, **kwargs):
        return {"enabled": True}

    def disable_auto_reconnect(self):
        return {"enabled": False}


def main():
    with tempfile.TemporaryDirectory(prefix="ghostpin-preview-") as directory:
        # Isolate the import-time legacy data migration as well as persistence.
        with patch("os.path.expanduser", return_value=directory):
            import location_service
        import app

        device = PreviewDevice()
        app.device_mgr = device
        app.loc_svc = location_service.LocationService(device.simulator, device.bridge)
        origin = {"name": "Eiffel Tower", "lat": 48.8584, "lon": 2.2945}
        destination = {"name": "Champ de Mars", "lat": 48.8556, "lon": 2.2986}

        def directions(start, end):
            return {"provider": "fixture", "route_name": "Champ de Mars walk",
                    "origin": origin, "destination": destination,
                    "coordinates": [[2.2945, 48.8584], [2.2965, 48.8570], [2.2986, 48.8556]],
                    "distance_km": 0.43, "duration_min": 5.2}

        def search(query, *args):
            return [{**place, "display_name": place["name"] + ", Paris, France",
                     "type": "landmark", "source": "fixture"}
                    for place in (origin, destination)]

        app._google_directions_route = directions
        app._fetch_google_maps = search
        app._fetch_photon = lambda *args: []
        app._fetch_nominatim = lambda *args: []
        app._fetch_speed_profile = lambda coordinates, speed: ([speed] * (len(coordinates) - 1), [])
        app._get_ip_location = lambda: {**origin, "city": "Paris", "country": "FR", "timezone": "Europe/Paris"}
        app.app.view_functions["api_default_location"] = lambda: app.jsonify(
            available=True, **origin, city="Paris", country="FR", speed_unit="kmh",
            source="fixture", accuracy="approximate")
        app.check_release = lambda: {"available": False, "current_version": app.VERSION,
                                     "message": "Preview update check complete"}
        # Exercise roam controls and the real movement engine with a fixed path.
        app.app.view_functions["api_roam_route"] = lambda: app.jsonify(
            **{key: value for key, value in directions(None, None).items()
               if key not in ("origin", "destination")},
            waypoints=[{"lat": origin["lat"], "lng": origin["lon"]},
                       {"lat": destination["lat"], "lng": destination["lon"]}],
            speeds=[20, 20], holds=[])
        port = int(os.environ.get("GHOSTPIN_PREVIEW_PORT", "8080"))
        app._ALLOWED_ORIGINS = frozenset({f"http://127.0.0.1:{port}", f"http://localhost:{port}"})
        print(f"Isolated preview at http://127.0.0.1:{port}; no real device or saved data.")
        try:
            app.app.run(host="127.0.0.1", port=port, use_reloader=False)
        finally:
            app.loc_svc.clear_location()


if __name__ == "__main__":
    main()
