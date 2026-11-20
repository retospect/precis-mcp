"""Shared tmux-pane parsing for the fleet drivers — one parser per agent TUI.

Pure text in, facts out: callers feed ``tmux capture-pane -p`` output (with
``-e`` where escapes matter); nothing here runs tmux. Used by
``scripts/fleet-panes`` (``fleet panes …``) and ``scripts/fleet-wave``, which
previously carried its own copy of the dialog detector. The pane-hash /
quiet-age state machine from /tmp/gripe-fleet/stuck-check.sh lives in
``scripts/fleet-panes``, not here — this module never touches disk.

States reported: ``working | idle | prompt | error | done | frozen``.
``frozen`` is assigned by the caller (pane tail unchanged past its
threshold); ``done`` means the agent process exited (the window shows a
shell again) or, for Codex, its turn banner is the last meaningful line.

Layout samples live in tests/fixtures/panes/ (scrubbed captures, 2026-10-19).
"""

from __future__ import annotations

import re
import shlex
from dataclasses import dataclass, field

# --- shared regexes ----------------------------------------------------------

ANSI_RE = re.compile(r"\x1b\[[0-9;?]*[A-Za-z]")
#: A dim SGR span (Claude Code's placeholder hint in an empty input box).
DIM_SPAN_RE = re.compile(r"\x1b\[(?:[0-9;]*;)?2m.*?(?=\x1b\[(?:0|22)?m|$)")
BOX_RE = re.compile(r"^[\s│┃║╭╮╰╯─━═┌┐└┘├┤┬┴┼]*$")
SIDE_RE = re.compile(r"^[\s│┃║]+|[\s│┃║]+$")
#: Dialog markers (same set scripts/fleet's bash dialog_re keys on).
DIALOG_RE = re.compile(r"Enter to select|Do you want to|Do you trust the files")
#: A dialog's first option line: ``❯ 1. Yes`` / ``  1. Yes`` / ``› 1. Skip``.
OPTION_RE = re.compile(r"^[\s│┃║]*[❯›❭]?\s*1\.\s")
PROMPT_CHARS = "❯❭›"
#: Kimi's input box leads with a bare ``>`` — too common in scrollback
#: for the shared set (a ``> quote`` inside a Claude/Codex transcript is
#: output, not a box), so it is kimi-scoped and bounded to the pane's
#: bottom lines: the box is pinned just above the status line and the
#: bottom-most prompt line wins (gr651178).
_KIMI_BOX_RE = re.compile(r"^>(?:\s|$)")
_INPUT_BOX_DEPTH = 8
#: Spinners: Claude "… (12s · esc to interrupt)", Devin "Running tools · 3m
#: (esc twice to interrupt)", Codex "Working (8s • esc to interrupt)".
BUSY_RE = re.compile(r"… \(\d+|esc (twice )?to interrupt|• Working \(\d")
ERROR_RE = re.compile(
    r"usage limit|rate limit|API Error|overloaded|Traceback|permission denied"
    r"|Connection refused|context window|unknown command|quota.*exhausted",
    re.I,
)
#: Codex request_user_input modal (fleet-report sampled it live 2026-10-09).
CODEX_QUESTION_RE = re.compile(
    r"Question \d+/\d+ \(\d+ unanswered\)|enter to submit answer"
)
CODEX_PERMISSION_RE = re.compile(
    r"Would you like to run|Do you want to allow|Approve (this|once)",
    re.I,
)

# --- context fill ------------------------------------------------------------


def ctx_percent(pane: str) -> int | None:
    """Context fill % used, any agent; the bottom-most footer wins.

    Devin ``Context: 212k / 262k tokens (81%)``, Claude ``Opus 5.5 19% ctx``
    (both % used), Codex ``Context 87% left`` (% left → inverted). Footer
    text of another agent shown higher up (a fixture on screen) loses to
    the real status line at the bottom.
    """
    best: tuple[int, float] | None = None
    for pat, used in (
        (r"Context:[^\n()]*\((\d+)%\)", True),
        (r"(\d+)%\s*ctx\b", True),
        (r"Context\s+(\d+)%\s*left", False),
    ):
        for m in re.finditer(pat, pane, re.I):
            if best is None or m.start() >= best[0]:
                pct = float(m.group(1))
                best = (m.start(), pct if used else 100 - pct)
    return None if best is None else round(best[1])


