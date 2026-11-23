"""LlmHandler — the `llm` model catalog (migration 0071, ``llm-catalog`` (git-only)).

A model card is one numeric ref per model (``claude-opus-5-5``, ``qwen-heavy``): the
body is the capability prose (embedded as ``card_combined`` — an ``llm`` card *is a
vector*, so ``search(kind='llm', q='careful SQL')`` matches on capability), and ``meta``
carries the structured facts (``model_id`` / ``tier_floor`` / ``offerings`` /
``capability`` / ``provenance``). Slice 1 is read-only: the cards are minted + kept true
by the ``llm_reconcile`` pass (and a seed step); agents *read* the catalog, they don't
hand-author it. The whole thing ships dark — an empty catalog is byte-identical to
today's behaviour (``Tier`` stays the floor).

Structurally this is the quest/gripe shape (numeric ref + ``emits_card`` + an append-only
log). The card write lives in :mod:`precis.llm_catalog` (one writer shared with the
reconcile worker). This handler is the MCP surface: ``get`` (with model-slug resolution),
``search``, and a guarded ``put`` that funnels to the shared writer. The typed review-log
(``llm_review`` chunks) + the ``llm_call_log`` tote arrive in slice 3.

``/ops`` and source ids read stored ``llm.op.*`` overrides directly from
this handler's store, not the process-global routing cache. These are
configuration reads, never LLM calls or claims about effective runtime
routing: request pins and runtime gates can change where a call lands.

``/decisions`` compares shadow routing with executed legacy calls per task kind, and the run_slice lane-pick shadow
(``meta.decision``) per task class.
``/suitability`` shows the stored suitability posteriors — validated
outcomes rolled up per ``model@adapter`` × task kind × difficulty band
by the nightly ``llm_suitability`` cadence (read-only; decide() never
reads them).
``/preview`` (gr681308) answers what the router would choose for a task
kind: it runs the router's own ``decide()`` over the cached ledger
snapshot and resolves the legacy ladder, then lists every candidate in
the demanded tier, ranked, with the chosen one marked. Open to every
caller because it builds no provider, places no call and writes nothing.
The demand is a worker's (``work_class`` defaults to background, not the
ambient MCP class), since the kinds asked about run unattended. It does
not model request pins, keyword rules or the gates a real call meets.
``/health`` (gr681308) groups the call log by executed path (the
decision's ``executed`` stamp, ``legacy`` before shadowing), model and
transport: call count, error share and p95 ``duration_ms``, bounded by
``args={'window_days': N}`` (7 default, at most 30) on the ``ts`` index;
gated rows are excluded. ``/decide-modes`` shows the winning decide mode
(``legacy``, ``shadow``, ``on``) for each task kind logged in the window
plus the default. ``/breaker`` reports the dollar breaker's status —
tripped or ok, the window, spend against cap, the open alert's start
("since"), and any resume override; the subscription quota taper is a
separate mechanism (``/pools``). All three are read-only.
``/accounts`` and ``/pools`` (migration 0280, S1a) read the owned-capacity
ledger: an account is one credential at one provider under one plan
(``llm_account``); a pool is a rate bucket with a reset rule and explicit
scope (an ``external_rate_limits`` row keyed ``llm:<account>:<window>``).
Both are read-only here except ``put(kind='llm', args={'account': {...}})``,
which is owner-only (:func:`precis.todo.guards.is_owner`). For a hosted
OpenAI-compatible provider (Groq, OpenRouter, Cerebras) the same put is
the whole onboarding door (td573668): ``endpoint={'base_url': ...}``
writes the account's own ``llm.profiles`` entry — binding the
``api_key_ref`` to its one provider host so a re-purposed key can never
route a vault secret elsewhere — ``limits`` records the published
free-tier rate limits on the account's pool rows, and
``entitlement.models.<id>`` + ``privacy.training_use`` carry price and
stance with their sources. That evidence contract is a property of the
stored row, not of the onboarding put — every put on an ``openai_compat``
account re-validates the merged row, so a later put can't land an
unsourced price or stance. A hosted account is created disabled; a
second put enables it. The profile entry is always ``kind='metered'`` —
never ``cap``, ``adapter``, ``gate`` or ``ops``. No routing behaviour
changes: ``pick_lane``, ``ladder``, ``resolve_profile`` and
``rate_limit.acquire``/``park``/``_record_refusal`` read the same rows
as before this migration.
"""

from __future__ import annotations

import json
import logging
import re
from datetime import UTC, datetime
from typing import Any, ClassVar
from urllib.parse import urlparse

from precis import llm_catalog, secret_status, settings
from precis.errors import BadInput, NotFound, Upstream
from precis.handlers._numeric_ref import NumericRefHandler
from precis.protocol import ArgsSpec, KindSpec
from precis.response import Response
from precis.store import Ref, Tag
from precis.todo.guards import is_owner
from precis.utils import handle_registry
from precis.utils.llm import accounts as llm_accounts
from precis.utils.llm.operations import LLM_OPERATIONS, OP_KEY_PREFIX
from precis.utils.timeutil import as_utc

log = logging.getLogger(__name__)

#: ``args={'account': {...}}`` validation vocab (S1a step 4). Mirrors the
#: migration's CHECK constraint so a reject here never diverges from what
#: the DB would refuse anyway.
_ACCOUNT_ADAPTERS: frozenset[str] = frozenset(
    {
        "claude_p",
        "claude_agent",
        "codex_exec",
        "devin_tree",
        "devin_api",
        "kimi_tree",
        "api",
        "openai_compat",
        "openai_tools",
        "local",
    }
)
_ACCOUNT_TASK_CLASSES: frozenset[str] = frozenset(
    {"*", "code", "review", "validate", "triage", "brief", "pcb", "docs", "skill"}
    # security-review (td574934 A2): an account row may LIST the class —
    # plans.boundary.pick_lane still refuses a non-SECURITY_LANE_ADAPTERS
    # lane for it, whatever the row says.
    | {"security-review"}
)
_ACCOUNT_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9\-]*$")
_ACCOUNT_POOL_KEY_RE = re.compile(r"^llm:[a-z0-9-]+:[a-z0-9]+$")
_ACCOUNT_CONCURRENCY_MIN = 1
_ACCOUNT_CONCURRENCY_MAX = 32

#: Columns this put merges, read-modify-write (whole-row replace, never a
#: jsonb patch) — ``name`` is the key, not a mergeable column.
_ACCOUNT_MERGE_COLS: tuple[str, ...] = (
    "vendor",
    "plan",
    "adapter",
    "profile",
    "api_key_ref",
    "concurrency",
    "task_classes",
    "models",
    "default_model",
    "enabled",
    "entitlement",
    "privacy",
    "promo_expiry",
    "pools",
)

#: Defaults for a column this put never saw before — a brand-new account
#: whose payload omits it. Mirrors the migration's table-level DEFAULTs.
_ACCOUNT_DEFAULTS: dict[str, Any] = {
    "profile": None,
    "api_key_ref": None,
    "concurrency": 1,
    "task_classes": (),
    "models": (),
    "default_model": None,
    "enabled": False,
    "entitlement": {},
    "privacy": {},
    "promo_expiry": None,
    "pools": (),
}

_ACCOUNT_UPSERT_SQL = """
INSERT INTO llm_account (
    name, vendor, plan, adapter, profile, api_key_ref, concurrency,
    task_classes, models, default_model, enabled, entitlement, privacy,
    promo_expiry, pools, updated_at
) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s::jsonb, %s::jsonb,
          %s, %s, now())
ON CONFLICT (name) DO UPDATE SET
    vendor = EXCLUDED.vendor,
    plan = EXCLUDED.plan,
    adapter = EXCLUDED.adapter,
    profile = EXCLUDED.profile,
    api_key_ref = EXCLUDED.api_key_ref,
    concurrency = EXCLUDED.concurrency,
    task_classes = EXCLUDED.task_classes,
    models = EXCLUDED.models,
    default_model = EXCLUDED.default_model,
    enabled = EXCLUDED.enabled,
    entitlement = EXCLUDED.entitlement,
    privacy = EXCLUDED.privacy,
    promo_expiry = EXCLUDED.promo_expiry,
    pools = EXCLUDED.pools,
    updated_at = now()
"""

#: A missing pool row is created in the rate-less carrier shape (0121's
#: seed shape): capacity 1 / refill 1.0 / tokens 1 / class 'llm-api' / no
#: daily cap — the same shape :func:`precis.utils.rate_limit.acquire` has
#: always granted against, so a brand-new pool key never changes routing.
_POOL_CARRIER_SEED_SQL = """
INSERT INTO external_rate_limits (provider, capacity, refill_per_sec, tokens,
                                   daily_cap, "class")
VALUES (%s, 1, 1.0, 1, NULL, 'llm-api')
ON CONFLICT (provider) DO NOTHING
"""

#: ``api_key_ref`` → the one endpoint host that vault secret may be sent
#: to on a hosted OpenAI-compatible account (``endpoint=`` on the account
#: put, td573668). The pair is what makes the door safe: a mistyped or
#: re-purposed key can never point a credential at another provider.
_ENDPOINT_KEY_HOSTS: dict[str, str] = {
    "GROQ_API_KEY": "api.groq.com",
    "OPENROUTER_API_KEY": "openrouter.ai",
    "CEREBRAS_API_KEY": "api.cerebras.ai",
}

