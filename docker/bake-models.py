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
    """Tolerate SuryaOCRConfig() with no kwargs."""
    from surya.recognition.model.config import SuryaOCRConfig

    _original_init = SuryaOCRConfig.__init__

    def _patched_init(self, *args, **kwargs):  # type: ignore[no-untyped-def]
        kwargs.setdefault("encoder", {})
        kwargs.setdefault("decoder", {})
        return _original_init(self, *args, **kwargs)

    SuryaOCRConfig.__init__ = _patched_init  # type: ignore[method-assign]


def main() -> None:
    _patch_get_text_config()
    _patch_surya_config()

    # Marker layout / OCR / detection / table-recognition models.
    from marker.models import create_model_dict

    create_model_dict()

    # BAAI/bge-m3 for chunk embeddings.
    from sentence_transformers import SentenceTransformer

    SentenceTransformer("BAAI/bge-m3")


if __name__ == "__main__":
    main()