#: Absolute context sizes panes print. A ``N% ctx`` footer cannot answer
#: "~120k tokens?" — the window is unknown — so the peer-loop compact
#: threshold (gr515153) reads these instead: Devin ``Context: 200k / 262k
#: tokens (76%)``, Claude's post-turn ``new task? /clear to save 196.5k
#: tokens`` and its mid-turn spinner ``(47s · esc to interrupt · 88.2k
#: tokens)``. Bottom-most wins, same rule as ctx_percent.
_CTX_TOKEN_RES = (
    re.compile(r"Context:\s*(\d+(?:\.\d+)?)\s*([kKmM])\s*/"),
    re.compile(r"/clear to save\s+(\d+(?:\.\d+)?)\s*([kKmM])\s+tokens"),
    re.compile(r"·\s*(\d+(?:\.\d+)?)\s*([kKmM])\s+tokens"),
)


def ctx_tokens(pane: str) -> int | None:
    """Context fill in tokens, any agent; ``None`` when the pane prints no
    absolute size (a bare ``N% ctx`` footer is unjudgeable by a token
    threshold — leave it alone rather than guess the window)."""
    best: tuple[int, float] | None = None
    for pat in _CTX_TOKEN_RES:
        for m in re.finditer(pat, pane):
            if best is None or m.start() >= best[0]:
                mult = 1_000 if m.group(2).lower() == "k" else 1_000_000
                best = (m.start(), float(m.group(1)) * mult)
    return None if best is None else round(best[1])


def nonempty_tail(pane: str, n: int) -> list[str]:
    return [ln for ln in pane.splitlines() if ln.strip()][-n:]


# --- dialogs ----------------------------------------------------------------


def _dialog_tail(pane: str) -> list[str]:
    """The last 25 non-empty lines, cut below the last bare input line."""
    tail = nonempty_tail(pane, 25)
    for k in range(len(tail) - 1, -1, -1):
        bare = SIDE_RE.sub("", tail[k])
        if bare.startswith("❯") and not OPTION_RE.match(tail[k]):
            tail = tail[k + 1 :]
            break
    return tail


def dialog_type(pane: str) -> str | None:
    """permission / trust / question for a pane at a dialog, else None.

    A marker needs a numbered option line after it: a marker string merely
    printed in scrollback (a diff, a file) is not a dialog. Codex's update
    and request_user_input modals count as ``question``."""
    tail = _dialog_tail(pane)
    for i, ln in enumerate(tail):
        if not any(OPTION_RE.match(a) for a in tail[i + 1 :]):
            continue
        if "Do you trust the files" in ln:
            return "trust"
        if "Do you want to" in ln or CODEX_PERMISSION_RE.search(ln):
            return "permission"
    if any("Enter to select" in ln for ln in tail[-3:]) and any(
        OPTION_RE.match(ln) for ln in tail
    ):
        return "question"
    if any(CODEX_QUESTION_RE.search(ln) for ln in tail):
        return "question"
    # Codex option modal ("› 1. …" + an "enter … esc" footer).
    if any(OPTION_RE.match(ln) for ln in tail) and re.search(
        r"enter (continue|to select|confirm)", "\n".join(tail[-3:]), re.I
    ):
        return "question"
    return None


def dialog_command(pane: str) -> str:
    """The command a dialog asks about: the nearest non-box line above the
    first marker within the 30 lines before it."""
    lines = pane.splitlines()
    idx = [i for i, ln in enumerate(lines) if DIALOG_RE.search(ln)]
    if not idx:
        return ""
    last = idx[-1]
    window = lines[max(0, last - 30) : last + 1]
    first = next(i for i, ln in enumerate(window) if DIALOG_RE.search(ln))
    # A tool dialog shows the command in `│`-prefixed lines under its header
    # ("Bash command"); the hook's own reason lines are not the command.
    boxed = [
        SIDE_RE.sub("", ln)
        for ln in window[:first]
        if ln.lstrip().startswith("│") and "requires confirmation" not in ln
    ]
    if boxed and boxed[0]:
        return boxed[0][:100]
    above = [
        SIDE_RE.sub("", ln)
        for ln in window[:first]
        if ln.strip() and not BOX_RE.match(ln)
    ]
    above = [a for a in above if a and not re.match(r"^(❯\s*)?\d+\.", a)]
    return above[-1][:100] if above else ""


def dialog_options(pane: str) -> list[str]:
    """The numbered option lines of the open dialog, side glyphs stripped."""
    tail = _dialog_tail(pane)
    start = max(
        (i for i, ln in enumerate(tail) if DIALOG_RE.search(ln)),
        default=0,
    )
    out = []
    for ln in tail[start:]:
        bare = SIDE_RE.sub("", ln).strip()
        if re.match(r"^[❯›❭]?\s*\d+\.", bare):
            out.append(bare[:100])
    return out


