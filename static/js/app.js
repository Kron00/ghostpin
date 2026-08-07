// NOTE: The one remaining innerHTML usage (search item icon SVG) uses only
// static markup — no user input is interpolated. All user-supplied text uses
// textContent or DOM creation methods to prevent XSS.

// ── State ────────────────────────────────────────────────────
let map, marker, userLocationMarker, routeLine, routeDisplayLine;
let routePoints = [];
let routeMarkers = [];
let routePolling = null;
let lastRouteStatus = null;
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
let calculatedRouteSummary = null;
let calculatedRouteHolds = null;
let activeSpoofLocation = null;
// Unit is remembered once chosen; before that it follows the country the IP
// lookup reports, so someone in the US is not handed km/h to correct.
let speedUnit = localStorage.getItem("speed_unit") === "mph" ? "mph"
              : (localStorage.getItem("speed_unit") === "kmh" ? "kmh" : null);

function applyDetectedSpeedUnit(unit) {
    if (unit !== "mph" && unit !== "kmh") return;
    if (roamUnitMiles === null) {
        roamUnitMiles = unit === "mph";
        if (typeof setRoamUnitBounds === "function") setRoamUnitBounds();
        if (typeof updateRoamUI === "function") updateRoamUI();
    }
    if (speedUnit !== null) return;
    speedUnit = unit;
    const toggle = $("speed-unit-toggle");
    if (toggle) toggle.textContent = unit === "mph" ? "mph" : "km/h";
    setSpeedInputFromKmh(selectedSpeed);
    updateStatusBar();
}
let speedUpdateTimer = null;

const KMH_PER_MPH = 1.609344;

function routeFollowZoomForSpeed(speed) {
    // Close enough to watch the dot actually move. Tiles go to 19, so even the
    // fast tier stays tight rather than pulling out to a city view.
    if (speed <= 30) return 18;
    if (speed <= 70) return 17;
    return 17;
}

function displaySpeedFromKmh(speedKmh) {
    const value = speedUnitOrDefault() === "mph" ? speedKmh / KMH_PER_MPH : speedKmh;
    return Math.abs(value - Math.round(value)) < 0.05 ? String(Math.round(value)) : value.toFixed(1);
}

function speedUnitOrDefault() { return speedUnit || "kmh"; }

function readSpeedKmh() {
    const value = parseFloat($("speed-input")?.value);
    if (!Number.isFinite(value) || value <= 0) return null;
    return speedUnitOrDefault() === "mph" ? value * KMH_PER_MPH : value;
}

function setSpeedInputFromKmh(speedKmh) {
    const input = $("speed-input");
    if (!input) return;
    input.value = displaySpeedFromKmh(speedKmh);
    input.min = speedUnitOrDefault() === "mph" ? "0.6" : "1";
    input.max = speedUnitOrDefault() === "mph" ? "186.4" : "300";
    input.step = "0.1";
}

function setSelectedSpeed(speedKmh, updateInput = true) {
    selectedSpeed = Math.max(1, Math.min(300, speedKmh));
    if (updateInput) setSpeedInputFromKmh(selectedSpeed);
    updateStatusBar();
}

function formatSpeed(speedKmh) {
    return displaySpeedFromKmh(speedKmh) + " " + (speedUnitOrDefault() === "mph" ? "mph" : "km/h");
}

function formatRouteDistance(distanceKm) {
    return speedUnitOrDefault() === "mph" ? (distanceKm * 0.621371).toFixed(1) + " mi" : distanceKm.toFixed(1) + " km";
}

function finiteNumber(value) {
    if (value == null || value === "") return null;
    const number = Number(value);
    return Number.isFinite(number) ? number : null;
}

function formatEtaSeconds(value) {
    const parsed = finiteNumber(value);
    if (parsed == null || parsed < 0) return null;
    const seconds = Math.round(parsed);
    if (seconds < 60) return seconds + "s";
    if (seconds < 3600) return Math.max(1, Math.round(seconds / 60)) + "m";
    let hours = Math.floor(seconds / 3600);
    let minutes = Math.round((seconds % 3600) / 60);
    if (minutes === 60) { hours += 1; minutes = 0; }
    return hours + "h " + minutes + "m";
}

