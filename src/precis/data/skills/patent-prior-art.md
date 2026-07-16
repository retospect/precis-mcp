---
id: patent-prior-art
title: precis — patent prior-art / IDS (section style)
summary: the prior-art / IDS disclosures — list material references as [pc…] corpus chunks; the IDS is a view rendered over them
status: active
style: patent-prior-art
role: section
archetype: managed
manages: [reference]
---
You are writing the **Prior-Art / IDS Disclosures** section. List the documents material to patentability — external patents, published applications, and non-patent literature (papers). Reference each one directly as a corpus chunk — a **patent** by its patent-chunk handle `[pk…]`, a **paper** by its paper-chunk handle `[pc…]` (`pk` and `pc` are different kinds; a patent cited as `[pc…]` routes to the paper resolver and will not link). Cite only sources you can point at. For each entry give the standard bibliographic handle: for patents, the publication number, inventor/assignee, and issue/publication date; for literature, authors, title, venue, and date — drawn from the cited chunk.

Voice: formal, neutral, factual; make no admission that any listed reference is prior art beyond the duty to disclose, and characterise relevance only sparingly if at all. Order patents before non-patent literature, each group chronologically.

This section is the source of record; the **Information Disclosure Statement (IDS) is a view rendered over these `[pc…]` references**, not a separately maintained list. Keep one disclosure per entry so the view can deduplicate cleanly. Rendering of the formal IDS form is an expansion (ADR 0037 §5).
