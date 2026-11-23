---
id: precis-key-memory-help
family: memory
title: precis — key memory and standing orders
summary: Fill a new user’s key memory and keep their standing orders and current links up to date
answers:
  - what belongs in a key memory?
  - how do I record and read standing orders?
  - who is the user and how do they want agents to work?
  - how do I fill an empty key memory for a new user?
applies-to: get/put/edit/link (kind='memory')
status: active
tags: [workflow, addressing]
---

# precis-key-memory-help — the user’s standing orders and current links

Read the key memory named in the session startup text. If you need the
account’s handle, use `get(kind='profile', id='me')`. Read the hub with
`get(kind='memory', id='me<N>')`.

## Structure the hub

- **Who the user is and how they work** — a few lines in the user's words.
- **Standing orders** — one child memory per order, with one claim as its
  title, at most 12 words. Put scope, the user’s wording, the UTC date and
  the source session or ruling in its gloss. Link the child to the hub with
  one `part-of` edge and use `cites` or `derived-from` for graph evidence.
  Tag the hub `section:index` so its member listing gives one line per
  order. Verify the ordinary hub read shows the orders and their handles.
- **Current things** — links to the user’s hubs, active plans and saved
  searches that answer “what is open now?” Leave project details and
  status on the linked refs.
- **Skills to open first** — skill IDs with a short cue for when to open each.
- **By voice** — the user’s standing preferences for voice sessions.

Use the memory conventions in [[precis-memory-help]]. Keep the hub body
within the 4,000-character fisheye excerpt — the cap in
`src/precis/key_memory.py::HUB_BODY_MAX_CHARS`, the same limit the write
path warns at and the fisheye cut uses (a test pins the three together).
Prune obsolete pointers and move detail into linked memories when a read
gets too long. Project history belongs with the project, and session
state belongs in the resume note.

## Keep it current in every session

When the user states a durable fact about themselves or a lasting instruction
for agents, update their key memory in the same session. This includes a
changed role, a new preference or standing order, and a project or hub the
user wants to keep in view. Check the existing entry first, preserve the
user’s wording, and date and source the update. Fix the pointer when a
linked item closes or is superseded. Read back the hub and any changed
order to verify the update.

Under the current policy, an agent in the user’s session records
user-stated information directly when the connection permits it. Attribute
the change and keep it reversible through the memory’s revision history.
Propose inferences for the user to confirm. Background machinery proposes
changes for review under the current setting. That setting can change to
allow automatic writes. If this connection cannot write, tell the user
what still needs to be recorded.

## Fill an empty stub

Ask only for what the user has yet to tell you. Start with these short questions.

1. How should I describe your role and the way you work?
2. What standing instructions should every agent follow?
3. Which hubs, plans or saved searches should I keep at hand?
4. Which skills should I open first, and what should I do differently by voice?

Use the answers to fill the hub and link the individual orders. Read it
back and let the user correct the wording, scope and links.
