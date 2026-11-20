---
id: precis-quantum-help
family: science
title: precis — quantum computers: what they solve today and IBM's free tier
summary: deciding whether a problem belongs on a quantum computer, judging quantum advantage claims against classical baselines, what IBM's free Open Plan allows and how to estimate QPU seconds, and how to reach IBM hardware outside precis. precis has no QPU access.
answers:
  - what kinds of problems can a quantum computer solve today?
  - should this problem run on a quantum computer or a classical method?
  - is this quantum advantage claim real?
  - can a quantum computer beat CCSD(T), DMRG or tensor networks?
  - does QAOA or VQE beat classical methods?
  - what does the IBM Quantum free Open Plan allow?
  - how many QPU seconds will my circuit use?
  - how do I run a circuit on IBM Quantum from precis?
  - what does Qiskit Runtime, the Sampler or the Estimator do?
  - is there a quantum kind in precis?
applies-to: research triage and study design; no verb or kind runs quantum circuits
status: active
tags: [design, external-sources]
---

# precis-quantum-help — triage first, hardware rarely

precis cannot run a quantum circuit: there is no `quantum` kind, job type or IBM credential. Use this skill to decide whether a problem belongs on a QPU, judge claims, and scope a study. Every fact is dated 2026-10; the last section says how far each is verified.

Default answer for a real workload: **run the classical method.** On today's noisy hardware, a QPU run is an experiment about the method, not a cheaper way to get the answer.

## Decide whether a problem belongs on a quantum computer
## Should this run on a QPU or a classical method?
## What kinds of problems can quantum computers solve today?

| Problem | Status on IBM hardware | Use instead, or compare with |
|---|---|---|
| Sampling circuits built to be classically hard (doped Clifford, random circuits) | Strongest evidence; the output has no practical use | Nothing at full scale; the point is the benchmark |
| Many-body dynamics and observables at high entanglement | Plausible in selected regimes; unproven | Tensor networks, DMRG, especially low entanglement or 1D |
| Molecular ground states (VQE and variants) | Algorithm research on small active spaces | CCSD(T), selected CI, DMRG |
| Combinatorial optimization (QAOA) | Prototyping; no broad win | CP-SAT, MILP, annealing, problem-specific heuristics |
| Machine learning (feature maps, kernels) | Experiments; no established advantage | Classical ML |
| Factoring, cryptography, generic simulation | Not possible; needs fault tolerance | Not applicable |

Answer these before proposing a QPU run:

1. What precise quantity is wanted, and to what accuracy?
2. Which strongest classical method applies, and has it failed on this instance, or only been assumed to?
3. Does the problem fit: about 120–156 qubits, and a circuit that survives the noise once routed to the device?
4. Would disagreement with the classical result be interpretable, or just noise?
5. Is a sampled or mitigated estimate with error bars acceptable?

Any "no" or "unknown" on 2–5 sends it to the classical route. Annealing-shaped work in precis, such as board placement, stays classical.

The closest fit for precis chemistry: strongly correlated active sites, such as transition-metal catalysts from `structure` or `pathway` work, run side by side with a classical method on the same active space. Expect a comparison study, not a better energy. Before scoping one, check which classical lanes exist:

```python
get(kind='skill', id='precis-job-observe-help')
```

## Judge a quantum advantage claim in a paper
## Is this quantum speedup real?
## Can a quantum computer beat tensor networks?

A claim holds only with all five:

1. A precisely defined quantity, not "the output of this circuit".
2. A reproducible accuracy target.
3. Comparison against the strongest relevant classical method, not the authors' own or a default-tuned one.
4. Full resource accounting: shots, mitigation, calibration, classical pre- and post-processing, validation.
5. An end-to-end time or cost win on that quantity.

Red flags:

- Classical methods disagreeing shows the regime is hard. It does not show the quantum answer is right.
- More qubits or gates is not hardness; entanglement structure and the requested output decide it.
- A speedup against a vendor's own classical software is a press claim.
- Classical cost "estimates" assume a contraction path and hardware; the baseline keeps moving.

Reference cases:

