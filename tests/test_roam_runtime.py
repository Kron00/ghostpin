"""Roam lookup and cancellation regressions, without a phone or live providers."""
import unittest
import threading
import time
from unittest.mock import Mock, patch

import app


class RoadLookupTests(unittest.TestCase):
    def test_stalled_primary_does_not_block_healthy_mirror(self):
        release = threading.Event()
        success = Mock()
        success.json.return_value = {'elements': [{'type': 'way'}]}

        def post(url, **kwargs):
            if url == app._OVERPASS_ENDPOINTS[0]:
                release.wait(timeout=2)
            return success

        try:
            with patch.object(app, '_overpass_last_good', None), patch.object(
                    app, '_OVERPASS_HEDGE_SECONDS', 0.02), patch.object(
                    app.http_requests, 'post', post):
                start = time.monotonic()
                self.assertEqual(app._overpass_elements('query'), [{'type': 'way'}])
                self.assertLess(time.monotonic() - start, 1)
                self.assertFalse(release.is_set())
        finally:
            release.set()

    def test_overpass_runtime_error_uses_next_mirror_and_remembers_it(self):
        partial = Mock()
        partial.json.return_value = {'remark': 'runtime error: Query timed out', 'elements': []}
        success = Mock()
        success.json.return_value = {'elements': [{'type': 'way'}]}
        with patch.object(app, '_overpass_last_good', None), patch.object(
                app.http_requests, 'post', side_effect=[partial, success, success]) as post:
            self.assertEqual(app._overpass_elements('query'), [{'type': 'way'}])
            self.assertEqual(app._overpass_elements('query'), [{'type': 'way'}])
            self.assertEqual(post.call_args_list[2].args[0], app._OVERPASS_ENDPOINTS[1])

    def test_empty_roads_and_network_failure_have_different_errors(self):
        client = app.app.test_client()
        data = {'lat': 48.8584, 'lon': 2.2945, 'radius': 1000}
        with patch.object(app, '_search_allowed', return_value=True):
            with patch.object(app, '_fetch_road_graph', return_value=None):
                response = client.post('/api/roam/route', json=data)
                self.assertEqual(response.status_code, 404)
                self.assertIn('larger radius', response.json['error'])
            with patch.object(app, '_fetch_road_graph', side_effect=app.http_requests.ConnectionError):
                self.assertEqual(client.post('/api/roam/route', json=data).status_code, 503)

    def test_invalid_radius_and_distance_do_not_call_provider(self):
        client = app.app.test_client()
        with patch.object(app, '_fetch_road_graph') as lookup:
            for key, value in [('radius', 0), ('radius', -1), ('radius', 'NaN'),
                               ('radius', 'Infinity'), ('radius', 50001), ('target_km', 'NaN')]:
                data = {'lat': 48.8584, 'lon': 2.2945, 'radius': 1000, key: value}
                self.assertEqual(client.post('/api/roam/route', json=data).status_code, 400)
            lookup.assert_not_called()
