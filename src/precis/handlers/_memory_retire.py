"""Retirement's prose receipt: one indexed live-body lookup.

The verbatim word index (``chunks.tsv``) nominates candidate refs; the
chunk-kind and ref predicates exclude tombstones and other body kinds
before text is read, and the shared Python detector verifies each
candidate's aggregated body. The server cursor retains only the capped
receipt. No mention edges are consulted.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from psycopg import Connection

from precis.graph.garden.candidates import memory_handle_mentions

MENTION_REPORT_CAP = 25
_HEADING = re.compile(r"^\s*(#{1,6})\s+(.+?)\s*#*\s*$")
_HISTORY = re.compile(r"^(?:Earlier|Retired here)(?:\s*[:—–-]|\s*$)", re.IGNORECASE)
_DATE = re.compile(r"^\s*\d{4}-\d{2}-\d{2}\b")
_CURRENT = re.compile(r"^\s*(?:Current|Now)\s*:", re.IGNORECASE)


@dataclass(frozen=True)
class Mention:
    ref_id: int
    sentence: str
    historical: bool
    nth: int | None = None


@dataclass(frozen=True)
class MentionReport:
    mentions: list[Mention]
    total: int
    blocking: int

    def render(self, handle: str, date: str, why: str) -> str:
        lines = [f"Also update: {self.total} mentioning memory node(s)."]
        for mention in self.mentions:
            label = " (dated history; exempt)" if mention.historical else ""
            lines.append(f"me{mention.ref_id}{label}: {mention.sentence}")
            reason = f"{handle} retired {date}: {why}"
            nth = f", match='nth', nth={mention.nth}" if mention.nth is not None else ""
            lines.append(
                f"edit(kind='memory', id='me{mention.ref_id}', "
                f"find={mention.sentence!r}{nth}, text='<write the current truth in>', "
                f"reason={reason!r})"
            )
        if self.total > len(self.mentions):
            lines.append(f"+{self.total - len(self.mentions)} more")
        return "\n".join(lines)


def _mention(ref_id: int, body: str, handle: str) -> Mention | None:
    """Choose a current sentence before history; keep the exact edit anchor.

    An Earlier/Retired here heading covers its subtree. Plain markers cover
    the following lines until a heading or Current:/Now: marker. Dates exempt
    only their own line. Wrapped sentences retain their newlines; repeated
    sentences carry nth rather than widening the edit to the entire body.
    """
    history_level: int | None = None
    historical: Mention | None = None
    boundaries = list(re.finditer(r"(?<=[.!?])\s+|\n[ \t]*\n", body))
    starts = [0, *(m.end() for m in boundaries)]
    ends = [*(m.start() for m in boundaries), len(body)]
    offset = 0
    lines: list[tuple[int, str]] = []
    for raw_line in body.splitlines(keepends=True):
        line = raw_line.rstrip("\r\n")
        lines.append((offset, line))
        if (
            _HEADING.match(line)
            or _HISTORY.match(line.strip().lstrip("*"))
            or _DATE.match(line)
        ):
            starts.extend((offset, offset + len(raw_line)))
            ends.extend((offset, offset + len(line)))
        offset += len(raw_line)
    for offset, line in lines:
        heading = _HEADING.match(line)
        if heading:
            level, title = len(heading[1]), heading[2]
            if history_level is not None and level <= history_level:
                history_level = None
            if _HISTORY.match(title):
                history_level = level
        elif _CURRENT.match(line):
            history_level = None
        elif _HISTORY.match(line.strip().lstrip("*")):
            history_level = 7
        matches = memory_handle_mentions(line, handle)
        if not matches:
            continue
        match = matches[0]
        start = max(p for p in starts if p <= offset + match.start())
        end = min(p for p in ends if p >= offset + match.end())
        sentence = body[start:end].strip()
        nth = body[:start].count(sentence) + 1 if body.count(sentence) > 1 else None
        is_history = history_level is not None or _DATE.match(line) is not None
        found = Mention(ref_id, sentence, is_history, nth)
        if not is_history:
            return found
        if historical is None:
            historical = found
    return historical


def scan_mentions(conn: Connection, ref_id: int) -> MentionReport:
    """One indexed query, streamed and capped; only live memory_body text.

    ``to_tsquery`` on the stored ``tsv`` nominates candidates — exact
    whole-lexeme hits for ``me<N>`` and its one- and two-leading-zero
    forms — so the scan is complete at any corpus size; the shared Python
    detector predicate verifies each candidate. Aggregating all live body
    chunks preserves history-section context. Known gap: a handle glued
    inside a dotted or slashed compound token (``docs/me<N>``,
    ``me<N>.md``, a URL path) is one lexeme and is never nominated.
    """
    handle = f"me{ref_id}"
    terms = " | ".join(f"me{'0' * zeros}{ref_id}" for zeros in range(3))
    mentions: list[Mention] = []
    total = blocking = 0
    with conn.cursor(name=f"memory_retire_{ref_id}") as cursor:
        cursor.execute(
            "WITH candidates AS ("
            "SELECT DISTINCT c.ref_id FROM chunks c "
            "JOIN refs r ON r.ref_id = c.ref_id "
            "WHERE c.tsv @@ to_tsquery('english', %s) "
            "AND c.chunk_kind = 'memory_body' AND c.ord >= 0 "
            "AND c.retired_at IS NULL "
            "AND r.kind = 'memory' AND r.retired_at IS NULL "
            "AND r.ref_id <> %s) "
            "SELECT w.ref_id, "
            "string_agg(c.text, E'\\n' ORDER BY c.ord, c.chunk_id) AS body "
            "FROM candidates w JOIN chunks c ON c.ref_id = w.ref_id "
            "WHERE c.chunk_kind = 'memory_body' AND c.ord >= 0 "
            "AND c.retired_at IS NULL "
            "GROUP BY w.ref_id ORDER BY w.ref_id",
            (terms, ref_id),
        )
        for rid, body in cursor:
            mention = _mention(int(rid), str(body), handle)
            if mention is None:
                continue
            total += 1
            blocking += not mention.historical
            if len(mentions) < MENTION_REPORT_CAP:
                mentions.append(mention)
    return MentionReport(mentions, total, blocking)
