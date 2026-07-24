# Idea — graph locality as the specialization mechanism (single agent, not an agent zoo)

> Status: raw idea capture, recovered 2026-07-23 from an orphaned vim swap
> file (never saved to disk before). Not yet a design-of-record — needs a
> decision on scope before it becomes an ADR. See `next-things.md` §1 and
> the 2026-07-23 planning discussion for how this relates to ADR 0054
> (argument graph) and the cross-paper citation-chunk grounding idea.

Here's a plan/idea.

As we build context from the knowledge graph (the fisheye method is a (big)
part of this already), a frontier-level agent has lots of leeway to do
stuff. What stuff needs doing is dependent on where on the graph we are
working. So, how do we provide graph-context-aware skills and goals to the
LLM as it works? We have quests, we have documents, they live on a graph
(including, I'd say, the semantic-scholar cite graph that can be traversed).

## Article and further analysis

ZS Associates engineers explained at AI Engineer World's Fair 2026 why they
abandoned their multi-agent pharma-analytics pipeline after it produced
correct causes but wrong recommendations.

The rebuilt system uses a deterministic signal-detection step before a
single agent investigates, with a pharma knowledge graph bounding its
hypothesis space.

What previously took an analyst three to four weeks now completes in
roughly 20-30 minutes across about 50 LLM turns.

That locality is exactly why a single agent on top of a good graph can "do
many distinct things" without needing a whole zoo of agents: you move the
locus of specialization into the graph's structure instead of into the
agent topology.
([apxml](https://apxml.com/courses/agentic-llm-memory-architectures/chapter-2-advanced-agent-architectures-reasoning/graph-based-reasoning))

## Locality on a well-connected graph

On a dense domain graph, "where you are" (the current node and its
neighborhood) already encodes a lot of context: entity type, constraints,
relevant relations, and admissible actions.
([ontotext](https://www.ontotext.com/blog/reasoning-with-big-knowledge-graphs/))

Traversal then becomes targeted: the agent doesn't have to globally search;
it just follows meaningful edges (e.g., drug → payer → tier, catalyst →
support → synthesis route) to get the specific slice of reasoning it needs.
([linkedin](https://www.linkedin.com/posts/anthony-alcaraz-b80763155_why-graphs-are-becoming-the-foundation-for-activity-7305879366322843648-9MZz))

Concretely, each neighborhood in the graph can correspond to a
qualitatively different activity:

- In a commercial pharma graph, being near `Payer→Plan→Tier` supports
  reimbursement reasoning and contracting actions.
  ([youtube](https://www.youtube.com/watch?v=u6jJcIFDLE4))
- In a microfluidics graph, being near `Channel→Junction→Valve→Pump`
  supports flow control, routing, and protocol planning.
- In a materials graph, being near `Alloy→Phase→Defect→Process` supports
  DFT setup, phase-stability reasoning, or process-window search.
  ([sciencedirect](https://www.sciencedirect.com/science/article/pii/S1674862X2200012X))

One agent can do all of these simply by landing in different neighborhoods
and using different edge types as hypotheses, instead of swapping in
different "specialist agents."

## Why this lets you "do many distinct things"

Several properties of graph-based reasoning make this work.
([medium](https://medium.com/@jsemrau/why-graphs-matter-to-ground-reasoning-and-memory-in-agents-9661a98c9cd0))

- **Semantic type locality**: node and edge types act like local schemas;
  once the agent is in a region with edges like `has_rate_constant`,
  `is_reversible`, or `requires_temperature`, the admissible questions and
  actions are almost self-evident.
- **Tool routing by topology**: you can associate tools or code paths with
  node/edge types (e.g., "DFT job launcher" on `ElectronicStructureModel`
  nodes, "CFD solver" on `Geometry→BoundaryCondition` edges). The same agent
  calls different tools depending on where it is in the graph.
  ([linkedin](https://www.linkedin.com/pulse/collaboration-hero-multi-agent-ai-leveraging-graphs-dr-su-mba-4x5pc))
- **Multi-hop compositionality**: because graphs support multi-hop
  reasoning, you can chain distinct activities, e.g.
  `Material → Property → Application → Device → ManufacturingStep`, with
  each hop using different tools and heuristics but under a single
  narrative.
  ([kdd](https://www.kdd.org/exploration_files/p124-Neural_Symbolic_KGR_survey.pdf))

This matches the intuition: you don't need multiple agents if the "distinct
things" are actually distinct *regions of the same structured space* that
one agent can traverse.

## Global coherence from local moves

The other side of locality is that the graph still provides global
structure, so the agent's local decisions tie into a coherent whole.
([w3](https://www.w3.org/2022/12/plausible-reasoning.pdf))

- Paths are interpretable: a recommendation is "because we traversed
  `Drug→Payer→TierChange→VolumeDrop→ChannelMix`," not a black-box chain of
  prompts.
  ([medium](https://medium.com/@saeedhajebi/building-ai-agents-with-knowledge-graph-memory-a-comprehensive-guide-to-graphiti-3b77e6084dec))
- Constraints can live in the ontology: disallowed actions, physical laws,
  business rules can be attached to nodes/edges and enforced wherever the
  agent roams.
  ([repositum.tuwien](https://repositum.tuwien.at/bitstream/20.500.12708/17187/1/Jahn%20Rebecca%20-%202021%20-%20Reasoning%20in%20knowledge%20graphs%20Methods%20and%20techniques.pdf))
- You can carve contexts into overlapping subgraphs: e.g., lab automation
  vs. simulation vs. data analysis, all sharing some nodes but with
  different local rules, so the same agent behaves differently depending on
  which subgraph it's currently "inside."
  ([arxiv](https://arxiv.org/html/2602.05665v1))

So the "location" idea is powerful: by making location in the graph the
primary conditioning variable, you get a single agent whose behavior is
strongly context-dependent without complicated agent topologies.

## How you could exploit this in the precis-mcp stack

For something like a DFT/MD/microfluidics workflow, you might define a
graph with regions roughly like:

- Experiment / simulation design (materials, geometries, boundary
  conditions).
- Execution (cluster jobs, controllers, instruments).
- Analysis (results, derived properties, models).
- Planning / campaigns (hypotheses, experiment sets, optimization loops).
  ([apxml](https://apxml.com/courses/agentic-llm-memory-architectures/chapter-2-advanced-agent-architectures-reasoning/graph-based-reasoning))

Then:

- Bind tools to regions (Slurm submit on `Job` nodes, COMSOL/CFD runners on
  `Geometry+Mesh`, lab-controller APIs on `Instrument` nodes).
- Let one agent navigate this graph; "doing many distinct things" becomes
  "following different paths through the same graph," not invoking
  different agents.
  ([medium](https://medium.com/@jsemrau/why-graphs-matter-to-ground-reasoning-and-memory-in-agents-9661a98c9cd0))

If we describe one concrete workflow to unify first (e.g., "end-to-end
microfluidic chip design and experiment execution," or — closer to what
exists today — "end-to-end lit-review quest over the citation graph"), the
next step is a node/edge schema showing exactly how locality would map to
distinct agent behaviors.

## Open question

This is a framing/architecture proposal, not a scoped feature. Before it
becomes an ADR it needs: (a) a decision on which existing precis-mcp
subsystem is the first testbed (quest layer + citation graph is the
obvious candidate — see `next-things.md` §1), and (b) an honest look at
whether today's quest-worker dispatch already does a version of this
(kind-scoped dispatch, `state-map.md`) or whether it's still agent-topology
shaped underneath.
