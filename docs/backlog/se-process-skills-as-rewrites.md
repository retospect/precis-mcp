# se: process skills as geometry rewrites — a `suggested_fix` that carries the ops

Follow-on filed at the ship of `se-print-implementer` (2026-09-17).
Process DRC (`precis_se.printing`, `view='print'`) returns
`ValidationIssue`s whose `suggested_fix` is **text** ("split the span",
"add a chamfer under the face", "drill after printing"). Owner anchor:
`precis_se.printing`.

**What.** A process skill is a rewrite: the finding carries the L3 ops
that would fix it (a `cut`/`chamfer` node added to the block's bound cad
design, a `set_envelope` on a sub-`min_feature` spoke), applied only
through the normal `edit(kind='se', ops=[...])` / `edit(kind='cad', ...)`
path — never silently, the finding just hands the agent a ready op list.
First skill: `bridge-closing-a-bored-ceiling` (a `bridge` finding whose
patch is a hole ceiling → the sacrificial-layer / teardrop rewrite).

**Why.** se-print-implementer's rule table already names the fix per
rule; an agent reading `view='print'` today has to translate prose into
ops by hand, and the translation is the same every time.

**Test:** a bored-ceiling fixture's `bridge` finding carries an ops list;
applying it through `edit` clears the finding on re-read; nothing in the
tree changes without that explicit `edit`.
