# Security Policy

## Supported versions

Ghostpin is a desktop application without a server component. Only the latest
release receives fixes.

| Version | Supported |
| ------- | --------- |
| 2.0.x   | Yes       |
| < 2.0   | No        |

## Reporting a vulnerability

**Please do not report security issues through public GitHub issues.**

Use GitHub's private vulnerability reporting:
[Report a vulnerability](https://github.com/Kron00/ghostpin/security/advisories/new)

Include:

- What the issue is and the impact you believe it has
- Steps to reproduce, or a proof of concept
- The Ghostpin version, macOS version, and iOS version involved
- Whether the issue is in Ghostpin itself or in a dependency

You can expect an initial response within 7 days. If the issue is confirmed, we
will work on a fix and credit you in the release notes unless you prefer
otherwise.

## Scope

Ghostpin's threat model is narrow: it is a locally-run tool operated by the owner
of the device it controls. The Flask server binds to `127.0.0.1` and is not
intended to be reachable from the network.

**In scope:**

- Anything that lets a process or site outside Ghostpin drive the local API and
  control a connected device — for example DNS rebinding against the local
  server, or a missing origin check.
- Command injection, path traversal, or unsafe deserialization reachable from
  the UI or from a crafted GPX file.
- Privilege escalation through the `tunneld` fallback path, which is the one
  place Ghostpin can request administrator rights.
- Leakage of device identifiers, coordinates, or saved locations to a third
  party.
- Anything that causes the app to write outside its own support directory.

**Out of scope:**

- The fact that Ghostpin can change a device's reported location. That is the
  entire purpose of the tool, and it uses a documented Apple developer service
  that requires Developer Mode and an established trust relationship.
- Vulnerabilities that require the attacker to already have local code execution
  as the user, or physical access to an unlocked Mac.
- Issues in [pymobiledevice3](https://github.com/doronz88/pymobiledevice3) —
  report those upstream, though we appreciate a heads-up so we can pin around
  them.
- Missing hardening on a binding that is already localhost-only, absent a
  demonstrated bypass.

## Third-party services

Ghostpin sends search queries and the visible map center to place-search
providers, and requests map tiles as you pan. It sends no device identifiers,
coordinates you have set, or saved data to any third party. See the Privacy
section of the [README](README.md#privacy) for the full list of endpoints.
