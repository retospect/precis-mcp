---
id: precis-review-paragraph-flow
title: precis — one-pass paragraph-flow review
summary: Every paragraph must have a topic sentence, a developed body, and a transition; check each one and mint findings on offenders
applies-to: get (kind='tex'), put (kind='finding')
status: active
---

# precis-review-paragraph-flow — does each paragraph stand on its own?

One review pass. One concern: paragraph-internal structure and
inter-paragraph flow. The reader should be able to read the first
sentence of any paragraph and know its claim; the body should
develop only that claim; the last sentence should set up what
comes next.

When this discipline breaks, the symptoms are:

- A paragraph that buries its point in sentence 3 or 4 (no topic
  sentence).
- A paragraph that does two unrelated things (no single claim).
- A paragraph that ends abruptly without setting up the next one
  (broken flow).
- A paragraph whose last sentence contradicts the next one's
  first (broken flow, harder failure).

## The procedure

Walk the manuscript paragraph by paragraph. Easiest path:

```python
# Get the toc to find every section, then drill into each.
get(kind='tex', id='main.tex', view='toc')

# For each section file, fetch it block by block:
for handle in section_handles:
    block = get(kind='tex', id=handle)
    # Block bodies in tex are paragraphs (see precis-tex-help block
    # grammar — blank line is the boundary). Equation environments
    # and figure floats are paragraphs too; skip them — flow doesn't
    # apply.
```

For each prose paragraph, check four things:

### 1. Topic sentence

Read sentence 1. Ask: "if I read only this sentence, do I know what
this paragraph will argue?" If yes — pass. If no, the topic sentence
is missing or buried. Find where the actual claim lives (often
sentence 3 or 4) and mint a finding asking the writer to lift it
to the lead.

### 2. Single claim, body develops it

Walk sentences 2 through N-1. Each should support, qualify, give
evidence for, or extend the topic sentence's claim. If a sentence
introduces a new claim unrelated to the topic, the paragraph is
doing two things — finding: paragraph splits.

### 3. Transition to next paragraph

Read the last sentence. Ask: "does this set up the next
paragraph?" The transition can be explicit ("This raises the
question of …", "The same reasoning extends to …") or implicit
(end-of-paragraph claim is the start-of-next-paragraph subject).
A flat full-stop on a stand-alone claim with no link to the next
paragraph's subject is a broken transition — finding.

### 4. Cross-paragraph continuity

Read sentence N of paragraph P and sentence 1 of paragraph P+1
back-to-back. The reader should feel a forward step, not a topic
jump. If P+1 starts a wholly new subject without a section break,
that's a missing subhead — finding.

## Output: one finding per paragraph

Mint `kind='finding'` refs against the manuscript ref. Body shape:

```python
put(kind='finding',
    text='''Paragraph-flow finding in chapters--intro~motivation block 4:

Sentence 1: "Carbon nanotubes have been studied since the 1990s."
Sentence 3 carries the actual claim: "Their ballistic transport at
room temperature is what makes them candidate transistors."

Severity: MODERATE — the topic sentence is generic background;
the paragraph's actual point is buried mid-paragraph. Lift sentence
3 to the lead and re-paragraph the historical context as a separate
"Background" paragraph or trim it.''',
    rel='paragraph-flow-finding')
```

Severity guide:

- **SUBSTANTIVE** — paragraph has no claim at all, OR two unrelated
  claims, OR opens by contradicting the prior paragraph without
  signposting.
- **MODERATE** — claim buried mid-paragraph, weak transition, lazy
  full-stop ending.
- **NITPICK** — sentence-level rhythm. Don't bother; the writer
  can self-edit those.

## Skipped block kinds

- `\begin{equation}` / `\begin{align}` / `\begin{figure}` /
  `\begin{table}` — math and floats are not paragraphs.
- Section headings — these get reviewed in
  precis-review-section-structure, not here.
- Bullet lists — flow doesn't apply per-bullet; check the
  introducing sentence belongs.

## Anti-patterns

- Reading sentence-by-sentence for style. This pass is about
  structure, not prose.
- Marking every paragraph that opens with a transition word as
  "good flow". Form ≠ function. Read the actual claims.
- Bundling all paragraph findings into a section-level summary.
  One finding per paragraph so each can be resolved independently.

## See also

```python
get(kind='skill', id='precis-review-section-structure')   # section-level structure
get(kind='skill', id='precis-tex-help')                   # block grammar
get(kind='skill', id='precis-common-reviewer')            # shared reviewer discipline
get(kind='skill', id='precis-finding-help')               # finding shape
```
