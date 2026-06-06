"""Pre-populate marker + bge-m3 caches during Docker build.

Workaround for a surya↔transformers incompatibility: newer
``transformers.configuration_utils.from_dict`` does
``logger.info(f"Model config {config}")`` which evaluates the
f-string eagerly. Formatting ``config`` calls ``__repr__`` →
``to_json_string`` → ``to_diff_dict`` → ``self.__class__()``
with no kwargs, which trips ``SuryaOCRConfig.__init__`` because
that constructor does ``kwargs.pop("encoder")`` unconditionally
and raises ``KeyError: 'encoder'``.

The format call happens regardless of the log level (f-string is
eager), so ``TRANSFORMERS_VERBOSITY=error`` does NOT dodge it.

This shim monkeypatches ``SuryaOCRConfig.__init__`` to inject a
default for ``encoder`` when called with no kwargs, then runs the
two cache-bake calls. At runtime the patched module is whatever
``import surya`` provides — we don't ship this shim, only use it
during the Docker bake step.
"""

from __future__ import annotations

import importlib


def _patch_get_text_config() -> None:
    """Make transformers' ``get_text_config()`` deterministic for surya.

    Newer transformers' weight-init path does
    ``self.config.get_text_config()`` to pick the "text" sub-config and
    pull ``initializer_range``. The base implementation walks a list of
    known attribute names (``text_config``, ``decoder``, ``language_config``,
    ``text_encoder``) and raises ``ValueError("Multiple valid text
    configs were found")`` when more than one is present. Surya configs
    expose both ``text_encoder`` and ``decoder``, which trips the check.

    Surya doesn't override ``get_text_config``, and ``_init_weights``
    only needs *any* config with ``initializer_range`` — it doesn't
    care which. Patch the base ``PretrainedConfig`` class to break the
    tie by returning ``decoder`` when both are present.
    """
    from transformers.configuration_utils import PretrainedConfig

    _orig = PretrainedConfig.get_text_config

    def _patched(self, decoder=False):  # type: ignore[no-untyped-def]
        try:
            return _orig(self, decoder=decoder)
        except ValueError:
            # Multiple valid text configs — pick decoder if present,
            # else text_encoder, else fall back to self.
            for attr in ("decoder", "text_config", "text_encoder", "language_config"):
                cfg = getattr(self, attr, None)
                if cfg is not None:
                    return cfg
            return self

    PretrainedConfig.get_text_config = _patched  # type: ignore[method-assign]


def _patch_surya_config() -> None:
    """Short-circuit transformers' debug-format path for surya configs.

    transformers' ``from_dict`` does ``logger.info(f"Model config {config}")``.
    The f-string is eager, so __repr__ runs regardless of log level.
    __repr__ → to_json_string → to_diff_dict, and to_diff_dict needs to
    instantiate self.__class__() with no kwargs to compute "diff vs
    defaults". surya configs (SuryaOCRConfig, plus its encoder/decoder
    children) require multiple structured kwargs at construction, so the
    no-arg path can't be made safe by injecting placeholders — each
    placeholder unblocks one ``kwargs.pop`` only for the next line to
    trip ``decoder_config["bos_token_id"]`` etc.

    Simpler fix: override ``to_diff_dict`` on surya's config classes to
    return ``{}``. The debug log emits ``SuryaOCRConfig {}`` instead of
    a full dump, which is fine — this only affects the once-per-load
    info message during the bake step.
    """

    def _empty_diff(self):  # type: ignore[no-untyped-def]
        return {}

    # Surya's internal module layout shifted between 0.13 and 0.17:
    # SuryaOCRConfig was removed; SuryaModelConfig moved from
    # surya.foundation.config to surya.common.surya.config; and
    # SuryaLayoutConfig / TableRecConfig were renamed or relocated.
    # Patch every known location best-effort — a missing class on
    # either side of the rename is silently skipped, and adding too
    # many here is harmless.
    _candidates = [
        # surya 0.13.x layout (pre-rename)
        ("surya.recognition.model.config", "SuryaOCRConfig"),
        ("surya.foundation.config", "SuryaModelConfig"),
        ("surya.layout.model.config", "SuryaLayoutConfig"),
        ("surya.table_rec.model.config", "TableRecConfig"),
        # surya 0.17.x layout
        ("surya.common.surya.config", "SuryaModelConfig"),
        ("surya.common.surya.config", "SuryaDecoderConfig"),
        ("surya.common.surya.config", "SuryaEncoderConfig"),
        ("surya.table_rec.model.config", "SuryaTableRecConfig"),
        ("surya.table_rec.model.config", "SuryaTableRecDecoderConfig"),
    ]
    for module_name, class_name in _candidates:
        try:
            mod = importlib.import_module(module_name)
            cls = getattr(mod, class_name, None)
            if cls is not None:
                cls.to_diff_dict = _empty_diff  # type: ignore[method-assign]
        except ImportError:
            continue


