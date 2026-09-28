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


class RoadCacheTests(unittest.TestCase):
    def setUp(self):
        import tempfile
        from pathlib import Path
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        for name, value in (
                ('_ROAM_DISK_CACHE_DIR', Path(temporary.name)),
                ('_ROAM_GRAPH_CACHE', {}), ('_ROAM_GRAPH_PENDING', {})):
            patcher = patch.object(app, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        self.key = (48.8584, 2.2945, 1000)
        # A small square is a useful bidirectional cyclic road network.
        points = [(2.294, 48.858), (2.295, 48.858),
                  (2.295, 48.859), (2.294, 48.859)]
        self.elements = [
            {'type': 'way', 'tags': {'highway': 'residential'},
             'nodes': [i, (i + 1) % 4],
             'geometry': [{'lon': x, 'lat': y}
                          for x, y in (points[i], points[(i + 1) % 4])]}
            for i in range(4)
        ]

    def test_relaunch_reuses_disk_roads_and_regenerates_graph_identity(self):
        with patch.object(app, '_overpass_elements', return_value=self.elements) as lookup:
            first = app._fetch_road_graph(*self.key)
            app._ROAM_GRAPH_CACHE.clear()  # Simulate a new app process.
            second = app._fetch_road_graph(*self.key)
        lookup.assert_called_once()
        self.assertEqual(first['allowed_steps'], second['allowed_steps'])
        self.assertEqual(first['junction_kinds'], second['junction_kinds'])
        self.assertNotEqual(first['identity'], second['identity'])

    def test_expired_corrupt_wrong_version_and_oversized_data_are_refetched(self):
        import gzip
        import json
        import os
        for damage in ('expired', 'corrupt', 'schema', 'nested', 'oversized', 'decoded'):
            with self.subTest(damage=damage):
                app._ROAM_GRAPH_CACHE.clear()
                app._save_road_elements(self.key, self.elements)
                path = app._road_cache_path(self.key)
                if damage == 'expired':
                    stale = time.time() - app._ROAM_GRAPH_CACHE_TTL - 1
                    os.utime(path, (stale, stale))
                elif damage == 'corrupt':
                    path.write_bytes(b'not gzip')
                elif damage == 'schema':
                    path.write_bytes(gzip.compress(json.dumps({
                        'version': -1, 'key': self.key, 'elements': self.elements,
                    }).encode()))
                elif damage == 'nested':
                    path.write_bytes(gzip.compress(json.dumps({
                        'version': app._ROAM_DISK_CACHE_VERSION, 'key': self.key,
                        'elements': [{'type': 'way', 'tags': [], 'geometry': [None]}],
                    }).encode()))
                limit = '_ROAM_DISK_MAX_BYTES'  if damage == 'oversized' else '_ROAM_DISK_MAX_DECODED_BYTES'
                with patch.object(app, limit, 1 if damage in ('oversized', 'decoded') else 1000000), patch.object(
                        app, '_overpass_elements', return_value=self.elements) as lookup:
                    self.assertIsNotNone(app._fetch_road_graph(*self.key))
                    lookup.assert_called_once()

    def test_cache_write_failure_does_not_block_route(self):
        with patch.object(app, '_overpass_elements', return_value=self.elements), patch.object(
                app.os, 'replace', side_effect=PermissionError):
            self.assertIsNotNone(app._fetch_road_graph(*self.key))
        self.assertEqual(list(app._ROAM_DISK_CACHE_DIR.iterdir()), [])

    def test_cache_is_bounded_and_failed_lookups_are_not_saved(self):
        for i in range(7):
            app._save_road_elements((48.8584, 2.2945, 1000 + i), self.elements)
        self.assertEqual(len(list(app._ROAM_DISK_CACHE_DIR.iterdir())), app._ROAM_GRAPH_CACHE_MAX)
        for empty in (None, []):
            with patch.object(app, '_overpass_elements', return_value=empty), patch.object(
                    app, '_save_road_elements') as save:
                try:
                    app._fetch_road_graph(48.8584, 2.2945, 2000)
                except app.http_requests.ConnectionError:
                    pass
                save.assert_not_called()

    def test_concurrent_starts_share_lookup_and_build(self):
        from concurrent.futures import ThreadPoolExecutor
        entered, release = threading.Event(), threading.Event()

        def fetch(query):
            entered.set()
            release.wait(2)
            return self.elements

        with patch.object(app, '_overpass_elements', side_effect=fetch) as lookup:
            with ThreadPoolExecutor(max_workers=2) as pool:
                first = pool.submit(app._fetch_road_graph, *self.key)
                self.assertTrue(entered.wait(1))
                second = pool.submit(app._fetch_road_graph, *self.key)
                release.set()
                self.assertIs(first.result(3), second.result(3))
            lookup.assert_called_once()

    def test_failed_inflight_lookup_can_be_retried(self):
        with patch.object(app, '_overpass_elements', side_effect=[None, self.elements]) as lookup:
            with self.assertRaises(app.http_requests.ConnectionError):
                app._fetch_road_graph(*self.key)
            self.assertEqual(app._ROAM_GRAPH_PENDING, {})
            self.assertIsNotNone(app._fetch_road_graph(*self.key))
            self.assertEqual(lookup.call_count, 2)

    def test_cusp_pruning_preserves_the_legal_loop_and_traffic_controls(self):
        self.elements[0]['tags']['oneway'] = 'yes'
        self.elements += [
            {'type': 'way', 'tags': {'highway': 'residential'},
             'nodes': [0, 4], 'geometry': [
                 {'lon': 2.294, 'lat': 48.858}, {'lon': 2.294, 'lat': 48.857}]},
            {'type': 'way', 'tags': {'highway': 'residential'},
             'nodes': [4, 5, 1], 'geometry': [
                 {'lon': 2.294, 'lat': 48.857}, {'lon': 2.294, 'lat': 48.8578},
                 {'lon': 2.295, 'lat': 48.858}]},
            {'type': 'node', 'id': 1, 'tags': {'highway': 'traffic_signals'}},
        ]
        with patch.object(app, '_overpass_elements', return_value=self.elements):
            graph = app._fetch_road_graph(*self.key)
        self.assertEqual({step[0] for step in graph['allowed_steps']}, {0, 1, 2, 3})
        self.assertIn((0, 0, 1), graph['allowed_steps'])
        self.assertNotIn((0, 1, 0), graph['allowed_steps'])
        self.assertEqual(graph['junction_kinds'], {1: 'signal'})

    def test_memory_cache_keeps_original_disk_expiry(self):
        import os
        app._save_road_elements(self.key, self.elements)
        now = time.time()
        almost_expired = now - app._ROAM_GRAPH_CACHE_TTL + 10
        os.utime(app._road_cache_path(self.key), (almost_expired, almost_expired))
        with patch.object(app, '_overpass_elements', return_value=self.elements) as lookup:
            first = app._fetch_road_graph(*self.key)
            lookup.assert_not_called()
            with patch.object(app.time, 'time', return_value=now + 11):
                second = app._fetch_road_graph(*self.key)
            lookup.assert_called_once()
        self.assertNotEqual(first['identity'], second['identity'])
