# Phase 0 PR 4 — plugin ref passes (`precis.ref_passes`)

## Motivation

The `precis-dft` package needs to run a `view_worker` ref pass —
materializing the `structure_draft` view chunks asynchronously
after each edit so the agent-visible `edit` verb stays sub-second.
That worker cannot live in precis-mcp itself because it depends
on ASE / pymatgen / spglib — heavy science libraries we want out
of the core's dependency graph.

Today, every `RefPass` is hand-wired in `cli/worker.py`'s
`worker_cmd` function (`src/precis/cli/worker.py:332-548`). A
plugin can't register its own.

PR 4 adds a `precis.ref_passes` entry-point group so plugins can
ship background workers alongside their handlers and job_types,
mirroring how `precis.handlers` already lets plugins ship kinds.

## Today's shape

- `src/precis/workers/runner.py:111` — `RefPass = Callable[[int], BatchResult]`.
- `src/precis/cli/worker.py:332-548` — every ref pass is added
  manually to `ref_passes: list[RefPass]` inside a
  `_pass_enabled(name)` gate. Example
  (`cli/worker.py:431-449`):

  ```python
  if _pass_enabled("job_claude_inproc"):
      from precis.workers.executors.claude_inproc import (
          run_claude_inproc_pass,
      )

      def _job_claude_inproc_pass(batch_size: int) -> _BatchResult:
          r = run_claude_inproc_pass(store, limit=min(batch_size, 4))
          return _BatchResult(
              handler="job_claude_inproc",
              claimed=r["claimed"],
              ok=r["ok"],
              failed=r["failed"],
          )

      ref_passes.append(_job_claude_inproc_pass)
  ```

- `_pass_enabled(name)` (`cli/worker.py:317-325`) returns True
  if `--only` matches the name OR the name is in the profile's
  pass set.
- The `system` profile lists most passes (per CLAUDE.md);
  `agent` lists `structural`, `deep_review`; `dream` and
  `cron-tick` are their own profiles.
- `src/precis/dispatch.py:423-514` — `PLUGIN_GROUP =
  "precis.handlers"` and `_load_plugins(hub)`. The template to
  clone for the new EP group.

## Design

### Entry-point contract

A plugin advertises a ref pass via:

```toml
[project.entry-points."precis.ref_passes"]
view_worker = "precis_dft.workers.view_worker:factory"
```

The pointed-at object MUST be a **factory**, not the pass itself:

```python
# precis_dft/workers/view_worker.py
from precis.workers.runner import BatchResult, RefPass

def factory(
    store: "Store",
    *,
    profile: str,
    args: "Any",
) -> tuple[str, RefPass, frozenset[str]] | None:
    """Build a ref pass for this worker process.

    Returns:
        (pass_name, callable, profiles) — registered into the
        worker's pass list, gated by `_pass_enabled(pass_name)`.
        `profiles` is the set of `--profile` values this pass
        belongs to (e.g. ``frozenset({'system'})``).
        Returns ``None`` to opt out of this worker process
        entirely (e.g. plugin needs a GPU and this node has none).
    """
```

Three returns: the pass name (used by `_pass_enabled`), the
callable (signature `(batch_size: int) -> BatchResult`), and the
set of profiles it belongs to.

### Discovery and loading

A new module `src/precis/workers/_plugin_passes.py`:

```python
REF_PASS_PLUGIN_GROUP = "precis.ref_passes"

def discover_plugin_ref_passes(
    store: Store, *, profile: str, args: Any,
) -> list[tuple[str, RefPass, frozenset[str]]]:
    """Load all plugin ref pass factories matching the current
    profile. Failure isolation per `_load_plugins`: a broken
    plugin is logged and skipped, never raised."""
    out: list[tuple[str, RefPass, frozenset[str]]] = []
    try:
        eps = importlib.metadata.entry_points(group=REF_PASS_PLUGIN_GROUP)
    except Exception as exc:
        log.warning("precis.ref_passes discovery failed: %s", exc)
        return out
    for ep in eps:
        name = getattr(ep, "name", "<unknown>")
        try:
            factory = ep.load()
        except Exception as exc:
            log.warning("ref_pass %r failed to load: %s", name, exc)
            continue
        try:
            result = factory(store, profile=profile, args=args)
        except Exception as exc:
            log.warning("ref_pass %r factory raised: %s", name, exc)
            continue
        if result is None:
            log.info("ref_pass %r opted out of profile=%s", name, profile)
            continue
        try:
            pass_name, callable_, profiles = result
        except (TypeError, ValueError) as exc:
            log.warning("ref_pass %r factory returned bad shape: %s", name, exc)
            continue
        out.append((pass_name, callable_, profiles))
    return out
```

### Wiring into `worker_cmd`

