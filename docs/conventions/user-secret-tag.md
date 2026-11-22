# `user:<handle>` vs `user_secret:<handle>` (gr556002)

- `user:<handle>` is a **display / preamble-routing** tag: "this is about or
  for this user". It is not secret and gates nothing; any caller with
  ordinary `search`/`get` access can read it.
- `user_secret:<handle>` is **read-gated**. A ref carrying it is visible
  only to the viewer whose account handle matches, or to an admin.

Server queries apply the store visibility predicate before selecting,
counting or paginating refs, chunks, links and tag attachments.
Both endpoints must be visible to return a link. Tag discovery counts and
samples use the viewer's current visibility, including on shared tags.

A private cache entry reserves its key. A cache fill cannot replace it
on another viewer's behalf. Dispatch checks all unfinished dependencies
and uses the result for eligibility. Board views show dependency details
only when both endpoints are visible.

- **Viewer** = the authenticated browser account, the authenticated revision
  actor `user:<abbrev>`, or the stdio process actor. No wire argument names a
  viewer. Callers without a matching handle see only shared refs unless
  they qualify for the admin bypass.
- **Admin** = an abbrev in `PRECIS_SECRET_ADMINS` (comma separated). An
  admin read past the gate is logged at WARNING on `precis.secret_scope`.
- **Direct SQL** (`agent_rw`, `prod-psql`) bypasses the check. Standing
  order: agents do not read `user_secret` rows by SQL; the server-side
  check is the enforcement.
- Attach the tag to the ref. An expired attachment releases the restriction.