def input_box_prompt(stripped: str, agent: str = "") -> bool:
    """A side-stripped line leads with the agent's input prompt char."""
    if not stripped:
        return False
    return stripped[0] in PROMPT_CHARS or (
        agent == "kimi" and _KIMI_BOX_RE.match(stripped) is not None
    )


def input_box_visible(pane: str, agent: str = "") -> bool:
    """The agent's input box is drawn: a prompt-char line in the tail."""
    for raw in pane.splitlines()[-_INPUT_BOX_DEPTH:]:
        stripped = SIDE_RE.sub("", ANSI_RE.sub("", raw))
        if input_box_prompt(stripped, agent):
            return True
    return False


def unsent_input(pane_with_escapes: str, agent: str = "") -> str:
    """Text typed into the input box but not sent: what follows the last
    ``❯``/``›`` line's prompt char, dim placeholders and ANSI removed.
    ``agent`` widens the prompt set — a kimi pane's box is a bare ``>``,
    matched only in the bottom lines so ``> `` scrollback is not input."""
    lines = pane_with_escapes.splitlines()
    floor = len(lines) - _INPUT_BOX_DEPTH
    for i in range(len(lines) - 1, -1, -1):
        raw = lines[i]
        plain = ANSI_RE.sub("", raw)
        stripped = SIDE_RE.sub("", plain)
        if not stripped:
            continue
        if stripped[0] not in PROMPT_CHARS and not (
            agent == "kimi" and i >= floor and _KIMI_BOX_RE.match(stripped) is not None
        ):
            continue
        tail = raw[raw.index(stripped[0]) + 1 :]
        tail = ANSI_RE.sub("", DIM_SPAN_RE.sub("", tail))
        tail = SIDE_RE.sub("", tail.replace("\xa0", " ")).strip()
        # The agents' empty-box placeholders are plain text, not dim spans.
        if re.match(
            r"Ask Codex to do anything|Press Enter to send"
            r"|Guide Devin while|Ask Devin to build",
            tail,
        ):
            return ""
        return tail
    return ""


#: Mid-compaction banner. Devin shows "Compacting conversation history
#: with N messages and M tokens", Claude "Compacting conversation". An
#: Enter sent while it is up is swallowed — the text sits in the input
#: box unsent (gr515698) — so send helpers wait it out before typing.
COMPACT_RE = re.compile(r"\bCompacting\b")


def compacting(pane: str) -> bool:
    """True while the pane tail shows a compaction in progress."""
    return any(COMPACT_RE.search(ln) for ln in nonempty_tail(pane, 15))


# --- agent banners -------------------------------------------------------------

#: An agent's quota-exhausted banner. Devin shows "▲ Quota exhausted /
#: Your weekly usage quota has been exhausted." (fixture devin-quota.txt).
QUOTA_RE = re.compile(
    r"quota\b[^\n]{0,40}exhausted|usage quota has been exhausted", re.I
)


def quota_banner(pane: str) -> bool:
    """True when the pane tail carries a quota-exhausted banner."""
    return bool(QUOTA_RE.search("\n".join(nonempty_tail(pane, 15))))


# --- kimi subscription limits -------------------------------------------------
# Wording mirrors src/precis/utils/llm/kimi.py classify_failure: the
# concurrent-request limit is risk control (the plan's enforcement
# signal), a usage limit is the account window spent.

#: A real 429 line — "status code: 429" or the CLI's "You've reached your
#: …" lead-in. Without the anchor a wording match is only a scrollback
#: mention (a regex source line in a kimi.py diff).
_KIMI_ANCHOR_RE = re.compile(r"reached your|status code: 429", re.I)
_KIMI_CONCURRENT_RE = re.compile(r"concurrent request limit|too many requests", re.I)
_KIMI_QUOTA_RE = re.compile(r"usage limit|monthly quota", re.I)


def kimi_limit(pane: str) -> str:
    """Kimi's subscription-limit wording in the pane tail: ``concurrent``
    for the risk-control 429 (``concurrent request limit`` or ``too many
    requests`` after a ``status code: 429``), ``quota`` for an account
    usage limit (``usage limit``/``monthly quota``), else ``""``.

    The class comes from the last anchored line onward, so a 429 whose
    detail arrives on the next line still classifies.
    """
    tail = nonempty_tail(pane, 15)
    last = -1
    for i, ln in enumerate(tail):
        if _KIMI_ANCHOR_RE.search(ln):
            last = i
    if last < 0:
        return ""
    text = "\n".join(tail[last:])
    if _KIMI_CONCURRENT_RE.search(text):
        return "concurrent"
    if _KIMI_QUOTA_RE.search(text):
        return "quota"
    return ""