After the built-in `ref_passes.append(...)` calls finish
(`cli/worker.py:548`), append:

```python
from precis.workers._plugin_passes import discover_plugin_ref_passes

for pass_name, callable_, profiles in discover_plugin_ref_passes(
    store, profile=args.profile, args=args,
):
    if profile_passes is not None and pass_name not in profile_passes:
        # Honour profile-set gate; the factory may have returned a
        # pass that doesn't belong on this profile (unusual but
        # allowed). The gate is the final word.
        log.info(
            "ref_pass %r built but not in profile=%s pass set",
            pass_name, args.profile,
        )
        continue
    if not _pass_enabled(pass_name):
        continue
    ref_passes.append(callable_)
    log.info("ref_pass %r registered (profile=%s)", pass_name, args.profile)
```

The factory's `profiles` return is informational and useful for
`--only` (which lets an operator force-enable a plugin pass);
the worker_cmd's `_pass_enabled` is the source of truth.

### What `precis_dft.workers.view_worker.factory` does

Pseudocode for the precis-dft side (informative, not part of
this PR):

```python
def factory(store, *, profile, args):
    if profile != "system":
        return None
    def view_pass(batch_size: int) -> BatchResult:
        r = run_view_pass(store, limit=batch_size)
        return BatchResult(handler="dft_view_worker", **r)
    return ("dft_view_worker", view_pass, frozenset({"system"}))
```

### Backward compatibility

Existing built-in ref passes are unchanged. The plugin loop runs
after every built-in is wired, so a plugin can't shadow a
built-in pass name. If two plugins return the same `pass_name`,
both are registered (the round-robin runs both); document as
"don't reuse names" but don't enforce.

## What does not change

- `RefPass` type alias and `run_loop` driver
  (`workers/runner.py`).
- Profile pass-set composition for the system / agent / dream /
  cron-tick profiles. Plugins must fit into one of these or
  declare a new profile (out of scope; would require CLI work).
- `_pass_enabled` semantics.
- The way `--only` selects a single pass for backfills.
- Built-in ref passes' wiring.

## Risk and rollback

- **Plugin failure isolation**: every layer of the load
  (entry-point enumeration, factory load, factory call, return
  unpack) catches `Exception` and logs. The worker process keeps
  running on any plugin failure.
- **Factory side effects**: the factory is called once per
  `worker_cmd` invocation. A factory that opens DB connections
  or loads ML weights at factory-call time pays that cost up
  front; long-running models should be lazy-loaded inside the
  pass callable.
- Rollback is a revert; plugin passes drop out of the loop.

## Tests

- `tests/workers/test_plugin_ref_passes.py` (new):
  - Mock an entry point via `importlib.metadata` monkeypatch.
  - Verify it loads, runs, and is registered into the
    worker's ref_passes list.
  - Verify a factory returning `None` is logged and skipped.
  - Verify a factory raising during build is logged and
    skipped without breaking the worker.
  - Verify a malformed return value is logged and skipped.
- `tests/cli/test_worker_plugin_passes.py` (extend or new):
  - Build a fake plugin via a temp installed wheel; assert
    `precis worker --once --profile=system` runs it.

## Files touched

| File | Change |
|---|---|
| `src/precis/workers/_plugin_passes.py` | New: `discover_plugin_ref_passes` + `REF_PASS_PLUGIN_GROUP`. |
| `src/precis/cli/worker.py` | Append plugin pass discovery after the built-in `ref_passes.append(...)` block (after line ~548). |
| `tests/workers/test_plugin_ref_passes.py` | New. |
| `tests/cli/test_worker_plugin_passes.py` | New. |
| `docs/user-facing/plugin-authoring.md` | Add a "Background workers (ref passes)" section after the handler authoring section. |
| `CHANGELOG.md` | Entry under `## Unreleased`. |
| `pyproject.toml` | Version bump. |

## Out of scope (separate PRs)

- Plugin registries for job_types and migrations (PR 1).
- Idempotency hardening, MCP frame chunking, `meta.no_index`
  filter (PR 2).
- `coordinator` executor + `wake_runner` (PR 3).

## Open questions

- **Should plugin passes participate in `--only`** when a plugin
  is installed but not normally on the profile? Currently yes:
  the factory builds the pass, `_pass_enabled` honours `--only`
  ahead of the profile gate. Document, otherwise no change.
- **A `precis worker --list-plugin-passes` introspection
  command** for confirming what's discovered. Useful but not
  blocking; defer.
- **Failure budget**: should a plugin pass that raises in its
  callable repeatedly get auto-disabled in the same worker
  process? Today, every chunk-handler raise is caught and the
  loop continues. Same goes for plugin passes via the same
  `run_loop` exception handler. Adequate for v1.
