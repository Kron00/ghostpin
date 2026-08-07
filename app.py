import json
import html
import math
import random
import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor

import requests as http_requests
from flask import Flask, jsonify, request, render_template, send_from_directory

from device_manager import DeviceManager
from location_service import LocationService


PORT = 8080

app = Flask(__name__)

# Binding to loopback does not stop a hostile page from reaching the API: a
# domain that resolves to 127.0.0.1 makes the browser treat these endpoints as
# same-origin, which would expose the UDID, current location, and saved history
# through plain GETs. Rejecting unexpected Host headers closes that off.
app.config["TRUSTED_HOSTS"] = ["127.0.0.1", "localhost"]

# The API is unauthenticated because it only ever binds to 127.0.0.1. That still
# leaves it reachable by any page open in a browser on this machine, which can
# submit a cross-origin form POST to endpoints that take no body. Rejecting
# mismatched origins closes that off; requests carrying neither header (the
# native webview, curl) are allowed through.
_ALLOWED_ORIGINS = frozenset({
    f"http://127.0.0.1:{PORT}",
    f"http://localhost:{PORT}",
})


@app.before_request
def _reject_cross_origin_writes():
    if request.method in ("GET", "HEAD", "OPTIONS"):
        return None

    origin = request.headers.get("Origin")
    if origin is not None:
        if origin not in _ALLOWED_ORIGINS:
            return jsonify({"error": "cross-origin request rejected"}), 403
        return None

    referer = request.headers.get("Referer")
    if referer and not any(referer.startswith(o + "/") or referer == o
                           for o in _ALLOWED_ORIGINS):
        return jsonify({"error": "cross-origin request rejected"}), 403

    return None


# The page loads Leaflet from unpkg and tiles from CARTO; everything else is
# same-origin. Geocoding happens in Python, so the browser itself never needs
# to reach a third party — connect-src stays 'self'. 'unsafe-inline' is only
# granted to styles, which Leaflet and the panel code set as attributes.
_CSP = "; ".join([
    "default-src 'self'",
    "script-src 'self' https://unpkg.com",
    "style-src 'self' https://unpkg.com 'unsafe-inline'",
    "img-src 'self' data: blob: https://*.basemaps.cartocdn.com https://unpkg.com",
    "connect-src 'self'",
    "font-src 'self'",
    "object-src 'none'",
    "base-uri 'none'",
    "form-action 'none'",
    "frame-ancestors 'none'",
])


@app.after_request
def _security_headers(response):
    response.headers.setdefault("Content-Security-Policy", _CSP)
    response.headers.setdefault("X-Content-Type-Options", "nosniff")
    response.headers.setdefault("Referrer-Policy", "no-referrer")
    return response


# Global state — initialized by main() or main_app.py
device_mgr = None
loc_svc = None
_state_lock = threading.Lock()
_schedule_thread = None
_schedule_active = False

# Small in-process caches keep startup fast and avoid hammering the public
# geocoders while someone is typing.
_default_location_cache = {"data": None, "ts": 0}
_default_location_lock = threading.Lock()
_search_cache = {}
_search_cache_lock = threading.Lock()
_nominatim_lock = threading.Lock()
_last_nominatim_time = 0


def _check_ready():
    """Return error response if device/service not ready, else None."""
    if device_mgr is None:
        return jsonify({"error": "Tunnel not connected. Ensure iPhone is plugged in and restart the app."}), 503
    if loc_svc is None:
        return jsonify({"error": "No device connected. Plug in your iPhone and restart."}), 503
    return None


# ── Pages ──────────────────────────────────────────────────────

@app.route("/")
def index():
    return render_template("index.html")


@app.route("/favicon.ico")
def favicon():
    # Browsers ask for this unprompted; without it every load logs a 404.
    return send_from_directory(app.static_folder, "icon.png", mimetype="image/png")


# ── Device API ─────────────────────────────────────────────────

@app.route("/api/device")
def api_device():
    if device_mgr is None:
        return jsonify({"connected": False, "name": None, "ios_version": None,
                        "udid": None, "model": None, "connection_type": None,
                        "error": "Tunnel not running"})
    return jsonify(device_mgr.get_device_info())


@app.route("/api/device/connections")
def api_device_connections():
    if device_mgr is None:
        return jsonify([])
    return jsonify(device_mgr.get_available_connections())


@app.route("/api/devices")
def api_devices_list():
    """List all unique devices visible to the tunnel."""
    if device_mgr is None:
        return jsonify([])
    return jsonify(device_mgr.get_all_devices())


@app.route("/api/device/switch", methods=["POST"])
def api_device_switch():
    if device_mgr is None:
        return jsonify({"error": "Not initialized"}), 503
    data = request.json or {}
    prefer_wifi = data.get("wifi", False)
    udid = data.get("udid")
    try:
        if loc_svc:
            loc_svc._stop_keepalive()
        info = device_mgr.reconnect(udid=udid, prefer_wifi=prefer_wifi)
        if loc_svc:
            loc_svc.simulator = device_mgr.simulator
            if loc_svc.current_location:
                loc_svc._start_keepalive()
        return jsonify({"status": "Switched", **info})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/device/connect", methods=["POST"])
def api_device_connect():
    global device_mgr, loc_svc

    data = request.json or {}
    prefer_wifi = data.get("wifi", False)
    udid = data.get("udid")

    with _state_lock:
        if device_mgr is None:
            device_mgr = DeviceManager()

        if loc_svc:
            loc_svc._stop_keepalive()
            loc_svc.stop_route()

        try:
            if device_mgr.device_info.get("connected"):
                info = device_mgr.reconnect(udid=udid, prefer_wifi=prefer_wifi, retries=3)
            else:
                info = device_mgr.connect(udid=udid, prefer_wifi=prefer_wifi, retries=3)

            loc_svc = LocationService(device_mgr.simulator, device_mgr.bridge)
            _start_schedule_checker()
            return jsonify({"status": "Connected", **info})
        except Exception as e:
            return jsonify({"error": str(e)}), 500


@app.route("/api/device/auto-reconnect", methods=["POST"])
def api_auto_reconnect():
    if device_mgr is None:
        return jsonify({"error": "Not initialized"}), 503
    data = request.json or {}
    enabled = data.get("enabled", True)

    def before_reconnect():
        # Runs before the old session is torn down. The previous keep-alive
        # thread would otherwise keep writing to a DVT channel that is being
        # closed underneath it, and go on retrying against the dead session
        # while the new one is being built.
        global loc_svc
        with _state_lock:
            if loc_svc:
                loc_svc.stop_route()
                loc_svc._stop_keepalive()

    def on_reconnect(info):
        global loc_svc
        with _state_lock:
            if device_mgr and device_mgr.simulator:
                loc_svc = LocationService(device_mgr.simulator, device_mgr.bridge)

    if enabled:
        return jsonify(device_mgr.enable_auto_reconnect(
            callback=on_reconnect, pre_callback=before_reconnect
        ))
    else:
        return jsonify(device_mgr.disable_auto_reconnect())


@app.route("/api/tunnel/status")
def api_tunnel_status():
    if device_mgr is None:
        return jsonify({"running": False, "mode": None})
    return jsonify(device_mgr.get_tunnel_status())


# ── Default location ───────────────────────────────────────────

@app.route("/api/default-location")
def api_default_location():
    now = time.time()
    cached = _default_location_cache["data"]
    if cached and now - _default_location_cache["ts"] < 900:
        return jsonify(cached)

    with _default_location_lock:
        cached = _default_location_cache["data"]
        if cached and time.time() - _default_location_cache["ts"] < 900:
            return jsonify(cached)

        providers = (
            ("https://ipwho.is/", {}, lambda d: (
                d.get("success") and d.get("latitude") is not None,
                d.get("latitude"), d.get("longitude"), d.get("city", ""),
                d.get("region", ""), d.get("country_code", ""),
            )),
            ("https://ipinfo.io/json", {"headers": {"User-Agent": "Ghostpin/2.0"}}, lambda d: (
                "," in d.get("loc", ""),
                *(d.get("loc", ",").split(",", 1)), d.get("city", ""),
                d.get("region", ""), d.get("country", ""),
            )),
        )

        for url, extra, parse in providers:
            try:
                response = http_requests.get(url, timeout=3, **extra)
                response.raise_for_status()
                valid, lat, lon, city, region, country = parse(response.json())
                if not valid:
                    continue
                result = {
                    "available": True,
                    "lat": float(lat),
                    "lon": float(lon),
                    "city": city,
                    "region": region,
                    "country": country,
                    # Countries that post speeds in mph.
                    "speed_unit": "mph" if str(country).upper() in
                                  {"US", "GB", "UK", "LR", "MM", "BS", "BZ", "KY", "VG"}
                                  else "kmh",
                    "source": "network",
                    "accuracy": "approximate",
                }
                _default_location_cache.update(data=result, ts=time.time())
                return jsonify(result)
            except (TypeError, ValueError, http_requests.RequestException):
                continue

    return jsonify({"available": False, "source": "unavailable"})


# ── Location consistency API ──────────────────────────────────

_ip_cache = {"data": None, "ts": 0}


def _get_ip_location():
    """Get the user's real IP geolocation (cached 5 min). Returns dict or None."""
    global _ip_cache
    if _ip_cache["data"] and time.time() - _ip_cache["ts"] < 300:
        return _ip_cache["data"]

    result = None
    try:
        r = http_requests.get("https://ipinfo.io/json", timeout=3,
                              headers={"User-Agent": "Ghostpin/1.0"})
        data = r.json()
        loc = data.get("loc", "")
        if "," in loc:
            lat, lon = loc.split(",")
            result = {"lat": float(lat), "lon": float(lon),
                      "city": data.get("city", ""), "country": data.get("country", ""),
                      "timezone": data.get("timezone", "")}
    except Exception:
        pass

    if result is None:
        try:
            r = http_requests.get("https://ipwho.is/", timeout=3)
            data = r.json()
            if data.get("success") and "latitude" in data:
                result = {"lat": data["latitude"], "lon": data["longitude"],
                          "city": data.get("city", ""), "country": data.get("country_code", ""),
                          "timezone": data.get("timezone", {}).get("id", "")}
        except Exception:
            pass

    if result:
        _ip_cache = {"data": result, "ts": time.time()}
    return result


