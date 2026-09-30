---
status: idea
title: hardening residuals from the hub-title-200-truncation incident
pillar: platform
---

# Title round-trip assert + MCP staleness banner

Two hardening residuals from the 2026-08-19 truncated-title incident
(root cause: `precis-mcp-dev-stdio.sh` served a four-days-stale `/app`
bind-mount predating the `[:200]`-cap removal; repaired, see `git log`).

1. Assert in `mint_hub`/`refine_claim_sentence`
   (`src/precis/taproot/hub.py`) that the persisted `refs.title` round-trips
   equal to the claim sentence, so a stale caller fails loudly instead of
   silently truncating. This bug was invisible for three weeks.
2. **Superseded by td458385 (Reto, 2026-09-30) — closes by removal.** The
   decision is that the per-session stdio containers are retired rather
   than hardened and every session moves to the shared HTTP server, so the
   population this item hardens stops existing. Do not implement the
   staleness banner or arm the watchdog on them. The measurements below
   are why the decision went that way, and are kept because the next
   person to propose supervising a stdio container needs them; delete this
   sub-item once the migration is done.

   **Re-opened (superseded, see above).** `precis-mcp-dev-stdio.sh` had a `--check` preflight for
   *dependency* drift but nothing warned that `/app` was N commits behind
   `origin/main`. That launcher is gone, and for the shared server the
   checkout watchdog (`install_watchdog.CheckoutWatchdog`) does close the
   gap — it exits the moment the source tree's HEAD moves, so a banner
   there would report a state lasting seconds. The closure generalised
   that to the whole fleet, and the generalisation is wrong: the
   per-session `precis-mcp-dev-*` containers are still live and still
   stale, and they are the population the 2026-08-19 incident happened in.
   This item now also owns gr458061's restart-on-drift half.

   What the fleet actually looks like, measured 2026-09-30:

   - Eleven `precis-mcp-dev-*` containers, up 22 h to 6 days. `/app` is a
     **read-only bind of the live main checkout**, `.git` included — not a
     baked image copy. So the *files* are current and only the loaded
     Python modules are stale. Staleness here is process age, nothing
     more, which is why no in-container stat, grep or import can see it.
   - `PRECIS_CHECKOUT_WATCHDOG` is unset on all of them and
     `InstallWatchdog._fingerprint_for` returns `None` outside
     `site-packages`, which an editable install is. Both arms are off.
   - Each is `docker run -i --rm`: `AutoRemove=true`, `Restart=no`.

   Those last two facts decide the shape of any fix, and both were got
   wrong once already:

   - **There is no restart.** `docker restart` on an autoremove container
     stops it and the reaper deletes it. Worse, stdin is the pipe of the
     `docker run -i` that the owning Claude Code client spawned, so a
     surviving container would not be re-attached anyway. The only thing
     that produces a fresh container is the *client* reconnecting its MCP,
     which spawns a new one from the config. Any plan phrased as "restart
     the containers" is not executable; the executable version is "each
     session reconnects", and `--rm` means there is usually nothing left
     to reap.
   - **Do not arm `PRECIS_CHECKOUT_WATCHDOG` on these containers until
     they have a supervisor.** The variable would fingerprint correctly —
     `/app` is a real checkout — but `/app` is *main's working tree*,
     which moves on every qland, several times an hour during a burst.
     The arm polls at 5 s and exits on the first HEAD move. The shared
     server survives that because the ensure script's
     `--restart unless-stopped` brings a new one up; these have
     `Restart=no` and no supervisor but a human noticing `precis` went red
     in `/mcp`. Arming them trades "stale until the session ends" for
     "every session's MCP dies every few minutes during a burst". That is
     a worse failure, and it is the reason reporting-then-restarting has
     to arrive in that order rather than the reverse.

   So the open work is a supervisor for a per-session stdio container, or
   a decision that per-session containers should be short-lived instead —
   not a banner and not the env var alone.
