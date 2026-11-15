---
id: precis-business-plan-help
family: work
title: precis — business plans as assumption ledgers in graph memory
summary: a business plan is a hub of small linked memory nodes under the Business plans hub — frame, ranked falsifiable assumptions with precommitted pass/pivot/kill lines, evidence from the graph and paid research, unit economics through calc, bottom-up market, comparables, pre-mortem and kill charter, a verdict node that is revised in place, and tests that run as todos
answers:
  - how do I evaluate whether a business idea is viable before building it?
  - where do business plans live and how do I list all of them?
  - how do I start a business plan and what nodes does it need?
  - how do I rank the assumptions and decide what to test first?
  - how do I compute unit economics and size a market bottom-up?
  - how do I build the scenario model as a spreadsheet with formulas?
  - how do I reach a go, pivot or no-go verdict and record it?
  - how do I turn the open tests into work?
applies-to: get/put/edit/link/tag (kind='memory'), search(kind='memory', view='toc' | args={'under': …}), put/get (kind='draft', chunk_kind='table'), get (kind='calc' | 'websearch' | 'perplexity-reasoning' | 'perplexity-research' | 'edgar'), search (kind='patent' | 'news' | 'paper'), put/link (kind='todo')
tags: [workflow]
kinds: [memory, todo, draft, calc, websearch, perplexity-reasoning, perplexity-research, edgar, patent]
status: active
---

# precis-business-plan-help — the plan is a ledger of assumptions, not a document

A business plan here is a **hub of small linked memory nodes**: one node
per claim, each carrying its evidence, under one plan root that is
`part-of` the Business plans hub. The method is portfolio-of-assumptions
testing: list what must be true, rank by risk, write pass / pivot / kill
lines before testing, test the riskiest cheaply, spend more only as
evidence climbs from opinions to behaviour to money to repeated money.
A deck or PDF is an export of the graph, never the primary record.

Two rules carry through every section:

- **One claim, one owner.** A fact about the venture lives in one node;
  other nodes mention its handle. A number appears with its source handle
  or the word `assumed`.
- **Thresholds before results.** Pass, pivot and kill lines are written
  into the assumption node before the test runs and change only on new
  evidence that is recorded before the result is seen.

The hub shape (`section:index` roots, one `part-of` parent, bodies
under 4000 characters) is the memory graph's own:
[[precis-memory-help]]. The spec graph uses the same shape for software
specifications: [[precis-spec-help]].

## Find the Business plans hub and list every plan
## Where do business plans live?
## See all business plans at once

Plan roots are defined by their tag, not their address. The forest view
lists every plan root with keywords and member counts; the hub's own
outline shows the whole tree.

```python
search(kind="memory", tags=["bizplan:*"], view="toc")           # every plan root
search(kind="memory", tags=["section:index"], q="business plans")  # the hub itself
get(kind="memory", id="me<hub>", view="toc", args={"depth": 2})    # hub → plans → sections
get(kind="memory", id="me<root>", view="toc")                      # one plan's outline
search(kind="memory", q="payment fees", args={"under": "me<root>"})  # inside one plan
```

Read a plan root first: its body carries the idea, the objective, the
constraints, the current `Verdict:` line and the plan todo. Then read
sections in the outline's order; `extent='fisheye+1hop'` on an
assumption node shows its evidence ring.

## Start a new business plan
## Frame an idea before researching it

Search first — `search(kind="memory", tags=["bizplan:*"], view="toc")`
— so a second plan for the same idea edits the first. Then mint the
root as `part-of` the hub:

```python
put(kind="memory",
    title="Business plan: <slug>: <purpose in at most eight words>",
    text="<the idea in one paragraph: who, what problem, what offer, who pays whom>\n\n"
         "Objective: <lifestyle | microbusiness | venture-scale>, <founder's required outcome>.\n"
         "Constraints: learning budget <money> and <weeks>; no-gos <legal, ethical, personal>.\n"
         "Verdict: untested.\n"
         "Plan todo: td<N> (tests run there, results land here).",
    link="memory:me<hub>", rel="part-of",
    tags=["section:index", "bizplan:<slug>"],
    meta={"hook": "<slug> plan root"})
```

