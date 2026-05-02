---
id: precis-oracle-help
title: precis — consult an oracle for a perspective shift
status: shipped
tier: 1
floor: any
applies-to: get / search / tag / link (kind='oracle')
last-updated: 2026-05-02
---

# precis-oracle-help — consult an oracle

`oracle` is a **decision-making aid for analysis paralysis and stuck
cognition**, not a fortune-telling surface. The mechanism is the one
de Bono, Eno, and Schmidt identified decades ago: **random stimulation
disrupts entrenched thought patterns**. Lateral thinking in a tool
shape.

The tradition matters less than the act of consultation. What helps
is being forced to read one principle you did not choose and ask
"does this apply?"

## When to use it

- **Analysis paralysis.** Two options look equally good after an
  hour of deliberation. Consult an oracle, notice your emotional
  reaction to the entry it returns, use that to disambiguate.
- **Stuck on a problem.** You've been staring at it long enough that
  you're re-running the same reasoning loop. A random principle from
  a wisdom tradition forces a reframe.
- **Pre-commitment sanity check.** Before shipping a decision, pull
  one entry. If the principle contradicts your choice, take the
  contradiction seriously — it might be telling you something.
- **Warm-up.** Agent doesn't know what the session is about yet; one
  oracle pull is a cheap starting angle.

**Not for**: actual answers. Oracles don't predict; they perturb.

## Available traditions

```python
get(kind='oracle')             # list all traditions
```

Today: `stoic`, `zen`, `talmudic`, `iching`, `engineering`,
`chengyu`, `proverbs-irish`, `proverbs-euro`, `proverbs-buddhist`.
Each tradition is a curated set of principles with provenance.

## Consulting

**Default is random.** Calling `get(kind='oracle', id='<slug>')`
without a selector returns one random entry from that tradition:

```python
get(kind='oracle', id='stoic')
# → one random Stoic principle, ~150 tokens
```

**Catalogue**: `view='index'` or `id='<slug>/index'` returns a
titled catalogue so you can see what's in the tradition:

```python
get(kind='oracle', id='stoic/index')     # 15 titled entries
get(kind='oracle', id='iching/index')    # 64 hexagrams
```

**Deterministic fetch**: `~N` selector returns entry N. Positions
are **1-indexed** — chosen so that I-Ching addresses match the
standard hexagram numbering, and other traditions ride the same
uniform convention. Prev/next/index navigation rides in the
trailer:

```python
get(kind='oracle', id='stoic~4')          # Festina lente specifically
get(kind='oracle', id='iching~49')        # Hexagram 49 — Transformation
```

**Searching**: `search(kind='oracle', q='...', scope='<tradition>')`
narrows to one tradition; omit `scope=` for cross-tradition search.

## Related random-perturbation tools

| Tool | Call | When |
|---|---|---|
| Oracle — curated principles | `get(kind='oracle', id='<tradition>')` | Structural / philosophical guidance |
| Random corpus pick | `get(kind='random')` | "Show me something from the corpus I might have forgotten" |

Oracle > random for deliberation help: the curated entries carry
enough substance to reframe a decision. Random is better for
discovery warm-up; oracles are better for the paralysis case.

**Coin flip / dice** are not on the MCP surface today — `calc` is
sympy-backed and sympy's `random()` / `randint()` aren't wired to
return numeric samples (they return symbolic objects). For binary
or N-way decisions that genuinely want a fair draw, flip an actual
coin, roll a die, or run a one-liner outside the tool surface
(`python -c "import random; print(random.randint(1,6))"`). The
agent doesn't need to own randomness generation for it to be
useful in a workflow.

## Typical flow — analysis paralysis

```python
# You've been weighing two options for an hour.
get(kind='oracle', id='stoic')
# → "The impediment to action advances action." (Meditations V.20)

# Notice: does this land? If yes, the "difficult" option is probably
# the right one, and the deliberation was avoidance. If the entry
# feels irrelevant, pull again from a different tradition.
get(kind='oracle', id='iching')
# → Hexagram 49 — Transformation. "Change must be timely and justified."

# You now have two random perturbations. Your emotional response to
# each is the actual signal. The oracles were the excuse.
```

The wisdom is your reorientation afterwards, not the text on the
page.

## Tagging and linking

Like every slug-addressed kind, oracle entries can be tagged and
cross-linked:

```python
# "This entry helped me decide" — link it to the memory of the decision
put(kind='memory', text='Chose to ship v2 partially.',
    tags=['kind:decision'])
# → memory ref id=88
link(kind='oracle', id='stoic~9', target='memory:88', rel='supports')
```

Open tags (`topic-*`, `project-*`) work freely. Closed axes aren't
accepted on oracle (bodies are curated, not workflow).

## What oracles are NOT

- Not a fortune-telling surface. The handler does not claim to
  predict anything.
- Not a magic 8-ball for shirking responsibility. The oracle is the
  perturbation; the decision is still yours.
- Not editable from the agent. Tradition bodies arrive via the
  corpus seeding pipeline, never from `put()`.

## See also

- `precis-random-help` — random pick from the whole corpus (broader, less curated)
- `precis-overview` — verbs and kinds
- Background: Edward de Bono, *Lateral Thinking* (1967); Brian Eno
  & Peter Schmidt, *Oblique Strategies* (1975). The mechanism is
  "random stimulation" — random input forces cognitive reframing.
