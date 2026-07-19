"""LlmHandler — the `llm` model catalog (migration 0071, docs/proposals/llm-catalog.md).

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
"""

from __future__ import annotations

from typing import Any, ClassVar

from precis import llm_catalog
from precis.errors import BadInput, NotFound
from precis.handlers._numeric_ref import NumericRefHandler
from precis.protocol import KindSpec
from precis.response import Response
from precis.store import Ref, Tag
from precis.utils import handle_registry


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
            "against the live OpenRouter feed and flags drift. Never exported. "
            "See docs/proposals/llm-catalog.md."
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

    #: The prose embeds so the card is a vector (search-by-capability). Emission
    #: itself runs through :func:`precis.llm_catalog.upsert_card`, not the base
    #: ``_create`` path — the flag documents the intent + keeps the base path
    #: correct if ever exercised.
    emits_card: ClassVar[bool] = True

    # ── id resolution: model slug, numeric id, or the lm handle ─────

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

    def get(  # type: ignore[override]
        self,
        *,
        id: str | int | None = None,
        view: str | None = None,
        q: str | None = None,
        **_kw: Any,
    ) -> Response:
        concrete = id is not None and not (isinstance(id, str) and id.startswith("/"))
        if concrete and view in ("tote", "reviews"):
            ref = self._ref_for(id)  # type: ignore[arg-type]
            if view == "tote":
                return Response(body=self._render_tote(ref))
            return Response(body=self._render_reviews(ref))
        # Resolve a model slug to the card's ref before the base handler's
        # integer coercion; path views (``/recent``) and numeric ids fall through.
        if isinstance(id, str):
            s = id.strip()
            core = s[len("llm:") :] if s.startswith("llm:") else s
            if not s.startswith("/") and not core.isdigit():
                ref = self._ref_for(id)
                return super().get(id=ref.id, view=view, q=q, **_kw)
        return super().get(id=id, view=view, q=q, **_kw)

    # ── put: guarded funnel to the shared catalog writer ────────────

    def put(  # type: ignore[override]
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
        **_kw: Any,
    ) -> Response:
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

    # ── rendering ────────────────────────────────────────────────────

    def _render_one(self, ref: Ref, tags: list[Tag]) -> str:  # type: ignore[override]
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


__all__ = ["LlmHandler"]
