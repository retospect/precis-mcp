"""Server configuration. Loaded once at startup, frozen.

All env vars use the `PRECIS_` prefix. A `.env` file in CWD is consulted
as a lower-precedence source.
"""

from __future__ import annotations

from typing import Literal

from pydantic_settings import BaseSettings, SettingsConfigDict

LogLevel = Literal["DEBUG", "INFO", "WARN", "WARNING", "ERROR"]
EmbedderName = Literal["mock", "bge-m3", "remote"]


class PrecisConfig(BaseSettings):
    """Loaded from env (PRECIS_*) and optional .env file. Frozen."""

    model_config = SettingsConfigDict(
        env_prefix="PRECIS_",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        frozen=True,
    )

    log_level: LogLevel = "INFO"
    database_url: str | None = None  # required from phase 2 onward
    default_corpus: str = "default"

    db_connect_retry_seconds: float = 30.0
    """Boot-time budget for connecting the store before giving up.

    When ``database_url`` is set but the DB is unreachable at boot — the
    classic case is a long-lived MCP subprocess spawned during a node
    reboot, before the DB node / tailscale is up — :func:`build_runtime`
    retries the connection for up to this many seconds, then **raises**
    (crashing the process) rather than silently coming up storeless.

    Coming up storeless is the worse failure: the server answers the MCP
    handshake but reports ``unknown kind`` for every DB-backed kind, and a
    liveness-only supervisor never restarts it (the 2026-07-14 asa
    incident — a degraded ``precis serve`` served asa a memory/gripe-less
    context for hours). Crashing instead lets the parent respawn until the
    DB is back. The window rides out the boot-race without churning on a
    tight crash loop (each ``Store.connect`` attempt itself blocks up to
    the pool ``open_timeout`` ≈ 10s). Set via
    ``PRECIS_DB_CONNECT_RETRY_SECONDS``; ``0`` disables the retry (fail on
    the first attempt).
    """

    owner: str = "owner"
    """Canonical username for the human running this instance.

    The single human identity the system addresses when it needs a
    person — the asker behind a web "ask a follow-up", the default
    addressee of an ``ask-user`` pause tag (stamped as
    ``user:<owner>``), and any "this is the human" default. Generalises
    the formerly hard-coded ``"reto"`` so the surface is multi-user
    ready: routing filters by ``user:<owner>`` rather than an implicit
    single user.

    Defaults to ``"owner"`` so a fresh install reads correctly with no
    config. Reto's instance sets ``PRECIS_OWNER=elmsfeuer`` to line up
    with the existing ``user:elmsfeuer`` tags. See
    ``docs/design/user-identity-and-ask-routing.md``.

    Set via ``PRECIS_OWNER`` in the env.
    """
    embedder: EmbedderName = "mock"
    """Which `Embedder` implementation to load.

    - ``"mock"`` (default): deterministic, no model load. Use for tests
      and local smoke runs.
    - ``"bge-m3"``: load `BAAI/bge-m3` via `sentence-transformers`
      (heavy; requires the optional `paper` extra). In-process.
    - ``"remote"``: an HTTP client (`RemoteEmbedder`) to a
      ``precis serve-embeddings`` service — no local model load, no
      `torch`. Requires ``PRECIS_EMBEDDER_URL`` (ADR 0020).
    """

    embedder_url: str | None = None
    """Endpoint(s) for ``embedder="remote"``.

    Ordered, comma-separated base URLs, e.g.
    ``http://127.0.0.1:8181`` or
    ``http://127.0.0.1:8181,http://pg-host.local:8181``. The client
    prefers the first healthy endpoint and falls back to the next. In
    the all-local fleet topology each node points at its own loopback
    embedder; a forwarded endpoint is an optional fallback.

    Set via ``PRECIS_EMBEDDER_URL`` in the env. Ignored unless
    ``embedder="remote"``.
    """

    embedder_timeout: float = 30.0
    """Per-call HTTP deadline (seconds) for ``embedder="remote"``.

    Set via ``PRECIS_EMBEDDER_TIMEOUT`` in the env.
    """

    embedder_max_retries: int = 5
    """Max retries per endpoint for ``embedder="remote"`` before falling
    back to the next endpoint (and then raising ``EmbedderUnavailable``).
    Retries use exponential backoff with full jitter; ``429`` / ``5xx`` /
    connection failures are retryable. The default is deliberately
    patient: a busy embedder returns ``429`` + ``Retry-After: 1`` and a
    restarting one returns ``5xx`` / refuses the connection for the few
    seconds its model takes to warm — the jittered backoff window
    (≈ tens of seconds) rides those out so a transient blip never
    surfaces as a failed pass.

    Set via ``PRECIS_EMBEDDER_MAX_RETRIES`` in the env.
    """

    corpus_dir: str | None = None
    """Filesystem root of the ingested-PDF corpus, laid out as
    ``<corpus_dir>/<letter>/<cite_key>.pdf`` by ``precis watch``
    (``letter`` = lowercase first alnum char of the cite_key, else ``_``).

    Used by the web paper viewer (``precis_web``) to stream a paper's PDF
    to the browser. On the cluster this is the NFS mount. When unset the
    web layer falls back to ``~/work/corpus`` (the ``precis watch``
    default). Set via ``PRECIS_CORPUS_DIR`` in the env.
    """

    root: str | None = None
    """The single root directory for **all** prose-file kinds:
    ``markdown``, ``plaintext``, ``tex``.

    Each kind walks this tree and filters by its file extensions:

    - ``markdown`` → ``.md``
    - ``plaintext`` → ``.txt``, ``.log``
    - ``tex`` → ``.tex``

    Slugs encode the file's relative path under this root
    (``chapters/intro.tex`` → ``chapters--intro``). The handlers
    treat this directory as the **only** writable area: every read
    and write is normalised through ``Path.resolve()`` and validated
    with ``Path.relative_to(self.root)``, which rejects ``../``
    traversal and symlink escapes alike.

    When ``PRECIS_ROOT`` is unset, **none** of the three file kinds
    register. Set via ``PRECIS_ROOT`` in the env.

    Named ``root`` (not ``precis_root``) so pydantic-settings with
    ``env_prefix='PRECIS_'`` derives the expected env var
    ``PRECIS_ROOT``. The earlier name ``precis_root`` was
    double-prefixed and silently no-op'd on every deployment.
    """

    python_roots: str | None = None
    """Python repos exposed to the ``python`` kind.

    Format: ``alias1:/abs/path1,alias2:/abs/path2``. Each alias is the
    repo's short identifier used in addresses (e.g. ``precis::pkg.mod``);
    each path is an absolute directory. Unparseable entries (missing
    ``:``, non-existent path, duplicate alias) are dropped with a
    warning; the remaining valid entries form the handler's known
    roots. When unset (or zero valid entries), the ``python`` kind is
    hidden. Set via ``PRECIS_PYTHON_ROOTS`` in the env.
    """

    startup_skills: str | None = None
    """Comma-separated list of skill ids to pin at cold-start.

    The bodies are reachable via the existing ``prompts/list`` wiring
    (every available skill registers as a prompt). Pinning **also**
    surfaces them in ``serverInfo.instructions`` so an agent sees the
    operator's recommended starting set on the first connect, even
    when the MCP client doesn't auto-render prompts at session start.

    Format: ``precis-search-help,precis-paper-help`` (whitespace
    around commas tolerated; duplicates ignored). Unknown slugs are
    logged and surfaced via a one-line banner notice. The default
    empty list keeps cold-start lean by design.

    Set via ``PRECIS_STARTUP_SKILLS`` in the env. See
    ``precis-startup-skills-help`` for the full discovery model.
    """

    startup_skills_cap_kb: int = 50
    """Cap on the total resolved-body size of pinned startup skills.

    Defensive guard against operator misconfiguration (pinning the
    entire skill corpus inflates context for every connecting agent).
    Skills whose cumulative body bytes would exceed the cap are
    dropped from the tail with a banner notice. Set to ``0`` to
    disable the cap (not recommended — leaves the budget unbounded).

    Set via ``PRECIS_STARTUP_SKILLS_CAP_KB`` in the env.
    """

    default_tags: str | None = None
    """Comma-separated list of session-context tags to merge on
    ``put`` for note-like kinds.

    A note-like kind opts in via ``KindSpec.note_like=True``
    (today: memory, gripe, conv, anki, todo, markdown,
    plaintext, tex). A ``put`` on such a kind has its ``tags=``
    payload union-merged with the parsed default set, preserving
    the caller's explicit-first ordering. The dispatcher emits a
    one-line hint listing the merged defaults so the agent sees
    the mutation.

    A ``tag`` verb call doesn't mutate — instead the dispatcher
    emits a suggestion hint listing any defaults missing from
    ``add=``, leaving the operator-explicit verb under operator
    control.

    Format: ``fbproj,2026-q2,team-research`` (whitespace tolerated,
    duplicates dropped, first occurrence wins). The default empty
    tuple is the no-op posture matching today's behaviour.

    Set via ``PRECIS_DEFAULT_TAGS`` in the env.
    """

    kinds_disabled: str | None = None
    """Comma-separated list of kinds to prohibit at boot.

    A prohibited kind is skipped entirely during
    :func:`precis.dispatch.boot` — its handler is never constructed,
    no abilities are registered, and the cold-start banner surfaces
    it on the ``Kinds unavailable:`` line with reason ``prohibited``.
    Resource gating (env vars declared on
    :class:`precis.protocol.KindSpec.requires_env`, store / embedder
    presence, file root) is orthogonal and applies independently.

    Format: ``patent,wolfram`` (whitespace tolerated, duplicates
    dropped). The default empty list keeps every resource-available
    kind loaded — matching today's behaviour. Unknown kind names are
    accepted (they're a no-op against the live registry); see
    ``precis-kinds-disabled-help`` for the operator workflow.

    Set via ``PRECIS_KINDS_DISABLED`` in the env.
    """

    # ── Anki sync (slice 2) ────────────────────────────────────────────
    anki_enabled: bool = False
    """Gate the headless AnkiWeb sync (`precis anki-sync`). Default-off, so the
    slice merges dark; enable only on the single designated sync runner (the Mac
    `precis-infra` stack), where ansible has `pip install`-ed the `anki` wheel.
    Set via ``PRECIS_ANKI_ENABLED``."""

    anki_user: str | None = None
    """AnkiWeb login email for the sync. A per-runner secret. ``PRECIS_ANKI_USER``."""

    anki_password: str | None = None
    """AnkiWeb password for the sync. A per-runner secret. ``PRECIS_ANKI_PASSWORD``."""

    anki_mirror_dir: str | None = None
    """Directory holding the single authoritative `.anki2` mirror. Must be
    stable across runs and unique to the one sync runner (two mirrors syncing
    one account would manufacture a full-sync conflict). ``PRECIS_ANKI_MIRROR_DIR``."""

    anki_deck: str = "Precis"
    """The deck precis-authored cards land in. ``PRECIS_ANKI_DECK``."""

    anki_fix_enabled: bool = False
    """Run the precis-fix pass on each sync — LLM-rewrite cards the user tagged
    `precis-fix` in Anki (editing that one foreign card is opt-in per the tag).
    Also toggled per-run by `precis anki-sync --fix`. ``PRECIS_ANKI_FIX_ENABLED``."""

    anki_project_enabled: bool = False
    """Project ALL Anki cards (incl. foreign, any notetype) into PG as read-only
    `anki` refs each sync, so the whole collection is searchable + feeds the
    knowledge-model. Read-only derived index — can't corrupt the account. Also
    toggled by `precis anki-sync --project`. ``PRECIS_ANKI_PROJECT_ENABLED``."""

    # ── Budget guardrails ──────────────────────────────────────────────
    budget_daily_usd: float = 20.0
    """Global 24h spend cap (USD) for the circuit breaker. Bounds router LLMs
    (claude + OpenRouter) and paid fetches (perplexity, …) together. Over the
    cap, new *expensive* work is refused; cheap/free/interactive still flow.
    A placeholder — tune from observed spend on the /budget meter before
    relying on it. ``PRECIS_BUDGET_DAILY_USD``. Read at runtime by
    :mod:`precis.budget.meter` (which also honours a web-set override)."""

    budget_hourly_usd: float = 5.0
    """Global hourly spend cap (USD) — catches a tight loop within the day
    before it eats the daily cap. Same trip behaviour as the daily cap.
    ``PRECIS_BUDGET_HOURLY_USD``."""

    budget_cheap_max_usd: float = 0.02
    """Upper bound (USD) of the ``cheap`` cost band; above it a call is
    ``expensive`` and subject to the breaker. ``PRECIS_BUDGET_CHEAP_MAX_USD``.
    Read by :mod:`precis.budget.bands`."""


def load_config() -> PrecisConfig:
    return PrecisConfig()
