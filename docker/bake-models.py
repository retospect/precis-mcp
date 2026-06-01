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
    """Tolerate SuryaOCRConfig() with no kwargs."""
    from surya.recognition.model.config import SuryaOCRConfig

    _original_init = SuryaOCRConfig.__init__

    def _patched_init(self, *args, **kwargs):  # type: ignore[no-untyped-def]
        kwargs.setdefault("encoder", {})
        kwargs.setdefault("decoder", {})
        return _original_init(self, *args, **kwargs)

    SuryaOCRConfig.__init__ = _patched_init  # type: ignore[method-assign]


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
