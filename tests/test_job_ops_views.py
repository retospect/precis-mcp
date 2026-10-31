"""Tests for ``JobHandler``'s ``id='/limits'`` and ``id='/delta'`` ops
views (gr504488, gr504490).

``/limits`` is the read-only surface onto ``external_rate_limits``
(migration 0121) — the table agents used to detour to prod-psql for.
``/delta`` is the doctor tick's "only what changed" read, keyed on the
last succeeded doctor_tick's start. Rows are tagged with a uuid so
assertions survive the shared ``precis_test`` DB.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest

from precis.alerts import raise_alert
from precis.dispatch import Hub
from precis.handlers.gripe import GripeHandler
from precis.handlers.job import JobHandler
from precis.store import Store, Tag


@pytest.fixture
def jobs(hub: Hub) -> JobHandler:
    return JobHandler(hub=hub)


@pytest.fixture
def gripe(hub: Hub) -> GripeHandler:
    return GripeHandler(hub=hub)


def _seed_limit(
    store: Store,
    provider: str,
    *,
    capacity: int = 4,
    refill: float = 1.0,
    tokens: float = 4,
    daily_cap: int | None = None,
    day_used: int = 0,
    **override_cols: object,
) -> None:
    with store.pool.connection() as conn:
        conn.execute(
            "DELETE FROM external_rate_limits WHERE provider = %s", (provider,)
        )
        conn.execute(
            "INSERT INTO external_rate_limits "
            "(provider, capacity, refill_per_sec, tokens, daily_cap, day_used) "
            "VALUES (%s, %s, %s, %s, %s, %s)",
            (provider, capacity, refill, tokens, daily_cap, day_used),
        )
        if override_cols:
            sets = ", ".join(f'"{c}" = %s' for c in override_cols)
            conn.execute(
                f"UPDATE external_rate_limits SET {sets} WHERE provider = %s",
                (*override_cols.values(), provider),
            )
        conn.commit()


# ── /limits ─────────────────────────────────────────────────────────


def test_limits_lists_named_providers(jobs: JobHandler, store: Store) -> None:
    prov = f"test-{uuid4().hex[:8]}"
    _seed_limit(store, prov, capacity=4, tokens=4, daily_cap=1000, day_used=42)

    resp = jobs.get(id="/limits")

    assert "external_rate_limits" in resp.body
    assert f"{prov} tokens=4.0/4" in resp.body
    assert "day=42/1000" in resp.body


def test_limits_hides_host_rows_by_default(jobs: JobHandler, store: Store) -> None:
    host_row = f"host:test-{uuid4().hex[:8]}.example"
    _seed_limit(store, host_row)

    resp = jobs.get(id="/limits")
    assert host_row not in resp.body
    assert "host:<netloc> rows hidden" in resp.body

    resp_all = jobs.get(id="/limits?hosts=1")
    assert host_row in resp_all.body


def test_limits_provider_pin(jobs: JobHandler, store: Store) -> None:
    prov = f"test-{uuid4().hex[:8]}"
    other = f"host:test-{uuid4().hex[:8]}.example"
    _seed_limit(store, prov, capacity=2, refill=0.0, tokens=0)
    _seed_limit(store, other)

    resp = jobs.get(id=f"/limits?provider={prov}")
    assert f"{prov} tokens=0.0/2" in resp.body
    assert other not in resp.body

    resp_host = jobs.get(id=f"/limits?provider={other}")
    assert other in resp_host.body


def test_limits_unknown_provider_is_empty(jobs: JobHandler) -> None:
    resp = jobs.get(id=f"/limits?provider=no-such-{uuid4().hex[:8]}")
    assert "no external_rate_limits row matches" in resp.body


# ── /limits effective-policy block (outbound_policy) ──────────────────


def test_limits_shows_effective_policy_with_layers(
    jobs: JobHandler, store: Store
) -> None:
    """Each row resolves its EffectivePolicy and every field names the
    merge layer it came from (S4 acceptance)."""
    prov = f"test-{uuid4().hex[:8]}"
    _seed_limit(store, prov, max_wait_s=5, on_refuse="defer")

    resp = jobs.get(id=f"/limits?provider={prov}")
    body = resp.body

    assert "effective:" in body
    # A named row with no POLICIES entry resolves under the default
    # policy: provider-api inferred, the lane's numbers read off the row.
    assert "cls=provider-api (inferred)" in body
    assert "rate=4/1/s daily=- (db)" in body
    assert "concurrency=- (class)" in body
    assert "max_wait_s=5 (db)" in body
    assert "connect_s=- (class)" in body
    assert "read_s=- (class)" in body
    assert "total_s=- (class)" in body
    assert "on_refuse=defer (db)" in body
    assert "retry=5x 1..60s (code)" in body
    assert "state: parked=- last_used=- refused_today=0 last_refused=-" in body


def test_limits_core_names_every_field_layer(jobs: JobHandler, store: Store) -> None:
    """The spec probe: ``/limits?provider=core`` names the layer of
    every field."""
    _seed_limit(store, "core", capacity=8, refill=1.0, tokens=8)
    resp = jobs.get(id="/limits?provider=core")
    body = resp.body
    for field in (
        "cls=",
        "rate=",
        "concurrency=",
        "max_wait_s=",
        "connect_s=",
        "read_s=",
        "total_s=",
        "on_refuse=",
        "retry=",
    ):
        assert field in body, field
    # core maps to POLICIES['core'] — one interpretation, no 'via' tag.
    assert "cls=provider-api (inferred)" in body


def test_limits_llm_api_carrier_shows_slot_occupancy(
    jobs: JobHandler, store: Store
) -> None:
    """An llm-api carrier host row resolves under openai-tools too, and
    the hold row's free/capacity renders."""
    _seed_limit(store, "host:openrouter.ai")
    with store.pool.connection() as conn:
        conn.execute(
            "DELETE FROM resource_slot_holds WHERE resource = 'llm-api:openrouter'"
        )
        conn.execute(
            "INSERT INTO resource_slots (host, resource, capacity, free, kind) "
            "VALUES ('api', 'llm-api:openrouter', 16, 11, 'hard') "
            "ON CONFLICT (host, resource) DO UPDATE SET capacity = 16, free = 11"
        )
        conn.commit()

    resp = jobs.get(id="/limits?provider=host:openrouter.ai")
    body = resp.body

    assert "effective (via provider-less): cls=scraped-host (inferred)" in body
    assert "effective (via openai-tools): cls=llm-api (code)" in body
    assert "concurrency=llm-api:openrouter (class)" in body
    assert "slots=llm-api:openrouter 11/16" in body


