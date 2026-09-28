"""Feature regressions using temporary storage and no phone or network."""
import io
import json
import os
import tempfile
import unittest
from unittest.mock import patch

import app as app_module
import location_service
from location_service import LocationService


class FeatureApiTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        for name, filename in {
            "SAVED_FILE": "saved.json", "PROFILES_FILE": "profiles.json",
            "SCHEDULES_FILE": "schedules.json", "ROUTES_FILE": "routes.json",
            "DATA_DIR": "",
        }.items():
            patcher = patch.object(location_service, name, os.path.join(temporary.name, filename))
            patcher.start()
            self.addCleanup(patcher.stop)
        self.service = LocationService(None, None)
        for name, value in (("loc_svc", self.service), ("device_mgr", None)):
            patcher = patch.object(app_module, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        self.client = app_module.app.test_client()

    def test_saved_location_lifecycle_without_device(self):
        for lat in (1, 3):
            response = self.client.post('/api/saved', json={"name": "Museum", "lat": lat, "lon": 2, "category": "Travel"})
            self.assertEqual(response.status_code, 200)
        self.assertEqual(self.client.get('/api/saved').get_json(), [
            {"name": "Museum", "lat": 3, "lon": 2, "category": "Travel"}])
        self.assertEqual(self.client.get('/api/saved/categories').get_json(), ['Travel'])
        self.client.delete('/api/saved/Museum')
        self.assertEqual(self.client.get('/api/saved').get_json(), [])

    def test_invalid_persistence_requests_do_not_write(self):
        cases = [
            ('saved', {"name": "Bad", "lat": 91, "lon": 0}),
            ('saved', {"name": "Bad", "lat": "NaN", "lon": 0}),
            ('saved', {"name": "Bad", "lat": 0, "lon": 0, "category": []}),
            ('saved', {"name": [], "lat": 0, "lon": 0}),
            ('profiles', {"name": "Bad", "lat": 0}),
            ('profiles', {"name": "Bad", "speed": "Infinity"}),
            ('routes', {"name": "Bad", "waypoints": None}),
            ('routes', {"name": "Bad", "waypoints": [{"lat": 0, "lng": 0}, {"lat": 95, "lng": 0}]}),
            ('schedules', {"name": "Bad", "lat": 0, "lon": 0, "time": "25:00"}),
            ('schedules', {"name": "Bad", "lat": 0, "lon": 0, "time": "09:00", "days": "mon"}),
        ]
        for endpoint, data in cases:
            with self.subTest(endpoint=endpoint, data=data):
                response = self.client.post('/api/' + endpoint, json=data)
                self.assertEqual(response.status_code, 400, response.data)
                self.assertIn('error', response.get_json())
                self.assertEqual(self.client.get('/api/' + endpoint).get_json(), [])

    def test_non_object_json_is_a_useful_client_error(self):
        for body in ([], [1], "name", 12, None):
            response = self.client.post('/api/saved', data=json.dumps(body), content_type='application/json')
            self.assertEqual(response.status_code, 400)
            self.assertIn('JSON object', response.get_json()['error'])

    def test_profiles_save_settings_without_a_location(self):
        response = self.client.post('/api/profiles', json={"name": "Walking"})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_json()['profile']['speed'], 5)

    def test_schedules_created_in_same_millisecond_have_independent_ids(self):
        data = {"name": "Morning", "lat": 1, "lon": 2, "time": "09:30", "days": []}
        with patch.object(location_service.time, 'time', return_value=1000):
            first = self.client.post('/api/schedules', json=data).get_json()['schedule']
            second = self.client.post('/api/schedules', json=data).get_json()['schedule']
        self.assertNotEqual(first['id'], second['id'])
        self.assertEqual(len(first['days']), 7)
        self.client.post('/api/schedules/' + first['id'] + '/toggle', json={"enabled": False})
        schedules = self.client.get('/api/schedules').get_json()
        self.assertFalse(schedules[0]['enabled'])
        self.assertTrue(schedules[1]['enabled'])
        self.client.delete('/api/schedules/' + first['id'])
        self.assertEqual(len(self.client.get('/api/schedules').get_json()), 1)

    def test_gpx_import_works_offline_and_rejects_invalid_coordinates(self):
        for lat, expected in (('40.7', 200), ('NaN', 400), ('91', 400)):
            xml = f'<gpx><trk><trkseg><trkpt lat="{lat}" lon="-74"/></trkseg></trk></gpx>'
            response = self.client.post('/api/gpx/import', data={"file": (io.BytesIO(xml.encode()), 'route.gpx')})
            self.assertEqual(response.status_code, expected)
        response = self.client.post('/api/gpx/import', data={"file": (io.BytesIO(b'\xff'), 'bad.gpx')})
        self.assertEqual(response.status_code, 400)

    def test_circular_route_is_offline_closed_and_valid_at_poles(self):
        for lat, lon in ((90, 180), (40, 179.999)):
            response = self.client.post('/api/route/circular', json={"lat": lat, "lon": lon, "radius": 1000})
            self.assertEqual(response.status_code, 200)
            points = response.get_json()['waypoints']
            self.assertEqual(points[0], points[-1])
            self.assertEqual(len(points), 37)
            self.assertEqual(len({(p['lat'], p['lng']) for p in points[:-1]}), 36)
            for point in points:
                self.assertTrue(-90 <= point['lat'] <= 90)
                self.assertTrue(-180 <= point['lng'] <= 180)
        for extra in ({"points": 0}, {"radius": -1}, {"radius": "NaN"}):
            response = self.client.post('/api/route/circular', json={"lat": 0, "lon": 0, **extra})
            self.assertEqual(response.status_code, 400)

    def test_route_input_is_validated_before_adaptive_network_requests(self):
        base = {"waypoints": [{"lat": 0, "lng": 0}, {"lat": 1, "lng": 1}],
                "adaptive": True, "coordinates": [[0, 0], [1, 1]]}
        with patch.object(app_module, '_check_ready', return_value=None), patch.object(app_module, '_fetch_speed_profile') as fetch:
            for extra in ({"coordinates": [[0, 0], [None, 1]]},
                          {"coordinates": [1, 2]}, {"speed": "NaN"}, {"waypoints": None}):
                response = self.client.post('/api/route/start', json={**base, **extra})
                self.assertEqual(response.status_code, 400, response.data)
            fetch.assert_not_called()

    def test_switching_movement_stops_competing_runners(self):
        self.service.current_location = {"lat": 0, "lon": 0}
        self.service._route_active = True
        self.service._wander_active = True
        with patch.object(self.service, 'stop_route') as route_stop, patch.object(self.service, 'stop_wander') as wander_stop, patch.object(location_service.threading, 'Thread'):
            self.service.joystick_start('n', 5)
            route_stop.assert_called_once()
            wander_stop.assert_called_once()
        self.service._wander_active = False
        with patch.object(self.service, 'stop_route') as route_stop, patch.object(self.service, 'joystick_stop') as joystick_stop, patch.object(location_service.threading, 'Thread'):
            self.service.start_wander(0, 0, 100, 5)
            route_stop.assert_called_once()
            joystick_stop.assert_called_once()

    def test_invalid_movement_never_starts_threads_or_writes(self):
        with patch.object(self.service, '_sim_set') as write, patch.object(location_service.threading, 'Thread') as thread:
            for speed in (0, float('nan'), 301):
                with self.assertRaises(ValueError):
                    self.service.joystick_start('n', speed)
                with self.assertRaises(ValueError):
                    self.service.start_wander(0, 0, 10, speed)
            with self.assertRaises(ValueError):
                self.service.start_wander(0, 0, -1, 5)
            write.assert_not_called()
            thread.assert_not_called()


if __name__ == '__main__':
    unittest.main()