| Claim | Reading |
|---|---|
| IBM kicked Ising, 127 qubits (2023) | Reproduced classically by tensor-network follow-ups; the standard cautionary case |
| IBM/UChicago doped Clifford sampling (2026, arXiv:2607.25941) | Certifies its own fidelity bound; narrow task with no direct use. Strongest of the set |
| Qedma/RIKEN/BlueQubit Floquet dynamics, 74 qubits (2026, arXiv:2607.24937) | Classical methods disagreed; validated with independent mitigation estimators and a partial repeat on other hardware. Promising, unsettled |
| Algorithmiq operator Loschmidt echo, 56 qubits (2026, arXiv:2607.25998) | Trust rests on consistency across devices and a noise model. Promising, unsettled |
| BlueQubit Nighthawk random circuits (2026) | 61 qubits, 918 CZ gates, estimated 110 Frontier-years to reproduce. Benchmark only; depends on the cost estimate |
| Q-CTRL 3,000x fermionic simulation (2026) | Vendor release, against its own tuned classical software. Unreviewed |

Read further: the review arXiv:2603.18825 (tensor networks as the moving baseline, section 4 on where they fail), its references Hangleiter arXiv:2603.09901 and Eisert and Preskill arXiv:2510.19928, and the Quantum Advantage Tracker. Pull them before citing, then record the verdict with [[precis-claim-fidelity-help]].

```python
get(kind='web', id='https://quantum-advantage-tracker.github.io')
get(kind='web', id='https://arxiv.org/html/2603.18825v2')
```

## Check what IBM Quantum's free Open Plan allows
## What can I do without paying for quantum computing time?

| Item | Open Plan |
|---|---|
| QPU time | 10 minutes per rolling 28 days |
| Extra time | Active users can opt in to 180 more minutes over 12 months (since 2026-03) |
| Region | `us-east` only |
| Execution modes | Job and batch; no sessions |
| Qiskit Functions | Not included; Premium, Flex and On-Prem only |
| Submission rate | 5 jobs per minute per user |
| Queue | No published wait time; always plan asynchronously |
| Devices | Whatever the account's backends list returns; `ibm_kingston` (Heron r2) was added in 2026-04. Nighthawk `ibm_phoenix` is paid-only |

Paid plans for comparison: Pay-As-You-Go $96 per minute, Flex from $72 (400-minute annual minimum), Premium from $48 (5,200-minute annual minimum). At $96 per minute, $25 buys about 16 seconds.

Consequences:

- No sessions means tight variational loops do not fit. Optimize on a simulator; use hardware to evaluate final parameters.
- IBM holds circuits, parameters and results for three years while the account is active, encrypted at rest. Review IBM's data sheet before submitting circuits from unpublished work.
- IBM recommends this plan for learning and exploring. Unattended use by agents is not confirmed against its terms.

Devices: Heron r1 has 133 qubits, r2 and r3 have 156 on a heavy-hex lattice; Nighthawk r2 has 120 programmable qubits on a square grid. IBM reports budgets of about 5,000 two-qubit gates on Heron and over 7,500 on Nighthawk; these are not layer depth, and they do not promise a useful answer.

## Estimate how many QPU seconds a circuit will use
## How long will my quantum job take against the quota?

Usage is the time the QPU is locked to the workload. Queue time does not count.

```text
usage ≈ 2 s per sub-job + (rep_delay + circuit length) × executions
executions = circuits after parameter broadcasting × shots
quick form, no advanced mitigation, default rep_delay: 2 + 0.00035 × executions
```

| Run | Executions | Estimate | Runs per 600 s |
|---|---|---|---|
| 1 circuit, 4,096 shots | 4,096 | 3.4 s | ~175 |
| 20 circuits, 4,096 shots | 81,920 | 31 s or more | ~19 |

- Mitigation adds circuits; PEA and PEC add noise-learning runs and cost far more. Estimator groups commuting observables, which reduces executions.
- Large jobs split into sub-jobs, each with its own 2 s.
- A system error reports zero usage. A user error or cancel still charges what was consumed, including QPU preparation.
- `max_execution_time` caps QPU usage per job, up to the service limit of three hours. Set it below the remaining balance.
- Measured usage: `job.usage()`, or `job.metrics()['usage']['quantum_seconds']`. The Instances page shows rolling 28-day usage in real time.

