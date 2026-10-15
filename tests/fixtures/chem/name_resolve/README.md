# Name-resolver fixtures

Replayed by `tests/test_chem_resolve.py`; no test touches the network.

- `pubchem_*.json`: recorded live from PubChem PUG REST on 2026-10-15
  (`/compound/name/<q>/cids/JSON` and
  `/compound/cid/<cids>/property/SMILES,InChIKey,Title/JSON`).
- `opsin_*.json`: hand-built from OPSIN's documented `ws/<name>.json`
  response. The CIR bodies (one SMILES per line) are inline in the test.
  The dev sandbox could not reach www.ebi.ac.uk or cactus.nci.nih.gov
  when these were written; re-record them when it can.