@app.route("/api/stealth/check")
def api_stealth_check():
    """Check approximate IP, simulated GPS, and timezone consistency."""
    result = {"check_available": False, "unavailable_reason": None,
              "ip_mismatch": False, "ip_location": None,
              "spoof_location": None, "distance_km": None, "warnings": []}

    if loc_svc is None:
        result["unavailable_reason"] = "no_location"
        return jsonify(result)

    spoof = loc_svc.get_current()
    if spoof is None:
        result["unavailable_reason"] = "no_location"
        return jsonify(result)

    ip_loc = _get_ip_location()
    if ip_loc is None:
        result["unavailable_reason"] = "ip_lookup"
        return jsonify(result)

    result["check_available"] = True
    result["ip_location"] = ip_loc
    result["spoof_location"] = spoof

    dist = LocationService._haversine(
        ip_loc["lat"], ip_loc["lon"], spoof["lat"], spoof["lon"]
    ) / 1000
    result["distance_km"] = round(dist, 1)

    # IP mismatch: >100 km apart
    if dist > 100:
        severity = "high" if dist > 500 else "medium"
        result["ip_mismatch"] = True
        result["warnings"].append({
            "type": "ip_mismatch",
            "severity": severity,
            "message": (f"Your IP is in {ip_loc.get('city', '?')}, "
                        f"{ip_loc.get('country', '?')} but GPS is "
                        f"{round(dist)} km away. Use a VPN to match."),
        })

    # Timezone: estimate UTC offset from longitude
    spoof_utc = round(spoof["lon"] / 15)
    ip_utc = round(ip_loc["lon"] / 15)
    if abs(spoof_utc - ip_utc) > 1:
        sign = "+" if spoof_utc >= 0 else ""
        result["warnings"].append({
            "type": "timezone_mismatch",
            "severity": "medium",
            "message": (f"Device timezone may not match spoofed location "
                        f"(~UTC{sign}{spoof_utc}). Change in iOS Settings "
                        f"> General > Date & Time."),
        })

    return jsonify(result)


# ── Location API ───────────────────────────────────────────────

def _seed_cooldown_reference():
    """Give the cooldown something to measure the first jump against.

    Without this the opening teleport of a session — real location to wherever
    the user picked, often the longest jump they will make — is treated as no
    movement at all. The IP lookup is coarse, but it is the right order of
    magnitude and far better than pretending the jump never happened.
    """
    if loc_svc is None or loc_svc._last_known_position or loc_svc.current_location:
        return
    home = _get_ip_location()
    if home and home.get("lat") is not None and home.get("lon") is not None:
        loc_svc._last_known_position = {"lat": home["lat"], "lon": home["lon"]}


@app.route("/api/location/set", methods=["POST"])
def api_set_location():
    err = _check_ready()
    if err:
        return err
    data = request.json or {}
    try:
        lat = float(data["lat"])
        lon = float(data["lon"])
    except (KeyError, TypeError, ValueError):
        return jsonify({"error": "lat and lon are required (numbers)"}), 400

    if not (-90 <= lat <= 90) or not (-180 <= lon <= 180):
        return jsonify({"error": "Invalid coordinates"}), 400

    try:
        if loc_svc._route_active:
            loc_svc.stop_route()
        loc_svc.joystick_stop()
        loc_svc.stop_wander()
        _seed_cooldown_reference()
        result = loc_svc.set_location(lat, lon)
        loc_svc.add_to_history(lat, lon)
        return jsonify(result)
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/location/clear", methods=["POST"])
def api_clear_location():
    err = _check_ready()
    if err:
        return err
    try:
        result = loc_svc.clear_location()
        return jsonify(result)
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/location/current")
def api_current_location():
    if loc_svc is None:
        return jsonify({"error": "No iPhone connected. Plug it in, unlock it, and try again."}), 503
    loc = loc_svc.get_current()
    if loc is None:
        return jsonify({"error": "No location set"}), 404
    return jsonify(loc)


# ── Cooldown API ───────────────────────────────────────────────

@app.route("/api/cooldown")
def api_cooldown():
    if loc_svc is None:
        return jsonify({"active": False, "remaining_seconds": 0})
    return jsonify(loc_svc.get_cooldown())


# ── Joystick API ───────────────────────────────────────────────

@app.route("/api/joystick/move", methods=["POST"])
def api_joystick_move():
    err = _check_ready()
    if err:
        return err
    data = request.json or {}
    direction = data.get("direction", "n")
    speed = data.get("speed", 5)
    try:
        speed = float(speed)
    except (TypeError, ValueError):
        speed = 5
    try:
        result = loc_svc.joystick_start(direction, speed)
        return jsonify(result)
    except ValueError as e:
        return jsonify({"error": str(e)}), 400
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/joystick/stop", methods=["POST"])
def api_joystick_stop():
    err = _check_ready()
    if err:
        return err
    return jsonify(loc_svc.joystick_stop())


# ── Search API ────────────────────────────────────────────────

_COORDINATE_QUERY = re.compile(
    r"^\s*(-?(?:\d+(?:\.\d+)?|\.\d+))\s*[,\s]\s*"
    r"(-?(?:\d+(?:\.\d+)?|\.\d+))\s*$"
)


def _join_unique(parts):
    """Join non-empty address parts without repeating the same label."""
    output = []
    seen = set()
    for part in parts:
        value = str(part or "").strip()
        key = value.casefold()
        if value and key not in seen:
            output.append(value)
            seen.add(key)
    return ", ".join(output)


def _haversine_km(lat1, lon1, lat2, lon2):
    radius_km = 6371.0088
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlambda = math.radians(lon2 - lon1)
    a = (math.sin(dphi / 2) ** 2
         + math.cos(phi1) * math.cos(phi2) * math.sin(dlambda / 2) ** 2)
    return radius_km * 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))


def _parse_focus(args):
    try:
        lat = float(args.get("lat"))
        lon = float(args.get("lon"))
        if -90 <= lat <= 90 and -180 <= lon <= 180:
            return lat, lon
    except (TypeError, ValueError):
        pass
    return None

def _dedup_results(results):
    seen_names = set()
    seen_coords = []
    out = []
    for r in results:
        name_key = r["display_name"].lower().strip()
        if name_key in seen_names:
            continue
        lat, lon = r["lat"], r["lon"]
        too_close = False
        for slat, slon in seen_coords:
            if abs(lat - slat) < 0.002 and abs(lon - slon) < 0.002:
                too_close = True
                break
        if too_close:
            continue
        seen_names.add(name_key)
        seen_coords.append((lat, lon))
        out.append(r)
    return out


def _parse_photon(data):
    results = []
    for f in data.get("features", []):
        props = f.get("properties", {})
        coords = f.get("geometry", {}).get("coordinates", [])
        if len(coords) < 2:
            continue
        name = props.get("name", "")
        if not name:
            continue
        street = _join_unique((props.get("housenumber"), props.get("street")))
        subtitle = _join_unique((street, props.get("district"), props.get("city"),
                                 props.get("state"), props.get("postcode"), props.get("country")))
        display_name = _join_unique((name, subtitle))
        extent = props.get("extent")
        bbox = None
        if isinstance(extent, list) and len(extent) == 4:
            # Photon extent: [minLon, minLat, maxLon, maxLat]
            bbox = [extent[1], extent[0], extent[3], extent[2]]
        results.append({
            "name": name,
            "subtitle": subtitle,
            "display_name": display_name,
            "lat": float(coords[1]), "lon": float(coords[0]),
            "type": props.get("osm_value", props.get("type", "")),
            "source": "photon",
            "bbox": bbox,
        })
    return results


def _parse_nominatim(data):
    results = []
    for r in data:
        try:
            address = r.get("address") or {}
            namedetails = r.get("namedetails") or {}
            name = (namedetails.get("name") or r.get("name")
                    or r["display_name"].split(",", 1)[0]).strip()
            subtitle = r["display_name"]
            if subtitle.casefold().startswith(name.casefold() + ","):
                subtitle = subtitle[len(name) + 1:].strip()
            bounding = r.get("boundingbox")
            bbox = None
            if isinstance(bounding, list) and len(bounding) == 4:
                # Nominatim: [south, north, west, east]
                bbox = [float(bounding[0]), float(bounding[2]),
                        float(bounding[1]), float(bounding[3])]
            results.append({
                "name": name,
                "subtitle": subtitle,
                "display_name": r["display_name"],
                "lat": float(r["lat"]), "lon": float(r["lon"]),
                "type": r.get("type", ""),
                "source": "nominatim",
                "bbox": bbox,
                "importance": float(r.get("importance", 0) or 0),
                "country_code": address.get("country_code", ""),
            })
        except (KeyError, TypeError, ValueError):
            continue
    return results


def _fetch_photon(query, focus, zoom):
    params = {
        "q": query,
        "limit": 20,
        "lang": "en",
        "dedupe": 1,
    }
    if focus:
        params.update({
            "lat": focus[0],
            "lon": focus[1],
            "zoom": zoom,
            "location_bias_scale": 0.2,
        })
    response = http_requests.get(
        "https://photon.komoot.io/api/",
        params=params,
        headers={"User-Agent": "Ghostpin/2.0"},
        timeout=5,
    )
    response.raise_for_status()
    return _parse_photon(response.json())


def _fetch_google_maps(query, focus, zoom, country_code):
    """Use Google Maps' keyless web autocomplete as the primary provider.

    This is intentionally isolated: it is an undocumented endpoint and may
    change. Any failure is handled by the caller, which keeps the documented
    OpenStreetMap providers available as a transparent fallback.
    """
    lat, lon = focus or (0.0, 0.0)
    map_span = max(1000, min(20000000, 53750 * (2 ** (13 - zoom))))
    pb = (f"!2i5!4m9!1m3!1d{map_span}!2d{lon}!3d{lat}!2m0!3m2!1i1280!2i800"
          f"!4f{zoom + 0.1}!7i20!10b1")
    response = http_requests.get(
        "https://www.google.com/s",
        params={
            "tbm": "map",
            "gs_ri": "maps",
            "suggest": "p",
            "hl": "en",
            "gl": country_code,
            "q": query,
            "pb": pb,
        },
        headers={
            "User-Agent": ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                           "AppleWebKit/537.36 (KHTML, like Gecko) "
                           "Chrome/126.0.0.0 Safari/537.36"),
            "Accept-Language": "en-US,en;q=0.9",
            "Referer": "https://www.google.com/maps/",
        },
        timeout=5,
    )
    response.raise_for_status()
    payload = response.text
    if payload.startswith(")]}'"):
        payload = payload.split("\n", 1)[1]
    data = json.loads(payload)

    items = data[0][1]
    results = []
    for raw in items:
        try:
            suggestion = raw[22]
            name = str(suggestion[1][0]).strip()
            coordinates = suggestion[11]
            if not coordinates or coordinates[2] is None or coordinates[3] is None:
                continue
            lat_value = float(coordinates[2])
            lon_value = float(coordinates[3])
            subtitle = str(suggestion[2][0]).strip() if suggestion[2] else ""
            full_name = str(suggestion[0][0]).strip() if suggestion[0] else ""
            display_name = full_name or _join_unique((name, subtitle))
            results.append({
                "name": name,
                "subtitle": subtitle,
                "display_name": display_name,
                "lat": lat_value,
                "lon": lon_value,
                "type": "place",
                "source": "google",
                "bbox": None,
            })
        except (IndexError, TypeError, ValueError):
            continue
    return results


def _fetch_nominatim(query, focus):
    global _last_nominatim_time
    params = {
        "q": query,
        "format": "jsonv2",
        "limit": 12,
        "addressdetails": 1,
        "namedetails": 1,
        "accept-language": "en",
    }
    if focus:
        lat, lon = focus
        # This is a preference, not a hard boundary: explicit searches for a
        # distant landmark still work while generic POI searches stay local.
        lon_span = min(3.0, 1.5 / max(0.25, math.cos(math.radians(lat))))
        params.update({
            "viewbox": f"{lon - lon_span},{lat + 1.5},{lon + lon_span},{lat - 1.5}",
            "bounded": 0,
        })

    with _nominatim_lock:
        wait = 1.05 - (time.time() - _last_nominatim_time)
        if wait > 0:
            time.sleep(wait)
        response = http_requests.get(
            "https://nominatim.openstreetmap.org/search",
            params=params,
            headers={
                "User-Agent": "Ghostpin/2.0 (local macOS app; "
                              "https://github.com/Kron00/ghostpin)"
            },
            timeout=6,
        )
        _last_nominatim_time = time.time()

    response.raise_for_status()
    return _parse_nominatim(response.json())


