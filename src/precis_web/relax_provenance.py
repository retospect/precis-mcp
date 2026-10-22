"""Was this structure relaxed, and how — read off the records, never the prose.

gr504809: an se design page bound six structures and nothing on it said
whether, or how, any of them had been relaxed. The relax facts exist in two
places, both written by precis's own relax paths:

* ``refs.meta['last_relax']`` — stamped by ``structure_save(relax_summary=…)``
  when an in-request relax rewrote the stored atoms; a later plain save drops
  it (it is a structure-owned meta key), so its presence means the atoms on
  disk ARE that relax's output.
* ``struct_runs`` — one row per relax. A dispatched rung (xtb/ml/dft) records
  its relaxed geometry on the row and does NOT rewrite the stored atoms, so a
  run on the current version without a matching ``last_relax`` means "a
  relaxed geometry exists, the viewer shows the input". ``provenance =
  'external'`` rows carry imported numbers.

A geometry relaxed outside precis (e.g. an RDKit UFF pass in a scratch
builder, then saved as plain atoms) leaves neither record — only whatever the
author typed into the description. That text is shown, labelled unchecked,
and is never parsed into a relax claim: reading "UFF-relaxed" out of free
text would make an unrecorded relax look recorded, which is the confusion
this module exists to remove.
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import quote

from precis.errors import NotFound
from precis.handlers._slug_ref_shared import resolve_live_slug_ref
from precis.utils import handle_registry

#: Rungs that only repair geometry — no force field, no energy.
REPAIR_RUNGS = frozenset({"clean", "geo"})

#: Display order and plural phrase for the design-level summary line.
_STATE_PHRASES: tuple[tuple[str, str], ...] = (
    ("relaxed", "relaxed"),
    ("unconverged", "relax not converged"),
    ("run-not-applied", "relaxed geometry not applied"),
    ("external", "external relax data"),
    ("repaired", "geometry repair only"),
    ("none", "no relax recorded"),
    ("missing", "binding does not resolve"),
)

_TONES = {
    "relaxed": "success",
    "external": "info",
    "unconverged": "warning",
    "run-not-applied": "warning",
    "repaired": "warning",
    "none": "danger",
    "missing": "danger",
}


@dataclass(frozen=True)
class RelaxState:
    """One structure's relax verdict: ``state`` (a key of ``_TONES``), a
    one-line ``headline``, a ``detail`` sentence saying what the stored atoms
    are, the run that backs it, and ``record`` — the same facts as plain
    JSON for export metadata."""

    state: str
    headline: str
    detail: str
    run_id: int | None = None
    record: dict[str, Any] = field(default_factory=dict)

    @property
    def tone(self) -> str:
        return _TONES.get(self.state, "warning")


@dataclass(frozen=True)
class BoundStructure:
    """A structure-bound se block and its bound structure's relax verdict."""

    block: str
    slug: str
    relax: RelaxState
    ref_id: int | None = None
    handle: str | None = None
    version: int | None = None
    composition: dict[str, int] = field(default_factory=dict)
    description: str = ""

    @property
    def href(self) -> str | None:
        return f"/structure/{quote(self.slug, safe='')}" if self.ref_id else None

    @property
    def run_href(self) -> str | None:
        if self.ref_id is None or self.relax.run_id is None:
            return None
        return f"{self.href}/run/{self.relax.run_id}"


def _rung_label(rung: str) -> str:
    # Lazy: routes.structure imports modules that may import this one.
    from precis_web.routes.structure import rung_info

    return rung_info(rung)["label"]


def _fmt_num(v: Any) -> str:
    return f"{float(v):.3g}"


def _calc_bits(model: Any, max_force: Any, fmax: Any) -> str:
    out = ""
    if model:
        out += f" · {model}"
    if max_force is not None:
        out += f" · max force {_fmt_num(max_force)} eV/Å"
    if fmax is not None:
        out += f" · target fmax {_fmt_num(fmax)} eV/Å"
    return out


def _method_summary(method: Any) -> str:
    if not isinstance(method, dict) or not method:
        return "method not recorded"
    keys = ("functional", "code", "model", "dataset", "dataset_doi")
    parts = [f"{k} {method[k]}" for k in keys if method.get(k)]
    return ", ".join(parts) or ", ".join(f"{k} {v}" for k, v in sorted(method.items()))