function queueLiveSpeedUpdate(speedKmh, immediate = false) {
    clearTimeout(speedUpdateTimer);
    // Nothing to tell the phone if there is no phone. The speed is applied
    // when a route starts anyway.
    if (!deviceReady()) return;
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
    speedUnit = speedUnitOrDefault() === "kmh" ? "mph" : "kmh";
    localStorage.setItem("speed_unit", speedUnit);
    setSelectedSpeed(speedKmh);
    const toggle = $("speed-unit-toggle");
    toggle.textContent = speedUnitOrDefault() === "mph" ? "mph" : "km/h";
    toggle.title = speedUnitOrDefault() === "mph" ? "Switch speed units to kilometers per hour" : "Switch speed units to miles per hour";
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
    // This check only compares location signals, so its controls should not
    // imply that iOS cannot identify a software-simulated position.
    $("btn-stealth").title = "Location consistency guide";
    $("btn-stealth").setAttribute("aria-label", "Open location consistency guide");
    $("status-stealth-text").textContent = "CHECK NOT RUN";
    $("status-stealth-dot").className = "status-dot";
    $("status-stealth").title = "Compares the simulated location, IP-based location, and approximate timezone";
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
            applyDetectedSpeedUnit(savedHome.speed_unit);
        }
    } catch (e) {}
    try {
        const r = await fetch("/api/default-location");
        const d = await r.json();
        if (d.available && d.lat != null && d.lon != null) {
            startupLocation = d;
            startLat = d.lat; startLon = d.lon; startZoom = 13;
            localStorage.setItem("last_home", JSON.stringify(d));
            applyDetectedSpeedUnit(d.speed_unit);
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
    ["lat-input", "lon-input"].forEach(id => $(id).addEventListener("input", syncReadoutFromInputs));
    $("btn-route-start").addEventListener("click", startRoute);
    $("btn-route-stop").addEventListener("click", stopRoute);
    $("btn-route-pause").addEventListener("click", pauseRoute);
    $("btn-route-resume").addEventListener("click", resumeRoute);
    $("btn-route-clear").addEventListener("click", clearRoutePoints);
    $("btn-route-calculate").addEventListener("click", calculateAddressRoute);
    $("btn-add-stop").addEventListener("click", addStop);
    $("btn-swap-stops").addEventListener("click", reverseStops);
    document.querySelectorAll(".mode-tab").forEach(tab => {
        tab.addEventListener("click", () => setBuildMode(tab.dataset.build));
    });
    renderStops();

    $("close-loop").addEventListener("change", event => {
        closeLoop = event.target.checked;
        renderStopsOnMap();
        invalidateCalculatedRoute();
        updateRouteUI();
    });
    $("mode-flat").addEventListener("click", () => setDriveMode(false));
    $("mode-realistic").addEventListener("click", () => setDriveMode(true));
    updateAdaptiveUI();

    $("roam-radius-slider").addEventListener("input", () => syncRoamRadius("slider"));
    $("btn-build-help").addEventListener("click", () => $("build-help").classList.remove("hidden"));
    $("btn-build-help-close").addEventListener("click", () => $("build-help").classList.add("hidden"));
    $("build-help").addEventListener("click", event => {
        if (event.target === $("build-help")) $("build-help").classList.add("hidden");
    });

    $("btn-roam-center").addEventListener("click", () => setMapPick("roam"));
    // Do not pass the click event into startRoaming's `silent` parameter. A
    // MouseEvent is truthy, which made a normal first start skip close-follow,
    // the initial toast, and continuation-state reset as if it were a seam.
    $("btn-roam-start").addEventListener("click", () => startRoaming());
    $("btn-roam-stop").addEventListener("click", stopRoaming);
    $("roam-radius").addEventListener("input", () => syncRoamRadius("field"));
    $("roam-unit-toggle").addEventListener("click", () => {
        // Convert the number so the distance on the ground does not jump.
        const metres = roamRadiusMetres();
        roamUnitMiles = roamUnitMiles === false;
        localStorage.setItem("roam_unit", roamUnitMiles ? "mi" : "km");
        if (metres) {
            const converted = roamUnitMiles ? metres / METRES_PER_MILE : metres / 1000;
            $("roam-radius").value = converted.toFixed(2);
        }
        setRoamUnitBounds(); syncRoamRadius("field");
    });
    setRoamUnitBounds();
    updateRoamUI();
    $("btn-map-pick").addEventListener("click", () => setMapPick(mapPickTarget === "append" ? null : "append"));
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

    // Joystick
    document.querySelectorAll(".joy-btn[data-dir]").forEach(btn => {
        btn.addEventListener("mousedown", () => joystickMove(btn.dataset.dir));
        btn.addEventListener("mouseup", joystickStop);
        btn.addEventListener("mouseleave", joystickStop);
    });
    $("btn-joy-stop").addEventListener("click", joystickStop);
    document.querySelectorAll("[data-ob-next]").forEach(el =>
        el.addEventListener("click", () => obNext(parseInt(el.dataset.obNext, 10))));
    document.querySelectorAll("[data-ob-skip]").forEach(el => el.addEventListener("click", obSkip));
    document.querySelectorAll("[data-ob-done]").forEach(el => el.addEventListener("click", obDone));

    document.addEventListener("keydown", onKeyDown);
    document.addEventListener("keyup", onKeyUp);

    // Speed
    $("speed-input").addEventListener("input", () => {
        const speedKmh = readSpeedKmh();
        // Showing 999 while moving at 32 is worse than correcting the field.
        if (speedKmh != null && speedKmh > 300) { setSpeedInputFromKmh(300); setSelectedSpeed(300); return; }
        if (speedKmh == null) return;
        setSelectedSpeed(speedKmh, false);
        queueLiveSpeedUpdate(selectedSpeed);
    });
    $("speed-input").addEventListener("change", () => setSpeedInputFromKmh(selectedSpeed));
    $("speed-unit-toggle").addEventListener("click", toggleSpeedUnit);
    $("speed-unit-toggle").textContent = speedUnitOrDefault() === "mph" ? "mph" : "km/h";
    $("speed-unit-toggle").title = speedUnitOrDefault() === "mph" ? "Switch speed units to kilometers per hour" : "Switch speed units to miles per hour";
    setSelectedSpeed(selectedSpeed);

    // Close dropdowns on outside click
    document.addEventListener("click", e => {
        if (!e.target.closest(".search-container")) $("search-results").classList.remove("visible");
        if (!e.target.closest("#device-badge") && !e.target.closest("#device-dropdown")) $("device-dropdown")?.classList.add("hidden");
    });

    // Tabs
    document.querySelectorAll(".tab-btn").forEach(btn => {
        btn.addEventListener("click", () => setPlacesTab(btn.dataset.tab));
    });

    // HUD Panels
    document.querySelectorAll(".hud-panel-header").forEach(header => {
        header.addEventListener("click", e => { if (!e.target.closest(".hud-panel-close")) togglePanel(header.dataset.panel); });
    });
    document.querySelectorAll(".hud-panel-close").forEach(btn => { btn.addEventListener("click", () => togglePanel(btn.dataset.panel)); });
    restorePanelStates();
    setPlacesTab(localStorage.getItem("places_tab") || "recent");

    // Position Places panel
    positionPlacesPanel();
    new ResizeObserver(positionPlacesPanel).observe($("panel-location"));
    new ResizeObserver(positionPlacesPanel).observe($("panel-places"));
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

    $("btn-stealth").addEventListener("click", toggleTips);
    $("btn-tips-close").addEventListener("click", toggleTips);
    $("tips-overlay").addEventListener("click", e => { if (e.target === $("tips-overlay")) toggleTips(); });
    $("stealth-banner-close").addEventListener("click", dismissStealthBanner);
    $("stealth-banner-tip").addEventListener("click", () => { dismissStealthBanner(); toggleTips(); });
    $("status-stealth")?.addEventListener("click", toggleTips);

    // Device dropdown & connect buttons
    $("device-badge").addEventListener("click", toggleDeviceDropdown);
    $("device-badge").addEventListener("keydown", event => {
        if (event.key !== "Enter" && event.key !== " ") return;
        event.preventDefault();
        $("device-badge").click();
    });
    $("btn-connect").addEventListener("click", () => connectDevice(false));
    $("btn-connect-wifi").addEventListener("click", () => connectDevice(true));

    // Follow mode
    $("follow-mode")?.addEventListener("change", e => { followMode = e.target.checked; });

    // Route save
    $("btn-route-save")?.addEventListener("click", openRouteSaveForm);
    $("btn-route-save-open").addEventListener("click", openRouteSaveForm);
    $("btn-route-save-confirm").addEventListener("click", saveCurrentRoute);
    $("btn-route-save-cancel").addEventListener("click", () => $("route-save-form").classList.add("hidden"));
    $("route-save-name").addEventListener("keydown", e => { if (e.key === "Enter") saveCurrentRoute(); });

    // Coord HUD
    map.on("mousemove", e => { const h = $("coord-hud"); if (h) { h.classList.remove("hidden"); $("coord-hud-text").textContent = e.latlng.lat.toFixed(6) + ", " + e.latlng.lng.toFixed(6); } });
    map.on("mouseout", () => { $("coord-hud")?.classList.add("hidden"); });

    // Load data
    pollDevice(); loadSaved(); loadProfiles(); loadSchedules(); loadRouteHistory(); loadRecent(); renderPopular(); updateStatusBar(); checkStealth();
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
function togglePanel(panelId) {
    const p = $(panelId); if (!p) return;
    p.classList.toggle("collapsed");
    const collapsed = p.classList.contains("collapsed");
    const name = (p.querySelector(".hud-panel-header > span")?.textContent || "panel").toLowerCase();
    p.querySelectorAll(".hud-panel-close").forEach(btn => {
        btn.setAttribute("aria-label", (collapsed ? "Expand " : "Collapse ") + name + " panel");
    }); localStorage.setItem("panel_" + panelId, p.classList.contains("collapsed") ? "collapsed" : "open"); positionPlacesPanel(); }

// The left column is two stacked fixed panels, so their heights have to be
// shared out by hand. Collapsing Places hands its space to Location rather
// than leaving a gap: the point of collapsing one is to see more of the other.
function positionPlacesPanel() {
    const loc = $("panel-location"), places = $("panel-places");
    if (!loc || !places) return;
    if (window.matchMedia("(max-width: 940px)").matches) {
        // Panels are in normal flow at this width; leave the layout alone.
        loc.style.maxHeight = ""; places.style.top = ""; places.style.maxHeight = "";
        return;
    }

    const margin = 16, gap = 6;
    const top = loc.getBoundingClientRect().top;
    const available = window.innerHeight - top - margin;
    const collapsed = places.classList.contains("collapsed");

    // Measure what Places actually needs before deciding Location's cap.
    places.style.maxHeight = "none";
    const placesHeight = places.offsetHeight;

    if (collapsed) {
        loc.style.maxHeight = Math.max(220, available - placesHeight - gap) + "px";
    } else {
        loc.style.maxHeight = "";   // back to the CSS cap so both get a share
    }

    const placesTop = loc.getBoundingClientRect().bottom + gap;
    places.style.top = placesTop + "px";
    if (!collapsed) {
        places.style.maxHeight = Math.max(120, window.innerHeight - placesTop - margin) + "px";
    }
}
function restorePanelStates() { ["panel-location","panel-places","panel-movement"].forEach(id => { if (localStorage.getItem("panel_" + id) === "collapsed") $(id)?.classList.add("collapsed"); }); }

// Remember which Places tab (Recent/Saved/Popular) was open so a restart lands
// back on it — otherwise a saved place looks lost under the default Recent tab.
function setPlacesTab(name) {
    if (!["recent", "saved", "popular"].includes(name)) name = "recent";
    document.querySelectorAll(".tab-btn").forEach(b => b.classList.toggle("active", b.dataset.tab === name));
    document.querySelectorAll(".tab-content").forEach(c => c.classList.toggle("active", c.id === "tab-" + name));
    localStorage.setItem("places_tab", name);
}

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
function toggleCoordFormat() {
    coordFormat = coordFormat === "dd" ? "dms" : "dd";
    localStorage.setItem("coord_fmt", coordFormat);
    $("btn-coord-fmt").textContent = coordFormat.toUpperCase();
    applyCoordInputType();
    const source = marker ? { lat: marker.getLatLng().lat, lng: marker.getLatLng().lng } : lastCoords;
    if (source) updateCoordInputs(source.lat, source.lng);
}
function toDMS(deg, isLon) { const dir = isLon ? (deg >= 0 ? "E" : "W") : (deg >= 0 ? "N" : "S"); deg = Math.abs(deg); const d = Math.floor(deg); const m = Math.floor((deg - d) * 60); const s = ((deg - d - m / 60) * 3600).toFixed(1); return d + "\u00B0" + m + "'" + s + '"' + dir; }
let lastCoords = null;

// Keep the readout honest while the fields are being edited by hand.
function syncReadoutFromInputs() {
    const readout = $("coord-text");
    if (!readout) return;
    const latRaw = $("lat-input").value.trim();
    const lonRaw = $("lon-input").value.trim();
    if (!latRaw && !lonRaw) {
        readout.textContent = "Click the map to set a location";
        readout.classList.remove("coord-glow");
        lastCoords = null;
        return;
    }
    const lat = parseFloat(latRaw), lon = parseFloat(lonRaw);
    // In DMS the fields hold text the readout cannot render; leave it be
    // rather than showing something wrong.
    if (coordFormat !== "dd" || Number.isNaN(lat) || Number.isNaN(lon)) return;
    if (Math.abs(lat) > 90 || Math.abs(lon) > 180) return;
    readout.textContent = lat.toFixed(6) + ", " + lon.toFixed(6);
    readout.classList.add("coord-glow");
    lastCoords = { lat, lng: lon };
}


// A number input silently drops 48°51'29.7"N, so the field type has to follow
// the coordinate format.
function applyCoordInputType() {
    const type = coordFormat === "dms" ? "text" : "number";
    ["lat-input", "lon-input"].forEach(id => { if ($(id) && $(id).type !== type) $(id).type = type; });
}

function updateCoordInputs(lat, lng) {
    lastCoords = { lat, lng };
    applyCoordInputType(); if (coordFormat === "dms") { $("lat-input").value = toDMS(lat, false); $("lon-input").value = toDMS(lng, true); } else { $("lat-input").value = lat.toFixed(6); $("lon-input").value = lng.toFixed(6); } if ($("coord-text")) { $("coord-text").textContent = lat.toFixed(6) + ", " + lng.toFixed(6); $("coord-text").classList.add("coord-glow"); } }

// ── Teleport ────────────────────────────────────────────────
function toggleTeleport() { teleportMode = !teleportMode; $("btn-teleport").classList.toggle("active", teleportMode); toast(teleportMode ? "Teleport ON \u2014 click map to move instantly" : "Teleport OFF", teleportMode ? "success" : "error"); }

// ── Shortcuts ───────────────────────────────────────────────
function toggleShortcuts() { $("shortcuts-overlay").classList.toggle("hidden"); }

// ── Map ─────────────────────────────────────────────────────
function onMapClick(e) {
    // Picking wins over everything else: the click was asked for.
    if (mapPickTarget !== null) { handleMapPick(e.latlng.lat, e.latlng.lng); return; }
    if (e.originalEvent.shiftKey || routePoints.length > 0) { addRoutePoint(e.latlng.lat, e.latlng.lng); }
    else if (teleportMode) { placeMarker(e.latlng.lat, e.latlng.lng); teleportTo(e.latlng.lat, e.latlng.lng); }
    else { placeMarker(e.latlng.lat, e.latlng.lng); }
}

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
function coordsInRange(lat, lon) {
    return Number.isFinite(lat) && Number.isFinite(lon)
        && Math.abs(lat) <= 90 && Math.abs(lon) <= 180;
}

async function teleportTo(lat, lon) {
    if (!coordsInRange(lat, lon)) return toast("Latitude must be within ±90 and longitude within ±180", "error");
    storePreviousLocation();
    try { const r = await fetch("/api/location/set", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ lat, lon }) }); if (r.ok) { activeSpoofLocation = { lat, lon }; adoptDotAsRoamCentre(true); toast("Teleported to " + lat.toFixed(4) + ", " + lon.toFixed(4)); addToRecent(lat, lon); _stealthDismissed = false; checkStealth(); } else { const d = await r.json(); toast(d.error || "Failed", "error"); } } catch (e) { toast("Connection error", "error"); }
}

// ── Location set/clear ──────────────────────────────────────
async function setLocation() {
    const lat = parseFloat($("lat-input").value), lon = parseFloat($("lon-input").value);
    if (isNaN(lat) || isNaN(lon)) return toast("Place a marker on the map first", "error");
    // Catch an impossible coordinate here rather than asking the phone to
    // reject it after a round trip.
    if (!coordsInRange(lat, lon)) return toast("Latitude must be within ±90 and longitude within ±180", "error");
    storePreviousLocation();
    try { const r = await fetch("/api/location/set", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ lat, lon }) }); const d = await r.json(); if (r.ok) { activeSpoofLocation = { lat, lon }; adoptDotAsRoamCentre(true); toast("Location set: " + lat.toFixed(4) + ", " + lon.toFixed(4)); placeMarker(lat, lon); addToRecent(lat, lon); _stealthDismissed = false; checkStealth(); } else toast(d.error || "Failed", "error"); } catch (e) { toast("Connection error", "error"); }
}

async function clearLocation() {
    try { const r = await fetch("/api/location/clear", { method: "POST" }); if (r.ok) { activeSpoofLocation = null; toast("Reset to real GPS"); if (marker) { map.removeLayer(marker); marker = null; } $("lat-input").value = ""; $("lon-input").value = ""; if ($("coord-text")) { $("coord-text").textContent = "Click the map to set a location"; $("coord-text").classList.remove("coord-glow"); } if (startupLocation) goToUserLocation(); _stealthDismissed = false; await checkStealth(); } else { const d = await r.json().catch(() => ({})); toast(d.error || "Failed to reset", "error"); } } catch (e) { toast("Connection error", "error"); }
}