def _rank_results(results, query, focus):
    query_key = query.casefold().strip()
    query_tokens = {token for token in re.split(r"\W+", query_key) if token}

    for order, result in enumerate(results):
        name_key = result.get("name", "").casefold()
        display_key = result["display_name"].casefold()
        score = max(0, 30 - order)
        if name_key == query_key:
            score += 160
        elif name_key.startswith(query_key):
            score += 110
        elif query_key in name_key:
            score += 75
        if query_tokens and query_tokens.issubset(set(re.split(r"\W+", display_key))):
            score += 40
        score += min(20, float(result.get("importance", 0) or 0) * 25)

        if focus:
            distance = _haversine_km(focus[0], focus[1], result["lat"], result["lon"])
            result["distance_km"] = round(distance, 1)
            score += 50 / (1 + distance / 20)
        if result.get("source") == "google":
            score += 35
        result["_score"] = score

    results.sort(key=lambda item: item["_score"], reverse=True)
    for result in results:
        result.pop("_score", None)
        result.pop("importance", None)
        result.pop("country_code", None)
    return results


# Every cache miss fans out to three geocoders, and unique queries never hit
# the cache. Bound both the rate and the concurrency so a runaway caller
# cannot exhaust the request threads or get the user throttled upstream.
_SEARCH_RATE_PER_SEC = 8.0
_SEARCH_BURST = 16.0
_search_tokens = _SEARCH_BURST
_search_tokens_ts = time.time()
_search_rate_lock = threading.Lock()
_search_slots = threading.Semaphore(4)


def _search_allowed():
    global _search_tokens, _search_tokens_ts
    with _search_rate_lock:
        now = time.time()
        _search_tokens = min(
            _SEARCH_BURST,
            _search_tokens + (now - _search_tokens_ts) * _SEARCH_RATE_PER_SEC,
        )
        _search_tokens_ts = now
        if _search_tokens < 1.0:
            return False
        _search_tokens -= 1.0
        return True


@app.route("/api/search")
def api_search():
    query = request.args.get("q", "").strip()
    if not query:
        return jsonify([])
    if len(query) > 160:
        return jsonify({"error": "Search is too long"}), 400

    coordinate_match = _COORDINATE_QUERY.match(query)
    if coordinate_match:
        lat, lon = map(float, coordinate_match.groups())
        if -90 <= lat <= 90 and -180 <= lon <= 180:
            return jsonify([{
                "name": "Coordinates",
                "subtitle": f"{lat:.6f}, {lon:.6f}",
                "display_name": f"{lat:.6f}, {lon:.6f}",
                "lat": lat,
                "lon": lon,
                "type": "coordinates",
                "source": "coordinates",
                "bbox": None,
                "distance_km": 0,
            }])

    focus = _parse_focus(request.args)
    try:
        zoom = max(2, min(18, int(float(request.args.get("zoom", 12)))))
    except (TypeError, ValueError):
        zoom = 12
    country_code = request.args.get("country", "us").strip().lower()
    if not re.fullmatch(r"[a-z]{2}", country_code):
        country_code = "us"

    cache_key = (query.casefold(), round(focus[0], 2) if focus else None,
                 round(focus[1], 2) if focus else None, zoom // 2, country_code)
    with _search_cache_lock:
        cached = _search_cache.get(cache_key)
        if cached and time.time() - cached["ts"] < 300:
            return jsonify(cached["data"])

    # Past this point the request costs three outbound calls, so it is rate
    # limited. The cache lookup above stays free.
    if not _search_allowed():
        return jsonify({"error": "Too many searches, slow down"}), 429
    if not _search_slots.acquire(timeout=5):
        return jsonify({"error": "Search is busy, try again"}), 429

    try:
        return _search_providers(query, focus, zoom, country_code, cache_key)
    finally:
        _search_slots.release()


def _search_providers(query, focus, zoom, country_code, cache_key):
    all_results = []
    with ThreadPoolExecutor(max_workers=3) as pool:
        futures = (
            pool.submit(_fetch_google_maps, query, focus, zoom, country_code),
            pool.submit(_fetch_photon, query, focus, zoom),
            pool.submit(_fetch_nominatim, query, focus),
        )
        for future in futures:
            try:
                all_results.extend(future.result())
            except (IndexError, KeyError, TypeError, ValueError, json.JSONDecodeError,
                    http_requests.RequestException):
                continue

    if not all_results:
        return jsonify([])

    ranked = _rank_results(_dedup_results(all_results), query, focus)[:12]
    with _search_cache_lock:
        _search_cache[cache_key] = {"data": ranked, "ts": time.time()}
        if len(_search_cache) > 100:
            oldest = min(_search_cache, key=lambda key: _search_cache[key]["ts"])
            _search_cache.pop(oldest, None)
    return jsonify(ranked)


# ── Route API ──────────────────────────────────────────────────

def _google_directions_route(origin, destination):
    """Fetch and decode Google's keyless web directions result."""
    headers = {
        "User-Agent": ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                       "AppleWebKit/537.36 (KHTML, like Gecko) "
                       "Chrome/126.0.0.0 Safari/537.36"),
        "Accept-Language": "en-US,en;q=0.9",
        "Referer": "https://www.google.com/maps/",
    }
    page = http_requests.get(
        "https://www.google.com/maps/dir/",
        params={
            "api": "1",
            "origin": origin,
            "destination": destination,
            "travelmode": "driving",
            "hl": "en",
        },
        headers=headers,
        timeout=12,
    )
    page.raise_for_status()
    match = re.search(r'href="([^"]*/maps/preview/directions\?[^\"]+)"', page.text)
    if not match:
        raise ValueError("Google Maps could not calculate that route")

    preview_url = html.unescape(match.group(1))
    if preview_url.startswith("/"):
        preview_url = "https://www.google.com" + preview_url
    preview = http_requests.get(preview_url, headers=headers, timeout=15)
    preview.raise_for_status()
    payload_text = preview.text
    if payload_text.startswith(")]}'"):
        payload_text = payload_text.split("\n", 1)[1]
    payload = json.loads(payload_text)

    try:
        places = payload[0][0]
        route = payload[0][1][0]
        summary = route[0]
        start = places[0][0][0]
        finish = places[1][0][0]
        start_lat, start_lon = float(start[2][2]), float(start[2][3])
        end_lat, end_lon = float(finish[2][2]), float(finish[2][3])
        google_distance_m = float(summary[2][0])
        google_duration_s = float(summary[3][0])
        route_name = summary[1] or "Google Maps route"

        # Google exposes each route shape as parallel arrays of E7 latitude
        # and longitude deltas. The first value is absolute; the rest are
        # cumulative deltas. Route 0 matches the selected summary above.
        encoded_shape = payload[0][7][0]
        lat_deltas, lon_deltas = encoded_shape[0], encoded_shape[1]
        if len(lat_deltas) != len(lon_deltas) or len(lat_deltas) < 2:
            raise ValueError
        lat_e7 = lon_e7 = 0
        coordinates = []
        for lat_delta, lon_delta in zip(lat_deltas, lon_deltas):
            lat_e7 += int(lat_delta)
            lon_e7 += int(lon_delta)
            coordinates.append([lon_e7 / 10_000_000, lat_e7 / 10_000_000])
    except (IndexError, TypeError, ValueError) as exc:
        raise ValueError("Google Maps returned an unreadable route") from exc

    return {
        "provider": "google",
        "route_name": route_name,
        "origin": {"name": start[0], "lat": start_lat, "lon": start_lon},
        "destination": {"name": finish[0], "lat": end_lat, "lon": end_lon},
        "waypoints": [
            {"lat": start_lat, "lng": start_lon},
            {"lat": end_lat, "lng": end_lon},
        ],
        "coordinates": coordinates,
        "distance_km": round(google_distance_m / 1000, 2),
        "duration_min": round(google_duration_s / 60, 1),
    }


def _google_multi_stop_route(stops):
    """Chain Google's two-point directions across an arbitrary list of stops.

    Google's keyless endpoint is undocumented and its multi-waypoint response
    shape is not something to depend on, so each consecutive pair is requested
    as its own leg and the legs are stitched together. Predictable, at the cost
    of one request per leg.
    """
    legs = [_google_directions_route(a, b) for a, b in zip(stops, stops[1:])]

    coordinates = []
    waypoints = []
    stop_indices = []
    total_km = 0.0
    total_min = 0.0

    for index, leg in enumerate(legs):
        leg_coords = leg["coordinates"]
        # The end of one leg and the start of the next are the same place.
        if coordinates and leg_coords and coordinates[-1] == leg_coords[0]:
            leg_coords = leg_coords[1:]
        coordinates.extend(leg_coords)
        if index < len(legs) - 1:
            stop_indices.append(len(coordinates) - 1)
        total_km += leg["distance_km"]
        total_min += leg["duration_min"]
        if index == 0:
            waypoints.append({"lat": leg["origin"]["lat"], "lng": leg["origin"]["lon"]})
        waypoints.append({"lat": leg["destination"]["lat"], "lng": leg["destination"]["lon"]})

    first, last = legs[0], legs[-1]
    if len(legs) == 1:
        route_name = first["route_name"]
    else:
        route_name = f"{first['origin']['name']} → {last['destination']['name']} via {len(legs) - 1} stop(s)"

    return {
        "provider": "google",
        "route_name": route_name,
        "origin": first["origin"],
        "destination": last["destination"],
        "stops": [leg["origin"] for leg in legs] + [last["destination"]],
        "waypoints": waypoints,
        "coordinates": coordinates,
        "stop_indices": stop_indices,
        "distance_km": round(total_km, 2),
        "duration_min": round(total_min, 1),
        "legs": len(legs),
    }



# ── Roaming the road network ───────────────────────────────────
# Wandering in straight lines across whatever happens to be there — buildings,
# rivers — does not look like a person moving. Roaming instead takes a random
# walk over the actual road graph inside the area, which the ordinary route
# runner can then drive.

# ── Roam as a random walk on the road graph ───────────────────
# Roam used to pre-calculate a whole tour through scattered waypoints, which
# drew a giant route on the map and inherited OSRM's dead-end detours. Instead,
# fetch the area's drivable roads once, prune dead ends from the graph, and
# wander it turn by turn: at each junction pick a road at random, avoiding the
# one just driven and recently used ones. Only a modest chunk is generated per
# request; the client stitches chunks endlessly and shows a short lookahead.

_ROAM_GRAPH_CACHE = {}
_ROAM_GRAPH_CACHE_MAX = 4
_ROAM_RECENT_EDGES = 12
_ROAM_RECENT_NODES = 12
_ROAM_MIN_TURN_DEG = 45.0
_ROAM_HEADING_DISTANCE_M = 20.0


