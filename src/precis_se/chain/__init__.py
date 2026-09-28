"""se's nucleic-acid domain — DNA/RNA design in the block tree
(docs/backlog/se-nucleic-acid.md, slice 1).

The binding between the chemistry-free :mod:`precis_chain` geometry kernel
and se's six-level IR. Nothing in this subpackage touches the store: the
ops that write these records live in :mod:`precis_se.ops`, the store-aware
ones (``relax_chain``, ``fold_layout``) in the handler, and this package is
the vocabulary, the numbers and the derivations in between.

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
- :mod:`precis_se.chain.drc` — the pure ``chain_*`` findings, called once
  from :func:`precis_se.drc.drc`.

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

**Not in this slice** (and deliberately not stubbed): the handler-level
``relax_chain``/``fold_layout``, the ``[chain]`` ViennaRNA extra and the
fold findings, the ``material``-row ``chain_floppy`` re-emission, the
``meta.loop_curve`` seam ``relax_chain`` writes, and everything in
``se-nucleic-realize-export``.
"""

from __future__ import annotations

from precis_se.chain.pairing import Pairing, derive_pairing
from precis_se.chain.vocab import ChainError, DomainSpec

__all__ = ["ChainError", "DomainSpec", "Pairing", "derive_pairing"]
