"""Handlers — one adapter per kind (~70 kinds).

Each module implements :class:`precis.protocol.Handler` for one kind and
declares its verb surface via :class:`precis.protocol.KindSpec`; handlers
register with the :class:`precis.dispatch.Hub` at boot (contract + failure
modes: :mod:`precis.dispatch`). Shared shapes live in underscore-prefixed
sibling modules (``_numeric_ref``, ``_todo_views``, ``_job_bubble``, ...).

**Container-aware ``exclude=`` (cite closure).** ``search(kind='paper')``
and ``get(kind='semanticscholar')`` accept ``exclude=`` entries that are
paper slugs, a draft ref (``dr…``), or a draft chunk (``dc…``, hierarchical
subtree); containers resolve server-side to every paper cited within —
``[pa…]`` direct, ``[pc…]`` via owning paper, ``[fi…]`` via the hub's
supporter papers (:mod:`precis.handlers._exclude_closure`). The
semanticscholar surface additionally diffs every external hit against the
held corpus (DOI → arXiv → normalized title), flagging
``held``/``stub``/``NEW`` — a NEW hit accepts via the existing
``put(kind='paper', doi=…)`` stub path. Relevance-ranked only, no recency
assumption. Bogus container entries raise ``BadInput``; bare stale slugs
keep the historical silent-drop contract.

The todo tree
=============

``kind='todo'`` is a hierarchical todo graph unifying intent, scheduling,
execution, and review. Handler detail: :mod:`precis.handlers.todo`;
skills ``precis-todo-tree-help``, ``precis-minter-help``.

**Facet model (§M, migration 0102).** One faceted kind — ``tags`` +
``meta`` — never a family of kinds: level gradient, schedule shape, LLM
auto-run tier all live in ``meta``. The one kept boundary is todo ↔ job: a
job is claimed/leased/executor-run (``FOR UPDATE SKIP LOCKED``,
``idem_key``, sweeper, lease-steal); a todo is durable intent, never
leased. Pinned by ``tests/test_todo_job_boundary.py``.

**Hierarchy.** ``parent_id`` on refs. Gradient: ``meta.rotation_root=true``
(strategic root) / ``meta.worker_mintable=false`` (tactical) / neither
(subtask, the default). Ancestry walked on read; a 1/N rotation across
strategic roots drives 7-day picks. Reparenting goes through the reserved
``parent`` link relation, not a raw column write.

**auto_check leaves.** ``meta.auto_check`` wait-for-condition evaluators
live under ``workers/auto_check_evaluators/``: ``paper_ingested``,
``discord_reply_received``, ``time_past``, ``tag_present``,
``child_job_succeeded``, ``derived_job_succeeded``,
``all_child_findings_resolved``.

**Recurring (Watches).** ``meta.schedule`` presence *is* recurring — cron
/ ``every:`` shorthand, or a one-shot ``at``; no separate tag
(``level:recurring`` retired, §M). The schedule worker
(``workers/schedule/worker.py``) mints one worker-mintable subtask per due
tick; ``meta.deliver={'target': ...}`` fires ``pg_notify('precis.cron',
...)`` for asa_bot instead (the retired ``kind='cron'`` mechanism, folded
into recurring). ``prio`` is an int column on refs (1..10); ``PRIO:*`` is
a back-compat tag alias.

**Jobs — two lanes by parent kind.** ``JobHandler.put`` requires a
``parent_id`` whose kind is in ``JOB_PARENT_KINDS`` (``todo``/
``structure``/``cad``/``draft``/``quest``, plus a coordinator job for
campaign fan-out). *Intent lane* — parent a todo: rotation + failure
bubble + ``child_job_succeeded``. *Compute lane* — parent a build
subject: a derived, idempotent, cache-fillable job owned by the artifact;
a blocking task links ``requested`` → job (``derived_job_succeeded``
closes it; the bubble follows the link on failure). The ``dispatch``
worker walks open todos carrying ``meta.executor``, mints ``kind='job'``
children, auto-injects ``meta.auto_check={'type':
'child_job_succeeded'}``. A **succeeded** child blocks re-mint for a
deterministic (non-``llm_tier``) parent until ``auto_check`` flips it
done (``dispatch._job_blocks_dispatch_sql``) — the brake against daily
re-mint storms. On **failure** the parent gets a ``child-failed:<job_id>``
open tag (``handlers/_job_bubble.py``) and leaves the doable view;
infra-class failures (``INFRA_FAILURE_TAGS`` — lease-expiry orphan sweep
or a signal-killed/result-less compute child) get a bounded auto-retry
(``ORPHAN_RETRY_CAP`` 3 per 6h window) before latching; content-class
failures latch immediately. A latched bubble isn't necessarily terminal:
the sweeper's ``unpark`` phase (``workers/sweeper.py``) gives a parked
parent up to ``UNPARK_CAP`` (3) autonomous, cool-down-gated re-arms before
stamping the terminal ``child-failed-final`` tag a human must clear.

**Planner coroutines.** A ``meta.llm_tier`` todo runs ``plan_tick``: each
tick is a job that may mint children or yield (``ask-user:``) and still
exit succeeded. ``child_job_succeeded`` never auto-closes an
``llm_tier`` parent or one with a live child todo. Lease is 90 min. A
tick cut off by exhaustion (``--max-turns`` or wall-clock timeout) is
resumable, not a failure — re-minted up to
``meta.plan_tick_resume_streak`` (default 3, env
``PRECIS_PLAN_TICK_RESUME_CAP``), then one auto-decompose tick
(``meta.plan_tick_decompose_attempted``) before a real ``child-failed:``
bubble. A tick that called no precis verb is likewise resumable-not-
success (``_precis_tools_used`` in ``executors/claude_inproc.py``,
transport-neutral: stream ``tool_use`` count or
``LlmResult.tool_calls``). A child parked only on
``ask-user:``/``waiting-for:`` (no hard-block tag) stops blocking
re-candidacy 6h after the parent's last ``plan_tick``
(``dispatch._parked_child_still_blocks_sql``), so one human-blocked leaf
doesn't freeze the subtree. Draft-bound ticks get sources pre-rendered
into the prompt's variable layer
(``workers/planner_prompt._render_draft_sources``, ``needs_sources``
predicate) — the dominant exhaustion class was fetching, not thinking.

**Planner cost guardrails.** ``workers/planner_guardrails.check_parent``
runs before every mint, cheapest first: per-todo tick cap
(``PRECIS_MAX_TICKS`` 10 → ``halt:tick-cap``), per-todo cost
(``PRECIS_MAX_TODO_USD`` $2), per-tree cost (``PRECIS_MAX_TREE_USD``
$10), global daily ceiling (``PRECIS_DAILY_COST_CEILING`` $20, pauses
discretionary dispatch for the round). Cadence work (parent carries
``meta.schedule``, ``dispatch._cadence_parent_ids``) and zero-LLM compute
mints (``ssh_node``/``job_inproc`` executor, no ``llm_tier``,
``dispatch._zero_llm_parent_ids``) are exempt from the ceiling alone,
still bound by the three caps. Dollar caps read ``llm_call_log.cost_usd``
(``plan_tick`` stamps ``LlmRequest.ref_id`` = parent), subscription
transports included. Code defaults; prod runs the deploy templates'
values (``tests/test_deploy_planner_caps.py`` asserts every render site
sets all four).

**Orphan subtrees.** ``retired_at`` is not transitive; the ancestor walk
is ``utils/ref_tree.deleted_in_ancestry``, applied both dispatching
(``dispatch._drop_orphaned`` silently skips candidates under a deleted
todo) and growing (``quest/weave_review.mint_review_todo`` raises
``OrphanedParentError`` before inserting — code-minting paths bypass
``TodoHandler.put``'s liveness check).

**Views.** ``TodoView`` StrEnum + ``_TREE_SEARCH_VIEWS`` dispatch table
in :mod:`precis.handlers.todo` (import-time totality assert).
``view='tree'`` walks ``kind IN ('todo','job')`` so child jobs render
with a ``⚙`` marker; ``view='attention'`` unions ``ask-user`` leaves,
``child-failed`` parents, and halted todos; ``view='projects'``
(``_todo_views.render_projects``) lists workspace-owning roots.

**Projects.** A project is a strategic-root todo owning
``meta.workspace`` (no new kind). ``TodoHandler.put`` stamps a
``project:<slug>`` tag derived from ``meta.workspace.path``
(``utils/workspace.project_tag_for_path``) on every write path.
``meta.workspace.brief`` cascades down the subtree into the planner
prompt's variable layer (``workers/planner_prompt._render_project_brief``)
— per-project, kept out of the cached system layer.
"""
