# Security Policy

## Supported Versions

| Version | Supported          |
| ------- | ------------------ |
| latest  | :white_check_mark: |

## Reporting a Vulnerability

If you discover a security vulnerability, please report it responsibly:

1. **Do not** open a public GitHub issue.
2. Use [GitHub's private vulnerability reporting](https://github.com/retospect/precis-mcp/security/advisories/new) to submit a report.
3. You will receive an acknowledgement within 48 hours.

## Security Practices

- All GitHub Actions are **pinned by commit SHA** to prevent supply chain attacks.
- PyPI publishing uses **trusted publishing** (OIDC) — no long-lived API tokens.
- Build artifacts include **provenance attestations** via [actions/attest-build-provenance](https://github.com/actions/attest-build-provenance).
- **Dependabot** monitors dependencies (pip + GitHub Actions) for known vulnerabilities.

## Known / accepted open alerts

Some Dependabot alerts are **real but not currently resolvable** because an
upstream dependency caps the fixed version. These are tracked, risk-assessed,
and rechecked on a schedule — they are **not** unnoticed. Do **not** attempt a
lockfile-only bump; it will fail to resolve. The authoritative list is the
snoozed items in [`docs/backlog/`](docs/backlog/README.md) (front-matter
`snooze-until:` + an `Unblock-when` condition, e.g.
[`pillow-marker-pin`](docs/backlog/pillow-marker-pin.md)).

| Alert | Package | Blocked by | Why tolerable | Recheck |
| ----- | ------- | ---------- | ------------- | ------- |
| [#56–#67](https://github.com/retospect/precis-mcp/security/dependabot) (11, mostly high) | `pillow` <12.3.0 heap-OOB / DoS / decompression-bomb bypass | `marker-pdf` (≤2.0.0) hard-pins `pillow<11.0.0`; needed by the `[paper]` OCR/layout extra, so `>=12.3.0` is **unsatisfiable** until marker lifts the cap | precis only feeds Marker/Pillow trusted PDF ingestion behind the `[paper]` extra — none of the specific vectors (PSD/FITS/JPEG2000/TGA/mmap font-loading paths) are reachable from precis's own code | `2026-08-06` (see `docs/backlog/pillow-marker-pin.md`) |
