"""AlphaFold3 container plumbing (ADR 0056 slice 4).

Three pure, gate-testable pieces — the input builder, the ``docker run`` argv
builder, and the output parser — so the container's *shape* is validated
without a GPU or the image (the live run is exercised through the
``RUNNER``/``STAGER`` hooks in ``jobs.py``, the ``struct_relax`` pattern).

Grounded on the **real working install on spark** (memory:
alphafold-spark-facts): AlphaFold3 v3.0.1, image ``alphafold3:ready`` (ARM64,
NVIDIA GB10). De-novo / single-sequence mode (``--norun_data_pipeline``, empty
MSA/templates) — no 300 GB databases, ~10 min/157aa. JSON-in / mmCIF-out.

**Input.** :func:`build_af3_input` writes the AF3-dialect JSON: one protein
chain, empty MSA/templates (de-novo).

**Argv.** :func:`build_fold_argv` is the ``docker run`` command the dispatch
ssh's to the fold node: the input JSON in, the model + XLA caches mounted, the
output tree out under the bind-mounted ``/output``.

**Parser.** :func:`parse_af3_output` scans the output tree for the model mmCIF
(``*_model.cif``) and the ``*_summary_confidences.json`` — defensively (an
``rglob``, since the exact subdir/lowercasing is verified only at the first
live run) — into our :class:`~precis_bio.ir.ProteinFold`.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from precis.utils.container_limits import container_limit_flags
from precis_bio.ir import (
    MODE_DE_NOVO,
    ProteinFold,
    mean_plddt_from_cif,
    normalize_sequence,
)

#: Bind-mount points inside the AF3 container (matching the working invocation).
CONTAINER_INPUT = "/input/protein.json"
CONTAINER_MODELS = "/models"
CONTAINER_OUTPUT = "/output"
CONTAINER_XLA_CACHE = "/root/.cache/xla_extension"

#: The AF3 input filename staged into the shared in-dir.
INPUT_FILE = "protein.json"


def build_af3_input(
    name: str, sequence: str, *, seeds: list[int] | None = None
) -> dict[str, Any]:
    """The AlphaFold3-dialect input JSON for one single-chain de-novo fold.

    Empty ``unpairedMsa``/``pairedMsa``/``templates`` select single-sequence
    inference (paired with ``--norun_data_pipeline`` on the argv). Chain id
    ``A`` — one protein entity.
    """
    return {
        "name": name,
        "modelSeeds": [int(s) for s in (seeds or [1])],
        "dialect": "alphafold3",
        "version": 1,
        "sequences": [
            {
                "protein": {
                    "id": "A",
                    "sequence": normalize_sequence(sequence),
                    "unpairedMsa": "",
                    "pairedMsa": "",
                    "templates": [],
                }
            }
        ],
    }


def build_fold_argv(
    *,
    ref_id: int,
    in_dir: str,
    out_dir: str,
    image: str,
    models_dir: str,
    container_cmd: str = "docker",
    xla_cache_dir: str | None = None,
    mem_limit: str | None = None,
) -> list[str]:
    """The ``docker run`` argv for one AF3 de-novo fold (pure, testable).

    Deterministic ``--name precis-fold-<ref_id>`` so a sweeper can kill it by
    name. ``--gpus all`` (the GB10), the input JSON + models mounted read-only,
    the output tree writable, and (when given) a persistent XLA cache to skip
    the ~5-10 min recompile. The command line mirrors ``run_alphafold3.sh`` on
    spark verbatim; ``models_dir`` is required (the weights are mounted, never
    baked — ADR 0056 §5).

    ``mem_limit`` (a docker size string like ``"100g"``, from
    ``PRECIS_FOLD_MEM_LIMIT``) caps the container's memory so an AF3 XLA-compile
    spike can't exhaust the node — the GB10 is a DGX Spark with **unified**
    CPU+GPU LPDDR5X, so an uncapped fold can starve the resident worker. Sets
    ``--memory`` + an equal ``--memory-swap`` (no swap thrashing on top of the
    cap). ``None`` ⇒ no cap (unchanged default). Cheap insurance; a fold on a
    21-mer stays well under any sane cap, and the operator sizes it in the role.
    """
    argv = [
        container_cmd,
        "run",
        "--rm",
        "--gpus",
        "all",
        "--name",
        f"precis-fold-{ref_id}",
    ]
    argv += container_limit_flags()
    if mem_limit:
        argv += ["--memory", mem_limit, "--memory-swap", mem_limit]
    argv += [
        "-v",
        f"{Path(in_dir) / INPUT_FILE}:{CONTAINER_INPUT}:ro",
        "-v",
        f"{models_dir}:{CONTAINER_MODELS}:ro",
        "-v",
        f"{out_dir}:{CONTAINER_OUTPUT}",
    ]
    if xla_cache_dir:
        argv += ["-v", f"{xla_cache_dir}:{CONTAINER_XLA_CACHE}"]
    argv += [
        image,
        "python3",
        "run_alphafold.py",
        f"--json_path={CONTAINER_INPUT}",
        f"--model_dir={CONTAINER_MODELS}",
        f"--output_dir={CONTAINER_OUTPUT}",
        "--norun_data_pipeline",
        "--flash_attention_implementation=xla",
    ]
    return argv


def _as_float(v: Any) -> float | None:
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _find_one(out_dir: str, suffix: str) -> Path | None:
    """The (lexically first) file under ``out_dir`` whose name ends ``suffix``.

    An ``rglob`` walk, not a fixed path: AF3 nests the outputs under a
    ``<name>/`` subdir whose exact casing is verified only at the first live
    run, so scanning is the robust read (memory flags the naming as unconfirmed).
    """
    matches = sorted(Path(out_dir).rglob(f"*{suffix}"))
    return matches[0] if matches else None


def parse_af3_output(
    out_dir: str,
    *,
    name: str,
    sequence: str,
    engine: str = "alphafold3",
    engine_version: str = "af3",
    mode: str = MODE_DE_NOVO,
    seeds: list[int] | None = None,
) -> ProteinFold:
    """Read an AF3 output tree into a :class:`ProteinFold` (best-effort).

    Finds the model mmCIF + the summary-confidences JSON by suffix scan; pulls
    ptm/iptm/ranking_score/has_clash from the summary (defensive ``.get`` —
    the key names are confirmed only at the first live run) and the mean pLDDT
    from the CIF's Cα B-factors. Missing pieces degrade to ``None`` / an empty
    model rather than raising, so a partial run still lands what it has.
    """
    seq = normalize_sequence(sequence)
    cif_path = _find_one(out_dir, "_model.cif")
    summary_path = _find_one(out_dir, "_summary_confidences.json")

    cif = cif_path.read_text() if cif_path else ""
    summary: dict[str, Any] = {}
    if summary_path:
        try:
            loaded = json.loads(summary_path.read_text())
            if isinstance(loaded, dict):
                summary = loaded
        except (ValueError, OSError):
            summary = {}

    provenance: dict[str, Any] = {
        "engine": engine,
        "out_dir": str(out_dir),
        "model_cif": cif_path.name if cif_path else None,
        "summary": summary_path.name if summary_path else None,
        "has_clash": summary.get("has_clash"),
        "fraction_disordered": summary.get("fraction_disordered"),
    }

    return ProteinFold(
        name=name,
        sequence=seq,
        engine=engine,
        engine_version=engine_version,
        mode=mode,
        cif=cif,
        plddt_mean=mean_plddt_from_cif(cif) if cif else None,
        ptm=_as_float(summary.get("ptm")),
        iptm=_as_float(summary.get("iptm")),
        ranking_score=_as_float(summary.get("ranking_score")),
        n_residues=len(seq),
        seeds=[int(s) for s in (seeds or [])],
        provenance=provenance,
    )
