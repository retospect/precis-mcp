# Watching the loop

A **quest** is a striving that keeps working while you are not looking:
it reads, proposes candidates, runs simulations on them, ranks the
results onto a Pareto frontier, writes down what it learned, and goes
around again. This chapter is about seeing what it did.

> Two different things are called "sims" here. The **compute lane**
> inside a quest — candidates turned into structure and pathway
> simulations, ranked into a frontier — is what the dashboard below
> shows. The separate **`precis sim` harness**, which drives external
> Pareto-simulation repositories, is command-line only and has no web
> view at all; see the end of this chapter.

## The dashboard

Open a quest from **Browse → Quests**, or go to `/refs/quest/<id>`.
Reading top to bottom:

**Header** — the striving itself, its status and priority, and a
**momentum** badge: `active`, `warming`, or `stalled`, computed from
how much has actually happened lately (recent logbook entries and
recent activity from the work serving this quest). Hover it for the
counts behind the verdict. If you look at one number on this page, look
at this one — `stalled` on a quest you believe is running means
something is wrong.

**Lineage** — the quests above and below this one. Strivings serve
other strivings; this is where you see the chain.

**Happening now** — the recent-activity callout: what was dispatched
and what came back measured.

**Dossier** — the living synthesis, which is the quest's actual
output. Two parts: a **narrative** it rewrites as understanding
changes, and a **ledger** of everything attempted, as a tree, with
outcomes. The ledger is the honest record — including the things that
did not work, which is usually the more valuable half.

**Logbook** — the append-only trail, newest first. Nothing is ever
edited out of it.

**Frontier — Pareto candidates** — the scatter plot of every candidate
against the objectives being traded off. The marker grammar:

| Marker | Meaning |
|---|---|
| ★ star | on the Pareto frontier |
| ● circle | off it |
| filled blue | confirmed — the simulation is trusted |
| hollow | provisional or awaiting simulation |

**Provisional** matters. A candidate whose simulation threw a warning —
a pathway that didn't converge, a molecule that came unstuck from the
surface — is excluded from the confirmed frontier and can never
graduate, but its measured numbers are still shown, in their own band,
with the reasons named. The loop is never allowed to hide a measurement
from you just because it doesn't trust it.

**Gaps** — the exploration queue: what this quest knows it doesn't
know.

**Retinue** — everything currently in this quest's service, by kind.

**🔍 Spy on last session** — jumps into the full transcript of the most
recent tick. When you want to know *why* it did something rather than
what it did, this is the door.

## What the dashboard does not tell you yet

It shows momentum, but not **cadence**. There is currently no place in
the web UI that says *when the loop last came around*, when it is due
next, whether it is resting and why, or whether a proposal is in flight
holding the next tick back. The loop is deliberately one-proposal-at-a-
time: after it dispatches work it waits for the simulations to land
before ticking again, and it also rests — with a lengthening backoff —
after a tick that found nothing new.

All of that is visible from the command line:

    precis quest status <id>

which reports the recent tick events, the simulation jobs under this
quest's candidates and their states, the candidate rows, a logbook tail,
and what the quest has spent on model calls.

Closing this gap is a known piece of work; until it lands, a quest that
looks quiet on the dashboard is best diagnosed with `quest status`
rather than assumed dead.

## Other command-line views

    precis quest frontier <id>     # the frontier as text
    precis quest dossier <id>      # narrative + ledger
    precis quest gaps <id>         # the exploration queue
    precis quest figure <id>       # static Pareto/profile plots for a paper

`quest figure` is the one to reach for when the frontier needs to go
*into* a document — it renders publication-quality plots and freezes
the data behind them, so the figure in the paper and the numbers on the
dashboard cannot drift apart.

## The external sim harness

`precis sim` is separate machinery: plain command-line, no jobs, no
workers, for driving simulation repositories that live outside precis
and have their own Pareto studies.

    precis sim list                # registered sims
    precis sim ingest <slug>       # pull its prose + CSV outputs into the corpus
    precis sim verify <slug> --dry-run

`verify` is the interesting one. Each sim declares claims it is
uncertain about; verify searches the corpus for support, has a model
judge whether the found source actually settles it, and — when it does —
writes the citation back into the sim's own repository on a branch, mints
the supporting objects, and logs a deed against the quest. `--dry-run`
shows you the diff and writes nothing, which is how you should run it
the first time.

There is no web view for any of this. If you want to see what a
registered sim is doing, the command line is the only surface today.
