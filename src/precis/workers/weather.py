"""weather — the morning briefing's spoken aviation + marine section.

gr507718 (Reto 2026-10-23, approved scope: decoded aviation weather plus
Met Éireann marine warnings, keyword-selected; NOTAM dropped — no free
feed). The news digest itself is an LLM pass, but METAR/TAF are
*deterministic* telegrams, and the same brief text is what the morning
cast narrates — letting the model paraphrase them would garble both the
precision and the pronunciation. So this section is assembled
deterministically, after the LLM brief, via avwx-engine's speech renderer:
``29014KT`` reads as "winds two niner zero at 14 knots", ``Z`` as "zulu",
``OVC`` as "overcast". avwx does the parsing and the spoken form; precis
only fetches and assembles — no hand-rolled decoder.

Feeds (all free, no key):

- METAR/TAF: the aviationweather.gov data API, ``format=raw`` → avwx.
- International SIGMETs: same API, ``format=json``, filtered client-side
  by FIR id (the upstream ``loc`` param is ignored — it answers the whole
  globe). These are the aviation "unusual" flag: a SIGMET exists only when
  hazardous conditions warrant one. AIRMETs are a US-only product, so they
  are not fetched for non-US FIRs.
- Marine: Met Éireann's CAP warnings JSON; a keyword rule on type +
  headline picks the marine warnings (defaults ``gale,small craft`` — land
  wind/rain warnings don't match).

All fetches go through ``polite_safe_get`` (SSRF-pinned, politeness
backoff). Every block degrades independently — a down feed becomes a
one-line "unavailable" note, never a failed brief. Station lists and
keywords are operator config (``PRECIS_BRIEFING_*``), not constants.
"""

from __future__ import annotations

import html as _html
import logging
import re
from collections.abc import Callable, Sequence
from datetime import UTC, datetime
from typing import Any, cast

log = logging.getLogger(__name__)

METAR_URL = "https://aviationweather.gov/api/data/metar"
TAF_URL = "https://aviationweather.gov/api/data/taf"
ISIGMET_URL = "https://aviationweather.gov/api/data/isigmet"
MARINE_WARNINGS_URL = "https://www.met.ie/Open_Data/json/warning_ALL.json"
ICAO_NOTAMS_URL = "https://applications.icao.int/dataservices/api/notams-realtime-list"
ICAO_API_KEY_SECRET = "PRECIS_ICAO_API_KEY"

_FETCH_TIMEOUT_S = 20.0

#: ``(url, query params) -> body text``; injected in tests.
Fetch = Callable[[str, dict[str, str]], str]


def _default_fetch(url: str, params: dict[str, str]) -> str:
    from precis.utils.http import http_client, polite_safe_get, polite_user_agent

    with http_client(
        timeout=_FETCH_TIMEOUT_S, user_agent=polite_user_agent()
    ) as client:
        resp = polite_safe_get(client, url, params=params)
    resp.raise_for_status()
    return resp.text


def _csv(value: str | Sequence[str] | None) -> list[str]:
    """A comma-separated config string (or list) → clean tokens."""
    if value is None:
        return []
    if isinstance(value, str):
        value = value.split(",")
    return [v.strip() for v in value if v and v.strip()]


# ── aviation: METAR + TAF → spoken text via avwx ──────────────────────


def _split_metars(text: str) -> list[str]:
    """``format=raw`` METAR output — one telegram per line."""
    return [ln.strip() for ln in text.splitlines() if ln.strip()]


def _split_tafs(text: str) -> list[str]:
    """``format=raw`` TAF output — a telegram wraps over several lines;
    a new report starts on a ``TAF `` (or ``TAF AMD``/``TAF COR``) line."""
    reports: list[str] = []
    for ln in text.splitlines():
        if ln.lstrip().startswith("TAF") or not reports:
            reports.append(ln.strip())
        else:
            reports[-1] += " " + ln.strip()
    return [r for r in reports if r]


