"""gr540872: stored coverage points open in the shared structure viewer."""

from __future__ import annotations

import html
from typing import Any
from urllib.parse import parse_qs, urlsplit

import pytest

pytest.importorskip("fastapi")

from fastapi.testclient import TestClient

from precis.store import scan_geometry
from precis.store.types import Tag
from precis.structure.importers.extxyz import extxyz_to_scene
from precis_web.app import create_app
from precis_web.config import WebConfig
from precis_web.scan_view import coverage_points, geometry_url, xyz_frames

pytestmark = pytest.mark.db

_FINAL = (
    '2\nLattice="5 0 0 0 5 0 0 0 12" Properties=species:S:1:pos:R:3 '
    'pbc="T T T" energy=-10.5\nPd 0 0 0\nH 1.1 1.2 -0.75\n'
)
_EARLY = _FINAL.replace("-0.75", "2.25")
_POINT = "(111):H:1"
_FAILED = "(111):H:2"
_MIXED = "(111):H+O:1,1"


@pytest.fixture
def scan_client(runtime_with_store, tmp_path) -> TestClient:
    return TestClient(
        create_app(
            runtime=runtime_with_store, web_config=WebConfig(corpus_dir=tmp_path)
        )
    )


@pytest.fixture
def scan_job(runtime_with_store) -> tuple[int, int]:
    store = runtime_with_store.live_store
    surface = store.insert_ref(kind="structure", slug="scan_slab", title="Scan slab")
    job = store.insert_ref(
        kind="job",
        slug=None,
        parent_id=surface.id,
        title="Scan H and O",
        meta={
            "job_type": "surface_coverage_scan",
            "model": "m",
            "coverage": {
                "facets": [
                    {
                        "facet": "(111)",
                        "adsorbates": {
                            "H": {
                                "points": [
                                    {
                                        "n": 1,
                                        "gamma": -0.12,
                                        "theta": 0.25,
                                        "energy": -10.5,
                                        "converged": True,
                                        "geometry_id": _POINT,
                                    },
                                    {
                                        "n": 2,
                                        "gamma": 0.0,
                                        "theta": 0.5,
                                        "energy": -10.0,
                                        "converged": False,
                                        "detached": True,
                                        "geometry_id": _FAILED,
                                    },
                                    {"n": 3},
                                ],
                            }
                        },
                        "mixtures": [
                            {
                                "label": "H+O",
                                "species": ["H", "O"],
                                "points": [
                                    {
                                        "counts": {"H": 1, "O": 1},
                                        "gamma": -0.1,
                                        "theta": 0.5,
                                        "energy": -12.5,
                                        "converged": True,
                                        "geometry_id": _MIXED,
                                    }
                                ],
                            }
                        ],
                    }
                ]
            },
        },
    )
    store.add_tag(job.id, Tag.closed("STATUS", "succeeded"), set_by="agent")
    scan_geometry.store_geometries(
        store,
        job.id,
        [
            {"id": _POINT, "final": _FINAL},
            {"id": _FAILED, "final": _FINAL, "frames": _EARLY + _FINAL, "failed": True},
            {"id": _MIXED, "final": _FINAL},
        ],
    )
    return int(job.id), int(surface.id)


def test_stored_scan_point_uses_shared_viewer(
    scan_client: TestClient, scan_job, runtime_with_store
) -> None:
    job_id, _ = scan_job
    page = scan_client.get(geometry_url(job_id, _POINT))
    assert page.status_code == 200
    assert 'id="mol"' in page.text
    assert 'src="/static/3dmol/3Dmol-min.js"' in page.text
    assert "$3Dmol.createViewer" in page.text
    assert "H 1.100000 1.200000 -0.750000" in page.text
    assert ">-0.750000</td>" in page.text  # actual subsurface z, not wrapped
    assert "Read-only snapshot" in page.text
    assert 'id="sv-clip"' in page.text and 'id="sv-detail"' in page.text
    assert f'href="/refs/job/{job_id}"' in page.text
    assert "sv-proposal" not in page.text and "/proposal" not in page.text
    assert "Failed-point frames" not in page.text
    # Append-only reruns render the newest stored point, not a cached payload.
    scan_geometry.store_geometries(
        runtime_with_store.live_store, job_id, [{"id": _POINT, "final": _EARLY}]
    )
    assert ">2.250000</td>" in scan_client.get(geometry_url(job_id, _POINT)).text


