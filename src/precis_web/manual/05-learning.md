# Tracking your study

Open **Learning** from the navigation or your account page. Its calendar is
a compact private record of study activity. Each date shows how many study
answers you submitted and how many Anki reviews arrived through Anki sync.
A blank day has no recorded activity.

## Optional check-in

Choose a date and mark **Studied** or **Rest**. Minutes and a short note are
optional when recording study. A short session counts; an unsuccessful
attempt still takes effort. These are labelled self-reports and do not change
concept mastery, retire cards or mark a quiz correct.

Click a calendar day to read its current check-in. Recording that date again
corrects its status while retaining the earlier event in your private history.
A rest entry does not require or record study minutes.

## Current context

**Current context** leads the page and lists the cards you are learning right
now — every live card authored in précis (by the card forge, an agent, or by
hand) that represents a concept, grouped by concept. Cards whose concept was
retired or never linked sit in a trailing **No live concept** group of their
own: **boost** and **drop** work there, but **I know this** does not — there
is no concept to mark mastered. Read-only imports from Anki do not appear.
Every cloze gap is
masked; hover it (or tab to it) to reveal the answer, so the list works as a
self-quiz. Enter an answer and select **Check answer** to reveal the card and
count the attempt. Your answer text is discarded.

- **boost** queues a card to come up *today*: the next Anki sync brings its
  due date forward. Your review history is never rewritten — only the due
  date moves. A boosted card shows *boost queued* until the sync applies it.
- **drop** retires that one card. The next Anki sync removes its note.
- Automatic cards show a **walk**, **highlight**, or **forge** badge.
  Use **drop all** for a source and UTC mint date to retire that batch
  from the page, including matching cards under **No live concept**.
  The Anki page offers the same controls for its forge cards with a live
  concept. For a walk card, the date is when the forge made it. Dropping
  a walk card pauses walk intake for that concept on your account for
  90 days.
- **I know this** marks the concept mastered and permanently retires your cards
  for it. The next Anki sync removes those notes. Use **Undo** under
  **Marked as mastered** to restore the cards and reverse the dismissal.
  If Anki already removed them, the next sync recreates the notes with
  fresh review schedules.

The panel is yours alone: it lists only the signed-in account's cards.

## Card what you highlight

When signed in, select 2–2,000 characters of page text and click **Card
this** or press `c`. The form shows the selection as context. Selections of
up to 60 characters also fill the **term** field, which you can edit.

**Queue card** files your request and links it to its source record when the
page has one. A matching shared concept or taxonomy term can supply the
name and definition. The forge uses your selection when writing cards for
your review list and Anki sync, including for terms you already have cards for.

The confirmation toast offers **Undo** for a few seconds. It cancels that
submission while it is queued. A concept created for your request is removed
when it has no remaining asks or cards. Undo also removes a page link created
solely by that submission. Once the forge writes a card for a submission, the
card stays in your review list. Copy and the native context menu
work as usual.

## Your recent walk

**Your recent walk** shows the pages you visited through `/eye` while signed
in, newest first, from the past 90 days. Only your account can see this
history. Select **Clear walk** to delete all your recorded visits.

## Set your pacing

On the same page, set your timezone and an optional typical session length.
The initial thirty minutes is guidance. Clear it if a time estimate is not
useful; it never prevents you from continuing to study. Podcasts are extra
listening.

Your configured timezone determines what **today** means. An explicitly
reported local date remains the date you chose when you later change zones.
Study answers and Anki reviews are grouped in your current timezone.

## Connect an assistant

Your account page has an **MCP identity** section. Mint a labelled token, copy
it when shown, and use it as the bearer credential in your MCP client's
configuration. The plaintext is shown once. You can keep separate tokens
for different clients and revoke one without affecting the others.

The account owner determines access. Shared installation credentials and
ordinary background agents cannot read this study log. Tokens should be
handled as passwords; do not paste them into notes, URLs or chat transcripts.

## What the calendar measures

The calendar measures recorded activity. It does not grade answers, measure
retention or infer that you listened to a podcast.