## Prepare a circuit without spending quota
## Simulate a quantum circuit locally

IBM retired its cloud simulators; simulation is local.

| Tool | Use |
|---|---|
| Qiskit Aer `AerSimulator` | Ideal or custom-noise runs; `from_backend` builds a noise model from a real device |
| `qiskit_ibm_runtime.fake_provider` | Device snapshot: coupling map, gates, error rates. Can be stale; `refresh` updates it from the service |
| Local testing mode | Pass a fake backend or Aer as the primitive's `mode`; no quota and no queue. Most cloud options are ignored, and no cloud mitigation runs |

Primitives: **Sampler** returns measured bitstrings, for circuits with measurements. **Estimator** returns expectation values of observables. From `qiskit-ibm-runtime` 0.50 the client-side versions are current (`qiskit_ibm_runtime.executor_sampler.Sampler`, `executor_estimator.Estimator`) and `SamplerV2`/`EstimatorV2` are deprecated. IBM's service is now named IBM Quantum Compute Service, formerly Qiskit Runtime.

No precis lane runs these. Run them outside precis, or on the sandbox lane if its image has Qiskit (unconfirmed): [[precis-job-observe-help]].

## Reach IBM quantum hardware from outside precis
## Is there an MCP server for IBM Quantum?

IBM documents official MCP servers, added to the MCP client's config by a human operator:

| Package | Does | Token |
|---|---|---|
| `qiskit-mcp-server` | Create, transpile and serialize circuits locally | No |
| `qiskit-docs-mcp-server` | Search Qiskit docs | No |
| `qiskit-ibm-runtime-mcp-server` | List backends, submit jobs, fetch results; real hardware | Yes |
| `qiskit-ibm-transpiler-mcp-server` | AI-assisted circuit optimization | Yes |

Its runtime tools: `run_sampler_tool`, `run_estimator_tool`, `list_my_jobs_tool`, `get_job_status_tool`, `get_job_results_tool`, `cancel_job_tool`, plus account setup and deletion. Attaching it takes minutes and suits a one-off trial in one session. It is outside the budget breaker and the job record, leaves no ref in the graph, and cluster agents do not get it. Record any result by hand as a finding: [[precis-finding-help]].

## Check how far these facts are verified
## Where did this quantum research come from?

Compiled with AI assistance (Claude, and Perplexity for search). Perplexity is not a primary source, and its citation numbers did not line up with its source lists.

| Tier | Facts | Source |
|---|---|---|
| IBM docs, read directly | Open Plan quota, opt-in, region; usage formula and failed-job rules; `max_execution_time`; data retention; processor sizes; MCP packages | `web:543764`–`web:543768`, plans-overview |
| IBM site, read directly | Plan prices; the three 2026 papers as IBM describes them (a vendor account) | ibm.com/quantum/products, `web:543770` |
| Review, read in part | Baseline argument | arXiv:2603.18825 |
| Perplexity only | No sessions or Functions on Open Plan; 5 jobs per minute; gate budgets; `ibm_kingston`, `ibm_phoenix`; MCP tool names; local simulation tools; problem-status table; BlueQubit figures | `perplexity-reasoning:542053`, `perplexity-reasoning:542067` |
| Web search summary only | Q-CTRL claim | Not opened |

Before relying on a Perplexity-only fact, read IBM's page. Page reads are free; a new Perplexity query is not.

```python
get(kind='web', id='https://quantum.cloud.ibm.com/docs/en/guides/plans-overview')
get(kind='web', id='https://quantum.cloud.ibm.com/docs/en/guides/estimate-job-run-time')
get(kind='web', id='https://quantum.cloud.ibm.com/docs/en/guides/qiskit-mcp-servers')
```

## See also

- [[precis-claim-fidelity-help]] — record whether a source supports a claim
- [[precis-finding-help]] — store a result or verdict
- [[precis-job-observe-help]] — the compute lanes a classical baseline would use
- [[precis-perplexity-help]] — cost and citation rules for Perplexity
- [[precis-web-help]] — read a cited page
