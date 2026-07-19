# Structure round-trip eval — trend log

Fidelity = mean representation-invariant match of the rebuilt structure vs the
source, over the generated gold set (see `docs/design/structure-roundtrip-eval.md`).
Run ~monthly; `roundtrip.py` appends a dated row. Higher fidelity = the structure
survived the describe→build language round trip; watch it rise as models and the
structure tool improve.

## 2026-07-19  (6 structures, same-model round trip)

| model | fidelity | $/trip |
|---|---|---|
| opus-4.8 | 1.000 | $49.282m |
| deepseek-v4-pro | 0.670 | $8.550m |
| sonnet-5 | 0.814 | $24.690m |
| haiku-4.5 | 0.825 | $7.853m |
| gpt-oss-120b | 0.966 | $1.085m |

## 2026-07-19  (6 structures × 3 trials, same-model round trip)

| model | fidelity | clean% | fault% | $/trip |
|---|---|---|---|---|
| opus-4.8 | 1.000 | 100% | 0% | $49.769m |
| deepseek-v4-pro | 0.919 | 89% | 6% | $7.303m |
| sonnet-5 | 1.000 | 100% | 0% | $21.709m |
| haiku-4.5 | 0.825 | 0% | 0% | $7.853m |
| gpt-oss-120b | 0.831 | 67% | 6% | $0.651m |
