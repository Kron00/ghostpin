import json
import os
import tempfile
import threading
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

    def test_stop_controls_work_without_a_device(self):
        generation = app_module.loc_svc._route_generation
        for mode in ('route', 'joystick', 'wander'):
            response = self.client.post('/api/' + mode + '/stop')
            self.assertEqual(response.status_code, 200, response.data)
        self.assertGreater(app_module.loc_svc._route_generation, generation)

    def test_device_actions_still_require_a_connection(self):
        response = self.client.post("/api/location/set", json={"lat": 1, "lon": 2})
        self.assertEqual(response.status_code, 503)

    def test_corrupt_file_quarantine_cannot_remove_a_concurrent_save(self):
        path = os.path.join(self.temp_dir.name, "race.json")
        with open(path, "w", encoding="utf-8") as handle:
            handle.write("not json")

        quarantine_started = threading.Event()
        writer_replaced = threading.Event()
        release_quarantine = threading.Event()
        original_replace = os.replace

        def coordinated_replace(source, destination):
            if source == path and destination == path + ".corrupt":
                quarantine_started.set()
                release_quarantine.wait(timeout=2)
            result = original_replace(source, destination)
            if destination == path:
                writer_replaced.set()
            return result

        reader = threading.Thread(target=location_service._read_json_list, args=(path,))
        writer = threading.Thread(
            target=location_service._atomic_write_json,
            args=(path, [{"name": "New"}]),
        )
        with patch.object(location_service.os, "replace", side_effect=coordinated_replace):
            reader.start()
            self.assertTrue(quarantine_started.wait(timeout=1))
            writer.start()
            writer_replaced.wait(timeout=.2)
            release_quarantine.set()
            reader.join(timeout=2)
            writer.join(timeout=2)

        self.assertFalse(reader.is_alive())
        self.assertFalse(writer.is_alive())
        with open(path, "r", encoding="utf-8") as handle:
            self.assertEqual(json.load(handle), [{"name": "New"}])
        with open(path + ".corrupt", "r", encoding="utf-8") as handle:
            self.assertEqual(handle.read(), "not json")


if __name__ == "__main__":
    unittest.main()
