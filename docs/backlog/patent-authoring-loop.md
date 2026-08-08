# patent-authoring-loop

## Residuals (from OPEN-ITEMS)

- Validate the loop end-to-end on a real `doc_type=patent` draft: sweep +
  ingest prior art (needs PRECIS_PATENT_RAW_ROOT + EPO OPS on the executor)
  → iterate description → claims with the FTO working_set → scoping decision
  → export (in-text cites, no \printbibliography). Watch the patent-ingest
  gate on the agent host + surname extraction on non-comma bylines.
- Slice 7: visual claim-family tree-eye + interactive /patent/<slug> claims
  view (new render/route surfaces; owner precis_web/routes/).
- Reto wants: run the drafting mostly on local models; prep/check the panel
  screw holder device; find/add the supplemental filing documents so
  EU/US/CN filing gets pushbutton at reasonable cost.
