"""systemd unit templates quote their ``Environment=`` values (gr764103).

systemd splits an unquoted ``Environment=K=V`` on whitespace and strips double
quotes, so ``PRECIS_SLICE_REPOS={"catpath": "/home/coder/catpath"}`` reached the
process as ``{catpath:`` and the host silently stopped advertising the
``repo_checkout_catpath`` capability. The earlier tests only ``json.loads`` the
Ansible value; these render each unit and parse the ``Environment=`` lines the
way systemd does (quoted word, ``\\"`` / ``\\\\`` escapes, ``%%`` specifier).
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import jinja2
import pytest

_REPO_ROOT = Path(__file__).resolve().parent.parent
_ROLES = _REPO_ROOT / "deploy" / "roles"

_UNITS = {
    "service_unit": _ROLES / "service_unit" / "templates" / "systemd.service.j2",
    "precis_worker": _ROLES
    / "precis_worker"
    / "templates"
    / "precis-worker.service.j2",
    "precis_worker_agent": _ROLES
    / "precis_worker_agent"
    / "templates"
    / "precis-worker-agent.service.j2",
    "precis_watch": _ROLES / "precis_watch" / "templates" / "precis-watch.service.j2",
    "monitoring_receiver": _ROLES
    / "monitoring"
    / "templates"
    / "precis-alertmanager-receiver.service.j2",
    "llamaswap": _ROLES / "llamacpp" / "templates" / "llamaswap.service.j2",
    "precis_embedder": _ROLES
    / "precis_embedder"
    / "templates"
    / "precis-embedder.service.j2",
    "precis_embedder_watchdog": _ROLES
    / "precis_embedder_watchdog"
    / "templates"
    / "precis-embedder-watchdog.service.j2",
}

_REPOS = {"catpath": "/home/coder/catpath"}
#: Values that systemd mangles when unquoted: JSON (space, quotes), a plain
#: space, ``%`` (specifier), a backslash, a double quote, and all of them at once.
_NASTY = {
    "PRECIS_SLICE_REPOS": json.dumps(_REPOS),
    "T_SPACE": "a b",
    "T_PERCENT": "50%",
    "T_BACKSLASH": "C:\\dir\\x",
    "T_QUOTE": 'say "hi"',
    "T_COMBO": 'a b\\"50%% %h "x" \\',
    # a trailing newline would split the line and unbalance the quote; the
    # macro trims it, so the delivered value is the stripped one
    "T_NEWLINE": "x y\n",
}
_COMBO = _NASTY["T_COMBO"]

_CONTEXT: dict[str, dict[str, object]] = {
    "service_unit": {
        "service_unit_env": dict(_NASTY),
    },
    "precis_worker": {
        "precis_shared_env": dict(_NASTY),
        "precis_worker_venv": _COMBO,
        "precis_worker_dsn": _COMBO,
        "precis_worker_hf_home": _COMBO,
        "precis_worker_embedder": _COMBO,
        "precis_worker_embedder_url": _COMBO,
        "precis_capabilities": {"dft": [_COMBO]},
        "precis_suspended_job_types": _COMBO,
        "inventory_hostname": _COMBO,
        "groups": {"agent_sandbox_hosts": [_COMBO]},
    },
    "precis_worker_agent": {
        "precis_shared_env": dict(_NASTY),
        "precis_worker_agent_venv": _COMBO,
        "precis_worker_agent_dsn": _COMBO,
        "precis_worker_agent_embedder": _COMBO,
        "precis_worker_agent_embedder_url": _COMBO,
        "precis_agent_image": _COMBO,
        "precis_suspended_job_types": _COMBO,
    },
    "precis_watch": {
        "precis_shared_env": dict(_NASTY),
        "precis_watch_venv": _COMBO,
        "precis_watch_dsn": _COMBO,
        "precis_worker_hf_home": _COMBO,
    },
    "monitoring_receiver": {
        "precis_shared_env": dict(_NASTY),
        "monitoring_receiver_venv": _COMBO,
        "monitoring_receiver_pgpass": _COMBO,
        "monitoring_receiver_dsn": _COMBO,
    },
    "llamaswap": {"llamacpp_bin_dir": _COMBO},
    "precis_embedder": {
        "precis_embedder_venv": _COMBO,
        "precis_embedder_hf_home": _COMBO,
    },
    "precis_embedder_watchdog": {
        "precis_embedder_watchdog_host": "h",
        "precis_embedder_watchdog_port": 1,
        "precis_embedder_watchdog_fail_threshold": _COMBO,
        "precis_embedder_watchdog_statefile": _COMBO,
        "precis_embedder_watchdog_curl_timeout": _COMBO,
        "precis_embedder_watchdog_systemd_unit": _COMBO,
    },
}

# env key -> expected delivered value, for the non-loop entries.
_EXPECTED_FIXED: dict[str, dict[str, str]] = {
    "service_unit": {},
    "precis_worker": {
        "PATH": _COMBO + "/bin:/usr/local/bin:/usr/bin:/bin",
        "PRECIS_DATABASE_URL": _COMBO,
        "HF_HOME": _COMBO,
        "PRECIS_EMBEDDER": _COMBO,
        "PRECIS_EMBEDDER_URL": _COMBO,
        "PRECIS_DFT_NODE": _COMBO,
        "PRECIS_SUSPENDED_JOB_TYPES": _COMBO,
        "PRECIS_NODE": _COMBO,
    },
    "precis_worker_agent": {
        "PATH": _COMBO + "/bin:/usr/local/bin:/usr/bin:/bin",
        "PRECIS_DATABASE_URL": _COMBO,
        "PRECIS_EMBEDDER": _COMBO,
        "PRECIS_EMBEDDER_URL": _COMBO,
        "PRECIS_AGENT_IMAGE": _COMBO,
        "PRECIS_SUSPENDED_JOB_TYPES": _COMBO,
    },
    "precis_watch": {
        "PATH": _COMBO + "/bin:/usr/local/bin:/usr/bin:/bin",
        "PRECIS_DATABASE_URL": _COMBO,
        "HF_HOME": _COMBO,
    },
    "monitoring_receiver": {
        "PATH": _COMBO + "/bin:/usr/local/bin:/usr/bin:/bin",
        "PGPASSFILE": _COMBO,
        "PRECIS_DATABASE_URL": _COMBO,
    },
    "llamaswap": {"PATH": _COMBO + ":/usr/local/bin:/usr/bin:/bin"},
    "precis_embedder": {
        "PATH": _COMBO + "/bin:/usr/local/bin:/usr/bin:/bin",
        "HF_HOME": _COMBO,
    },
    "precis_embedder_watchdog": {
        "PRECIS_EMBEDDER_WATCHDOG_URL": "http://h:1/readyz",
        "PRECIS_EMBEDDER_WATCHDOG_FAIL_THRESHOLD": _COMBO,
        "PRECIS_EMBEDDER_WATCHDOG_STATEFILE": _COMBO,
        "PRECIS_EMBEDDER_WATCHDOG_CURL_TIMEOUT": _COMBO,
        "PRECIS_EMBEDDER_WATCHDOG_RESTART_CMD": "systemctl restart " + _COMBO,
    },
}


def _combine(base: dict[str, object], *more: dict[str, object]) -> dict[str, object]:
    out = dict(base)
    for m in more:
        out.update(m)
    return out


def _render(source: str, **ctx: object) -> str:
    """Render like ansible: ``trim_blocks`` on, ``combine``/``bool`` stubbed in."""
    env = jinja2.Environment(trim_blocks=True, undefined=jinja2.ChainableUndefined)
    env.filters["combine"] = _combine
    env.filters["bool"] = lambda v: str(v).strip().lower() in {"1", "true", "yes", "on"}
    return env.from_string(source).render(ansible_managed="managed", **ctx)


def _systemd_words(rest: str) -> list[str]:
    """Split an ``Environment=`` right-hand side into words like systemd: words
    break on unquoted whitespace, ``"`` delimits a quoted run (and is dropped),
    and inside a word ``\\"`` and ``\\\\`` unescape."""
    words: list[str] = []
    cur: list[str] = []
    in_word = in_quote = False
    i = 0
    while i < len(rest):
        c = rest[i]
        if c == "\\" and i + 1 < len(rest) and rest[i + 1] in '"\\':
            cur.append(rest[i + 1])
            in_word = True
            i += 2
            continue
        if c == '"':
            in_quote = not in_quote
            in_word = True
        elif c.isspace() and not in_quote:
            if in_word:
                words.append("".join(cur))
                cur, in_word = [], False
        else:
            cur.append(c)
            in_word = True
        i += 1
    if in_word:
        words.append("".join(cur))
    return words


