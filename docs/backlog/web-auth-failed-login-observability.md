# precis-web: no signal on failed authentication

- **Status**: open, filed 2026-08-22.
- **Why now**: precis-web went public on 2026-08-22
  (`tailscale funnel` on melchior, see `web-basic-auth-users.md`). On a
  tailnet this was theoretical. It isn't any more.

## The gap

The Basic gate (`precis_web/auth.py`) has **no rate limiting, no
lockout, and no logging of failed attempts**. Confirmed by grep:
`auth.py` contains no `throttle` / `lockout` / `attempts` machinery, and
a wrong password produces no log line at any level.

So a sustained guessing campaign against `https://<gateway>/` from the
public internet is **completely invisible**. Not "hard to spot" —
there is no record that it happened.

## What is NOT the problem

Worth stating, so this doesn't get fixed at the wrong layer:

- **The password is not the weak point.** scrypt costs ~60 ms per
  attempt (a natural throttle, and failures are never cached so they
  always pay it), and the live credential is ~120 bits. Online guessing
  is not a credible path.
- **Username enumeration is not open either.** Measured 2026-08-22:
  real-user-wrong-password 40.2 ms (sd 3.8) vs nonexistent-user 38.9 ms
  (sd 1.3) / 39.4 ms (sd 0.8), n=60 each. The ~1 ms delta is the same
  order as the difference between two *nonexistent* usernames. The
  dummy-scrypt path works. (An n=12 run showed ~4 ms and looked like a
  leak — it was noise. Use n≥60 if re-measuring.)

The problem is purely **observability**: you would not know.

## Shape of a fix

1. **Log failed attempts** — login, source IP, timestamp, at `WARNING`.
   The cheapest thing that converts "invisible" into "greppable", and it
   composes with the existing log shipping. Do this first; it is most of
   the value.
2. **Counter + alert** — failures per source over a window, into the
   existing alert path (`kind='alert'`), so a campaign pages someone
   rather than waiting to be noticed.
3. **Rate limiting** — only after 1 and 2. It is the piece most likely
   to lock out the legitimate single user, and without the logging you
   cannot tune the threshold honestly.

Deliberately not 2FA: HTTP Basic has nowhere to put a second factor
(see `web-basic-auth-users.md` §5 on why recovery is also out).

## Definition of done

A wrong password leaves a `WARNING` line carrying login + source IP; a
test asserts it; the runbook says where to grep. Rate limiting is
explicitly out of scope for the first cut.
