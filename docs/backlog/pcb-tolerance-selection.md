# pcb tolerance-aware selection and BOM compaction

Status: backlog entry (Reto 2026-10-30). Feeds op='part' (shipped
a2c5e2137). Related: pcb-design-intent.md (the declared constraints this
engine checks), precis-part-select-help (the selection flow it extends).

## Decisions (Reto, in-pane 2026-10-30)

1. **Exact corners, not Monte Carlo.** A chain of n parts gets its 2ⁿ
   min/max tolerance permutations enumerated — exact and explainable
   ("worst case at ROC1 max / ROC2 min → ratio +0.6 %"), tractable to
   n≈15, dividers are n=2–3. RSS rides along as the second column
   (1 % resistors are independent; corners bound the tail, RSS the
   typical build). No simulation.
2. **Intent is authored, not re-inferred** — see pcb-design-intent.md;
   chains/templates declare their constraints; topology detection is the
   fallback for undeclared legacy boards.
3. **Consolidation is a SET optimization**, not per-part greedy: choose
   the set of BOM values minimizing (lines, coarseness, cost) subject to
   every position's corner-checked window — e.g. a 2.2 part with a wide
   band and a 3.3 part can collapse to one line when windows overlap,
   each substitution annotated on the component (meta: substituted_from,
   window, corners_ok) so the swap is legible forever.

## The taz-leaf case that motivates it (numbers from the board comments)

U2 divider chain, all ratio-constrained, all comfortably E24-or-coarser:
R21 ROC1 15.8M → E6 15M moves the MPPT ratio only −1.1 % (a leg's ratio
sensitivity is the OTHER leg's fraction, 4.42/20.22 ≈ 0.22); R22 4.32M →
E24 4.3M (VBAT_OV 3.32→3.31 V); R23 5.62M/R25 5.6M → 5.6M; R26/R27 need
the OK-window (2.44/2.85 V) equation checked. The whole 9-value divider
chain compacts to ~5 E24 values, every one stockable — vs today's five
E96 stock-0 parts blocking fab.

## Build sketch

- `precis/pcb/tolerance.py`: value+tolerance model (band from the bound
  part's real class: ±5/2/1/0.5/0.1 %), corner enumerator over declared
  chains (formula registry from pcb-design-intent), RSS, window
  computation per position.
- Consolidation pass: group (type, package, class, band) → intersect
  windows → prefer coarsest E-series → fewest lines → stock/price; emit
  proposals with the annotation payload.
- MCP surface: `view='tolerance'` on kind='pcb' (per-chain corners table,
  green/red vs authored windows); a propose op whose accept path is
  op='part'; a `tolerance` DRC rule family so a later drifted rebind
  (corners now red) is a persistent finding.
- Board script: `spec=` on res/cap/ind calls (or circuit-template params)
  carried through compact.py into the design.

First fixture: the taz-leaf five stock-0 dividers — the hand-done math in
this entry becomes test #1.
