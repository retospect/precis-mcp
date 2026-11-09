import ast
import json
import re
import statistics
import sys
from collections import Counter, defaultdict

BS = chr(92)
DQ = chr(34)
SQ = chr(39)
OPEN = "([" + chr(123)
CLOSE = ")]" + chr(125)
CALL_RE = re.compile(r"(get|search|put|edit|delete|tag|link)[(](.*)[)]" + r"\s*", re.S)


def split_top(s):
    parts, depth, q, cur = [], 0, None, []
    i = 0
    while i < len(s):
        ch = s[i]
        if q:
            cur.append(ch)
            if ch == BS and i + 1 < len(s):
                cur.append(s[i + 1])
                i += 1
            elif ch == q:
                q = None
        elif ch in (SQ, DQ):
            q = ch
            cur.append(ch)
        elif ch in OPEN:
            depth += 1
            cur.append(ch)
        elif ch in CLOSE:
            depth -= 1
            cur.append(ch)
        elif ch == "," and depth == 0:
            parts.append("".join(cur))
            cur = []
        else:
            cur.append(ch)
        i += 1
    if cur:
        parts.append("".join(cur))
    return [p.strip() for p in parts if p.strip()]


def parse_hint(line):
    m = CALL_RE.fullmatch(line.strip())
    if not m:
        return None
    verb, argstr = m.group(1), m.group(2)
    args = dict()
    for p in split_top(argstr):
        if "=" not in p:
            continue
        k, v = p.split("=", 1)
        k = k.strip()
        if not re.fullmatch(r"[a-z_]+", k):
            continue
        try:
            args[k] = ast.literal_eval(v.strip())
        except Exception:
            args[k] = ...
    return dict(verb=verb, args=args)


def result_text(c):
    if isinstance(c, str):
        return c
    if isinstance(c, list):
        return chr(10).join(x.get("text", "") for x in c if isinstance(x, dict))
    return ""


streams = defaultdict(list)  # (ref, parent) -> list of call dicts in order
results = dict()  # tool_use_id -> (text, is_error)
nlines = 0
with open(sys.argv[1], encoding="utf-8") as fh:
    lines = fh.readlines()
for raw in lines:
    parts = raw.rstrip(chr(10)).split("|", 2)
    if len(parts) != 3 or not parts[2].startswith(chr(123)):
        continue
    nlines += 1
    try:
        o = json.loads(parts[2])
    except Exception:
        continue
    ref = parts[0]
    msg = o.get("message") or dict()
    content = msg.get("content")
    if not isinstance(content, list):
        continue
    parent = o.get("parent_tool_use_id")
    for b in content:
        if not isinstance(b, dict):
            continue
        if b.get("type") == "tool_use" and str(b.get("name", "")).startswith(
            "mcp__precis__"
        ):
            verb = b["name"].split("__")[-1]
            inp = b.get("input") or dict()
            streams[(ref, parent)].append(
                dict(
                    id=b["id"],
                    verb=verb,
                    kind=inp.get("kind"),
                    view=inp.get("view"),
                    keys=set(inp),
                    inp=inp,
                )
            )
        elif b.get("type") == "tool_result":
            t = result_text(b.get("content"))
            results[b.get("tool_use_id")] = (
                t,
                bool(b.get("is_error")) or t.lstrip().startswith("[error:"),
            )


def is_err(call):
    r = results.get(call["id"])
    return None if r is None else r[1]


def matches(hint, call, level):
    if call["verb"] != hint["verb"] or (call["kind"] or None) != (
        hint["args"].get("kind") or None
    ):
        return False
    if level == 1:
        return True
    hv = hint["args"].get("view")
    if hv is not None and hv is not ... and call["view"] != hv:
        return False
    hk = set(hint["args"])
    if not hk <= call["keys"]:
        return False
    if level == 2:
        return True
    for k, v in hint["args"].items():
        if v is ... or k in ("id", "q"):
            continue
        if call["inp"].get(k) != v:
            return False
    return True


