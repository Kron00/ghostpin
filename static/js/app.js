// NOTE: The one remaining innerHTML usage (search item icon SVG) uses only
// static markup — no user input is interpolated. All user-supplied text uses
// textContent or DOM creation methods to prevent XSS.

// ── State ────────────────────────────────────────────────────
let map, marker, userLocationMarker, routeLine, routeDisplayLine;
let routePoints = [];
let routeMarkers = [];
let routePolling = null;
let searchTimeout = null;
let selectedSpeed = 15;
let darkTiles = true;
let teleportMode = false;
let recentLocations = [];
let coordFormat = localStorage.getItem("coord_fmt") || "dd";
let lightTheme = localStorage.getItem("theme") === "light";
let previousLocation = null;
let followMode = false;
let movementPolling = null;
let searchHighlightIndex = -1;
let searchAbortController = null;
let startupLocation = null;
let routeDistanceKm = 0;
let routeTraveledLine = null;
let trailPoints = [];
let calculatedRouteCoordinates = null;
let calculatedRouteProvider = null;
let activeSpoofLocation = null;
let speedUnit = localStorage.getItem("speed_unit") === "mph" ? "mph" : "kmh";
let speedUpdateTimer = null;

const KMH_PER_MPH = 1.609344;

function routeFollowZoomForSpeed(speed) {
    if (speed <= 7) return 18;
    if (speed <= 25) return 17;
    return 16;
}

function displaySpeedFromKmh(speedKmh) {
    const value = speedUnit === "mph" ? speedKmh / KMH_PER_MPH : speedKmh;
    return Math.abs(value - Math.round(value)) < 0.05 ? String(Math.round(value)) : value.toFixed(1);
}

function readSpeedKmh() {
    const value = parseFloat($("speed-input")?.value);
    if (!Number.isFinite(value) || value <= 0) return null;
    return speedUnit === "mph" ? value * KMH_PER_MPH : value;
}

function setSpeedInputFromKmh(speedKmh) {
    const input = $("speed-input");
    if (!input) return;
    input.value = displaySpeedFromKmh(speedKmh);
    input.min = speedUnit === "mph" ? "0.6" : "1";
    input.max = speedUnit === "mph" ? "186.4" : "300";
    input.step = "0.1";
}

function updateSpeedPresetState() {
    document.querySelectorAll(".seg-btn").forEach(button => {
        button.classList.toggle("active", Math.abs(parseFloat(button.dataset.speed) - selectedSpeed) < 0.05);
    });
}

function setSelectedSpeed(speedKmh, updateInput = true) {
    selectedSpeed = Math.max(1, Math.min(300, speedKmh));
    if (updateInput) setSpeedInputFromKmh(selectedSpeed);
    updateSpeedPresetState();
    updateStatusBar();
}

function formatSpeed(speedKmh) {
    return displaySpeedFromKmh(speedKmh) + " " + (speedUnit === "mph" ? "mph" : "km/h");
}

function formatRouteDistance(distanceKm) {
    return speedUnit === "mph" ? (distanceKm * 0.621371).toFixed(1) + " mi" : distanceKm.toFixed(1) + " km";
}

function queueLiveSpeedUpdate(speedKmh, immediate = false) {
    clearTimeout(speedUpdateTimer);
    const send = async () => {
        try {
            const response = await fetch("/api/movement/speed", {
                method: "POST",
                headers: { "Content-Type": "application/json" },
                body: JSON.stringify({ speed_kmh: speedKmh }),
            });
            const data = await response.json();
            if (!response.ok) throw new Error(data.error || "Speed update failed");
            if (data.active?.length) toast("Speed updated live: " + formatSpeed(data.speed_kmh));
        } catch (error) {
            if (routePolling || movementPolling) toast(error.message || "Speed update failed", "error");
        }
    };
    if (immediate) send();
    else speedUpdateTimer = setTimeout(send, 180);
}

function toggleSpeedUnit() {
    const speedKmh = selectedSpeed;
    speedUnit = speedUnit === "kmh" ? "mph" : "kmh";
    localStorage.setItem("speed_unit", speedUnit);
    setSelectedSpeed(speedKmh);
    const toggle = $("speed-unit-toggle");
    toggle.textContent = speedUnit === "mph" ? "mph" : "km/h";
    toggle.title = speedUnit === "mph" ? "Switch speed units to kilometers per hour" : "Switch speed units to miles per hour";
    loadRouteHistory();
}

const TILES = {
    dark: {
        url: "https://{s}.basemaps.cartocdn.com/dark_all/{z}/{x}/{y}{r}.png",
        attr: '&copy; <a href="https://carto.com/">CARTO</a> &copy; <a href="https://osm.org/">OSM</a>',
    },
    light: {
        url: "https://{s}.basemaps.cartocdn.com/light_all/{z}/{x}/{y}{r}.png",
        attr: '&copy; <a href="https://carto.com/">CARTO</a> &copy; <a href="https://osm.org/">OSM</a>',
    },
};

const POPULAR = [
    { name: "Times Square, NYC", lat: 40.7580, lon: -73.9855 },
    { name: "Eiffel Tower, Paris", lat: 48.8584, lon: 2.2945 },
    { name: "Tokyo Tower, Japan", lat: 35.6586, lon: 139.7454 },
    { name: "Big Ben, London", lat: 51.5007, lon: -0.1246 },
    { name: "Sydney Opera House", lat: -33.8568, lon: 151.2153 },
    { name: "Statue of Liberty, NYC", lat: 40.6892, lon: -74.0445 },
    { name: "Colosseum, Rome", lat: 41.8902, lon: 12.4922 },
    { name: "Dubai Mall, UAE", lat: 25.1972, lon: 55.2744 },
];

let tileLayer;

// Static SVG for search results (no user data)
const SEARCH_ICON_SVG = '<svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M21 10c0 7-9 13-9 13s-9-6-9-13a9 9 0 0118 0z"/><circle cx="12" cy="10" r="3"/></svg>';

