# Publishing claims

A **nanopublication** is one sentence, its evidence, and a signature —
small enough that a stranger can check it without trusting you or your
server. precis mints them from the claim graph it has already built out
of the papers you read.

The claim graph stays authoritative. A nanopub is the *published
identity and wire format* of one claim in it: a content-addressed
artifact, signed with a key, stamped into a public timestamp chain, and
eventually posted to a registry. Once posted it is public forever, so
the path there is deliberately full of doors.

## Where

**`/nanopub`** is the workbench. Three panes with draggable dividers:
the claim forest on the left (compounds nest the atoms they are built
from; evidence hangs off as leaves), the review pane in the middle, the
source paper on the right.

Two things are pinned above the tree because they must not rot
invisibly: the **disputed strip**, sorted by how long the dispute has
been open, and the **timestamp batch status** with its stuck-pending
alert.

Add `?draft=dr<id>` to scope the whole forest to the claims one draft
cites — the "have I reviewed everything this paper leans on?" view.

An individual claim opens at **`/claim/fi<id>`**: the claim, its
evidence DAG, the publish row, and one action box that offers exactly
the action its current state allows.

## The ladder

    candidate → reviewed → signed → anchored → published

with `rejected` available off *reviewed*, and `superseded` / `retracted`
off *published*.

Each rung freezes more:

| Rung | What freezes |
|---|---|
| **reviewed** | the exact sentence and its grounding |
| **signed** / **anchored** | the artifact bytes |
| **published** | everything, publicly, permanently |

Reword a claim after approving it and you get a new claim identity.
Re-sign the same wording and only the artifact hash changes.

## The workflow

**1. Review the evidence.** Read the claim against its sources in the
paper pane. Add an evidence edge the system missed; remove one that
doesn't hold.

**2. Clear the withheld edges.** An inbound evidence edge that has
neither been machine-verified nor signed off by a person **blocks
publication**. Each one gets a per-edge sign-off button. There is no
mute button and no bulk clear — that is the point.

**3. Approve.** This freezes the sentence and its grounding. The form
arrives prefilled with a quote and a locator candidate for each
grounding passage that already passes the mechanical gates, so the
common case is read-and-confirm. **Reopen** backs this out if you
change your mind.

**4. Sign.** The button signs for real, with the human attesting key.
That key loads only through an explicitly interactive door — no worker,
no scheduled job, no agent can reach it. That restriction is the entire
meaning of the word "signed" here: it certifies that a person looked.

**5. Anchor.** Automatic, nothing to do. Signed artifacts are batched
nightly into a Merkle tree, one timestamp per batch, and upgraded to a
full proof when the calendar confirms.

**6. Publish.** **Not a web button.** The registry POST is the one true
point of no return, so it lives only on the command line, one claim at a
time — there is no publish-everything:

    precis nanopub publish fi<id> --live

and is triple-gated — a person must run it interactively, `--live` must
be passed (otherwise it is a dry run), and preflight must come back with
zero blocking issues. Publication additionally requires an *attesting*
entry in the trust allowlist: a bot signature alone publishes nothing,
and an empty allowlist means nothing is publishable at all.

## Gates you will meet

Mechanical checks run before a claim can be minted, and they explain
themselves when they fire. The common ones: the claim contradicts
another live claim; the evidence is second-hand rather than the primary
source (a related-work section, a passage that itself cites `[12]`, or a
source whose full text isn't in the corpus); the quote isn't verbatim in
the stored text; the locator isn't unique within the paper; a claim
carries no quote, or a hypothesis carries one; a quantity has no stated
bound; the source PDF is a duplicate. Compound claims cannot publish
before the atoms they are built from.

A **hanging claim** — one with no live source in the corpus — can be
minted and read but never published.

## Reading an artifact

`/np/<code>` serves the exact frozen bytes as `application/trig`. Nothing
is re-serialised on the way out; what you download is what was signed.

## Where things stand

The machinery is complete and nothing has been posted to a public
registry yet. Keys exist; publishing a first claim is a deliberate
human decision, made at the command line.