#: Devin's status footer leads with the model ("SWE-2 High   Context: …").
_DEVIN_MODEL_RE = re.compile(r"^\s*(SWE-[\w.]+(?:[ -]High)?)", re.I)


def devin_model(pane: str) -> str:
    """Devin's footer model normalised to an id (``SWE-2 High`` →
    ``swe-2-high``); ``""`` when no footer line is in the tail. Only the
    last lines are searched — a scrollback mention is not the model."""
    for ln in reversed(nonempty_tail(pane, 10)):
        m = _DEVIN_MODEL_RE.match(ln)
        if m:
            return re.sub(r"[ -]+", "-", m.group(1).lower())
    return ""


# --- chrome ------------------------------------------------------------------
# Lines dropped by `read` and ignored for "last meaningful line". Agent text
# (⏺/• blocks, diffs, the spinner) stays.

RULE_RE = re.compile(r"^[\s─━═]{8,}$|^[\s─━═]+[^(]*\([^)]*\)[\s─━═]*$")
CHROME_RES = [
    RULE_RE,  # box rules, incl. "── (bypass permissions on) ──"
    re.compile(r"^\s*[❯›❭]\s*$"),  # empty input box
    re.compile(
        r"[❯›❭]\s*(Ask Codex to do anything|Guide Devin while it works"
        r"|Ask Devin to build features)"
    ),
    re.compile(r"Press Enter to send queued messages"),
    re.compile(
        r"-- INSERT --|⏵⏵|shift\+tab to cycle|accept edits on"
        r"|bypass permissions on|plan mode on"
    ),
    re.compile(
        r"← for agents|\? for shortcuts|⚠ \d+ warnings|↓ select"
        r"|↑ edit · ↵ send now|^f2 to view"
    ),
    re.compile(r"^\s*○ "),  # Devin's queued-message list
    re.compile(r"Context:.*\(\d+%\)|\d+% ctx\b|Context \d+% left"),
    re.compile(r"^[A-Z][A-Za-z]+( [0-9.]+)?\s{2,}"),  # model name footer
    re.compile(r"^\s*SWE-2"),  # Devin's model/hint footer line
    re.compile(r"^\s*[a-zA-Z0-9._-]+ · ~?/"),  # codex "dir · branch" footer
]


def is_chrome(line: str) -> bool:
    if BOX_RE.match(line):
        return True
    return any(re.search(p, line) for p in CHROME_RES)


def strip_chrome(pane: str) -> list[str]:
    """Content lines only: side box glyphs stripped, rules/footers dropped."""
    out = []
    for ln in pane.splitlines():
        if is_chrome(ln):
            continue
        text = SIDE_RE.sub("", ln).rstrip()
        if text.strip():
            out.append(text)
    return out


def last_meaningful(pane: str, width: int = 80) -> str:
    lines = strip_chrome(pane)
    return lines[-1][:width] if lines else ""


# --- per-agent state ---------------------------------------------------------


def detect_agent(command: str, window_name: str = "") -> str:
    """Agent kind from the pane's current command, else the window prefix.

    A ``node`` command means Claude only as a last resort: a kimi window
    whose binary reports ``node`` is still a kimi pane."""
    c = command.lower()
    for agent in ("claude", "codex", "devin", "kimi"):
        if agent in c:
            return agent
    for p in ("claude-", "devin-", "codex-", "kimi-"):
        if window_name.startswith(p):
            return p[:-1]
    if c in ("node",):
        return "claude"
    return "other"


def permission_mode(agent: str, pane: str) -> str:
    """The TUI's advertised permission mode; ``?`` when the pane hides it."""
    if agent == "devin":
        return "bypass" if "bypass permissions on" in pane else "auto"
    if agent == "claude":
        if "⏵⏵ auto mode on" in pane or "auto mode on" in pane:
            return "auto"
        if "bypass permissions on" in pane:
            return "bypass"
        if "plan mode on" in pane:
            return "plan"
        if "accept edits on" in pane:
            return "accept-edits"
        return "?"
    return "?"


@dataclass
class PaneReport:
    agent: str
    state: str = "idle"
    permission: str = "?"
    ctx: int | None = None
    dialog: str | None = None
    command: str = ""
    options: list[str] = field(default_factory=list)
    queued: int = 0
    last: str = ""