#: ``privacy.training_use`` vocabulary a hosted account must record.
_TRAINING_USE_VALUES: frozenset[str] = frozenset({"no", "yes", "unknown"})

#: Units a ``limits`` entry may declare — the recurring-reset subset of
#: the column's CHECK constraint.
_LIMIT_UNITS: frozenset[str] = frozenset({"calls", "tokens"})

#: ``app_settings`` key of the provider-profile table an ``endpoint`` put
#: writes into (the JSON name → profile map ``resolve_profile`` reads).
_PROFILES_KEY = "llm.profiles"

#: A ``limits`` entry's pool row — the reporting columns an
#: owner-published free-tier limit carries. On conflict only those
#: columns update: ``used_reported`` / ``reported_at`` / ``resets_at`` /
#: ``parked_until`` are usage evidence the owner cannot know and the put
#: never touches.
_POOL_LIMIT_UPSERT_SQL = """
INSERT INTO external_rate_limits (provider, capacity, refill_per_sec, tokens,
                                   daily_cap, "class", unit, limit_reported,
                                   reset_rule, source, confidence)
VALUES (%s, 1, 1.0, 1, NULL, 'llm-api', %s, %s, %s, 'user', 'published')
ON CONFLICT (provider) DO UPDATE SET
    unit = EXCLUDED.unit,
    limit_reported = EXCLUDED.limit_reported,
    reset_rule = EXCLUDED.reset_rule,
    source = EXCLUDED.source,
    confidence = EXCLUDED.confidence
"""

_PROFILES_UPSERT_SQL = """
INSERT INTO app_settings (key, value, updated_at, updated_by)
VALUES (%s, %s, now(), %s)
ON CONFLICT (key) DO UPDATE SET
    value = EXCLUDED.value, updated_at = now(), updated_by = EXCLUDED.updated_by
"""

#: Seed-then-lock for the ``llm.profiles`` row: ``SELECT … FOR UPDATE``
#: on an absent row locks nothing, so two concurrent first creates could
#: each read an empty map and have their upserts overwrite one another —
#: one account lands without its profile (td573741). Inserting the row
#: first pins it so the lock serializes the second create on the first.
_PROFILES_SEED_SQL = """
INSERT INTO app_settings (key, value, updated_at, updated_by)
VALUES (%s, '{}', now(), %s)
ON CONFLICT (key) DO NOTHING
"""


def _https_source(value: Any) -> bool:
    """An evidence source URL — https only, like every fetched reference."""
    return isinstance(value, str) and value.startswith("https://")


def _iso_stamp(value: Any) -> bool:
    """A ``verified_at``-style ISO timestamp."""
    if not isinstance(value, str):
        return False
    try:
        datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return False
    return True