def _parse_environment(unit: str) -> dict[str, str]:
    """Every ``Environment=`` line as systemd delivers it: each word is a
    ``K=V`` assignment (a word without ``=`` is dropped by systemd, so it is a
    failure here), and ``%%`` in the value becomes ``%``."""
    env: dict[str, str] = {}
    for line in unit.splitlines():
        if not line.startswith("Environment="):
            continue
        for word in _systemd_words(line[len("Environment=") :]):
            key, sep, value = word.partition("=")
            assert sep, f"systemd would drop the word {word!r} of {line!r}"
            env[key] = value.replace("%%", "%")
    return env


def _expected(name: str) -> dict[str, str]:
    expected = dict(_EXPECTED_FIXED[name])
    ctx = _CONTEXT[name]
    loop_env = ctx.get("service_unit_env") or ctx.get("precis_shared_env") or {}
    assert isinstance(loop_env, dict)
    expected.update({k: str(v).strip() for k, v in loop_env.items()})
    return expected


def _assert_round_trips(name: str, source: str) -> None:
    unit = _render(source, **_CONTEXT[name])
    got = _parse_environment(unit)
    for key, want in _expected(name).items():
        assert got.get(key) == want, (
            f"{name}: Environment {key} reached systemd as {got.get(key)!r}, "
            f"want {want!r}"
        )


