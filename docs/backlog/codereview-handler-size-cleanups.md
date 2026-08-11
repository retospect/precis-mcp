# codereview: handler/mixin size + inheritance-as-config cleanups

- `store/_draft_ops.py::DraftMixin` — 80 methods / 2217 code lines,
  ≥7 responsibilities. First cut: lift the ~10-method review surface into
  its own object. `handlers/draft.py::DraftHandler` (53 methods) hosts 11
  unrelated `_*_hint` LLM lint generators — extract a draft-lint module.
- `handlers/finding.py::FindingHandler` outgrew `NumericRefHandler`
  (adds 15 methods incl. a 250-line `_put_acquiring` state machine; now
  bigger than the shared base) — graduate it toward a standalone handler.
- `handlers/perplexity.py` — `WebsearchHandler`/`ThinkHandler`/
  `ResearchHandler` override zero methods; they exist to hold ClassVars.
  Convert to a config-dataclass parameterization like
  `diagram/handler.py::DiagramHandler.LANG` already does. The base also
  reaches into an undeclared attr via
  `_cache_base.py` `getattr(self, "cost_per_call_usd", 0.0)`.
- `precis_web/item_view.py::ItemPresenter` — 10-method hierarchy with one
  subclass overriding one method; keep an eye on whether it earns its
  ceremony (module docstring already tracks promotion honestly).
