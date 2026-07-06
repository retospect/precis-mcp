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
lockfile-only bump; it will fail to resolve. The authoritative, machine-parseable
list (with `Recheck-after:` dates and `Unblock-when:` conditions) is the
**"⏸️ Snoozed — blocked upstream"** section of [`OPEN-ITEMS.md`](OPEN-ITEMS.md).

| Alert | Package | Blocked by | Why tolerable | Recheck |
| ----- | ------- | ---------- | ------------- | ------- |
| [#44](https://github.com/retospect/precis-mcp/security/dependabot/44) (high) | `transformers` <5.3.0 RCE | `marker-pdf` (≤1.10.2) hard-pins `transformers<5.0.0`; needed by the `[paper]` OCR/layout extra, so `>=5.3.0` is **unsatisfiable** until marker lifts the cap | precis only loads the trusted local **bge-m3** embedder — never a user-supplied model path or `trust_remote_code`, which is what these `transformers` RCEs require | `2026-07-18` (see OPEN-ITEMS.md) |
