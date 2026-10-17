# .claude/fleet/generated

GENERATED from .claude/fleet/authority.md by scripts/fleet-authority; do not edit.

JSON cannot carry a comment, so this note covers every file here:
claude-settings-<tier>.json, devin-config-<tier>.json, codex-<tier>.txt and
footer-<tier>.txt. Change .claude/fleet/authority.md, then run
`scripts/fleet-authority render`. tests/test_fleet_authority_drift.py fails
when any file here, this README included, differs from a fresh render.

Launchers do not copy these files. `scripts/fleet-authority apply --tier
R|W --to <worktree> --yes` replaces only the `permissions` key of the
worktree's .claude/settings.local.json and .devin/config.json (old copy kept
as .bak) and prints the tier's Codex flags.
