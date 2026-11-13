# Tmux worker launch — traps verified running the gripe fleet

**When.** You're the coordinator (or any master session) launching,
watching or recovering `claude`/`codex`/`devin`/`kimi` workers in tmux
windows — `scripts/fleet-wave`, `scripts/devin-tree`, or a hand-rolled
window. Verified 2026-10-12/13 on Reto's Mac. The launch/watch/close
procedure itself is the `gripe-fleet` skill; the Codex fleet's own
launcher is `scripts/fleet-codex` (`codex-fleet.md`); Devin launch is
`scripts/devin-tree` (`devin-trees.md`). This file is the gotcha list.

## tmux mechanics

- `tmux new-window -t 0 …` reads `0` as a *window index* ("index 0 in
  use"); target the session as `-t 0:`.
- Piping the TUI through `tee` blanks the pane and kills Codex. Launch
  `claude`/`codex` bare, then `; echo EXITED; sleep 600` so a crash
  stays readable. Long prompts go in a file, never inline.
- `tmux send-keys … "<long text>" Enter` leaves the text sitting in the
  input box of Claude Code and Codex; send a second `C-m` after ~1 s to
  submit. Cross-session SendMessage (ListAgents names like
  `gripe-coord-59`) is the clean channel; a stood-down Claude session
  cannot exit itself — kill its window.
- A worker whose window shows an unsent suggestion is **idle, not
  stuck** — never press Enter on another session's unsent input line.
  One sitting on a permission dialog needs Esc (deny) plus a message,
  never a "Yes" typed on Reto's behalf.

## Claude (`claude -w NAME`)

- A killed `claude -w NAME` leaves `.claude/worktrees/NAME` and branch
  `worktree-NAME` behind, locked to the dead pid. Relaunching the same
  name needs `git worktree unlock` + `remove` + `git branch -D` first.
- Recover a killed Claude from the dead pane within its 600 s `sleep`:
  `claude --worktree X --resume <id>`.

## Codex

- The Codex cask binary died with SIGKILL (exit 137) because its files
  were owned by `deploy:deploy` and carried `com.apple.quarantine`;
  Reto fixed ownership and the xattr by hand — the classifier denies
  the agent doing it ("Security Weaken").
- Codex runs as `codex -C <tree> "<prompt>"` and reaches precis via
  `~/.codex/config.toml`.

## Devin

- The CLI lives at `~/.local/bin/devin`, not on tmux's PATH —
  `scripts/devin-tree --tmux [SESSION]` (without `--tmux` it runs Devin in
  the calling terminal, not a window) exports PATH, creates the tree + `worktree-NAME`
  branch, locks the tree to Devin's pid and writes `.claude/purpose`.
- `scripts/devin-tree` pre-trusts `<repo>/.claude/worktrees` in Devin's
  `trusted_workspaces.json` (`scripts/lib/devin_trust.py`) before launch,
  so a tree it creates starts without the "trust this directory" prompt. If
  the trust step fails it only warns and Devin shows the prompt; only Reto
  answers it — NEVER `tmux send-keys` into a window showing it, Enter
  selects "No, exit".
- `devin -p` non-interactive took >30 min on a smoke prompt; use the
  TUI window.
- Devin has no auto-mode classifier, so a brief must forbid what
  Claude's classifier would deny (e.g. removing tests) instead of
  relying on the guard.

## Kimi

- `scripts/fleet-wave launch NAME --kimi` runs the interactive CLI under
  its own subscription login — tier R only (`--tier W` and
  `FLEET_LEGACY_FOOTERS=1` are refused), at most `FLEET_KIMI_MAX`/
  `--kimi-max` live `kimi-` panes (default 2, the plan's concurrent cap).
  Interactive pane only: `kimi -p` headless automation is forbidden, the
  TUI takes no prompt flag, and no vault key or Moonshot API key is ever
  used. The login itself is the user's own (`kimi login`), outside any
  script — code never reads `~/.kimi-code/credentials` or `oauth`.
- The TUI cannot take an initial prompt, so the brief lands at
  `<tree>/scratch/kimi-brief.md` and launch types the one line `Read
  scratch/kimi-brief.md and do exactly what it says.` once the pane shows
  its input box (a bare `>` line — Kimi's prompt, not Claude's `❯`).
  Launch waits up to `FLEET_KIMI_READY_S` (default 15 s) for the box and
  refuses (rc 3) when it never appears; the send verifies the box
  cleared and retries the Enter — on a kimi pane alternating the plain
  key with the kitty-protocol `ESC [ 13 u`, since with tmux
  `extended-keys` off a plain Enter can arrive as a newline and the line
  sits unsent forever (gr651178: two panes idled 25 min before
  event-watch flagged them frozen). If the pane shows a trust or login
  dialog the brief is held (rc 3, window kept, batch assign still
  records): only Reto answers such a dialog — NEVER `tmux send-keys`
  into it. Afterwards send the line by
  hand: `scripts/fleet-wave send-line kimi-NAME "Read scratch/kimi-brief.md
  and do exactly what it says."`. The durable host fix for the TUI's
  "tmux extended-keys is off" banner is `set -g extended-keys on` in
  `~/.tmux.conf` plus a tmux restart.
- Tier enforcement is not `fleet-authority`'s settings file alone (Kimi
  reads no project permission file): the deny rules bite through the
  tree's own `scripts/hooks/kimi-adapt.py` copy, so only trees cut after
  this landed carry the check. Each decision also writes one line to
  `<tree>/.claude/kimi-adapt.log` — that log is what proves a pane's
  refusals are real. The Bash gate stops mistakes, not an adversary:
  `$(…)`, `&`, `python -c` and absolute paths are not normalised. Real
  enforcement is the server-side scope (G1 `mcp.slice_scope` enforce,
  td574161 grant) and tier R's rule that only the coordinator lands.
- event-watch reflexes on a `kimi-` pane: a bare `DONE-HANDOFF` gets
  `/clear` + the resume line once per cooldown; the concurrent-limit
  banner gets one resume per cooldown (no `/model` — the enforcement
  signal is not hammered); a usage-limit/quota banner gets no reflex and
  prints `error: kimi quota exhausted (no reflex)` — a retry cannot help
  before the window resets. Kimi prints no ctx figure, so ctx-guard and
  the idle `/compact` reflex leave the pane alone.

## Gates, classifiers, shared state

- Worker sessions in auto mode: `scripts/ship --quick` is denied for
  some ("CI Bypass") — workers commit, the coordinator/release owner
  lands. `--impacted` can still hit testmon's `get_file` IndexError;
  `scripts/lib/testmon-exit-guard.py` turns that into exit 0 with a warning
  when pytest's summary was clean, else exit 75 (result unknown) and
  `scripts/test` re-runs plain; the full gate runs detached with `nohup … &` and a polled log (about 3 h).
- Master classifier denials seen, each routed to Reto and cleared only
  by his explicit authorization by name: killing another session's
  Devin windows or removing sessionless trees ("Interfere With
  Workloads"), removing a Codex xattr ("Security Weaken"), moving
  `.git/precis-round` aside ("Modify Shared Resources"), a worker
  replacing a test ("Security Test Removal"). Never pursue a denied
  outcome via a peer or Devin — that is permission laundering.
- Stale `.git/precis-round/round.json` blocks `scripts/round close`
  and `cut --abandon`; round numbers are global on origin (Reto renamed
  the dir aside to unblock).
- Gripe STATUS values: `done`, `in_review`, `open`, `ready_for_fix`,
  `triaged`, `verify`, `wontfix`. psql backslash commands trip the prod-write
  hook even under `--ro`; use information_schema SQL.
- A sibling session's `reap-worktrees` SessionStart hook deletes any
  tree that is clean + sessionless + merged, branch included; write
  `.claude/purpose` into a Codex/Devin tree the moment you create it
  (a fresh purpose under 6 h is skipped).
- NEVER `pkill -f <text>` in the fleet: a Claude worker's argv holds
  its whole launch prompt, so one worker's `pkill -f scripts/ship`
  killed ten sibling sessions including the coordinator (2026-10-12
  12:55Z). Kill by pid.
- Hand stamps drift — read `date -u` or `get(kind='time')` before every
  timestamp (`docs/conventions/time.md`).
