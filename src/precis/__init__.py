"""precis-mcp v8 — the MCP server and the research substrate behind it.

``precis serve`` exposes the seven-verb surface (``get``/``search``/``put``/
``edit``/``delete``/``tag``/``link``, ``kind=`` discriminated) over ~70 content
kinds, all backed by one Postgres DB (``refs`` one row per thing, ``chunks``
the append-only body, typed ``links``). Orientation: ``docs/codebase.md``;
this docstring only maps the packages. DB-resident settings ops:
``docs/runbooks/settings-ops.md``.

**Packages under ``src/``.** ``precis`` is the core; the rest are first-party
plugins and bridges sharing the repo:

- ``precis_se`` (``se`` kind), ``precis_chem`` (``route``), ``precis_bio``
  (``protein``), ``precis_pathway``, ``precis_estimate`` — domain kinds loaded
  through the ``precis.handlers`` / ``precis.job_types`` / ``precis.migrations``
  / ``precis.skills`` / ``precis.handle_codes`` entry points
  (``pyproject.toml``; own migration namespace each). Core kinds use the
  in-tree ``BUILTINS`` registry instead.
- ``precis_chain`` (chemistry-free nucleic-acid geometry), ``precis_surface``
  (lattice-agnostic smooth-surface kernel), ``precis_dft``, ``hexfold``
  (topology-only ``.hx`` notation; never imports precis) — kernels the plugins
  and workers bind.
- ``precis_web`` (web UI, HTTP Basic against ``web_users``), ``asa_bot``
  (Discord bridge), ``asa_slack`` (Slack bridge).

**Inside ``precis``.**

- *Request path.* ``server.py`` (thin FastMCP wrapper) and the ``tools/``
  registry (shared by MCP and the CLI; ``command_parser`` for the one-tool
  command profile) call ``runtime/`` (``PrecisRuntime``; ``dispatch`` resolves
  the kind and verb, ``search`` fans out across kinds), which routes to
  ``handlers/`` — one adapter per kind implementing ``protocol.Handler`` and
  declaring a ``KindSpec``; ``dispatch.py`` holds the registry, plugin loading
  and the service ``Hub``. ``response.py`` and ``hints.py`` shape what comes
  back.
- *Storage.* ``store/`` (psycopg3 pool, forward-only ``Migrator``, composed
  sub-stores) over ``migrations/``; ``settings.py`` / ``secrets.py`` are the
  DB-resident config and vault layers.
- *The factory.* ``ingest/`` (PDF to refs + chunks, embeddings left NULL),
  ``workers/`` (derived-queue passes, schedule cadences, job executors; fill
  embeddings, cards, classification), ``jobs/``, ``backfill/``, ``quest/`` (the
  research loop), ``nanopub/`` + ``taproot/`` (claim graph and the signed
  publication pipeline).
- *Domain kernels.* ``cad/``, ``pcb/``, ``structure/``, ``structsolve/``,
  ``blocktree/`` and ``design/`` (the shared design spine ``se`` and ``cad``
  rent), ``sim/``, ``thermo/``, ``viz3d/``, ``supply/`` plus loose
  domain-logic modules (``cad_resolve``, ``fit_classes``, ``component_series``,
  ``thread_forming``).
- *Surfaces and tooling.* ``cli/`` (``precis`` subcommands, registry in
  ``cli/registry.py``), ``data/skills/`` (the agent-facing runtime docs served
  by ``get(kind='skill')``), ``skill_index/``, ``python_index/``,
  ``utils/`` (shared leaf helpers; ``utils/llm`` is the LLM router).

**Lifecycle.** ``runtime.factory.build_runtime`` builds the runtime once
(binding the process-global stores the router and vault read); core kinds
register from the in-tree ``BUILTINS``, plugins load last through
``dispatch._load_plugins``. Config is loaded once at startup and frozen
(``config.py``).

``__version__`` is read from the installed distribution metadata, so it cannot
drift from ``pyproject.toml``; the literal is a fallback for a bare source tree.
"""

from importlib.metadata import PackageNotFoundError
from importlib.metadata import version as _dist_version

#: The one source of truth for the running version is the installed
#: distribution metadata (``pyproject.toml``'s ``version``, baked into
#: ``*.dist-info`` at install time). Reading it here means ``__version__``
#: can never drift from the packaged version the way a hand-maintained
#: literal did (it lagged at 8.17.0 while the package shipped 8.20.0).
#: The literal below is only a fallback for a bare source tree with no
#: install metadata (rare — even editable installs write a ``.dist-info``).
try:
    __version__ = _dist_version("precis-mcp")
except PackageNotFoundError:  # pragma: no cover — uninstalled source tree
    __version__ = "8.35.11"