def _station_of(parsed: Any, report: str) -> str:
    """The report's ICAO code — from avwx's station when resolved, else the
    first 4-letter token after the leading METAR/TAF keyword."""
    icao = getattr(getattr(parsed, "station", None), "icao", None)
    if isinstance(icao, str) and icao:
        return icao
    for tok in report.split()[1:]:
        if re.fullmatch(r"[A-Z]{4}", tok):
            return tok
    return "?"


def _spoken_reports(reports: list[str], *, kind: str) -> dict[str, tuple[str, str]]:
    """Parse each telegram with avwx → ``{ICAO: (name, spoken)}`` keyed by
    station code so a METAR pairs with its TAF. Unparseable reports are
    dropped with a log — raw telegram text must not reach the narrator."""
    from avwx import Metar, Taf
    from avwx.parsing import speech
    from avwx.structs import MetarData, TafData

    cls = Metar if kind == "metar" else Taf
    out: dict[str, tuple[str, str]] = {}
    for report in reports:
        try:
            parsed = cls.from_report(report)
            if parsed is None or parsed.data is None or parsed.units is None:
                raise ValueError("no parsed data")
            # ``cls`` already pins the data type; the union is avwx's.
            spoken = (
                speech.metar(cast("MetarData", parsed.data), parsed.units)
                if kind == "metar"
                else speech.taf(cast("TafData", parsed.data), parsed.units)
            )
            if not spoken:
                raise ValueError("empty spoken render")
            code = _station_of(parsed, report)
            name = getattr(getattr(parsed, "station", None), "name", None) or code
            out[code] = (f"{name} ({code})" if name != code else code, spoken)
        except Exception:
            log.warning("weather: %s parse failed: %r", kind, report[:80])
    return out


def _aviation_block(
    fetch: Fetch, stations: list[str], firs: list[str], notam_keywords: list[str]
) -> list[str]:
    """One line per station: spoken METAR + spoken TAF. Then the SIGMET
    flag lines for the configured FIRs and the NOTAM lines."""
    lines: list[str] = []
    metars = _spoken_reports(
        _split_metars(fetch(METAR_URL, {"ids": ",".join(stations), "format": "raw"})),
        kind="metar",
    )
    tafs = _spoken_reports(
        _split_tafs(fetch(TAF_URL, {"ids": ",".join(stations), "format": "raw"})),
        kind="taf",
    )
    for station in (s.upper() for s in stations):
        label, metar = metars.get(station, (station, ""))
        _, taf = tafs.get(station, ("", ""))
        parts = [p for p in (metar, f"Forecast: {taf}" if taf else "") if p]
        if parts:
            lines.append(f"**{label}:** {'. '.join(parts)}")
    if firs:
        lines.extend(_sigmet_lines(fetch, firs))
    lines.extend(_notam_lines(fetch, stations, notam_keywords))
    return lines


# ── NOTAMs — ICAO API Data Service, key from the secrets store ────────


def _notam_text(item: dict[str, Any]) -> str:
    """Best text field of a realtime-NOTAM record — the feed's field names
    drift between series, so take the longest string value."""
    return max(
        (str(v) for v in item.values() if isinstance(v, str) and len(v) > 10),
        key=len,
        default="",
    )


def _notam_lines(fetch: Fetch, stations: list[str], keywords: list[str]) -> list[str]:
    """NOTAM lines for the configured stations. Missing key → the
    'no key configured' note; fetch failure propagates to the caller's
    degrade note. Each NOTAM whose text matches a keyword is listed;
    the rest collapse to a count."""
    import json

    from precis import secrets

    key = (secrets.get_secret(ICAO_API_KEY_SECRET) or "").strip()
    if not key:
        return ["NOTAM: no key configured."]
    raw = json.loads(
        fetch(
            ICAO_NOTAMS_URL,
            {
                "api_key": key,
                "format": "json",
                "locations": ",".join(s.upper() for s in stations),
            },
        )
    )
    items = (
        raw
        if isinstance(raw, list)
        else raw.get("notams", [])
        if isinstance(raw, dict)
        else []
    )
    keys = [_norm(k) for k in keywords]
    flagged: list[str] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        text = _notam_text(item)
        if not text:
            continue
        loc = str(item.get("location") or item.get("icaoId") or "").strip()
        ident = str(
            item.get("id") or item.get("notamID") or item.get("number") or ""
        ).strip()
        label = " ".join(x for x in (loc, ident) if x) or "NOTAM"
        if any(k in _norm(text) for k in keys):
            flagged.append(f"- {label}: {_WS_RE.sub(' ', text).strip()[:200]}")
    head = f"NOTAMs ({', '.join(s.upper() for s in stations)}): {len(items)} active"
    if not keys:
        return [head + "."]
    if not flagged:
        return [head + ", none unusual."]
    return [head + f", {len(flagged)} unusual:", *flagged]


