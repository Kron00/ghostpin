"""Exercise the real movement threads and Flask API with a recording transport."""
import asyncio
import os
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

import app as app_module
import location_service
from location_service import LocationService


class RecordingSimulator:
    def __init__(self):
        self.writes = []
        self.clears = 0
        self.before_set = None

    async def set(self, lat, lon):
        if self.before_set:
            self.before_set()
        self.writes.append((lat, lon))

    async def clear(self):
        self.clears += 1


class InMemoryBridge:
    def run(self, coroutine, timeout=None):
        return asyncio.run(coroutine)


class MovementFlowTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        data_patch = patch.object(location_service, 'DATA_DIR', temporary.name)
        data_patch.start()
        self.addCleanup(data_patch.stop)
        self.simulator = RecordingSimulator()
        self.service = LocationService(self.simulator, InMemoryBridge())
        self.addCleanup(self.service.clear_location)
        for name, value in (('loc_svc', self.service), ('device_mgr', object()), ('_get_ip_location', lambda: None)):
            patcher = patch.object(app_module, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        self.client = app_module.app.test_client()

    def wait_for(self, predicate, timeout=6):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if predicate():
                return
            time.sleep(0.01)
        self.fail('Movement did not reach expected state within timeout')

    def start_route(self, length=0.00001):
        response = self.client.post('/api/route/start', json={
            'waypoints': [{'lat': 48.8584, 'lng': 2.2945}, {'lat': 48.8584 + length, 'lng': 2.2945}],
            'coordinates': [[2.2945, 48.8584], [2.2945, 48.8584 + length]],
            'speed': 5, 'mode': 'once', 'gps_noise': False,
        })
        self.assertEqual(response.status_code, 200, response.data)

    def test_route_runs_to_exact_endpoint_then_reset_clears_transport(self):
        self.start_route()
        self.wait_for(lambda: not self.service._route_active)
        status = self.client.get('/api/route/status').get_json()
        self.assertEqual(status['progress_pct'], 100)
        self.assertEqual(status['eta_seconds'], 0)
        self.assertGreater(len(self.simulator.writes), 2)
        self.assertAlmostEqual(self.service.current_location['lat'], 48.85841, places=7)
        self.assertAlmostEqual(self.service.current_location['lon'], 2.2945, places=7)
        self.assertEqual(self.client.post('/api/location/clear').status_code, 200)
        self.assertEqual(self.simulator.clears, 1)
        self.assertIsNone(self.service.current_location)
        self.assertFalse(self.service._keepalive_active)

    def test_pause_resume_speed_and_joystick_handoff(self):
        self.start_route(length=0.0003)
        self.wait_for(lambda: len(self.simulator.writes) >= 3)
        self.assertEqual(self.client.post('/api/route/pause').status_code, 200)
        time.sleep(0.2)  # Allow an in-flight emission to settle.
        paused = dict(self.service.current_location)
        time.sleep(0.25)
        self.assertEqual(self.service.current_location, paused)
        self.assertTrue(self.client.get('/api/route/status').get_json()['paused'])
        response = self.client.post('/api/movement/speed', json={'speed_kmh': 12})
        self.assertEqual(response.get_json()['active'], ['route'])
        self.assertEqual(self.client.post('/api/route/resume').status_code, 200)
        self.wait_for(lambda: self.service.current_location != paused)
        response = self.client.post('/api/joystick/move', json={'direction': 'e', 'speed': 5})
        self.assertEqual(response.status_code, 200)
        self.assertFalse(self.service._route_active)
        before = dict(self.service.current_location)
        self.wait_for(lambda: self.service.current_location['lon'] > before['lon'])
        self.assertEqual(self.client.post('/api/joystick/stop').status_code, 200)
        self.assertFalse(self.service._joystick_active)
        self.assertEqual(self.client.post('/api/location/clear').status_code, 200)
        self.assertIsNone(self.service.current_location)

    def test_reset_waits_for_an_inflight_joystick_write(self):
        entered, release = threading.Event(), threading.Event()
        self.service.current_location = {'lat': 48.8584, 'lon': 2.2945}

        def delay_write():
            entered.set()
            release.wait(timeout=2)

        self.simulator.before_set = delay_write
        self.service.joystick_start('n', 5)
        self.assertTrue(entered.wait(timeout=1))
        reset = threading.Thread(target=self.service.clear_location)
        reset.start()
        time.sleep(0.05)
        release.set()
        reset.join(timeout=3)
        self.assertFalse(reset.is_alive())
        self.assertIsNone(self.service.current_location)
        self.assertFalse(self.service._keepalive_active)
        self.assertEqual(self.simulator.clears, 1)


if __name__ == '__main__':
    unittest.main()
