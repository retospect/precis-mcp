"""slice_env boundary probe (td574934 A4, gr596928).

The slice session env is an allowlist
(:data:`precis.workers.job_types.run_slice.ENV_ALLOWLIST` read by
:func:`~precis.workers.job_types.run_slice.slice_env`): every name the
worker's environment may carry into a session. The boundary: no DB DSN
and no vault variable may be on it — a session that inherits
``PRECIS_DATABASE_URL`` or a vendor key bypasses every other fence.

The name set checked is derived, not hard-coded: every literal
``get_secret(…)`` / ``mounted_secret(…)`` / ``require_vault_secret(…)`` /
``reveal_unbound(…)`` vault name in the tree's ``src/``, every
``*_REF``/``*_VAR`` env-name constant, plus the fixed connect/auth family
(``PG*``, ``*_DATABASE_URL``, the vendor keys) — so a newly introduced
credential is covered without editing the probe. Harvesting still only
covers names the tree already reads, so a *shape* check runs on top
(td598968): any allowlisted/emitted name matching
:data:`_CREDENTIAL_NAME_RE` — ``*_DSN``, ``*_SECRET*``, ``*_TOKEN``,
``*_PASSWORD``, ``*_KEY``, ``*_DATABASE_*``, ``*_DB_*``, ``*_VAULT_*``,
``PG*`` and friends — is denied whether or not anything references it,
which is what refuses a freshly coined ``ARCHIVE_DSN``. Two names are
sanctioned by design and exempt: ``PRECIS_MCP_TOKEN`` (the session's own
scoped bearer) and ``CLAUDE_CODE_OAUTH_TOKEN`` (the subscription login —
the allowlist's whole point, filled from the vault when absent). Under
``mcp=False`` even those must not cross.

Contract: exit 0 pass, 1 boundary breach (stdout = the finding), else
cannot-run (exit 2 here). ``argv[1]`` names the tree root (default: cwd).
"""

from __future__ import annotations

import os
import re
import sys
from pathlib import Path

#: The two allowlist entries that are credential-shaped BY DESIGN —
#: everything else credential-shaped must be absent.
_SANCTIONED = frozenset({"PRECIS_MCP_TOKEN", "CLAUDE_CODE_OAUTH_TOKEN"})

#: DSN + auth names no vault read is needed to know about.
_FIXED_DENY = frozenset(
    {
        "PRECIS_DATABASE_URL",
        "DATABASE_URL",
        "PGHOST",
        "PGPORT",
        "PGUSER",
        "PGPASSWORD",
        "PGDATABASE",
        "PGSERVICE",
        "PGPASSFILE",
        "PGSSLMODE",
        "PGCONNECT_TIMEOUT",
        "ANTHROPIC_API_KEY",
        "OPENAI_API_KEY",
        "KIMI_MODEL_API_KEY",
        "KIMI_CODE_API_KEY",
        "DEVIN_API_TOKEN",
        "GH_TOKEN",
        "PRECIS_SECRETS_FILE_DIR",
        # Credential-CARRYING config rather than a credential-shaped name:
        # kubeconfig embeds client certs, docker config registry auth,
        # netrc and askpass helpers login material.
        "KUBECONFIG",
        "DOCKER_CONFIG",
        "NETRC",
        "GIT_ASKPASS",
        "SSH_ASKPASS",
    }
)

#: Name SHAPES that mean credential whatever the rest of the word is
#: (td598968 — the harvested set only covers credentials the tree
#: already reads, so an unreferenced ``ARCHIVE_DSN`` used to pass).
#: Matched on whole ``A-Z0-9`` components: ``SSH_AUTH_SOCK``'s ``AUTH``
#: and ``PRECIS_NO_AUTOREAP`` deliberately do not trip it.
_CRED_NAME_PARTS = (
    "DSN",
    "SECRET",
    "TOKEN",
    "PASSWORD",
    "PASSWD",
    "PASSPHRASE",
    "PASSFILE",
    "PASS",
    "KEY",
    "CREDENTIALS",
    "CREDENTIAL",
    "CREDS",
    "BEARER",
    "OAUTH",
    "DATABASE",
    "DB",
    "VAULT",
    "JWT",
    "CERTIFICATE",
    "CERT",
    "PEM",
)
_CREDENTIAL_NAME_RE = re.compile(
    r"(?:^|_)(?:" + "|".join(_CRED_NAME_PARTS) + r")(?:_|$)|^PG[A-Z0-9_]*$"
)

#: Literal vault names: ``get_secret("NAME")`` & friends.
_VAULT_CALL_RE = re.compile(
    r"(?:get_secret|mounted_secret|require_vault_secret|reveal_unbound)"
    r"\(\s*[\"']([A-Z][A-Z0-9_]+)[\"']"
)
#: Env-name constants: ``ENV_VAR = "NAME"``, ``API_KEY_REF = "NAME"``.
_ENV_CONST_RE = re.compile(r"(?:_REF|_VAR)\s*=\s*[\"']([A-Z][A-Z0-9_]+)[\"']")


def _secret_names(root: Path) -> set[str]:
    """Every credential-shaped env name the tree knows about."""
    names = set(_FIXED_DENY) | set(_SANCTIONED)
    src = root / "src"
    for py in src.rglob("*.py") if src.is_dir() else []:
        try:
            text = py.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        names.update(_VAULT_CALL_RE.findall(text))
        names.update(_ENV_CONST_RE.findall(text))
    return names


def check(root: Path) -> list[str]:
    """Every finding; empty = pass."""
    src = str(root / "src")
    if src not in sys.path:
        sys.path.insert(0, src)
    from precis.workers.job_types import run_slice

    findings: list[str] = []
    names = _secret_names(root)
    denied = names - _SANCTIONED
    leaked = sorted(set(run_slice.ENV_ALLOWLIST) & denied)
    if leaked:
        findings.append("ENV_ALLOWLIST carries DSN/vault names: " + ", ".join(leaked))
    saved = dict(os.environ)
    os.environ.update({n: "probe-marker" for n in names})
    try:
        env_mcp = run_slice.slice_env(0, "low", mcp=True)
        env_plain = run_slice.slice_env(0, "low", mcp=False)
    finally:
        os.environ.clear()
        os.environ.update(saved)
    bad = sorted(set(env_mcp) & denied)
    if bad:
        findings.append("slice_env(mcp=True) emits DSN/vault names: " + ", ".join(bad))
    bad_plain = sorted(set(env_plain) & names)
    if bad_plain:
        findings.append(
            "slice_env(mcp=False) emits credential names: " + ", ".join(bad_plain)
        )
    shaped = sorted(
        n
        for n in set(run_slice.ENV_ALLOWLIST) | set(env_mcp) | set(env_plain)
        if n not in _SANCTIONED and _CREDENTIAL_NAME_RE.search(n)
    )
    if shaped:
        findings.append(
            "credential-shaped name(s) reach the session: " + ", ".join(shaped)
        )
    return findings


def main(argv: list[str] | None = None) -> int:
    """Probe entry: ``[tree_root]``; 0 pass, 1 breach, 2 cannot-run."""
    argv = list(sys.argv if argv is None else argv)
    root = Path(argv[1]).resolve() if len(argv) > 1 else Path.cwd()
    try:
        findings = check(root)
    except Exception as exc:
        print(f"cannot-run: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 2
    for finding in findings:
        print(finding)
    return 1 if findings else 0


if __name__ == "__main__":
    raise SystemExit(main())