# ── SIGMETs — the aviation "unusual" flag ─────────────────────────────

#: ICAO SIGMET hazard codes → spoken words (not a telegram decoder — the
#: AWC feed is already JSON; this only names the hazard class for audio).
_SIGMET_HAZARDS = {
    "VA": "volcanic ash",
    "TS": "thunderstorms",
    "TSGR": "thunderstorms with hail",
    "TURB": "severe turbulence",
    "ICE": "severe icing",
    "FZRA": "freezing rain",
    "MTW": "mountain waves",
    "DS": "dust storms",
    "SS": "sand storms",
    "TC": "tropical cyclones",
    "SQL": "squall-line thunderstorms",
    "RDOACT": "a radioactive cloud",
}

_COMPASS = {
    "N": "north",
    "NNE": "north-northeast",
    "NE": "northeast",
    "ENE": "east-northeast",
    "E": "east",
    "ESE": "east-southeast",
    "SE": "southeast",
    "SSE": "south-southeast",
    "S": "south",
    "SSW": "south-southwest",
    "SW": "southwest",
    "WSW": "west-southwest",
    "W": "west",
    "WNW": "west-northwest",
    "NW": "northwest",
    "NNW": "north-northwest",
}


def _zulu(epoch: Any) -> str:
    """Epoch seconds → ``HH:MM Zulu``; empty when the field is missing."""
    try:
        return datetime.fromtimestamp(int(epoch), tz=UTC).strftime("%H:%M Zulu")
    except (TypeError, ValueError, OSError):
        return ""


def _sigmet_spoken(sig: dict[str, Any]) -> str:
    """One spoken line for an international SIGMET JSON record."""
    hazard = _SIGMET_HAZARDS.get(
        str(sig.get("hazard") or "").upper(), "hazardous weather"
    )
    qualifier = str(sig.get("qualifier") or "").strip()
    fir = str(sig.get("firName") or sig.get("firId") or "").strip().title()
    bits = [f"{fir or 'Unknown FIR'} SIGMET {sig.get('seriesId') or ''}".strip()]
    detail = hazard + (f" ({qualifier.lower()})" if qualifier else "")
    top = sig.get("top")
    if top:
        base = int(sig.get("base") or 0)
        detail += (
            f", up to flight level {int(top) // 100}"
            if base <= 0
            else f", flight levels {base // 100} to {int(top) // 100}"
        )
    direction = _COMPASS.get(str(sig.get("dir") or "").upper())
    if direction and sig.get("spd"):
        detail += f", moving {direction} at {sig['spd']} knots"
    until = _zulu(sig.get("validTimeTo"))
    if until:
        detail += f", valid until {until}"
    bits.append(detail)
    return ": ".join(bits) + "."


def _sigmet_lines(fetch: Fetch, firs: list[str]) -> list[str]:
    import json

    raw = json.loads(fetch(ISIGMET_URL, {"format": "json"}))
    wanted = {f.upper() for f in firs}
    hits = [
        s
        for s in (raw if isinstance(raw, list) else [])
        if str(s.get("firId") or "").upper() in wanted
    ]
    names = ", ".join(firs)
    if not hits:
        return [f"No active SIGMETs for {names}."]
    return [f"SIGMETs for {names}:", *("- " + _sigmet_spoken(s) for s in hits)]