// ── Init ─────────────────────────────────────────────────────
document.addEventListener("DOMContentLoaded", async () => {
    if (lightTheme) document.body.classList.add("light");
    if (!localStorage.getItem("ob_done")) {
        $("onboarding").classList.remove("hidden");
    } else {
        $("onboarding").classList.add("hidden");
    }

    let startLat = 20, startLon = 0, startZoom = 2;
    try {
        const savedHome = JSON.parse(localStorage.getItem("last_home") || "null");
        if (savedHome?.lat != null && savedHome?.lon != null) {
            startupLocation = { ...savedHome, stale: true };
            startLat = savedHome.lat; startLon = savedHome.lon; startZoom = 13;
        }
    } catch (e) {}
    try {
        const r = await fetch("/api/default-location");
        const d = await r.json();
        if (d.available && d.lat != null && d.lon != null) {
            startupLocation = d;
            startLat = d.lat; startLon = d.lon; startZoom = 13;
            localStorage.setItem("last_home", JSON.stringify(d));
        }
    } catch (e) {}

    const tileSet = lightTheme ? TILES.light : TILES.dark;
    darkTiles = !lightTheme;
    map = L.map("map", { zoomControl: false }).setView([startLat, startLon], startZoom);
    tileLayer = L.tileLayer(tileSet.url, { attribution: tileSet.attr, maxZoom: 19, subdomains: "abcd" }).addTo(map);
    map.on("click", onMapClick);
    if (startupLocation) showUserLocation(startupLocation);

    // Core buttons
    $("btn-set").addEventListener("click", setLocation);
    $("btn-clear").addEventListener("click", clearLocation);
    $("btn-paste").addEventListener("click", pasteCoords);
    $("btn-route-start").addEventListener("click", startRoute);
    $("btn-route-stop").addEventListener("click", stopRoute);
    $("btn-route-pause").addEventListener("click", pauseRoute);
    $("btn-route-resume").addEventListener("click", resumeRoute);
    $("btn-route-clear").addEventListener("click", clearRoutePoints);
    $("btn-route-calculate").addEventListener("click", calculateAddressRoute);
    $("route-origin").addEventListener("keydown", e => { if (e.key === "Enter") calculateAddressRoute(); });
    $("route-destination").addEventListener("keydown", e => { if (e.key === "Enter") calculateAddressRoute(); });
    $("btn-layer").addEventListener("click", toggleTiles);
    $("btn-my-location").addEventListener("click", goToUserLocation);
    $("btn-zoom-in").addEventListener("click", () => map.zoomIn());
    $("btn-zoom-out").addEventListener("click", () => map.zoomOut());
    $("btn-teleport").addEventListener("click", toggleTeleport);
    $("btn-clear-recent").addEventListener("click", clearRecent);
    $("search-input").addEventListener("input", onSearchInput);
    $("search-input").addEventListener("keydown", onSearchKeydown);
    $("search-input").addEventListener("focus", () => {
        if ($("search-results").children.length > 0) $("search-results").classList.add("visible");
    });
    $("btn-theme").addEventListener("click", toggleTheme);
    $("btn-coord-fmt").addEventListener("click", toggleCoordFormat);
    $("btn-gpx-import").addEventListener("click", () => $("gpx-file").click());
    $("gpx-file").addEventListener("change", importGPX);
    $("btn-gpx-export").addEventListener("click", exportGPX);
    $("btn-wander-start").addEventListener("click", startWander);
    $("btn-wander-stop").addEventListener("click", stopWander);
    $("btn-circular").addEventListener("click", generateCircularRoute);

    // Joystick
    document.querySelectorAll(".joy-btn[data-dir]").forEach(btn => {
        btn.addEventListener("mousedown", () => joystickMove(btn.dataset.dir));
        btn.addEventListener("mouseup", joystickStop);
        btn.addEventListener("mouseleave", joystickStop);
    });
    $("btn-joy-stop").addEventListener("click", joystickStop);
    document.addEventListener("keydown", onKeyDown);
    document.addEventListener("keyup", onKeyUp);

    // Speed
    document.querySelectorAll(".seg-btn").forEach(btn => {
        btn.addEventListener("click", () => {
            setSelectedSpeed(parseFloat(btn.dataset.speed));
            queueLiveSpeedUpdate(selectedSpeed, true);
        });
    });
    $("speed-input").addEventListener("input", () => {
        const speedKmh = readSpeedKmh();
        if (speedKmh == null || speedKmh > 300) return;
        setSelectedSpeed(speedKmh, false);
        queueLiveSpeedUpdate(selectedSpeed);
    });
    $("speed-input").addEventListener("change", () => setSpeedInputFromKmh(selectedSpeed));
    $("speed-unit-toggle").addEventListener("click", toggleSpeedUnit);
    $("speed-unit-toggle").textContent = speedUnit === "mph" ? "mph" : "km/h";
    $("speed-unit-toggle").title = speedUnit === "mph" ? "Switch speed units to kilometers per hour" : "Switch speed units to miles per hour";
    setSelectedSpeed(selectedSpeed);

    // Close dropdowns on outside click
    document.addEventListener("click", e => {
        if (!e.target.closest(".search-container")) $("search-results").classList.remove("visible");
        if (!e.target.closest("#device-badge") && !e.target.closest("#device-dropdown")) $("device-dropdown")?.classList.add("hidden");
    });

    // Tabs
    document.querySelectorAll(".tab-btn").forEach(btn => {
        btn.addEventListener("click", () => {
            document.querySelectorAll(".tab-btn").forEach(b => b.classList.remove("active"));
            document.querySelectorAll(".tab-content").forEach(c => c.classList.remove("active"));
            btn.classList.add("active");
            document.getElementById("tab-" + btn.dataset.tab)?.classList.add("active");
        });
    });

    // HUD Panels
    document.querySelectorAll(".hud-panel-header").forEach(header => {
        header.addEventListener("click", e => { if (!e.target.closest(".hud-panel-close")) togglePanel(header.dataset.panel); });
    });
    document.querySelectorAll(".hud-panel-close").forEach(btn => { btn.addEventListener("click", () => togglePanel(btn.dataset.panel)); });
    restorePanelStates();

    // Position Places panel
    // Places sits under Location in the left column. Both are fixed-position,
    // so nothing stops the second one running off the bottom of the screen —
    // give it whatever room is left and let its body scroll inside that.
    function positionPlacesPanel() {
        const loc = $("panel-location"), places = $("panel-places");
        if (!loc || !places) return;
        const top = loc.getBoundingClientRect().bottom + 6;
        places.style.top = top + "px";
        places.style.maxHeight = Math.max(120, window.innerHeight - top - 16) + "px";
    }
    positionPlacesPanel();
    new ResizeObserver(positionPlacesPanel).observe($("panel-location"));
    window.addEventListener("resize", positionPlacesPanel);

    // Undo
    $("btn-undo").addEventListener("click", undoTeleport);

    // Inline forms
    $("btn-save").addEventListener("click", showSaveForm);
    $("btn-save-cancel").addEventListener("click", () => $("save-form").classList.add("hidden"));
    $("btn-save-confirm").addEventListener("click", confirmSaveLocation);
    $("btn-schedule-add").addEventListener("click", showScheduleForm);
    $("btn-schedule-cancel").addEventListener("click", () => $("schedule-form").classList.add("hidden"));
    $("btn-schedule-confirm").addEventListener("click", confirmAddSchedule);
    $("btn-profile-save").addEventListener("click", showProfileForm);
    $("btn-profile-cancel").addEventListener("click", () => $("profile-form").classList.add("hidden"));
    $("btn-profile-confirm").addEventListener("click", confirmSaveProfile);

    // Category/day pills
    document.querySelectorAll(".cat-pill").forEach(p => { p.addEventListener("click", () => { document.querySelectorAll(".cat-pill").forEach(x => x.classList.remove("active")); p.classList.add("active"); }); });
    document.querySelectorAll(".day-pill").forEach(p => { p.addEventListener("click", () => p.classList.toggle("active")); });

    // Shortcuts
    $("btn-shortcuts").addEventListener("click", toggleShortcuts);
    $("btn-shortcuts-close").addEventListener("click", toggleShortcuts);
    $("shortcuts-overlay").addEventListener("click", e => { if (e.target === $("shortcuts-overlay")) toggleShortcuts(); });

    // Stealth / Anti-detection
    $("btn-stealth").addEventListener("click", toggleTips);
    $("btn-tips-close").addEventListener("click", toggleTips);
    $("tips-overlay").addEventListener("click", e => { if (e.target === $("tips-overlay")) toggleTips(); });
    $("stealth-banner-close").addEventListener("click", dismissStealthBanner);
    $("stealth-banner-tip").addEventListener("click", () => { dismissStealthBanner(); toggleTips(); });
    $("status-stealth")?.addEventListener("click", toggleTips);

    // Device dropdown & connect buttons
    $("device-badge").addEventListener("click", toggleDeviceDropdown);
    $("btn-connect").addEventListener("click", () => connectDevice(false));
    $("btn-connect-wifi").addEventListener("click", () => connectDevice(true));

    // Follow mode
    $("follow-mode")?.addEventListener("change", e => { followMode = e.target.checked; });

    // Route save
    $("btn-route-save")?.addEventListener("click", saveCurrentRoute);

    // Coord HUD
    map.on("mousemove", e => { const h = $("coord-hud"); if (h) { h.classList.remove("hidden"); $("coord-hud-text").textContent = e.latlng.lat.toFixed(6) + ", " + e.latlng.lng.toFixed(6); } });
    map.on("mouseout", () => { $("coord-hud")?.classList.add("hidden"); });

    // Load data
    pollDevice(); loadSaved(); loadProfiles(); loadSchedules(); loadRouteHistory(); loadRecent(); renderPopular(); updateStatusBar();
    setInterval(pollDevice, 5000);
    setInterval(pollCooldown, 2000);
    // Reflect scheduled or externally-triggered location changes in the UI.
    setInterval(pollPosition, 5000);
});

function $(id) { return document.getElementById(id); }

