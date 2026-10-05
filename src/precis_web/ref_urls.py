"""Canonical human readers for stored refs, shared by navigation surfaces.

Browse-menu membership is unrelated to detail coverage. Optional plugin refs
remain readable when their handler is unavailable. Slug readers require a
stored slug; incomplete refs use generic detail rather than inventing a slug.
"""

from urllib.parse import quote

NATIVE_REF_URLS: dict[str, str] = {
    "paper": "/papers/{id}",
    "draft": "/smartdraft/{id}",
    "agentlog": "/agentlogs/{id}",
    "datasheet": "/datasheets/{id}",
    "folder": "/drive?folder={id}",
    "todo": "/todo?focus={id}",
    "cad": "/cad/{slug}",
    "se": "/se/{slug}",
    "structure": "/structure/{slug}",
    "figure": "/figure/{slug}",
    "mermaid": "/mermaid/{slug}",
    "pcb": "/pcb/{slug}",
}


def ref_url(kind: str, ref_id: int | str, slug: str | None = None) -> str:
    """Native reader when addressable, otherwise total generic detail."""
    template = NATIVE_REF_URLS.get(kind)
    if template and ("{slug}" not in template or slug):
        return template.format(id=ref_id, slug=quote(slug or "", safe=""))
    return f"/refs/{quote(kind, safe='')}/{ref_id}"
