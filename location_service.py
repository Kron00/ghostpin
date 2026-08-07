import bisect
import json
import math
import os
import platform
import random
import shutil
import threading
import time
import xml.etree.ElementTree as ET
from datetime import datetime

import requests as http_requests


# ── Cooldown table (Pokemon Go style) ─────────────────────────
# (distance_km, cooldown_seconds)
COOLDOWN_TABLE = [
    (1, 60), (5, 120), (10, 420), (25, 660), (30, 840),
    (65, 1320), (81, 1500), (100, 2100), (250, 2700),
    (500, 3600), (750, 4800), (1000, 7200),
]


MAX_ACCEL_MS2 = 2.0
MAX_DECEL_MS2 = 3.0
MAX_LATERAL_MS2 = 3.0
MIN_CORNER_KMH = 8.0
SPEED_JITTER_TAU_S = 8.0
SPEED_JITTER_SD = 0.07
SPEED_JITTER_CLAMP = (0.85, 1.15)
# These track-relative noise defaults are provisional: correlated error is
# well-supported, but its exact magnitude and anisotropy are device-dependent.
GPS_NOISE_TAU_S = 20.0
GPS_NOISE_SD_CROSS_M = 1.5
GPS_NOISE_SD_ALONG_M = 1.0

# Ten hertz is a display-oriented cap for a channel that cannot provide motion
# metadata. Wi-Fi pressure permanently selects the safer five-hertz schedule.
EMIT_MIN_HZ = 1.0
EMIT_MAX_HZ = 10.0
EMIT_FALLBACK_HZ = 5.0
EMIT_MIN_MOVEMENT_M = 0.02
EMIT_WRITE_P99_LIMIT_S = 0.080
EMIT_LOSS_LIMIT = 0.01
# A slow DVT write must make the route late, not make the next fix catch up by
# several seconds in one visible leap. At the fastest normal road profile this
# caps a recovery step at roughly 22 m before the small GPS-noise component.
MAX_ROUTE_ADVANCE_S = 0.75
DRIVEN_PATH_SPACING_M = 5.0
MAX_DRIVEN_PATH_POINTS = 25000

# Realistic driving sits a random amount over the posted limit — never under,
# because effectively no one does. One offset per trip, like a driver's habit.
OVER_LIMIT_MIN_MPH = 1.0
OVER_LIMIT_MAX_MPH = 15.0
MPH_TO_KMH = 1.609344


def _get_data_dir():
    """Get the user-writable data directory (platform-aware)."""
    if platform.system() == "Windows":
        base = os.environ.get("APPDATA", os.path.expanduser("~"))
        d = os.path.join(base, "Ghostpin")
        legacy = os.path.join(base, "iPhone Spoofer")
    else:
        d = os.path.join(os.path.expanduser("~"), "Library", "Application Support", "Ghostpin")
        legacy = os.path.join(
            os.path.expanduser("~"), "Library", "Application Support", "iPhone Spoofer"
        )
    os.makedirs(d, exist_ok=True)
    # Preserve a user's existing bookmarks, routes, profiles, and schedules
    # when moving from the MIT-licensed upstream app to Ghostpin.
    if os.path.isdir(legacy):
        for filename in (
            "saved_locations.json", "profiles.json", "schedules.json", "routes.json"
        ):
            source = os.path.join(legacy, filename)
            destination = os.path.join(d, filename)
            if os.path.isfile(source) and not os.path.exists(destination):
                shutil.copy2(source, destination)
    return d


DATA_DIR = _get_data_dir()
SAVED_FILE = os.path.join(DATA_DIR, "saved_locations.json")
PROFILES_FILE = os.path.join(DATA_DIR, "profiles.json")
SCHEDULES_FILE = os.path.join(DATA_DIR, "schedules.json")
ROUTES_FILE = os.path.join(DATA_DIR, "routes.json")