# ── marine: Met Éireann CAP warnings, keyword-selected ────────────────

_TAG_RE = re.compile(r"(?is)<(script|style)\b.*?</\1>|<[^>]+>")
_WS_RE = re.compile(r"\s+")


def _plain(raw: str) -> str:
    """HTML → one-line visible text (the warnings API embeds ``<p>`` markup)."""
    return _WS_RE.sub(" ", _html.unescape(_TAG_RE.sub(" ", raw or ""))).strip()


def _norm(text: str) -> str:
    """Case/punctuation-folded text for keyword matching — ``small-craft``
    must match the ``small craft`` keyword."""
    return re.sub(r"[-_]+", " ", (text or "").lower())


def _iso_utc(value: Any) -> str:
    """An ISO timestamp (any offset) → ``HH:MM UTC``; empty when absent."""
    try:
        ts = datetime.fromisoformat(str(value))
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=UTC)
        return ts.astimezone(UTC).strftime("%H:%M UTC")
    except (TypeError, ValueError):
        return ""


def _marine_lines(fetch: Fetch, keywords: list[str], now: datetime) -> list[str]:
    import json

    raw = json.loads(fetch(MARINE_WARNINGS_URL, {}))
    warnings = raw if isinstance(raw, list) else []
    keys = [_norm(k) for k in keywords]
    out: list[str] = []
    for w in warnings:
        if not isinstance(w, dict):
            continue
        haystack = _norm(f"{w.get('type') or ''} {w.get('headline') or ''}")
        if not any(k in haystack for k in keys):
            continue
        expiry = _iso_utc(w.get("expiry"))
        try:
            exp_dt = datetime.fromisoformat(str(w.get("expiry")))
            if exp_dt.tzinfo is None:
                exp_dt = exp_dt.replace(tzinfo=UTC)
            if exp_dt < now:
                continue  # already lapsed — the feed keeps them a while
        except (TypeError, ValueError):
            pass
        body = _plain(str(w.get("description") or ""))
        line = _plain(str(w.get("headline") or "")) or str(w.get("type") or "warning")
        if body:
            line += f". {body}"
        if expiry:
            line += f" In effect until {expiry}."
        out.append("- " + line.rstrip(".") + ".")
    if not out:
        return ["No active marine warnings."]
    return ["Marine warnings (Met Éireann):", *out]


# ── assembly ──────────────────────────────────────────────────────────


def weather_section(
    *,
    stations: str | Sequence[str] | None = None,
    firs: str | Sequence[str] | None = None,
    notam_keywords: str | Sequence[str] | None = None,
    marine_keywords: str | Sequence[str] | None = None,
    fetch: Fetch | None = None,
    now: datetime | None = None,
) -> str:
    """The briefing's ``## Weather`` section; ``""`` when nothing is
    configured or every block came back empty.

    Per-feed failures degrade to a one-line note (the brief must never
    fail on weather). ``fetch`` is injectable for tests; the default goes
    through ``polite_safe_get``.
    """
    do_fetch = fetch or _default_fetch
    now = now or datetime.now(UTC)
    lines: list[str] = []

    stations_ = _csv(stations)
    if stations_:
        try:
            lines.extend(
                _aviation_block(do_fetch, stations_, _csv(firs), _csv(notam_keywords))
            )
        except Exception:
            log.warning("weather: aviation fetch failed", exc_info=True)
            lines.append("Aviation weather unavailable right now.")

    marine_keys = _csv(marine_keywords)
    if marine_keys:
        try:
            lines.extend(_marine_lines(do_fetch, marine_keys, now))
        except Exception:
            log.warning("weather: marine fetch failed", exc_info=True)
            lines.append("Marine warnings unavailable right now.")

    if not lines:
        return ""
    return "## Weather\n\n" + "\n\n".join(lines)


__all__ = [
    "ISIGMET_URL",
    "MARINE_WARNINGS_URL",
    "METAR_URL",
    "TAF_URL",
    "weather_section",
]
