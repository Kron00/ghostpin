# Changelog

All notable changes to this project are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Changed

- Rebuilt the interface on a single design system. The coordinate readout is now
  the anchor of the Location panel — tabular monospaced figures on an instrument
  face — and the accent colour is reserved almost entirely for Warp, so the
  primary action no longer looks identical to Reset, Undo, and Save.
- Replaced the stacks of hairline dividers and inline-styled rows in the panels
  with labelled sections, and grouped the floating status pills into one status
  strip.
- Rewrote the light theme. It is now a token-level override and nothing else,
  which is what makes it maintainable; the previous version re-declared
  component rules and drifted into near-illegible pale grey on cream.
- Panels stack and scroll in document order below 940px instead of overlapping.

### Fixed

- The light theme was effectively unreadable: most text sat between 1:1 and 3:1
  against its background. Every visible text element now meets WCAG AA (4.5:1)
  in both themes, verified by measuring composited colours in the browser.
- Panel contents could overlap each other. The panel body is a height-capped
  flex column, so its children shrank below their own content — the coordinate
  readout was drawn on top of the latitude field.
- The Places panel ran off the bottom of the window; it now takes the space left
  under the Location panel and scrolls within it.
- Removed the nested scroll region inside the Places panel.
- Icon-only controls, the map, and the toast region had no accessible names.
- GPX waypoint names were always discarded on import. An `Element` with no
  children is falsy, so the fallback `or` in the namespace lookup threw away a
  valid `<name>`.

### Security

- Reject requests carrying an unexpected `Host` header. Binding to loopback does
  not stop a hostile page: a domain resolving to `127.0.0.1` makes the browser
  treat the API as same-origin, which exposed the UDID, current location, and
  saved history over plain GETs.
- Reject cross-origin writes. Any page open in the browser could previously
  submit a form POST to endpoints that take no body, resetting a spoof or
  triggering a device reconnect.
- Refuse GPX files that declare a `DOCTYPE` or entity, and cap imports at 16 MB
  and 100,000 waypoints. ElementTree expands internal entities, so a small file
  could otherwise exhaust memory on import.
- Write the privileged tunnel log inside the user's own support directory rather
  than a fixed path in world-writable `/tmp`, where it could be pre-created as a
  symlink and truncated by the root shell.
- Resolve `cmd.exe` by absolute path in the Windows elevation helper, so a
  planted `cmd.exe` earlier on `PATH` cannot be what the user elevates.

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

[Unreleased]: https://github.com/Kron00/ghostpin/compare/v2.0.0...HEAD
[2.0.0]: https://github.com/Kron00/ghostpin/releases/tag/v2.0.0
