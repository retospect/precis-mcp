---
id: precis-voice-chat-help
family: core
title: precis — talking by voice (live conversation)
summary: speak to a user in live voice chat, describe refs without reading ids aloud, and turn on the word-handle switch
answers:
  - how do I talk to the user by voice?
  - how do I name gripes, todos and papers aloud?
  - how do I turn word handles on in a live voice call?
  - what can I do when the connector refuses the word-handle switch?
applies-to: live voice chat; any session where the user listens instead of reads
tags: [addressing]
kinds: [handle]
status: active
---

# Talking by voice

Any MCP connection may be a voice call. Follow this skill from the first
reply when speaking with the user.

1. Never read an id aloud (`gr740061`, six digits). Say a description:
   "the voice guidance gripe", "Miller 2023, the ammonia one".
2. Write the exact handle once in brackets after the description on first
   mention: `the voice guidance gripe [gr740061]`. If the client reads
   brackets aloud, use a Markdown link: `[the voice guidance gripe](gr740061)`.
3. With nothing to describe, or if asked for word handles, say the
   two-word handle ("gentle beacon"). If the converter reports a clash,
   say all three words it returns.
4. `put(kind='handle', text='on')` adds a `word` column to search and list rows.
   A `word` column shows the switch is enabled, either explicitly or
   through automatic client matching.
5. If the switch is refused, keep using descriptions. For a one-off word
   handle, call `get(kind='handle', id=740061)` with the ref's numeric id.
   See [[precis-session-context-help]] for connector switch scope.

Full detail, converter and clash handling: [[precis-word-handles-help]],
[[precis-handle-help]], [[precis-session-context-help]]. Writing audio
drafts and episodes is covered in [[precis-voice]].
