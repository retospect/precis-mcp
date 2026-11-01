"""Route modules — one per tab (todo, papers, console, status).

Coverage-scan job/Pourbaix tables read metadata only; ``refs.job_geometry``
reads extxyz through ``scan_geometry.fetch_point`` so blob placement stays
store-owned. It reuses the structure template's 3Dmol canvas/script blocks.
Failed-point frames use read-only GETs, never minting structures or rerunning
relaxations. Original periodic images are preserved so atom z coordinates
describe the stored snapshot.
"""