def test_limits_shows_host_row_occupancy(jobs: JobHandler, store: Store) -> None:
    """§6: the trailer carries the host: seeding-cap occupancy."""
    resp = jobs.get(id="/limits")
    assert "host: rows" in resp.body
    assert "/500 seeded" in resp.body
    assert "evictable" in resp.body


# ── /delta ──────────────────────────────────────────────────────────


def test_delta_reports_new_alert(jobs: JobHandler, store: Store) -> None:
    fp = f"fp:{uuid4().hex[:8]}"
    aid, is_new = raise_alert(store, source="testsrc", fingerprint=fp, title="cond")
    assert is_new

    resp = jobs.get(id="/delta?since=1")

    assert "NEW ALERTS" in resp.body
    assert f"al{aid} testsrc/{fp}" in resp.body


def test_delta_splits_repeats_and_persistent(jobs: JobHandler, store: Store) -> None:
    tag = uuid4().hex[:8]
    fp_rep = f"fp:rep:{tag}"
    fp_persist = f"fp:persist:{tag}"
    rep_id, _ = raise_alert(store, source="s", fingerprint=fp_rep, title="r")
    pers_id, _ = raise_alert(store, source="s", fingerprint=fp_persist, title="p")

    with store.pool.connection() as conn:
        # rep: created long ago, re-sighted now (updated_at fresh).
        conn.execute(
            "UPDATE refs SET created_at = now() - interval '3 days' WHERE ref_id = %s",
            (rep_id,),
        )
        # persist: open but untouched for days — nothing new this window.
        conn.execute(
            "UPDATE refs SET created_at = now() - interval '3 days', "
            "updated_at = now() - interval '2 days' WHERE ref_id = %s",
            (pers_id,),
        )
        conn.commit()

    resp = jobs.get(id="/delta?since=1")

    new_section = resp.body.split("REPEAT SIGHTINGS")[0]
    assert f"al{rep_id}" not in new_section
    assert f"al{rep_id} s/{fp_rep}" in resp.body.split("REPEAT SIGHTINGS")[1]
    assert f"al{pers_id} s/{fp_persist}" in resp.body.split("PERSISTENT OPEN")[1]


