# Patent-evidence parity — residual watch items

All five build phases shipped; behavior lives in the owning docstrings
(`workers/hub_refine.py`, `handlers/_patent_ingest.py`, `_patent_family.py`,
`citation.py`, `taproot/hub.py`, `workers/dream_agent.py`; terminology →
`docs/glossary.md`). Remaining is enablement + watches, no build work:

- **Enable the axis in prod:** `axis:patent_example` is default-OFF like every
  axis service; until `precis service prio '*' axis:patent_example 1`, patent
  chunks stay unclassified and the prophetic caveat never fires. Operator
  step, needs the prod-write key.
- **Watch on first prod use:** priority-claims extraction (`_patent_xml.py`)
  was built from the ST.36 shape without a live-OPS sample; a mismatch
  degrades safely to full ingest, but the first real stub decision deserves
  a glance.
- **Watch axis precision:** the tense-heuristic lives in the small-tier model
  prompt; if `prophetic` precision disappoints, escalate via a
  confident-pattern regex stage or a `role3`-style local model.

test: axis enabled in prod; first classified patent chunk shows a
worked/prophetic tag and a prophetic-grounded evidence edge carries the caveat.
