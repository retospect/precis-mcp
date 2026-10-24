"""The morning briefing's ``## Weather`` section (gr507718): spoken METAR/TAF
via avwx, FIR-filtered SIGMET flags, keyword-selected Met Éireann marine
warnings — all deterministic and best-effort."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Any

import pytest

from precis.store.types import ChunkInsert
from precis.workers import briefing, weather

METAR_RAW = (
    "METAR EIDW 231630Z 28016KT 9999 FEW008 SCT025 08/03 Q1009 NOSIG\n"
    "METAR EINN 231630Z 29014KT 9999 -SHRA FEW020CB SCT028 10/06 Q1013 NOSIG\n"
)
TAF_RAW = (
    "TAF EIDW 231100Z 2312/2412 27014KT 9999 SCT020 BKN030\n"
    "TAF EINN 231100Z 2312/2412 29015KT 9999 FEW018 SCT030 \n"
    "  TEMPO 2312/2318 28018G28KT \n"
    "  TEMPO 2312/2319 -SHRA FEW018CB \n"
    "  PROB30 \n"
    "  TEMPO 2316/2319 4000 SHRA\n"
)
ISIGMET_JSON = json.dumps(
    [
        {
            "icaoId": "EISN",
            "firId": "EISN",
            "firName": "SHANNON",
            "seriesId": "2",
            "hazard": "TURB",
            "qualifier": "SEV TURB",
            "base": 5000,
            "top": 12000,
            "dir": "SW",
            "spd": "15",
            "validTimeTo": 1792778400,
        },
        {
            "icaoId": "MHTG",
            "firId": "MHTG",
            "firName": "MHCC CENTRAL AMERICAN",
            "seriesId": "3",
            "hazard": "VA",
            "qualifier": "FUEGO",
            "base": 0,
            "top": 14000,
            "dir": "SW",
            "spd": "5",
            "validTimeTo": 1792778400,
        },
    ]
)
MARINE_JSON = json.dumps(
    [
        {
            "id": 1,
            "type": "small-craft",
            "severity": "Moderate",
            "level": "yellow",
            "headline": "Small Craft warning for all coasts of Ireland",
            "description": "<p>East to northeast winds will reach force 6 at times.</p>",
            "status": "Warning",
            "regions": ["EI805"],
            "expiry": "2026-10-24T22:00:00+01:00",
        },
        {
            "id": 2,
            "type": "Gale",
            "severity": "Moderate",
            "level": "yellow",
            "headline": "Status Yellow - Marine Gale warning on the Irish Sea",
            "description": "<p>Winds will reach gale force 8 at times.</p>",
            "status": "Warning",
            "regions": ["EI806"],
            "expiry": "2026-10-24T19:00:00+01:00",
        },
        {
            "id": 3,
            "type": "Wind",
            "severity": "Moderate",
            "level": "yellow",
            "headline": "Status Yellow - Wind warning for Leinster",
            "description": "<p>Strong gusts inland.</p>",
            "status": "Warning",
            "regions": ["EI12"],
            "expiry": "2026-10-24T19:00:00+01:00",
        },
        {
            "id": 4,
            "type": "Gale",
            "severity": "Moderate",
            "level": "yellow",
            "headline": "Status Yellow - Marine Gale warning, lapsed",
            "description": "<p>Already expired.</p>",
            "status": "Warning",
            "regions": ["EI806"],
            "expiry": "2026-10-20T19:00:00+01:00",
        },
    ]
)

NOW = datetime(2026, 10, 23, 12, 0, tzinfo=UTC)


def _fetch(*, fail: set[str] | None = None) -> weather.Fetch:
    bodies = {
        weather.METAR_URL: METAR_RAW,
        weather.TAF_URL: TAF_RAW,
        weather.ISIGMET_URL: ISIGMET_JSON,
        weather.MARINE_WARNINGS_URL: MARINE_JSON,
    }

    def fetch(url: str, params: dict[str, str]) -> str:
        if fail and url in fail:
            raise RuntimeError("feed down")
        return bodies[url]

    return fetch


def _section(**kw: Any) -> str:
    kw.setdefault("stations", "EINN,EIDW")
    kw.setdefault("firs", "EISN")
    kw.setdefault("marine_keywords", "gale,small craft")
    kw.setdefault("fetch", _fetch())
    kw.setdefault("now", NOW)
    return weather.weather_section(**kw)


def test_section_renders_spoken_metar_and_taf() -> None:
    section = _section()
    assert section.startswith("## Weather")
    assert "Shannon" in section and "Dublin" in section
    # Spoken, not telegram: digits and codes are said out loud.
    assert "knots" in section and "zulu" in section.lower()
    assert "Cumulonimbus" in section
    assert "29014KT" not in section and "OVC" not in section
    assert "Forecast:" in section


def test_sigmets_are_filtered_to_configured_firs() -> None:
    section = _section()
    assert "SIGMETs for EISN" in section
    assert "severe turbulence" in section
    assert "southwest at 15 knots" in section
    # MHTG (Central American FIR) is not in the configured list.
    assert "FUEGO" not in section and "MHTG" not in section


def test_no_sigmets_says_so() -> None:
    section = _section(firs="EISN", fetch=_fetch())
    assert "SIGMET" in section
    empty = weather.weather_section(
        stations="EINN",
        firs="EISN",
        fetch=lambda url, params: (
            "[]" if url == weather.ISIGMET_URL else _fetch()(url, params)
        ),
        now=NOW,
    )
    assert "No active SIGMETs for EISN" in empty


def test_marine_keyword_rule_selects_and_expires() -> None:
    section = _section()
    # small-craft's hyphen still matches the "small craft" keyword.
    assert "Small Craft warning" in section
    assert "Marine Gale warning on the Irish Sea" in section
    # A land Wind warning doesn't match the marine keywords.
    assert "Wind warning for Leinster" not in section
    # An expired warning is dropped even though it matches a keyword.
    assert "lapsed" not in section
    # HTML markup is stripped; the expiry is spoken in UTC.
    assert "<p>" not in section and "UTC" in section


def test_notam_missing_key_notes_it(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("PRECIS_ICAO_API_KEY", raising=False)
    section = _section(notam_keywords="aerodrome closed")
    assert "NOTAM: no key configured" in section


def test_notam_keyword_match_flags_unusual(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("PRECIS_ICAO_API_KEY", "k")
    notams = json.dumps(
        [
            {"location": "EINN", "id": "A1", "body": "AERODROME CLOSED DUE TO WORKS"},
            {"location": "EIDW", "id": "B2", "body": "TWY A LIGHTS U/S"},
        ]
    )

    def fetch(url: str, params: dict[str, str]) -> str:
        if url == weather.ICAO_NOTAMS_URL:
            return notams
        return _fetch()(url, params)

    section = _section(notam_keywords="aerodrome closed", fetch=fetch)
    assert "NOTAMs (EINN, EIDW): 2 active, 1 unusual" in section
    assert "AERODROME CLOSED" in section
    assert "TWY A LIGHTS" not in section


def test_feed_failure_degrades_not_fails() -> None:
    section = _section(fetch=_fetch(fail={weather.MARINE_WARNINGS_URL}))
    assert "Marine warnings unavailable" in section
    assert "knots" in section  # the aviation block still rendered


def test_empty_config_produces_no_section() -> None:
    assert (
        weather.weather_section(stations="", marine_keywords="", fetch=_fetch()) == ""
    )


def test_run_briefing_appends_weather_section(store: Any) -> None:
    ref = store.insert_ref(kind="news", slug="n-test-1", title="overnight", meta={})
    store.chunks.insert_chunks(
        ref.id, [ChunkInsert(ord=0, text="headline body", meta={})]
    )

    sent: list[Any] = []

    class _LLM:
        def complete(self, messages: Any) -> Any:
            sent.append(messages)
            return type("R", (), {"text": "the digest", "model": "m"})()

    captured: list[str] = []
    r = briefing.run_briefing(
        store,
        client=_LLM(),
        sink=captured.append,
        weather_fn=lambda: "## Weather\n\nspoken test",
    )
    assert r["articles"] == 1
    assert captured and captured[0].endswith("## Weather\n\nspoken test")
    # The deterministic section must stay out of the LLM input.
    assert "## Weather" not in json.dumps(sent)


def test_run_briefing_survives_weather_failure(store: Any) -> None:
    ref = store.insert_ref(kind="news", slug="n-test-2", title="overnight", meta={})
    store.chunks.insert_chunks(
        ref.id, [ChunkInsert(ord=0, text="headline body", meta={})]
    )

    class _LLM:
        def complete(self, messages: Any) -> Any:
            return type("R", (), {"text": "the digest", "model": "m"})()

    def _boom() -> str:
        raise RuntimeError("weather blew up")

    captured: list[str] = []
    r = briefing.run_briefing(
        store, client=_LLM(), sink=captured.append, weather_fn=_boom
    )
    assert r["articles"] == 1 and captured == ["the digest"]