The objective decides every later gate: a microbusiness passes on
covered fixed costs and a living, a venture-scale plan needs a path to a
very large outcome. Write it down before research starts or the
evidence will be read to fit whichever is convenient.

Then add the load-bearing canvas claims as `section:reference` children
— customer and problem, existing alternatives, value proposition,
channel, revenue model, cost structure, unfair advantage — one node
each, title the claim in at most twelve words, `meta={"hook": "<slug>
canvas <box>", "order": n}`. Boxes that are not load-bearing stay a
sentence in the root.

## Build the assumption ledger
## Rank the risks and pick what to test first

Every material canvas claim becomes one assumption node, `part-of` the
root, hook `<slug> assumption <n>`:

```python
put(kind="memory",
    title="<the assumption as a claim, at most twelve words>",
    text="Type: desirability | viability | feasibility | legal | team.\n"
         "Falsifiable: at least <X> of <population Y> do <behaviour Z> by <date T> under <conditions C>.\n"
         "Risk: importance <1-5> × evidence weakness <1-5> × irreversibility <1-3> = <score>.\n"
         "Cheapest credible test: <what, with whom, costing what>.\n"
         "Pass: <line>. Pivot: <line>. Kill: <line>.\n"
         "Budget: <money>, deadline <date>. Owner: <who>.\n"
         "Evidence: <handles, or 'none yet'>.",
    link="memory:me<root>", rel="part-of",
    tags=["section:reference"],
    meta={"hook": "<slug> assumption <n>", "order": <n>})
```

Test the highest risk score first; at equal risk, the cheaper and faster
test. The usual order, to be reordered by the idea: a specific reachable
segment has a frequent costly problem → that segment changes behaviour
and pays enough → the promise can be delivered → users return → they can
be acquired repeatedly below allowable cost → contribution, lifetime
value and payback work → the reachable market supports the objective →
legal, rights and dependency risks are manageable → the team can commit →
the wedge expands and defends. Rights-heavy, regulated, deep-tech or
hardware ideas move legal or feasibility to the top: a hard constraint
there kills the plan regardless of demand.

Evidence ranks: opinions and future-intent surveys generate hypotheses
only; recent behaviour and documented spending support the problem;
reputation or time risked supports urgency; deposits, preorders and paid
pilots support willingness to pay; repeated independent payment,
retained cohorts and referrals at full price support a business.
Friends, investors and people the founders recruited are not
independent demand.

## Research an assumption
## Fill the ledger with evidence instead of opinion

The graph first: `search(q="<topic>")` across kinds finds earlier
research caches, memories, findings and papers already paid for. Then
the paid tiers, by question shape ([[precis-perplexity-help]]): one fact
→ `websearch`; a comparison or legal position → `perplexity-reasoning`;
a landscape (comparables, regulation, market data, payment economics)
→ `perplexity-research`, at most one or two per plan, with
`args={"no_sources": True}` to keep the return readable.

```python
get(kind="websearch", q="Stripe standard US card fee 2026")
get(kind="perplexity-reasoning", q="Is indexing and linking to artist-hosted audio a public performance in the EU and US?")
get(kind="perplexity-research", q="<landscape question naming the comparables, regulation and data you need>", args={"no_sources": True})
```

Distil, never paste: the assumption node gets the two or three facts
that move its score, each with the cache handle (`[websearch:<id>]`,
`[perplexity-research:<slug>]`) or the source page `[web:<id>]`. The
cached report stays readable by its slug.

Other sources by question: a public comparable's real economics →
`get(kind="edgar", id="ticker:<x>")` ([[precis-edgar-help]]); freedom to
operate → `search(kind="patent", q="<mechanism>")`
([[precis-patent-search-help]]); timing and sentiment →
`search(kind="news", q="<topic>")`; an academic claim the plan rests on
(a recommendation method, a behavioural effect) → `search(kind="paper",
q="<claim>")`, cited through a finding hub ([[precis-finding-help]]).

Customer interviews are evidence only when recorded as facts. Ask about
their past, not your idea; compliments and "I would pay" are not data.
One memory node per interview, `part-of` the assumption it tests, tagged
`evidence:interview`, with this body so facts stay apart from reading:

