# Writing a precis plugin

Third-party Python packages can add new `kind=` handlers to a running
`precis-mcp` server without touching the precis repo. Plugins are
discovered at boot time via the standard
[`importlib.metadata`](https://docs.python.org/3/library/importlib.metadata.html)
entry-point mechanism and registered into the same hub the built-in
handlers use. The LLM-facing MCP surface is unchanged — your kind
shows up in `tools/list`, participates in `precis-help`, and is
dispatched via the same seven verbs (`get`, `search`, `put`, `edit`,
`delete`, `tag`, `link`).

## The contract

A plugin handler must:

1. Subclass `precis.protocol.Handler`.
2. Declare a `KindSpec` as a `ClassVar` describing which verbs it
   supports, whether ids are numeric or slug, and any required env
   vars.
3. Accept `*, hub: precis.dispatch.Hub` in `__init__`.
4. Implement the verb methods it advertises. Unadvertised verbs
   inherit the base-class stub that raises `Unsupported`.
5. Raise `precis.dispatch.InitError` from `__init__` if it cannot
   usefully run (missing optional dep, missing env var, bad config).
   The boot loop logs the reason and hides the kind; the server
   stays up.

The canonical minimal example is `precis.handlers.calc.CalcHandler`
— ~40 lines of handler proper, stateless, no DB. Read it end-to-end:
[`../src/precis/handlers/calc.py`](../src/precis/handlers/calc.py).

## Entry-point declaration

In your plugin package's `pyproject.toml`:

```toml
[project.entry-points."precis.handlers"]
wikipedia = "precis_wikipedia:WikipediaHandler"
```

The left side is a free-form name used only in log messages. The
right side is an `importable.path:Class` reference to your `Handler`
subclass. One package may register multiple entry-points.

After `pip install precis-wikipedia`, the next `precis` boot
discovers and registers the handler automatically. No config file
edits; no PR to the main repo.

## Minimal example

```python
# precis_wikipedia/__init__.py
from typing import Any, ClassVar

from precis.dispatch import Hub, InitError
from precis.protocol import Handler, KindSpec
from precis.response import Response


class WikipediaHandler(Handler):
    spec: ClassVar[KindSpec] = KindSpec(
        kind="wikipedia",
        title="Wikipedia article summary",
        description="Fetch a short summary of a Wikipedia article by title.",
        supports_get=True,
        id_required=True,
    )

    def __init__(self, *, hub: Hub) -> None:
        # Import optional deps inside __init__ so a missing dep
        # becomes a clean InitError (the kind drops off the surface
        # with a WARNING, the server stays up).
        try:
            import wikipedia
        except ImportError as e:
            raise InitError(
                "wikipedia plugin needs the 'wikipedia' package"
            ) from e
        self._wikipedia = wikipedia
        _ = hub  # populated automatically by _register_with

    def get(self, *, id: str | None = None, **_: Any) -> Response:
        if not id:
            from precis.errors import BadInput
            raise BadInput(
                "wikipedia requires an article title as id=",
                next="get(kind='wikipedia', id='Python (programming language)')",
            )
        return Response(body=self._wikipedia.summary(id))
```

Pair it with:

```toml
# pyproject.toml (plugin package)
[project]
name = "precis-wikipedia"
version = "0.1.0"
dependencies = ["precis-mcp>=6.0.0a0", "wikipedia>=1.4"]

[project.entry-points."precis.handlers"]
wikipedia = "precis_wikipedia:WikipediaHandler"
```

That's it.

## Using the hub from your handler

`self.hub` is populated by the base class right after construction
(`Handler._register_with`). From there, reach for shared
infrastructure via the Hub service methods:

| Call | Purpose |
|------|---------|
| `self.hub.embed_one(text)` / `self.hub.embed(texts)` | Vector embeddings. Raises `RuntimeError` if no embedder is wired — guard with an `InitError` if your handler needs embeddings and the store is stateless. |
| `self.hub.emit_hint(hint)` | Append a deduplicated tip to the current request's output. The runtime collects hints per-request and renders them after the verb result. |
| `self.hub.store` | The psycopg pool wrapper. Only reach for it if you truly need persistent state; most plugins should be stateless or cache-backed. |
| `self.hub.handler_for(kind)` | Look up another handler for cross-kind coordination. |
| `self.hub.kinds` | The set of all live kinds at boot. |

Prefer the service methods over module-global reach-ins — the
underlying implementations stay swappable this way.

## Failure semantics

Plugin loading is **deliberately more tolerant** than built-in
handler loading. Built-in bugs crash boot so the maintainer notices;
third-party bugs are logged and skipped so one bad plugin cannot
brick the MCP server.

| Failure mode | Result |
|---|---|
| `entry_point.load()` raises anything | Plugin skipped, WARNING logged with the exception type. |
| `__init__` raises `InitError` | Plugin skipped. Canonical missing-dep / bad-config path. |
| `__init__` raises `ImportError` / `ValueError` | Plugin skipped. |
| `__init__` raises any other `Exception` | Plugin skipped, WARNING logged. (For built-ins this would propagate.) |
| Plugin's `kind` collides with an already-registered kind | Plugin skipped via `DuplicateRegistration`, WARNING logged. Built-ins win. |

Only `BaseException` subclasses outside `Exception`
(`KeyboardInterrupt`, `SystemExit`) propagate.

## Ordering

Built-in handlers register first; plugins register afterwards. Kind
names are claimed on a first-come basis, so a plugin cannot override
a built-in kind. If you believe a built-in kind is broken, open an
issue on `retospect/precis-mcp` — don't ship a plugin that tries to
replace it silently.

## Testing

Plugin tests don't need to involve entry-points at all. Instantiate
your handler directly against a bare `Hub`:

```python
from precis.dispatch import Hub
from precis_wikipedia import WikipediaHandler


def test_wikipedia_summary():
    hub = Hub()
    handler = WikipediaHandler(hub=hub)
    handler._register_with(hub)

    resp = handler.get(id="Python (programming language)")
    assert "Guido" in resp.body
```

For handlers that need a store or embedder, pass stubs into the
`Hub(...)` constructor; see
[`tests/conftest.py`](../tests/conftest.py) in the precis repo for
the `fresh_db` / `store` fixtures the built-in handlers use.

To test the entry-point plumbing itself, mock
`precis.dispatch._entry_points`:

```python
import logging
from precis.dispatch import PLUGIN_GROUP, boot


def test_plugin_registers(monkeypatch):
    class FakeEP:
        name = "wiki"
        def load(self_):  # noqa: N805
            from precis_wikipedia import WikipediaHandler
            return WikipediaHandler

    from precis import dispatch
    monkeypatch.setattr(
        dispatch,
        "_entry_points",
        lambda *, group: [FakeEP()] if group == PLUGIN_GROUP else [],
    )

    hub = boot(store=None)
    assert "wikipedia" in hub.kinds
```

See
[`tests/test_dispatch.py`](../tests/test_dispatch.py) in the precis
repo for the full pattern — the `_FakeEP` helper and
`_patch_entry_points` wiring are both reusable.

## Versioning

The plugin surface — `Handler`, `KindSpec`, `Hub.embed_one`,
`Hub.emit_hint`, `Response`, `InitError`, the seven verb signatures
— is intentionally small and slow-moving. Pin `precis-mcp>=6.0.0a0`
in your plugin's dependencies for now; once `precis-mcp` ships 1.0,
we'll commit to semver guarantees on this surface.

Breaking changes to the handler contract will be called out in
`precis-mcp`'s `CHANGELOG.md` under a `[plugin-surface]` heading.

## See also

- [`../src/precis/handlers/calc.py`](../src/precis/handlers/calc.py)
  — canonical minimal handler.
- [`../src/precis/dispatch.py`](../src/precis/dispatch.py) — the Hub,
  the boot loop, and `_load_plugins` itself.
- [`../src/precis/protocol.py`](../src/precis/protocol.py) —
  `Handler` base class and `KindSpec` dataclass.
- [`seven-verb-surface-migration.md`](seven-verb-surface-migration.md)
  — design rationale for the verb surface and the D7 contract for
  handler registration.
