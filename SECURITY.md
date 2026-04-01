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