class LlmHandler(NumericRefHandler):
    spec: ClassVar[KindSpec] = KindSpec(
        kind="llm",
        title="LLM catalog",
        description=(
            "A model catalog card — one ref per model. Body is the capability "
            "prose (embedded, so the card is a vector); meta carries the facts "
            "(model_id, tier_floor, offerings, capability axes, provenance). Read "
            "with get(kind='llm', id='claude-opus-5-5') or search(kind='llm', "
            "q='careful SQL'). The llm_reconcile pass mints + refreshes cards "
            "against the live OpenRouter feed and flags drift. "
            "get(kind='llm', id='/placement') reports the local vs cloud share "
            "of logged calls, routed vs landed, per tier and day; "
            "get(kind='llm', id='op:<source>', view='stats') rolls up "
            "llm_call_log per calling op. get(kind='llm', id='/ops') lists "
            "operation routes; id='<source>' reads the stored route. "
            "get(kind='llm', id='/accounts') lists owned LLM credentials "
            "(name/vendor/plan/adapter/key presence/pools); id='/pools' "
            "lists their rate buckets (unit/limit/used/remaining/reset_rule). "
            "id='/decisions' compares shadow decisions with executed routes per task kind. "
            "id='/suitability' shows per-offering outcome posteriors "
            "(label/n/interval) for the ten busiest task kinds. "
            "id='/preview' with args={'task_kind': '<kind>'} lists the "
            "candidates the router would rank for that kind, the chosen one "
            "marked; no LLM call. "
            "id='/health' shows error rate and p95 latency by executed "
            "path/model/transport (args={'window_days': N}); "
            "id='/decide-modes' the decide mode in force per task kind; "
            "id='/breaker' whether the budget breaker is tripped and what "
            "resumes it. "
            "put(kind='llm', args={'account': {...}}) is owner-only — for a "
            "hosted OpenAI-compatible provider, endpoint/limits/entitlement/"
            "privacy evidence makes it the whole onboarding door (created "
            "disabled; a second put enables). "
            "Never exported. See ``llm-catalog`` (git-only)."
        ),
        supports_get=True,
        supports_search=True,
        supports_search_hits=True,
        supports_put=True,
        supports_delete=True,
        supports_tag=True,
        supports_link=True,
        is_numeric=True,
        id_required=False,
        note_like=True,
    )

    kind: ClassVar[str] = "llm"
    sense: ClassVar[str] = "llm card"

    #: ``put(kind='llm', args={'account': {...}})`` — the owner-only
    #: account/pool write (S1a step 4). ``get`` reads ``args=`` for
    #: ``id='/preview'`` (the demand whose routing is previewed) and
    #: ``id='/health'`` (``window_days``, the call-log window).
    _PREVIEW_ARGS: ClassVar[dict[str, type]] = {
        "task_kind": str,
        "source": str,
        "tier": str,
        "tools": bool,
        "privacy": str,
        "work_class": str,
    }
    ARGS: ClassVar[dict[str, ArgsSpec]] = {
        "put": ArgsSpec({"account"}),
        "get": ArgsSpec({**_PREVIEW_ARGS, "window_days": int}),
    }

    #: The prose embeds so the card is a vector (search-by-capability). Emission
    #: itself runs through :func:`precis.llm_catalog.upsert_card`, not the base
    #: ``_create`` path — the flag documents the intent + keeps the base path
    #: correct if ever exercised.
    emits_card: ClassVar[bool] = True

    # ── id resolution: model slug, numeric id, or the lm handle ─────

    def _find_resolve_ref(self, id: Any) -> Ref:
        return self._ref_for(id)

    def _ref_for(self, id: str | int) -> Ref:
        """Resolve ``id`` → a live ref, accepting a **model slug**
        (``claude-opus-5-5`` — the human key, looked up via ``meta.model_id``), a
        bare int, or ``llm:<int>``. The ``lm`` handle is decoded to a numeric
        public id by the runtime before it reaches here.
        """
        if isinstance(id, str):
            s = id.strip()
            core = s[len("llm:") :] if s.startswith("llm:") else s
            if not core.isdigit():
                ref = self.store.find_ref_by_meta(
                    kind=self.kind, key="model_id", value=s
                )
                if ref is None:
                    raise self._no_card(s)
                return ref
        return self._resolve_live_ref(self._coerce_id(id))

    def _no_card(self, slug: str) -> NotFound:
        """The NotFound for an unresolvable model slug (gr581238).

        A bare provider name (``openrouter``, ``groq``) is not a model id —
        when the slug names an ``llm_account`` vendor or account the refusal
        says so and points at the provider read. Either way the ``next=`` is
        a copy-pasteable call carrying the asked-for slug: a literal
        ``q='...'`` placeholder is prose, not a call that works.
        """
        core = slug[len("llm:") :] if slug.startswith("llm:") else slug
        if self._names_account(core):
            return NotFound(
                f"no llm card for model {slug!r} — {core!r} is a "
                "provider/account, not a model id",
                next=[
                    "get(kind='llm', id='/accounts') for that provider's account",
                    f"search(kind='llm', q={slug!r}) to find models mentioning it",
                ],
            )
        return NotFound(
            f"no llm card for model {slug!r}",
            next=f"search(kind='llm', q={slug!r}) to find models",
        )

    def _names_account(self, name: str) -> bool:
        """``name`` is an ``llm_account`` name or vendor (case-folded)."""
        lowered = name.lower()
        with self.store.pool.connection() as conn:
            row = conn.execute(
                "SELECT 1 FROM llm_account WHERE name = %s OR vendor = %s LIMIT 1",
                (lowered, lowered),
            ).fetchone()
        return row is not None

    # ── get: slug resolution + the tote / reviews views ─────────────

    def get(
        self,
        *,
        id: str | int | list[str | int] | None = None,
        view: str | None = None,
        q: str | None = None,
        args: dict[str, Any] | None = None,
        **_kw: Any,
    ) -> Response:
        if (r := self._route_find(id=id, view=view, q=q)) is not None:
            return r
        if isinstance(id, str) and id.strip() == "/preview":
            self._check_list_id_view(id, view)
            return Response(body=self._render_preview(args or {}))
        if isinstance(id, str) and id.strip() == "/health":
            self._check_list_id_view(id, view)
            # The early return bypasses the base ``/<x>`` dispatch so
            # ``args={'window_days'}`` reaches the render — run the same
            # paging-selector gate it would, else ``page_size=``/``sort=``
            # would silently succeed (review td760794).
            self._check_list_selectors(
                "health",
                _kw.get("page"),
                _kw.get("page_size"),
                _kw.get("sort"),
                _kw.get("closed_since"),
                _kw.get("closed_until"),
            )
            return Response(body=self._render_health(args or {}))
        if args:
            raise BadInput(
                f"args= keys {sorted(args)} apply to id='/preview' or "
                "id='/health' only",
                options=[
                    "get(kind='llm', id='/preview', args={'task_kind': '<task kind>'})",
                    "get(kind='llm', id='/health', args={'window_days': 7})",
                ],
            )
        if isinstance(id, list):
            return super().get(id=id, view=view, q=q, **_kw)  # refused: no batch form
        # ``id='op:<source>'`` — per-op ``llm_call_log`` stats (gr502653):
        # the source names the calling op (e.g. ``op:graph_garden:propose``).
        if isinstance(id, str) and id.strip().startswith("op:"):
            source = id.strip()[3:].strip()
            if not source:
                raise BadInput(
                    "op: needs the llm_call_log source name",
                    next="get(kind='llm', id='op:<source>', view='stats')",
                )
            if view not in (None, "stats"):
                raise BadInput(
                    f"view={view!r} doesn't apply to an op: read",
                    options=["stats"],
                    next=f"get(kind='llm', id='op:{source}', view='stats')",
                )
            return Response(body=self._render_op_stats(source))
        concrete = id is not None and not (isinstance(id, str) and id.startswith("/"))
        if concrete and view in ("tote", "reviews"):
            assert id is not None  # `concrete` already guarantees this
            ref = self._ref_for(id)
            if view == "tote":
                return Response(body=self._render_tote(ref))
            return Response(body=self._render_reviews(ref))
        # Resolve a model slug to the card's ref before the base handler's
        # integer coercion; path views (``/recent``) and numeric ids fall through.
        if isinstance(id, str):
            s = id.strip()
            core = s[len("llm:") :] if s.startswith("llm:") else s
            if not s.startswith("/") and not core.isdigit():
                route = self._op_route(s)
                if route is not None:
                    if view is not None or any(v is not None for v in _kw.values()):
                        raise BadInput(
                            "view= and card selectors don't apply to a stored op route",
                            next=f"get(kind='llm', id={s!r})",
                        )
                    return route
                ref = self._ref_for(id)
                return super().get(id=ref.id, view=view, q=q, **_kw)
        return super().get(id=id, view=view, q=q, **_kw)

    def _list_view(self, view: str) -> Response | None:
        if view == "ops":
            return self._op_routes()
        if view == "placement":
            return Response(body=self._render_placement())
        if view == "accounts":
            return Response(body=self._render_accounts())
        if view == "pools":
            return Response(body=self._render_pools())
        if view == "decisions":
            return Response(body=self._render_decisions())
        if view == "suitability":
            return Response(body=self._render_suitability())
        if view == "health":
            return Response(body=self._render_health({}))
        if view == "decide-modes":
            return Response(body=self._render_decide_modes())
        if view == "breaker":
            return Response(body=self._render_breaker())
        return super()._list_view(view)

    def _supported_list_views(self) -> tuple[str, ...]:
        return (
            *super()._supported_list_views(),
            "ops",
            "placement",
            "accounts",
            "pools",
            "decisions",
            "suitability",
            "preview",
            "health",
            "decide-modes",
            "breaker",
        )

    def _op_route(self, source: str) -> Response | None:
        with self.store.pool.connection() as conn:
            row = conn.execute(
                "SELECT value, updated_at FROM app_settings WHERE key = %s",
                (OP_KEY_PREFIX + source,),
            ).fetchone()
        if row is None and source not in LLM_OPERATIONS:
            return None
        lines = [f"# llm op route - {source}"]
        default = LLM_OPERATIONS.get(source)
        if default is not None:
            lines.append(
                f"code default: tier={default.tier.value} "
                f"model={default.model or '(tier default)'} "
                f"quota_fallback={default.quota_fallback}"
            )
        else:
            lines.append("unregistered operation: stored override is inert")
        if row is None:
            lines.append("no stored override; last set: never")
        else:
            stamp = as_utc(row[1])
            lines.append(
                "last set: " + (stamp.strftime("%Y-%m-%dT%H:%M:%SZ") if stamp else "?")
            )
            try:
                override = json.loads(row[0])
            except (TypeError, ValueError):
                override = None
            if not isinstance(override, dict):
                lines.append("invalid stored override: expected a JSON object")
            else:
                for key in ("tier", "model", "quota_fallback"):
                    if key in override:
                        lines.append(f"{key}={override[key]}")
                chain = override.get("chain")
                if chain is None:
                    lines.append("chain: not stored (inherits tier chain)")
                elif not isinstance(chain, list) or not chain:
                    lines.append("invalid stored chain: expected a non-empty list")
                else:
                    lines.append("stored chain (in order):")
                    for i, rung in enumerate(chain):
                        role = "primary" if i == 0 else "fallback"
                        # Routing knobs only: endpoint/auth fields never render.
                        fields = (
                            "placement",
                            "transport",
                            "model",
                            "max_tokens",
                            "reasoning_effort",
                            "thinking",
                            "effort",
                            "timeout_s",
                            "bare",
                            "profile",
                        )
                        detail = (
                            " ".join(f"{k}={rung[k]}" for k in fields if k in rung)
                            if isinstance(rung, dict)
                            else "(invalid rung: expected an object)"
                        )
                        lines.append(f"  {i + 1}. {role}: {detail}")
        lines.append(
            "Stored configuration only; request pins, tier chains and runtime "
            "gates may change the route. No LLM call made."
        )
        return Response(body="\n".join(lines))

    def _op_routes(self) -> Response:
        with self.store.pool.connection() as conn:
            rows = conn.execute(
                "SELECT key, updated_at FROM app_settings WHERE starts_with(key, %s) "
                "ORDER BY key",
                (OP_KEY_PREFIX,),
            ).fetchall()
        stored = {key[len(OP_KEY_PREFIX) :]: stamp for key, stamp in rows}
        sources = sorted(set(LLM_OPERATIONS) | set(stored))
        lines = [f"# llm op routes ({len(sources)})"]
        for source in sources:
            state = "registered" if source in LLM_OPERATIONS else "unregistered (inert)"
            stamp = as_utc(stored.get(source))
            when = stamp.strftime("%Y-%m-%dT%H:%M:%SZ") if stamp else "never"
            lines.append(f"  {source}: {state}; last set: {when}")
        lines.append("Read a stored chain: get(kind='llm', id='<source>')")
        return Response(body="\n".join(lines), result_count=len(sources))

    # ── put: guarded funnel to the shared catalog writer ────────────

    def put(
        self,
        *,
        id: str | int | None = None,
        text: str | None = None,
        model_id: str | None = None,
        tier_floor: str | None = None,
        offerings: Any = None,
        capability: Any = None,
        served_by: Any = None,
        provenance: Any = None,
        entry: str | None = None,
        by: str | None = None,
        tags: list[str] | None = None,
        link: str | None = None,
        rel: str | None = None,
        args: dict[str, Any] | None = None,
        **_kw: Any,
    ) -> Response:
        # ``put(args={'account': {...}})`` — the owner-only account/pool
        # write (S1a). Checked before the id-presence branch: an account
        # write carries no id= at all.
        if args is not None and "account" in args:
            return self._put_account(args["account"])
        # ``put(id=…, text=…, args={'entry': …})`` appends a WORM review entry to a card —
        # the quest-logbook idiom (the ledger layer). The base create-only guard
        # rejects id-presence, so we intercept before delegating.
        if id is not None:
            if not (text and text.strip()):
                raise BadInput(
                    f"appending a review to an {self._sense()} requires text=",
                    next=(
                        f"put(kind={self.kind!r}, id={id!r}, text='...', "
                        "args={'entry': 'measured-eval', 'by': 'agent'})"
                    ),
                )
            review_type = (entry or llm_catalog.DEFAULT_REVIEW_TYPE).strip().lower()
            if review_type not in llm_catalog.REVIEW_TYPES:
                raise BadInput(
                    f"unknown review type {entry!r}",
                    options=sorted(llm_catalog.REVIEW_TYPES),
                    next=(
                        "entry= is one of: "
                        + ", ".join(sorted(llm_catalog.REVIEW_TYPES))
                    ),
                )
            ref = self._ref_for(id)
            prov = provenance if isinstance(provenance, str) else None
            n = llm_catalog.append_review(
                self.store,
                ref.id,
                text=text,
                review_type=review_type,
                by=(by or "agent"),
                provenance=prov,
            )
            handle = handle_registry.try_format(self.kind, ref.id) or f"id={ref.id}"
            return Response(
                body=f"logged {review_type} review on {self.kind} {handle} (entry {n})."
            )
        if not model_id:
            raise BadInput(
                f"creating an {self._sense()} requires model_id= (the canonical "
                "model slug)",
                next=(
                    "the llm_reconcile pass mints cards automatically; a manual "
                    "create passes model_id= + text= (the capability prose)"
                ),
            )
        ref_id, created = llm_catalog.upsert_card(
            self.store,
            model_id=model_id,
            text=text or "",
            tier_floor=tier_floor,
            offerings=offerings,
            capability=capability,
            served_by=served_by,
            provenance=provenance,
        )
        handle = handle_registry.try_format(self.kind, ref_id) or f"id={ref_id}"
        verb = "created" if created else "refreshed"
        return Response(body=f"{verb} {self.kind} {handle} for model {model_id!r}.")

    # ── put: owner-only account/pool write (S1a) ────────────────────

    def _put_account(self, payload: Any) -> Response:
        """``put(kind='llm', args={'account': {...}})`` — owner-only
        read-modify-write upsert of one ``llm_account`` row, plus a carrier
        pool row for every key in its ``pools=`` that doesn't exist yet.

        Whole-row replace, never a jsonb patch: a column the payload omits
        keeps its current stored value on an update, or its table default
        on a first create — it is never merged key-by-key into
        ``entitlement``/``privacy``.
        """
        if not is_owner():
            raise BadInput(
                "writing an llm_account is owner-only",
                next="run this put from the CLI or a web:owner session, "
                "not an asa-* worker source",
            )
        if not isinstance(payload, dict):
            raise BadInput(
                "args={'account': {...}} must be an object",
                next="put(kind='llm', args={'account': "
                "{'name': 'my-account', 'vendor': ..., 'plan': ..., "
                "'adapter': ...}})",
            )
        if "base_url" in payload:
            raise BadInput(
                "llm_account carries no base_url — that lives on the "
                "llm.profiles entry an HTTP account names via profile=",
                next="pass endpoint={'base_url': 'https://<provider host>/v1'} "
                "— the door writes the llm.profiles entry itself",
            )
        allowed_keys = frozenset(_ACCOUNT_MERGE_COLS) | {
            "name",
            "endpoint",
            "limits",
        }
        unknown_keys = sorted(set(payload) - allowed_keys)
        if unknown_keys:
            raise BadInput(
                f"account payload key(s) {unknown_keys} are not recognized",
                options=sorted(allowed_keys),
                next=f"drop {unknown_keys} — accepted keys are {sorted(allowed_keys)}",
            )
        name = payload.get("name")
        if not isinstance(name, str) or not _ACCOUNT_NAME_RE.match(name):
            raise BadInput(
                f"account name {name!r} must be a lowercase slug ([a-z0-9][a-z0-9-]*)",
                next="args={'account': {'name': 'claude-sub', ...}}",
            )
        existing = llm_accounts.get_account(self.store, name)
        if existing is None:
            missing = [k for k in ("vendor", "plan", "adapter") if not payload.get(k)]
            if missing:
                raise BadInput(
                    f"creating llm_account {name!r} needs {missing}",
                    next="args={'account': {'name': "
                    f"{name!r}, 'vendor': ..., 'plan': ..., 'adapter': ...}}",
                )
        merged: dict[str, Any] = {}
        for col in _ACCOUNT_MERGE_COLS:
            if col in payload:
                merged[col] = payload[col]
            elif existing is not None:
                merged[col] = getattr(existing, col)
            else:
                merged[col] = _ACCOUNT_DEFAULTS[col]

        if merged["adapter"] not in _ACCOUNT_ADAPTERS:
            raise BadInput(
                f"adapter {merged['adapter']!r} is not a known adapter",
                options=sorted(_ACCOUNT_ADAPTERS),
                next=f"args={{'account': {{'adapter': {sorted(_ACCOUNT_ADAPTERS)[0]!r}}}}}",
            )
        api_key_ref = merged["api_key_ref"]
        if api_key_ref is not None:
            known = {s.name for s in secret_status.KNOWN_SECRETS}
            if api_key_ref not in known:
                raise BadInput(
                    f"api_key_ref {api_key_ref!r} is not a registered secret",
                    next="register it in precis.secret_status.KNOWN_SECRETS "
                    "first, or omit api_key_ref for a per-host login",
                )
        concurrency = merged["concurrency"]
        if (
            isinstance(concurrency, bool)
            or not isinstance(concurrency, int)
            or not (_ACCOUNT_CONCURRENCY_MIN <= concurrency <= _ACCOUNT_CONCURRENCY_MAX)
        ):
            raise BadInput(
                f"concurrency={concurrency!r} must be an integer "
                f"{_ACCOUNT_CONCURRENCY_MIN}-{_ACCOUNT_CONCURRENCY_MAX}",
                next="args={'account': {'concurrency': 1}}",
            )
        task_classes = merged["task_classes"] or ()
        if not isinstance(task_classes, (list, tuple)) or not all(
            isinstance(t, str) for t in task_classes
        ):
            raise BadInput(
                "task_classes must be a list of strings",
                next="args={'account': {'task_classes': ['code']}}",
            )
        unknown_classes = sorted(set(task_classes) - _ACCOUNT_TASK_CLASSES)
        if unknown_classes:
            raise BadInput(
                f"task_classes {unknown_classes} are not known task classes",
                options=sorted(_ACCOUNT_TASK_CLASSES),
                next=f"args={{'account': {{'task_classes': "
                f"{sorted(_ACCOUNT_TASK_CLASSES)}}}}} (pick a subset)",
            )
        pools = merged["pools"] or ()
        if not isinstance(pools, (list, tuple)) or not all(
            isinstance(p, str) for p in pools
        ):
            raise BadInput(
                "pools must be a list of strings",
                next="args={'account': {'pools': ['llm:my-account:weekly']}}",
            )
        bad_pools = sorted(p for p in pools if not _ACCOUNT_POOL_KEY_RE.match(p))
        if bad_pools:
            raise BadInput(
                f"pool key(s) {bad_pools} don't match llm:<account>:<window>",
                next="args={'account': {'pools': ['llm:my-account:weekly']}}",
            )
        models = merged["models"] or ()
        if not isinstance(models, (list, tuple)) or not all(
            isinstance(m, str) for m in models
        ):
            raise BadInput(
                "models must be a list of strings",
                next="args={'account': {'models': ['model-id']}}",
            )
        entitlement = merged["entitlement"] or {}
        privacy = merged["privacy"] or {}
        if not isinstance(entitlement, dict) or not isinstance(privacy, dict):
            raise BadInput(
                "entitlement and privacy must be objects",
                next="args={'account': {'entitlement': {}, 'privacy': {}}}",
            )
        default_model = merged["default_model"]
        if default_model is not None and default_model not in models:
            raise BadInput(
                f"default_model {default_model!r} is not in models {list(models)}",
                next="add it to models=, or drop default_model",
            )

        # ── the hosted-provider door (td573668) ──
        # ``endpoint`` writes the account's own llm.profiles entry, binding
        # its api_key_ref to the one provider host that secret may reach.
        endpoint_entry: dict[str, Any] | None = None
        endpoint = payload.get("endpoint")
        if endpoint is not None:
            if (
                not isinstance(endpoint, dict)
                or set(endpoint) != {"base_url"}
                or not isinstance(endpoint.get("base_url"), str)
            ):
                raise BadInput(
                    "endpoint must be an object carrying only a base_url string",
                    next="args={'account': {'endpoint': {'base_url': "
                    "'https://api.groq.com/openai/v1'}}}",
                )
            if merged["adapter"] != "openai_compat":
                raise BadInput(
                    "endpoint= applies only to adapter 'openai_compat'",
                    options=["openai_compat"],
                    next="args={'account': {'adapter': 'openai_compat', "
                    "'endpoint': {'base_url': 'https://<provider host>/v1'}}}",
                )
            if payload.get("profile") not in (None, name):
                raise BadInput(
                    "a hosted account's profile is its own name — the door "
                    "writes the llm.profiles entry itself",
                    next="drop profile= (or pass the account name)",
                )
            key_ref = merged["api_key_ref"]
            if key_ref not in _ENDPOINT_KEY_HOSTS:
                raise BadInput(
                    f"an endpoint account needs an api_key_ref from "
                    f"{sorted(_ENDPOINT_KEY_HOSTS)}",
                    options=sorted(_ENDPOINT_KEY_HOSTS),
                    next="name the provider's vault secret in api_key_ref",
                )
            parsed = urlparse(endpoint["base_url"])
            if (
                parsed.scheme != "https"
                or parsed.hostname != _ENDPOINT_KEY_HOSTS[key_ref]
            ):
                raise BadInput(
                    f"endpoint base_url {endpoint['base_url']!r} must be https "
                    f"on host {_ENDPOINT_KEY_HOSTS[key_ref]!r} for {key_ref}",
                    next=f"endpoint={{'base_url': "
                    f"'https://{_ENDPOINT_KEY_HOSTS[key_ref]}/<path>'}}",
                )
            merged["profile"] = name
            endpoint_entry = {
                "base_url": endpoint["base_url"],
                "api_key_ref": key_ref,
                "kind": "metered",
            }

        if (
            existing is None
            and merged["adapter"] == "openai_compat"
            and endpoint is None
        ):
            raise BadInput(
                "an openai_compat account is created through the endpoint door",
                next="add endpoint={'base_url': 'https://<provider host>/v1'} "
                "plus api_key_ref, privacy, entitlement and limits evidence",
            )
        if existing is None and endpoint is not None and merged["enabled"]:
            raise BadInput(
                "a hosted account is created disabled",
                next="create it, then enable with "
                f"args={{'account': {{'name': {name!r}, 'enabled': True}}}} "
                "once the key is in the vault",
            )
        if merged["adapter"] == "openai_compat":
            # The door's evidence contract is a property of the stored row,
            # not of the onboarding put: validate what this put would store
            # so a later put can't land an unsourced price or stance
            # (td573741). Every openai_compat account is door-created, so a
            # compliant merged row is always reachable.
            stance = merged["privacy"] or {}
            if stance.get("training_use") not in _TRAINING_USE_VALUES:
                raise BadInput(
                    "an openai_compat account records privacy.training_use as "
                    "'no', 'yes' or 'unknown'",
                    options=sorted(_TRAINING_USE_VALUES),
                    next="args={'account': {'privacy': {'training_use': 'no', "
                    "'source_url': 'https://<doc>', 'verified_at': '<ISO>'}}}",
                )
            if not _https_source(stance.get("source_url")):
                raise BadInput(
                    "privacy.source_url must be an https:// source URL for "
                    "the recorded stance",
                    next="args={'account': {'privacy': {'training_use': 'no', "
                    "'source_url': 'https://<doc>', 'verified_at': '<ISO>'}}}",
                )
            if not _iso_stamp(stance.get("verified_at")):
                raise BadInput(
                    "privacy.verified_at must be an ISO timestamp for the "
                    "recorded stance",
                    next="args={'account': {'privacy': {'training_use': 'no', "
                    "'source_url': 'https://<doc>', 'verified_at': '<ISO>'}}}",
                )
            ent_models = (merged["entitlement"] or {}).get("models") or {}
            if not isinstance(ent_models, dict):
                raise BadInput(
                    "entitlement.models must be an object of model-id → "
                    "{price_in, price_out, source_url, verified_at}",
                    next="args={'account': {'entitlement': {'models': "
                    "{'m': {'price_in': 0, 'price_out': 0, "
                    "'source_url': 'https://<doc>', 'verified_at': '<ISO>'}}}}}",
                )
            for model_id, spec in ent_models.items():
                if not isinstance(spec, dict):
                    raise BadInput(
                        f"entitlement.models.{model_id} must be an object",
                        next="{price_in, price_out, source_url, verified_at}",
                    )
                for field in ("price_in", "price_out"):
                    value = spec.get(field)
                    if (
                        isinstance(value, bool)
                        or not isinstance(value, (int, float))
                        or value < 0
                    ):
                        raise BadInput(
                            f"entitlement.models.{model_id}.{field} must be a "
                            "non-negative number (USD per 1M tokens)",
                            next=f"args={{'account': {{'entitlement': {{'models': "
                            f"{{{model_id!r}: {{'{field}': 0.5}}}}}}}}}} "
                            "(USD per 1M tokens, >= 0)",
                        )
                if not _https_source(spec.get("source_url")):
                    raise BadInput(
                        f"entitlement.models.{model_id}.source_url must be an "
                        "https:// source URL",
                        next=f"args={{'account': {{'entitlement': {{'models': "
                        f"{{{model_id!r}: {{'source_url': 'https://<pricing doc>'}}}}}}}}}}",
                    )
                if not _iso_stamp(spec.get("verified_at")):
                    raise BadInput(
                        f"entitlement.models.{model_id}.verified_at must be an "
                        "ISO timestamp",
                        next=f"args={{'account': {{'entitlement': {{'models': "
                        f"{{{model_id!r}: {{'verified_at': '2026-01-01T00:00:00Z'}}}}}}}}}}",
                    )

        # ── free-tier rate limits → pool rows ──
        limit_rows: list[dict[str, Any]] = []
        limits = payload.get("limits")
        if limits is not None:
            if not isinstance(limits, list) or not all(
                isinstance(entry, dict) for entry in limits
            ):
                raise BadInput(
                    "limits must be a list of {pool, unit, limit, reset_rule, "
                    "source_url, verified_at} objects",
                    next=f"args={{'account': {{'limits': [{{'pool': "
                    f"'llm:{name}:rpm', 'unit': 'calls', 'limit': 30, "
                    "'reset_rule': 'rolling:60', 'source_url': 'https://…', "
                    "'verified_at': '<ISO>'}}]}}}}",
                )
            pool_re = re.compile(rf"^llm:{re.escape(name)}:[a-z0-9]+$")
            for entry in limits:
                pool = entry.get("pool")
                if not isinstance(pool, str) or not pool_re.match(pool):
                    raise BadInput(
                        f"limit pool {pool!r} must be llm:{name}:<window> "
                        "([a-z0-9]+ window)",
                        next=f"pool 'llm:{name}:rpm'",
                    )
                if entry.get("unit") not in _LIMIT_UNITS:
                    raise BadInput(
                        f"limit unit {entry.get('unit')!r} is not a limit unit",
                        options=sorted(_LIMIT_UNITS),
                    )
                limit = entry.get("limit")
                if (
                    isinstance(limit, bool)
                    or not isinstance(limit, (int, float))
                    or limit <= 0
                ):
                    raise BadInput(
                        f"limit {limit!r} must be a positive number",
                        next=f"args={{'account': {{'limits': [{{'pool': "
                        f"'llm:{name}:rpm', 'limit': 30}}]}}}}",
                    )
                rule_text = entry.get("reset_rule")
                try:
                    rule = llm_accounts.parse_reset_rule(
                        rule_text if isinstance(rule_text, str) else ""
                    )
                except ValueError as exc:
                    raise BadInput(
                        str(exc),
                        next="args={'account': {'limits': [{'reset_rule': "
                        "'rolling:60'}]}} (or 'calendar:daily:00:00@UTC')",
                    ) from exc
                if not isinstance(
                    rule, (llm_accounts.RollingReset, llm_accounts.CalendarReset)
                ):
                    raise BadInput(
                        "a free-tier limit needs a recurring reset_rule "
                        "(rolling: or calendar:), never expires:",
                        next="args={'account': {'limits': "
                        "[{'reset_rule': 'rolling:60'}]}}",
                    )
                if not _https_source(entry.get("source_url")):
                    raise BadInput(
                        "a limits entry needs an https:// source_url",
                        next="args={'account': {'limits': "
                        "[{'source_url': 'https://<rate-limit doc>'}]}}",
                    )
                if not _iso_stamp(entry.get("verified_at")):
                    raise BadInput(
                        "a limits entry needs an ISO verified_at",
                        next="args={'account': {'limits': "
                        "[{'verified_at': '2026-01-01T00:00:00Z'}]}}",
                    )
                limit_rows.append(
                    {
                        "pool": pool,
                        "unit": entry["unit"],
                        "limit": limit,
                        "reset_rule": llm_accounts.format_reset_rule(rule),
                        "source_url": entry["source_url"],
                        "verified_at": entry["verified_at"],
                    }
                )
            pools = tuple(dict.fromkeys([*pools, *(e["pool"] for e in limit_rows)]))
            merged["pools"] = pools
            recorded = dict(entitlement.get("limits") or {})
            for e in limit_rows:
                recorded[e["pool"]] = {
                    "unit": e["unit"],
                    "limit": e["limit"],
                    "source_url": e["source_url"],
                    "verified_at": e["verified_at"],
                }
            entitlement = {**entitlement, "limits": recorded}

        enabling = bool(merged["enabled"]) and merged["adapter"] == "openai_compat"
        with self.store.tx() as conn:
            if endpoint_entry is not None or enabling:
                conn.execute(
                    _PROFILES_SEED_SQL,
                    (_PROFILES_KEY, "put:llm.account"),
                )
                row = conn.execute(
                    "SELECT value FROM app_settings WHERE key = %s FOR UPDATE",
                    (_PROFILES_KEY,),
                ).fetchone()
                try:
                    profiles = json.loads(row[0]) if row else {}
                except (TypeError, ValueError):
                    profiles = {}
                if not isinstance(profiles, dict):
                    profiles = {}
                if endpoint_entry is not None:
                    current = profiles.get(name)
                    if current is not None and current != endpoint_entry:
                        raise BadInput(
                            f"llm.profiles already holds a different {name!r} "
                            "entry — the door never overwrites it",
                            next="fix or remove the existing llm.profiles "
                            f"entry for {name!r} first",
                        )
                    if current is None:
                        profiles[name] = endpoint_entry
                        conn.execute(
                            _PROFILES_UPSERT_SQL,
                            (
                                _PROFILES_KEY,
                                json.dumps(profiles, sort_keys=True),
                                "put:llm.account",
                            ),
                        )
                if enabling:
                    profile_name = merged["profile"]
                    if (
                        not isinstance(profile_name, str)
                        or profile_name not in profiles
                    ):
                        raise BadInput(
                            f"enabling {name!r} needs an llm.profiles entry "
                            f"named {profile_name!r}",
                            next="create the account with endpoint= — the "
                            "door writes its own profile entry",
                        )
                    if (merged["privacy"] or {}).get(
                        "training_use"
                    ) not in _TRAINING_USE_VALUES:
                        raise BadInput(
                            f"enabling {name!r} needs privacy.training_use "
                            "recorded ('no', 'yes' or 'unknown')",
                            options=sorted(_TRAINING_USE_VALUES),
                            next="args={'account': {'privacy': {'training_use': "
                            "'no', 'source_url': 'https://<doc>', "
                            "'verified_at': '<ISO>'}}}",
                        )
                    if not merged["models"]:
                        raise BadInput(
                            f"enabling {name!r} needs at least one model in models=",
                            next=f"args={{'account': {{'name': {name!r}, "
                            "'models': ['model-id'], 'default_model': 'model-id'}}}}",
                        )
            conn.execute(
                _ACCOUNT_UPSERT_SQL,
                (
                    name,
                    merged["vendor"],
                    merged["plan"],
                    merged["adapter"],
                    merged["profile"],
                    api_key_ref,
                    concurrency,
                    list(task_classes),
                    list(models),
                    default_model,
                    bool(merged["enabled"]),
                    json.dumps(entitlement),
                    json.dumps(privacy),
                    merged["promo_expiry"],
                    list(pools),
                ),
            )
            for pool_key in pools:
                conn.execute(_POOL_CARRIER_SEED_SQL, (pool_key,))
            for e in limit_rows:
                conn.execute(
                    _POOL_LIMIT_UPSERT_SQL,
                    (e["pool"], e["unit"], e["limit"], e["reset_rule"]),
                )
        if endpoint_entry is not None:
            from precis.utils.llm import live_config

            settings.invalidate(_PROFILES_KEY)
            live_config.bust_cache()
        verb = "created" if existing is None else "updated"
        return Response(
            body=f"{verb} llm_account {name!r} "
            f"(adapter={merged['adapter']}, enabled={bool(merged['enabled'])}, "
            f"pools={list(pools)})."
        )

    # ── rendering ────────────────────────────────────────────────────

    def _render_one(self, ref: Ref, tags: list[Tag]) -> str:
        meta = ref.meta or {}
        model_id = meta.get("model_id", "?")
        handle = handle_registry.try_format(self.kind, ref.id) or f"llm:{ref.id}"
        lines = [f"# {handle} — {model_id}"]
        if meta.get("tier_floor"):
            lines.append(f"tier floor: {meta['tier_floor']}")
        params = meta.get("params") or {}
        if params:
            lines.append("params: " + ", ".join(f"{k}={v}" for k, v in params.items()))

        offerings = meta.get("offerings") or []
        if offerings:
            lines += ["", f"## offerings ({len(offerings)})"]
            for o in offerings:
                bits: list[str] = []
                if o.get("effort"):
                    bits.append(f"effort={o['effort']}")
                if o.get("transport"):
                    bits.append(f"via {o['transport']}")
                if o.get("max_input") is not None:
                    bits.append(f"in≤{o['max_input']}")
                if o.get("max_output") is not None:
                    bits.append(f"out≤{o['max_output']}")
                if o.get("price_in") is not None or o.get("price_out") is not None:
                    bits.append(f"${o.get('price_in')}/${o.get('price_out')} per 1M")
                if o.get("quant"):
                    bits.append(f"quant={o['quant']}")
                lines.append("  · " + ", ".join(bits))

        endpoints = meta.get("endpoints") or []
        if endpoints:
            quants = sorted(
                {str(e.get("quant") or "?") for e in endpoints if isinstance(e, dict)}
            )
            wins = [
                int(e["max_input"])
                for e in endpoints
                if isinstance(e, dict) and e.get("max_input")
            ]
            prices = [
                float(e["price_in"])
                for e in endpoints
                if isinstance(e, dict) and e.get("price_in") is not None
            ]
            bits = [f"{len(endpoints)} bookable variants", f"quant {'/'.join(quants)}"]
            if wins:
                bits.append(f"window {min(wins):,}–{max(wins):,}")
            if prices:
                bits.append(f"${min(prices)}–${max(prices)} in/1M")
            lines += ["", "## endpoints", "  · " + ", ".join(bits)]

        capability = meta.get("capability") or {}
        if capability:
            lines += ["", "## capability (1–5 ordinal)"]
            for axis in llm_catalog.CAPABILITY_AXES:
                if axis in capability:
                    val = capability[axis]
                    score = val.get("score") if isinstance(val, dict) else val
                    conf = (
                        f" (conf {val['confidence']})"
                        if isinstance(val, dict) and val.get("confidence")
                        else ""
                    )
                    lines.append(f"  {axis}: {score}{conf}")

        prov = meta.get("provenance")
        if prov:
            lines += ["", f"provenance: {prov}"]

        # Ledger pointers (the tote + review log — layer 2). Cheap count only;
        # the full rollups live on view='tote' / view='reviews'.
        reviews = llm_catalog.list_reviews(self.store, ref.id)
        if reviews:
            lines += [
                "",
                f"reviews: {len(reviews)} "
                f"(get(kind='llm', id={ref.id}, view='reviews')) · "
                f"telemetry: get(kind='llm', id={ref.id}, view='tote')",
            ]

        # The capability prose (the embedded body).
        if ref.title:
            lines += ["", ref.title.rstrip()]
        if tags:
            lines += ["", "tags: " + " ".join(str(t) for t in tags)]
        return "\n".join(lines)

    def _render_tote(self, ref: Ref) -> str:
        """``view='tote'`` — the realized-telemetry rollup over ``llm_call_log``."""
        model_id = (ref.meta or {}).get("model_id", "?")
        tote = llm_catalog.llm_tote(self.store, model_id)
        lines = [f"# tote — {model_id} (last 30d)"]
        if tote.calls == 0:
            lines.append("no llm_call_log rows yet for this model.")
            return "\n".join(lines)
        lines.append(f"calls: {tote.calls}")
        lines.append(f"realized cost: ${tote.cost_usd:.4f}")
        if tote.error_rate is not None:
            lines.append(f"error rate: {tote.error_rate:.1%}")
        if tote.p50_duration_ms is not None:
            lines.append(f"p50 duration: {tote.p50_duration_ms:.0f} ms")
        if tote.avg_turns is not None:
            lines.append(f"avg turns: {tote.avg_turns:.2f}")
        by_src = llm_catalog.llm_tote_by_source(self.store, model_id)
        if len(by_src) > 1:
            lines += ["", "by source:"]
            for src, r in by_src:
                err = f", err {r.error_rate:.0%}" if r.error_rate is not None else ""
                lines.append(f"  {src}: {r.calls} calls, ${r.cost_usd:.4f}{err}")
        return "\n".join(lines)

    def _render_op_stats(self, source: str, *, window_days: int = 30) -> str:
        """``id='op:<source>', view='stats'`` — the per-op tote rollup over
        ``llm_call_log`` plus a per-model split (gr502653)."""
        from precis.utils.llm.local_first_stats import render_stats

        try:
            local_first = render_stats(self.store, source, window_days=window_days)
        except Exception:
            log.warning("local-first stats unavailable for %s", source, exc_info=True)
            local_first = "local-first stats unavailable."
        tote = llm_catalog.llm_op_tote(self.store, source, window_days=window_days)
        lines = [f"# llm op stats — {source} (last {window_days}d)"]
        if tote.calls == 0:
            lines.append(f"no llm_call_log rows for source {source!r} in the window.")
            if local_first:
                lines += ["", local_first]
            return "\n".join(lines)
        lines.append(f"calls: {tote.calls}")
        lines.append(f"realized cost: ${tote.cost_usd:.4f}")
        if tote.error_rate is not None:
            lines.append(f"error rate: {tote.error_rate:.1%}")
        if tote.p50_duration_ms is not None:
            lines.append(f"p50 duration: {tote.p50_duration_ms:.0f} ms")
        if tote.avg_turns is not None:
            lines.append(f"avg turns: {tote.avg_turns:.2f}")
        if tote.input_tokens or tote.output_tokens:
            lines.append(
                f"tokens: {tote.input_tokens:,} in / {tote.output_tokens:,} out"
            )
        by_model = llm_catalog.llm_op_models(
            self.store, source, window_days=window_days
        )
        if by_model:
            spans = llm_catalog.llm_op_model_span(
                self.store, source, window_days=window_days
            )
            lines += ["", "by model:"]
            for model, r in by_model:
                err = f", err {r.error_rate:.0%}" if r.error_rate is not None else ""
                span = spans.get(model)
                when = f", {span[0]} to {span[1]}" if span else ""
                lines.append(
                    f"  {model}: {r.calls} calls, ${r.cost_usd:.4f}{err}{when}"
                )
        if local_first:
            lines += ["", local_first]
        return "\n".join(lines)

    def _render_placement(
        self, *, window_days: int = 7, source: str | None = None
    ) -> str:
        """``id='/placement'`` — local vs cloud share of logged LLM calls:
        landed (``placement``) per tier and per UTC day, and the routed-vs-landed
        split (``placement_routed``) where the row carries it."""
        from precis.format import render_agent_table

        def pct(n: int, d: int) -> str:
            return f"{n / d:.1%}" if d else "—"

        lines = [f"# llm placement — last {window_days}d"]
        by_tier = llm_catalog.placement_share(
            self.store, by="tier", window_days=window_days, source=source
        )
        if not by_tier:
            lines.append("no llm_call_log rows in the window.")
            return "\n".join(lines)
        calls = sum(r.calls for r in by_tier)
        local = sum(r.landed_local for r in by_tier)
        known = sum(r.routed_known for r in by_tier)
        lines.append(
            f"landed local: {local}/{calls} ({pct(local, calls)}); routed recorded "
            f"on {known}/{calls} — rows before migration 0179 count as landed only."
        )
        schema = [
            "key",
            "calls",
            "landed_local",
            "routed_local",
            "kept_local",
            "fell_to_cloud",
            "billed_usd",
        ]

        def table(rows: list[llm_catalog.PlacementShareRow]) -> list[str]:
            data = [
                {
                    "key": r.key,
                    "calls": str(r.calls),
                    "landed_local": f"{r.landed_local} ({pct(r.landed_local, r.calls)})",
                    "routed_local": str(r.routed_local),
                    "kept_local": pct(r.routed_local_kept, r.routed_local),
                    "fell_to_cloud": str(r.routed_local - r.routed_local_kept),
                    "billed_usd": f"{r.billed_usd:.2f}",
                }
                for r in rows
            ]
            return [render_agent_table(data, schema=schema)]

        lines += ["", "## by tier", *table(by_tier)]
        by_day = llm_catalog.placement_share(
            self.store, by="day", window_days=window_days, source=source
        )
        lines += ["", "## by day (UTC)", *table(by_day)]
        lines += [
            "",
            "kept_local = routed local AND landed local, over routed local. "
            "fell_to_cloud = routed local, landed cloud (skip, busy slot, or "
            "failover). billed_usd excludes rows landed local (priced, not spent).",
        ]
        return "\n".join(lines)

    def _render_reviews(self, ref: Ref) -> str:
        """``view='reviews'`` — the append-only, typed, dated review log."""
        model_id = (ref.meta or {}).get("model_id", "?")
        reviews = llm_catalog.list_reviews(self.store, ref.id)
        if not reviews:
            return f"# reviews — {model_id}\n\nno reviews yet."
        lines = [f"# reviews — {model_id} ({len(reviews)})"]
        for b in reviews:
            m = b.meta or {}
            rtype = m.get("entry_type", "?")
            who = m.get("by", "?")
            prov = m.get("provenance")
            stamp = b.created_at.date().isoformat() if b.created_at else "?"
            head = f"\n### {rtype} · {stamp} · {who}"
            if prov:
                head += f" · {prov}"
            lines.append(head)
            lines.append(b.text)
        return "\n".join(lines)

    def _render_accounts(self) -> str:
        """``id='/accounts'`` — every ``llm_account`` row. ``key`` names the
        vault secret only (never a value); unknown/absent fields render as
        ``unknown``."""

        def fmt_ts(v: Any) -> str:
            if v is None:
                return "unknown"
            stamp = as_utc(v)
            return stamp.strftime("%Y-%m-%dT%H:%M:%SZ") if stamp else "unknown"

        accounts = llm_accounts.list_accounts(self.store)
        lines = [f"# llm accounts ({len(accounts)})"]
        if not accounts:
            lines.append("(none)")
            return "\n".join(lines)
        for a in accounts:
            if a.api_key_ref is None:
                key = "none (per-host login, no vault secret)"
            else:
                has_it = (
                    "in vault"
                    if secret_status.present(a.api_key_ref, store=self.store)
                    else "MISSING"
                )
                key = f"{a.api_key_ref} ({has_it})"
            parked = (
                f" parked={a.parked_reason}@{fmt_ts(a.parked_at)}"
                if a.parked_reason
                else ""
            )
            health = (
                f" health={a.health_outcome}@{fmt_ts(a.health_checked_at)}"
                if a.health_outcome
                else ""
            )
            lines.append(
                f"  {a.name}: vendor={a.vendor} plan={a.plan} "
                f"adapter={a.adapter} enabled={a.enabled} "
                f"concurrency={a.concurrency} "
                f"task_classes={','.join(a.task_classes) or 'unknown'} "
                f"default_model={a.default_model or 'unknown'} key={key} "
                f"pools={','.join(a.pools) or 'unknown'} "
                f"promo_expiry={a.promo_expiry or 'none'} "
                f"models={','.join(a.models) or 'unknown'} lane_rank={a.lane_rank}"
                f"{parked}{health}"
            )
        return "\n".join(lines)

    def _render_decisions(self, *, window_days: int = 7) -> str:
        """Summarize shadow agreement and projected spend for executed calls."""
        rows = llm_catalog.decision_rollup(self.store, window_days=window_days)
        lines = [
            f"# llm decisions ({sum(row.calls for row in rows)} calls, {window_days} d)"
        ]
        for row in rows:
            agreement = 100 * row.agree / row.calls if row.calls else 0
            lines.append(
                f"  {row.task_kind}: calls={row.calls} agree={agreement:.0f}% "
                f"claude legacy={row.legacy_claude} shadow={row.shadow_claude} "
                f"usd actual={row.actual_usd:.6f} projected={row.projected_usd:.6f} "
                f"({row.projected_known}/{row.calls} priced)"
            )
        slice_rows = llm_catalog.slice_decision_rollup(
            self.store, window_days=window_days
        )
        if slice_rows:
            lines.append(
                f"# slice lane picks ({sum(r.picks for r in slice_rows)} picks, "
                f"{window_days} d)"
            )
            for r in slice_rows:
                pct = 100 * r.agree / r.picks if r.picks else 0
                lines.append(
                    f"  {r.task_class}: picks={r.picks} agree={pct:.0f}% "
                    f"lane_match={r.lane_match}"
                )
        return "\n".join(lines)

    def _render_health(self, args: dict[str, Any]) -> str:
        """``id='/health'`` — error rate and p95 latency per executed path."""
        example = "get(kind='llm', id='/health', args={'window_days': 7})"
        if unexpected := args.keys() - {"window_days"}:
            raise BadInput(
                f"args= keys {sorted(unexpected)} are unsupported for id='/health'",
                next=example,
            )
        raw = args.get("window_days", 7)
        if isinstance(raw, bool) or not isinstance(raw, int) or not 1 <= raw <= 30:
            raise BadInput(
                "window_days must be a whole number from 1 to 30",
                next=example,
            )
        rows = llm_catalog.llm_health(self.store, window_days=raw)
        lines = [f"# llm health ({sum(r.calls for r in rows)} calls, {raw} d)"]
        for r in rows:
            p95 = f"{r.p95_ms:.0f} ms" if r.p95_ms is not None else "n/a"
            lines.append(
                f"  path={r.path} model={r.model} transport={r.transport}: "
                f"calls={r.calls} errors={100 * r.error_rate:.0f}% p95={p95}"
            )
        return "\n".join(lines)

    def _render_decide_modes(self) -> str:
        """``id='/decide-modes'`` — the winning decide mode per task kind."""
        from precis.utils.llm import decide

        kinds = llm_catalog.logged_task_kinds(self.store)
        lines = [f"# llm decide modes (default {decide.mode_for(None)})"]
        for kind in kinds:
            lines.append(f"  {kind}: {decide.mode_for(kind)}")
        return "\n".join(lines)

    def _render_breaker(self) -> str:
        """``id='/breaker'`` — is the dollar breaker tripped, and what resumes it."""
        from precis import alerts
        from precis.budget import meter
        from precis.budget import settings as budget_settings

        status = meter.current_status(self.store, use_cache=False)
        if status is None:
            return "breaker: unknown (no spend reading available)"
        resume = budget_settings.get_resume_until(self.store)
        now = datetime.now(UTC)
        resume_at = as_utc(resume) if resume is not None else None
        resumed = resume_at is not None and resume_at > now
        override = f"active until {resume_at:%Y-%m-%d %H:%M} UTC" if resumed else "none"
        window = status.tripped_window
        if window is None:
            return (
                f"breaker: ok hourly ${status.hourly_spent:.2f} of "
                f"${status.hourly_cap:.2f}, daily ${status.daily_spent:.2f} of "
                f"${status.daily_cap:.2f}; resume override: {override}\n"
                "The subscription quota taper is a separate mechanism: see "
                "id='/pools'."
            )
        spent, cap = (
            (status.hourly_spent, status.hourly_cap)
            if window == "hourly"
            else (status.daily_spent, status.daily_cap)
        )
        hist = alerts.alert_history(
            self.store, source="budget", fingerprint=f"cap-{window}"
        )
        opened = as_utc((hist.get("open") or {}).get("created_at"))
        since = f"{opened:%Y-%m-%d %H:%M} UTC" if opened is not None else "unknown"
        if resumed:
            # The override bypasses the soft trip (breaker._gate): paid
            # work runs through the cap until the instant ``override``
            # names, so "paused" would lie about the effective state.
            pause = (
                "paid work still runs through the cap under the resume "
                "override; when it lapses the cap pauses paid work unless "
                "the window has rolled off or a human raised it"
            )
        else:
            pause = (
                "paid work is paused, free local work still runs; it "
                "resumes when the window rolls off or a human raises the "
                "cap or resumes on the budget page"
            )
        return (
            f"breaker: tripped {window} ${spent:.2f} of ${cap:.2f} since {since}; "
            f"{pause}; resume override: {override}\n"
            "The subscription quota taper is a separate mechanism: see "
            "id='/pools'."
        )

    def _render_suitability(self, *, window_days: int = 7, limit: int = 10) -> str:
        """``id='/suitability'`` — stored posteriors for the busiest kinds.

        Reads stored ``meta.suitability`` only (written by the nightly
        ``llm_suitability`` cadence) plus the one task-kind volume query —
        no scipy, no outcome rescan. A busy kind with no stored posterior
        prints ``no outcomes``; an empty catalog renders the header only.
        """
        from precis.utils.llm.suitability import busiest_task_kinds

        kinds = busiest_task_kinds(self.store, window_days=window_days, limit=limit)
        lines = [f"# llm suitability ({len(kinds)} task kinds, {window_days} d)"]
        if not kinds:
            return "\n".join(lines)
        with self.store.pool.connection() as conn:
            cards = conn.execute(
                "SELECT meta->'suitability'->'offerings' FROM refs "
                "WHERE kind = 'llm' AND retired_at IS NULL "
                "AND meta ? 'suitability'"
            ).fetchall()
        by_kind: dict[str, list[tuple[str, str, dict[str, Any], dict[str, Any]]]] = {}
        for (offerings,) in cards:
            for okey, entry in (offerings or {}).items():
                for kind, bands in (entry.get("kinds") or {}).items():
                    for band, cell in bands.items():
                        by_kind.setdefault(kind, []).append((okey, band, cell, entry))
        for kind in kinds:
            entries = sorted(by_kind.get(kind) or [], key=lambda e: (e[0], e[1]))
            if not entries:
                lines.append(f"  {kind}: no outcomes")
                continue
            for okey, band, cell, entry in entries:
                lo, hi = cell.get("lower90"), cell.get("upper90")
                span = (
                    f"[{lo:.3f}, {hi:.3f}]"
                    if isinstance(lo, int | float) and isinstance(hi, int | float)
                    else "[?, ?]"
                )
                l90 = f"{lo:.3f}" if isinstance(lo, int | float) else "?"
                lines.append(
                    f"  {kind}: {okey} band={band} label={cell.get('label')} "
                    f"n={cell.get('n')} L90={l90} interval={span} "
                    f"identity={'observed' if entry.get('identity') else 'requested only'} "
                    f"drift={entry.get('drift_at') or 'no'}"
                )
        return "\n".join(lines)

    def _render_preview(self, args: dict[str, Any]) -> str:
        """``id='/preview'`` — what the router would choose for a task kind.

        The task kind's recipe names the source and the source's
        registered operation the tier; ``source`` or ``tier`` in ``args``
        replaces either. Every offering inside the demanded tier is
        listed: the ranked ones in rank order (the first is the choice),
        then the rejected ones with the filter that dropped them.
        Offerings below the tier are counted, not listed.
        """
        from dataclasses import replace

        from precis.utils.llm import decide, operations, recipes, router

        example = "get(kind='llm', id='/preview', args={'task_kind': '<task kind>'})"
        if unexpected := args.keys() - self._PREVIEW_ARGS.keys():
            raise BadInput(
                f"args= keys {sorted(unexpected)} are unsupported for id='/preview'",
                next=example,
            )
        task_kind = str(args.get("task_kind") or "").strip()
        source = str(args.get("source") or "").strip()
        if not task_kind and not source:
            raise BadInput(
                "the router preview needs args={'task_kind': …} (or 'source', "
                "an operation source tag)",
                next=example,
            )
        siblings: list[str] = []
        if not source:
            siblings = recipes.sources_for(task_kind)
            source = next(
                (s for s in siblings if operations.resolve_op(s) is not None),
                siblings[0] if siblings else "",
            )
        op = operations.resolve_op(source)
        tiers = [t.value for t in reversed(router.Tier)]
        asked = args.get("tier")
        if asked is None and op is None:
            raise BadInput(
                f"no tier for task kind {task_kind or source!r}: no recipe "
                "source of it is a registered operation — name the tier",
                options=tiers,
                next=f"get(kind='llm', id='/preview', args={{'task_kind': "
                f"{task_kind or '<task kind>'!r}, 'tier': 'small'}})",
            )
        try:
            tier = router.Tier(str(asked).strip().lower()) if asked else None
        except ValueError:
            raise BadInput(
                f"unknown tier {asked!r}", options=tiers, next=example
            ) from None
        tier_note = ""
        if tier is None:
            assert op is not None
            tier = op[0]
        elif op is not None and op[0] is not tier:
            tier_note = f" (asked; the operation runs {op[0].value})"
        tools = args.get("tools")
        if tools not in (None, True, False):
            raise BadInput(
                f"tools must be true or false, got {tools!r}",
                options=["true", "false"],
                next=example,
            )
        choices = {
            "privacy": ("default", "local-only"),
            "work_class": ("background", "deliverable", "interactive"),
        }
        picked: dict[str, str] = {}
        for key, allowed in choices.items():
            picked[key] = str(args.get(key) or allowed[0])
            if picked[key] not in allowed:
                raise BadInput(
                    f"unknown {key} {picked[key]!r}",
                    options=list(allowed),
                    next=example,
                )
        demand = recipes.resolve(source, task_kind or None, None, picked["privacy"])
        req = router.LlmRequest(
            tier=tier,
            source=source,
            tools_needed=bool(tools),
            task_kind=demand.task_kind,
            consequence=demand.consequence,
            privacy_class=demand.privacy_class,
            work_class=picked["work_class"],
        )
        ledger = decide.ledger_from_store(self.store, now=datetime.now(UTC))
        if ledger is None:
            raise Upstream(
                "the routing ledger could not be read; no preview",
                next="retry in a minute: " + example,
            )
        model = (op[1] if op else None) or router.resolve_model(tier)
        ladder = operations.resolve_op_chain(
            source, tools_needed=req.tools_needed
        ) or router.resolve_chain(tier, tools_needed=req.tools_needed, model=model)
        if ladder:
            # The tie-break the router applies: a dead-even pick lands on
            # the lane the legacy ladder runs first.
            ledger = replace(
                ledger,
                legacy_adapter=ladder[0].transport.value,
                legacy_model=ladder[0].model or model,
            )
        mode = decide.mode_for(demand.task_kind)
        result = decide.decide(req, decide.requirement_for(req), ledger, mode)
        listed = [r for r in result.rejected if r.reason not in decide.TIER_REJECTIONS]
        below = len(result.rejected) - len(listed)
        effect = {
            "on": "this choice executes; the legacy ladder is its fallback",
            "shadow": "the legacy ladder executes; this choice is only logged",
        }.get(mode, "the legacy ladder executes; this choice is not consulted")
        lines = [
            f"# llm route preview — task kind {demand.task_kind}",
            f"demand: source={source or '(none)'} tier={tier.value}{tier_note} "
            f"tools={'yes' if req.tools_needed else 'no'} "
            f"privacy={demand.privacy_class} work_class={picked['work_class']} "
            f"consequence={demand.consequence}",
        ]
        if len(siblings) > 1:
            lines.append(
                "other sources of this kind: "
                + ", ".join(s for s in siblings if s != source)
                + " — args={'source': '<source>'} previews one"
            )
        lines.append(f"decide mode: {mode} — {effect}")
        lines.append(
            f"chosen: {result.chosen.key}"
            if result.chosen is not None
            else f"chosen: none — no offering survives; the tier model "
            f"{result.fallback_model} runs"
        )
        lines.append(
            f"candidates ({len(result.ranked)} ranked, {len(listed)} rejected):"
        )
        for i, r in enumerate(result.ranked, 1):
            # The reason is ``band; billing; pool state`` plus a demotion
            # (``unhealthy: …``, ``saturated: …``, ``over-ceiling: …``).
            demotion = r.reason.split("; ")[3:]
            word = demotion[0].split(":")[0] if demotion else "healthy"
            price = r.offering.price_in
            lines.append(
                f"  {i}. {'*' if r.offering is result.chosen else ' '} "
                f"{r.offering.key} — {word}: {r.reason}"
                + (f"; price_in={price:g}" if price is not None else "")
            )
        lines.extend(f"  -   {r.offering_key} — {r.filter}: {r.reason}" for r in listed)
        if below:
            lines.append(
                f"{below} offering{'s' if below != 1 else ''} below the "
                f"{tier.value} tier floor not listed"
            )
        lines.append(
            "legacy ladder: "
            + ", ".join(decide.rung_key(rung, model) for rung in ladder)
        )
        lines.append(
            "No LLM call made, nothing written. Pools and health are a "
            "15-second snapshot; request pins, keyword rules and runtime "
            "gates can change the route."
        )
        return "\n".join(lines)

    def _render_pools(self) -> str:
        """``id='/pools'`` — every ``llm:``-prefixed ``external_rate_limits``
        row. ``pacing`` is the pool's read-time state
        (:func:`~precis.utils.llm.accounts.pacing`); unknown/absent fields
        render as ``unknown``."""

        def fmt(v: Any) -> str:
            return "unknown" if v is None else str(v)

        def fmt_ts(v: Any) -> str:
            if v is None:
                return "unknown"
            stamp = as_utc(v)
            return stamp.strftime("%Y-%m-%dT%H:%M:%SZ") if stamp else "unknown"

        pools = llm_accounts.list_pools(self.store)
        lines = [f"# llm pools ({len(pools)})"]
        if not pools:
            lines.append("(none)")
            return "\n".join(lines)
        now = datetime.now(UTC)
        for p in pools:
            lines.append(
                f"  {p.provider}: unit={fmt(p.unit)} limit={fmt(p.limit_reported)} "
                f"used={fmt(p.used_reported)} remaining={fmt(p.remaining_reported)} "
                f"reported_at={fmt_ts(p.reported_at)} source={fmt(p.source)} "
                f"reset_rule={fmt(p.reset_rule)} resets_at={fmt_ts(p.resets_at)} "
                f"expires_at={fmt_ts(p.expires_at)} "
                f"retry_at={fmt_ts(p.parked_until)} stale={str(p.stale).lower()} "
                f"pacing={llm_accounts.pacing(p, now).state}"
            )
        return "\n".join(lines)


__all__ = ["LlmHandler"]