// A UDID identifies the device permanently, and this panel ends up in
// screenshots attached to bug reports. Show enough to tell two phones apart.
function maskUdid(udid) {
    if (!udid) return "--";
    const text = String(udid);
    return text.length <= 12 ? "…" : text.slice(0, 6) + "…" + text.slice(-4);
}
function esc(s) { const d = document.createElement("div"); d.textContent = s; return d.innerHTML; }
function escAttr(s) { return esc(s).replace(/"/g, "&quot;").replace(/'/g, "&#39;"); }

// ── Panels ──────────────────────────────────────────────────
function togglePanel(panelId) { const p = $(panelId); if (!p) return; p.classList.toggle("collapsed"); localStorage.setItem("panel_" + panelId, p.classList.contains("collapsed") ? "collapsed" : "open"); }
function restorePanelStates() { ["panel-location","panel-places","panel-movement"].forEach(id => { if (localStorage.getItem("panel_" + id) === "collapsed") $(id)?.classList.add("collapsed"); }); }

// ── Onboarding ──────────────────────────────────────────────
let obPage = 1;
function obNext(page) { document.getElementById("ob-page-" + obPage).classList.add("hidden"); document.getElementById("ob-page-" + page).classList.remove("hidden"); obPage = page; document.querySelectorAll(".ob-dot").forEach((d, i) => d.classList.toggle("active", i + 1 === page)); }
function obSkip() { localStorage.setItem("ob_done", "1"); $("onboarding").classList.add("hidden"); }
function obDone() { if ($("ob-dismiss").checked) localStorage.setItem("ob_done", "1"); $("onboarding").classList.add("hidden"); }

// ── Toasts ──────────────────────────────────────────────────
function toast(msg, type = "success") { const dur = type === "warning" ? 5000 : 3000; const el = document.createElement("div"); el.className = "toast " + type; el.textContent = msg; $("toasts").appendChild(el); setTimeout(() => { el.classList.add("out"); setTimeout(() => el.remove(), 300); }, dur); }

// ── Theme ───────────────────────────────────────────────────
function toggleTheme() { lightTheme = !lightTheme; document.body.classList.toggle("light", lightTheme); localStorage.setItem("theme", lightTheme ? "light" : "dark"); const t = lightTheme ? TILES.light : TILES.dark; darkTiles = !lightTheme; map.removeLayer(tileLayer); tileLayer = L.tileLayer(t.url, { attribution: t.attr, maxZoom: 19, subdomains: "abcd" }).addTo(map); }

// ── Coord format ────────────────────────────────────────────
function toggleCoordFormat() { coordFormat = coordFormat === "dd" ? "dms" : "dd"; localStorage.setItem("coord_fmt", coordFormat); $("btn-coord-fmt").textContent = coordFormat.toUpperCase(); if (marker) { const ll = marker.getLatLng(); updateCoordInputs(ll.lat, ll.lng); } }
function toDMS(deg, isLon) { const dir = isLon ? (deg >= 0 ? "E" : "W") : (deg >= 0 ? "N" : "S"); deg = Math.abs(deg); const d = Math.floor(deg); const m = Math.floor((deg - d) * 60); const s = ((deg - d - m / 60) * 3600).toFixed(1); return d + "\u00B0" + m + "'" + s + '"' + dir; }
function updateCoordInputs(lat, lng) { if (coordFormat === "dms") { $("lat-input").value = toDMS(lat, false); $("lon-input").value = toDMS(lng, true); } else { $("lat-input").value = lat.toFixed(6); $("lon-input").value = lng.toFixed(6); } if ($("coord-text")) { $("coord-text").textContent = lat.toFixed(6) + ", " + lng.toFixed(6); $("coord-text").classList.add("coord-glow"); } }

// ── Teleport ────────────────────────────────────────────────
function toggleTeleport() { teleportMode = !teleportMode; $("btn-teleport").classList.toggle("active", teleportMode); toast(teleportMode ? "Teleport ON \u2014 click map to move instantly" : "Teleport OFF", teleportMode ? "success" : "error"); }

// ── Shortcuts ───────────────────────────────────────────────
function toggleShortcuts() { $("shortcuts-overlay").classList.toggle("hidden"); }

// ── Map ─────────────────────────────────────────────────────
function onMapClick(e) { if (e.originalEvent.shiftKey || routePoints.length > 0) { addRoutePoint(e.latlng.lat, e.latlng.lng); } else if (teleportMode) { placeMarker(e.latlng.lat, e.latlng.lng); teleportTo(e.latlng.lat, e.latlng.lng); } else { placeMarker(e.latlng.lat, e.latlng.lng); } }

function showUserLocation(location) {
    const icon = L.divIcon({
        className: "user-location-container",
        html: '<div class="user-location-marker"><div class="user-location-ring"></div><div class="user-location-dot"></div></div>',
        iconSize: [24, 24],
        iconAnchor: [12, 12],
    });
    const latlng = [location.lat, location.lon];
    if (userLocationMarker) userLocationMarker.setLatLng(latlng);
    else userLocationMarker = L.marker(latlng, { icon, interactive: true, zIndexOffset: -100 }).addTo(map);

    const place = [location.city, location.region, location.country].filter(Boolean).join(", ");
    const prefix = location.stale ? "Last known area" : "Your approximate area";
    userLocationMarker.bindTooltip(
        '<strong>' + prefix + '</strong>' + (place ? '<br>' + esc(place) : ""),
        { direction: "top", offset: [0, -10], className: "user-location-tooltip" }
    );
}

function goToUserLocation() {
    if (!startupLocation) return toast("Your approximate location is unavailable", "warning");
    map.flyTo([startupLocation.lat, startupLocation.lon], Math.max(map.getZoom(), 13), { duration: 0.8 });
    userLocationMarker?.openTooltip();
}

function placeMarker(lat, lng) {
    const icon = L.divIcon({ className: "map-marker-container", html: '<div class="map-marker"><div class="map-marker-pulse"></div><div class="map-marker-dot"></div></div>', iconSize: [20, 20], iconAnchor: [10, 10] });
    if (marker) { marker.setLatLng([lat, lng]); } else { marker = L.marker([lat, lng], { icon: icon }).addTo(map); }
    trailPoints.push([lat, lng]); if (trailPoints.length > 20) trailPoints.shift();
    updateCoordInputs(lat, lng); updateStatusBar();
}

function toggleTiles() { darkTiles = !darkTiles; const t = darkTiles ? TILES.dark : TILES.light; map.removeLayer(tileLayer); tileLayer = L.tileLayer(t.url, { attribution: t.attr, maxZoom: 19, subdomains: "abcd" }).addTo(map); }

// ── Teleport to ─────────────────────────────────────────────
async function teleportTo(lat, lon) {
    storePreviousLocation();
    try { const r = await fetch("/api/location/set", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ lat, lon }) }); if (r.ok) { activeSpoofLocation = { lat, lon }; toast("Teleported to " + lat.toFixed(4) + ", " + lon.toFixed(4)); addToRecent(lat, lon); _stealthDismissed = false; checkStealth(); } else { const d = await r.json(); toast(d.error || "Failed", "error"); } } catch (e) { toast("Connection error", "error"); }
}

// ── Location set/clear ──────────────────────────────────────
async function setLocation() {
    const lat = parseFloat($("lat-input").value), lon = parseFloat($("lon-input").value);
    if (isNaN(lat) || isNaN(lon)) return toast("Place a marker on the map first", "error");
    storePreviousLocation();
    try { const r = await fetch("/api/location/set", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ lat, lon }) }); const d = await r.json(); if (r.ok) { activeSpoofLocation = { lat, lon }; toast("Location set: " + lat.toFixed(4) + ", " + lon.toFixed(4)); placeMarker(lat, lon); addToRecent(lat, lon); _stealthDismissed = false; checkStealth(); } else toast(d.error || "Failed", "error"); } catch (e) { toast("Connection error", "error"); }
}

async function clearLocation() {
    try { const r = await fetch("/api/location/clear", { method: "POST" }); if (r.ok) { activeSpoofLocation = null; toast("Reset to real GPS"); if (marker) { map.removeLayer(marker); marker = null; } $("lat-input").value = ""; $("lon-input").value = ""; if ($("coord-text")) { $("coord-text").textContent = "Click the map to set a location"; $("coord-text").classList.remove("coord-glow"); } if (startupLocation) goToUserLocation(); _stealthDismissed = false; await checkStealth(); } else { const d = await r.json().catch(() => ({})); toast(d.error || "Failed to reset", "error"); } } catch (e) { toast("Connection error", "error"); }
}

// ── Undo ────────────────────────────────────────────────────
function storePreviousLocation() { if (activeSpoofLocation) { previousLocation = { ...activeSpoofLocation }; $("btn-undo").disabled = false; $("btn-undo").title = "Undo to " + previousLocation.lat.toFixed(4) + ", " + previousLocation.lon.toFixed(4); } }
async function undoTeleport() {
    if (!previousLocation) return toast("No previous location", "error");
    try { const r = await fetch("/api/location/set", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(previousLocation) }); if (r.ok) { activeSpoofLocation = { ...previousLocation }; toast("Undone \u2014 back to " + previousLocation.lat.toFixed(4) + ", " + previousLocation.lon.toFixed(4)); placeMarker(previousLocation.lat, previousLocation.lon); map.flyTo([previousLocation.lat, previousLocation.lon], map.getZoom(), { duration: 0.8 }); previousLocation = null; $("btn-undo").disabled = true; $("btn-undo").title = "No previous location"; _stealthDismissed = false; checkStealth(); } } catch (e) { toast("Undo failed", "error"); }
}