def relax_state(meta: dict[str, Any] | None, runs: list[dict[str, Any]]) -> RelaxState:
    """The verdict for one structure from its ``refs.meta`` and its
    ``struct_runs`` rows (dicts: ``id``, ``fidelity``, ``status``, ``model``,
    ``on_version``, ``converged``, ``n_steps``, ``max_force``,
    ``provenance``, ``params``, ``method``, ``has_geometry``), any order."""
    meta = meta or {}
    version = meta.get("version")
    lr = meta.get("last_relax") if isinstance(meta.get("last_relax"), dict) else None
    done = sorted(
        (r for r in runs if (r.get("status") or "succeeded") == "succeeded"),
        key=lambda r: int(r["id"]),
        reverse=True,
    )
    current = [r for r in done if version is not None and r["on_version"] == version]
    computed = [r for r in current if (r.get("provenance") or "computed") == "computed"]
    earlier = sum(
        1 for r in done if version is not None and r["on_version"] < int(version)
    )
    tail = (
        f" {earlier} earlier relax run{'s' if earlier != 1 else ''} "
        "describe older versions of these atoms."
        if earlier
        else ""
    )

    if lr and lr.get("rung"):
        rung = str(lr["rung"])
        match = next((r for r in computed if r["fidelity"] == rung), None)
        fmax = ((match or {}).get("params") or {}).get("fmax")
        model = lr.get("model") or (match or {}).get("model")
        converged = bool(lr.get("converged"))
        n = lr.get("n_steps")
        label = _rung_label(rung)
        record = {
            "state": "",
            "rung": rung,
            "rung_label": label,
            "model": model,
            "converged": converged,
            "n_steps": n,
            "max_force_eV_per_A": lr.get("max_force"),
            "fmax_eV_per_A": fmax,
            "run_id": match["id"] if match else None,
            "structure_version": version,
        }
        detail = "The stored atoms are this relax's output" + (
            f" (run r{match['id']})." if match else "."
        )
        later = next(
            (
                r
                for r in computed
                if r["fidelity"] != rung
                and r.get("has_geometry")
                and (match is None or int(r["id"]) > int(match["id"]))
            ),
            None,
        )
        if later is not None:
            detail += (
                f" A later {later['fidelity']} relax (r{later['id']}) also ran on "
                "this version; its relaxed atoms are in that run, not applied here."
            )
        if lr.get("note"):
            detail += f" Note: {lr['note']}"
        if rung in REPAIR_RUNGS:
            state = "repaired"
            headline = (
                f"geometry repair only · {rung} ({label}) — no force field, "
                f"no energy · {'converged' if converged else 'not converged'}"
                + (f" in {n} steps" if n is not None else "")
            )
        elif not converged:
            state = "unconverged"
            headline = (
                f"relax not converged · {rung} ({label})"
                + (f" · stopped after {n} steps" if n is not None else "")
                + _calc_bits(model, lr.get("max_force"), fmax)
            )
            detail += " Not a minimum: treat the atoms as a snapshot."
        else:
            state = "relaxed"
            headline = (
                f"relaxed · {rung} ({label}) · converged"
                + (f" in {n} steps" if n is not None else "")
                + _calc_bits(model, lr.get("max_force"), fmax)
            )
        record["state"] = state
        return RelaxState(state, headline, detail + tail, record["run_id"], record)

    external = next((r for r in current if r.get("provenance") == "external"), None)
    if external is not None:
        summary = _method_summary(external.get("method"))
        return RelaxState(
            "external",
            f"external relax data · {external['fidelity']} · {summary}",
            "Imported numbers from an outside dataset, not a relax run here." + tail,
            int(external["id"]),
            {
                "state": "external",
                "rung": external["fidelity"],
                "method": external.get("method"),
                "run_id": int(external["id"]),
                "structure_version": version,
            },
        )

    pending = next((r for r in computed if r.get("has_geometry")), None)
    if pending is not None:
        rung = str(pending["fidelity"])
        label = _rung_label(rung)
        fmax = (pending.get("params") or {}).get("fmax")
        return RelaxState(
            "run-not-applied",
            f"relaxed geometry not applied · {rung} ({label}) run r{pending['id']} · "
            f"{'converged' if pending.get('converged') else 'not converged'}"
            + _calc_bits(pending.get("model"), pending.get("max_force"), fmax),
            f"A {rung} relax ran on these atoms; its relaxed atoms are stored in "
            f"run r{pending['id']}, not in the structure — the atoms shown here "
            "are the unrelaxed input." + tail,
            int(pending["id"]),
            {
                "state": "run-not-applied",
                "rung": rung,
                "rung_label": label,
                "model": pending.get("model"),
                "converged": bool(pending.get("converged")),
                "n_steps": pending.get("n_steps"),
                "max_force_eV_per_A": pending.get("max_force"),
                "fmax_eV_per_A": fmax,
                "run_id": int(pending["id"]),
                "structure_version": version,
            },
        )

    return RelaxState(
        "none",
        "no relax recorded",
        "precis has no relax on record for these atoms: they are exactly as "
        "saved. A relax done outside precis is not recorded here, even when "
        "the description says so." + tail,
        None,
        {"state": "none", "structure_version": version},
    )