// ── Undo ────────────────────────────────────────────────────
function storePreviousLocation() { if (activeSpoofLocation) { previousLocation = { ...activeSpoofLocation }; $("btn-undo").disabled = false; $("btn-undo").title = "Undo to " + previousLocation.lat.toFixed(4) + ", " + previousLocation.lon.toFixed(4); } }
async function undoTeleport() {
    if (!previousLocation) return toast("No previous location", "error");
    try { const r = await fetch("/api/location/set", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(previousLocation) }); if (r.ok) { activeSpoofLocation = { ...previousLocation }; adoptDotAsRoamCentre(true); toast("Undone \u2014 back to " + previousLocation.lat.toFixed(4) + ", " + previousLocation.lon.toFixed(4)); placeMarker(previousLocation.lat, previousLocation.lon); map.flyTo([previousLocation.lat, previousLocation.lon], map.getZoom(), { duration: 0.8 }); previousLocation = null; $("btn-undo").disabled = true; $("btn-undo").title = "No previous location"; _stealthDismissed = false; checkStealth(); } } catch (e) { toast("Undo failed", "error"); }
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
            const actions = document.createElement("div");
            actions.className = "search-actions";

            // Split control: one click adds the place where it would naturally
            // go; the caret is there for the rare "no, make it the destination".
            const target = smartAddTarget();
            const split = document.createElement("div");
            split.className = "split-btn";

            const add = document.createElement("button");
            add.type = "button";
            add.className = "icon-btn";
            add.textContent = target ? "+ " + stopLabel(target.index) : "+";
            add.title = target ? "Add as stop " + stopLabel(target.index) : "Route is full";
            add.setAttribute("aria-label", add.title);
            add.disabled = !target;
            add.addEventListener("click", event => {
                event.stopPropagation();
                addPlaceToRoute(res);
                c.classList.remove("visible");
            });

            const caret = document.createElement("button");
            caret.type = "button";
            caret.className = "icon-btn";
            caret.textContent = "▾";
            caret.title = "Choose which stop to set";
            caret.setAttribute("aria-label", caret.title);
            caret.addEventListener("click", event => {
                event.stopPropagation();
                openAssignMenu(caret, res);
            });

            split.append(add, caret);
            actions.appendChild(split);

            const googleLink = document.createElement("button");
            googleLink.className = "icon-btn search-google-link";
            googleLink.type = "button";
            googleLink.title = "Open this place in Google Maps";
            googleLink.setAttribute("aria-label", googleLink.title);
            googleLink.textContent = "↗";
            googleLink.addEventListener("click", event => {
                event.stopPropagation();
                const url = "https://www.google.com/maps/search/?api=1&query=" + encodeURIComponent(res.display_name || (res.lat + "," + res.lon));
                window.open(url, "_blank", "noopener");
            });
            actions.appendChild(googleLink);

            item.appendChild(iconDiv); item.appendChild(content); item.appendChild(source); item.appendChild(actions);
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
    // On load nobody pressed anything, so the controls stay live and only the
    // status line reports the search. Disabling them made it look stuck.
    const status = $("connect-status");
    if (status) status.textContent = "Looking for your iPhone…";
    try {
        const r = await fetch("/api/device/connect", {
            method: "POST", headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ wifi: false }),
        });
        const d = await r.json().catch(() => ({}));
        if (r.ok) {
            toast("Connected via " + d.connection_type + " (" + (d.tunnel_mode || "local") + ")");
            if (status) status.textContent = "";
            pollDevice();
        } else if (status) {
            status.textContent = d.error || "No iPhone found. Plug one in and press Connect USB.";
        }
    } catch (e) {
        if (status) status.textContent = "No iPhone found. Plug one in and press Connect USB.";
    }
}

// ── Connection ──────────────────────────────────────────────
async function connectDevice(wifi = false, udid = null) {
    const status = $("connect-status"), btnU = $("btn-connect"), btnW = $("btn-connect-wifi");
    const origU = btnU?.textContent, origW = btnW?.textContent;
    // Only the control that was pressed shows the loading state; the other
    // stays available so the alternative transport can still be tried.
    const active = wifi ? btnW : btnU;
    if (active) { active.disabled = true; active.classList.add("btn-loading"); }
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
function showSaveForm() {
    setTimeout(() => revealForm("save-form"), 0); const lat = parseFloat($("lat-input").value), lon = parseFloat($("lon-input").value); if (isNaN(lat) || isNaN(lon)) return toast("Place a marker first", "error"); $("save-form").classList.remove("hidden"); $("save-name").value = ""; $("save-name").focus(); }
async function confirmSaveLocation() { const name = $("save-name").value.trim(); if (!name) return toast("Enter a name", "error"); const lat = parseFloat($("lat-input").value), lon = parseFloat($("lon-input").value); const cat = document.querySelector(".cat-pill.active")?.dataset.cat || "default"; try { const r = await fetch("/api/saved", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ name, lat, lon, category: cat }) }); if (r.ok) { loadSaved(); setPlacesTab("saved"); toast('Saved "' + name + '"'); $("save-form").classList.add("hidden"); } else { const d = await r.json(); toast(d.error || "Failed", "error"); } } catch (e) { toast("Connection error", "error"); } }

// ── Route / Movement ────────────────────────────────────────
function addRoutePoint(lat, lng, preserveCalculated = false) { if (!preserveCalculated) { calculatedRouteCoordinates = null; calculatedRouteProvider = null; calculatedRouteSummary = null; calculatedRouteHolds = null; } routePoints.push({ lat, lng }); const m = L.circleMarker([lat, lng], { radius: 6, color: "#6F999A", fillColor: "#6F999A", fillOpacity: 1, weight: 0 }).addTo(map); m.bindTooltip(String(routePoints.length), { permanent: true, direction: "center", className: "route-label" }); routeMarkers.push(m); if (routePoints.length >= 2) { if (routeLine) map.removeLayer(routeLine); routeLine = L.polyline(routePoints.map(p => [p.lat, p.lng]), { color: "#6F999A", weight: 2, dashArray: "8 6", opacity: 0.6 }).addTo(map); } updateRouteUI(); }
// Drawing a calculated route replaces the geometry but must not throw away
// the itinerary that produced it.
function clearRouteGeometry() {
    routePoints = []; calculatedRouteCoordinates = null; calculatedRouteProvider = null; calculatedRouteSummary = null; calculatedRouteHolds = null;
    routeMarkers.forEach(m => map.removeLayer(m)); routeMarkers = [];
    if (routeLine) { map.removeLayer(routeLine); routeLine = null; }
    if (routeDisplayLine) { map.removeLayer(routeDisplayLine); routeDisplayLine = null; }
    if (routeTraveledLine) { map.removeLayer(routeTraveledLine); routeTraveledLine = null; }
    $("route-progress").classList.add("hidden");
}

// Clear means the whole plan: geometry, stops and everything drawn for them.
function clearRoutePoints() {
    clearRouteGeometry();
    clearStopsOnMap();
    routeStops = [{ text: "", place: null }, { text: "", place: null }];
    setMapPick(null);
    renderStops();
    const addressStatus = $("route-address-status");
    if (addressStatus) { addressStatus.textContent = ""; addressStatus.className = "route-address-status hidden"; }
    $("route-save-form")?.classList.add("hidden");
    updateRouteUI();
}
function updateRouteUI() {
    // With no calculated geometry the stops themselves are the route, so the
    // server can join them. Keeps Start live as soon as a plan exists.
    const placed = routeStops.filter(stop => stop.place && stop.place.lat != null);
    if (!calculatedRouteCoordinates && placed.length >= 2) {
        routePoints = placed.map(stop => ({ lat: stop.place.lat, lng: stop.place.lon }));
        if (closeLoop && routePoints.length >= 2) routePoints.push({ ...routePoints[0] });
    }
    const ready = routePoints.length >= 2;
    $("btn-route-start").disabled = !ready;
    const save = $("btn-route-save-open");
    if (save) save.disabled = !ready;
    $("route-hint").textContent = ready
        ? routePoints.length + " point" + (routePoints.length > 1 ? "s" : "") + " ready"
        : "Add at least two stops to build a route";
}

// ── Address autocomplete ─────────────────────────────────────
// The top-bar search already resolves places; route fields deserve the same
// rather than making people type an address exactly right and hope.

function attachAutocomplete(input, resultsEl, onPick) {
    let timer = null, items = [], cursor = -1, controller = null;

    const close = () => { resultsEl.classList.remove("visible"); cursor = -1; };

    const render = () => {
        resultsEl.textContent = "";
        if (!items.length) {
            const empty = document.createElement("div");
            empty.className = "ac-empty";
            empty.textContent = "No matches";
            resultsEl.appendChild(empty);
        } else {
            items.forEach((place, index) => {
                const row = document.createElement("div");
                row.className = "ac-item" + (index === cursor ? " highlighted" : "");
                row.textContent = place.name || place.display_name;
                if (place.subtitle) {
                    const sub = document.createElement("small");
                    sub.textContent = place.subtitle;
                    row.appendChild(sub);
                }
                row.addEventListener("mousedown", event => {
                    event.preventDefault();
                    input.value = place.name || place.display_name;
                    close();
                    if (onPick) onPick(place);
                });
                resultsEl.appendChild(row);
            });
        }
        resultsEl.classList.add("visible");
    };

    const search = async query => {
        if (controller) controller.abort();
        controller = new AbortController();
        const centre = map ? map.getCenter() : null;
        const params = new URLSearchParams({ q: query });
        if (centre) { params.set("lat", centre.lat); params.set("lon", centre.lng); params.set("zoom", map.getZoom()); }
        try {
            const response = await fetch("/api/search?" + params, { signal: controller.signal });
            if (!response.ok) return close();
            const data = await response.json();
            items = Array.isArray(data) ? data.slice(0, 6) : [];
            cursor = -1;
            render();
        } catch (error) { /* aborted or offline */ }
    };

    input.addEventListener("input", () => {
        clearTimeout(timer);
        const query = input.value.trim();
        if (query.length < 3) return close();
        timer = setTimeout(() => search(query), 220);
    });

    input.addEventListener("keydown", event => {
        if (!resultsEl.classList.contains("visible")) return;
        if (event.key === "ArrowDown") { event.preventDefault(); cursor = Math.min(cursor + 1, items.length - 1); render(); }
        else if (event.key === "ArrowUp") { event.preventDefault(); cursor = Math.max(cursor - 1, 0); render(); }
        else if (event.key === "Enter" && cursor >= 0) {
            event.preventDefault();
            const place = items[cursor];
            input.value = place.name || place.display_name;
            close();
            if (onPick) onPick(place);
        } else if (event.key === "Escape") { close(); }
    });

    input.addEventListener("blur", () => setTimeout(close, 120));
}

// ── Multi-stop builder ───────────────────────────────────────

let routeStops = [{ text: "", place: null }, { text: "", place: null }];

// Stops are lettered in order: A, B, C, D… Adding a stop adds the next letter,
// which is also what the buttons on each search result offer.
function stopLabel(index) { return String.fromCharCode(65 + index); }

function stopPlaceholder(index) {
    if (index === 0) return "Start";
    if (index === routeStops.length - 1) return "Destination";
    return "Stop " + stopLabel(index);
}

