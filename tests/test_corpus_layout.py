"""``precis.corpus_layout`` — the one definition of the corpus PDF layout
plus the worker-side env accessors."""

from __future__ import annotations

import os
from pathlib import Path

from precis.corpus_layout import (
    DEFAULT_CORPUS,
    corpus_pdf_dest,
    corpus_roots_from_env,
    host_name,
)


def test_corpus_pdf_dest_shard() -> None:
    root = Path("/corpus")
    assert corpus_pdf_dest("kong24", root) == root / "k" / "kong24.pdf"
    # first char upper-cased for the shard
    assert corpus_pdf_dest("Zhang22", root) == root / "z" / "Zhang22.pdf"
    # non-alnum first char → the "_" shard
    assert corpus_pdf_dest("_weird", root) == root / "_" / "_weird.pdf"
    # custom suffix (the fetcher preserves the original extension)
    assert corpus_pdf_dest("kong24", root, suffix=".PDF") == root / "k" / "kong24.PDF"


def test_corpus_roots_from_env_pathsep_and_default() -> None:
    a, b = "/opt/shared/corpus", "/opt/nas/corpus"
    env = {"PRECIS_CORPUS_DIR": os.pathsep.join([a, b])}
    assert corpus_roots_from_env(env) == (Path(a), Path(b))
    # unset → the single default root
    assert corpus_roots_from_env({}) == (DEFAULT_CORPUS,)
    # blank entries dropped
    assert corpus_roots_from_env({"PRECIS_CORPUS_DIR": f"{a}{os.pathsep}"}) == (
        Path(a),
    )


def test_host_name_env_override() -> None:
    assert host_name({"PRECIS_HOST_NAME": "melchior"}) == "melchior"
    # falls back to the real hostname (non-empty) when unset
    assert host_name({}) != ""