// ── Paste ───────────────────────────────────────────────────
async function pasteCoords() {
    try { const text = await navigator.clipboard.readText(); let lat, lon; const urlMatch = text.match(/@(-?\d+\.?\d*),(-?\d+\.?\d*)/); const coordMatch = text.match(/(-?\d+\.?\d*)[,\s]+(-?\d+\.?\d*)/); if (urlMatch) { lat = parseFloat(urlMatch[1]); lon = parseFloat(urlMatch[2]); } else if (coordMatch) { lat = parseFloat(coordMatch[1]); lon = parseFloat(coordMatch[2]); } if (lat != null && lon != null && !isNaN(lat) && !isNaN(lon) && Math.abs(lat) <= 90 && Math.abs(lon) <= 180) { updateCoordInputs(lat, lon); map.flyTo([lat, lon], 15, { duration: 1 }); placeMarker(lat, lon); toast("Pasted: " + lat.toFixed(4) + ", " + lon.toFixed(4)); } else { toast("No valid coordinates in clipboard", "error"); } } catch (e) { toast("Clipboard access denied", "error"); }
}

// ── Search ──────────────────────────────────────────────────
function renderSearchMessage(message, className = "") {
    const container = $("search-results");
    container.textContent = "";
    const row = document.createElement("div");
    row.className = "search-message " + className;
    row.textContent = message;
    container.appendChild(row);
    container.classList.add("visible");
}

function onSearchInput(e) {
    const q = e.target.value.trim();
    clearTimeout(searchTimeout);
    searchAbortController?.abort();
    if (q.length < 2) { $("search-results").classList.remove("visible"); return; }
    searchTimeout = setTimeout(() => doSearch(q), 400);
}

function onSearchKeydown(e) {
    const results = $("search-results"), items = results.querySelectorAll(".search-item");
    if (!items.length || !results.classList.contains("visible")) { if (e.key === "Escape") { results.classList.remove("visible"); searchHighlightIndex = -1; } return; }
    if (e.key === "ArrowDown") { e.preventDefault(); searchHighlightIndex = Math.min(searchHighlightIndex + 1, items.length - 1); updateSearchHighlight(items); }
    else if (e.key === "ArrowUp") { e.preventDefault(); searchHighlightIndex = Math.max(searchHighlightIndex - 1, 0); updateSearchHighlight(items); }
    else if (e.key === "Enter") { e.preventDefault(); const index = searchHighlightIndex >= 0 ? searchHighlightIndex : 0; items[index]?.click(); searchHighlightIndex = -1; }
    else if (e.key === "Escape") { results.classList.remove("visible"); searchHighlightIndex = -1; }
}

function updateSearchHighlight(items) { items.forEach((item, i) => { item.classList.toggle("highlighted", i === searchHighlightIndex); if (i === searchHighlightIndex) item.scrollIntoView({ block: "nearest" }); }); }

async function doSearch(q) {
    searchHighlightIndex = -1;
    searchAbortController?.abort();
    searchAbortController = new AbortController();
    renderSearchMessage("Searching Google Maps…", "loading");
    try {
        const center = map.getCenter();
        const params = new URLSearchParams({
            q,
            lat: center.lat.toFixed(6),
            lon: center.lng.toFixed(6),
            zoom: String(Math.round(map.getZoom())),
        });
        const country = startupLocation?.country;
        if (country && /^[a-z]{2}$/i.test(country)) params.set("country", country.toLowerCase());
        const r = await fetch("/api/search?" + params.toString(), { signal: searchAbortController.signal });
        const results = await r.json();
        const c = $("search-results");
        if ($("search-input").value.trim() !== q) return;
        if (!r.ok) return renderSearchMessage(results.error || "Search unavailable", "error");
        if (!Array.isArray(results) || !results.length) return renderSearchMessage("No places found. Try an address or coordinates.", "empty");
        c.textContent = "";
        results.forEach(res => {
            const item = document.createElement("div");
            item.className = "search-item";
            item.dataset.lat = res.lat; item.dataset.lon = res.lon;
            const iconDiv = document.createElement("div");
            iconDiv.className = "search-item-icon";
            iconDiv.innerHTML = SEARCH_ICON_SVG; // Static SVG, no user data
            const content = document.createElement("div");
            content.className = "search-item-content";
            const title = document.createElement("div");
            title.className = "search-item-title";
            title.textContent = res.name || res.display_name;
            const meta = document.createElement("div");
            meta.className = "search-item-meta";
            const details = [res.subtitle, res.distance_km != null ? formatSearchDistance(res.distance_km) : ""].filter(Boolean);
            meta.textContent = details.join(" · ");
            content.appendChild(title); content.appendChild(meta);
            const source = document.createElement("span");
            source.className = "search-source " + (res.source === "google" ? "google" : "osm");
            source.textContent = res.source === "google" ? "Google" : (res.source === "coordinates" ? "GPS" : "OSM");
            const googleLink = document.createElement("button");
            googleLink.className = "search-google-link";
            googleLink.type = "button";
            googleLink.title = "Open this place in Google Maps";
            googleLink.textContent = "↗";
            googleLink.addEventListener("click", event => {
                event.stopPropagation();
                const url = "https://www.google.com/maps/search/?api=1&query=" + encodeURIComponent(res.display_name || (res.lat + "," + res.lon));
                window.open(url, "_blank", "noopener");
            });
            item.appendChild(iconDiv); item.appendChild(content); item.appendChild(source); item.appendChild(googleLink);
            item.addEventListener("click", () => {
                const lat = parseFloat(item.dataset.lat), lon = parseFloat(item.dataset.lon);
                if (Array.isArray(res.bbox) && res.bbox.length === 4) {
                    map.fitBounds([[res.bbox[0], res.bbox[1]], [res.bbox[2], res.bbox[3]]], { maxZoom: 17, padding: [30, 30] });
                } else map.flyTo([lat, lon], 16, { duration: 1.2 });
                placeMarker(lat, lon); c.classList.remove("visible"); $("search-input").value = res.display_name; if (teleportMode) teleportTo(lat, lon);
            });
            item.addEventListener("mouseenter", () => { searchHighlightIndex = Array.from(c.querySelectorAll(".search-item")).indexOf(item); updateSearchHighlight(c.querySelectorAll(".search-item")); });
            c.appendChild(item);
        });
        c.classList.add("visible");
    } catch (e) { if (e.name !== "AbortError") renderSearchMessage("Search is temporarily unavailable", "error"); }
}

function formatSearchDistance(km) {
    if (km < 1) return Math.max(1, Math.round(km * 1000)) + " m";
    if (km < 10) return km.toFixed(1) + " km";
    return Math.round(km) + " km";
}

// ── Device polling ──────────────────────────────────────────
let wasConnected = false;
let _autoConnectAttempted = false;
let currentDeviceInfo = null;

function setReadinessValue(id, text, state) {
    const el = $(id);
    if (!el) return;
    el.textContent = text;
    el.className = "readiness-value " + state;
}

function renderDeviceReadiness(d) {
    const developerReady = d.developer_mode === true;
    const ddiReady = d.ddi_mounted === true;
    const tunnelReady = Boolean(d.tunnel_mode);
    setReadinessValue("dev-developer", developerReady ? "Ready" : "Off", developerReady ? "ready" : "error");
    setReadinessValue("dev-ddi", ddiReady ? "Mounted" : "Missing", ddiReady ? "ready" : "error");
    setReadinessValue("dev-tunnel", tunnelReady ? (d.tunnel_mode === "userspace" ? "Userspace" : "Root fallback") : "Unavailable", tunnelReady ? "ready" : "error");
    return developerReady && ddiReady && tunnelReady;
}

async function pollDevice() {
    try {
        const r = await fetch("/api/device"); const d = await r.json(); const dot = $("device-dot");
        currentDeviceInfo = d;
        if (d.connected) {
            const ready = renderDeviceReadiness(d);
            dot.classList.toggle("connected", ready); dot.classList.toggle("degraded", !ready); const ct = d.connection_type || "USB";
            $("device-label").textContent = d.name || "iPhone";
            $("dev-ios").textContent = d.ios_version || "--"; $("dev-model").textContent = d.model || "--"; $("dev-udid").textContent = maskUdid(d.udid);
            const cb = $("dev-conn"); cb.textContent = ct; cb.className = "conn-badge " + ct.toLowerCase();
            $("device-info-compact")?.classList.remove("hidden"); $("setup-guide")?.classList.add("hidden");
            if ($("status-conn-text")) $("status-conn-text").textContent = ct;
            if (!wasConnected) { wasConnected = true; toast("iPhone connected (" + ct + ")"); }
        } else {
            dot.classList.remove("connected", "degraded"); $("device-label").textContent = "No device";
            setReadinessValue("dev-developer", "Unavailable", "error");
            setReadinessValue("dev-ddi", "Unavailable", "error");
            setReadinessValue("dev-tunnel", "Unavailable", "error");
            $("device-info-compact")?.classList.add("hidden"); $("setup-guide")?.classList.remove("hidden");
            if ($("status-conn-text")) $("status-conn-text").textContent = "--"; wasConnected = false;
            // Auto-connect on first poll if tunnel is running
            if (!_autoConnectAttempted) { _autoConnectAttempted = true; autoConnect(); }
        }
    } catch (e) {}
}