// Fill a stop from a search result and show the result in the builder.
function setStopFromPlace(index, place) {
    if (index < 0 || index >= routeStops.length) return;
    const label = place.name || place.display_name;
    routeStops[index] = { text: label, place };
    invalidateCalculatedRoute();
    // Jumping to Stops mid-pick hides the "Stop picking" control while it is
    // still armed, so stay put while appending from the map.
    if (mapPickTarget !== "append") setBuildMode("stops");
    renderStops();
    // Draw it, but leave the view exactly where it was: moving the map out
    // from under someone mid-plan is disorienting.
    renderStopsOnMap();
    toast("Stop " + stopLabel(index) + " set to " + label);
}

// Routes get built in order, so the one-click action fills the first empty
// stop and only adds a new one when they are all taken. Picking an exact slot
// is the rare case and lives behind the caret.
function smartAddTarget() {
    const empty = routeStops.findIndex(stop => !stop.text.trim());
    if (empty !== -1) return { index: empty, isNew: false };
    if (routeStops.length >= 10) return null;
    return { index: routeStops.length, isNew: true };
}

function addPlaceToRoute(place) {
    const target = smartAddTarget();
    if (!target) return toast("A route can have at most 10 stops", "error");
    if (target.isNew) routeStops.push({ text: "", place: null });
    setStopFromPlace(target.index, place);
}

// ── Picking stops off the map ────────────────────────────────
// A stop can come from search, from typing, or from pointing at the map.
// While picking is armed the map click means "put it here" and nothing else,
// so it can't be confused with placing the marker or teleporting.

let mapPickTarget = null;   // null | "append" | stop index

function setMapPick(target) {
    mapPickTarget = target;
    const armed = target !== null;
    document.body.classList.toggle("map-picking", armed);
    const toggle = $("btn-map-pick");
    if (toggle) {
        toggle.classList.toggle("active", target === "append");
        toggle.textContent = target === "append" ? "Stop picking" : "Click the map to add stops";
    }
    renderStops();
    if (target === "roam") {
        $("route-hint").textContent = "Click the map to set the centre of the area. Esc to cancel.";
    } else if (armed) {
        $("route-hint").textContent = target === "append"
            ? "Click the map to add a stop. Esc to stop."
            : "Click the map to set stop " + stopLabel(target) + ". Esc to cancel.";
    } else {
        updateRouteUI();
    }
}

function handleMapPick(lat, lng) {
    if (mapPickTarget === "roam") { setRoamCentre(lat, lng); setMapPick(null); return; }
    const label = lat.toFixed(5) + ", " + lng.toFixed(5);
    const place = { name: label, display_name: label, lat, lon: lng };
    if (mapPickTarget === "append") {
        const target = smartAddTarget();
        if (!target) { setMapPick(null); return toast("A route can have at most 10 stops", "error"); }
        if (target.isNew) routeStops.push({ text: "", place: null });
        setStopFromPlace(target.index, place);
        // Stay armed so a whole itinerary can be clicked out in one go.
        setMapPick("append");
    } else {
        setStopFromPlace(mapPickTarget, place);
        setMapPick(null);
    }
}

// ── Stops on the map ─────────────────────────────────────────
// Every stop now carries coordinates, so the plan can be drawn as soon as it
// exists rather than only after Calculate. Dashed, because it is the intent
// and not yet a real road route.

let stopMarkers = [];
let stopLine = null;
let closeLoop = false;

function clearStopsOnMap() {
    stopMarkers.forEach(m => map.removeLayer(m));
    stopMarkers = [];
    if (stopLine) { map.removeLayer(stopLine); stopLine = null; }
}

function renderStopsOnMap(fit = false) {
    if (!map) return;
    clearStopsOnMap();
    const placed = routeStops
        .map((stop, index) => ({ stop, index }))
        .filter(entry => entry.stop.place && entry.stop.place.lat != null);

    placed.forEach(({ stop, index }) => {
        const marker = L.circleMarker([stop.place.lat, stop.place.lon], {
            radius: 8, color: "#79C2B8", fillColor: "#79C2B8", fillOpacity: 1, weight: 0,
        }).addTo(map);
        marker.bindTooltip(stopLabel(index), {
            permanent: true, direction: "center", className: "route-label",
        });
        marker.bindPopup(stop.text);
        stopMarkers.push(marker);
    });

    if (placed.length >= 2) {
        const line = placed.map(e => [e.stop.place.lat, e.stop.place.lon]);
        if (closeLoop && placed.length >= 2) line.push(line[0]);
        stopLine = L.polyline(line, {
            color: "#79C2B8", weight: 2, dashArray: "7 6", opacity: 0.65,
        }).addTo(map);
    }

    if (fit && placed.length) {
        const bounds = L.latLngBounds(placed.map(e => [e.stop.place.lat, e.stop.place.lon]));
        map.fitBounds(bounds, { padding: [60, 60], maxZoom: 16 });
    }
}

// Any change to the stops makes a previously calculated road route wrong.
// Leaving it in place means Start drives the route you just edited away from.
function invalidateCalculatedRoute() {
    if (!calculatedRouteCoordinates) return;
    calculatedRouteCoordinates = null;
    calculatedRouteProvider = null;
    calculatedRouteSummary = null;
    calculatedRouteHolds = null;
    routeDistanceKm = 0;
    if (routeDisplayLine) { map.removeLayer(routeDisplayLine); routeDisplayLine = null; }
    const status = $("route-address-status");
    if (status) { status.textContent = ""; status.className = "route-address-status hidden"; }
    updateRouteUI();
}

function moveStop(from, to) {
    if (to < 0 || to >= routeStops.length || from === to) return;
    const [moved] = routeStops.splice(from, 1);
    routeStops.splice(to, 0, moved);
    renderStops();
    invalidateCalculatedRoute();
}



// ── Adaptive speed ───────────────────────────────────────────
// Posted limits come from OpenStreetMap. Google keeps its own behind the paid
// Roads API, so it is not an option here.

let adaptiveSpeed = localStorage.getItem("adaptive_speed") === "1";

// Realistic mode has no speed field, so the profile's untagged fallback and the
// total-failure fallback both need a sensible cruise. ~30 mph in km/h.
const REALISTIC_FALLBACK_KMH = 48;

// Flat drives the chosen speed; Realistic has no field, so send a sensible
// cruise the server uses for untagged roads and if the limit lookup fails.
function routeSpeedKmh() {
    return adaptiveSpeed ? REALISTIC_FALLBACK_KMH : (readSpeedKmh() || selectedSpeed);
}

function setDriveMode(realistic) {
    adaptiveSpeed = realistic;
    localStorage.setItem("adaptive_speed", realistic ? "1" : "0");
    updateAdaptiveUI();
    toast(realistic ? "Realistic — posted limits, a few mph over" : "Flat speed");
}

function updateAdaptiveUI() {
    const flat = $("mode-flat"), real = $("mode-realistic");
    if (!flat || !real) return;
    flat.classList.toggle("active", !adaptiveSpeed);
    flat.setAttribute("aria-selected", (!adaptiveSpeed).toString());
    real.classList.toggle("active", adaptiveSpeed);
    real.setAttribute("aria-selected", adaptiveSpeed.toString());
    $("flat-speed-row")?.classList.toggle("hidden", adaptiveSpeed);
    $("adaptive-note")?.classList.toggle("hidden", !adaptiveSpeed);
    renderCalculatedRouteStatus();
}

// ── Roam radius slider ───────────────────────────────────────

function syncRoamRadius(from) {
    const slider = $("roam-radius-slider"), field = $("roam-radius");
    if (from === "slider") field.value = slider.value;
    else slider.value = field.value;
    renderRoamArea();
    updateRoamUI();
}

function setRoamUnitBounds() {
    // Miles and kilometres want different ranges to feel right on the slider.
    const slider = $("roam-radius-slider"), field = $("roam-radius");
    const max = roamUnitMiles !== false ? 30 : 50;
    slider.max = max; field.max = max;
}

// ── Roam ─────────────────────────────────────────────────────
// Move about at random inside an area rather than along a route. The centre is
// picked on the map; the radius is shown in whichever unit suits the distance.

let roamCentre = null;
let roamCircle = null;
// Follows the same detection as the speed unit until explicitly chosen.
let roamUnitMiles = localStorage.getItem("roam_unit") === "mi" ? true
                  : (localStorage.getItem("roam_unit") === "km" ? false : null);
let roamActive = false;
let roamRetryTimer = null;
let roamWalkState = null;
let roamChunkSpeeds = null;
let roamChunkHolds = null;
let roamRecoveryInProgress = false;
// The next chunk, fetched while the current one is still driving so the seam
// between them has no stop-and-wait. Cleared on stop and once consumed.
let roamPrefetch = null;
let roamPrefetching = false;

function roamChunkRequest(fromLocation, useWalkState) {
    const speed = routeSpeedKmh();
    // Realistic ignores a low flat-speed slider, so size the chunk by a typical
    // local limit. Long chunks (~30 min) mean the (prefetched) seam is rare; the
    // map only ever shows a short lookahead, so chunk length is invisible.
    const chunkSpeed = adaptiveSpeed ? Math.max(speed, 48) : speed;
    const targetKm = Math.max(3, chunkSpeed * 0.5);
    const request = { lat: roamCentre.lat, lon: roamCentre.lon,
                      radius: roamRadiusMetres(), target_km: targetKm };
    if (fromLocation && Number.isFinite(fromLocation.lat) && Number.isFinite(fromLocation.lon)) {
        request.from_lat = fromLocation.lat;
        request.from_lon = fromLocation.lon;
    }
    if (useWalkState && roamWalkState) request.walk_state = roamWalkState;
    return request;
}

async function fetchRoamChunkData(fromLocation, useWalkState) {
    const response = await fetch("/api/roam/route", {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify(roamChunkRequest(fromLocation, useWalkState))
    });
    const data = await response.json();
    return response.ok ? { data } : { error: data.error || "Could not find roads there" };
}

// While the current chunk is well underway, quietly fetch the next one from
// where this chunk ends, so continueRoaming can start it instantly.
function prefetchNextRoamChunk() {
    if (!roamActive || roamPrefetch || roamPrefetching || !roamChunkCoords) return;
    const end = roamChunkCoords[roamChunkCoords.length - 1];
    if (!end) return;
    roamPrefetching = true;
    fetchRoamChunkData({ lat: end[1], lon: end[0] }, true)
        .then(result => { if (roamActive && result && result.data) roamPrefetch = result.data; })
        .catch(() => {})
        .finally(() => { roamPrefetching = false; });
}

const METRES_PER_MILE = 1609.344;

// Roam is meant to run until the user clicks Stop. A hiccup while stitching the
// next chunk — a network blip or a temporarily unavailable graph — must not
// end it: keep roaming alive and retry shortly. Only a failure on the very first
// tour (the user is watching and can adjust) or an explicit Stop ends it.
function failRoam(silent, message) {
    if (silent && roamActive) {
        updateRoamUI();
        clearTimeout(roamRetryTimer);
        roamRetryTimer = setTimeout(() => { if (roamActive) continueRoaming(); }, 3000);
        return;
    }
    roamActive = false;
    updateRoamUI();
    toast(message, "error");
}

function roamRadiusMetres() {
    const value = parseFloat($("roam-radius").value);
    if (!(value > 0)) return null;
    return roamUnitMiles !== false ? value * METRES_PER_MILE : value * 1000;
}

