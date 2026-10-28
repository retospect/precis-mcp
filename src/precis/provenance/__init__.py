"""Provenance in code — db handles carried inside workspace scripts.

A workspace script (pcb/se/other) cites the db objects its numbers came
from with a compact handle in a comment — ``# pa4474653 (dropout)``.
:mod:`precis.provenance.refs` is the machinery around that convention
(gr514049, ``docs/backlog/workspace-db-references.md``): find the
annotations, lint that every handle resolves, and expand them to
self-contained citations when code leaves the instance.
"""
