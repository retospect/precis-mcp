"""se's nucleic-acid domain — DNA/RNA design in the block tree.

The binding between the chemistry-free :mod:`precis_chain` geometry kernel
and se's six-level IR. The pure ops that write these records live in
:mod:`precis_se.ops`; the two handler-level ones are
:mod:`precis_se.chain.relax` (``relax_chain``) and
:mod:`precis_se.chain.fold` (``fold_layout``). Every module here but
:mod:`precis_se.chain.relax` and :mod:`precis_se.chain.findings` is
store-free — those two are the ones that read a ``material``
persistence-length row, which is exactly why the op and the finding that
uses it are handler-level. ``fold_layout`` is store-free too and
handler-level for the other two reasons: an optional dependency
(ViennaRNA, the ``[chain]`` extra) and O(n³) compute.

**The decomposition** (scadnano's, chosen 2026-09-27): a **helix** carries
the geometry — a centre line, a motif, per-unit frames, a swept tube; a
**strand** carries the route — an ordered list of **domains**, each a
stretch ``[start, end)`` of one helix traversed forward or back; a **loop**
is the single-stranded gap between two consecutive domains, pinned at two
backbone exits with its nucleotide count free. **Pairing is derived**, from
co-occupancy of a helix offset, never declared
(:mod:`precis_se.chain.pairing`). Everything origami calls a feature is a
consequence: a crossover is a 0/1-nt loop between adjacent helices, a
toehold is a single-occupancy domain, a hairpin is one strand with two
antiparallel domains on one helix and a loop between them.

**Where each thing lives.**

- :mod:`precis_se.chain.nucleic` — the numbers, each with its source: the
  B-DNA/A-RNA motifs, the honeycomb/square lattices, ssDNA contour,
  persistence lengths, inter-helix spacing, the 12 Leontis–Westhof pair
  families. The kernel deliberately has none of these.
- :mod:`precis_se.chain.vocab` — write-time shape rules for
  ``se_blocks.chain`` and a ``kind='domain'`` topology row
  (:class:`~precis_se.chain.vocab.DomainSpec`), and the one units boundary
  (metres in, metres stored).
- :mod:`precis_se.chain.layout` — a stored record → kernel arrays: the
  centre-line path, per-unit origins/frames, the segment tiling
  ``layout_chain`` materialises, the capsules the clash check runs on.
- :mod:`precis_se.chain.pairing` — ``derive_pairing``, O(total domain
  length).
- :mod:`precis_se.chain.occupancy` — a walker state's foothold-occupancy
  map (``{"<strand>.<ord>": "<helix>@<offset>" | None}``): parsing/vetting
  at write time, and the transient domain-row rewrite every state-aware
  read and the station settle apply it through.
- :mod:`precis_se.chain.drc` — the pure ``chain_*`` findings, called once
  from :func:`precis_se.drc.drc`.
- :mod:`precis_se.chain.relax` — the ``relax_chain`` op: the mechanical
  settle over the ``layout_chain`` segments, the pose write-back, and the
  ``meta.loop_curve`` seam. Reads the store (the Lp row).
- :mod:`precis_se.chain.fold` — the ``fold_layout`` op (a ViennaRNA MFE
  dot-bracket as helix/strand/domain records, with a NOMINAL placement) and
  the fold findings. The one lazy ``import RNA`` in the tree lives here, so
  a venv without the ``[chain]`` extra still boots and still renders
  ``view='drc'``.
- :mod:`precis_se.chain.findings` — the handler-side findings, appended (and,
  for ``chain_floppy``, **substituted**) by
  :func:`precis_se.handler._render_drc`.

**Two seams this slice owes the follow-up items**, stated here because
they are contracts rather than conveniences:

1. Every ``layout_chain`` child ``<helix>.s<k>`` carries its inclusive
   ``[start, end]`` unit range in its own ``chain`` record, and one
   helix's children **tile its unit range exactly** — so
   ``se-nucleic-realize-export``'s realizer can find the segment covering
   any offset by comparison alone
   (:func:`precis_se.chain.layout.segment_ranges`).
2. :func:`precis_se.chain.pairing.derive_pairing` and (in the handler)
   ``relax_chain`` take a ``state=`` kwarg from day one, a no-op when
   ``None``, so ``se-walker-light-protocol`` never has to change either
   signature.

**Atoms and interop** (the third slice, 2026-09-30): ``realize_chain``
(:func:`precis_se.atomic.generate.prepare_realize_chain`) places the
Arnott B-DNA fibre templates (:mod:`precis_se.chain.atoms`) in one
segment's worth of unit frames — plus its placed loops, on request — and
binds the minted ``structure`` to that **segment child**, so
``envelope_fit`` holds the atoms against the segment's own capsule.
``view='export'`` (:mod:`precis_se.chain.export`) writes scadnano,
caDNAno (lattice-only), oxDNA and PDB; ``structure`` ``view='pdb'`` writes
one realized region. Not built: A-RNA templates (an RNA helix is refused,
not approximated), import of any of those formats, H-bonds as bonds.

**Walker states** (``se-walker-light-protocol`` slice A, 2026-09-30): a
walker is a plain block with tethered legs (``declare_strand``'s
``anchor=``/``tether_nt=``); a **station** is a declared state whose
``occupancy`` (:mod:`precis_se.chain.occupancy`) says which foothold each
leg's foot domain sits on. ``relax_chain(state=...)`` settles the walker as
one more rigid body in the bundle and stores the result in the state's own
pose slot (:func:`precis.design.states.set_state_pose`) rather than the
tree's default pose; a read applies the stored pose then the occupancy
(:func:`precis_se.handler._apply_state_arg`). ``declare_stations`` sugars
the hand-over-hand gait into states + transitions in one call. Full agent
docs: the ``precis-se-chain-help`` skill's "walker" section.

**Provenance.** This domain shipped in two slices over 2026-09-27..29 and its
``docs/backlog/`` item is gone, delete-on-ship. What that item carried now
lives where the code it justifies is: every **number** with its primary
source and the arithmetic that cross-checks it in
:mod:`precis_se.chain.nucleic` (the backbone azimuth delta, the groove-width
convention, the ``chain_loop_short`` frustration budget — each on the
constant it belongs to); the **per-neighbour register table**, which offsets
admit a 0-nt crossover on each lattice and why the two strand directions sit
half a turn apart, in the ``precis-se-chain-help`` skill, since that is the
answer an agent asks for; the **build narrative** in ``git log``. Cite this
docstring, not the deleted item.

One thing the item recorded is open and unowned, restated here because it is
a *negative* result nobody should re-derive: the four-helix square-lattice
ribbon fixture (``tests/test_se_chain_drc.py``, ``test_se_chain_ops.py``) was
built alongside a claim that a radiating four-arm junction *cannot* have four
register-correct crossovers. That claim is **false as argued** — its
``3*(k - k0) == 16 (mod 32)`` does have solutions, 3 being invertible mod 32 —
and the joint system over both crossover pairs was never written out. Treat
four-arm register-correctness as an open question, not a constraint.
"""

from __future__ import annotations

from precis_se.chain.pairing import Pairing, derive_pairing
from precis_se.chain.vocab import ChainError, DomainSpec

__all__ = ["ChainError", "DomainSpec", "Pairing", "derive_pairing"]