function renderRoamArea() {
    if (roamCircle) { map.removeLayer(roamCircle); roamCircle = null; }
    const radius = roamRadiusMetres();
    if (!roamCentre || !radius) return;
    roamCircle = L.circle([roamCentre.lat, roamCentre.lon], {
        radius,
        color: "#79C2B8", weight: 2, dashArray: "7 6", opacity: 0.7,
        fillColor: "#79C2B8", fillOpacity: 0.07,
    }).addTo(map);
}

function setRoamCentre(lat, lon) {
    roamCentre = { lat, lon };
    const slot = $("btn-roam-center");
    slot.textContent = lat.toFixed(5) + ", " + lon.toFixed(5);
    slot.classList.remove("is-empty");
    renderRoamArea();
    updateRoamUI();
}

// The location already on the map is the obvious roam centre. Return it from the
// live spoof, or failing that the coordinate inputs, so a placed dot need not be
// picked a second time.
function placedLocation() {
    if (activeSpoofLocation && Number.isFinite(activeSpoofLocation.lat) && Number.isFinite(activeSpoofLocation.lon))
        return { lat: activeSpoofLocation.lat, lon: activeSpoofLocation.lon };
    const lat = parseFloat($("lat-input").value), lon = parseFloat($("lon-input").value);
    if (Number.isFinite(lat) && Number.isFinite(lon)) return { lat, lon };
    return null;
}

// Fill the roam centre from the placed dot. `force` overrides an existing centre
// (a fresh placement wins); without it, only an empty centre is filled (opening
// the tab). Never while a route or roam runs, so a moving dot cannot drag it.
function adoptDotAsRoamCentre(force = false) {
    if (roamActive || routePolling) return;
    if (!force && roamCentre) return;
    const p = placedLocation();
    if (p) setRoamCentre(p.lat, p.lon);
}

function updateRoamUI() {
    const ready = !!roamCentre && !!roamRadiusMetres();
    $("btn-roam-start").disabled = !ready || roamActive;
    $("btn-roam-stop").disabled = !roamActive;
    $("roam-unit-toggle").textContent = roamUnitMiles !== false ? "mi" : "km";
}

async function startRoaming(silent = false, fromLocation = null, prefetchedData = null) {
    // A placed dot stands in for a centre that was never explicitly picked.
    if (!silent) adoptDotAsRoamCentre();
    const radius = roamRadiusMetres();
    if (!roamCentre || !radius) return toast("Pick a centre and a radius first", "error");
    const speed = routeSpeedKmh();
    const button = $("btn-roam-start");
    button.disabled = true;
    if (!silent) { roamWalkState = null; roamPrefetch = null; }
    if (!silent) toast("Finding roads to roam…");

    try {
        let data = prefetchedData;
        if (!data) {
            const result = await fetchRoamChunkData(fromLocation, silent);
            if (result.error) { return failRoam(silent, result.error); }
            data = result.data;
        }

        const startRequest = {
            waypoints: data.waypoints, speed, mode: "once",
            randomize_speed: $("speed-randomize").checked,
            coordinates: data.coordinates, provider: "roam",
            adaptive: adaptiveSpeed
        };
        // Realistic: the walk already knows each road's limit and its signs.
        if (adaptiveSpeed && Array.isArray(data.speeds) && data.speeds.length) {
            startRequest.speeds = data.speeds;
            if (Array.isArray(data.holds)) startRequest.holds = data.holds;
        }
        const start = await fetch("/api/route/start", {
            method: "POST", headers: { "Content-Type": "application/json" },
            body: JSON.stringify(startRequest)
        });
        const started = await start.json();
        if (!start.ok) { return failRoam(silent, started.error || "Could not start roaming"); }

        roamWalkState = data.walk_state || null;
        roamChunkSpeeds = Array.isArray(data.speeds) ? data.speeds : null;
        roamChunkHolds = Array.isArray(data.holds) ? data.holds : [];
        roamActive = true;
        updateRoamUI();
        drawRoamPath(data.coordinates);
        clearInterval(routePolling);
        routePolling = setInterval(pollRoute, 1000);
        // Follow the dot up close on the first tour, like a route does. Later
        // tours (silent) leave the view alone so a manual zoom-out is not undone.
        if (!silent) {
            followMode = true;
            if ($("follow-mode")) $("follow-mode").checked = true;
            const roamStart = data.coordinates?.[0] ? [data.coordinates[0][1], data.coordinates[0][0]] : null;
            if (roamStart) { map.stop(); map.setView(roamStart, routeFollowZoomForSpeed(speed), { animate: false }); }
        }
        startMovementTracking();
        if (!silent) toast("Roaming " + data.distance_km + " km of roads");
    } catch (e) {
        failRoam(silent, "Could not start roaming");
    }
}

let roamPathLine = null;
let roamChunkCoords = null;
let roamChunkIdx = 0;

// Roam is a random wander, so drawing the whole chunk would spoil it and
// clutter the map. Keep the geometry and show only a short tail of road ahead
// of the dot — enough to see where the next turn goes.
function drawRoamPath(coordinates) {
    roamChunkCoords = coordinates;
    roamChunkIdx = 0;
    if (roamPathLine) { map.removeLayer(roamPathLine); roamPathLine = null; }
    if (coordinates?.length) updateRoamLookahead(coordinates[0][1], coordinates[0][0]);
}

function remainingRoamChunk(fromLocation) {
    const coords = roamChunkCoords;
    if (!coords?.length || !fromLocation) return null;
    const scale = Math.cos(fromLocation.lat * Math.PI / 180) * 111320;
    const distance = (point) => Math.hypot(
        (point[1] - fromLocation.lat) * 111320,
        (point[0] - fromLocation.lon) * scale
    );
    let best = Math.max(0, Math.min(roamChunkIdx, coords.length - 1));
    let bestDistance = distance(coords[best]);
    // The last position poll normally leaves roamChunkIdx exact. Search around
    // it for a write that landed between geometry points, but never scan far
    // enough backward to match an earlier lap of a small loop.
    const start = Math.max(0, best - 10);
    const end = Math.min(coords.length, best + 250);
    for (let index = start; index < end; index++) {
        const candidateDistance = distance(coords[index]);
        if (candidateDistance < bestDistance) {
            best = index;
            bestDistance = candidateDistance;
        }
    }

    const coordinates = [[fromLocation.lon, fromLocation.lat], ...coords.slice(best + 1)];
    if (coordinates.length < 2) return null;
    const speeds = Array.isArray(roamChunkSpeeds)
        && roamChunkSpeeds.length === coords.length - 1
        ? roamChunkSpeeds.slice(best) : null;
    const holds = Array.isArray(roamChunkHolds) ? roamChunkHolds
        .filter((hold) => Array.isArray(hold) && hold[0] > best)
        .map((hold) => [hold[0] - best, hold[1]]) : [];
    return { coordinates, speeds, holds };
}

async function recoverRoamingAfterDeviceError(message) {
    if (!roamActive || roamRecoveryInProgress) return;
    roamRecoveryInProgress = true;
    clearTimeout(roamRetryTimer);
    updateRoamUI();
    toast(message + " Reconnecting…", "error");
    try {
        const reconnect = await fetch("/api/device/connect", {
            method: "POST", headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ wifi: currentDeviceInfo?.connection_type === "WiFi" })
        });
        if (!reconnect.ok) throw new Error("Device reconnect failed");
        if (!roamActive) return;

        const fromLocation = activeSpoofLocation
            && Number.isFinite(activeSpoofLocation.lat)
            && Number.isFinite(activeSpoofLocation.lon)
            ? { lat: activeSpoofLocation.lat, lon: activeSpoofLocation.lon }
            : null;
        const remaining = remainingRoamChunk(fromLocation);
        if (!remaining) {
            roamRecoveryInProgress = false;
            await continueRoaming();
            return;
        }

        const speed = routeSpeedKmh();
        const startRequest = {
            waypoints: [
                { lat: remaining.coordinates[0][1], lng: remaining.coordinates[0][0] },
                { lat: remaining.coordinates.at(-1)[1], lng: remaining.coordinates.at(-1)[0] },
            ],
            speed, mode: "once", randomize_speed: $("speed-randomize").checked,
            coordinates: remaining.coordinates, provider: "roam", adaptive: adaptiveSpeed
        };
        if (adaptiveSpeed && remaining.speeds?.length) {
            startRequest.speeds = remaining.speeds;
            startRequest.holds = remaining.holds;
        }
        const startedResponse = await fetch("/api/route/start", {
            method: "POST", headers: { "Content-Type": "application/json" },
            body: JSON.stringify(startRequest)
        });
        const started = await startedResponse.json().catch(() => ({}));
        if (!startedResponse.ok) throw new Error(started.error || "Could not resume roaming");
        if (!roamActive) {
            await fetch("/api/route/stop", { method: "POST" });
            return;
        }

        roamChunkSpeeds = remaining.speeds;
        roamChunkHolds = remaining.holds;
        drawRoamPath(remaining.coordinates);
        clearInterval(routePolling);
        routePolling = setInterval(pollRoute, 1000);
        startMovementTracking();
        pollDevice();
        toast("iPhone reconnected — roaming resumed");
    } catch (e) {
        if (roamActive) {
            roamRetryTimer = setTimeout(
                () => recoverRoamingAfterDeviceError(message), 3000
            );
        }
    } finally {
        roamRecoveryInProgress = false;
    }
}

function updateRoamLookahead(lat, lon) {
    const coords = roamChunkCoords;
    if (!coords || !coords.length) return;
    const scale = Math.cos(lat * Math.PI / 180) * 111320;
    const dist = (aLat, aLon, bLat, bLon) =>
        Math.hypot((aLat - bLat) * 111320, (aLon - bLon) * scale);
    // The dot only moves forward, so search a window ahead of the last match.
    let best = roamChunkIdx, bestD = Infinity;
    const end = Math.min(coords.length, roamChunkIdx + 250);
    for (let i = roamChunkIdx; i < end; i++) {
        const d = dist(lat, lon, coords[i][1], coords[i][0]);
        if (d < bestD) { bestD = d; best = i; }
    }
    roamChunkIdx = best;
    const tail = [[lat, lon]];
    let acc = 0;
    let prevLat = coords[best][1], prevLon = coords[best][0];
    for (let i = best; i < coords.length && acc < 400; i++) {
        const ptLat = coords[i][1], ptLon = coords[i][0];
        acc += dist(prevLat, prevLon, ptLat, ptLon);
        tail.push([ptLat, ptLon]);
        prevLat = ptLat; prevLon = ptLon;
    }
    if (roamPathLine) roamPathLine.setLatLngs(tail);
    else roamPathLine = L.polyline(tail,
        { color: "#79C2B8", weight: 3, opacity: 0.7, dashArray: "7 7" }).addTo(map);
}

// When one stretch of road runs out, quietly lay out another so roaming keeps
// going without repeating the same loop.
async function continueRoaming() {
    if (!roamActive) return;
    // The next chunk was prefetched from this chunk's end while it was still
    // driving, so start it straight away — no stop-and-wait at the seam.
    if (roamPrefetch) {
        const prefetched = roamPrefetch;
        roamPrefetch = null;
        await startRoaming(true, null, prefetched);
        return;
    }
    let fromLocation = null;
    try {
        // The tracking poll stops with the completed route, so its cached
        // coordinate can lag behind the phone at precisely this seam.
        const response = await fetch("/api/location/current");
        if (response.ok) {
            const current = await response.json();
            const currentLat = finiteNumber(current.lat), currentLon = finiteNumber(current.lon);
            if (currentLat != null && currentLon != null) {
                fromLocation = { lat: currentLat, lon: currentLon };
            }
        }
    } catch (e) { /* the server will start from the scattered point */ }
    await startRoaming(true, fromLocation);
}

