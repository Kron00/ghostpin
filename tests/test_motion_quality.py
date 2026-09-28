"""Physical motion and emission regressions, independent of network or a phone."""
import math
import random
import unittest
from unittest.mock import patch

from location_service import (LocationService, MAX_ACCEL_MS2, MAX_DECEL_MS2,
                              EMIT_MAX_HZ, GPS_NOISE_MAX_DRIFT_MS)


class MotionQualityTests(unittest.TestCase):
    def service(self, coordinates=None, speed=30, limits=None):
        service = LocationService(None, None)
        service._route_path = service._prepare_driven_path(coordinates or [
            [2.2945, 48.8584], [2.3145, 48.8584]])
        service._route_speed_target = speed
        service._route_speeds = limits
        return service

    def advance(self, service, plan, distance, speed, dt=.05):
        elapsed, next_speed = service._advance_route_motion(
            plan, distance, speed, dt, 1, plan['total_distance'])
        state = service._plan_state_at_time(plan, elapsed)
        self.assertGreaterEqual(state['distance'] + 1e-7, distance)
        self.assertLessEqual(next_speed - speed, MAX_ACCEL_MS2 * dt + 1e-7)
        if plan['total_distance'] - state['distance'] > .02:
            self.assertLessEqual(speed - next_speed, MAX_DECEL_MS2 * dt + 1e-7)
        return state['distance'], next_speed

    def test_speed_changes_preserve_position_and_ramp_in_both_directions(self):
        service = self.service(speed=30)
        plan = service._build_motion_plan()
        distance = speed = 0
        for _ in range(200):
            distance, speed = self.advance(service, plan, distance, speed)
        self.assertAlmostEqual(speed * 3.6, 30, places=3)
        for target in (60, 5, 40):
            service._route_speed_target = target
            plan = service._build_motion_plan()
            old_distance, old_speed = distance, speed
            distance, speed = self.advance(service, plan, distance, speed)
            self.assertLess(distance - old_distance, 1)
            self.assertLess(abs(speed - old_speed), .16)
            for _ in range(200):
                distance, speed = self.advance(service, plan, distance, speed)
            self.assertAlmostEqual(speed * 3.6, target, places=3)

    def test_short_route_finishes_exactly_without_creeping_forever(self):
        service = self.service([[2.2945, 48.8584], [2.2945, 48.85841]], speed=5)
        plan = service._build_motion_plan()
        distance = speed = 0
        for _ in range(100):
            distance, speed = self.advance(service, plan, distance, speed)
        self.assertEqual(distance, plan['total_distance'])
        self.assertEqual(service._plan_time_at_distance(plan, distance), plan['movement_duration'])
        self.assertEqual(speed, 0)

    def test_limits_and_corner_braking_constrain_the_driven_speed(self):
        service = self.service([[2.2945, 48.8584], [2.2985, 48.8584],
                                [2.2985, 48.8614]], speed=90, limits=[30, 20])
        plan = service._build_motion_plan()
        distance = speed = 0
        corner_speeds = []
        for _ in range(2500):
            distance, speed = self.advance(service, plan, distance, speed)
            state = service._plan_state_at_time(plan, service._plan_time_at_distance(plan, distance))
            self.assertLessEqual(speed, state['ceiling'] + .05)
            if 285 < distance < 300:
                corner_speeds.append(speed * 3.6)
            if distance == plan['total_distance']:
                break
        self.assertTrue(corner_speeds)
        self.assertLessEqual(max(corner_speeds), 20)
        self.assertLess(min(corner_speeds), 19)
        self.assertEqual(distance, plan['total_distance'])

    def test_one_latency_spike_does_not_permanently_halve_cadence(self):
        service = self.service()
        service._route_write_latencies = [.012] * 59 + [.4]
        service._route_write_attempts = 60
        service._update_route_emit_health()
        self.assertEqual(service._target_emit_hz(), EMIT_MAX_HZ)
        service._route_write_latencies = [.09] * 60
        service._route_write_attempts = 80
        service._update_route_emit_health()
        self.assertLess(service._target_emit_hz(), EMIT_MAX_HZ)
        service._route_write_latencies = [.012] * 60
        service._route_write_attempts = 160
        service._update_route_emit_health()
        self.assertEqual(service._target_emit_hz(), EMIT_MAX_HZ)

    def test_noise_drifts_continuously_instead_of_jumping_each_fix(self):
        service = self.service()
        rng = random.Random(7)
        old = (0, 0)
        with patch('location_service.random.gauss', rng.gauss):
            for _ in range(2000):
                service._advance_gps_noise(.05)
                new = service._route_noise_along, service._route_noise_across
                for before, after in zip(old, new):
                    self.assertLessEqual(abs(after - before), GPS_NOISE_MAX_DRIFT_MS * .05 + 1e-9)
                old = new
        self.assertGreater(math.hypot(*old), .001)

    def test_slow_and_failed_writes_never_create_catchup_jumps(self):
        service = self.service([[2.2945, 48.8584], [2.2995, 48.8584]])
        service._route_plans = {'forward': service._build_motion_plan()}
        service._route_active = True
        service._route_generation = 1
        service._route_speed_factor = 1
        service._route_gps_noise = False
        now = [1000.0]
        writes = []
        calls = [0]

        def write(lat, lon, timeout=None):
            calls[0] += 1
            now[0] += 2.0 if calls[0] in (100, 110, 111) else .012
            if calls[0] in (110, 111):
                raise TimeoutError('simulated slow transport')
            writes.append((now[0], lat, lon))

        with patch('location_service.time.monotonic', lambda: now[0]), \
                patch('location_service.time.sleep', lambda seconds: now.__setitem__(0, now[0] + seconds)), \
                patch.object(service, '_sim_set', write):
            self.assertTrue(service._drive_route_pass('forward', 1))
        steps = [service._haversine(a[1], a[2], b[1], b[2]) for a, b in zip(writes, writes[1:])]
        # Even after a two-second stall/failure the largest step is bounded by
        # 1.5 slow-cadence periods at 30 km/h, never by the missed wall time.
        self.assertLess(max(steps), 2.51)
        self.assertEqual(service._route_write_failures_total, 2)
        self.assertEqual(service._target_emit_hz(), EMIT_MAX_HZ)
        self.assertEqual(service._route_progress, 100)
        gaps = [b[0] - a[0] for a, b in zip(writes, writes[1:])]
        self.assertGreater(min(gaps), .025)  # no catch-up bursts

    def test_joystick_uses_elapsed_time_and_turns_without_velocity_snap(self):
        service = self.service()
        service.current_location = {'lat': 48.8584, 'lon': 2.2945}
        service._joystick_active = True
        service._joystick_direction = 'n'
        service._joystick_speed = 5
        now = [1000.0]
        writes = []

        def write(lat, lon, timeout=None):
            now[0] += .04  # Writing must not be added to a relative sleep.
            writes.append((now[0], lat, lon))
            if len(writes) == 100:
                service._joystick_direction = 'e'
            if len(writes) == 200:
                service._joystick_active = False

        with patch('location_service.time.monotonic', lambda: now[0]), \
                patch('location_service.time.sleep', lambda seconds: now.__setitem__(0, now[0] + seconds)), \
                patch.object(service, '_sim_set', write):
            service._joystick_loop()
        self.assertLess(writes[-1][0] - writes[0][0], 11)
        # The turn initially retains northward momentum while easing east.
        self.assertGreater(writes[100][1], writes[99][1])
        self.assertGreater(writes[100][2], writes[99][2])
        for a, b in zip(writes[30:90], writes[31:91]):
            measured = service._haversine(a[1], a[2], b[1], b[2]) / (b[0] - a[0]) * 3.6
            self.assertAlmostEqual(measured, 5, delta=.02)

    def test_signal_stop_is_exact_and_can_pull_away_again(self):
        coordinates = [[2.2945, 48.8584], [2.2955, 48.8584], [2.2965, 48.8584]]
        service = self.service(coordinates, speed=30)
        service._route_coordinates = coordinates
        service._route_holds = [{'id': 0, 'original_index': 1, 'kind': 'signal'}]
        service._map_route_holds()
        plan = service._build_motion_plan()
        stop = plan['holds'][0]['distance']
        distance = speed = 0
        for _ in range(1000):
            elapsed, speed = service._advance_route_motion(plan, distance, speed, .05, 1, stop)
            distance = service._plan_state_at_time(plan, elapsed)['distance']
            self.assertLessEqual(distance, stop + 1e-7)
            if abs(distance - stop) < 1e-7:
                break
        self.assertAlmostEqual(distance, stop)
        self.assertEqual(speed, 0)
        for _ in range(20):
            distance, speed = self.advance(service, plan, distance, speed)
        self.assertGreater(distance, stop + .5)
        self.assertGreater(speed, 1)

    def test_closed_loop_keeps_its_speed_across_the_seam(self):
        service = self.service([[2.2945, 48.8584], [2.2955, 48.8584],
                                [2.2955, 48.8594], [2.2945, 48.8594],
                                [2.2945, 48.8584]], speed=5)
        service._route_plans = {'cycle': service._build_motion_plan(cyclic=True)}
        service._route_active = True
        service._route_generation = 1
        service._route_speed_factor = 1
        service._route_gps_noise = False
        now = [1000.0]
        writes = []
        def write(lat, lon, timeout=None):
            now[0] += .012
            writes.append(service._route_speed_current)
        with patch('location_service.time.monotonic', lambda: now[0]), \
                patch('location_service.time.sleep', lambda seconds: now.__setitem__(0, now[0] + seconds)), \
                patch.object(service, '_sim_set', write):
            self.assertTrue(service._drive_route_pass('cycle', 1))
            self.assertAlmostEqual(service._route_speed_current, 5, places=3)
            seam = len(writes)
            self.assertTrue(service._drive_route_pass('cycle', 1))
        self.assertTrue(all(abs(v - 5) < .001 for v in writes[seam-5:seam+5]))

    def test_preview_uses_accepted_progress_at_repeated_intersections(self):
        service = self.service([[2.2945, 48.8584], [2.2985, 48.8584],
                                [2.2985, 48.8614], [2.2945, 48.8584],
                                [2.2905, 48.8584]], speed=30)
        plan = service._build_motion_plan()
        state = service._plan_state_at_time(plan, service._plan_time_at_distance(plan, 20))
        service._route_active = True
        service._route_generation = 1
        with patch.object(service, '_sim_set'):
            _, written = service._write_route_fix(state['lat'], state['lon'], 1, plan=plan, state=state)
        self.assertTrue(written)
        location = service.get_current(include_route=True)
        preview = location['route_preview']
        self.assertEqual(location['route_coordinate_index'], 0)
        self.assertEqual(preview[0], [location['lon'], location['lat']])
        self.assertGreater(preview[1][0], preview[0][0])  # still heading east
        distance = sum(service._haversine(a[1], a[0], b[1], b[0]) for a, b in zip(preview, preview[1:]))
        self.assertAlmostEqual(distance, 400, delta=.02)
        self.assertGreater(len(preview), 50)  # includes the rounded driven curve
        service._route_generation += 1
        self.assertNotIn('route_preview', service.get_current(include_route=True))


if __name__ == '__main__':
    unittest.main()