def test_delta_covers_gripes_and_comments(
    jobs: JobHandler, store: Store, gripe: GripeHandler
) -> None:
    tag = uuid4().hex[:8]
    gripe.put(text=f"delta-gripe-{tag}: probe body")
    gr_id = int(gripe.store.list_refs(kind="gripe", limit=1)[0].id)

    gripe.put(id=gr_id, text=f"comment-{tag}")

    resp = jobs.get(id="/delta?since=1")
    assert f"delta-gripe-{tag}" in resp.body
    assert "NEW GRIPES" in resp.body
    assert f"gr{gr_id}" in resp.body.split("GRIPE COMMENTS")[1]


def test_delta_cutoff_from_last_doctor_tick(jobs: JobHandler, store: Store) -> None:
    """With a succeeded doctor_tick job present, the cutoff is its
    started_at — not the 8h fallback."""
    started = datetime.now(UTC) - timedelta(hours=6)
    ref = store.insert_ref(
        kind="job",
        slug=None,
        title=f"doctor_tick test {uuid4().hex[:8]}",
        meta={
            "job_type": "doctor_tick",
            "started_at": started.isoformat(),
        },
    )
    store.add_tag(ref.id, Tag.closed("STATUS", "succeeded"))

    resp = jobs.get(id="/delta")

    assert f"doctor_tick jo{ref.id} start" in resp.body
    assert started.strftime("%Y-%m-%dT%H") in resp.body


def test_delta_since_hours_override(jobs: JobHandler) -> None:
    resp = jobs.get(id="/delta?since=2")
    assert "since=2h" in resp.body


def test_delta_bad_since_raises(jobs: JobHandler) -> None:
    from precis.errors import BadInput

    with pytest.raises(BadInput):
        jobs.get(id="/delta?since=yesterday-ish")


# ── /settings (gr538186) ────────────────────────────────────────────

# Fake values for the redaction tests — one marked constant so the
# secret-gate marker stays rare. Every literal that trips the tree gate
# sits on the line right under the marker (gate: marker exempts its own
# line and the next); the rest are gate-clean fakes.
# fmt: off
_FAKE_SETTINGS: dict[str, str] = {  # secret-gate: allow — fake values for redaction tests
    "dsn": "postgres://user:hunter2@db.internal/precis", "tok_url": "https://node.example/cb?token=tok-probe-123", "key_url": "https://node.example/cb?api_key=key-probe-456", "pw_cfg": "user=ops password=pw-probe-789", "ghs_tok": "ghs_aB3dE5fG7hJ9kL1mN3pQ5rS7tU9vW1x", "asia_key": "ASIAAB3DEF4GH5JK6LM7", "cg_ip_peer": "peer 100.64.0.5.", "cg_ip_word": "host100.64.0.5",
    "json_pw": '{"password": "hunter2"}',
    "yaml_pw": "password: hunter2",
    "pw_kv": "pw=x",
    "tok_kv": "tok=x",
    "basic_auth": "Authorization: Basic dXNlcjpwYXNz",
    "npm_tok": "npm_probe123",
    "sk_live_tok": "sk_live_probe123",
    "ya29_tok": "ya29.probe123",
    "xoxe_tok": "xoxe-probe123",
    "hf_tok": "hf_probe123",
    "userinfo_url": "https://tok123@github.com/x",
    "bare_node": "somenode",
    "lan_node": "gpu1.lan",
    "host_port_kv": "host=somenode port=5432",
    "v6_zone": "fe80::1%eth0",
    "sk_tok": "sk-test-probe-secret",
    "aws_key": "AKIAEXAMPLEFAKE0000",
    "hex32": "0123456789abcdef" * 2,
    "bearer_json": '{"auth":"Bearer abc123def"}',
    "email": "someone@example.org",
    "endpoint_json": '{"endpoint": "thinkpad"}',
    "name_json": '{"name": "alice"}',
    "args_json": '{"args": {"value": "s3cr3t-pass"}}',
    "horse": "correct-horse-battery",
    "alice_mbp": "alice-mbp",
    "v4_lit": "192.0.2.7",
    "good_ts": "2026-10-29T12:30:00Z",
    "not_ts": "not-a-timestamp-marker",
    "good_model": "kimi-k2",
}
# fmt: on