async function autoConnect() {
    try {
        if ($("connect-status")) $("connect-status").textContent = "Auto-connecting...";
        await connectDevice(false);
    } catch (e) {}
}

// ── Connection ──────────────────────────────────────────────
async function connectDevice(wifi = false, udid = null) {
    const status = $("connect-status"), btnU = $("btn-connect"), btnW = $("btn-connect-wifi");
    const origU = btnU?.textContent, origW = btnW?.textContent;
    if (btnU) { btnU.disabled = true; btnU.classList.add("btn-loading"); }
    if (btnW) { btnW.disabled = true; btnW.classList.add("btn-loading"); }
    if (wifi && btnW) btnW.textContent = "Connecting...";
    else if (btnU) btnU.textContent = "Connecting...";
    if (status) status.textContent = wifi ? "Scanning for WiFi device..." : "Scanning for USB device...";
    try { const r = await fetch("/api/device/connect", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ wifi, udid }) }); const d = await r.json(); if (r.ok) { toast("Connected via " + d.connection_type + " (" + (d.tunnel_mode || "local") + ")"); if (status) status.textContent = ""; pollDevice(); } else { toast(d.error || "Failed", "error"); if (status) status.textContent = d.error || "Failed"; } } catch (e) { toast("Connection error", "error"); if (status) status.textContent = "Error"; } finally { if (btnU) { btnU.disabled = false; btnU.classList.remove("btn-loading"); btnU.textContent = origU; } if (btnW) { btnW.disabled = false; btnW.classList.remove("btn-loading"); btnW.textContent = origW; } }
}

// ── Multi-device ────────────────────────────────────────────
async function toggleDeviceDropdown() {
    const dd = $("device-dropdown");
    if (!dd.classList.contains("hidden")) { dd.classList.add("hidden"); return; }
    try {
        const r = await fetch("/api/devices"); const devices = await r.json(); const list = $("device-dropdown-list");
        list.textContent = "";
        const active = document.createElement("div");
        active.className = "device-readiness-card";
        const info = currentDeviceInfo || {};
        const statusItems = info.connected ? [
            ["Developer Mode", info.developer_mode === true ? "Ready" : "Off", info.developer_mode === true],
            ["Developer Image", info.ddi_mounted === true ? "Mounted" : "Missing", info.ddi_mounted === true],
            ["Tunnel", info.tunnel_mode === "userspace" ? "Userspace" : (info.tunnel_mode === "tunneld" ? "Root fallback" : "Unavailable"), Boolean(info.tunnel_mode)]
        ] : [["Device", "Not connected", false]];
        const heading = document.createElement("div"); heading.className = "device-dropdown-heading"; heading.textContent = "ACTIVE DEVICE"; active.appendChild(heading);
        statusItems.forEach(([label, value, ok]) => { const row = document.createElement("div"); row.className = "device-readiness-row"; const l = document.createElement("span"); l.textContent = label; const v = document.createElement("span"); v.className = "readiness-value " + (ok ? "ready" : "error"); v.textContent = value; row.append(l, v); active.appendChild(row); });
        list.appendChild(active);
        const availableHeading = document.createElement("div"); availableHeading.className = "device-dropdown-heading available"; availableHeading.textContent = "AVAILABLE DEVICES"; list.appendChild(availableHeading);
        if (!devices.length) { const e = document.createElement("div"); e.className = "empty-state"; e.style.padding = "10px"; e.textContent = "No devices found"; list.appendChild(e); }
        else { devices.forEach(dev => { const opt = document.createElement("div"); opt.className = "device-option"; const u = document.createElement("span"); u.className = "mono"; u.textContent = maskUdid(dev.udid); opt.appendChild(u); dev.connection_types.forEach(t => { const b = document.createElement("span"); b.className = "conn-badge " + t.toLowerCase(); b.textContent = t; opt.appendChild(b); }); opt.addEventListener("click", async () => { await connectDevice(false, dev.udid); dd.classList.add("hidden"); }); list.appendChild(opt); }); }
        const badge = $("device-badge"), rect = badge.getBoundingClientRect();
        dd.style.top = (rect.bottom + 4) + "px"; dd.style.right = (window.innerWidth - rect.right) + "px";
        dd.classList.remove("hidden");
    } catch (e) { toast("Failed to load devices", "error"); }
}

// ── Recent ──────────────────────────────────────────────────
async function loadRecent() { try { const r = await fetch("/api/history"); const history = await r.json(); recentLocations = history.slice(0, 15).map(h => ({ lat: Number(h.lat), lon: Number(h.lon), ts: Number(h.ts) * 1000 })); renderRecent(); } catch (e) { renderRecent(); } }
function addToRecent(lat, lon) { const entry = { lat: +lat.toFixed(6), lon: +lon.toFixed(6), ts: Date.now() }; recentLocations = recentLocations.filter(r => Math.abs(r.lat - entry.lat) > 0.0005 || Math.abs(r.lon - entry.lon) > 0.0005); recentLocations.unshift(entry); recentLocations = recentLocations.slice(0, 15); renderRecent(); }
async function clearRecent() { try { const r = await fetch("/api/history", { method: "DELETE" }); if (!r.ok) throw new Error("clear failed"); recentLocations = []; renderRecent(); toast("History cleared"); } catch (e) { toast("Failed to clear history", "error"); } }
function renderRecent() { const c = $("recent-list"); c.textContent = ""; if (!recentLocations.length) { const e = document.createElement("div"); e.className = "empty-state"; e.textContent = "No recent locations"; c.appendChild(e); return; } recentLocations.forEach(r => { const item = document.createElement("div"); item.className = "saved-item"; item.dataset.lat = r.lat; item.dataset.lon = r.lon; const n = document.createElement("span"); n.className = "saved-name"; n.textContent = r.lat.toFixed(4) + ", " + r.lon.toFixed(4); const co = document.createElement("span"); co.className = "saved-coords"; co.textContent = timeAgo(r.ts); item.appendChild(n); item.appendChild(co); item.addEventListener("click", () => { map.flyTo([r.lat, r.lon], 15, { duration: 1 }); placeMarker(r.lat, r.lon); if (teleportMode) teleportTo(r.lat, r.lon); }); c.appendChild(item); }); }
function timeAgo(ts) { const s = Math.floor((Date.now() - ts) / 1000); if (s < 60) return "just now"; if (s < 3600) return Math.floor(s / 60) + "m ago"; if (s < 86400) return Math.floor(s / 3600) + "h ago"; return Math.floor(s / 86400) + "d ago"; }

// ── Popular ─────────────────────────────────────────────────
function renderPopular() { const c = $("popular-list"); c.textContent = ""; POPULAR.forEach(p => { const item = document.createElement("div"); item.className = "saved-item"; const n = document.createElement("span"); n.className = "saved-name"; n.textContent = p.name; item.appendChild(n); item.addEventListener("click", () => { map.flyTo([p.lat, p.lon], 15, { duration: 1.2 }); placeMarker(p.lat, p.lon); if (teleportMode) teleportTo(p.lat, p.lon); }); c.appendChild(item); }); }

// ── Saved locations ─────────────────────────────────────────
async function loadSaved() {
    try { const r = await fetch("/api/saved"); const locs = await r.json(); const c = $("saved-list"); c.textContent = "";
    if (!locs.length) { const e = document.createElement("div"); e.className = "empty-state"; e.textContent = "No saved locations"; c.appendChild(e); return; }
    locs.forEach(l => { const item = document.createElement("div"); item.className = "saved-item"; item.dataset.lat = l.lat; item.dataset.lon = l.lon; const n = document.createElement("span"); n.className = "saved-name"; n.textContent = l.name; const co = document.createElement("span"); co.className = "saved-coords"; co.textContent = l.lat.toFixed(2) + ", " + l.lon.toFixed(2); const del = document.createElement("button"); del.className = "saved-del"; del.title = "Delete"; del.textContent = "\u00D7"; del.addEventListener("click", async e => { e.stopPropagation(); await fetch("/api/saved/" + encodeURIComponent(l.name), { method: "DELETE" }); loadSaved(); toast('Deleted "' + l.name + '"'); }); item.appendChild(n); item.appendChild(co); item.appendChild(del); item.addEventListener("click", e => { if (e.target.classList.contains("saved-del")) return; map.flyTo([l.lat, l.lon], 15, { duration: 1 }); placeMarker(l.lat, l.lon); if (teleportMode) teleportTo(l.lat, l.lon); }); c.appendChild(item); });
    } catch (e) {}
}