async function stopRoaming() {
    roamActive = false;
    clearTimeout(roamRetryTimer);
    try { await fetch("/api/route/stop", { method: "POST" }); } catch (e) { /* already stopped */ }
    try { await fetch("/api/wander/stop", { method: "POST" }); } catch (e) { /* legacy */ }
    if (roamPathLine) { map.removeLayer(roamPathLine); roamPathLine = null; }
    roamChunkCoords = null;
    roamWalkState = null;
    roamChunkSpeeds = null;
    roamChunkHolds = null;
    roamPrefetch = null;
    roamPrefetching = false;
    roamRecoveryInProgress = false;
    updateRoamUI();
    stopMovementTracking();
    toast("Roaming stopped");
}

// ── Explicit placement menu ──────────────────────────────────

let assignMenuEl = null;

function closeAssignMenu() {
    if (assignMenuEl) { assignMenuEl.remove(); assignMenuEl = null; }
}

function openAssignMenu(anchor, place) {
    closeAssignMenu();
    const menu = document.createElement("div");
    menu.className = "assign-menu";

    const title = document.createElement("div");
    title.className = "assign-menu-title";
    title.textContent = "Add to route";
    menu.appendChild(title);

    const target = smartAddTarget();
    if (target) {
        const add = document.createElement("button");
        add.type = "button";
        add.className = "assign-menu-item";
        const marker = document.createElement("span");
        marker.className = "stop-marker";
        marker.textContent = stopLabel(target.index);
        const text = document.createElement("span");
        text.textContent = target.isNew ? "Add as a new stop" : "Fill the empty stop";
        add.append(marker, text);
        add.addEventListener("click", () => { closeAssignMenu(); addPlaceToRoute(place); });
        menu.appendChild(add);
        menu.appendChild(Object.assign(document.createElement("div"), { className: "assign-menu-sep" }));
    }

    routeStops.forEach((stop, index) => {
        // The smart action above already covers this one.
        if (target && !target.isNew && index === target.index) return;
        const item = document.createElement("button");
        item.type = "button";
        item.className = "assign-menu-item";
        const marker = document.createElement("span");
        marker.className = "stop-marker";
        marker.textContent = stopLabel(index);
        const text = document.createElement("span");
        if (stop.text.trim()) { text.textContent = "Replace " + stop.text; }
        else { text.textContent = "Empty"; text.className = "is-empty"; }
        item.append(marker, text);
        item.addEventListener("click", () => { closeAssignMenu(); setStopFromPlace(index, place); });
        menu.appendChild(item);
    });

    document.body.appendChild(menu);
    const box = anchor.getBoundingClientRect();
    const width = menu.offsetWidth, height = menu.offsetHeight;
    menu.style.left = Math.max(8, Math.min(box.right - width, window.innerWidth - width - 8)) + "px";
    menu.style.top = (box.bottom + height + 8 > window.innerHeight ? box.top - height - 4 : box.bottom + 4) + "px";
    assignMenuEl = menu;
}

document.addEventListener("click", event => {
    if (assignMenuEl && !assignMenuEl.contains(event.target)) closeAssignMenu();
});
document.addEventListener("keydown", event => {
    if (event.key !== "Escape") return;
    closeAssignMenu();
    if (mapPickTarget !== null) setMapPick(null);
});

function renderStops() {
    const list = $("stop-list");
    if (!list) return;
    list.textContent = "";

    routeStops.forEach((stop, index) => {
        const row = document.createElement("div");
        row.className = "stop-row" + (index === 0 ? " is-start" : (index === routeStops.length - 1 ? " is-end" : ""));

        // The letter doubles as a drag handle: reordering here is what makes
        // precise up-front placement unnecessary.
        const marker = document.createElement("span");
        marker.className = "stop-marker";
        marker.textContent = stopLabel(index);
        marker.draggable = true;
        marker.title = "Drag to reorder";
        marker.addEventListener("dragstart", event => {
            event.dataTransfer.setData("text/plain", String(index));
            event.dataTransfer.effectAllowed = "move";
            row.classList.add("dragging");
        });
        marker.addEventListener("dragend", () => {
            row.classList.remove("dragging");
            document.querySelectorAll(".stop-row").forEach(r => r.classList.remove("drop-target"));
        });
        row.addEventListener("dragover", event => { event.preventDefault(); row.classList.add("drop-target"); });
        row.addEventListener("dragleave", () => row.classList.remove("drop-target"));
        row.addEventListener("drop", event => {
            event.preventDefault();
            row.classList.remove("drop-target");
            const from = parseInt(event.dataTransfer.getData("text/plain"), 10);
            if (!Number.isNaN(from)) moveStop(from, index);
        });
        row.appendChild(marker);

        // A stop is filled from the search bar or by pointing at the map, never
        // by typing here. One search field beats four half-working ones.
        const slot = document.createElement("button");
        slot.type = "button";
        slot.className = "stop-slot" + (stop.text ? "" : " is-empty");
        slot.textContent = stop.text || stopPlaceholder(index);
        slot.title = stop.text
            ? stop.text + " — click to search for a replacement"
            : "Search above, or use ◎ to click it on the map";
        slot.setAttribute("aria-label", "Stop " + stopLabel(index) + ": " + (stop.text || "empty"));
        slot.addEventListener("click", () => {
            const search = $("search-input");
            search.focus();
            search.select();
            toast("Search for a place, then press + " + stopLabel(index));
        });
        // Keyboard equivalent of the drag handle, which is mouse-only.
        slot.addEventListener("keydown", event => {
            if (!event.altKey) return;
            if (event.key !== "ArrowUp" && event.key !== "ArrowDown") return;
            event.preventDefault();
            const to = index + (event.key === "ArrowUp" ? -1 : 1);
            moveStop(index, to);
            const moved = document.querySelectorAll("#stop-list .stop-slot")[Math.max(0, Math.min(to, routeStops.length - 1))];
            if (moved) moved.focus();
        });
        row.appendChild(slot);

        // Point at the map instead.
        const pick = document.createElement("button");
        pick.className = "stop-pick" + (mapPickTarget === index ? " active" : "");
        pick.type = "button";
        pick.textContent = "\u25CE";
        pick.title = "Set this stop by clicking the map";
        pick.setAttribute("aria-label", pick.title);
        pick.addEventListener("click", () => setMapPick(mapPickTarget === index ? null : index));
        row.appendChild(pick);

        const remove = document.createElement("button");
        remove.className = "stop-remove";
        remove.textContent = "\u00D7";
        remove.setAttribute("aria-label", "Remove this stop");
        remove.disabled = routeStops.length <= 2;
        remove.addEventListener("click", () => { routeStops.splice(index, 1); renderStops(); invalidateCalculatedRoute(); });
        row.appendChild(remove);

        list.appendChild(row);
    });

    renderStopsOnMap();
    updateRouteUI();
}

function addStop() {
    if (routeStops.length >= 10) return toast("A route can have at most 10 stops", "error");
    routeStops.push({ text: "", place: null });
    invalidateCalculatedRoute();
    renderStops();
}

function reverseStops() { routeStops.reverse(); renderStops(); }

function setBuildMode(mode) {
    if (mode !== "map" && mapPickTarget === "append") setMapPick(null);
    document.querySelectorAll(".mode-tab").forEach(tab => {
        const on = tab.dataset.build === mode;
        tab.classList.toggle("active", on);
        tab.setAttribute("aria-selected", on ? "true" : "false");
    });
    document.querySelectorAll(".build-pane").forEach(pane => {
        pane.classList.toggle("active", pane.id === "build-" + mode);
    });
    if (mode === "roam") {
        adoptDotAsRoamCentre();
        $("route-hint").textContent = roamCentre
            ? "Centred on your placed location. Set a radius, then start roaming."
            : "Pick a centre and a radius, then start roaming.";
    } else {
        updateRouteUI();
    }
}

function drawCalculatedRoute(data, label) {
    clearRouteGeometry();
    // A circle is 49 waypoints; numbering every one of them buries the map in
    // markers. Past a handful, the line alone communicates the shape.
    if (data.waypoints.length > 12) {
        routePoints = data.waypoints.map(point => ({ lat: point.lat, lng: point.lng }));
    } else {
        data.waypoints.forEach(point => addRoutePoint(point.lat, point.lng, true));
    }
    calculatedRouteCoordinates = data.coordinates;
    calculatedRouteProvider = data.provider;
    routeDistanceKm = data.distance_km;
    calculatedRouteSummary = {
        distanceKm: finiteNumber(data.distance_km),
        googleEtaMin: finiteNumber(data.duration_min),
        routeName: typeof data.route_name === "string" ? data.route_name : "",
    };
    calculatedRouteHolds = Array.isArray(data.stop_indices)
        ? data.stop_indices
            .filter((index, position, indices) => Number.isInteger(index) && index > 0
                && index < data.coordinates.length - 1 && indices.indexOf(index) === position)
            .map(index => [index, "waypoint"])
        : null;
    if (routeLine) { map.removeLayer(routeLine); routeLine = null; }
    routeDisplayLine = L.polyline(data.coordinates.map(c => [c[1], c[0]]), { color: "#6F999A", weight: 3, opacity: 0.78 }).addTo(map);
    map.fitBounds(routeDisplayLine.getBounds(), { padding: [35, 35] });
    updateRouteUI();
    // After updateRouteUI, which writes its own generic hint.
    $("route-hint").textContent = label;
}

function renderCalculatedRouteStatus() {
    const status = $("route-address-status");
    if (!status || !calculatedRouteSummary) return;
    const summary = calculatedRouteSummary;
    const speedKmh = readSpeedKmh() || selectedSpeed;
    const googleEta = summary.googleEtaMin == null ? null : formatEtaSeconds(summary.googleEtaMin * 60);
    const projection = summary.distanceKm == null || !(speedKmh > 0)
        ? null
        : formatEtaSeconds((summary.distanceKm / speedKmh) * 3600);
    const googleParts = [];
    if (googleEta) googleParts.push("Google ETA: " + googleEta);
    if (summary.distanceKm != null) googleParts.push(formatRouteDistance(summary.distanceKm));
    if (summary.routeName) googleParts.push(summary.routeName);
    let text = googleParts.join(" · ");
    if (adaptiveSpeed) {
        // Adaptive drives the posted limits, so Google's own estimate is the
        // closest guide; a flat-speed projection would be misleading.
        text += (text ? ". " : "") + "Adaptive follows posted limits, so expect close to Google's time plus junction stops.";
    } else if (projection) {
        text += (text ? ". " : "") + "At your " + formatSpeed(speedKmh) + ": "
            + projection + ", not a promised ETA.";
    }
    status.className = "route-address-status success";
    status.textContent = text;
}

