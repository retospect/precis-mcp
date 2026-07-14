"""Text-to-speech engines behind the :class:`precis.export.audio.Synthesizer`
seam. Local-first (Kokoro); gated by the ``[tts]`` extra + model env paths, so
non-TTS builds never import it.
"""

from __future__ import annotations
