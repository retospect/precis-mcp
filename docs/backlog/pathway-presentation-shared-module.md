---
status: draft
title: fold the duplicated pathway presentation logic into autocatpath.presentation
prio: normal
---

# Fold the duplicated pathway presentation logic into `autocatpath.presentation`

## Motivation / why

A pathway run has two web surfaces, and they duplicate the *model layer*
under the drawing rather than just the drawing:

* `autocatpath/report.py` writes a self-contained, deterministic
  `index.html` — offline, `file://`-openable, scp-able to a collaborator
  who has no precis and no DB.
* `precis_web` renders `/refs/pathway/<id>` from the stored record, with
  the candidate stepper, U/pH lever, warnings and ref links the offline
  page has no business carrying.

Both are wanted; the surfaces are not the problem. Three pieces of
*logic* exist twice:

1. **Kinetics verdict + payload trim** — `report._kinetics_verdict` /
   `_kinetics_payload` vs `precis_web/pathway_kinetics.py`, whose module
   docstring asks the next editor to re-sync by hand.
2. **Root→leaf path enumeration and ranking** — `viz._select_paths` vs
   `precis_web/routes/refs.py::_pathway_paths`.
3. **Species display labels** — `viz._species_label` vs precis's own.

What makes this worse than ordinary duplication: each copy carries its own
detailed suite (`catpath/tests/test_kinetics_verdict.py`,
`tests/precis_web/test_pathway_kinetics.py`). A threshold moved on one side
leaves BOTH green. The tests cement the fork instead of catching it.

Half-shipped already: `tests/precis_web/test_pathway_kinetics_parity.py`
pins (1) against a golden generated from the engine, so drift now reddens.
That is a gate, not a fix — the two copies still exist, and (2) and (3) are
still unguarded.

## In scope

* An `autocatpath.presentation` module owning the verdict, the path
  selection/ranking, and the species labeller — the engine already depends
  on all three, and precis already depends on the engine, so the dependency
  direction is unchanged and no new package appears.
* Delete precis's copies (`precis_web/pathway_kinetics.py`'s ported half,
  `refs.py::_pathway_paths`, the label helper) and their now-redundant
  tests. The parity file becomes a thin import check, or goes away entirely
  once there is one implementation left to test.
* Dispatch the pre-0.18 warning shim on the engine version instead of
  sniffing sentence wording. **The stamp already exists** — `persist.py`
  writes `meta.autocatpath_version` as a sibling of `meta.results`, so
  there is nothing to add and no backfill to run; the reader simply is not
  using it. Measured on prod 2026-09-27:

  | engine | refs | with kinetics |
  |---|---|---|
  | 0.17.0 | 230 | 229 |
  | 0.20.0 | 24 | 24 |
  | 0.22.0 | 9 | 8 |
  | 0.18.0 / 0.19.0 | 1 each | 1 each |
  | 0.2.0 / 0.12.1 / 0.13.0 | 221 | 0 |
  | (null) | 196 | 0 |

  Two conclusions. The shim is **load-bearing** — 229 of the 263
  kinetics-bearing records are 0.17.0, the pre-0.18 vintage — so it cannot
  be deleted, only version-gated. And every record that *has* kinetics has
  a version, so the gate is available today.

* Make an unrecognised pressure warning degrade **visibly**. Today a
  "no pressure stated" warning matching neither wording yields no caveat at
  all: the disclaimer disappears from the panel and nothing says so. Pinned
  as `pressure_defaulted_unparsed_wording` in the parity golden, which
  characterizes the wart without endorsing it. Both copies share the
  behaviour, so the fix has to land on both sides at once — which is why it
  belongs here and not as a precis-only patch.
* Fix `report.py`'s claim (in the module docstring, the "One profile
  renderer" paragraph) that the PNG and HTML paths agree "by construction,
  never by two implementations happening to match". `_pathway_paths`
  falsifies it. This edit is in the catpath repo.

## Explicitly NOT in scope

* **Merging the two JS renderers** (`report.py`'s `_PAGE_SCRIPT`, ~861
  lines; the inline script in `templates/refs/pathway_detail.html.j2`, ~981).
  They diverge on affordances, not on drawing, and there is no cross-check
  possible for either. Revisit only once the payload is shared; until then
  treat both as frozen — a feature added to one widens a gap nothing can
  test.
* **De-duplicating the vendored 3Dmol** (501 KB in each tree). Two copies of
  a third-party library is not a maintenance surface, and independent pins
  are arguably correct.
* **Folding catpath into precis.** The offline report is a different
  product: a folder you hand to someone with no precis. Keep it.
* An upper version pin on `autocatpath`. Considered and rejected — the
  parity gate already runs against whatever engine is installed in the dev
  image, which is a live check where a `<0.23` ceiling would only be a
  guess that breaks deploys.

## Acceptance criteria

* `grep -c 'def kinetics_verdict' src/` is 0; the web panel and the offline
  report reach the same function.
* `_pathway_paths` is gone from `routes/refs.py`; the page's `paths` payload
  comes from the engine's selector.
* The pre-0.18 wording regex is behind a `meta.autocatpath_version` check
  rather than being the check, and an unmatched "no pressure stated"
  warning produces a visible caveat instead of silence.
* `tests/precis_web/test_pathway_kinetics_parity.py` still passes unchanged
  through the refactor (it is the characterization baseline) before being
  retired.
* The false "by construction" sentence in `report.py` is corrected.

## Target + blast radius

`src/precis_web/pathway_kinetics.py` · `src/precis_web/routes/refs.py`
(`_pathway_paths`, `_pathway_detail`) · `src/precis_web/static/pathway-kinetics.js`
(payload shape only) · `src/precis_pathway/runner.py` (record write, for the
version stamp) · catpath `src/autocatpath/{report,viz}.py`.

Cross-repo: needs a catpath version bump + wheel redeploy before precis can
import the new module (`scripts/ship` on catpath, then the host wheelhouse —
a same-version wheel is a host no-op).

## The constraint that makes this non-trivial

`precis_web` imports `autocatpath` **nowhere** — checked, zero hits — and
that is deliberate, not an oversight. `routes/refs.py:60` imports
`kinetics_payload` at module scope, so it runs on every web boot, including
on hosts that never install the `[catalyst]` extra. The engine is optional:
`precis_pathway/handler.py:140` raises `InitError` when it is absent and the
`pathway` kind darks; `deploy/roles/precis_web/tasks/main.yml:102` installs
`precis-mcp[catalyst]` only for hosts in the `autocatpath_plugin` capability
list. **This is why the code was ported instead of imported.**

So the fold-in is not a plain import swap. Either `pathway_kinetics` imports
the shared module lazily inside each function with an `ImportError` path, or
the shared helpers move somewhere that is not an optional extra. Cost this
properly before starting — an eager import here takes down the web app on
every non-plugin host, which is a worse failure than the duplication.

Note this interacts with the precis-util / plugin-split design (a separate
thread): if catpath becomes one of several `precis-xxx` plugin models, where
shared presentation logic lives is a question that design answers, and this
item should follow it rather than pre-empt it.

## Open questions / decisions log

* Does `presentation` also own the *payload assembly*, or only the three
  pure helpers? The trims are not currently comparable — the engine's reads
  `kinetics.json` off a directory, precis's trims a stored dict — so a
  shared trim means first agreeing one input type.
* Ordering against the pathway-effects-report work already in flight
  (`pathway-conditions-effects-report.md`), which touches the same page.
