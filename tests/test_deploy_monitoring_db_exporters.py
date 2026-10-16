"""gr465126 (ruling D2a): the DB scrape jobs and the rules fed only by their
metrics sit behind one role var, ``monitoring_db_exporters`` (default false).

With it off, a monitoring node whose DB node runs no postgres_exporter
(:9187) or pgbouncer_exporter (:9127) renders neither job, so no ``up``
series exists for them — NodeDown cannot fire on a target that was never
scraped — and PostgresDown, which reads postgres_exporter's ``pg_up``, is
not loaded. With it on, the rendered config and rules are exactly the
pre-gate output.
"""

from __future__ import annotations

from pathlib import Path

import jinja2
import yaml

_ROLE = Path(__file__).resolve().parent.parent / "deploy" / "roles" / "monitoring"

_VARS = {
    "ansible_managed": "x",
    "postgres_host": "pg",
    "groups": {"all": ["pg", "n1"]},
    "hostvars": {
        "pg": {"ansible_host": "203.0.113.10"},
        "n1": {"ansible_host": "203.0.113.11"},
    },
}

#: Jobs and rules fed solely by postgres_exporter / pgbouncer_exporter
#: metrics. ``up{job="postgres"|"pgbouncer"}`` disappears with the jobs, so
#: NodeDown needs no guard of its own.
_DB_JOBS = {"postgres", "pgbouncer"}
_DB_RULES = {"PostgresDown"}


def _render(name: str, **over: object) -> str:
    env = jinja2.Environment(undefined=jinja2.ChainableUndefined)
    env.filters["bool"] = bool
    src = (_ROLE / "templates" / name).read_text(encoding="utf-8")
    return env.from_string(src).render(**{**_VARS, **over})


def _jobs(flag: bool) -> list[dict]:
    doc = yaml.safe_load(_render("prometheus.yml.j2", monitoring_db_exporters=flag))
    return doc["scrape_configs"]


def _rules(flag: bool) -> dict[str, dict]:
    doc = yaml.safe_load(_render("alert_rules.yml.j2", monitoring_db_exporters=flag))
    return {r["alert"]: r for g in doc["groups"] for r in g["rules"]}


def test_role_default_is_off() -> None:
    defaults = yaml.safe_load(
        (_ROLE / "defaults" / "main.yml").read_text(encoding="utf-8")
    )
    assert defaults["monitoring_db_exporters"] is False


def test_flag_off_renders_no_db_jobs_and_no_db_only_rules() -> None:
    jobs = _jobs(False)
    assert [j["job_name"] for j in jobs] == ["prometheus", "node"]
    text = _render("alert_rules.yml.j2", monitoring_db_exporters=False)
    rules = {r["alert"]: r for g in yaml.safe_load(text)["groups"] for r in g["rules"]}
    assert _DB_RULES.isdisjoint(rules)
    # No leftover reference to a metric only the DB exporters publish.
    assert "pg_up" not in text
    # The node backstop stays: it still guards every job that does render.
    assert rules["NodeDown"]["expr"] == "up == 0"
    # Nothing else was dropped.
    assert {
        "HighDiskUsage",
        "HighMemoryUsage",
        "ColimaDockerDiskHigh",
        "NFSMountHung",
        "NASMountHung",
    } <= set(rules)


def test_flag_off_is_also_the_unset_render() -> None:
    # ChainableUndefined + ``| bool`` == the role default: an inventory that
    # never sets the var gets the off render, not a template error.
    doc = yaml.safe_load(_render("prometheus.yml.j2"))
    assert _DB_JOBS.isdisjoint(j["job_name"] for j in doc["scrape_configs"])
    doc = yaml.safe_load(_render("alert_rules.yml.j2"))
    rules = {r["alert"] for g in doc["groups"] for r in g["rules"]}
    assert _DB_RULES.isdisjoint(rules)


def test_flag_on_renders_both_db_jobs_and_postgresdown() -> None:
    jobs = {j["job_name"]: j for j in _jobs(True)}
    assert jobs["postgres"]["static_configs"][0]["targets"] == ["203.0.113.10:9187"]
    assert jobs["pgbouncer"]["static_configs"][0]["targets"] == ["203.0.113.10:9127"]
    assert [j["job_name"] for j in _jobs(True)] == [
        "prometheus",
        "node",
        "postgres",
        "pgbouncer",
    ]
    pg = _rules(True)["PostgresDown"]
    assert pg["expr"] == "pg_up == 0"
    assert pg["for"] == "2m"
    assert pg["labels"]["severity"] == "critical"
