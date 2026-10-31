"""Kinetics v2 slice 10 glue that needs NO engine (D10): ``view='manifest'``
text, the ``put(mode='revalidate')`` refusals and mint, the views/modes
declaration and the job type's executors.  CI runs without the ``catalyst``
extra, so this file must never import ``autocatpath`` — the engine-backed
acceptance replays live in ``test_pathway_revalidate.py``.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from precis.errors import BadInput
from precis.workers.executors import EXECUTOR_PROVIDES, ZERO_LLM_EXECUTORS
from precis_pathway import manifest as vm
from precis_pathway import revalidate_job
from precis_pathway.handler import PathwayHandler

MID = "a" * 64


def _record(*, engine_sha: str | None = "b" * 40) -> dict[str, Any]:
    """A stored manifest as ``to_json`` writes it (numbers only)."""
    cell = {"rule": "log10", "value": 0.01}
    return {
        "schema": "validation_manifest",
        "schema_version": 1,
        "manifest_id": MID,
        "code": {
            "precis_sha": "c" * 40,
            "autocatpath_version": "0.38.0",
            "autocatpath_sha": engine_sha,
        },
        "lock": {"uv_lock_sha256": "d" * 64, "lock_source": "uv.lock", "mlip": []},
        "platform": {"arch": "x86_64", "os": "linux", "accelerator": "cpu"},
        "seeds": {
            "search_seeds": [0],
            "kinetics_seed": 0,
            "kmc_seed": None,
            "lhs_seed": None,
        },
        "inputs": {
            "config_snapshot_sha256": "e" * 64,
            "network": {"template": "ammonia"},
        },
        "tolerances": [
            {
                "observable": "tof_chain",
                "unit": "decades",
                "same_platform": cell,
                "cross_platform": {"rule": "log10", "value": 0.05},
            }
        ],
    }


def _entry(verdict: str, *, complete: bool, not_run: list[str]) -> dict[str, Any]:
    plat = {"arch": "x86_64", "os": "linux", "accelerator": "cpu"}
    return {
        "at": "2026-10-24T00:00:00Z",
        "job_id": 7,
        "verdict": verdict,
        "complete": complete,
        "column": "same_platform",
        "platform_then": plat,
        "platform_now": plat,
        "observables": [],
        "not_run": not_run,
        "not_run_why": {
            leg: vm.NOT_RUN_LEGS.get(leg, vm.NO_TPD_RECORD) for leg in not_run
        },
        "caveats": [],
    }


def test_manifest_of_reads_only_a_record() -> None:
    assert vm.manifest_of({"validation_manifest": "junk"}) is None
    assert vm.manifest_of({"validation_manifest": _record()}) == _record()


# --- view='manifest' ---------------------------------------------------------------


def test_view_says_not_validated_until_a_complete_run() -> None:
    meta: dict[str, Any] = {"validation_manifest": _record()}
    text = vm.manifest_text(meta)
    assert text.splitlines()[1].startswith("validated: NO")
    assert "tof_chain · decades · log10 ≤ 0.01 · log10 ≤ 0.05" in text
    assert f"args={{'manifest': '{MID}'}}" in text

    meta["revalidation"] = {
        MID: [
            _entry(
                "reproduced_partial(energies,tpd)",
                complete=False,
                not_run=["energies", "tpd"],
            )
        ]
    }
    text = vm.manifest_text(meta)
    assert text.splitlines()[1].startswith("validated: NO")
    assert "job 7: reproduced_partial(energies,tpd) (complete: false)" in text
    assert (
        "not run: energies — the min-energy-path leg runs on the GPU seed lane" in text
    )

    meta["revalidation"][MID].append(_entry("reproduced", complete=True, not_run=[]))
    assert vm.manifest_text(meta).splitlines()[1].startswith("validated: yes")


def test_view_flags_an_unstamped_engine() -> None:
    text = vm.manifest_text({"validation_manifest": _record(engine_sha=None)})
    assert "ENGINE COMMIT NOT STAMPED" in text and "cannot be validated" in text


def test_view_without_a_record_says_so() -> None:
    assert "No validation manifest" in vm.manifest_text({"results": {}})


# --- put(mode='revalidate') ----------------------------------------------------------


class _Store:
    def __init__(self, refs: dict[str, Any]) -> None:
        self.refs = refs

    def get_ref(self, *, kind: str, id: Any) -> Any:
        return self.refs.get(str(id))


def _handler(refs: dict[str, Any]) -> PathwayHandler:
    h = PathwayHandler.__new__(PathwayHandler)  # __init__ probes the engine
    setattr(h, "hub", SimpleNamespace(live_store=_Store(refs)))  # a stub hub
    return h


def _ref(meta: dict[str, Any]) -> Any:
    return SimpleNamespace(id=11, meta=meta)


def test_revalidate_refusals_name_the_next_step() -> None:
    h = _handler({"pw-a": _ref({"manifest_id": MID}), "pw-b": _ref({})})
    with pytest.raises(BadInput, match="needs the pathway handle"):
        h.put(mode="revalidate")
    with pytest.raises(BadInput, match="no pathway 'pw-x'"):
        h.put(id="pw-x", mode="revalidate")
    with pytest.raises(BadInput, match="carries no validation manifest") as e:
        h.put(id="pw-b", mode="revalidate")
    assert "view='manifest'" in str(e.value.next)
    with pytest.raises(BadInput, match="is not this pathway's") as e:
        h.put(id="pw-a", mode="revalidate", args={"manifest": "0" * 64})
    assert MID in str(e.value.next)


def test_revalidate_mints_one_cpu_job(monkeypatch: pytest.MonkeyPatch) -> None:
    import precis.handlers.job as job_mod

    calls: list[dict[str, Any]] = []

    class _Jobs:
        def __init__(self, *, hub: Any) -> None:
            pass

        def put(self, **kw: Any) -> Any:
            calls.append(kw)
            return SimpleNamespace(body="job #99 queued", ref_id=99)

    monkeypatch.setattr(job_mod, "JobHandler", _Jobs)
    h = _handler({"pw-a": _ref({"manifest_id": MID})})
    resp = h.put(id="pw-a", mode="revalidate")  # args default to the stored id
    assert "re-validation of manifest aaaaaaaaaaaa queued" in resp.body
    (kw,) = calls
    assert kw["job_type"] == "autocatpath_revalidate"
    assert kw["executor"] == "ssh_node" and kw["requires"] == {}
    assert kw["params"] == {"pathway_ref_id": 11, "manifest_id": MID}
    assert kw["idem_key"] == f"autocatpath_revalidate:11:{MID}"


# --- declarations ------------------------------------------------------------------


def test_views_and_modes_are_declared() -> None:
    spec = PathwayHandler.spec
    assert spec.modes == (
        "preview",
        "revalidate",
        "recompute_kinetics",
        "verify",
        "measure_modes",
    )
    # kinetics-sweep (slice 4) was routed but undeclared, so get refused it
    assert {"kinetics-sweep", "manifest", "tpd"} <= set(spec.views)
    assert len(spec.views) == 16


def test_executors_exclude_every_llm_executor() -> None:
    spec = revalidate_job.SPEC
    assert spec.name == "autocatpath_revalidate"
    assert spec.compatible_executors
    assert spec.compatible_executors <= ZERO_LLM_EXECUTORS
    llm = set(EXECUTOR_PROVIDES) - ZERO_LLM_EXECUTORS
    assert llm, "the executor registry lists LLM executors"
    assert not (spec.compatible_executors & llm)


def test_job_type_is_registered() -> None:
    from importlib.metadata import entry_points

    eps = {ep.name: ep.value for ep in entry_points(group="precis.job_types")}
    assert eps.get("autocatpath_revalidate") == "precis_pathway.revalidate_job:SPEC"