class LocationService:
    def __init__(self, simulator, bridge):
        self.simulator = simulator
        self.bridge = bridge
        self.current_location = None
        self._keepalive_active = False
        self._keepalive_thread = None
        self._keepalive_stop = None
        self._keepalive_lock = threading.Lock()
        self._simulation_lock = threading.RLock()
        self._file_lock = threading.Lock()

        # Route state
        self._route_active = False
        self._route_paused = False
        self._route_thread = None
        self._route_progress = 0
        self._route_distance = 0
        self._route_duration = 0
        self._route_speed_target = 0
        self._route_speed_current = 0
        self._route_speeds = None
        self._route_over_limit_kmh = 0.0
        self._route_posted_limit_kmh = None
        self._route_holds = []
        self._route_holding = False
        self._route_hold_remaining = 0
        self._route_error = None
        self._route_coordinates = None
        self._route_mode = "once"       # "once", "loop", "pingpong"
        self._speed_randomize = False
        self._route_gps_noise = True
        self._route_emit_max_hz = EMIT_MAX_HZ
        self._route_emit_target_hz = EMIT_MAX_HZ
        self._route_emit_degraded = False
        self._route_emit_hz = 0
        self._route_emit_times = []
        self._route_write_latencies = []
        self._route_write_failures = 0
        self._route_write_attempts = 0
        self._route_write_failures_total = 0
        self._route_emit_deadlines = 0
        self._route_emit_coalesced = 0
        self._route_remaining = 0
        self._route_eta = 0
        self._route_path = None
        self._route_plans = {}
        self._route_plan_version = 0
        self._route_plan_lock = threading.RLock()
        self._route_state_lock = threading.RLock()
        self._route_generation = 0
        self._route_pass_key = "forward"
        self._route_pass_distance = 0

        # Joystick state
        self._joystick_active = False
        self._joystick_thread = None
        self._joystick_direction = None
        self._joystick_speed = 5

        # Cooldown state
        self._last_teleport_time = None
        self._last_teleport_coords = None
        self._cooldown_end = 0
        self._last_known_position = None

        # Random wander state
        self._wander_active = False
        self._wander_thread = None
        self._wander_center = None
        self._wander_radius = 0
        self._wander_speed = 5

    # ── Core ───────────────────────────────────────────────

    def _sim_set(self, lat, lon, timeout=30):
        # DTX channels are stateful and are not safe for concurrent writes from
        # keep-alive, joystick, route, and schedule threads.
        with self._simulation_lock:
            self.bridge.run(self.simulator.set(lat, lon), timeout=timeout)

    def _sim_clear(self):
        with self._simulation_lock:
            self.bridge.run(self.simulator.clear())

    def set_location(self, lat, lon):
        # Cooldown is about how far the phone appears to have travelled, which
        # is just as true of the first jump after a reset as of any other. Use
        # the last position we knew, which outlives clear_location.
        reference = self.current_location or self._last_known_position
        if reference:
            dist = self._haversine(reference["lat"], reference["lon"], lat, lon) / 1000
            self._cooldown_end = time.time() + self._calculate_cooldown(dist)
        self._last_teleport_time = time.time()
        self._last_teleport_coords = {"lat": lat, "lon": lon}

        self._stop_keepalive()
        self._sim_set(lat, lon)
        self.current_location = {"lat": lat, "lon": lon}
        self._last_known_position = {"lat": lat, "lon": lon}
        self._start_keepalive()
        return {"status": "Location set", "lat": lat, "lon": lon}

    def clear_location(self):
        self._stop_keepalive()
        # Deliberately keep _last_known_position: the next jump is still a jump
        # from where the phone was last pretending to be.
        self.current_location = None
        self._cooldown_end = 0
        self._last_teleport_time = None
        self._last_teleport_coords = None
        self.stop_route()
        self.joystick_stop()
        self.stop_wander()
        try:
            self._sim_clear()
        except Exception:
            pass
        return {"status": "Location cleared"}

    def get_current(self):
        return self.current_location

    # ── Cooldown ───────────────────────────────────────────

    @staticmethod
    def _calculate_cooldown(distance_km):
        """Return cooldown seconds for a given teleport distance."""
        if distance_km <= 0:
            return 0
        for max_dist, secs in COOLDOWN_TABLE:
            if distance_km <= max_dist:
                return secs
        return 7200  # 2 hours for 1000+ km

    def get_cooldown(self):
        """Return cooldown state."""
        now = time.time()
        remaining = max(0, self._cooldown_end - now)
        return {
            "active": remaining > 0,
            "remaining_seconds": round(remaining),
            "total_seconds": max(0, round(self._cooldown_end - (self._last_teleport_time or now))) if self._last_teleport_time else 0,
            "last_teleport": self._last_teleport_time,
        }

    # ── Keep-alive ─────────────────────────────────────────

    def _start_keepalive(self):
        with self._keepalive_lock:
            if (
                self._keepalive_thread
                and self._keepalive_thread.is_alive()
                and self._keepalive_stop
                and not self._keepalive_stop.is_set()
            ):
                return
            stop_event = threading.Event()
            self._keepalive_active = True
            self._keepalive_stop = stop_event
            self._keepalive_thread = threading.Thread(
                target=self._keepalive_loop, args=(stop_event,), daemon=True
            )
            self._keepalive_thread.start()

    def _stop_keepalive(self):
        with self._keepalive_lock:
            self._keepalive_active = False
            t = self._keepalive_thread
            stop_event = self._keepalive_stop
            if stop_event:
                stop_event.set()
        if t and t.is_alive():
            t.join(timeout=3)
        with self._keepalive_lock:
            if self._keepalive_thread is t:
                self._keepalive_thread = None
                self._keepalive_stop = None

    def _keepalive_loop(self, stop_event):
        fail_count = 0
        while not stop_event.is_set():
            try:
                if self.current_location:
                    lat = self.current_location["lat"]
                    lon = self.current_location["lon"]
                    # Reassert the exact pin. Adding artificial jitter here makes
                    # a stationary spoof visibly bounce in Maps, and on newer iOS
                    # can look like competing real/simulated location sources.
                    self._sim_set(lat, lon)
                    fail_count = 0
            except Exception as exc:
                fail_count += 1
                if fail_count in {3, 10}:
                    print(f"[!] Keep-alive retry {fail_count}: {exc}")
            # Reassert the current coordinate once per second so newer iOS
            # versions do not discard idle simulation, and keep retrying transient
            # channel failures instead of silently abandoning the location.
            stop_event.wait(1.0)

    # ── Joystick / WASD movement ───────────────────────────

    # Direction vectors (lat_delta, lon_delta) normalized
    _DIRECTIONS = {
        "n":  (1, 0), "s":  (-1, 0), "e":  (0, 1), "w":  (0, -1),
        "ne": (1, 1), "nw": (1, -1), "se": (-1, 1), "sw": (-1, -1),
    }

    def joystick_start(self, direction, speed_kmh=5):
        """Start continuous movement in a direction."""
        if direction not in self._DIRECTIONS:
            raise ValueError(f"Invalid direction: {direction}")
        if not self.current_location:
            raise ValueError("No location set. Set a location first.")

        self._joystick_direction = direction
        self._joystick_speed = speed_kmh

        if not self._joystick_active:
            self._stop_keepalive()
            self._joystick_active = True
            self._joystick_thread = threading.Thread(target=self._joystick_loop, daemon=True)
            self._joystick_thread.start()

        return {"status": "Moving", "direction": direction, "speed": speed_kmh}

    def joystick_stop(self):
        """Stop joystick movement."""
        self._joystick_active = False
        if self._joystick_thread and self._joystick_thread.is_alive():
            self._joystick_thread.join(timeout=2)
        self._joystick_thread = None
        if self.current_location:
            self._start_keepalive()
        return {"status": "Stopped"}

    def _joystick_loop(self):
        TICK = 0.2  # 200ms per tick
        while self._joystick_active and self.current_location:
            d = self._DIRECTIONS.get(self._joystick_direction, (0, 0))
            lat = self.current_location["lat"]
            lon = self.current_location["lon"]

            # Convert speed to degrees per tick
            speed_deg = self._joystick_speed / (111.32 * 3600) * TICK
            # Normalize diagonal so it's not faster
            mag = math.sqrt(d[0] ** 2 + d[1] ** 2) or 1
            dlat = d[0] / mag * speed_deg
            # Longitude correction for latitude
            dlon = d[1] / mag * speed_deg / max(math.cos(math.radians(lat)), 0.01)

            new_lat = max(-90, min(90, lat + dlat))
            new_lon = max(-180, min(180, lon + dlon))

            try:
                self._sim_set(new_lat, new_lon)
                self.current_location = {"lat": new_lat, "lon": new_lon}
            except Exception:
                pass
            time.sleep(TICK)

    # ── Movement / Routes ──────────────────────────────────

    def set_movement_speed(self, speed_kmh):
        """Update every movement engine so an active simulation changes speed immediately."""
        speed = float(speed_kmh)
        if not 1 <= speed <= 300:
            raise ValueError("Speed must be between 1 and 300 km/h")

        with self._route_plan_lock:
            self._route_speed_target = speed
            if self._route_path:
                self._rebuild_route_plans_locked()
        self._joystick_speed = speed
        self._wander_speed = speed

        active = []
        if self._route_active:
            active.append("route")
        if self._joystick_active:
            active.append("joystick")
        if self._wander_active:
            active.append("wander")
        return {"status": "Speed updated", "speed_kmh": speed, "active": active}

    def start_route(self, waypoints, speed_kmh=5, mode="once", randomize_speed=False,
                    coordinates=None, provider="osrm", speeds=None, holds=None,
                    gps_noise=True, emit_max_hz=EMIT_MAX_HZ):
        with self._route_state_lock:
            if self._route_active:
                raise ValueError("A route is already running. Stop it first.")
        if len(waypoints) < 2:
            raise ValueError("Need at least 2 waypoints.")
        if mode not in ("once", "loop", "pingpong"):
            mode = "once"

        speed = float(speed_kmh)
        if not 1 <= speed <= 300:
            raise ValueError("Speed must be between 1 and 300 km/h")
        emit_cap = float(emit_max_hz)
        if not EMIT_MIN_HZ <= emit_cap <= EMIT_MAX_HZ:
            raise ValueError("Emission rate cap must be between 1 and 10 Hz")

        if coordinates is not None:
            if not isinstance(coordinates, list) or not (2 <= len(coordinates) <= 20000):
                raise ValueError("Calculated route geometry is invalid")
            normalized = []
            for point in coordinates:
                if not isinstance(point, (list, tuple)) or len(point) < 2:
                    raise ValueError("Calculated route geometry is invalid")
                lon, lat = float(point[0]), float(point[1])
                if not (-90 <= lat <= 90 and -180 <= lon <= 180):
                    raise ValueError("Calculated route geometry is invalid")
                normalized.append([lon, lat])
            coordinates = normalized
        else:
            coords_str = ";".join(f"{wp['lng']},{wp['lat']}" for wp in waypoints)
            url = f"https://router.project-osrm.org/route/v1/driving/{coords_str}?geometries=geojson&overview=full"

            resp = http_requests.get(url, timeout=15)
            data = resp.json()

            if data.get("code") != "Ok" or not data.get("routes"):
                raise ValueError(f"Routing failed: {data.get('message', 'No route found')}")

            route = data["routes"][0]
            coordinates = route["geometry"]["coordinates"]

        profile = None
        if speeds is not None:
            if len(speeds) != len(coordinates) - 1:
                raise ValueError("Speed profile must contain one speed per route segment")
            profile = [float(value) for value in speeds]
            if any(value <= 0 for value in profile):
                raise ValueError("Speed profile values must be positive")

        normalized_holds = []
        for hold_id, item in enumerate(holds or []):
            if not isinstance(item, (list, tuple)) or len(item) != 2:
                raise ValueError("Route holds are invalid")
            coordinate_index, kind = item
            if (not isinstance(coordinate_index, int)
                    or not 0 <= coordinate_index < len(coordinates)
                    or kind not in ("signal", "stop", "waypoint")):
                raise ValueError("Route holds are invalid")
            normalized_holds.append({
                "id": hold_id,
                "original_index": coordinate_index,
                "kind": kind,
            })

        route_path = self._prepare_driven_path(coordinates)
        if route_path["cumulative"][-1] < 0.001:
            raise ValueError("Route geometry has no movement")
        self._map_route_holds(route_path, coordinates, normalized_holds)
        route_closed_loop = (
            mode == "loop"
            and self._haversine(
                coordinates[0][1], coordinates[0][0],
                coordinates[-1][1], coordinates[-1][0],
            ) <= 50
        )
        self.joystick_stop()
        self.stop_wander()
        # The stop helpers restore keep-alive for an idle location. Stop it
        # after both helpers so route updates are the only DVT writes while a
        # route is active.
        self._stop_keepalive()

        with self._route_state_lock:
            if self._route_active:
                raise ValueError("A route is already running. Stop it first.")
            self._route_generation += 1
            generation = self._route_generation
            self._route_active = True
            try:
                self._route_coordinates = coordinates
                self._route_speed_target = speed
                self._route_speed_current = 0
                # One target speed per segment when the caller worked out a
                # profile from posted limits; otherwise one speed governs all.
                self._route_speeds = profile
                # Realistic mode drives a random 1-15 mph over every posted
                # limit, fixed for the trip. Flat mode has no profile and no
                # offset.
                self._route_over_limit_kmh = (
                    random.uniform(OVER_LIMIT_MIN_MPH, OVER_LIMIT_MAX_MPH) * MPH_TO_KMH
                    if profile else 0.0
                )
                self._route_posted_limit_kmh = profile[0] if profile else None
                self._route_holds = normalized_holds
                self._route_error = None
                self._route_mode = mode
                self._route_provider = provider
                self._speed_randomize = randomize_speed
                self._route_gps_noise = bool(gps_noise)
                self._route_emit_max_hz = emit_cap
                self._route_emit_target_hz = emit_cap
                self._route_emit_degraded = False
                self._route_emit_hz = 0
                self._route_emit_times = []
                self._route_write_latencies = []
                self._route_write_failures = 0
                self._route_write_attempts = 0
                self._route_write_failures_total = 0
                self._route_emit_deadlines = 0
                self._route_emit_coalesced = 0
                self._route_holding = False
                self._route_hold_remaining = 0
                self._route_eta = 0
                self._route_path = route_path
                self._route_closed_loop = route_closed_loop
                self._route_pass_key = "cycle" if route_closed_loop else "forward"
                self._route_pass_distance = 0
                self._route_paused = False
                self._route_progress = 0
                with self._route_plan_lock:
                    self._rebuild_route_plans_locked()
                    self._route_eta = self._route_duration
                self._route_thread = threading.Thread(
                    target=self._route_loop, args=(generation,), daemon=True
                )
                self._route_thread.start()
            except Exception:
                self._route_active = False
                self._route_generation += 1
                self._route_thread = None
                raise

        return {
            "status": "Route started",
            "distance_km": round(self._route_distance / 1000, 2),
            "total_points": len(coordinates),
            "coordinates": coordinates,
            "mode": mode,
            "provider": provider,
        }

    def _prepare_driven_path(self, coordinates):
        points = []
        origins = []

        def append_point(point, origin):
            if points and self._haversine(
                    points[-1][1], points[-1][0], point[1], point[0]) < 0.001:
                return
            points.append([point[0], point[1]])
            origins.append(origin)

        append_point(coordinates[0], 0)
        for index in range(1, len(coordinates) - 1):
            previous = coordinates[index - 1]
            corner = coordinates[index]
            following = coordinates[index + 1]
            incoming = self._vector_metres(previous, corner)
            outgoing = self._vector_metres(corner, following)
            incoming_length = math.hypot(*incoming)
            outgoing_length = math.hypot(*outgoing)
            if incoming_length < 0.001 or outgoing_length < 0.001:
                append_point(corner, index)
                continue
            cosine = max(-1.0, min(1.0, (
                incoming[0] * outgoing[0] + incoming[1] * outgoing[1]
            ) / (incoming_length * outgoing_length)))
            turn_degrees = math.degrees(math.acos(cosine))
            if turn_degrees <= 15:
                append_point(corner, index)
                continue

            cutback = min(0.25 * incoming_length, 0.25 * outgoing_length, 12.0)
            before_fraction = 1 - cutback / incoming_length
            after_fraction = cutback / outgoing_length
            before = self._interpolate_coordinate(
                previous, corner, before_fraction
            )
            after = self._interpolate_coordinate(
                corner, following, after_fraction
            )
            append_point(before, index - 1)
            # A handful of points is enough to make the heading rotate instead
            # of snap without exploding long, nearly straight road geometry.
            before_lon = corner[0] + self._longitude_delta(corner[0], before[0])
            after_lon = corner[0] + self._longitude_delta(corner[0], after[0])
            for fraction in (0.25, 0.5, 0.75, 1.0):
                inverse = 1 - fraction
                rounded = [
                    self._wrap_longitude(
                    inverse * inverse * before_lon
                    + 2 * inverse * fraction * corner[0]
                    + fraction * fraction * after_lon),
                    inverse * inverse * before[1]
                    + 2 * inverse * fraction * corner[1]
                    + fraction * fraction * after[1],
                ]
                append_point(rounded, index)
        append_point(coordinates[-1], len(coordinates) - 1)

        base_distances = [
            self._haversine(
                points[index][1], points[index][0],
                points[index + 1][1], points[index + 1][0],
            )
            for index in range(len(points) - 1)
        ]
        total_distance = sum(base_distances)
        insertion_budget = max(0, MAX_DRIVEN_PATH_POINTS - len(points))
        spacing = DRIVEN_PATH_SPACING_M
        if insertion_budget:
            spacing = max(spacing, total_distance / insertion_budget)
        # Sparse routes get five-metre samples, while very long routes scale
        # the spacing up so densification alone can never exceed this budget.
        dense_points = [points[0]]
        dense_origins = [origins[0]]
        segment_origins = []
        for index, distance in enumerate(base_distances):
            subdivisions = (
                max(1, math.ceil(distance / spacing))
                if insertion_budget else 1
            )
            for step in range(1, subdivisions + 1):
                fraction = step / subdivisions
                dense_points.append(self._interpolate_coordinate(
                    points[index], points[index + 1], fraction
                ))
                dense_origins.append(
                    origins[index + 1]
                    if step == subdivisions else origins[index]
                )
                # Every inserted edge remains on the same original segment as
                # the undensified edge, preserving posted limits and holds.
                segment_origins.append(origins[index])

        cumulative = [0.0]
        for index in range(len(dense_points) - 1):
            cumulative.append(cumulative[-1] + self._haversine(
                dense_points[index][1], dense_points[index][0],
                dense_points[index + 1][1], dense_points[index + 1][0],
            ))
        return {
            "points": dense_points,
            "origins": dense_origins,
            "segment_origins": segment_origins,
            "cumulative": cumulative,
        }

    def _map_route_holds(self, path=None, coordinates=None, holds=None):
        path = path or self._route_path
        coordinates = coordinates or self._route_coordinates
        holds = self._route_holds if holds is None else holds
        points = path["points"]
        origins = path["origins"]
        for hold in holds:
            raw = coordinates[hold["original_index"]]
            candidates = [
                index for index, origin in enumerate(origins)
                if origin == hold["original_index"]
            ]
            if not candidates:
                candidates = range(len(points))
            hold["base_index"] = min(candidates, key=lambda index: self._haversine(
                raw[1], raw[0], points[index][1], points[index][0]
            ))

    @staticmethod
    def _wrap_longitude(longitude):
        return (longitude + 180) % 360 - 180

    @classmethod
    def _longitude_delta(cls, start, end):
        return cls._wrap_longitude(end - start)

    @classmethod
    def _interpolate_coordinate(cls, start, end, fraction):
        return [
            cls._wrap_longitude(
                start[0] + cls._longitude_delta(start[0], end[0]) * fraction
            ),
            start[1] + (end[1] - start[1]) * fraction,
        ]

    @classmethod
    def _vector_metres(cls, start, end):
        middle_latitude = math.radians((start[1] + end[1]) / 2)
        return (
            cls._longitude_delta(start[0], end[0])
            * 111320 * max(math.cos(middle_latitude), 0.01),
            (end[1] - start[1]) * 111320,
        )

    def _curvature_radius(self, previous, point, following):
        a = self._haversine(point[1], point[0], following[1], following[0])
        b = self._haversine(previous[1], previous[0], following[1], following[0])
        c = self._haversine(previous[1], previous[0], point[1], point[0])
        first = self._vector_metres(point, previous)
        second = self._vector_metres(point, following)
        twice_area = abs(first[0] * second[1] - first[1] * second[0])
        if twice_area < 0.001 and first[0] * second[0] + first[1] * second[1] > 0:
            # Collinearity alone cannot distinguish a straight road from
            # arriving and departing along the same ray. The latter must stop.
            return 0.0
        if twice_area < 0.001 or min(a, b, c) < 0.001:
            return math.inf
        return a * b * c / (2 * twice_area)

    def _adaptive_ceiling(self, origin):
        if not self._route_speeds:
            return math.inf
        index = min(max(int(origin), 0), len(self._route_speeds) - 1)
        # Sit the trip's fixed amount over the posted limit — never under.
        return (self._route_speeds[index] + self._route_over_limit_kmh) / 3.6

    @staticmethod
    def _expected_dwell(kind):
        if kind == "signal":
            return 10.0
        if kind == "stop":
            return 2.0
        return 112.5

    @staticmethod
    def _sample_dwell(kind):
        if kind == "signal":
            return 0.0 if random.random() < 0.6 else random.uniform(5, 45)
        if kind == "stop":
            return random.uniform(1, 3)
        return random.uniform(45, 180)

    def _rebuild_route_plans_locked(self):
        if not self._route_path:
            return
        if getattr(self, "_route_closed_loop", False):
            self._route_plans = {
                "cycle": self._build_motion_plan(reverse=False, cyclic=True),
            }
            primary = self._route_plans["cycle"]
        else:
            self._route_plans = {
                "forward": self._build_motion_plan(reverse=False, cyclic=False),
                "reverse": self._build_motion_plan(reverse=True, cyclic=False),
            }
            primary = self._route_plans.get(self._route_pass_key,
                                             self._route_plans["forward"])
        self._route_distance = primary["total_distance"]
        self._route_duration = primary["planned_duration"]
        self._route_remaining = max(
            0.0, self._route_distance - min(self._route_pass_distance, self._route_distance)
        )
        self._route_plan_version += 1

    def _build_motion_plan(self, reverse=False, cyclic=False):
        base_points = self._route_path["points"]
        base_origins = self._route_path["origins"]
        base_segment_origins = self._route_path["segment_origins"]
        entries = [
            (point, base_origins[index], index)
            for index, point in enumerate(base_points)
        ]
        if reverse:
            entries.reverse()
            segment_origins = list(reversed(base_segment_origins))
        else:
            segment_origins = list(base_segment_origins)

        dropped_base_index = None
        if cyclic and len(entries) > 1 and self._haversine(
                entries[0][0][1], entries[0][0][0],
                entries[-1][0][1], entries[-1][0][0]) < 0.001:
            dropped_base_index = entries[-1][2]
            entries.pop()

        points = [entry[0] for entry in entries]
        origins = [entry[1] for entry in entries]
        base_indices = [entry[2] for entry in entries]
        if not points:
            points = [base_points[0]]
            origins = [base_origins[0]]
            base_indices = [0]
        if cyclic and len(segment_origins) < len(points):
            segment_origins.append(origins[-1])

        hold_points = {}
        for hold in self._route_holds:
            base_index = hold["base_index"]
            if base_index == dropped_base_index:
                point_index = 0
            else:
                try:
                    point_index = base_indices.index(base_index)
                except ValueError:
                    continue
            hold_points.setdefault(point_index, []).append(hold)

        target = self._route_speed_target / 3.6
        # Adaptive means "drive the road": the posted limit governs each tagged
        # segment, and the slider is folded into the profile only as the fallback
        # where no limit is posted. It must NOT also cap the tagged segments —
        # min(slider, limit) made a low slider crawl a 45 road at the slider
        # speed. Without adaptive, the slider is the flat speed everywhere.
        if self._route_speeds:
            segment_caps = [self._adaptive_ceiling(origin) for origin in segment_origins]
        else:
            segment_caps = [target for _ in segment_origins]
        curvature_caps = []
        ceilings = []
        for index, point in enumerate(points):
            if cyclic and len(points) > 2:
                previous = points[(index - 1) % len(points)]
                following = points[(index + 1) % len(points)]
                radius = self._curvature_radius(previous, point, following)
            elif 0 < index < len(points) - 1:
                radius = self._curvature_radius(points[index - 1], point,
                                                points[index + 1])
            else:
                radius = math.inf
            if radius <= 0:
                curvature = 0.0
            else:
                curvature = (math.inf if math.isinf(radius) else max(
                    MIN_CORNER_KMH / 3.6, math.sqrt(MAX_LATERAL_MS2 * radius)
                ))
            if cyclic and segment_caps:
                profile_cap = min(segment_caps[index - 1], segment_caps[index])
            elif not segment_caps:
                profile_cap = target
            elif index == 0:
                profile_cap = segment_caps[0]
            elif index == len(points) - 1:
                profile_cap = segment_caps[-1]
            else:
                profile_cap = min(segment_caps[index - 1], segment_caps[index])
            curvature_caps.append(curvature)
            ceilings.append(min(profile_cap, curvature))
            if index in hold_points:
                ceilings[-1] = 0.0

        if cyclic and len(points) > 1:
            distances = [
                self._haversine(
                    points[index][1], points[index][0],
                    points[(index + 1) % len(points)][1],
                    points[(index + 1) % len(points)][0],
                )
                for index in range(len(points))
            ]
            velocities = list(ceilings)
            # Constraints need to propagate across the array boundary as well
            # as through it; two full wraps cover either side of every stop.
            for _ in range(3):
                for index, distance in enumerate(distances):
                    following = (index + 1) % len(points)
                    velocities[following] = min(
                        velocities[following],
                        math.sqrt(max(0.0, velocities[index] ** 2
                                      + 2 * MAX_ACCEL_MS2 * distance)),
                    )
                for index in range(len(points) - 1, -1, -1):
                    previous = (index - 1) % len(points)
                    distance = distances[previous]
                    velocities[previous] = min(
                        velocities[previous],
                        math.sqrt(max(0.0, velocities[index] ** 2
                                      + 2 * MAX_DECEL_MS2 * distance)),
                    )
            points = points + [points[0]]
            origins = origins + [origins[0]]
            base_indices = base_indices + [base_indices[0]]
            ceilings = ceilings + [ceilings[0]]
            curvature_caps = curvature_caps + [curvature_caps[0]]
            velocities = velocities + [velocities[0]]
        else:
            distances = [
                self._haversine(
                    points[index][1], points[index][0],
                    points[index + 1][1], points[index + 1][0],
                )
                for index in range(len(points) - 1)
            ]
            velocities = [0.0] * len(points)
            for index in range(1, len(points)):
                velocities[index] = min(
                    ceilings[index],
                    math.sqrt(max(0.0, velocities[index - 1] ** 2
                                  + 2 * MAX_ACCEL_MS2 * distances[index - 1])),
                )
            if velocities:
                velocities[-1] = 0.0
            for index in range(len(points) - 2, -1, -1):
                velocities[index] = min(
                    velocities[index],
                    math.sqrt(max(0.0, velocities[index + 1] ** 2
                                  + 2 * MAX_DECEL_MS2 * distances[index])),
                )

        cumulative_distance = [0.0]
        cumulative_time = [0.0]
        segments = []
        for index, distance in enumerate(distances):
            segment = self._build_motion_segment(
                distance, velocities[index], velocities[index + 1],
                segment_caps[index],
            )
            segments.append(segment)
            cumulative_distance.append(cumulative_distance[-1] + distance)
            cumulative_time.append(cumulative_time[-1] + segment["duration"])

        planned_holds = []
        for point_index, holds in hold_points.items():
            planned_index = point_index
            if cyclic and point_index == 0:
                planned_index = len(points) - 1
            for hold in holds:
                planned_holds.append({
                    "id": hold["id"],
                    "kind": hold["kind"],
                    "point_index": planned_index,
                    "distance": cumulative_distance[planned_index],
                    "time": cumulative_time[planned_index],
                    "expected": self._expected_dwell(hold["kind"]),
                })
        planned_holds.sort(key=lambda hold: (hold["time"], hold["id"]))
        expected_dwell = sum(hold["expected"] for hold in planned_holds)
        tangents = self._path_tangents(points, cyclic)
        return {
            "points": points,
            "tangents": tangents,
            "origins": origins,
            "base_indices": base_indices,
            "ceilings": ceilings,
            "segment_caps": segment_caps,
            "curvature_caps": curvature_caps,
            "velocities": velocities,
            "distances": distances,
            "cumulative_distance": cumulative_distance,
            "cumulative_time": cumulative_time,
            "segments": segments,
            "holds": planned_holds,
            "cyclic": cyclic,
            "total_distance": cumulative_distance[-1],
            "movement_duration": cumulative_time[-1],
            "planned_duration": cumulative_time[-1] + expected_dwell,
        }

    def _path_tangents(self, points, cyclic):
        if len(points) < 2:
            return [(0.0, 1.0)]
        tangents = []
        final_unique = len(points) - 1 if cyclic else len(points)
        for index in range(final_unique):
            if cyclic:
                previous = points[(index - 1) % final_unique]
                following = points[(index + 1) % final_unique]
            elif index == 0:
                previous = points[0]
                following = points[1]
            elif index == len(points) - 1:
                previous = points[-2]
                following = points[-1]
            else:
                previous = points[index - 1]
                following = points[index + 1]
            tangent = self._vector_metres(previous, following)
            magnitude = math.hypot(*tangent) or 1.0
            tangents.append((tangent[0] / magnitude, tangent[1] / magnitude))
        if cyclic:
            tangents.append(tangents[0])
        return tangents

    def _build_motion_segment(self, distance, start_speed, end_speed,
                              segment_cap):
        if distance <= 0:
            return {"kind": "linear", "duration": 0.0, "distance": 0.0,
                    "start_speed": start_speed, "end_speed": end_speed,
                    "acceleration": 0.0, "ceiling": 0.0}

        if start_speed + end_speed > 0.000001:
            duration = 2 * distance / (start_speed + end_speed)
            return {
                "kind": "linear",
                "duration": duration,
                "distance": distance,
                "start_speed": start_speed,
                "end_speed": end_speed,
                "acceleration": (end_speed - start_speed) / duration,
                "ceiling": segment_cap,
            }

        # Two endpoints (or two adjacent stops) contain no sample at which the
        # prescribed two-pass envelope can express a non-zero velocity. A
        # bounded triangular/trapezoidal pulse is the physical zero-zero guard.
        peak = segment_cap
        acceleration_distance = peak ** 2 / (2 * MAX_ACCEL_MS2)
        deceleration_distance = peak ** 2 / (2 * MAX_DECEL_MS2)
        if acceleration_distance + deceleration_distance > distance:
            peak = math.sqrt(
                2 * distance / (1 / MAX_ACCEL_MS2 + 1 / MAX_DECEL_MS2)
            )
            acceleration_distance = peak ** 2 / (2 * MAX_ACCEL_MS2)
            deceleration_distance = distance - acceleration_distance
        cruise_distance = max(0.0, distance - acceleration_distance
                              - deceleration_distance)
        phases = []
        phase_time = 0.0
        phase_distance = 0.0

        def add_phase(duration, speed, acceleration):
            nonlocal phase_time, phase_distance
            if duration <= 0:
                return
            phases.append({
                "start_time": phase_time,
                "start_distance": phase_distance,
                "duration": duration,
                "start_speed": speed,
                "acceleration": acceleration,
            })
            phase_distance += speed * duration + 0.5 * acceleration * duration ** 2
            phase_time += duration

        add_phase(peak / MAX_ACCEL_MS2, 0.0, MAX_ACCEL_MS2)
        add_phase(cruise_distance / max(peak, 0.000001), peak, 0.0)
        add_phase(peak / MAX_DECEL_MS2, peak, -MAX_DECEL_MS2)
        return {
            "kind": "phases",
            "duration": phase_time,
            "distance": distance,
            "start_speed": 0.0,
            "end_speed": 0.0,
            "ceiling": segment_cap,
            "phases": phases,
        }

    @staticmethod
    def _motion_segment_state(segment, elapsed):
        elapsed = max(0.0, min(elapsed, segment["duration"]))
        if segment["kind"] == "linear":
            speed = max(0.0, segment["start_speed"]
                        + segment["acceleration"] * elapsed)
            distance = (segment["start_speed"] * elapsed
                        + 0.5 * segment["acceleration"] * elapsed ** 2)
            return min(segment["distance"], max(0.0, distance)), speed

        phase = segment["phases"][-1]
        for candidate in segment["phases"]:
            if elapsed <= candidate["start_time"] + candidate["duration"]:
                phase = candidate
                break
        local_time = min(phase["duration"], max(0.0, elapsed - phase["start_time"]))
        speed = max(0.0, phase["start_speed"]
                    + phase["acceleration"] * local_time)
        distance = (phase["start_distance"] + phase["start_speed"] * local_time
                    + 0.5 * phase["acceleration"] * local_time ** 2)
        return min(segment["distance"], max(0.0, distance)), speed

    @staticmethod
    def _motion_segment_time(segment, distance):
        distance = max(0.0, min(distance, segment["distance"]))
        if distance <= 0 or segment["duration"] <= 0:
            return 0.0
        if segment["kind"] == "linear":
            acceleration = segment["acceleration"]
            start_speed = segment["start_speed"]
            if abs(acceleration) < 0.000001:
                return distance / max(start_speed, 0.000001)
            discriminant = max(0.0, start_speed ** 2 + 2 * acceleration * distance)
            return max(0.0, min(segment["duration"],
                                (math.sqrt(discriminant) - start_speed) / acceleration))

        for phase in segment["phases"]:
            phase_end = (phase["start_distance"]
                         + phase["start_speed"] * phase["duration"]
                         + 0.5 * phase["acceleration"] * phase["duration"] ** 2)
            if distance <= phase_end + 0.000001:
                local_distance = max(0.0, distance - phase["start_distance"])
                acceleration = phase["acceleration"]
                if abs(acceleration) < 0.000001:
                    local_time = local_distance / max(phase["start_speed"], 0.000001)
                else:
                    discriminant = max(
                        0.0, phase["start_speed"] ** 2
                        + 2 * acceleration * local_distance,
                    )
                    local_time = ((math.sqrt(discriminant) - phase["start_speed"])
                                  / acceleration)
                return phase["start_time"] + max(
                    0.0, min(phase["duration"], local_time)
                )
        return segment["duration"]

    def _plan_state_at_time(self, plan, elapsed):
        elapsed = max(0.0, min(elapsed, plan["movement_duration"]))
        if not plan["segments"]:
            lon, lat = plan["points"][0]
            return {"lat": lat, "lon": lon, "speed": 0.0, "distance": 0.0,
                    "ceiling": 0.0, "tangent": (0.0, 1.0), "segment": 0}
        index = min(
            len(plan["segments"]) - 1,
            max(0, bisect.bisect_right(plan["cumulative_time"], elapsed) - 1),
        )
        local_time = elapsed - plan["cumulative_time"][index]
        local_distance, speed = self._motion_segment_state(
            plan["segments"][index], local_time
        )
        segment_distance = plan["distances"][index]
        fraction = min(1.0, local_distance / segment_distance) if segment_distance else 1.0
        start = plan["points"][index]
        end = plan["points"][index + 1]
        lon = self._wrap_longitude(
            start[0] + self._longitude_delta(start[0], end[0]) * fraction
        )
        lat = start[1] + (end[1] - start[1]) * fraction
        start_tangent = plan["tangents"][index]
        end_tangent = plan["tangents"][index + 1]
        tangent = (
            start_tangent[0] + (end_tangent[0] - start_tangent[0]) * fraction,
            start_tangent[1] + (end_tangent[1] - start_tangent[1]) * fraction,
        )
        magnitude = math.hypot(*tangent) or 1.0
        tangent = (tangent[0] / magnitude, tangent[1] / magnitude)
        return {
            "lat": lat,
            "lon": lon,
            "speed": speed,
            "distance": plan["cumulative_distance"][index] + local_distance,
            "ceiling": plan["segment_caps"][index],
            "tangent": tangent,
            "segment": index,
        }

    def _plan_time_at_distance(self, plan, distance):
        distance = max(0.0, min(distance, plan["total_distance"]))
        if not plan["segments"]:
            return 0.0
        index = min(
            len(plan["segments"]) - 1,
            max(0, bisect.bisect_right(plan["cumulative_distance"], distance) - 1),
        )
        local_distance = distance - plan["cumulative_distance"][index]
        return (plan["cumulative_time"][index]
                + self._motion_segment_time(plan["segments"][index], local_distance))

    def _record_route_emit(self, emitted_at):
        self._route_emit_times.append(emitted_at)
        self._route_emit_times = self._route_emit_times[-40:]
        if len(self._route_emit_times) > 1:
            span = self._route_emit_times[-1] - self._route_emit_times[0]
            if span > 0:
                self._route_emit_hz = ((len(self._route_emit_times) - 1) / span)

    def _route_write_p99(self):
        if not self._route_write_latencies:
            return 0.0
        ordered = sorted(self._route_write_latencies)
        index = max(0, math.ceil(0.99 * len(ordered)) - 1)
        return ordered[index]

    def _update_route_emit_health(self):
        if self._route_emit_degraded:
            return
        failure_rate = (
            self._route_write_failures_total / self._route_write_attempts
            if self._route_write_attempts else 0.0
        )
        coalesced_rate = (
            self._route_emit_coalesced / self._route_emit_deadlines
            if self._route_emit_deadlines else 0.0
        )
        if (self._route_write_p99() > EMIT_WRITE_P99_LIMIT_S
                or failure_rate > EMIT_LOSS_LIMIT
                or coalesced_rate > EMIT_LOSS_LIMIT):
            # A sticky fallback avoids repeatedly pushing a marginal Wi-Fi
            # channel back into the condition that made it miss updates.
            self._route_emit_degraded = True
            self._route_emit_target_hz = min(
                self._route_emit_max_hz, EMIT_FALLBACK_HZ
            )

    def _record_route_coalesced(self, count):
        if count <= 0:
            return
        self._route_emit_deadlines += count
        self._route_emit_coalesced += count
        self._update_route_emit_health()

    def _write_route_fix(self, lat, lon, generation, record=True):
        # A route that has been stopped or superseded must never write, even if
        # its thread was parked waiting for the simulation lock when that
        # happened. The generation is re-checked under the lock so a stale fix
        # cannot land on top of a later teleport, clear, or newer route.
        if generation != self._route_generation:
            return 0.0, False
        started = time.monotonic()
        self._route_write_attempts += 1
        try:
            with self._simulation_lock:
                if generation != self._route_generation:
                    return time.monotonic() - started, False
                self._sim_set(lat, lon, timeout=6)
            finished = time.monotonic()
            latency = finished - started
            self._route_write_latencies.append(latency)
            self._route_write_latencies = self._route_write_latencies[-100:]
            self.current_location = {"lat": lat, "lon": lon}
            self._route_write_failures = 0
            if record:
                self._record_route_emit(finished)
            self._update_route_emit_health()
            return latency, True
        except Exception as exc:
            latency = time.monotonic() - started
            self._route_write_latencies.append(latency)
            self._route_write_latencies = self._route_write_latencies[-100:]
            self._route_write_failures += 1
            self._route_write_failures_total += 1
            self._update_route_emit_health()
            # Eight consecutive failures still mean the phone is really gone,
            # even after selecting the more forgiving channel cadence.
            if self._route_write_failures >= 8:
                self._route_error = (
                    "Lost contact with the iPhone — route stopped. "
                    "Check it is plugged in and unlocked."
                )
                print(f"[!] Route stopped: {exc}")
                self._route_active = False
            return latency, False

    def _target_emit_hz(self):
        return max(EMIT_MIN_HZ, self._route_emit_target_hz)

    def _wait_for_route_deadline(self, deadline):
        while self._route_active:
            if self._route_paused:
                return False
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return True
            time.sleep(min(0.05, remaining))
        return False

    def _next_route_deadline(self, start, index, rate, finished):
        nominal_index = index + 1
        future_index = max(
            nominal_index,
            math.floor(max(0.0, finished - start) * rate) + 1,
        )
        self._record_route_coalesced(future_index - nominal_index)
        target_rate = self._target_emit_hz()
        if target_rate != rate:
            # Changing the period establishes a new absolute schedule at the
            # completed write; the old faster deadlines are no longer owed.
            return finished, 1, target_rate
        return start, future_index, rate

    def _advance_gps_noise(self, elapsed):
        if elapsed <= 0:
            return
        decay = math.exp(-elapsed / GPS_NOISE_TAU_S)
        innovation = math.sqrt(max(0.0, 1 - decay ** 2))
        self._route_noise_along = (
            decay * self._route_noise_along
            + GPS_NOISE_SD_ALONG_M * innovation * random.gauss(0, 1)
        )
        self._route_noise_across = (
            decay * self._route_noise_across
            + GPS_NOISE_SD_CROSS_M * innovation * random.gauss(0, 1)
        )

    def _route_coordinate(self, plan, state, elapsed, stop_distance):
        self._advance_gps_noise(elapsed)
        if not self._route_gps_noise or state["speed"] <= 0.01:
            self._route_noise_last_station = state["distance"]
            return state["lat"], state["lon"]

        taper = min(1.0, state["speed"] / 2.0)
        along = self._route_noise_along * taper
        across = self._route_noise_across * taper
        station = max(0.0, state["distance"] + along)
        if self._route_noise_last_station is not None:
            station = max(self._route_noise_last_station, station)
        station = min(stop_distance, station)
        self._route_noise_last_station = station

        station_time = self._plan_time_at_distance(plan, station)
        station_state = self._plan_state_at_time(plan, station_time)
        east, north = station_state["tangent"]
        # Very large cross-track excursions can put a fix on the wrong arm of
        # a tight bend. Saturating smoothly still leaves metre-scale GNSS error
        # while keeping its offset curve single-valued through rounded corners.
        across = 3.5 * math.tanh(across / 3.5)
        east_offset = -north * across
        north_offset = east * across
        lat = station_state["lat"] + north_offset / 111320
        lon = station_state["lon"] + east_offset / (
            111320 * max(math.cos(math.radians(station_state["lat"])), 0.01)
        )
        return lat, lon

    def _future_hold(self, plan, consumed, elapsed):
        for hold in plan["holds"]:
            if hold["id"] not in consumed and hold["time"] >= elapsed - 0.000001:
                return hold
        return None

    def _update_route_plan_status(self, plan, state, elapsed, consumed,
                                  hold_remaining=0):
        expected_holds = sum(
            hold["expected"] for hold in plan["holds"]
            if hold["id"] not in consumed and hold["time"] > elapsed + 0.000001
        )
        self._route_pass_distance = state["distance"]
        self._route_progress = (100.0 if plan["total_distance"] <= 0 else min(
            100.0, state["distance"] / plan["total_distance"] * 100
        ))
        self._route_remaining = max(0.0, plan["total_distance"] - state["distance"])
        self._route_eta = max(
            0.0, plan["movement_duration"] - elapsed
            + expected_holds + hold_remaining
        )

    def _route_dwell(self, state, duration, plan=None, elapsed=0, consumed=None):
        self._route_holding = True
        self._route_speed_current = 0
        self._route_hold_remaining = duration
        if duration > 0:
            self._route_emit_times = []
            self._route_emit_hz = 0
        remaining = duration
        last_tick = time.monotonic()
        while remaining > 0 and self._route_active:
            if self._route_paused:
                time.sleep(0.05)
                last_tick = time.monotonic()
                continue
            now = time.monotonic()
            step = max(0.0, now - last_tick)
            last_tick = now
            remaining = max(0.0, remaining - step)
            self._route_hold_remaining = remaining
            self._advance_gps_noise(step)
            if plan is not None:
                self._update_route_plan_status(
                    plan, state, elapsed, consumed or set(), remaining
                )
            else:
                self._route_eta = remaining
            time.sleep(min(0.05, remaining))
        self._route_hold_remaining = 0
        self._route_holding = False

    def _drive_route_pass(self, plan_key, generation):
        with self._route_plan_lock:
            plan = self._route_plans[plan_key]
            version = self._route_plan_version
            self._route_duration = plan["planned_duration"]
            self._route_distance = plan["total_distance"]
        self._route_pass_key = plan_key
        self._route_pass_distance = 0
        self._route_progress = 0
        self._route_remaining = plan["total_distance"]
        consumed = set()
        elapsed = 0.0
        last_wall = time.monotonic()
        schedule_start = last_wall
        deadline_index = 0
        scheduled_rate = self._target_emit_hz()
        self._route_noise_last_station = None
        last_emitted_distance = None
        initial_state = self._plan_state_at_time(plan, 0)
        if self.current_location and self._haversine(
                self.current_location["lat"], self.current_location["lon"],
                initial_state["lat"], initial_state["lon"]) < EMIT_MIN_MOVEMENT_M:
            last_emitted_distance = 0.0

        while self._route_active and generation == self._route_generation:
            if self._route_paused:
                while self._route_paused and self._route_active:
                    self._route_speed_current = 0
                    time.sleep(0.05)
                resumed = time.monotonic()
                last_wall = resumed
                schedule_start = resumed
                deadline_index = 0
                scheduled_rate = self._target_emit_hz()
            if not self._route_active:
                return False

            with self._route_plan_lock:
                if version != self._route_plan_version:
                    distance = self._route_pass_distance
                    plan = self._route_plans[plan_key]
                    elapsed = self._plan_time_at_distance(plan, distance)
                    version = self._route_plan_version
                    self._route_duration = plan["planned_duration"]
                    self._route_distance = plan["total_distance"]
                    replanned = time.monotonic()
                    last_wall = replanned
                    schedule_start = replanned
                    deadline_index = 0
                    scheduled_rate = self._target_emit_hz()

            current_state = self._plan_state_at_time(plan, elapsed)
            next_hold = self._future_hold(plan, consumed, elapsed)
            if next_hold and next_hold["time"] <= elapsed + 0.000001:
                elapsed = next_hold["time"]
                current_state = self._plan_state_at_time(plan, elapsed)
                current_state["speed"] = 0.0
                consumed.add(next_hold["id"])
                self._route_speed_current = 0
                self._update_route_plan_status(plan, current_state, elapsed, consumed)
                _, written = self._write_route_fix(
                    current_state["lat"], current_state["lon"], generation
                )
                if written:
                    last_emitted_distance = current_state["distance"]
                dwell = self._sample_dwell(next_hold["kind"])
                self._route_dwell(current_state, dwell, plan, elapsed, consumed)
                resumed = time.monotonic()
                last_wall = resumed
                schedule_start = resumed
                deadline_index = 0
                scheduled_rate = self._target_emit_hz()
                continue

            deadline = schedule_start + deadline_index / scheduled_rate
            if not self._wait_for_route_deadline(deadline):
                continue
            now = time.monotonic()
            self._route_emit_deadlines += 1
            wall_elapsed = min(MAX_ROUTE_ADVANCE_S, max(0.0, now - last_wall))
            last_wall = now
            if self._speed_randomize:
                self._route_speed_factor += (
                    (1 - self._route_speed_factor) * wall_elapsed / SPEED_JITTER_TAU_S
                    + SPEED_JITTER_SD
                    * math.sqrt(2 * wall_elapsed / SPEED_JITTER_TAU_S)
                    * random.gauss(0, 1)
                )
                self._route_speed_factor = max(
                    SPEED_JITTER_CLAMP[0],
                    min(SPEED_JITTER_CLAMP[1], self._route_speed_factor),
                )
            else:
                self._route_speed_factor = 1.0

            factor = self._route_speed_factor
            if self._route_speeds:
                # Realistic never eases below its posted-limit-plus-offset cruise
                # through jitter; only a corner or a stop (both in the plan) may.
                factor = max(1.0, factor)
            if current_state["speed"] > 0.001:
                factor = min(
                    factor,
                    current_state["ceiling"] / current_state["speed"],
                )
            candidate = min(
                plan["movement_duration"], elapsed + wall_elapsed * max(0.0, factor)
            )
            next_hold = self._future_hold(plan, consumed, elapsed)
            reached_hold = bool(next_hold and candidate >= next_hold["time"])
            if reached_hold:
                candidate = next_hold["time"]
            elapsed = candidate
            state = self._plan_state_at_time(plan, elapsed)
            if reached_hold:
                state["speed"] = 0.0
            if self._route_speeds and plan["segment_caps"]:
                segment_index = min(state["segment"], len(plan["segment_caps"]) - 1)
                self._route_posted_limit_kmh = max(
                    0.0,
                    plan["segment_caps"][segment_index] * 3.6
                    - self._route_over_limit_kmh,
                )
            actual_speed = min(state["ceiling"], state["speed"] * factor)
            self._route_speed_current = max(0.0, actual_speed * 3.6)
            at_end = elapsed >= plan["movement_duration"]

            stop_distance = plan["total_distance"]
            if next_hold:
                stop_distance = next_hold["distance"]
            if reached_hold or at_end:
                lat, lon = state["lat"], state["lon"]
            else:
                noisy_state = dict(state)
                noisy_state["speed"] = actual_speed
                lat, lon = self._route_coordinate(
                    plan, noisy_state, wall_elapsed, stop_distance
                )
            self._update_route_plan_status(plan, state, elapsed, consumed)
            moved = (last_emitted_distance is None
                     or state["distance"] - last_emitted_distance
                     >= EMIT_MIN_MOVEMENT_M)
            written = False
            if reached_hold or at_end or moved:
                _, written = self._write_route_fix(lat, lon, generation)
                if written:
                    last_emitted_distance = state["distance"]

            if reached_hold:
                consumed.add(next_hold["id"])
                self._route_speed_current = 0
                dwell = self._sample_dwell(next_hold["kind"])
                self._route_dwell(state, dwell, plan, elapsed, consumed)
                resumed = time.monotonic()
                last_wall = resumed
                schedule_start = resumed
                deadline_index = 0
                scheduled_rate = self._target_emit_hz()
                continue
            if at_end:
                return True
            schedule_start, deadline_index, scheduled_rate = (
                self._next_route_deadline(
                    schedule_start, deadline_index, scheduled_rate,
                    time.monotonic(),
                )
            )
        return False

    def _route_loop(self, generation):
        self._route_speed_factor = 1.0
        self._route_noise_along = random.gauss(0, GPS_NOISE_SD_ALONG_M)
        self._route_noise_across = random.gauss(0, GPS_NOISE_SD_CROSS_M)
        first_key = "cycle" if getattr(self, "_route_closed_loop", False) else "forward"

        plan_key = first_key
        finished_once = False
        while self._route_active and generation == self._route_generation:
            completed = self._drive_route_pass(plan_key, generation)
            if not completed or not self._route_active:
                break
            if self._route_mode == "once":
                finished_once = True
                break
            if getattr(self, "_route_closed_loop", False):
                continue

            with self._route_plan_lock:
                plan = self._route_plans[plan_key]
                endpoint = self._plan_state_at_time(plan, plan["movement_duration"])
            self._route_speed_current = 0
            self._route_dwell(endpoint, self._sample_dwell("waypoint"))
            plan_key = "reverse" if plan_key == "forward" else "forward"

        if finished_once:
            self._route_progress = 100
            self._route_remaining = 0
            self._route_eta = 0
        self._route_speed_current = 0
        self._route_holding = False
        self._route_hold_remaining = 0
        self._route_active = False
        if self.current_location:
            self._start_keepalive()

    def pause_route(self):
        if not self._route_active:
            return {"status": "No route running"}
        self._route_paused = True
        return {"status": "Route paused"}

    def resume_route(self):
        if not self._route_active:
            return {"status": "No route running"}
        self._route_paused = False
        return {"status": "Route resumed"}

    def stop_route(self):
        # Bump the generation before joining so a thread parked inside the
        # write path refuses to emit once it wakes, even if the join times out
        # and we stop waiting for it.
        with self._route_state_lock:
            self._route_active = False
            self._route_generation += 1
        self._route_paused = False
        self._route_speed_current = 0
        self._route_holding = False
        self._route_hold_remaining = 0
        if self._route_thread and self._route_thread.is_alive():
            self._route_thread.join(timeout=7)
        self._route_thread = None
        if self.current_location and not (self._keepalive_thread and self._keepalive_thread.is_alive()):
            self._start_keepalive()
        return {"status": "Route stopped"}

    def get_route_status(self):
        if self._route_coordinates is None:
            return None
        return {
            "active": self._route_active,
            "paused": self._route_paused,
            "progress_pct": round(self._route_progress, 1),
            "distance_km": round(self._route_distance / 1000, 2),
            "duration_min": round(self._route_duration / 60, 1),
            "speed_kmh": round(self._route_speed_current, 2),
            "target_speed_kmh": self._route_speed_target,
            "remaining_km": round(self._route_remaining / 1000, 3),
            "eta_seconds": int(round(max(0.0, self._route_eta))),
            "holding": self._route_holding,
            "adaptive": bool(self._route_speeds),
            "posted_limit_kmh": (
                round(self._route_posted_limit_kmh, 2)
                if self._route_posted_limit_kmh is not None else None
            ),
            "over_limit_mph": (
                round(self._route_over_limit_kmh / MPH_TO_KMH, 2)
                if self._route_speeds else None
            ),
            "cruise_target_kmh": (
                round(self._route_posted_limit_kmh + self._route_over_limit_kmh, 2)
                if self._route_posted_limit_kmh is not None else None
            ),
            "emit_hz": round(self._route_emit_hz, 2),
            "error": self._route_error,
            "mode": self._route_mode,
            "provider": getattr(self, "_route_provider", "osrm"),
        }

    # ── Circular route generator ───────────────────────────

    def generate_circular_route(self, center_lat, center_lon, radius_m, points=36):
        """Generate waypoints in a circle. Returns list of {lat, lng}."""
        waypoints = []
        for i in range(points + 1):  # +1 to close the circle
            angle = 2 * math.pi * i / points
            dlat = (radius_m / 111320) * math.cos(angle)
            dlon = (radius_m / (111320 * math.cos(math.radians(center_lat)))) * math.sin(angle)
            waypoints.append({
                "lat": center_lat + dlat,
                "lng": center_lon + dlon,
            })
        return waypoints

    # ── Random wander ──────────────────────────────────────

    def start_wander(self, lat, lon, radius_m, speed_kmh=5):
        if self._wander_active:
            raise ValueError("Wander is already running.")
        if not self.current_location:
            self.set_location(lat, lon)

        self._wander_center = {"lat": lat, "lon": lon}
        self._wander_radius = radius_m
        self._wander_speed = speed_kmh
        self._wander_active = True
        self.joystick_stop()
        self._stop_keepalive()

        self._wander_thread = threading.Thread(target=self._wander_loop, daemon=True)
        self._wander_thread.start()

        return {"status": "Wandering", "center": self._wander_center, "radius": radius_m}

    def stop_wander(self):
        self._wander_active = False
        if self._wander_thread and self._wander_thread.is_alive():
            self._wander_thread.join(timeout=5)
        self._wander_thread = None
        if self.current_location:
            self._start_keepalive()
        return {"status": "Wander stopped"}

    def _wander_loop(self):
        while self._wander_active and self.current_location:
            # Pick a random point within radius
            angle = random.uniform(0, 2 * math.pi)
            dist = random.uniform(0, self._wander_radius)
            c = self._wander_center
            dlat = (dist / 111320) * math.cos(angle)
            dlon = (dist / (111320 * math.cos(math.radians(c["lat"])))) * math.sin(angle)
            target_lat = c["lat"] + dlat
            target_lon = c["lon"] + dlon

            # Walk to the target in 300ms steps. Read the shared speed on every
            # step so edits in the UI take effect without restarting wander.
            while self._wander_active and self.current_location:
                cur = self.current_location
                remaining = self._haversine(
                    cur["lat"], cur["lon"], target_lat, target_lon
                )
                if remaining <= 0.05:
                    break
                speed_ms = max(float(self._wander_speed) / 3.6, 0.01)
                step_distance = min(remaining, speed_ms * 0.3)
                frac = step_distance / remaining
                nlat = cur["lat"] + (target_lat - cur["lat"]) * frac
                nlon = cur["lon"] + (target_lon - cur["lon"]) * frac
                try:
                    self._sim_set(nlat, nlon)
                    self.current_location = {"lat": nlat, "lon": nlon}
                except Exception:
                    pass
                time.sleep(0.3)

            # Pause briefly at the destination
            time.sleep(random.uniform(1, 4))

    def get_wander_status(self):
        return {
            "active": self._wander_active,
            "center": self._wander_center,
            "radius": self._wander_radius,
            "speed_kmh": self._wander_speed,
        }

    # ── GPX import / export ────────────────────────────────

    # A GPX file is untrusted input. ElementTree will not fetch external
    # entities, but it does expand internal ones, so a small file can still
    # balloon into gigabytes of memory on import.
    MAX_GPX_BYTES = 16 * 1024 * 1024
    MAX_GPX_WAYPOINTS = 100_000

    def import_gpx(self, gpx_content):
        """Parse GPX XML and return waypoints [{lat, lng, name?}]."""
        size = len(gpx_content.encode("utf-8", "ignore")
                   if isinstance(gpx_content, str) else gpx_content)
        if size > self.MAX_GPX_BYTES:
            raise ValueError(
                f"GPX file is too large ({size // (1024 * 1024)} MB); "
                f"the limit is {self.MAX_GPX_BYTES // (1024 * 1024)} MB"
            )

        probe = gpx_content[:4096]
        if isinstance(probe, bytes):
            probe = probe.decode("utf-8", "ignore")
        if "<!DOCTYPE" in probe.upper() or "<!ENTITY" in probe.upper():
            raise ValueError("GPX file declares a DOCTYPE or entity, which is not allowed")

        root = ET.fromstring(gpx_content)
        ns = {"gpx": "http://www.topografix.com/GPX/1/1"}
        waypoints = []

        # Try track points first
        for trkpt in root.findall(".//gpx:trkpt", ns) or root.findall(".//{http://www.topografix.com/GPX/1/0}trkpt"):
            lat = float(trkpt.get("lat"))
            lon = float(trkpt.get("lon"))
            waypoints.append({"lat": lat, "lng": lon})

        # Fallback: route points
        if not waypoints:
            for rtept in root.findall(".//gpx:rtept", ns) or root.findall(".//{http://www.topografix.com/GPX/1/0}rtept"):
                lat = float(rtept.get("lat"))
                lon = float(rtept.get("lon"))
                waypoints.append({"lat": lat, "lng": lon})

        # Fallback: waypoints (wpt)
        if not waypoints:
            for wpt in root.findall(".//gpx:wpt", ns) or root.findall(".//{http://www.topografix.com/GPX/1/0}wpt"):
                lat = float(wpt.get("lat"))
                lon = float(wpt.get("lon"))
                # An Element with no children is falsy, so `or` would discard a
                # perfectly good <name> here. Test for None explicitly.
                name_el = wpt.find("gpx:name", ns)
                if name_el is None:
                    name_el = wpt.find("{http://www.topografix.com/GPX/1/0}name")
                name = name_el.text if name_el is not None else None
                waypoints.append({"lat": lat, "lng": lon, "name": name})

        # Try without namespace (common in simple GPX files)
        if not waypoints:
            for tag in ("trkpt", "rtept", "wpt"):
                for el in root.iter(tag):
                    lat = float(el.get("lat"))
                    lon = float(el.get("lon"))
                    waypoints.append({"lat": lat, "lng": lon})
                if waypoints:
                    break

        if not waypoints:
            raise ValueError("No waypoints found in GPX file")

        if len(waypoints) > self.MAX_GPX_WAYPOINTS:
            waypoints = waypoints[:self.MAX_GPX_WAYPOINTS]

        return {"waypoints": waypoints, "count": len(waypoints)}

    def export_gpx(self, name="Ghostpin Route"):
        """Export current route coordinates or saved locations as GPX."""
        gpx = ET.Element("gpx", version="1.1", creator="Ghostpin",
                         xmlns="http://www.topografix.com/GPX/1/1")

        if self._route_coordinates:
            trk = ET.SubElement(gpx, "trk")
            ET.SubElement(trk, "name").text = name
            trkseg = ET.SubElement(trk, "trkseg")
            for lng, lat in self._route_coordinates:
                ET.SubElement(trkseg, "trkpt", lat=str(lat), lon=str(lng))
        elif self.current_location:
            wpt = ET.SubElement(gpx, "wpt",
                                lat=str(self.current_location["lat"]),
                                lon=str(self.current_location["lon"]))
            ET.SubElement(wpt, "name").text = "Current Location"

        return ET.tostring(gpx, encoding="unicode", xml_declaration=True)

    # ── Profiles ───────────────────────────────────────────

    def get_profiles(self):
        if not os.path.exists(PROFILES_FILE):
            return []
        try:
            with open(PROFILES_FILE, "r") as f:
                return json.load(f)
        except (json.JSONDecodeError, IOError):
            return []

    def save_profile(self, name, data=None):
        """Save current state as a named profile."""
        profile = {
            "name": name,
            "lat": data.get("lat") if data else (self.current_location or {}).get("lat"),
            "lon": data.get("lon") if data else (self.current_location or {}).get("lon"),
            "speed": (data.get("speed", self._route_speed_target)
                      if data else self._route_speed_target),
            "route_mode": data.get("route_mode", self._route_mode) if data else self._route_mode,
            "created": datetime.now().isoformat(),
        }
        with self._file_lock:
            profiles = self.get_profiles()
            # Update existing or append
            for i, p in enumerate(profiles):
                if p["name"] == name:
                    profiles[i] = profile
                    break
            else:
                profiles.append(profile)
            with open(PROFILES_FILE, "w") as f:
                json.dump(profiles, f, indent=2)
        return {"status": f"Profile '{name}' saved", "profile": profile}

    def load_profile(self, name):
        """Load a named profile and apply it."""
        profiles = self.get_profiles()
        for p in profiles:
            if p["name"] == name:
                if p.get("lat") is not None and p.get("lon") is not None:
                    self.set_location(p["lat"], p["lon"])
                return {"status": f"Profile '{name}' loaded", "profile": p}
        raise ValueError(f"Profile '{name}' not found")

    def delete_profile(self, name):
        with self._file_lock:
            profiles = [p for p in self.get_profiles() if p["name"] != name]
            with open(PROFILES_FILE, "w") as f:
                json.dump(profiles, f, indent=2)
        return {"status": f"Profile '{name}' deleted"}

    # ── Schedules ──────────────────────────────────────────

    def get_schedules(self):
        if not os.path.exists(SCHEDULES_FILE):
            return []
        try:
            with open(SCHEDULES_FILE, "r") as f:
                return json.load(f)
        except (json.JSONDecodeError, IOError):
            return []

    def save_schedule(self, name, lat, lon, time_str, days=None):
        """Save a scheduled location. time_str is 'HH:MM', days is list like ['mon','tue']."""
        schedule = {
            "id": str(int(time.time() * 1000)),
            "name": name,
            "lat": lat, "lon": lon,
            "time": time_str,
            "days": days or ["mon", "tue", "wed", "thu", "fri", "sat", "sun"],
            "enabled": True,
        }
        with self._file_lock:
            schedules = self.get_schedules()
            schedules.append(schedule)
            with open(SCHEDULES_FILE, "w") as f:
                json.dump(schedules, f, indent=2)
        return {"status": "Schedule created", "schedule": schedule}

    def delete_schedule(self, schedule_id):
        with self._file_lock:
            schedules = [s for s in self.get_schedules() if s["id"] != schedule_id]
            with open(SCHEDULES_FILE, "w") as f:
                json.dump(schedules, f, indent=2)
        return {"status": "Schedule deleted"}

    def toggle_schedule(self, schedule_id, enabled):
        with self._file_lock:
            schedules = self.get_schedules()
            for s in schedules:
                if s["id"] == schedule_id:
                    s["enabled"] = enabled
                    break
            with open(SCHEDULES_FILE, "w") as f:
                json.dump(schedules, f, indent=2)
        return {"status": "Schedule updated"}

    def check_schedules(self):
        """Check if any schedule should fire now. Called periodically."""
        now = datetime.now()
        current_time = now.strftime("%H:%M")
        day_name = now.strftime("%a").lower()

        for s in self.get_schedules():
            if not s.get("enabled", True):
                continue
            days = s.get("days") or []
            # No selected weekdays means daily. The UI allows this state and
            # previously displayed the schedule even though it could never run.
            if s["time"] == current_time and (not days or day_name in days):
                try:
                    self.set_location(s["lat"], s["lon"])
                    print(f"[*] Schedule fired: {s['name']} -> {s['lat']}, {s['lon']}")
                except Exception as e:
                    print(f"[!] Schedule error: {e}")

    # ── Saved locations ────────────────────────────────────

    def get_saved(self):
        if not os.path.exists(SAVED_FILE):
            return []
        try:
            with open(SAVED_FILE, "r") as f:
                data = json.load(f)
        except (json.JSONDecodeError, IOError):
            return []
        # Ensure all items have a category field
        for loc in data:
            if "category" not in loc:
                loc["category"] = "default"
        return data

    def save_location(self, name, lat, lon, category="default"):
        with self._file_lock:
            locations = self.get_saved()
            for loc in locations:
                if loc["name"] == name:
                    loc["lat"] = lat
                    loc["lon"] = lon
                    loc["category"] = category
                    break
            else:
                locations.append({"name": name, "lat": lat, "lon": lon, "category": category})
            with open(SAVED_FILE, "w") as f:
                json.dump(locations, f, indent=2)
        return {"status": f"Saved '{name}'"}

    def delete_location(self, name):
        with self._file_lock:
            locations = [loc for loc in self.get_saved() if loc["name"] != name]
            with open(SAVED_FILE, "w") as f:
                json.dump(locations, f, indent=2)
        return {"status": f"Deleted '{name}'"}

    def get_categories(self):
        """Return list of unique categories."""
        cats = set()
        for loc in self.get_saved():
            cats.add(loc.get("category", "default"))
        return sorted(cats)

    # ── History timeline ───────────────────────────────────

    def get_history(self):
        """Return location history from localStorage-compatible file."""
        hist_file = os.path.join(DATA_DIR, "history.json")
        if not os.path.exists(hist_file):
            return []
        try:
            with open(hist_file, "r") as f:
                return json.load(f)
        except (json.JSONDecodeError, IOError):
            return []

    def add_to_history(self, lat, lon):
        hist_file = os.path.join(DATA_DIR, "history.json")
        with self._file_lock:
            history = self.get_history()
            history.insert(0, {
                "lat": lat, "lon": lon,
                "ts": time.time(),
                "time": datetime.now().strftime("%Y-%m-%d %H:%M"),
            })
            history = history[:100]  # Keep last 100
            with open(hist_file, "w") as f:
                json.dump(history, f, indent=2)

    def clear_history(self):
        hist_file = os.path.join(DATA_DIR, "history.json")
        with self._file_lock:
            with open(hist_file, "w") as f:
                json.dump([], f, indent=2)
        return {"status": "History cleared"}

    # ── Route history ─────────────────────────────────

    def get_routes(self):
        if not os.path.exists(ROUTES_FILE):
            return []
        try:
            with open(ROUTES_FILE, "r") as f:
                return json.load(f)
        except (json.JSONDecodeError, IOError):
            return []

    def save_route(self, name, waypoints, speed, mode, distance):
        route = {
            "id": str(int(time.time() * 1000)),
            "name": name,
            "waypoints": waypoints,
            "speed": speed,
            "mode": mode,
            "distance_km": distance,
            "created": datetime.now().isoformat(),
        }
        with self._file_lock:
            routes = self.get_routes()
            routes.insert(0, route)
            routes = routes[:50]
            with open(ROUTES_FILE, "w") as f:
                json.dump(routes, f, indent=2)
        return {"status": f"Route '{name}' saved", "route": route}

    def delete_route(self, route_id):
        with self._file_lock:
            routes = [r for r in self.get_routes() if r["id"] != route_id]
            with open(ROUTES_FILE, "w") as f:
                json.dump(routes, f, indent=2)
        return {"status": "Route deleted"}

    # ── Helpers ────────────────────────────────────────────

    @staticmethod
    def _haversine(lat1, lon1, lat2, lon2):
        R = 6371000
        p1, p2 = math.radians(lat1), math.radians(lat2)
        dp = math.radians(lat2 - lat1)
        dl = math.radians(lon2 - lon1)
        a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
        return R * 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))