def _roam_way_direction(tags):
    """Return allowed OSM-way directions as ``(forward, backward)``.

    OSM defines ``oneway`` relative to the stored node order.  Explicit motor
    vehicle overrides take precedence, while roundabouts and motorways carry
    the documented implicit forward-only direction.  A time-dependent or
    unknown restriction is skipped: guessing bidirectional there could put the
    simulated car against traffic.
    """
    tags = tags or {}
    if any(key in tags for key in (
            "oneway:conditional", "oneway:motor_vehicle:conditional",
            "oneway:motorcar:conditional")):
        return None

    raw = tags.get("oneway:motorcar")
    if raw is None:
        raw = tags.get("oneway:motor_vehicle")
    if raw is None:
        raw = tags.get("oneway")

    if raw is not None:
        value = str(raw).strip().lower()
        if value in ("yes", "true", "1"):
            return True, False
        if value in ("-1", "reverse"):
            return False, True
        if value in ("no", "false", "0"):
            return True, True
        return None

    if tags.get("junction") == "roundabout" or tags.get("highway") == "motorway":
        return True, False
    return True, True


def _fetch_road_graph(lat, lon, radius_m):
    """The drivable road graph inside the circle, dead ends pruned, or None."""
    cache_key = (round(lat, 4), round(lon, 4), int(radius_m))
    if cache_key in _ROAM_GRAPH_CACHE:
        return _ROAM_GRAPH_CACHE[cache_key]

    d_lat = radius_m / 111320.0
    d_lon = radius_m / (111320.0 * math.cos(math.radians(lat)))
    bbox = f"{lat - d_lat},{lon - d_lon},{lat + d_lat},{lon + d_lon}"
    query = (
        "[out:json][timeout:50];("
        f'way[highway~"^(motorway|trunk|primary|secondary|tertiary|unclassified'
        f'|residential|living_street|road)(_link)?$"]({bbox});'
        f'node[highway~"^(stop|give_way|traffic_signals)$"]({bbox});'
        ");out geom;"
    )
    elements = _overpass_elements(query)
    if not elements:
        return None

    cos_lat = math.cos(math.radians(lat))

    def inside(nlat, nlon):
        dy = (nlat - lat) * 111320.0
        dx = (nlon - lon) * 111320.0 * cos_lat
        return math.hypot(dx, dy) <= radius_m

    ways = []
    junction_kinds = {}
    node_uses = {}
    for element in elements:
        if not isinstance(element, dict):
            continue
        if element.get("type") == "way":
            tags = element.get("tags") or {}
            speed = _parse_maxspeed(tags.get("maxspeed")) or \
                _CLASS_DEFAULT_KMH.get(tags.get("highway"))
            direction = _roam_way_direction(tags)
            node_ids = element.get("nodes") or []
            geometry = element.get("geometry") or []
            if (not speed or direction is None
                    or len(node_ids) != len(geometry) or len(node_ids) < 2):
                continue
            ways.append((node_ids, geometry, speed, direction))
            for node_id in node_ids:
                node_uses[node_id] = node_uses.get(node_id, 0) + 1
        elif element.get("type") == "node":
            highway = (element.get("tags") or {}).get("highway")
            if highway in ("stop", "give_way", "traffic_signals"):
                kind = "signal" if highway == "traffic_signals" else "stop"
                junction_kinds[element["id"]] = kind

    # Split ways into edges at junctions (nodes shared between ways) and way
    # ends. An edge with any vertex outside the circle is dropped, so roads
    # leaving the area become stubs that the dead-end pruning removes.
    edges = []
    adjacency = {}
    for node_ids, geometry, speed, direction in ways:
        cut = [0]
        for index in range(1, len(node_ids) - 1):
            if node_uses[node_ids[index]] > 1:
                cut.append(index)
        cut.append(len(node_ids) - 1)
        for a, b in zip(cut, cut[1:]):
            seg_ids = node_ids[a:b + 1]
            seg_pts = [(float(p["lon"]), float(p["lat"])) for p in geometry[a:b + 1]]
            if len(seg_ids) < 2:
                continue
            if not all(inside(p[1], p[0]) for p in seg_pts):
                continue
            edge_id = len(edges)
            edges.append({
                "a": seg_ids[0], "b": seg_ids[-1],
                "ids": seg_ids, "points": seg_pts, "speed": speed,
                "forward": direction[0], "backward": direction[1],
            })
            adjacency.setdefault(seg_ids[0], set()).add(edge_id)
            adjacency.setdefault(seg_ids[-1], set()).add(edge_id)

    # Prune dead ends: any node reachable only one way is somewhere a wanderer
    # would have to double back out of, which is exactly the look to avoid.
    queue = [n for n, e in adjacency.items() if len(e) == 1]
    removed = set()
    while queue:
        node = queue.pop()
        live = [e for e in adjacency.get(node, ()) if e not in removed]
        if len(live) != 1:
            continue
        edge_id = live[0]
        removed.add(edge_id)
        edge = edges[edge_id]
        other = edge["b"] if edge["a"] == node else edge["a"]
        adjacency[node].discard(edge_id)
        adjacency[other].discard(edge_id)
        if len(adjacency[other]) == 1:
            queue.append(other)

    def outward_vector(node, edge_id):
        """Metre vector pointing away from a junction along an incident edge."""
        edge = edges[edge_id]
        points = edge["points"] if edge["a"] == node else list(reversed(edge["points"]))
        origin = points[0]
        for point in points[1:]:
            dy = (point[1] - origin[1]) * 111320.0
            dx = ((point[0] - origin[0]) * 111320.0
                  * math.cos(math.radians(origin[1])))
            if math.hypot(dx, dy) >= _ROAM_HEADING_DISTANCE_M:
                return dx, dy
        point = points[-1]
        return (
            (point[0] - origin[0]) * 111320.0 * math.cos(math.radians(origin[1])),
            (point[1] - origin[1]) * 111320.0,
        )

    def angle_degrees(first, second):
        denominator = math.hypot(*first) * math.hypot(*second)
        if denominator <= 0:
            return 180.0
        cosine = max(-1.0, min(1.0,
            (first[0] * second[0] + first[1] * second[1]) / denominator))
        return math.degrees(math.acos(cosine))

    # Some OSM junctions use two distinct edge IDs for roads that both leave
    # the node along the same ray (a ramp/freeway nose is a common example).
    # Topologically that is degree two, but visually driving in on one and out
    # on the other is a U-turn over the same corridor. Remove these geometric
    # dead ends, then repeat ordinary leaf pruning after each batch.
    while True:
        cusps = set()
        for node, edge_ids in adjacency.items():
            live = [edge_id for edge_id in edge_ids if edge_id not in removed]
            vectors = {
                edge_id: outward_vector(node, edge_id) for edge_id in live
            }
            for edge_id in live:
                if not any(
                    other != edge_id
                    and angle_degrees(vectors[edge_id], vectors[other])
                        >= _ROAM_MIN_TURN_DEG
                    for other in live
                ):
                    cusps.add(edge_id)
        if not cusps:
            break
        removed.update(cusps)
        for edge_id in cusps:
            edge = edges[edge_id]
            adjacency[edge["a"]].discard(edge_id)
            adjacency[edge["b"]].discard(edge_id)
        queue = [node for node, edge_ids in adjacency.items()
                 if len([edge_id for edge_id in edge_ids if edge_id not in removed]) == 1]
        while queue:
            node = queue.pop()
            live = [edge_id for edge_id in adjacency.get(node, ())
                    if edge_id not in removed]
            if len(live) != 1:
                continue
            edge_id = live[0]
            removed.add(edge_id)
            edge = edges[edge_id]
            other = edge["b"] if edge["a"] == node else edge["a"]
            adjacency[node].discard(edge_id)
            adjacency[other].discard(edge_id)
            if len([candidate for candidate in adjacency[other]
                    if candidate not in removed]) == 1:
                queue.append(other)

    # Leaf pruning alone leaves a second kind of tendril: a cyclic pocket joined
    # to the useful network by one bridge road. A walk can circle the pocket,
    # but its only exit is still the road it entered on. Remove graph bridges,
    # then keep the largest remaining cyclic road component so Roam cannot enter
    # a topological cul-de-sac or get trapped repeating a tiny isolated loop.
    discovery = {}
    low = {}
    bridges = set()
    clock = 0
    parent_node = {}
    parent_edge = {}

    # Iterative Tarjan bridge search. A five-mile suburban graph can contain
    # thousands of junctions; recursive DFS overflows Python's call stack and
    # turned an otherwise valid Roam request into an HTTP 500.
    for root in adjacency:
        if root in discovery or not any(
                edge_id not in removed for edge_id in adjacency[root]):
            continue
        clock += 1
        discovery[root] = low[root] = clock
        parent_node[root] = None
        parent_edge[root] = None
        stack = [(root, iter(adjacency.get(root, ())))]
        while stack:
            node, edge_iterator = stack[-1]
            try:
                edge_id = next(edge_iterator)
            except StopIteration:
                stack.pop()
                edge_to_parent = parent_edge[node]
                parent = parent_node[node]
                if edge_to_parent is not None:
                    low[parent] = min(low[parent], low[node])
                    if low[node] > discovery[parent]:
                        bridges.add(edge_to_parent)
                continue
            if edge_id in removed or edge_id == parent_edge[node]:
                continue
            edge = edges[edge_id]
            other = edge["b"] if edge["a"] == node else edge["a"]
            if other not in discovery:
                parent_node[other] = node
                parent_edge[other] = edge_id
                clock += 1
                discovery[other] = low[other] = clock
                stack.append((other, iter(adjacency.get(other, ()))))
            else:
                low[node] = min(low[node], discovery[other])

    removed.update(bridges)
    without_bridges = {
        node: {edge_id for edge_id in edge_ids if edge_id not in removed}
        for node, edge_ids in adjacency.items()
    }
    without_bridges = {node: edge_ids for node, edge_ids in without_bridges.items()
                       if edge_ids}

    components = []
    seen_nodes = set()
    for first_node in without_bridges:
        if first_node in seen_nodes:
            continue
        stack = [first_node]
        component_nodes = set()
        component_edges = set()
        while stack:
            node = stack.pop()
            if node in component_nodes:
                continue
            component_nodes.add(node)
            seen_nodes.add(node)
            for edge_id in without_bridges.get(node, ()):
                component_edges.add(edge_id)
                edge = edges[edge_id]
                other = edge["b"] if edge["a"] == node else edge["a"]
                if other not in component_nodes:
                    stack.append(other)
        if component_edges:
            components.append((component_edges, component_nodes))

    if not components:
        return None
    kept_edges, kept_nodes = max(components, key=lambda component: len(component[0]))
    topological_adjacency = {
        node: edge_ids & kept_edges
        for node, edge_ids in without_bridges.items()
        if node in kept_nodes and edge_ids & kept_edges
    }

    if not topological_adjacency:
        return None

    # Direction is deliberately applied after undirected leaf/bridge pruning.
    # The topology answers "would this road form a tendril?"; these directed
    # traversal states answer "may a car enter it from this end?".  Keep the
    # largest strongly connected component of the non-backtracking state graph
    # so every allowed arrival always has a legal onward road other than the
    # physical edge just driven.  This is what lets Roam respect one-way roads
    # without introducing a new directed dead end or out-and-back retrace.
    directed_steps = []
    steps_from = {}
    for edge_id in kept_edges:
        edge = edges[edge_id]
        if edge["forward"]:
            step = (edge_id, edge["a"], edge["b"])
            directed_steps.append(step)
            steps_from.setdefault(edge["a"], set()).add(step)
        if edge["backward"]:
            step = (edge_id, edge["b"], edge["a"])
            directed_steps.append(step)
            steps_from.setdefault(edge["b"], set()).add(step)

    transitions = {
        step: {
            candidate for candidate in steps_from.get(step[2], ())
            if candidate[0] != step[0]
        }
        for step in directed_steps
    }
    reverse_transitions = {step: set() for step in directed_steps}
    for step, next_steps in transitions.items():
        for next_step in next_steps:
            reverse_transitions[next_step].add(step)

    # Iterative Kosaraju avoids recursion limits on five-mile city graphs.
    visited = set()
    finish_order = []
    for root in directed_steps:
        if root in visited:
            continue
        visited.add(root)
        stack = [(root, False)]
        while stack:
            step, finishing = stack.pop()
            if finishing:
                finish_order.append(step)
                continue
            stack.append((step, True))
            for next_step in transitions[step]:
                if next_step not in visited:
                    visited.add(next_step)
                    stack.append((next_step, False))

    state_components = []
    assigned = set()
    for root in reversed(finish_order):
        if root in assigned:
            continue
        component = set()
        stack = [root]
        assigned.add(root)
        while stack:
            step = stack.pop()
            component.add(step)
            for previous_step in reverse_transitions[step]:
                if previous_step not in assigned:
                    assigned.add(previous_step)
                    stack.append(previous_step)
        if component and all(transitions[step] & component for step in component):
            state_components.append(component)

    if not state_components:
        return None
    allowed_steps = max(
        state_components,
        key=lambda component: (len({step[0] for step in component}), len(component)),
    )
    live_edge_ids = {step[0] for step in allowed_steps}
    live_nodes = {node for step in allowed_steps for node in step[1:]}
    live_adjacency = {
        node: {
            edge_id for edge_id in topological_adjacency.get(node, ())
            if edge_id in live_edge_ids
        }
        for node in live_nodes
    }
    live_adjacency = {
        node: edge_ids for node, edge_ids in live_adjacency.items() if edge_ids
    }
    outgoing = {}
    for edge_id, origin, _ in allowed_steps:
        outgoing.setdefault(origin, set()).add(edge_id)

    outward_vectors = {
        (node, edge_id): outward_vector(node, edge_id)
        for node, edge_ids in live_adjacency.items()
        for edge_id in edge_ids
    }

    graph = {"edges": edges, "adjacency": live_adjacency,
             "outgoing": outgoing, "allowed_steps": allowed_steps,
             "junction_kinds": junction_kinds, "cache_key": cache_key,
             "outward_vectors": outward_vectors}
    if len(_ROAM_GRAPH_CACHE) >= _ROAM_GRAPH_CACHE_MAX:
        _ROAM_GRAPH_CACHE.pop(next(iter(_ROAM_GRAPH_CACHE)))
    _ROAM_GRAPH_CACHE[cache_key] = graph
    return graph


