# Schema reconcile must preserve PostgreSQL ACLs (P0)

migra diffs don't emit GRANTs, so a reconciled new table ends up owned by
deploy with no agent_rw/agent_ro grants. Add an ACL diff / re-grant step to
`scripts/reconcile` + `src/precis/store/migrate.py`. Sonnet-shaped.
