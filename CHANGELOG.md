# Changelog

All notable changes to this project are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

## [2.1.0] — 2026-08-06

Interface rebuild, a rethought route builder, adaptive speed, and the fixes
from eight rounds of automated browser auditing — six of them against the UI,
two against a real iPhone.

### Added

- **Routes with any number of stops**, lettered A, B, C…, filled from the
  search bar or by pointing at the map, reorderable by drag or Alt+arrow, with
  an option to return to the start and close the loop.
- **Roam**: pick a centre and a radius and wander the real road network inside
  it at random. It lays out a fresh stretch of road when one runs out, so it
  keeps going indefinitely rather than repeating a loop.
- **Adaptive speed** following posted limits from OpenStreetMap, easing off at
  stop signs, give-ways and traffic signals. Google's limits sit behind a paid,
  access-restricted API and are not usable.
- Search results can be sent straight to a stop, and every address field has
  the same autocomplete as the search bar.
- Speed units follow the country the IP lookup reports until set by hand.

### Changed

- The interface was rebuilt on one design system, anchored on the coordinate
  readout. Every visible text element meets WCAG AA in both themes.
- The route builder is three explicit modes — Stops, Circle became Roam, and
  Map — with a "?" explaining each.
- Walk/Bike/Drive presets are gone; one speed field plus the Adaptive toggle.
- Stops are filled from the search bar rather than typed into, so there is one
  search field instead of several half-working ones.
- Import/Export GPX removed in favour of Save route.

### Fixed

- The cooldown did not apply to the first jump after a Reset — usually the
  longest, and the one most likely to look like impossible travel.
- A route kept reporting itself as running when the phone went away, because
  every write failure was being swallowed.
- Editing the stops left a previously calculated route live, so Start would
  drive the route you had just edited away from.
- Onboarding was unusable under the Content-Security-Policy, which blocks the
  inline handlers it relied on.
- Roaming moved in straight lines across whatever was there; it follows roads.
- Around thirty smaller faults found by the audits: coordinate format blanking
  the fields, stale readouts, forms opening below the fold, controls colliding
  at narrow widths, arrow keys firing device calls, labels that did not match
  behaviour, and a favicon that 404'd on every load.

### Security

- Reject unexpected `Host` headers, blocking DNS rebinding against the local
  API, and reject cross-origin writes.
- Content-Security-Policy, `X-Content-Type-Options` and `Referrer-Policy`.
- Verify the peer's UDID before driving it over the privileged fallback.
- Cancel timed-out device coroutines; one lock owns the session lifecycle.
- GPX imports refuse DOCTYPE/entity declarations and are size-capped.
- UDIDs masked in logs and in the interface.
- Dependencies pinned exactly; GitHub Actions pinned to commit SHAs.

## [2.0.0] — 2026-08-05

First public release under the Ghostpin name. This is a rebrand and substantial
rework of the upstream iPhone Location Spoofer; see [NOTICE.md](NOTICE.md) for
attribution.

### Added

- Native macOS app bundle and DMG via `build.sh`, with a generated icon and a
  self-signed, internally consistent bundle.
- Pure-Python userspace RSD tunnel created inside the app process, so normal
  operation on iOS 17.4+ no longer requires `sudo` or an administrator password.
- Automatic Developer Disk Image mounting when a compatible image is missing.
- Multi-device selection over USB or paired Wi-Fi.
- Route simulation for walking, cycling, and driving, with GPX import and export.
- WASD/arrow-key joystick control and a random wander mode.
- Saved locations, recent locations, named profiles, schedules, and route
  history, persisted to `~/Library/Application Support/Ghostpin/`.
- Anti-flagging guidance: teleport cooldown timer, optional GPS jitter, and an
  IP/GPS mismatch warning with remediation steps.
- Google Maps keyless autocomplete as the primary place-search source, with
  Photon and Nominatim as automatic fallbacks.
- Dark and light cartographic map themes.
- Migration of existing user data from the upstream app's support directory on
  first launch.

### Changed

- Upgraded to `pymobiledevice3` 10.3.1 for iOS 27 compatibility.
- Rewrote the interface as a floating-panel workspace in a native `pywebview`
  window rather than a browser tab.
- Location keep-alive now serializes all DVT writes and reasserts the current
  coordinate once per second while the device stays connected, which fixes drift
  and dropped fixes during long route runs.
- The startup map marker showing the Mac's approximate network location is now
  styled distinctly from the device marker, to make clear it does not set the
  iPhone's location.

### Fixed

- Route timing no longer accumulates error over long simulated routes.
- Persisted saved and recent locations reload correctly after a restart.
- Reset reliably returns the device to its real GPS fix.

### Security

- The Flask server binds to `127.0.0.1` only.
- The privileged `tunneld` path is now a fallback, used only when userspace
  tunnel creation fails, rather than the default.

## Earlier history

Versions 1.0 through 1.5 were developed under the upstream project name and are
not distributed as Ghostpin releases. Their notable work included the initial
Flask and Leaflet interface, Wi-Fi mode, Python 3.13 support, the anti-detection
stealth suite, and the first `pymobiledevice3` v9 compatibility pass.

[Unreleased]: https://github.com/Kron00/ghostpin/compare/v2.1.0...HEAD
[2.1.0]: https://github.com/Kron00/ghostpin/releases/tag/v2.1.0
[2.0.0]: https://github.com/Kron00/ghostpin/releases/tag/v2.0.0