def _random_walk(graph, start_lat, start_lon, target_km, walk_state=None):
    """Wander the graph from the nearest road: coordinates, speeds, holds."""
    edges, adjacency = graph["edges"], graph["adjacency"]
    outgoing = graph.get("outgoing", adjacency)
    allowed_steps = graph.get("allowed_steps")
    junction_kinds = graph["junction_kinds"]
    outward_vectors = graph.get("outward_vectors", {})
    cos_lat = math.cos(math.radians(start_lat))

    def metres(a, b):
        dy = (a[1] - b[1]) * 111320.0
        dx = (a[0] - b[0]) * 111320.0 * cos_lat
        return math.hypot(dx, dy)

    coordinates = []
    speeds = []
    holds = []
    recent = []
    recent_steps = []
    node_history = {}
    node_history_order = []

    def append_leg(points, ids, speed):
        for point, node_id in zip(points, ids):
            if coordinates and coordinates[-1] == list(point):
                continue
            coordinates.append(list(point))
            if len(coordinates) > 1:
                speeds.append(round(speed, 2))
            kind = junction_kinds.get(node_id)
            if kind:
                holds.append([len(coordinates) - 1, kind])

    # Continue from the exact graph node and recent-edge history returned by
    # the previous chunk. Without this state a new chunk can mistake some other
    # incident edge for the incoming road, randomly choose the real incoming
    # road, and make a visible U-turn exactly at the stitch.
    state_is_valid = False
    if isinstance(walk_state, dict):
        state_node = walk_state.get("node")
        state_recent = walk_state.get("recent_edges")
        state_steps = walk_state.get("recent_steps")
        state_history = walk_state.get("node_history")
        state_key = walk_state.get("graph_key")
        if (isinstance(state_node, int) and not isinstance(state_node, bool)
                and isinstance(state_recent, list)
                and isinstance(state_steps, list)
                and isinstance(state_history, list)
                and state_key == list(graph["cache_key"])
                and state_node in adjacency):
            live_edges = set().union(*adjacency.values())
            cleaned_recent = [
                edge_id for edge_id in state_recent[-_ROAM_RECENT_EDGES:]
                if isinstance(edge_id, int) and not isinstance(edge_id, bool)
                and edge_id in live_edges
            ]
            for item in state_steps[-_ROAM_RECENT_EDGES:]:
                if (not isinstance(item, list) or len(item) != 3
                        or any(not isinstance(value, int) or isinstance(value, bool)
                               for value in item)):
                    continue
                edge_id, origin_node, destination_node = item
                if (edge_id not in live_edges
                        or origin_node not in adjacency
                        or destination_node not in adjacency):
                    continue
                edge = edges[edge_id]
                if ({origin_node, destination_node} != {edge["a"], edge["b"]}
                        or edge_id not in outgoing.get(origin_node, ())
                        or (allowed_steps is not None
                            and (edge_id, origin_node, destination_node)
                            not in allowed_steps)):
                    continue
                recent_steps.append((edge_id, origin_node, destination_node))

            # The final recent step must be a legal directed arrival at this
            # node, not merely an incident edge ID from an older client.
            if (cleaned_recent and recent_steps
                    and cleaned_recent[-1] in adjacency[state_node]
                    and recent_steps[-1][0] == cleaned_recent[-1]
                    and recent_steps[-1][2] == state_node):
                for item in state_history[-_ROAM_RECENT_NODES:]:
                    if (not isinstance(item, list) or len(item) != 3
                            or not isinstance(item[0], int)
                            or isinstance(item[0], bool)):
                        continue
                    history_node, history_incoming, history_outgoing = item
                    if (history_incoming is not None
                            and (not isinstance(history_incoming, int)
                                 or isinstance(history_incoming, bool)
                                 or history_incoming not in live_edges)):
                        continue
                    if (not isinstance(history_outgoing, int)
                            or isinstance(history_outgoing, bool)
                            or history_outgoing not in live_edges
                            or history_node not in adjacency
                            or history_outgoing not in outgoing.get(history_node, ())):
                        continue
                    node_history[history_node] = (history_incoming, history_outgoing)
                    if history_node in node_history_order:
                        node_history_order.remove(history_node)
                    node_history_order.append(history_node)
                incident = edges[next(iter(adjacency[state_node]))]
                point = (incident["points"][0] if incident["a"] == state_node
                         else incident["points"][-1])
                if metres(point, (start_lon, start_lat)) <= 50.0:
                    node = state_node
                    recent = cleaned_recent
                    coordinates.append(list(point))
                    state_is_valid = True

    if not state_is_valid:
        # Initial chunk: snap to the closest vertex of a legally traversable
        # live edge. At a junction there is no incoming road yet, so let the
        # normal random choice consider every legal departure. Mid-edge, follow
        # only an allowed OSM-way direction to its endpoint.
        start = (start_lon, start_lat)
        best = None
        for edge_id, edge in enumerate(edges):
            origins = [
                origin for origin in (edge["a"], edge["b"])
                if edge_id in outgoing.get(origin, ())
            ]
            if not origins:
                continue
            for index, point in enumerate(edge["points"]):
                distance = metres(point, start)
                if best is None or distance < best[0]:
                    best = (distance, edge_id, index, origins)
        if best is None:
            return None
        _, edge_id, index, origins = best
        edge = edges[edge_id]
        if index == 0:
            node = edge["a"]
            coordinates.append(list(edge["points"][0]))
        elif index == len(edge["points"]) - 1:
            node = edge["b"]
            coordinates.append(list(edge["points"][-1]))
        else:
            origin = random.choice(origins)
            if origin == edge["a"]:
                points, ids, node = (
                    edge["points"][index:], edge["ids"][index:], edge["b"]
                )
            else:
                points, ids, node = (
                    list(reversed(edge["points"][:index + 1])),
                    list(reversed(edge["ids"][:index + 1])), edge["a"]
                )
            append_leg(points, ids, edge["speed"])
            recent.append(edge_id)
            recent_steps.append((edge_id, origin, node))

    travelled = 0.0
    for i in range(1, len(coordinates)):
        travelled += metres(tuple(coordinates[i - 1]), tuple(coordinates[i]))

    target_m = target_km * 1000
    # Bounded by edges, not distance, purely as a runaway guard.
    for _ in range(2000):
        if travelled >= target_m:
            break
        incoming = recent[-1] if recent else None
        options = [e for e in outgoing.get(node, ()) if e != incoming]
        if not options:
            # The directed non-backtracking core should make this impossible;
            # never recover by reversing the physical edge just driven.
            break

        if incoming is not None and (node, incoming) in outward_vectors:
            incoming_vector = outward_vectors[(node, incoming)]
            non_uturn = []
            for candidate in options:
                candidate_vector = outward_vectors.get((node, candidate))
                if candidate_vector is None:
                    non_uturn.append(candidate)
                    continue
                denominator = math.hypot(*incoming_vector) * math.hypot(*candidate_vector)
                if denominator <= 0:
                    non_uturn.append(candidate)
                    continue
                cosine = max(-1.0, min(1.0,
                    (incoming_vector[0] * candidate_vector[0]
                     + incoming_vector[1] * candidate_vector[1]) / denominator))
                if math.degrees(math.acos(cosine)) >= _ROAM_MIN_TURN_DEG:
                    non_uturn.append(candidate)
            if non_uturn:
                options = non_uturn

        destinations = {}
        for candidate in options:
            candidate_edge = edges[candidate]
            destinations[candidate] = (
                candidate_edge["b"] if candidate_edge["a"] == node
                else candidate_edge["a"]
            )
        safe_options = [
            candidate for candidate in options
            if not any(
                step_edge == candidate
                and step_origin == destinations[candidate]
                and step_destination == node
                for step_edge, step_origin, step_destination in recent_steps
            )
        ]
        choice_pool = safe_options or options
        previous_turn = node_history.get(node)
        unseen = [
            edge_id for edge_id in choice_pool
            if previous_turn is None or edge_id not in previous_turn
        ]
        fresh = [edge_id for edge_id in unseen if edge_id not in recent]
        if fresh or unseen:
            edge_id = random.choice(fresh or unseen)
        elif previous_turn and previous_turn[1] in choice_pool:
            # We have returned through a short loop and every road is recent.
            # Repeating the prior departure leads back toward unexplored exits;
            # choosing the prior arrival is the visible out-and-back failure.
            edge_id = previous_turn[1]
        else:
            fresh = [edge_id for edge_id in options if edge_id not in recent]
            edge_id = random.choice(fresh or options)
        departed_node = node
        node_history[departed_node] = (incoming, edge_id)
        if departed_node in node_history_order:
            node_history_order.remove(departed_node)
        node_history_order.append(departed_node)
        while len(node_history_order) > _ROAM_RECENT_NODES:
            forgotten = node_history_order.pop(0)
            node_history.pop(forgotten, None)
        edge = edges[edge_id]
        if edge["a"] == node:
            points, ids, node = edge["points"], edge["ids"], edge["b"]
        else:
            points, ids, node = (list(reversed(edge["points"])),
                                 list(reversed(edge["ids"])), edge["a"])
        before = len(coordinates)
        append_leg(points, ids, edge["speed"])
        for i in range(max(1, before), len(coordinates)):
            travelled += metres(tuple(coordinates[i - 1]), tuple(coordinates[i]))
        recent.append(edge_id)
        recent[:] = recent[-_ROAM_RECENT_EDGES:]
        recent_steps.append((edge_id, departed_node, node))
        recent_steps[:] = recent_steps[-_ROAM_RECENT_EDGES:]

    if len(coordinates) < 2:
        return None
    return {
        "provider": "roam",
        "coordinates": coordinates,
        "speeds": speeds,
        "holds": holds,
        "waypoints": [
            {"lat": coordinates[0][1], "lng": coordinates[0][0]},
            {"lat": coordinates[-1][1], "lng": coordinates[-1][0]},
        ],
        "distance_km": round(travelled / 1000, 2),
        "walk_state": {
            "graph_key": list(graph["cache_key"]),
            "node": node,
            "recent_edges": recent,
            "recent_steps": [list(step) for step in recent_steps],
            "node_history": [
                [history_node, node_history[history_node][0], node_history[history_node][1]]
                for history_node in node_history_order
            ],
        },
    }


