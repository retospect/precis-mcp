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
    from surya.recognition.model.config import SuryaOCRConfig

    def _empty_diff(self):  # type: ignore[no-untyped-def]
        return {}

    SuryaOCRConfig.to_diff_dict = _empty_diff  # type: ignore[method-assign]

    # Other surya configs use the same machinery; patch the ones we know
    # come up. Adding too few here just produces a follow-on KeyError on
    # the next config class; adding too many is harmless.
    try:
        from surya.foundation.config import SuryaModelConfig

        SuryaModelConfig.to_diff_dict = _empty_diff  # type: ignore[method-assign]
    except ImportError:
        pass

    try:
        from surya.layout.model.config import SuryaLayoutConfig

        SuryaLayoutConfig.to_diff_dict = _empty_diff  # type: ignore[method-assign]
    except ImportError:
        pass

    try:
        from surya.table_rec.model.config import TableRecConfig

        TableRecConfig.to_diff_dict = _empty_diff  # type: ignore[method-assign]
    except ImportError:
        pass


def main() -> None:
    _patch_surya_config()

    # Marker layout / OCR / detection / table-recognition models.
    from marker.models import create_model_dict

    create_model_dict()

    # BAAI/bge-m3 for chunk embeddings.
    from sentence_transformers import SentenceTransformer

    SentenceTransformer("BAAI/bge-m3")


if __name__ == "__main__":
    main()
