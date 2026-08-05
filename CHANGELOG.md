# Changelog

All notable changes to this project are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

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
