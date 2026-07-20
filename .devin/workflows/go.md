---
description: Implement the agreed spec, ship to main, and deploy to the cluster — the dark-factory one-keystroke. /land plus scripts/deploy.
---

You said **go**. Turn the spec established this session into shipped,
deployed software. Everything mechanical is a script — spend tokens on the
implementation and on genuine failures, nothing else.

The user may pass a ship message after the command; otherwise write one.

## Procedure

1. **Implement the spec.** If the change discussed this session isn't fully
   written yet, implement it now — code + tests, on a feature branch. If it's
   already done, skip straight to shipping. Do not re-ask for confirmation of
   a spec already agreed on; that's what "go" means.

2. **Ship.** Follow `/land` steps 1–6 (message → doc refresh →
   `scripts/ship "<message>"` → failure handling). **Do NOT deploy if the
   ship failed.**

3. **Deploy.** Only after a green ship:
   ```
   scripts/deploy
   ```
   This pings all hosts (aborts on any unreachable — a partial deploy mixes
   versions), then runs the ansible redeploy (reinstalls `precis-mcp@main`
   into every venv, bounces every daemon, auto-applies pending migrations).
   The output is verbose — prefer `rtk err -- scripts/deploy`. If it exits
   non-zero, surface the failing ansible task verbatim — the cluster may be
   on mixed versions; do not declare success.

4. **Confirm — always end with this exact three-line block** (verify each
   line; `git rev-parse origin/main` for the sha):
   ```
   Merged to main:  ✓ <sha> on origin/main   (or ✗ — ship failed above)
   Pushed:          ✓ origin/main             (the squash-merge IS the push)
   Deployed:        ✓ cluster running <sha>   (or ✗ — deploy failed above)
   ```
   Use ✗ on any line that did not happen (red gate → all three ✗; green ship
   but failed deploy → first two ✓, deploy ✗).

5. **Residuals + next steps** — same as `/land` steps 7–8 (persist to
   `OPEN-ITEMS.md`; a precis `gripe`/`todo` write hits PROD, so only with the
   user's explicit go-ahead; bounded fixes get their own branch + /go cycle).