// ── Inline save form ────────────────────────────────────────
function showSaveForm() { const lat = parseFloat($("lat-input").value), lon = parseFloat($("lon-input").value); if (isNaN(lat) || isNaN(lon)) return toast("Place a marker first", "error"); $("save-form").classList.remove("hidden"); $("save-name").value = ""; $("save-name").focus(); }
async function confirmSaveLocation() { const name = $("save-name").value.trim(); if (!name) return toast("Enter a name", "error"); const lat = parseFloat($("lat-input").value), lon = parseFloat($("lon-input").value); const cat = document.querySelector(".cat-pill.active")?.dataset.cat || "default"; try { const r = await fetch("/api/saved", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ name, lat, lon, category: cat }) }); if (r.ok) { loadSaved(); toast('Saved "' + name + '"'); $("save-form").classList.add("hidden"); } else { const d = await r.json(); toast(d.error || "Failed", "error"); } } catch (e) { toast("Connection error", "error"); } }

// ── Route / Movement ────────────────────────────────────────
function addRoutePoint(lat, lng, preserveCalculated = false) { if (!preserveCalculated) { calculatedRouteCoordinates = null; calculatedRouteProvider = null; } routePoints.push({ lat, lng }); const m = L.circleMarker([lat, lng], { radius: 6, color: "#6F999A", fillColor: "#6F999A", fillOpacity: 1, weight: 0 }).addTo(map); m.bindTooltip(String(routePoints.length), { permanent: true, direction: "center", className: "route-label" }); routeMarkers.push(m); if (routePoints.length >= 2) { if (routeLine) map.removeLayer(routeLine); routeLine = L.polyline(routePoints.map(p => [p.lat, p.lng]), { color: "#6F999A", weight: 2, dashArray: "8 6", opacity: 0.6 }).addTo(map); } updateRouteUI(); }
function clearRoutePoints() { routePoints = []; calculatedRouteCoordinates = null; calculatedRouteProvider = null; routeMarkers.forEach(m => map.removeLayer(m)); routeMarkers = []; if (routeLine) { map.removeLayer(routeLine); routeLine = null; } if (routeDisplayLine) { map.removeLayer(routeDisplayLine); routeDisplayLine = null; } if (routeTraveledLine) { map.removeLayer(routeTraveledLine); routeTraveledLine = null; } const addressStatus = $("route-address-status"); if (addressStatus) { addressStatus.textContent = ""; addressStatus.className = "route-address-status"; } updateRouteUI(); $("route-progress").classList.add("hidden"); }
function updateRouteUI() { $("btn-route-start").disabled = routePoints.length < 2; $("route-hint").textContent = routePoints.length > 0 ? routePoints.length + " point" + (routePoints.length > 1 ? "s" : "") + " \u2014 shift+click to add more" : "Shift+click map to add route points"; }

async function calculateAddressRoute() {
    const origin = $("route-origin").value.trim();
    const destination = $("route-destination").value.trim();
    const status = $("route-address-status");
    if (!origin || !destination) return toast("Enter both route addresses", "error");
    const button = $("btn-route-calculate");
    button.disabled = true; button.textContent = "Calculating…";
    status.className = "route-address-status"; status.textContent = "Asking Google Maps for the driving route…";
    try {
        const response = await fetch("/api/route/calculate", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ origin, destination }) });
        const data = await response.json();
        if (!response.ok) { status.className = "route-address-status error"; status.textContent = data.error || "Route calculation failed"; return toast(status.textContent, "error"); }
        clearRoutePoints();
        data.waypoints.forEach(point => addRoutePoint(point.lat, point.lng, true));
        calculatedRouteCoordinates = data.coordinates;
        calculatedRouteProvider = data.provider;
        routeDistanceKm = data.distance_km;
        if (routeLine) { map.removeLayer(routeLine); routeLine = null; }
        routeDisplayLine = L.polyline(data.coordinates.map(c => [c[1], c[0]]), { color: "#6F999A", weight: 3, opacity: 0.78 }).addTo(map);
        map.fitBounds(routeDisplayLine.getBounds(), { padding: [35, 35] });
        const miles = data.distance_km * 0.621371;
        status.textContent = "Google · " + miles.toFixed(1) + " mi · " + data.duration_min + " min · " + data.route_name;
        $("route-hint").textContent = "Google route ready";
        toast("Google Maps route calculated");
    } catch (error) {
        status.className = "route-address-status error"; status.textContent = "Google Maps route service is unavailable"; toast(status.textContent, "error");
    } finally {
        button.disabled = false; button.textContent = "Calculate with Google Maps";
    }
}

async function startRoute() {
    if (routePoints.length < 2) return; const speed = readSpeedKmh() || selectedSpeed; const mode = $("route-mode").value; const randomize = $("speed-randomize").checked;
    try { const r = await fetch("/api/route/start", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ waypoints: routePoints, speed, mode, randomize_speed: randomize, coordinates: calculatedRouteCoordinates, provider: calculatedRouteProvider }) }); const d = await r.json(); if (!r.ok) return toast(d.error || "Route failed", "error");
    routeDistanceKm = d.distance_km || 0;
    if (d.coordinates) {
        if (routeLine) { map.removeLayer(routeLine); routeLine = null; }
        if (routeDisplayLine) map.removeLayer(routeDisplayLine);
        routeDisplayLine = L.polyline(d.coordinates.map(c => [c[1], c[0]]), { color: "#6F999A", weight: 3, opacity: 0.78 }).addTo(map);
    }
    followMode = true;
    if ($("follow-mode")) $("follow-mode").checked = true;
    const routeStart = d.coordinates?.[0] ? [d.coordinates[0][1], d.coordinates[0][0]] : [routePoints[0].lat, routePoints[0].lng];
    map.stop();
    // Set the watch zoom before the first position poll. A fly animation can
    // be interrupted by that poll and leave the map at its old, wide zoom.
    map.setView(routeStart, routeFollowZoomForSpeed(speed), { animate: false });
    $("btn-route-start").disabled = true; $("btn-route-stop").disabled = false; $("btn-route-pause").classList.remove("hidden"); $("btn-route-resume").classList.add("hidden"); $("route-progress").classList.remove("hidden");
    toast("Route started: " + formatRouteDistance(d.distance_km) + " (" + mode + ") · following"); routePolling = setInterval(pollRoute, 1000); await pollPosition(); startMovementTracking();
    } catch (e) { toast("Route error", "error"); }
}

async function stopRoute() { try { await fetch("/api/route/stop", { method: "POST" }); endRoute(); toast("Route stopped"); } catch (e) { toast("Failed", "error"); } }
async function pauseRoute() { try { await fetch("/api/route/pause", { method: "POST" }); $("btn-route-pause").classList.add("hidden"); $("btn-route-resume").classList.remove("hidden"); toast("Route paused"); } catch (e) { toast("Failed", "error"); } }
async function resumeRoute() { try { await fetch("/api/route/resume", { method: "POST" }); $("btn-route-resume").classList.add("hidden"); $("btn-route-pause").classList.remove("hidden"); toast("Route resumed"); } catch (e) { toast("Failed", "error"); } }

async function pollRoute() {
    try { const r = await fetch("/api/route/status"); if (!r.ok) { endRoute(); return; } const d = await r.json();
    $("progress-bar").style.width = d.progress_pct + "%"; $("route-pct").textContent = Math.round(d.progress_pct) + "%";
    if ($("status-route")) { $("status-route").classList.remove("hidden"); const rem = routeDistanceKm * (1 - d.progress_pct / 100); const eta = d.speed_kmh > 0 ? Math.round((rem / d.speed_kmh) * 60) : 0; $("status-route-text").textContent = formatRouteDistance(rem) + " | ETA " + eta + "m | " + Math.round(d.progress_pct) + "%"; }
    if (!d.active) { endRoute(); toast("Route completed"); if ($("btn-route-save")) $("btn-route-save").style.display = ""; }
    } catch (e) {}
}

function endRoute() { clearInterval(routePolling); routePolling = null; $("btn-route-start").disabled = false; $("btn-route-stop").disabled = true; $("btn-route-pause").classList.add("hidden"); $("btn-route-resume").classList.add("hidden"); $("route-progress").classList.add("hidden"); if ($("status-route")) $("status-route").classList.add("hidden"); if (routeTraveledLine) { map.removeLayer(routeTraveledLine); routeTraveledLine = null; } stopMovementTracking(); }