@app.route("/api/roam/route", methods=["POST"])
def api_roam_route():
    data = request.json or {}
    try:
        lat = float(data["lat"])
        lon = float(data["lon"])
        radius = float(data.get("radius", 1000))
        target_km = float(data.get("target_km", 8))
    except (KeyError, TypeError, ValueError):
        return jsonify({"error": "A centre and radius are required"}), 400
    if not (-90 <= lat <= 90 and -180 <= lon <= 180):
        return jsonify({"error": "Centre is out of range"}), 400

    from_lat = from_lon = None
    if "from_lat" in data or "from_lon" in data:
        try:
            from_lat = float(data["from_lat"])
            from_lon = float(data["from_lon"])
        except (KeyError, TypeError, ValueError):
            return jsonify({"error": "Both current-position coordinates are required"}), 400
        if not (-90 <= from_lat <= 90 and -180 <= from_lon <= 180):
            return jsonify({"error": "Current position is out of range"}), 400

    if not _search_allowed():
        return jsonify({"error": "Too many requests, slow down"}), 429

    walk_state = data.get("walk_state")
    if walk_state is not None:
        if (not isinstance(walk_state, dict)
                or not isinstance(walk_state.get("graph_key"), list)
                or not isinstance(walk_state.get("node"), int)
                or isinstance(walk_state.get("node"), bool)
                or not isinstance(walk_state.get("recent_edges"), list)
                or len(walk_state["recent_edges"]) > _ROAM_RECENT_EDGES
                or any(not isinstance(edge_id, int) or isinstance(edge_id, bool)
                       for edge_id in walk_state["recent_edges"])
                or not isinstance(walk_state.get("recent_steps"), list)
                or len(walk_state["recent_steps"]) > _ROAM_RECENT_EDGES
                or any(
                    not isinstance(item, list) or len(item) != 3
                    or any(not isinstance(value, int) or isinstance(value, bool)
                           for value in item)
                    for item in walk_state["recent_steps"]
                )
                or not isinstance(walk_state.get("node_history"), list)
                or len(walk_state["node_history"]) > _ROAM_RECENT_NODES
                or any(
                    not isinstance(item, list) or len(item) != 3
                    or not isinstance(item[0], int) or isinstance(item[0], bool)
                    or (item[1] is not None
                        and (not isinstance(item[1], int) or isinstance(item[1], bool)))
                    or not isinstance(item[2], int) or isinstance(item[2], bool)
                    for item in walk_state["node_history"]
                )):
            return jsonify({"error": "Roam continuation state is invalid"}), 400

    # Wander the road graph turn by turn. If every Overpass mirror is down,
    # fail this chunk so the active client retries; never substitute the old
    # scattered-waypoint tour, which is not a random walk.
    target = max(0.35, min(target_km, 60.0))
    graph = _fetch_road_graph(lat, lon, radius)
    if not graph:
        return jsonify({
            "error": "Road graph is temporarily unavailable. Trying again shortly."
        }), 503
    walk_lat = from_lat if from_lat is not None else lat
    walk_lon = from_lon if from_lon is not None else lon
    route = _random_walk(graph, walk_lat, walk_lon, target, walk_state)
    if not route:
        return jsonify({"error": "No roads found in that area. Try a larger radius."}), 404
    return jsonify(route)


# ── Adaptive speed ─────────────────────────────────────────────
# Google's posted speed limits live behind the Roads API, which needs a paid
# key and is restricted to Asset Tracking customers, so they are not available
# to us. OpenStreetMap tags the same information as `maxspeed`, free and
# keyless, along with stop signs and traffic signals — which is what makes it
# possible to stop at a junction rather than only slow down for a slower road.

_MPH_TO_KMH = 1.609344
_ADAPTIVE_DEFAULT_KMH = 48.0        # ~30 mph where nothing is tagged

# Most residential streets carry no `maxspeed` tag in OSM, which left realistic
# mode with too little data and made it give up. Fall back to a typical limit
# for the road's class (US defaults, mph) so every driveable road has a speed.
_CLASS_DEFAULT_KMH = {
    highway: mph * _MPH_TO_KMH for highway, mph in {
        "motorway": 65, "motorway_link": 45, "trunk": 55, "trunk_link": 40,
        "primary": 45, "primary_link": 30, "secondary": 40, "secondary_link": 30,
        "tertiary": 35, "tertiary_link": 25, "unclassified": 30, "residential": 25,
        "living_street": 15, "service": 15, "road": 30,
    }.items()
}
# A stop sign or signal that governs the driven road sits ON its OSM way, so it
# lands within a few metres of the route line. A generous radius here matched
# cross-street stop signs — the ones facing the side road, not the driver — and
# made realistic mode halt at nearly every residential intersection.
_JUNCTION_RADIUS_M = 10.0
_HOLD_DEDUP_M = 25.0
_MATCH_RADIUS_M = 35.0
_ALLOWED_HOLD_KINDS = frozenset(("signal", "stop", "waypoint"))
_MAX_ROUTE_HOLDS = 1000


def _parse_maxspeed(value):
    """OSM maxspeed is free text: '50', '35 mph', 'RU:urban'. Numbers only."""
    if not value:
        return None
    text = str(value).strip().lower()
    match = re.match(r"^(\d+(?:\.\d+)?)\s*(mph|km/h|kmh)?$", text)
    if not match:
        return None
    number = float(match.group(1))
    if number <= 0:
        return None
    return number * _MPH_TO_KMH if match.group(2) == "mph" else number


# The main Overpass instance frequently returns 504/429 under load. A single
# failure there used to make adaptive silently fall back to a flat speed (a
# 45 mph road driven at the slider's default), so try several public mirrors in
# turn and take the first that answers.
_OVERPASS_ENDPOINTS = (
    "https://overpass-api.de/api/interpreter",
    "https://overpass.kumi.systems/api/interpreter",
    "https://overpass.private.coffee/api/interpreter",
    "https://maps.mail.ru/osm/tools/overpass/api/interpreter",
)


def _overpass_elements(query):
    """Return the elements for an Overpass query, trying each mirror in turn, or
    None if every mirror fails."""
    for url in _OVERPASS_ENDPOINTS:
        try:
            response = http_requests.post(
                url,
                data={"data": query},
                headers={"User-Agent": "Ghostpin/2.0 (https://github.com/Kron00/ghostpin)"},
                timeout=25,
            )
            response.raise_for_status()
            elements = response.json().get("elements", [])
            if isinstance(elements, list):
                return elements
        except (http_requests.RequestException, ValueError):
            continue
    return None