```text
Who: <role; recruited how; independent of the founders: yes|no>
Last time: <the specific recent occurrence>
Cost: <time, money, reputation it cost them>
Tried: <workarounds and why each failed>
Spent: <what they already pay for this>
Decides and pays: <who>
Verbatim: "<their words>"
Interpretation: <ours, separately>
```

Count independent interviews with `search(kind="memory",
tags=["evidence:interview"], args={"under": "me<root>"})`; only
`Spent:` and `Cost:` lines reach the moderate evidence rung, a
`Verbatim:` compliment reaches none.

## Compute unit economics
## Check whether one customer pays for itself
## Build the scenario model as a spreadsheet

The scenario model is a **formula table** in the plan's draft: inputs
are plain columns, outputs are formula columns that recompute on every
write, one row per scenario ([[precis-table-help]],
[[precis-draft-rich-content-help]]). Mint the draft once, owned by the
plan todo, then add the table:

```python
put(kind="draft", id="<slug>-plan", title="<slug> — business plan model",
    args={"project": <plan todo id>})
put(kind="draft", id="<slug>-plan",
    args={"chunk_kind": "table",
          "caption": "Scenario model, USD per month; inputs carry their source in the economics node",
          "columns": [{"name": "scenario", "type": "text"},
                      {"name": "customers"}, {"name": "conv"}, {"name": "price"},
                      {"name": "fee_pct"}, {"name": "fee_fixed"}, {"name": "fixed_cost"},
                      {"name": "net_per_sale", "formula": "price - (price*fee_pct + fee_fixed)"},
                      {"name": "contribution", "formula": "customers*conv*net_per_sale"},
                      {"name": "gap", "formula": "contribution - fixed_cost"}],
          "rows": [["downside", 100000, 0.005, 5, 0.029, 0.30, 8000, None, None, None],
                   ["base", 100000, 0.01, 7, 0.029, 0.30, 8000, None, None, None],
                   ["upside", 100000, 0.02, 10, 0.029, 0.30, 8000, None, None, None]],
          "footer": {"gap": "max"}, "at": {"last": True}})
get(kind="draft", id="dc<table>", view="toon", args={"cols": ["scenario", "contribution", "gap"]})
```

Rows span every column, formula cells included (`None`); change an
input with `patch=` and the outputs follow. Formulas reference column
names and take `+ - * / **`, comparisons, `if_(cond, a, b)` and
`abs round min max sum avg sqrt log exp`. Add a column per revenue
line (commission, membership, tools) and a `total` formula over them;
add a row per objective when fixed cost differs (microbusiness versus
staffed). `calc` ([[precis-calc-help]]) stays for one-off checks and
unit conversions:

```python
get(kind="calc", q="7*0.05 - (7*0.029 + 0.30)")   # platform cut minus card fee on a 7 USD tip
get(kind="calc", q="(45*0.8)/(1-0.95) / 120")     # LTV/CAC: margin per period / churn, over CAC
```

Formulas: contribution per order = revenue − variable cost of goods −
fulfilment − payment fees − usage infrastructure − variable support −
refunds; fully loaded acquisition cost = all sales, marketing, tooling
and onboarding spend ÷ new customers; lifetime value = contribution per
period ÷ churn per period, from real cohorts as soon as any exist.
Default gates, to be replaced by the plan's own: contribution positive
now; conservative lifetime value at least three times acquisition cost;
payback inside the cash runway. Record the one lever whose change flips
the gate; a plan that passes only when every lever improves at once has
not passed.

The economics node is `section:reference`, hook `<slug> unit
economics`; it lists each input with its source handle or `assumed`,
names the table chunk `dc<id>`, and states the flip lever. The numbers
live in the table, the argument lives in the node.

## Size the market bottom-up
## Decide if the reachable market covers the objective

Count buying units the team can name and reach, multiply by the tested
annual contribution per unit, filter by what the channel can serve:

```python
get(kind="calc", q="2500 * 0.1 * 180")   # reachable units × obtainable share × annual contribution
```

The gate is `SOM contribution ≥ fixed costs + required return`, where
the required return is the root's objective. A share of a large top-down
total is not evidence; a small beachhead is not a small company if the
expansion path is named. Record TAM, SAM and SOM in contribution
dollars with their counting source in one node, hook `<slug> market`.

## Compare against prior art and comparables

