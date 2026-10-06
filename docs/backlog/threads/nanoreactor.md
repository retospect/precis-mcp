# nanoreactor

## Resume

- **Pillar:** 3d-design
- **Next:** T1 entry slice: `/rxn` web ledger and kept reaction records (nanoreactor owner).
- **Blocked by:** Station-path-finder waits on this viewing/storage slice. T2 waits on hexfold's k = 3 seams.
- **Unblocks:** The per-station barrier ledger that decides whether the NO → NH3 nanoreactor needs a drive; the reaction movie.
- **Acceptance:** Each item's own acceptance criteria; the thread ends with the T6 movie of a designed nanoreactor.
- **Worktree:** `nanoreactor`
- **Builds:** Not estimated here; use the owning item's current slice estimate.
- **Detail:** [Ranked work](#do-next) · [Horizon](#horizon) · [Coordination map](INDEX.md).

## Thread context

**Status:** ends when a designed NO → NH3 nanoreactor plays as a
four-pane movie built from computed station records, every barrier on
the path checked at DFT ([nanoreactor-no-nh3](../nanoreactor-no-nh3.md)).
T0 tabulated thermochemistry shipped in R13/R14, including the radical
fits; T1 starts with a viewing surface and a stored equation set. Order
follows the data: each tool consumes the previous one's output.
**Worktree:** `nanoreactor`

## Do next

1. **T1 entry: web ledger and kept reaction records** — active, spec below.
   T0 ledger slice 1 shipped (`get(kind='rxn', view='energetics')`), including
   H2NO/HNOH/NH2OH coverage; no xTB fallback is needed for these fits.
2. **backlog/nanoreactor-station-path-finder.md** — T1; blocked by the
   entry slice above. The bare and
   theozyme tiers decide whether a drive is needed, and its station record
   is the contract every later tool reads.

### T1 entry slice specification

Owning work record: `todo:470273` (development evidence and release follow-up;
the first kept reaction ref is added after deployment).

`GET /rxn` accepts `q`, `T` (default 298.15 K), and optional
`n_electrons`. A small form renders the existing rxn handler's ledger as
tables: per-step ΔH/ΔG/cumulative/uphill, then each step's species with
name/CAS/source and the handler's source/licence notes verbatim. The page
states gas-phase NASA-7 fits at 1 bar, no barriers, and no electrode
reference; the mechanism's original pressure remains unverified and its
entropy constants unadjusted. Explicit liquid inputs retain their phase
labels. Handler refusal text must survive bad inputs without a 500.

`POST /rxn/keep` uses `put(kind='rxn', id=<stable slug>,
meta={'energetics': {'q': ..., 'T': ..., 'n_electrons': ...}})`.
Use `rxn`: this is a kept tabulated reaction equation set, whereas
`pathway` is catpath's computed run record to which future station records
attach. No SMILES or measured property value is invented. Store validated
inputs and a computed text snapshot; the default reader and
`get(kind='rxn', id=..., view='energetics')` recompute from pinned sources.
The snapshot is labelled archival, never used as the current derived view.
Repeated identical keeps reuse the same slug; refusal writes no record.

The kept record opens through the shared `/refs/rxn/<id>` reader. Build on
current main; coordinate the R16 kind-gate change rather than duplicating
it. Update `precis-rxn-help` with `/rxn` and the keep call. Acceptance:
the four explicit NO→HNO→H2NO→NH2OH→NH3+H2O steps render at 300 K;
keep creates/reuses a ref and its reader shows the derived ledger;
bad temperature and impossible balance show the handler's refusal.
No coefficients, source ranges/licences, barriers, jobs or models change.
Root owns release/full gate; target the cycle after R16. After deployment,
keep the first NO→NH3 ledger at 300 K and link its melchior `/refs` URL
here. Candidate review is independent and precedes integration.

## Horizon

1. **backlog/hexfold-nanoreactor-tube.md** — T2; waits on k = 3 seams
   (hexfold-toolkit owns the build).
2. **backlog/nanoreactor-no-nh3.md §T3** — track designer; spec once
   T1's station record exists.
3. **backlog/nanoreactor-no-nh3.md §T4** — ring/pendant designer; spec
   once T3 exists.
4. **backlog/nanoreactor-no-nh3.md §T5** — assembled QM/MM check; needs
   T2 + T4.
5. **backlog/nanoreactor-movie.md** — T6; renders T1 records first,
   T5 records later.

## Parked

- **backlog/cnt-channel-staged-catalysis.md** — the general channel
  concept and its reading list; unparks with T2.