def _fetch_speed_profile(coordinates, fallback_kmh):
    """Return segment speeds and junction holds, or None if both are unavailable.

    One Overpass query for the route's bounding box, then nearest-match each
    coordinate. The speed list may be None while holds are still known. Errors
    return None rather than stopping a route from running.
    """
    try:
        if not isinstance(coordinates, list) or len(coordinates) < 2:
            return None
        route_points = []
        for point in coordinates:
            if not isinstance(point, (list, tuple)) or len(point) < 2:
                return None
            lon, lat = float(point[0]), float(point[1])
            if not (math.isfinite(lat) and math.isfinite(lon)):
                return None
            route_points.append((lon, lat))

        lats = [point[1] for point in route_points]
        lons = [point[0] for point in route_points]
        south, north, west, east = min(lats), max(lats), min(lons), max(lons)
        # A continent-sized bbox would return an unusable amount of data.
        if (north - south) > 1.2 or (east - west) > 1.2:
            return None

        pad = 0.003
        bbox = f"{south - pad},{west - pad},{north + pad},{east + pad}"
        query = (
            "[out:json][timeout:25];("
            f'way[highway~"^(motorway|trunk|primary|secondary|tertiary|unclassified|residential|living_street|service|road)(_link)?$"]({bbox});'
            f'node[highway~"^(stop|give_way|traffic_signals)$"]({bbox});'
            ");out geom;"
        )
        response = http_requests.post(
            "https://overpass-api.de/api/interpreter",
            data={"data": query},
            headers={"User-Agent": "Ghostpin/2.0 (https://github.com/Kron00/ghostpin)"},
            timeout=25,
        )
        response.raise_for_status()
        elements = response.json().get("elements", [])
        if not isinstance(elements, list):
            return None

        # Bucket everything into a coarse grid so matching stays linear.
        cell = 0.004
        ways = {}
        route_grid = {}
        junctions = []
        seen_junctions = set()

        def key(lat, lon):
            return (int(lat / cell), int(lon / cell))

        for index, (lon, lat) in enumerate(route_points):
            route_grid.setdefault(key(lat, lon), []).append((index, lat, lon))

        for element in elements:
            if not isinstance(element, dict):
                continue
            if element.get("type") == "way":
                tags = element.get("tags") or {}
                speed = _parse_maxspeed(tags.get("maxspeed"))
                if not speed:
                    # No posted limit — assume the class's typical limit, or skip
                    # anything not driveable (footway, cycleway, path…).
                    speed = _CLASS_DEFAULT_KMH.get(tags.get("highway"))
                if not speed:
                    continue
                # Densify along each segment so a route point sitting on this
                # road matches even where the way's own vertices are far apart —
                # long straights otherwise left most of the route "untagged" and
                # tripped the sparse-coverage rejection below.
                previous = None
                for point in element.get("geometry", []) or []:
                    way_lat, way_lon = float(point["lat"]), float(point["lon"])
                    if previous is not None:
                        plat, plon = previous
                        dlat = (way_lat - plat) * 111320.0
                        dlon = (way_lon - plon) * 111320.0 * math.cos(math.radians(plat))
                        steps = min(50, int(math.hypot(dlat, dlon) // 20.0))
                        for s in range(1, steps + 1):
                            fraction = s / (steps + 1)
                            ilat = plat + (way_lat - plat) * fraction
                            ilon = plon + (way_lon - plon) * fraction
                            ways.setdefault(key(ilat, ilon), []).append((ilat, ilon, speed))
                    ways.setdefault(key(way_lat, way_lon), []).append(
                        (way_lat, way_lon, speed)
                    )
                    previous = (way_lat, way_lon)
            elif element.get("type") == "node":
                highway = (element.get("tags") or {}).get("highway")
                if highway not in ("stop", "give_way", "traffic_signals"):
                    continue
                node_lat = float(element["lat"])
                node_lon = float(element["lon"])
                identity = element.get("id")
                if identity is None:
                    identity = (round(node_lat, 7), round(node_lon, 7), highway)
                if identity in seen_junctions:
                    continue
                seen_junctions.add(identity)
                kind = "signal" if highway == "traffic_signals" else "stop"
                junctions.append((node_lat, node_lon, kind))

        def neighbours(grid, lat, lon):
            base = key(lat, lon)
            for dy in (-1, 0, 1):
                for dx in (-1, 0, 1):
                    for entry in grid.get((base[0] + dy, base[1] + dx), []):
                        yield entry

        def metres(lat1, lon1, lat2, lon2):
            dlat = (lat2 - lat1) * 111320.0
            dlon = (lon2 - lon1) * 111320.0 * math.cos(math.radians(lat1))
            return math.hypot(dlat, dlon)

        hold_candidates = []
        for node_lat, node_lon, kind in junctions:
            best_index, best_distance = None, _JUNCTION_RADIUS_M
            for index, route_lat, route_lon in neighbours(route_grid, node_lat, node_lon):
                distance = metres(node_lat, node_lon, route_lat, route_lon)
                if distance < best_distance:
                    best_index, best_distance = index, distance
            if best_index is not None:
                hold_candidates.append([best_index, kind])

        cumulative = [0.0]
        for (lon1, lat1), (lon2, lat2) in zip(route_points, route_points[1:]):
            cumulative.append(cumulative[-1] + metres(lat1, lon1, lat2, lon2))

        holds = []
        for index, kind in sorted(hold_candidates, key=lambda hold: hold[0]):
            if holds and cumulative[index] - cumulative[holds[-1][0]] < _HOLD_DEDUP_M:
                # A signal is the more specific description when OSM carries
                # several controls for one physical junction.
                if kind == "signal" and holds[-1][1] != "signal":
                    holds[-1] = [index, kind]
                continue
            holds.append([index, kind])

        speeds = None
        if ways:
            base_speed = (fallback_kmh if fallback_kmh and fallback_kmh > 0
                          else _ADAPTIVE_DEFAULT_KMH)
            matched_speeds = []
            tagged = 0

            for lon, lat in route_points[:-1]:
                best_speed, best_distance = None, _MATCH_RADIUS_M
                for way_lat, way_lon, speed in neighbours(ways, lat, lon):
                    distance = metres(lat, lon, way_lat, way_lon)
                    if distance < best_distance:
                        best_distance, best_speed = distance, speed
                if best_speed:
                    tagged += 1
                matched_speeds.append(round(best_speed or base_speed, 2))

            # Sparse tags are not enough to claim adaptive speed, but known
            # junctions remain useful independently of road-limit coverage.
            if tagged >= max(3, len(matched_speeds) * 0.1):
                # The motion engine already plans acceleration between segments;
                # averaging here could raise a low posted ceiling above its limit.
                speeds = matched_speeds

        if speeds is None and not holds:
            return None
        return speeds, holds
    except Exception:
        return None


def _validate_route_holds(value, coordinates):
    if value is None:
        return []
    if not isinstance(value, list):
        raise ValueError("Holds must be a list of [coordinate index, kind]")
    if len(value) > _MAX_ROUTE_HOLDS:
        raise ValueError(f"A route can have at most {_MAX_ROUTE_HOLDS} holds")
    if value and not isinstance(coordinates, list):
        raise ValueError("Holds require calculated route coordinates")

    holds = []
    for hold in value:
        if not isinstance(hold, list) or len(hold) != 2:
            raise ValueError("Each hold must be [coordinate index, kind]")
        index, kind = hold
        if isinstance(index, bool) or not isinstance(index, int):
            raise ValueError("Each hold coordinate index must be an integer")
        if not isinstance(kind, str) or kind not in _ALLOWED_HOLD_KINDS:
            raise ValueError("Hold kind must be signal, stop, or waypoint")
        if not 0 <= index < len(coordinates):
            raise ValueError("Hold coordinate index is outside the route")
        holds.append([index, kind])
    return holds


def _waypoint_holds(stop_indices, coordinates):
    if stop_indices is None:
        return []
    if not isinstance(stop_indices, list):
        raise ValueError("Stop indices must be a list of coordinate indices")
    if len(stop_indices) > _MAX_ROUTE_HOLDS:
        raise ValueError(f"A route can have at most {_MAX_ROUTE_HOLDS} holds")
    if stop_indices and not isinstance(coordinates, list):
        raise ValueError("Stop indices require calculated route coordinates")

    holds = []
    for index in stop_indices:
        if isinstance(index, bool) or not isinstance(index, int):
            raise ValueError("Each stop index must be an integer")
        if not 0 <= index < len(coordinates):
            raise ValueError("Stop index is outside the route")
        holds.append([index, "waypoint"])
    return holds


def _merge_route_holds(coordinates, *groups):
    """Merge ordered hold sources, preserving the earlier source on overlap."""
    candidates = []
    sequence = 0
    for priority, group in enumerate(groups):
        for index, kind in group:
            candidates.append([index, kind, priority, sequence])
            sequence += 1
    if not candidates:
        return []

    cumulative = [0.0]
    for first, second in zip(coordinates, coordinates[1:]):
        lon1, lat1 = float(first[0]), float(first[1])
        lon2, lat2 = float(second[0]), float(second[1])
        cumulative.append(
            cumulative[-1] + _haversine_km(lat1, lon1, lat2, lon2) * 1000
        )

    merged = []
    for candidate in sorted(candidates, key=lambda item: (item[0], item[3])):
        if merged and cumulative[candidate[0]] - cumulative[merged[-1][0]] < _HOLD_DEDUP_M:
            if candidate[2] < merged[-1][2]:
                merged[-1] = candidate
            continue
        merged.append(candidate)

    # Explicit and waypoint holds outrank adaptive controls if a very long
    # urban route would otherwise exceed the runner's defensive bound.
    if len(merged) > _MAX_ROUTE_HOLDS:
        merged = sorted(merged, key=lambda item: (item[2], item[0]))[:_MAX_ROUTE_HOLDS]
        merged.sort(key=lambda item: item[0])
    return [[index, kind] for index, kind, _, _ in merged]


@app.route("/api/route/calculate", methods=["POST"])
def api_route_calculate():
    data = request.json or {}

    # Preferred form is a list of stops; origin/destination is still accepted.
    stops = data.get("stops")
    if not isinstance(stops, list):
        stops = [data.get("origin", ""), data.get("destination", "")]
    stops = [str(stop).strip() for stop in stops]
    stops = [stop for stop in stops if stop]

    if len(stops) < 2:
        return jsonify({"error": "Enter at least a start and a destination"}), 400
    if len(stops) > 10:
        return jsonify({"error": "A route can have at most 10 stops"}), 400
    if any(len(stop) > 200 for stop in stops):
        return jsonify({"error": "Address is too long"}), 400

    if not _search_allowed():
        return jsonify({"error": "Too many requests, slow down"}), 429

    try:
        return jsonify(_google_multi_stop_route(stops))
    except (ValueError, json.JSONDecodeError) as exc:
        return jsonify({"error": str(exc)}), 400
    except http_requests.RequestException:
        return jsonify({"error": "Google Maps route service is unavailable"}), 502




@app.route("/api/route/start", methods=["POST"])
def api_route_start():
    err = _check_ready()
    if err:
        return err
    data = request.json or {}
    waypoints = data.get("waypoints", [])
    speed = data.get("speed", 5)
    mode = data.get("mode", "once")
    randomize = data.get("randomize_speed", False)
    coordinates = data.get("coordinates")
    try:
        speed = float(speed)
    except (TypeError, ValueError):
        speed = 5

    try:
        client_holds = _validate_route_holds(data.get("holds"), coordinates)
        waypoint_holds = _waypoint_holds(data.get("stop_indices"), coordinates)
        if len(client_holds) + len(waypoint_holds) > _MAX_ROUTE_HOLDS:
            raise ValueError(f"A route can have at most {_MAX_ROUTE_HOLDS} holds")
        gps_noise = data.get("gps_noise", True)
        if not isinstance(gps_noise, bool):
            raise ValueError("GPS noise must be true or false")
        client_speeds = data.get("speeds")
        if client_speeds is not None:
            if (not isinstance(client_speeds, list) or not coordinates
                    or len(client_speeds) != len(coordinates) - 1):
                raise ValueError("Speed profile must contain one speed per route segment")
            client_speeds = [float(value) for value in client_speeds]
            if any(not math.isfinite(v) or not 1 <= v <= 300 for v in client_speeds):
                raise ValueError("Speed profile values must be between 1 and 300 km/h")
    except (TypeError, ValueError) as exc:
        return jsonify({"error": str(exc) or "Invalid route request"}), 400

    speeds = None
    adaptive_holds = []
    adaptive_used = False
    if data.get("adaptive") and coordinates:
        # A roam walk already knows each road's limit from the graph; only
        # fetch a profile when the caller could not supply one.
        if client_speeds is not None:
            speeds = client_speeds
            adaptive_used = True
        else:
            profile = _fetch_speed_profile(coordinates, speed)
            if profile is not None:
                speeds, adaptive_holds = profile
                adaptive_used = speeds is not None

    try:
        holds = _merge_route_holds(
            coordinates, waypoint_holds, client_holds, adaptive_holds
        )
        result = loc_svc.start_route(
            waypoints,
            speed_kmh=speed,
            mode=mode,
            randomize_speed=randomize,
            coordinates=coordinates,
            provider=data.get("provider") or "osrm",
            speeds=speeds,
            holds=holds,
            gps_noise=gps_noise,
        )
        result["adaptive"] = adaptive_used
        return jsonify(result)
    except ValueError as e:
        return jsonify({"error": str(e)}), 400
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/movement/speed", methods=["POST"])
def api_movement_speed():
    err = _check_ready()
    if err:
        return err
    data = request.json or {}
    try:
        speed = float(data.get("speed_kmh"))
        return jsonify(loc_svc.set_movement_speed(speed))
    except (TypeError, ValueError) as exc:
        return jsonify({"error": str(exc) or "Enter a valid speed"}), 400


@app.route("/api/route/stop", methods=["POST"])
def api_route_stop():
    err = _check_ready()
    if err:
        return err
    return jsonify(loc_svc.stop_route())


@app.route("/api/route/pause", methods=["POST"])
def api_route_pause():
    err = _check_ready()
    if err:
        return err
    return jsonify(loc_svc.pause_route())


@app.route("/api/route/resume", methods=["POST"])
def api_route_resume():
    err = _check_ready()
    if err:
        return err
    return jsonify(loc_svc.resume_route())


@app.route("/api/route/status")
def api_route_status():
    if loc_svc is None:
        return jsonify({"error": "No iPhone connected. Plug it in, unlock it, and try again."}), 503
    status = loc_svc.get_route_status()
    if status is None:
        return jsonify({"error": "No route active"}), 404
    return jsonify(status)


@app.route("/api/route/circular", methods=["POST"])
def api_route_circular():
    err = _check_ready()
    if err:
        return err
    data = request.json or {}
    try:
        lat = float(data["lat"])
        lon = float(data["lon"])
        radius = float(data.get("radius", 200))
        points = int(data.get("points", 36))
    except (KeyError, TypeError, ValueError):
        return jsonify({"error": "lat, lon, and radius are required"}), 400

    waypoints = loc_svc.generate_circular_route(lat, lon, radius, points)
    return jsonify({"waypoints": waypoints, "count": len(waypoints)})


# ── Wander API ─────────────────────────────────────────────────

@app.route("/api/wander/start", methods=["POST"])
def api_wander_start():
    err = _check_ready()
    if err:
        return err
    data = request.json or {}
    try:
        lat = float(data["lat"])
        lon = float(data["lon"])
        radius = float(data.get("radius", 100))
        speed = float(data.get("speed", 5))
    except (KeyError, TypeError, ValueError):
        return jsonify({"error": "lat and lon are required"}), 400
    try:
        result = loc_svc.start_wander(lat, lon, radius, speed)
        return jsonify(result)
    except ValueError as e:
        return jsonify({"error": str(e)}), 400
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/wander/stop", methods=["POST"])
def api_wander_stop():
    err = _check_ready()
    if err:
        return err
    return jsonify(loc_svc.stop_wander())


@app.route("/api/wander/status")
def api_wander_status():
    if loc_svc is None:
        return jsonify({"active": False})
    return jsonify(loc_svc.get_wander_status())


# ── GPX API ────────────────────────────────────────────────────

@app.route("/api/gpx/import", methods=["POST"])
def api_gpx_import():
    err = _check_ready()
    if err:
        return err
    # Accept either file upload or raw body
    if request.files and "file" in request.files:
        content = request.files["file"].read().decode("utf-8")
    else:
        content = request.get_data(as_text=True)
    if not content:
        return jsonify({"error": "No GPX data provided"}), 400
    try:
        result = loc_svc.import_gpx(content)
        return jsonify(result)
    except ValueError as e:
        return jsonify({"error": str(e)}), 400
    except Exception as e:
        return jsonify({"error": f"Failed to parse GPX: {e}"}), 400


@app.route("/api/gpx/export")
def api_gpx_export():
    if loc_svc is None:
        return jsonify({"error": "No iPhone connected. Plug it in, unlock it, and try again."}), 503
    name = request.args.get("name", "Ghostpin Route")
    gpx_str = loc_svc.export_gpx(name)
    return app.response_class(gpx_str, mimetype="application/gpx+xml",
                              headers={"Content-Disposition": f"attachment; filename=route.gpx"})


# ── Saved locations API ───────────────────────────────────────

@app.route("/api/saved")
def api_saved_list():
    if loc_svc is None:
        return jsonify([])
    category = request.args.get("category")
    locs = loc_svc.get_saved()
    if category:
        locs = [l for l in locs if l.get("category", "default") == category]
    return jsonify(locs)


@app.route("/api/saved", methods=["POST"])
def api_saved_add():
    if loc_svc is None:
        return jsonify({"error": "No iPhone connected. Plug it in, unlock it, and try again."}), 503
    data = request.json or {}
    name = data.get("name", "").strip()
    if not name:
        return jsonify({"error": "Name is required"}), 400
    try:
        lat = float(data["lat"])
        lon = float(data["lon"])
    except (KeyError, TypeError, ValueError):
        return jsonify({"error": "lat and lon are required"}), 400
    category = data.get("category", "default")
    result = loc_svc.save_location(name, lat, lon, category)
    return jsonify(result)


@app.route("/api/saved/<path:name>", methods=["DELETE"])
def api_saved_delete(name):
    if loc_svc is None:
        return jsonify({"error": "No iPhone connected. Plug it in, unlock it, and try again."}), 503
    return jsonify(loc_svc.delete_location(name))


@app.route("/api/saved/categories")
def api_saved_categories():
    if loc_svc is None:
        return jsonify([])
    return jsonify(loc_svc.get_categories())


# ── History API ────────────────────────────────────────────────

@app.route("/api/history")
def api_history():
    if loc_svc is None:
        return jsonify([])
    return jsonify(loc_svc.get_history())


@app.route("/api/history", methods=["DELETE"])
def api_history_clear():
    if loc_svc is None:
        return jsonify({"error": "No iPhone connected. Plug it in, unlock it, and try again."}), 503
    return jsonify(loc_svc.clear_history())


# ── Route history API ─────────────────────────────────

@app.route("/api/routes")
def api_routes_list():
    if loc_svc is None:
        return jsonify([])
    return jsonify(loc_svc.get_routes())


@app.route("/api/routes", methods=["POST"])
def api_routes_save():
    if loc_svc is None:
        return jsonify({"error": "No iPhone connected. Plug it in, unlock it, and try again."}), 503
    data = request.json or {}
    name = data.get("name", "").strip()
    if not name:
        return jsonify({"error": "Name is required"}), 400
    waypoints = data.get("waypoints", [])
    if len(waypoints) < 2:
        return jsonify({"error": "Need at least 2 waypoints"}), 400
    speed = data.get("speed", 5)
    mode = data.get("mode", "once")
    distance = data.get("distance_km", 0)
    return jsonify(loc_svc.save_route(name, waypoints, speed, mode, distance))


@app.route("/api/routes/<route_id>", methods=["DELETE"])
def api_routes_delete(route_id):
    if loc_svc is None:
        return jsonify({"error": "No iPhone connected. Plug it in, unlock it, and try again."}), 503
    return jsonify(loc_svc.delete_route(route_id))


# ── Profiles API ───────────────────────────────────────────────

@app.route("/api/profiles")
def api_profiles_list():
    if loc_svc is None:
        return jsonify([])
    return jsonify(loc_svc.get_profiles())


@app.route("/api/profiles", methods=["POST"])
def api_profiles_save():
    if loc_svc is None:
        return jsonify({"error": "No iPhone connected. Plug it in, unlock it, and try again."}), 503
    data = request.json or {}
    name = data.get("name", "").strip()
    if not name:
        return jsonify({"error": "Name is required"}), 400
    return jsonify(loc_svc.save_profile(name, data))


@app.route("/api/profiles/<path:name>/load", methods=["POST"])
def api_profiles_load(name):
    err = _check_ready()
    if err:
        return err
    try:
        return jsonify(loc_svc.load_profile(name))
    except ValueError as e:
        return jsonify({"error": str(e)}), 404


@app.route("/api/profiles/<path:name>", methods=["DELETE"])
def api_profiles_delete(name):
    if loc_svc is None:
        return jsonify({"error": "No iPhone connected. Plug it in, unlock it, and try again."}), 503
    return jsonify(loc_svc.delete_profile(name))


# ── Schedules API ──────────────────────────────────────────────

@app.route("/api/schedules")
def api_schedules_list():
    if loc_svc is None:
        return jsonify([])
    return jsonify(loc_svc.get_schedules())


@app.route("/api/schedules", methods=["POST"])
def api_schedules_create():
    if loc_svc is None:
        return jsonify({"error": "No iPhone connected. Plug it in, unlock it, and try again."}), 503
    data = request.json or {}
    name = data.get("name", "").strip()
    if not name:
        return jsonify({"error": "Name is required"}), 400
    try:
        lat = float(data["lat"])
        lon = float(data["lon"])
    except (KeyError, TypeError, ValueError):
        return jsonify({"error": "lat and lon are required"}), 400
    time_str = data.get("time", "")
    days = data.get("days", None)
    return jsonify(loc_svc.save_schedule(name, lat, lon, time_str, days))


@app.route("/api/schedules/<schedule_id>", methods=["DELETE"])
def api_schedules_delete(schedule_id):
    if loc_svc is None:
        return jsonify({"error": "No iPhone connected. Plug it in, unlock it, and try again."}), 503
    return jsonify(loc_svc.delete_schedule(schedule_id))


@app.route("/api/schedules/<schedule_id>/toggle", methods=["POST"])
def api_schedules_toggle(schedule_id):
    if loc_svc is None:
        return jsonify({"error": "No iPhone connected. Plug it in, unlock it, and try again."}), 503
    data = request.json or {}
    return jsonify(loc_svc.toggle_schedule(schedule_id, data.get("enabled", True)))


# ── Schedule checker ───────────────────────────────────────────

def _start_schedule_checker():
    global _schedule_thread, _schedule_active
    if _schedule_active:
        return
    _schedule_active = True
    _schedule_thread = threading.Thread(target=_schedule_loop, daemon=True)
    _schedule_thread.start()


def _schedule_loop():
    """Check schedules every 30 seconds."""
    last_check_minute = None
    while _schedule_active:
        if loc_svc:
            import datetime
            now = datetime.datetime.now()
            current_minute = now.strftime("%H:%M")
            if current_minute != last_check_minute:
                last_check_minute = current_minute
                try:
                    loc_svc.check_schedules()
                except Exception:
                    pass
        time.sleep(30)


# ── API docs ───────────────────────────────────────────────────

@app.route("/api")
def api_docs():
    """Simple API documentation."""
    endpoints = []
    for rule in app.url_map.iter_rules():
        if rule.rule.startswith("/api"):
            endpoints.append({
                "path": rule.rule,
                "methods": sorted(rule.methods - {"OPTIONS", "HEAD"}),
            })
    endpoints.sort(key=lambda e: e["path"])
    return jsonify({"endpoints": endpoints, "version": "2.1.0"})


# ── CLI Main (for start.sh usage) ─────────────────────────────

def main():
    global device_mgr, loc_svc

    print()
    print("=" * 50)
    print("  Ghostpin")
    print("=" * 50)
    print()

    device_mgr = DeviceManager()

    print("[*] Looking for device...")
    try:
        info = device_mgr.connect(retries=5)
        print(f"    UDID: {info['udid']}")
        loc_svc = LocationService(device_mgr.simulator, device_mgr.bridge)
        _start_schedule_checker()
        print("[+] Device connected")
    except Exception:
        print("[*] No device found yet — connect from the UI")
        loc_svc = None

    print()
    print(f"[+] Ready! Open http://localhost:{PORT} in your browser")
    print("    Press Ctrl+C to stop")
    print()

    app.run(host="127.0.0.1", port=PORT, debug=False)


if __name__ == "__main__":
    main()
