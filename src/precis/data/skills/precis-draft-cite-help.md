---
id: precis-draft-cite-help
title: precis — citing and cross-referencing inside a draft (handles route by what they name)
summary: in draft prose a [fi<id>] finding hub is a citation and every other handle ([dc<id>], [me<id>]) is a link; cite the hub, never its paper or chunk; never fabricate or type a bare DOI/arXiv id; a paper not held is requested and the citing work parked behind the ingest, never faked
answers:
  - how do I cite a paper I don't have yet without faking the reference?
  - which handles count as citations in a draft and which are only links?
  - why was my put/edit refused with an unresolved handle?
  - can I type a DOI or arXiv id into draft prose?
applies-to: put/edit (kind='draft'), get (kind='finding'|'paper')
status: active
tags: [drafting]
kinds: [draft]
---

# precis-draft-cite-help — references in draft prose

How a reference written into a `draft` chunk is read: as a citation, a
link, or a refused fabrication. Authoring the chunk itself is
[[precis-draft-help]]; which hub to cite when none exists is
[[precis-cite-paper-help]].

## References in prose — handles route by what they name

Prose is **markdown**. Copy `[<handle>]` from search/get (never guess);
`[text](<handle>)` adds display words. References route by target:

| write | route | means | renders / exports |
|---|---|---|---|
| `[fi<id>]` finding hub (grounded on `[pc<id>]` paper / `[pk<id>]` patent chunks) | **citation** | this hub's evidence supports the claim | `cites` edge + one bibliography entry per originator paper at export |
| `[dc<id>]` draft chunk, `[me<id>]` memory, any other kind | **link** | provenance / cross-ref | `related-to` backlink; never in the bibliography |
| `[text](<handle>)` / `[text](https://…)` | (either) / web | display text / web link | hyperlink |

Cite the **finding hub** (`[fi41]`), never its paper/chunk: it names exact
grounding chunks (`source_handle='pc234'`). Multiple hubs can support one
sentence (`[fi41][fi92]`). Export resolves hubs to originator papers and
creates `\cite{}` plus one bibliography entry per paper; never type `\cite{}`.
Convert legacy `[pc<id>]`/`[pa<id>]` cites via backfill or
`precis-citation-help`. Links (`[me<id>]`, cross-draft `[dc<id>]`) are
provenance, not citations; intra-draft refs stay document-internal (TOC/`\ref`).

**Rigor.** Read hub evidence (`get(id='fi<id>', view='evidence')`) and its
grounding chunk (`get(id='pc<id>')`); both must directly support the claim.
If weak, soften it or find a better source, preferably primary. Never cite
related-but-non-supporting work or overstate evidence: one study calls for a
tentative claim; replication, reviews or meta-analyses can support stronger
ones. The cite popover shows the chunk verbatim. A bare paper mention exposes
only keywords; find a missing chunk with
`get(kind='paper', id='<slug>~lo..hi', view='toc')`, then narrow the range.

**Backfill raw cites to a living hub cite** via a todo:
`put(kind='todo', text='taproot backfill <slug>',
meta={'executor': 'claude_inproc', 'job_type': 'taproot_backfill',
'params': {'scope': <slug-or-dc>}})` — converts `[pc<id>]`/`[pa<id>]`
cites to `[fi<id>]` claim-hub cites on the cluster worker; poll
`get(kind='job', id='jo<id>')`. See `precis-taproot-backfill-help`.

**Never fabricate handles.** A `put`/`edit` introducing an unresolved
handle-shaped ref (e.g. numeric `[45650]` or typo `[dc…]`) is refused with
`BadInput` and writes nothing. Copy handles from search/get. Deliberate
forward refs use `finding #<slug>`; verbatim reads flag them **⚠ unresolved**
and they never autolink or export. Use `[fi<id>]`, creating the finding first
with `put(kind='finding', …)` if needed.

**Formatting.** `` `code` ``, `$…$`/`$$…$$` math (KaTeX), `<sub>`/`<sup>`
for chemistry/units (`NH<sub>2</sub>`, `g<sup>-1</sup>`); no emphasis
markup (see `precis-draft-help` *Add prose*). Citations/cross-refs render as a
compact superscript, so handles don't clutter the sentence. A chunk
cross-ref uses the target's `dc<id>` handle, never a numeric id like
`[45650]` (refused on write). Math must actually be math: a `$…$` span
with unbalanced `{ }`, or two money-dollars accidentally pairing across
prose (`$10-50 …, versus $200`), is demoted to escaped literal text by
both exporters and trips a `⚠ math that won't render` hint on write —
escape a literal dollar as `\$`.

## Cite by handle, not by arXiv id or DOI

Do not type an arXiv id or DOI into prose: cite `[pa<id>]` / `[pc<id>]`
and the export renders the identifier and link. A raw id never resolves,
joins the cite graph, or follows a preprint to its published version. A
bare id trips a `⚠ bare identifier in prose` hint on write (a hint, not a
refusal; ignore it when a chunk quotes an identifier format on purpose).

## Cite a paper we don't have yet — request it, don't fake it

1. **Re-check the corpus.** `search(kind='paper', q=…)` — may already be
   held under another slug/cite_key.
2. **Find the source, never cite the finder.** Mine bibliographies of
   held papers, or `get(kind="semanticscholar", id="refs:<doi>"|"cites:<doi>"|"<title or topic>")`;
   `get(kind="perplexity-research", q="<question>")` as fallback — convert
   whatever it names to a resolvable id and ingest that, never cite the
   web page itself.
3. **Request it + park the citing work behind the ingest.**

   ```python
   put(kind="paper", doi="10.1038/nature10352")  # idempotent request
   wait = put(
       kind="todo",
       text="[auto] wait for 10.1038/nature10352 ingested+indexed",
       meta={"auto_check": {"type": "paper_ingested", "doi": "10.1038/nature10352",
                             "timeout_at": "<ISO-8601, e.g. +7d>"}},
   )
   link(kind="todo", id="<your citing todo>", target=f"todo:{wait.id}", rel="blocked-by")
   ```

4. **No resolvable id, only a fuzzy claim?** `put(kind='finding',
   text='<claim>', …)`; `finding_chase` resolves it, then cite on a re-tick.
5. **Still nothing?** Soften the claim to match the evidence, or drop it.

Never invent a paper-chunk handle or write `paper:slug` for a paper not
held — cite the in-flight chase `[fi<id>]` until the paper lands and a
hub is grounded on it. See
`precis-stubs-help`, `precis-auto-todo-help`, `precis-paper-help`.


## See also

- [[precis-draft-help]] — author a living document: create, search, edit, review, audit.
- [[precis-cite-paper-help]] — the cite-a-paper router: hub exists / paper held / paper not held.
- [[precis-citation-help]] — citation kind + verifier workflow.
- [[precis-paper-help]] — read, cite, search held papers.
- [[precis-stubs-help]] — request a paper we don't have (acquisition backlog).
- [[precis-finding-help]] — flag a claim / chase an un-ingested DOI.
- [[precis-auto-todo-help]] — wait-on-ingest (`paper_ingested`) leaf pattern.
- [[precis-taproot-help]] — cite a claim hub (living `[fi<id>]`).
- [[precis-taproot-backfill-help]] — backfill `[pc<id>]`/`[pa<id>]` cites to hub cites.
- [[precis-draft-export-help]] — what export does with citations.
