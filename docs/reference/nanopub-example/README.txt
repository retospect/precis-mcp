nanopub-example/ — the 2026-08-14 publication wargame, on real prod data.
Design of record: docs/backlog/claim-publication-nanopub-ots.md (the
"Composition model" + "Mint gates" sections came out of this exercise).

Set 1 — hub fi176435 rejiggered (atoms + prose join, no compound):
  claim-1-flexible-mof-anisotropy-400.trig
  claim-2-flexible-mof-softest-direction-below-1gpa.trig
  claim-3-rigid-mof-low-asymmetry.trig
  passage.txt   — the prose paragraph that joins them; documents why no
                  compound was minted (single-paper restatement)
Quotes verbatim from prod chunks of ref 3025 (Ortiz/Boutin/Fuchs/Coudert,
PRL 109, 195502 (2012), doi 10.1103/PhysRevLett.109.195502).

Set 2 — hub fi177584, the compound wargame (2 cross-paper atoms + merge):
  qi-atom-a-mechanical-tuning.trig   (Zhu et al., Angew 2023)
  qi-atom-b-scaling.trig             (Yang et al., PNAS 2022)
  qi-merge-scalable-switching.trig   (the compound — REJECTED, ledger #1)
  qi-hypothesis-scaled-switching.trig (its honest replacement: typed
                  Hypothesis; atoms as motivation not evidence;
                  testableBy names the discriminating experiment)
  viewer.html   — standalone SVG-DAG viewer + detail panes + PDF
                  deep-link prototype (NODES model = future precis_web
                  route payload)

Real: claim sentences, scope values, quotes, snips, DOI/sha256, evidence
roles. PLACEHOLDER: trusty artifact codes (RAPLACEHOLDER_*) and all
signature/pubkey bytes — the mint step (canonicalize→sign→hash) is
unbuilt; writing fake hashes would be worse.

NOTE these examples predate the base-URI decision (2026-08-14): real
mints use https://w3id.org/np/RA… from day one (the base is inside the
hash — it can never be switched later), not the precis.retostamm.com/np/
base shown here. Table-first: bytes live in the append-only proof store,
registry POST at release makes the name resolve.

DEFECT LEDGER — kept deliberately; each defect became a mint gate:
  1. qi-merge label cross-binds: "scalable" (earned by atom B, static
     signatures) applied to "switching" (earned by atom A, different
     molecular family). Every clause maps, the BINDING is unearned —
     no paper shows mechanical QI switching at scale. → commensurability
     gate; this merge does not survive it as a fact-grade compound.
     RESOLVED: re-minted as qi-hypothesis-scaled-switching.trig — the
     transfer stated as a typed Hypothesis (declarative sentence, status
     in the type, motivation not evidence, testableBy experiment).
  2. qi-atom-b precis:quantity ("~42x/~9x") is not contained in its
     sourceQuotes. → quote-containment gate.
  3. qi-merge assertion names atoms by trusty URI; correct split is AIDA
     URIs in the assertion, trusty URIs only in prov:wasDerivedFrom.
  4. dct:created hand-authored. → mint-time-only timestamps.
  5. dct:license CC-BY asserted over content including verbatim
     publisher quotes. → license scoped to our triples.
  6. viewer's chunk=23 fallback link is a local coordinate — fine in a
     UI, never in the published provenance graph (universal anchors
     only: DOI + pdf_sha256 + quote + snip).
  7. File comments (# lines) are OUTSIDE the integrity envelope —
     hashing/signing cover canonicalized quads only; strip at mint.

Prod hygiene found en route (filed:
docs/backlog/pdf-sha256-identifier-hygiene.md): ref 5937 has two
pdf_sha256 rows (dup ingest); ref 42109 has chunks but no sha row.

searchSnip convention: lowercase ASCII tokens, digits and hyphens only —
snips are lexical-search keys, double as PDF deep-link queries
(/pdf/<sha256>?search=<snip>), and must survive any encoding.
