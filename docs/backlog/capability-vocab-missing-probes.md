# Capability vocabulary tokens with no registered probe — warn-spam + unroutable

**Found** 2026-08-14 in melchior's worker log after the dark-factory deploy.

`capability_probe.probe_host_resources()` warns `no probe for required
capability %r` for every vocabulary token absent from `_PROBES` — currently
`git`, `claude_bin`, `claude_config_mount`, `clones_dir` (the ADR-0048
sandbox capabilities). Two consequences:

- **Log spam**: the docstring says "logged once" but the warning fires on
  every probe run (heartbeat path), so it repeats forever on every host.
- **Unroutable capabilities**: a token with no probe is `None` (never
  advertises, never retracts), so jobs gated on those capabilities can't be
  capability-routed anywhere — they only run wherever something else
  already advertised the row.

Fix: register real probes for the four sandbox tokens (`shutil.which` for
git/claude_bin, path-exists for the mounts/dirs), and/or actually log the
missing-probe warning once per process (module-level seen-set), not per run.