#: ``(fixture field, fragments that must never render)`` — every leak
#: shape the gr538186 review found, seeded under an *allowlisted* key:
#: the typed-value rule must withhold each one.
_SETTINGS_LEAK_CASES: list[tuple[str, list[str]]] = [
    ("json_pw", ["hunter2", "password"]),
    ("yaml_pw", ["hunter2"]),
    ("pw_kv", ["pw=x"]),
    ("tok_kv", ["tok=x"]),
    ("basic_auth", ["dXNlcjpwYXNz", "Basic"]),
    ("ghs_tok", ["ghs_aB3d"]),
    ("npm_tok", ["npm_probe123"]),
    ("sk_live_tok", ["sk_live_probe123"]),
    ("ya29_tok", ["ya29.probe123"]),
    ("xoxe_tok", ["xoxe-probe123"]),
    ("hf_tok", ["hf_probe123"]),
    ("asia_key", ["ASIA" + "AB3DEF4GH5JK6LM7"]),
    ("aws_key", ["AKIAEXAMPLEFAKE0000"]),
    ("sk_tok", ["sk-test-probe-secret"]),
    ("userinfo_url", ["tok123@github.com"]),
    ("dsn", ["hunter2", "db.internal", "postgres:"]),
    ("bare_node", ["somenode"]),
    ("lan_node", ["gpu1.lan"]),
    ("host_port_kv", ["somenode", "5432"]),
    ("cg_ip_peer", ["100.64" + ".0.5"]),
    ("cg_ip_word", ["100.64" + ".0.5"]),
    ("v6_zone", ["fe80"]),
    ("hex32", ["0123456789abcdef"]),
    ("bearer_json", ["abc123def"]),
    ("email", ["someone@example.org"]),
    ("endpoint_json", ["thinkpad", "endpoint"]),
    ("name_json", ["alice"]),
    ("args_json", ["s3cr3t-pass", "args"]),
    ("horse", ["correct-horse-battery"]),
    ("v4_lit", ["192.0.2.7"]),
]


def _seed_setting(store: Store, key: str, value: str) -> None:
    with store.pool.connection() as conn:
        conn.execute(
            "INSERT INTO app_settings (key, value, updated_at) "
            "VALUES (%s, %s, now()) "
            "ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value",
            (key, value),
        )
        conn.commit()


def _drop_setting(store: Store, key: str) -> None:
    with store.pool.connection() as conn:
        conn.execute("DELETE FROM app_settings WHERE key = %s", (key,))
        conn.commit()


def _settings_body_for(jobs: JobHandler, store: Store, key: str, value: str) -> str:
    _seed_setting(store, key, value)
    try:
        return jobs.get(id="/settings").body
    finally:
        _drop_setting(store, key)


def test_settings_renders_typed_values(jobs: JobHandler, store: Store) -> None:
    """Positives: a bool, int, float, ISO timestamp, duration, a
    per-key enum member and a ``*.model`` name all render."""
    tag = uuid4().hex[:8]
    cases = {
        f"dream.{tag}.b": ("true", "true"),
        f"dream.{tag}.i": ("42", "42"),
        f"dream.{tag}.f": ("0.75", "0.75"),
        f"dream.{tag}.t": (_FAKE_SETTINGS["good_ts"], "2026-10-29T12:30:00"),
        f"dream.{tag}.d": ("15m", "15m"),
        f"dream.{tag}.mode": ("shadow", "shadow"),
        "schema.gate": ("warn", "warn"),
        f"dream.{tag}.tier": ("big", "big"),
        f"dream.{tag}.model": (_FAKE_SETTINGS["good_model"], "kimi-k2"),
    }
    for k, (v, _want) in cases.items():
        _seed_setting(store, k, v)
    try:
        body = jobs.get(id="/settings").body
        assert "## app_settings" in body
        for _k, (_v, want) in cases.items():
            assert want in body
    finally:
        for k in cases:
            _drop_setting(store, k)