_RUNS_SQL = """
    SELECT ref_id, id, fidelity, status, model, on_version, converged,
           n_steps, max_force, provenance, params, method,
           (final_geometry IS NOT NULL) AS has_geometry
      FROM struct_runs
     WHERE ref_id = ANY(%s)
"""


def _runs_by_ref(store: Any, ref_ids: list[int]) -> dict[int, list[dict[str, Any]]]:
    out: dict[int, list[dict[str, Any]]] = {}
    if not ref_ids:
        return out
    with store.pool.connection() as conn:
        rows = conn.execute(_RUNS_SQL, (ref_ids,)).fetchall()
    for r in rows:
        out.setdefault(int(r[0]), []).append(
            {
                "id": int(r[1]),
                "fidelity": r[2],
                "status": r[3],
                "model": r[4],
                "on_version": int(r[5]),
                "converged": bool(r[6]),
                "n_steps": r[7],
                "max_force": float(r[8]) if r[8] is not None else None,
                "provenance": r[9],
                "params": r[10] or {},
                "method": r[11],
                "has_geometry": bool(r[12]),
            }
        )
    return out


def structure_relax(store: Any, ref: Any) -> RelaxState:
    """The verdict for one live ``structure`` ref."""
    return relax_state(
        ref.meta or {}, _runs_by_ref(store, [int(ref.id)]).get(int(ref.id), [])
    )


def bound_structures(store: Any, tree: Any) -> list[BoundStructure]:
    """Every structure-bound block of ``tree`` (sorted by block name) with
    its bound structure's CURRENT relax verdict — the structure's state
    today, also when ``tree`` is a past revision."""
    bound: list[tuple[str, str, Any]] = []
    for name in sorted(tree.blocks):
        node = tree.blocks[name]
        slug = getattr(node, "bound", None)
        if getattr(node, "bound_kind", None) != "structure" or not slug:
            continue
        try:
            ref = resolve_live_slug_ref(store, kind="structure", id=slug)
        except NotFound:
            ref = None
        bound.append((name, str(slug), ref))
    runs = _runs_by_ref(store, sorted({int(r.id) for _n, _s, r in bound if r}))
    out: list[BoundStructure] = []
    for name, slug, ref in bound:
        if ref is None:
            out.append(
                BoundStructure(
                    block=name,
                    slug=slug,
                    relax=RelaxState(
                        "missing",
                        "bound structure not found",
                        f"No live structure named {slug!r}; nothing to say about "
                        "its relaxation.",
                        record={"state": "missing"},
                    ),
                )
            )
            continue
        meta = ref.meta or {}
        comp = meta.get("composition")
        out.append(
            BoundStructure(
                block=name,
                slug=str(ref.slug or slug),
                relax=relax_state(meta, runs.get(int(ref.id), [])),
                ref_id=int(ref.id),
                handle=handle_registry.try_format("structure", ref.id, chunk=False),
                version=meta.get("version"),
                composition={str(k): int(v) for k, v in comp.items()}
                if isinstance(comp, dict)
                else {},
                description=str(meta.get("description") or ""),
            )
        )
    return out


def summary_line(rows: list[BoundStructure]) -> str:
    """``"6 bound structures: 1 geometry repair only, 5 no relax recorded"``."""
    counts = Counter(r.relax.state for r in rows)
    parts = [f"{counts[s]} {phrase}" for s, phrase in _STATE_PHRASES if counts[s]]
    n = len(rows)
    return f"{n} bound structure{'s' if n != 1 else ''}: " + ", ".join(parts)


def formula(composition: dict[str, int]) -> str:
    """Hill-order formula (C, H first, then alphabetical) from element counts."""
    if not composition:
        return ""
    order: list[str] = [
        e for e in ("C", "H") if e in composition and "C" in composition
    ]
    order += sorted(e for e in composition if e not in order)
    return "".join(f"{e}{composition[e] if composition[e] != 1 else ''}" for e in order)
