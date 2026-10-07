---
status: draft
title: Verify deployed supplier identity links and live reads
thread: se-machine-design
---

# Verify deployed supplier identity links and live reads

After the coordinator announces the verified deployed SHA, dogfood the identity-only supplier-link and live single-component reads for Digi-Key, Farnell and Mouser using existing components. Confirm stored/list/BOM remain offline, web loads live content only on demand, and component values cite independently ingested manufacturer datasheets. Do not run worker/model jobs or import supplier API facts as part of this follow-up. The implementation and licence rationale live in `precis.supply` and the component handler docstrings.