One node per comparable, `part-of` the root (a sub-hub when more than
eight): what it is, its model, its outcome with the date you checked,
and the one lesson the plan inherits. Search the graph first —
`search(kind="memory", q="<company>")` — so a comparable already
surveyed by another plan is mentioned by handle, not restated. The set
must include the status quo and the indirect alternative the customer
uses today, not only lookalike startups.

## Run the pre-mortem and write the kill charter

State the plan in one line, declare it eighteen months later and failed,
and have each founder or reviewer list causes independently before
sharing. Rank causes by probability, severity and evidence weakness;
each top cause becomes either an assumption node or a tripwire in the
kill charter, hook `<slug> kill charter`:

```text
If <metric or event> crosses <threshold> by <date>, <owner> stops or pivots <what>.
```

Hard kill lines cover demand (commitments after two offers), price (no
buyer at the contribution-positive price), retention, feasibility,
regulation, unit economics, cash and team. A tripwire with a date is also
a todo tagged `due:<YYYY-MM-DD>` under the plan todo. Criteria change
only for genuinely new evidence, noted before the result is known.

## Reach a verdict
## Decide go, pivot, no-go or inconclusive

The verdict node is `section:reference`, hook `<slug> verdict`, and the
root's `Verdict:` line is edited in the same session:

- **GO** — every existential assumption has behavioural evidence,
  payment and repeat behaviour support demand, feasibility and legal
  gates pass, contribution is positive, conservative economics and
  reachable market meet the objective. Release the next budget tranche
  only, with a named milestone.
- **PIVOT** — the problem is validated but one load-bearing element
  (segment, buyer, offer, channel, price, revenue model) failed and an
  adjacent hypothesis is cheap to test. Change that one element, update
  the canvas nodes, rerun the failed gate.
- **NO-GO** — a hard legal, safety or physical constraint fails, demand
  does not convert after two disciplined offers, the sustainable price is
  below cost, the market cannot support the objective, or the learning
  budget is spent.
- **INCONCLUSIVE** — wrong participants, too little exposure, weak
  evidence. Never a pass: redesign once with a fixed extra budget, then
  treat a second inconclusive as a fail for funding.

The body names the evidence handles that carry the verdict, what would
change it, and the next tranche. Revise with `edit` — the audit trail
keeps the history; a new verdict node per revision splits the owner.

## Turn tests into work

Tests and tripwires are todos, never memory nodes; results land back on
the assumption node by `edit`. One plan todo per plan, linked `plan-of`
the root; one child per test, sequenced with `blocked-by`
([[precis-todo-tree-help]]):

```python
put(kind="todo", text="<slug> — business plan", tags=["bizplan:<slug>"], meta={"llm_tier": None})
link(kind="todo", id=<plan todo>, rel="plan-of", target="memory:me<root>")
put(kind="todo", text="Test: <assumption n> — <the cheapest credible test>",
    parent_id=<plan todo>, tags=["bizplan:<slug>"], meta={"llm_tier": None})
link(kind="todo", id=<test B>, rel="blocked-by", target="todo:<test A>")
```

When a test closes: `edit` the assumption node's `Evidence:` and score,
re-rank, and revisit the verdict if a gate moved.

## Export the plan as a document or deck

A reader outside the graph gets a `draft` ([[precis-draft-help]]) whose
sections follow the investor order — purpose in one sentence, problem
and current alternatives, solution, why now, market, competition,
business model, team, financial evidence, vision — each paragraph
derived from the nodes and naming their handles; a deck is a `pres`
from the same nodes. Regenerate from the graph after a verdict change;
never edit the export into disagreement with the nodes.

## Lint a plan before trusting its verdict

- Every assumption node has pass, pivot and kill lines dated before its
  evidence.
- Every number has a handle or `assumed`; no number next to a cite that
  the cited text does not contain.
- Numbers follow the notation canon: an en dash for a range (`60–90%`),
  no digit grouping (`10000`, not `10,000`); the write audit flags both.
- The verdict rests on at least one behavioural or monetary evidence
  handle, not interviews alone.
- Every kill-charter tripwire has an owner and a date, and a `due:` todo.
- Every comparable has an outcome with a checked date.
- Each node has one `part-of` parent, body under 4000 characters, at
  most three typed evidence edges; cross-references are handle mentions.
- The root's `Verdict:` line matches the verdict node.