// ── Live tracking ───────────────────────────────────────────
function startMovementTracking() { if (movementPolling) return; movementPolling = setInterval(pollPosition, 500); }
function stopMovementTracking() { if (movementPolling) { clearInterval(movementPolling); movementPolling = null; } trailPoints = []; }
async function pollPosition() { try { const r = await fetch("/api/location/current"); if (!r.ok) return; const loc = await r.json(); activeSpoofLocation = { lat: loc.lat, lon: loc.lon }; placeMarker(loc.lat, loc.lon); if (followMode) { map.stop(); map.panTo([loc.lat, loc.lon], { animate: true, duration: 0.3, noMoveStart: true }); } } catch (e) {} }

// ── GPX ─────────────────────────────────────────────────────
async function importGPX() { const file = $("gpx-file").files[0]; if (!file) return; const fd = new FormData(); fd.append("file", file); try { const r = await fetch("/api/gpx/import", { method: "POST", body: fd }); const d = await r.json(); if (!r.ok) return toast(d.error || "Import failed", "error"); clearRoutePoints(); d.waypoints.forEach(wp => addRoutePoint(wp.lat, wp.lng)); if (d.waypoints.length > 0) map.flyTo([d.waypoints[0].lat, d.waypoints[0].lng], 14); toast("Imported " + d.count + " waypoints"); } catch (e) { toast("Import failed", "error"); } $("gpx-file").value = ""; }
async function exportGPX() {
    try {
        const response = await fetch("/api/gpx/export?name=Ghostpin%20Route");
        if (!response.ok) {
            const data = await response.json().catch(() => ({}));
            return toast(data.error || "Export failed", "error");
        }
        const content = await response.text();

        // The packaged macOS app uses a native Save dialog. WKWebView does not
        // reliably honor window.open/attachment downloads.
        if (window.pywebview?.api?.save_gpx) {
            const result = await window.pywebview.api.save_gpx(content, "ghostpin-route.gpx");
            if (result?.ok) return toast("GPX exported");
            if (result?.cancelled) return;
            return toast(result?.error || "Export failed", "error");
        }

        // Browser development fallback.
        const blob = new Blob([content], { type: "application/gpx+xml" });
        const url = URL.createObjectURL(blob);
        const link = document.createElement("a");
        link.href = url;
        link.download = "ghostpin-route.gpx";
        document.body.appendChild(link);
        link.click();
        link.remove();
        URL.revokeObjectURL(url);
        toast("GPX exported");
    } catch (error) {
        toast("Export failed", "error");
    }
}

// ── Joystick ────────────────────────────────────────────────
const _keyMap = { w: "n", a: "w", s: "s", d: "e", arrowup: "n", arrowdown: "s", arrowleft: "w", arrowright: "e" };
let _activeKeys = new Set();
let joystickCommand = Promise.resolve();
function onKeyDown(e) {
    if (e.target.tagName === "INPUT" || e.target.tagName === "TEXTAREA" || e.target.tagName === "SELECT") return;
    if (e.key === "?" || (e.key === "/" && e.shiftKey)) { e.preventDefault(); toggleShortcuts(); return; }
    if (e.key.toLowerCase() === "t") { e.preventDefault(); toggleTeleport(); return; }
    if (e.key.toLowerCase() === "g") { e.preventDefault(); toggleTips(); return; }
    if (e.key === "Escape") { document.querySelectorAll(".inline-form:not(.hidden)").forEach(f => f.classList.add("hidden")); if (!$("shortcuts-overlay").classList.contains("hidden")) toggleShortcuts(); if (!$("tips-overlay").classList.contains("hidden")) toggleTips(); return; }
    if (e.key === "+" || e.key === "=") { map.zoomIn(); return; }
    if (e.key === "-") { map.zoomOut(); return; }
    const dir = _keyMap[e.key.toLowerCase()]; if (!dir) return; e.preventDefault(); _activeKeys.add(dir); const combined = _combineDirections(); if (combined) joystickMove(combined);
}
function onKeyUp(e) { const dir = _keyMap[e.key.toLowerCase()]; if (!dir) return; _activeKeys.delete(dir); if (_activeKeys.size === 0) joystickStop(); else { const combined = _combineDirections(); if (combined) joystickMove(combined); } }
function _combineDirections() { const has = d => _activeKeys.has(d); if (has("n") && has("e")) return "ne"; if (has("n") && has("w")) return "nw"; if (has("s") && has("e")) return "se"; if (has("s") && has("w")) return "sw"; if (has("n")) return "n"; if (has("s")) return "s"; if (has("e")) return "e"; if (has("w")) return "w"; return null; }

function joystickMove(direction) {
    const speed = readSpeedKmh() || selectedSpeed;
    document.querySelectorAll(".joy-btn").forEach(b => b.classList.remove("active"));
    const btn = document.querySelector('.joy-btn[data-dir="' + direction + '"]');
    if (btn) btn.classList.add("active");
    startMovementTracking();
    joystickCommand = joystickCommand.catch(() => {}).then(async () => {
        const r = await fetch("/api/joystick/move", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ direction, speed }) });
        if (!r.ok) throw new Error("Joystick move failed");
    });
    return joystickCommand;
}

function joystickStop() {
    document.querySelectorAll(".joy-btn").forEach(b => b.classList.remove("active"));
    joystickCommand = joystickCommand.catch(() => {}).then(async () => {
        await fetch("/api/joystick/stop", { method: "POST" });
        await pollPosition();
        stopMovementTracking();
    });
    return joystickCommand;
}

// ── Wander ──────────────────────────────────────────────────
async function startWander() { const lat = parseFloat($("lat-input").value), lon = parseFloat($("lon-input").value); if (isNaN(lat) || isNaN(lon)) return toast("Set a location first", "error"); const radius = parseInt($("wander-radius").value, 10) || 200; const speed = readSpeedKmh() || selectedSpeed; try { const r = await fetch("/api/wander/start", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ lat, lon, radius, speed }) }); const d = await r.json(); if (r.ok) { $("btn-wander-start").disabled = true; $("btn-wander-stop").disabled = false; toast("Wandering within " + radius + "m"); startMovementTracking(); } else toast(d.error || "Failed", "error"); } catch (e) { toast("Error", "error"); } }
async function stopWander() { try { await fetch("/api/wander/stop", { method: "POST" }); $("btn-wander-start").disabled = false; $("btn-wander-stop").disabled = true; toast("Wander stopped"); stopMovementTracking(); } catch (e) { toast("Error", "error"); } }
async function generateCircularRoute() { const lat = parseFloat($("lat-input").value), lon = parseFloat($("lon-input").value); if (isNaN(lat) || isNaN(lon)) return toast("Set a location first", "error"); const radius = parseInt($("wander-radius").value, 10) || 200; try { const r = await fetch("/api/route/circular", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ lat, lon, radius }) }); const d = await r.json(); if (r.ok) { clearRoutePoints(); d.waypoints.forEach(wp => addRoutePoint(wp.lat, wp.lng)); toast("Circular route: " + d.count + " points"); } else toast(d.error || "Failed", "error"); } catch (e) { toast("Error", "error"); } }

// ── Cooldown ────────────────────────────────────────────────
async function pollCooldown() {
    try { const r = await fetch("/api/cooldown"); const d = await r.json(); const badge = $("cooldown-badge"), timeEl = $("cooldown-time"), bar = $("cooldown-bar");
    if (d.active) { const mins = Math.floor(d.remaining_seconds / 60), secs = d.remaining_seconds % 60; timeEl.textContent = String(mins).padStart(2, "0") + ":" + String(secs).padStart(2, "0"); const pct = d.total_seconds > 0 ? ((d.total_seconds - d.remaining_seconds) / d.total_seconds * 100) : 0; bar.style.width = pct + "%"; badge.textContent = "WAIT"; badge.className = "cooldown-badge active"; badge.classList.remove("hidden"); }
    else { timeEl.textContent = "00:00"; bar.style.width = "100%"; badge.textContent = "SAFE"; badge.className = "cooldown-badge"; badge.classList.remove("hidden"); }
    if ($("status-cooldown-text")) { $("status-cooldown-text").textContent = d.active ? "WAIT" : "SAFE"; $("status-cooldown-text").style.color = d.active ? "var(--red)" : "var(--green)"; }
    if ($("status-dot")) $("status-dot").classList.toggle("connected", !d.active);
    } catch (e) {}
}

// ── Status bar ──────────────────────────────────────────────
function updateStatusBar() { if ($("status-speed-text")) $("status-speed-text").textContent = formatSpeed(selectedSpeed); }

// ── Stealth / Anti-detection ───────────────────────────────
let _stealthDismissed = false;

