"""Read-only coverage-scan rows shared by the job and Pourbaix readers."""

from __future__ import annotations

from typing import Any
from urllib.parse import urlencode


def geometry_url(job_id: int, point: str, *, frames: bool = False) -> str:
    params: dict[str, str] = {"point": point}
    if frames:
        params["frames"] = "true"
    return f"/refs/job/{job_id}/geometry?{urlencode(params)}"


def coverage_points(job_id: int, meta: dict[str, Any]) -> list[dict[str, Any]] | None:
    """Metadata only: never load geometry blobs to render a point table.

    Old scans have no geometry_id; do not invent one from facet/n/counts.
    """
    if meta.get("job_type") != "surface_coverage_scan":
        return None
    rows: list[dict[str, Any]] = []
    for facet in (meta.get("coverage") or {}).get("facets") or []:
        groups = list((facet.get("adsorbates") or {}).items())
        groups.extend(
            (mix.get("label") or "+".join(mix.get("species") or []), mix)
            for mix in facet.get("mixtures") or []
        )
        for adsorbate, group in groups:
            for point in group.get("points") or []:
                counts = point.get("counts") or {}
                pid = point.get("geometry_id")
                rows.append(
                    {
                        "facet": facet.get("facet"),
                        "adsorbate": adsorbate,
                        "counts": counts,
                        "n": point.get("n", sum(counts.values()) if counts else None),
                        "gamma": point.get("gamma"),
                        "theta": point.get("theta"),
                        "energy": point.get("energy"),
                        "converged": point.get("converged"),
                        "detached": point.get("detached", False),
                        "url": geometry_url(job_id, str(pid)) if pid else None,
                    }
                )
    return rows


def xyz_frames(text: str) -> list[str]:
    """Split extxyz by its atom counts, retaining each header and atom row."""
    lines = text.splitlines(keepends=True)
    frames = []
    start = 0
    while start < len(lines):
        if not lines[start].strip():
            start += 1
            continue
        n = int(lines[start].strip())
        end = start + n + 2
        if n < 1 or end > len(lines):
            raise ValueError("incomplete extxyz frame")
        frames.append("".join(lines[start:end]))
        start = end
    if not frames:
        raise ValueError("empty extxyz")
    return frames
