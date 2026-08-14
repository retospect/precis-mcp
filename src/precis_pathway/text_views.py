"""Text + Mermaid renderings of a autocatpath reaction network.

The point (per precis's ethos): the LLM should read and **argue with** the
network as structured text, not squint at a rendered PNG. Two inputs:

* a cheap `topology` dict (from `runner.network_topology`, pre-compute — no
  energies), and
* a computed `graph` dict (`node_link_data`, from a finished run — carries
  relative energies, barriers, and low-confidence flags).

Both render to a legible list and to Mermaid `flowchart` source (itself text,
and renderable as a diagram by clients that want one). Pure — imports nothing.
"""

from __future__ import annotations

import re
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from .types import NetworkTopology

_ID_RE = re.compile(r"[^A-Za-z0-9_]")


def _mid(name: str) -> str:
    """A Mermaid-safe node id (alnum/underscore)."""
    return "n_" + _ID_RE.sub("_", str(name))


def _formula(composition: dict[str, Any]) -> str:
    """Render an adsorbate composition Counter as a formula, e.g. {N:1,O:2}→'NO2'."""
    if not composition:
        return "*(bare slab)*"
    parts = []
    for el in sorted(composition):
        n = composition[el]
        parts.append(f"{el}{n if n != 1 else ''}")
    return "".join(parts) + "*"  # trailing * = adsorbed


# ── from the cheap pre-compute topology ─────────────────────────────────
def topology_to_text(topo: NetworkTopology) -> str:
    s = topo.get("strategy", "?")
    head = (
        f"{topo['substrate']} → {topo['target']} on {topo['element']}  (network: {s})"
    )
    lines = [f"# Reaction network — {head}", ""]
    states = topo.get("states", [])
    lines.append(f"## Intermediates ({len(states)})")
    for st in states:
        lines.append(f"  {st['name']:<14} {_formula(st['composition'])}")
    steps = topo.get("steps", [])
    lines += [
        "",
        f"## Elementary steps ({len(steps)} — atom-conserving, get NEB barriers)",
    ]
    for step in steps:
        lines.append(f"  {step['reactant']} → {step['product']}   [{step['name']}]")
    links = topo.get("links", [])
    if links:
        lines += ["", "## Supply links (stoichiometry bridges, no barrier)"]
        for ln in links:
            lines.append(f"  {ln['reactant']} ⇢ {ln['product']}")
    lines += [
        "",
        "_No energies yet — this is the pre-compute network. Argue with it, "
        "edit the config, then run to get barriers + pooled uncertainty._",
    ]
    return "\n".join(lines)


def topology_to_mermaid(topo: NetworkTopology) -> str:
    lines = ["flowchart LR"]
    for st in topo.get("states", []):
        lines.append(
            f'  {_mid(st["name"])}["{st["name"]}<br/>{_formula(st["composition"])}"]'
        )
    for step in topo.get("steps", []):
        lines.append(
            f"  {_mid(step['reactant'])} -->|{step['name']}| {_mid(step['product'])}"
        )
    for ln in topo.get("links", []):
        lines.append(f"  {_mid(ln['reactant'])} -.->|supply| {_mid(ln['product'])}")
    return "\n".join(lines)


# ── from the computed graph (node_link_data) ────────────────────────────
def graph_to_mermaid(graph: dict[str, Any]) -> str:
    lines = ["flowchart LR"]
    for n in graph.get("nodes", []):
        rel = n.get("rel_energy", 0.0)
        std = n.get("energy_std", 0.0)
        flag = " ⚠" if n.get("low_confidence") else ""
        lines.append(
            f'  {_mid(n["id"])}["{n["id"]}<br/>{rel:+.2f}±{std:.2f} eV{flag}"]'
        )
    for e in graph.get("links", []):
        src, tgt = _mid(e["source"]), _mid(e["target"])
        if e.get("kind") == "supply":
            lines.append(f"  {src} -.->|supply| {tgt}")
        else:
            ea = e.get("barrier", 0.0)
            estd = e.get("barrier_std", 0.0)
            flag = " ⚠" if e.get("low_confidence") else ""
            lines.append(f'  {src} -->|"Ea {ea:.2f}±{estd:.2f}{flag}"| {tgt}')
    return "\n".join(lines)