async function checkStealth() {
    try {
        const r = await fetch("/api/stealth/check");
        const d = await r.json();
        const banner = $("stealth-banner");
        const stealthPill = $("status-stealth");
        const stealthText = $("status-stealth-text");
        const stealthDot = $("status-stealth-dot");
        // Update status bar pill
        if (stealthPill) {
            if (!d.warnings || !d.warnings.length) {
                stealthText.textContent = "STEALTH"; stealthDot.className = "status-dot connected";
            } else {
                const hasHigh = d.warnings.some(w => w.severity === "high");
                stealthText.textContent = hasHigh ? "EXPOSED" : "RISK";
                stealthDot.className = "status-dot" + (hasHigh ? "" : " warning");
            }
        }
        // Banner
        if (_stealthDismissed) return;
        if (!d.warnings || !d.warnings.length) { banner.classList.add("hidden"); return; }
        const sorted = d.warnings.sort((a, b) => (a.severity === "high" ? -1 : 1));
        $("stealth-banner-text").textContent = sorted[0].message;
        banner.classList.remove("hidden");
        banner.className = "stealth-banner-" + sorted[0].severity;
        // Client-side timezone check
        if (d.spoof_location) {
            const deviceOffsetH = -new Date().getTimezoneOffset() / 60;
            const targetOffsetH = Math.round(d.spoof_location.lon / 15);
            if (Math.abs(deviceOffsetH - targetOffsetH) > 1 && !d.warnings.find(w => w.type === "timezone_mismatch")) {
                toast("Timezone mismatch: your device is UTC" + (deviceOffsetH >= 0 ? "+" : "") + deviceOffsetH + " but target is ~UTC" + (targetOffsetH >= 0 ? "+" : "") + targetOffsetH, "warning");
            }
        }
    } catch (e) {}
}

function toggleTips() { $("tips-overlay").classList.toggle("hidden"); }
function dismissStealthBanner() { $("stealth-banner").classList.add("hidden"); _stealthDismissed = true; }

// ── Profiles ────────────────────────────────────────────────
async function loadProfiles() {
    try { const r = await fetch("/api/profiles"); const profiles = await r.json(); const c = $("profile-list"); c.textContent = "";
    if (!profiles.length) { const e = document.createElement("div"); e.className = "empty-state"; e.textContent = "No profiles"; c.appendChild(e); return; }
    profiles.forEach(p => { const item = document.createElement("div"); item.className = "saved-item"; const n = document.createElement("span"); n.className = "saved-name"; n.textContent = p.name; const co = document.createElement("span"); co.className = "saved-coords"; co.textContent = (p.lat != null ? p.lat.toFixed(2) : "--") + ", " + (p.lon != null ? p.lon.toFixed(2) : "--"); const del = document.createElement("button"); del.className = "saved-del"; del.title = "Delete"; del.textContent = "\u00D7"; del.addEventListener("click", async e => { e.stopPropagation(); await fetch("/api/profiles/" + encodeURIComponent(p.name), { method: "DELETE" }); loadProfiles(); toast('Deleted "' + p.name + '"'); }); item.appendChild(n); item.appendChild(co); item.appendChild(del); item.addEventListener("click", async e => { if (e.target.classList.contains("saved-del")) return; try { const r2 = await fetch("/api/profiles/" + encodeURIComponent(p.name) + "/load", { method: "POST" }); const d = await r2.json(); if (r2.ok) { toast('Profile "' + p.name + '" loaded'); if (d.profile?.lat != null) { placeMarker(d.profile.lat, d.profile.lon); map.flyTo([d.profile.lat, d.profile.lon], 15); } } else toast(d.error || "Failed", "error"); } catch (e2) { toast("Error", "error"); } }); c.appendChild(item); });
    } catch (e) {}
}
function showProfileForm() { $("profile-form").classList.remove("hidden"); $("profile-name").value = ""; $("profile-name").focus(); }
async function confirmSaveProfile() { const name = $("profile-name").value.trim(); if (!name) return toast("Enter a name", "error"); const lat = parseFloat($("lat-input").value), lon = parseFloat($("lon-input").value); try { const r = await fetch("/api/profiles", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ name, lat: isNaN(lat) ? null : lat, lon: isNaN(lon) ? null : lon, speed: readSpeedKmh() || selectedSpeed, route_mode: $("route-mode").value }) }); if (r.ok) { loadProfiles(); toast('Profile "' + name + '" saved'); $("profile-form").classList.add("hidden"); } else { const d = await r.json(); toast(d.error || "Failed", "error"); } } catch (e) { toast("Error", "error"); } }

// ── Schedules ───────────────────────────────────────────────
async function loadSchedules() {
    try { const r = await fetch("/api/schedules"); const schedules = await r.json(); const c = $("schedule-list"); c.textContent = "";
    if (!schedules.length) { const e = document.createElement("div"); e.className = "empty-state"; e.textContent = "No schedules"; c.appendChild(e); return; }
    schedules.forEach(s => { const item = document.createElement("div"); item.className = "saved-item"; const n = document.createElement("span"); n.className = "saved-name"; const dayLabel = s.days?.length ? s.days.map(d => d[0].toUpperCase() + d.slice(1)).join(", ") : "Daily"; n.textContent = s.name + " @ " + s.time; const co = document.createElement("span"); co.className = "saved-coords"; co.textContent = dayLabel + " · " + s.lat.toFixed(2) + ", " + s.lon.toFixed(2); const del = document.createElement("button"); del.className = "saved-del"; del.title = "Delete"; del.textContent = "\u00D7"; del.addEventListener("click", async e => { e.stopPropagation(); await fetch("/api/schedules/" + encodeURIComponent(s.id), { method: "DELETE" }); loadSchedules(); toast("Schedule deleted"); }); item.appendChild(n); item.appendChild(co); item.appendChild(del); c.appendChild(item); });
    } catch (e) {}
}
function showScheduleForm() { const lat = parseFloat($("lat-input").value), lon = parseFloat($("lon-input").value); if (isNaN(lat) || isNaN(lon)) return toast("Set a location first", "error"); $("schedule-form").classList.remove("hidden"); $("schedule-name").value = ""; $("schedule-name").focus(); }
async function confirmAddSchedule() { const name = $("schedule-name").value.trim(); if (!name) return toast("Enter a name", "error"); const time = $("schedule-time").value; if (!time) return toast("Set a time", "error"); const lat = parseFloat($("lat-input").value), lon = parseFloat($("lon-input").value); const days = Array.from(document.querySelectorAll(".day-pill.active")).map(p => p.dataset.day); try { const r = await fetch("/api/schedules", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ name, lat, lon, time, days }) }); if (r.ok) { loadSchedules(); toast("Schedule created"); $("schedule-form").classList.add("hidden"); } else { const d = await r.json(); toast(d.error || "Failed", "error"); } } catch (e) { toast("Error", "error"); } }

// ── Route History ───────────────────────────────────────────
async function loadRouteHistory() {
    try { const r = await fetch("/api/routes"); const routes = await r.json(); const c = $("route-history-list"); c.textContent = "";
    if (!routes.length) { const e = document.createElement("div"); e.className = "empty-state"; e.textContent = "No saved routes"; c.appendChild(e); return; }
    routes.forEach(rt => { const item = document.createElement("div"); item.className = "saved-item"; const n = document.createElement("span"); n.className = "saved-name"; n.textContent = rt.name; const co = document.createElement("span"); co.className = "saved-coords"; co.textContent = rt.distance_km != null ? formatRouteDistance(rt.distance_km) : "?"; const del = document.createElement("button"); del.className = "saved-del"; del.title = "Delete"; del.textContent = "\u00D7"; del.addEventListener("click", async e => { e.stopPropagation(); await fetch("/api/routes/" + encodeURIComponent(rt.id), { method: "DELETE" }); loadRouteHistory(); toast("Route deleted"); }); item.appendChild(n); item.appendChild(co); item.appendChild(del); item.addEventListener("click", e => { if (e.target.classList.contains("saved-del")) return; if (rt.waypoints) { clearRoutePoints(); rt.waypoints.forEach(wp => addRoutePoint(wp.lat, wp.lng)); if (rt.waypoints.length) map.flyTo([rt.waypoints[0].lat, rt.waypoints[0].lng], 14); toast('Loaded "' + rt.name + '"'); } }); c.appendChild(item); });
    } catch (e) {}
}
async function saveCurrentRoute() { if (routePoints.length < 2) return toast("No route to save", "error"); const name = prompt("Route name:"); if (!name?.trim()) return; try { const r = await fetch("/api/routes", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ name: name.trim(), waypoints: routePoints, speed: selectedSpeed, mode: $("route-mode").value, distance_km: routeDistanceKm }) }); if (r.ok) { loadRouteHistory(); toast('Route "' + name.trim() + '" saved'); $("btn-route-save").style.display = "none"; } } catch (e) { toast("Error", "error"); } }