def _bake_bge_m3() -> None:
    """Ensure BAAI/bge-m3 is in the HF cache; download only if missing.

    History: previous attempts with ``snapshot_download(... max_workers=4)``
    hung indefinitely inside the docker build — every worker thread parked
    in ``futex_wait`` with no socket activity. The hang reproduces with
    HF's xet-bridge protocol on at least some shards. Setting
    ``HF_HUB_DOWNLOAD_TIMEOUT`` did not help (the threads aren't doing
    socket I/O; they're waiting on a futex).

    Strategy: the Dockerfile seeds ``/opt/precis/models/hf`` from a prior
    image via the ``premodels`` build context BEFORE this script runs. If
    the cache already has a bge-m3 snapshot, we skip the download
    entirely. Only on a true cold build (no premodels image) do we hit
    the network — and then we still flip ``HF_HUB_OFFLINE`` off only for
    the duration of that one call.
    """
    import os
    import pathlib

    hf_home = pathlib.Path(os.environ.get("HF_HOME", "/opt/precis/models/hf"))
    snapshots = hf_home / "hub" / "models--BAAI--bge-m3" / "snapshots"
    have_cache = snapshots.is_dir() and any(snapshots.iterdir())

    if have_cache:
        print(
            f"[bake] bge-m3 cache already populated under {snapshots} — skipping download"
        )
        return

    print("[bake] bge-m3 cache empty — fetching from HF")
    os.environ.setdefault("HF_HUB_DOWNLOAD_TIMEOUT", "120")
    os.environ.setdefault("HF_HUB_DISABLE_PROGRESS_BARS", "0")

    import time

    from huggingface_hub import snapshot_download
    from huggingface_hub.errors import HfHubHTTPError, LocalEntryNotFoundError

    # HF's edge rate-limits the per-build cold fetch with 429s when CI
    # runs are bunched up. Retry with exponential backoff so a transient
    # rate-limit window doesn't fail the publish.
    delays = [5, 15, 45, 120, 300]
    last_err: Exception | None = None
    for attempt, delay in enumerate([0, *delays], start=1):
        if delay:
            print(f"[bake] retry {attempt - 1} after {delay}s …")
            time.sleep(delay)
        try:
            snapshot_download(
                repo_id="BAAI/bge-m3",
                repo_type="model",
                # Skip onnx artefacts we don't need for the sentence-
                # transformers path. Saves ~500 MB.
                ignore_patterns=["onnx/*", "*.onnx", "*.onnx_data"],
                max_workers=4,
                etag_timeout=30,
            )
            return
        except (HfHubHTTPError, LocalEntryNotFoundError) as e:
            # 429 surfaces as HfHubHTTPError directly; the API-then-fetch
            # path in snapshot_download wraps a 429 in LocalEntryNotFoundError
            # after a single failed repo_info call. Both are retryable.
            status = getattr(getattr(e, "response", None), "status_code", None)
            if (
                isinstance(e, HfHubHTTPError)
                and status is not None
                and status < 500
                and status != 429
            ):
                # Non-transient client error — fail fast.
                raise
            last_err = e
            print(f"[bake] HF fetch attempt {attempt} failed: {type(e).__name__}: {e}")
    assert last_err is not None
    raise last_err


def main() -> None:
    import os

    # Which caches to bake. The split images (ADR 0021) bake only what
    # they run: the `ingest` image needs marker/surya, the `embedder`
    # image needs bge-m3. The full runtime/dev images bake both.
    #   PRECIS_BAKE_ONLY = all (default) | marker | embed
    only = os.environ.get("PRECIS_BAKE_ONLY", "all").strip().lower()
    do_marker = only in ("all", "marker")
    do_embed = only in ("all", "embed")
    print(f"[bake] PRECIS_BAKE_ONLY={only!r} → marker={do_marker} embed={do_embed}")

    if do_marker:
        _patch_get_text_config()
        _patch_surya_config()

        # Marker layout / OCR / detection / table-recognition models.
        from marker.models import create_model_dict

        create_model_dict()

    if do_embed:
        # BAAI/bge-m3 for chunk embeddings — pre-fetch (or no-op if cache
        # is already seeded from the `premodels` build context).
        _bake_bge_m3()

        # Verify the cache resolves through sentence-transformers. Force
        # OFFLINE mode for the verification: even when the cache is fully
        # populated, the SentenceTransformer constructor otherwise hits
        # HF to check for a newer revision — that's the xet-bridge path
        # that was hanging in futex_wait for hours.
        os.environ["HF_HUB_OFFLINE"] = "1"
        os.environ["TRANSFORMERS_OFFLINE"] = "1"
        from sentence_transformers import SentenceTransformer

        SentenceTransformer("BAAI/bge-m3")


if __name__ == "__main__":
    main()
