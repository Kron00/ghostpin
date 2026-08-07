import json
import os
import tempfile
import unittest
from unittest.mock import patch

import app as app_module
import location_service
from location_service import LocationService


class PersistenceApiTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        paths = {
            "SAVED_FILE": "saved_locations.json",
            "PROFILES_FILE": "profiles.json",
            "SCHEDULES_FILE": "schedules.json",
            "ROUTES_FILE": "routes.json",
        }
        self.patchers = [
            patch.object(location_service, name, os.path.join(self.temp_dir.name, filename))
            for name, filename in paths.items()
        ]
        self.patchers.append(patch.object(location_service, "DATA_DIR", self.temp_dir.name))
        for patcher in self.patchers:
            patcher.start()

        fixtures = {
            "saved_locations.json": [{"name": "Home", "lat": 1.0, "lon": 2.0}],
            "profiles.json": [{"name": "Commute"}],
            "schedules.json": [{"id": "morning", "name": "Morning"}],
            "routes.json": [{"id": "route-1", "name": "Loop"}],
            "history.json": [{"lat": 3.0, "lon": 4.0, "ts": 1}],
        }
        for filename, contents in fixtures.items():
            with open(os.path.join(self.temp_dir.name, filename), "w", encoding="utf-8") as handle:
                json.dump(contents, handle)

        self.original_service = app_module.loc_svc
        self.original_manager = app_module.device_mgr
        app_module.loc_svc = LocationService(None, None)
        app_module.device_mgr = None
        app_module.app.config["TESTING"] = True
        self.client = app_module.app.test_client()

    def tearDown(self):
        app_module.loc_svc = self.original_service
        app_module.device_mgr = self.original_manager
        for patcher in reversed(self.patchers):
            patcher.stop()
        self.temp_dir.cleanup()

    def test_saved_data_loads_before_a_device_connects(self):
        endpoints = {
            "/api/saved": "Home",
            "/api/profiles": "Commute",
            "/api/schedules": "Morning",
            "/api/routes": "Loop",
        }
        for endpoint, expected_name in endpoints.items():
            with self.subTest(endpoint=endpoint):
                response = self.client.get(endpoint)
                self.assertEqual(response.status_code, 200)
                self.assertEqual(response.get_json()[0]["name"], expected_name)

        history = self.client.get("/api/history")
        self.assertEqual(history.status_code, 200)
        self.assertEqual(history.get_json()[0]["lat"], 3.0)

    def test_device_actions_still_require_a_connection(self):
        response = self.client.post("/api/location/set", json={"lat": 1, "lon": 2})
        self.assertEqual(response.status_code, 503)


if __name__ == "__main__":
    unittest.main()
