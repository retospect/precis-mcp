"""The canonical protein-fold IR (ADR 0056 slice 4).

One ``ProteinFold`` normalizes the output of *any* structure predictor
(AlphaFold3 de-novo, ColabFold MSA, …) into a single schema — the protein
sibling of ``precis_chem``'s ``RouteGraph`` ("swap the engine, keep the
IR"). Pure Python: **no bio dependencies** — the mmCIF the predictor emits
is parsed by a tiny whitespace scan of the ``_atom_site`` loop, so this
module imports cleanly on the always-on request path and the plugin loads
even when no folding stack is installed.

A fold result carries the predicted structure (the mmCIF ``cif`` text, with
per-atom pLDDT in the B-factor column) plus the scalar confidence summary
(``plddt_mean`` from the CIF, ``ptm`` / ``iptm`` from the predictor's
``summary_confidences.json``). It is serialized to JSON on
``refs.meta.fold`` and rendered to a markdown card the LLM reads via
``get(kind='protein', id=…)``; ``view='cif'`` returns the raw structure.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from typing import Any

#: Version of the serialized envelope. Bump when the JSON shape changes so a
#: reader can migrate old ``meta.fold`` blobs (forward insurance — no consumer
#: of old shapes exists yet).
IR_VERSION = 1

#: Fold modes. ``de-novo`` = single-sequence (AF3 ``--norun_data_pipeline``, no
#: MSA); ``msa`` = with a multiple-sequence alignment (ColabFold, slice 4c).
MODE_DE_NOVO = "de-novo"
MODE_MSA = "msa"

#: The standard 20 amino-acid one-letter codes + ``X`` (unknown). A sequence
#: is validated against this set before a (10-minute GPU) fold is dispatched.
_AA_ALPHABET = frozenset("ACDEFGHIKLMNPQRSTVWYX")


def normalize_sequence(sequence: str) -> str:
    """Canonicalize a protein sequence — upper-case, whitespace stripped.

    Deliberately lexical (no bio deps): strips every kind of whitespace
    (FASTA wrapping, tabs, newlines) and upper-cases. Validation that the
    residues are actually amino acids is :func:`validate_sequence` (called by
    the handler), kept separate so the IR stays a pure data type.
    """
    return "".join(str(sequence).split()).upper()


def validate_sequence(sequence: str) -> str:
    """Normalize + validate a protein sequence; raise ``ValueError`` if bad.

    A fold is an expensive GPU job, so a malformed sequence is rejected at
    ``put`` time rather than wasted on the node. Returns the normalized
    sequence on success.
    """
    seq = normalize_sequence(sequence)
    if not seq:
        raise ValueError("empty protein sequence")
    bad = sorted({c for c in seq if c not in _AA_ALPHABET})
    if bad:
        raise ValueError(
            f"invalid amino-acid code(s) {bad} — a sequence is the 20 standard "
            "one-letter codes (ACDEFGHIKLMNPQRSTVWY) plus X for unknown"
        )
    return seq


@dataclass(frozen=True, slots=True)
class ProteinFold:
    """A normalized predicted structure for ``sequence``."""

    name: str
    sequence: str
    engine: str
    engine_version: str
    #: ``de-novo`` (single-sequence) or ``msa`` — see the module constants.
    mode: str = MODE_DE_NOVO
    #: The predicted structure as mmCIF text (per-atom pLDDT in the B-factor
    #: column). Empty for a fold that produced no model.
    cif: str = ""
    #: Mean per-residue pLDDT (0–100) over Cα atoms, from the CIF B-factors.
    plddt_mean: float | None = None
    #: Predicted TM-score / interface pTM, from ``summary_confidences.json``.
    ptm: float | None = None
    iptm: float | None = None
    #: Overall ranking score the predictor assigned this sample, when reported.
    ranking_score: float | None = None
    #: Number of residues in the folded chain (``len(sequence)``).
    n_residues: int = 0
    #: The random seeds the prediction ran with (part of the content address).
    seeds: list[int] = field(default_factory=list)
    #: Free-form engine provenance (image digest, model dir, sample dirs, …).
    provenance: dict[str, Any] = field(default_factory=dict)

    @property
    def folded(self) -> bool:
        """True when the predictor produced a structure (a non-empty model)."""
        return bool(self.cif.strip())

    # ── serialization ────────────────────────────────────────────────
    def to_json(self) -> dict[str, Any]:
        return {
            "version": IR_VERSION,
            "name": self.name,
            "sequence": self.sequence,
            "engine": self.engine,
            "engine_version": self.engine_version,
            "mode": self.mode,
            "cif": self.cif,
            "plddt_mean": self.plddt_mean,
            "ptm": self.ptm,
            "iptm": self.iptm,
            "ranking_score": self.ranking_score,
            "n_residues": self.n_residues,
            "seeds": list(self.seeds),
            "provenance": self.provenance,
        }

    @classmethod
    def from_json(cls, d: dict[str, Any]) -> ProteinFold:
        return cls(
            name=str(d.get("name", "?")),
            sequence=str(d.get("sequence", "")),
            engine=str(d.get("engine", "?")),
            engine_version=str(d.get("engine_version", "?")),
            mode=str(d.get("mode", MODE_DE_NOVO)),
            cif=str(d.get("cif", "")),
            plddt_mean=d.get("plddt_mean"),
            ptm=d.get("ptm"),
            iptm=d.get("iptm"),
            ranking_score=d.get("ranking_score"),
            n_residues=int(d.get("n_residues", 0)),
            seeds=[int(s) for s in d.get("seeds", [])],
            provenance=dict(d.get("provenance", {})),
        )

    # ── renders ──────────────────────────────────────────────────────
    def render(self) -> str:
        """A markdown fold summary the LLM reads (the scalar confidences +
        sequence; the raw structure is ``view='cif'``, not inlined)."""
        state = "folded" if self.folded else "no model"
        head = (
            f"# protein → {self.name}\n"
            f"engine: {self.engine} ({self.engine_version}) · {self.mode} · {state}"
        )
        lines = [head, "", f"residues: {self.n_residues}"]
        if self.plddt_mean is not None:
            lines.append(
                f"mean pLDDT: {self.plddt_mean:.1f}  ({_plddt_band(self.plddt_mean)})"
            )
        if self.ptm is not None:
            lines.append(f"pTM: {self.ptm:.3f}")
        if self.iptm is not None:
            lines.append(f"ipTM: {self.iptm:.3f}")
        if self.ranking_score is not None:
            lines.append(f"ranking score: {self.ranking_score:.3f}")
        lines += ["", "sequence:", self._wrapped_sequence()]
        if self.folded:
            lines += ["", "structure: mmCIF available — get(view='cif')"]
        return "\n".join(lines)

    def _wrapped_sequence(self, width: int = 60) -> str:
        return "\n".join(
            self.sequence[i : i + width] for i in range(0, len(self.sequence), width)
        )

    def card_text(self) -> str:
        """Plain text embedded into the ``card_combined`` search chunk — the
        sequence (so a fold surfaces on a sequence/name query)."""
        return f"predicted protein structure {self.name}\n{self.sequence}"


def _plddt_band(plddt: float) -> str:
    """AF confidence bands for a mean-pLDDT gloss (0–100 scale)."""
    if plddt >= 90:
        return "very high"
    if plddt >= 70:
        return "confident"
    if plddt >= 50:
        return "low"
    return "very low"


def mean_plddt_from_cif(cif: str) -> float | None:
    """Mean Cα B-factor (= per-residue pLDDT) from an mmCIF ``_atom_site`` loop.

    A dependency-free scan: locate the ``_atom_site`` loop header block, find
    the ``label_atom_id`` and ``B_iso_or_equiv`` column ordinals, then average
    the B-factor over Cα rows. Best-effort — returns ``None`` if the loop or
    those columns aren't found (a garbled/empty CIF), so a fold still lands its
    ptm/iptm even when the pLDDT can't be read.
    """
    lines = cif.splitlines()
    headers: list[str] = []
    i = 0
    n = len(lines)
    # Find a `loop_` whose header block is the _atom_site columns.
    while i < n:
        if lines[i].strip() == "loop_":
            headers = []
            j = i + 1
            while j < n and lines[j].lstrip().startswith("_atom_site."):
                headers.append(lines[j].strip())
                j += 1
            if headers:
                i = j
                break
            i = j
        else:
            i += 1
    if not headers:
        return None
    try:
        atom_col = headers.index("_atom_site.label_atom_id")
        b_col = headers.index("_atom_site.B_iso_or_equiv")
    except ValueError:
        return None

    total = 0.0
    count = 0
    ncols = len(headers)
    while i < n:
        row = lines[i].strip()
        if not row or row.startswith(("_", "#", "loop_", "data_")):
            break
        fields = row.split()
        if len(fields) < ncols:
            i += 1
            continue
        if fields[atom_col] == "CA":
            try:
                total += float(fields[b_col])
                count += 1
            except ValueError:
                pass
        i += 1
    if count == 0:
        return None
    return total / count


def fold_cache_key(
    *,
    sequence: str,
    engine: str,
    engine_version: str,
    mode: str = MODE_DE_NOVO,
    seeds: list[int] | None = None,
) -> str:
    """Content address for a fold (ADR 0056 §6 / ADR 0007).

    Same ``(sequence, engine, engine_version, mode, seeds)`` ⇒ same key ⇒ zero
    recompute. The engine *version* (image digest in prod) invalidates the
    cache when the model weights change. Returned as ``fold:<sha256[:16]>``.
    """
    payload = json.dumps(
        {
            "q": normalize_sequence(sequence),
            "e": engine,
            "v": engine_version,
            "m": mode,
            "s": sorted(int(x) for x in (seeds or [])),
        },
        sort_keys=True,
    )
    digest = hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]
    return f"fold:{digest}"