def test_settings_normalizes_key_names(jobs: JobHandler, store: Store) -> None:
    """Only allowlist-pattern segments print; an operator-chosen
    segment — hostname or job id — collapses to ``<name>``."""
    tag = uuid4().hex[:8]
    keys = {
        "budget.hourly_usd": "5.0",  # every segment known — shows whole
        f"dream.gpu{tag[:4]}.last_run": _FAKE_SETTINGS["good_ts"],
        f"dream.{tag}": "probe-value-42",
    }
    for k, v in keys.items():
        _seed_setting(store, k, v)
    try:
        body = jobs.get(id="/settings").body
        assert "budget.hourly_usd" in body
        assert "dream.<name>.last_run" in body
        assert "dream.<name>" in body
        # …but the operator-chosen segments never print.
        assert tag not in body
        assert f"gpu{tag[:4]}" not in body
    finally:
        for k in keys:
            _drop_setting(store, k)


@pytest.mark.parametrize(
    ("field", "fragments"),
    _SETTINGS_LEAK_CASES,
    ids=[field for field, _ in _SETTINGS_LEAK_CASES],
)
def test_settings_withholds_leak_shapes(
    jobs: JobHandler, store: Store, field: str, fragments: list[str]
) -> None:
    """Every review-found leak shape withholds under an allowlisted
    key — DSNs, name=value secrets, token prefixes, hostnames, IPs."""
    key = f"dream.{uuid4().hex[:8]}"
    body = _settings_body_for(jobs, store, key, _FAKE_SETTINGS[field])
    assert "(withheld" in body
    for frag in fragments:
        assert frag not in body


def test_settings_withholds_secret_named_keys(jobs: JobHandler, store: Store) -> None:
    """A secret-shaped key name withholds its value even allowlisted —
    ``pwd``/``passphrase``/``dsn``/``url``/``api_key`` pieces."""
    tag = uuid4().hex[:8]
    keys = [
        "dream.pwd",
        f"llm.op.{tag}.passphrase",
        f"llm.op.{tag}.dsn",
        f"llm.op.{tag}.api_key",
        f"contact.{tag}.tokens",
        f"contact.{tag}.credentials",
    ]
    for k in keys:
        _seed_setting(store, k, f"value-for-{k}")
    try:
        body = jobs.get(id="/settings").body
        for k in keys:
            assert f"value-for-{k}" not in body
    finally:
        for k in keys:
            _drop_setting(store, k)


def test_settings_counts_denied_keys(jobs: JobHandler, store: Store) -> None:
    """Non-allowlisted keys are a count, not a row — the key name
    itself never prints (``deploy.fleet`` included)."""
    tag = uuid4().hex[:8]
    keys = [
        f"test.{tag}.internal",
        "deploy.fleet",
        "contact.email",
        f"release.{tag}.marker",
    ]
    for k in keys:
        _seed_setting(store, k, "opaque-marker-7")
    try:
        body = jobs.get(id="/settings").body
        assert "withheld)" in body
        for k in keys:
            assert k not in body
        assert "deploy.fleet" not in body
        assert "contact.email" not in body
        assert "opaque-marker-7" not in body
    finally:
        for k in keys:
            _drop_setting(store, k)


def test_settings_last_run_requires_timestamp(jobs: JobHandler, store: Store) -> None:
    tag = uuid4().hex[:8]
    good, bad = f"dream.{tag}.last_run", f"dream.{tag}x.last_run"
    _seed_setting(store, good, _FAKE_SETTINGS["good_ts"])
    _seed_setting(store, bad, _FAKE_SETTINGS["not_ts"])
    try:
        body = jobs.get(id="/settings").body
        assert "2026-10-29T12:30:00" in body
        assert "not-a-timestamp-marker" not in body
    finally:
        _drop_setting(store, good)
        _drop_setting(store, bad)