def pane_report(
    agent: str,
    pane: str,
    *,
    command_running: bool = True,
    frozen: bool = False,
) -> PaneReport:
    """One report for one pane. ``command_running`` is tmux's
    ``pane_dead == 0`` for the window's agent process."""
    rep = PaneReport(agent=agent)
    rep.ctx = ctx_percent(pane)
    rep.permission = permission_mode(agent, pane)
    rep.dialog = dialog_type(pane)
    if rep.dialog:
        rep.command = dialog_command(pane)
        rep.options = dialog_options(pane)
    rep.queued = len([ln for ln in pane.splitlines() if re.match(r"^\s*○ ", ln)])
    rep.last = last_meaningful(pane)
    busy = bool(BUSY_RE.search(pane))
    if rep.dialog:
        rep.state = "prompt"
    elif ERROR_RE.search("\n".join(nonempty_tail(pane, 12))) or (
        # Gated on the agent: a Claude pane editing kimi.py shows the
        # same words in scrollback and must not trip the limit class.
        agent == "kimi" and kimi_limit(pane)
    ):
        rep.state = "error"
    elif not command_running:
        rep.state = "done"
    elif frozen:
        rep.state = "frozen"
    elif busy:
        rep.state = "working"
    elif agent == "codex" and re.search(r"Worked for \d|Done for \d", rep.last):
        rep.state = "idle"
    else:
        rep.state = "idle"
    return rep


# --- answer-once guard -------------------------------------------------------

READ_ONLY_BIN = {"cat", "head", "tail", "grep", "rg", "ls", "wc", "pwd"}
READ_ONLY_GIT = {"log", "show", "diff", "status"}
MUTATING_FIND = {
    "-delete",
    "-exec",
    "-execdir",
    "-ok",
    "-okdir",
    "-fprint",
    "-fprintf",
}


def _stage_read_only(argv: list[str]) -> str | None:
    """Refusal reason for one pipeline stage, or None when clearly read-only."""
    if not argv:
        return "empty command"
    head, rest = argv[0], argv[1:]
    if head in {"sudo", "doas"}:
        return "runs via sudo"
    if head in READ_ONLY_BIN:
        return None
    if head == "sed":
        if "-n" not in rest:
            return "sed without -n"
        if any(a in ("-i", "--in-place") or a.startswith("-i") for a in rest):
            return "sed -i edits in place"
        return None
    if head == "find":
        if set(rest) & MUTATING_FIND:
            return "find with -delete/-exec/-fprint"
        return None
    if head == "git":
        if len(rest) >= 1 and rest[0] in READ_ONLY_GIT:
            return None
        return "git subcommand not in log/show/diff/status"
    return f"{head!r} is not on the read-only list"


def read_only_reason(command: str) -> str | None:
    """None when ``command`` is clearly read-only, else the refusal reason.

    Every pipeline stage must clear it; redirections and substitutions are
    refused outright regardless of the binaries named."""
    if re.search(r"[>`$`]|(\$\()", command):
        return "has redirection or substitution"
    try:
        words = shlex.split(command)
    except ValueError:
        return "unparseable"
    for stage in re.split(r"\|\||&&|;|\|", command):
        try:
            argv = shlex.split(stage)
        except ValueError:
            return "unparseable"
        if why := _stage_read_only(argv):
            return why
    return None if words else "empty command"


def answerable_once(pane: str) -> str | None:
    """None when the open dialog may be answered once, else the reason.

    Requires a permission dialog whose first option is a single-shot
    approval ("1. Yes", "1. Allow once"); "don't ask"/"always" options and
    non-approval dialogs refuse."""
    if dialog_type(pane) != "permission":
        return "no permission dialog"
    opts = dialog_options(pane)
    first = next((o for o in opts if re.match(r"^[❯›❭]?\s*1\.", o)), "")
    text = re.sub(r"^[❯›❭]?\s*1\.\s*", "", first).strip().lower()
    if not re.match(r"(yes|allow|proceed|approve)\b", text):
        return f"first option is not an approval: {first or 'none'}"
    if re.search(r"don't ask|always|trust|forever|all tools", text):
        return f"first option widens scope: {first}"
    return None


# --- launch policy -----------------------------------------------------------
#: Permission mode each agent is launched with by scripts/fleet-wave — the one
#: place wave launch policy lives (Devin bypass: Reto ruling me499092
#: "always bypass" for builder waves, 2026-10-12).
LAUNCH_PERMISSION_MODE = {"claude": "auto", "devin": "dangerous", "kimi": "auto"}
