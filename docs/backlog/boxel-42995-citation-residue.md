# Boxel draft dr42995 — citation residue after waves 4+5 (2026-09-17)

Open items left by the findings-citation conversion of prod draft `dr42995`
(slug `nano-computer`). Full per-leg record: memory
`boxel_42995_taproot_and_assembly.md` §WAVE 4/5. Owner for tool gaps:
`src/precis/draft/` (edit door) and `src/precis/taproot/`.

## Reto decisions (prod data, no code)

- **Approve queue.** Every hub cited from the draft since 2026-09-16 is
  still `candidate`. Hold the review-grounded ones until their primaries
  ingest: fi344137 (nacre; stub pa344160), fi344166, fi345433 (Berger
  1966), fi345435 (kinesin 8 nm; grounded on a reference-list entry),
  fi345436 (DUT-8 254 %; stub pa345389), fi345597, fi345598, fi345618.
- **UiO-66 modulus.** dc1512450 / dc1512524 / dc1512617 use E = 20 GPa;
  held sources say ~40 GPa (fi177485). The value feeds the buoyancy tables
  dc1512517 / dc1512563. Recompute, or label 20 GPa as a deliberate
  conservative assumption.
- **Azobenzene in MIL-53(Al).** pa54525 (pc1826524) states the
  photoisomerisation is blocked inside the host, but dc1513485 / dc1513628
  recommend that pairing as the PoC actuation mechanism.
- **dc1510490** "hours to days" crystallisation vs pc194078 "within a few
  minutes" once in the temperature window.
- **dc1513606** MIL-53(Al) table row was blanked (no Al-specific data);
  relabel to Cr-MIL-53 ~9 % (fi177501) if wanted.
- **dc1513596** now carries an in-prose "no held source pins this
  threshold" remark; cut or keep.
- Numbers deleted for lack of any held source, leaving derived
  figures-of-merit on unheld inputs: CNC E 110–150 GPa (dc1515132 /
  dc1515146 → FOM in dc1515137), CD-MOF ρ 0.76 (dc1515155 → FOM 1.9),
  kinesin 6 pN stall, DUT-8 40 % strain, MIL-53 8–50 % contraction.

## Hand edits the draft edit door cannot make (gr344147: caption text is
outside the tabular body)

- dc1507410: delete `, Cr--CO $\approx 37$~kcal/mol (155~kJ/mol)` from the
  `\mciteboxpC{uddin2001a}` text (pa154 never studied Cr–CO).
- dc1507690: drop `~\citeC{uddin2001a}` from the caption (supports no row).
- dc1510282: `stability\cite{nielsen1991,shakeel2006}.` →
  `stability [fi176833].`

## Corpus defects (fixable by an agent, prod writes)

- refs.title wrong: ref 1768 holds von Neumann 1956 "Probabilistic logics"
  text but is titled as a Lie-algebroid survey; ref 1257 holds Lyons &
  Vanderkulk 1962 (TMR) but is titled as an IASLC lung-cancer paper.
- Ref-level-only grounding on hubs cited by the draft: fi176854, fi176914,
  fi176927, fi177517 (pre-existing), fi345428 (Maekawa; add the pc3878131
  passage edge with `src_chunk_id`).
- fi344166 `scope.material` still reads "ZIF-8-vs-UiO-66" after the
  ZIF-8-only reword.
- dc1515596 RC-demos table: "Conductive polymer network" row has no cite;
  table uses legacy `\cite{}`.
- dc1510282 table: LNA / 2′-OMe RNA rows cited by nothing.

## Still unsourced in the held corpus (acquire or leave qualitative)

Stubs filed: pa343747, pa343814, pa343816, pa344107 (PEDOT), pa344148 +
pa337318 (kinesin), pa344160 (nacre), pa345389 (DUT-8), pa345429 (van
Ginneken), pa345496 (green chemistry), pa345579 (Kapton datasheet).
No target identified: dc1510382 Hamaker ~1e-20 J; dc1510420 / dc1510383 /
dc1510400 / dc1510463 DNA duplex ΔG 17–20 kT; dc1512120 aqueous
diffusion-limited 1e10 M⁻¹s⁻¹; dc1512234 boron 1 mg/L; dc1512231 zeolite
persistence; dc1507690 metal–ligand ΔG/Kd rows; dc1515779 / dc1515784 /
dc1515758 / dc1515781 antifuse-FPGA / EPROM precedents; dc1516092 PEDOT
1e-1 vs 1e-5 S/cm; dc1515173 PLA/Al barrier; dc1514882, dc1514940,
dc1514947, dc1515154; ord 5174 DNA-mismatch 1 nm; ord 5189 binding table;
dc1513605 azobenzene-driven contraction fraction; dc1512569 / dc1512639
COF-300 E, H.

## Tooling gaps surfaced (repo work)

- gr344147: table edit door cannot reach `\caption` / `\mcitebox` /
  footnote text — three hand edits above are the cost.
- `tools search` degrades to lexical silently when the local embedder
  returns 429 (`embedder_service.py` `max_inflight`=4, saturated by
  sibling gate stacks); the caller cannot tell. Surface the mode in the
  result header, or let `scripts/prod-precis` honour a pre-set
  `PRECIS_EMBEDDER_URL` so a quiet cluster embedder can be used.
- Subagents parked on their own background draft edits twice per leg;
  the edit CLI blocks in psycopg under a deep pgbouncer queue while the
  write has already landed (memory `precis_search_hang_no_progress`).
