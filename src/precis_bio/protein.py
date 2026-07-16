"""ProteinHandler — the structure-prediction ``protein`` kind (ADR 0056).

A ``protein`` is a slug-addressed authored artifact (like ``structure`` /
``route``): a sequence whose predicted structure (``meta.fold``) is folded by
a swappable engine on the compute lane and read back by the LLM as a
confidence summary + sequence — never a synchronous GPU call. Maps onto the
seven verbs:

- ``put``    — create/fold a protein for ``sequence=<AA>`` (``id=`` slug,
  ``engine=stub|alphafold3``). Returns a content-addressed **cache hit** if the
  same sequence+engine was already folded; else runs the engine (in-process
  ``stub``) or mints a ``fold`` compute job pinned to ``PRECIS_FOLD_NODE``.
  ``requested_by=<todo>`` blocks that todo on the job (ADR 0044).
- ``get``    — list proteins, render one fold summary (``id=slug``), or return
  the raw mmCIF structure (``view='cif'``).
- ``delete`` — soft-retire a protein.

Ships **dark** behind ``PRECIS_BIO_ENABLED`` (``KindSpec.requires_env``): the
kind is hidden from the catalogue and the dispatcher until the flag is set.
See ``docs/design/chem-tools-integration.md`` + ADR 0056.
"""

from __future__ import annotations

import os
import re
from typing import Any, ClassVar

from precis.dispatch import Hub, InitError
from precis.errors import BadInput, NotFound
from precis.protocol import Handler, KindSpec
from precis.response import Response
from precis_bio.engine import DEFAULT_SEEDS, resolve_engine
from precis_bio.ir import ProteinFold, fold_cache_key, validate_sequence
from precis_bio.persist import apply_fold_result

#: Env naming the compute node a ``fold`` job pins to. Unset ⇒ the handler runs
#: the (in-process) stub engine inline — the slice-0 fallback that keeps the
#: round-trip testable without a cluster (route's ``PRECIS_CHEM_ROUTE_NODE``
#: analogue).
FOLD_NODE_ENV = "PRECIS_FOLD_NODE"