async function calculateAddressRoute() {
    const status = $("route-address-status");
    const stops = routeStops.map(stop => stop.text.trim()).filter(Boolean);
    if (stops.length < 2) return toast("Enter at least a start and a destination", "error");
    if (closeLoop && stops.length >= 2) stops.push(stops[0]);
    const button = $("btn-route-calculate");
    button.disabled = true; button.textContent = "Calculating…";
    status.classList.remove("hidden");
    status.className = "route-address-status loading";
    status.textContent = stops.length > 2
        ? "Asking Google Maps for a route through " + stops.length + " stops…"
        : "Asking Google Maps for the driving route…";
    try {
        const response = await fetch("/api/route/calculate", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ stops }) });
        const data = await response.json();
        if (!response.ok) { status.className = "route-address-status error"; status.textContent = data.error || "Route calculation failed"; return toast(status.textContent, "error"); }
        drawCalculatedRoute(data, data.legs > 1 ? data.legs + " legs ready" : "Google route ready");
        renderCalculatedRouteStatus();
        toast("Route calculated");
    } catch (error) {
        status.className = "route-address-status error"; status.textContent = "Google Maps route service is unavailable"; toast(status.textContent, "error");
    } finally {
        button.disabled = false; button.textContent = "Calculate route";
    }
}

async function startRoute() {
    if (routePoints.length < 2) return; const speed = routeSpeedKmh(); const mode = $("route-mode").value; const randomize = $("speed-randomize").checked;
    const routeRequest = { waypoints: routePoints, speed, mode, randomize_speed: randomize, coordinates: calculatedRouteCoordinates, provider: calculatedRouteProvider, adaptive: adaptiveSpeed };
    if (calculatedRouteHolds !== null) routeRequest.holds = calculatedRouteHolds;
    try { const r = await fetch("/api/route/start", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(routeRequest) }); const d = await r.json(); if (!r.ok) return toast(d.error || "Route failed", "error");
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

async function stopRoute() {
    roamActive = false; try { await fetch("/api/route/stop", { method: "POST" }); endRoute(); toast("Route stopped"); } catch (e) { toast("Failed", "error"); } }
async function pauseRoute() { try { await fetch("/api/route/pause", { method: "POST" }); $("btn-route-pause").classList.add("hidden"); $("btn-route-resume").classList.remove("hidden"); toast("Route paused"); } catch (e) { toast("Failed", "error"); } }
async function resumeRoute() { try { await fetch("/api/route/resume", { method: "POST" }); $("btn-route-resume").classList.add("hidden"); $("btn-route-pause").classList.remove("hidden"); toast("Route resumed"); } catch (e) { toast("Failed", "error"); } }

// The planned line stays put; a brighter line grows over it as the phone
// covers ground, so progress is legible on the map itself.
function drawTravelled(pct) {
    if (!calculatedRouteCoordinates || !(pct > 0)) return;
    const coords = calculatedRouteCoordinates;
    const upto = Math.max(2, Math.round(coords.length * (pct / 100)));
    const path = coords.slice(0, upto).map(c => [c[1], c[0]]);
    if (routeTraveledLine) { routeTraveledLine.setLatLngs(path); return; }
    routeTraveledLine = L.polyline(path, { color: "#EAF2EC", weight: 4, opacity: 0.9 }).addTo(map);
}

// A short rolling history of driven speed (km/h) so the readout can tell that
// the car is speeding up or easing off, not just its instantaneous number.
let narrationSpeeds = [];

// The car's "brain": a plain-language description of what it is doing right now.
function describeDriving(data, targetOverride) {
    const unit = speedUnitOrDefault() === "mph" ? "mph" : "km/h";
    const currentSpeed = finiteNumber(data.speed_kmh);

    // Legacy server without the richer fields: keep the old compact readout.
    if (currentSpeed != null && finiteNumber(data.target_speed_kmh) == null
            && typeof data.holding !== "boolean" && typeof data.adaptive !== "boolean") {
        return formatSpeed(currentSpeed);
    }

    if (data.holding === true) {
        narrationSpeeds = [];
        if (data.hold_kind === "signal") return "Waiting at the light";
        if (data.hold_kind === "stop") return "Stopped at a stop sign";
        if (data.hold_kind === "waypoint") return "Paused at a stop";
        return "Stopped";
    }
    if (currentSpeed == null) return null;

    // Whole numbers read like a person narrating ("going 30 in a 25"), not a
    // speedometer, so round for the phrase.
    const mph = kmh => Math.round(displaySpeedFromKmh(kmh));
    const curDisp = mph(currentSpeed);
    const older = narrationSpeeds.length ? narrationSpeeds[0] : currentSpeed;
    const delta = currentSpeed - older;               // km/h over the window
    const rising = delta > 2.5;
    const falling = delta < -2.5;

    const nextKind = data.next_hold_kind;
    const nextM = finiteNumber(data.next_hold_m);
    if (falling && nextKind && nextM != null && nextM < 90) {
        if (nextKind === "signal") return "Slowing for the light";
        if (nextKind === "stop") return "Slowing for a stop sign";
        return "Slowing to stop";
    }
    if (currentSpeed < 3) return "Pulling away";
    if (rising) return "Speeding up · " + curDisp + " " + unit;
    if (falling) return "Easing off · " + curDisp + " " + unit;

    // Cruising. Under adaptive, frame it against the posted limit ("30 in a 25").
    const routeAdaptive = typeof data.adaptive === "boolean" ? data.adaptive : adaptiveSpeed;
    const postedKmh = finiteNumber(data.posted_limit_kmh);
    if (routeAdaptive && postedKmh != null && postedKmh > 0) {
        return "Cruising · " + curDisp + " in a " + mph(postedKmh);
    }
    const override = finiteNumber(targetOverride);
    const targetSpeed = override == null ? finiteNumber(data.target_speed_kmh) : override;
    if (!routeAdaptive && targetSpeed != null) {
        return "Cruising · " + curDisp + " / " + mph(targetSpeed) + " " + unit;
    }
    return "Cruising · " + curDisp + " " + unit;
}

function renderRouteSpeedStatus(data, targetOverride = null, fresh = false) {
    const statusSpeed = $("status-speed-text");
    if (!statusSpeed) return;
    // Sample the trend only from a real status poll, not from a slider redraw.
    if (fresh && data.holding !== true) {
        const value = finiteNumber(data.speed_kmh);
        if (value != null) {
            narrationSpeeds.push(value);
            if (narrationSpeeds.length > 4) narrationSpeeds.shift();
        }
    }
    const phrase = describeDriving(data, targetOverride);
    if (phrase != null) statusSpeed.textContent = phrase;
}

async function pollRoute() {
    try { const r = await fetch("/api/route/status"); if (!r.ok) {
        endRoute();
        if (roamActive) recoverRoamingAfterDeviceError("Route status unavailable.");
        return;
    } const d = await r.json();
    lastRouteStatus = d;
    const progressValue = finiteNumber(d.progress_pct);
    const progress = progressValue == null ? 0 : Math.max(0, Math.min(100, progressValue));
    $("progress-bar").style.width = progress + "%"; $("route-pct").textContent = Math.round(progress) + "%";
    drawTravelled(progress);
    renderRouteSpeedStatus(d, null, true);
    // Line up the next roam chunk once this one is well underway, so the seam
    // between them is seamless rather than a stop-and-wait.
    if (roamActive && d.active && progress > 55) prefetchNextRoamChunk();
    if ($("status-route")) {
        $("status-route").classList.remove("hidden");
        const remainingValue = finiteNumber(d.remaining_km);
        const totalDistance = finiteNumber(routeDistanceKm);
        const remaining = remainingValue != null && remainingValue >= 0
            ? remainingValue
            : Math.max(0, (totalDistance || 0) * (1 - progress / 100));
        const durationMin = finiteNumber(d.duration_min);
        let eta = formatEtaSeconds(d.eta_seconds);
        if (eta == null && durationMin != null && durationMin >= 0) {
            eta = formatEtaSeconds(durationMin * 60 * (1 - progress / 100));
        }
        // Distance remaining, live ETA, and percent — enough to read at a
        // glance. The planned total is shown when the route is calculated, so
        // repeating it here only widened the bar into the map controls.
        const routeParts = [formatRouteDistance(remaining) + " left"];
        if (eta != null) routeParts.push("ETA " + eta);
        routeParts.push(Math.round(progress) + "%");
        $("status-route-text").textContent = routeParts.join(" · ");
    }
    if (!d.active) {
        endRoute();
        if (d.error) {
            // Roam is user-stopped, not transport-stopped. Reconnect and resume
            // this exact graph-walk chunk rather than ending it or jumping.
            if (roamActive) {
                recoverRoamingAfterDeviceError(d.error);
                return;
            }
            pollDevice();
            return toast(d.error, "error");
        }
        if (roamActive) { continueRoaming(); return; }
        toast("Route completed");
        if ($("btn-route-save")) $("btn-route-save").style.display = "";
    }
    } catch (e) {}
}

function endRoute() { clearInterval(routePolling); routePolling = null; lastRouteStatus = null; narrationSpeeds = []; $("btn-route-start").disabled = false; $("btn-route-stop").disabled = true; $("btn-route-pause").classList.add("hidden"); $("btn-route-resume").classList.add("hidden"); $("route-progress").classList.add("hidden"); if ($("status-route")) $("status-route").classList.add("hidden"); if (routeTraveledLine) { map.removeLayer(routeTraveledLine); routeTraveledLine = null; } stopMovementTracking(); updateStatusBar(); }

// ── Live tracking ───────────────────────────────────────────
function startMovementTracking() { if (movementPolling) return; movementPolling = setInterval(pollPosition, 500); }
function stopMovementTracking() { if (movementPolling) { clearInterval(movementPolling); movementPolling = null; } trailPoints = []; }
async function pollPosition() {
    if (!deviceReady()) return; try { const r = await fetch("/api/location/current"); if (!r.ok) return; const loc = await r.json(); activeSpoofLocation = { lat: loc.lat, lon: loc.lon }; placeMarker(loc.lat, loc.lon); if (roamActive) updateRoamLookahead(loc.lat, loc.lon); if (followMode) { map.stop(); map.panTo([loc.lat, loc.lon], { animate: true, duration: 0.3, noMoveStart: true }); } } catch (e) {} }

// ── GPX ─────────────────────────────────────────────────────
// ── Joystick ────────────────────────────────────────────────
const _keyMap = { w: "n", a: "w", s: "s", d: "e", arrowup: "n", arrowdown: "s", arrowleft: "w", arrowright: "e" };
let _activeKeys = new Set();
let joystickCommand = Promise.resolve();
function onKeyDown(e) {
    const typing = e.target.tagName === "INPUT" || e.target.tagName === "TEXTAREA" || e.target.tagName === "SELECT";
    // Escape is the way out of a form, so it must survive the typing guard.
    if (typing && e.key !== "Escape") return;
    // Onboarding covers everything, so acting on a shortcut here would change
    // state the user cannot see.
    const onboarding = $("onboarding");
    if (onboarding && getComputedStyle(onboarding).display !== "none") return;
    if (e.key === "?" || (e.key === "/" && e.shiftKey)) { e.preventDefault(); toggleShortcuts(); return; }
    if (e.key.toLowerCase() === "t") { e.preventDefault(); toggleTeleport(); return; }
    if (e.key.toLowerCase() === "g") { e.preventDefault(); toggleTips(); return; }
    if (e.key === "Escape") { document.querySelectorAll(".inline-form:not(.hidden)").forEach(f => f.classList.add("hidden")); if (!$("shortcuts-overlay").classList.contains("hidden")) toggleShortcuts(); if (!$("tips-overlay").classList.contains("hidden")) toggleTips(); return; }
    if (e.key === "+" || e.key === "=") { map.zoomIn(); return; }
    if (e.key === "-") { map.zoomOut(); return; }
    const dir = _keyMap[e.key.toLowerCase()]; if (!dir) return; e.preventDefault(); _activeKeys.add(dir); const combined = _combineDirections(); if (combined) joystickMove(combined);
}
function onKeyUp(e) {
    if (e.target.tagName === "INPUT" || e.target.tagName === "TEXTAREA" || e.target.tagName === "SELECT") return;
    const dir = _keyMap[e.key.toLowerCase()]; if (!dir) return; _activeKeys.delete(dir); if (_activeKeys.size === 0) joystickStop(); else { const combined = _combineDirections(); if (combined) joystickMove(combined); } }
function _combineDirections() { const has = d => _activeKeys.has(d); if (has("n") && has("e")) return "ne"; if (has("n") && has("w")) return "nw"; if (has("s") && has("e")) return "se"; if (has("s") && has("w")) return "sw"; if (has("n")) return "n"; if (has("s")) return "s"; if (has("e")) return "e"; if (has("w")) return "w"; return null; }

function deviceReady() {
    const label = $("device-label");
    return !!(label && !/connect|no device|scanning/i.test(label.textContent || ""));
}

function joystickMove(direction) {
    // Silently doing nothing reads as a broken button.
    if (!deviceReady()) return toast("No iPhone connected", "error");
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
function updateStatusBar() {
    if (routePolling && lastRouteStatus && finiteNumber(lastRouteStatus.target_speed_kmh) != null) {
        renderRouteSpeedStatus(lastRouteStatus, selectedSpeed);
    } else if ($("status-speed-text")) {
        $("status-speed-text").textContent = formatSpeed(selectedSpeed);
    }
    renderCalculatedRouteStatus();
}

// ── Location consistency ──────────────────────────────────
let _stealthDismissed = false;

async function checkStealth() {
    try {
        const r = await fetch("/api/stealth/check");
        if (!r.ok) throw new Error("Location consistency check failed");
        const d = await r.json();
        const banner = $("stealth-banner");
        const stealthPill = $("status-stealth");
        const stealthText = $("status-stealth-text");
        const stealthDot = $("status-stealth-dot");
        const checkAvailable = d.check_available === true;
        const warnings = Array.isArray(d.warnings) ? d.warnings.slice() : [];

        // The browser timezone is another consistency signal, not evidence that
        // iOS sees the simulated position as a hardware-originated location.
        if (checkAvailable && d.spoof_location) {
            const browserOffsetH = -new Date().getTimezoneOffset() / 60;
            const targetOffsetH = Math.round(d.spoof_location.lon / 15);
            if (Math.abs(browserOffsetH - targetOffsetH) > 1
                    && !warnings.find(w => w.type === "timezone_mismatch")) {
                warnings.push({
                    type: "timezone_mismatch", severity: "medium",
                    message: "Browser timezone does not match the simulated location's approximate timezone."
                });
            }
        }

        if (stealthPill) {
            if (!checkAvailable) {
                stealthText.textContent = d.unavailable_reason === "no_location" ? "CHECK NOT RUN" : "CHECK UNAVAILABLE";
                stealthDot.className = "status-dot degraded";
                stealthPill.title = "Location consistency check could not run";
            } else if (!warnings.length) {
                stealthText.textContent = "LOCATION CONSISTENT";
                stealthDot.className = "status-dot connected";
                stealthPill.title = "Simulated location, IP-based location, and approximate timezone are consistent; this does not test iOS simulation flags";
            } else {
                const hasHigh = warnings.some(w => w.severity === "high");
                stealthText.textContent = hasHigh ? "LOCATION MISMATCH" : "CHECK WARNING";
                stealthDot.className = "status-dot degraded";
                stealthPill.title = "The location consistency check found a mismatch; this does not test iOS simulation flags";
            }
        }

        if (_stealthDismissed) return;
        if (!checkAvailable) {
            if (d.unavailable_reason === "no_location") { banner.classList.add("hidden"); return; }
            $("stealth-banner-text").textContent = "Location consistency check could not run because IP-based location is unavailable.";
            banner.className = "stealth-banner-medium";
            return;
        }
        if (!warnings.length) { banner.classList.add("hidden"); return; }
        const sorted = warnings.sort((a, b) => (a.severity === "high" ? -1 : 1));
        $("stealth-banner-text").textContent = sorted[0].message;
        banner.classList.remove("hidden");
        banner.className = "stealth-banner-" + sorted[0].severity;
    } catch (e) {
        if ($("status-stealth-text")) $("status-stealth-text").textContent = "CHECK UNAVAILABLE";
        if ($("status-stealth-dot")) $("status-stealth-dot").className = "status-dot degraded";
        if ($("status-stealth")) $("status-stealth").title = "Location consistency check could not run";
    }
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
function revealForm(id) {
    const form = $(id);
    if (!form) return;
    const anchor = form.querySelector(".btn-group") || form;
    anchor.scrollIntoView({ block: "nearest" });
}

function showProfileForm() { $("profile-form").classList.remove("hidden");
    revealForm("profile-form"); $("profile-name").value = ""; $("profile-name").focus(); }
async function confirmSaveProfile() { const name = $("profile-name").value.trim(); if (!name) return toast("Enter a name", "error"); const lat = parseFloat($("lat-input").value), lon = parseFloat($("lon-input").value); try { const r = await fetch("/api/profiles", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ name, lat: isNaN(lat) ? null : lat, lon: isNaN(lon) ? null : lon, speed: readSpeedKmh() || selectedSpeed, route_mode: $("route-mode").value }) }); if (r.ok) { loadProfiles(); toast('Profile "' + name + '" saved'); $("profile-form").classList.add("hidden"); } else { const d = await r.json(); toast(d.error || "Failed", "error"); } } catch (e) { toast("Error", "error"); } }