@pytest.mark.parametrize("name", sorted(_UNITS))
def test_unit_environment_values_round_trip_through_systemd_parsing(name: str) -> None:
    _assert_round_trips(name, _UNITS[name].read_text(encoding="utf-8"))


@pytest.mark.parametrize("name", ["service_unit", "precis_worker", "precis_watch"])
def test_slice_repos_json_survives_as_a_dict(name: str) -> None:
    unit = _render(_UNITS[name].read_text(encoding="utf-8"), **_CONTEXT[name])
    got = _parse_environment(unit)
    assert json.loads(got["PRECIS_SLICE_REPOS"]) == _REPOS


@pytest.mark.parametrize("name", sorted(_UNITS))
def test_reverting_to_the_raw_form_fails_the_round_trip(name: str) -> None:
    """The revert check: swap the macro body back to the old
    ``Environment=K=V`` form and the same assertion must fail."""
    source = _UNITS[name].read_text(encoding="utf-8")
    quoted = re.search(r'^Environment=".*"$', source, flags=re.MULTILINE)
    assert quoted, f"{name}: no quoted Environment= macro body to revert"
    raw = source.replace(quoted.group(0), "Environment={{ key }}={{ val }}")
    assert raw != source
    with pytest.raises(AssertionError):
        _assert_round_trips(name, raw)


def test_no_unit_template_renders_a_raw_environment_assignment() -> None:
    """Any ``Environment=`` line in deploy/ that renders a variable must go
    through the quoting macro; a raw ``Environment=K={{ v }}`` is the bug."""
    offenders = [
        f"{p.relative_to(_REPO_ROOT)}:{n}"
        for p in (_REPO_ROOT / "deploy").rglob("*")
        if p.is_file() and p.suffix in {".j2", ".yml", ".yaml"}
        for n, line in enumerate(p.read_text(encoding="utf-8").splitlines(), 1)
        # the quoted macro body is ``Environment="..."``; anything else is raw
        if re.match(r'\s*Environment=[^"]', line) and "{{" in line
    ]
    assert offenders == []
