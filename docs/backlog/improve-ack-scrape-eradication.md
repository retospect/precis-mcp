# Finish eradicating the ack-scrape idiom

The structured path exists (`Response.ref_id`/`reused`, `Hub.sibling`);
the regex-on-ack idiom may survive in `quest/search.py`,
`workers/executors/_context.py`, and the plugin packages
(`precis_bio/protein.py`, `precis_chem/route.py`,
`precis_pathway/handler.py`) — re-verify each site first (some were
cleaned since the 2026-08-02 audit), then convert mechanically.
Mechanical.
