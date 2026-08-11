---
status: ready
title: Record a retraction override in the export's sources appendix
---
The export gate blocks a draft that cites a `retracted` paper and lets the
user through with an explicit `ignore_retractions=1`
(`precis_web/routes/drafts.py::export_docx_route` / `::export_pdf_route`).
Taking that override is supposed to leave a trace *in the exported artifact*
— the decision the gate exists to make visible is exactly the one a reader of
the PDF cannot otherwise see.

Today it only logs server-side (`log.warning`, "drafts: export override"). A
server log is not a trace: the artifact leaves the building looking identical
to one with no retracted cites, and the log is gone by the time anyone reads
the paper.

Wire it into the sources appendix that `precis.export.sources` builds — a line
per overridden cite, naming the paper and its status. The gate already has the
`DraftRetractionReport` in hand at block time
(`precis.export.retraction::draft_retraction_report`), so the data is there;
this is plumbing it into the appendix renderer, which the gate's author did not
own.

Only reachable when a user actually overrides a block, so it is rare — but it
is the audit trail for the one case the whole gate is about.
