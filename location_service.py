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
        self._route_speed = 0
        self._route_speeds = None
        self._route_error = None
        self._route_coordinates = None
        self._route_mode = "once"       # "once", "loop", "pingpong"
        self._speed_randomize = False

        # Joystick state
        self._joystick_active = False
        self._joystick_thread = None
        self._joystick_direction = None
        self._joystick_speed = 5

        # Cooldown state
        self._last_teleport_time = None
        self._last_teleport_coords = None
        self._cooldown_end = 0

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
        # Update cooldown before moving
        if self.current_location:
            old = self.current_location
            dist = self._haversine(old["lat"], old["lon"], lat, lon) / 1000
            self._cooldown_end = time.time() + self._calculate_cooldown(dist)
        self._last_teleport_time = time.time()
        self._last_teleport_coords = {"lat": lat, "lon": lon}

        self._stop_keepalive()
        self._sim_set(lat, lon)
        self.current_location = {"lat": lat, "lon": lon}
        self._start_keepalive()
        return {"status": "Location set", "lat": lat, "lon": lon}

    def clear_location(self):
        self._stop_keepalive()
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
            "total_seconds": round(self._cooldown_end - (self._last_teleport_time or now)) if self._last_teleport_time else 0,
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

        self._route_speed = speed
        self._joystick_speed = speed
        self._wander_speed = speed
        if self._route_coordinates:
            self._route_duration = self._route_distance / max(speed / 3.6, 0.01)

        active = []
        if self._route_active:
            active.append("route")
        if self._joystick_active:
            active.append("joystick")
        if self._wander_active:
            active.append("wander")
        return {"status": "Speed updated", "speed_kmh": speed, "active": active}

    def start_route(self, waypoints, speed_kmh=5, mode="once", randomize_speed=False,
                    coordinates=None, provider="osrm", speeds=None):
        if self._route_active:
            raise ValueError("A route is already running. Stop it first.")
        if len(waypoints) < 2:
            raise ValueError("Need at least 2 waypoints.")
        if mode not in ("once", "loop", "pingpong"):
            mode = "once"

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
            self._route_distance = sum(
                self._haversine(coordinates[i][1], coordinates[i][0],
                                coordinates[i + 1][1], coordinates[i + 1][0])
                for i in range(len(coordinates) - 1)
            )
            self._route_duration = self._route_distance / max(float(speed_kmh) / 3.6, 0.01)
        else:
            coords_str = ";".join(f"{wp['lng']},{wp['lat']}" for wp in waypoints)
            url = f"https://router.project-osrm.org/route/v1/driving/{coords_str}?geometries=geojson&overview=full"

            resp = http_requests.get(url, timeout=15)
            data = resp.json()

            if data.get("code") != "Ok" or not data.get("routes"):
                raise ValueError(f"Routing failed: {data.get('message', 'No route found')}")

            route = data["routes"][0]
            coordinates = route["geometry"]["coordinates"]
            self._route_distance = route["distance"]
            self._route_duration = route["duration"]
        self._route_coordinates = coordinates
        self._route_speed = speed_kmh
        # One target speed per segment when the caller worked out a profile
        # from posted limits; otherwise a single speed for the whole route.
        self._route_speeds = list(speeds) if speeds else None
        self._route_error = None
        self._route_mode = mode
        self._route_provider = provider
        self._speed_randomize = randomize_speed
        self._route_active = True
        self._route_paused = False
        self._route_progress = 0
        self.joystick_stop()
        self.stop_wander()
        # The stop helpers restore keep-alive for an idle location. Stop it
        # after both helpers so route updates are the only DVT writes while a
        # route is active.
        self._stop_keepalive()

        self._route_thread = threading.Thread(
            target=self._route_loop, args=(coordinates,), daemon=True
        )
        self._route_thread.start()

        return {
            "status": "Route started",
            "distance_km": round(self._route_distance / 1000, 2),
            "total_points": len(coordinates),
            "coordinates": coordinates,
            "mode": mode,
            "provider": provider,
        }

    def _route_loop(self, coordinates):
        tick = 0.2
        total = len(coordinates)
        reverse = False

        while self._route_active:
            seq = list(range(total))
            if reverse:
                seq = list(reversed(seq))

            segment_distances = []
            for idx in range(len(seq) - 1):
                lng, lat = coordinates[seq[idx]]
                next_lng, next_lat = coordinates[seq[idx + 1]]
                segment_distances.append(self._haversine(lat, lng, next_lat, next_lng))
            path_distance = max(sum(segment_distances), 0.001)
            completed_distance = 0.0

            first_lng, first_lat = coordinates[seq[0]]
            try:
                self._sim_set(first_lat, first_lng)
                self.current_location = {"lat": first_lat, "lon": first_lng}
            except Exception:
                pass
            self._route_progress = 0
            segment_index = 0
            traveled = 0.0
            write_failures = 0
            last_update = time.monotonic()

            while traveled < path_distance and self._route_active:
                while self._route_paused and self._route_active:
                    time.sleep(tick)
                    # Paused time must not turn into a large position jump
                    # when the route resumes.
                    last_update = time.monotonic()
                if not self._route_active:
                    break

                time.sleep(tick)
                if not self._route_active or self._route_paused:
                    continue
                now = time.monotonic()
                elapsed = max(0.0, now - last_update)
                last_update = now
                speed_factor = random.uniform(0.8, 1.2) if self._speed_randomize else 1.0
                profile = self._route_speeds
                target_kmh = (profile[min(segment_index, len(profile) - 1)]
                              if profile else self._route_speed)
                self._route_speed = target_kmh
                speed_ms = max(float(target_kmh) / 3.6, 0.01)
                # DVT writes add latency, so advance by real wall time. Keep
                # the movement as one cumulative path distance so a tick that
                # crosses Google's many short geometry segments does not lose
                # its leftover distance at each segment boundary.
                traveled = min(
                    path_distance,
                    traveled + speed_ms * elapsed * speed_factor,
                )

                while (
                    segment_index < len(segment_distances) - 1
                    and completed_distance + segment_distances[segment_index] < traveled
                ):
                    completed_distance += segment_distances[segment_index]
                    segment_index += 1

                segment_distance = segment_distances[segment_index]
                start_lng, start_lat = coordinates[seq[segment_index]]
                end_lng, end_lat = coordinates[seq[segment_index + 1]]
                segment_offset = max(0.0, traveled - completed_distance)
                fraction = min(
                    1.0,
                    segment_offset / segment_distance if segment_distance else 1.0,
                )
                lat = start_lat + (end_lat - start_lat) * fraction
                lng = start_lng + (end_lng - start_lng) * fraction
                try:
                    self._sim_set(lat, lng, timeout=6)
                    self.current_location = {"lat": lat, "lon": lng}
                    write_failures = 0
                except Exception as exc:
                    # One dropped write is nothing; a run of them means the
                    # phone is gone. Freezing while still reporting "running"
                    # is the worst of both, so say so and stop.
                    write_failures += 1
                    if write_failures >= 8:
                        self._route_error = (
                            "Lost contact with the iPhone — route stopped. "
                            "Check it is plugged in and unlocked."
                        )
                        print(f"[!] Route stopped: {exc}")
                        self._route_active = False
                        break
                self._route_progress = min(100.0, (traveled / path_distance) * 100)

            if self._route_mode == "once":
                break
            elif self._route_mode == "pingpong":
                reverse = not reverse
            # "loop" just repeats with reverse=False

        if self._route_active:
            self._route_progress = 100
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
        self._route_active = False
        self._route_paused = False
        if self._route_thread and self._route_thread.is_alive():
            self._route_thread.join(timeout=5)
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
            "speed_kmh": self._route_speed,
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
            "speed": data.get("speed", self._route_speed) if data else self._route_speed,
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
