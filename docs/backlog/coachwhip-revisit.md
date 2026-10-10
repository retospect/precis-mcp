---
status: idea
pillar: local-compute
snooze-until: 2026-11-24
---

# Re-check Coachwhip (SSD-streamed MoE on Apple silicon)

https://github.com/1picassoai/coachwhip — Rust/Candle, Metal-only engine
that streams MoE experts from SSD with router-lookahead prefetch. Evaluated
2026-10-10: no use today. Its models (Qwen3 MoE up to Qwen3.5-122B) fit in
RAM on melchior (192 GB) and the 128 GB M4 Max, where plain llama.cpp is
5-10x faster; one request at a time, 16K context, loopback-only, no
DeepSeek/GLM/Kimi architecture.

Worth adopting only if a re-check finds: DeepSeek-V3/Kimi-K2-class support
(models over ~150 GB, where streaming beats not fitting), multi-request
serving, or a CUDA backend for the Sparks. Otherwise delete this file.
