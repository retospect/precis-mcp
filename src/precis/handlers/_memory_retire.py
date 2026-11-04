"""Retirement's prose receipt: one bounded live-body window.

The ref window and chunk-kind predicate exclude tombstones and other body
kinds before text is read. The server cursor retains only the capped receipt.
An overflowing window refuses even force: incomplete evidence cannot certify
that unlinked mentions were checked. No mention edges are consulted.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from psycopg import Connection

from precis.errors import BadInput
from precis.graph.garden.candidates import memory_handle_mentions

MENTION_REPORT_CAP = 25
MENTION_SCAN_CAP = 10000
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
    """One windowed query, streamed and capped; only live memory_body text.

    SQL's word boundaries are the prefilter for the shared Python detector
    predicate. Aggregating all body chunks preserves history-section context.
    Count one extra ref before scanning any body: if it exists, refuse rather
    than treating an unseen mention as absent. Raising the cap is an explicit
    capacity decision; force is a semantic override, never a scan bypass.
    """
    handle = f"me{ref_id}"
    mentions: list[Mention] = []
    total = blocking = 0
    with conn.cursor(name=f"memory_retire_{ref_id}") as cursor:
        cursor.execute(
            "WITH memory_window AS MATERIALIZED ("
            "SELECT ref_id FROM refs WHERE kind = 'memory' AND retired_at IS NULL "
            "AND ref_id <> %s ORDER BY ref_id LIMIT %s), "
            "size AS (SELECT count(*) AS n FROM memory_window), "
            "bodies AS (SELECT w.ref_id, "
            "string_agg(c.text, E'\\n' ORDER BY c.ord, c.chunk_id) AS body "
            "FROM memory_window w JOIN chunks c USING (ref_id) "
            "WHERE (SELECT n FROM size) <= %s "
            "AND c.chunk_kind = 'memory_body' AND c.ord >= 0 "
            "AND c.retired_at IS NULL GROUP BY w.ref_id "
            "HAVING bool_or(c.text ~* %s)) "
            "SELECT size.n, bodies.ref_id, bodies.body FROM size "
            "LEFT JOIN bodies ON true ORDER BY bodies.ref_id",
            (ref_id, MENTION_SCAN_CAP + 1, MENTION_SCAN_CAP, rf"\mme0*{ref_id}\M"),
        )
        for scanned, rid, body in cursor:
            if scanned > MENTION_SCAN_CAP:
                raise BadInput(
                    f"retirement scan exceeds {MENTION_SCAN_CAP} live memories; "
                    "nothing retired (force cannot bypass an incomplete scan)",
                    next="ask the service owner to increase the memory retirement "
                    "scan capacity or add an indexed handle lookup, then retry the dry run",
                )
            if rid is None:
                continue
            mention = _mention(int(rid), str(body), handle)
            if mention is None:
                continue
            total += 1
            blocking += not mention.historical
            if len(mentions) < MENTION_REPORT_CAP:
                mentions.append(mention)
    return MentionReport(mentions, total, blocking)
