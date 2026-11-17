"""Shared chat context, independent of frontend packages.

Profiles keep the Discord prompt byte-compatible while web chat omits the
bot's inner life and bounds history locally. ToolCaller keeps transport and
runtime wiring outside core; context consumes the existing text tool surface.
"""
