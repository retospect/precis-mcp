# Kind enablement convention

Handler authors: declare resource requirements via
`KindSpec.requires_env` rather than via inline checks at the
boot site. The dispatch composition root
(`precis.dispatch.boot`) consults the
`precis.kind_gate.gate(spec, disabled=...)` predicate **before**
calling the handler's `__init__`, so:

- a kind whose env vars aren't set is skipped without importing
  the handler module;
- a kind listed in `PRECIS_KINDS_DISABLED` is skipped without
  consulting its env vars;
- both classes of skip surface on the cold-start
  `Kinds unavailable:` banner with a short reason.

## Declarative posture

The shape every new handler should follow:

```python
class FooHandler(Handler):
    spec: ClassVar[KindSpec] = KindSpec(
        kind="foo",
        title="...",
        description="...",
        supports_get=True,
        # Declare ALL required env vars here. The gate enforces
        # presence before calling __init__.
        requires_env=("FOO_API_KEY", "FOO_TENANT_ID"),
    )

    def __init__(self, *, hub: Hub) -> None:
        # By the time we land here, the gate has validated every
        # env in spec.requires_env is set non-empty. Read them
        # directly; a defensive re-check is OK as a guard against
        # drift between the gate's tuple and what __init__ consumes.
        import os
        self.api_key = os.environ["FOO_API_KEY"]
        self.tenant = os.environ["FOO_TENANT_ID"]
        ...
```

## Secrets: `requires_secret` (vault-resolved credentials)

For a credential rather than a plain env var — an API key or token that
resolves through the secrets vault (ADR 0055) — declare it in the sibling
`requires_secret` tuple instead of `requires_env`. The gate checks each name
via `secrets.is_available` (env → vault) before `__init__`, and an unresolved
secret surfaces on the `Kinds unavailable:` banner with a `missing secret …`
reason, exactly like a missing env var. Example: `patent` gates on the EPO
credentials this way, so the kind is hidden only where the vault can't supply
them — its `PRECIS_PATENT_RAW_ROOT` is a config-defaulted path, not a gate
(the capability-universalization dropped the incidental env gates).

## Anti-pattern: inline boot-site gating

```python
# DON'T do this in precis/dispatch.py:boot()
foo_key = os.environ.get("FOO_API_KEY")
if foo_key:
    _gated(FooHandler, key=foo_key)
```

Reasons to avoid the inline check:

1. **Banner lies.** The cold-start `Kinds unavailable:` line
   reads `boot.loadabilities` to compute its content. An inline
   guard short-circuits *before* `_try`, so the kind has no entry
   at all — neither in `Kinds loaded:` nor in
   `Kinds unavailable:`. The operator can't tell the kind exists.
2. **Operator surprise.** When the env var is mis-typed
   (`FOO_API_TOKEN` instead of `FOO_API_KEY`), the inline guard
   silently skips the kind; the declarative gate surfaces
   `missing FOO_API_KEY`.
3. **Test surface.** Tests can override env via the dataclass
   constructor (`PrecisConfig(...)`) which feeds the gate; an
   inline guard would require monkey-patching `os.environ` from
   the boot site, which is fragile.
4. **Convergence.** The gate is the single source of truth for
   "should this kind load"; spreading the predicate across boot
   sites and handler `__init__` makes it hard to audit.

## When `requires_env` isn't enough

Some handlers need more than env-var presence (a live store, a
file root that exists, an optional Python dep that imports).
These remain inside the handler's `__init__`, raising
`InitError` with a message in the canonical `"<kind>: <reason>"`
shape:

```python
def __init__(self, *, hub: Hub) -> None:
    if hub.store is None:
        raise InitError("foo: store required")
    try:
        import optional_dep
    except ImportError as exc:
        raise InitError("foo: optional dep 'optional_dep' not installed") from exc
    self.store = hub.store
    self.dep = optional_dep
```

`_try` catches the `InitError` / `ImportError` and translates it
via `precis.kind_gate.loadability_from_exception` so the banner
reason reads cleanly (`store required`, `optional dep ... not installed`).

## Prohibition

The `PRECIS_KINDS_DISABLED` env var is parsed by
`precis.kind_gate.parse_disabled` into a frozen set and threaded
through `boot(..., kinds_disabled=...)` into every `_try` call
via the local `_gated` helper. The gate returns
`Loadability(loaded=False, reason='prohibited')` for any kind in
the set, regardless of resource availability — operator intent
wins over incidental env presence.

See `precis-kinds-disabled-help` (agent-facing skill) for the
operator workflow.

## References

- `src/precis/kind_gate.py` — `parse_disabled`, `Loadability`,
  `gate`, `loadability_from_exception`, `format_unavailable`.
- `src/precis/dispatch.py:_try` — gate consumer; populates
  `Hub.loadabilities`.
- `src/precis/server.py:_kinds_unavailable_line` —
  banner renderer.
- `src/precis/protocol.py:KindSpec.requires_env` —
  declarative env-var requirement.
- `docs/design/mcp-cold-start-token-budget.md` Phase 4 — the
  broader design context.
