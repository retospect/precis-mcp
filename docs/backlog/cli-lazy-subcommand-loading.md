---
status: draft
pillar: platform
title: lazify CLI subcommand loading via a precis.cli entry-point group — one missing optional dep must not take down `precis serve`
prio: high
model: opus
---

# Lazify CLI subcommand loading via a `precis.cli` entry-point group

## Motivation / why

`src/precis/cli/main.py:26` eagerly imports every subcommand module in one
`from precis.cli import (...)` block — 58 files in `src/precis/cli/`, including
`taproot.py` (70 K), `quest.py` (25 K) and `patent.py` (24 K). The console
script is `precis = "precis.cli:main"` (`pyproject.toml:435`), so **every**
`precis` invocation pays for all of them, `precis serve` included.

This has already caused a cluster outage. `tenacity` sat behind the `[paper]`
extra; a host without that extra hit `ModuleNotFoundError` importing a
subcommand it was never going to run, `precis serve` exited 1, every node's
embedder died and the embed worker crash-looped. The remedy at the time was to
promote `tenacity`/`pysbd`/`num2words`/`shapely` to core dependencies as
insurance — which treats the symptom. The defect is that an unrelated
subcommand's optional dependency can abort `serve`.

Fixing it is worth doing on that ground alone. It is *also* a hard prerequisite
for the plugin split (`plugin-split-runtime-shell.md`): without it,
`pip install precis-util precis-catpath` dies importing `precis.cli.taproot`,
which will not be installed at all.

## In scope

* A `precis.cli` entry-point group; each subcommand registers its own module
  rather than being named in a literal import block.
* Resolve a subcommand's module only when that subcommand is actually invoked.
* Carry each subcommand's one-line help **in entry-point metadata**, so
  top-level `precis --help` stays complete without importing any subcommand
  module. This is the part that makes the change invisible to users and is the
  easiest to get wrong.
* A test that a subcommand whose module raises `ImportError` leaves both
  `precis --help` and `precis serve` working, and fails only when that
  subcommand is invoked — with a message naming the missing dependency, not a
  bare traceback.

## Explicitly NOT in scope

* **Reverting the four promoted core deps.** `tenacity`/`pysbd`/`num2words`/
  `shapely` stay where they are; demoting them is a separate decision with its
  own blast radius, and doing it in this change would conflate the fix with a
  dependency move.
* **Splitting any package.** This lands standalone, on the current single
  wheel, and is valuable there. The plugin split consumes it; it does not
  depend on the split.
* **Reorganising the `cli/` tree.** Same 58 modules, same names, same file
  layout — only how they are discovered changes.
* **Lazifying anything outside `cli/`.** `server.py` is already a clean shell.

## Acceptance criteria

* `precis <subcommand> --help` and bare `precis --help` both work without
  importing the other 57 subcommand modules — asserted by a test that inspects
  `sys.modules` after each, not by timing.
* Top-level `--help` lists every subcommand with its one-line help, identical
  to today's output.
* A subcommand module that raises `ImportError` does not affect `precis serve`,
  `precis --help`, or any other subcommand; invoking that one subcommand
  reports the missing dependency by name.
* The `[project.scripts]` entry point and every existing `precis …` command
  line keep working unchanged — this is behaviour-neutral at the CLI surface.

## Target + blast radius

`src/precis/cli/main.py` (the import block and dispatch) · all 58
`src/precis/cli/*.py` modules (registration only) · `pyproject.toml`
(`[project.entry-points."precis.cli"]`, `[project.scripts]`) · anything that
shells out to `precis` — `scripts/`, `deploy/` roles, the workers.

Deploy note: this changes the installed file list and the import graph of a
long-running process. Cycle the services on deploy — see gr457894.

## Open questions / decisions log

* Does the one-line help live in the entry-point name, a separate
  `precis.cli.help` group, or a small manifest? It must be readable without
  importing the target module; that constraint picks the answer.
* Do the workers invoke subcommands in-process anywhere (rather than via the
  console script)? Those call sites need the same lazy resolution, and they are
  the likeliest place for a missed import to hide.
