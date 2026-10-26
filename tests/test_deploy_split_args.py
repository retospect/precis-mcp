"""Deploy playbooks: every free-form shell/command arg must split cleanly.

r109 (2026-10-26, gr519391): an apostrophe in a ``#`` comment inside the
deployed-history shell block of ``deploy/redeploy-precis.yml`` unbalanced
ansible's argument splitter. Ansible runs ``parse_kv`` → ``split_args``
over the *string* form of every shell/command/raw/script arg while parsing
the task, and an unbalanced quote (or jinja2 block) raises
``AnsibleParserError`` — the deploy died at parse time, before a host ran.
f508f42e0 fixed the comment; nothing in the suite caught the class of bug.
YAML ``#`` inside a block scalar is not a comment to the splitter — it is
just more arg text.

The gate walks every list-shaped ``deploy/**/*.yml`` — playbooks are lists
of plays and task files are lists of tasks (vars files are dicts and their
values are never split) — finds each free-form action arg given as a
string, and runs ansible's own splitter over it. A fixture playbook
carrying the r109 shape proves the gate fires. Skips with a reason when
ansible-core is not installed in the test env.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
import yaml

pytest.importorskip(
    "ansible.parsing.splitter",
    reason="ansible-core is not installed in the test env",
)

from ansible.errors import AnsibleParserError
from ansible.parsing.splitter import split_args

_DEPLOY = Path(__file__).resolve().parents[1] / "deploy"
_FIXTURE = (
    Path(__file__).resolve().parent / "fixtures" / "deploy" / "r109-shell-comment.yml"
)

# Actions whose string args ansible parses via parse_kv → split_args (the
# free-form set in ansible.parsing.mod_args), in their bare, fqcn and legacy
# spellings. A dict value (``shell: {cmd: ...}``) is passed verbatim — the
# splitter never sees it.
_ACTIONS = ("shell", "command", "raw", "script", "win_shell", "win_command")
_FREEFORM_KEYS = {
    key
    for prefix in ("", "ansible.builtin.", "ansible.legacy.", "ansible.windows.")
    for key in (prefix + a for a in _ACTIONS)
}


def _freeform_args(node: Any, where: str = "") -> list[tuple[str, str]]:
    """Every ``(where, arg-string)`` under *node* where a free-form action
    key carries a string value. Recurses through plays, task lists,
    block/rescue/always nests and role task files alike."""
    found: list[tuple[str, str]] = []
    if isinstance(node, dict):
        for key, value in node.items():
            at = f"{where}/{key}"
            if key in _FREEFORM_KEYS and isinstance(value, str):
                found.append((at, value))
            else:
                found.extend(_freeform_args(value, at))
    elif isinstance(node, list):
        for i, item in enumerate(node):
            found.extend(_freeform_args(item, f"{where}[{i}]"))
    return found


def _task_files() -> list[Path]:
    """``deploy/**/*.yml`` docs whose top level is a list — the shape of a
    playbook or a tasks file. Dict-topped files (group_vars, inventory
    samples) hold data, never task args."""
    out = []
    for path in sorted(_DEPLOY.rglob("*.yml")) + sorted(_DEPLOY.rglob("*.yaml")):
        docs = list(yaml.safe_load_all(path.read_text(encoding="utf-8")))
        if any(isinstance(doc, list) for doc in docs):
            out.append(path)
    return out


def test_deploy_tree_has_task_files() -> None:
    """Guard against a vacuous walk: if the glob found nothing, the gate
    below would pass over silence."""
    files = _task_files()
    assert files, "no list-shaped yml under deploy/ — the split_args gate is empty"
    assert any(p.name == "redeploy-precis.yml" for p in files)


def test_every_freeform_arg_splits_cleanly() -> None:
    """The r109 gate: split_args must accept every string-form shell/command
    arg in the deploy tree, comments included."""
    failures: list[str] = []
    scanned = 0
    for path in _task_files():
        rel = path.relative_to(_DEPLOY.parent)
        for doc in yaml.safe_load_all(path.read_text(encoding="utf-8")):
            for where, arg in _freeform_args(doc):
                scanned += 1
                try:
                    split_args(arg)
                except AnsibleParserError as e:
                    failures.append(f"{rel}:{where} — {str(e).splitlines()[0]}")
    assert scanned, "no free-form string args found — the gate is vacuous"
    assert not failures, "unbalanced quote/jinja2 in a free-form arg:\n" + "\n".join(
        failures
    )


def test_the_r109_fixture_playbook_fails_the_gate() -> None:
    """Proof the gate fires: the fixture's apostrophe-in-comment is exactly
    what killed the r109 deploy."""
    (doc,) = yaml.safe_load_all(_FIXTURE.read_text(encoding="utf-8"))
    found = _freeform_args(doc)
    assert len(found) == 1
    with pytest.raises(AnsibleParserError, match="unbalanced"):
        split_args(found[0][1])
