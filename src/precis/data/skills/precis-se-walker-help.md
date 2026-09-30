---
id: precis-se-walker-help
title: precis — DNA walkers in se (foothold-occupancy states, station settles, per-state poses)
summary: a walker is a plain se block with tethered legs (declare_strand anchor=/tether_nt=) that step along a track through declared stations — states whose occupancy=[{'<strand>.<ord>': '<helix>@<offset>'|null}] says which foothold each leg's foot domain sits on (declare_states, or the declare_stations sugar op for the common hand-over-hand gait); relax_chain(state={walker: name}) settles the walker as one more rigid body and stores the result in that state's own pose slot, so a later get(args={'state': ...}) or view='sweep' reads the station with no re-solve
answers:
  - how do I tether a walker leg to a rigid body instead of letting it float?
  - what does a walker state's occupancy map mean, and what does a null target do?
  - how do I declare a hand-over-hand gait without hand-writing every station's occupancy?
  - how do I settle one station's pose without moving the whole track?
  - how do I read a design posed into one station (view='block'/'chain'/'drc'/'tree'/'clearance')?
  - why does view='sweep' report chain_state_unrelaxed, and what does it mean for a leg tether?
  - what walker features aren't built yet (transition guards, spectral budget, make_steps, cursor geometry)?
applies-to: put/edit (kind='se', op=declare_strand(anchor=)|declare_states(occupancy=)|declare_stations|relax_chain(state=)) plus get(kind='se', view='sweep'|'block'|'chain'|'drc', args={'state': …})
status: active
tags: verbs, design
kinds: se
---

# precis-se-walker-help — DNA walkers in se

A **walker** is a plain se block (never a chain block — the rigid part the
legs hang off) with one or more tethered **legs**: strand blocks whose 5'
end hangs off a named port of the walker instead of just floating. A
**station** is a declared state whose `occupancy` says which foothold each
leg's foot domain sits on; `relax_chain(state=...)` settles the walker as
one more rigid body and stores its per-station pose, so reading a station
back needs no re-solve. Read [[precis-se-chain-help]] first for the
underlying helix/strand/domain vocabulary (`declare_helix`, `add_domain`,
`layout_chain`, the plain `relax_chain` settle) — this skill only covers
what a walker adds on top of it.

## Tether a leg — `declare_strand`

`declare_strand` grows two keys:

```python
{'op': 'declare_strand', 'block': 'la', 'anchor': 'w.pa', 'tether_nt': 12}
```

`anchor='<body block>.<port>'` — the strand's 5' end hangs off that port,
reached through `tether_nt` unpaired nucleotides (default 0). The anchor
block must be a **plain rigid body** — not a helix/strand/segment, and not
the strand itself — and, once you settle it, top-level (no `parent`: its
own parts move with it as children). The port needs its own `pose=`
(`add_port`/`set_port_pose`) — the tether has nothing to pull from
otherwise. The strand's **first** domain (`add_domain`'s `ord=0`, appended
as usual) is the **foot** — the one the tether reaches and the one
`occupancy` targets.

## Declare occupancy states — `declare_states`

`declare_states` grows one key:

```python
{'op': 'declare_states', 'block': 'w', 'states': [
    {'name': 'st0', 'occupancy': {'la.0': 'f0@4', 'lb.0': None}},
]}
```

`occupancy` keys are `'<strand>.<ord>'` (the foot domain, almost always
`.0`); each target is `'<helix>@<offset>'` — the domain's **start** offset
in that state (length/direction still come from the domain row, never a
1-bp override) — or `null` for a **free** leg: lifted off every helix,
exempt from pairing and `chain_dangling_domain` in that state, no loop
spring, and reported as unpaired/`free` by `view='chain'`. A `pose` key on
a state is **refused** — a state's pose is derived by `relax_chain`, never
authored.

## `declare_stations` — the hand-over-hand sugar op

Sugar over the two ops above for the common gait:

```python
{'op': 'declare_stations', 'walker': 'w',
 'legs': ['la', 'lb'],                       # rear -> front
 'footholds': ['f0@4', 'f1@4', 'f2@4'],       # along the track, in order
 'driver_kind': 'light',                      # default
 'forward_driver': '405nm', 'reverse_driver': '365nm'}
```

Station *i* has leg *j* on foothold *i+j* — `N = len(footholds) -
len(legs) + 1` stations, named `st0…st{N-1}`. Writes the forward chain
`st<i> → st<i+1>` plus each reverse edge (`declare_transitions`' shape,
`driver_kind` on both, `forward_driver`/`reverse_driver` as `driver_ref`).
**Replaces** the walker's states and transitions, like the two ops it
stands for — but a **re-declare never wipes a stored pose** (the states
upsert keeps `pose` untouched when the new declaration doesn't supply
one).

## Settle a station — `relax_chain(state=)`

`relax_chain` grows one key:

```python
edit(kind='se', id='walk', ops=[{'op': 'relax_chain', 'state': {'w': 'st1'}}])
```

Needs a **saved** design — `put` first, then `edit` the settle (a fresh
`put` can't resolve `state=` against states that don't exist in the store
yet). This turns the settle into a **station settle**: the state's
occupancy is applied to the domain rows for this run only, the walker
joins the bundle as one more rigid body (its axis = the block's local `z`
through its envelope's `z` extent — a flat/degenerate envelope's shorter
extent is floored at 1 nm so it still has one), and each tethered leg
becomes a one-sided loop spring at `(n+1)·c` between the walker's anchor
port and its foot's backbone exit (a free leg gets no spring). **With no
`move=`, the walker moves ALONE** — the track is the fixed frame the legs
pull against, so `layout_chain`'s revisable segments are NOT the default
movers here (unlike a plain settle); `move=` still names segments
explicitly when you want the track to move too. The settled placement
lands in the **state's own pose slot** — never the tree's default pose,
which a station settle never touches — and **no `loop_curve` is written**
(a curve is a fact about the declared route, not about one station).

## Reads that pick up a station

`view='block'|'chain'|'drc'|''|'tree'|'clearance'` all take
`args={'state': {'<walker>': '<state name>'}}`: the state's stored
**pose** applies first (world pose recomposed, so children follow), then
its merged **occupancy** rewrites the domain rows for this read only —
pairing, the chain findings and `view='chain'` all report the station.
`view='block'`'s states table shows `occupancy` and `pose` = `stored
(relaxed)` once a settle has run, `UNRELAXED` when occupancy is declared
but nothing has settled it yet, `—` for a state with no occupancy at all.

## `view='sweep'` over stations

A combination whose state declares occupancy but has no stored pose
reports `chain_state_unrelaxed` (one row per unrelaxed block=state, the
combo counted UNRELAXED in the verdict line) instead of guessing at the
walker's default pose; a combination that IS settled runs
`envelope_overlaps` plus the two chain rules `chain_loop_short`/
`chain_clash` as usual. A walker's own tether is checked as
`chain_loop_short` too (`subject` = `'<strand> tether'`) at the body's
*current* pose (the state's stored pose once `args.state` applied it,
else the block's default) — with half a nucleotide of slack over the raw
`(n+1)·c` reach, since a station settle's spring is one-sided and stops
exactly at reach.

## Not here yet

Later slices of the same item: a transition's `params.guard` (a ratchet
predicate over the *from* state's occupancy), the spectral channel-budget
DRC (`chain_spectral_crosstalk`/`chain_channel_budget`), `make_steps`
(turning declared transitions into a `make` protocol), and
`view='stations'` (cursor-site-vs-track geometry).

## See also

- [[precis-se-chain-help]] — helices, strands, domains, pairing, the plain
  `relax_chain` settle: the vocabulary a walker's legs and footholds are
  built from.
- [[precis-se-help]] — blocks, ports, connects, the `declare_states`/
  `declare_transitions` machinery this skill's ops sit on top of.
