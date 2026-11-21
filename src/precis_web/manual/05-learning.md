# Tracking your study

Open **Learning** from the navigation or your account page. Its calendar is
your private record of study activity. A blank day means **No study recorded**,
including days when you may have studied outside précis.

## Check in

Choose a date and mark **Studied** or **Rest**. Minutes and a short note are
optional when recording study. A short session counts; an unsuccessful
attempt still takes effort. These are labelled self-reports and do not change
concept mastery, retire cards or mark a quiz correct.

Click a calendar day to read its current check-in. Recording that date again
corrects its status while retaining the earlier event in your private history.
A rest entry does not require or record study minutes.

## Current context

Below the calendar, **Current context** lists the cards you are learning right
now — every live card authored in précis (by the card forge, an agent, or by
hand) that represents a concept, grouped by concept. Read-only imports from
Anki do not appear. Every cloze gap is
masked; hover it (or tab to it) to reveal the answer, so the list works as a
self-quiz.

- **boost** queues a card to come up *today*: the next Anki sync brings its
  due date forward. Your review history is never rewritten — only the due
  date moves. A boosted card shows *boost queued* until the sync applies it.
- **drop** retires that one card (the same retire path as the Anki page).
- **I know this** marks the concept mastered and permanently retires your cards
  for it. The next Anki sync removes those notes. Use **Undo** under
  **Marked as mastered** to restore the cards and reverse the dismissal.
  If Anki already removed them, the next sync recreates the notes with
  fresh review schedules.

The panel is yours alone: it lists only the signed-in account's cards.

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

## Connect an assistant

Your account page has an **MCP identity** section. Mint a labelled token, copy
it when shown, and use it as the bearer credential in your MCP client's
configuration. The plaintext is shown once. You can keep separate tokens
for different clients and revoke one without affecting the others.

The account owner determines access. Shared installation credentials and
ordinary background agents cannot read this study log. Tokens should be
handled as passwords; do not paste them into notes, URLs or chat transcripts.

## What the calendar measures

This version records manual study and rest check-ins. It does not yet import
your Anki reviews, measure retention, recommend the next paper or infer that
you listened to a podcast. Those learning features need separate evidence.
