# Contributing to Ghostpin

Thanks for your interest in improving Ghostpin. This document covers how to get
a development environment running, what we look for in a change, and the rules
that are non-negotiable for a project in this problem space.

## Ground rules

Ghostpin exists to make Apple's *developer* location-simulation service pleasant
to use. Contributions must stay inside that boundary.

We will **not** accept changes that:

- Bypass, disable, or work around Developer Mode, device trust, pairing, or any
  other Apple security control.
- Target devices the operator does not own or control.
- Add functionality whose primary purpose is defeating a specific service's
  fraud or integrity checks.
- Add telemetry, analytics, crash reporting, or any outbound call that is not
  required to render the map or resolve a search query.

Anti-detection features that already exist (cooldown timers, GPS jitter, IP/GPS
mismatch warnings) are there to prevent the user from producing physically
impossible location traces and getting their own account flagged. Extensions in
that spirit are fine. Building an evasion toolkit is not.

## Development setup

Requirements:

- macOS
- Python 3.10 or newer — Homebrew Python 3.12 or 3.13 recommended
- An iPhone with Developer Mode enabled and a trust relationship with the Mac

```bash
git clone https://github.com/Kron00/ghostpin.git
cd ghostpin
./start.sh
```

`start.sh` creates or reuses `.venv`, installs the pinned dependencies, and
starts the Flask UI on `127.0.0.1:8080`. It does not require `sudo` on iOS 17.4
and newer.

To build the native app bundle and DMG:

```bash
./build.sh
```

## Making a change

1. Open an issue first for anything larger than a bug fix. It is much cheaper to
   agree on an approach before the code exists.
2. Branch from `main`. Use a descriptive branch name.
3. Keep the change focused. A PR that fixes one bug is easier to review, and far
   easier to revert, than one that fixes a bug and reformats three files.
4. Match the surrounding style. The codebase uses 4-space indentation, snake_case
   in Python, and camelCase in JavaScript. There is no formatter in CI, so please
   don't reflow code you aren't otherwise touching.
5. Update the docs in the same PR. If you change a flag, an endpoint, or a
   keyboard shortcut, the README should reflect it before the PR merges.
6. Add an entry to `CHANGELOG.md` under `## [Unreleased]`.

## Testing

Ghostpin talks to real hardware, so there is no automated test suite that can
cover the interesting paths. Before opening a PR, exercise the flows your change
touches against an actual iPhone and describe what you did in the PR body.

At minimum, for changes to connection or location code:

- Connect over USB and confirm the device appears and reports its iOS version.
- Set a location and confirm it takes effect in Apple Maps on the device.
- Run a route simulation to completion.
- Reset, and confirm the device returns to its real GPS fix.

**Never attach screenshots of your phone or of the app showing real
coordinates.** Device screenshots leak home addresses, UDIDs, carrier
information, and saved places. Crop aggressively, or better, spoof to a public
landmark first and screenshot that. `test-evidence/` and `*.gpx` are gitignored
for this reason — please leave them that way.

## Reporting bugs

Use the bug report template. The two things that make a report actionable are
your exact iOS version and the full text of any error banner, including the
`pymobiledevice3` traceback if one appeared in the terminal.

Scrub your UDID and any real coordinates before posting.

## Security issues

Do not open a public issue. See [SECURITY.md](SECURITY.md).

## License

By contributing, you agree that your contributions are licensed under the MIT
License, consistent with [LICENSE](LICENSE) and the upstream attribution
recorded in [NOTICE.md](NOTICE.md).