def test_failed_point_has_a_stored_frame_slider(
    scan_client: TestClient, scan_job
) -> None:
    job_id, _ = scan_job
    final = scan_client.get(geometry_url(job_id, _FAILED))
    assert "Failed-point frames" in final.text
    url = geometry_url(job_id, _FAILED, frames=True)
    first = scan_client.get(url)
    assert first.status_code == 200
    assert 'name="frame" min="1" max="2"' in first.text
    assert "Stored frame 1 of 2" in first.text and ">2.250000</td>" in first.text
    last = scan_client.get(url + "&frame=2")
    assert last.status_code == 200
    assert "Stored frame 2 of 2" in last.text and ">-0.750000</td>" in last.text
    for frame in (0, 3):
        assert scan_client.get(url + f"&frame={frame}").status_code == 404
    assert scan_client.get(geometry_url(job_id, _POINT, frames=True)).status_code == 404


def test_missing_foreign_and_retired_geometries_are_404(
    scan_client: TestClient, scan_job, runtime_with_store
) -> None:
    job_id, surface_id = scan_job
    store = runtime_with_store.live_store
    assert scan_client.get(geometry_url(job_id, "missing")).status_code == 404
    assert scan_client.get(geometry_url(999999999, _POINT)).status_code == 404
    assert scan_client.get(geometry_url(surface_id, _POINT)).status_code == 404
    other = store.insert_ref(kind="job", slug=None, title="Another scan")
    assert scan_client.get(geometry_url(other.id, _POINT)).status_code == 404
    store.retire_ref(job_id)
    assert scan_client.get(geometry_url(job_id, _POINT)).status_code == 404


def test_job_lists_points_without_loading_blobs(
    scan_client: TestClient, scan_job, monkeypatch: pytest.MonkeyPatch
) -> None:
    job_id, _ = scan_job

    def no_blob(*args: Any, **kwargs: Any) -> None:
        raise AssertionError("point tables must not load extxyz")

    monkeypatch.setattr(scan_geometry, "fetch_point", no_blob)
    page = scan_client.get(f"/refs/job/{job_id}")
    assert page.status_code == 200
    assert "Coverage-scan points" in page.text
    for point in (_POINT, _FAILED, _MIXED):
        assert (
            f'href="{html.escape(geometry_url(job_id, point), quote=True)}"'
            in page.text
        )
    assert "-0.120000" in page.text and "0.250000" in page.text
    assert "-10.500000" in page.text and ">0.000000</td>" in page.text
    assert ">yes</td>" in page.text and ">no</td>" in page.text
    assert "detached" in page.text and "H+O" in page.text
    assert "not stored" in page.text and "unrecorded" in page.text
    assert "Properties=species" not in page.text


def test_pourbaix_points_link_to_the_same_viewer(
    scan_client: TestClient, scan_job
) -> None:
    job_id, surface_id = scan_job
    page = scan_client.get(f"/pourbaix/st{surface_id}")
    assert page.status_code == 200
    assert "Stored coverage points and geometries" in page.text
    assert (
        f'href="{html.escape(geometry_url(job_id, _POINT), quote=True)}"' in page.text
    )
    assert f'href="/refs/job/{job_id}"' in page.text
    assert "Properties=species" not in page.text


def test_geometry_urls_roundtrip_point_identifiers() -> None:
    point = '(111):H+O:1,1 /?&"</script>'
    url = geometry_url(123, point)
    assert parse_qs(urlsplit(url).query) == {"point": [point]}
    assert coverage_points(1, {}) is None
    assert coverage_points(1, {"job_type": "surface_coverage_scan"}) == []
    assert xyz_frames(_EARLY + _FINAL) == [_EARLY, _FINAL]
    for text in ("", "2\nheader\nH 0 0 0\n", "junk\n"):
        with pytest.raises(ValueError):
            xyz_frames(text)


def test_invalid_stored_geometry_is_reported(
    scan_client: TestClient, scan_job, runtime_with_store
) -> None:
    job_id, _ = scan_job
    scan_geometry.store_geometries(
        runtime_with_store.live_store,
        job_id,
        [{"id": "broken", "final": "2\nheader\nH 0 0 0\n"}],
    )
    page = scan_client.get(geometry_url(job_id, "broken"))
    assert page.status_code == 422
    assert "Cannot read stored extxyz" in page.text


def test_viewer_does_not_change_import_wrapping_default() -> None:
    scene = extxyz_to_scene(_FINAL)
    unwrapped = extxyz_to_scene(_FINAL, wrap=False)
    wrapped_z = scene.cell.frac_to_cart(scene.atoms["aH1"].frac)[2]
    original_z = unwrapped.cell.frac_to_cart(unwrapped.atoms["aH1"].frac)[2]
    assert wrapped_z == pytest.approx(11.25)
    assert original_z == pytest.approx(-0.75)
