# se protein chain import — PDB/mmCIF → Cα trace → chain path + tube

IDEA (the nucleic-acid chain domain it builds on has shipped —
`precis_se.chain`). `import_trace(block, source=
'structure:<slug>'|text, chain, step='per-residue'|'per-ss-segment')` →
`declare_helix`-analogue with protein motifs in `precis_se/chain/protein.py`
(α-helix 0.15 nm/res, 100°/res, r 0.23 nm; β-strand 0.33 nm/res; coil 0.38 nm
Cα–Cα, Lp ≈ 0.5 nm) + `layout_chain` tube (r 0.5 nm default). Reader =
`precis_chain.pdb.read_trace`; mmCIF fields per
`src/precis_bio/converge.py::parse_atom_site` (extend with `label_asym_id`,
`label_seq_id`). Covers AlphaFold output already converged to `structure`. No
RCSB fetcher (later, via `utils/safe_fetch`). test: 20-residue ideal α-helix
CIF → Cα bend radius matches the 0.23 nm helix formula, 3.6 ± 0.1 residues
per turn, every Cα inside the tube.