// ── Schedules ───────────────────────────────────────────────
async function loadSchedules() {
    try { const r = await fetch("/api/schedules"); const schedules = await r.json(); const c = $("schedule-list"); c.textContent = "";
    if (!schedules.length) { const e = document.createElement("div"); e.className = "empty-state"; e.textContent = "No schedules"; c.appendChild(e); return; }
    schedules.forEach(s => { const item = document.createElement("div"); item.className = "saved-item"; const n = document.createElement("span"); n.className = "saved-name"; const dayLabel = s.days?.length ? s.days.map(d => d[0].toUpperCase() + d.slice(1)).join(", ") : "Daily"; n.textContent = s.name + " @ " + s.time; const co = document.createElement("span"); co.className = "saved-coords"; co.textContent = dayLabel + " · " + s.lat.toFixed(2) + ", " + s.lon.toFixed(2); const del = document.createElement("button"); del.className = "saved-del"; del.title = "Delete"; del.textContent = "\u00D7"; del.addEventListener("click", async e => { e.stopPropagation(); await fetch("/api/schedules/" + encodeURIComponent(s.id), { method: "DELETE" }); loadSchedules(); toast("Schedule deleted"); }); item.appendChild(n); item.appendChild(co); item.appendChild(del); c.appendChild(item); });
    } catch (e) {}
}
function showScheduleForm() { const lat = parseFloat($("lat-input").value), lon = parseFloat($("lon-input").value); if (isNaN(lat) || isNaN(lon)) return toast("Set a location first", "error"); $("schedule-form").classList.remove("hidden");
    setTimeout(() => revealForm("schedule-form"), 0); $("schedule-name").value = ""; $("schedule-name").focus(); }
async function confirmAddSchedule() { const name = $("schedule-name").value.trim(); if (!name) return toast("Enter a name", "error"); const time = $("schedule-time").value; if (!time) return toast("Set a time", "error"); const lat = parseFloat($("lat-input").value), lon = parseFloat($("lon-input").value); const days = Array.from(document.querySelectorAll(".day-pill.active")).map(p => p.dataset.day); try { const r = await fetch("/api/schedules", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ name, lat, lon, time, days }) }); if (r.ok) { loadSchedules(); toast("Schedule created"); $("schedule-form").classList.add("hidden"); } else { const d = await r.json(); toast(d.error || "Failed", "error"); } } catch (e) { toast("Error", "error"); } }

// ── Route History ───────────────────────────────────────────
async function loadRouteHistory() {
    try { const r = await fetch("/api/routes"); const routes = await r.json(); const c = $("route-history-list"); c.textContent = "";
    if (!routes.length) { const e = document.createElement("div"); e.className = "empty-state"; e.textContent = "No saved routes"; c.appendChild(e); return; }
    routes.forEach(rt => { const item = document.createElement("div"); item.className = "saved-item"; const n = document.createElement("span"); n.className = "saved-name"; n.textContent = rt.name; const co = document.createElement("span"); co.className = "saved-coords"; co.textContent = rt.distance_km != null ? formatRouteDistance(rt.distance_km) : "?"; const del = document.createElement("button"); del.className = "saved-del"; del.title = "Delete"; del.textContent = "\u00D7"; del.addEventListener("click", async e => { e.stopPropagation(); await fetch("/api/routes/" + encodeURIComponent(rt.id), { method: "DELETE" }); loadRouteHistory(); toast("Route deleted"); }); item.appendChild(n); item.appendChild(co); item.appendChild(del); item.addEventListener("click", e => { if (e.target.classList.contains("saved-del")) return; if (rt.waypoints && rt.waypoints.length >= 2) {
            clearRoutePoints();
            routeStops = rt.waypoints.slice(0, 10).map(wp => {
                const label = wp.lat.toFixed(5) + ", " + wp.lng.toFixed(5);
                return { text: label, place: { name: label, display_name: label, lat: wp.lat, lon: wp.lng } };
            });
            renderStops();
            renderStopsOnMap(true);
            toast('Loaded "' + rt.name + '"');
        } }); c.appendChild(item); });
    } catch (e) {}
}
function suggestedRouteName() {
    const named = routeStops.filter(stop => stop.text.trim()).map(stop => stop.text.trim());
    if (named.length >= 2) return named[0] + " \u2192 " + named[named.length - 1];
    return "Route";
}

function openRouteSaveForm() {
    if (routePoints.length < 2) return toast("Build a route first", "error");
    const form = $("route-save-form");
    form.classList.remove("hidden");
    const field = $("route-save-name");
    field.value = suggestedRouteName();
    field.focus();
    field.select();
}

async function saveCurrentRoute() {
    if (routePoints.length < 2) return toast("Build a route first", "error");
    const name = $("route-save-name").value.trim();
    if (!name) return toast("Give the route a name", "error");
    try {
        const r = await fetch("/api/routes", {
            method: "POST", headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ name, waypoints: routePoints, speed: selectedSpeed,
                                   mode: $("route-mode").value, distance_km: routeDistanceKm })
        });
        if (!r.ok) {
            const detail = await r.json().catch(() => ({}));
            return toast(detail.error || "Could not save the route", "error");
        }
        $("route-save-form").classList.add("hidden");
        await loadRouteHistory();
        toast('Route "' + name + '" saved');
    } catch (e) { toast("Could not save the route", "error"); }
}