def test_settings_json_walk_allowlists_keys(jobs: JobHandler, store: Store) -> None:
    """An ``llm.op.*`` JSON override renders its known fields; a denied
    object key withholds name and value."""
    key = f"llm.op.{uuid4().hex[:8]}"
    value = (
        '{"tier": "big", "model": "kimi-k2", '
        '"endpoint": "http://somenode:11434", '
        '"api_key": "sk-test-probe-secret"}'
    )
    body = _settings_body_for(jobs, store, key, value)
    assert '"tier": "big"' in body
    assert '"model": "kimi-k2"' in body
    assert "api_key" not in body
    assert "endpoint" not in body
    assert "sk-test-probe-secret" not in body
    assert "somenode" not in body


def test_settings_collapses_per_day_keys(jobs: JobHandler, store: Store) -> None:
    """100 per-day marker keys collapse to one stem line — the view
    stays well under the 4 KB card cap."""
    tag = uuid4().hex[:8]
    keys = [f"dream.cw{tag}:minted:{2100 + i}-01-01" for i in range(100)]
    for i, k in enumerate(keys):
        _seed_setting(store, k, str(i))
    try:
        body = jobs.get(id="/settings").body
        assert "100 per-day keys — newest 2199-01-01" in body
        assert "dream.<name>:<name>*" in body
        for k in keys:
            assert k not in body
        assert len(body.encode("utf-8")) < 4096
    finally:
        for k in keys:
            _drop_setting(store, k)


def test_settings_service_config_typed_cells(jobs: JobHandler, store: Store) -> None:
    """Service names print only from the registry enum; actor and host
    material never render — a node hostname in the host column or an
    operator laptop name in the actor cell."""
    from precis.workers.registry import SERVICES
    from precis.workers.service_config import (
        clear_service_config,
        set_service_concurrency,
        set_service_prio,
    )

    tag = uuid4().hex[:8]
    host = f"testnode-{tag}"
    real_service = SERVICES[0].name
    fake_service = f"test_svc_{tag}"
    set_service_prio(store, host, real_service, 3, actor="alice-mbp")
    set_service_concurrency(store, "*", fake_service, 4, actor="ops@somenode")
    try:
        body = jobs.get(id="/settings").body
        assert "## service_config" in body
        assert real_service in body  # registered name is the service enum
        assert fake_service not in body  # unknown word: no enum → withheld
        assert host not in body  # node hostname masked
        assert "(masked)" in body
        assert "alice-mbp" not in body  # actor: no enum → withheld
        assert "somenode" not in body
        assert "prio" in body and "conc" in body
    finally:
        clear_service_config(store, host, real_service)
        clear_service_config(store, "*", fake_service)


def test_settings_view_is_safe_for_any_agent(jobs: JobHandler, store: Store) -> None:
    """The view is ungated on purpose — there is no privileged read.
    A plain ``get`` (the only caller shape the API offers) over rows
    stuffed with operator material returns only typed cells, ``<name>``
    segments and the withheld count."""
    tag = uuid4().hex[:8]
    keys = {
        f"dream.{tag}": _FAKE_SETTINGS["dsn"],
        f"test.{tag}": _FAKE_SETTINGS["pw_cfg"],
        f"llm.op.{tag}.api_key": _FAKE_SETTINGS["sk_tok"],
    }
    for k, v in keys.items():
        _seed_setting(store, k, v)
    try:
        body = jobs.get(id="/settings").body
        for v in keys.values():
            assert v not in body
        for frag in ("hunter2", "db.internal", "sk-test-probe-secret"):
            assert frag not in body
        assert "withheld" in body
    finally:
        for k in keys:
            _drop_setting(store, k)


def test_settings_empty_tables_render_cleanly(jobs: JobHandler) -> None:
    resp = jobs.get(id="/settings")
    assert resp.body.startswith("# settings")
    assert "## app_settings" in resp.body
    assert "## service_config" in resp.body
