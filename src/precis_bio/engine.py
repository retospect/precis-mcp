"""Structure-prediction engine port + the built-in engines (ADR 0056 slice 4).

The engine is the swappable leaf behind the ``protein`` kind. A
:class:`FoldEngine` maps a sequence to a normalized
:class:`~precis_bio.ir.ProteinFold`; adding ColabFold (MSA mode, slice 4c) is
a new adapter, never a change to the kind or the verb surface.

Two **transports** (the ``precis_chem`` split, minus ``service``) — how the
engine actually runs. The ``fold`` job dispatches on this, not the name:

* **inprocess** — the deterministic :class:`StubFoldEngine`: no bio deps, no
  GPU, so the whole compute-lane round-trip + the content-addressed cache are
  gate-testable without a cluster (catpath's in-process EMT analogue).
* **container** — a one-shot GPU container the ``fold`` job runs on the fold
  node (the ``struct_relax`` / ``retrosynth`` pattern): the dispatch stages the
  AF3 input JSON, ssh's a ``docker run`` to the node, and parses the mmCIF +
  summary-confidences it drops. :class:`AlphaFold3Engine`. jax/AF3/CUDA live
  **inside the image**, never here, so this module imports clean with no extra.
"""

from __future__ import annotations

import os
from typing import Protocol, runtime_checkable

from precis_bio.ir import MODE_DE_NOVO, ProteinFold, normalize_sequence

#: The engine transports the ``fold`` dispatch knows how to run.
TRANSPORT_INPROCESS = "inprocess"
TRANSPORT_CONTAINER = "container"

#: Default random seed when a caller names none (AF3 ``modelSeeds``).
DEFAULT_SEEDS = [1]

#: Env naming the fold node's model directory (mounted read-only into the
#: container). On spark: ``/home/reto/alphafold3/models`` (world-readable).
FOLD_MODELS_ENV = "PRECIS_FOLD_MODELS_DIR"
#: Env overriding the AF3 image tag (default matches the spark install).
FOLD_IMAGE_ENV = "PRECIS_FOLD_IMAGE"
#: Env naming a persistent host XLA cache dir (skips the ~5-10 min recompile).
FOLD_XLA_CACHE_ENV = "PRECIS_FOLD_XLA_CACHE"


@runtime_checkable
class FoldEngine(Protocol):
    """A predictor that produces a normalized structure for a sequence."""

    name: str
    version: str
    #: How the engine runs: ``inprocess`` / ``container``. The dispatch branches.
    transport: str
    #: The fold mode this engine runs (``de-novo`` / ``msa``).
    mode: str
    #: Back-compat alias — ``True`` iff :attr:`transport` is ``container``.
    is_container: bool

    def fold(self, sequence: str, *, seeds: list[int] | None = None) -> ProteinFold:
        """Return a fold for ``sequence`` (in-process engines only)."""
        ...


class StubFoldEngine:
    """A deterministic, GPU-free toy predictor — the slice-0 fallback.

    It does **no real folding**: it emits a tiny fixed mmCIF (two Cα atoms) and
    a constant pLDDT so the output is reproducible across runs (tests + the
    ``PRECIS_FOLD_NODE``-unset inline path). Provenance stamped ``stub`` so a
    stub fold is never mistaken for a predicted one. Its only job is to exercise
    the substrate: mint → fold → write-back → cache hit.
    """

    name = "stub"
    version = "stub-v1"
    transport = TRANSPORT_INPROCESS
    mode = MODE_DE_NOVO
    is_container = False

    def fold(self, sequence: str, *, seeds: list[int] | None = None) -> ProteinFold:
        seq = normalize_sequence(sequence)
        cif = _stub_cif(seq)
        return ProteinFold(
            name="stub",
            sequence=seq,
            engine=self.name,
            engine_version=self.version,
            mode=self.mode,
            cif=cif,
            plddt_mean=50.0,
            ptm=0.5,
            iptm=None,
            ranking_score=0.5,
            n_residues=len(seq),
            seeds=list(seeds or DEFAULT_SEEDS),
            provenance={"engine": "stub", "note": "deterministic placeholder"},
        )


def _stub_cif(sequence: str) -> str:
    """A minimal but real mmCIF the pLDDT scanner can parse (two Cα atoms at a
    constant B-factor = 50). Deterministic — no chemistry, no coordinates that
    mean anything; it only exercises the parse + write-back path."""
    header = "data_stub\nloop_\n" + "\n".join(
        "_atom_site." + c
        for c in (
            "group_PDB",
            "id",
            "label_atom_id",
            "label_comp_id",
            "label_asym_id",
            "label_seq_id",
            "Cartn_x",
            "Cartn_y",
            "Cartn_z",
            "B_iso_or_equiv",
        )
    )
    rows = [
        f"ATOM {i + 1} CA ALA A {i + 1} {i * 3.8:.3f} 0.000 0.000 50.00"
        for i in range(min(2, max(1, len(sequence))))
    ]
    return header + "\n" + "\n".join(rows) + "\n"


