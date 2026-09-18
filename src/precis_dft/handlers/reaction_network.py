"""``kind='reaction_network'`` — abstract mechanism graphs.

Read-only at the handler level for v1: the built-in library
(13 networks under ``precis_dft/data/reaction_networks/``) is the
canonical set. The agent can fetch any of them; custom networks
will be a v2 addition.

``get(kind='reaction_network', id='reaction:oer_4step_acid')``
returns the network's text summary; ``view='species'`` and
``view='steps'`` return the structured pieces.
"""

from __future__ import annotations

from typing import Any

from precis.protocol import Handler, KindSpec
from precis.response import Response
from precis_dft import reactions


class ReactionNetworkHandler(Handler):
    spec = KindSpec(
        kind="reaction_network",
        title="Reaction mechanism graph",
        description=(
            "Abstract reaction mechanism: species library + ordered "
            "elementary steps with electron/proton stoichiometry. Reusable "
            "across materials. Built-in library covers OER (acid / alkaline "
            "/ LOM / dual-site), HER, ORR, CO2RR, NRR."
        ),
        supports_get=True,
        supports_search=True,
        is_numeric=False,
        id_required=True,
        views=("toc", "species", "steps", "references"),
    )

    def __init__(self, *, hub: Any) -> None:
        self.hub = hub
        self._library: dict[str, dict[str, Any]] = reactions.load_library()

    def library(self) -> dict[str, dict[str, Any]]:
        """Public accessor for ``reaction_evaluate`` and friends."""
        return dict(self._library)

    def get(self, **kw: Any) -> Response:
        id_ = kw.get("id")
        if not id_:
            # No id → list the library.
            return Response(body=self._format_library_index())
        network = self._library.get(id_)
        if network is None:
            return Response(
                body=(
                    f"reaction network not found: {id_}. Available: {sorted(self._library)}"
                )
            )

        view = kw.get("view")
        if view in (None, "summary", "toc"):
            return Response(body=_format_summary(network))
        if view == "species":
            return Response(body=str(network.get("species", [])))
        if view == "steps":
            return Response(body=str(network.get("steps", [])))
        if view == "references":
            return Response(body="\n".join(network.get("references", [])))
        return Response(body=f"unknown view {view!r} for kind='reaction_network'")

    def search(self, **kw: Any) -> Response:
        q = (kw.get("q") or "").lower().strip()
        hits = [
            net
            for net in self._library.values()
            if q in net.get("id", "").lower() or q in net.get("name", "").lower()
        ]
        if not hits:
            return Response(body=f"no networks matched {q!r}")
        return Response(
            body="\n".join(f"{net['id']}: {net.get('name', '')}" for net in hits)
        )

    def _format_library_index(self) -> str:
        lines = [f"# reaction networks ({len(self._library)})"]
        for net_id, net in sorted(self._library.items()):
            lines.append(f"- {net_id}: {net.get('name', '')}")
        return "\n".join(lines)


def _format_summary(network: dict[str, Any]) -> str:
    """One-screen text summary of a reaction network."""
    species = network.get("species", []) or []
    steps = network.get("steps", []) or []
    refs = network.get("references", []) or []
    return (
        f"# {network.get('id', '?')}\n"
        f"name:        {network.get('name', '?')}\n"
        f"species:     {len(species)} entries\n"
        f"steps:       {len(steps)} elementary step(s)\n"
        f"n_PCET:      {sum(1 for s in steps if int(s.get('n_e', 0)) > 0)}\n"
        f"references:  {len(refs)} DOI(s)\n"
    )
