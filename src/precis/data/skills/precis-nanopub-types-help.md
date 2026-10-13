---
id: precis-nanopub-types-help
family: evidence
title: precis — report and definition nanopub types (human-set at approve)
summary: report and definition are non-empirical nanopub types that exist only as an artifact_type key on the approve payload; agents cannot create them; gate slugs and the genre/copula rules
answers:
  - what are the report and definition artifact types?
  - can I make a hub a report or definition to get past no-epistemic-mode?
  - why did report-attribution, report-genre, report-source-genre or definition-copula refuse?
applies-to: approve payload artifact_type; precis nanopub approve --payload; /nanopub review form
status: active
tags: [workflow, troubleshooting]
kinds: [finding]
---

# precis-nanopub-types-help — report and definition: two non-empirical types, set only at approve

Some hubs state no finding: "Canatu supplies films to OEMs" (a company
says so) or "negatively curved all-carbon frameworks are called
schwarzites" (a term's meaning). They cannot name a method, so as claims
they fail `no-epistemic-mode` / `no-evidence-verb`, and rewording cannot
honestly fix that. Two types hold them. You cannot create either: **the
type exists only as a key on the approve payload**, which a person
submits (`/nanopub` review form or `precis nanopub approve --payload`).
`put(kind='finding', artifact_type=…)` and `mint_hub` refuse it, and the
reword sweep skips any hub carrying the marker — so a failing claim
cannot be moved into these types by an agent or a batch pass. Do not
propose one as a way past a lint failure: measured on prod, 5 of ~3,600
claim hubs are non-empirical; the other ~2,260 that fail the pair are
empirical claims missing a method token, and the pair exists to catch
those.

Approve payload: add `"artifact_type": "report"` or `"definition"`
(anything else is a `BadInput`). Grounding is still required: a verbatim
quote plus snip from the source, with no `hanging` form. The hearsay,
quote and snip gates apply unchanged. The two epistemic codes are
replaced by:

- `report` — the sentence names an attribution verb (reports / reported
  / states / stated / announces / announced / describes / described, or
  "according to") **and** a source genre from a closed list (conference
  report/paper, company report, annual report, technical report, press
  release, SEC filing, datasheet, product announcement, white paper,
  website): "A 2014 conference report by Canatu states that Canatu
  supplies carbon NanoBud films to major OEMs." The source must agree:
  no grounding passage may sit in a `paper` whose Crossref
  `meta.entry_type` is `journal-article` (a journal article is a finding
  by genre). Datasheet, edgar and patent sources pass; so does a paper
  typed proceedings-article, report, posted-content, other or untyped,
  and a paper whose `meta.journal` names a proceedings-style venue
  (Symposium, Digest, Proceedings, Conference, Workshop, Meeting
  Abstracts) even when Crossref calls it a journal-article (the SID
  Symposium Digest is filed that way); journals that merely carry a
  venue word (Proceedings of the National Academy of Sciences, of the
  IEEE, Digest Journal of …) are on a not-venue list and stay refused.
  The signed artifact is `precis:Report` and carries `prov:wasAttributedTo`
  its source node — it asserts that the source said it, never a finding.
- `definition` — the sentence names a definitional copula (is/are
  called, termed, defined as, known as, named; refers to; denotes).
  Signed as `precis:Definition`.

Gate slugs: `report-attribution`, `report-genre`, `report-source-genre`,
`definition-copula` (plus `schema-lint` for a missing passage). The
`/nanopub` header strip counts report/definition approvals per week; a
count into the dozens means the exemption is being used as a bypass.

## See also

- [[precis-nanopub-help]] — mint pipeline, gates, publish state
- [[precis-taproot-mint-help]] — claim admissibility
