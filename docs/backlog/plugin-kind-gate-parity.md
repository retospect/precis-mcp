---
status: idea
title: Make kind enablement one boundary for built-ins, plugins, and siblings
prio: high
model: opus
---

# Make kind enablement one boundary for built-ins, plugins, and siblings

`PRECIS_KINDS_DISABLED` is not authoritative today. `precis.dispatch._load_plugins`
constructs and registers entry-point handlers without `kind_gate.gate`, without a
`Loadability`, and without the parsed prohibition set; reproduced with a fake
entry point, where `boot(kinds_disabled={'probe'})` still loaded `probe` and the
cold-start banner had no verdict. `Hub.sibling` has the same boundary problem:
on a booted hub, an absent/prohibited `job` or `todo` handler is lazily rebuilt
for direct internal use.

Make one gate-aware construction path cover built-ins and plugins while keeping
plugins' wider exception isolation. A booted hub must never lazily resurrect a
kind that boot rejected; test-only bare-hub convenience needs an explicit
separate path. Pin plugin `requires_env`/`requires_secret`/`requires_setting`,
prohibition reasons, banner loadability, and prohibited `job`/`todo` sibling
behaviour.

Owner anchors: `src/precis/dispatch.py::_try`,
`src/precis/dispatch.py::_load_plugins`, `src/precis/dispatch.py::Hub.sibling`,
`src/precis/kind_gate.py::gate`.
