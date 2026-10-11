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

None currently. Upstream caps are worked around rather than accepted where
the fix is safe: `marker-pdf` (≤2.0.0) caps `pillow<11`, so `pyproject.toml`'s
`[tool.uv] override-dependencies` forces `pillow>=12.3.0` (Dependabot
#56–#67) and every cluster install passes `--overrides
/opt/precis/overrides.txt` (written by
`deploy/playbooks/03-precis-constraints.yml`). Drop the override once
marker-pdf lifts its cap.