stat = Counter()
rec_attempts = []
by_cls = defaultdict(Counter)
for key, calls in streams.items():
    for i, c in enumerate(calls):
        if is_err(c) is not True:
            continue
        txt = results[c["id"]][0]
        stat["errors"] += 1
        m = re.match(r"\s*\[error:([A-Za-z]+)\]", txt)
        et = m.group(1) if m else "Unknown"
        nxt = [
            l.split("next:", 1)[1].strip()
            for l in txt.splitlines()
            if l.strip().startswith("next:")
        ]
        hints = [h for h in (parse_hint(n) for n in nxt) if h]
        has_call = bool(hints)
        stat["with_next_line"] += bool(nxt)
        stat["with_callable_hint"] += has_call
        following = calls[i + 1 : i + 4]
        same = [
            x
            for x in calls[i + 1 :]
            if x["verb"] == c["verb"] and x["kind"] == c["kind"]
        ]
        for n, f in enumerate(same, 1):
            if is_err(f) is False:
                rec_attempts.append(n)
                break
        if not following:
            stat["no_following_call"] += 1
            continue
        f1 = following[0]
        grp = "hint" if has_call else "nohint"
        stat[grp + "_n"] += 1
        sv = f1["verb"] == c["verb"] and f1["kind"] == c["kind"]
        stat[grp + "_next_same_vk"] += sv
        stat[grp + "_next_same_vk_ok"] += sv and is_err(f1) is False
        by_cls[et][grp + "_n"] += 1
        if has_call:
            for lvl in (1, 2, 3):
                hit1 = any(matches(h, f1, lvl) for h in hints)
                stat[f"L{lvl}_next_match"] += hit1
                stat[f"L{lvl}_next_match_ok"] += hit1 and is_err(f1) is False
                stat[f"L{lvl}_within3_match"] += any(
                    matches(h, f, lvl) for h in hints for f in following
                )
                stat[f"L{lvl}_within3_match_ok"] += any(
                    matches(h, f, lvl) and is_err(f) is False
                    for h in hints
                    for f in following
                )
            by_cls[et]["L1_hit"] += any(matches(h, f1, 1) for h in hints)
            by_cls[et]["L2_hit"] += any(matches(h, f1, 2) for h in hints)
print(
    "event lines parsed:",
    nlines,
    "streams:",
    len(streams),
    "precis calls:",
    sum(len(v) for v in streams.values()),
)
print("jobs:", len(set(k[0] for k in streams)))
for k in sorted(stat):
    print(f"{k}\t{stat[k]}")
if rec_attempts:
    rec_attempts.sort()
    print(
        "recovered_errors",
        len(rec_attempts),
        "median_attempts",
        statistics.median(rec_attempts),
        "p90",
        rec_attempts[int(0.9 * (len(rec_attempts) - 1))],
    )
for et, c in sorted(by_cls.items()):
    print("cls", et, dict(c))

print("--- by hint class (an error counts in every class it carries a hint of) ---")


def hclass(h, c):
    if h["verb"] == "get" and h["args"].get("kind") == "skill":
        return "skill_read"
    if h["verb"] == c["verb"] and h["args"].get("kind") == c["kind"]:
        return "retry_same_verb_kind"
    return "other_call"


st3 = Counter()
for key, calls in streams.items():
    for i, c in enumerate(calls):
        if is_err(c) is not True:
            continue
        txt = results[c["id"]][0]
        nxt = [
            l.split("next:", 1)[1].strip()
            for l in txt.splitlines()
            if l.strip().startswith("next:")
        ]
        hints = [h for h in (parse_hint(n) for n in nxt) if h]
        following = calls[i + 1 : i + 4]
        if not following or not hints:
            continue
        f1 = following[0]
        for name in ("skill_read", "retry_same_verb_kind", "other_call"):
            hs = [h for h in hints if hclass(h, c) == name]
            if not hs:
                continue
            st3[name + "_n"] += 1
            st3[name + "_L1_next"] += any(matches(h, f1, 1) for h in hs)
            st3[name + "_L2_next"] += any(matches(h, f1, 2) for h in hs)
            st3[name + "_L2_next_ok"] += (
                any(matches(h, f1, 2) for h in hs) and is_err(f1) is False
            )
            st3[name + "_L2_within3"] += any(
                matches(h, f, 2) for h in hs for f in following
            )
            st3[name + "_L1_within3"] += any(
                matches(h, f, 1) for h in hs for f in following
            )
for k in sorted(st3):
    print(f"{k}\t{st3[k]}")
