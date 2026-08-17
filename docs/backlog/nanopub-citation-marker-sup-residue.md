---
status: draft
title: "nanopub gates: citation-marker regex misses <sup>N</sup> superscript residue"
model: sonnet
---

# Citation-marker gate hole: `<sup>N</sup>` residue passes

Found 2026-08-17 while grounding pa4365 for the nanobud campaign. The
stored chunk text renders superscript citation numerals as literal
`<sup>8</sup>` markup (marker-ingest output), e.g. pc550457:

> …a narrow band gap of about 0.12 eV, characterizing a semiconducting
> feature, which is similar to the previous report.`<sup>8</sup>`

The shipped citation-marker check in `nanopub/gates.py::_check_passage`
(the 2026-08-16 regex) catches `[12]`-style brackets, markdown-link
residue, and `(Author, Year)` — but NOT `<sup>N</sup>`. A quote ending
"…similar to the previous report.<sup>8</sup>" (or trimmed just before
the tag, leaving "the previous report." — a citing sentence) mints
clean today.

Fix: extend the regex with an `<sup>\d+(,\s*\d+)*</sup>` alternative
(and consider bare trailing `.<sup>` fragments), plus a test case using
the pc550457 shape. The campaign's offline gate mirror
(session-e26f279b scratchpad `specs4/build.py`) already carries the
extra check — port it.

Consequence today: agents must eyeball for superscript residue
manually; one pa4365 hub (h1) dropped its 0.12 eV number because the
only sentence stating it is citation-bearing in exactly this way.