class ProteinHandler(Handler):
    spec: ClassVar[KindSpec] = KindSpec(
        kind="protein",
        title="Protein",
        description=(
            "A predicted protein structure (precis-bio plugin, ADR 0056). "
            "put(id='<slug>', sequence='<AA>', engine='stub'|'alphafold3', "
            "requested_by=<todo>) folds a sequence — a content-addressed cache "
            "hit if already folded, else an in-process stub or a minted fold "
            "compute job on the GPU node. get lists proteins, renders one fold "
            "summary (id=slug), or returns the mmCIF (view='cif'); delete "
            "soft-retires. The LLM reads confidences + sequence, never runs a "
            "GPU fold in the request path. See chem-tools-integration.md."
        ),
        supports_get=True,
        supports_put=True,
        supports_delete=True,
        is_numeric=False,
        id_required=False,
        role="artifact",
        corpus_role="none",
        can_own_jobs=True,
        # Dark-ship: the kind is hidden until the flag is set.
        requires_env=("PRECIS_BIO_ENABLED",),
    )

    def __init__(self, *, hub: Hub) -> None:
        if hub.store is None:
            raise InitError("protein: store required")
        self.store = hub.store
        self.embedder = hub.embedder

    # ── put ──────────────────────────────────────────────────────────
    def put(  # type: ignore[override]
        self,
        *,
        id: str | int | None = None,
        sequence: str | None = None,
        engine: str | None = None,
        title: str | None = None,
        requested_by: int | str | None = None,
        seeds: list[int] | None = None,
        **_kw: Any,
    ) -> Response:
        if id is None or not str(id).strip():
            raise BadInput(
                "put(kind='protein') requires id= (the protein slug)",
                next="put(kind='protein', id='insulin-a', sequence='GIVEQCCTSICSLYQLENYCN')",
            )
        slug = str(id).strip()
        if sequence is None or not str(sequence).strip():
            raise BadInput(
                "put(kind='protein') requires sequence= (the amino-acid sequence)",
                next="put(kind='protein', id='insulin-a', sequence='GIVEQCCTSICSLYQLENYCN')",
            )
        try:
            seq = validate_sequence(sequence)
        except ValueError as exc:
            raise BadInput(
                str(exc), next="sequence= the 1-letter amino-acid codes"
            ) from exc
        try:
            eng = resolve_engine(engine)
        except ValueError as exc:
            raise BadInput(str(exc), next="engine='stub' | 'alphafold3'") from exc
        seed_list = [int(s) for s in (seeds or DEFAULT_SEEDS)]
        key = fold_cache_key(
            sequence=seq,
            engine=eng.name,
            engine_version=eng.version,
            mode=eng.mode,
            seeds=seed_list,
        )

        existing = self.store.get_ref(kind="protein", id=slug)
        # Content-addressed cache hit: same slug already carries a fold under
        # this exact key ⇒ zero recompute (ADR 0007 / 0056 §6).
        if existing is not None:
            meta = existing.meta or {}
            if meta.get("cache_key") == key and meta.get("fold"):
                fold = ProteinFold.from_json(meta["fold"])
                return Response(
                    body=f"# protein '{slug}' — cache hit (no recompute)\n\n"
                    + fold.render()
                )

        seed_meta = {
            "sequence": seq,
            "engine": eng.name,
            "engine_version": eng.version,
            "mode": eng.mode,
            "cache_key": key,
            "status": "folding",
            "seeds": seed_list,
        }
        if existing is None:
            ref = self.store.insert_ref(
                kind="protein",
                slug=slug,
                title=(title or slug).strip() or slug,
                meta=seed_meta,
            )
        else:
            ref = existing
            self.store.stamp_ref_meta(ref.id, seed_meta)

        params = {
            "protein_ref_id": ref.id,
            "name": (title or slug).strip() or slug,
            "sequence": seq,
            "engine": eng.name,
            "engine_version": eng.version,
            "mode": eng.mode,
            "seeds": seed_list,
            "cache_key": key,
        }

        node = os.environ.get(FOLD_NODE_ENV)
        if node:
            # Compute lane: mint a derived job on the fold node (ADR 0044).
            return self._dispatch(ref, params, node, requested_by)

        # Slice-0 inline fallback (no fold node configured): run the in-process
        # engine now. A container engine raises here — tell the caller to
        # configure a node.
        try:
            fold = eng.fold(seq, seeds=seed_list)
        except NotImplementedError as exc:
            raise BadInput(
                f"engine '{eng.name}' needs a compute node: {exc}",
                next=f"set {FOLD_NODE_ENV}=<node> (+ PRECIS_FOLD_MODELS_DIR), "
                "or use engine='stub'",
            ) from exc
        apply_fold_result(self.store, ref.id, fold, cache_key=key)
        state = "folded" if fold.folded else "no model"
        return Response(
            body=f"# protein '{slug}' — {state} ({fold.engine}, in-process)\n\n"
            + fold.render()
        )

    def _dispatch(
        self,
        ref: Any,
        params: dict[str, Any],
        node: str,
        requested_by: int | str | None,
    ) -> Response:
        """Mint a ``fold`` job pinned to the fold node (ADR 0044).

        The job is a *derived* compute step: it parents on the **protein**, not
        a todo — the artifact owns it (cache-fillable, idempotent). When a
        caller names ``requested_by`` it also wants to block on the result; we
        then write a ``requested`` link + inject a ``derived_job_succeeded``
        auto_check so that todo closes on success / bubbles on failure. Mirrors
        ``RouteHandler._dispatch`` / ``StructureHandler._dispatch_relax``.
        """
        from precis.handlers.job import JobHandler

        requester_id = _as_int_or_none(requested_by)
        if requester_id is not None:
            from precis.handlers import _todo_guards as todo_guards

            todo_guards.check_parent_exists(self.store, requester_id)

        job_params = dict(params)
        job_params["target_node"] = node

        hub = self.hub if self.hub is not None else Hub(store=self.store)
        job_resp = JobHandler(hub=hub).put(
            job_type="fold",
            executor="ssh_node",
            parent_id=ref.id,  # the artifact owns the job (compute lane)
            params=job_params,
            # Collapse re-submits of the same fold onto one in-flight job.
            idem_key=params["cache_key"],
        )
        note = ""
        if requester_id is not None:
            self._wire_requester(requester_id, job_resp.body)
            note = f" (todo #{requester_id} will block on it)"
        return Response(
            body=(
                f"# protein '{ref.slug}' dispatched to {node}{note}\n\n"
                f"{job_resp.body}\n\n"
                f"The fold lands on the protein on completion. "
                f"Poll: get(kind='protein', id='{ref.slug}')."
            )
        )

    def _wire_requester(self, requester_id: int, job_resp_body: str) -> None:
        """Link the requesting todo to the job + arm its wait (ADR 0044).

        ``requester --requested--> job`` + inject a ``derived_job_succeeded``
        auto_check when the todo has none. Idempotent. Copied from
        ``RouteHandler._wire_requester``.
        """
        m = re.search(r"id=(\d+)", job_resp_body)
        if m is None:
            return
        job_id = int(m.group(1))
        with self.store.tx() as conn:
            self.store.add_link(
                src_ref_id=requester_id,
                dst_ref_id=job_id,
                relation="requested",
                set_by="system",
                conn=conn,
            )
            conn.execute(
                """
                UPDATE refs
                   SET meta = meta || jsonb_build_object(
                                'auto_check',
                                jsonb_build_object('type', 'derived_job_succeeded')
                              )
                 WHERE ref_id = %s
                   AND NOT (meta ? 'auto_check')
                """,
                (requester_id,),
            )

    # ── get ──────────────────────────────────────────────────────────
    def get(  # type: ignore[override]
        self,
        *,
        id: str | int | None = None,
        view: str | None = None,
        **_kw: Any,
    ) -> Response:
        if id is None or (isinstance(id, str) and id.strip() in ("", "/")):
            return self._render_list()
        ref = self.store.get_ref(kind="protein", id=str(id).strip())
        if ref is None:
            raise NotFound(f"protein {id!r} not found")
        meta = ref.meta or {}
        blob = meta.get("fold")
        if not blob:
            status = meta.get("status") or "folding"
            return Response(
                body=f"# protein '{ref.slug}' — {status}\n\n"
                f"engine: {meta.get('engine', '?')}\n"
                f"residues: {len(meta.get('sequence') or '')}\n\n"
                "(no fold yet — the compute job hasn't landed; poll again)"
            )
        fold = ProteinFold.from_json(blob)
        v = (view or "").strip().lower()
        if v in ("cif", "mmcif"):
            if not fold.cif.strip():
                return Response(body=f"# protein '{ref.slug}' — no structure model")
            return Response(body=fold.cif)
        if v == "structure":
            return self._converge_structure(ref, fold)
        if v and v not in ("fold", "summary"):
            raise BadInput(
                f"unknown protein view {view!r}",
                next="view='cif' (raw mmCIF) | view='structure' (3D structure "
                "projection) | omit for the fold summary",
            )
        return Response(body=fold.render())

    #: Above this atom count the O(N²) covalent-bond detector is skipped (the
    #: structure viewer then shows an element-coloured atom cloud). A single-
    #: chain fold is usually under it; a big complex is not.
    _BOND_DETECT_MAX = 600

    def _converge_structure(self, ref: Any, fold: ProteinFold) -> Response:
        """Project a fold's mmCIF into a derived ``structure`` ref (ADR 0043) so
        it renders in the 3D viewer. Content-slugged (``<protein>-fold``) +
        idempotent — a second call is a cache hit. Links protein↔structure via
        the ``has-fold-structure`` relation (asymmetric, DB-mirrored inverse).
        """
        if not fold.cif.strip():
            return Response(
                body=f"# protein '{ref.slug}' — no structure model to project"
            )
        slug = f"{ref.slug}-fold"
        existing = self.store.get_ref(kind="structure", id=slug)
        if existing is not None:
            return Response(
                body=f"# protein '{ref.slug}' → structure '{slug}' (cached)\n\n"
                f"View in 3D: /structure/{slug} · get(kind='structure', id='{slug}')"
            )

        from precis_bio.converge import cif_to_scene

        try:
            scene = cif_to_scene(fold.cif, detect_bonds_max=self._BOND_DETECT_MAX)
        except ValueError as exc:
            raise BadInput(f"could not build a structure from the fold: {exc}") from exc
        natoms = len(scene.atoms)
        comp = scene.composition()
        card = f"folded structure of protein {ref.slug}: {natoms} atoms " + " ".join(
            f"{el}{n}" for el, n in sorted(comp.items())
        )
        sref, _created = self.store.structure_save(
            slug=slug,
            title=f"{ref.title or ref.slug} (folded structure)",
            scene=scene,
            version=1,
            card_text=card,
            description=(
                f"AlphaFold projection of protein '{ref.slug}' "
                f"(non-periodic, mean pLDDT "
                f"{fold.plddt_mean:.1f})."
                if fold.plddt_mean is not None
                else f"AlphaFold projection of protein '{ref.slug}' (non-periodic)."
            ),
        )
        with self.store.tx() as conn:
            self.store.add_link(
                src_ref_id=ref.id,
                dst_ref_id=sref.id,
                relation="has-fold-structure",
                set_by="system",
                conn=conn,
            )
        if scene.bonds:
            bonds_note = f", {len(scene.bonds)} inferred bonds"
        elif natoms > self._BOND_DETECT_MAX:
            bonds_note = " (atoms only — too large for auto-bonds)"
        else:
            bonds_note = ""
        return Response(
            body=f"# protein '{ref.slug}' → structure '{slug}'\n\n"
            f"{natoms} atoms{bonds_note}. View in 3D: /structure/{slug}\n"
            f"get(kind='structure', id='{slug}')"
        )

    # ── delete ────────────────────────────────────────────────────────
    def delete(  # type: ignore[override]
        self, *, id: str | int | None = None, **_kw: Any
    ) -> Response:
        if id is None or not str(id).strip():
            raise BadInput("delete(kind='protein') requires id= (the protein slug)")
        ref = self.store.get_ref(kind="protein", id=str(id).strip())
        if ref is None:
            raise NotFound(f"protein {id!r} not found")
        self.store.soft_delete_ref(ref.id)
        return Response(body=f"retired protein '{ref.slug}'")

    # ── helpers ────────────────────────────────────────────────────────
    def _render_list(self) -> Response:
        proteins = self.store.list_refs(kind="protein", order_by="id_desc", limit=50)
        if not proteins:
            return Response(
                body="no proteins yet\n\nNext: put(kind='protein', id='insulin-a', "
                "sequence='GIVEQCCTSICSLYQLENYCN')"
            )
        lines = [f"# {len(proteins)} protein(s)"]
        for r in proteins:
            meta = r.meta or {}
            status = meta.get("status") or "?"
            plddt = meta.get("plddt_mean")
            conf = f"  pLDDT {plddt:.0f}" if isinstance(plddt, (int, float)) else ""
            nres = len(meta.get("sequence") or "")
            lines.append(f"- {r.slug}  [{status}]  {nres} aa{conf}")
        return Response(body="\n".join(lines))


def _as_int_or_none(v: Any) -> int | None:
    """Coerce a requester id to int, tolerating a ``todo:<n>`` / string id."""
    if v is None:
        return None
    raw = str(v).strip()
    raw = raw.split(":", 1)[1] if raw.startswith("todo:") else raw
    try:
        return int(raw)
    except (TypeError, ValueError):
        return None
