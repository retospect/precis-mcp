---
description: "One honest 'what needs doing' across repo dev work (graph specs, gripes, PRs, Dependabot) and the prod todo queue, plus hygiene and system-health scans."
argument-hint: "[optional focus, e.g. 'dark-factory' or 'drafts']"
allowed-tools: Read, Bash(grep:*), Bash(ssh:*), Bash(gh:*), Bash(scripts/migration-check:*), Bash(scripts/docs-index:*), Bash(scripts/memory-lint:*), Bash(scripts/skill-eval:*), Bash(scripts/backlog-lint:*), Bash(scripts/token-review:*), Bash(scripts/surface-review:*), Bash(scripts/db-thrash-review:*), Bash(scripts/skill-search-review:*), Bash(scripts/gripe-gc-review:*), Bash(scripts/fda-grant-review:*), Bash(scripts/secrets-check-review:*), Bash(scripts/prod-precis secret check:*), Bash(scripts/nightly:*), Bash(scripts/main-ci-status:*), Bash(scripts/prod-precis maintenance cfp-watch:*), Bash(scripts/coderef:*), mcp__precis__get, mcp__precis__search
---

<!-- precis-stub: whatneedsdoing -->

# whatneedsdoing

This skill lives in the precis corpus. Before any task work, read it
and follow it:

    get(kind='skill', id='whatneedsdoing')

Search precis skills (`search(kind='skill', q='<goal>')`) before
starting any other task.

MCP down: stop the task. Retry the connection with growing waits
(1, 2, 4, 8 minutes, then every 15) and resume when it answers. Do
not work the task without skills.

Arguments: $ARGUMENTS