class AlphaFold3Engine:
    """AlphaFold3 adapter — container engine (ADR 0056 slice 4).

    AF3 runs in the ``alphafold3:ready`` image on the GPU fold node
    (grounded on the real spark install); it is **not** run in-process
    (containerizing keeps the jax/CUDA env + the 1 GB weights off the always-on
    workers). So :meth:`fold` raises — the ``fold`` job routes container engines
    to ``precis_bio.jobs._run_container``, which builds the ``docker run`` argv
    (:meth:`run_argv`) and parses the mmCIF + summary-confidences (:meth:`parse`).
    De-novo / single-sequence mode. The remaining live wiring is a node-deploy
    concern (slice 4b: ``PRECIS_FOLD_NODE`` + the models mount).
    """

    name = "alphafold3"
    version = "af3-v3.0.1"
    transport = TRANSPORT_CONTAINER
    mode = MODE_DE_NOVO
    is_container = True

    @property
    def image(self) -> str:
        """The AF3 image tag (overridable at deploy via :data:`FOLD_IMAGE_ENV`)."""
        return os.environ.get(FOLD_IMAGE_ENV) or "alphafold3:ready"

    def run_argv(
        self,
        *,
        ref_id: int,
        in_dir: str,
        out_dir: str,
        name: str,
        sequence: str,
        seeds: list[int],
        container_cmd: str,
        models_dir: str,
    ) -> list[str]:
        """The ``docker run`` argv for one fold (delegates to the AF3 argv
        builder; lazy import keeps this module bio-dep-free). ``name``/
        ``sequence``/``seeds`` are consumed when the input JSON is staged, not
        on the command line — the argv only names the mounts + entrypoint."""
        from precis_bio.alphafold import build_fold_argv

        return build_fold_argv(
            ref_id=ref_id,
            in_dir=in_dir,
            out_dir=out_dir,
            image=self.image,
            models_dir=models_dir,
            container_cmd=container_cmd,
            xla_cache_dir=os.environ.get(FOLD_XLA_CACHE_ENV) or None,
        )

    def build_input(
        self, *, name: str, sequence: str, seeds: list[int]
    ) -> dict[str, object]:
        """The AF3 input JSON staged into the in-dir (lazy import)."""
        from precis_bio.alphafold import build_af3_input

        return build_af3_input(name, sequence, seeds=seeds)

    def parse(
        self,
        out_dir: str,
        *,
        name: str,
        sequence: str,
        seeds: list[int],
    ) -> ProteinFold:
        """Parse the AF3 output tree into a :class:`ProteinFold` (lazy import)."""
        from precis_bio.alphafold import parse_af3_output

        return parse_af3_output(
            out_dir,
            name=name,
            sequence=sequence,
            engine=self.name,
            engine_version=self.version,
            mode=self.mode,
            seeds=seeds,
        )

    def fold(self, sequence: str, *, seeds: list[int] | None = None) -> ProteinFold:
        raise NotImplementedError(
            "alphafold3 is a container engine: the fold job runs it via "
            "`docker run` on the fold node (precis_bio.jobs._run_container), "
            "not in-process. Set PRECIS_FOLD_NODE + PRECIS_FOLD_MODELS_DIR to "
            "enable it (slice 4b)."
        )


#: Registry of built-in engines by name. ColabFold (MSA) extends this at 4c.
_ENGINES: dict[str, type] = {
    StubFoldEngine.name: StubFoldEngine,
    AlphaFold3Engine.name: AlphaFold3Engine,
}

#: Default engine when a caller names none. ``stub`` keeps the merge inert +
#: gate-testable; a real fold names ``alphafold3`` (+ a configured fold node).
DEFAULT_ENGINE = StubFoldEngine.name


def resolve_engine(name: str | None) -> FoldEngine:
    """Resolve a fold engine by name (default :data:`DEFAULT_ENGINE`)."""
    key = (name or DEFAULT_ENGINE).strip().lower()
    cls = _ENGINES.get(key)
    if cls is None:
        raise ValueError(f"unknown fold engine {name!r}; known: {sorted(_ENGINES)}")
    return cls()  # type: ignore[return-value]
