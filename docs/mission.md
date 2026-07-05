# Mission & narrative

> Positioning, not architecture. This is the single source for pitch
> decks, talks, and the project's "why" — copy from here rather than
> re-inventing the prose each time. For how the system is *built*, see
> `docs/design/`; for the "why this way" decisions, see
> `docs/decisions/`. Keep the **Facts** section honest: it goes stale,
> so refresh it with the query at the bottom before quoting numbers.

## Mission

**Précis is an untiring research collaborator.** A partner that never
sleeps and works from real feedback, it takes on the open problems a
field has left unsolved, does the computational work, and writes it up
with real citations.

Two things make it more than another RAG chatbot:

- **Grounded.** No claim stands without a precise citation — a specific
  paragraph in a real publication that supports it. Hallucination is
  designed out, not apologized for.
- **Untiring.** It does not sleep, it takes real feedback, and it
  iterates. Progress is bounded by the work, not by human attention
  spans.

## Narrative (the elevator version)

Because LLMs are prone to hallucination, Précis is built around a RAG
setup that won't let a claim stand without a precise citation, meaning
a specific paragraph in a real publication that supports it. Papers are
ingested automatically when they are available from open-access
sources, or I pull them from the library manually. Citation-graph
exploration keeps discovering new papers, which are ingested in turn,
and we also scour other open-access corpora such as patents. Every
citation the system emits points back to one of these publications. The
corpus currently holds close to 9,000 fully ingested papers, over
10,000 tracked in all counting those still being fetched, broken into
roughly 1.5 million searchable chunks. To make that navigable, each
document gets precomputed keywords and summaries plus a dynamic table of
contents built by clustering those keywords, so the LLM can find very
specific claims token-efficiently instead of reading whole papers. The
agents also get real tools, including a pocket calculator, a symbolic
math solver, and search backends like Perplexity and Wolfram Alpha.
These are not directly citeable, but they can surface further DOIs that
then get ingested and become citeable. Work can also wait, so a task can
park itself until a specific paper is ingested or until the user gives
feedback, then resume automatically. A background "dreaming" process
roams the material under active investigation plus distant points in
embedding space, asking the LLM to connect them through a scientific
lens such as Shannon, Einstein, or Newton. These leaps are not
citeable, but they surface ideas that may inspire the work. A
self-organizing dispatch system runs a team of writer and reviewer
agents with varied skills to carry the work forward. Current work in
progress is to give those agents machine-usable tools, built for LLM
vision rather than a flattened 2D projection that has to be recovered by
image recognition. The agents can see, show, and traverse whole systems
through structured lenses, using token windows the way a person uses
attention but without being bound by human attention spans. With those
tools they can build DFT models and CAD-for-CFD geometry and actually
run them, backed by a full cascade of simulators matched to each order
of magnitude, plus local copies of the open MOF and catalyst databases
with search and access. The goal is to gather the open problems the
field has left unsolved, bring them together, work the computational
part, and write up the result, or take a rough idea and just do the
things. Because the system does not sleep and gets real feedback, it can
iterate untiringly. The tool is at https://github.com/retospect/precis-mcp.

## Pull-quotes (slide-sized)

- "An untiring research collaborator: it never sleeps and works from
  real feedback."
- "No claim without a citation — a specific paragraph in a real paper
  that supports it."
- "Take the field's backlog of unsolved problems and actually finish
  them."
- "Built for LLM vision, not a flattened 2D projection recovered by
  image recognition."

## Facts (refresh before quoting)

As of **2026-07-05** (prod, `precis_prod`):

| Metric | Value |
|--------|------:|
| Fully ingested papers (PDF + chunks) | ~8,885 |
| Papers tracked in all (incl. stubs being fetched) | ~10,500 |
| Searchable body chunks | ~1.5 million |

Refresh with:

```sh
ssh caspar 'psql -h 100.126.127.107 -p 6432 -U agent_rw -d precis_prod -c "
  SELECT
    (SELECT count(*) FROM refs   WHERE kind = '\''paper'\'')                          AS papers_tracked,
    (SELECT count(*) FROM refs   WHERE kind = '\''paper'\'' AND pdf_sha256 IS NOT NULL) AS papers_ingested,
    (SELECT count(*) FROM chunks WHERE ord >= 0)                                     AS body_chunks;"'
```
