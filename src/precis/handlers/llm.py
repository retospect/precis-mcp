"""LlmHandler — the `llm` model catalog (migration 0071, ``llm-catalog`` (git-only)).

A model card is one numeric ref per model (``claude-opus-4-8``, ``qwen-heavy``): the
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

``/accounts`` and ``/pools`` (migration 0280, S1a) read the owned-capacity
ledger: an account is one credential at one provider under one plan
(``llm_account``); a pool is a rate bucket with a reset rule and explicit
scope (an ``external_rate_limits`` row keyed ``llm:<account>:<window>``).
Both are read-only here except ``put(kind='llm', args={'account': {...}})``,
which is owner-only (:func:`precis.todo.guards.is_owner`) and never accepts
a ``base_url`` — that stays on the ``llm.profiles`` entry an HTTP account
names via ``profile=``. No routing behaviour changes: ``pick_lane``,
``ladder``, ``resolve_profile`` and ``rate_limit.acquire``/``park``/
``_record_refusal`` read the same rows as before this migration.
"""

from __future__ import annotations

import json
import logging
import re
from typing import Any, ClassVar

from precis import llm_catalog, secret_status
from precis.errors import BadInput, NotFound
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
    {"*", "code", "review", "validate", "triage", "brief", "pcb", "docs"}
)
_ACCOUNT_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9\-]*$")
_ACCOUNT_POOL_KEY_RE = re.compile(r"^llm:[a-z0-9-]+:[a-z0-9]+$")
_ACCOUNT_CONCURRENCY_MIN = 1
_ACCOUNT_CONCURRENCY_MAX = 8

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


class LlmHandler(NumericRefHandler):
    spec: ClassVar[KindSpec] = KindSpec(
        kind="llm",
        title="LLM catalog",
        description=(
            "A model catalog card — one ref per model. Body is the capability "
            "prose (embedded, so the card is a vector); meta carries the facts "
            "(model_id, tier_floor, offerings, capability axes, provenance). Read "
            "with get(kind='llm', id='claude-opus-4-8') or search(kind='llm', "
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
            "put(kind='llm', args={'account': {...}}) is owner-only. "
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
    #: account/pool write (S1a step 4). No other verb reads ``args=`` here.
    ARGS: ClassVar[dict[str, ArgsSpec]] = {"put": ArgsSpec({"account"})}

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
        (``claude-opus-4-8`` — the human key, looked up via ``meta.model_id``), a
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
                    raise NotFound(
                        f"no llm card for model {s!r}",
                        next=f"search(kind={self.kind!r}, q='...') to find models",
                    )
                return ref
        return self._resolve_live_ref(self._coerce_id(id))

    # ── get: slug resolution + the tote / reviews views ─────────────

    def get(
        self,
        *,
        id: str | int | list[str | int] | None = None,
        view: str | None = None,
        q: str | None = None,
        **_kw: Any,
    ) -> Response:
        if (r := self._route_find(id=id, view=view, q=q)) is not None:
            return r
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
        return super()._list_view(view)

    def _supported_list_views(self) -> tuple[str, ...]:
        return (
            *super()._supported_list_views(),
            "ops",
            "placement",
            "accounts",
            "pools",
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
        # ``put(id=…, text=…, entry=…)`` appends a WORM review entry to a card —
        # the quest-logbook idiom (the ledger layer). The base create-only guard
        # rejects id-presence, so we intercept before delegating.
        if id is not None:
            if not (text and text.strip()):
                raise BadInput(
                    f"appending a review to an {self._sense()} requires text=",
                    next=(
                        f"put(kind={self.kind!r}, id={id!r}, text='...', "
                        "entry='measured-eval', by='agent')"
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
                next="put the base_url through llm.profiles, then name it "
                "here with args={'account': {'profile': '<profiles entry>'}}",
            )
        allowed_keys = frozenset(_ACCOUNT_MERGE_COLS) | {"name"}
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

        with self.store.tx() as conn:
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
        if len(by_model) > 1:
            lines += ["", "by model:"]
            for model, r in by_model:
                err = f", err {r.error_rate:.0%}" if r.error_rate is not None else ""
                lines.append(f"  {model}: {r.calls} calls, ${r.cost_usd:.4f}{err}")
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
            lines.append(
                f"  {a.name}: vendor={a.vendor} plan={a.plan} "
                f"adapter={a.adapter} enabled={a.enabled} "
                f"concurrency={a.concurrency} "
                f"task_classes={','.join(a.task_classes) or 'unknown'} "
                f"default_model={a.default_model or 'unknown'} key={key} "
                f"pools={','.join(a.pools) or 'unknown'}"
            )
        return "\n".join(lines)

    def _render_pools(self) -> str:
        """``id='/pools'`` — every ``llm:``-prefixed ``external_rate_limits``
        row. ``pacing`` is the literal ``n/a`` until S3; unknown/absent
        fields render as ``unknown``."""

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
        for p in pools:
            lines.append(
                f"  {p.provider}: unit={fmt(p.unit)} limit={fmt(p.limit_reported)} "
                f"used={fmt(p.used_reported)} remaining={fmt(p.remaining_reported)} "
                f"reported_at={fmt_ts(p.reported_at)} source={fmt(p.source)} "
                f"reset_rule={fmt(p.reset_rule)} resets_at={fmt_ts(p.resets_at)} "
                f"expires_at={fmt_ts(p.expires_at)} pacing=n/a"
            )
        return "\n".join(lines)


__all__ = ["LlmHandler"]
