"""Scenario + service environment — the production context.

A single named scenario sets objective weights and quantity instead of a
caller hand-tuning weights per run (multiscale-design-system-spec.md §1.3),
and points at the service environment the design must survive. The
environment's **lifetime master switch** is the highest-leverage field in
the system: a week-long prototype drops corrosion, creep and fatigue
entirely; ten years outdoors makes them dominant. Same design, different
physics — which is why the scenario is recorded on every verdict.

Three presets are seeded by migration ``0162_design_core.sql``:
``prototype``, ``small_batch``, ``mass_production``
(:data:`PRESET_SCENARIOS`). They are ordinary rows, not constants — a
caller may add their own with ``status='proposed'``.

**Load cases apply by default.** The standard library (shock, vibration,
off-axis) is not a list a design opts into; it applies to every design, and
coming off it needs an exemption row carrying a reason
(:func:`exempt_load_case`). That asymmetry is the whole point: a design
that only works statically gets caught rather than quietly never being
asked.

Vocabulary: a **scenario** is this — the production context. A *situation*
is a different thing (a named swept-volume bundle with a three-verdict rule
table, build-order step 3); see docs/glossary.md.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from psycopg import Connection
from psycopg.rows import dict_row

from precis.design._db import read_conn, write_conn

#: The seeded presets, in ascending quantity order.
PRESET_SCENARIOS: tuple[str, ...] = ("prototype", "small_batch", "mass_production")

_ENV_COLS = (
    "env_id, name, lifetime_checks, expected_lifetime_s, temp_min_k, temp_max_k, "
    "pressure_pa, humidity_pct, vibration_grms, vibration_spectrum, cycle_count, "
    "duty_cycle, chemical_exposure, status, description"
)
_SCENARIO_COLS = (
    "scenario_id, name, quantity, objective_weights, service_env_id, status, "
    "description"
)
_LOAD_CASE_COLS = "case_id, name, family, standard, spec, description"


@dataclass(frozen=True)
class ServiceEnvironment:
    """What a design must survive. Every field is optional except the
    identity and the master switch — absence means *unstated*, never zero
    (the same honesty rule the se contract tier follows)."""

    env_id: str
    name: str
    #: THE master switch: does the lifetime physics family (corrosion,
    #: creep, fatigue) run at all? ``expected_lifetime_s`` then scales it.
    lifetime_checks: bool = True
    expected_lifetime_s: float | None = None
    temp_min_k: float | None = None
    temp_max_k: float | None = None
    pressure_pa: float | None = None
    humidity_pct: float | None = None
    vibration_grms: float | None = None
    vibration_spectrum: dict[str, Any] | None = None
    cycle_count: int | None = None
    duty_cycle: float | None = None
    chemical_exposure: tuple[str, ...] = ()
    status: str = "proposed"
    description: str | None = None


@dataclass(frozen=True)
class LoadCase:
    """One case in the load-case library. ``standard`` rows apply to every
    design by default."""

    case_id: str
    name: str
    family: str
    standard: bool = False
    spec: dict[str, Any] = field(default_factory=dict)
    description: str | None = None


@dataclass(frozen=True)
class Scenario:
    """The production context governing one design."""

    scenario_id: str
    name: str
    quantity: int | None = None
    objective_weights: dict[str, float] = field(default_factory=dict)
    service_environment: ServiceEnvironment | None = None
    status: str = "proposed"
    description: str | None = None

    @property
    def lifetime_checks(self) -> bool:
        """The master switch, read through the scenario. Absent a service
        environment nothing is known about service life, so the honest
        answer is "don't claim lifetime physics ran"."""
        return bool(
            self.service_environment and self.service_environment.lifetime_checks
        )


def _env_from_row(row: dict[str, Any]) -> ServiceEnvironment:
    return ServiceEnvironment(
        env_id=row["env_id"],
        name=row["name"],
        lifetime_checks=bool(row["lifetime_checks"]),
        expected_lifetime_s=row["expected_lifetime_s"],
        temp_min_k=row["temp_min_k"],
        temp_max_k=row["temp_max_k"],
        pressure_pa=row["pressure_pa"],
        humidity_pct=row["humidity_pct"],
        vibration_grms=row["vibration_grms"],
        vibration_spectrum=(
            dict(row["vibration_spectrum"])
            if row["vibration_spectrum"] is not None
            else None
        ),
        cycle_count=row["cycle_count"],
        duty_cycle=row["duty_cycle"],
        chemical_exposure=tuple(row["chemical_exposure"] or ()),
        status=row["status"],
        description=row["description"],
    )


def _scenario_from_row(row: dict[str, Any], env: ServiceEnvironment | None) -> Scenario:
    return Scenario(
        scenario_id=row["scenario_id"],
        name=row["name"],
        quantity=row["quantity"],
        objective_weights=dict(row["objective_weights"] or {}),
        service_environment=env,
        status=row["status"],
        description=row["description"],
    )


def get_scenario(
    store: Any, scenario_id: str, *, conn: Connection | None = None
) -> Scenario | None:
    """One scenario with its service environment attached, or None."""
    with read_conn(store, conn) as c:
        with c.cursor(row_factory=dict_row) as cur:
            cur.execute(
                f"SELECT {_SCENARIO_COLS} FROM design_scenarios WHERE scenario_id = %s",
                (scenario_id,),
            )
            row = cur.fetchone()
            if row is None:
                return None
            return _scenario_from_row(row, _fetch_env(cur, row["service_env_id"]))


def list_scenarios(store: Any, *, conn: Connection | None = None) -> list[Scenario]:
    """Every scenario, presets first (``core`` tier), then by id."""
    with read_conn(store, conn) as c:
        with c.cursor(row_factory=dict_row) as cur:
            cur.execute(
                f"SELECT {_SCENARIO_COLS} FROM design_scenarios "
                "ORDER BY (status = 'core') DESC, quantity ASC NULLS LAST, "
                "scenario_id ASC"
            )
            rows = cur.fetchall()
            return [
                _scenario_from_row(r, _fetch_env(cur, r["service_env_id"]))
                for r in rows
            ]


def _fetch_env(cur: Any, env_id: str | None) -> ServiceEnvironment | None:
    """The environment for one scenario row. Deliberately a second query
    per scenario rather than a join: the table is tiny (three seeded rows
    plus whatever a caller mints), and keeping the row→dataclass mappers
    one-per-table is worth more here than one round trip."""
    if env_id is None:
        return None
    cur.execute(
        f"SELECT {_ENV_COLS} FROM design_service_environments WHERE env_id = %s",
        (env_id,),
    )
    row = cur.fetchone()
    return _env_from_row(row) if row is not None else None


def set_design_scenario(
    store: Any,
    ref_id: int,
    scenario_id: str,
    *,
    set_by: str | None = None,
    conn: Connection | None = None,
) -> None:
    """Record which scenario governs a design (one per design — a second
    call replaces the first)."""
    with write_conn(store, conn) as c:
        c.execute(
            "INSERT INTO design_scenario_link (ref_id, scenario_id, set_by) "
            "VALUES (%s, %s, %s) "
            "ON CONFLICT (ref_id) DO UPDATE SET "
            "scenario_id = EXCLUDED.scenario_id, set_by = EXCLUDED.set_by, "
            "set_at = now()",
            (ref_id, scenario_id, set_by),
        )


def design_scenario(
    store: Any, ref_id: int, *, conn: Connection | None = None
) -> Scenario | None:
    """The scenario governing a design, or None when it has not chosen one.

    None is a real answer, not an error: a design sketched before anyone
    decided how many to build has no scenario, and validate/DRC output says
    so rather than pretending a default.
    """
    with read_conn(store, conn) as c:
        row = c.execute(
            "SELECT scenario_id FROM design_scenario_link WHERE ref_id = %s",
            (ref_id,),
        ).fetchone()
    if row is None:
        return None
    return get_scenario(store, str(row[0]), conn=conn)


def standard_load_cases(
    store: Any, *, conn: Connection | None = None
) -> list[LoadCase]:
    """The standard library — the cases that apply to every design unless
    exempted."""
    with read_conn(store, conn) as c:
        with c.cursor(row_factory=dict_row) as cur:
            cur.execute(
                f"SELECT {_LOAD_CASE_COLS} FROM design_load_cases "
                "WHERE standard ORDER BY case_id ASC"
            )
            return [_load_case_from_row(r) for r in cur.fetchall()]


def _load_case_from_row(row: dict[str, Any]) -> LoadCase:
    return LoadCase(
        case_id=row["case_id"],
        name=row["name"],
        family=row["family"],
        standard=bool(row["standard"]),
        spec=dict(row["spec"] or {}),
        description=row["description"],
    )


def load_cases_for(
    store: Any,
    ref_id: int,
    *,
    scenario_id: str | None = None,
    conn: Connection | None = None,
) -> list[LoadCase]:
    """Every load case that actually applies to a design: the standard
    library, plus any extras its scenario pulls in, minus the ones it has
    explicitly exempted.

    ``scenario_id`` overrides the stored choice — that is how "what would
    this design owe under ``mass_production``?" is asked without writing
    anything.
    """
    if scenario_id is None:
        with read_conn(store, conn) as c:
            row = c.execute(
                "SELECT scenario_id FROM design_scenario_link WHERE ref_id = %s",
                (ref_id,),
            ).fetchone()
        scenario_id = str(row[0]) if row is not None else None
    with read_conn(store, conn) as c:
        with c.cursor(row_factory=dict_row) as cur:
            cur.execute(
                f"SELECT {_LOAD_CASE_COLS} FROM design_load_cases "
                "WHERE standard OR case_id IN ("
                "  SELECT case_id FROM design_scenario_load_cases "
                "  WHERE scenario_id = %s) "
                "ORDER BY case_id ASC",
                (scenario_id,),
            )
            cases = [_load_case_from_row(r) for r in cur.fetchall()]
    exempt = set(exemptions(store, ref_id, conn=conn))
    return [c for c in cases if c.case_id not in exempt]


def exempt_load_case(
    store: Any,
    ref_id: int,
    case_id: str,
    reason: str,
    *,
    set_by: str | None = None,
    conn: Connection | None = None,
) -> None:
    """Exempt a design from one load case. ``reason`` is required and
    non-empty — an exemption nobody justified is the failure mode the
    default-on library exists to prevent."""
    text = (reason or "").strip()
    if not text:
        raise ValueError(
            f"exempting load case {case_id!r} needs a reason — "
            "the standard library applies by default for a reason"
        )
    with write_conn(store, conn) as c:
        c.execute(
            "INSERT INTO design_load_case_exemptions "
            "(ref_id, case_id, reason, set_by) VALUES (%s, %s, %s, %s) "
            "ON CONFLICT (ref_id, case_id) DO UPDATE SET "
            "reason = EXCLUDED.reason, set_by = EXCLUDED.set_by",
            (ref_id, case_id, text, set_by),
        )


def unexempt_load_case(
    store: Any, ref_id: int, case_id: str, *, conn: Connection | None = None
) -> bool:
    """Drop an exemption, putting the case back in force. True when a row
    was removed."""
    with write_conn(store, conn) as c:
        rows = c.execute(
            "DELETE FROM design_load_case_exemptions "
            "WHERE ref_id = %s AND case_id = %s RETURNING id",
            (ref_id, case_id),
        ).fetchall()
    return bool(rows)


def exemptions(
    store: Any, ref_id: int, *, conn: Connection | None = None
) -> dict[str, str]:
    """``{case_id: reason}`` for a design's exemptions — the audit view."""
    with read_conn(store, conn) as c:
        rows = c.execute(
            "SELECT case_id, reason FROM design_load_case_exemptions "
            "WHERE ref_id = %s ORDER BY case_id ASC",
            (ref_id,),
        ).fetchall()
    return {str(r[0]): str(r[1]) for r in rows}
