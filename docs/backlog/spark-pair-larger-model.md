# spark pair: spend the ~76G idle headroom on a larger big-tier model

Measured 2026-08-10 with DeepSeek-V4-Flash UD-Q8_K_XL (151G, RPC-sharded)
serving at `-np 4` (4×~32k slots): castor 82G used / 38G available, pollux
83G / 38G — ~76G of the pair's 242G sits idle. A slightly larger model (or
bigger quant / more KV) would fit in a ~210–220G total budget: e.g. the
Qwen3-235B Q6_K (180G, already on castor's disk) sharded across the pair,
or a higher-precision DeepSeek quant if one lands in that window. Leave
headroom for KV growth (scales with slots × ctx) and the OS.

Opposite direction from the parked Q3 single-box experiment (Q3_K_XL ~104G
on castor alone, freeing pollux) — decide which way to spend the pair
before doing either. Model switch = relink `~/serve-current.sh` on castor +
`systemctl restart llama-server` (~10 min load, big tier falls to cloud
rung meanwhile); keep card `served_by[0].max_parallel` + `resource_slots`
in lockstep with `-np` (memory: spark-pair-big-model-serving).

test: new model serves on the pair, tok/s single + @4-way measured vs
DeepSeek's 10.3/27.2, `llm.chain.big` repointed, zero llm_call_log errors
over a day.
