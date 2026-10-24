# Proposed authority.md addition: the board (for Reto to apply)

Drafted by the board-v2 builder; not authority. Suggested text for a new
section in `.claude/fleet/authority.md`.

## The board

Every loop — CHAT, COORDINATOR, GRIPE_FIX and BUILDER — posts routine
status to the board (`scripts/board`), not to a peer's message queue:
one `--state` line per event, at done, at each landing (`landed`), and
the moment a lane goes `red` or `blocked` (a blocked line names the
handoff path). Lines are short: over 120 chars is refused; detail goes
in a handoff or gripe. Readers keep a cursor — `read --new --as <reader>`
prints only what that reader hasn't seen; `watch` gives a live coloured
view in a tmux window.

SendMessage and `panes say` are only for things that need action now: a
decision, a blocker, a permission refusal, a landed-sha handoff. Routine
status in a message wakes a whole session at full context (gr515152).
