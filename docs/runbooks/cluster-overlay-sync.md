# Updating the cluster deploy overlay on melchior

**When.** You changed the gitignored overlay (`deploy/inventory/`: real
`hosts.yml` / `topology.yml` / `host_vars` / `group_vars` plus the
vault-encrypted `vault.yml`) and a deploy launched **on melchior** must see it.
The overlay cannot ride precis-mcp's public git — it is secret and the leak
gate forbids real node names in tracked `deploy/` files. Deploy mechanics:
[`cluster-deploy`](./cluster-deploy.md).

## Shape

- melchior has a **dev** precis-mcp checkout (user = the controller's login,
  separate from the `deploy`-user venv).
- `git_deploy_helper` in that user's home is a **non-bare** git repo with
  `receive.denyCurrentBranch=updateInstead`; the dev checkout's
  `deploy/inventory` is a **symlink** to it.
- On the controller (Mac), `deploy/inventory/` is its own git repo with remote
  `deploy-helper` → that melchior path over ssh.
- `.vault-pass` is hand-carried once, out of band, to
  `precis-mcp/deploy/.vault-pass` (chmod 600). It sits *beside* `inventory/`,
  never inside the overlay repo, so the key never travels in git. Re-carry only
  on a vault re-key.
- If the first push into an unborn branch ever fails to check out, fall back to
  a bare repo + working clone.

## Update procedure (from the Mac MAIN checkout — `git -C`, never `cd`)

    OV=<main checkout>/deploy/inventory
    git -C "$OV" commit -am 'overlay: <what changed>' \
      && git -C "$OV" push deploy-helper main

`updateInstead` checks the push out into melchior's worktree, so the symlink
reflects it. No manual `pull`, no separate clone.

## From a worktree-isolated session

The harness refuses `git -C <main>/deploy/inventory` and Edit/Write under it,
and a `!` command from the user runs in the same session so it is refused too
(the detail is withheld over Remote Control, so it looks like a silent
failure). What works:

1. `git clone <OV> /tmp/overlay-clone` (allowed); edit + commit there.
2. `git remote add deploy-helper <dev-user>@melchior:<home>/git_deploy_helper`
   and push.
3. The Mac copy now lags by the clone's commits (git refuses a push into its
   checked-out branch) — the user fast-forwards it from a plain terminal.

## ⚠ `remote rejected … (Working directory has unstaged changes)`

`updateInstead` refuses on **any** dirty tracked file — not only files the push
would touch — so one stale in-place edit on melchior jams the whole channel
silently (it sat 4 days once). Someone edited `git_deploy_helper` directly
instead of pushing from the Mac. Resolve:

1. List the dirty files on melchior (`git status`, `git diff` over ssh).
2. For each: if it holds real config that exists nowhere else (e.g. a
   `host_vars` flag), **adopt it into the Mac overlay first**, then
   `git checkout -- .` on melchior; if it is a stale/damaged copy, revert it.
3. Push again.

**Diagnosing a dirty vault file: compare DECRYPTED content, never ciphertext.**
ansible-vault re-encrypts with a fresh nonce on every save, so a changed
ciphertext proves nothing. Run
`ansible-vault view --vault-password-file deploy/.vault-pass <f>` on both
sides, parse, and diff **key sets** — never print values. In the incident this
turned "someone edited secrets, don't touch it" into "the live file is a
strict subset (truncated to about half the keys), git HEAD and the user's own
backup agree byte-for-byte, reverting is restorative". Leaving it alone would
have left ~14 secrets missing indefinitely: every deploy launched **on
melchior** in the window rendered without them. If asa / email / Kagi /
Outlook misbehave after such a window, look here first.

**Root pattern:** config edited in place on a node diverges from the Mac source
of truth and *nothing watches*. It surfaces only as a side effect of unrelated
work. Edit the overlay on the Mac, push; never edit on the node.
