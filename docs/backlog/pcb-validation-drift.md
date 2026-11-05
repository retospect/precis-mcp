# Repair PCB validation drift found by the full suite

The full 83df68247 suite reports 24 failures. The exact 24-node comparison
on the original b86c2e36d engine reproduces 23; the Python reverse smoke
test differs by run context and remains under investigation. This scope
repairs the 20 PCB-owned failing nodes, not unrelated production code.
Evidence: task scratch `silk-full-suite-1105/full-83df.log` and baseline
run 20261105-130809-35300; root-cause analysis names the contracts below.

The 18 footprint-pose cases require a polygon representation even though
f25db2513 intentionally preserves native analytic rectangular lands with
world rotation. Preserve the independent reference-matrix checks: assert
the native rectangle dimensions/angle contract, reconstruct its actual
world corners independently from the emitted fields, and compare physical
corner geometry with the existing reference transform, accounting for
bottom-side winding. Keep all drill, pin and side assertions. Do not call
the production pad transformer to compute expected geometry, weaken
tolerances or change pad placement code.

The dead-export allowlist still lists `connected_pin_pairs` as unwired,
although the legitimate gerber_view caller from 33594d40a now uses it.
Remove that stale allowlist item; preserve the real caller audit and API.

Both PCB package docstrings exceed the existing 1500-word ceiling:
baseline PCB 1933 words, topoplace 1819; the silk rationale adds 52 words.
Keep the concise module maps and the reasons for non-obvious tradeoffs,
including original vendor-length accounting. Move detailed inventories or
extended rationale into linked reference documentation where necessary.
Bring both docstrings comfortably below the unchanged ceiling without
exemptions. Preserve the displaced information and add an appropriate
navigation link. No routing, geometry, fabrication or public API changes.

Run the full affected footprint-pose and dead-export files, repository
docstring lint, types and Ruff. Review the final test/reference geometry
and documentation diff before local intake through scripts/take-agent.
Keep unrelated coverage/upstream-watch and reverse-context findings
separate; do not repin a hash or weaken runtime policy to silence them.
No route/solver, production write, application deployment or ship.
