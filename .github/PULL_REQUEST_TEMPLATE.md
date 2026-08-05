## What this changes

<!-- A short description of the change and why it is needed. Link the issue it
     closes, e.g. "Closes #12". -->

## How it was tested

<!-- Ghostpin talks to real hardware, so describe what you actually exercised.
     Include your iOS version and connection type. -->

- iOS version:
- Connection: USB / Wi-Fi

For changes to connection or location code, confirm:

- [ ] Device connects and reports its iOS version
- [ ] Setting a location takes effect in Apple Maps on the device
- [ ] A route simulation runs to completion
- [ ] Reset returns the device to its real GPS fix

## Checklist

- [ ] The change is focused on one thing
- [ ] Docs updated if behavior, endpoints, or shortcuts changed
- [ ] `CHANGELOG.md` updated under `## [Unreleased]`
- [ ] No real coordinates, UDIDs, home addresses, or device screenshots included
- [ ] No new outbound network calls, telemetry, or analytics
- [ ] Does not bypass Developer Mode, device trust, or any Apple security control
